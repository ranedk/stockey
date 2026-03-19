#!/usr/bin/env python3

import os
import sys
import json
import argparse
from decimal import Decimal
from datetime import date, datetime

import psycopg2
import psycopg2.extras

from environs import Env


env = Env()
env.read_env()


def json_default(obj):
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    if isinstance(obj, Decimal):
        return float(obj)
    raise TypeError(f"Type not serializable: {type(obj).__name__}")


def get_connection():
    return psycopg2.connect(
        host=env("POSTGRES_HOST"),
        port=env("POSTGRES_PORT"),
        dbname=env("POSTGRES_DB"),
        user=env("POSTGRES_USER"),
        password=env("POSTGRES_PASSWORD"),
        cursor_factory=psycopg2.extras.RealDictCursor,
    )


def main():
    parser = argparse.ArgumentParser(description="Run an SQL query and print results.")
    parser.add_argument("query", help="SQL query to execute")
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Optional client-side limit on returned rows",
    )
    args = parser.parse_args()

    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(args.query)

                if cur.description is None:
                    # INSERT/UPDATE/DELETE/DDL
                    print(json.dumps({
                        "status": "ok",
                        "rowcount": cur.rowcount,
                    }, indent=2))
                    return

                rows = cur.fetchall()

                if args.limit is not None:
                    rows = rows[:args.limit]

                print(json.dumps(rows, indent=2, default=json_default))

    except Exception as e:
        print(json.dumps({
            "status": "error",
            "error": str(e),
        }, indent=2))
        sys.exit(1)


if __name__ == "__main__":
    main()
