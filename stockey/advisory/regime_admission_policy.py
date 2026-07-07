"""Regime admission policy: 3-state, hysteresis-guarded top-of-funnel width control.

The regret ledger showed the funnel's admission gates (market-cap band, liquidity floor)
blocking winners in a rising market while the entry-confirmation gates earn their keep.
This module lets ADMISSION width flex with market state -- and nothing else:

- Three discrete states (`risk_on` / `neutral` / `risk_off`) resolved from already-persisted
  inputs: benchmark participation (advisory_market_regime ret20/ret60), breadth
  (advisory_market_context_summary_daily), and macro risk state. No continuous formulas.
- Hysteresis: the effective state only flips after REGIME_ADMISSION_CONFIRM_DAYS consecutive
  raw days of the new state -- except flips INTO risk_off, which apply immediately (safety).
- Every day's decision is a recorded row (inputs + parameters), so the policy is auditable
  and the regret ledger can attribute admission width to outcomes.
- Consumers: the rule engine's market-cap/liquidity hard-reject tolerances and the
  market-action scan's daily cap. Entry confirmation, risk sizing, stops, and score
  thresholds are explicitly NOT touched. `neutral` reproduces the previous hardcoded
  behavior exactly, and every failure path falls back to `neutral` -- fail-open to current
  behavior, never wider.
"""
from __future__ import annotations

import argparse
import json
import os
from typing import Any

import pandas as pd

from advisory.fallback_telemetry import record_local_fallback_event
from utils.db import sql_to_df, upsert_to_db
from utils.schema_migrations import apply_schema_migration

TABLE_NAME = "advisory_regime_admission_policy"
REGIME_ADMISSION_MIGRATION_ID = "20260707_advisory_regime_admission_policy_base"
REGIME_ADMISSION_SCHEMA_STATEMENTS = [
    f"""
    CREATE TABLE IF NOT EXISTS {TABLE_NAME} (
        asof_date TIMESTAMPTZ NOT NULL,
        raw_state TEXT NOT NULL,
        effective_state TEXT NOT NULL,
        consecutive_days BIGINT,
        inputs_json TEXT,
        parameters_json TEXT,
        load_ts TIMESTAMPTZ,
        UNIQUE (asof_date)
    )
    """,
]

RISK_ON = "risk_on"
NEUTRAL = "neutral"
RISK_OFF = "risk_off"
STATES = (RISK_ON, NEUTRAL, RISK_OFF)

MARKET_REGIME_TABLE = "advisory_market_regime"
MARKET_CONTEXT_SUMMARY_TABLE = "advisory_market_context_summary_daily"

CONFIRM_DAYS = int(os.getenv("REGIME_ADMISSION_CONFIRM_DAYS", "3"))
UPTREND_RET20 = float(os.getenv("REGIME_ADMISSION_UPTREND_RET20", "0.03"))
UPTREND_RET60 = float(os.getenv("REGIME_ADMISSION_UPTREND_RET60", "0.06"))
DOWNTREND_RET20 = float(os.getenv("REGIME_ADMISSION_DOWNTREND_RET20", "-0.03"))
DOWNTREND_RET60 = float(os.getenv("REGIME_ADMISSION_DOWNTREND_RET60", "-0.06"))
RISK_ON_MIN_BREADTH_PCT = float(os.getenv("REGIME_ADMISSION_RISK_ON_MIN_BREADTH_PCT", "55.0"))
RISK_ON_MACRO_STATES = {"NORMAL", "WATCH"}
RISK_OFF_MACRO_STATES = {"ELEVATED", "STRESS", "HIGH"}


def _state_env(state: str, key: str, default: float) -> float:
    return float(os.getenv(f"REGIME_ADMISSION_{state.upper()}_{key}", str(default)))


# neutral values are today's previously-hardcoded literals (rule_engine 0.70 / 1.30 / 0.50,
# market_action_scan cap 30) so the neutral state is byte-for-byte current behavior.
def admission_parameters(state: str) -> dict[str, Any]:
    normalized = str(state or "").strip().lower()
    if normalized == RISK_ON:
        return {
            "state": RISK_ON,
            "scan_limit": int(_state_env(RISK_ON, "SCAN_LIMIT", 50)),
            "market_cap_below_tolerance": _state_env(RISK_ON, "MCAP_BELOW_TOLERANCE", 0.50),
            "market_cap_above_tolerance": _state_env(RISK_ON, "MCAP_ABOVE_TOLERANCE", 1.60),
            "liquidity_floor_multiplier": _state_env(RISK_ON, "LIQUIDITY_FLOOR_MULTIPLIER", 0.35),
        }
    if normalized == RISK_OFF:
        return {
            "state": RISK_OFF,
            "scan_limit": int(_state_env(RISK_OFF, "SCAN_LIMIT", 10)),
            "market_cap_below_tolerance": _state_env(RISK_OFF, "MCAP_BELOW_TOLERANCE", 0.85),
            "market_cap_above_tolerance": _state_env(RISK_OFF, "MCAP_ABOVE_TOLERANCE", 1.15),
            "liquidity_floor_multiplier": _state_env(RISK_OFF, "LIQUIDITY_FLOOR_MULTIPLIER", 0.75),
        }
    return {
        "state": NEUTRAL,
        "scan_limit": int(_state_env(NEUTRAL, "SCAN_LIMIT", 30)),
        "market_cap_below_tolerance": _state_env(NEUTRAL, "MCAP_BELOW_TOLERANCE", 0.70),
        "market_cap_above_tolerance": _state_env(NEUTRAL, "MCAP_ABOVE_TOLERANCE", 1.30),
        "liquidity_floor_multiplier": _state_env(NEUTRAL, "LIQUIDITY_FLOOR_MULTIPLIER", 0.50),
    }


def ensure_table() -> None:
    apply_schema_migration(
        migration_id=REGIME_ADMISSION_MIGRATION_ID,
        description="Record the daily regime admission state and the top-of-funnel parameters it selected.",
        statements=REGIME_ADMISSION_SCHEMA_STATEMENTS,
        metadata={"module": "advisory.regime_admission_policy", "tables": [TABLE_NAME]},
    )


def resolve_inputs(asof_date: Any | None = None) -> dict[str, Any]:
    """Read the already-persisted market inputs (point-in-time <= asof)."""
    parsed = pd.to_datetime(asof_date, utc=True, errors="coerce")
    if pd.isna(parsed):
        parsed = pd.Timestamp.utcnow()
    inputs: dict[str, Any] = {
        "asof_date": parsed.isoformat(),
        "benchmark_ret_20d": None,
        "benchmark_ret_60d": None,
        "regime_name": None,
        "macro_risk_state": None,
        "risk_off_flag": None,
        "breadth_above_dma50_pct": None,
        "participation": "unknown",
    }
    try:
        regime = sql_to_df(
            f"""
            SELECT benchmark_ret_20d, benchmark_ret_60d, regime_name, macro_risk_state, risk_off_flag
            FROM {MARKET_REGIME_TABLE} WHERE asof_date <= %s ORDER BY asof_date DESC LIMIT 1
            """,
            params=(parsed,),
        )
        if not regime.empty:
            row = regime.iloc[0]
            inputs["benchmark_ret_20d"] = None if pd.isna(row.get("benchmark_ret_20d")) else float(row["benchmark_ret_20d"])
            inputs["benchmark_ret_60d"] = None if pd.isna(row.get("benchmark_ret_60d")) else float(row["benchmark_ret_60d"])
            inputs["regime_name"] = None if row.get("regime_name") is None else str(row["regime_name"])
            inputs["macro_risk_state"] = None if row.get("macro_risk_state") is None else str(row["macro_risk_state"]).upper()
            inputs["risk_off_flag"] = None if pd.isna(row.get("risk_off_flag")) else bool(row["risk_off_flag"])
    except Exception as exc:
        record_local_fallback_event(
            module="advisory.regime_admission_policy", source=MARKET_REGIME_TABLE, severity="warn",
            fallback_type="regime_admission_regime_load_failed",
            reason="Admission policy could not load the market regime row; state resolution degrades to neutral.",
            error=exc, metadata={"asof_date": str(asof_date)},
        )
    try:
        context = sql_to_df(
            f"""
            SELECT breadth_above_dma50_pct FROM {MARKET_CONTEXT_SUMMARY_TABLE}
            WHERE asof_date <= %s ORDER BY asof_date DESC LIMIT 1
            """,
            params=(parsed,),
        )
        if not context.empty and not pd.isna(context.iloc[0].get("breadth_above_dma50_pct")):
            inputs["breadth_above_dma50_pct"] = float(context.iloc[0]["breadth_above_dma50_pct"])
    except Exception as exc:
        record_local_fallback_event(
            module="advisory.regime_admission_policy", source=MARKET_CONTEXT_SUMMARY_TABLE, severity="warn",
            fallback_type="regime_admission_breadth_load_failed",
            reason="Admission policy could not load market-context breadth; state resolution degrades to neutral.",
            error=exc, metadata={"asof_date": str(asof_date)},
        )
    ret20 = inputs["benchmark_ret_20d"]
    ret60 = inputs["benchmark_ret_60d"]
    if ret20 is not None or ret60 is not None:
        # same thresholds as recommendation_diagnostics.summarize_market_participation_context
        if (ret20 is not None and ret20 >= UPTREND_RET20) or (ret60 is not None and ret60 >= UPTREND_RET60):
            inputs["participation"] = "benchmark_uptrend"
        elif (ret20 is not None and ret20 <= DOWNTREND_RET20) or (ret60 is not None and ret60 <= DOWNTREND_RET60):
            inputs["participation"] = "benchmark_downtrend"
        else:
            inputs["participation"] = "flat_or_mixed"
    return inputs


def resolve_raw_state(inputs: dict[str, Any]) -> str:
    macro = str(inputs.get("macro_risk_state") or "").upper()
    participation = str(inputs.get("participation") or "unknown")
    breadth = inputs.get("breadth_above_dma50_pct")
    if bool(inputs.get("risk_off_flag")) or macro in RISK_OFF_MACRO_STATES or participation == "benchmark_downtrend":
        return RISK_OFF
    if (
        participation == "benchmark_uptrend"
        and breadth is not None
        and float(breadth) >= RISK_ON_MIN_BREADTH_PCT
        and macro in RISK_ON_MACRO_STATES
    ):
        return RISK_ON
    return NEUTRAL


def _previous_policy_row(asof_date: pd.Timestamp) -> dict[str, Any] | None:
    frame = sql_to_df(
        f"SELECT asof_date, raw_state, effective_state, consecutive_days FROM {TABLE_NAME} "
        "WHERE asof_date < %s ORDER BY asof_date DESC LIMIT 1",
        params=(asof_date,),
    )
    return None if frame.empty else frame.iloc[0].to_dict()


def resolve_and_record(asof_date: Any | None = None) -> dict[str, Any]:
    """Resolve today's admission state (with hysteresis) and persist the decision row.

    Idempotent per asof_date (upsert). Flips INTO risk_off apply immediately; any other flip
    requires CONFIRM_DAYS consecutive raw days of the new state.
    """
    ensure_table()
    parsed = pd.to_datetime(asof_date, utc=True, errors="coerce")
    if pd.isna(parsed):
        parsed = pd.Timestamp.utcnow().normalize()
    parsed = parsed.normalize()
    inputs = resolve_inputs(parsed)
    raw_state = resolve_raw_state(inputs)
    previous = None
    try:
        previous = _previous_policy_row(parsed)
    except Exception as exc:
        record_local_fallback_event(
            module="advisory.regime_admission_policy", source=TABLE_NAME, severity="warn",
            fallback_type="regime_admission_history_load_failed",
            reason="Admission policy could not load the prior state; hysteresis restarts from neutral.",
            error=exc, metadata={"asof_date": str(parsed)},
        )
    prev_effective = str(previous.get("effective_state")) if previous else NEUTRAL
    prev_raw = str(previous.get("raw_state")) if previous else NEUTRAL
    prev_consecutive = int(previous.get("consecutive_days") or 0) if previous else 0

    consecutive = prev_consecutive + 1 if raw_state == prev_raw else 1
    if raw_state == prev_effective:
        effective = prev_effective
    elif raw_state == RISK_OFF:
        effective = RISK_OFF  # safety: tighten immediately
    elif consecutive >= max(1, CONFIRM_DAYS):
        effective = raw_state
    else:
        effective = prev_effective

    parameters = admission_parameters(effective)
    row = pd.DataFrame(
        [
            {
                "asof_date": parsed,
                "raw_state": raw_state,
                "effective_state": effective,
                "consecutive_days": consecutive,
                "inputs_json": json.dumps(inputs, ensure_ascii=False, sort_keys=True, default=str),
                "parameters_json": json.dumps(parameters, ensure_ascii=False, sort_keys=True),
                "load_ts": pd.Timestamp.utcnow(),
            }
        ]
    )
    upsert_to_db(row, TABLE_NAME, unique_keys=["asof_date"], timescaledb_column="asof_date")
    return {"asof_date": parsed.isoformat(), "raw_state": raw_state, "effective_state": effective,
            "consecutive_days": consecutive, "inputs": inputs, "parameters": parameters}


def resolve_active_policy(asof_date: Any | None = None, *, record_if_missing: bool = True) -> dict[str, Any]:
    """The admission parameters consumers use. Reads the recorded row for the date (recording it
    when absent); ANY failure returns neutral parameters == previous hardcoded behavior."""
    try:
        parsed = pd.to_datetime(asof_date, utc=True, errors="coerce")
        if pd.isna(parsed):
            parsed = pd.Timestamp.utcnow()
        parsed = parsed.normalize()
        frame = sql_to_df(
            f"SELECT effective_state, parameters_json FROM {TABLE_NAME} "
            "WHERE asof_date <= %s ORDER BY asof_date DESC LIMIT 1",
            params=(parsed,),
        )
        if not frame.empty and str(frame.iloc[0].get("effective_state") or ""):
            parameters = json.loads(frame.iloc[0].get("parameters_json") or "{}")
            if parameters.get("state") in STATES:
                return parameters
        if record_if_missing:
            return resolve_and_record(parsed)["parameters"]
    except Exception as exc:
        record_local_fallback_event(
            module="advisory.regime_admission_policy", source=TABLE_NAME, severity="warn",
            fallback_type="regime_admission_policy_resolve_failed",
            reason="Admission policy resolution failed; consumers use neutral (previous hardcoded) parameters.",
            error=exc, metadata={"asof_date": str(asof_date)},
        )
    return admission_parameters(NEUTRAL)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Resolve and record the daily regime admission state.")
    parser.add_argument("--date", default=None)
    parser.add_argument("--format", choices=["json", "text"], default="text")
    args = parser.parse_args(argv)
    result = resolve_and_record(args.date)
    if args.format == "json":
        print(json.dumps(result, indent=2, default=str))
    else:
        print(
            f"[regime_admission] asof={result['asof_date'][:10]} raw={result['raw_state']} "
            f"effective={result['effective_state']} consecutive={result['consecutive_days']} "
            f"scan_limit={result['parameters']['scan_limit']} "
            f"mcap_tolerance=({result['parameters']['market_cap_below_tolerance']}, {result['parameters']['market_cap_above_tolerance']}) "
            f"liquidity_floor_x={result['parameters']['liquidity_floor_multiplier']}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
