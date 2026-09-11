"""Restore a retired stockey table from its 2026-08-15 retirement archive.

Two tables retired 2026-08-15 as write-only -- no reader anywhere -- were revived 2026-09-11 because
systrader now reads them as slicing traits: nseindia_mto (delivery %) and nseindia_circuit_hit
(price-band hits). Each was archived to S3 as monthly gzip CSVs (docs/DATA_INVENTORY.md). This one-off
puts a table's history back through the exact frame shape its live parser writes, so restored rows and
rows parsed today are indistinguishable (tests/test_data_platform.py::
test_restored_*_archive_rows_match_the_live_parser). Days after the archive ends are filled by the
parser's own backfill:

    python -m data.nseindia.bhavcopy_parser --force --from 2026-08-01

Idempotent: every month is an upsert on the table's unique keys, so a re-run changes nothing.

    python scripts/restore_retired_table.py nseindia_mto
    python scripts/restore_retired_table.py nseindia_circuit_hit --dry-run
"""
from __future__ import annotations

import argparse
import gzip
import io
import sys
from pathlib import Path
from typing import Callable

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from data.nseindia.bhavcopy_parser import with_company_master  # noqa: E402
from scripts.s3_query_runner import AWS_BUCKET_NAME, _get_client  # noqa: E402
from utils.db import upsert_to_db  # noqa: E402

ARCHIVE_ROOT = "archives/retired_2026-08-15/"


def _naive_date(col: pd.Series) -> pd.Series:
    # The archive stored the column as timestamptz at midnight UTC; the live parsers write a naive date.
    return pd.to_datetime(col, utc=True).dt.tz_convert(None)


def _mto_frame(df: pd.DataFrame) -> pd.DataFrame:
    """As bhavcopy_parser.parse_mto builds it, before company mapping."""
    df = df[df["record_type"].astype(str).str.strip() == "20"]
    out = df[["record_type", "sr_no", "symbol", "series", "volume", "deliverable_volume", "deliverable_percent"]].copy()
    for c in ["volume", "deliverable_volume"]:
        out[c] = pd.to_numeric(out[c], errors="coerce").astype("Int64")
    out["deliverable_percent"] = pd.to_numeric(out["deliverable_percent"], errors="coerce")
    out["symbol"] = out["symbol"].astype(str).str.strip()
    out["series"] = out["series"].astype(str).str.strip()
    out["sr_no"] = out["sr_no"].astype(str).str.strip()  # text, as parse_mto pins it
    out["date"] = _naive_date(df["date"])
    return out.drop_duplicates(subset=["date", "symbol"], keep="last")


def _circuit_hit_frame(df: pd.DataFrame) -> pd.DataFrame:
    """As bhavcopy_parser.parse_circuit_hit builds it, before company mapping."""
    out = df[["symbol", "series", "circuit_hit"]].copy()
    out["date"] = _naive_date(df["date"])
    return out.drop_duplicates(subset=["date", "symbol", "series", "circuit_hit"], keep="last")


# table -> (frame builder, the live parser's unique keys, read dtypes)
TABLES: dict[str, tuple[Callable[[pd.DataFrame], pd.DataFrame], list[str], dict[str, type]]] = {
    "nseindia_mto": (_mto_frame, ["date", "symbol"], {"symbol": str, "series": str}),
    "nseindia_circuit_hit": (
        _circuit_hit_frame,
        ["date", "symbol", "series", "circuit_hit"],
        {"symbol": str, "series": str, "circuit_hit": str},
    ),
}


def frame_from_archive(table: str, raw_gz: bytes) -> pd.DataFrame:
    """One archived month of `table` as its live parser would have built it."""
    build, _, dtypes = TABLES[table]
    return build(pd.read_csv(io.BytesIO(gzip.decompress(raw_gz)), dtype=dtypes))


def archive_keys(s3, table: str) -> list[str]:
    prefix = f"{ARCHIVE_ROOT}{table}/"
    keys: list[str] = []
    token = None
    while True:
        kwargs = {"Bucket": AWS_BUCKET_NAME, "Prefix": prefix, "MaxKeys": 1000}
        if token:
            kwargs["ContinuationToken"] = token
        resp = s3.list_objects_v2(**kwargs)
        keys += [o["Key"] for o in resp.get("Contents", []) if o["Key"].endswith(".csv.gz")]
        if not resp.get("IsTruncated"):
            return sorted(keys)
        token = resp["NextContinuationToken"]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("table", choices=sorted(TABLES))
    ap.add_argument("--dry-run", action="store_true", help="read and count, write nothing")
    args = ap.parse_args()

    _, unique_keys, _ = TABLES[args.table]
    s3 = _get_client()
    keys = archive_keys(s3, args.table)
    if not keys:
        print(f"no archive objects for {args.table} under {ARCHIVE_ROOT}", file=sys.stderr)
        return 1
    total = 0
    for key in keys:
        df = frame_from_archive(args.table, s3.get_object(Bucket=AWS_BUCKET_NAME, Key=key)["Body"].read())
        if not args.dry_run:
            upsert_to_db(with_company_master(df), args.table, unique_keys=unique_keys)
        total += len(df)
        print(f"{key.rsplit('/', 1)[-1]}: {len(df)} rows", flush=True)
    print(f"{'would restore' if args.dry_run else 'restored'} {total} rows from {len(keys)} months into {args.table}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
