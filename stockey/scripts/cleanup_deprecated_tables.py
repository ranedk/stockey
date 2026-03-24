from __future__ import annotations

import argparse
import json

from utils.db import db_session, sql_to_df


DEPRECATED_TABLES = [
    "dhan_screeners",
    "dhan_screener_snapshots",
]


def list_existing_deprecated_tables() -> list[str]:
    df = sql_to_df(
        """
        SELECT table_name
        FROM information_schema.tables
        WHERE table_schema = 'public'
          AND table_name = ANY(%s)
        ORDER BY table_name
        """,
        params=(DEPRECATED_TABLES,),
    )
    if df.empty:
        return []
    return df["table_name"].astype(str).tolist()


def drop_deprecated_tables() -> list[str]:
    existing = list_existing_deprecated_tables()
    if not existing:
        return []
    with db_session() as (_, cur):
        for table_name in existing:
            cur.execute(f"DROP TABLE IF EXISTS {table_name}")
    return existing


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Drop deprecated database tables that are no longer used by the advisory stack.")
    parser.add_argument("--dry-run", action="store_true", help="List deprecated tables without dropping them.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    existing = list_existing_deprecated_tables()
    dropped = [] if args.dry_run else drop_deprecated_tables()
    print(
        json.dumps(
            {
                "status": "ok",
                "deprecated_tables": DEPRECATED_TABLES,
                "existing_tables": existing,
                "dropped_tables": dropped,
                "dry_run": bool(args.dry_run),
            },
            indent=2,
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
