"""Re-load NSE market-cap files for every day where a company is missing from nseindia_mcap.

Written 2026-09-25 for the parse_mcap first-row bug: NSE's mcap file has no title line, so
the old parser made the first stock (20MICRONS) the header and dropped it on every day from
2024-02-01 (when NSE started publishing the file) to 2026-09-23.

A gap is a company row (ISIN not INF*, i.e. not an ETF / fund unit, which NSE publishes no
market cap for) in nseindia_ohlcv with no nseindia_mcap row for the same symbol, series and
date. For each such date the day's PR zip is downloaded through the shared NSE rate gate
and its mcap file re-parsed with parse_mcap (an idempotent upsert). Resumable: gaps are
recomputed on start, so a re-run only fetches what is still missing.

    python scripts/backfill_mcap_gaps.py            # fill every gap
    python scripts/backfill_mcap_gaps.py --dry-run  # list gap dates only
"""

from __future__ import annotations

import argparse
import glob
import io
import os
import sys
import tempfile
import time
import zipfile

import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from data.nseindia.bhavcopy_parser import parse_mcap  # noqa: E402
from utils.db import sql_to_df  # noqa: E402
from utils.nse_rate_limiter import nse_request_gate  # noqa: E402

MCAP_FIRST_DATE = "2024-02-01"
PR_URL = "https://nsearchives.nseindia.com/archives/equities/bhavcopy/pr/PR{ddmmyy}.zip"
HEADERS = {"User-Agent": "Mozilla/5.0", "Referer": "https://www.nseindia.com/"}

GAPS_QUERY = """
SELECT o.date::date AS d, count(*) AS missing
  FROM nseindia_ohlcv o
  LEFT JOIN nseindia_mcap m ON m.symbol = o.symbol AND m.date = o.date AND m.series = o.series
 WHERE o.date >= %s AND o.series IN ('EQ', 'BE', 'SM', 'ST')
   AND o.isin NOT LIKE 'INF%%' AND m.symbol IS NULL
 GROUP BY 1 ORDER BY 1
"""


def gap_dates() -> list:
    return sql_to_df(GAPS_QUERY, params=(MCAP_FIRST_DATE,))["d"].tolist()


def fetch_pr_zip(day) -> bytes | None:
    url = PR_URL.format(ddmmyy=day.strftime("%d%m%y"))
    for attempt in range(3):
        with nse_request_gate():
            resp = requests.get(url, headers=HEADERS, timeout=60)
        if resp.status_code == 200 and resp.content[:2] == b"PK":
            return resp.content
        if resp.status_code == 404:
            return None
        time.sleep(10 * (attempt + 1))
    raise RuntimeError(f"{url}: HTTP {resp.status_code} after 3 attempts")


def reload_day(day) -> int:
    content = fetch_pr_zip(day)
    if content is None:
        print(f"{day}: no PR zip on NSE", flush=True)
        return 0
    with tempfile.TemporaryDirectory() as tmp:
        zipfile.ZipFile(io.BytesIO(content)).extractall(tmp)
        files = [p for p in glob.glob(os.path.join(tmp, "**", "*"), recursive=True)
                 if os.path.basename(p).lower().startswith("mcap")]
        if not files:
            print(f"{day}: PR zip has no mcap file", flush=True)
            return 0
        return sum(len(parse_mcap(p)) for p in files)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    days = gap_dates()
    print(f"{len(days)} dates with a company missing from nseindia_mcap", flush=True)
    if args.dry_run:
        print([str(d) for d in days], flush=True)
        return 0
    failed = []
    for i, day in enumerate(days, 1):
        try:
            rows = reload_day(day)
            print(f"[{i}/{len(days)}] {day}: {rows} rows upserted", flush=True)
        except Exception as exc:  # keep going; the re-run picks it up
            failed.append(str(day))
            print(f"[{i}/{len(days)}] {day}: FAILED {exc!r}", flush=True)
    remaining = gap_dates()
    print(f"done. failed={failed} dates_still_with_gaps={len(remaining)}", flush=True)
    return 1 if failed or remaining else 0


if __name__ == "__main__":
    raise SystemExit(main())
