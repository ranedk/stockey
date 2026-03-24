from __future__ import annotations

import argparse
import json
from datetime import date as date_cls

import pandas as pd

from utils.company_master import attach_company_master_id
from utils.db import sql_to_df, upsert_to_db


CONSTITUENTS_TABLE = "advisory_screener_constituents"
SNAPSHOT_TABLE = "public.dhan_screener_snapshots"

NUMERIC_COLUMNS = [
    "security_id",
    "rank",
    "last_price",
    "price_change",
    "price_change_pct",
    "volume",
    "market_cap",
    "pe_ratio",
]


def load_snapshots(
    *,
    screener_slug: str | None = None,
    snapshot_date: date_cls | None = None,
    latest_only: bool = True,
) -> pd.DataFrame:
    params: dict[str, object] = {}
    if latest_only and snapshot_date is None:
        conditions: list[str] = []
        if screener_slug:
            conditions.append("s.screener_slug = %(screener_slug)s")
            params["screener_slug"] = screener_slug
        where_sql = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        return sql_to_df(
            f"""
            SELECT s.screener_slug, s.screener_name, s.screener_url, s.date, s.raw_json, s.load_ts
            FROM {SNAPSHOT_TABLE} AS s
            JOIN (
                SELECT screener_slug, MAX(date) AS max_date
                FROM {SNAPSHOT_TABLE}
                GROUP BY screener_slug
            ) latest
              ON latest.screener_slug = s.screener_slug
             AND latest.max_date = s.date
            {where_sql}
            ORDER BY s.screener_slug, s.date
            """,
            params=params or None,
        )

    conditions = []
    if screener_slug:
        conditions.append("screener_slug = %(screener_slug)s")
        params["screener_slug"] = screener_slug
    if snapshot_date:
        conditions.append("date = %(snapshot_date)s")
        params["snapshot_date"] = snapshot_date
    where_sql = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    return sql_to_df(
        f"""
        SELECT screener_slug, screener_name, screener_url, date, raw_json, load_ts
        FROM {SNAPSHOT_TABLE}
        {where_sql}
        ORDER BY screener_slug, date
        """,
        params=params or None,
    )


def extract_metadata(payload: dict) -> dict[str, object]:
    for value in payload.values():
        if not isinstance(value, dict):
            continue
        body = value.get("b")
        if not isinstance(body, dict):
            continue
        data = body.get("data")
        if not isinstance(data, list):
            continue
        for item in data:
            if isinstance(item, dict) and any(
                key in item for key in ["name", "seo_id", "scanx_id", "screener_id"]
            ):
                return item
    return {}


def extract_constituent_items(payload: dict) -> list[dict[str, object]]:
    best: list[dict[str, object]] = []
    for value in payload.values():
        if not isinstance(value, dict):
            continue
        body = value.get("b")
        if not isinstance(body, dict):
            continue
        data = body.get("data")
        if not isinstance(data, list):
            continue
        matches = [
            item
            for item in data
            if isinstance(item, dict) and item.get("Sym") and item.get("Exch")
        ]
        if len(matches) > len(best):
            best = matches
    return best


def normalize_snapshot_row(snapshot: pd.Series) -> pd.DataFrame:
    payload = snapshot["raw_json"]
    if not isinstance(payload, dict):
        return pd.DataFrame()

    items = extract_constituent_items(payload)
    if not items:
        return pd.DataFrame()

    metadata = extract_metadata(payload)
    rows: list[dict[str, object]] = []
    for rank, item in enumerate(items, start=1):
        rows.append(
            {
                "date": pd.Timestamp(snapshot["date"], tz="UTC").normalize(),
                "screener_slug": snapshot["screener_slug"],
                "screener_name": snapshot["screener_name"],
                "screener_url": snapshot["screener_url"],
                "scanx_name": metadata.get("name"),
                "scanx_seo_id": metadata.get("seo_id"),
                "scanx_id": metadata.get("scanx_id"),
                "scanx_screener_id": metadata.get("screener_id"),
                "ticker": item.get("Sym"),
                "exchange": item.get("Exch"),
                "security_id": item.get("Sid"),
                "instrument": item.get("Inst"),
                "isin": item.get("Isin"),
                "display_name": item.get("DispSym"),
                "rank": rank,
                "last_price": item.get("Ltp"),
                "price_change": item.get("Pchange"),
                "price_change_pct": item.get("PPerchange"),
                "volume": item.get("Volume"),
                "market_cap": item.get("Mcap"),
                "pe_ratio": item.get("Pe"),
                "raw_item_json": json.dumps(item, ensure_ascii=False, sort_keys=True),
                "load_ts": pd.Timestamp.utcnow(),
            }
        )

    df = pd.DataFrame(rows)
    if df.empty:
        return df

    df["ticker"] = df["ticker"].astype("string").str.strip().str.upper()
    df["exchange"] = df["exchange"].astype("string").str.strip().str.upper()
    for col in NUMERIC_COLUMNS:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df = attach_company_master_id(
        df,
        ticker_column="ticker",
        exchange_column="exchange",
    )
    ordered = [
        "date",
        "screener_slug",
        "screener_name",
        "screener_url",
        "scanx_name",
        "scanx_seo_id",
        "scanx_id",
        "scanx_screener_id",
        "ticker",
        "exchange",
        "company_master_id",
        "security_id",
        "instrument",
        "isin",
        "display_name",
        "rank",
        "last_price",
        "price_change",
        "price_change_pct",
        "volume",
        "market_cap",
        "pe_ratio",
        "raw_item_json",
        "load_ts",
    ]
    return df[ordered].drop_duplicates(
        subset=["date", "screener_slug", "ticker", "exchange"],
        keep="last",
    )


def build_constituents(
    *,
    screener_slug: str | None = None,
    snapshot_date: date_cls | None = None,
    latest_only: bool = True,
) -> pd.DataFrame:
    snapshots = load_snapshots(
        screener_slug=screener_slug,
        snapshot_date=snapshot_date,
        latest_only=latest_only,
    )
    if snapshots.empty:
        return pd.DataFrame()

    frames = [normalize_snapshot_row(row) for _, row in snapshots.iterrows()]
    frames = [frame for frame in frames if not frame.empty]
    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True)


def persist_constituents(df: pd.DataFrame) -> None:
    if df.empty:
        return
    upsert_to_db(
        df,
        CONSTITUENTS_TABLE,
        unique_keys=["date", "screener_slug", "ticker", "exchange"],
        timescaledb_column="date",
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Normalize stored Dhan ScanX screener snapshots into a constituents table."
    )
    parser.add_argument("--screener", dest="screener_slug")
    parser.add_argument("--date", type=date_cls.fromisoformat)
    parser.add_argument(
        "--all-dates",
        action="store_true",
        help="Process all stored snapshot dates instead of only the latest per screener.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Build and summarize rows without writing to the database.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    df = build_constituents(
        screener_slug=args.screener_slug,
        snapshot_date=args.date,
        latest_only=not args.all_dates and args.date is None,
    )
    if not args.dry_run:
        persist_constituents(df)

    result = {
        "status": "ok",
        "table": CONSTITUENTS_TABLE,
        "row_count": int(len(df)),
        "screener_count": int(df["screener_slug"].nunique()) if not df.empty else 0,
        "date_min": df["date"].min().date().isoformat() if not df.empty else None,
        "date_max": df["date"].max().date().isoformat() if not df.empty else None,
        "sample": df.head(5).to_dict(orient="records") if not df.empty else [],
        "dry_run": bool(args.dry_run),
    }
    print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
