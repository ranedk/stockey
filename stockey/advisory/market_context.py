from __future__ import annotations

import argparse
import json
import math
from typing import Any

import numpy as np
import pandas as pd

from utils.db import sql_to_df, upsert_to_db
from utils.sync import parse_datetime_arg


UNIVERSE_TABLE = "advisory_market_context_universe_daily"
SUMMARY_TABLE = "advisory_market_context_summary_daily"
DEFAULT_TOP_FRACTION = 0.50
DEFAULT_EVENT_LOOKBACK_DAYS = 20
DEFAULT_NEWS_LOOKBACK_DAYS = 7
DEFAULT_MIN_AVG_TRADED_VALUE_20D = 1_00_00_000.0
DEFAULT_MIN_PRICE = 20.0


def normalize_timestamp(series: pd.Series) -> pd.Series:
    return pd.to_datetime(series, utc=True, errors="coerce").dt.normalize()


def _asof(value: pd.Timestamp | None) -> pd.Timestamp:
    ts = pd.Timestamp.utcnow() if value is None else pd.Timestamp(value)
    if ts.tzinfo is None:
        ts = ts.tz_localize("UTC")
    return ts.tz_convert("UTC").normalize()


def _table_exists(table_name: str) -> bool:
    df = sql_to_df(
        """
        SELECT EXISTS (
            SELECT 1
            FROM information_schema.tables
            WHERE table_schema = 'public'
              AND table_name = %s
        ) AS exists_flag
        """,
        params=(table_name,),
    )
    return bool(not df.empty and df.iloc[0]["exists_flag"])


def _json_text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        num = float(value)
    except Exception:
        return default
    if not math.isfinite(num):
        return default
    return num


def _bool_series(df: pd.DataFrame, col: str, default: bool = False) -> pd.Series:
    if col not in df.columns:
        return pd.Series(default, index=df.index, dtype="bool")
    return df[col].astype("boolean").fillna(default).astype(bool)


def _numeric_series(df: pd.DataFrame, col: str, default: float = 0.0) -> pd.Series:
    if col not in df.columns:
        return pd.Series(default, index=df.index, dtype="float64")
    return pd.to_numeric(df[col], errors="coerce").fillna(default)


def _clean_records(df: pd.DataFrame) -> list[dict[str, Any]]:
    if df.empty:
        return []
    clean = df.copy()
    clean = clean.astype(object).where(pd.notna(clean), None)
    return clean.to_dict(orient="records")


def load_latest_technical(asof_date: pd.Timestamp) -> pd.DataFrame:
    if not _table_exists("advisory_technical_daily"):
        return pd.DataFrame()
    df = sql_to_df(
        """
        WITH ranked AS (
            SELECT
                asof_date,
                company_master_id,
                symbol,
                series,
                sector_code,
                sector_name,
                adj_close,
                total_value,
                avg_traded_value_20d,
                avg_traded_value_60d,
                rs_vs_benchmark,
                rs_vs_sector,
                dist_52w_high,
                dma_50_slope_20d_pct,
                trend_persistence_60d,
                accumulation_days_20d,
                distribution_days_20d,
                pass_liquidity_20d,
                pass_above_dma_50,
                pass_trend_alignment,
                pass_near_52w_high,
                ROW_NUMBER() OVER (PARTITION BY UPPER(TRIM(symbol)) ORDER BY asof_date DESC, load_ts DESC) AS rn
            FROM advisory_technical_daily
            WHERE asof_date <= %(asof_date)s
              AND NULLIF(TRIM(symbol), '') IS NOT NULL
              AND COALESCE(series, 'EQ') = 'EQ'
        )
        SELECT *
        FROM ranked
        WHERE rn = 1
        """,
        params={"asof_date": asof_date},
        retries=4,
        statement_timeout_ms=0,
        chunksize=50000,
    )
    if df.empty:
        return df
    df["asof_date"] = normalize_timestamp(df["asof_date"])
    df["symbol"] = df["symbol"].astype("string").str.strip().str.upper()
    for col in [
        "adj_close",
        "total_value",
        "avg_traded_value_20d",
        "avg_traded_value_60d",
        "rs_vs_benchmark",
        "rs_vs_sector",
        "dist_52w_high",
        "dma_50_slope_20d_pct",
        "trend_persistence_60d",
        "accumulation_days_20d",
        "distribution_days_20d",
    ]:
        df[col] = pd.to_numeric(df.get(col), errors="coerce")
    for col in ["pass_liquidity_20d", "pass_above_dma_50", "pass_trend_alignment", "pass_near_52w_high"]:
        df[col] = _bool_series(df, col)
    return df.drop(columns=["rn"], errors="ignore")


def load_latest_market_cap(asof_date: pd.Timestamp) -> pd.DataFrame:
    if not _table_exists("advisory_screener_constituents"):
        return pd.DataFrame(columns=["symbol", "market_cap", "market_cap_source_date"])
    df = sql_to_df(
        """
        WITH ranked AS (
            SELECT
                UPPER(TRIM(ticker)) AS symbol,
                market_cap,
                date AS market_cap_source_date,
                ROW_NUMBER() OVER (PARTITION BY UPPER(TRIM(ticker)) ORDER BY date DESC, load_ts DESC) AS rn
            FROM advisory_screener_constituents
            WHERE date <= %(asof_date)s
              AND NULLIF(TRIM(ticker), '') IS NOT NULL
              AND market_cap IS NOT NULL
        )
        SELECT symbol, market_cap, market_cap_source_date
        FROM ranked
        WHERE rn = 1
        """,
        params={"asof_date": asof_date},
        retries=4,
    )
    if df.empty:
        return pd.DataFrame(columns=["symbol", "market_cap", "market_cap_source_date"])
    df["symbol"] = df["symbol"].astype("string").str.strip().str.upper()
    df["market_cap"] = pd.to_numeric(df["market_cap"], errors="coerce")
    df["market_cap_source_date"] = normalize_timestamp(df["market_cap_source_date"])
    return df.drop_duplicates(subset=["symbol"], keep="last")


def load_exchange_context(asof_date: pd.Timestamp, symbols: list[str]) -> pd.DataFrame:
    if not symbols or not _table_exists("advisory_exchange_features_daily"):
        return pd.DataFrame(columns=["symbol"])
    df = sql_to_df(
        """
        WITH ranked AS (
            SELECT
                *,
                ROW_NUMBER() OVER (PARTITION BY UPPER(TRIM(symbol)) ORDER BY asof_date DESC, load_ts DESC) AS rn
            FROM advisory_exchange_features_daily
            WHERE asof_date <= %(asof_date)s
              AND UPPER(TRIM(symbol)) = ANY(%(symbols)s)
        )
        SELECT
            symbol,
            latest_exchange_event_date,
            latest_exchange_event_source,
            latest_exchange_event_type,
            deal_cluster_count_20d,
            insider_net_value_90d,
            short_selling_event_count_20d,
            upcoming_earnings_14d,
            exchange_accumulation_score,
            exchange_distribution_score,
            exchange_event_score
        FROM ranked
        WHERE rn = 1
        """,
        params={"asof_date": asof_date, "symbols": symbols},
        retries=4,
        statement_timeout_ms=0,
        chunksize=50000,
    )
    if df.empty:
        return pd.DataFrame(columns=["symbol"])
    df["symbol"] = df["symbol"].astype("string").str.strip().str.upper()
    numeric_cols = [
        "deal_cluster_count_20d",
        "insider_net_value_90d",
        "short_selling_event_count_20d",
        "exchange_accumulation_score",
        "exchange_distribution_score",
        "exchange_event_score",
    ]
    for col in numeric_cols:
        df[col] = pd.to_numeric(df.get(col), errors="coerce")
    df["upcoming_earnings_14d"] = df.get("upcoming_earnings_14d").fillna(False).astype(bool)
    return df.drop_duplicates(subset=["symbol"], keep="last")


def load_regime_context(asof_date: pd.Timestamp) -> dict[str, Any]:
    if not _table_exists("advisory_market_regime"):
        return {}
    df = sql_to_df(
        """
        SELECT
            asof_date,
            regime_name,
            macro_risk_state,
            macro_stress_score,
            macro_sizing_multiplier,
            risk_off_flag,
            shock_flag,
            regime_notes
        FROM advisory_market_regime
        WHERE asof_date <= %(asof_date)s
        ORDER BY asof_date DESC
        LIMIT 1
        """,
        params={"asof_date": asof_date},
        retries=4,
    )
    return df.iloc[0].to_dict() if not df.empty else {}


def _count_events(
    *,
    table_name: str,
    date_col: str,
    asof_date: pd.Timestamp,
    symbols: list[str],
    lookback_days: int,
    extra_cols: str = "",
) -> pd.DataFrame:
    if not symbols or not _table_exists(table_name):
        return pd.DataFrame(columns=["symbol"])
    start_date = asof_date - pd.Timedelta(days=int(lookback_days))
    extra_parts = [
        line.strip().lstrip(",").strip()
        for line in extra_cols.splitlines()
        if line.strip().lstrip(",").strip()
    ]
    extra_select = ""
    if extra_parts:
        extra_select = "\n            , " + "\n            , ".join(extra_parts)
    df = sql_to_df(
        f"""
        SELECT
            UPPER(TRIM(symbol)) AS symbol,
            COUNT(*) AS event_count
            {extra_select}
        FROM {table_name}
        WHERE {date_col} >= %(start_date)s
          AND {date_col} <= %(asof_date)s
          AND UPPER(TRIM(symbol)) = ANY(%(symbols)s)
        GROUP BY UPPER(TRIM(symbol))
        """,
        params={"start_date": start_date, "asof_date": asof_date, "symbols": symbols},
        retries=4,
        statement_timeout_ms=0,
        chunksize=50000,
    )
    if df.empty:
        return pd.DataFrame(columns=["symbol"])
    df["symbol"] = df["symbol"].astype("string").str.strip().str.upper()
    for col in df.columns:
        if col != "symbol":
            df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0)
    return df


def load_event_counts(asof_date: pd.Timestamp, symbols: list[str]) -> pd.DataFrame:
    base = pd.DataFrame({"symbol": symbols})
    watch = _count_events(
        table_name="advisory_watch_events",
        date_col="published_on",
        asof_date=asof_date,
        symbols=symbols,
        lookback_days=DEFAULT_NEWS_LOOKBACK_DAYS,
    ).rename(columns={"event_count": "announcement_event_count_7d"})
    news = _count_events(
        table_name="advisory_news_events",
        date_col="published_on",
        asof_date=asof_date,
        symbols=symbols,
        lookback_days=DEFAULT_NEWS_LOOKBACK_DAYS,
    ).rename(columns={"event_count": "news_event_count_7d"})
    evals = _count_events(
        table_name="advisory_event_evaluations",
        date_col="published_on",
        asof_date=asof_date,
        symbols=symbols,
        lookback_days=DEFAULT_EVENT_LOOKBACK_DAYS,
        extra_cols=""",
            , SUM(CASE WHEN LOWER(COALESCE(direction, setup_effect, sentiment, verdict, '')) ~ '(positive|bull|buy|benefit|favorable|investable)' THEN 1 ELSE 0 END) AS positive_event_count_20d
            , SUM(CASE WHEN LOWER(COALESCE(direction, setup_effect, sentiment, verdict, '')) ~ '(negative|bear|sell|risk|unfavorable|avoid|veto)' THEN 1 ELSE 0 END) AS negative_event_count_20d
        """,
    ).rename(columns={"event_count": "evaluated_event_count_20d"})
    out = base.merge(watch, on="symbol", how="left").merge(news, on="symbol", how="left").merge(evals, on="symbol", how="left")
    count_cols = [
        "announcement_event_count_7d",
        "news_event_count_7d",
        "evaluated_event_count_20d",
        "positive_event_count_20d",
        "negative_event_count_20d",
    ]
    for col in count_cols:
        out[col] = pd.to_numeric(out.get(col), errors="coerce").fillna(0).astype(int)
    return out


def _rank_pct(series: pd.Series) -> pd.Series:
    numeric = pd.to_numeric(series, errors="coerce")
    if numeric.notna().sum() <= 1:
        return pd.Series(0.0, index=series.index)
    return numeric.rank(pct=True, method="average").fillna(0.0)


def _macro_sensitivity(row: pd.Series) -> str:
    sector = str(row.get("sector_code") or "").lower()
    if any(token in sector for token in ["bank", "fin", "nbfc"]):
        return "rates_sensitive"
    if any(token in sector for token in ["oil", "gas", "chem", "metal", "commodity"]):
        return "commodity_sensitive"
    if any(token in sector for token in ["it", "tech", "pharma", "export"]):
        return "fx_global_sensitive"
    return "broad_market"


def build_market_context(
    *,
    asof_date: pd.Timestamp | None = None,
    top_fraction: float = DEFAULT_TOP_FRACTION,
    min_avg_traded_value_20d: float = DEFAULT_MIN_AVG_TRADED_VALUE_20D,
    min_price: float = DEFAULT_MIN_PRICE,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    effective_asof = _asof(asof_date)
    technical = load_latest_technical(effective_asof)
    if technical.empty:
        return pd.DataFrame(), pd.DataFrame()

    market_cap = load_latest_market_cap(effective_asof)
    frame = technical.merge(market_cap, on="symbol", how="left")
    frame["avg_traded_value_20d"] = pd.to_numeric(frame["avg_traded_value_20d"], errors="coerce")
    frame["adj_close"] = pd.to_numeric(frame["adj_close"], errors="coerce")
    frame = frame[
        frame["avg_traded_value_20d"].ge(float(min_avg_traded_value_20d))
        & frame["adj_close"].ge(float(min_price))
        & _bool_series(frame, "pass_liquidity_20d")
    ].copy()
    if frame.empty:
        return pd.DataFrame(), pd.DataFrame()

    frame["liquidity_rank_pct"] = _rank_pct(frame["avg_traded_value_20d"])
    frame["market_cap_rank_pct"] = _rank_pct(frame["market_cap"])
    has_market_cap = frame["market_cap"].notna().any()
    if has_market_cap:
        frame["context_rank_score"] = (0.60 * frame["market_cap_rank_pct"]) + (0.40 * frame["liquidity_rank_pct"])
    else:
        frame["context_rank_score"] = frame["liquidity_rank_pct"]
    frame = frame.sort_values(["context_rank_score", "avg_traded_value_20d", "symbol"], ascending=[False, False, True]).reset_index(drop=True)
    top_count = max(1, int(math.ceil(len(frame) * max(0.05, min(float(top_fraction), 1.0)))))
    frame["context_rank"] = np.arange(1, len(frame) + 1)
    frame["in_top_context"] = frame["context_rank"].le(top_count)
    symbols = frame.loc[frame["in_top_context"], "symbol"].dropna().astype(str).tolist()

    top_frame = frame[frame["in_top_context"]].copy()
    events = load_event_counts(effective_asof, symbols)
    exchange = load_exchange_context(effective_asof, symbols)
    top = top_frame.merge(events, on="symbol", how="left").merge(exchange, on="symbol", how="left")

    for col in [
        "announcement_event_count_7d",
        "news_event_count_7d",
        "evaluated_event_count_20d",
        "positive_event_count_20d",
        "negative_event_count_20d",
        "deal_cluster_count_20d",
        "short_selling_event_count_20d",
        "exchange_accumulation_score",
        "exchange_distribution_score",
        "exchange_event_score",
    ]:
        top[col] = _numeric_series(top, col)
    top["upcoming_earnings_14d"] = _bool_series(top, "upcoming_earnings_14d")
    rs_signal = pd.to_numeric(top["rs_vs_benchmark"], errors="coerce")
    rs_signal = rs_signal.where(rs_signal.notna(), pd.to_numeric(top["rs_vs_sector"], errors="coerce"))
    top["trend_leader_flag"] = (
        _bool_series(top, "pass_trend_alignment")
        & _bool_series(top, "pass_near_52w_high")
        & rs_signal.fillna(-1.0).gt(0.0)
    )
    top["technical_leadership_score"] = (
        _bool_series(top, "pass_above_dma_50").astype(float) * 0.20
        + _bool_series(top, "pass_trend_alignment").astype(float) * 0.30
        + _bool_series(top, "pass_near_52w_high").astype(float) * 0.20
        + rs_signal.fillna(0.0).clip(lower=-0.10, upper=0.10).add(0.10).div(0.20).mul(0.30)
    ).clip(lower=0.0, upper=1.0)
    top["macro_sensitivity_tag"] = top.apply(_macro_sensitivity, axis=1)
    top["asof_date"] = effective_asof
    top["load_ts"] = pd.Timestamp.utcnow()

    regime = load_regime_context(effective_asof)
    summary = build_market_context_summary(top, regime=regime, universe_count=len(frame), asof_date=effective_asof)
    return _ordered_universe(top), summary


def _sector_records(frame: pd.DataFrame, *, ascending: bool, limit: int = 8) -> list[dict[str, Any]]:
    if frame.empty or "sector_code" not in frame.columns:
        return []
    grouped = (
        frame.groupby("sector_code", dropna=False)
        .agg(
            symbol_count=("symbol", "nunique"),
            median_rs_vs_benchmark=("rs_vs_benchmark", "median"),
            leader_count=("trend_leader_flag", "sum"),
            event_count=("evaluated_event_count_20d", "sum"),
            positive_events=("positive_event_count_20d", "sum"),
            negative_events=("negative_event_count_20d", "sum"),
        )
        .reset_index()
    )
    grouped["leader_pct"] = grouped["leader_count"] / grouped["symbol_count"].replace(0, np.nan)
    grouped = grouped.sort_values(["median_rs_vs_benchmark", "leader_pct", "symbol_count"], ascending=[ascending, ascending, False])
    return grouped.head(limit).replace({np.nan: None}).to_dict(orient="records")


def _event_clusters(frame: pd.DataFrame, limit: int = 8) -> list[dict[str, Any]]:
    if frame.empty:
        return []
    grouped = (
        frame.groupby("sector_code", dropna=False)
        .agg(
            symbol_count=("symbol", "nunique"),
            announcement_events_7d=("announcement_event_count_7d", "sum"),
            news_events_7d=("news_event_count_7d", "sum"),
            evaluated_events_20d=("evaluated_event_count_20d", "sum"),
            positive_events_20d=("positive_event_count_20d", "sum"),
            negative_events_20d=("negative_event_count_20d", "sum"),
            exchange_score=("exchange_event_score", "sum"),
        )
        .reset_index()
    )
    grouped["cluster_score"] = (
        grouped["announcement_events_7d"]
        + grouped["news_events_7d"]
        + grouped["evaluated_events_20d"]
        + grouped["exchange_score"].abs()
    )
    grouped = grouped[grouped["cluster_score"].gt(0)].sort_values("cluster_score", ascending=False)
    return grouped.head(limit).replace({np.nan: None}).to_dict(orient="records")


def build_market_context_summary(
    top: pd.DataFrame,
    *,
    regime: dict[str, Any],
    universe_count: int,
    asof_date: pd.Timestamp,
) -> pd.DataFrame:
    if top.empty:
        return pd.DataFrame()
    rs_signal = pd.to_numeric(top["rs_vs_benchmark"], errors="coerce")
    rs_signal = rs_signal.where(rs_signal.notna(), pd.to_numeric(top["rs_vs_sector"], errors="coerce"))
    breadth_above_dma50 = float(_bool_series(top, "pass_above_dma_50").mean())
    breadth_trend = float(_bool_series(top, "pass_trend_alignment").mean())
    breadth_rs = float(rs_signal.fillna(-1.0).gt(0).mean())
    positive_events = int(pd.to_numeric(top["positive_event_count_20d"], errors="coerce").fillna(0).sum())
    negative_events = int(pd.to_numeric(top["negative_event_count_20d"], errors="coerce").fillna(0).sum())
    risk_on_score = float(np.nanmean([breadth_above_dma50, breadth_trend, breadth_rs]))
    risk_off_score = float(1.0 - risk_on_score)
    macro_risk_state = str(regime.get("macro_risk_state") or "")
    if macro_risk_state.upper() in {"HIGH", "STRESS", "RISK_OFF"} or bool(regime.get("risk_off_flag")):
        risk_off_score = min(1.0, risk_off_score + 0.15)
        risk_on_score = max(0.0, risk_on_score - 0.15)
    leading = _sector_records(top, ascending=False)
    lagging = _sector_records(top, ascending=True)
    clusters = _event_clusters(top)
    regime_name = str(regime.get("regime_name") or "UNKNOWN")
    summary_text = (
        f"Top-context universe has {len(top)} symbols from {universe_count} investable names. "
        f"Breadth: {breadth_above_dma50:.0%} above 50-DMA, {breadth_trend:.0%} in trend alignment, "
        f"{breadth_rs:.0%} outperforming benchmark. Regime: {regime_name}."
    )
    row = {
        "asof_date": asof_date,
        "universe_count": int(universe_count),
        "top_context_count": int(len(top)),
        "regime_name": regime_name,
        "macro_risk_state": regime.get("macro_risk_state"),
        "macro_stress_score": _safe_float(regime.get("macro_stress_score"), default=np.nan),
        "macro_sizing_multiplier": _safe_float(regime.get("macro_sizing_multiplier"), default=np.nan),
        "risk_on_score": round(risk_on_score, 6),
        "risk_off_score": round(risk_off_score, 6),
        "breadth_above_dma50_pct": round(breadth_above_dma50 * 100.0, 6),
        "breadth_trend_alignment_pct": round(breadth_trend * 100.0, 6),
        "breadth_rs_positive_pct": round(breadth_rs * 100.0, 6),
        "positive_event_count_20d": positive_events,
        "negative_event_count_20d": negative_events,
        "news_event_count_7d": int(pd.to_numeric(top["news_event_count_7d"], errors="coerce").fillna(0).sum()),
        "announcement_event_count_7d": int(pd.to_numeric(top["announcement_event_count_7d"], errors="coerce").fillna(0).sum()),
        "leading_sectors_json": _json_text(leading),
        "lagging_sectors_json": _json_text(lagging),
        "event_clusters_json": _json_text(clusters),
        "summary_text": summary_text,
        "load_ts": pd.Timestamp.utcnow(),
    }
    return pd.DataFrame([row])


def _ordered_universe(df: pd.DataFrame) -> pd.DataFrame:
    cols = [
        "asof_date",
        "symbol",
        "company_master_id",
        "sector_code",
        "sector_name",
        "market_cap",
        "market_cap_source_date",
        "adj_close",
        "avg_traded_value_20d",
        "avg_traded_value_60d",
        "total_value",
        "rs_vs_benchmark",
        "rs_vs_sector",
        "dist_52w_high",
        "dma_50_slope_20d_pct",
        "trend_persistence_60d",
        "accumulation_days_20d",
        "distribution_days_20d",
        "pass_above_dma_50",
        "pass_trend_alignment",
        "pass_near_52w_high",
        "liquidity_rank_pct",
        "market_cap_rank_pct",
        "context_rank_score",
        "context_rank",
        "in_top_context",
        "technical_leadership_score",
        "trend_leader_flag",
        "announcement_event_count_7d",
        "news_event_count_7d",
        "evaluated_event_count_20d",
        "positive_event_count_20d",
        "negative_event_count_20d",
        "latest_exchange_event_date",
        "latest_exchange_event_source",
        "latest_exchange_event_type",
        "deal_cluster_count_20d",
        "insider_net_value_90d",
        "short_selling_event_count_20d",
        "upcoming_earnings_14d",
        "exchange_accumulation_score",
        "exchange_distribution_score",
        "exchange_event_score",
        "macro_sensitivity_tag",
        "load_ts",
    ]
    for col in cols:
        if col not in df.columns:
            df[col] = pd.NA
    return df[cols].drop_duplicates(subset=["asof_date", "symbol"], keep="last").reset_index(drop=True)


def persist_market_context(universe: pd.DataFrame, summary: pd.DataFrame) -> None:
    if not universe.empty:
        upsert_to_db(universe, UNIVERSE_TABLE, unique_keys=["asof_date", "symbol"], timescaledb_column="asof_date")
    if not summary.empty:
        upsert_to_db(summary, SUMMARY_TABLE, unique_keys=["asof_date"], timescaledb_column="asof_date")


def load_latest_market_context(asof_date: pd.Timestamp | None = None, *, limit: int = 50) -> dict[str, Any]:
    effective_asof = _asof(asof_date)
    summary = pd.DataFrame()
    universe = pd.DataFrame()
    if _table_exists(SUMMARY_TABLE):
        summary = sql_to_df(
            f"""
            SELECT *
            FROM {SUMMARY_TABLE}
            WHERE asof_date <= %(asof_date)s
            ORDER BY asof_date DESC
            LIMIT 1
            """,
            params={"asof_date": effective_asof},
            retries=4,
        )
    if _table_exists(UNIVERSE_TABLE):
        universe = sql_to_df(
            f"""
            SELECT *
            FROM {UNIVERSE_TABLE}
            WHERE asof_date = (
                SELECT MAX(asof_date)
                FROM {UNIVERSE_TABLE}
                WHERE asof_date <= %(asof_date)s
            )
              AND in_top_context = TRUE
            ORDER BY context_rank ASC
            LIMIT %(limit)s
            """,
            params={"asof_date": effective_asof, "limit": int(limit)},
            retries=4,
        )
    return {
        "summary": _clean_records(summary)[0] if not summary.empty else {},
        "top_universe": _clean_records(universe) if not universe.empty else [],
    }


def summarize(universe: pd.DataFrame, summary: pd.DataFrame) -> dict[str, Any]:
    row = summary.to_dict(orient="records")[0] if not summary.empty else {}
    return {
        "status": "ok",
        "universe_table": UNIVERSE_TABLE,
        "summary_table": SUMMARY_TABLE,
        "universe_rows": int(len(universe)),
        "summary_rows": int(len(summary)),
        "asof_date": None if not row else str(row.get("asof_date")),
        "top_context_count": row.get("top_context_count"),
        "summary_text": row.get("summary_text"),
        "sample": _clean_records(universe.head(10)) if not universe.empty else [],
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build top-50% broad market context for advisory interpretation.")
    parser.add_argument("--date", type=parse_datetime_arg, help="As-of date in YYYY-MM-DD")
    parser.add_argument("--top-fraction", type=float, default=DEFAULT_TOP_FRACTION)
    parser.add_argument("--min-avg-traded-value-20d", type=float, default=DEFAULT_MIN_AVG_TRADED_VALUE_20D)
    parser.add_argument("--min-price", type=float, default=DEFAULT_MIN_PRICE)
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    universe, summary = build_market_context(
        asof_date=args.date,
        top_fraction=float(args.top_fraction),
        min_avg_traded_value_20d=float(args.min_avg_traded_value_20d),
        min_price=float(args.min_price),
    )
    if not args.dry_run:
        persist_market_context(universe, summary)
    print(json.dumps(summarize(universe, summary), indent=2, default=str, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
