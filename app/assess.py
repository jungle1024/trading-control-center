"""원천 데이터 → 판정(정상·주의·장애), 성과 지표, 조치 필요 항목.

원칙(구축 가이드)
- 신호·주문 요청·실제 체결·실현손익을 구분한다.
- 실현손익과 평가손익을 합산하지 않는다.
- 휴장·장 마감 후 '신호 없음'을 장애로 보지 않는다(거래 일정 기준 판정).
- '오늘 매매 없음'과 '오늘 분석 안 함'을 구분한다.
"""
from __future__ import annotations

import time
from datetime import date, datetime, timedelta, timezone

from app import tcal
from app.sources import APPS

ET = tcal.ET
UTC = timezone.utc

# 존스 국면 → 가이드의 5단계, 위험 점수(클수록 위험)
STAGE = {
    "UP_STRONG": ("상승", 0), "UP": ("상승", 1), "UP_WEAK": ("둔화", 2), "RECOVERY": ("회복", 2),
    "MIXED": ("경계", 3), "TURN_WARN": ("경계", 3), "DOWN_EASING": ("회복", 4), "BROKE_200": ("하락", 5),
    "DOWNTREND": ("하락", 6),
}
LV_ORDER = {"fail": 4, "warn": 3, "ok": 1, "idle": 0, "na": -1}


def lv(level: str, text: str, **kw) -> dict:
    return {"level": level, "text": text, **kw}


def worst(*items) -> str:
    vals = [i["level"] for i in items if i and i["level"] != "na"]
    return max(vals, key=lambda x: LV_ORDER[x]) if vals else "na"


def iso(x):
    if x is None:
        return None
    if isinstance(x, (int, float)):
        return datetime.fromtimestamp(x, UTC).isoformat()
    if isinstance(x, datetime):
        return x.astimezone(UTC).isoformat()
    return str(x)


def _ts(x) -> float | None:
    if x is None:
        return None
    if isinstance(x, (int, float)):
        return float(x)
    if isinstance(x, datetime):
        return x.timestamp()
    return None


def _d(x) -> date | None:
    if x is None:
        return None
    if isinstance(x, datetime):
        return x.date()
    if isinstance(x, date):
        return x
    try:
        return date.fromisoformat(str(x)[:10])
    except ValueError:
        return None


def r2(x, n=2):
    return None if x is None else round(float(x), n)


class Ctx:
    """판정 시점의 거래 일정."""

    def __init__(self, now: datetime | None = None):
        self.now = now or datetime.now(UTC)
        self.et = self.now.astimezone(ET)
        self.today = self.et.date()
        self.session_today = tcal.is_session(self.today)
        self.min = self.et.hour * 60 + self.et.minute
        close = tcal.close_time(self.today)
        self.close_min = close.hour * 60 + close.minute
        if not self.session_today:
            self.phase, self.phase_name = "closed", "휴장" if self.today.weekday() < 5 else "주말 휴장"
        elif self.min < 240:
            self.phase, self.phase_name = "night", "장 시작 전(야간)"
        elif self.min < 570:
            self.phase, self.phase_name = "pre", "프리마켓"
        elif self.min < self.close_min:
            self.phase, self.phase_name = "regular", "정규장"
        elif self.min < 1200:
            self.phase, self.phase_name = "post", "애프터마켓"
        else:
            self.phase, self.phase_name = "night", "장 마감 후"
        # 매매 결과 기준일: 오늘 04:00 ET 이후면 오늘, 아니면 직전 거래일
        self.basis = self.today if (self.session_today and self.min >= 240) else tcal.prev_session(self.today)
        # 결과 확정 기대일: 마감 + 3시간(켈 야간 스캔·로스 리포트 이후)
        self.settled = self.today if (self.session_today and self.min >= self.close_min + 180) else tcal.prev_session(self.today)
        self.next_session = self.today if (self.session_today and self.min < self.close_min) else self._next(self.today)

    @staticmethod
    def _next(d: date) -> date:
        d += timedelta(days=1)
        while not tcal.is_session(d):
            d += timedelta(days=1)
        return d

    def info(self) -> dict:
        return {"now": iso(self.now), "et": self.et.strftime("%Y-%m-%d %H:%M"), "phase": self.phase, "phase_name": self.phase_name,
                "basis": self.basis.isoformat(), "settled": self.settled.isoformat(), "next_session": self.next_session.isoformat(),
                "session_today": self.session_today, "early_close": self.today.isoformat() in tcal.EARLY_CLOSE}


def issue(app, sev, key, title, detail="", link=None):
    return {"app": app, "severity": sev, "key": f"{app}:{key}", "title": title, "detail": detail,
            "link": link or APPS.get(app, {}).get("url")}


# ───────────────────────── 공통: 서버 상태
def server_level(app: str, h: dict) -> dict:
    if not h:
        return lv("warn", "점검 전")
    if not h.get("ok"):
        return lv("fail", f"응답 없음 · {h.get('err')}", ms=h.get("ms"))
    b = h.get("body") or {}
    if b.get("db") is False:
        return lv("fail", "서버 응답 · DB 미연결", ms=h["ms"])
    return lv("ok", f"정상 응답 {h['ms']}ms" + (f" · v{b['version']}" if b.get("version") else ""), ms=h["ms"])


# ───────────────────────── 거래 성과 지표 (청산 완료 단위)
def perf(rows: list[dict], capital: float | None = None) -> dict:
    """rows: [{pnl, r, exit_ts, hold_s}] — 청산 완료 포지션 단위."""
    rows = sorted([r for r in rows if r.get("pnl") is not None], key=lambda r: r.get("exit_ts") or 0)
    n = len(rows)
    if not n:
        return {"n": 0}
    pnl = [float(r["pnl"]) for r in rows]
    wins = [x for x in pnl if x > 0]
    loss = [x for x in pnl if x < 0]
    eq = peak = mdd = 0.0
    for x in pnl:
        eq += x
        peak = max(peak, eq)
        mdd = min(mdd, eq - peak)
    rs = [float(r["r"]) for r in rows if r.get("r") is not None]
    holds = [r["hold_s"] for r in rows if r.get("hold_s")]
    out = {"n": n, "wins": len(wins), "win_rate": round(len(wins) / n * 100, 1), "net": round(sum(pnl), 2),
           "pf": round(sum(wins) / -sum(loss), 2) if loss else (None if not wins else "∞"),
           "expectancy": round(sum(pnl) / n, 2), "avg_win": r2(sum(wins) / len(wins)) if wins else None,
           "avg_loss": r2(sum(loss) / len(loss)) if loss else None, "mdd": round(mdd, 2),
           "avg_r": round(sum(rs) / len(rs), 2) if rs else None, "avg_hold_s": int(sum(holds) / len(holds)) if holds else None}
    if capital:
        out["ret_pct"] = round(sum(pnl) / capital * 100, 2)
        out["mdd_pct"] = round(mdd / capital * 100, 2)
    return out


# ───────────────────────── 존스
def assess_jones(J: dict, h: dict, ctx: Ctx) -> dict:
    app = "JONES"
    url = APPS[app]["url"]
    iss = []
    srv = server_level(app, h)
    if srv["level"] == "fail":
        iss.append(issue(app, "P2", "server", "Jones 서버 응답 없음", srv["text"]))
    b = (h or {}).get("body") or {}
    m = J.get("market")
    exp = tcal.expected_last_session(ctx.et)
    runs = J.get("runs") or []
    last_run = runs[0] if runs else None

    # 데이터
    if not b:
        data = lv("warn", "서버 응답 없음 — DB 기록으로만 판정" + (f" · 최근 실행 {'성공' if last_run and last_run['ok'] else '실패'}" if last_run else ""))
    elif b.get("fmp_key") is False:
        data = lv("fail", "FMP 키 없음")
        iss.append(issue(app, "P2", "fmp", "Jones FMP 키 없음", "Render 환경변수 FMP_API_KEY 확인"))
    elif last_run and not last_run["ok"] and last_run["err"]:
        data = lv("warn", f"최근 실행 실패: {last_run['err'][:80]}")
    else:
        data = lv("ok", "FMP 연결 정상" + (f" · 최근 실행 {last_run['kind']} 성공" if last_run else ""))

    # 분석
    md = _d(m["d"]) if m else None
    if not m:
        ana = lv("fail", "확정 분석 결과 없음")
        iss.append(issue(app, "P2", "no_market", "Jones 확정 분석 결과 없음"))
    elif md == exp:
        ana = lv("ok", f"{md} 확정 분석 완료", at=iso(m["computed_at"]))
    else:
        due = datetime.combine(exp, tcal.close_time(exp), ET) + timedelta(minutes=tcal.CONFIRM_AFTER_MIN)
        late_h = (ctx.et - due).total_seconds() / 3600
        if late_h > 2:
            ana = lv("fail", f"확정 분석 지연 — 최신 {md}, 기대 {exp}")
            iss.append(issue(app, "P2", "stale", "Jones 시장분석 지연", f"최신 확정 {md} · 기대 {exp} · {late_h:.0f}시간 경과", url + "/#records"))
        else:
            ana = lv("warn", f"확정 계산 대기 (기대 {exp})")
    stat = {"server": srv, "data": data, "analysis": ana, "trade": lv("na", "매매 없음(분석 전용)")}

    # 시장 판단
    market = None
    if m:
        rg = m.get("regime") or {}
        code = rg.get("code")
        stage, risk = STAGE.get(code, ("확인 필요", 3))
        hist = J.get("history") or []
        prev = hist[1] if len(hist) > 1 else None
        nw = len(m.get("warnings") or [])
        if prev:
            pr = STAGE.get(prev["code"], ("", 3))[1]
            if risk > pr or (risk == pr and nw > (prev["nw"] or 0)):
                change = "위험 증가"
            elif risk < pr or (risk == pr and nw < (prev["nw"] or 0)):
                change = "위험 감소"
            else:
                change = "유지"
            prev_info = {"d": iso(prev["d"])[:10], "name": prev["name"], "nw": prev["nw"]}
        else:
            change, prev_info = "비교 이력 없음", None
        # 같은 국면이 며칠째인지
        streak = 0
        for x in hist:
            if x["code"] == code:
                streak += 1
            else:
                break
        idx = {}
        for s in ("SPY", "QQQ", "IWM", "DIA"):
            x = (m.get("indexes") or {}).get(s)
            if not x:
                continue
            above = (x.get("gap200") or 0) >= 0
            up = x.get("dir200") == "up"
            if above and up:
                lab, tone = "장기 상승", "bull"
            elif above:
                lab, tone = "상승 둔화", "warn"
            elif up:
                lab, tone = f"200일선 이탈 {x.get('below200_days') or ''}일째", "bear"
            else:
                lab, tone = "하락 추세", "bear"
            mas = " · ".join(f"{p}일 {'▲' if x.get('dir' + p) == 'up' else '▼'}" for p in ("5", "20", "60"))
            idx[s] = {**x, "label": lab, "tone": tone, "ma_text": mas + f" · 200일 {'▲' if up else '▼'}"}
        br = m.get("breadth") or {}
        market = {"d": iso(m["d"])[:10], "computed_at": iso(m["computed_at"]), "code": code, "name": rg.get("name"), "tone": rg.get("tone"),
                  "reasons": rg.get("reasons") or [], "stage": stage, "risk": risk, "change": change, "prev": prev_info, "streak": streak,
                  "defense": bool(m.get("defense")), "defense_text": m.get("defense_text"), "warnings": m.get("warnings") or [],
                  "indexes": idx, "vix": m.get("vix"), "sectors_above": m.get("sectors_above"), "sectors": m.get("sectors") or [],
                  "near": m.get("near") or [], "opportunities": m.get("opportunities") or [],
                  "breadth": {k: br.get(k) for k in ("pct200", "sp_pct200", "pct200_20ago", "pct50", "pct20", "nh", "nl", "adv", "dec", "nhnl10")},
                  "trend_long": "장기 상승 유지" if risk <= 2 else ("장기 추세 훼손" if risk >= 5 else "장기 추세 혼조"),
                  "early_warning": f"조기경보 {nw}건" if nw else "조기경보 없음"}
        if prev and change == "위험 증가":
            iss.append(issue(app, "P2", f"risk_up:{market['d']}", f"시장 위험 증가 — {rg.get('name')}",
                             f"전일 {prev['name']} → {rg.get('name')} · 경보 {nw}건", url + "/#market"))
    prov = J.get("provisional")
    provisional = None
    if prov and (not m or _d(prov["d"]) > md):
        provisional = {"d": iso(prov["d"])[:10], "name": (prov.get("regime") or {}).get("name"), "at": iso(prov["computed_at"])}

    # 종목 발굴
    cands = J.get("candidates") or []
    prev_syms = J.get("prev_syms") or set()
    by_type, by_risk = {}, {}
    for c in cands:
        by_type[c["type"] or "기타"] = by_type.get(c["type"] or "기타", 0) + 1
        by_risk[c["risk"] or "-"] = by_risk.get(c["risk"] or "-", 0) + 1
    top = sorted([c for c in cands if c["risk"] != "높음"], key=lambda c: -(c["rs"] or 0))[:10]
    discover = {"d": iso(J.get("cand_d"))[:10] if J.get("cand_d") else None, "n": len(cands),
                "new": len([c for c in cands if c["symbol"] not in prev_syms]) if prev_syms else None,
                "by_type": dict(sorted(by_type.items())), "by_risk": by_risk,
                "top": [{**c, "new": c["symbol"] not in prev_syms if prev_syms else None} for c in top],
                "link": url + "/#cands", "note": "Jones 웹앱은 종목별 주소를 지원하지 않아 후보 목록 화면으로 연결합니다."}

    # KPI (분석 효과)
    ae = J.get("alert_eval") or {}
    oc = J.get("outcomes") or {}
    kpi = {"alert_hit_rate": (ae.get("a") or {}).get("hit_rate"), "alert_episodes": (ae.get("a") or {}).get("episodes"),
           "alert_avg_lead": (ae.get("a") or {}).get("avg_lead"), "alert_avg_drop": (ae.get("a") or {}).get("avg_drop"),
           "strong_hit_rate": (ae.get("s") or {}).get("hit_rate"),
           "cand_outcomes": oc.get("n_out"), "r5_rate": round(oc["r5"] / oc["decided"] * 100, 1) if oc.get("decided") else None,
           "ex20_med": r2(oc.get("ex20_med")), "ex20_n": oc.get("ex20_n")}

    bad_alerts = [a for a in (J.get("alerts") or []) if a.get("err") or (a.get("sent") and not str(a["sent"]).startswith("ntfy:2"))]
    return {"app": app, "status": stat, "overall": worst(*stat.values()), "market": market, "provisional": provisional,
            "discover": discover, "kpi": kpi, "runs": [{**r, "started_at": iso(r["started_at"]), "finished_at": iso(r["finished_at"]),
                                                       "d": iso(r["d"])} for r in runs[:5]],
            "restarts24": J.get("restarts24"), "alerts_bad": len(bad_alerts), "issues": iss,
            "headline": stat["analysis"]["text"]}


# ───────────────────────── 로스
def assess_ross(R: dict, h: dict, ctx: Ctx, acct: dict | None) -> dict:
    app = "ROSS"
    url = APPS[app]["url"]
    iss = []
    st = R.get("settings") or {}
    sched = st.get("autosched") or {}
    auto = st.get("auto") or {}
    risk = ((st.get("strategy_v1") or {}).get("risk") or {}).get("values") or {}
    params = sched.get("params") or auto.get("params") or {}
    capital = risk.get("account") or (st.get("main") or {}).get("account")
    max_loss = params.get("live_max_loss_usd") or risk.get("live_max_loss_usd")
    basis = ctx.basis.isoformat()
    b = (h or {}).get("body") or {}

    srv = server_level(app, h)
    if srv["level"] == "fail":
        iss.append(issue(app, "P1", "server", "Ross 서버 응답 없음", srv["text"]))

    evc = {}
    for r in R.get("ev_counts") or []:
        evc.setdefault(r["et_date"], {})[r["kind"]] = {"n": r["n"], "syms": r["syms"]}
    today_ev = evc.get(basis, {})

    # 데이터
    if not b:
        data = lv("warn", "서버 응답 없음 — 시세 연결 확인 불가" + (f" · 오늘 데이터 차단 {today_ev['data_block']['n']}회" if today_ev.get("data_block") else ""))
    elif not b.get("fmp") or not b.get("uw"):
        data = lv("fail", "시세 키 없음: " + ", ".join(k.upper() for k in ("fmp", "uw") if not b.get(k)))
        iss.append(issue(app, "P1", "keys", "Ross 시세 데이터 키 없음", data["text"]))
    elif today_ev.get("data_block"):
        data = lv("warn", f"오늘 데이터 신선도 차단 {today_ev['data_block']['n']}회 (오래된 시세로 주문 안 함)")
    elif b and ctx.phase in ("pre", "regular") and b.get("uw_ws") not in (None, "connected", "open", "ok", "live"):
        data = lv("warn", f"UW 실시간 연결 상태: {b.get('uw_ws')}")
    else:
        data = lv("ok", "FMP·UW 연결 정상" + (f" · UW 실시간 {b.get('uw_ws')}" if b.get("uw_ws") else ""))

    # 분석(자동 가동)
    start_et = sched.get("start_et") or 415
    started_today = bool(today_ev.get("start")) or auto.get("run_date") == ctx.today.isoformat()
    hm = f"{start_et // 60:02d}:{start_et % 60:02d}"
    if not ctx.session_today:
        ana = lv("idle", f"{ctx.phase_name} — 다음 거래일 {ctx.next_session} {hm} ET 자동 시작")
    elif not sched.get("enabled"):
        ana = lv("warn", "자동 가동 꺼짐" + (f" ({sched.get('off_reason')})" if sched.get("off_reason") else ""))
        iss.append(issue(app, "P2", "sched_off", "Ross 자동 가동 꺼짐", "의도한 중지인지 확인하세요. " + (sched.get("off_reason") or ""), url + "/#auto"))
    elif ctx.min < start_et:
        ana = lv("idle", f"가동 대기 — {hm} ET 시작 예정")
    elif started_today:
        ana = lv("ok", f"{basis} 자동 가동됨 · {sched.get('mode', 'live')} 모드", at=iso(auto.get("started_at")))
    elif ctx.min > start_et + 10 and ctx.min < ctx.close_min:
        ana = lv("fail", f"{hm} ET 시작 예정이었으나 가동 기록 없음")
        iss.append(issue(app, "P1", f"not_started:{basis}", "Ross 자동매매 미가동", f"{basis} {hm} ET 시작 기록 없음", url + "/#auto"))
    else:
        ana = lv("warn", "오늘 가동 기록 없음")

    # 거래(브로커·주문 엔진)
    trades = R.get("trades") or []
    open_tr = [t for t in trades if t["status"] != "closed"]
    tr_lvls = []
    if b and not b.get("toss"):
        tr_lvls.append(lv("fail", "토스증권 키 미설정"))
        iss.append(issue(app, "P1", "toss", "Ross 토스증권 연결 없음", "Render 환경변수 TOSS_CLIENT_ID/SECRET 확인"))
    if b and b.get("trading") in (False, "off", None) and sched.get("mode") == "live" and b.get("toss"):
        tr_lvls.append(lv("warn", "주문 스위치 꺼짐(TOSS_TRADING)"))
    if open_tr:
        stale = [t for t in open_tr if t["et_date"] < ctx.today.isoformat() or (t["et_date"] == basis and ctx.min >= ctx.close_min)]
        if stale:
            tr_lvls.append(lv("fail", f"장 마감 후 미청산·상태 불명 {len(stale)}건"))
            iss.append(issue(app, "P1", "open_after_close", "Ross 장 마감 후 미청산/상태 불명 포지션",
                             ", ".join(f"{t['sym']}({t['et_date']} {t['status']})" for t in stale), url + "/#auto"))
        else:
            tr_lvls.append(lv("ok", f"보유 중 {len(open_tr)}건 관리"))
    fails = [o for o in R.get("orders") or [] if o["et_date"] == basis and o["ok"] is False]
    if fails:
        nfail = sum(o["n"] for o in fails)
        tr_lvls.append(lv("warn", f"오늘 주문 실패 {nfail}건"))
        iss.append(issue(app, "P2", f"order_fail:{basis}", f"Ross 주문 실패 {nfail}건", ", ".join(f"{o['kind']} {o['n']}" for o in fails), url + "/#auto"))
    if ctx.phase in ("pre", "regular") and b.get("toss_ws") and b.get("toss_ws") not in ("connected", "open", "ok", "live", "idle"):
        tr_lvls.append(lv("warn", f"토스 실시간 {b.get('toss_ws')}"))
    if not b:
        tr_lvls.append(lv("warn", "서버 응답 없음 — 증권사 연결 확인 불가"))
    trd = max(tr_lvls, key=lambda x: LV_ORDER[x["level"]]) if tr_lvls else lv(
        "ok" if b.get("toss") else "warn", "토스증권 연결 · 주문 엔진 " + ("켜짐" if b.get("trading") not in (False, "off") else "꺼짐"))
    stat = {"server": srv, "data": data, "analysis": ana, "trade": trd}

    # 성과
    def row(t):
        d = t["data"] or {}
        ent, ext = d.get("entry_at"), d.get("exit_at")
        return {"pnl": d.get("pnl"), "r": d.get("r_mult"), "exit_ts": ext, "hold_s": (ext - ent) if ext and ent else None}
    closed = [t for t in trades if t["status"] == "closed"]
    day_closed = [t for t in closed if t["et_date"] == basis]
    day = perf([row(t) for t in day_closed])
    cum = perf([row(t) for t in closed], capital)
    reports = {r["et_date"]: r for r in R.get("reports") or []}
    rep = reports.get(basis)
    funnel_ev = {"watch": (today_ev.get("watch") or {}).get("syms", 0), "skip": (today_ev.get("skip") or {}).get("n", 0),
                 "order": (today_ev.get("order") or {}).get("n", 0), "entry": (today_ev.get("entry") or {}).get("n", 0),
                 "exit": (today_ev.get("exit") or {}).get("n", 0)}
    f = (rep or {}).get("funnel") or {}
    funnel = {"scans": f.get("scans"), "watched": f.get("watched", funnel_ev["watch"]), "signals": f.get("triggers"),
              "rejected": f.get("skips", funnel_ev["skip"]), "orders": funnel_ev["order"] or None, "fills": f.get("entries", funnel_ev["entry"]),
              "closed": len(day_closed), "source": "장 마감 리포트" if rep else "실시간 이벤트"}
    sub = [o for o in R.get("orders") or [] if o["kind"] == "auto_submit"]
    n_sub, n_bad = sum(o["n"] for o in sub), sum(o["n"] for o in sub if o["ok"] is False)
    fail_rate = round(n_bad / n_sub * 100, 1) if n_sub else None

    day_pnl = day.get("net", 0.0) if day.get("n") else 0.0
    loss_use = round(-day_pnl / max_loss * 100, 1) if (max_loss and day_pnl < 0) else 0.0
    if loss_use >= 100:
        iss.append(issue(app, "P1", f"loss_limit:{basis}", "Ross 하루 손실한도 초과", f"실현 ${day_pnl:.2f} / 한도 ${max_loss}"))
    elif loss_use >= 70:
        iss.append(issue(app, "P2", f"loss_70:{basis}", f"Ross 손실한도 {loss_use:.0f}% 사용", f"실현 ${day_pnl:.2f} / 한도 ${max_loss}"))
    # 시험 운영(누적 손실 한도)
    trial = None
    if sched.get("trial_start"):
        tr_rows = [t for t in closed if t["et_date"] >= sched["trial_start"]]
        tsum = round(sum((t["data"] or {}).get("pnl") or 0 for t in tr_rows), 2)
        cap = sched.get("trial_loss_cap")
        days = len({t["et_date"] for t in tr_rows})
        trial = {"start": sched["trial_start"], "days": sched.get("trial_days"), "pnl": tsum, "cap": cap, "n": len(tr_rows),
                 "use_pct": round(-tsum / cap * 100, 1) if cap and tsum < 0 else 0.0, "max_loss_days": sched.get("max_loss_days"), "trade_days": days}
        if cap and tsum < 0 and -tsum >= cap * 0.7:
            iss.append(issue(app, "P1" if -tsum >= cap else "P2", "trial_cap", "Ross 시험 운영 누적 손실한도 접근",
                             f"누적 ${tsum} / 한도 ${cap}"))
    # 장 마감 리포트·안전장치 점검
    eod_due = ctx.settled.isoformat()
    if tcal.is_session(ctx.settled) and eod_due not in reports and sched.get("enabled"):
        iss.append(issue(app, "P2", f"eod_missing:{eod_due}", "Ross 장 마감 리포트 미생성", f"{eod_due} 리포트가 없습니다 — 체결 동기화 확인", url + "/#report"))
    checks = []
    for rd in sorted(reports)[-1:]:
        for c in reports[rd].get("checks") or []:
            checks.append({**c, "date": rd})
            if c.get("status") not in ("OK", "해당 없음"):
                iss.append(issue(app, "P1" if c.get("status") in ("FAIL", "실패", "NG") else "P2", f"check:{rd}:{c.get('name')}",
                                 f"Ross 안전장치 점검 이상 — {c.get('name')}", f"{rd} · {c.get('detail')}", url + "/#report"))
    bad_alert = (R.get("alerts24") or {}).get("bad") or 0
    if bad_alert:
        iss.append(issue(app, "P3", "ntfy_fail", f"Ross 휴대폰 알림 발송 실패 {bad_alert}건(24h)"))

    # 계좌(선택)
    account = {"capital_setting": capital, "source": "설정값"}
    a = (acct or {}).get(app) or {}
    if a.get("ok"):
        dd = a["data"]
        bp = ((dd.get("buyingPower") or {}).get("USD")) or {}
        hold = dd.get("holdings")
        items = hold if isinstance(hold, list) else (hold or {}).get("items") or (hold or {}).get("holdings") or []
        account.update(source="토스증권 실조회", at=iso(a.get("at")), buying_power=bp, holdings=items, errors=dd.get("errors"))
    elif a.get("configured"):
        account["error"] = a.get("err")
        iss.append(issue(app, "P3", "acct_err", "Ross 토스 계좌 조회 실패", a.get("err") or ""))
    positions = [{"app": app, "account": "토스증권", "symbol": t["sym"], "qty": (t["data"] or {}).get("remain") or (t["data"] or {}).get("shares"),
                  "entry": (t["data"] or {}).get("entry"), "stop": (t["data"] or {}).get("stop"), "kind": "자동 단타", "status": t["status"]}
                 for t in open_tr]
    other = [{"app": app, "account": "토스증권", "symbol": x["symbol"], "qty": float(x["qty"] or 0), "entry": float(x["avg_price"] or 0),
              "kind": k, "status": "자동매매 제외"} for k, lst in (("장기 보유", R.get("longterm")), ("정리 대기", R.get("legacy"))) for x in (lst or [])]

    gate = (rep or (reports[max(reports)] if reports else {})).get("gate") if reports else None
    recent = [{"date": d, "pnl": (reports[d].get("perf") or {}).get("pnl"), "n": (reports[d].get("perf") or {}).get("n"),
               "win_rate": (reports[d].get("perf") or {}).get("win_rate"), "final": reports[d].get("final")} for d in sorted(reports)[-10:]]
    return {"app": app, "status": stat, "overall": worst(*stat.values()), "basis": basis, "mode": sched.get("mode") or auto.get("mode"),
            "sched": {"enabled": sched.get("enabled"), "start": hm, "entry_window": f"{(params.get('entry_start') or 420) // 60:02d}:{(params.get('entry_start') or 420) % 60:02d}"
                      f"~{(params.get('entry_end') or 690) // 60:02d}:{(params.get('entry_end') or 690) % 60:02d} ET",
                      "max_trades": params.get("max_trades"), "budget": params.get("live_budget_usd")},
            "day": day, "day_pnl": round(day_pnl, 2), "cum": cum, "funnel": funnel, "order_fail_rate": fail_rate, "orders_submitted": n_sub,
            "loss_limit": {"limit": max_loss, "used_pct": loss_use, "realized": round(day_pnl, 2)}, "trial": trial, "gate": gate,
            "report": {"date": basis, "exists": bool(rep), "final": (rep or {}).get("final")}, "checks": checks, "recent": recent,
            "positions": positions, "other_holdings": other, "account": account,
            "trades_today": [{"sym": t["sym"], "status": t["status"], "pnl": (t["data"] or {}).get("pnl"), "r": (t["data"] or {}).get("r_mult"),
                              "entry": (t["data"] or {}).get("entry"), "et_entry": (t["data"] or {}).get("et_entry"), "grade": (t["data"] or {}).get("grade"),
                              "shares": (t["data"] or {}).get("shares")} for t in trades if t["et_date"] == basis],
            "issues": iss, "headline": stat["analysis"]["text"]}


# ───────────────────────── 켈
def assess_kell(K: dict, h: dict, ctx: Ctx, acct: dict | None) -> dict:
    app = "KELL"
    url = APPS[app]["url"]
    iss = []
    st = (K.get("settings") or {}).get("main") or {}
    mode = st.get("mode", "off")
    env = {"live": "live", "paper": "paper"}.get(mode)
    mode_name = {"live": "실매매", "paper": "가상(모의)", "off": "꺼짐"}.get(mode, mode)
    b = (h or {}).get("body") or {}

    srv = server_level(app, h)
    if srv["level"] == "fail":
        iss.append(issue(app, "P1" if mode == "live" else "P2", "server", "Kell 서버 응답 없음", srv["text"]))

    jobs = K.get("jobs") or []
    nightly = [j for j in jobs if j["job"] == "nightly"]
    last_ok = next((j for j in nightly if j["ok"]), None)
    last = nightly[0] if nightly else None
    # 야간 스캔 기대일: 마감 + 3시간 지난 마지막 거래일
    exp = ctx.settled
    if not b:
        data = lv("warn", "서버 응답 없음 — DB 기록으로만 판정")
    elif b.get("fmp") is False:
        data = lv("fail", "FMP 키 없음")
        iss.append(issue(app, "P2", "fmp", "Kell FMP 키 없음"))
    elif last and last["ok"] is False:
        data = lv("warn", f"최근 야간 작업 실패: {(last['error'] or '')[-100:]}")
    else:
        data = lv("ok", "FMP 일봉 수집 정상")

    as_of = _d(last_ok["as_of"]) if last_ok else None
    watch = K.get("watch") or []
    live_watch = [w for w in watch if w["status"] in ("watch", "active")]
    if not last_ok:
        ana = lv("fail", "야간 스캔 성공 기록 없음")
    elif as_of and as_of >= exp:
        msg = f"{as_of} 야간 스캔 완료 · 통과 {last_ok.get('passed') or 0} · 실매매 감시 {len(live_watch)}"
        if not live_watch:
            msg += " (조건 충족 종목 없음 — 정상)"
        ana = lv("ok", msg, at=iso(last_ok["finished"]))
    else:
        failed = last and last["ok"] is False
        ana = lv("fail" if failed else "warn", f"야간 스캔 {'실패' if failed else '지연'} — 최신 {as_of}, 기대 {exp}")
        iss.append(issue(app, "P2", f"nightly:{exp}", f"Kell 야간 스캔 {'실패' if failed else '지연'}",
                         f"최신 성공 {as_of} · 기대 {exp}" + (f" · {(last['error'] or '')[-120:]}" if failed else ""), url + "/#/system"))

    # 거래 엔진
    eng = (K.get("engine") or [None])[0]
    pos = K.get("pos_open") or []
    tr = []
    if mode == "off":
        tr.append(lv("idle", "매매 모드 꺼짐"))
    else:
        kis = (b.get("kis") or {}).get(env) if b else None
        if b and kis is False:
            tr.append(lv("fail", f"한국투자증권 {mode_name} 키 없음"))
            iss.append(issue(app, "P1", "kis", f"Kell 한국투자증권({mode_name}) 연결 없음", "Render 환경변수 KIS_* 확인"))
        if ctx.phase == "regular" and ctx.min >= 575:      # 9:35 ET 이후(엔진 9:30 시작)
            beat = _ts(eng["last_beat"]) if eng and _d(eng["d"]) == ctx.today else None
            if not beat or time.time() - beat > 300:
                tr.append(lv("fail", "장중 엔진 신호(heartbeat) 끊김" + (f" — 마지막 {iso(beat)}" if beat else " — 오늘 기록 없음")))
                iss.append(issue(app, "P1" if mode == "live" else "P2", f"engine:{ctx.today}", "Kell 장중 엔진 중단 의심",
                                 "5분 이상 heartbeat 없음", url + "/#/today"))
            else:
                tr.append(lv("ok", f"엔진 가동 중 · {int(time.time() - beat)}초 전 신호"))
        if b and b.get("stopwatch") is False:
            tr.append(lv("warn", "손절 감시(stopwatch) 비정상"))
    unprot = [p for p in pos if not p.get("stop_active")]
    if unprot:
        tr.append(lv("fail", f"손절 보호 없는 보유 {len(unprot)}건"))
        iss.append(issue(app, "P1", "unprotected", "Kell 손절 미보호 포지션", ", ".join(p["symbol"] for p in unprot), url + "/#/positions"))
    stuck = [o for o in K.get("orders") or [] if o["status"] in ("sending", "sent", "unknown", "cancel_sent")
             and (time.time() - (_ts(o["updated"]) or _ts(o["created"]) or time.time())) > 300]
    if stuck:
        tr.append(lv("fail", f"주문 상태 불명 {len(stuck)}건"))
        iss.append(issue(app, "P1", "order_unknown", "Kell 주문 상태 불명(체결 확인 누락)",
                         ", ".join(f"{o['symbol']} {o['side']} {o['qty']} ({o['status']})" for o in stuck[:5]), url + "/#/orders"))
    trd = max(tr, key=lambda x: LV_ORDER[x["level"]]) if tr else lv("ok", f"{mode_name} 모드 · 보유 {len(pos)}건")
    stat = {"server": srv, "data": data, "analysis": ana, "trade": trd}

    # 성과
    closed = [p for p in K.get("pos_closed") or [] if not env or p["env"] == env]
    def row(p):
        ent, ext = _ts(p["entry_at"]), _ts(p["closed_at"])
        return {"pnl": p["realized"], "r": p["r_mult"], "exit_ts": ext, "hold_s": (ext - ent) if ext and ent else None}
    capital = None
    peak = (K.get("broker") or {}).get(f"peak:{env}") if env else None
    if peak:
        capital = peak.get("v")
    cum = perf([row(p) for p in closed], capital)
    if cum.get("avg_hold_s"):
        cum["avg_hold_days"] = round(cum["avg_hold_s"] / 86400, 1)
    day_closed = [p for p in closed if p["closed_at"] and p["closed_at"].astimezone(ET).date() == ctx.basis]
    day_real = round(sum(p["realized"] or 0 for p in day_closed), 2)
    mypos = [p for p in pos if not env or p["env"] == env]
    unreal, plist = 0.0, []
    for p in mypos:
        px = p.get("last_price")
        u = (px - p["entry_price"]) * (p["qty_open"] or 0) if px and p["entry_price"] else None
        if u is not None:
            unreal += u
        risk_amt = (p["entry_price"] - (p["stop"] or 0)) * (p["qty_open"] or 0) if p["stop"] else None
        plist.append({"app": app, "account": "한국투자증권" + ("" if env == "live" else " 모의"), "symbol": p["symbol"], "qty": p["qty_open"],
                      "entry": p["entry_price"], "last": px, "stop": p["stop"], "unreal": r2(u), "risk_to_stop": r2(risk_amt),
                      "days": (ctx.today - p["entry_date"]).days if p["entry_date"] else None, "kind": f"수축 돌파 {p['kind']}",
                      "protected": bool(p.get("stop_active")), "status": "보유", "realized_partial": p["realized"]})
    sig = [s for s in K.get("signals") or [] if _d(s["for_date"]) == ctx.basis]
    funnel = {"scan_as_of": iso((K.get("scan") or {}).get("as_of"))[:10] if K.get("scan") else None,
              "scanned": (K.get("scan") or {}).get("n"), "passed": (K.get("scan") or {}).get("passed"),
              "watch_live": len(live_watch), "watch_shadow": len([w for w in watch if w["status"] in ("shadow", "paper_only")]),
              "excluded": len([w for w in watch if w["status"] == "excluded"]),
              "signals": sum(s["n"] for s in sig), "entries": len([p for p in mypos if p["entry_date"] == ctx.basis]),
              "closed": len(day_closed)}
    # 계좌(선택)
    account = {"source": "가상 자본(최고치)" if env == "paper" else "설정 없음", "capital_setting": capital, "env": env}
    a = (acct or {}).get(app) or {}
    if a.get("ok") and a["data"].get("connected"):
        dd = a["data"]
        account.update(source="한국투자증권 실조회", at=iso(a.get("at")), cash_usd=dd.get("cash_usd"), summary=dd.get("summary"),
                       holdings=dd.get("holdings"), unprotected=dd.get("unprotected"), open_orders=len(dd.get("open_orders") or []))
        if dd.get("unprotected"):
            iss.append(issue(app, "P1", "acct_unprotected", "Kell 증권사 잔고 중 손절 보호 없는 종목", ", ".join(dd["unprotected"])))
    elif a.get("configured"):
        account["error"] = a.get("err") or (a.get("data") or {}).get("reason")
    return {"app": app, "status": stat, "overall": worst(*stat.values()), "mode": mode, "mode_name": mode_name, "env": env,
            "basis": ctx.basis.isoformat(), "market": {**K["market"], "as_of": iso(K["market"]["as_of"])[:10]} if K.get("market") else None,
            "watch": [{**w, "for_date": iso(w["for_date"])[:10], "earnings": iso(w["earnings"])} for w in watch],
            "funnel": funnel, "cum": cum, "day_realized": day_real, "unrealized": round(unreal, 2), "positions": plist,
            "account": account, "shadow": K.get("shadow"),
            "jobs": [{**j, "started": iso(j["started"]), "finished": iso(j["finished"]), "as_of": iso(j["as_of"])} for j in jobs[:6]],
            "engine": {"d": iso(eng["d"]) if eng else None, "last_beat": iso(eng["last_beat"]) if eng else None, "beats": eng["beats"] if eng else None},
            "issues": iss, "headline": stat["analysis"]["text"]}


# ───────────────────────── 통합 위험
def assess_risk(J: dict, R: dict, K: dict, acct: dict | None) -> dict:
    iss = []
    pos = (R.get("positions") or []) + (K.get("positions") or [])
    other = R.get("other_holdings") or []
    syms = {}
    for p in pos + other:
        syms.setdefault(p["symbol"], set()).add(p["account"])
    dup = [s for s, a in syms.items() if len(a) > 1]
    if dup:
        iss.append(issue("DASH", "P3", "dup", "두 계좌 동일 종목 보유", ", ".join(dup)))
    m = (J or {}).get("market") or {}
    unknown = sum(1 for i in (R.get("issues") or []) + (K.get("issues") or []) if "불명" in i["title"] or "미청산" in i["title"])
    stale = sum(1 for x in (J, R, K) for i in (x.get("issues") or []) if any(w in i["title"] for w in ("지연", "데이터", "미생성")))
    risk_to_stop = round(sum(p.get("risk_to_stop") or 0 for p in pos), 2)
    acc_rows = []
    ra, ka = R.get("account") or {}, K.get("account") or {}
    acc_rows.append({"name": "토스증권 (Ross)", "source": ra.get("source"), "capital": ra.get("capital_setting"),
                     "realized_today": R.get("day_pnl"), "unreal": None, "open": len(R.get("positions") or []),
                     "excluded_holdings": len(other), "error": ra.get("error")})
    acc_rows.append({"name": "한국투자증권 (Kell)" + ("" if K.get("env") == "live" else " · " + (K.get("mode_name") or "")),
                     "source": ka.get("source"), "capital": ka.get("capital_setting"), "cash": ka.get("cash_usd"),
                     "realized_today": K.get("day_realized"), "unreal": K.get("unrealized"), "open": len(K.get("positions") or []),
                     "error": ka.get("error")})
    live_cap = [x for x in (ra.get("capital_setting"),) if x]
    if K.get("env") == "live" and ka.get("capital_setting"):
        live_cap.append(ka["capital_setting"])
    total_cap = round(sum(live_cap), 2) if live_cap else None
    real_today = round((R.get("day_pnl") or 0) + (K.get("day_realized") if K.get("env") == "live" else 0), 2)
    if not os_env_has_passwords():
        iss.append(issue("DASH", "P3", "acct_setup", "증권사 실잔고 미연동",
                         "Render 환경변수 ROSS_APP_PASSWORD · KELL_APP_PASSWORD 입력 시 실제 잔고로 표시(현재 설정 자본 기준)", None))
    return {"accounts": acc_rows, "positions": pos, "other_holdings": other, "duplicates": dup, "total_capital_live": total_cap,
            "realized_today_live": real_today, "unreal_kell": K.get("unrealized"), "risk_to_stop": risk_to_stop,
            "loss_limit": R.get("loss_limit"), "trial": R.get("trial"),
            "market": {"stage": m.get("stage"), "name": m.get("name"), "defense": m.get("defense"), "change": m.get("change"),
                       "kell_regime": ((K.get("market") or {}).get("regime"))},
            "unknown_orders": unknown, "data_warnings": stale, "issues": iss,
            "note": "통합 대시보드는 조회·경보 전용입니다. 자동매매 중지·청산은 각 전략 서버가 책임집니다."}


def os_env_has_passwords() -> bool:
    import os
    return bool(os.environ.get("ROSS_APP_PASSWORD") or os.environ.get("KELL_APP_PASSWORD"))
