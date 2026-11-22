"""미국 정규장 달력 (ET). 휴장일은 NYSE 공지 기준으로 손으로 관리한다 — 매년 12월에 다음 해를 추가할 것."""
from __future__ import annotations

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")

HOLIDAYS = {
    # 2026
    "2026-01-01", "2026-01-19", "2026-02-16", "2026-04-03", "2026-05-25", "2026-06-19",
    "2026-07-03", "2026-09-07", "2026-11-26", "2026-12-25",
    # 2027
    "2027-01-01", "2027-01-18", "2027-02-15", "2027-03-26", "2027-05-31", "2027-06-18",
    "2027-07-05", "2027-09-06", "2027-11-25", "2027-12-24",
}
EARLY_CLOSE = {"2026-11-27", "2026-12-24", "2027-11-26"}   # 13:00 ET 마감

CONFIRM_AFTER_MIN = 40     # 마감 후 40분 뒤 확정 계산 (FMP 일봉 반영 대기)


def now_et() -> datetime:
    return datetime.now(ET)


def is_session(d: date) -> bool:
    return d.weekday() < 5 and d.isoformat() not in HOLIDAYS


def close_time(d: date) -> time:
    return time(13, 0) if d.isoformat() in EARLY_CLOSE else time(16, 0)


def prev_session(d: date) -> date:
    d -= timedelta(days=1)
    while not is_session(d):
        d -= timedelta(days=1)
    return d


def expected_last_session(now: datetime | None = None) -> date:
    """지금 시점에서 확정 일봉이 있어야 하는 가장 최근 거래일."""
    now = now or now_et()
    d = now.date()
    if is_session(d):
        ct = datetime.combine(d, close_time(d), ET) + timedelta(minutes=CONFIRM_AFTER_MIN)
        if now >= ct:
            return d
    return prev_session(d)


def market_open(now: datetime | None = None) -> bool:
    now = now or now_et()
    d = now.date()
    if not is_session(d):
        return False
    return time(9, 30) <= now.time() < close_time(d)


def confirm_due(now: datetime | None = None) -> bool:
    now = now or now_et()
    d = now.date()
    if not is_session(d):
        return False
    return now >= datetime.combine(d, close_time(d), ET) + timedelta(minutes=CONFIRM_AFTER_MIN)


def next_confirm_at(now: datetime | None = None) -> datetime:
    """다음 확정 계산 예정 시각(ET)."""
    now = now or now_et()
    d = now.date()
    for _ in range(10):
        if is_session(d):
            at = datetime.combine(d, close_time(d), ET) + timedelta(minutes=CONFIRM_AFTER_MIN)
            if at > now:
                return at
        d += timedelta(days=1)
    return now
