"""Global Trading Control Center — 미국 주식 전략 통합 운영 대시보드.

전략 앱(Jones·Ross·Kell·MA20·60)은 독립적으로 분석·매매하고, 이 앱은 그 결과를 읽어 모니터링만 한다.
- 주문 권한 없음 · 증권사 주문 키 없음 · 전략 앱 코드/DB 변경 없음
- 이 서버가 멈춰도 Ross·Kell 자동매매는 영향 없음
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import os
import time
import traceback
from datetime import date
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse

from app import assess, db, report, sources, store, tcal

VERSION = "1.1.0"
DASH_PASSWORD = os.environ.get("DASH_PASSWORD", "")
SESSION_SECRET = os.environ.get("SESSION_SECRET", "tcc-dev-secret")
INTERVAL = int(os.environ.get("COLLECT_INTERVAL_S", "60"))
UI = Path(__file__).resolve().parent.parent / "ui"

app = FastAPI(title="Global Trading Control Center", docs_url=None, redoc_url=None)
SNAP: dict = {"ready": False}
STATE = {"last_run": 0.0, "running": False, "errors": [], "report_done": None, "lock": None}


# ───────────────────────── 인증
def _token() -> str:
    return hmac.new(SESSION_SECRET.encode(), DASH_PASSWORD.encode(), hashlib.sha256).hexdigest()


def _authed(req: Request) -> bool:
    return not DASH_PASSWORD or hmac.compare_digest(req.cookies.get("tcc_auth", ""), _token())


OPEN = {"/login", "/health"}


@app.middleware("http")
async def guard(req: Request, call_next):
    if req.url.path in OPEN or _authed(req):
        return await call_next(req)
    if req.url.path.startswith("/api/"):
        return JSONResponse({"code": "needs_login", "message": "로그인이 필요합니다."}, 401)
    return RedirectResponse("/login")


LOGIN = """<!DOCTYPE html><html lang=ko><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<title>로그인 · Global Trading Control Center</title><style>body{background:#0e1116;color:#e8ecf1;font:15px -apple-system,system-ui,sans-serif;display:grid;place-items:center;height:100vh;margin:0}
form{background:#161b22;border:1px solid #2a313c;padding:30px;border-radius:14px;display:flex;flex-direction:column;gap:12px;min-width:300px}
b{font-size:17px}small{color:#8b96a5}input{background:#0e1116;color:#e8ecf1;border:1px solid #2a313c;border-radius:8px;padding:11px;font-size:15px}
button{background:#3b82f6;color:#fff;border:0;border-radius:8px;padding:11px;font-weight:600;font-size:15px}.e{color:#f87171;font-size:13px}</style>
<form method=post action=/login><b>Global Trading Control Center</b><small>미국 주식 3대 전략 통합 운영 대시보드</small>
<input type=password name=pw placeholder="비밀번호 (Render 환경변수 DASH_PASSWORD)" autofocus>__E__<button>들어가기</button></form></html>"""


@app.get("/login", response_class=HTMLResponse)
async def login_page():
    return LOGIN.replace("__E__", "")


@app.post("/login")
async def login(req: Request):
    from urllib.parse import parse_qs
    pw = (parse_qs((await req.body()).decode()).get("pw") or [""])[0]
    if DASH_PASSWORD and hmac.compare_digest(pw, DASH_PASSWORD):
        r = RedirectResponse("/", status_code=303)
        r.set_cookie("tcc_auth", _token(), max_age=60 * 86400, httponly=True, secure=True, samesite="lax")
        return r
    return HTMLResponse(LOGIN.replace("__E__", "<span class=e>비밀번호가 맞지 않습니다.</span>"), 401)


@app.get("/logout")
async def logout():
    r = RedirectResponse("/login")
    r.delete_cookie("tcc_auth")
    return r


# ───────────────────────── 수집 주기
def _fallback(app_id: str, h: dict, err: str) -> dict:
    srv = assess.server_level(app_id, h)
    st = {"server": srv, "data": assess.lv("fail", f"DB 조회 실패: {err[:120]}"), "analysis": assess.lv("warn", "판정 불가"),
          "trade": assess.lv("na" if app_id == "JONES" else "warn", "판정 불가")}
    return {"app": app_id, "status": st, "overall": "fail", "issues": [assess.issue(app_id, "P2", "db_read", f"{app_id} 데이터 조회 실패", err[:200])],
            "headline": "데이터 조회 실패", "positions": [], "error": err}


async def collect():
    if STATE["running"]:
        return
    STATE["running"] = True
    t0 = time.time()
    errs = []
    try:
        ctx = assess.Ctx()
        health = await sources.probe_all()

        async def read(fn, name):
            try:
                return await asyncio.to_thread(fn), None
            except Exception as e:
                errs.append(f"{name}: {type(e).__name__}: {e}")
                return None, f"{type(e).__name__}: {e}"
        (J, ej), (R, er), (K, ek), (M, em) = await asyncio.gather(read(sources.jones, "jones"), read(sources.ross, "ross"),
                                                                   read(sources.kell, "kell"), read(sources.ma, "ma"))
        ma_book = {"paper": "paper", "approve": "live", "auto": "live"}.get(((M or {}).get("settings") or {}).get("mode"))
        acct = {}
        if any(os.environ.get(f"{k}_APP_PASSWORD") for k in ("ROSS", "KELL", "MA")):
            try:
                acct = await sources.accounts(ma_book=ma_book)
            except Exception as e:
                errs.append(f"accounts: {type(e).__name__}: {e}")

        def safe(fn, raw, err, app_id, *a):
            if raw is None:
                return _fallback(app_id, health.get(app_id), err or "no data")
            try:
                return fn(raw, health.get(app_id), ctx, *a)
            except Exception as e:
                errs.append(f"assess {app_id}: {traceback.format_exc()[-400:]}")
                return _fallback(app_id, health.get(app_id), f"판정 오류 {type(e).__name__}: {e}")
        aj = safe(assess.assess_jones, J, ej, "JONES")
        ar = safe(assess.assess_ross, R, er, "ROSS", acct)
        ak = safe(assess.assess_kell, K, ek, "KELL", acct)
        am = safe(assess.assess_ma, M, em, "MA", acct)
        risk = assess.assess_risk(aj, ar, ak, am, acct)
        issues = aj["issues"] + ar["issues"] + ak["issues"] + am["issues"] + risk["issues"]
        issues.sort(key=lambda i: (i["severity"], i["app"]))

        stored, resolved, ingest = [], [], {}
        try:
            ingest = await asyncio.to_thread(store.ingest)
            changes = await asyncio.to_thread(store.sync_issues, issues)
            await store.notify(changes)
            stored = await asyncio.to_thread(store.open_issues)
            resolved = await asyncio.to_thread(store.recent_resolved)
        except Exception as e:
            errs.append(f"store: {type(e).__name__}: {e}")
        st_by = {s["key"]: s for s in stored}
        for i in issues:
            s = st_by.get(i["key"])
            if s:
                i.update(state=s["state"], first_seen=assess.iso(s["first_seen"]), acked_at=assess.iso(s["acked_at"]),
                         notified=s["notified"], notify_err=s["notify_err"])
            else:
                i.setdefault("state", "open")

        snap = {"ready": True, "version": VERSION, "built_at": assess.iso(time.time()), "took_ms": int((time.time() - t0) * 1000),
                "ctx": ctx.info(), "apps": {k: {**{kk: v for kk, v in a.items() if kk != "url"}, "url": a["url"]} for k, a in sources.APPS.items()},
                "health": {k: {kk: vv for kk, vv in (v or {}).items() if kk != "body"} | {"body": (v or {}).get("body")} for k, v in health.items()},
                "jones": aj, "ross": ar, "kell": ak, "ma": am, "risk": risk, "issues": issues,
                "resolved": [{**r, "first_seen": assess.iso(r["first_seen"]), "resolved_at": assess.iso(r["resolved_at"]),
                              "acked_at": assess.iso(r["acked_at"])} for r in resolved],
                "ingest": ingest, "errors": errs, "ntfy": bool(store.NTFY_TOPIC),
                "accounts_linked": {k: bool(os.environ.get(f"{k}_APP_PASSWORD")) for k in ("ROSS", "KELL", "MA")}}
        SNAP.clear()
        SNAP.update(snap)
        await _maybe_report(ctx)
    except Exception:
        errs.append(traceback.format_exc()[-800:])
        SNAP["errors"] = errs
    finally:
        STATE.update(running=False, last_run=time.time(), errors=errs)


async def _maybe_report(ctx: assess.Ctx):
    """결과 확정 시각(마감+3h)부터 다음 거래일 04:00 ET 전까지만 보고서를 만든다(그 뒤엔 기준일이 바뀜)."""
    d = ctx.settled
    if not tcal.is_session(d) or ctx.basis != d or STATE["report_done"] == d:
        return
    try:
        old = await asyncio.to_thread(store.report, d)
        if old and old["status"] == "확정":
            STATE["report_done"] = d
            return
        r = report.build(d, SNAP)
        prev = await asyncio.to_thread(store.save_report, d, r["status"], r)
        tag = f"sent:{r['status']}"
        if not store.NTFY_TOPIC:
            await asyncio.to_thread(store.mark_report_sent, d, "ntfy 미설정")
        elif prev["sent"] != tag and (prev["old_status"] is None or r["status"] == "확정"):
            err = await store.send(f"📊 통합 운영보고서 {d} ({r['status']})", report.to_text(r), priority=3, tags=["bar_chart"])
            await asyncio.to_thread(store.mark_report_sent, d, tag if not err else f"fail:{err[:60]}")
        if r["status"] == "확정":
            STATE["report_done"] = d
    except Exception as e:
        SNAP.setdefault("errors", []).append(f"report: {type(e).__name__}: {e}")


async def loop():
    while True:
        await collect()
        await asyncio.sleep(INTERVAL)


@app.on_event("startup")
async def startup():
    try:
        await asyncio.to_thread(db.init)
    except Exception as e:
        STATE["errors"].append(f"db init: {e}")
    if os.environ.get("TCC_LOOP", "on") != "off":
        asyncio.create_task(loop())


# ───────────────────────── API
@app.get("/health")
async def health():
    return {"ok": True, "version": VERSION, "ready": SNAP.get("ready", False), "last_run": STATE["last_run"],
            "db": bool(db.DATABASE_URL), "auth": bool(DASH_PASSWORD), "orders": "none (read-only)"}


@app.get("/")
async def root():
    return FileResponse(UI / "index.html", headers={"Cache-Control": "no-store"})


@app.get("/api/snapshot")
async def snapshot():
    if not SNAP.get("ready"):
        if not STATE["running"]:
            await collect()
        else:
            for _ in range(40):
                await asyncio.sleep(0.5)
                if SNAP.get("ready"):
                    break
    return SNAP


@app.post("/api/refresh")
async def refresh():
    if time.time() - STATE["last_run"] < 10:
        return {"ok": True, "skipped": "10초 이내 갱신됨"}
    await collect()
    return {"ok": True}


@app.get("/api/events")
async def events(app_id: str | None = None, sev: str = "P4", limit: int = 200):
    if sev not in store.SEV_RANK:
        raise HTTPException(400, "sev")
    rows = await asyncio.to_thread(store.events, max(10, min(limit, 500)), app_id, sev)
    return {"rows": [{**r, "at": assess.iso(r["at"])} for r in rows]}


@app.post("/api/issues/ack")
async def ack(req: Request):
    b = await req.json()
    ok = await asyncio.to_thread(store.ack, str(b.get("key", ""))[:300])
    if ok:
        for i in SNAP.get("issues") or []:
            if i["key"] == b.get("key"):
                i["state"] = "acked"
    return {"ok": ok}


@app.get("/api/reports")
async def reports():
    rows = await asyncio.to_thread(store.reports)
    return {"rows": [{**r, "d": assess.iso(r["d"])[:10], "built_at": assess.iso(r["built_at"])} for r in rows]}


@app.get("/api/reports/live")
async def report_live():
    """지금 시점 기준 초안(저장 안 함)."""
    if not SNAP.get("ready"):
        raise HTTPException(503, "수집 전")
    d = date.fromisoformat(SNAP["ctx"]["basis"])
    r = report.build(d, SNAP)
    return {**r, "status": "초안(실시간)" if r["status"] != "확정" else r["status"], "text": report.to_text(r)}


@app.get("/api/reports/{d}")
async def report_get(d: str):
    try:
        dd = date.fromisoformat(d)
    except ValueError:
        raise HTTPException(400, "날짜 형식")
    r = await asyncio.to_thread(store.report, dd)
    if not r:
        raise HTTPException(404, "보고서 없음")
    return {**r["data"], "built_at": assess.iso(r["built_at"]), "sent": r["sent"], "text": report.to_text(r["data"])}


@app.post("/api/notify/test")
async def notify_test():
    err = await store.send("✅ 통합 대시보드 알림 테스트", "Global Trading Control Center ntfy 연결 확인", priority=3, tags=["white_check_mark"])
    return {"ok": err is None, "err": err}
