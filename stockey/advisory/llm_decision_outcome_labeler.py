"""Realized-outcome labeler for LLM-direct decisions ([P-LLM-AUTH], closes the loop).

The .3 monitor judges decisions on MATURED benchmark-excess after cost; nothing produces those
labels yet, so every decision reads `matured=False`. This module computes them, point-in-time:
for each persisted decision whose holding horizon has fully elapsed as of the label date, it takes
the symbol's forward return and the benchmark's forward return over the same window, nets cost, and
emits the after-cost excess + the flags the monitor consumes. It then provides a principled,
data-driven `evaluate_policy_graduation` so `graduation_passed` (.1/.5) can only ever be earned from
real matured outcomes -- never asserted.

Point-in-time discipline (no lookahead): the forward window uses only closes STRICTLY AFTER the
decision's as-of date (mirroring `advisory/return_attribution`), and a decision matures only when the
horizon-th forward close exists as of the label date. Pure compute core + thin injectable loaders,
so the judging logic is unit-tested without a DB.

Judging convention: entries (BUY/BUY_MORE) are long alpha; exits (SELL/PARTIAL_SELL) score the
avoided relative move (a good sell = the position underperformed the benchmark). `resolved_beta_only`
(made money but didn't beat the benchmark) is meaningful for entries only.
"""

from __future__ import annotations

import os
from typing import Any, Callable

import pandas as pd

from utils.schema_migrations import apply_schema_migration

DEFAULT_HORIZON_DAYS = int(os.getenv("LLM_DECISION_OUTCOME_HORIZON_DAYS", "20"))
DEFAULT_COST_BPS = float(os.getenv("LLM_DECISION_OUTCOME_COST_BPS", "25"))
GRADUATION_MIN_MATURED = int(os.getenv("LLM_DECISION_GRADUATION_MIN_MATURED", "20"))

ENTRY_ACTIONS = {"BUY", "BUY_MORE"}
EXIT_ACTIONS = {"SELL", "PARTIAL_SELL"}
DIRECTIONAL_ACTIONS = ENTRY_ACTIONS | EXIT_ACTIONS

OUTCOMES_TABLE = "advisory_llm_decision_outcomes"
SCHEMA_MIGRATION_ID = "20260624_advisory_llm_decision_outcomes_base"
SCHEMA_STATEMENTS = [
    f"""
    CREATE TABLE IF NOT EXISTS {OUTCOMES_TABLE} (
        decided_at TIMESTAMPTZ NOT NULL,
        symbol TEXT NOT NULL,
        horizon_days BIGINT NOT NULL,
        labeled_asof_date TIMESTAMPTZ,
        matured BOOLEAN,
        action TEXT,
        symbol_forward_return DOUBLE PRECISION,
        benchmark_forward_return DOUBLE PRECISION,
        realized_return_after_cost DOUBLE PRECISION,
        realized_excess_after_cost DOUBLE PRECISION,
        excess_hit BOOLEAN,
        resolved_beta_only BOOLEAN,
        cost_bps DOUBLE PRECISION,
        entry_date TIMESTAMPTZ,
        exit_date TIMESTAMPTZ,
        load_ts TIMESTAMPTZ,
        UNIQUE (symbol, decided_at, horizon_days)
    )
    """,
]


def ensure_tables() -> None:
    apply_schema_migration(
        migration_id=SCHEMA_MIGRATION_ID,
        description="Create the realized-outcome table for LLM-direct decisions (matured benchmark-excess labels).",
        statements=SCHEMA_STATEMENTS,
        metadata={"tables": [OUTCOMES_TABLE], "authority_scope": "llm_decision_outcome_monitoring"},
    )


def _series(rows: Any) -> list[tuple[pd.Timestamp, float]]:
    """Normalize a price series to sorted (normalized-date, positive-close) tuples."""
    out: list[tuple[pd.Timestamp, float]] = []
    for row in rows or []:
        if isinstance(row, dict):
            date, close = row.get("date"), row.get("close")
        else:
            date, close = row[0], row[1]
        ts = pd.to_datetime(date, utc=True, errors="coerce")
        try:
            value = float(close)
        except (TypeError, ValueError):
            value = float("nan")
        if pd.isna(ts) or not (value == value) or value <= 0:  # value==value drops NaN
            continue
        out.append((ts.normalize(), value))
    out.sort(key=lambda item: item[0])
    return out


def _forward_window(series: list[tuple[pd.Timestamp, float]], asof_ts: pd.Timestamp, horizon: int):
    """Entry = first close strictly after asof; exit = horizon-th such close. None if not enough."""
    eligible = [item for item in series if item[0] > asof_ts]
    if len(eligible) < int(horizon):
        return None
    return eligible[0], eligible[int(horizon) - 1]


def compute_decision_outcome(
    *,
    action: str,
    asof_date: Any,
    horizon_days: int,
    symbol_series: Any,
    benchmark_series: Any,
    cost_bps: float = DEFAULT_COST_BPS,
) -> dict[str, Any]:
    """Realized after-cost benchmark-excess outcome for one decision. Pure; no lookahead."""
    action_u = str(action or "").strip().upper()
    if action_u not in DIRECTIONAL_ACTIONS:
        return {"matured": False, "reason": f"non_directional_action:{action_u or 'none'}", "action": action_u}
    asof_ts = pd.to_datetime(asof_date, utc=True, errors="coerce")
    if pd.isna(asof_ts):
        return {"matured": False, "reason": "bad_asof_date", "action": action_u}
    asof_ts = asof_ts.normalize()
    horizon = int(horizon_days)

    sym = _forward_window(_series(symbol_series), asof_ts, horizon)
    bench = _forward_window(_series(benchmark_series), asof_ts, horizon)
    if sym is None or bench is None:
        return {"matured": False, "reason": "insufficient_forward_history", "action": action_u, "horizon_days": horizon}

    (s_entry_d, s_entry), (s_exit_d, s_exit) = sym
    (_b_entry_d, b_entry), (_b_exit_d, b_exit) = bench
    symbol_return = s_exit / s_entry - 1.0
    benchmark_return = b_exit / b_entry - 1.0

    direction = 1.0 if action_u in ENTRY_ACTIONS else -1.0
    cost = float(cost_bps) / 10000.0
    realized_return_after_cost = direction * symbol_return - cost
    realized_excess_after_cost = direction * (symbol_return - benchmark_return) - cost
    is_entry = action_u in ENTRY_ACTIONS
    return {
        "matured": True,
        "action": action_u,
        "horizon_days": horizon,
        "cost_bps": float(cost_bps),
        "symbol_forward_return": symbol_return,
        "benchmark_forward_return": benchmark_return,
        "realized_return_after_cost": realized_return_after_cost,
        "realized_excess_after_cost": realized_excess_after_cost,
        "excess_hit": realized_excess_after_cost > 0,
        "resolved_beta_only": bool(is_entry and realized_return_after_cost > 0 and realized_excess_after_cost <= 0),
        "entry_date": s_entry_d,
        "exit_date": s_exit_d,
    }


def evaluate_policy_graduation(
    monitor_report: dict[str, Any],
    *,
    min_matured: int = GRADUATION_MIN_MATURED,
    require_no_alerts: bool = True,
) -> dict[str, Any]:
    """Data-driven graduation: earned ONLY from enough matured outcomes with healthy after-cost excess.

    This is what may legitimately set `graduation_passed` (.1/.5). It never asserts confidence -- it
    reads the .3 monitor's matured aggregate. Graduation is a policy-level signal, not per-decision.
    """
    summary = (monitor_report or {}).get("summary", {}) or {}
    matured = int(summary.get("matured_count") or 0)
    mean_excess = summary.get("mean_excess_after_cost")
    alert_count = int((monitor_report or {}).get("alert_count") or 0)

    reasons: list[str] = []
    if matured < int(min_matured):
        reasons.append(f"insufficient_matured: {matured} < {int(min_matured)}")
    if require_no_alerts and alert_count > 0:
        reasons.append(f"systematic_error_alerts: {alert_count}")
    if mean_excess is None or mean_excess <= 0:
        reasons.append(f"mean_excess_not_positive: {mean_excess}")

    return {
        "graduated": not reasons,
        "matured_count": matured,
        "mean_excess_after_cost": mean_excess,
        "alert_count": alert_count,
        "min_matured": int(min_matured),
        "blocked_reasons": reasons,
    }


# --------------------------------------------------------------------------------------------------
# Thin injectable DB layer (defaults hit the real DB; tests inject fakes).
# --------------------------------------------------------------------------------------------------

def _default_decisions_loader(asof_date: Any, horizon_days: int) -> list[dict[str, Any]]:
    try:
        from utils.db import sql_to_df
        from advisory.llm_decision_store import DECISIONS_TABLE

        df = sql_to_df(
            f"SELECT decided_at, asof_date, symbol, proposed_action, event_class, sufficiency_path "
            f"FROM {DECISIONS_TABLE} WHERE proposed_action IN ('BUY','BUY_MORE','SELL','PARTIAL_SELL')",
            params={},
        )
        return [] if df is None or df.empty else [row.to_dict() for _, row in df.iterrows()]
    except Exception:
        return []


def _default_symbol_price_loader(symbol: str, from_date: Any, to_date: Any) -> list[tuple[Any, float]]:
    try:
        from utils.db import sql_to_df

        df = sql_to_df(
            "SELECT asof_date AS date, adj_close AS close FROM advisory_technical_daily "
            "WHERE symbol=%(symbol)s AND asof_date>=%(from)s AND asof_date<=%(to)s ORDER BY asof_date",
            params={"symbol": str(symbol).upper(), "from": str(from_date)[:10], "to": str(to_date)[:10]},
        )
        return [] if df is None or df.empty else list(df.itertuples(index=False, name=None))
    except Exception:
        return []


def _default_benchmark_loader(from_date: Any, to_date: Any) -> list[tuple[Any, float]]:
    try:
        from advisory.return_attribution import load_benchmark_history_for_attribution

        df = load_benchmark_history_for_attribution(
            from_date=pd.to_datetime(from_date, utc=True), to_date=pd.to_datetime(to_date, utc=True)
        )
        return [] if df is None or df.empty else list(df[["date", "benchmark_close"]].itertuples(index=False, name=None))
    except Exception:
        return []


def label_decisions(
    asof_date: Any,
    *,
    horizon_days: int = DEFAULT_HORIZON_DAYS,
    cost_bps: float = DEFAULT_COST_BPS,
    decisions_loader: Callable[[Any, int], list[dict[str, Any]]] = _default_decisions_loader,
    symbol_price_loader: Callable[[str, Any, Any], list[tuple[Any, float]]] = _default_symbol_price_loader,
    benchmark_loader: Callable[[Any, Any], list[tuple[Any, float]]] = _default_benchmark_loader,
) -> dict[str, Any]:
    """Label every directional decision whose horizon elapsed as of asof_date.

    Returns {"outcomes": [<row>...], "outcomes_by_key": {(symbol, decided_at): <outcome>}}. The
    outcomes_by_key feeds `advisory/llm_decision_store.decisions_to_monitor_records` / the .3 monitor.
    """
    decisions = decisions_loader(asof_date, int(horizon_days)) or []
    benchmark_series = benchmark_loader("1900-01-01", asof_date)
    rows: list[dict[str, Any]] = []
    outcomes_by_key: dict[tuple[Any, Any], dict[str, Any]] = {}

    for decision in decisions:
        symbol = decision.get("symbol")
        decided_at = decision.get("decided_at")
        decision_asof = decision.get("asof_date") or decided_at
        symbol_series = symbol_price_loader(symbol, decision_asof, asof_date)
        outcome = compute_decision_outcome(
            action=decision.get("proposed_action"),
            asof_date=decision_asof,
            horizon_days=horizon_days,
            symbol_series=symbol_series,
            benchmark_series=benchmark_series,
            cost_bps=cost_bps,
        )
        outcomes_by_key[(symbol, decided_at)] = outcome
        rows.append({
            "decided_at": decided_at,
            "symbol": symbol,
            "horizon_days": int(horizon_days),
            "labeled_asof_date": asof_date,
            "matured": bool(outcome.get("matured")),
            "action": outcome.get("action"),
            "symbol_forward_return": outcome.get("symbol_forward_return"),
            "benchmark_forward_return": outcome.get("benchmark_forward_return"),
            "realized_return_after_cost": outcome.get("realized_return_after_cost"),
            "realized_excess_after_cost": outcome.get("realized_excess_after_cost"),
            "excess_hit": outcome.get("excess_hit"),
            "resolved_beta_only": outcome.get("resolved_beta_only"),
            "cost_bps": float(cost_bps),
            "entry_date": outcome.get("entry_date"),
            "exit_date": outcome.get("exit_date"),
        })
    return {"outcomes": rows, "outcomes_by_key": outcomes_by_key}


def persist_outcomes(rows: list[dict[str, Any]], *, labeled_at: Any) -> int:
    """Persist labeled outcomes to `advisory_llm_decision_outcomes`. Returns rows written."""
    if not rows:
        return 0
    from utils.db import upsert_to_db

    ensure_tables()
    frame = pd.DataFrame(rows)
    frame["load_ts"] = pd.Timestamp(labeled_at)
    for column in ("decided_at", "labeled_asof_date", "entry_date", "exit_date", "load_ts"):
        if column in frame.columns:
            frame[column] = pd.to_datetime(frame[column], utc=True, errors="coerce")
    upsert_to_db(frame, OUTCOMES_TABLE, unique_keys=["symbol", "decided_at", "horizon_days"], timescaledb_column="decided_at")
    return len(rows)
