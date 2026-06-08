from __future__ import annotations

import argparse
import hashlib
import json
from typing import Any

import pandas as pd

from utils.db import db_session, sql_to_df, upsert_to_db
from utils.sync import parse_datetime_arg


TABLE_NAME = "advisory_exchange_events"
EVENT_COLUMNS = [
    "event_id",
    "event_source",
    "event_type",
    "symbol",
    "company_master_id",
    "event_date",
    "known_on",
    "disclosure_date",
    "participant",
    "side",
    "quantity",
    "price",
    "value_inr",
    "holding_pct_before",
    "holding_pct_after",
    "event_summary",
    "raw_json",
    "load_ts",
]


def normalize_timestamp(series: pd.Series) -> pd.Series:
    return pd.to_datetime(series, utc=True, errors="coerce").dt.normalize()


def table_exists(table_name: str) -> bool:
    try:
        df = sql_to_df(
            """
            SELECT 1 AS exists_flag
            FROM information_schema.tables
            WHERE table_schema = 'public'
              AND table_name = %s
            LIMIT 1
            """,
            params=(table_name,),
        )
    except Exception:
        return False
    return not df.empty


def require_table_exists(table_name: str) -> bool:
    df = sql_to_df(
        """
        SELECT 1 AS exists_flag
        FROM information_schema.tables
        WHERE table_schema = 'public'
          AND table_name = %s
        LIMIT 1
        """,
        params=(table_name,),
    )
    return not df.empty


def _json_ready(value: Any) -> Any:
    if isinstance(value, pd.Timestamp):
        return None if pd.isna(value) else value.isoformat()
    if pd.isna(value):
        return None
    return value


def _event_id(row: dict[str, Any]) -> str:
    identity = {
        key: _json_ready(row.get(key))
        for key in [
            "event_source",
            "symbol",
            "event_type",
            "event_date",
            "known_on",
            "participant",
            "side",
            "quantity",
            "price",
            "value_inr",
        ]
    }
    payload = json.dumps(identity, sort_keys=True, default=str, ensure_ascii=False)
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()


def _clean_symbol(series: pd.Series) -> pd.Series:
    return series.astype("string").str.strip().str.upper()


def _numeric(series: pd.Series | None) -> pd.Series:
    if series is None:
        return pd.Series(dtype="float64")
    return pd.to_numeric(series.astype("string").str.replace(",", "", regex=False), errors="coerce")


def _normalize_side(value: Any) -> str | None:
    if pd.isna(value):
        return None
    text = str(value).strip().upper()
    if text in {"BUY", "B", "PURCHASE", "ACQUIRE", "ACQUISITION"}:
        return "BUY"
    if text in {"SELL", "S", "SALE", "DISPOSE", "DISPOSAL"}:
        return "SELL"
    if "BUY" in text or "ACQU" in text:
        return "BUY"
    if "SELL" in text or "SALE" in text or "DISPOS" in text:
        return "SELL"
    return text or None


def _finish_events(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df
    df["event_date"] = normalize_timestamp(df["event_date"])
    df["known_on"] = normalize_timestamp(df["known_on"])
    df["disclosure_date"] = normalize_timestamp(df.get("disclosure_date", df["known_on"]))
    df["symbol"] = _clean_symbol(df["symbol"])
    for col in ["quantity", "price", "value_inr", "holding_pct_before", "holding_pct_after"]:
        if col not in df.columns:
            df[col] = pd.NA
        df[col] = pd.to_numeric(df[col], errors="coerce")
    if "company_master_id" not in df.columns:
        df["company_master_id"] = pd.NA
    if "participant" not in df.columns:
        df["participant"] = pd.NA
    if "side" not in df.columns:
        df["side"] = pd.NA
    df["side"] = df["side"].map(_normalize_side)
    df["raw_json"] = [
        json.dumps({key: _json_ready(value) for key, value in row.items()}, ensure_ascii=False, sort_keys=True, default=str)
        for row in df.to_dict(orient="records")
    ]
    df["event_id"] = [_event_id(row) for row in df.to_dict(orient="records")]
    df["load_ts"] = pd.Timestamp.utcnow()
    return df[EVENT_COLUMNS].dropna(subset=["event_id", "symbol", "known_on"]).drop_duplicates(subset=["event_id", "known_on"], keep="last")


def normalize_block_or_bulk(df: pd.DataFrame, *, source: str) -> pd.DataFrame:
    if df.empty:
        return df
    out = pd.DataFrame(index=df.index)
    out["event_source"] = source
    out["event_type"] = "BLOCK_DEAL" if source == "nse_block_deal" else "BULK_DEAL"
    out["symbol"] = df.get("symbol")
    out["company_master_id"] = df.get("company_master_id")
    out["event_date"] = df.get("date")
    out["known_on"] = df.get("date")
    out["disclosure_date"] = df.get("date")
    out["participant"] = df.get("client_name")
    out["side"] = df.get("buysell")
    out["quantity"] = _numeric(df.get("quantity"))
    out["price"] = _numeric(df.get("price"))
    out["value_inr"] = out["quantity"] * out["price"]
    out["holding_pct_before"] = pd.NA
    out["holding_pct_after"] = pd.NA
    out["event_summary"] = (
        out["event_type"].astype(str)
        + " "
        + out["side"].fillna("").astype(str)
        + " by "
        + out["participant"].fillna("").astype(str)
    )
    return _finish_events(out)


def normalize_short_selling(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df
    out = pd.DataFrame(index=df.index)
    out["event_source"] = "nse_short_selling"
    out["event_type"] = "SHORT_SELLING"
    out["symbol"] = df.get("symbol")
    out["company_master_id"] = df.get("company_master_id")
    out["event_date"] = df.get("date")
    out["known_on"] = df.get("date")
    out["disclosure_date"] = df.get("date")
    out["participant"] = pd.NA
    out["side"] = "SELL"
    out["quantity"] = _numeric(df.get("quantity"))
    out["price"] = pd.NA
    out["value_inr"] = pd.NA
    out["holding_pct_before"] = pd.NA
    out["holding_pct_after"] = pd.NA
    out["event_summary"] = "Short selling quantity " + out["quantity"].fillna(0).astype(int).astype(str)
    return _finish_events(out)


def normalize_insider_deals(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df
    out = pd.DataFrame(index=df.index)
    out["event_source"] = "nse_insider_deal"
    out["event_type"] = "INSIDER_DEAL"
    out["symbol"] = df.get("symbol")
    out["company_master_id"] = df.get("company_master_id")
    out["event_date"] = df.get("trade_date_to", df.get("date"))
    out["known_on"] = df.get("date")
    out["disclosure_date"] = df.get("date")
    out["participant"] = df.get("insider_name")
    out["side"] = df.get("transaction_type")
    out["quantity"] = _numeric(df.get("quantity"))
    out["price"] = pd.NA
    out["value_inr"] = _numeric(df.get("value_inr"))
    out["holding_pct_before"] = _numeric(df.get("holding_pct_before"))
    out["holding_pct_after"] = _numeric(df.get("holding_pct_after"))
    out["event_summary"] = (
        "Insider "
        + out["side"].fillna("").astype(str)
        + " by "
        + out["participant"].fillna("").astype(str)
        + " category="
        + df.get("person_category", pd.Series("", index=df.index)).fillna("").astype(str)
    )
    return _finish_events(out)


def normalize_corporate_actions(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df
    out = pd.DataFrame(index=df.index)
    out["event_source"] = "nse_corporate_action"
    action_type = df.get("action_type", pd.Series("CORPORATE_ACTION", index=df.index)).fillna("CORPORATE_ACTION")
    out["event_type"] = action_type.astype(str).str.strip().str.upper().replace({"": "CORPORATE_ACTION"})
    out["symbol"] = df.get("symbol")
    out["company_master_id"] = df.get("company_master_id")
    out["event_date"] = df.get("date")
    out["known_on"] = df.get("ca_broadcast_date", df.get("record_date", df.get("date")))
    out["disclosure_date"] = out["known_on"]
    out["participant"] = df.get("company", df.get("security_name"))
    out["side"] = pd.NA
    out["quantity"] = pd.NA
    out["price"] = pd.NA
    out["value_inr"] = pd.NA
    out["holding_pct_before"] = pd.NA
    out["holding_pct_after"] = pd.NA
    subject = df.get("subject", pd.Series("Corporate action", index=df.index)).fillna("Corporate action").astype(str)
    source = df.get("source", pd.Series("", index=df.index)).fillna("").astype(str)
    out["event_summary"] = ("Corporate action " + out["event_type"].astype(str) + " " + subject + " source=" + source).str.strip()
    return _finish_events(out)


def normalize_earnings_events(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df
    out = pd.DataFrame(index=df.index)
    out["event_source"] = "nse_earnings_event"
    out["event_type"] = "EARNINGS_EVENT"
    out["symbol"] = df.get("symbol")
    out["company_master_id"] = df.get("company_master_id")
    out["event_date"] = df.get("reporting_date", df.get("date"))
    out["known_on"] = df.get("date")
    out["disclosure_date"] = df.get("date")
    out["participant"] = df.get("company")
    out["side"] = pd.NA
    out["quantity"] = pd.NA
    out["price"] = pd.NA
    out["value_inr"] = pd.NA
    out["holding_pct_before"] = pd.NA
    out["holding_pct_after"] = pd.NA
    out["event_summary"] = (
        "Earnings event period="
        + df.get("period", pd.Series("", index=df.index)).fillna("").astype(str)
        + " reporting_date="
        + pd.to_datetime(out["event_date"], utc=True, errors="coerce").astype(str)
    )
    return _finish_events(out)


def normalize_recent_events(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df
    out = pd.DataFrame(index=df.index)
    out["event_source"] = "nse_event_calendar"
    out["event_type"] = "NSE_EVENT"
    out["symbol"] = df.get("symbol")
    out["company_master_id"] = df.get("company_master_id")
    out["event_date"] = df.get("date")
    out["known_on"] = df.get("date")
    out["disclosure_date"] = df.get("date")
    out["participant"] = pd.NA
    out["side"] = pd.NA
    out["quantity"] = pd.NA
    out["price"] = pd.NA
    out["value_inr"] = pd.NA
    out["holding_pct_before"] = pd.NA
    out["holding_pct_after"] = pd.NA
    out["event_summary"] = (
        df.get("purpose", pd.Series("NSE event", index=df.index)).fillna("NSE event").astype(str)
        + " "
        + df.get("details", pd.Series("", index=df.index)).fillna("").astype(str)
    ).str.strip()
    return _finish_events(out)


def _load_table(table_name: str, *, from_date: pd.Timestamp | None, to_date: pd.Timestamp | None) -> pd.DataFrame:
    if not table_exists(table_name):
        return pd.DataFrame()
    clauses: list[str] = []
    params: dict[str, Any] = {}
    if from_date is not None:
        clauses.append("date >= %(from_date)s")
        params["from_date"] = from_date
    if to_date is not None:
        clauses.append("date <= %(to_date)s")
        params["to_date"] = to_date
    where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    return sql_to_df(f"SELECT * FROM {table_name} {where_sql} ORDER BY date", params=params or None)


def build_exchange_events(
    *,
    from_date: pd.Timestamp | None = None,
    to_date: pd.Timestamp | None = None,
) -> pd.DataFrame:
    corporate_action_frames = [
        normalize_corporate_actions(_load_table("nseindia_corporate_actions_normalized", from_date=from_date, to_date=to_date)),
        normalize_corporate_actions(_load_table("nseindia_corporate_actions", from_date=from_date, to_date=to_date)),
        normalize_corporate_actions(_load_table("nseindia_corporate_actions_bc_raw", from_date=from_date, to_date=to_date)),
    ]
    frames = [
        normalize_block_or_bulk(_load_table("nseindia_block_deals", from_date=from_date, to_date=to_date), source="nse_block_deal"),
        normalize_block_or_bulk(_load_table("nseindia_bulk_deals", from_date=from_date, to_date=to_date), source="nse_bulk_deal"),
        normalize_short_selling(_load_table("nseindia_short_selling", from_date=from_date, to_date=to_date)),
        normalize_insider_deals(_load_table("nseindia_insider_deals", from_date=from_date, to_date=to_date)),
        *corporate_action_frames,
        normalize_earnings_events(_load_table("nseindia_earnings_events", from_date=from_date, to_date=to_date)),
        normalize_recent_events(_load_table("nseindia_events", from_date=from_date, to_date=to_date)),
    ]
    frames = [frame for frame in frames if not frame.empty]
    if not frames:
        return pd.DataFrame()
    normalized_frames = [frame.reindex(columns=EVENT_COLUMNS) for frame in frames]
    return pd.concat(normalized_frames, ignore_index=True, sort=False).drop_duplicates(subset=["event_id", "known_on"], keep="last")


def persist_exchange_events(df: pd.DataFrame) -> None:
    if df.empty:
        return
    upsert_to_db(df, TABLE_NAME, unique_keys=["event_id", "known_on"], timescaledb_column="known_on")


def repair_missing_event_types(*, dry_run: bool = True) -> dict[str, object]:
    if not require_table_exists(TABLE_NAME):
        return {"status": "missing_table", "table": TABLE_NAME, "dry_run": bool(dry_run), "matched_rows": 0, "updated_rows": 0}
    preview_deals = sql_to_df(
        f"""
        SELECT COUNT(*) AS matched_rows
        FROM {TABLE_NAME}
        WHERE (event_source IS NULL OR event_type IS NULL)
          AND side IN ('BUY', 'SELL')
          AND participant IS NOT NULL
          AND quantity IS NOT NULL
          AND price IS NOT NULL
        """,
        retries=2,
        statement_timeout_ms=15000,
    )
    preview_shorts = sql_to_df(
        f"""
        SELECT COUNT(*) AS matched_rows
        FROM {TABLE_NAME}
        WHERE (event_source IS NULL OR event_type IS NULL)
          AND event_summary LIKE 'Short selling quantity%%'
          AND quantity IS NOT NULL
        """,
        retries=2,
        statement_timeout_ms=15000,
    )
    preview_unclassified = sql_to_df(
        f"""
        SELECT COUNT(*) AS matched_rows
        FROM {TABLE_NAME}
        WHERE event_source IS NULL OR event_type IS NULL
        """,
        retries=2,
        statement_timeout_ms=15000,
    )
    deal_rows = 0 if preview_deals.empty else int(preview_deals.iloc[0].get("matched_rows") or 0)
    short_rows = 0 if preview_shorts.empty else int(preview_shorts.iloc[0].get("matched_rows") or 0)
    raw_unclassified_rows = 0 if preview_unclassified.empty else int(preview_unclassified.iloc[0].get("matched_rows") or 0)
    unclassified_rows = max(0, raw_unclassified_rows - deal_rows - short_rows)
    matched_rows = deal_rows + short_rows + unclassified_rows
    if dry_run or matched_rows == 0:
        return {
            "status": "dry_run" if dry_run else "ok",
            "table": TABLE_NAME,
            "dry_run": bool(dry_run),
            "matched_rows": matched_rows,
            "deal_rows": deal_rows,
            "short_selling_rows": short_rows,
            "unclassified_rows": unclassified_rows,
            "updated_rows": 0,
            "repair_source": "nse_legacy_deal",
            "repair_type": "LEGACY_DEAL",
        }
    with db_session() as (_, cur):
        cur.execute(
            f"""
            UPDATE {TABLE_NAME}
            SET
                event_source = COALESCE(event_source, 'nse_legacy_deal'),
                event_type = COALESCE(event_type, 'LEGACY_DEAL'),
                event_summary = CASE
                    WHEN event_summary IS NULL OR event_summary LIKE 'nan %%'
                    THEN CONCAT('Legacy NSE deal ', side, ' by ', participant)
                    ELSE event_summary
                END
            WHERE (event_source IS NULL OR event_type IS NULL)
              AND side IN ('BUY', 'SELL')
              AND participant IS NOT NULL
              AND quantity IS NOT NULL
              AND price IS NOT NULL
            """
        )
        updated_deals = int(cur.rowcount or 0)
        cur.execute(
            f"""
            UPDATE {TABLE_NAME}
            SET
                event_source = COALESCE(event_source, 'nse_short_selling'),
                event_type = COALESCE(event_type, 'SHORT_SELLING')
            WHERE (event_source IS NULL OR event_type IS NULL)
              AND event_summary LIKE 'Short selling quantity%%'
              AND quantity IS NOT NULL
            """
        )
        updated_shorts = int(cur.rowcount or 0)
        cur.execute(
            f"""
            UPDATE {TABLE_NAME}
            SET
                event_source = COALESCE(event_source, 'nse_unclassified_event'),
                event_type = COALESCE(event_type, 'UNCLASSIFIED_EVENT'),
                event_summary = COALESCE(event_summary, 'Unclassified legacy NSE event')
            WHERE event_source IS NULL OR event_type IS NULL
            """
        )
        updated_unclassified = int(cur.rowcount or 0)
        updated_rows = updated_deals + updated_shorts + updated_unclassified
    return {
        "status": "applied",
        "table": TABLE_NAME,
        "dry_run": False,
        "matched_rows": matched_rows,
        "deal_rows": deal_rows,
        "short_selling_rows": short_rows,
        "unclassified_rows": unclassified_rows,
        "updated_rows": updated_rows,
        "updated_deal_rows": updated_deals,
        "updated_short_selling_rows": updated_shorts,
        "updated_unclassified_rows": updated_unclassified,
        "repair_source": "nse_legacy_deal",
        "repair_type": "LEGACY_DEAL",
    }


def summarize(df: pd.DataFrame) -> dict[str, object]:
    if df.empty:
        return {"status": "ok", "table": TABLE_NAME, "row_count": 0, "event_counts": {}, "sample": []}
    return {
        "status": "ok",
        "table": TABLE_NAME,
        "row_count": int(len(df)),
        "event_counts": {str(k): int(v) for k, v in df["event_source"].value_counts().to_dict().items()},
        "date_min": df["known_on"].min().date().isoformat(),
        "date_max": df["known_on"].max().date().isoformat(),
        "sample": df[["known_on", "symbol", "event_source", "event_type", "event_summary"]].tail(5).to_dict(orient="records"),
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Normalize NSE deal and exchange-event rows for advisory context.")
    parser.add_argument("--from-date", type=parse_datetime_arg)
    parser.add_argument("--to-date", type=parse_datetime_arg)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--repair-missing-types", action="store_true", help="Repair legacy rows with null event_source/event_type.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.repair_missing_types:
        result = repair_missing_event_types(dry_run=bool(args.dry_run))
        print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
        return 0
    df = build_exchange_events(
        from_date=pd.Timestamp(args.from_date, tz="UTC") if args.from_date else None,
        to_date=pd.Timestamp(args.to_date, tz="UTC") if args.to_date else None,
    )
    if not args.dry_run:
        persist_exchange_events(df)
    result = summarize(df)
    result["dry_run"] = bool(args.dry_run)
    print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
