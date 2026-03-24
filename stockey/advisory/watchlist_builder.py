from __future__ import annotations

import argparse
import json

import pandas as pd

from utils.db import db_session, sql_to_df, upsert_to_db
from utils.sync import parse_datetime_arg


TABLE_NAME = "advisory_watchlist"


def normalize_timestamp(series: pd.Series) -> pd.Series:
    return pd.to_datetime(series, utc=True, errors="coerce").dt.normalize()


def load_candidate_rows(
    *,
    asof_date: pd.Timestamp | None = None,
    setup_ids: list[str] | None = None,
    symbols: list[str] | None = None,
) -> pd.DataFrame:
    clauses = []
    params: list[object] = []
    if asof_date is not None:
        clauses.append("asof_date = %s")
        params.append(asof_date)
    else:
        clauses.append("asof_date = (SELECT MAX(asof_date) FROM advisory_candidates)")
    if setup_ids:
        clauses.append("setup_id = ANY(%s)")
        params.append([value.upper() for value in setup_ids])
    if symbols:
        clauses.append("symbol = ANY(%s)")
        params.append([value.upper() for value in symbols])
    df = sql_to_df(
        f"""
        SELECT *
        FROM advisory_candidates
        WHERE {' AND '.join(clauses)}
        ORDER BY setup_id, symbol
        """,
        params=tuple(params) if params else None,
    )
    if df.empty:
        return df
    df["asof_date"] = normalize_timestamp(df["asof_date"])
    df["symbol"] = df["symbol"].astype("string").str.upper()
    if "watch_enabled" not in df.columns:
        df["watch_enabled"] = True
    else:
        df["watch_enabled"] = df["watch_enabled"].fillna(True)
    if "watch_reasons" not in df.columns:
        df["watch_reasons"] = "[]"
    df = df[df["watch_enabled"] == True].reset_index(drop=True)
    return df


def build_watchlist(
    *,
    asof_date: pd.Timestamp | None = None,
    setup_ids: list[str] | None = None,
    symbols: list[str] | None = None,
) -> pd.DataFrame:
    candidates = load_candidate_rows(asof_date=asof_date, setup_ids=setup_ids, symbols=symbols)
    if candidates.empty:
        return pd.DataFrame()

    try:
        existing = sql_to_df(
            """
            SELECT asof_date, setup_id, symbol, last_checked_at, last_document_published_on, watch_status
            FROM advisory_watchlist
            """
        )
    except Exception:
        existing = pd.DataFrame()
    if not existing.empty:
        existing["asof_date"] = normalize_timestamp(existing["asof_date"])
        existing["symbol"] = existing["symbol"].astype("string").str.upper()
        existing = existing[
            existing.set_index(["asof_date", "setup_id", "symbol"]).index.isin(
                candidates.set_index(["asof_date", "setup_id", "symbol"]).index
            )
        ]

    out = candidates.copy()
    out["watch_reasons_json"] = out["watch_reasons"].astype("string")
    out["watch_status"] = "active"
    out["watch_started_at"] = pd.Timestamp.utcnow()
    out["last_checked_at"] = pd.NaT
    out["last_document_published_on"] = pd.NaT
    out["load_ts"] = pd.Timestamp.utcnow()

    if not existing.empty:
        out = out.merge(
            existing,
            on=["asof_date", "setup_id", "symbol"],
            how="left",
            suffixes=("", "_existing"),
        )
        out["watch_status"] = out["watch_status_existing"].fillna(out["watch_status"])
        out["last_checked_at"] = pd.to_datetime(out["last_checked_at_existing"], utc=True, errors="coerce")
        out["last_document_published_on"] = pd.to_datetime(
            out["last_document_published_on_existing"], utc=True, errors="coerce"
        )
        out = out.drop(
            columns=[
                "watch_status_existing",
                "last_checked_at_existing",
                "last_document_published_on_existing",
            ],
            errors="ignore",
        )

    ordered_cols = [
        "asof_date",
        "setup_id",
        "setup_name",
        "regime_name",
        "symbol",
        "company_master_id",
        "screener_slug",
        "rank",
        "watch_enabled",
        "watch_reasons_json",
        "watch_status",
        "watch_started_at",
        "last_checked_at",
        "last_document_published_on",
        "load_ts",
    ]
    return out[ordered_cols].drop_duplicates(subset=["asof_date", "setup_id", "symbol"], keep="last")


def persist_watchlist(df: pd.DataFrame, *, rebuild: bool = False, asof_date: pd.Timestamp | None = None) -> None:
    if df.empty:
        return
    if rebuild and asof_date is not None:
        with db_session() as (_, cur):
            cur.execute(
                f"CREATE TABLE IF NOT EXISTS {TABLE_NAME} (asof_date TIMESTAMPTZ, setup_id TEXT, symbol TEXT)"
            )
            cur.execute(f"DELETE FROM {TABLE_NAME} WHERE asof_date = %s", (asof_date,))
    upsert_to_db(
        df,
        TABLE_NAME,
        unique_keys=["asof_date", "setup_id", "symbol"],
        timescaledb_column="asof_date",
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build advisory announcement watchlist from candidates.")
    parser.add_argument("--date", type=parse_datetime_arg, help="Asof date in YYYY-MM-DD")
    parser.add_argument("--setup", dest="setup_ids", nargs="*", help="Optional setup ids")
    parser.add_argument("--symbols", nargs="*", help="Optional symbols")
    parser.add_argument("--rebuild", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def summarize(df: pd.DataFrame) -> dict[str, object]:
    if df.empty:
        return {
            "status": "ok",
            "table": TABLE_NAME,
            "row_count": 0,
            "setup_count": 0,
            "symbol_count": 0,
            "sample": [],
        }
    return {
        "status": "ok",
        "table": TABLE_NAME,
        "row_count": int(len(df)),
        "setup_count": int(df["setup_id"].nunique()),
        "symbol_count": int(df["symbol"].nunique()),
        "sample": df.head(10).to_dict(orient="records"),
    }


def main() -> int:
    args = parse_args()
    asof_date = pd.Timestamp(args.date, tz="UTC") if args.date else None
    df = build_watchlist(
        asof_date=asof_date,
        setup_ids=args.setup_ids,
        symbols=args.symbols,
    )
    if not args.dry_run:
        persist_watchlist(df, rebuild=args.rebuild, asof_date=asof_date or (df["asof_date"].max() if not df.empty else None))
    result = summarize(df)
    result["dry_run"] = bool(args.dry_run)
    print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
