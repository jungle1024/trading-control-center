"""장 마감 후 통합 운영보고서.

발행 조건(가이드 9장): Jones 분석 완료 · Ross 체결 동기화(장 마감 리포트) · Kell 야간 스캔·잔고 갱신 ·
세 앱 상태 수집 · 손익/위험 계산. 하나라도 빠지면 '잠정'으로 먼저 내고, 모두 갖춰지면 '확정'으로 다시 낸다.
미확정 값은 0이 아니라 '미수신'으로 표시한다.
"""
from __future__ import annotations

from app import tcal


def build(d, snap: dict) -> dict:
    J, R, K, M, risk = snap["jones"], snap["ross"], snap["kell"], snap.get("ma") or {}, snap["risk"]
    V = snap.get("v5") or {}
    ds = d.isoformat()
    m = J.get("market") or {}
    conds = {
        "Jones 시장분석": m.get("d") == ds,
        "Ross 체결 동기화(장 마감 리포트)": bool((R.get("report") or {}).get("exists")) and R.get("basis") == ds,
        "Kell 야간 스캔·잔고": K["status"]["analysis"]["level"] == "ok" and (K.get("funnel") or {}).get("scan_as_of") == ds,
        **({"Jones ETF 엔진 장 마감 리포트": ((J.get("etf") or {}).get("report") or {}).get("d") == ds}
           if (J.get("etf") or {}).get("mode", "off") != "off" else {}),
        **({"MA20·60 장 마감 리포트": ((M.get("report") or {}).get("day") == ds)} if M.get("mode") not in (None, "research") else {}),
        **({"V5 장 마감 리포트·야간 배치": ((V.get("report") or {}).get("d") == ds and (V.get("regime") or {}).get("d") == ds)}
           if not V.get("unlinked") and V.get("status") else {}),
        "MA20·60 야간 분석": (M.get("status") or {}).get("analysis", {}).get("level") == "ok" and (M.get("funnel") or {}).get("as_of") == ds,
        "전 앱 상태 수집": all(x["status"]["server"]["level"] != "fail" for x in (J, R, K, M) if x.get("status")),
        "손익·위험 계산": True,
    }
    status = "확정" if all(conds.values()) else "잠정"
    rd, cum = R.get("day") or {}, R.get("cum") or {}
    issues = snap.get("issues") or []
    nxt = []
    if m.get("risk", 0) >= 3:
        nxt.append(f"Jones {m.get('name')} 지속 여부 검토 (경보 {len(m.get('warnings') or [])}건)")
    if M.get("positions"):
        nxt.append("MA20·60 보유 " + ", ".join(p["symbol"] for p in M["positions"]) + " 손절·청산 예정 점검")
    if (M.get("funnel") or {}).get("watch_live") or (M.get("funnel") or {}).get("watch_paper"):
        f = M["funnel"]
        nxt.append(f"MA20·60 진입 대기 {f.get('watch_live', 0) + f.get('watch_paper', 0)}종목 ({M.get('mode_name')})")
    if K.get("positions"):
        nxt.append("Kell 보유 " + ", ".join(p["symbol"] for p in K["positions"]) + " 손절 기준 점검")
    for i in issues:
        if i["severity"] in ("P1", "P2"):
            nxt.append(f"[{i['severity']}] {i['title']}")
    if not nxt:
        nxt.append("특이사항 없음 — 다음 거래일 정상 운영")
    sec_r = {"closed": rd.get("n", 0) if conds["Ross 체결 동기화(장 마감 리포트)"] or rd.get("n") else None,
             "win_rate": rd.get("win_rate"), "realized": R.get("day_pnl") if R.get("basis") == ds else None,
             "funnel": R.get("funnel"), "unknown_orders": sum(1 for i in issues if i["app"] == "ROSS" and "불명" in i["title"]),
             "cum_n": cum.get("n"), "cum_net": cum.get("net"), "cum_win": cum.get("win_rate"), "loss_limit": R.get("loss_limit"),
             "checks_bad": [c for c in R.get("checks") or [] if c.get("status") not in ("OK", "해당 없음")]}
    sec_k = {"mode": K.get("mode_name"), "holdings": len(K.get("positions") or []), "entries": (K.get("funnel") or {}).get("entries"),
             "realized": K.get("day_realized"), "unrealized": K.get("unrealized"), "watch_live": (K.get("funnel") or {}).get("watch_live"),
             "passed": (K.get("funnel") or {}).get("passed"), "scan_as_of": (K.get("funnel") or {}).get("scan_as_of")}
    mf = M.get("funnel") or {}
    sec_m = {"mode": M.get("mode_name"), "holdings": len(M.get("positions") or []), "entries": mf.get("entries"),
             "realized": M.get("day_realized"), "unrealized": M.get("unrealized"), "signals": mf.get("signals"), "buyable": mf.get("buyable"),
             "watch": (mf.get("watch_live") or 0) + (mf.get("watch_paper") or 0), "scan_as_of": mf.get("as_of"),
             "research_pass": {k: v.get("pass") for k, v in ((M.get("research") or {}).get("kinds") or {}).items()}}
    return {
        "date": ds, "status": status, "conditions": conds, "session": tcal.is_session(d),
        "jones_etf": {"mode": (J.get("etf") or {}).get("mode_name"), "holdings": len(J.get("positions") or []),
                      "realized": (J.get("etf") or {}).get("day_realized"), "bad_checks": ((J.get("etf") or {}).get("report") or {}).get("bad"),
                      "paper": [{"name": a["name"], "equity": a["equity"], "ret_pct": a["ret_pct"], "open_n": a["open_n"]} for a in (J.get("etf") or {}).get("paper") or []]},
        "jones": {"stage": m.get("stage"), "name": m.get("name"), "change": m.get("change"), "warnings": len(m.get("warnings") or []),
                  "new_candidates": (J.get("discover") or {}).get("new"), "candidates": (J.get("discover") or {}).get("n"),
                  "defense": m.get("defense"), "market_d": m.get("d")},
        "ross": sec_r, "kell": sec_k, "ma": sec_m,
        "v5": {"mode": V.get("mode_name"), "unlinked": bool(V.get("unlinked")), "regime": (V.get("regime") or {}).get("label"),
               "holdings": len(V.get("positions") or []), "realized": V.get("day_realized"), "cands": V.get("cands_by"),
               "bad_checks": (V.get("report") or {}).get("bad"), "equity": (V.get("equity") or {}).get("equity")},
        "risk": {"realized_total_live": risk.get("realized_today_live"), "unreal_kell": risk.get("unreal_kell"), "unreal_all": risk.get("unreal_all"),
                 "loss_limit_breach": sum(1 for i in issues if "손실한도 초과" in i["title"]),
                 "incidents_open": len([i for i in issues if i["severity"] in ("P1", "P2")]),
                 "incidents_resolved": len(snap.get("resolved") or [])},
        "next": nxt,
        "note": "실현손익과 평가손익은 서로 다른 값이며 합산하지 않습니다. 미수신 값은 '미수신'으로 표시합니다.",
    }


def to_text(r: dict) -> str:
    def v(x, fmt="{}", none="미수신"):
        return none if x is None else fmt.format(x)
    J, Ro, Ke, Ri = r["jones"], r["ross"], r["kell"], r["risk"]
    lines = [f"미국 주식 통합 운영보고서 {r['date']} ({r['status']})",
             f"1) Jones: {v(J['name'])} · 전일 대비 {v(J['change'])} · 경보 {J['warnings']} · 후보 {v(J['candidates'])}(신규 {v(J['new_candidates'])})",
             f"2) Ross: 청산 {v(Ro['closed'])}건 · 승률 {v(Ro['win_rate'], '{}%', '-')} · 실현 {v(Ro['realized'], '${:+.2f}')} · 미확인 주문 {Ro['unknown_orders']}건",
             f"3) Kell({Ke['mode']}): 보유 {Ke['holdings']} · 신규 {v(Ke['entries'])} · 실현 {v(Ke['realized'], '${:+.2f}')} · 평가 {v(Ke['unrealized'], '${:+.2f}')}",
             (f"1b) Jones ETF 200일({r['jones_etf']['mode']}): 보유 {r['jones_etf']['holdings']} · 실현 {v(r['jones_etf']['realized'], '${:+.2f}')}"
              + (" · 가상 운용 " + " / ".join(f"{a['name'].split(' ')[0]} {a['ret_pct']:+.2f}%" for a in r['jones_etf']['paper'] if a.get('ret_pct') is not None) if r['jones_etf'].get('paper') else "")
              if r.get("jones_etf") else "1b) Jones ETF: 미수신"),
             (f"3c) V5({r['v5']['mode']}): 시장 {v(r['v5']['regime'])} · 보유 {r['v5']['holdings']} · 실현 {v(r['v5']['realized'], '${:+.2f}')} · 후보 " + (" · ".join(f"{k} {n}" for k, n in sorted((r['v5'].get('cands') or {}).items())) or "없음")
              if r.get("v5") and not r["v5"].get("unlinked") else "3c) V5: 데이터 미연결"),
             (f"3b) MA20·60({r['ma']['mode']}): 보유 {r['ma']['holdings']} · 신호 {v(r['ma']['signals'])} · 매수 가능 {v(r['ma']['buyable'])} · 진입 대기 {r['ma']['watch']} · 실현 {v(r['ma']['realized'], '${:+.2f}')}" if r.get("ma") else "3b) MA20·60: 미수신"),
             f"4) 위험: 실매매 실현 합계 {v(Ri['realized_total_live'], '${:+.2f}')} · 손실한도 초과 {Ri['loss_limit_breach']} · 미해결 장애 {Ri['incidents_open']}",
             "5) 다음 거래일: " + " / ".join(r["next"][:4])]
    if r["status"] == "잠정":
        lines.append("미충족: " + ", ".join(k for k, ok in r["conditions"].items() if not ok))
    return "\n".join(lines)
