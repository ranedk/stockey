from __future__ import annotations

import argparse
import json

from data.dhanlive.screener import (
    list_registered_screeners,
    load_latest_snapshots,
    remove_registered_screener,
    summarize_latest_snapshots,
    upsert_registered_screener,
)


def print_records(df) -> None:
    if df.empty:
        print("[]", flush=True)
        return
    records = df.to_dict(orient="records")
    print(json.dumps(records, indent=2, default=str), flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="Manage the registered Dhan ScanX screeners")
    subparsers = parser.add_subparsers(dest="command", required=True)

    add_parser = subparsers.add_parser("add", help="Add or update one or more screeners")
    add_parser.add_argument("urls", nargs="+", help="Screener URLs")
    add_parser.add_argument("--name", help="Optional display name when adding exactly one screener")

    list_parser = subparsers.add_parser("list", help="List registered screeners")
    list_parser.add_argument("--active-only", action="store_true", help="Show only active screeners")

    remove_parser = subparsers.add_parser("remove", help="Remove a screener by slug or URL")
    remove_parser.add_argument("identifiers", nargs="+", help="Screener slug(s) or URL(s)")

    latest_parser = subparsers.add_parser("latest", help="Show the latest stored snapshot(s)")
    latest_parser.add_argument("--screener", help="Specific screener slug")
    latest_parser.add_argument("--raw", action="store_true", help="Include the full raw_json payload")

    args = parser.parse_args()

    if args.command == "add":
        if args.name and len(args.urls) != 1:
            raise SystemExit("--name can only be used when adding exactly one screener URL")
        results = []
        for index, url in enumerate(args.urls):
            name = args.name if index == 0 else None
            results.append(upsert_registered_screener(screener_url=url, screener_name=name))
        print(json.dumps(results, indent=2, default=str), flush=True)
        return

    if args.command == "list":
        print_records(list_registered_screeners(active_only=args.active_only))
        return

    if args.command == "remove":
        results = [remove_registered_screener(identifier) for identifier in args.identifiers]
        print(json.dumps(results, indent=2, default=str), flush=True)
        return

    if args.command == "latest":
        if args.raw:
            print_records(load_latest_snapshots(args.screener))
        else:
            print_records(summarize_latest_snapshots(args.screener))
        return


if __name__ == "__main__":
    main()
