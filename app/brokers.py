"""연결 증권 계좌 — 5개 전략 웹앱이 어떤 증권사·어떤 계좌(실계좌/모의)를 어떤 용도로 쓰는지.

매 수집 주기마다 각 앱의 /health · 계좌 조회(로그인 GET) · DB 설정에서 다시 만든다(고정 값 없음).
그래서 앱에서 계좌·환경·모드를 바꾸면 다음 주기(약 1분, 계좌 조회는 5분)에 화면이 같이 바뀐다.

표준 필드(선택): 앱이 /health 에 아래 목록을 내보내면 그것을 우선 쓴다(계좌번호는 앱이 가린 값만, 대시보드가 끝 4자리만 다시 남김).
  "broker_accounts": [{"broker": "한국투자증권", "env": "paper|real", "account": "****1234-01", "label": "PTJ 전용 모의",
                       "role": "ETF 200일 E · 발굴 A·B·C", "trading": true, "buying_power_usd": 100000, "holdings": 0}]

계좌 변경 감지: 앱별 (증권사·계좌 구분·환경·계좌 식별·용도) 지문을 dash_cursor('acct:<앱>')에 저장, 바뀌면 이벤트 + ntfy.
"""
from __future__ import annotations

import hashlib
import json
import re

ENV_KIND = {"real": "실계좌", "live": "실계좌", "paper": "모의 계좌", "demo": "모의 계좌", "kis_paper": "모의 계좌"}


def mask(v) -> str | None:
    """계좌 식별값은 끝 4자리만 남긴다(앱이 이미 가렸어도 한 번 더)."""
    if v in (None, ""):
        return None
    s = str(v)
    if len(s) <= 4:
        return s
    return "•••" + s[-4:]


def _num(v):
    try:
        return float(str(v).replace(",", "").replace("$", ""))
    except (TypeError, ValueError):
        return None


def _bp_usd(bp) -> float | None:
    """토스 buying-power 응답에서 달러 금액 하나를 꺼낸다(응답 키가 바뀌어도 숫자 필드를 찾음)."""
    if isinstance(bp, (int, float, str)):
        return _num(bp)
    if not isinstance(bp, dict):
        return None
    for k in ("orderableAmount", "orderable", "buyingPower", "amount", "available", "cash", "value"):
        if k in bp:
            v = bp[k]
            if isinstance(v, dict):
                v = v.get("amount") or v.get("value")
            n = _num(v)
            if n is not None:
                return n
    for v in bp.values():
        n = _num(v) if not isinstance(v, dict) else None
        if n is not None:
            return n
    return None


def _row(app, name, broker, env, *, kind, role, account=None, label=None, connected=None, trading=None,
         buying_power=None, holdings=None, source="", note=None):
    return {"app": app, "app_name": name, "broker": broker, "env": env, "acct_type": ENV_KIND.get(env, "—" if env is None else env),
            "label": label, "account": mask(account), "kind": kind, "role": role, "connected": connected, "trading": trading,
            "buying_power": buying_power, "holdings": holdings, "source": source, "note": note}


def _std(app, name, b) -> list[dict] | None:
    lst = (b or {}).get("broker_accounts")
    if not isinstance(lst, list) or not lst:
        return None
    out = []
    for x in lst:
        if not isinstance(x, dict):
            continue
        env = (x.get("env") or "").lower() or None
        kind = x.get("kind") or ("실매매" if env in ("real", "live") and x.get("trading") else "모의투자" if env in ("paper", "demo") and x.get("trading") else "연결만")
        out.append(_row(app, name, x.get("broker") or "—", env, kind=kind, role=x.get("role") or "—", account=x.get("account"),
                        label=x.get("label"), connected=x.get("connected", True), trading=x.get("trading"),
                        buying_power=_num(x.get("buying_power_usd")), holdings=x.get("holdings"), source="앱 /health(표준 필드)"))
    return out or None


# ───────────────────────── 앱별 — 표준 필드가 없을 때 /health·계좌 조회·DB 설정으로 추정
def ross(h, acct, R, Rraw) -> list[dict]:
    b = (h or {}).get("body") or {}
    std = _std("ROSS", "Ross", b)
    if std:
        return std
    a = (acct or {}).get("ROSS") or {}
    d = a.get("data") or {} if a.get("ok") else {}
    st = (Rraw or {}).get("settings") or {}
    sched = st.get("autosched") or {}
    logic = ((R or {}).get("logic") or {}).get("name") or "로스 단타"
    live = sched.get("enabled") and sched.get("mode") == "live"
    hold = d.get("holdings")
    items = hold if isinstance(hold, list) else (hold or {}).get("items") or (hold or {}).get("holdings") or []
    lt = b.get("longterm")
    role = (f"실매매({logic})" if live else "자동 매매 꺼짐") + " · 그림자 라인은 가상(주문 없음)" + (f" · 장기 보유 {lt}종목 별도" if lt else "")
    p = sched.get("params") or {}
    note = f"1회 ${p.get('live_budget_usd')} · 하루 손실 ${p.get('live_max_loss_usd')} · 최대 {p.get('max_trades')}회" if p else None
    return [_row("ROSS", "Ross", "토스증권", "real", kind="실매매" if live else "연결만", role=role,
                 account=(f"순번 {d['accountSeq']}" if d.get("accountSeq") is not None else None),
                 connected=(b.get("toss") if b else None), trading=b.get("trading") if b else None,
                 buying_power=_bp_usd(((d.get("buyingPower") or {}).get("USD"))) if d else None,
                 holdings=len(items) if d else None,
                 source="토스 실조회" if d else ("/health" + (" · 계좌 조회 실패" if a.get("configured") else " · ROSS_APP_PASSWORD 없음")), note=note)]


def kell(h, acct, K, Kraw) -> list[dict]:
    b = (h or {}).get("body") or {}
    std = _std("KELL", "Kell", b)
    if std:
        return std
    a = (acct or {}).get("KELL") or {}
    d = (a.get("data") or {}) if a.get("ok") else {}
    mode = (((Kraw or {}).get("settings") or {}).get("main") or {}).get("mode") or (K or {}).get("env") or "paper"
    conf = b.get("kis") or {}
    envs = [e for e, ok in conf.items() if ok] or ([d["env"]] if d.get("env") else [])
    out = []
    for e in envs or [None]:
        if e is None:
            out.append(_row("KELL", "Kell", "한국투자증권", None, kind="미연결", role="KIS 키 없음 — 가상 매매만",
                            source="/health", note=None))
            continue
        if mode == e:
            kind = "실매매" if e == "live" else "모의투자"
            role = "자동 매매 주문"
        else:
            kind = "연결만"
            role = "가상 매매 중 — 이 계좌로 주문 안 함" if mode == "paper" else f"앱 모드 {mode} — 이 계좌로 주문 안 함"
        mine = d if d.get("env") == e else {}
        out.append(_row("KELL", "Kell", "한국투자증권", e, kind=kind, role=role, label=mine.get("env_label"),
                        connected=True if b else None, trading=(mode == e),
                        buying_power=_num(mine.get("cash_usd")) if mine else None,
                        holdings=len(mine.get("holdings") or []) if mine else None,
                        source="KIS 실조회" if mine else "/health"))
    return out


def ma(h, acct, M, Mraw) -> list[dict]:
    b = (h or {}).get("body") or {}
    std = _std("MA", "MA20·60", b)
    if std:
        return std
    br = b.get("broker") or {}
    st = (Mraw or {}).get("settings") or {}
    mode = st.get("mode")
    lo = st.get("live_override") or {}
    a = (acct or {}).get("MA") or {}
    fd = ((a.get("data") or {}).get("funds") or {}) if a.get("ok") else {}
    envs = [e for e in ("real", "demo") if e in br] or ([br.get("env")] if br.get("env") else [])
    out = []
    for e in envs or [None]:
        if e is None:
            out.append(_row("MA", "MA20·60", "키움증권", None, kind="미연결", role="키움 키 없음 — 가상 매매만", source="/health"))
            continue
        live_book = mode in ("live", "auto", "approve", "manual")      # MA 실매매 계열 모드
        if e == "real" and live_book:
            kind, role = "실매매", "실매매 주문(소액 승인 조건)"
        elif e == "real" and lo.get("enabled"):
            kind, role = "승인·대기", f"실매매 승인({str(lo.get('approved_at') or '')[:16]}) · 현재 모드 {mode} — 주문 안 함"
        else:
            kind, role = "연결만", f"앱 모드 {mode} — 이 계좌로 주문 안 함"
        note = f"누적 손실 상한 −${lo.get('loss_cap_usd')} · 첫 진입 1주" if lo.get("enabled") and e == "real" else None
        if st.get("live_block") and e == "real":
            note = (note + " · " if note else "") + "신규 진입 중단 중(대사 불일치)"
        stats = br.get(e) or {}
        out.append(_row("MA", "MA20·60", "키움증권", e, kind=kind, role=role, connected=(stats.get("errors", 0) == 0) if stats else (True if b else None),
                        trading=(kind == "실매매"), buying_power=_num(fd.get("orderable_usd") or fd.get("cash_usd")) if fd and e == br.get("env") else None,
                        source="키움 실조회" if fd else ("/health" + ("" if a.get("configured") else " · MA_APP_PASSWORD 없음")), note=note))
    return out


def jones(h, J, Jraw) -> list[dict]:
    b = (h or {}).get("body") or {}
    std = _std("JONES", "Jones", b)
    if std:
        return std
    rtt = b.get("broker_rtt") or {}
    label = rtt.get("label") or ""
    orders = b.get("orders") or ""
    if not label and orders:
        m = re.search(r"—\s*(\S+\s*\S*)", orders)
        label = m.group(1) if m else ""
    env = "paper" if "모의" in label else ("real" if label else None)
    broker = "한국투자증권" if ("한국투자" in label or rtt.get("broker") == "kis") else ("토스증권" if "토스" in label else (label or "—"))
    eng = (Jraw or {}).get("engine") or {}
    disc = (Jraw or {}).get("disc_live") or {}
    roles = []
    if eng.get("mode") == "live":
        roles.append(f"ETF 200일 E 주문(예산 ${eng.get('budget'):,.0f})" if eng.get("budget") else "ETF 200일 E 주문")
    elif eng.get("mode") == "paper":
        roles.append("E 가상")
    on = [k for k, v in sorted(disc.items()) if (v or {}).get("on")]
    if on:
        roles.append("발굴 " + "·".join(on) + " 주문")
    trading = "주문 스위치 켜짐" in orders if orders else None
    ordering = bool(roles) and any("주문" in r for r in roles)
    kind = ("실매매" if env == "real" else "모의투자") if ordering and trading is not False else ("연결만" if label else "미연결")
    return [_row("JONES", "Jones", broker, env, kind=kind, role=" · ".join(roles) or "가상 운용만 — 주문 안 함", label="PTJ 전용" if env == "paper" else None,
                 connected=("연결" in orders and "미연결" not in orders) if orders else None, trading=trading,
                 source="/health · DB 설정", note=None if env != "paper" else "모의투자 — 실제 돈 아님")]


def v5(h, V, Vraw) -> list[dict]:
    b = (h or {}).get("body") or {}
    std = _std("V5", "V5", b)
    if std:
        return std
    clients = list((b.get("broker_rtt") or {}).keys())
    modes = ((Vraw or {}).get("settings") or {}).get("modes") or (V or {}).get("modes") or {}
    msg = ((Vraw or {}).get("kis_check") or {}).get("msg") or ""
    seen = {"paper": re.search(r"모의\s*(\S+)\s*보유\s*(\d+)종목\s*주문가능\s*\$([\d,\.]+)", msg),
            "live": re.search(r"실전\s*(\S+)\s*보유\s*(\d+)종목\s*주문가능\s*\$([\d,\.]+)", msg)}
    envs = clients or [e for e in ("paper", "live") if seen.get(e)]
    out = []
    for e in envs or [None]:
        if e is None:
            out.append(_row("V5", "V5", "한국투자증권", None, kind="미연결", role="KIS 키 없음", source="/health"))
            continue
        uses = [k for k, v in sorted(modes.items()) if v == e]
        kind = ("실매매" if e == "live" else "모의투자") if uses else "연결만"
        role = (f"전략 {'·'.join(uses)} 주문" if uses else "이 계좌로 주문하는 전략 없음") + \
               (f" · 전략 {'·'.join(k for k, v in sorted(modes.items()) if v == 'sim')} 내부 가상" if e == "paper" and any(v == "sim" for v in modes.values()) else "")
        m = seen.get(e)
        out.append(_row("V5", "V5", "한국투자증권", "real" if e == "live" else "paper", kind=kind, role=role, label="V5 전용",
                        connected=(m.group(1) == "정상") if m else (True if b else None), trading=bool(uses),
                        buying_power=_num(m.group(3)) if m else None, holdings=int(m.group(2)) if m else None,
                        source="/health · V5 KIS 점검 기록" if m else "/health"))
    return out


def build(health: dict, acct: dict | None, assessed: dict, raw: dict) -> list[dict]:
    out = []
    for fn, app, args in ((ross, "ROSS", ("acct", "R")), (kell, "KELL", ("acct", "K")), (ma, "MA", ("acct", "M")),
                          (jones, "JONES", ("J",)), (v5, "V5", ("V",))):
        h = (health or {}).get(app) or {}
        if not h.get("ok") and app != "ROSS":          # Ross 는 DB 설정만으로도 실계좌·용도를 알 수 있음
            out.append({**_row(app, {"MA": "MA20·60", "JONES": "Jones", "KELL": "Kell"}.get(app, app), "—", None, kind="확인 불가",
                                    role="서버 응답 없음 — 연결 계좌를 확인할 수 없음", source="/health " + (h.get("err") or "실패")), "health_ok": False})
            continue
        try:
            a, r = assessed.get(app), raw.get(app)
            rows = fn(h, acct, a, r) if "acct" in args else fn(h, a, r)
        except Exception as e:                       # 한 앱 오류가 다른 앱 표시를 막지 않게
            rows = [_row(app, app, "—", None, kind="확인 실패", role=f"{type(e).__name__}: {str(e)[:80]}", source="대시보드")]
        for x in rows:
            x["health_ok"] = bool(h.get("ok"))
        out += rows
    return out


def fingerprints(rows: list[dict], acct: dict | None) -> dict[str, int]:
    """앱별 계좌 지문. 서버 응답이 없거나 계좌 조회가 실패한 앱은 이번 주기에 비교하지 않는다(일시 장애를 변경으로 오인 방지)."""
    out = {}
    by = {}
    for x in rows:
        by.setdefault(x["app"], []).append(x)
    for app, xs in by.items():
        if not all(x["health_ok"] for x in xs):
            continue
        a = (acct or {}).get(app) or {}
        if a.get("configured") and not a.get("ok"):
            continue
        key = sorted((x["broker"], x["acct_type"], x["env"] or "", x["account"] or "", x["kind"]) for x in xs)
        out[app] = int(hashlib.sha256(json.dumps(key, ensure_ascii=False).encode()).hexdigest()[:12], 16)
    return out


def summary(app_rows: list[dict]) -> str:
    return " / ".join(f"{x['broker']} {x['acct_type']}" + (f" {x['account']}" if x["account"] else "") + f" — {x['kind']}" for x in app_rows)
