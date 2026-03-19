from __future__ import annotations

import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterable

import pandas as pd
import redis
from psycopg2 import sql

from utils.db import db_session


REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SYMBOLS_FILE = REPO_ROOT / "config" / "tracked_symbols.txt"
DEFAULT_START_DATE = datetime(2014, 1, 1)


def parse_datetime_arg(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.strptime(value, "%Y-%m-%d")


def load_tracked_symbols(symbol_args: Iterable[str] | None = None) -> list[str]:
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
    elif DEFAULT_SYMBOLS_FILE.exists():
        for line in DEFAULT_SYMBOLS_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            values.append(line.upper())

    deduped: list[str] = []
    seen = set()
    for value in values:
        if value not in seen:
            seen.add(value)
            deduped.append(value)
    return deduped


def get_redis_client(host: str, port: int) -> redis.Redis:
    return redis.Redis(host=host, port=port, decode_responses=True)


def get_redis_cursor(redis_client: redis.Redis, key: str) -> datetime | None:
    raw = redis_client.get(key)
    return parse_datetime_arg(raw)


def set_redis_cursor(redis_client: redis.Redis, key: str, value: datetime) -> None:
    redis_client.set(key, value.strftime("%Y-%m-%d"))


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

    try:
        with db_session(dict_factory=True) as (_, cur):
            cur.execute(query, tuple(params))
            row = cur.fetchone()
    except Exception:
        return None

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
    start = from_date or DEFAULT_START_DATE
    end = to_date or datetime.today()
    return start, end
