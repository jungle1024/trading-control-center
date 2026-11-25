"""이벤트 영구 저장 · 문제(issue) 수명주기 · ntfy 통보.

- 세 앱의 이벤트를 원천 id 기준으로 증분 수집(event_id = 앱:원천:id → 재수집해도 중복 없음)
- 대시보드가 찾아낸 문제는 key 단위로 open → acked → resolved 를 관리
- ntfy는 '최초 발생 · 악화 · 해제' 시점에만 보낸다. 발송 성공 ≠ 확인. 확인은 화면의 [확인] 버튼.
- ntfy 실패는 notify_err 에 남기고 다음 주기에 재전송한다.
"""
from __future__ import annotations

import json
import os

import httpx
import psycopg2.extras as ex

from app.db import q, rw

NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "")
NTFY_SERVER = os.environ.get("NTFY_SERVER", "https://ntfy.sh")
PUBLIC_URL = os.environ.get("RENDER_EXTERNAL_URL", "")
SEV_RANK = {"P1": 1, "P2": 2, "P3": 3, "P4": 4}

# (원천, SQL, 앱, 매핑)
ROSS_KIND = {"start": ("엔진 시작", "P3"), "stop": ("엔진 정지", "P3"), "watch": ("감시 후보", "P4"), "skip": ("신호 탈락", "P4"),
             "order": ("주문 요청", "P3"), "entry": ("체결·진입", "P2"), "exit": ("청산", "P2"), "protect": ("손절 보호", "P4"),
             "data_block": ("데이터 차단", "P2"), "autosched": ("가동 설정", "P3"), "drop": ("감시 제외", "P4"),
             "config": ("설정 변경", "P3")}
KELL_KIND = {"alert": ("ntfy 발송", "P4"), "error": ("오류", "P2"), "nightly": ("분석 완료", "P3"), "engine": ("엔진", "P3"), "settings": ("설정 변경", "P3"),
             "research": ("연구 작업", "P4"), "order": ("주문", "P2"), "fill": ("체결", "P2"), "exit": ("청산", "P2"), "stop": ("손절", "P2"),
             "signal": ("매매 신호", "P3"), "candidate": ("신규 후보", "P3")}


def _ingest_src(src: str, sql: str, mapper, reader=None) -> int:
    cur_id = q("select last_id from dash_cursor where src=%s", (src,))
    last = cur_id[0]["last_id"] if cur_id else 0
    rows = (reader or q)(sql, (last,))
    if not rows:
        return 0
    with rw() as cur:
        for r in rows:
            m = mapper(r)
            if m is None:
                continue
            cur.execute("""insert into dash_events(event_id, at, app, kind, severity, symbol, msg, data)
                           values(%s,%s,%s,%s,%s,%s,%s,%s) on conflict (event_id) do nothing""",
                        (f"{src}:{r['id']}", r["at"], *m))
        cur.execute("""insert into dash_cursor(src, last_id, updated) values(%s,%s,now())
                       on conflict (src) do update set last_id=excluded.last_id, updated=now()""", (src, max(r["id"] for r in rows)))
    return len(rows)


MA_EV = {"nightly": ("분석 완료", "P3"), "error": ("오류", "P2"), "manage_error": ("장중 관리 오류", "P2"), "order_sent": ("주문 접수", "P2"),
         "order_unknown": ("주문 불확실", "P1"), "order_rejected": ("주문 거부", "P2"), "order_cancel": ("주문 취소", "P3"),
         "exit_plan": ("청산 예정", "P2"), "reconcile": ("잔고 대사 차이", "P2"), "settings": ("설정 변경", "P3"), "mode": ("모드 변경", "P3"),
         "protect": ("보유 보호", "P3"), "broker": ("키움 연결 점검", "P4"), "funds": ("매수 여력 확인", "P4"), "research": ("과거 검증", "P4"),
         "fill": ("체결", "P2"), "entry": ("진입", "P2"), "exit": ("청산", "P2")}


def _ma(r):
    name, sev = MA_EV.get(r["kind"], (r["kind"], "P3"))
    if r["kind"] == "broker" and "실패" in (r["msg"] or ""):
        name, sev = "키움 연결 실패", "P2"
    return ("MA", name, sev, r["symbol"], r["msg"], None)


JONES_EV = {"confirm": ("분석 완료", "P3"), "brief": ("시장 브리핑", "P4"), "error": ("오류", "P2"), "live_mode": ("ETF 엔진 모드", "P3"),
            "live_error": ("ETF 엔진 오류", "P2"), "live_stop": ("비상 손절", "P1"), "live_order": ("ETF 주문", "P2"), "live_fill": ("ETF 체결", "P2"),
            "paper_daily": ("가상 운용 결과", "P4"), "paper_bt": ("가상 운용 백테스트", "P4"), "live_report": ("ETF 장 마감 리포트", "P3")}


V5_EV = {"engine": ("엔진", "P3"), "broker": ("KIS 연결", "P4"), "nightly": ("야간 배치", "P4"), "improve": ("자동 갱신", "P3"),
         "setting": ("설정 변경", "P3"), "replay": ("과거 재현", "P4"), "order": ("주문", "P2"), "fill": ("체결", "P2"), "entry": ("진입", "P2"),
         "exit": ("청산", "P2"), "stop": ("손절", "P2"), "error": ("오류", "P2"), "guard": ("가드 차단", "P2"), "reject": ("주문 거부", "P2"),
         "unknown": ("주문 불명", "P1"), "watchdog": ("워치독 손절", "P1"), "live": ("실매매", "P2"), "eod": ("장 마감 리포트", "P3")}


def _v5(r):
    if r["kind"] == "data_block":          # 하루 수백 건 — 개수만 화면에 요약하고 이벤트 기록에는 넣지 않는다
        return None
    name, sev = V5_EV.get(r["kind"], (r["kind"], "P3"))
    return ("V5", name + (f" · {r['strategy']}" if r.get("strategy") else ""), sev, r["sym"], r["msg"], None)


def ingest_v5() -> int:
    from app.db import q_v5
    return _ingest_src("v5_events", "select id, at, kind, strategy, sym, left(msg, 600) msg from v5_events where id > %s order by id limit 1000",
                       _v5, reader=q_v5)


def _jones(r):
    if r["kind"] == "startup":
        return ("JONES", "서버 시작", "P4", None, r["msg"], None)
    name, sev = JONES_EV.get(r["kind"], (r["kind"], "P3"))
    if r["level"] in ("error",) and sev > "P2":
        sev = "P2"
    return ("JONES", name, sev, None, r["msg"], None)


def _ross(r):
    if r["kind"] == "state":
        return None
    name, sev = ROSS_KIND.get(r["kind"], (r["kind"], "P3"))
    return ("ROSS", name, sev, r["sym"], r["msg"], None)


def _kell(r):
    name, sev = KELL_KIND.get(r["kind"], (r["kind"], "P3"))
    return ("KELL", name, sev, r["symbol"], r["msg"], None)


def ingest() -> dict:
    out = {}
    out["jones"] = _ingest_src("ptj_events", "select id, at, kind, level, left(msg, 600) msg from ptj_events where id > %s order by id limit 500", _jones)
    out["ross"] = _ingest_src("rc_auto_events", "select id, at, kind, sym, left(msg, 600) msg from rc_auto_events where id > %s order by id limit 1000", _ross)
    out["kell"] = _ingest_src("kc_events", "select id, at, kind, symbol, left(msg, 600) msg from kc_events where id > %s order by id limit 500", _kell)
    out["ma"] = _ingest_src("mt_events", "select id, at, kind, symbol, left(msg, 600) msg from mt_events where id > %s order by id limit 500", _ma)
    return out


# ───────────────────────── 문제 수명주기
def sync_issues(current: list[dict]) -> list[dict]:
    """현재 판정된 문제 목록을 저장소와 맞춘다. 반환: 통보가 필요한 변화 목록."""
    cur_by = {i["key"]: i for i in current}
    stored = {r["key"]: r for r in q("select * from dash_issues where state <> 'resolved'")}
    changes = []
    with rw() as cur:
        for k, i in cur_by.items():
            s = stored.get(k)
            if not s:
                cur.execute("""insert into dash_issues(key, app, severity, title, detail, state) values(%s,%s,%s,%s,%s,'open')
                               on conflict (key) do update set severity=excluded.severity, title=excluded.title, detail=excluded.detail,
                               state='open', first_seen=now(), last_seen=now(), resolved_at=null, acked_at=null, notified=null, notify_err=null""",
                            (k, i["app"], i["severity"], i["title"], i["detail"]))
                cur.execute("""insert into dash_events(event_id, at, app, kind, severity, msg, status) values(%s, now(), %s, '문제 발생', %s, %s, 'open')
                               on conflict do nothing""", (f"DASH:open:{k}:{_now_key()}", i["app"], i["severity"], f"{i['title']} — {i['detail']}"))
                changes.append(("open", i))
            else:
                worse = SEV_RANK[i["severity"]] < SEV_RANK.get(s["severity"], 9)
                cur.execute("update dash_issues set last_seen=now(), severity=%s, title=%s, detail=%s where key=%s",
                            (i["severity"], i["title"], i["detail"], k))
                if worse:
                    changes.append(("worse", i))
                elif NTFY_TOPIC and s["state"] == "open" and (s["notified"] is None or s["notify_err"]):
                    changes.append(("open", i))      # 지난번 통보 실패 → 재전송
        for k, s in stored.items():
            if k not in cur_by:
                cur.execute("update dash_issues set state='resolved', resolved_at=now() where key=%s", (k,))
                cur.execute("""insert into dash_events(event_id, at, app, kind, severity, msg, status) values(%s, now(), %s, '문제 해제', %s, %s, 'resolved')
                               on conflict do nothing""", (f"DASH:resolved:{k}:{_now_key()}", s["app"], s["severity"], f"해제: {s['title']}"))
                changes.append(("resolved", dict(s)))
    return changes


def _now_key():
    import time
    return int(time.time() // 60)


def ack(key: str) -> bool:
    with rw() as cur:
        cur.execute("update dash_issues set state='acked', acked_at=now() where key=%s and state='open'", (key,))
        ok = cur.rowcount > 0
        if ok:
            cur.execute("""insert into dash_events(event_id, at, app, kind, severity, msg, status)
                           select %s, now(), app, '관리자 확인', severity, '확인: ' || title, 'acked' from dash_issues where key=%s""",
                        (f"DASH:ack:{key}:{_now_key()}", key))
    return ok


def open_issues() -> list[dict]:
    return q("""select key, app, severity, title, detail, state, first_seen, last_seen, acked_at, notified, notify_err
                from dash_issues where state <> 'resolved' order by severity, first_seen""")


def recent_resolved(hours=48) -> list[dict]:
    return q("""select key, app, severity, title, first_seen, resolved_at, acked_at from dash_issues
                where state='resolved' and resolved_at > now() - make_interval(hours => %s) order by resolved_at desc limit 30""", (hours,))


# ───────────────────────── ntfy
async def notify(changes: list) -> None:
    """P1·P2 문제의 발생/악화/해제만 통보. P3·P4는 화면·보고서로만."""
    if not NTFY_TOPIC:
        return
    for kind, i in changes:
        sev = i["severity"]
        if sev not in ("P1", "P2"):
            continue
        title = {"open": "🚨" if sev == "P1" else "⚠️", "worse": "⏫", "resolved": "✅"}[kind] + f" [{sev}] " + i["title"]
        body = (i.get("detail") or "") if kind != "resolved" else "문제가 해제되었습니다."
        err = await send(title, body, priority=5 if sev == "P1" and kind != "resolved" else 3, tags=["warning"] if kind != "resolved" else ["white_check_mark"])
        with rw() as cur:
            cur.execute("update dash_issues set notified=%s, notify_err=%s where key=%s", (f"{kind}:{sev}", err, i["key"]))


async def send(title: str, body: str, priority=3, tags=None) -> str | None:
    """성공 시 None, 실패 시 오류 문자열(재시도 대상). 토픽이 없으면 '미설정'."""
    if not NTFY_TOPIC:
        return "NTFY_TOPIC 미설정"
    try:
        payload = {"topic": NTFY_TOPIC, "title": title[:250], "message": body[:3800] or "-", "priority": priority, "tags": tags or []}
        if PUBLIC_URL:
            payload["click"] = PUBLIC_URL
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.post(NTFY_SERVER, content=json.dumps(payload).encode(), headers={"Content-Type": "application/json"})
        return None if r.status_code < 300 else f"HTTP {r.status_code}"
    except Exception as e:
        return f"{type(e).__name__}: {str(e)[:100]}"


def events(limit=150, app=None, min_sev="P4") -> list[dict]:
    return q("""select event_id, at, app, kind, severity, symbol, msg, status from dash_events
                where (%s::text is null or app=%s) and severity <= %s order by at desc limit %s""", (app, app, min_sev, limit))


def save_report(d, status, data) -> dict:
    with rw() as cur:
        cur.execute("select status, sent from dash_reports where d=%s", (d,))
        old = cur.fetchone()
        cur.execute("""insert into dash_reports(d, status, data, built_at) values(%s,%s,%s,now())
                       on conflict (d) do update set status=excluded.status, data=excluded.data, built_at=now()""",
                    (d, status, ex.Json(data, dumps=lambda o: json.dumps(o, default=str, ensure_ascii=False))))
    return {"old_status": old["status"] if old else None, "sent": old["sent"] if old else None}


def mark_report_sent(d, tag):
    with rw() as cur:
        cur.execute("update dash_reports set sent=%s where d=%s", (tag, d))


def reports(limit=30):
    return q("select d, status, built_at, sent from dash_reports order by d desc limit %s", (limit,))


def report(d):
    r = q("select d, status, built_at, sent, data from dash_reports where d=%s", (d,))
    return r[0] if r else None


async def topic_check() -> str:
    """NTFY_TOPIC 이 바뀌었으면(해시 비교) 새 토픽으로 확인 메시지 1회 발송. 토픽 이름은 저장하지 않는다."""
    import hashlib
    if not NTFY_TOPIC:
        return "미설정"
    h = int(hashlib.sha256(NTFY_TOPIC.encode()).hexdigest()[:12], 16)
    row = q("select last_id from dash_cursor where src='ntfy_topic'")
    if row and row[0]["last_id"] == h:
        return "변경 없음"
    err = await send("✅ 통합 대시보드 알림 연결 확인",
                     "알림 토픽이 설정·변경되어 이 토픽으로 연결을 확인합니다. 이 메시지가 보이면 긴급·중요 경보와 매일 09:30 일일보고서를 이 토픽으로 받습니다.",
                     priority=3, tags=["white_check_mark"])
    if err:
        return f"발송 실패: {err}"
    with rw() as cur:
        cur.execute("""insert into dash_cursor(src, last_id, updated) values('ntfy_topic', %s, now())
                       on conflict (src) do update set last_id=excluded.last_id, updated=now()""", (h,))
        cur.execute("""insert into dash_events(event_id, at, app, kind, severity, msg, status) values(%s, now(), 'DASH', '알림 토픽 확인', 'P4', %s, 'info')
                       on conflict do nothing""", (f"DASH:topic:{h}", "알림 토픽 변경 감지 — 확인 메시지 발송 성공"))
    return "발송"


# ───────────────────────── 재배포 겹침 방지
# Render 무중단 배포 때 옛 인스턴스와 새 인스턴스가 1~2분 함께 돈다. 가장 늦게 시작한 인스턴스만
# 문제 판정·알림·보고서를 맡는다(옛 인스턴스가 문제를 열고 닫아 알림이 반복되던 현상 방지).
def claim_leader(started_ms: int):
    with rw() as cur:
        cur.execute("""insert into dash_cursor(src, last_id, updated) values('leader', %s, now())
                       on conflict (src) do update set last_id=greatest(dash_cursor.last_id, excluded.last_id), updated=now()""",
                    (started_ms,))


def is_leader(started_ms: int) -> bool:
    row = q("select last_id from dash_cursor where src='leader'")
    return not row or row[0]["last_id"] <= started_ms
