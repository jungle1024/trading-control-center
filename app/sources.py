"""원천 데이터 수집 — 세 전략 앱의 결과를 '읽기만' 한다.

1) 공개 /health (서버 상태)
2) 공유 DB의 각 앱 테이블 (읽기 전용 트랜잭션)
3) (선택) 각 앱 비밀번호가 설정된 경우에만 계좌 조회 GET API — 증권사 잔고 확인용
"""
from __future__ import annotations

import asyncio
import os
import time

import httpx

from app.db import q

APPS = {
    "JONES": {"name": "폴 튜더 존스", "short": "Jones", "role": "장기 시장분석·종목 발굴",
              "url": os.environ.get("JONES_URL", "https://ptj-200-scanner-2e0a.onrender.com"), "trading": False},
    "ROSS": {"name": "로스 카메론", "short": "Ross", "role": "초단타 자동 실매매 · 토스증권",
             "url": os.environ.get("ROSS_URL", "https://ross-cameron-scanner-zcc5.onrender.com"), "trading": True},
    "KELL": {"name": "올리버 켈", "short": "Kell", "role": "수축 돌파 수일 매매 · 한국투자증권",
             "url": os.environ.get("KELL_URL", "https://kell-contraction-breakout-c8tn.onrender.com"), "trading": True},
    "MA": {"name": "20·60 이동평균선 추세", "short": "MA20·60", "role": "20·60일선·추세 저항선 돌파 · 키움증권",
           "url": os.environ.get("MA_URL", "https://ma2060-trend-trader-x7oa.onrender.com"), "trading": True},
}


# ───────────────────────── 1. 서버 상태
async def probe(client: httpx.AsyncClient, app: str) -> dict:
    t0 = time.time()
    try:
        r = await client.get(APPS[app]["url"] + "/health", timeout=20)
        ms = int((time.time() - t0) * 1000)
        if r.status_code != 200:
            return {"ok": False, "http": r.status_code, "ms": ms, "err": f"HTTP {r.status_code}"}
        return {"ok": True, "http": 200, "ms": ms, "body": r.json(), "at": time.time()}
    except Exception as e:
        return {"ok": False, "http": None, "ms": int((time.time() - t0) * 1000), "err": f"{type(e).__name__}: {str(e)[:120]}"}


async def probe_all() -> dict:
    async with httpx.AsyncClient(follow_redirects=False, headers={"User-Agent": "trading-control-center/1.0"}) as c:
        res = await asyncio.gather(*(probe(c, a) for a in APPS))
    return dict(zip(APPS, res))


# ───────────────────────── 3. (선택) 계좌 조회 — 읽기 전용 GET 만 호출
_ACCT_CACHE: dict = {}


async def _login_get(app: str, paths) -> dict:
    """로그인 후 읽기 전용 GET 만 호출. paths: 문자열 하나 또는 {이름: 경로}."""
    pw = os.environ.get(f"{app}_APP_PASSWORD", "")
    if not pw:
        return {"configured": False}
    base = APPS[app]["url"]
    async with httpx.AsyncClient(timeout=30, follow_redirects=False) as c:
        if app == "ROSS":
            r = await c.post(base + "/login", data={"pw": pw})
        elif app == "MA":
            r = await c.post(base + "/api/login", data={"pw": pw})
        else:
            r = await c.post(base + "/api/login", json={"password": pw})
        if r.status_code not in (200, 303):
            return {"configured": True, "ok": False, "err": f"로그인 실패 HTTP {r.status_code}"}
        if isinstance(paths, str):
            r = await c.get(base + paths)
            if r.status_code != 200:
                return {"configured": True, "ok": False, "err": f"{paths} HTTP {r.status_code}: {r.text[:160]}"}
            return {"configured": True, "ok": True, "data": r.json(), "at": time.time()}
        out, errs = {}, []
        for name, path in paths.items():
            r = await c.get(base + path)
            if r.status_code == 200:
                out[name] = r.json()
            else:
                errs.append(f"{path} HTTP {r.status_code}: {r.text[:120]}")
        return {"configured": True, "ok": bool(out), "data": out, "err": " · ".join(errs) or None, "at": time.time()}


async def accounts(max_age: int = 300, ma_book: str | None = None) -> dict:
    """증권사 실잔고 — 5분 캐시(증권사 API 부담 최소화)."""
    now = time.time()
    if _ACCT_CACHE.get("at") and now - _ACCT_CACHE["at"] < max_age:
        return _ACCT_CACHE["v"]
    out = {}
    jobs = [("ROSS", "/api/toss/account"), ("KELL", "/api/account"),
            ("MA", {"funds": "/api/broker/funds", **({"book": f"/api/paper?book={ma_book}"} if ma_book else {})})]
    for app, path in jobs:
        try:
            out[app] = await _login_get(app, path)
        except Exception as e:
            out[app] = {"configured": True, "ok": False, "err": f"{type(e).__name__}: {str(e)[:120]}"}
    _ACCT_CACHE.update(at=now, v=out)
    return out


# ───────────────────────── 2. DB 조회 (읽기 전용)
def jones() -> dict:
    mk = q("""select d, computed_at, data->'regime' regime, data->'defense' defense, data->>'defense_text' defense_text,
                     data->'warnings' warnings, data->'vix' vix, data->'sectors_above' sectors_above,
                     data->'breadth' breadth, data->'opportunities' opportunities, data->'flow'->'near' near,
                     (select jsonb_object_agg(k, jsonb_build_object('close', v->'close', 'chg', v->'chg', 'gap200', v->'gap200',
                        'dir200', v->'dir200', 'dir5', v->'dir5', 'dir20', v->'dir20', 'dir60', v->'dir60', 'gap20', v->'gap20',
                        'gap60', v->'gap60', 'gap5', v->'gap5', 'align', v->'align', 'below200_days', v->'below200_days',
                        'off_high', v->'off_high', 'slope200', v->'slope200'))
                        from jsonb_each(data->'indexes') e(k, v)) indexes,
                     (select jsonb_agg(jsonb_build_object('name', s->'name', 'symbol', s->'symbol', 'gap200', s->'gap200',
                        'dir200', s->'dir200', 'chg', s->'chg')) from jsonb_array_elements(data->'sectors') s) sectors
              from ptj_market where not provisional order by d desc limit 1""")
    prov = q("""select d, computed_at, data->'regime' regime from ptj_market where provisional order by d desc limit 1""")
    hist = q("""select d, data->'regime'->>'code' code, data->'regime'->>'name' name,
                       jsonb_array_length(coalesce(data->'warnings', '[]'::jsonb)) nw
                from ptj_market where not provisional order by d desc limit 30""")
    runs = q("""select id, kind, d, started_at, finished_at, ok, left(err, 300) err from ptj_runs order by id desc limit 10""")
    cd = q("select max(d) d from ptj_candidates")
    d = cd[0]["d"] if cd else None
    cands, prev_syms, by_type = [], set(), []
    if d:
        cands = q("""select symbol, data->>'name' name, data->'type_names'->>0 type, data->>'risk' risk,
                            (data->>'rs')::float rs, (data->>'close')::float close, (data->>'gap200')::float gap200,
                            data->>'sector' sector, data->'why'->>0 why
                     from ptj_candidates where d=%s""", (d,))
        pd = q("select max(d) d from ptj_candidates where d < %s", (d,))
        if pd and pd[0]["d"]:
            prev_syms = {r["symbol"] for r in q("select symbol from ptj_candidates where d=%s", (pd[0]["d"],))}
    oc = q("""select count(*) filter (where outcome is not null) n_out,
                     count(*) filter (where (outcome->>'r5_before_stop') is not null) decided,
                     count(*) filter (where (outcome->>'r5_before_stop')::boolean) r5,
                     percentile_cont(0.5) within group (order by (outcome->>'ret20')::float - coalesce((outcome->>'spy20')::float, 0))
                        filter (where outcome->>'ret20' is not null) ex20_med,
                     count(*) filter (where outcome->>'ret20' is not null) ex20_n
              from ptj_candidates""")
    ae = q("select data->'all' a, data->'strong' s from ptj_settings where k='alert_eval'")
    ev = q("""select id, at, d, kind, level, left(msg, 400) msg from ptj_events order by id desc limit 40""")
    restarts = q("select count(*) n from ptj_events where kind='startup' and at > now() - interval '24 hours'")
    alerts = q("select id, at, kind, title, sent, left(err, 200) err from ptj_alerts order by id desc limit 10")
    return {"market": mk[0] if mk else None, "provisional": prov[0] if prov else None, "history": hist, "runs": runs,
            "cand_d": d, "candidates": cands, "prev_syms": prev_syms, "outcomes": oc[0] if oc else {},
            "alert_eval": ae[0] if ae else None, "events": ev, "restarts24": restarts[0]["n"] if restarts else 0, "alerts": alerts}


def ross() -> dict:
    st = {r["k"]: r["data"] for r in q("""select k, data from rc_settings
                                         where k in ('autosched','auto','strategy_v1','main','auto_stats')""")}
    trades = q("""select id, et_date, sym, status, updated, data from rc_auto_trades
                  where mode='live' and et_date >= to_char(now() - interval '400 days', 'YYYY-MM-DD') order by et_date, id""")
    reports = q("""select et_date, created, data->'perf' perf, data->'checks' checks, data->'gate' gate, data->'funnel' funnel,
                          data->'final' final, data->'mode' mode
                   from rc_auto_reports order by et_date desc limit 40""")
    ev_counts = q("""select et_date, kind, count(*) n, count(distinct sym) syms from rc_auto_events
                     where et_date >= to_char(now() - interval '10 days', 'YYYY-MM-DD') group by 1, 2""")
    ev = q("""select id, at, et_date, kind, sym, left(msg, 300) msg from rc_auto_events where kind <> 'state' order by id desc limit 40""")
    orders = q("""select et_date, kind, ok, count(*) n from rc_orders where at > now() - interval '60 days' group by 1, 2, 3""")
    lt = q("select symbol, name, qty, avg_price from rc_longterm")
    legacy = q("select symbol, name, qty, avg_price from rc_legacy")
    alerts = q("""select count(*) n, count(*) filter (where sent not like 'ntfy:2%%' or coalesce(err,'') <> '') bad
                  from rc_alerts where at > now() - interval '24 hours'""")
    return {"settings": st, "trades": trades, "reports": reports, "ev_counts": ev_counts, "events": ev, "orders": orders,
            "longterm": lt, "legacy": legacy, "alerts24": alerts[0] if alerts else {}}


def kell() -> dict:
    st = {r["k"]: r["data"] for r in q("select k, data from kc_settings")}
    peak = {r["k"]: r["data"] for r in q("select k, data from kc_broker where k like 'peak:%%' or k = 'stopwatch'")}
    eng = q("select d, mode, started, last_beat, beats from kc_engine_days order by d desc limit 5")
    jobs = q("""(select id, job, started, finished, ok, as_of, left(error, 300) error,
                        stats->'passed' passed, stats->'watch' watch, stats->'regime' regime
                 from kc_jobs where job='nightly' order by id desc limit 8)
                union all
                (select id, job, started, finished, ok, as_of, left(error, 300) error, null, null, null
                 from kc_jobs where job<>'nightly' order by id desc limit 4)
                order by id desc""")
    mk = q("select as_of, regime, data->>'why' why from kc_market order by as_of desc limit 1")
    wd = q("select max(for_date) d from kc_watch")
    watch = []
    if wd and wd[0]["d"]:
        watch = q("""select for_date, symbol, kind, ctype, pivot, invalidation, score, status, note, earnings
                     from kc_watch where for_date=%s order by score desc nulls last""", (wd[0]["d"],))
    scan = q("""select as_of, count(*) n, count(*) filter (where passed) passed from kc_scan
                where as_of = (select max(as_of) from kc_scan) group by as_of""")
    pos_open = q("""select p.id, p.env, p.symbol, p.kind, p.mode, p.entry_date, p.entry_at, p.entry_price, p.qty, p.qty_open,
                           p.stop_init, p.stop, p.plan_r, p.partial_done, p.pending_exit, p.realized,
                           s.last_price, s.last_check, s.active stop_active, s.state stop_state
                    from kc_positions p left join kc_stops s on s.env=p.env and s.symbol=p.symbol and s.active
                    where p.status='open' order by p.entry_at""")
    pos_closed = q("""select id, env, symbol, kind, entry_date, entry_at, entry_price, qty, exit_value, realized, r_mult, closed_at
                      from kc_positions where status='closed' order by closed_at""")
    orders = q("""select id, created, updated, env, symbol, side, qty, price, purpose, status from kc_orders
                  where created > now() - interval '14 days' order by id desc limit 60""")
    sig = q("""select for_date, env, action, count(*) n from kc_signals where for_date >= current_date - 10 group by 1, 2, 3""")
    shadow = q("select count(*) n, max(created) last from kc_shadow_signals where created > now() - interval '30 days'")
    ev = q("select id, at, kind, symbol, left(msg, 300) msg from kc_events order by id desc limit 40")
    return {"settings": st, "broker": peak, "engine": eng, "jobs": jobs, "market": mk[0] if mk else None, "watch": watch,
            "scan": scan[0] if scan else None, "pos_open": pos_open, "pos_closed": pos_closed, "orders": orders,
            "signals": sig, "shadow": shadow[0] if shadow else {}, "events": ev}


def ma() -> dict:
    st = {r["k"]: r["data"] for r in q("select k, data from mt_settings where k='main'")}
    jobs = q("""(select id, job, started, finished, ok, as_of, left(error, 300) error, stats->'kinds' kinds, stats->'signals' signals,
                        stats->'buyable' buyable, stats->'watch' watch, stats->'approval' approval
                 from mt_jobs where job='nightly' order by id desc limit 6)
                union all
                (select id, job, started, finished, ok, as_of, left(error, 300) error, null, null, null, null, stats->'approval'
                 from mt_jobs where job<>'nightly' order by id desc limit 3)
                order by id desc""")
    mk = q("select as_of, data from mt_market order by as_of desc limit 1")
    sd = q("select max(as_of) d from mt_scan")
    scan, fails = [], []
    if sd and sd[0]["d"]:
        scan = q("""select kind, count(*) n, count(*) filter (where signal) sig, count(*) filter (where buyable) buy
                    from mt_scan where as_of=%s group by kind""", (sd[0]["d"],))
        fails = q("""select f fail, count(*) n from mt_scan, jsonb_array_elements_text(coalesce(fails, '[]'::jsonb)) f
                     where as_of=%s and kind in ('A','B','C') group by f order by n desc limit 6""", (sd[0]["d"],))
    wd = q("select max(for_date) d from mt_watch")
    watch = []
    if wd and wd[0]["d"]:
        watch = q("""select for_date, symbol, kind, level, entry_ref, chase_max, stop, target, rr, score, status, note
                     from mt_watch where for_date=%s order by status='excluded', score desc nulls last""", (wd[0]["d"],))
    pos_open = q("""select id, book, symbol, kind, opened_at, entry, qty, qty_open, stop0, stop, target, risk_ps, partial_done,
                           exit_next_open, realized from mt_positions where status='open' order by opened_at""")
    pos_closed = q("""select id, book, symbol, kind, opened_at, closed_at, entry, qty, realized, risk_ps, exit_why
                      from mt_positions where status='closed' order by closed_at""")
    orders = q("""select id, created, sent_at, env, symbol, side, kind, qty, price, status, reason from mt_orders
                  where created > now() - interval '14 days' and status <> 'previewed' order by id desc limit 60""")
    locks = q("select symbol, order_id, reason, created from mt_order_lock")
    research = q("""select id, created, name, status, summary->'approval' approval from mt_research order by id desc limit 1""")
    broker = q("""select at, msg from mt_events where kind='broker' order by id desc limit 1""")
    errs = q("""select count(*) n, max(at) last from mt_events where kind in ('manage_error','error','order_rejected')
                and at > now() - interval '24 hours'""")
    protect = q("select count(*) n from mt_protect")
    return {"settings": st.get("main") or {}, "jobs": jobs, "market": mk[0] if mk else None, "scan_as_of": sd[0]["d"] if sd else None,
            "scan": scan, "fails": fails, "watch": watch, "pos_open": pos_open, "pos_closed": pos_closed, "orders": orders,
            "locks": locks, "research": research[0] if research else None, "broker": broker[0] if broker else None,
            "errs24": errs[0] if errs else {}, "protect_n": protect[0]["n"] if protect else 0}
