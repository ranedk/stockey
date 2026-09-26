#!/usr/bin/env python3

import argparse
import json
from datetime import date, datetime
from decimal import Decimal

from utils.ingestion_state import clear_state, get_state_entries, summarize_state_entries


def json_default(obj):
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    if isinstance(obj, Decimal):
        return float(obj)
    raise TypeError(f"Type not serializable: {type(obj).__name__}")


def main(argv: list[str] | None = None):
    parser = argparse.ArgumentParser(description="Inspect and manage ingestion_file_state entries")
    sub = parser.add_subparsers(dest="action", required=True)

    list_p = sub.add_parser("list", help="List ingestion state entries")
    list_p.add_argument("--source", dest="source_prefix", help="Source prefix, e.g. bhavcopy")
    list_p.add_argument("--status", help="Status filter, e.g. failed or processed")
    list_p.add_argument("--limit", type=int, default=100)

    summary_p = sub.add_parser("summary", help="Summarize ingestion state counts by status/source/classification")
    summary_p.add_argument("--source", dest="source_prefix", help="Source prefix, e.g. bhavcopy")
    summary_p.add_argument("--status", help="Status filter, e.g. failed or empty_valid_source")
    summary_p.add_argument("--limit", type=int, default=1000)
    summary_p.add_argument("--sample-limit", type=int, default=20)

    clear_p = sub.add_parser("clear", help="Delete a stored state row so the file can be retried cleanly")
    clear_p.add_argument("--source", dest="source_prefix", required=True)
    clear_p.add_argument("--key", dest="object_key", required=True)

    args = parser.parse_args(argv)

    if args.action == "list":
        rows = get_state_entries(
            source_prefix=args.source_prefix,
            status=args.status,
            limit=args.limit,
        )
        print(
            json.dumps(
                {
                    "status": "ok",
                    "count": len(rows),
                    "rows": rows,
                },
                indent=2,
                default=json_default,
            )
        )
        return

    if args.action == "summary":
        rows = get_state_entries(
            source_prefix=args.source_prefix,
            status=args.status,
            limit=args.limit,
        )
        print(
            json.dumps(
                summarize_state_entries(rows, sample_limit=args.sample_limit),
                indent=2,
                default=json_default,
            )
        )
        return

    if args.action == "clear":
        clear_state(args.source_prefix, args.object_key)
        print(
            json.dumps(
                {
                    "status": "ok",
                    "cleared": {
                        "source_prefix": args.source_prefix,
                        "object_key": args.object_key,
                    },
                },
                indent=2,
            )
        )
        return

    raise SystemExit(f"unsupported action: {args.action}")


if __name__ == "__main__":
    main()
