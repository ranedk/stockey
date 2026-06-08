from __future__ import annotations

import argparse
import hashlib
import json
from typing import Any

import pandas as pd

from utils.db import sql_to_df, upsert_to_db
from utils.sync import parse_datetime_arg


BHAVCOPY_EVIDENCE_TABLE = "advisory_bhavcopy_evidence_daily"
ANNOUNCEMENT_EVIDENCE_TABLE = "advisory_announcement_evidence"
DEFAULT_LOOKBACK_DAYS = 365


def _sql_num(expression: str) -> str:
    return f"NULLIF(regexp_replace(({expression})::text, '[^0-9.\\-]', '', 'g'), '')::double precision"


def _json_ready(value: Any) -> Any:
    if isinstance(value, pd.Timestamp):
        return None if pd.isna(value) else value.isoformat()
    try:
        if pd.isna(value):
            return None
    except Exception:
        pass
    return value


def _records(df: pd.DataFrame, *, limit: int = 5) -> list[dict[str, Any]]:
    if df.empty:
        return []
    clean = df.tail(max(0, int(limit))).copy().astype(object).where(pd.notna(df.tail(max(0, int(limit)))), None)
    return [{key: _json_ready(value) for key, value in row.items()} for row in clean.to_dict(orient="records")]


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
            retries=2,
            statement_timeout_ms=5000,
        )
    except Exception:
        return False
    return not df.empty


def table_columns(table_name: str) -> set[str]:
    try:
        df = sql_to_df(
            """
            SELECT column_name
            FROM information_schema.columns
            WHERE table_schema = 'public'
              AND table_name = %s
            """,
            params=(table_name,),
            retries=2,
            statement_timeout_ms=5000,
        )
    except Exception:
        return set()
    return set(df["column_name"].astype(str).tolist()) if not df.empty else set()


def _max_date(table_name: str, column: str) -> pd.Timestamp | None:
    if not table_exists(table_name):
        return None
    try:
        df = sql_to_df(
            f'SELECT MAX("{column}") AS max_date FROM "{table_name}"',
            retries=2,
            statement_timeout_ms=5000,
        )
    except Exception:
        return None
    if df.empty:
        return None
    ts = pd.to_datetime(df.iloc[0].get("max_date"), utc=True, errors="coerce")
    return None if pd.isna(ts) else ts.normalize()


def _normalize_date(value: Any) -> pd.Timestamp | None:
    if value is None:
        return None
    ts = pd.to_datetime(value, utc=True, errors="coerce")
    if pd.isna(ts):
        return None
    return ts.normalize()


def resolve_date_range(
    *,
    table_name: str,
    date_column: str,
    from_date: Any = None,
    to_date: Any = None,
    rebuild: bool = False,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
) -> tuple[pd.Timestamp, pd.Timestamp]:
    effective_to = _normalize_date(to_date) or pd.Timestamp.utcnow().normalize()
    effective_from = _normalize_date(from_date)
    if effective_from is None and not rebuild:
        max_date = _max_date(table_name, date_column)
        if max_date is not None:
            effective_from = max_date + pd.Timedelta(days=1)
    if effective_from is None:
        effective_from = effective_to - pd.Timedelta(days=max(1, int(lookback_days)))
    return effective_from, effective_to


def _classify_deal_pressure(row: pd.Series) -> str:
    net_deal = float(row.get("deal_net_value_inr") or 0.0)
    short_qty = float(row.get("short_selling_quantity") or 0.0)
    circuit = int(row.get("circuit_hit_count") or 0)
    if circuit > 0:
        return "circuit_risk"
    if net_deal > 10_000_000 and short_qty <= 0:
        return "accumulation"
    if net_deal < -10_000_000 or short_qty > 0:
        return "distribution_or_pressure"
    return "neutral"


def _score_bhavcopy_row(row: pd.Series) -> float:
    score = 0.0
    turnover = float(row.get("turnover_value_inr") or 0.0)
    avg_turnover = float(row.get("avg_turnover_value_20d") or 0.0)
    net_deal = float(row.get("deal_net_value_inr") or 0.0)
    short_qty = float(row.get("short_selling_quantity") or 0.0)
    circuit = int(row.get("circuit_hit_count") or 0)
    if avg_turnover > 0 and turnover > 1.5 * avg_turnover:
        score += 0.15
    if net_deal > 0:
        score += min(0.35, net_deal / 100_000_000.0 * 0.05)
    if net_deal < 0:
        score -= min(0.35, abs(net_deal) / 100_000_000.0 * 0.05)
    if short_qty > 0:
        score -= min(0.25, short_qty / 1_000_000.0 * 0.03)
    if circuit > 0:
        score -= 0.20
    return round(max(-1.0, min(1.0, score)), 6)


def build_bhavcopy_evidence(
    *,
    from_date: Any = None,
    to_date: Any = None,
    rebuild: bool = False,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
) -> pd.DataFrame:
    start_date, end_date = resolve_date_range(
        table_name=BHAVCOPY_EVIDENCE_TABLE,
        date_column="asof_date",
        from_date=from_date,
        to_date=to_date,
        rebuild=rebuild,
        lookback_days=lookback_days,
    )
    if start_date > end_date:
        return pd.DataFrame()
    warmup_start = start_date - pd.Timedelta(days=30)
    print(f"[advisory.event_evidence_store] bhavcopy start from={start_date.date()} to={end_date.date()}", flush=True)
    query = f"""
        WITH base AS (
            SELECT
                date::date AS asof_date,
                upper(symbol::text) AS symbol,
                max(company_master_id::text) AS company_master_id,
                max({_sql_num("close")}) AS close,
                sum({_sql_num("volume")}) AS volume,
                sum({_sql_num("total_value")}) AS turnover_value_inr,
                sum({_sql_num("number_of_trades")}) AS number_of_trades,
                max({_sql_num("previous_close")}) AS previous_close
            FROM nseindia_ohlcv
            WHERE date BETWEEN %(warmup_start)s AND %(end_date)s
              AND upper(coalesce(series::text, 'EQ')) = 'EQ'
            GROUP BY date::date, upper(symbol::text)
        ),
        rolling AS (
            SELECT
                *,
                avg(turnover_value_inr) OVER (PARTITION BY symbol ORDER BY asof_date ROWS BETWEEN 19 PRECEDING AND CURRENT ROW) AS avg_turnover_value_20d,
                avg(volume) OVER (PARTITION BY symbol ORDER BY asof_date ROWS BETWEEN 19 PRECEDING AND CURRENT ROW) AS avg_volume_20d,
                stddev_pop(close) OVER (PARTITION BY symbol ORDER BY asof_date ROWS BETWEEN 19 PRECEDING AND CURRENT ROW) AS close_volatility_20d
            FROM base
        ),
        cmvolt AS (
            SELECT
                date::date AS asof_date,
                upper(symbol::text) AS symbol,
                max({_sql_num("annualized_volatility")}) AS annualized_volatility,
                max({_sql_num("current_day_daily_volatility")}) AS current_day_daily_volatility
            FROM nseindia_cmvolt
            WHERE date BETWEEN %(start_date)s AND %(end_date)s
            GROUP BY date::date, upper(symbol::text)
        ),
        var_margin AS (
            SELECT
                for_date::date AS asof_date,
                upper(symbol::text) AS symbol,
                max({_sql_num("applicable_margin")}) AS applicable_margin,
                max({_sql_num("var_margin")}) AS var_margin,
                max({_sql_num("extreme_loss_rate")}) AS extreme_loss_rate
            FROM nseindia_var1
            WHERE for_date BETWEEN %(start_date)s AND %(end_date)s
            GROUP BY for_date::date, upper(symbol::text)
        ),
        deals AS (
            SELECT
                date::date AS asof_date,
                upper(symbol::text) AS symbol,
                sum(CASE WHEN upper(coalesce(buysell::text, '')) LIKE 'BUY%%' THEN {_sql_num("quantity")} * {_sql_num("price")} ELSE 0 END) AS block_buy_value_inr,
                sum(CASE WHEN upper(coalesce(buysell::text, '')) LIKE 'SELL%%' THEN {_sql_num("quantity")} * {_sql_num("price")} ELSE 0 END) AS block_sell_value_inr,
                count(*) AS block_deal_count,
                0::double precision AS bulk_buy_value_inr,
                0::double precision AS bulk_sell_value_inr,
                0::bigint AS bulk_deal_count
            FROM nseindia_block_deals
            WHERE date BETWEEN %(start_date)s AND %(end_date)s
            GROUP BY date::date, upper(symbol::text)
            UNION ALL
            SELECT
                date::date AS asof_date,
                upper(symbol::text) AS symbol,
                0::double precision AS block_buy_value_inr,
                0::double precision AS block_sell_value_inr,
                0::bigint AS block_deal_count,
                sum(CASE WHEN upper(coalesce(buysell::text, '')) LIKE 'BUY%%' THEN {_sql_num("quantity")} * {_sql_num("price")} ELSE 0 END) AS bulk_buy_value_inr,
                sum(CASE WHEN upper(coalesce(buysell::text, '')) LIKE 'SELL%%' THEN {_sql_num("quantity")} * {_sql_num("price")} ELSE 0 END) AS bulk_sell_value_inr,
                count(*) AS bulk_deal_count
            FROM nseindia_bulk_deals
            WHERE date BETWEEN %(start_date)s AND %(end_date)s
            GROUP BY date::date, upper(symbol::text)
        ),
        deal_agg AS (
            SELECT
                asof_date,
                symbol,
                sum(block_buy_value_inr) AS block_buy_value_inr,
                sum(block_sell_value_inr) AS block_sell_value_inr,
                sum(block_deal_count) AS block_deal_count,
                sum(bulk_buy_value_inr) AS bulk_buy_value_inr,
                sum(bulk_sell_value_inr) AS bulk_sell_value_inr,
                sum(bulk_deal_count) AS bulk_deal_count
            FROM deals
            GROUP BY asof_date, symbol
        ),
        shorts AS (
            SELECT
                date::date AS asof_date,
                upper(symbol::text) AS symbol,
                sum({_sql_num("quantity")}) AS short_selling_quantity,
                count(*) AS short_selling_count
            FROM nseindia_short_selling
            WHERE date BETWEEN %(start_date)s AND %(end_date)s
            GROUP BY date::date, upper(symbol::text)
        ),
        circuits AS (
            SELECT
                date::date AS asof_date,
                upper(symbol::text) AS symbol,
                count(*) AS circuit_hit_count,
                string_agg(DISTINCT circuit_hit::text, ', ' ORDER BY circuit_hit::text) AS circuit_hit_types
            FROM nseindia_circuit_hit
            WHERE date BETWEEN %(start_date)s AND %(end_date)s
            GROUP BY date::date, upper(symbol::text)
        )
        SELECT
            r.asof_date,
            r.symbol,
            r.company_master_id,
            r.close,
            r.previous_close,
            CASE WHEN r.previous_close IS NULL OR r.previous_close = 0 THEN NULL ELSE (r.close / r.previous_close) - 1 END AS daily_return,
            r.volume,
            r.avg_volume_20d,
            r.turnover_value_inr,
            r.avg_turnover_value_20d,
            r.number_of_trades,
            r.close_volatility_20d,
            c.annualized_volatility,
            c.current_day_daily_volatility,
            v.applicable_margin,
            v.var_margin,
            v.extreme_loss_rate,
            coalesce(d.block_buy_value_inr, 0) AS block_buy_value_inr,
            coalesce(d.block_sell_value_inr, 0) AS block_sell_value_inr,
            coalesce(d.block_deal_count, 0) AS block_deal_count,
            coalesce(d.bulk_buy_value_inr, 0) AS bulk_buy_value_inr,
            coalesce(d.bulk_sell_value_inr, 0) AS bulk_sell_value_inr,
            coalesce(d.bulk_deal_count, 0) AS bulk_deal_count,
            coalesce(d.block_buy_value_inr, 0) + coalesce(d.bulk_buy_value_inr, 0)
              - coalesce(d.block_sell_value_inr, 0) - coalesce(d.bulk_sell_value_inr, 0) AS deal_net_value_inr,
            coalesce(s.short_selling_quantity, 0) AS short_selling_quantity,
            coalesce(s.short_selling_count, 0) AS short_selling_count,
            coalesce(ci.circuit_hit_count, 0) AS circuit_hit_count,
            ci.circuit_hit_types
        FROM rolling r
        LEFT JOIN cmvolt c ON c.asof_date = r.asof_date AND c.symbol = r.symbol
        LEFT JOIN var_margin v ON v.asof_date = r.asof_date AND v.symbol = r.symbol
        LEFT JOIN deal_agg d ON d.asof_date = r.asof_date AND d.symbol = r.symbol
        LEFT JOIN shorts s ON s.asof_date = r.asof_date AND s.symbol = r.symbol
        LEFT JOIN circuits ci ON ci.asof_date = r.asof_date AND ci.symbol = r.symbol
        WHERE r.asof_date BETWEEN %(start_date)s AND %(end_date)s
        ORDER BY r.asof_date, r.symbol
    """
    df = sql_to_df(
        query,
        params={"warmup_start": warmup_start.date(), "start_date": start_date.date(), "end_date": end_date.date()},
        retries=4,
        statement_timeout_ms=0,
        chunksize=50000,
    )
    if df.empty:
        return df
    numeric_cols = [col for col in df.columns if col not in {"asof_date", "symbol", "company_master_id", "circuit_hit_types"}]
    for col in numeric_cols:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df["asof_date"] = pd.to_datetime(df["asof_date"], utc=True, errors="coerce").dt.normalize()
    df["symbol"] = df["symbol"].astype("string").str.upper()
    df["deal_pressure"] = df.apply(_classify_deal_pressure, axis=1)
    df["evidence_score"] = df.apply(_score_bhavcopy_row, axis=1)
    df["evidence_summary"] = df.apply(
        lambda row: (
            f"turnover={float(row.get('turnover_value_inr') or 0):.0f}; "
            f"deal_net={float(row.get('deal_net_value_inr') or 0):.0f}; "
            f"short_qty={float(row.get('short_selling_quantity') or 0):.0f}; "
            f"circuit_hits={int(row.get('circuit_hit_count') or 0)}; "
            f"pressure={row.get('deal_pressure')}"
        ),
        axis=1,
    )
    df["load_ts"] = pd.Timestamp.utcnow()
    return df.drop_duplicates(subset=["asof_date", "symbol"], keep="last")


def _evidence_id(unique_id: Any, published_on: Any) -> str:
    payload = f"{unique_id}|{published_on}"
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()


def build_announcement_evidence(
    *,
    from_date: Any = None,
    to_date: Any = None,
    rebuild: bool = False,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
) -> pd.DataFrame:
    start_date, end_date = resolve_date_range(
        table_name=ANNOUNCEMENT_EVIDENCE_TABLE,
        date_column="published_on",
        from_date=from_date,
        to_date=to_date,
        rebuild=rebuild,
        lookback_days=lookback_days,
    )
    if start_date > end_date:
        return pd.DataFrame()
    print(f"[advisory.event_evidence_store] announcements start from={start_date.date()} to={end_date.date()}", flush=True)
    has_evaluations = table_exists("advisory_event_evaluations")
    eval_join = ""
    eval_columns = """
        NULL::text AS event_source,
        NULL::text AS event_class,
        NULL::text AS direction,
        NULL::double precision AS materiality,
        NULL::double precision AS surprise,
        NULL::double precision AS novelty,
        NULL::double precision AS contradiction,
        NULL::double precision AS confidence,
        NULL::text AS verdict,
        NULL::text AS what_happened,
        NULL::text AS rationale,
        NULL::text AS event_tensor_json,
        NULL::text AS prompt_version
    """
    if has_evaluations:
        eval_join = """
            LEFT JOIN (
                SELECT DISTINCT ON (unique_id)
                    unique_id,
                    event_source,
                    event_class,
                    direction,
                    materiality,
                    surprise,
                    novelty,
                    contradiction,
                    confidence,
                    verdict,
                    what_happened,
                    rationale,
                    event_tensor_json,
                    prompt_version,
                    evaluated_at
                FROM advisory_event_evaluations
                WHERE published_on BETWEEN %(start_date)s AND (%(end_date)s::date + interval '1 day')
                ORDER BY unique_id, evaluated_at DESC NULLS LAST, load_ts DESC NULLS LAST
            ) e ON e.unique_id = d.unique_id
        """
        eval_columns = """
            e.event_source,
            e.event_class,
            e.direction,
            e.materiality,
            e.surprise,
            e.novelty,
            e.contradiction,
            e.confidence,
            e.verdict,
            e.what_happened,
            e.rationale,
            e.event_tensor_json,
            e.prompt_version
        """
    query = f"""
        SELECT
            d.unique_id,
            upper(d.ticker::text) AS symbol,
            d.company_master_id,
            d.exchange,
            d.company_name,
            d.subject,
            d.filed_under_category,
            d.published_on,
            d.exchange_published_on,
            d.attachment_url,
            d.attachment_name,
            d.parse_status,
            d.ocr_status,
            d.pdf_status,
            d.last_error,
            d.raw_s3_key,
            d.pdf_s3_key,
            d.ocr_s3_key,
            d.full_ocr_s3_key,
            d.audio_transcript_s3_key,
            d.concise_summary_s3_key,
            d.number_of_pages,
            coalesce({_sql_num("d.concise_summary_chars")}, {_sql_num("d.full_ocr_chars")}, {_sql_num("d.ocr_chars")}, 0) AS evidence_chars,
            left(coalesce(d.concise_summary_text, d.concise_summary_excerpt, d.full_ocr_excerpt, d.ocr_excerpt, d.text, ''), 2000) AS evidence_excerpt,
            {eval_columns}
        FROM announcement_pipeline_documents d
        {eval_join}
        WHERE d.published_on BETWEEN %(start_date)s AND (%(end_date)s::date + interval '1 day')
        ORDER BY d.published_on, d.ticker, d.unique_id
    """
    df = sql_to_df(
        query,
        params={"start_date": start_date.date(), "end_date": end_date.date()},
        retries=4,
        statement_timeout_ms=0,
        chunksize=20000,
    )
    if df.empty:
        return df
    df["published_on"] = pd.to_datetime(df["published_on"], utc=True, errors="coerce")
    df["asof_date"] = df["published_on"].dt.normalize()
    df["symbol"] = df["symbol"].astype("string").str.upper()
    for col in ["materiality", "surprise", "novelty", "contradiction", "confidence", "evidence_chars"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    df["has_text_evidence"] = df["evidence_excerpt"].astype("string").str.strip().ne("")
    df["has_s3_evidence"] = df[["raw_s3_key", "pdf_s3_key", "ocr_s3_key", "full_ocr_s3_key", "audio_transcript_s3_key", "concise_summary_s3_key"]].notna().any(axis=1)
    df["evidence_id"] = [_evidence_id(row.get("unique_id"), row.get("published_on")) for row in df.to_dict(orient="records")]
    df["evidence_summary"] = df.apply(
        lambda row: str(row.get("what_happened") or row.get("evidence_excerpt") or row.get("subject") or "")[:500],
        axis=1,
    )
    df["source_reliability"] = df.apply(
        lambda row: "high" if str(row.get("parse_status") or "").lower() in {"completed", "parsed", "success", "ok"} or bool(row.get("has_s3_evidence")) else "low",
        axis=1,
    )
    df["load_ts"] = pd.Timestamp.utcnow()
    return df.drop_duplicates(subset=["evidence_id"], keep="last")


def persist_bhavcopy_evidence(df: pd.DataFrame) -> None:
    if df.empty:
        return
    upsert_to_db(df, BHAVCOPY_EVIDENCE_TABLE, unique_keys=["asof_date", "symbol"], timescaledb_column="asof_date")


def persist_announcement_evidence(df: pd.DataFrame) -> None:
    if df.empty:
        return
    upsert_to_db(df, ANNOUNCEMENT_EVIDENCE_TABLE, unique_keys=["published_on", "evidence_id"], timescaledb_column="published_on")


def build_event_evidence_store(
    *,
    from_date: Any = None,
    to_date: Any = None,
    rebuild: bool = False,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
    include_bhavcopy: bool = True,
    include_announcements: bool = True,
    dry_run: bool = False,
) -> dict[str, Any]:
    bhavcopy = pd.DataFrame()
    announcements = pd.DataFrame()
    if include_bhavcopy:
        bhavcopy = build_bhavcopy_evidence(from_date=from_date, to_date=to_date, rebuild=rebuild, lookback_days=lookback_days)
        if not dry_run:
            persist_bhavcopy_evidence(bhavcopy)
    if include_announcements:
        announcements = build_announcement_evidence(from_date=from_date, to_date=to_date, rebuild=rebuild, lookback_days=lookback_days)
        if not dry_run:
            persist_announcement_evidence(announcements)
    return {
        "status": "ok",
        "dry_run": bool(dry_run),
        "tables": {
            "bhavcopy": BHAVCOPY_EVIDENCE_TABLE,
            "announcements": ANNOUNCEMENT_EVIDENCE_TABLE,
        },
        "bhavcopy": {
            "rows": int(len(bhavcopy)),
            "symbols": 0 if bhavcopy.empty else int(bhavcopy["symbol"].nunique()),
            "date_min": None if bhavcopy.empty else _json_ready(bhavcopy["asof_date"].min()),
            "date_max": None if bhavcopy.empty else _json_ready(bhavcopy["asof_date"].max()),
            "sample": _records(bhavcopy),
        },
        "announcements": {
            "rows": int(len(announcements)),
            "symbols": 0 if announcements.empty else int(announcements["symbol"].nunique()),
            "date_min": None if announcements.empty else _json_ready(announcements["published_on"].min()),
            "date_max": None if announcements.empty else _json_ready(announcements["published_on"].max()),
            "sample": _records(announcements),
        },
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build compact event evidence tables from bhavcopy and announcement sources.")
    parser.add_argument("--from-date", type=parse_datetime_arg)
    parser.add_argument("--to-date", type=parse_datetime_arg)
    parser.add_argument("--rebuild", action="store_true")
    parser.add_argument("--lookback-days", type=int, default=DEFAULT_LOOKBACK_DAYS)
    parser.add_argument("--skip-bhavcopy", action="store_true")
    parser.add_argument("--skip-announcements", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    payload = build_event_evidence_store(
        from_date=args.from_date,
        to_date=args.to_date,
        rebuild=bool(args.rebuild),
        lookback_days=max(1, int(args.lookback_days)),
        include_bhavcopy=not bool(args.skip_bhavcopy),
        include_announcements=not bool(args.skip_announcements),
        dry_run=bool(args.dry_run),
    )
    print(json.dumps(payload, indent=2, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
