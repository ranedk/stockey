from __future__ import annotations

import argparse
import json
from datetime import timezone
from typing import Any

import pandas as pd

from advisory.data_sync import ensure_advisory_symbol_inputs
from advisory.fundamental_snapshot import build_fundamental_snapshot, persist_fundamental_snapshot
from advisory.intraday_features import TABLE_NAME as INTRADAY_FEATURES_TABLE
from advisory.intraday_features import build_intraday_features, persist_intraday_features
from advisory.news_theme_engine import load_active_theme_screener_mapping
from advisory.peer_sync import sync_peer_data
from advisory.setup_registry import load_setup_registry
from advisory.technical_features import build_technical_features, persist_technical_features
from utils.db import db_session, sql_to_df, upsert_to_db
from utils.sync import parse_datetime_arg


CANDIDATES_TABLE = "advisory_candidates"
REJECTIONS_TABLE = "advisory_candidate_rejections"
OVERLAY_TABLE = "advisory_market_overlay_daily"

DEFAULT_SCORING_WEIGHTS = {
    "technical": 0.35,
    "fundamental": 0.30,
    "regime_fit": 0.20,
    "event": 0.15,
}
DEFAULT_SCORE_THRESHOLDS = {
    "pass_now": 0.68,
    "watch_breakout": 0.58,
    "watch_pullback": 0.52,
    "watch_event": 0.48,
    "abstain": 0.40,
    "near_miss_gap": 0.05,
}


def normalize_timestamp(series: pd.Series) -> pd.Series:
    return pd.to_datetime(series, utc=True, errors="coerce").dt.normalize()


def table_exists(table_name: str) -> bool:
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


def safe_float(value: Any) -> float | None:
    out = pd.to_numeric(value, errors="coerce")
    if pd.isna(out):
        return None
    return float(out)


def ensure_rule_output_tables() -> None:
    with db_session() as (_, cur):
        cur.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {CANDIDATES_TABLE} (
                asof_date TIMESTAMPTZ NOT NULL,
                screener_date TIMESTAMPTZ,
                setup_id TEXT NOT NULL,
                setup_name TEXT,
                setup_family TEXT,
                holding_horizon_note TEXT,
                regime_name TEXT,
                base_regime TEXT,
                news_overlay TEXT,
                theme_ids TEXT,
                symbol TEXT NOT NULL,
                company_master_id TEXT,
                screener_slug TEXT,
                source_screener_slug TEXT,
                source_screener_list TEXT,
                rank BIGINT,
                candidate_state TEXT,
                watch_reason_detail TEXT,
                technical_score DOUBLE PRECISION,
                fundamental_score DOUBLE PRECISION,
                regime_fit_score DOUBLE PRECISION,
                event_score DOUBLE PRECISION,
                setup_score DOUBLE PRECISION,
                avg_traded_value_20d DOUBLE PRECISION,
                rs_vs_benchmark DOUBLE PRECISION,
                rs_vs_sector DOUBLE PRECISION,
                intraday_close_vs_vwap_pct DOUBLE PRECISION,
                intraday_pct_bars_above_vwap DOUBLE PRECISION,
                intraday_close_location_pct DOUBLE PRECISION,
                intraday_opening_range_breakout_up BOOLEAN,
                intraday_prev_day_breakout_up BOOLEAN,
                intraday_failed_prev_day_breakout BOOLEAN,
                intraday_volume_vs_20d DOUBLE PRECISION,
                intraday_breakout_score DOUBLE PRECISION,
                intraday_pattern_label TEXT,
                intraday_interval_minutes INTEGER,
                total_revenue_qoq_growth_vs_sector DOUBLE PRECISION,
                profit_after_tax_qoq_growth_vs_sector DOUBLE PRECISION,
                debt_to_equity_vs_sector DOUBLE PRECISION,
                entry_style TEXT,
                attractive_price_low DOUBLE PRECISION,
                attractive_price_high DOUBLE PRECISION,
                invalidation_price DOUBLE PRECISION,
                entry_note TEXT,
                near_miss_flag BOOLEAN,
                watch_enabled BOOLEAN,
                watch_reasons TEXT,
                rule_pass BOOLEAN,
                load_ts TIMESTAMPTZ,
                UNIQUE (asof_date, setup_id, symbol)
            )
            """
        )
        candidate_columns = {
            "screener_date": "TIMESTAMPTZ",
            "setup_name": "TEXT",
            "setup_family": "TEXT",
            "holding_horizon_note": "TEXT",
            "regime_name": "TEXT",
            "base_regime": "TEXT",
            "news_overlay": "TEXT",
            "theme_ids": "TEXT",
            "company_master_id": "TEXT",
            "screener_slug": "TEXT",
            "source_screener_slug": "TEXT",
            "source_screener_list": "TEXT",
            "rank": "BIGINT",
            "candidate_state": "TEXT",
            "watch_reason_detail": "TEXT",
            "technical_score": "DOUBLE PRECISION",
            "fundamental_score": "DOUBLE PRECISION",
            "regime_fit_score": "DOUBLE PRECISION",
            "event_score": "DOUBLE PRECISION",
            "setup_score": "DOUBLE PRECISION",
            "avg_traded_value_20d": "DOUBLE PRECISION",
            "rs_vs_benchmark": "DOUBLE PRECISION",
            "rs_vs_sector": "DOUBLE PRECISION",
            "intraday_close_vs_vwap_pct": "DOUBLE PRECISION",
            "intraday_pct_bars_above_vwap": "DOUBLE PRECISION",
            "intraday_close_location_pct": "DOUBLE PRECISION",
            "intraday_opening_range_breakout_up": "BOOLEAN",
            "intraday_prev_day_breakout_up": "BOOLEAN",
            "intraday_failed_prev_day_breakout": "BOOLEAN",
            "intraday_volume_vs_20d": "DOUBLE PRECISION",
            "intraday_breakout_score": "DOUBLE PRECISION",
            "intraday_pattern_label": "TEXT",
            "intraday_interval_minutes": "INTEGER",
            "total_revenue_qoq_growth_vs_sector": "DOUBLE PRECISION",
            "profit_after_tax_qoq_growth_vs_sector": "DOUBLE PRECISION",
            "debt_to_equity_vs_sector": "DOUBLE PRECISION",
            "entry_style": "TEXT",
            "attractive_price_low": "DOUBLE PRECISION",
            "attractive_price_high": "DOUBLE PRECISION",
            "invalidation_price": "DOUBLE PRECISION",
            "entry_note": "TEXT",
            "near_miss_flag": "BOOLEAN",
            "watch_enabled": "BOOLEAN",
            "watch_reasons": "TEXT",
            "rule_pass": "BOOLEAN",
            "load_ts": "TIMESTAMPTZ",
        }
        for column, sql_type in candidate_columns.items():
            cur.execute(f"ALTER TABLE {CANDIDATES_TABLE} ADD COLUMN IF NOT EXISTS {column} {sql_type}")

        cur.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {REJECTIONS_TABLE} (
                asof_date TIMESTAMPTZ NOT NULL,
                setup_id TEXT NOT NULL,
                symbol TEXT,
                reason_code TEXT NOT NULL,
                screener_date TIMESTAMPTZ,
                setup_name TEXT,
                company_master_id TEXT,
                base_regime TEXT,
                news_overlay TEXT,
                severity TEXT,
                is_near_miss BOOLEAN,
                delta_to_pass DOUBLE PRECISION,
                reason_detail TEXT,
                load_ts TIMESTAMPTZ,
                UNIQUE (asof_date, setup_id, symbol, reason_code)
            )
            """
        )
        rejection_columns = {
            "screener_date": "TIMESTAMPTZ",
            "setup_name": "TEXT",
            "company_master_id": "TEXT",
            "base_regime": "TEXT",
            "news_overlay": "TEXT",
            "severity": "TEXT",
            "is_near_miss": "BOOLEAN",
            "delta_to_pass": "DOUBLE PRECISION",
            "reason_detail": "TEXT",
            "load_ts": "TIMESTAMPTZ",
        }
        for column, sql_type in rejection_columns.items():
            cur.execute(f"ALTER TABLE {REJECTIONS_TABLE} ADD COLUMN IF NOT EXISTS {column} {sql_type}")


DEFAULT_FRESHNESS_POLICY = {
    "technical_max_age_days": 15,
    "fundamentals_max_age_days": 150,
    "regime_max_age_days": 15,
    "intraday_max_age_days": 2,
    "fundamentals_required": True,
}
DEFAULT_MAX_SNAPSHOT_REFRESH_AGE_DAYS = 7
DEFAULT_MAX_INTRADAY_PREFETCH_AGE_DAYS = 14


def get_effective_dates(asof_date: pd.Timestamp | None = None) -> dict[str, pd.Timestamp | None]:
    screener_cutoff = asof_date
    if screener_cutoff is None:
        screener_df = sql_to_df("SELECT MAX(date) AS screener_date FROM advisory_screener_constituents")
        if screener_df.empty or pd.isna(pd.to_datetime(screener_df.iloc[0]["screener_date"], utc=True, errors="coerce")):
            return {
                "requested_asof_date": None,
                "screener_date": None,
                "regime_date": None,
                "technical_date": None,
                "fundamentals_date": None,
                "intraday_date": None,
            }
        screener_cutoff = pd.to_datetime(screener_df.iloc[0]["screener_date"], utc=True, errors="coerce").normalize()

    df = sql_to_df(
        """
        SELECT
            (SELECT MAX(date) FROM advisory_screener_constituents WHERE date <= %(asof_date)s) AS screener_date,
            (SELECT MAX(asof_date) FROM advisory_market_regime WHERE asof_date <= %(asof_date)s) AS regime_date,
            (SELECT MAX(asof_date) FROM advisory_technical_daily WHERE asof_date <= %(asof_date)s) AS technical_date,
            (SELECT MAX(asof_date) FROM advisory_fundamentals_daily WHERE asof_date <= %(asof_date)s) AS fundamentals_date
        """,
        params={"asof_date": screener_cutoff},
    )
    if df.empty:
        return {
            "requested_asof_date": screener_cutoff.normalize(),
            "screener_date": None,
            "regime_date": None,
            "technical_date": None,
            "fundamentals_date": None,
            "intraday_date": None,
        }
    row = df.iloc[0]
    screener_date = pd.to_datetime(row.get("screener_date"), utc=True, errors="coerce")
    regime_date = pd.to_datetime(row.get("regime_date"), utc=True, errors="coerce")
    technical_date = pd.to_datetime(row.get("technical_date"), utc=True, errors="coerce")
    fundamentals_date = pd.to_datetime(row.get("fundamentals_date"), utc=True, errors="coerce")
    intraday_date = None
    if table_exists(INTRADAY_FEATURES_TABLE):
        intraday_df = sql_to_df(
            f"SELECT MAX(asof_date) AS intraday_date FROM {INTRADAY_FEATURES_TABLE} WHERE asof_date <= %s",
            params=(screener_cutoff,),
        )
        if not intraday_df.empty:
            intraday_date = pd.to_datetime(intraday_df.iloc[0].get("intraday_date"), utc=True, errors="coerce")
    return {
        "requested_asof_date": screener_cutoff.normalize(),
        "screener_date": None if pd.isna(screener_date) else screener_date.normalize(),
        "regime_date": None if pd.isna(regime_date) else regime_date.normalize(),
        "technical_date": None if pd.isna(technical_date) else technical_date.normalize(),
        "fundamentals_date": None if pd.isna(fundamentals_date) else fundamentals_date.normalize(),
        "intraday_date": None if intraday_date is None or pd.isna(intraday_date) else intraday_date.normalize(),
    }


def load_regime(asof_date: pd.Timestamp) -> dict[str, Any] | None:
    df = sql_to_df(
        """
        SELECT *
        FROM advisory_market_regime
        WHERE asof_date <= %s
        ORDER BY asof_date DESC
        LIMIT 1
        """,
        params=(asof_date,),
    )
    if df.empty:
        return None
    return df.iloc[0].to_dict()


def table_exists(table_name: str) -> bool:
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


def load_overlay(asof_date: pd.Timestamp, regime_name: str | None = None) -> dict[str, Any]:
    if table_exists(OVERLAY_TABLE):
        df = sql_to_df(
            f"""
            SELECT *
            FROM {OVERLAY_TABLE}
            WHERE asof_date <= %s
            ORDER BY asof_date DESC
            LIMIT 1
            """,
            params=(asof_date,),
        )
        if not df.empty:
            row = df.iloc[0].to_dict()
            row["overlay_name"] = str(row.get("overlay_name") or "NONE").upper()
            return row
    return {
        "asof_date": asof_date,
        "base_regime": regime_name,
        "overlay_name": "NONE",
        "overlay_intensity": 0.0,
        "overlay_reason": "overlay table missing or no overlay row for date",
        "source_count": 0,
    }


def resolve_setup_screeners(
    setup: dict[str, Any],
    overlay_name: str | None,
    *,
    theme_screener_mapping: dict[str, Any] | None = None,
) -> tuple[list[str], str, list[str]]:
    base_screeners = [str(value) for value in (setup.get("screeners") or setup.get("screener_slugs") or []) if value]
    if not base_screeners and setup.get("screener_slug"):
        base_screeners = [str(setup["screener_slug"])]
    theme_ids: list[str] = []
    if str(setup.get("setup_id") or "").upper() == "EVENT_OPPORTUNITY_V1":
        theme_ids = [str(value) for value in ((theme_screener_mapping or {}).get("theme_ids") or []) if value]
        for value in ((theme_screener_mapping or {}).get("screener_slugs") or []):
            if value and str(value) not in base_screeners:
                base_screeners.append(str(value))
    overlay_cfg = (setup.get("overlay_screeners") or {}).get(str(overlay_name or "NONE").upper(), {})
    add = [str(value) for value in (overlay_cfg.get("add") or []) if value]
    remove = {str(value) for value in (overlay_cfg.get("remove") or []) if value}
    active = [value for value in base_screeners if value not in remove]
    for value in add:
        if value not in active:
            active.append(value)
    return active, str(setup.get("screener_mode") or "union").lower(), theme_ids


def load_screener_universe(asof_date: pd.Timestamp, screener_slugs: list[str] | None, *, screener_mode: str = "union") -> pd.DataFrame:
    clauses = ["date = %s"]
    params: list[object] = [asof_date]
    normalized_slugs = [str(value) for value in (screener_slugs or []) if str(value).strip()]
    if normalized_slugs:
        clauses.append("screener_slug = ANY(%s)")
        params.append(normalized_slugs)
    df = sql_to_df(
        f"""
        SELECT
            date AS screener_date,
            screener_slug,
            screener_name,
            ticker AS symbol,
            exchange,
            company_master_id,
            rank,
            last_price,
            volume,
            market_cap,
            pe_ratio
        FROM advisory_screener_constituents
        WHERE {' AND '.join(clauses)}
        ORDER BY rank, symbol
        """,
        params=tuple(params),
    )
    if df.empty:
        return df
    df["screener_date"] = normalize_timestamp(df["screener_date"])
    df["symbol"] = df["symbol"].astype("string").str.upper()
    required = sorted(set(normalized_slugs))
    if screener_mode == "intersection" and required:
        counts = df.groupby("symbol")["screener_slug"].nunique()
        eligible = counts[counts >= len(required)].index.astype("string").tolist()
        df = df[df["symbol"].isin(eligible)]
    if df.empty:
        return df
    rows: list[dict[str, Any]] = []
    for symbol, group in df.groupby("symbol", sort=False):
        ranked = group.sort_values(["rank", "screener_slug"], kind="stable")
        first = ranked.iloc[0].to_dict()
        screener_list = sorted(group["screener_slug"].dropna().astype(str).unique().tolist())
        first["source_screener_slug"] = first.get("screener_slug")
        first["source_screener_list"] = json.dumps(screener_list, ensure_ascii=False)
        first["source_screener_count"] = len(screener_list)
        rows.append(first)
    return pd.DataFrame(rows)


def load_technical(asof_date: pd.Timestamp) -> pd.DataFrame:
    df = sql_to_df(
        """
        SELECT DISTINCT ON (symbol)
            asof_date AS technical_snapshot_date,
            company_master_id,
            symbol,
            series,
            security_id,
            isin,
            benchmark_name,
            sector_code,
            sector_name,
            adj_close,
            adj_high,
            adj_low,
            volume,
            total_value,
            dma_20,
            dma_50,
            dma_200,
            atr_20,
            atr_compression_pct,
            bb_width,
            dist_20d_high,
            dist_50d_high,
            dist_52w_high,
            avg_traded_value_20d,
            avg_traded_value_60d,
            rs_vs_benchmark,
            sector_peer_ret_20d,
            sector_peer_count,
            rs_vs_sector,
            breakout_extension_pct,
            pass_above_dma_20,
            pass_above_dma_50,
            pass_above_dma_200,
            pass_liquidity_20d,
            pass_near_52w_high,
            pass_breakout_extension,
            load_ts
        FROM advisory_technical_daily
        WHERE asof_date <= %s
        ORDER BY symbol, asof_date DESC
        """,
        params=(asof_date,),
    )
    if df.empty:
        return df
    df["technical_snapshot_date"] = normalize_timestamp(df["technical_snapshot_date"])
    df["symbol"] = df["symbol"].astype("string").str.upper()
    return df


def load_intraday(asof_date: pd.Timestamp) -> pd.DataFrame:
    if not table_exists(INTRADAY_FEATURES_TABLE):
        return pd.DataFrame()
    df = sql_to_df(
        f"""
        SELECT DISTINCT ON (symbol)
            asof_date AS intraday_snapshot_date,
            symbol,
            company_master_id,
            interval_minutes,
            bar_count,
            session_open,
            session_high,
            session_low,
            session_close,
            session_volume,
            intraday_vwap,
            intraday_range_pct,
            intraday_open_to_close_pct,
            intraday_close_vs_vwap_pct,
            intraday_pct_bars_above_vwap,
            intraday_close_location_pct,
            intraday_opening_range_high,
            intraday_opening_range_low,
            intraday_opening_range_breakout_up,
            intraday_opening_range_breakout_down,
            intraday_prev_day_high,
            intraday_prev_day_low,
            intraday_prev_day_breakout_up,
            intraday_failed_prev_day_breakout,
            intraday_first_30m_return_pct,
            intraday_last_60m_return_pct,
            intraday_volume_vs_20d,
            intraday_breakout_score,
            intraday_pattern_label,
            model_name,
            model_score,
            load_ts
        FROM {INTRADAY_FEATURES_TABLE}
        WHERE asof_date <= %s
          AND interval_minutes = 1
        ORDER BY symbol, asof_date DESC
        """,
        params=(asof_date,),
    )
    if df.empty:
        return df
    df["intraday_snapshot_date"] = normalize_timestamp(df["intraday_snapshot_date"])
    df["symbol"] = df["symbol"].astype("string").str.upper()
    return df


def load_fundamentals(asof_date: pd.Timestamp) -> pd.DataFrame:
    df = sql_to_df(
        """
        SELECT DISTINCT ON (symbol)
            asof_date AS fundamentals_snapshot_date,
            *
        FROM advisory_fundamentals_daily
        WHERE asof_date <= %s
        ORDER BY symbol, asof_date DESC
        """,
        params=(asof_date,),
    )
    if df.empty:
        return df
    df["fundamentals_snapshot_date"] = normalize_timestamp(df["fundamentals_snapshot_date"])
    df["symbol"] = df["symbol"].astype("string").str.upper()
    return df


def refresh_missing_snapshots(symbols: list[str], effective_date: pd.Timestamp) -> dict[str, object]:
    sync_result = ensure_advisory_symbol_inputs(symbols, to_date=effective_date)
    peer_sync_result = sync_peer_data(symbols=symbols, to_date=effective_date)

    technical_df = build_technical_features(symbols=symbols, from_date=effective_date, to_date=effective_date, rebuild=False)
    persist_technical_features(technical_df, rebuild=False, symbols=symbols)

    intraday_df, intraday_meta = build_intraday_features(symbols=symbols, asof_date=effective_date)
    persist_intraday_features(intraday_df, rebuild=False, asof_date=effective_date)

    fundamentals_df = build_fundamental_snapshot(symbols=symbols, from_date=effective_date, to_date=effective_date, rebuild=False)
    persist_fundamental_snapshot(fundamentals_df)

    return {
        "data_sync": sync_result,
        "peer_sync": peer_sync_result,
        "technical_rows": int(len(technical_df)),
        "intraday_rows": int(len(intraday_df)),
        "intraday_meta": intraday_meta,
        "fundamental_rows": int(len(fundamentals_df)),
    }


def _days_stale_from_today(value: pd.Timestamp | None) -> int | None:
    ts = pd.to_datetime(value, utc=True, errors="coerce")
    if pd.isna(ts):
        return None
    today = pd.Timestamp.now(tz=timezone.utc).normalize()
    return int((today - ts.normalize()).days)


def compare(value: Any, operator: str, threshold: Any) -> bool:
    if pd.isna(value):
        return False
    if operator == "eq":
        return value == threshold
    if operator == "gte":
        return value >= threshold
    if operator == "gt":
        return value > threshold
    if operator == "lte":
        return value <= threshold
    if operator == "lt":
        return value < threshold
    raise ValueError(f"Unsupported operator: {operator}")


def score_rule(value: Any, operator: str, threshold: Any) -> float:
    if pd.isna(value):
        return 0.0
    if operator == "eq":
        return 1.0 if value == threshold else 0.0
    numeric_value = safe_float(value)
    numeric_threshold = safe_float(threshold)
    if numeric_value is None or numeric_threshold is None:
        return 0.0
    if operator in {"gte", "gt"}:
        if numeric_value >= numeric_threshold:
            return 1.0
        gap = abs(numeric_threshold - numeric_value)
        scale = max(abs(numeric_threshold), 1.0)
        return max(0.0, 1.0 - (gap / scale))
    if operator in {"lte", "lt"}:
        if numeric_value <= numeric_threshold:
            return 1.0
        gap = abs(numeric_value - numeric_threshold)
        scale = max(abs(numeric_threshold), 1.0)
        return max(0.0, 1.0 - (gap / scale))
    return 0.0


def average_score(values: list[float], default: float = 0.5) -> float:
    usable = [float(value) for value in values if value is not None]
    if not usable:
        return default
    return round(sum(usable) / len(usable), 6)


def build_rejection(reason_code: str, reason_detail: str, *, severity: str = "hard", is_near_miss: bool = False, delta_to_pass: float | None = None) -> dict[str, Any]:
    return {
        "reason_code": reason_code,
        "reason_detail": reason_detail,
        "severity": severity,
        "is_near_miss": bool(is_near_miss),
        "delta_to_pass": None if delta_to_pass is None else round(float(delta_to_pass), 6),
    }


def compute_regime_fit_score(regime_name: str, setup: dict[str, Any]) -> float:
    policy = setup.get("regime_policy") or {}
    if regime_name in set(policy.get("preferred") or []):
        return 1.0
    if regime_name in set(policy.get("acceptable") or []):
        return 0.7
    if regime_name in set(policy.get("avoid") or []):
        return 0.25
    if regime_name in set(setup.get("allowed_regimes") or []):
        return 0.6
    return 0.0


def overlay_is_allowed(overlay_name: str, setup: dict[str, Any]) -> bool:
    allowed = {str(value).upper() for value in (setup.get("allowed_overlays") or []) if value}
    blocked = {str(value).upper() for value in (setup.get("blocked_overlays") or []) if value}
    overlay_upper = str(overlay_name or "NONE").upper()
    if overlay_upper in blocked:
        return False
    if allowed and overlay_upper not in allowed:
        return False
    return True


def overlay_is_explicitly_blocked(overlay_name: str, setup: dict[str, Any]) -> bool:
    blocked = {str(value).upper() for value in (setup.get("blocked_overlays") or []) if value}
    return str(overlay_name or "NONE").upper() in blocked


def regime_is_explicitly_blocked(regime_name: str, setup: dict[str, Any]) -> bool:
    blocked = {str(value).upper() for value in (setup.get("blocked_regimes") or []) if value}
    return str(regime_name or "").upper() in blocked


def component_age_days(candidate_asof_date: pd.Timestamp | None, source_asof_date: Any) -> int | None:
    if candidate_asof_date is None:
        return None
    source_ts = pd.to_datetime(source_asof_date, utc=True, errors="coerce")
    if pd.isna(source_ts):
        return None
    return int((candidate_asof_date.normalize() - source_ts.normalize()).days)


def evaluate_freshness(
    row: pd.Series,
    *,
    candidate_asof_date: pd.Timestamp,
    setup: dict[str, Any],
) -> list[dict[str, Any]]:
    policy = {**DEFAULT_FRESHNESS_POLICY, **(setup.get("freshness_policy") or {})}
    rejections: list[dict[str, Any]] = []

    technical_age = component_age_days(candidate_asof_date, row.get("technical_asof_date"))
    if technical_age is None:
        rejections.append(build_rejection("missing_technical_snapshot", "technical snapshot missing", severity="hard"))
    elif technical_age > int(policy.get("technical_max_age_days", DEFAULT_FRESHNESS_POLICY["technical_max_age_days"])):
        rejections.append(build_rejection("technical_snapshot_stale", f"technical_age_days={technical_age}", severity="hard"))

    fundamentals_age = component_age_days(candidate_asof_date, row.get("fundamentals_asof_date"))
    fundamentals_required = bool(policy.get("fundamentals_required", DEFAULT_FRESHNESS_POLICY["fundamentals_required"]))
    if fundamentals_age is None:
        if fundamentals_required:
            rejections.append(build_rejection("missing_fundamental_snapshot", "fundamental snapshot missing", severity="hard"))
    elif fundamentals_age > int(policy.get("fundamentals_max_age_days", DEFAULT_FRESHNESS_POLICY["fundamentals_max_age_days"])):
        severity = "hard" if fundamentals_required else "soft"
        rejections.append(build_rejection("fundamental_snapshot_stale", f"fundamentals_age_days={fundamentals_age}", severity=severity))

    regime_age = component_age_days(candidate_asof_date, row.get("regime_asof_date"))
    if regime_age is None:
        rejections.append(build_rejection("missing_regime_snapshot", "regime snapshot missing", severity="hard"))
    elif regime_age > int(policy.get("regime_max_age_days", DEFAULT_FRESHNESS_POLICY["regime_max_age_days"])):
        rejections.append(build_rejection("regime_snapshot_stale", f"regime_age_days={regime_age}", severity="hard"))

    intraday_mode = str(setup.get("intraday_usage_mode") or "confirm_only").lower()
    intraday_age = component_age_days(candidate_asof_date, row.get("intraday_asof_date"))
    intraday_limit = int(policy.get("intraday_max_age_days", DEFAULT_FRESHNESS_POLICY["intraday_max_age_days"]))
    if intraday_mode == "tactical_primary":
        if intraday_age is None:
            rejections.append(build_rejection("missing_intraday_snapshot", "intraday confirmation snapshot missing", severity="hard"))
        elif intraday_age > intraday_limit:
            rejections.append(build_rejection("intraday_snapshot_stale", f"intraday_age_days={intraday_age}", severity="hard"))
    elif intraday_age is not None and intraday_age > intraday_limit:
        rejections.append(build_rejection("intraday_snapshot_stale", f"intraday_age_days={intraday_age}", severity="soft"))

    return rejections


def intraday_positive_signal(row: pd.Series) -> bool:
    breakout_score = safe_float(row.get("intraday_breakout_score"))
    close_vs_vwap_pct = safe_float(row.get("intraday_close_vs_vwap_pct"))
    close_location_pct = safe_float(row.get("intraday_close_location_pct"))
    failed_breakout = bool(row.get("intraday_failed_prev_day_breakout"))
    return bool(
        not failed_breakout
        and breakout_score is not None
        and breakout_score >= 0.55
        and close_vs_vwap_pct is not None
        and close_vs_vwap_pct >= 0.0
        and close_location_pct is not None
        and close_location_pct >= 0.55
    )


def intraday_negative_signal(row: pd.Series) -> bool:
    breakout_score = safe_float(row.get("intraday_breakout_score"))
    close_vs_vwap_pct = safe_float(row.get("intraday_close_vs_vwap_pct"))
    failed_breakout = bool(row.get("intraday_failed_prev_day_breakout"))
    return bool(failed_breakout or (breakout_score is not None and breakout_score < 0.30) or (close_vs_vwap_pct is not None and close_vs_vwap_pct < -0.25))


def get_freshness_policy(setup: dict[str, Any]) -> dict[str, Any]:
    policy = dict(DEFAULT_FRESHNESS_POLICY)
    policy.update(dict(setup.get("freshness_policy") or {}))
    return policy


def get_intraday_usage_mode(setup: dict[str, Any]) -> str:
    return str(setup.get("intraday_usage_mode") or "confirm_only").lower()


def snapshot_age_days(snapshot_date: Any, asof_date: Any) -> int | None:
    snapshot_ts = pd.to_datetime(snapshot_date, utc=True, errors="coerce")
    asof_ts = pd.to_datetime(asof_date, utc=True, errors="coerce")
    if pd.isna(snapshot_ts) or pd.isna(asof_ts):
        return None
    return int((asof_ts.normalize() - snapshot_ts.normalize()).days)


def compute_component_scores(row: pd.Series, *, regime_name: str, setup: dict[str, Any]) -> dict[str, float]:
    intraday_usage_mode = get_intraday_usage_mode(setup)
    technical_rule_defs = list(setup.get("technical_rules", []))
    if intraday_usage_mode != "none":
        technical_rule_defs += list(setup.get("intraday_rules", []))
    technical_scores = [score_rule(row.get(rule["column"]), rule["operator"], rule["value"]) for rule in technical_rule_defs]
    if technical_scores:
        technical_score = average_score(technical_scores, default=0.0)
    else:
        technical_parts = [
            1.0 if bool(row.get("pass_above_dma_20")) else 0.0,
            1.0 if bool(row.get("pass_above_dma_50")) else 0.0,
            1.0 if bool(row.get("pass_above_dma_200")) else 0.0,
            score_rule(row.get("rs_vs_benchmark"), "gte", 0.0),
            score_rule(row.get("rs_vs_sector"), "gte", 0.0),
        ]
        intraday_available = any(
            not pd.isna(row.get(column))
            for column in [
                "intraday_close_vs_vwap_pct",
                "intraday_pct_bars_above_vwap",
                "intraday_close_location_pct",
                "intraday_volume_vs_20d",
                "intraday_breakout_score",
            ]
        )
        if intraday_available or any(
            bool(row.get(column))
            for column in [
                "intraday_opening_range_breakout_up",
                "intraday_prev_day_breakout_up",
                "intraday_failed_prev_day_breakout",
            ]
        ):
            technical_parts.append(
                average_score(
                    [
                        score_rule(row.get("intraday_close_vs_vwap_pct"), "gt", 0.0),
                        score_rule(row.get("intraday_pct_bars_above_vwap"), "gte", 0.55),
                        score_rule(row.get("intraday_close_location_pct"), "gte", 0.65),
                        1.0 if bool(row.get("intraday_opening_range_breakout_up")) else 0.25,
                        1.0 if bool(row.get("intraday_prev_day_breakout_up")) else 0.25,
                        0.0 if bool(row.get("intraday_failed_prev_day_breakout")) else 1.0,
                        score_rule(row.get("intraday_volume_vs_20d"), "gte", 0.8),
                        score_rule(row.get("intraday_breakout_score"), "gte", 0.55),
                    ]
                )
            )
        technical_score = average_score(technical_parts)

    fundamental_rules = setup.get("fundamental_rules", [])
    if fundamental_rules:
        fundamental_score = average_score([score_rule(row.get(rule["column"]), rule["operator"], rule["value"]) for rule in fundamental_rules], default=0.0)
    else:
        fundamental_score = average_score(
            [
                score_rule(row.get("total_revenue_qoq_growth_vs_sector"), "gte", 0.0),
                score_rule(row.get("profit_after_tax_qoq_growth_vs_sector"), "gte", 0.0),
                score_rule(row.get("debt_to_equity_vs_sector"), "lte", 0.25),
                score_rule(row.get("promoter_total_vs_sector"), "gte", 0.0),
                score_rule(row.get("fii_vs_sector"), "gte", 0.0),
            ]
        )

    regime_fit_score = compute_regime_fit_score(regime_name, setup)
    event_score = 0.5
    weights = {**DEFAULT_SCORING_WEIGHTS, **(setup.get("scoring_weights") or {})}
    total_weight = sum(float(value) for value in weights.values()) or 1.0
    setup_score = (
        (technical_score * float(weights["technical"]))
        + (fundamental_score * float(weights["fundamental"]))
        + (regime_fit_score * float(weights["regime_fit"]))
        + (event_score * float(weights["event"]))
    ) / total_weight
    return {
        "technical_score": round(technical_score, 6),
        "fundamental_score": round(fundamental_score, 6),
        "regime_fit_score": round(regime_fit_score, 6),
        "event_score": round(event_score, 6),
        "setup_score": round(setup_score, 6),
    }


def compute_invalidation_price(row: pd.Series) -> float | None:
    adj_close = safe_float(row.get("adj_close"))
    atr_20 = safe_float(row.get("atr_20"))
    dma_20 = safe_float(row.get("dma_20"))
    dma_50 = safe_float(row.get("dma_50"))
    anchors = [value for value in [dma_20, dma_50] if value is not None]
    if adj_close is not None and atr_20 is not None:
        anchors.append(adj_close - (2.0 * atr_20))
    if not anchors:
        return None
    return round(max(anchors), 2)


def build_entry_plan(row: pd.Series, candidate_state: str) -> dict[str, Any]:
    adj_close = safe_float(row.get("adj_close"))
    atr_20 = safe_float(row.get("atr_20")) or 0.0
    dma_20 = safe_float(row.get("dma_20"))
    dma_50 = safe_float(row.get("dma_50"))
    invalidation_price = compute_invalidation_price(row)

    if candidate_state == "WATCH_PULLBACK":
        anchor = dma_20 if dma_20 is not None and adj_close is not None and adj_close >= dma_20 else dma_50
        style = "PULLBACK_TO_20DMA" if anchor == dma_20 else "PULLBACK_TO_50DMA"
        if anchor is None:
            return {
                "entry_style": style,
                "attractive_price_low": None,
                "attractive_price_high": None,
                "invalidation_price": invalidation_price,
                "entry_note": "Wait for a pullback into the moving-average support zone.",
            }
        return {
            "entry_style": style,
            "attractive_price_low": round(anchor - (0.5 * atr_20), 2),
            "attractive_price_high": round(anchor + (0.25 * atr_20), 2),
            "invalidation_price": invalidation_price,
            "entry_note": "Good name but extended; wait for a controlled pullback into support.",
        }

    if candidate_state == "WATCH_BREAKOUT":
        if adj_close is None:
            return {
                "entry_style": "BREAKOUT_PIVOT",
                "attractive_price_low": None,
                "attractive_price_high": None,
                "invalidation_price": invalidation_price,
                "entry_note": "Setup forming; wait for a clean breakout trigger.",
            }
        return {
            "entry_style": "BREAKOUT_PIVOT",
            "attractive_price_low": round(adj_close + (0.1 * atr_20), 2),
            "attractive_price_high": round(adj_close + (0.6 * atr_20), 2),
            "invalidation_price": invalidation_price,
            "entry_note": "Quality setup forming; buy only on confirmation through the pivot zone.",
        }

    if candidate_state == "WATCH_EVENT":
        return {
            "entry_style": "RETEST_OF_PRIOR_BREAKOUT",
            "attractive_price_low": dma_20,
            "attractive_price_high": adj_close,
            "invalidation_price": invalidation_price,
            "entry_note": "Event-sensitive setup; wait for announcement, results, or order-flow confirmation.",
        }

    if candidate_state == "PASS_NOW":
        if adj_close is None:
            return {
                "entry_style": "BREAKOUT_PIVOT",
                "attractive_price_low": None,
                "attractive_price_high": None,
                "invalidation_price": invalidation_price,
                "entry_note": "Entry acceptable now subject to liquidity and execution review.",
            }
        return {
            "entry_style": "BREAKOUT_PLUS_EXTENSION_BAND",
            "attractive_price_low": round(max(adj_close - (0.25 * atr_20), 0.0), 2),
            "attractive_price_high": round(adj_close + (0.5 * atr_20), 2),
            "invalidation_price": invalidation_price,
            "entry_note": "Entry acceptable now within the current breakout band.",
        }

    if candidate_state == "ABSTAIN":
        return {
            "entry_style": None,
            "attractive_price_low": None,
            "attractive_price_high": None,
            "invalidation_price": invalidation_price,
            "entry_note": "Do nothing for now; edge is too weak or mixed to justify monitoring or allocation.",
        }

    return {
        "entry_style": None,
        "attractive_price_low": None,
        "attractive_price_high": None,
        "invalidation_price": invalidation_price,
        "entry_note": None,
    }


def evaluate_setup_row(row: pd.Series, *, regime_name: str, overlay_name: str, setup: dict[str, Any]) -> tuple[str, dict[str, Any], list[dict[str, Any]]]:
    thresholds = {**DEFAULT_SCORE_THRESHOLDS, **(setup.get("score_thresholds") or {})}
    freshness_policy = get_freshness_policy(setup)
    intraday_usage_mode = get_intraday_usage_mode(setup)
    rejections: list[dict[str, Any]] = []
    soft_failures: list[str] = []

    if regime_is_explicitly_blocked(regime_name, setup):
        rejections.append(build_rejection("regime_blocked", f"regime={regime_name}", severity="hard"))
    elif regime_name not in set(setup.get("allowed_regimes") or []):
        soft_failures.append(f"regime:{regime_name.lower()}_not_preferred")
    if overlay_is_explicitly_blocked(overlay_name, setup):
        rejections.append(build_rejection("overlay_not_allowed", f"overlay={overlay_name}", severity="hard"))
    elif not overlay_is_allowed(overlay_name, setup):
        soft_failures.append(f"overlay:{str(overlay_name or 'NONE').lower()}_not_preferred")

    if pd.isna(row.get("company_master_id")):
        rejections.append(build_rejection("missing_company_master_id", "company_master_id is null", severity="hard"))

    if pd.isna(row.get("adj_close")):
        rejections.append(build_rejection("missing_technical_snapshot", "technical snapshot missing for date", severity="hard"))

    technical_age_days = snapshot_age_days(row.get("technical_snapshot_date"), row.get("asof_date"))
    fundamentals_age_days = snapshot_age_days(row.get("fundamentals_snapshot_date"), row.get("asof_date"))
    intraday_age_days = snapshot_age_days(row.get("intraday_snapshot_date"), row.get("asof_date"))
    regime_age_days = snapshot_age_days(row.get("regime_snapshot_date"), row.get("asof_date"))

    if technical_age_days is not None and technical_age_days > int(freshness_policy["technical_max_age_days"]):
        soft_failures.append(f"technical:stale_{technical_age_days}d")
    if regime_age_days is not None and regime_age_days > int(freshness_policy["regime_max_age_days"]):
        soft_failures.append(f"regime:stale_{regime_age_days}d")

    fundamentals_required = bool(freshness_policy.get("fundamentals_required", True))
    if pd.isna(row.get("fundamentals_freshness_status")) and fundamentals_required:
        rejections.append(build_rejection("missing_fundamental_snapshot", "fundamental snapshot missing for date", severity="hard"))

    market_cap = safe_float(row.get("market_cap"))
    market_cap_min = safe_float(setup.get("market_cap_min"))
    market_cap_max = safe_float(setup.get("market_cap_max"))
    if market_cap_min is not None:
        if market_cap is None or market_cap < (market_cap_min * 0.70):
            rejections.append(build_rejection("market_cap_below_min", f"market_cap={market_cap}", severity="hard"))
        elif market_cap < market_cap_min:
            soft_failures.append("market_cap:below_target")
    if market_cap_max is not None:
        if market_cap is None or market_cap > (market_cap_max * 1.30):
            rejections.append(build_rejection("market_cap_above_max", f"market_cap={market_cap}", severity="hard"))
        elif market_cap > market_cap_max:
            soft_failures.append("market_cap:above_target")

    traded_value = safe_float(row.get("avg_traded_value_20d"))
    min_liquidity = safe_float(setup.get("min_avg_traded_value_20d"))
    if min_liquidity is not None and (traded_value is None or traded_value < (min_liquidity * 0.50)):
        rejections.append(build_rejection("liquidity_far_below_min", f"avg_traded_value_20d={traded_value}", severity="hard"))

    extension = safe_float(row.get("breakout_extension_pct"))
    max_extension = safe_float(setup.get("max_breakout_extension_pct"))
    if max_extension is not None and (extension is None or extension > (max_extension + 5.0)):
        rejections.append(build_rejection("overextended_breakout", f"breakout_extension_pct={extension}", severity="hard"))

    scores = compute_component_scores(row, regime_name=regime_name, setup=setup)
    score_gap = max(0.0, float(thresholds["pass_now"]) - float(scores["setup_score"]))
    near_miss_flag = score_gap > 0.0 and score_gap <= float(thresholds["near_miss_gap"])

    dist_52w_high = safe_float(row.get("dist_52w_high"))
    watch_pullback_extension_pct = safe_float(setup.get("watch_pullback_extension_pct"))
    if fundamentals_age_days is not None and fundamentals_age_days > int(freshness_policy["fundamentals_max_age_days"]):
        soft_failures.append(f"fundamental:stale_{fundamentals_age_days}d")
    intraday_missing = pd.isna(row.get("intraday_snapshot_date"))
    intraday_stale = intraday_age_days is not None and intraday_age_days > int(freshness_policy["intraday_max_age_days"])
    for rule in setup.get("technical_rules", []):
        if not compare(row.get(rule["column"]), rule["operator"], rule["value"]):
            soft_failures.append(f"technical:{rule['column']}")
    if intraday_usage_mode != "none":
        if intraday_missing:
            if intraday_usage_mode == "tactical_primary":
                soft_failures.append("intraday:missing")
        elif intraday_stale:
            if intraday_usage_mode == "tactical_primary":
                soft_failures.append(f"intraday:stale_{intraday_age_days}d")
        for rule in setup.get("intraday_rules", []):
            if not compare(row.get(rule["column"]), rule["operator"], rule["value"]):
                soft_failures.append(f"intraday:{rule['column']}")
    for rule in setup.get("fundamental_rules", []):
        if not compare(row.get(rule["column"]), rule["operator"], rule["value"]):
            soft_failures.append(f"fundamental:{rule['column']}")
    if min_liquidity is not None and traded_value is not None and traded_value < min_liquidity:
        soft_failures.append("liquidity:below_target")
    if setup.get("min_dist_52w_high") is not None and (dist_52w_high is None or dist_52w_high < float(setup["min_dist_52w_high"])):
        soft_failures.append("technical:too_far_from_high")

    hard_rejections = [item for item in rejections if item["severity"] == "hard"]
    if hard_rejections:
        return "REJECT", {**scores, "near_miss_flag": near_miss_flag}, hard_rejections

    intraday_rule_failures = [value for value in soft_failures if value.startswith("intraday:")]
    non_intraday_soft_failures = [value for value in soft_failures if not value.startswith("intraday:")]
    candidate_state = "REJECT"
    watch_reason_detail = None
    if max_extension is not None and extension is not None and extension > max_extension:
        candidate_state = "WATCH_PULLBACK"
        watch_reason_detail = f"extended now at {extension:.2f}% above breakout reference"
    elif watch_pullback_extension_pct is not None and extension is not None and extension > watch_pullback_extension_pct:
        candidate_state = "WATCH_PULLBACK"
        watch_reason_detail = f"extended enough to wait for pullback at {extension:.2f}%"
    elif float(scores["setup_score"]) >= float(thresholds["pass_now"]) and len(non_intraday_soft_failures) <= 2:
        candidate_state = "PASS_NOW"
        watch_reason_detail = "qualifies now with acceptable score and entry condition"
    elif float(scores["setup_score"]) >= float(thresholds["watch_breakout"]):
        candidate_state = "WATCH_BREAKOUT"
        watch_reason_detail = "quality setup forming but not fully triggered"
    elif float(scores["setup_score"]) >= float(thresholds["watch_event"]) and len(non_intraday_soft_failures) <= 2:
        candidate_state = "WATCH_EVENT"
        watch_reason_detail = "candidate needs event confirmation before entry"
    elif near_miss_flag:
        candidate_state = "WATCH_EVENT"
        watch_reason_detail = "near miss on score; keep on watch for improvement"
    elif float(scores["setup_score"]) >= float(thresholds.get("abstain", DEFAULT_SCORE_THRESHOLDS["abstain"])):
        candidate_state = "ABSTAIN"
        watch_reason_detail = "explicit abstain: setup is not broken, but edge is too weak or mixed to monitor actively"

    severe_intraday_miss = len(intraday_rule_failures) >= 2 or intraday_negative_signal(row)
    if intraday_usage_mode == "timing_only" and candidate_state == "PASS_NOW" and intraday_rule_failures:
        if severe_intraday_miss:
            candidate_state = "WATCH_BREAKOUT"
            watch_reason_detail = "daily setup qualifies but intraday timing confirmation is not ready"
        else:
            watch_reason_detail = "qualifies now; intraday timing is mixed but still acceptable"
    elif intraday_usage_mode == "confirm_only" and candidate_state == "PASS_NOW" and intraday_rule_failures:
        if severe_intraday_miss:
            candidate_state = "WATCH_BREAKOUT"
            watch_reason_detail = "daily setup is valid but intraday confirmation is still weak"
        else:
            watch_reason_detail = "qualifies now; intraday confirmation is mixed but not broken"
    elif intraday_usage_mode == "tactical_primary":
        if intraday_rule_failures:
            if float(scores["setup_score"]) >= float(thresholds["watch_breakout"]):
                candidate_state = "WATCH_BREAKOUT"
                watch_reason_detail = "intraday tactical trigger not fully confirmed yet"
            elif float(scores["setup_score"]) >= float(thresholds.get("abstain", DEFAULT_SCORE_THRESHOLDS["abstain"])):
                candidate_state = "ABSTAIN"
                watch_reason_detail = "explicit abstain: tactical trigger quality is too weak to monitor actively"
            else:
                candidate_state = "REJECT"

    if candidate_state == "ABSTAIN":
        rejections.append(
            build_rejection(
                "abstain_low_edge",
                f"setup_score={scores['setup_score']:.4f} watch_event={float(thresholds['watch_event']):.4f} soft_failures={len(soft_failures)}",
                severity="soft",
                is_near_miss=False,
                delta_to_pass=score_gap,
            )
        )
        return "ABSTAIN", {**scores, "near_miss_flag": near_miss_flag, "watch_reason_detail": watch_reason_detail}, rejections

    if candidate_state == "REJECT":
        rejections.append(
            build_rejection(
                "setup_score_below_threshold",
                f"setup_score={scores['setup_score']:.4f} pass_now={float(thresholds['pass_now']):.4f}",
                severity="soft",
                is_near_miss=near_miss_flag,
                delta_to_pass=score_gap,
            )
        )
        if len(soft_failures) == 1:
            rejections.append(build_rejection("single_rule_near_miss", soft_failures[0], severity="soft", is_near_miss=True, delta_to_pass=score_gap))
        return "REJECT", {**scores, "near_miss_flag": near_miss_flag}, rejections

    if candidate_state.startswith("WATCH_") and len(soft_failures) == 1 and watch_reason_detail:
        watch_reason_detail = f"{watch_reason_detail}; near miss on {soft_failures[0]}"

    return candidate_state, {**scores, "near_miss_flag": near_miss_flag, "watch_reason_detail": watch_reason_detail}, rejections


def run_rule_engine(
    *,
    asof_date: pd.Timestamp | None = None,
    setup_ids: list[str] | None = None,
    config_path: str | None = None,
    skip_intraday_prefetch: bool = False,
    max_snapshot_refresh_age_days: int = DEFAULT_MAX_SNAPSHOT_REFRESH_AGE_DAYS,
    max_intraday_prefetch_age_days: int = DEFAULT_MAX_INTRADAY_PREFETCH_AGE_DAYS,
) -> tuple[pd.DataFrame, pd.DataFrame, dict[str, Any]]:
    effective_dates = get_effective_dates(asof_date)
    screener_date = effective_dates.get("screener_date")
    if screener_date is None:
        return pd.DataFrame(), pd.DataFrame(), {"effective_date": None, "screener_date": None, "regime_name": None}

    regime = load_regime(screener_date)
    if regime is None:
        return pd.DataFrame(), pd.DataFrame(), {"effective_date": str(screener_date), "screener_date": str(screener_date), "regime_name": None}

    setups = load_setup_registry(config_path)
    if setup_ids:
        selected = {value.upper() for value in setup_ids}
        setups = [setup for setup in setups if setup["setup_id"].upper() in selected]
    else:
        setups = [setup for setup in setups if not bool(setup.get("research_only"))]

    technical = load_technical(screener_date)
    intraday = load_intraday(screener_date)
    fundamentals = load_fundamentals(screener_date)

    candidate_rows: list[dict[str, Any]] = []
    rejection_rows: list[dict[str, Any]] = []
    meta = {
        "effective_date": str(screener_date),
        "screener_date": str(screener_date),
        "regime_name": regime.get("regime_name"),
        "regime_snapshot_date": str(regime.get("asof_date")) if regime.get("asof_date") is not None else None,
        "technical_snapshot_date": str(technical["technical_snapshot_date"].max()) if not technical.empty and "technical_snapshot_date" in technical.columns else None,
        "intraday_snapshot_date": str(intraday["intraday_snapshot_date"].max()) if not intraday.empty and "intraday_snapshot_date" in intraday.columns else None,
        "fundamentals_snapshot_date": str(fundamentals["fundamentals_snapshot_date"].max()) if not fundamentals.empty and "fundamentals_snapshot_date" in fundamentals.columns else None,
    }
    overlay = load_overlay(pd.to_datetime(regime.get("asof_date"), utc=True, errors="coerce") if regime.get("asof_date") is not None else screener_date, regime_name=str(regime.get("regime_name") or ""))
    overlay_name = str(overlay.get("overlay_name") or "NONE").upper()
    theme_screener_mapping = load_active_theme_screener_mapping(asof_date=screener_date)
    meta["overlay_name"] = overlay_name
    meta["overlay_reason"] = overlay.get("overlay_reason")
    meta["overlay_intensity"] = overlay.get("overlay_intensity")
    meta["overlay_snapshot_date"] = str(overlay.get("asof_date")) if overlay.get("asof_date") is not None else None
    meta["active_theme_ids"] = theme_screener_mapping.get("theme_ids") or []
    meta["theme_screeners"] = theme_screener_mapping.get("screener_slugs") or []
    meta["theme_error"] = theme_screener_mapping.get("error")
    days_stale = _days_stale_from_today(screener_date)
    meta["days_stale_from_today"] = days_stale

    setup_screeners: dict[str, list[str]] = {}
    setup_screener_modes: dict[str, str] = {}
    screener_frames = []
    for setup in setups:
        active_screeners, screener_mode, active_theme_ids = resolve_setup_screeners(
            setup,
            overlay_name,
            theme_screener_mapping=theme_screener_mapping,
        )
        setup_screeners[setup["setup_id"].upper()] = active_screeners
        setup_screener_modes[setup["setup_id"].upper()] = screener_mode
        meta.setdefault("active_screeners_by_setup", {})[setup["setup_id"]] = active_screeners
        meta.setdefault("active_theme_ids_by_setup", {})[setup["setup_id"]] = active_theme_ids
        frame = load_screener_universe(screener_date, active_screeners, screener_mode=screener_mode)
        if not frame.empty:
            screener_frames.append(frame)
    screener_frames = [frame for frame in screener_frames if not frame.empty]
    if screener_frames:
        for frame in screener_frames:
            frame["asof_date"] = screener_date
        symbols_to_refresh = sorted(pd.concat(screener_frames, ignore_index=True)["symbol"].dropna().astype("string").str.upper().drop_duplicates().tolist())
        technical_symbols = technical["symbol"].dropna().astype("string").str.upper().drop_duplicates().tolist() if not technical.empty else []
        intraday_symbols = intraday["symbol"].dropna().astype("string").str.upper().drop_duplicates().tolist() if not intraday.empty else []
        fundamental_symbols = fundamentals["symbol"].dropna().astype("string").str.upper().drop_duplicates().tolist() if not fundamentals.empty else []
        available_symbols = set(technical_symbols).intersection(fundamental_symbols)
        missing_symbols = [symbol for symbol in symbols_to_refresh if symbol not in available_symbols]
        missing_intraday_symbols = [symbol for symbol in symbols_to_refresh if symbol not in set(intraday_symbols)]
        meta["missing_snapshot_symbols"] = missing_symbols
        meta["missing_intraday_symbols"] = missing_intraday_symbols
        allow_snapshot_refresh = (
            days_stale is None
            or int(max_snapshot_refresh_age_days) < 0
            or days_stale <= int(max_snapshot_refresh_age_days)
        )
        allow_intraday_prefetch = (
            not skip_intraday_prefetch
            and (
                days_stale is None
                or int(max_intraday_prefetch_age_days) < 0
                or days_stale <= int(max_intraday_prefetch_age_days)
            )
        )
        if missing_symbols and allow_snapshot_refresh:
            meta["preflight"] = refresh_missing_snapshots(missing_symbols, screener_date)
            technical = load_technical(screener_date)
            fundamentals = load_fundamentals(screener_date)
            intraday = load_intraday(screener_date)
        elif missing_symbols and not allow_snapshot_refresh:
            meta["preflight"] = {
                "status": "skipped",
                "reason": "historical_snapshot_refresh_disabled",
                "missing_snapshot_symbols": missing_symbols,
                "days_stale_from_today": days_stale,
                "max_snapshot_refresh_age_days": int(max_snapshot_refresh_age_days),
            }
        elif missing_intraday_symbols and allow_intraday_prefetch:
            intraday_df, intraday_meta = build_intraday_features(symbols=missing_intraday_symbols, asof_date=screener_date)
            persist_intraday_features(intraday_df, rebuild=False, asof_date=screener_date)
            intraday = load_intraday(screener_date)
            meta["intraday_preflight"] = intraday_meta
        elif missing_intraday_symbols and not allow_intraday_prefetch:
            meta["intraday_preflight"] = {
                "status": "skipped",
                "reason": "historical_intraday_prefetch_disabled" if not skip_intraday_prefetch else "skip_intraday_prefetch",
                "missing_intraday_symbols": missing_intraday_symbols,
                "days_stale_from_today": days_stale,
                "max_intraday_prefetch_age_days": int(max_intraday_prefetch_age_days),
            }

    regime_name = str(regime["regime_name"])
    for setup in setups:
        screener_slugs = setup_screeners.get(setup["setup_id"].upper(), [])
        universe = load_screener_universe(
            screener_date,
            screener_slugs,
            screener_mode=setup_screener_modes.get(setup["setup_id"].upper(), "union"),
        )
        if universe.empty:
            rejection_rows.append(
                {
                    "asof_date": screener_date,
                    "screener_date": screener_date,
                    "setup_id": setup["setup_id"],
                    "setup_name": setup["setup_name"],
                    "symbol": None,
                    "company_master_id": None,
                    "base_regime": regime_name,
                    "news_overlay": overlay_name,
                    "theme_ids": json.dumps(meta.get("active_theme_ids_by_setup", {}).get(setup["setup_id"], [])),
                    "reason_code": "missing_screener_universe",
                    "severity": "hard",
                    "is_near_miss": False,
                    "delta_to_pass": None,
                    "reason_detail": f"screener_slugs={screener_slugs}",
                    "load_ts": pd.Timestamp.utcnow(),
                }
            )
            continue
        universe = universe.copy()
        universe["asof_date"] = screener_date

        merged = universe.merge(
            technical.drop_duplicates(subset=["symbol"], keep="last"),
            on=["symbol", "company_master_id"],
            how="left",
            suffixes=("", "_tech"),
        )
        merged = merged.merge(
            fundamentals.drop_duplicates(subset=["symbol"], keep="last"),
            on=["symbol", "company_master_id"],
            how="left",
            suffixes=("", "_fund"),
        )
        if not intraday.empty:
            merged = merged.merge(
                intraday.drop_duplicates(subset=["symbol"], keep="last"),
                on=["symbol", "company_master_id"],
                how="left",
                suffixes=("", "_intraday"),
            )
        merged["regime_snapshot_date"] = pd.to_datetime(regime.get("asof_date"), utc=True, errors="coerce")

        for _, row in merged.iterrows():
            candidate_state, evaluation, rejections = evaluate_setup_row(row, regime_name=regime_name, overlay_name=overlay_name, setup=setup)
            if candidate_state != "REJECT":
                entry_plan = build_entry_plan(row, candidate_state)
                candidate_rows.append(
                    {
                        "asof_date": screener_date,
                        "screener_date": screener_date,
                        "setup_id": setup["setup_id"],
                        "setup_name": setup["setup_name"],
                        "setup_family": setup.get("setup_family"),
                        "holding_horizon_note": setup.get("holding_horizon_note"),
                        "regime_name": regime_name,
                        "base_regime": regime_name,
                        "news_overlay": overlay_name,
                        "theme_ids": json.dumps(meta.get("active_theme_ids_by_setup", {}).get(setup["setup_id"], [])),
                        "symbol": row["symbol"],
                        "company_master_id": row["company_master_id"],
                        "screener_slug": row.get("screener_slug"),
                        "source_screener_slug": row.get("source_screener_slug") or row.get("screener_slug"),
                        "source_screener_list": row.get("source_screener_list"),
                        "rank": row.get("rank"),
                        "candidate_state": candidate_state,
                        "watch_reason_detail": evaluation.get("watch_reason_detail"),
                        "technical_score": evaluation["technical_score"],
                        "fundamental_score": evaluation["fundamental_score"],
                        "regime_fit_score": evaluation["regime_fit_score"],
                        "event_score": evaluation["event_score"],
                        "setup_score": evaluation["setup_score"],
                        "avg_traded_value_20d": row.get("avg_traded_value_20d"),
                        "rs_vs_benchmark": row.get("rs_vs_benchmark"),
                        "rs_vs_sector": row.get("rs_vs_sector"),
                        "intraday_close_vs_vwap_pct": row.get("intraday_close_vs_vwap_pct"),
                        "intraday_pct_bars_above_vwap": row.get("intraday_pct_bars_above_vwap"),
                        "intraday_close_location_pct": row.get("intraday_close_location_pct"),
                        "intraday_opening_range_breakout_up": row.get("intraday_opening_range_breakout_up"),
                        "intraday_prev_day_breakout_up": row.get("intraday_prev_day_breakout_up"),
                        "intraday_failed_prev_day_breakout": row.get("intraday_failed_prev_day_breakout"),
                        "intraday_volume_vs_20d": row.get("intraday_volume_vs_20d"),
                        "intraday_breakout_score": row.get("intraday_breakout_score"),
                        "intraday_pattern_label": row.get("intraday_pattern_label"),
                        "intraday_interval_minutes": row.get("interval_minutes"),
                        "total_revenue_qoq_growth_vs_sector": row.get("total_revenue_qoq_growth_vs_sector"),
                        "profit_after_tax_qoq_growth_vs_sector": row.get("profit_after_tax_qoq_growth_vs_sector"),
                        "debt_to_equity_vs_sector": row.get("debt_to_equity_vs_sector"),
                        "entry_style": entry_plan["entry_style"],
                        "attractive_price_low": entry_plan["attractive_price_low"],
                        "attractive_price_high": entry_plan["attractive_price_high"],
                        "invalidation_price": entry_plan["invalidation_price"],
                        "entry_note": entry_plan["entry_note"],
                        "near_miss_flag": evaluation["near_miss_flag"],
                        "watch_enabled": True,
                        "watch_reasons": json.dumps(setup.get("watch_reasons", [])),
                        "rule_pass": candidate_state == "PASS_NOW",
                        "load_ts": pd.Timestamp.utcnow(),
                    }
                )
            for rejection in rejections:
                rejection_rows.append(
                    {
                        "asof_date": screener_date,
                        "screener_date": screener_date,
                        "setup_id": setup["setup_id"],
                        "setup_name": setup["setup_name"],
                        "symbol": row["symbol"],
                        "company_master_id": row["company_master_id"],
                        "base_regime": regime_name,
                        "news_overlay": overlay_name,
                        "reason_code": rejection["reason_code"],
                        "severity": rejection.get("severity"),
                        "is_near_miss": rejection.get("is_near_miss"),
                        "delta_to_pass": rejection.get("delta_to_pass"),
                        "reason_detail": rejection["reason_detail"],
                        "load_ts": pd.Timestamp.utcnow(),
                    }
                )

    candidates = pd.DataFrame(candidate_rows)
    if not candidates.empty:
        candidates = candidates.sort_values(["setup_id", "setup_score", "technical_score", "fundamental_score", "symbol"], ascending=[True, False, False, False, True]).copy()
        candidates["rank"] = candidates.groupby("setup_id").cumcount() + 1
    rejections_df = pd.DataFrame(rejection_rows)
    return candidates, rejections_df, meta


def persist_rule_outputs(candidates: pd.DataFrame, rejections: pd.DataFrame, *, asof_date: pd.Timestamp | None, rebuild: bool = False) -> None:
    ensure_rule_output_tables()
    if rebuild and asof_date is not None:
        with db_session() as (_, cur):
            cur.execute(f"DELETE FROM {CANDIDATES_TABLE} WHERE asof_date = %s", (asof_date,))
            cur.execute(f"DELETE FROM {REJECTIONS_TABLE} WHERE asof_date = %s", (asof_date,))
    if not candidates.empty:
        candidates = candidates.copy()
        for column in [
            "intraday_opening_range_breakout_up",
            "intraday_prev_day_breakout_up",
            "intraday_failed_prev_day_breakout",
            "near_miss_flag",
            "watch_enabled",
            "rule_pass",
        ]:
            if column in candidates.columns:
                candidates[column] = candidates[column].map(
                    lambda value: None if pd.isna(value) else bool(value)
                ).astype("boolean")
        upsert_to_db(candidates, CANDIDATES_TABLE, unique_keys=["asof_date", "setup_id", "symbol"], timescaledb_column="asof_date")
    if not rejections.empty:
        rejections = rejections.copy()
        if "is_near_miss" in rejections.columns:
            rejections["is_near_miss"] = rejections["is_near_miss"].map(
                lambda value: None if pd.isna(value) else str(value).strip().lower() in {"1", "true", "t", "yes", "y"}
            ).astype("boolean")
        if "delta_to_pass" in rejections.columns:
            rejections["delta_to_pass"] = pd.to_numeric(rejections["delta_to_pass"], errors="coerce")
        if "severity" in rejections.columns:
            rejections["severity"] = rejections["severity"].astype("string")
        if "reason_code" in rejections.columns:
            rejections["reason_code"] = rejections["reason_code"].astype("string")
        if "reason_detail" in rejections.columns:
            rejections["reason_detail"] = rejections["reason_detail"].astype("string")
        upsert_to_db(rejections, REJECTIONS_TABLE, unique_keys=["asof_date", "setup_id", "symbol", "reason_code"], timescaledb_column="asof_date")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Apply setup rules to advisory snapshots.")
    parser.add_argument("--date", type=parse_datetime_arg, help="Asof date in YYYY-MM-DD")
    parser.add_argument("--setup", dest="setup_ids", nargs="*", help="Setup ids to evaluate")
    parser.add_argument("--config", help="Override setup registry YAML path")
    parser.add_argument("--skip-intraday-prefetch", action="store_true", help="Skip on-demand intraday feature backfill inside the rule engine")
    parser.add_argument("--rebuild", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def summarize(candidates: pd.DataFrame, rejections: pd.DataFrame, meta: dict[str, Any]) -> dict[str, Any]:
    return {
        "status": "ok",
        "candidates_table": CANDIDATES_TABLE,
        "rejections_table": REJECTIONS_TABLE,
        "effective_date": meta.get("effective_date"),
        "screener_date": meta.get("screener_date"),
        "snapshot_dates": {
            "regime": meta.get("regime_snapshot_date"),
            "overlay": meta.get("overlay_snapshot_date"),
            "technicals": meta.get("technical_snapshot_date"),
            "intraday": meta.get("intraday_snapshot_date"),
            "fundamentals": meta.get("fundamentals_snapshot_date"),
        },
        "regime_name": meta.get("regime_name"),
        "overlay_name": meta.get("overlay_name"),
        "overlay_reason": meta.get("overlay_reason"),
        "candidate_count": int(len(candidates)),
        "rejection_count": int(len(rejections)),
        "candidate_state_counts": candidates["candidate_state"].value_counts().to_dict() if not candidates.empty and "candidate_state" in candidates.columns else {},
        "avg_setup_score": None if candidates.empty else round(float(candidates["setup_score"].mean()), 6),
        "candidate_sample": candidates.head(10).to_dict(orient="records") if not candidates.empty else [],
        "top_rejections": rejections["reason_code"].value_counts().head(10).to_dict() if not rejections.empty else {},
    }


def main() -> int:
    args = parse_args()
    asof_date = pd.Timestamp(args.date, tz="UTC") if args.date else None
    candidates, rejections, meta = run_rule_engine(
        asof_date=asof_date,
        setup_ids=args.setup_ids,
        config_path=args.config,
        skip_intraday_prefetch=bool(args.skip_intraday_prefetch),
    )
    effective_date = pd.to_datetime(meta.get("effective_date"), utc=True, errors="coerce")
    if not args.dry_run:
        persist_rule_outputs(candidates, rejections, asof_date=None if pd.isna(effective_date) else effective_date, rebuild=args.rebuild)
    result = summarize(candidates, rejections, meta)
    result["dry_run"] = bool(args.dry_run)
    print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
