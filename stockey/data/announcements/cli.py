from __future__ import annotations

import argparse
from datetime import date

from .managed_pipeline import ManagedAnnouncementPipeline


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Stateful announcement pipeline ingest")
    parser.add_argument("--ticker", required=True)
    parser.add_argument("--from-date", required=True, type=date.fromisoformat)
    parser.add_argument("--to-date", required=True, type=date.fromisoformat)
    parser.add_argument("--exchange", action="append", dest="exchanges")
    parser.add_argument("--report", action="append", dest="reports")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    pipeline = ManagedAnnouncementPipeline()
    summary = pipeline.ingest_date_range(
        ticker=args.ticker,
        from_date=args.from_date,
        to_date=args.to_date,
        exchanges=args.exchanges,
        parse_reports=args.reports,
    )
    print(
        {
            "requested": summary.requested,
            "discovered": summary.discovered,
            "downloaded": summary.downloaded,
            "ocred": summary.ocred,
            "categorized": summary.categorized,
            "parsed": summary.parsed,
            "skipped": summary.skipped,
            "failed": summary.failed,
        }
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
