"""DB 접근.

- 전략 앱 테이블(ptj_·rc_·kc_)은 읽기 전용 세션(SET SESSION READ ONLY)으로만 조회한다.
- 대시보드 자신의 기록은 dash_ 테이블에만 쓴다.
"""
from __future__ import annotations

import os
from contextlib import contextmanager

import threading
import time

import psycopg2
import psycopg2.extras
import psycopg2.errors
from psycopg2 import pool

DATABASE_URL = os.environ.get("DATABASE_URL", "")
# V5 통합 자동매매는 전용 DB(싱가포르) — 외부 접속 URL 을 사용자가 Render 환경변수로 입력. 읽기 전용 세션으로만 사용.
V5_DATABASE_URL = os.environ.get("V5_DATABASE_URL", "")


class NoDB(Exception):
    pass


# 수집 1회(약 60초)마다 쿼리 수십 개를 실행한다. 쿼리마다 새 연결을 열면 공유 DB(CPU 0.1·메모리 256MB)에
# 분당 80개 넘는 접속 생성 부하가 생기므로, 수집 주기 동안 소수의 연결을 재사용하고 주기 끝에 모두 닫는다.
_POOLS: dict[str, pool.ThreadedConnectionPool] = {}
_PLOCK = threading.Lock()
MAX_CONN = int(os.environ.get("DB_MAX_CONN", "2"))  # minconn=maxconn 이어야 반납한 연결이 재사용됨(풀 생성 시 바로 연결)


def _pool(url: str):
    with _PLOCK:
        p = _POOLS.get(url)
        if p is None or p.closed:
            p = _POOLS[url] = pool.ThreadedConnectionPool(MAX_CONN, MAX_CONN, url, connect_timeout=10,
                                                          application_name="trading-control-center",
                                                          options="-c statement_timeout=15000")
        return p


def _borrow(p):
    # 풀이 가득 차면(동시 조회 > MAX_CONN) 잠시 기다린다.
    for _ in range(300):
        try:
            return p.getconn()
        except pool.PoolError:
            time.sleep(0.05)
    return p.getconn()


def close_pools():
    """수집 주기 끝 — 열린 연결을 모두 닫아 DB 메모리를 비워 둔다."""
    with _PLOCK:
        for p in _POOLS.values():
            try:
                p.closeall()
            except Exception:
                pass
        _POOLS.clear()


@contextmanager
def _conn(readonly: bool, url: str | None = None):
    url = url or DATABASE_URL
    if not url:
        raise NoDB("DATABASE_URL 미설정")
    p = _pool(url)
    c = _borrow(p)
    bad = False
    try:
        if c.closed:
            raise psycopg2.InterfaceError("closed")
        c.set_session(readonly=readonly, autocommit=False)
        with c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            yield cur
        if readonly:
            c.rollback()
        else:
            c.commit()
    except (psycopg2.OperationalError, psycopg2.InterfaceError):
        bad = True  # DB 재시작 등으로 끊긴 연결은 버린다
        raise
    except Exception:
        try:
            c.rollback()
        except Exception:
            bad = True
        raise
    finally:
        try:
            p.putconn(c, close=bad or c.closed)
        except Exception:
            pass
        if bad:  # 끊김은 보통 DB 재시작 — 같은 풀의 다른 연결도 죽었으므로 풀을 통째로 버림
            with _PLOCK:
                if _POOLS.get(url) is p:
                    _POOLS.pop(url, None)
                    try:
                        p.closeall()
                    except Exception:
                        pass


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


def _retry(fn):
    """끊긴 연결(DB 재시작 등)이면 새 연결로 1회만 다시 시도. 시간 초과(QueryCanceled)는 재시도하지 않음."""
    try:
        return fn()
    except psycopg2.errors.QueryCanceled:
        raise
    except (psycopg2.OperationalError, psycopg2.InterfaceError):
        return fn()


def q(sql: str, args=()) -> list[dict]:
    def run():
        with ro() as cur:
            cur.execute(sql, args)
            return [dict(r) for r in cur.fetchall()]
    return _retry(run)


def q_v5(sql: str, args=()) -> list[dict]:
    """V5 전용 DB — 읽기 전용 트랜잭션."""
    if not V5_DATABASE_URL:
        raise NoDB("V5_DATABASE_URL 미설정")
    def run():
        with _conn(True, V5_DATABASE_URL) as cur:
            cur.execute(sql, args)
            return [dict(r) for r in cur.fetchall()]
    return _retry(run)


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
