from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from typing import Any

import pandas as pd

from advisory.setup_registry import load_setup_registry
from utils.db import db_session, sql_to_df, upsert_to_db
from utils.sync import parse_datetime_arg


ALLOCATIONS_TABLE = "advisory_allocations"
_TRANSITION_CUTOFF = 0.12
_MATERIALITY_ORDER = {"low": 1, "medium": 2, "high": 3}
_RISK_ORDER = {"none": 0, "low": 1, "medium": 2, "high": 3}


@dataclass(frozen=True)
class SetupRiskProfile:
    base_risk_bucket: str
    max_allocation_inr: int
    invalidation_anchor: str


SETUP_RISK_PROFILES: dict[str, SetupRiskProfile] = {
    "SME_MOMENTUM_V1": SetupRiskProfile(
        base_risk_bucket="high",
        max_allocation_inr=25_000,
        invalidation_anchor="dma_20",
    ),
    "LARGECAP_BREAKOUT_V1": SetupRiskProfile(
        base_risk_bucket="medium",
        max_allocation_inr=100_000,
        invalidation_anchor="dma_50",
    ),
    "DEFENSIVE_TARIFF_V1": SetupRiskProfile(
        base_risk_bucket="medium",
        max_allocation_inr=75_000,
        invalidation_anchor="dma_50",
    ),
    "MIDCAP_IMPROVER_V1": SetupRiskProfile(
        base_risk_bucket="medium_high",
        max_allocation_inr=50_000,
        invalidation_anchor="dma_20",
    ),
    "SME_TACTICAL_SWING_V1": SetupRiskProfile(
        base_risk_bucket="high",
        max_allocation_inr=25_000,
        invalidation_anchor="dma_20",
    ),
    "MIDCAP_IMPROVER_SWING_V1": SetupRiskProfile(
        base_risk_bucket="medium_high",
        max_allocation_inr=50_000,
        invalidation_anchor="dma_20",
    ),
    "LARGECAP_BREAKOUT_POSITION_V1": SetupRiskProfile(
        base_risk_bucket="medium",
        max_allocation_inr=100_000,
        invalidation_anchor="dma_50",
    ),
    "DEFENSIVE_REGIME_POSITION_V1": SetupRiskProfile(
        base_risk_bucket="medium",
        max_allocation_inr=75_000,
        invalidation_anchor="dma_50",
    ),
}

RISK_BUCKET_ORDER = ["low", "medium", "medium_high", "high"]
RISK_BUCKET_MULTIPLIER = {
    "low": 1.00,
    "medium": 0.85,
    "medium_high": 0.70,
    "high": 0.50,
}
CONVICTION_MULTIPLIER = {
    "low": 0.40,
    "medium": 0.70,
    "high": 1.00,
}


def get_setup_risk_profiles() -> dict[str, SetupRiskProfile]:
    profiles = dict(SETUP_RISK_PROFILES)
    for setup in load_setup_registry():
        risk_profile = dict(setup.get("risk_profile") or {})
        if not risk_profile:
            continue
        setup_id = str(setup.get("setup_id") or "").upper()
        if not setup_id:
            continue
        profiles[setup_id] = SetupRiskProfile(
            base_risk_bucket=str(risk_profile.get("base_risk_bucket") or "medium_high"),
            max_allocation_inr=int(risk_profile.get("max_allocation_inr") or 40_000),
            invalidation_anchor=str(risk_profile.get("invalidation_anchor") or "dma_20"),
        )
    return profiles


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


def ensure_allocations_table() -> None:
    with db_session() as (_, cur):
        cur.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {ALLOCATIONS_TABLE} (
                published_on TIMESTAMPTZ NOT NULL,
                asof_date TIMESTAMPTZ,
                allocated_at TIMESTAMPTZ,
                setup_id TEXT NOT NULL,
                setup_name TEXT,
                symbol TEXT NOT NULL,
                company_master_id TEXT,
                unique_id TEXT NOT NULL,
                evaluation_status TEXT,
                evaluation_verdict TEXT,
                investable_now BOOLEAN,
                materiality TEXT,
                setup_effect TEXT,
                event_class TEXT,
                state_transition_hint TEXT,
                score_impact DOUBLE PRECISION,
                confidence DOUBLE PRECISION,
                risk_bucket TEXT,
                conviction_bucket TEXT,
                allocation_status TEXT,
                suggested_allocation_inr DOUBLE PRECISION,
                allocation_pct_of_adv20d DOUBLE PRECISION,
                stop_price DOUBLE PRECISION,
                invalidation_price DOUBLE PRECISION,
                invalidation_rule TEXT,
                notes TEXT,
                context_snapshot_json TEXT,
                load_ts TIMESTAMPTZ,
                UNIQUE (published_on, setup_id, symbol, unique_id)
            )
            """
        )
        column_defs = {
            "asof_date": "TIMESTAMPTZ",
            "allocated_at": "TIMESTAMPTZ",
            "setup_name": "TEXT",
            "company_master_id": "TEXT",
            "evaluation_status": "TEXT",
            "evaluation_verdict": "TEXT",
            "investable_now": "BOOLEAN",
            "materiality": "TEXT",
            "setup_effect": "TEXT",
            "event_class": "TEXT",
            "state_transition_hint": "TEXT",
            "score_impact": "DOUBLE PRECISION",
            "confidence": "DOUBLE PRECISION",
            "risk_bucket": "TEXT",
            "conviction_bucket": "TEXT",
            "allocation_status": "TEXT",
            "suggested_allocation_inr": "DOUBLE PRECISION",
            "allocation_pct_of_adv20d": "DOUBLE PRECISION",
            "stop_price": "DOUBLE PRECISION",
            "invalidation_price": "DOUBLE PRECISION",
            "invalidation_rule": "TEXT",
            "notes": "TEXT",
            "context_snapshot_json": "TEXT",
            "load_ts": "TIMESTAMPTZ",
        }
        for column, sql_type in column_defs.items():
            cur.execute(f"ALTER TABLE {ALLOCATIONS_TABLE} ADD COLUMN IF NOT EXISTS {column} {sql_type}")


def load_event_evaluations(
    *,
    asof_date: pd.Timestamp | None = None,
    symbols: list[str] | None = None,
    setup_ids: list[str] | None = None,
    include_allocated: bool = False,
) -> pd.DataFrame:
    if not table_exists("advisory_event_evaluations"):
        return pd.DataFrame()

    clauses = ["1 = 1"]
    params: list[object] = []
    if asof_date is not None:
        clauses.append("e.asof_date = %s")
        params.append(asof_date)
    else:
        clauses.append("e.asof_date = (SELECT MAX(asof_date) FROM advisory_event_evaluations)")
    if symbols:
        clauses.append("e.symbol = ANY(%s)")
        params.append([value.upper() for value in symbols])
    if setup_ids:
        clauses.append("e.setup_id = ANY(%s)")
        params.append([value.upper() for value in setup_ids])

    alloc_join = ""
    alloc_select = ""
    if table_exists(ALLOCATIONS_TABLE):
        alloc_join = f"""
        LEFT JOIN {ALLOCATIONS_TABLE} a
          ON a.published_on = e.published_on
         AND a.setup_id = e.setup_id
         AND a.symbol = e.symbol
         AND a.unique_id = e.unique_id
        """
        alloc_select = ", a.unique_id AS allocated_unique_id"
        if not include_allocated:
            clauses.append("a.unique_id IS NULL")

    df = sql_to_df(
        f"""
        SELECT
            e.*
            {alloc_select}
        FROM advisory_event_evaluations e
        {alloc_join}
        WHERE {' AND '.join(clauses)}
        ORDER BY e.published_on, e.setup_id, e.symbol, e.unique_id
        """,
        params=tuple(params) if params else None,
    )
    if df.empty:
        return df
    df["asof_date"] = normalize_timestamp(df["asof_date"])
    df["published_on"] = pd.to_datetime(df["published_on"], utc=True, errors="coerce")
    df["symbol"] = df["symbol"].astype("string").str.upper()
    df["score_impact"] = pd.to_numeric(df["score_impact"], errors="coerce").fillna(0.0)

    def _pick_level(values: pd.Series, order: dict[str, int], default: str) -> str:
        cleaned = [str(value).lower() for value in values if str(value).strip()]
        if not cleaned:
            return default
        return max(cleaned, key=lambda item: order.get(item, -1))

    rows: list[dict[str, Any]] = []
    for (asof_key, setup_id, symbol), group in df.groupby(["asof_date", "setup_id", "symbol"], dropna=False, sort=False):
        group = group.sort_values(["published_on"], ascending=[True], kind="stable")
        latest = group.iloc[-1].to_dict()
        hints = {str(value).upper() for value in group["state_transition_hint"].dropna().astype(str)}
        verdicts = {str(value).lower() for value in group["verdict"].dropna().astype(str)}
        total_score_impact = round(max(-0.35, min(0.35, float(group["score_impact"].sum()))), 4)

        if "DOWNGRADE_TO_REJECT" in hints or "reject" in verdicts:
            transition_hint = "DOWNGRADE_TO_REJECT"
        elif "UPGRADE_TO_PASS_NOW" in hints:
            transition_hint = "UPGRADE_TO_PASS_NOW"
        elif total_score_impact >= _TRANSITION_CUTOFF:
            transition_hint = "RAISE_SCORE_ONLY"
        elif total_score_impact <= -_TRANSITION_CUTOFF:
            transition_hint = "CUT_SCORE_ONLY"
        elif "REVIEW_MANUAL" in hints:
            transition_hint = "REVIEW_MANUAL"
        else:
            transition_hint = "NO_CHANGE"

        if "reject" in verdicts:
            verdict = "reject"
        elif "continue" in verdicts:
            verdict = "continue"
        elif "review_manual" in verdicts:
            verdict = "review_manual"
        else:
            verdict = str(latest.get("verdict") or "review_manual")

        any_investable = bool(pd.Series(group.get("investable_now")).fillna(False).astype(bool).any())
        if verdict == "reject":
            investable_now = False
        elif transition_hint == "DOWNGRADE_TO_REJECT":
            investable_now = False
        else:
            investable_now = any_investable

        if total_score_impact >= _TRANSITION_CUTOFF:
            setup_effect = "strengthens"
            sentiment = "positive"
        elif total_score_impact <= -_TRANSITION_CUTOFF:
            setup_effect = "weakens"
            sentiment = "negative"
        else:
            setup_effect = str(latest.get("setup_effect") or "neutral")
            sentiment = str(latest.get("sentiment") or "neutral")

        merged = dict(latest)
        merged["asof_date"] = asof_key
        merged["setup_id"] = setup_id
        merged["symbol"] = symbol
        merged["published_on"] = latest.get("published_on")
        merged["event_class"] = latest.get("event_class")
        merged["state_transition_hint"] = transition_hint
        merged["score_impact"] = total_score_impact
        merged["verdict"] = verdict
        merged["investable_now"] = investable_now
        merged["setup_effect"] = setup_effect
        merged["sentiment"] = sentiment
        merged["materiality"] = _pick_level(group["materiality"], _MATERIALITY_ORDER, str(latest.get("materiality") or "low"))
        merged["governance_risk"] = _pick_level(group["governance_risk"], _RISK_ORDER, str(latest.get("governance_risk") or "none"))
        merged["balance_sheet_risk"] = _pick_level(group["balance_sheet_risk"], _RISK_ORDER, str(latest.get("balance_sheet_risk") or "none"))
        merged["execution_risk"] = _pick_level(group["execution_risk"], _RISK_ORDER, str(latest.get("execution_risk") or "none"))
        merged["confidence"] = float(pd.to_numeric(group["confidence"], errors="coerce").dropna().max()) if not pd.to_numeric(group["confidence"], errors="coerce").dropna().empty else latest.get("confidence")
        merged["has_review_manual"] = "REVIEW_MANUAL" in hints or "review_manual" in verdicts
        rows.append(merged)

    return pd.DataFrame(rows)


def load_watch_states(
    *,
    asof_date: pd.Timestamp | None = None,
    symbols: list[str] | None = None,
    setup_ids: list[str] | None = None,
) -> pd.DataFrame:
    if not table_exists("advisory_watchlist"):
        return pd.DataFrame()

    clauses = ["1 = 1"]
    params: list[object] = []
    if asof_date is not None:
        clauses.append("asof_date = %s")
        params.append(asof_date)
    else:
        clauses.append("asof_date = (SELECT MAX(asof_date) FROM advisory_watchlist)")
    if symbols:
        clauses.append("symbol = ANY(%s)")
        params.append([value.upper() for value in symbols])
    if setup_ids:
        clauses.append("setup_id = ANY(%s)")
        params.append([value.upper() for value in setup_ids])

    df = sql_to_df(
        f"""
        SELECT
            asof_date,
            setup_id,
            symbol,
            candidate_state,
            current_state,
            watch_status
        FROM advisory_watchlist
        WHERE {' AND '.join(clauses)}
        ORDER BY asof_date, setup_id, symbol
        """,
        params=tuple(params) if params else None,
    )
    if df.empty:
        return df
    df["asof_date"] = normalize_timestamp(df["asof_date"])
    df["symbol"] = df["symbol"].astype("string").str.upper()
    return df


def load_base_candidate_fallbacks(
    *,
    asof_date: pd.Timestamp | None = None,
    symbols: list[str] | None = None,
    setup_ids: list[str] | None = None,
    include_allocated: bool = False,
) -> pd.DataFrame:
    if not table_exists("advisory_watchlist"):
        return pd.DataFrame()

    clauses = ["w.current_state IN ('PASS_NOW', 'WATCH_BREAKOUT')"]
    params: list[object] = []
    if asof_date is not None:
        clauses.append("w.asof_date = %s")
        params.append(asof_date)
    else:
        clauses.append("w.asof_date = (SELECT MAX(asof_date) FROM advisory_watchlist)")
    if symbols:
        clauses.append("w.symbol = ANY(%s)")
        params.append([value.upper() for value in symbols])
    if setup_ids:
        clauses.append("w.setup_id = ANY(%s)")
        params.append([value.upper() for value in setup_ids])

    df = sql_to_df(
        f"""
        SELECT
            w.asof_date,
            w.setup_id,
            c.setup_name,
            w.symbol,
            c.company_master_id,
            w.candidate_state,
            w.current_state
        FROM advisory_watchlist w
        LEFT JOIN advisory_candidates c
          ON c.asof_date = w.asof_date
         AND c.setup_id = w.setup_id
         AND c.symbol = w.symbol
        WHERE {' AND '.join(clauses)}
        ORDER BY w.asof_date, w.setup_id, w.symbol
        """,
        params=tuple(params) if params else None,
    )
    if df.empty:
        return df
    df["asof_date"] = normalize_timestamp(df["asof_date"])
    df["symbol"] = df["symbol"].astype("string").str.upper()
    df["published_on"] = pd.to_datetime(df["asof_date"], utc=True, errors="coerce")
    df["unique_id"] = df.apply(
        lambda row: f"candidate:{pd.to_datetime(row['asof_date'], utc=True, errors='coerce').date()}:{str(row['setup_id']).upper()}:{str(row['symbol']).upper()}",
        axis=1,
    )
    if table_exists(ALLOCATIONS_TABLE) and not include_allocated:
        existing_clauses = ["1 = 1"]
        existing_params: list[object] = []
        if asof_date is not None:
            existing_clauses.append("asof_date = %s")
            existing_params.append(asof_date)
        existing = sql_to_df(
            f"""
            SELECT asof_date, setup_id, symbol, unique_id
            FROM {ALLOCATIONS_TABLE}
            WHERE {' AND '.join(existing_clauses)}
            """,
            params=tuple(existing_params) if existing_params else None,
        )
        if not existing.empty:
            existing["asof_date"] = normalize_timestamp(existing["asof_date"])
            existing["symbol"] = existing["symbol"].astype("string").str.upper()
            existing_keys = {
                (
                    pd.to_datetime(item["asof_date"], utc=True, errors="coerce"),
                    str(item["setup_id"]).upper(),
                    str(item["symbol"]).upper(),
                    str(item["unique_id"]),
                )
                for item in existing.to_dict(orient="records")
            }
            df = df[
                ~df.apply(
                    lambda row: (
                        pd.to_datetime(row["asof_date"], utc=True, errors="coerce"),
                        str(row["setup_id"]).upper(),
                        str(row["symbol"]).upper(),
                        str(row["unique_id"]),
                    )
                    in existing_keys,
                    axis=1,
                )
            ]
    if df.empty:
        return df

    def _fallback_confidence(current_state: Any) -> float:
        state = str(current_state or "").upper()
        if state == "PASS_NOW":
            return 0.60
        if state == "WATCH_BREAKOUT":
            return 0.45
        return 0.30

    rows: list[dict[str, Any]] = []
    for _, row in df.iterrows():
        current_state = str(row.get("current_state") or "").upper()
        rows.append(
            {
                "published_on": row["published_on"],
                "asof_date": row["asof_date"],
                "setup_id": row["setup_id"],
                "setup_name": row.get("setup_name"),
                "symbol": row["symbol"],
                "company_master_id": row.get("company_master_id"),
                "unique_id": row["unique_id"],
                "evaluation_status": "completed",
                "verdict": "continue",
                "investable_now": current_state in {"PASS_NOW", "WATCH_BREAKOUT"},
                "materiality": "low",
                "setup_effect": "neutral",
                "event_class": "BASE_CANDIDATE",
                "state_transition_hint": "NO_CHANGE",
                "score_impact": 0.0,
                "confidence": _fallback_confidence(current_state),
                "sentiment": "neutral",
                "governance_risk": "none",
                "balance_sheet_risk": "none",
                "execution_risk": "none",
                "has_review_manual": False,
                "candidate_state": row.get("candidate_state"),
                "current_state": current_state,
                "watch_status": "active",
                "is_base_candidate_fallback": True,
            }
        )
    return pd.DataFrame(rows)


def load_point_in_time_context(symbol: str, published_on: pd.Timestamp) -> dict[str, Any]:
    df = sql_to_df(
        """
        SELECT
            tech.asof_date AS technical_asof_date,
            tech.adj_close,
            tech.dma_20,
            tech.dma_50,
            tech.dma_200,
            tech.atr_20,
            tech.avg_traded_value_20d,
            tech.rs_vs_benchmark,
            tech.rs_vs_sector,
            tech.breakout_extension_pct,
            fund.asof_date AS fundamentals_asof_date,
            fund.debt_to_equity,
            fund.debt_to_equity_vs_sector
        FROM (
            SELECT *
            FROM advisory_technical_daily
            WHERE symbol = %(symbol)s
              AND asof_date <= %(published_on)s
            ORDER BY asof_date DESC
            LIMIT 1
        ) tech
        LEFT JOIN (
            SELECT *
            FROM advisory_fundamentals_daily
            WHERE symbol = %(symbol)s
              AND asof_date <= %(published_on)s
            ORDER BY asof_date DESC
            LIMIT 1
        ) fund
          ON TRUE
        """,
        params={"symbol": symbol.upper(), "published_on": published_on},
    )
    if df.empty:
        return {}
    out = df.iloc[0].to_dict()
    for key in ["technical_asof_date", "fundamentals_asof_date"]:
        if key in out and pd.notna(out[key]):
            out[key] = pd.to_datetime(out[key], utc=True, errors="coerce").isoformat()
    return out


def clamp_risk_bucket(bucket: str) -> str:
    if bucket not in RISK_BUCKET_ORDER:
        return "medium_high"
    return bucket


def bump_risk_bucket(bucket: str, steps: int = 1) -> str:
    idx = RISK_BUCKET_ORDER.index(clamp_risk_bucket(bucket))
    return RISK_BUCKET_ORDER[min(len(RISK_BUCKET_ORDER) - 1, idx + max(steps, 0))]


def compute_risk_bucket(row: pd.Series, context: dict[str, Any]) -> str:
    profile = get_setup_risk_profiles().get(str(row["setup_id"]).upper(), SetupRiskProfile("medium_high", 40_000, "dma_20"))
    bucket = profile.base_risk_bucket

    if str(row.get("governance_risk", "none")) in {"high"}:
        return "high"
    if any(str(row.get(field, "none")) in {"medium", "high"} for field in ["governance_risk", "balance_sheet_risk", "execution_risk"]):
        bucket = bump_risk_bucket(bucket, 1)

    avg_traded_value_20d = pd.to_numeric(context.get("avg_traded_value_20d"), errors="coerce")
    if pd.notna(avg_traded_value_20d):
        if avg_traded_value_20d < 2_500_000:
            bucket = bump_risk_bucket(bucket, 1)
        if avg_traded_value_20d < 1_000_000:
            bucket = "high"

    breakout_extension_pct = pd.to_numeric(context.get("breakout_extension_pct"), errors="coerce")
    if pd.notna(breakout_extension_pct) and breakout_extension_pct > 8:
        bucket = bump_risk_bucket(bucket, 1)

    debt_to_equity_vs_sector = pd.to_numeric(context.get("debt_to_equity_vs_sector"), errors="coerce")
    if pd.notna(debt_to_equity_vs_sector) and debt_to_equity_vs_sector > 0.5:
        bucket = bump_risk_bucket(bucket, 1)

    event_class = str(row.get("event_class", "")).upper()
    score_impact = pd.to_numeric(row.get("score_impact"), errors="coerce")
    if event_class in {"AUDITOR_GOVERNANCE", "DILUTION", "PLEDGE_UP", "GUIDANCE_DOWNGRADE", "RESULTS_NEGATIVE", "POLICY_SECTOR_NEGATIVE"}:
        bucket = bump_risk_bucket(bucket, 1)
    if pd.notna(score_impact) and score_impact <= -0.20:
        bucket = bump_risk_bucket(bucket, 1)

    return clamp_risk_bucket(bucket)


def compute_conviction_bucket(row: pd.Series, context: dict[str, Any]) -> str:
    score = 0.0
    confidence = pd.to_numeric(row.get("confidence"), errors="coerce")
    score += (0.0 if pd.isna(confidence) else float(confidence)) * 2.0

    materiality = str(row.get("materiality", "")).lower()
    setup_effect = str(row.get("setup_effect", "")).lower()
    sentiment = str(row.get("sentiment", "")).lower()

    if materiality == "high":
        score += 1.25
    elif materiality == "medium":
        score += 0.75

    if setup_effect == "strengthens":
        score += 1.0
    elif setup_effect == "neutral":
        score += 0.25
    elif setup_effect in {"weakens", "contradicts"}:
        score -= 1.0

    if sentiment == "positive":
        score += 0.5
    elif sentiment == "mixed":
        score -= 0.25
    elif sentiment == "negative":
        score -= 1.0

    score_impact = pd.to_numeric(row.get("score_impact"), errors="coerce")
    if pd.notna(score_impact):
        score += float(score_impact) * 2.0

    state_transition_hint = str(row.get("state_transition_hint", "")).upper()
    if state_transition_hint == "UPGRADE_TO_PASS_NOW":
        score += 0.75
    elif state_transition_hint == "DOWNGRADE_TO_REJECT":
        score -= 1.25
    elif state_transition_hint == "RAISE_SCORE_ONLY":
        score += 0.30
    elif state_transition_hint == "CUT_SCORE_ONLY":
        score -= 0.50

    rs_vs_benchmark = pd.to_numeric(context.get("rs_vs_benchmark"), errors="coerce")
    rs_vs_sector = pd.to_numeric(context.get("rs_vs_sector"), errors="coerce")
    adj_close = pd.to_numeric(context.get("adj_close"), errors="coerce")
    dma_20 = pd.to_numeric(context.get("dma_20"), errors="coerce")
    dma_50 = pd.to_numeric(context.get("dma_50"), errors="coerce")
    dma_200 = pd.to_numeric(context.get("dma_200"), errors="coerce")

    if pd.notna(rs_vs_benchmark) and rs_vs_benchmark > 0:
        score += 0.4
    if pd.notna(rs_vs_sector) and rs_vs_sector > 0:
        score += 0.4
    if pd.notna(adj_close) and pd.notna(dma_20) and adj_close >= dma_20:
        score += 0.25
    if pd.notna(adj_close) and pd.notna(dma_50) and adj_close >= dma_50:
        score += 0.25
    if pd.notna(adj_close) and pd.notna(dma_200) and adj_close >= dma_200:
        score += 0.25

    if score >= 3.25:
        return "high"
    if score >= 1.8:
        return "medium"
    return "low"


def compute_invalidation(profile: SetupRiskProfile, context: dict[str, Any]) -> tuple[float | None, float | None, str]:
    adj_close = pd.to_numeric(context.get("adj_close"), errors="coerce")
    atr_20 = pd.to_numeric(context.get("atr_20"), errors="coerce")
    dma_20 = pd.to_numeric(context.get("dma_20"), errors="coerce")
    dma_50 = pd.to_numeric(context.get("dma_50"), errors="coerce")

    anchor_value = dma_20 if profile.invalidation_anchor == "dma_20" else dma_50
    candidates = []
    if pd.notna(anchor_value):
        candidates.append(float(anchor_value))
    if pd.notna(adj_close) and pd.notna(atr_20):
        candidates.append(float(adj_close - (2.0 * atr_20)))
    if not candidates:
        return None, None, "Technical invalidation unavailable: missing ATR/DMA context."

    invalidation_price = max(candidates)
    stop_price = invalidation_price
    rule = f"Invalidate if close breaks below {profile.invalidation_anchor} or roughly 2 ATR from current price."
    return round(stop_price, 2), round(invalidation_price, 2), rule


def round_allocation(value: float) -> float:
    if value <= 0:
        return 0.0
    return float(int(value // 1000) * 1000)


def build_allocations(
    *,
    asof_date: pd.Timestamp | None = None,
    symbols: list[str] | None = None,
    setup_ids: list[str] | None = None,
    include_allocated: bool = False,
) -> pd.DataFrame:
    evaluations = load_event_evaluations(
        asof_date=asof_date,
        symbols=symbols,
        setup_ids=setup_ids,
        include_allocated=include_allocated,
    )
    base_candidates = load_base_candidate_fallbacks(
        asof_date=asof_date,
        symbols=symbols,
        setup_ids=setup_ids,
        include_allocated=include_allocated,
    )
    if not base_candidates.empty:
        if evaluations.empty:
            evaluations = base_candidates
        else:
            existing_keys = {
                (pd.to_datetime(row["asof_date"], utc=True, errors="coerce"), str(row["setup_id"]).upper(), str(row["symbol"]).upper())
                for row in evaluations[["asof_date", "setup_id", "symbol"]].to_dict(orient="records")
            }
            base_candidates = base_candidates[
                ~base_candidates.apply(
                    lambda row: (
                        pd.to_datetime(row["asof_date"], utc=True, errors="coerce"),
                        str(row["setup_id"]).upper(),
                        str(row["symbol"]).upper(),
                    )
                    in existing_keys,
                    axis=1,
                )
            ]
            if not base_candidates.empty:
                evaluations = pd.concat([evaluations, base_candidates], ignore_index=True, sort=False)
    if evaluations.empty:
        return pd.DataFrame()
    watch_states = load_watch_states(asof_date=asof_date, symbols=symbols, setup_ids=setup_ids)
    if not watch_states.empty:
        evaluations = evaluations.merge(
            watch_states[["asof_date", "setup_id", "symbol", "candidate_state", "current_state", "watch_status"]],
            on=["asof_date", "setup_id", "symbol"],
            how="left",
        )

    setup_profiles = get_setup_risk_profiles()
    rows: list[dict[str, Any]] = []
    for _, row in evaluations.iterrows():
        setup_id = str(row["setup_id"]).upper()
        profile = setup_profiles.get(setup_id, SetupRiskProfile("medium_high", 40_000, "dma_20"))
        context = load_point_in_time_context(str(row["symbol"]), pd.to_datetime(row["published_on"], utc=True, errors="coerce"))
        risk_bucket = compute_risk_bucket(row, context)
        conviction_bucket = compute_conviction_bucket(row, context)
        stop_price, invalidation_price, invalidation_rule = compute_invalidation(profile, context)

        verdict = str(row.get("verdict", "")).lower()
        evaluation_status = str(row.get("evaluation_status", "")).lower()
        investable_now = bool(row.get("investable_now", False))
        current_state = str(row.get("current_state", "")).upper()
        event_class = str(row.get("event_class", "")).upper()
        state_transition_hint = str(row.get("state_transition_hint", "")).upper()
        score_impact_raw = pd.to_numeric(row.get("score_impact"), errors="coerce")
        score_impact = 0.0 if pd.isna(score_impact_raw) else float(score_impact_raw)
        is_base_candidate_fallback = bool(row.get("is_base_candidate_fallback", False))
        promoted_to_pass_now = current_state == "PASS_NOW" and state_transition_hint != "DOWNGRADE_TO_REJECT" and verdict != "reject"
        actionable_now = investable_now or promoted_to_pass_now

        notes: list[str] = []
        if is_base_candidate_fallback:
            notes.append("Allocation is based on base rule-engine state because no event evaluation was available.")
            if current_state == "WATCH_BREAKOUT":
                notes.append("Entry timing is still early; size remains conservative until confirmation improves.")
        if evaluation_status != "completed":
            allocation_status = "review_manual"
            suggested_allocation_inr = 0.0
            notes.append(f"LLM evaluation status is {evaluation_status}.")
        elif state_transition_hint == "DOWNGRADE_TO_REJECT":
            allocation_status = "rejected"
            suggested_allocation_inr = 0.0
            notes.append(f"Event transition {state_transition_hint} blocked automatic allocation.")
        elif verdict == "review_manual":
            if not actionable_now:
                allocation_status = "rejected"
                suggested_allocation_inr = 0.0
                notes.append("Event evaluation does not support a fresh allocation.")
            else:
                raw_cap = float(profile.max_allocation_inr)
                liquidity_cap = pd.to_numeric(context.get("avg_traded_value_20d"), errors="coerce")
                if pd.notna(liquidity_cap):
                    liquidity_cap = float(liquidity_cap) * 0.005
                else:
                    liquidity_cap = raw_cap
                    notes.append("Liquidity cap fallback used because ADV20 was missing.")

                suggested_allocation_inr = min(
                    raw_cap * CONVICTION_MULTIPLIER[conviction_bucket] * RISK_BUCKET_MULTIPLIER[risk_bucket],
                    liquidity_cap,
                )
                event_multiplier = max(0.50, min(1.25, 1.0 + score_impact))
                suggested_allocation_inr = suggested_allocation_inr * event_multiplier
                suggested_allocation_inr = round_allocation(suggested_allocation_inr)
                allocation_status = "allocated" if suggested_allocation_inr > 0 else "review_manual"
                notes.append("LLM requested manual review; keep this flagged for operator review.")
                if promoted_to_pass_now and not investable_now:
                    notes.append("Watchlist promotion to PASS_NOW overrode a conservative event investable flag.")
        elif verdict == "reject" or not actionable_now:
            allocation_status = "rejected"
            suggested_allocation_inr = 0.0
            notes.append("Event evaluation does not support a fresh allocation.")
        elif str(row.get("governance_risk", "none")) == "high":
            allocation_status = "review_manual"
            suggested_allocation_inr = 0.0
            notes.append("High governance risk blocks automatic allocation.")
        else:
            raw_cap = float(profile.max_allocation_inr)
            liquidity_cap = pd.to_numeric(context.get("avg_traded_value_20d"), errors="coerce")
            if pd.notna(liquidity_cap):
                liquidity_cap = float(liquidity_cap) * 0.005
            else:
                liquidity_cap = raw_cap
                notes.append("Liquidity cap fallback used because ADV20 was missing.")

            suggested_allocation_inr = min(
                raw_cap * CONVICTION_MULTIPLIER[conviction_bucket] * RISK_BUCKET_MULTIPLIER[risk_bucket],
                liquidity_cap,
            )
            if is_base_candidate_fallback and current_state == "WATCH_BREAKOUT":
                suggested_allocation_inr = suggested_allocation_inr * 0.60
            event_multiplier = max(0.50, min(1.25, 1.0 + score_impact))
            suggested_allocation_inr = suggested_allocation_inr * event_multiplier
            suggested_allocation_inr = round_allocation(suggested_allocation_inr)

            if suggested_allocation_inr <= 0:
                allocation_status = "review_manual"
                notes.append("Computed allocation rounded down to zero.")
            else:
                allocation_status = "allocated"
                if state_transition_hint:
                    notes.append(f"Event transition: {state_transition_hint}.")
                if event_class:
                    notes.append(f"Event class: {event_class}.")
                if promoted_to_pass_now and not investable_now:
                    notes.append("Watchlist promotion to PASS_NOW overrode a conservative event investable flag.")
                if bool(row.get("has_review_manual", False)):
                    notes.append("Another recent event still requires manual review.")

        adv20 = pd.to_numeric(context.get("avg_traded_value_20d"), errors="coerce")
        allocation_pct_of_adv20d = None
        if pd.notna(adv20) and adv20 > 0 and suggested_allocation_inr > 0:
            allocation_pct_of_adv20d = round(float(suggested_allocation_inr) / float(adv20), 6)

        rows.append(
            {
                "published_on": row["published_on"],
                "asof_date": row["asof_date"],
                "allocated_at": pd.Timestamp.utcnow(),
                "setup_id": row["setup_id"],
                "setup_name": row.get("setup_name"),
                "symbol": row["symbol"],
                "company_master_id": row.get("company_master_id"),
                "unique_id": row["unique_id"],
                "evaluation_status": row.get("evaluation_status"),
                "evaluation_verdict": row.get("verdict"),
                "investable_now": row.get("investable_now"),
                "materiality": row.get("materiality"),
                "setup_effect": row.get("setup_effect"),
                "event_class": row.get("event_class"),
                "state_transition_hint": row.get("state_transition_hint"),
                "score_impact": row.get("score_impact"),
                "confidence": row.get("confidence"),
                "risk_bucket": risk_bucket,
                "conviction_bucket": conviction_bucket,
                "allocation_status": allocation_status,
                "suggested_allocation_inr": suggested_allocation_inr,
                "allocation_pct_of_adv20d": allocation_pct_of_adv20d,
                "stop_price": stop_price,
                "invalidation_price": invalidation_price,
                "invalidation_rule": invalidation_rule,
                "notes": " ".join(notes) if notes else None,
                "context_snapshot_json": json.dumps(context, ensure_ascii=False, default=str, sort_keys=True),
                "load_ts": pd.Timestamp.utcnow(),
            }
        )

    return pd.DataFrame(rows)


def persist_allocations(df: pd.DataFrame) -> None:
    ensure_allocations_table()
    if df.empty:
        return
    with db_session() as (_, cur):
        pairs = (
            df[["asof_date", "setup_id"]]
            .dropna()
            .drop_duplicates()
            .to_dict(orient="records")
        )
        for item in pairs:
            cur.execute(
                f"DELETE FROM {ALLOCATIONS_TABLE} WHERE asof_date = %s AND setup_id = %s",
                (
                    pd.to_datetime(item["asof_date"], utc=True, errors="coerce").to_pydatetime(),
                    str(item["setup_id"]),
                ),
            )
    upsert_to_db(
        df,
        ALLOCATIONS_TABLE,
        unique_keys=["published_on", "setup_id", "symbol", "unique_id"],
        timescaledb_column="published_on",
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build advisory risk buckets and allocations from evaluated watch events.")
    parser.add_argument("--date", type=parse_datetime_arg, help="Event asof date in YYYY-MM-DD")
    parser.add_argument("--symbols", nargs="*", help="Optional symbols")
    parser.add_argument("--setup", dest="setup_ids", nargs="*", help="Optional setup ids")
    parser.add_argument("--include-allocated", action="store_true", help="Rebuild rows that already exist in advisory_allocations")
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def summarize(df: pd.DataFrame) -> dict[str, Any]:
    if df.empty:
        return {
            "status": "ok",
            "table": ALLOCATIONS_TABLE,
            "row_count": 0,
            "allocated_count": 0,
            "review_count": 0,
            "rejected_count": 0,
            "sample": [],
        }
    return {
        "status": "ok",
        "table": ALLOCATIONS_TABLE,
        "row_count": int(len(df)),
        "allocated_count": int((df["allocation_status"] == "allocated").sum()),
        "review_count": int((df["allocation_status"] == "review_manual").sum()),
        "rejected_count": int((df["allocation_status"] == "rejected").sum()),
        "sample": df.head(10).to_dict(orient="records"),
    }


def main() -> int:
    args = parse_args()
    asof_date = pd.Timestamp(args.date, tz="UTC") if args.date else None
    df = build_allocations(
        asof_date=asof_date,
        symbols=args.symbols,
        setup_ids=args.setup_ids,
        include_allocated=bool(args.include_allocated),
    )
    if not args.dry_run:
        persist_allocations(df)
    result = summarize(df)
    result["dry_run"] = bool(args.dry_run)
    print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
