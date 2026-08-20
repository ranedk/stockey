from __future__ import annotations

import os
from datetime import date, datetime, timedelta
from typing import Iterable

import pandas as pd
import redis
from psycopg2 import sql

from utils.fallback_telemetry import record_local_fallback_event
from utils.db import db_session, with_db_retries
from utils.redis_utils import get_redis_client as get_resilient_redis_client


DEFAULT_START_DATE = datetime(2014, 1, 1)


def parse_datetime_arg(value: str | None) -> datetime | None:
    # BUG FOUND LIVE 2026-08-19 (re-audit): only ever accepted a bare "YYYY-MM-DD" date, but
    # data/dhanlive/ohlcv_pull.py's own --help epilog (and the --from-datetime/--to-datetime flag
    # names themselves, distinct from --from-date/--to-date) advertise full ISO-8601
    # datetime-with-offset input for intraday pulls ("2026-04-09T09:15:00+05:30") -- reproduced
    # live: crashed with exit code 2 before main() even ran. Bare-date strings (every other
    # existing caller of this function -- earnings_events.py, benchmark_sync.py,
    # indices_downloader.py, ...) still parse exactly as before; only a value the old code could
    # never accept anyway now also succeeds.
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d")
    except ValueError:
        pass
    return datetime.fromisoformat(value)


def load_tracked_symbols(symbol_args: Iterable[str] | None = None) -> list[str]:
    """Explicit symbol list for a one-off/small-scope loader: --symbols arg, else
    STOCKEY_SYMBOLS env var, else empty. No file fallback (removed 2026-08-14 --
    used to silently fall back to a static config/tracked_symbols.txt placeholder
    list, which is exactly the footgun this function must not reintroduce). A
    caller that needs "the current tradeable NSE universe" should call
    utils.universe.get_equity_universe() instead of expecting this to supply it."""
    values: list[str] = []

    if symbol_args:
        for item in symbol_args:
            values.extend(part.strip().upper() for part in item.split(",") if part.strip())
    elif os.getenv("STOCKEY_SYMBOLS"):
        values.extend(
            part.strip().upper()
            for part in os.getenv("STOCKEY_SYMBOLS", "").split(",")
            if part.strip()
        )

    deduped: list[str] = []
    seen = set()
    for value in values:
        if value not in seen:
            seen.add(value)
            deduped.append(value)
    return deduped


def get_redis_client(host: str, port: int) -> redis.Redis:
    return get_resilient_redis_client(host=host, port=port, decode_responses=True)


def get_redis_cursor(redis_client: redis.Redis, key: str) -> datetime | None:
    raw = redis_client.get(key)
    return parse_datetime_arg(raw)


def set_redis_cursor(redis_client: redis.Redis, key: str, value: datetime) -> None:
    redis_client.set(key, value.strftime("%Y-%m-%d"))


def get_redis_set_members(redis_client: redis.Redis, key: str) -> set[str]:
    try:
        return set(redis_client.smembers(key))
    except Exception as exc:
        record_local_fallback_event(
            module="utils.sync",
            source=f"redis:{key}",
            fallback_type="redis_set_members_unavailable",
            severity="warn",
            reason=(
                "Redis set members could not be loaded; caller will continue with an empty processed-state set, "
                "which may cause safe reprocessing until Redis recovers."
            ),
            error=exc,
            metadata={"key": key, "command": "smembers"},
        )
        print(f"[utils.sync] failed to load redis set members key={key}: {exc.__class__.__name__}: {exc}", flush=True)
        return set()


def filter_missing_date_members(
    values: Iterable[date | datetime],
    existing_members: set[str],
    *,
    fmt: str = "%Y-%m-%d",
) -> list[date | datetime]:
    return [value for value in values if value.strftime(fmt) not in existing_members]


def get_db_max_date(
    table_name: str,
    *,
    date_column: str = "date",
    filters: dict[str, object] | None = None,
) -> datetime | None:
    filters = filters or {}
    where_sql = sql.SQL("")
    params: list[object] = []
    if filters:
        clauses = []
        for key, value in filters.items():
            clauses.append(sql.SQL("{} = %s").format(sql.Identifier(key)))
            params.append(value)
        where_sql = sql.SQL(" WHERE {}").format(sql.SQL(" AND ").join(clauses))

    query = sql.SQL("SELECT MAX({}) AS max_date FROM {}{}").format(
        sql.Identifier(date_column),
        sql.Identifier(*table_name.split(".")),
        where_sql,
    )

    def _execute() -> dict | None:
        with db_session(dict_factory=True) as (_, cur):
            cur.execute(query, tuple(params))
            return cur.fetchone()

    try:
        row = with_db_retries(_execute, operation_name=f"get_db_max_date:{table_name}")
    except Exception as exc:
        message = str(exc).lower()
        if exc.__class__.__name__ in {"UndefinedTable", "UndefinedColumn"} or "does not exist" in message:
            return None
        raise

    if not row or not row["max_date"]:
        return None

    dt = pd.to_datetime(row["max_date"], errors="coerce")
    if pd.isna(dt):
        return None
    if getattr(dt, "tzinfo", None) is not None:
        dt = dt.tz_convert("UTC").tz_localize(None)
    return dt.to_pydatetime()


def choose_from_date(
    explicit_from: datetime | None,
    latest_known_dates: Iterable[datetime | None],
    *,
    default_start: datetime = DEFAULT_START_DATE,
) -> datetime:
    if explicit_from is not None:
        return explicit_from

    known = [value for value in latest_known_dates if value is not None]
    if not known:
        return default_start
    return max(known) + timedelta(days=1)


def normalize_date_window(
    from_date: datetime | None,
    to_date: datetime | None,
) -> tuple[datetime, datetime]:
    def _as_naive_utc(value: datetime) -> datetime:
        timestamp = pd.Timestamp(value)
        if timestamp.tzinfo is not None:
            timestamp = timestamp.tz_convert("UTC").tz_localize(None)
        return timestamp.to_pydatetime()

    start = _as_naive_utc(from_date or DEFAULT_START_DATE)
    end = _as_naive_utc(to_date or datetime.today())
    return start, end
