"""Conditional strategy allocation (Phase A): size the momentum sleeve by market state.

The base-breakout vs momentum mix is a RUNTIME policy, not a blanket choice (spec 3c). A
momentum sleeve should lean in when the environment is hospitable (broad uptrend, healthy
breadth) and be starved when it is fragile -- because momentum's failure mode is a
*correlated factor unwind* that per-trade sizing cannot see, so the control is a
portfolio-level sleeve cap conditioned on regime.

Phase A is rule-based and deterministic (regime admission state -> momentum cap), with the
guards from the spec baked in:
  - momentum cap never exceeds MOMENTUM_SLEEVE_MAX_PCT (never let momentum dominate the book),
  - base-breakout core is always preserved (the momentum cap is a fraction, < 1),
  - risk_off/neutral/risk_on gradation (the forward regime signal),
Every day's decision is a recorded row. Fail-open to the neutral cap. Phase B
(performance-following adaptation + critic tuning) layers on later.
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

TABLE_NAME = "advisory_strategy_allocation_policy"
MIGRATION_ID = "20260712_advisory_strategy_allocation_policy_base"
SCHEMA_STATEMENTS = [
    f"""
    CREATE TABLE IF NOT EXISTS {TABLE_NAME} (
        asof_date TIMESTAMPTZ NOT NULL,
        regime_state TEXT,
        momentum_sleeve_cap_pct DOUBLE PRECISION,
        inputs_json TEXT,
        load_ts TIMESTAMPTZ,
        UNIQUE (asof_date)
    )
    """,
]

MOMENTUM_ARCHETYPE = "momentum"
# per-state momentum sleeve caps (env-tunable; the critic tunes these in Phase B)
MOMENTUM_CAP_RISK_ON = float(os.getenv("MOMENTUM_SLEEVE_CAP_RISK_ON_PCT", "0.40"))
MOMENTUM_CAP_NEUTRAL = float(os.getenv("MOMENTUM_SLEEVE_CAP_NEUTRAL_PCT", "0.25"))
MOMENTUM_CAP_RISK_OFF = float(os.getenv("MOMENTUM_SLEEVE_CAP_RISK_OFF_PCT", "0.10"))
MOMENTUM_SLEEVE_MAX_PCT = float(os.getenv("MOMENTUM_SLEEVE_MAX_PCT", "0.50"))  # never dominate the book


def ensure_table() -> None:
    apply_schema_migration(
        migration_id=MIGRATION_ID,
        description="Record the daily conditional momentum-sleeve allocation cap (Phase A, regime-conditioned).",
        statements=SCHEMA_STATEMENTS,
        metadata={"module": "advisory.strategy_allocation_policy", "tables": [TABLE_NAME]},
    )


def _momentum_cap_for_state(state: str) -> float:
    normalized = str(state or "").strip().lower()
    if normalized == "risk_on":
        cap = MOMENTUM_CAP_RISK_ON
    elif normalized == "risk_off":
        cap = MOMENTUM_CAP_RISK_OFF
    else:
        cap = MOMENTUM_CAP_NEUTRAL
    return max(0.0, min(float(cap), MOMENTUM_SLEEVE_MAX_PCT))  # guard: never dominate the book


def resolve_and_record(asof_date: Any | None = None) -> dict[str, Any]:
    ensure_table()
    parsed = pd.to_datetime(asof_date, utc=True, errors="coerce")
    if pd.isna(parsed):
        parsed = pd.Timestamp.utcnow()
    parsed = parsed.normalize()
    # forward regime signal from the admission policy (read-only; fail-open to neutral)
    try:
        from advisory.regime_admission_policy import resolve_active_policy as resolve_admission

        state = str(resolve_admission(parsed, record_if_missing=False).get("state") or "neutral")
    except Exception:
        state = "neutral"
    cap = _momentum_cap_for_state(state)
    row = pd.DataFrame([
        {
            "asof_date": parsed, "regime_state": state, "momentum_sleeve_cap_pct": cap,
            "inputs_json": json.dumps({"regime_state": state, "sleeve_max_pct": MOMENTUM_SLEEVE_MAX_PCT}, sort_keys=True),
            "load_ts": pd.Timestamp.utcnow(),
        }
    ])
    upsert_to_db(row, TABLE_NAME, unique_keys=["asof_date"], timescaledb_column="asof_date")
    return {"asof_date": parsed.isoformat(), "regime_state": state, "momentum_sleeve_cap_pct": cap}


def resolve_momentum_sleeve_cap(asof_date: Any | None = None, *, record_if_missing: bool = True) -> float:
    """The momentum sleeve's effective portfolio cap for the date. ANY failure returns the
    neutral cap -- fail-open to a defensible middle, never to an unbounded sleeve."""
    try:
        parsed = pd.to_datetime(asof_date, utc=True, errors="coerce")
        if pd.isna(parsed):
            parsed = pd.Timestamp.utcnow()
        parsed = parsed.normalize()
        frame = sql_to_df(
            f"SELECT asof_date, momentum_sleeve_cap_pct FROM {TABLE_NAME} "
            "WHERE asof_date <= %s ORDER BY asof_date DESC LIMIT 1",
            params=(parsed,),
        )
        if not frame.empty:
            row_date = pd.to_datetime(frame.iloc[0].get("asof_date"), utc=True, errors="coerce")
            cap = frame.iloc[0].get("momentum_sleeve_cap_pct")
            if not pd.isna(row_date) and row_date.normalize() == parsed and cap is not None and not pd.isna(cap):
                return float(cap)
        if record_if_missing:
            return float(resolve_and_record(parsed)["momentum_sleeve_cap_pct"])
        if not frame.empty and frame.iloc[0].get("momentum_sleeve_cap_pct") is not None:
            return float(frame.iloc[0]["momentum_sleeve_cap_pct"])
    except Exception as exc:
        record_local_fallback_event(
            module="advisory.strategy_allocation_policy", source=TABLE_NAME, severity="warn",
            fallback_type="strategy_allocation_resolve_failed",
            reason="Strategy allocation resolution failed; momentum sleeve uses the neutral cap.",
            error=exc, metadata={"asof_date": str(asof_date)},
        )
    return _momentum_cap_for_state("neutral")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Resolve and record the daily conditional momentum-sleeve cap.")
    parser.add_argument("--date", default=None)
    parser.add_argument("--format", choices=["text", "json"], default="text")
    args = parser.parse_args(argv)
    result = resolve_and_record(args.date)
    if args.format == "json":
        print(json.dumps(result, indent=2, default=str))
    else:
        print(f"[strategy_allocation] asof={result['asof_date'][:10]} regime={result['regime_state']} "
              f"momentum_sleeve_cap={result['momentum_sleeve_cap_pct']:.2f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
