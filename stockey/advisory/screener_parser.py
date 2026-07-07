from __future__ import annotations

import argparse
import json
import re
from datetime import date as date_cls

import pandas as pd

from utils.db import db_session, execute_db_operation, sql_to_df, upsert_to_db


CONSTITUENTS_TABLE = "advisory_screener_constituents"
SNAPSHOT_TABLE = "public.screenerin_screener_snapshots"
NUMERIC_COLUMNS = [
    "rank",
    "last_price",
    "market_cap",
    "pe_ratio",
    "price_change",
    "price_change_pct",
]
INTEGER_COLUMNS = [
    "scanx_screener_id",
    "security_id",
    "volume",
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


def resolve_company_master(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df
    source_tickers = df["source_ticker"].dropna().astype("string").unique().tolist()
    if not source_tickers:
        df["company_master_id"] = pd.NA
        df["ticker"] = df["source_ticker"]
        df["exchange"] = pd.NA
        return df

    lookup = sql_to_df(
        """
        SELECT company_master_id, nse_ticker, bse_ticker, company_name, dhan_nse_id
        FROM company_master
        WHERE nse_ticker = ANY(%s) OR bse_ticker = ANY(%s)
        """,
        params=(source_tickers, source_tickers),
    )
    name_lookup = sql_to_df(
        """
        SELECT company_master_id, nse_ticker, bse_ticker, company_name, dhan_nse_id
        FROM company_master
        WHERE company_name IS NOT NULL
        """
    )
    nse_map = (
        lookup.dropna(subset=["nse_ticker"]).drop_duplicates(subset=["nse_ticker"], keep="last").set_index("nse_ticker")
        if not lookup.empty
        else pd.DataFrame()
    )
    bse_map = (
        lookup.dropna(subset=["bse_ticker"]).drop_duplicates(subset=["bse_ticker"], keep="last").set_index("bse_ticker")
        if not lookup.empty
        else pd.DataFrame()
    )
    name_map: dict[str, pd.Series] = {}
    if not name_lookup.empty:
        keyed = name_lookup.copy()
        keyed["company_name_key"] = keyed["company_name"].map(_canonical_text)
        keyed = keyed.dropna(subset=["company_name_key"]).drop_duplicates(subset=["company_name_key"], keep="last")
        name_map = {
            str(row["company_name_key"]): row
            for _, row in keyed.iterrows()
        }

    resolved_company_ids: list[object] = []
    resolved_tickers: list[object] = []
    resolved_exchanges: list[object] = []
    for source_ticker, display_name in zip(
        df["source_ticker"].astype("string").tolist(),
        df["display_name"].astype("string").tolist(),
        strict=False,
    ):
        nse_row = nse_map.loc[source_ticker] if isinstance(nse_map, pd.DataFrame) and source_ticker in nse_map.index else None
        bse_row = bse_map.loc[source_ticker] if isinstance(bse_map, pd.DataFrame) and source_ticker in bse_map.index else None

        chosen = None
        chosen_nse_ticker = None
        chosen_ticker = pd.NA
        chosen_exchange = pd.NA
        if isinstance(nse_row, pd.DataFrame):
            nse_row = nse_row.iloc[0]
        if isinstance(bse_row, pd.DataFrame):
            bse_row = bse_row.iloc[0]

        if nse_row is not None:
            chosen = nse_row
            chosen_nse_ticker = source_ticker
        elif bse_row is not None:
            chosen = bse_row
            chosen_nse_ticker = bse_row.get("nse_ticker")
        else:
            name_key = _canonical_text(display_name)
            if name_key and name_key in name_map:
                chosen = name_map[name_key]
                chosen_nse_ticker = chosen.get("nse_ticker")
            elif name_key:
                prefix_matches = [
                    row
                    for key, row in name_map.items()
                    if key.startswith(name_key) or name_key.startswith(key)
                ]
                if len(prefix_matches) == 1:
                    chosen = prefix_matches[0]
                    chosen_nse_ticker = chosen.get("nse_ticker")

        if chosen is not None:
            nse_ticker = chosen_nse_ticker
            if pd.notna(nse_ticker) and not str(nse_ticker).endswith("-BOM"):
                chosen_exchange = "NSE"
                chosen_ticker = nse_ticker

        resolved_company_ids.append(chosen.get("company_master_id") if chosen is not None else pd.NA)
        resolved_tickers.append(chosen_ticker)
        resolved_exchanges.append(chosen_exchange if pd.notna(chosen_exchange) else pd.NA)

    out = df.copy()
    out["company_master_id"] = pd.Series(resolved_company_ids, dtype="string")
    out["ticker"] = pd.Series(resolved_tickers, dtype="string").str.upper()
    out["exchange"] = pd.Series(resolved_exchanges, dtype="string").str.upper()
    return out[out["ticker"].notna()].reset_index(drop=True)


def _canonical_text(value: object) -> str | None:
    if pd.isna(value):
        return None
    text = re.sub(r"[^a-z0-9]+", "", str(value).lower())
    return text or None


def normalize_snapshot_row(snapshot: pd.Series) -> pd.DataFrame:
    payload = snapshot["raw_json"]
    if not isinstance(payload, dict):
        return pd.DataFrame()
    companies = payload.get("companies")
    if not isinstance(companies, list) or not companies:
        return pd.DataFrame()

    rows: list[dict[str, object]] = []
    for index, item in enumerate(companies, start=1):
        metrics = item.get("metrics") if isinstance(item.get("metrics"), dict) else {}
        rows.append(
            {
                "date": pd.Timestamp(snapshot["date"], tz="UTC").normalize(),
                "screener_slug": snapshot["screener_slug"],
                "screener_name": snapshot["screener_name"],
                "screener_url": snapshot["screener_url"],
                "scanx_name": pd.NA,
                "scanx_seo_id": pd.NA,
                "scanx_id": pd.NA,
                "scanx_screener_id": pd.NA,
                "source_ticker": str(item.get("page_slug") or "").strip().upper() or pd.NA,
                "display_name": item.get("name"),
                "rank": item.get("s_no") if item.get("s_no") is not None else index,
                "last_price": metrics.get("cmp_rs"),
                "price_change": pd.NA,
                "price_change_pct": pd.NA,
                "volume": pd.NA,
                "market_cap": metrics.get("mar_cap_rscr"),
                "pe_ratio": metrics.get("p_e"),
                "security_id": pd.NA,
                "instrument": pd.NA,
                "isin": pd.NA,
                "raw_item_json": json.dumps(item, ensure_ascii=False, sort_keys=True),
                "load_ts": pd.Timestamp.utcnow(),
            }
        )

    df = pd.DataFrame(rows)
    if df.empty:
        return df
    df["source_ticker"] = df["source_ticker"].astype("string").str.upper()
    df = resolve_company_master(df)
    for col in NUMERIC_COLUMNS:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    for col in INTEGER_COLUMNS:
        df[col] = pd.to_numeric(df[col], errors="coerce").astype("Int64")
    df["rank"] = pd.to_numeric(df["rank"], errors="coerce").astype("Int64")
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
    return df[ordered].drop_duplicates(subset=["date", "screener_slug", "ticker", "exchange"], keep="last")


def build_constituents(
    *,
    screener_slug: str | None = None,
    snapshot_date: date_cls | None = None,
    latest_only: bool = True,
) -> pd.DataFrame:
    snapshots = load_snapshots(screener_slug=screener_slug, snapshot_date=snapshot_date, latest_only=latest_only)
    frames: list[pd.DataFrame] = []
    if not snapshots.empty:
        frames = [normalize_snapshot_row(row) for _, row in snapshots.iterrows()]
        frames = [frame for frame in frames if not frame.empty]
    # Action-first discovery lane: the whole-market breakout scan joins the universe as an
    # ordinary constituents source (junk-eliminating filters only), so the downstream
    # candidate/technical/risk/lifecycle gates still decide quality. A targeted
    # screener_slug rebuild keeps the scan out of that slug's refresh.
    if screener_slug is None:
        from advisory.market_action_scan import MARKET_ACTION_SCAN_ENABLED, safe_scan_market_action

        if MARKET_ACTION_SCAN_ENABLED:
            scan_frame = safe_scan_market_action(asof_date=snapshot_date)
            if not scan_frame.empty:
                frames.append(scan_frame)
        # Hypothesis strategy instances: each active hypothesis's universe joins as its own
        # screener source (hypothesis-<id>); paused/retired hypotheses stop emitting.
        from advisory.hypothesis_screeners import HYPOTHESIS_SCREENERS_ENABLED, safe_build_hypothesis_constituents

        if HYPOTHESIS_SCREENERS_ENABLED:
            hypothesis_frame = safe_build_hypothesis_constituents(asof_date=snapshot_date)
            if not hypothesis_frame.empty:
                frames.append(hypothesis_frame)
    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True)


def persist_constituents(df: pd.DataFrame) -> None:
    if df.empty:
        return
    keys = (
        df[["date", "screener_slug"]]
        .drop_duplicates()
        .itertuples(index=False, name=None)
    )

    def _delete_existing_constituents() -> None:
        with db_session() as (_, cur):
            for snapshot_date, screener_slug in keys:
                cur.execute(
                    f"DELETE FROM {CONSTITUENTS_TABLE} WHERE date = %s AND screener_slug = %s",
                    (snapshot_date, screener_slug),
                )

    execute_db_operation(
        _delete_existing_constituents,
        operation_name="screener_parser:persist_constituents:delete_existing",
    )
    upsert_to_db(
        df,
        CONSTITUENTS_TABLE,
        unique_keys=["date", "screener_slug", "ticker", "exchange"],
        timescaledb_column="date",
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Normalize stored Screener.in snapshots into advisory_screener_constituents.")
    parser.add_argument("--screener", dest="screener_slug")
    parser.add_argument("--date", type=date_cls.fromisoformat)
    parser.add_argument("--all-dates", action="store_true", help="Process all stored dates instead of latest only.")
    parser.add_argument("--dry-run", action="store_true", help="Build rows without writing to the database.")
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
