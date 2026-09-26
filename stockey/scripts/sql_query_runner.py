#!/usr/bin/env python3

import argparse
import json
import sys
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import psycopg2
import psycopg2.extras
from environs import Env

from utils.fallback_telemetry import record_local_fallback_event


env = Env()
env.read_env()


def json_default(obj):
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    if isinstance(obj, Decimal):
        return float(obj)
    raise TypeError(f"Type not serializable: {type(obj).__name__}")


def format_error(exc: Exception) -> str:
    return str(exc).strip() or repr(exc)


def get_connection():
    return psycopg2.connect(
        host=env("POSTGRES_HOST"),
        port=env.int("POSTGRES_PORT"),
        dbname=env("POSTGRES_DB"),
        user=env("POSTGRES_USER"),
        password=env("POSTGRES_PASSWORD"),
        cursor_factory=psycopg2.extras.RealDictCursor,
    )


def parse_params(raw: str | None):
    if not raw:
        return None
    parsed = json.loads(raw)
    if not isinstance(parsed, (list, dict)):
        raise ValueError("--params-json must decode to a list or object")
    return parsed


def load_query(args) -> str:
    if args.file:
        return Path(args.file).read_text(encoding="utf-8").strip()
    if args.query:
        return args.query.strip()
    if not sys.stdin.isatty():
        return sys.stdin.read().strip()
    raise ValueError("Provide a query, --file, or pipe SQL on stdin")


def build_result(query: str, rows, rowcount: int, limited: bool, columns):
    return {
        "status": "ok",
        "query": query,
        "rowcount": rowcount,
        "limited": limited,
        "columns": columns,
        "rows": rows,
    }


def main():
    parser = argparse.ArgumentParser(
        description="Run an SQL query and print JSON results."
    )
    parser.add_argument("query", nargs="?", help="SQL query to execute")
    parser.add_argument("--file", help="Read SQL from a file")
    parser.add_argument(
        "--params-json",
        help='JSON object or array for query parameters, e.g. \'{"symbol":"RELIANCE"}\'',
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Optional client-side limit on returned rows",
    )
    parser.add_argument(
        "--read-only",
        action="store_true",
        help="Reject queries that are not SELECT, WITH, EXPLAIN, or VALUES",
    )
    args = parser.parse_args()

    try:
        query = load_query(args)
        params = parse_params(args.params_json)

        first_token = query.lstrip().split(None, 1)[0].upper() if query.strip() else ""
        if args.read_only and first_token not in {"SELECT", "WITH", "EXPLAIN", "VALUES"}:
            raise ValueError("Read-only mode only allows SELECT/WITH/EXPLAIN/VALUES")

        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(query, params)

                if cur.description is None:
                    print(
                        json.dumps(
                            {
                                "status": "ok",
                                "query": query,
                                "rowcount": cur.rowcount,
                                "columns": [],
                                "rows": [],
                            },
                            indent=2,
                            default=json_default,
                        )
                    )
                    return

                rows = cur.fetchall()
                limited = False
                if args.limit is not None and len(rows) > args.limit:
                    rows = rows[: args.limit]
                    limited = True

                result = build_result(
                    query=query,
                    rows=rows,
                    rowcount=cur.rowcount,
                    limited=limited,
                    columns=[desc.name for desc in cur.description],
                )
                print(json.dumps(result, indent=2, default=json_default))

    except Exception as e:
        record_local_fallback_event(
            module="scripts.sql_query_runner",
            source="postgres",
            fallback_type="sql_query_runner_failed",
            severity="error",
            reason="SQL query runner failed before returning a normal query result.",
            error=e,
            metadata={"read_only": bool(getattr(args, "read_only", False)), "has_file": bool(getattr(args, "file", None))},
        )
        print(
            json.dumps(
                {
                    "status": "error",
                    "error": format_error(e),
                },
                indent=2,
            )
        )
        sys.exit(1)


if __name__ == "__main__":
    main()
