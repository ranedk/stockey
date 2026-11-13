"""Read-only access to the serving tables (PLATFORM_PRD.md section 4: public traffic never
touches pipeline tables -- prices are the one exception until the daily price serving table
exists, and every price read is date-bounded)."""
from __future__ import annotations

import os
from pathlib import Path

from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool


def _dsn() -> str:
    if os.environ.get("API_DATABASE_URL"):
        return os.environ["API_DATABASE_URL"]
    env = {}
    path = Path(__file__).resolve().parents[2] / "stockey" / ".env"
    for line in path.read_text().splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip().strip('"').strip("'")
    return (f"host={env.get('POSTGRES_HOST', 'localhost')} port={env.get('POSTGRES_PORT', '5432')} "
            f"dbname={env['POSTGRES_DB']} user={env['POSTGRES_USER']} password={env['POSTGRES_PASSWORD']}")


pool = ConnectionPool(_dsn(), min_size=1, max_size=8, open=False,
                      kwargs={"row_factory": dict_row, "options": "-c statement_timeout=15000"})


def rows(sql: str, params: tuple | dict | None = None) -> list[dict]:
    with pool.connection() as conn:
        conn.read_only = True
        return conn.execute(sql, params).fetchall()


def one(sql: str, params: tuple | dict | None = None) -> dict | None:
    r = rows(sql, params)
    return r[0] if r else None
