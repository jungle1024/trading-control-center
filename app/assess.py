"""원천 데이터 → 판정(정상·주의·장애), 성과 지표, 조치 필요 항목.

원칙(구축 가이드)
- 신호·주문 요청·실제 체결·실현손익을 구분한다.
- 실현손익과 평가손익을 합산하지 않는다.
- 휴장·장 마감 후 '신호 없음'을 장애로 보지 않는다(거래 일정 기준 판정).
- '오늘 매매 없음'과 '오늘 분석 안 함'을 구분한다.
"""
from __future__ import annotations

import os
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
        # 결과 확정 기대일: 모든 야간 작업(켈 23:15 UTC · MA 다음날 00:15 UTC)이 끝났을 시각 = 거래일 다음날 01:30 UTC
        self.settled = self.last_due(1, 1, 30)
        self.next_session = self.today if (self.session_today and self.min < self.close_min) else self._next(self.today)

    def last_due(self, day_offset: int, hh: int, mm: int) -> date:
        """'거래일 d + day_offset 일의 hh:mm UTC' 가 이미 지난 가장 최근 거래일 d (야간 작업 기대일 계산)."""
        d = self.today if self.session_today else tcal.prev_session(self.today)
        for _ in range(10):
            due = datetime(d.year, d.month, d.day, hh, mm, tzinfo=UTC) + timedelta(days=day_offset)
            if self.now >= due:
                return d
            d = tcal.prev_session(d)
        return d

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
ROSS_LOGIC = {"ross": "로스 단타", "integrated": "로스×파비오 통합"}
RTT_WARN_MS = 1500        # 토스 API 전체 p95 경고 기준
RTT_ORDER_WARN_MS = 1500  # 주문 API p95 — 넘으면 P2


def toss_rtt(t: dict | None) -> dict | None:
    """Ross /health 의 toss_rtt → 요약. 최근 5분 창이 비면 최근 50건 기준."""
    if not t:
        return None
    w, ln = t.get("window") or {}, t.get("last_n") or {}
    base = w if w.get("n") else ln
    order = (t.get("by_kind") or {}).get("order") or {}
    last = t.get("last") or {}
    out = {"n": base.get("n") or 0, "basis": "5분" if w.get("n") else f"최근 {ln.get('n') or 0}건",
           "avg": base.get("avg_ms"), "p95": base.get("p95_ms"), "max": base.get("max_ms"),
           "order_n": order.get("n") or 0, "order_p95": order.get("p95_ms"), "order_avg": order.get("avg_ms"),
           "net_errors_5m": t.get("net_errors_5m") or 0, "last_ms": last.get("ms"), "last_age_s": last.get("age_s"), "last_kind": last.get("kind")}
    if out["n"]:
        out["short"] = f"평균 {out['avg']:.0f}ms · p95 {out['p95']:.0f}ms"
        out["text"] = (f"{out['basis']} {out['n']}건 평균 {out['avg']:.0f}ms · p95 {out['p95']:.0f}ms · 최대 {out['max']:.0f}ms"
                       + (f" · 주문 p95 {out['order_p95']:.0f}ms" if out["order_p95"] else "") + f" · 네트워크 오류 5분 {out['net_errors_5m']}회")
    else:
        out["short"] = None
        out["text"] = "최근 호출 없음" + (f" · 네트워크 오류 5분 {out['net_errors_5m']}회" if out["net_errors_5m"] else "")
    return out


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
    # 거래: ETF 200일 자동매매 엔진(계좌 E, 한국투자 모의투자) — 2026-11-24 추가
    eng = J.get("engine") or {}
    emode = eng.get("mode", "off")
    emode_name = {"off": "꺼짐", "paper": "가상", "live": "실매매(한국투자 모의)"}.get(emode, emode)
    is_live = emode == "live"
    otext = str(b.get("orders") or "")
    tr = []
    if emode == "off":
        tr.append(lv("idle", "ETF 200일 엔진 꺼짐 — 분석만"))
    else:
        lt = b.get("live_engine")
        age = None
        if lt:
            try:
                age = (ctx.now - datetime.fromisoformat(str(lt))).total_seconds()
            except ValueError:
                age = None
        if b and (age is None or age > 180):
            tr.append(lv("fail", "ETF 엔진 루프 신호 없음" + (f" — {int(age)}초 전" if age else "")))
            iss.append(issue(app, "P1" if is_live else "P2", "engine", "Jones ETF 엔진 루프 정지 의심", f"엔진 마지막 신호 {lt or '없음'}", url + "/#run"))
        elif b:
            tr.append(lv("ok", f"ETF 엔진 {emode_name} · {int(age)}초 전 신호"))
        if is_live and b and ("미연결" in otext or "스위치 꺼짐" in otext):
            tr.append(lv("fail", otext))
            iss.append(issue(app, "P1", "broker", "Jones 실매매 모드인데 주문 불가", otext, url + "/#run"))
    rep = J.get("live_rep") or {}
    bad_checks = [c for c in (rep.get("checks") or []) if c.get("ok") is False]
    unknown = sum(o["n"] for o in J.get("porders") or [] if o["mode"] == "live" and o["code"] == "unknown")
    if emode != "off" and bad_checks:
        halt = [c["name"] for c in bad_checks if "대사" in c["name"]]
        tr.append(lv("fail" if halt else "warn", f"{iso(rep.get('d'))[:10]} 리포트 점검 이상 {len(bad_checks)}건"))
        iss.append(issue(app, "P1" if (halt and is_live) else "P2", f"live_rep:{iso(rep.get('d'))[:10]}",
                         "Jones 실매매 자동 정지(대사 실패)" if (halt and is_live) else "Jones ETF 엔진 장 마감 점검 이상",
                         " · ".join(c["name"] + (": " + "; ".join(map(str, c.get("detail") or []))[:80] if c.get("detail") else "") for c in bad_checks), url + "/#run"))
    if unknown:
        tr.append(lv("fail", f"주문 접수 불확실 {unknown}건"))
        iss.append(issue(app, "P1", "order_unknown", "Jones 주문 접수 불확실(실매매 신규 매수 정지)", f"{unknown}건 — 체결내역·잔고 확인 필요", url + "/#run"))
    ebook = [p for p in J.get("live_pos") or [] if p["mode"] == emode]
    trd = max(tr, key=lambda x: LV_ORDER[x["level"]]) if tr else lv("ok", f"ETF 엔진 {emode_name} · 보유 {len(ebook)}")
    stat = {"server": srv, "data": data, "analysis": ana, "trade": trd}
    # 계좌 E 성과·보유 (평가가는 앱 장부에만 있으므로 DB에서는 진입가·손절만)
    etr = [t for t in J.get("live_trades") or [] if t["mode"] == emode]
    def _row(t):
        ed = _d(t["exit_d"]); sd = _d(t["entry_d"])
        return {"pnl": t["pnl"], "r": t["r"], "exit_ts": datetime.combine(ed, datetime.min.time(), UTC).timestamp() if ed else None,
                "hold_s": (ed - sd).days * 86400 if ed and sd else None}
    budget = eng.get("budget")
    e_cum = perf([_row(t) for t in etr], budget)
    if e_cum.get("avg_hold_s") is not None:
        e_cum["avg_hold_days"] = round(e_cum["avg_hold_s"] / 86400, 1)
    e_day = round(sum(t["pnl"] or 0 for t in etr if _d(t["exit_d"]) == ctx.basis), 2)
    e_pos = [{"app": app, "account": "한국투자 모의(Jones E)" if is_live else "Jones E 가상", "symbol": p["symbol"], "qty": p["qty"],
              "entry": p["entry"], "last": None, "stop": p["stop"], "unreal": None,
              "risk_to_stop": r2((p["entry"] - p["stop"]) * p["qty"]) if p["entry"] and p["stop"] else None,
              "days": (ctx.today - _d(p["opened"])).days if _d(p["opened"]) else None, "kind": "ETF 200일 규칙",
              "protected": bool(p["stop"]), "status": "보유"} for p in ebook]
    pa = J.get("paper") or {}
    paper = [{"key": k, "name": v.get("name"), "equity": (v.get("book") or {}).get("equity"), "ret_pct": (v.get("stats") or {}).get("ret_pct"),
              "open_n": v.get("open_n"), "trades": (v.get("stats") or {}).get("trades"), "win_rate": (v.get("stats") or {}).get("win_rate"),
              "mdd_pct": (v.get("stats") or {}).get("mdd_pct"), "month_pct": (v.get("book") or {}).get("month_pct"),
              "month_blocked": (v.get("book") or {}).get("month_blocked"), "open_risk_pct": (v.get("book") or {}).get("open_risk_pct"),
              "as_of": (v.get("book") or {}).get("as_of")} for k, v in sorted((pa.get("accts") or {}).items())]
    etf = {"mode": emode, "mode_name": emode_name, "live": is_live, "budget": budget, "max_order": eng.get("max_order"), "etfs": eng.get("etfs"),
           "broker": otext, "changed_at": iso(J.get("engine_at")), "cum": e_cum, "day_realized": e_day, "positions": e_pos,
           "report": {"d": iso(rep.get("d"))[:10] if rep.get("d") else None, "bad": [c["name"] for c in bad_checks],
                      "n_checks": len(rep.get("checks") or [])} if rep else None,
           "orders14": {"ok": sum(o["n"] for o in J.get("porders") or [] if o["ok"]), "fail": sum(o["n"] for o in J.get("porders") or [] if o["ok"] is False)},
           "paper": paper, "paper_start": pa.get("start")}

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
            "restarts24": J.get("restarts24"), "alerts_bad": len(bad_alerts), "etf": etf, "positions": e_pos,
            "issues": iss, "headline": stat["analysis"]["text"]}


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
    # 토스 API 왕복 시간(2026-11-25 /health toss_rtt) — 단타는 주문 지연이 곧 체결가 차이
    rtt = toss_rtt(b.get("toss_rtt"))
    in_sess = ctx.session_today and ctx.phase in ("pre", "regular")
    if rtt and in_sess:
        if rtt["net_errors_5m"] >= 3:
            tr_lvls.append(lv("fail", f"토스 연결 오류 5분간 {rtt['net_errors_5m']}회"))
            iss.append(issue(app, "P2", f"toss_net:{basis}", "Ross 토스 연결 오류 반복", f"최근 5분 네트워크 오류 {rtt['net_errors_5m']}회 · {rtt['text']}", url + "/#auto"))
        elif (rtt.get("order_p95") or 0) > RTT_ORDER_WARN_MS:
            tr_lvls.append(lv("warn", f"토스 주문 응답 지연 p95 {rtt['order_p95']:.0f}ms"))
            iss.append(issue(app, "P2", f"toss_slow_order:{basis}", "Ross 토스 주문 응답 지연",
                             f"주문 p95 {rtt['order_p95']:.0f}ms (기준 {RTT_ORDER_WARN_MS}ms) · {rtt['text']}", url + "/#auto"))
        elif (rtt.get("p95") or 0) > RTT_WARN_MS or rtt["net_errors_5m"]:
            tr_lvls.append(lv("warn", f"토스 응답 지연 · {rtt['text']}"))
            iss.append(issue(app, "P3", f"toss_slow:{basis}", "Ross 토스 응답 지연", rtt["text"], url + "/#auto"))
    trd = max(tr_lvls, key=lambda x: LV_ORDER[x["level"]]) if tr_lvls else lv(
        "ok" if b.get("toss") else "warn", "토스증권 연결 · 주문 엔진 " + ("켜짐" if b.get("trading") not in (False, "off") else "꺼짐")
        + (f" · 응답 {rtt['short']}" if rtt and rtt.get("short") else ""))
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
    # 실매매 로직(로스 / 로스×파비오 통합)과 그림자 라인(반대 로직 가상 매매) 비교 — V2.0α-L
    sm = st.get("strategy_mode") or {}
    logic = sm.get("mode") or "ross"
    logic_of = lambda t: (t["data"] or {}).get("strategy_mode") or "ross"
    by_logic = {lg: {"name": ROSS_LOGIC.get(lg, lg), "day": perf([row(t) for t in day_closed if logic_of(t) == lg]),
                     "cum": perf([row(t) for t in closed if logic_of(t) == lg], capital)}
                for lg in sorted({logic_of(t) for t in closed} | {logic})}
    sh = R.get("shadow") or []
    sh_closed = [t for t in sh if t["status"] == "closed"]
    shadow = {"logic": "ross" if logic == "integrated" else "integrated", "open": len([t for t in sh if t["status"] != "closed"]),
              "day": perf([row(t) for t in sh_closed if t["et_date"] == basis]), "cum": perf([row(t) for t in sh_closed], capital),
              "since": min((t["et_date"] for t in sh), default=None)}
    shadow["name"] = ROSS_LOGIC.get(shadow["logic"], shadow["logic"])
    pg = ((R.get("review") or {}).get("progress")) or {}
    targets = {k: {kk: (pg.get(k) or {}).get(kk) for kk in ("target_pct", "target_usd", "pnl", "ret_pct", "achv_pct", "gap_usd", "days_left", "need_per_day", "n")}
               for k in ("week", "month", "year") if pg.get(k)}
    targets_meta = {"period": (R.get("review") or {}).get("period"), "kind": (R.get("review") or {}).get("kind"),
                    "at": iso((R.get("review") or {}).get("created")), "base": pg.get("base")} if pg else None
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
            "logic": {"mode": logic, "name": ROSS_LOGIC.get(logic, logic), "since": iso(sm.get("at"))}, "by_logic": by_logic, "shadow": shadow,
            "targets": targets, "targets_meta": targets_meta, "toss_rtt": rtt,
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
    # 야간 스캔 기대일: 크론(거래일 23:15 UTC) + 60분 여유가 지난 마지막 거래일 — 서머타임과 무관
    exp = ctx.last_due(1, 0, 15)
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
    al = K.get("alerts24") or {}
    if al.get("bad"):
        iss.append(issue(app, "P3", "ntfy_fail", f"Kell 휴대폰 알림 발송 실패 {al['bad']}건(24h)", "", url + "/#/system"))
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
            "account": account, "shadow": K.get("shadow"), "ntfy": {"configured": b.get("ntfy") if b else None, **(al or {})},
            "jobs": [{**j, "started": iso(j["started"]), "finished": iso(j["finished"]), "as_of": iso(j["as_of"])} for j in jobs[:6]],
            "engine": {"d": iso(eng["d"]) if eng else None, "last_beat": iso(eng["last_beat"]) if eng else None, "beats": eng["beats"] if eng else None},
            "issues": iss, "headline": stat["analysis"]["text"]}


# ───────────────────────── 20·60 이동평균선 추세 (MA)
MA_KIND = {"A": "A 하락→상승 전환", "B": "B 20/60 정배열 초기", "C": "C 상승 중 눌림", "down": "하락", "flat": "횡보", "excluded": "제외"}
MA_MODE = {"research": "연구(신호만)", "paper": "가상(모의)", "approve": "실매매·수동 승인", "auto": "실매매·완전 자동", "exit_only": "청산 전용"}
MA_COST_SIDE = 0.001
MA_DAY_LOSS = 0.02


def assess_ma(M: dict, h: dict, ctx: Ctx, acct: dict | None) -> dict:
    app = "MA"
    url = APPS[app]["url"]
    iss = []
    st = M.get("settings") or {}
    mode = st.get("mode", "research")
    book = {"paper": "paper", "approve": "live", "auto": "live"}.get(mode)
    live = book == "live"
    mode_name = MA_MODE.get(mode, mode)
    b = (h or {}).get("body") or {}
    equity = st.get("equity")

    srv = server_level(app, h)
    if srv["level"] == "fail":
        iss.append(issue(app, "P1" if live else "P2", "server", "MA20·60 서버 응답 없음", srv["text"]))

    jobs = M.get("jobs") or []
    nightly = [j for j in jobs if j["job"] == "nightly"]
    last_ok = next((j for j in nightly if j["ok"]), None)
    last = nightly[0] if nightly else None
    if not b:
        data = lv("warn", "서버 응답 없음 — DB 기록으로만 판정")
    elif b.get("fmp") is False:
        data = lv("fail", "FMP 키 없음")
        iss.append(issue(app, "P2", "fmp", "MA20·60 FMP 키 없음"))
    elif last and last["ok"] is False and (not last_ok or last["id"] > last_ok["id"]):
        data = lv("warn", f"최근 야간 작업 실패: {(last['error'] or '')[-100:]}")
    else:
        data = lv("ok", "FMP 일봉 수집 정상")

    # 분석: 야간 크론(거래일 다음날 00:15 UTC) + 60분 여유
    exp = ctx.last_due(1, 1, 15)
    as_of = _d(last_ok["as_of"]) if last_ok else None
    watch = M.get("watch") or []
    w_live = [w for w in watch if w["status"] == "watch"]
    w_paper = [w for w in watch if w["status"] == "paper_only"]
    if not last_ok:
        ana = lv("fail", "야간 분석 성공 기록 없음")
        iss.append(issue(app, "P2", "no_nightly", "MA20·60 야간 분석 기록 없음", "", url + "/#today"))
    elif as_of and as_of >= exp:
        k = last_ok.get("kinds") or {}
        msg = (f"{as_of} 야간 분석 완료 · A {k.get('A', 0)} · B {k.get('B', 0)} · C {k.get('C', 0)} · 신호 {last_ok.get('signals') or 0}"
               f" · 매수 가능 {last_ok.get('buyable') or 0} · 진입 대기 {len(w_live) + len(w_paper)}")
        if not (w_live or w_paper):
            msg += " (조건 충족 종목 없음 — 정상)"
        ana = lv("ok", msg, at=iso(last_ok["finished"]))
    else:
        failed = last and last["ok"] is False
        ana = lv("fail" if failed else "warn", f"야간 분석 {'실패' if failed else '지연'} — 최신 {as_of}, 기대 {exp}")
        iss.append(issue(app, "P2", f"nightly:{exp}", f"MA20·60 야간 분석 {'실패' if failed else '지연'}",
                         f"최신 성공 {as_of} · 기대 {exp}" + (f" · {(last['error'] or '')[-120:]}" if failed else ""), url + "/#today"))

    # 거래(장중 관리·주문)
    pos = M.get("pos_open") or []
    mypos = [p for p in pos if p["book"] == book] if book else []
    tr = []
    a = (acct or {}).get(app) or {}
    bk = ((a.get("data") or {}).get("book")) if a.get("ok") else None
    if mode == "research":
        tr.append(lv("idle", "연구 모드 — 신호만 기록, 매매 없음"))
    else:
        br = M.get("broker")
        if br and "실패" in (br["msg"] or ""):
            tr.append(lv("warn" if not live else "fail", f"최근 키움 연결 점검 실패 ({tm_short(br['at'])})"))
            if live:
                iss.append(issue(app, "P1", "kiwoom", "MA20·60 키움증권 연결 실패", br["msg"], url + "/#today"))
        if ctx.phase == "regular" and ctx.min >= 575:
            if bk:
                last_tick = (bk.get("state") or {}).get("last_tick")
                age = time.time() - float(last_tick) if last_tick else None
                if age is None or age > 300:
                    tr.append(lv("fail", "장중 관리 루프 신호 끊김" + (f" — {int(age)}초 전" if age else " — 오늘 기록 없음")))
                    iss.append(issue(app, "P1" if live else "P2", f"engine:{ctx.today}", "MA20·60 장중 관리 중단 의심",
                                     "5분 이상 장중 관리 신호 없음", url + "/#today"))
                else:
                    tr.append(lv("ok", f"장중 관리 가동 · {int(age)}초 전 점검"))
            elif not a.get("configured"):
                tr.append(lv("warn" if mypos else "ok", "장중 관리 상태 확인 불가 — MA_APP_PASSWORD 미설정" if mypos else f"{mode_name} 모드 · 보유 없음"))
    noprot = [p for p in mypos if not p.get("stop")]
    if noprot:
        tr.append(lv("fail", f"손절가 없는 보유 {len(noprot)}건"))
        iss.append(issue(app, "P1" if live else "P2", "nostop", "MA20·60 손절가 없는 포지션", ", ".join(p["symbol"] for p in noprot), url + "/#today"))
    locks = M.get("locks") or []
    stuck = [o for o in M.get("orders") or [] if o["status"] == "unknown" or
             (o["status"] == "sending" and time.time() - (_ts(o["sent_at"]) or _ts(o["created"]) or time.time()) > 300)]
    if locks or stuck:
        syms = sorted({x["symbol"] for x in locks} | {o["symbol"] for o in stuck})
        tr.append(lv("fail", f"주문 상태 불명·종목 잠금 {len(syms)}건"))
        iss.append(issue(app, "P1", "order_unknown", "MA20·60 주문 상태 불명(접수 여부 불확실)", ", ".join(syms) + " — 대사 필요", url + "/#today"))
    e24 = M.get("errs24") or {}
    if e24.get("n"):
        tr.append(lv("warn", f"24시간 오류·거부 {e24['n']}건"))
    trd = max(tr, key=lambda x: LV_ORDER[x["level"]]) if tr else lv("ok", f"{mode_name} 모드 · 보유 {len(mypos)}건")
    stat = {"server": srv, "data": data, "analysis": ana, "trade": trd}

    # 성과 (청산 완료, 비용 반영 순손익)
    closed = [p for p in M.get("pos_closed") or [] if not book or p["book"] == book]
    def row(p):
        fee = MA_COST_SIDE * 2 * (p["entry"] or 0) * (p["qty"] or 0)
        ent, ext = _ts(p["opened_at"]), _ts(p["closed_at"])
        base = (p["risk_ps"] or 0) * (p["qty"] or 0)
        return {"pnl": (p["realized"] or 0) - fee, "r": ((p["realized"] or 0) - fee) / base if base else None,
                "exit_ts": ext, "hold_s": (ext - ent) if ext and ent else None}
    cum = perf([row(p) for p in closed], equity)
    if cum.get("avg_hold_s"):
        cum["avg_hold_days"] = round(cum["avg_hold_s"] / 86400, 1)
    day_closed = [p for p in closed if p["closed_at"] and p["closed_at"].astimezone(ET).date() == ctx.basis]
    day_real = round(sum(row(p)["pnl"] for p in day_closed), 2)
    # 보유 — 실시간 평가는 앱 장부 API(비밀번호 설정 시), 없으면 최근 장 마감 리포트의 종가
    live_px = {p["symbol"]: p for p in (bk or {}).get("open") or []}
    mrep = M.get("report") or {}
    px_src = "실시간" if live_px else None
    if not live_px and mrep.get("book") == book:
        live_px = {h["symbol"]: {"price": h.get("close")} for h in mrep.get("holdings") or [] if h.get("close")}
        px_src = f"{iso(mrep.get('day'))[:10]} 종가" if live_px else None
    rep_issues = mrep.get("issues") or []
    if rep_issues:
        iss.append(issue(app, "P3", f"rep_issue:{iso(mrep.get('day'))[:10]}", f"MA20·60 장 마감 리포트 이상 분류 {len(rep_issues)}건",
                         " · ".join(f"{x.get('class')}: {x.get('detail')}" for x in rep_issues)[:200], url + "/#today"))
    unreal, plist = 0.0, []
    for p in mypos:
        lp = live_px.get(p["symbol"]) or {}
        px = lp.get("price")
        u = (px - p["entry"]) * (p["qty_open"] or 0) if px and p["entry"] else None
        if u is not None:
            unreal += u
        plist.append({"app": app, "account": "키움증권" + ("" if live else " 가상"), "symbol": p["symbol"], "qty": p["qty_open"],
                      "entry": p["entry"], "last": px, "stop": p["stop"], "unreal": r2(u),
                      "risk_to_stop": r2((p["entry"] - p["stop"]) * (p["qty_open"] or 0)) if p["stop"] else None,
                      "days": (ctx.today - p["opened_at"].astimezone(ET).date()).days if p["opened_at"] else None,
                      "kind": f"20·60 {p['kind']}형", "protected": bool(p.get("stop")), "status": "다음 개장 청산 예정" if p.get("exit_next_open") else "보유"})
    loss_use = round(-day_real / (equity * MA_DAY_LOSS) * 100, 1) if (equity and day_real < 0) else 0.0
    if loss_use >= 100:
        iss.append(issue(app, "P1" if live else "P2", f"loss:{ctx.basis}", "MA20·60 하루 손실한도(R4 2%) 도달",
                         f"실현 ${day_real} / 한도 ${equity * MA_DAY_LOSS:.0f}"))
    scan = {r["kind"]: r for r in M.get("scan") or []}
    sig = sum(r["sig"] for r in scan.values())
    buy = sum(r["buy"] for r in scan.values())
    funnel = {"as_of": iso(M.get("scan_as_of"))[:10] if M.get("scan_as_of") else None,
              "scanned": sum(r["n"] for r in scan.values()), "kinds": {k: scan.get(k, {}).get("n", 0) for k in MA_KIND},
              "signals": sig, "buyable": buy, "watch_live": len(w_live), "watch_paper": len(w_paper),
              "excluded": len([w for w in watch if w["status"] == "excluded"]),
              "entries": len([p for p in mypos if p["opened_at"] and p["opened_at"].astimezone(ET).date() == ctx.basis]),
              "closed": len(day_closed), "fails": M.get("fails") or []}
    res = M.get("research") or {}
    appr = res.get("approval") or {}
    research = {"name": res.get("name"), "at": iso(res.get("created")), "status": res.get("status"),
                "kinds": {k: {"pass": (v or {}).get("pass"), **{kk: ((v or {}).get("holdout") or {}).get(kk) for kk in ("n", "pf", "win", "avg_r")}}
                          for k, v in appr.items() if k in ("A", "B", "C")}}
    account = {"source": "전략 자본(설정)", "capital_setting": equity, "env": book}
    if a.get("ok"):
        fd = (a.get("data") or {}).get("funds") or {}
        account.update(source="키움증권 실조회", at=iso(a.get("at")), cash_usd=fd.get("usd_cash"), orderable_usd=fd.get("usd_orderable"),
                       krw_cash=fd.get("krw_cash"), error=a.get("err"))
    elif a.get("configured"):
        account["error"] = a.get("err")
    m = M.get("market") or {}
    md = m.get("data") or {}
    return {"app": app, "status": stat, "overall": worst(*stat.values()), "mode": mode, "mode_name": mode_name, "book": book, "live": live,
            "basis": ctx.basis.isoformat(), "settings": {k: st.get(k) for k in ("equity", "risk_rate", "max_positions", "max_new_per_day", "max_order_usd", "type_status")},
            "market": {"as_of": iso(m.get("as_of"))[:10] if m.get("as_of") else None, "SPY": md.get("SPY"), "QQQ": md.get("QQQ"), "breadth": md.get("breadth")},
            "watch": [{**w, "for_date": iso(w["for_date"])[:10]} for w in watch], "funnel": funnel, "cum": cum, "day_realized": day_real,
            "unrealized": round(unreal, 2) if live_px else None, "unreal_src": px_src, "positions": plist,
            "report": {"day": iso(mrep.get("day"))[:10] if mrep.get("day") else None, "issues": rep_issues, "perf": mrep.get("perf")} if mrep else None,
            "improve": {"at": iso((M.get("improve") or {}).get("created")), "status": (M.get("improve") or {}).get("status"),
                        "n": (M.get("improve") or {}).get("n"), "passed": (M.get("improve") or {}).get("passed")} if M.get("improve") else None,
            "version": {"vid": (M.get("version") or {}).get("vid"), "purpose": (M.get("version") or {}).get("purpose"),
                        "since": iso((M.get("version") or {}).get("activated_at"))}, "targets": M.get("targets"), "account": account, "research": research,
            "loss_limit": {"limit": round(equity * MA_DAY_LOSS, 2) if equity else None, "used_pct": loss_use, "realized": day_real},
            "jobs": [{**j, "started": iso(j["started"]), "finished": iso(j["finished"]), "as_of": iso(j["as_of"])} for j in jobs[:6]],
            "protect_n": M.get("protect_n"), "issues": iss, "headline": stat["analysis"]["text"]}


def tm_short(x):
    t = _ts(x)
    return datetime.fromtimestamp(t, ET).strftime("%m-%d %H:%M ET") if t else "-"


# ───────────────────────── V5 통합 자동매매 (A VCP · B 모멘텀 · C 20/60 눌림 — KIS 한 계좌, 전용 DB)
V5_MODE = {"off": "꺼짐", "sim": "내부 가상", "paper": "KIS 모의투자", "live": "KIS 실매매"}
V5_STRAT = {"A": "A 미너비니 VCP 수축돌파", "B": "B 로스 카메론 모멘텀(가상 전용)", "C": "C 20/60 눌림목"}
V5_REGIME = {"BULL": "강세장", "MIXED": "혼조장", "BEAR": "약세장", "PANIC": "급격한 위험장"}


def assess_v5(V: dict, h: dict, ctx: Ctx, acct: dict | None) -> dict:
    app = "V5"
    url = APPS[app]["url"]
    iss = []
    st = V.get("settings") or {}
    modes = st.get("modes") or {}
    any_live = any(v == "live" for v in modes.values())
    any_kis = any(v in ("paper", "live") for v in modes.values())
    mode_name = " · ".join(f"{k} {V5_MODE.get(v, v)}" for k, v in sorted(modes.items())) or "—"
    capital = (st.get("capital") or {}).get("usd")
    risk = st.get("risk") or {}
    b = (h or {}).get("body") or {}

    srv = server_level(app, h)
    if srv["level"] == "fail":
        iss.append(issue(app, "P1" if any_live else "P2", "server", "V5 서버 응답 없음", srv["text"]))

    hb = (V.get("heartbeat") or {}).get("engine") or {}
    hbd = hb.get("data") or {}
    age = time.time() - _ts(hb["at"]) if hb.get("at") else None
    # 데이터: 시세 원천(FMP·UW·KIS 웹소켓) 상태와 데이터 차단
    ev = {r["kind"]: r for r in V.get("ev24") or []}
    blocks = (ev.get("data_block") or {}).get("n", 0)
    fmp = hbd.get("fmp") or {}
    if b and b.get("engine") is False:
        data = lv("warn", "엔진 꺼짐(ENGINE_ENABLED)")
    elif fmp.get("errors") or fmp.get("r429"):
        data = lv("warn", f"FMP 오류 {fmp.get('errors', 0)} · 429 {fmp.get('r429', 0)}" + (f" · {fmp.get('last_err')}" if fmp.get("last_err") else ""))
    else:
        data = lv("ok", f"시세 정상 · KIS 웹소켓 {hbd.get('ws') or '—'}" + (f" · 24h 데이터 차단 {blocks}건(오래된 시세 주문 안 함)" if blocks else ""))

    # 분석: 야간 배치(크론 거래일 23:15 UTC) + 60분 여유 → 시장 환경·후보 날짜
    exp = ctx.last_due(1, 0, 15)
    reg = V.get("regime") or {}
    as_of = _d(reg.get("d"))
    cands = V.get("cands") or []
    by_s = {}
    for c in cands:
        by_s[c["strategy"]] = by_s.get(c["strategy"], 0) + 1
    if not as_of:
        ana = lv("fail", "야간 배치 기록 없음")
    elif as_of >= exp:
        ana = lv("ok", f"{as_of} 야간 배치 완료 · 시장 {V5_REGIME.get(reg.get('regime'), reg.get('label') or '—')} · 후보 "
                       + (" · ".join(f"{k} {v}" for k, v in sorted(by_s.items())) or "없음 (조건 충족 종목 없음 — 정상)"))
    else:
        ana = lv("warn", f"야간 배치 지연 — 최신 {as_of}, 기대 {exp}")
        iss.append(issue(app, "P2", f"nightly:{exp}", "V5 야간 배치 지연", f"최신 {as_of} · 기대 {exp}", url + "/#today"))

    # 거래: 엔진 하트비트·주문 상태·보유 손절
    tr = []
    if b and b.get("engine") is False:
        tr.append(lv("warn" if any_kis else "idle", "엔진 꺼짐"))
    elif age is None or age > 180:
        lvl = "fail" if ctx.phase in ("pre", "regular") else "warn"
        tr.append(lv(lvl, "엔진 하트비트 끊김" + (f" — {int(age)}초 전" if age else "")))
        iss.append(issue(app, "P1" if (any_live and lvl == "fail") else "P2", f"engine:{ctx.today}", "V5 엔진 하트비트 끊김",
                         f"마지막 {iso(hb.get('at')) or '없음'} · 워치독이 손절을 대신 집행", url + "/#today"))
    else:
        tr.append(lv("ok", f"엔진 가동 · {int(age)}초 전 신호 · {hbd.get('phase') or '-'}"))
    if hbd.get("errors"):
        tr.append(lv("warn", f"엔진 오류 {len(hbd['errors'])}건: {str(hbd['errors'][-1])[:80]}"))
    unk = [i for i in V.get("intents") or [] if i["status"] == "UNKNOWN" or
           (i["status"] in ("SENT", "SENDING") and time.time() - (_ts(i["at"]) or time.time()) > 300)]
    if unk:
        tr.append(lv("fail", f"주문 상태 불명 {len(unk)}건"))
        iss.append(issue(app, "P1", "order_unknown", "V5 주문 상태 불명(대사 필요)",
                         ", ".join(f"{i['sym']} {i['side']} {i['strategy']}({i['status']})" for i in unk[:5]), url + "/#today"))
    pos = V.get("positions") or []
    nostop = [p for p in pos if not p.get("stop")]
    if nostop:
        tr.append(lv("fail", f"손절가 없는 보유 {len(nostop)}건"))
        iss.append(issue(app, "P1" if any(p["mode"] in ("paper", "live") for p in nostop) else "P2", "nostop",
                         "V5 손절가 없는 포지션", ", ".join(p["sym"] for p in nostop), url + "/#today"))
    trd = max(tr, key=lambda x: LV_ORDER[x["level"]]) if tr else lv("ok", "정상")
    stat = {"server": srv, "data": data, "analysis": ana, "trade": trd}

    # 장 마감 리포트 안전장치(11개)
    rep = V.get("report") or {}
    checks = rep.get("checks") or {}
    bad = {k: v for k, v in checks.items() if v not in ("OK",) and not str(v).endswith("건")}
    for k, v in bad.items():
        iss.append(issue(app, "P1" if (any_live and k in ("상태 불명 주문 없음", "청산 실패 없음", "손절 감시 공백 없음", "주인 없는 보유 없음")) else "P2",
                         f"check:{iso(rep.get('d'))[:10]}:{k}", f"V5 안전장치 점검 이상 — {k}", f"{iso(rep.get('d'))[:10]} · {v}", url + "/#today"))
    al = V.get("alerts24") or {}
    if al.get("bad"):
        iss.append(issue(app, "P3", "ntfy_fail", f"V5 휴대폰 알림 발송 실패 {al['bad']}건(24h)"))

    # 성과(청산 완료 · 전략별)
    trades = V.get("trades") or []
    def row(t):
        o, c = _ts(t["opened"]), _ts(t["closed"])
        return {"pnl": t["pnl"], "r": t["r_mult"], "exit_ts": c, "hold_s": (c - o) if o and c else None}
    cum = perf([row(t) for t in trades], capital)
    if cum.get("avg_hold_s") is not None:
        cum["avg_hold_days"] = round(cum["avg_hold_s"] / 86400, 1)
    by_strat = {s: perf([row(t) for t in trades if t["strategy"] == s], (capital or 0) * (risk.get("alloc") or {}).get(s, 0) / 100 or None) for s in "ABC"}
    day = [t for t in trades if t["closed"] and t["closed"].astimezone(ET).date() == ctx.basis]
    day_real = round(sum(t["pnl"] or 0 for t in day), 2)
    eq = (V.get("equity") or [None])[0] or {}
    plist = [{"app": app, "account": "KIS " + V5_MODE.get(p["mode"], p["mode"]) + " (V5)", "symbol": p["sym"], "qty": p["qty"], "entry": p["avg"],
              "last": None, "stop": p["stop"], "unreal": None,
              "risk_to_stop": r2((p["avg"] - p["stop"]) * p["qty"]) if p.get("avg") and p.get("stop") else None,
              "days": (ctx.today - p["opened_at"].astimezone(ET).date()).days if p.get("opened_at") else None,
              "kind": f"V5 {p['strategy']} {p.get('setup') or ''}".strip(), "protected": bool(p.get("stop")), "status": p.get("state") or "보유"} for p in pos]
    day_loss_lim = (capital or 0) * (risk.get("day_loss_pct") or 0) / 100
    loss_use = round(-day_real / day_loss_lim * 100, 1) if (day_loss_lim and day_real < 0) else 0.0
    if loss_use >= 100:
        iss.append(issue(app, "P1" if any_live else "P2", f"loss:{ctx.basis}", "V5 하루 손실한도 도달", f"실현 ${day_real} / 한도 ${day_loss_lim:.0f}"))
    dd = round((eq.get("equity") / eq.get("peak") - 1) * 100, 2) if eq.get("equity") and eq.get("peak") else None
    if dd is not None and risk.get("max_dd_pct") and -dd >= risk["max_dd_pct"] * 0.8:
        iss.append(issue(app, "P1" if any_live else "P2", "dd", "V5 계좌 낙폭 한도 접근", f"고점 대비 {dd}% / 한도 −{risk['max_dd_pct']}%"))
    rv = ((V.get("review") or {}).get("perf") or {}).get("periods") or {}
    targets = {per: {s: {k: (rv.get(per) or {}).get(s, {}).get(k) for k in ("pct", "target", "achieve", "n")} for s in "ABC"} for per in ("week", "month") if per in rv}
    ver = V.get("version") or {}
    return {"app": app, "status": stat, "overall": worst(*stat.values()), "modes": modes, "mode_name": mode_name, "live": any_live, "kis": any_kis,
            "basis": ctx.basis.isoformat(), "capital": capital, "risk": {k: risk.get(k) for k in ("alloc", "risk_per_trade_pct", "day_loss_pct", "max_dd_pct", "max_symbol_pct")},
            "regime": {"d": iso(reg.get("d"))[:10] if reg.get("d") else None, "code": reg.get("regime"), "label": reg.get("label"), "why": reg.get("why")},
            "cand_d": iso(V.get("cand_d"))[:10] if V.get("cand_d") else None, "cands_by": by_s, "cands": cands[:15],
            "cum": cum, "by_strat": by_strat, "day_realized": day_real, "positions": plist,
            "equity": {"equity": eq.get("equity"), "peak": eq.get("peak"), "day_pnl": eq.get("day_pnl"), "dd_pct": dd, "d": iso(eq.get("d"))[:10] if eq.get("d") else None},
            "loss_limit": {"limit": round(day_loss_lim, 2) if day_loss_lim else None, "used_pct": loss_use, "realized": day_real},
            "report": {"d": iso(rep.get("d"))[:10] if rep.get("d") else None, "final": rep.get("final"), "checks": checks, "bad": list(bad),
                       "today": rep.get("today")} if rep else None,
            "engine": {"age": int(age) if age is not None else None, "phase": hbd.get("phase"), "ws": hbd.get("ws"), "cands": hbd.get("cands"),
                       "positions": hbd.get("positions"), "watchdog_at": iso(((V.get("heartbeat") or {}).get("watchdog") or {}).get("at"))},
            "data_blocks24": blocks, "nightly_last": iso((V.get("nightly") or {}).get("at")),
            "version": {"label": ver.get("label"), "since": iso(ver.get("activated_at")), "reason": ver.get("reason")}, "targets": targets,
            "ntfy": al, "issues": iss, "headline": stat["analysis"]["text"]}


def v5_unlinked(h: dict) -> dict:
    """V5_DATABASE_URL 이 없을 때: 서버 상태만."""
    srv = server_level("V5", h)
    b = (h or {}).get("body") or {}
    st = {"server": srv, "data": lv("warn", "V5 전용 DB 미연결 — V5_DATABASE_URL 입력 필요"),
          "analysis": lv("warn", "판정 불가(데이터 미연결)"),
          "trade": lv("ok" if b.get("engine") else "warn", "엔진 " + ("켜짐" if b.get("engine") else "꺼짐·확인 불가") + " (서버 신호만)")}
    iss = [issue("V5", "P2", "unlinked", "V5 데이터 미연결", "Render 대시보드 서비스 환경변수에 V5_DATABASE_URL(v5-db 외부 접속 URL) 입력 필요")]
    if srv["level"] == "fail":
        iss.append(issue("V5", "P2", "server", "V5 서버 응답 없음", srv["text"]))
    return {"app": "V5", "status": st, "overall": worst(*st.values()), "unlinked": True, "positions": [], "issues": iss,
            "headline": "데이터 미연결 — 서버 상태만 표시", "mode_name": "—", "cum": {}, "by_strat": {}, "day_realized": None}


# ───────────────────────── 통합 위험
def assess_risk(J: dict, R: dict, K: dict, M: dict, acct: dict | None, V: dict | None = None) -> dict:
    V = V or {}
    iss = []
    pos = (R.get("positions") or []) + (K.get("positions") or []) + (M.get("positions") or []) + ((J or {}).get("positions") or []) + (V.get("positions") or [])
    other = R.get("other_holdings") or []
    syms = {}
    for p in pos + other:
        syms.setdefault(p["symbol"], set()).add(p["account"])
    dup = [s for s, a in syms.items() if len(a) > 1]
    if dup:
        iss.append(issue("DASH", "P3", "dup", "여러 계좌 동일 종목 보유", ", ".join(dup)))
    m = (J or {}).get("market") or {}
    trade_apps = (J, R, K, M, V)
    unknown = sum(1 for x in trade_apps for i in (x.get("issues") or []) if "불명" in i["title"] or "미청산" in i["title"])
    stale = sum(1 for x in (J, R, K, M, V) for i in (x.get("issues") or []) if any(w in i["title"] for w in ("지연", "데이터", "미생성")))
    risk_to_stop = round(sum(p.get("risk_to_stop") or 0 for p in pos), 2)
    ra, ka, ma = R.get("account") or {}, K.get("account") or {}, M.get("account") or {}
    k_live, m_live = K.get("env") == "live", bool(M.get("live"))
    acc_rows = [
        {"name": "토스증권 (Ross)", "source": ra.get("source"), "capital": ra.get("capital_setting"), "live": True,
         "realized_today": R.get("day_pnl"), "unreal": None, "open": len(R.get("positions") or []),
         "excluded_holdings": len(other), "error": ra.get("error")},
        {"name": "한국투자증권 (Kell)" + ("" if k_live else " · " + (K.get("mode_name") or "")), "live": k_live,
         "source": ka.get("source"), "capital": ka.get("capital_setting"), "cash": ka.get("cash_usd"),
         "realized_today": K.get("day_realized"), "unreal": K.get("unrealized"), "open": len(K.get("positions") or []),
         "error": ka.get("error")},
        {"name": "한국투자 모의투자 (Jones E)" + " · " + (((J or {}).get("etf") or {}).get("mode_name") or "꺼짐"), "live": False,
         "source": "ETF 200일 엔진 예산(모의투자 — 실제 돈 아님)", "capital": ((J or {}).get("etf") or {}).get("budget"), "cash": None,
         "realized_today": ((J or {}).get("etf") or {}).get("day_realized"), "unreal": None, "open": len((J or {}).get("positions") or []), "error": None},
        {"name": "한국투자 KIS (V5 통합) · " + (V.get("mode_name") or "—"), "live": bool(V.get("live")),
         "source": "V5 전략 자본(설정)" if not V.get("unlinked") else "데이터 미연결", "capital": V.get("capital"), "cash": None,
         "realized_today": V.get("day_realized"), "unreal": None, "open": len(V.get("positions") or []), "error": None},
        {"name": "키움증권 (MA20·60)" + ("" if m_live else " · " + (M.get("mode_name") or "")), "live": m_live,
         "source": ma.get("source"), "capital": ma.get("capital_setting"), "cash": ma.get("orderable_usd", ma.get("cash_usd")),
         "realized_today": M.get("day_realized"), "unreal": M.get("unrealized"), "open": len(M.get("positions") or []),
         "error": ma.get("error")},
    ]
    live_cap = [r["capital"] for r in acc_rows if r["live"] and r["capital"]]
    total_cap = round(sum(live_cap), 2) if live_cap else None
    real_today = round(sum((r["realized_today"] or 0) for r in acc_rows if r["live"]), 2)
    unreal_all = round(sum(r["unreal"] or 0 for r in acc_rows), 2)
    missing = [k for k in ("ROSS", "KELL", "MA") if not os.environ.get(f"{k}_APP_PASSWORD")]
    if missing:
        iss.append(issue("DASH", "P3", "acct_setup", "증권사 실잔고 일부 미연동",
                         "Render 환경변수 " + " · ".join(f"{k}_APP_PASSWORD" for k in missing) + " 입력 시 실잔고·장중 관리 상태 표시", None))
    return {"accounts": acc_rows, "positions": pos, "other_holdings": other, "duplicates": dup, "total_capital_live": total_cap,
            "realized_today_live": real_today, "unreal_all": unreal_all, "unreal_kell": K.get("unrealized"), "risk_to_stop": risk_to_stop,
            "loss_limit": R.get("loss_limit"), "ma_loss_limit": M.get("loss_limit"), "v5_loss_limit": V.get("loss_limit"), "trial": R.get("trial"),
            "market": {"stage": m.get("stage"), "name": m.get("name"), "defense": m.get("defense"), "change": m.get("change"),
                       "kell_regime": ((K.get("market") or {}).get("regime")),
                       "ma_spy": (((M.get("market") or {}).get("SPY")) or {}).get("stage"),
                       "v5_regime": (V.get("regime") or {}).get("label")},
            "unknown_orders": unknown, "data_warnings": stale, "issues": iss,
            "note": "통합 대시보드는 조회·경보 전용입니다. 자동매매 중지·청산은 각 전략 서버가 책임집니다."}


def os_env_has_passwords() -> bool:
    import os
    return bool(os.environ.get("ROSS_APP_PASSWORD") or os.environ.get("KELL_APP_PASSWORD"))
