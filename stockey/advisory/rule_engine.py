from __future__ import annotations

import argparse
import json
from typing import Any

import pandas as pd

from advisory.data_sync import ensure_advisory_symbol_inputs
from advisory.fundamental_snapshot import build_fundamental_snapshot, persist_fundamental_snapshot
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
    "near_miss_gap": 0.05,
}


def normalize_timestamp(series: pd.Series) -> pd.Series:
    return pd.to_datetime(series, utc=True, errors="coerce").dt.normalize()


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


def get_effective_dates(asof_date: pd.Timestamp | None = None) -> tuple[pd.Timestamp | None, pd.Timestamp | None]:
    screener_cutoff = asof_date
    if screener_cutoff is None:
        screener_df = sql_to_df("SELECT MAX(date) AS screener_date FROM advisory_screener_constituents")
        if screener_df.empty or pd.isna(pd.to_datetime(screener_df.iloc[0]["screener_date"], utc=True, errors="coerce")):
            return None, None
        screener_cutoff = pd.to_datetime(screener_df.iloc[0]["screener_date"], utc=True, errors="coerce").normalize()

    df = sql_to_df(
        """
        SELECT
            (SELECT MAX(asof_date) FROM advisory_market_regime WHERE asof_date <= %(asof_date)s) AS regime_date,
            (SELECT MAX(asof_date) FROM advisory_technical_daily WHERE asof_date <= %(asof_date)s) AS technical_date,
            (SELECT MAX(asof_date) FROM advisory_fundamentals_daily WHERE asof_date <= %(asof_date)s) AS fundamentals_date,
            (SELECT MAX(date) FROM advisory_screener_constituents WHERE date <= %(asof_date)s) AS screener_date
        """,
        params={"asof_date": screener_cutoff},
    )
    if df.empty:
        return None, None
    row = df.iloc[0]
    screener_date = pd.to_datetime(row.get("screener_date"), utc=True, errors="coerce")
    regime_date = pd.to_datetime(row.get("regime_date"), utc=True, errors="coerce")
    technical_date = pd.to_datetime(row.get("technical_date"), utc=True, errors="coerce")
    fundamentals_date = pd.to_datetime(row.get("fundamentals_date"), utc=True, errors="coerce")
    if any(pd.isna(value) for value in [screener_date, regime_date, technical_date, fundamentals_date]):
        return None, None
    evaluation_date = min(regime_date.normalize(), technical_date.normalize(), fundamentals_date.normalize())
    return screener_date.normalize(), evaluation_date


def load_regime(asof_date: pd.Timestamp) -> dict[str, Any] | None:
    df = sql_to_df(
        """
        SELECT *
        FROM advisory_market_regime
        WHERE asof_date = %s
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
            WHERE asof_date = %s
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


def resolve_setup_screeners(setup: dict[str, Any], overlay_name: str | None) -> tuple[list[str], str]:
    base_screeners = [str(value) for value in (setup.get("screeners") or setup.get("screener_slugs") or []) if value]
    if not base_screeners and setup.get("screener_slug"):
        base_screeners = [str(setup["screener_slug"])]
    overlay_cfg = (setup.get("overlay_screeners") or {}).get(str(overlay_name or "NONE").upper(), {})
    add = [str(value) for value in (overlay_cfg.get("add") or []) if value]
    remove = {str(value) for value in (overlay_cfg.get("remove") or []) if value}
    active = [value for value in base_screeners if value not in remove]
    for value in add:
        if value not in active:
            active.append(value)
    return active, str(setup.get("screener_mode") or "union").lower()


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
        SELECT *
        FROM advisory_technical_daily
        WHERE asof_date = %s
        """,
        params=(asof_date,),
    )
    if df.empty:
        return df
    df["asof_date"] = normalize_timestamp(df["asof_date"])
    df["symbol"] = df["symbol"].astype("string").str.upper()
    return df


def load_fundamentals(asof_date: pd.Timestamp) -> pd.DataFrame:
    df = sql_to_df(
        """
        SELECT *
        FROM advisory_fundamentals_daily
        WHERE asof_date = %s
        """,
        params=(asof_date,),
    )
    if df.empty:
        return df
    df["asof_date"] = normalize_timestamp(df["asof_date"])
    df["symbol"] = df["symbol"].astype("string").str.upper()
    return df


def refresh_missing_snapshots(symbols: list[str], effective_date: pd.Timestamp) -> dict[str, object]:
    sync_result = ensure_advisory_symbol_inputs(symbols, to_date=effective_date)
    peer_sync_result = sync_peer_data(symbols=symbols, to_date=effective_date)

    technical_df = build_technical_features(symbols=symbols, from_date=effective_date, to_date=effective_date, rebuild=False)
    persist_technical_features(technical_df, rebuild=False, symbols=symbols)

    fundamentals_df = build_fundamental_snapshot(symbols=symbols, from_date=effective_date, to_date=effective_date, rebuild=False)
    persist_fundamental_snapshot(fundamentals_df)

    return {
        "data_sync": sync_result,
        "peer_sync": peer_sync_result,
        "technical_rows": int(len(technical_df)),
        "fundamental_rows": int(len(fundamentals_df)),
    }


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


def compute_component_scores(row: pd.Series, *, regime_name: str, setup: dict[str, Any]) -> dict[str, float]:
    technical_scores = [score_rule(row.get(rule["column"]), rule["operator"], rule["value"]) for rule in setup.get("technical_rules", [])]
    if technical_scores:
        technical_score = average_score(technical_scores, default=0.0)
    else:
        technical_score = average_score(
            [
                1.0 if bool(row.get("pass_above_dma_20")) else 0.0,
                1.0 if bool(row.get("pass_above_dma_50")) else 0.0,
                1.0 if bool(row.get("pass_above_dma_200")) else 0.0,
                score_rule(row.get("rs_vs_benchmark"), "gte", 0.0),
                score_rule(row.get("rs_vs_sector"), "gte", 0.0),
            ]
        )

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

    return {
        "entry_style": None,
        "attractive_price_low": None,
        "attractive_price_high": None,
        "invalidation_price": invalidation_price,
        "entry_note": None,
    }


def evaluate_setup_row(row: pd.Series, *, regime_name: str, overlay_name: str, setup: dict[str, Any]) -> tuple[str, dict[str, Any], list[dict[str, Any]]]:
    thresholds = {**DEFAULT_SCORE_THRESHOLDS, **(setup.get("score_thresholds") or {})}
    rejections: list[dict[str, Any]] = []

    if regime_name not in set(setup.get("allowed_regimes") or []):
        rejections.append(build_rejection("regime_not_allowed", f"regime={regime_name}", severity="hard"))
    if regime_name in {str(value) for value in (setup.get("blocked_regimes") or []) if value}:
        rejections.append(build_rejection("regime_blocked", f"regime={regime_name}", severity="hard"))
    if not overlay_is_allowed(overlay_name, setup):
        rejections.append(build_rejection("overlay_not_allowed", f"overlay={overlay_name}", severity="hard"))

    if pd.isna(row.get("company_master_id")):
        rejections.append(build_rejection("missing_company_master_id", "company_master_id is null", severity="hard"))

    if pd.isna(row.get("adj_close")):
        rejections.append(build_rejection("missing_technical_snapshot", "technical snapshot missing for date", severity="hard"))

    if pd.isna(row.get("fundamentals_freshness_status")):
        rejections.append(build_rejection("missing_fundamental_snapshot", "fundamental snapshot missing for date", severity="hard"))

    market_cap = safe_float(row.get("market_cap"))
    if setup.get("market_cap_min") is not None and (market_cap is None or market_cap < float(setup["market_cap_min"])):
        rejections.append(build_rejection("market_cap_below_min", f"market_cap={market_cap}", severity="hard"))
    if setup.get("market_cap_max") is not None and (market_cap is None or market_cap > float(setup["market_cap_max"])):
        rejections.append(build_rejection("market_cap_above_max", f"market_cap={market_cap}", severity="hard"))

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

    soft_failures: list[str] = []
    for rule in setup.get("technical_rules", []):
        if not compare(row.get(rule["column"]), rule["operator"], rule["value"]):
            soft_failures.append(f"technical:{rule['column']}")
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

    candidate_state = "REJECT"
    watch_reason_detail = None
    if max_extension is not None and extension is not None and extension > max_extension:
        candidate_state = "WATCH_PULLBACK"
        watch_reason_detail = f"extended now at {extension:.2f}% above breakout reference"
    elif watch_pullback_extension_pct is not None and extension is not None and extension > watch_pullback_extension_pct:
        candidate_state = "WATCH_PULLBACK"
        watch_reason_detail = f"extended enough to wait for pullback at {extension:.2f}%"
    elif float(scores["setup_score"]) >= float(thresholds["pass_now"]) and len(soft_failures) <= 1:
        candidate_state = "PASS_NOW"
        watch_reason_detail = "qualifies now with acceptable score and entry condition"
    elif float(scores["setup_score"]) >= float(thresholds["watch_breakout"]):
        candidate_state = "WATCH_BREAKOUT"
        watch_reason_detail = "quality setup forming but not fully triggered"
    elif float(scores["setup_score"]) >= float(thresholds["watch_event"]):
        candidate_state = "WATCH_EVENT"
        watch_reason_detail = "candidate needs event confirmation before entry"
    elif near_miss_flag:
        candidate_state = "WATCH_EVENT"
        watch_reason_detail = "near miss on score; keep on watch for improvement"

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


def run_rule_engine(*, asof_date: pd.Timestamp | None = None, setup_ids: list[str] | None = None, config_path: str | None = None) -> tuple[pd.DataFrame, pd.DataFrame, dict[str, Any]]:
    screener_date, effective_date = get_effective_dates(asof_date)
    if effective_date is None or screener_date is None:
        return pd.DataFrame(), pd.DataFrame(), {"effective_date": None, "screener_date": None, "regime_name": None}

    regime = load_regime(effective_date)
    if regime is None:
        return pd.DataFrame(), pd.DataFrame(), {"effective_date": str(effective_date), "screener_date": str(screener_date), "regime_name": None}

    setups = load_setup_registry(config_path)
    if setup_ids:
        selected = {value.upper() for value in setup_ids}
        setups = [setup for setup in setups if setup["setup_id"].upper() in selected]

    technical = load_technical(effective_date)
    fundamentals = load_fundamentals(effective_date)

    candidate_rows: list[dict[str, Any]] = []
    rejection_rows: list[dict[str, Any]] = []
    meta = {
        "effective_date": str(effective_date),
        "screener_date": str(screener_date),
        "regime_name": regime.get("regime_name"),
    }
    overlay = load_overlay(effective_date, regime_name=str(regime.get("regime_name") or ""))
    overlay_name = str(overlay.get("overlay_name") or "NONE").upper()
    meta["overlay_name"] = overlay_name
    meta["overlay_reason"] = overlay.get("overlay_reason")
    meta["overlay_intensity"] = overlay.get("overlay_intensity")

    setup_screeners: dict[str, list[str]] = {}
    setup_screener_modes: dict[str, str] = {}
    screener_frames = []
    for setup in setups:
        active_screeners, screener_mode = resolve_setup_screeners(setup, overlay_name)
        setup_screeners[setup["setup_id"].upper()] = active_screeners
        setup_screener_modes[setup["setup_id"].upper()] = screener_mode
        meta.setdefault("active_screeners_by_setup", {})[setup["setup_id"]] = active_screeners
        frame = load_screener_universe(screener_date, active_screeners, screener_mode=screener_mode)
        if not frame.empty:
            screener_frames.append(frame)
    screener_frames = [frame for frame in screener_frames if not frame.empty]
    if screener_frames:
        for frame in screener_frames:
            frame["asof_date"] = effective_date
        symbols_to_refresh = sorted(pd.concat(screener_frames, ignore_index=True)["symbol"].dropna().astype("string").str.upper().drop_duplicates().tolist())
        technical_symbols = technical["symbol"].dropna().astype("string").str.upper().drop_duplicates().tolist() if not technical.empty else []
        fundamental_symbols = fundamentals["symbol"].dropna().astype("string").str.upper().drop_duplicates().tolist() if not fundamentals.empty else []
        available_symbols = set(technical_symbols).intersection(fundamental_symbols)
        missing_symbols = [symbol for symbol in symbols_to_refresh if symbol not in available_symbols]
        meta["missing_snapshot_symbols"] = missing_symbols
        if missing_symbols:
            meta["preflight"] = refresh_missing_snapshots(missing_symbols, effective_date)
            technical = load_technical(effective_date)
            fundamentals = load_fundamentals(effective_date)

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
                    "asof_date": effective_date,
                    "screener_date": screener_date,
                    "setup_id": setup["setup_id"],
                    "setup_name": setup["setup_name"],
                    "symbol": None,
                    "company_master_id": None,
                    "base_regime": regime_name,
                    "news_overlay": overlay_name,
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
        universe["asof_date"] = effective_date

        merged = universe.merge(
            technical.drop_duplicates(subset=["asof_date", "symbol"], keep="last"),
            on=["asof_date", "symbol", "company_master_id"],
            how="left",
            suffixes=("", "_tech"),
        )
        merged = merged.merge(
            fundamentals.drop_duplicates(subset=["asof_date", "symbol", "company_master_id"], keep="last"),
            on=["asof_date", "symbol", "company_master_id"],
            how="left",
            suffixes=("", "_fund"),
        )

        for _, row in merged.iterrows():
            candidate_state, evaluation, rejections = evaluate_setup_row(row, regime_name=regime_name, overlay_name=overlay_name, setup=setup)
            if candidate_state != "REJECT":
                entry_plan = build_entry_plan(row, candidate_state)
                candidate_rows.append(
                    {
                        "asof_date": effective_date,
                        "screener_date": screener_date,
                        "setup_id": setup["setup_id"],
                        "setup_name": setup["setup_name"],
                        "setup_family": setup.get("setup_family"),
                        "holding_horizon_note": setup.get("holding_horizon_note"),
                        "regime_name": regime_name,
                        "base_regime": regime_name,
                        "news_overlay": overlay_name,
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
                        "asof_date": effective_date,
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
        upsert_to_db(candidates, CANDIDATES_TABLE, unique_keys=["asof_date", "setup_id", "symbol"], timescaledb_column="asof_date")
    if not rejections.empty:
        upsert_to_db(rejections, REJECTIONS_TABLE, unique_keys=["asof_date", "setup_id", "symbol", "reason_code"], timescaledb_column="asof_date")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Apply setup rules to advisory snapshots.")
    parser.add_argument("--date", type=parse_datetime_arg, help="Asof date in YYYY-MM-DD")
    parser.add_argument("--setup", dest="setup_ids", nargs="*", help="Setup ids to evaluate")
    parser.add_argument("--config", help="Override setup registry YAML path")
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
    candidates, rejections, meta = run_rule_engine(asof_date=asof_date, setup_ids=args.setup_ids, config_path=args.config)
    effective_date = pd.to_datetime(meta.get("effective_date"), utc=True, errors="coerce")
    if not args.dry_run:
        persist_rule_outputs(candidates, rejections, asof_date=None if pd.isna(effective_date) else effective_date, rebuild=args.rebuild)
    result = summarize(candidates, rejections, meta)
    result["dry_run"] = bool(args.dry_run)
    print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
