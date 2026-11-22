"""DB 접근.

- 전략 앱 테이블(ptj_·rc_·kc_)은 읽기 전용 세션(SET SESSION READ ONLY)으로만 조회한다.
- 대시보드 자신의 기록은 dash_ 테이블에만 쓴다.
"""
from __future__ import annotations

import os
from contextlib import contextmanager

import psycopg2
import psycopg2.extras

DATABASE_URL = os.environ.get("DATABASE_URL", "")


class NoDB(Exception):
    pass


@contextmanager
def _conn(readonly: bool):
    if not DATABASE_URL:
        raise NoDB("DATABASE_URL 미설정")
    c = psycopg2.connect(DATABASE_URL, connect_timeout=10, application_name="trading-control-center")
    try:
        c.set_session(readonly=readonly, autocommit=False)
        with c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("set statement_timeout = 15000")
            yield cur
        if readonly:
            c.rollback()
        else:
            c.commit()
    except Exception:
        c.rollback()
        raise
    finally:
        c.close()


@contextmanager
def ro():
    """전략 앱 데이터 조회 — 읽기 전용 트랜잭션."""
    with _conn(True) as cur:
        yield cur


@contextmanager
def rw():
    """dash_ 테이블 전용 쓰기."""
    with _conn(False) as cur:
        yield cur


def q(sql: str, args=()) -> list[dict]:
    with ro() as cur:
        cur.execute(sql, args)
        return [dict(r) for r in cur.fetchall()]


SCHEMA = """
create table if not exists dash_events(
  event_id text primary key,            -- 중복 방지 키 (앱:원천:원천id)
  at timestamptz not null,
  app text not null,                    -- JONES / ROSS / KELL / DASH
  kind text not null,
  severity text not null,               -- P1~P4
  symbol text,
  msg text not null,
  data jsonb,
  status text not null default 'info'   -- info / open / acked / resolved
);
create index if not exists dash_events_at on dash_events(at desc);
create table if not exists dash_cursor(src text primary key, last_id bigint not null default 0, updated timestamptz default now());
create table if not exists dash_issues(
  key text primary key,                 -- 같은 문제는 같은 key → 중복 알림 없음
  app text not null,
  severity text not null,
  title text not null,
  detail text,
  state text not null,                  -- open / acked / resolved
  first_seen timestamptz not null default now(),
  last_seen timestamptz not null default now(),
  acked_at timestamptz,
  resolved_at timestamptz,
  notified text,                        -- 마지막 통보 단계(open:P1, worse:P1, resolved)
  notify_err text
);
create table if not exists dash_reports(
  d date primary key,
  status text not null,                 -- 잠정 / 확정
  data jsonb not null,
  built_at timestamptz not null default now(),
  sent text
);
"""


def init():
    with rw() as cur:
        cur.execute(SCHEMA)
