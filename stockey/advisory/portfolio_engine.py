from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from typing import Any

import pandas as pd
from sqlalchemy.exc import SQLAlchemyError

from advisory.decision_trace import append_trace, append_trace_step, safe_trace_call
from advisory.fallback_telemetry import record_local_fallback_event
from advisory.setup_registry import load_setup_registry
from utils.db import db_session, execute_db_operation, sql_to_df, upsert_to_db
from utils.display_time import to_display_value
from utils.schema_migrations import apply_schema_migration
from utils.sync import parse_datetime_arg


ALLOCATIONS_TABLE = "advisory_allocations"
PORTFOLIO_TABLE = "advisory_portfolio_orders"
PORTFOLIO_SCHEMA_MIGRATION_ID = "20260611_advisory_portfolio_orders_base"

DEFAULT_PORTFOLIO_CAPITAL_INR = 300_000.0
DEFAULT_MAX_POSITIONS = 5
DEFAULT_SINGLE_POSITION_CAP_PCT = 0.35
DEFAULT_SETUP_CAP_PCT = 0.50
DEFAULT_MAX_POSITIONS_PER_OVERLAP_GROUP = 1

CONVICTION_SCORE = {
    "low": 1.0,
    "medium": 2.0,
    "high": 3.0,
}
RISK_PENALTY = {
    "low": 0.0,
    "medium": 0.2,
    "medium_high": 0.5,
    "high": 1.0,
}

PORTFOLIO_SCHEMA_STATEMENTS = [
    f"""
    CREATE TABLE IF NOT EXISTS {PORTFOLIO_TABLE} (
        published_on TIMESTAMPTZ NOT NULL,
        source_published_on TIMESTAMPTZ,
        asof_date TIMESTAMPTZ,
        planned_at TIMESTAMPTZ,
        setup_id TEXT NOT NULL,
        setup_name TEXT,
        symbol TEXT NOT NULL,
        company_master_id TEXT,
        unique_id TEXT NOT NULL,
        portfolio_capital_inr DOUBLE PRECISION,
        max_positions BIGINT,
        plan_rank BIGINT,
        portfolio_status TEXT,
        portfolio_reason TEXT,
        priority_score DOUBLE PRECISION,
        overlap_group TEXT,
        overlap_reason TEXT,
        event_class TEXT,
        state_transition_hint TEXT,
        score_impact DOUBLE PRECISION,
        requested_allocation_inr DOUBLE PRECISION,
        approved_allocation_inr DOUBLE PRECISION,
        remaining_capital_after_inr DOUBLE PRECISION,
        stop_price DOUBLE PRECISION,
        invalidation_price DOUBLE PRECISION,
        invalidation_rule TEXT,
        execution_notes TEXT,
        context_snapshot_json TEXT,
        load_ts TIMESTAMPTZ,
        UNIQUE (published_on, setup_id, symbol, unique_id)
    )
    """,
    f"ALTER TABLE {PORTFOLIO_TABLE} ADD COLUMN IF NOT EXISTS source_published_on TIMESTAMPTZ",
    f"ALTER TABLE {PORTFOLIO_TABLE} ADD COLUMN IF NOT EXISTS setup_name TEXT",
    f"ALTER TABLE {PORTFOLIO_TABLE} ADD COLUMN IF NOT EXISTS company_master_id TEXT",
    f"ALTER TABLE {PORTFOLIO_TABLE} ADD COLUMN IF NOT EXISTS portfolio_capital_inr DOUBLE PRECISION",
    f"ALTER TABLE {PORTFOLIO_TABLE} ADD COLUMN IF NOT EXISTS max_positions BIGINT",
    f"ALTER TABLE {PORTFOLIO_TABLE} ADD COLUMN IF NOT EXISTS plan_rank BIGINT",
    f"ALTER TABLE {PORTFOLIO_TABLE} ADD COLUMN IF NOT EXISTS portfolio_status TEXT",
    f"ALTER TABLE {PORTFOLIO_TABLE} ADD COLUMN IF NOT EXISTS portfolio_reason TEXT",
    f"ALTER TABLE {PORTFOLIO_TABLE} ADD COLUMN IF NOT EXISTS priority_score DOUBLE PRECISION",
    f"ALTER TABLE {PORTFOLIO_TABLE} ADD COLUMN IF NOT EXISTS overlap_group TEXT",
    f"ALTER TABLE {PORTFOLIO_TABLE} ADD COLUMN IF NOT EXISTS overlap_reason TEXT",
    f"ALTER TABLE {PORTFOLIO_TABLE} ADD COLUMN IF NOT EXISTS event_class TEXT",
    f"ALTER TABLE {PORTFOLIO_TABLE} ADD COLUMN IF NOT EXISTS state_transition_hint TEXT",
    f"ALTER TABLE {PORTFOLIO_TABLE} ADD COLUMN IF NOT EXISTS score_impact DOUBLE PRECISION",
    f"ALTER TABLE {PORTFOLIO_TABLE} ADD COLUMN IF NOT EXISTS requested_allocation_inr DOUBLE PRECISION",
    f"ALTER TABLE {PORTFOLIO_TABLE} ADD COLUMN IF NOT EXISTS approved_allocation_inr DOUBLE PRECISION",
    f"ALTER TABLE {PORTFOLIO_TABLE} ADD COLUMN IF NOT EXISTS remaining_capital_after_inr DOUBLE PRECISION",
    f"ALTER TABLE {PORTFOLIO_TABLE} ADD COLUMN IF NOT EXISTS stop_price DOUBLE PRECISION",
    f"ALTER TABLE {PORTFOLIO_TABLE} ADD COLUMN IF NOT EXISTS invalidation_price DOUBLE PRECISION",
    f"ALTER TABLE {PORTFOLIO_TABLE} ADD COLUMN IF NOT EXISTS invalidation_rule TEXT",
    f"ALTER TABLE {PORTFOLIO_TABLE} ADD COLUMN IF NOT EXISTS execution_notes TEXT",
    f"ALTER TABLE {PORTFOLIO_TABLE} ADD COLUMN IF NOT EXISTS context_snapshot_json TEXT",
    f"ALTER TABLE {PORTFOLIO_TABLE} ADD COLUMN IF NOT EXISTS load_ts TIMESTAMPTZ",
    f"ALTER TABLE {PORTFOLIO_TABLE} ADD COLUMN IF NOT EXISTS thesis_bucket TEXT",
    f"ALTER TABLE {PORTFOLIO_TABLE} ADD COLUMN IF NOT EXISTS bucket_reason TEXT",
    f"ALTER TABLE {PORTFOLIO_TABLE} ADD COLUMN IF NOT EXISTS target_price DOUBLE PRECISION",
    f"ALTER TABLE {PORTFOLIO_TABLE} ADD COLUMN IF NOT EXISTS target_basis TEXT",
    f"ALTER TABLE {PORTFOLIO_TABLE} ADD COLUMN IF NOT EXISTS target_confidence DOUBLE PRECISION",
    f"ALTER TABLE {PORTFOLIO_TABLE} ADD COLUMN IF NOT EXISTS target_review_date TIMESTAMPTZ",
    f"ALTER TABLE {PORTFOLIO_TABLE} ADD COLUMN IF NOT EXISTS expected_horizon_days BIGINT",
    f"ALTER TABLE {PORTFOLIO_TABLE} ADD COLUMN IF NOT EXISTS horizon_type TEXT",
    f"ALTER TABLE {PORTFOLIO_TABLE} ADD COLUMN IF NOT EXISTS horizon_end_date TIMESTAMPTZ",
    f"ALTER TABLE {PORTFOLIO_TABLE} ADD COLUMN IF NOT EXISTS horizon_basis TEXT",
    f"ALTER TABLE {PORTFOLIO_TABLE} ADD COLUMN IF NOT EXISTS data_dependency_reason TEXT",
    f"ALTER TABLE {PORTFOLIO_TABLE} ADD COLUMN IF NOT EXISTS continue_while TEXT",
    f"ALTER TABLE {PORTFOLIO_TABLE} ADD COLUMN IF NOT EXISTS key_monitor_fields_json TEXT",
    f"ALTER TABLE {PORTFOLIO_TABLE} ADD COLUMN IF NOT EXISTS recheck_frequency TEXT",
    f"ALTER TABLE {PORTFOLIO_TABLE} ADD COLUMN IF NOT EXISTS exit_event_rules_json TEXT",
    f"ALTER TABLE {PORTFOLIO_TABLE} ADD COLUMN IF NOT EXISTS invest_score_pct DOUBLE PRECISION",
]


@dataclass(frozen=True)
class PortfolioConfig:
    capital_inr: float
    max_positions: int
    single_position_cap_pct: float
    per_setup_cap_pct: float
    max_positions_per_overlap_group: int


def get_setup_cap_overrides() -> dict[str, float]:
    overrides: dict[str, float] = {}
    for setup in load_setup_registry():
        setup_id = str(setup.get("setup_id") or "").upper()
        cap_pct = pd.to_numeric(setup.get("portfolio_cap_pct"), errors="coerce")
        if setup_id and pd.notna(cap_pct):
            overrides[setup_id] = float(cap_pct)
    return overrides


def get_single_position_cap_overrides() -> dict[str, float]:
    overrides: dict[str, float] = {}
    for setup in load_setup_registry():
        setup_id = str(setup.get("setup_id") or "").upper()
        cap_pct = pd.to_numeric(setup.get("single_position_cap_pct"), errors="coerce")
        if setup_id and pd.notna(cap_pct):
            overrides[setup_id] = float(cap_pct)
    return overrides


def normalize_timestamp(series: pd.Series) -> pd.Series:
    return pd.to_datetime(series, utc=True, errors="coerce").dt.normalize()


def _record_portfolio_fallback(
    *,
    fallback_type: str,
    source: str,
    reason: str,
    error: Exception,
    metadata: dict[str, Any] | None = None,
) -> None:
    record_local_fallback_event(
        module="advisory.portfolio_engine",
        fallback_type=fallback_type,
        source=source,
        severity="warn",
        reason=reason,
        error=error,
        metadata=metadata or {},
    )


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
    except Exception as exc:
        _record_portfolio_fallback(
            fallback_type="portfolio_engine_table_lookup_failed",
            source=table_name,
            reason="Portfolio engine could not inspect whether a source/output table exists.",
            error=exc,
            metadata={"table_name": table_name},
        )
        return False
    return not df.empty


def ensure_portfolio_table() -> None:
    apply_schema_migration(
        migration_id=PORTFOLIO_SCHEMA_MIGRATION_ID,
        description="Create and normalize advisory portfolio order table.",
        statements=PORTFOLIO_SCHEMA_STATEMENTS,
        metadata={"module": "advisory.portfolio_engine", "tables": [PORTFOLIO_TABLE]},
    )


def load_allocations(
    *,
    asof_date: pd.Timestamp | None = None,
    symbols: list[str] | None = None,
    setup_ids: list[str] | None = None,
    include_planned: bool = False,
) -> pd.DataFrame:
    if not table_exists(ALLOCATIONS_TABLE):
        return pd.DataFrame()

    clauses = ["a.allocation_status = 'allocated'"]
    params: list[object] = []
    if asof_date is not None:
        clauses.append("a.asof_date = %s")
        params.append(asof_date)
    else:
        clauses.append("a.asof_date = (SELECT MAX(asof_date) FROM advisory_allocations)")
    if symbols:
        clauses.append("a.symbol = ANY(%s)")
        params.append([value.upper() for value in symbols])
    if setup_ids:
        clauses.append("a.setup_id = ANY(%s)")
        params.append([value.upper() for value in setup_ids])

    join_sql = ""
    select_sql = ""
    if table_exists(PORTFOLIO_TABLE):
        join_sql = f"""
        LEFT JOIN {PORTFOLIO_TABLE} p
          ON p.asof_date = a.asof_date
         AND p.setup_id = a.setup_id
         AND p.symbol = a.symbol
         AND p.unique_id = a.unique_id
        """
        select_sql = ", p.unique_id AS planned_unique_id"
        if not include_planned:
            clauses.append("p.unique_id IS NULL")

    try:
        df = sql_to_df(
            f"""
            SELECT
                a.*
                {select_sql}
            FROM {ALLOCATIONS_TABLE} a
            {join_sql}
            WHERE {' AND '.join(clauses)}
            ORDER BY a.published_on, a.setup_id, a.symbol
            """,
            params=tuple(params) if params else None,
        )
    except Exception as exc:
        _record_portfolio_fallback(
            fallback_type="portfolio_engine_allocations_load_failed",
            source=ALLOCATIONS_TABLE,
            reason="Portfolio engine could not load allocated risk rows for portfolio planning.",
            error=exc,
            metadata={
                "asof_date": str(asof_date) if asof_date is not None else None,
                "symbol_count": 0 if symbols is None else len(symbols),
                "setup_id_count": 0 if setup_ids is None else len(setup_ids),
                "include_planned": bool(include_planned),
            },
        )
        raise
    if df.empty:
        return df
    df["asof_date"] = normalize_timestamp(df["asof_date"])
    df["published_on"] = pd.to_datetime(df["published_on"], utc=True, errors="coerce")
    df["symbol"] = df["symbol"].astype("string").str.upper()
    return df


def load_symbol_metadata(symbols: list[str]) -> pd.DataFrame:
    if not symbols:
        return pd.DataFrame(columns=["symbol", "sector_code"])
    try:
        meta = sql_to_df(
            """
            WITH sector_counts AS (
                SELECT
                    UPPER(TRIM(symbol)) AS symbol,
                    TRIM(sector_code) AS sector_code,
                    COUNT(*) AS row_count
                FROM master_sharpely_equity
                WHERE NULLIF(TRIM(symbol), '') IS NOT NULL
                  AND NULLIF(TRIM(sector_code), '') IS NOT NULL
                  AND UPPER(TRIM(symbol)) = ANY(%s)
                GROUP BY UPPER(TRIM(symbol)), TRIM(sector_code)
            ),
            ranked AS (
                SELECT
                    symbol,
                    sector_code,
                    ROW_NUMBER() OVER (PARTITION BY symbol ORDER BY row_count DESC, sector_code) AS rn
                FROM sector_counts
            )
            SELECT symbol, sector_code
            FROM ranked
            WHERE rn = 1
            """,
            params=(symbols,),
        )
    except Exception as exc:
        _record_portfolio_fallback(
            fallback_type="portfolio_engine_symbol_metadata_load_failed",
            source="master_sharpely_equity",
            reason="Portfolio engine could not load sector metadata for overlap grouping.",
            error=exc,
            metadata={"symbol_count": len(symbols)},
        )
        raise
    if meta.empty:
        return pd.DataFrame(columns=["symbol", "sector_code"])
    meta["symbol"] = meta["symbol"].astype("string").str.upper()
    meta["sector_code"] = meta["sector_code"].astype("string")
    return meta.drop_duplicates(subset=["symbol"], keep="last")


def load_peer_edges(symbols: list[str]) -> pd.DataFrame:
    return pd.DataFrame(columns=["anchor_symbol", "peer_symbol"])


def build_overlap_map(symbols: list[str]) -> dict[str, tuple[str, str]]:
    unique_symbols = sorted({str(symbol).upper() for symbol in symbols if str(symbol).strip()})
    if not unique_symbols:
        return {}

    adjacency: dict[str, set[str]] = {symbol: set() for symbol in unique_symbols}
    peer_edges = load_peer_edges(unique_symbols)
    for _, edge in peer_edges.iterrows():
        left = str(edge["anchor_symbol"]).upper()
        right = str(edge["peer_symbol"]).upper()
        if left in adjacency and right in adjacency:
            adjacency[left].add(right)
            adjacency[right].add(left)

    overlap_map: dict[str, tuple[str, str]] = {}
    seen: set[str] = set()
    cluster_idx = 0
    for symbol in unique_symbols:
        if symbol in seen or not adjacency[symbol]:
            continue
        cluster_idx += 1
        stack = [symbol]
        component: list[str] = []
        while stack:
            node = stack.pop()
            if node in seen:
                continue
            seen.add(node)
            component.append(node)
            stack.extend(adjacency[node] - seen)
        group_name = f"peer_cluster:{cluster_idx}"
        for node in component:
            overlap_map[node] = (group_name, "peer_cluster")

    meta = load_symbol_metadata(unique_symbols)
    sector_map = {}
    if not meta.empty:
        sector_map = (
            meta.dropna(subset=["sector_code"])
            .set_index("symbol")["sector_code"]
            .astype(str)
            .to_dict()
        )
    for symbol in unique_symbols:
        if symbol in overlap_map:
            continue
        sector_code = sector_map.get(symbol)
        if sector_code:
            overlap_map[symbol] = (f"sector:{sector_code}", "sector_code")
        else:
            overlap_map[symbol] = (f"symbol:{symbol}", "symbol_only")
    return overlap_map


def effective_overlap_group(row: pd.Series, duplicate_symbols: set[str]) -> tuple[str, str]:
    symbol = str(row.get("symbol") or "").upper()
    if symbol in duplicate_symbols:
        return (f"symbol:{symbol}", "same_symbol")
    return (str(row.get("overlap_group") or f"symbol:{symbol}"), str(row.get("overlap_reason") or "symbol_only"))


def overlap_limit_for_reason(reason: str, config: PortfolioConfig) -> int:
    reason_normalized = str(reason or "").lower()
    base_limit = int(config.max_positions_per_overlap_group)
    if reason_normalized in {"same_symbol", "peer_cluster"}:
        return 1
    if reason_normalized == "sector_code":
        return max(base_limit + 1, 2)
    return max(base_limit, 1)


def compute_priority_score(row: pd.Series) -> float:
    confidence_raw = pd.to_numeric(row.get("confidence"), errors="coerce")
    confidence = 0.0 if pd.isna(confidence_raw) else float(confidence_raw)
    conviction_score = CONVICTION_SCORE.get(str(row.get("conviction_bucket", "")).lower(), 1.0)
    risk_penalty = RISK_PENALTY.get(str(row.get("risk_bucket", "")).lower(), 0.5)
    allocation_size_raw = pd.to_numeric(row.get("suggested_allocation_inr"), errors="coerce")
    allocation_size = 0.0 if pd.isna(allocation_size_raw) else float(allocation_size_raw)
    score_impact_raw = pd.to_numeric(row.get("score_impact"), errors="coerce")
    score_impact = 0.0 if pd.isna(score_impact_raw) else float(score_impact_raw)
    transition_hint = str(row.get("state_transition_hint", "")).upper()
    liquidity_penalty = 0.0
    adv_pct = pd.to_numeric(row.get("allocation_pct_of_adv20d"), errors="coerce")
    if pd.notna(adv_pct) and adv_pct > 0.0025:
        liquidity_penalty = min(float(adv_pct) * 100.0, 1.5)
    transition_bonus = 0.0
    if transition_hint == "UPGRADE_TO_PASS_NOW":
        transition_bonus = 1.0
    elif transition_hint == "RAISE_SCORE_ONLY":
        transition_bonus = 0.35
    elif transition_hint == "CUT_SCORE_ONLY":
        transition_bonus = -0.35
    return round((confidence * 3.0) + conviction_score - risk_penalty - liquidity_penalty + (allocation_size / 100_000.0) + (score_impact * 2.0) + transition_bonus, 6)


def compute_invest_score_pct(row: pd.Series) -> float:
    confidence = pd.to_numeric(row.get("confidence"), errors="coerce")
    confidence_component = 0.0 if pd.isna(confidence) else max(0.0, min(float(confidence), 1.0)) * 55.0
    conviction_bucket = str(row.get("conviction_bucket", "")).lower()
    conviction_component = {
        "low": 8.0,
        "medium": 14.0,
        "high": 20.0,
    }.get(conviction_bucket, 10.0)
    risk_bucket = str(row.get("risk_bucket", "")).lower()
    risk_component = {
        "low": 15.0,
        "medium": 11.0,
        "medium_high": 7.0,
        "high": 3.0,
    }.get(risk_bucket, 8.0)
    score_impact = pd.to_numeric(row.get("score_impact"), errors="coerce")
    score_impact_component = 0.0 if pd.isna(score_impact) else max(-8.0, min(8.0, float(score_impact) * 25.0))
    transition_hint = str(row.get("state_transition_hint") or "").upper()
    transition_component = {
        "UPGRADE_TO_PASS_NOW": 8.0,
        "RAISE_SCORE_ONLY": 4.0,
        "CUT_SCORE_ONLY": -4.0,
        "DOWNGRADE_TO_REJECT": -12.0,
    }.get(transition_hint, 0.0)
    liquidity_penalty = 0.0
    adv_pct = pd.to_numeric(row.get("allocation_pct_of_adv20d"), errors="coerce")
    if pd.notna(adv_pct) and adv_pct > 0.0025:
        liquidity_penalty = min(float(adv_pct) * 2000.0, 10.0)
    score = confidence_component + conviction_component + risk_component + score_impact_component + transition_component - liquidity_penalty
    return round(max(0.0, min(score, 100.0)), 2)


def build_execution_notes(row: pd.Series, approved_allocation: float, portfolio_status: str, portfolio_reason: str | None = None) -> str | None:
    notes: list[str] = []
    if portfolio_status == "approved":
        notes.append(f"Approve up to INR {approved_allocation:,.0f} subject to liquidity and execution review.")
    elif portfolio_status == "trimmed":
        notes.append(f"Trimmed from requested INR {float(row.get('suggested_allocation_inr') or 0.0):,.0f} due to portfolio caps.")
    elif portfolio_status == "deferred":
        if portfolio_reason == "overlap_cap":
            notes.append(f"Deferred because overlap group {row.get('overlap_group')} already reached its limit.")
        elif portfolio_reason == "max_positions":
            notes.append("Deferred because the portfolio already reached its max position count.")
        else:
            notes.append("Deferred because portfolio capital or setup cap was exhausted.")
    if pd.notna(row.get("stop_price")):
        notes.append(f"Stop reference {float(row['stop_price']):.2f}.")
    if pd.notna(row.get("invalidation_price")):
        notes.append(f"Invalidation reference {float(row['invalidation_price']):.2f}.")
    if str(row.get("state_transition_hint") or "").strip():
        notes.append(f"Event transition {row.get('state_transition_hint')}.")
    base_notes = str(row.get("notes") or "").strip()
    if base_notes:
        notes.append(base_notes)
    return " ".join(notes) if notes else None


def select_primary_symbol_rows(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty or "symbol" not in df.columns:
        return df
    working = df.copy()
    working["symbol"] = working["symbol"].astype("string").str.upper()
    return working.drop_duplicates(subset=["symbol"], keep="first").reset_index(drop=True)


def _parse_horizon_days(note: object) -> int | None:
    text = str(note or "").strip().lower()
    mapping = {
        "same day to 5 trading days": 5,
        "3 days to 6 weeks": 42,
        "3 days to 8 weeks": 56,
        "1 week to 10 weeks": 70,
        "2 weeks to 3 months": 90,
    }
    return mapping.get(text)


def derive_thesis_policy(row: pd.Series) -> dict[str, Any]:
    setup_family = str(row.get("setup_family") or "").upper()
    holding_horizon_note = str(row.get("holding_horizon_note") or "").strip()
    event_class = str(row.get("event_class") or "").strip()
    invalidation_price = pd.to_numeric(row.get("invalidation_price"), errors="coerce")
    stop_price = pd.to_numeric(row.get("stop_price"), errors="coerce")
    asof_date = pd.to_datetime(row.get("asof_date"), utc=True, errors="coerce")
    confidence = pd.to_numeric(row.get("confidence"), errors="coerce")

    thesis_bucket = "DATA_DEPENDENT"
    bucket_reason = "Keep the position only while the supporting technical, event, and regime evidence remains intact."
    target_price = None
    target_basis = None
    target_confidence = None if pd.isna(confidence) else float(confidence)
    target_review_date = None
    expected_horizon_days = _parse_horizon_days(holding_horizon_note)
    horizon_type = None
    horizon_end_date = None
    horizon_basis = None
    data_dependency_reason = "Evidence-driven idea without a clean fixed target or fixed expiry."
    continue_while = "No exit event fires and setup/regime support remains valid."
    key_monitor_fields = ["invalidation_price", "stop_price", "event_class", "state_transition_hint", "portfolio_reason"]
    recheck_frequency = "daily"

    if setup_family in {"INTRADAY_TACTICAL", "SME_TACTICAL", "MIDCAP_SWING", "EVENT_OPPORTUNITY"} or event_class:
        thesis_bucket = "TIME_HORIZON"
        bucket_reason = "This idea is expected to play out inside a bounded swing or event window."
        horizon_type = "event_window" if event_class else "swing"
        horizon_basis = holding_horizon_note or ("event-driven window" if event_class else "setup-defined horizon")
        if pd.notna(asof_date) and expected_horizon_days:
            horizon_end_date = asof_date + pd.Timedelta(days=int(expected_horizon_days))
        data_dependency_reason = None
        continue_while = None
        recheck_frequency = "intraday" if setup_family == "INTRADAY_TACTICAL" else "daily"
    elif setup_family in {"LARGECAP_POSITION", "DEFENSIVE_POSITION"}:
        thesis_bucket = "TARGET"
        bucket_reason = "This idea is better managed with a target/review frame than a short fixed trade window."
        target_basis = holding_horizon_note or "Position setup target review"
        if pd.notna(asof_date) and expected_horizon_days:
            target_review_date = asof_date + pd.Timedelta(days=int(expected_horizon_days))
        data_dependency_reason = None
        continue_while = None

    exit_rules: list[dict[str, Any]] = []
    if pd.notna(invalidation_price):
        exit_rules.append({"code": "INVALIDATION_HIT", "priority": 1, "threshold_price": float(invalidation_price)})
    if pd.notna(stop_price):
        exit_rules.append({"code": "STOP_HIT", "priority": 2, "threshold_price": float(stop_price)})
    exit_rules.append({"code": "THESIS_REVERSAL", "priority": 3})
    exit_rules.append({"code": "REGIME_BREAK", "priority": 4})

    return {
        "thesis_bucket": thesis_bucket,
        "bucket_reason": bucket_reason,
        "target_price": target_price,
        "target_basis": target_basis,
        "target_confidence": target_confidence,
        "target_review_date": target_review_date,
        "expected_horizon_days": expected_horizon_days,
        "horizon_type": horizon_type,
        "horizon_end_date": horizon_end_date,
        "horizon_basis": horizon_basis,
        "data_dependency_reason": data_dependency_reason,
        "continue_while": continue_while,
        "key_monitor_fields_json": json.dumps(key_monitor_fields, ensure_ascii=False, default=str),
        "recheck_frequency": recheck_frequency,
        "exit_event_rules_json": json.dumps(exit_rules, ensure_ascii=False, default=str),
    }


def build_portfolio_orders(
    *,
    asof_date: pd.Timestamp | None = None,
    symbols: list[str] | None = None,
    setup_ids: list[str] | None = None,
    include_planned: bool = False,
    config: PortfolioConfig | None = None,
) -> pd.DataFrame:
    config = config or PortfolioConfig(
        capital_inr=DEFAULT_PORTFOLIO_CAPITAL_INR,
        max_positions=DEFAULT_MAX_POSITIONS,
        single_position_cap_pct=DEFAULT_SINGLE_POSITION_CAP_PCT,
        per_setup_cap_pct=DEFAULT_SETUP_CAP_PCT,
        max_positions_per_overlap_group=DEFAULT_MAX_POSITIONS_PER_OVERLAP_GROUP,
    )
    allocations = load_allocations(
        asof_date=asof_date,
        symbols=symbols,
        setup_ids=setup_ids,
        include_planned=include_planned,
    )
    if allocations.empty:
        return pd.DataFrame()

    working = allocations.copy()
    overlap_map = build_overlap_map(working["symbol"].astype(str).tolist())
    working["overlap_group"] = working["symbol"].map(lambda s: overlap_map.get(str(s).upper(), (f"symbol:{s}", "symbol_only"))[0])
    working["overlap_reason"] = working["symbol"].map(lambda s: overlap_map.get(str(s).upper(), (f"symbol:{s}", "symbol_only"))[1])
    working["priority_score"] = working.apply(compute_priority_score, axis=1)
    working["invest_score_pct"] = working.apply(compute_invest_score_pct, axis=1)
    working["requested_allocation_inr"] = pd.to_numeric(working["suggested_allocation_inr"], errors="coerce").fillna(0.0)
    duplicate_symbols = {
        str(symbol).upper()
        for symbol, count in working["symbol"].astype("string").str.upper().value_counts().items()
        if int(count) > 1
    }
    effective_overlap = working.apply(lambda row: effective_overlap_group(row, duplicate_symbols), axis=1)
    working["effective_overlap_group"] = effective_overlap.map(lambda item: item[0])
    working["effective_overlap_reason"] = effective_overlap.map(lambda item: item[1])
    working = working.sort_values(
        by=["priority_score", "confidence", "requested_allocation_inr", "published_on"],
        ascending=[False, False, False, True],
        kind="stable",
    ).reset_index(drop=True)
    working = select_primary_symbol_rows(working)

    remaining_capital = float(config.capital_inr)
    setup_caps_used: dict[str, float] = {}
    overlap_group_counts: dict[str, int] = {}
    approved_positions = 0
    rows: list[dict[str, Any]] = []
    planned_now = pd.Timestamp.utcnow()

    default_single_position_cap = float(config.capital_inr) * float(config.single_position_cap_pct)
    setup_cap_overrides = get_setup_cap_overrides()
    single_position_cap_overrides = get_single_position_cap_overrides()

    for idx, (_, row) in enumerate(working.iterrows(), start=1):
        setup_id = str(row["setup_id"]).upper()
        single_position_cap = float(config.capital_inr) * float(single_position_cap_overrides.get(setup_id, config.single_position_cap_pct))
        requested = min(float(row["requested_allocation_inr"]), single_position_cap)
        setup_used = setup_caps_used.get(setup_id, 0.0)
        setup_cap_value = float(config.capital_inr) * float(setup_cap_overrides.get(setup_id, config.per_setup_cap_pct))
        setup_remaining = max(setup_cap_value - setup_used, 0.0)
        overlap_group = str(row.get("effective_overlap_group") or row.get("overlap_group"))
        overlap_reason = str(row.get("effective_overlap_reason") or row.get("overlap_reason"))
        overlap_limit = overlap_limit_for_reason(overlap_reason, config)
        overlap_count = overlap_group_counts.get(overlap_group, 0)
        portfolio_reason: str | None = None

        if overlap_count >= overlap_limit:
            approved = 0.0
            status = "deferred"
            portfolio_reason = "overlap_cap"
        elif approved_positions >= int(config.max_positions):
            approved = 0.0
            status = "deferred"
            portfolio_reason = "max_positions"
        else:
            approved = min(requested, remaining_capital, setup_remaining)
            if approved <= 0:
                status = "deferred"
                portfolio_reason = "capital_or_setup_cap"
            elif approved + 1e-9 < float(row["requested_allocation_inr"]):
                status = "trimmed"
                portfolio_reason = "capital_or_setup_cap"
            else:
                status = "approved"
                portfolio_reason = "within_limits"

        approved = float(int(approved // 1000) * 1000) if approved > 0 else 0.0
        if approved <= 0:
            status = "deferred"
            portfolio_reason = portfolio_reason or "capital_or_setup_cap"
        else:
            remaining_capital = max(remaining_capital - approved, 0.0)
            setup_caps_used[setup_id] = setup_used + approved
            overlap_group_counts[overlap_group] = overlap_count + 1
            approved_positions += 1

        portfolio_row = {
                "published_on": planned_now,
                "source_published_on": row.get("published_on"),
                "asof_date": row["asof_date"],
                "planned_at": planned_now,
                "setup_id": row["setup_id"],
                "setup_name": row.get("setup_name"),
                "symbol": row["symbol"],
                "company_master_id": row.get("company_master_id"),
                "unique_id": row["unique_id"],
                "portfolio_capital_inr": config.capital_inr,
                "max_positions": config.max_positions,
                "plan_rank": idx,
                "portfolio_status": status,
                "portfolio_reason": portfolio_reason,
                "priority_score": row["priority_score"],
                "invest_score_pct": row.get("invest_score_pct"),
                "overlap_group": overlap_group,
                "overlap_reason": overlap_reason,
                "event_class": row.get("event_class"),
                "state_transition_hint": row.get("state_transition_hint"),
                "score_impact": row.get("score_impact"),
                "requested_allocation_inr": float(row["requested_allocation_inr"]),
                "approved_allocation_inr": approved,
                "remaining_capital_after_inr": remaining_capital,
                "stop_price": row.get("stop_price"),
                "invalidation_price": row.get("invalidation_price"),
                "invalidation_rule": row.get("invalidation_rule"),
                "execution_notes": build_execution_notes(row, approved, status, portfolio_reason),
                "context_snapshot_json": row.get("context_snapshot_json"),
                "load_ts": pd.Timestamp.utcnow(),
            }
        portfolio_row.update(derive_thesis_policy(pd.Series({**row.to_dict(), **portfolio_row})))
        rows.append(portfolio_row)

    return pd.DataFrame(rows)


def persist_portfolio_orders(df: pd.DataFrame) -> None:
    ensure_portfolio_table()
    if df.empty:
        return
    out = df.copy()
    for column in [
        "portfolio_capital_inr",
        "priority_score",
        "invest_score_pct",
        "score_impact",
        "requested_allocation_inr",
        "approved_allocation_inr",
        "remaining_capital_after_inr",
        "stop_price",
        "invalidation_price",
        "target_price",
        "target_confidence",
    ]:
        if column in out.columns:
            out[column] = pd.to_numeric(out[column], errors="coerce")
    for column in ["max_positions", "plan_rank", "expected_horizon_days"]:
        if column in out.columns:
            out[column] = pd.to_numeric(out[column], errors="coerce").astype("Int64")
    for column in ["published_on", "source_published_on", "asof_date", "planned_at", "load_ts", "target_review_date", "horizon_end_date"]:
        if column in out.columns:
            out[column] = pd.to_datetime(out[column], utc=True, errors="coerce")
    pairs = (
        out[["asof_date", "symbol"]]
        .dropna()
        .drop_duplicates()
        .to_dict(orient="records")
    )

    def _delete_existing_portfolio_orders() -> None:
        with db_session() as (_, cur):
            for item in pairs:
                cur.execute(
                    f"DELETE FROM {PORTFOLIO_TABLE} WHERE asof_date = %s AND symbol = %s",
                    (
                        pd.to_datetime(item["asof_date"], utc=True, errors="coerce").to_pydatetime(),
                        str(item["symbol"]).upper(),
                    ),
                )

    execute_db_operation(
        _delete_existing_portfolio_orders,
        operation_name="portfolio_engine:delete_existing_orders",
    )
    upsert_to_db(
        out,
        PORTFOLIO_TABLE,
        unique_keys=["published_on", "setup_id", "symbol", "unique_id"],
        timescaledb_column="published_on",
    )
    _trace_portfolio_rows(out)


def _trace_portfolio_rows(df: pd.DataFrame) -> None:
    for _, row in df.iterrows():
        trace_id = safe_trace_call(
            append_trace,
            asof_date=row.get("asof_date"),
            symbol=row.get("symbol"),
            unique_id=row.get("unique_id"),
            setup_id=row.get("setup_id"),
            trigger_type="portfolio_allocation",
            final_action=row.get("portfolio_status"),
            final_reason=row.get("portfolio_reason") or row.get("execution_notes"),
            source_table=PORTFOLIO_TABLE,
            source_key=f"{row.get('published_on')}:{row.get('setup_id')}:{row.get('symbol')}:{row.get('unique_id')}",
            payload={
                "portfolio_status": row.get("portfolio_status"),
                "portfolio_reason": row.get("portfolio_reason"),
                "plan_rank": row.get("plan_rank"),
                "priority_score": row.get("priority_score"),
                "invest_score_pct": row.get("invest_score_pct"),
                "requested_allocation_inr": row.get("requested_allocation_inr"),
                "approved_allocation_inr": row.get("approved_allocation_inr"),
                "remaining_capital_after_inr": row.get("remaining_capital_after_inr"),
                "overlap_group": row.get("overlap_group"),
                "overlap_reason": row.get("overlap_reason"),
            },
        )
        if trace_id:
            safe_trace_call(
                append_trace_step,
                trace_id=trace_id,
                step_idx=35,
                stage="portfolio_allocation",
                status=str(row.get("portfolio_status") or "completed"),
                reason=row.get("execution_notes") or row.get("portfolio_reason"),
                input_payload=row.get("context_snapshot_json"),
                output_payload=row.to_dict(),
                payload={
                    "portfolio_status": row.get("portfolio_status"),
                    "portfolio_reason": row.get("portfolio_reason"),
                    "plan_rank": row.get("plan_rank"),
                    "priority_score": row.get("priority_score"),
                    "invest_score_pct": row.get("invest_score_pct"),
                    "requested_allocation_inr": row.get("requested_allocation_inr"),
                    "approved_allocation_inr": row.get("approved_allocation_inr"),
                    "remaining_capital_after_inr": row.get("remaining_capital_after_inr"),
                    "overlap_group": row.get("overlap_group"),
                    "overlap_reason": row.get("overlap_reason"),
                    "thesis_bucket": row.get("thesis_bucket"),
                    "bucket_reason": row.get("bucket_reason"),
                    "expected_horizon_days": row.get("expected_horizon_days"),
                    "target_price": row.get("target_price"),
                    "stop_price": row.get("stop_price"),
                    "invalidation_price": row.get("invalidation_price"),
                },
            )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build capital-aware portfolio order plans from advisory allocations.")
    parser.add_argument("--date", type=parse_datetime_arg, help="Allocation asof date in YYYY-MM-DD")
    parser.add_argument("--symbols", nargs="*", help="Optional symbols")
    parser.add_argument("--setup", dest="setup_ids", nargs="*", help="Optional setup ids")
    parser.add_argument("--capital-inr", type=float, default=DEFAULT_PORTFOLIO_CAPITAL_INR, help="Portfolio capital budget in INR")
    parser.add_argument("--max-positions", type=int, default=DEFAULT_MAX_POSITIONS, help="Maximum simultaneous approved positions")
    parser.add_argument("--single-position-cap-pct", type=float, default=DEFAULT_SINGLE_POSITION_CAP_PCT, help="Per-position cap as a fraction of total capital")
    parser.add_argument("--per-setup-cap-pct", type=float, default=DEFAULT_SETUP_CAP_PCT, help="Per-setup cap as a fraction of total capital")
    parser.add_argument("--max-positions-per-overlap-group", type=int, default=DEFAULT_MAX_POSITIONS_PER_OVERLAP_GROUP, help="Maximum simultaneous approved positions within the same overlap group")
    parser.add_argument("--include-planned", action="store_true", help="Rebuild rows that already exist in advisory_portfolio_orders")
    parser.add_argument("--format", choices=["json", "text"], default="json")
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def _text_cell(value: object) -> str:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return "-"
    if isinstance(value, float):
        return f"{value:.2f}".rstrip("0").rstrip(".")
    text = str(value)
    return text if text else "-"


def render_text_table(df: pd.DataFrame) -> str:
    if df.empty:
        return "No portfolio rows found."
    columns = [
        ("rank", 4),
        ("symbol", 14),
        ("setup_id", 22),
        ("bucket", 14),
        ("status", 10),
        ("reason", 18),
        ("approved_inr", 12),
        ("requested_inr", 13),
        ("priority", 8),
        ("event", 16),
        ("transition", 18),
    ]
    header = " ".join(label.ljust(width) for label, width in columns)
    separator = " ".join("-" * width for _, width in columns)
    lines = [header, separator]
    working = df.sort_values(["plan_rank", "priority_score"], ascending=[True, False], kind="stable")
    for _, row in working.iterrows():
        values = [
            _text_cell(row.get("plan_rank"))[:4],
            _text_cell(row.get("symbol"))[:14],
            _text_cell(row.get("setup_id"))[:22],
            _text_cell(row.get("thesis_bucket"))[:14],
            _text_cell(row.get("portfolio_status"))[:10],
            _text_cell(row.get("portfolio_reason"))[:18],
            _text_cell(row.get("approved_allocation_inr"))[:12],
            _text_cell(row.get("requested_allocation_inr"))[:13],
            _text_cell(row.get("priority_score"))[:8],
            _text_cell(row.get("event_class"))[:16],
            _text_cell(row.get("state_transition_hint"))[:18],
        ]
        lines.append(" ".join(value.ljust(width) for value, (_, width) in zip(values, columns)))
    return "\n".join(lines)


def summarize(df: pd.DataFrame) -> dict[str, Any]:
    if df.empty:
        return {
            "status": "ok",
            "table": PORTFOLIO_TABLE,
            "row_count": 0,
            "approved_count": 0,
            "trimmed_count": 0,
            "deferred_count": 0,
            "approved_capital_inr": 0.0,
            "sample": [],
        }
    return {
        "status": "ok",
        "table": PORTFOLIO_TABLE,
        "row_count": int(len(df)),
        "approved_count": int((df["portfolio_status"] == "approved").sum()),
        "trimmed_count": int((df["portfolio_status"] == "trimmed").sum()),
        "deferred_count": int((df["portfolio_status"] == "deferred").sum()),
        "approved_capital_inr": float(pd.to_numeric(df["approved_allocation_inr"], errors="coerce").fillna(0.0).sum()),
        "sample": to_display_value(df.head(10)),
    }


def emit_error(*, message: str, detail: str | None = None, as_json: bool) -> None:
    if as_json:
        print(
            json.dumps(
                {
                    "status": "error",
                    "table": PORTFOLIO_TABLE,
                    "message": message,
                    "detail": detail,
                },
                indent=2,
                ensure_ascii=False,
                default=str,
            )
        )
        return
    line = f"Portfolio unavailable: {message}"
    if detail:
        line = f"{line} ({detail})"
    print(line)


def main() -> int:
    args = parse_args()
    asof_date = pd.Timestamp(args.date, tz="UTC") if args.date else None
    config = PortfolioConfig(
        capital_inr=float(args.capital_inr),
        max_positions=int(args.max_positions),
        single_position_cap_pct=float(args.single_position_cap_pct),
        per_setup_cap_pct=float(args.per_setup_cap_pct),
        max_positions_per_overlap_group=int(args.max_positions_per_overlap_group),
    )
    include_planned = bool(args.include_planned or not args.dry_run)
    try:
        df = build_portfolio_orders(
            asof_date=asof_date,
            symbols=args.symbols,
            setup_ids=args.setup_ids,
            include_planned=include_planned,
            config=config,
        )
        if not args.dry_run:
            persist_portfolio_orders(df)
        result = summarize(df)
        result["dry_run"] = bool(args.dry_run)
        if args.format == "text":
            print(render_text_table(df))
        else:
            print(json.dumps(to_display_value(result), indent=2, ensure_ascii=False, default=str))
        return 0
    except SQLAlchemyError as exc:
        _record_portfolio_fallback(
            fallback_type="portfolio_engine_cli_database_failed",
            source=PORTFOLIO_TABLE,
            reason="Portfolio CLI failed because the database layer raised an error.",
            error=exc,
            metadata={
                "format": str(args.format),
                "dry_run": bool(args.dry_run),
                "include_planned": bool(include_planned),
            },
        )
        emit_error(
            message="database connection failed",
            detail=f"{exc.__class__.__name__}: {exc}",
            as_json=args.format == "json",
        )
        return 1
    except Exception as exc:
        error_name = exc.__class__.__name__
        if error_name in {"OperationalError", "InterfaceError"}:
            _record_portfolio_fallback(
                fallback_type="portfolio_engine_cli_database_failed",
                source=PORTFOLIO_TABLE,
                reason="Portfolio CLI failed because the database connection became unavailable.",
                error=exc,
                metadata={
                    "format": str(args.format),
                    "dry_run": bool(args.dry_run),
                    "include_planned": bool(include_planned),
                },
            )
            emit_error(
                message="database connection failed",
                detail=f"{error_name}: {exc}",
                as_json=args.format == "json",
            )
            return 1
        _record_portfolio_fallback(
            fallback_type="portfolio_engine_cli_build_failed",
            source=PORTFOLIO_TABLE,
            reason="Portfolio CLI failed while building or persisting portfolio orders.",
            error=exc,
            metadata={
                "format": str(args.format),
                "dry_run": bool(args.dry_run),
                "include_planned": bool(include_planned),
                "error_type": error_name,
            },
        )
        emit_error(
            message="portfolio build failed",
            detail=f"{error_name}: {exc}",
            as_json=args.format == "json",
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
