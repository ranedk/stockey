"""Factor graduation harness (disciplined self-adaptive layer, proposal-only by default).

The monitor (`advisory.factor_ic_sweep`) measures every candidate factor's edge each run. This harness is
the *governor* on top of it: it decides -- with an auditable state machine -- whether a candidate signal
has earned a small, bounded, REVERSIBLE weight, and it auto-reverts the moment the edge breaks. It exists
because the honest research finding is that ~13 independent time-blocks cannot support prescriptive daily
re-fitting (spec s8.1); the only safe way to adapt is to make a NEW signal clear a high, persistent bar
before it earns any weight, and to pull that weight back automatically the instant it stops clearing.

The gates a candidate must clear EVERY run to be `eligible` (alpha, not beta, not luck, not stale):
  * FDR-significant across the whole sweep (multiple-testing corrected)          -- not luck
  * bootstrap CI excludes zero AND walk-forward sign-consistent                  -- not noise
  * IC positive in BOTH regimes (favorable AND unfavorable tape)                 -- alpha, not beta
  * drift status green (recent window still holding the full-sample edge)        -- not decayed
  * enough matured sampled dates behind the estimate                            -- enough power

State machine (per candidate; in-use base signals are excluded -- they are the system, not candidates):
  observing --K consecutive eligible runs--> graduating --(reach K)--> active (earns bounded weight)
  active/graduating --any non-eligible run (incl. drift red)--> reverted (weight -> 0, must rebuild)

Bounded + reversible: an `active` factor's proposed weight is a small, capped function of its IC; on
revert it drops straight to 0. Proposal-only: the applied weight is 0 unless FACTOR_GRADUATION_APPLY_ENABLED
is explicitly turned on (DEFAULTS OFF), mirroring the master-enable-flag principle -- until an operator
opts in, this harness proposes and audits, it does not tilt the live scorer. Writes
`advisory_factor_graduation`; CLI: `python -m advisory.factor_graduation [--dry-run] [--format text|json]`.
"""
from __future__ import annotations

import argparse
import json
import os
from typing import Any

import numpy as np
import pandas as pd

from advisory import factor_ic_sweep as fis
from utils.db import sql_to_df, upsert_to_db
from utils.schema_migrations import apply_schema_migration

K_GRADUATE = int(os.getenv("FACTOR_GRADUATION_MIN_CONSECUTIVE_RUNS", "4"))
MIN_MATURED_DATES = int(os.getenv("FACTOR_GRADUATION_MIN_MATURED_DATES", "20"))
WEIGHT_CAP = float(os.getenv("FACTOR_GRADUATION_WEIGHT_CAP", "0.15"))
WEIGHT_PER_IC = float(os.getenv("FACTOR_GRADUATION_WEIGHT_PER_IC", "2.0"))
APPLY_ENABLED = os.getenv("FACTOR_GRADUATION_APPLY_ENABLED", "0").strip().lower() in ("1", "true", "yes", "on")

STATE_OBSERVING = "observing"
STATE_GRADUATING = "graduating"
STATE_ACTIVE = "active"
STATE_REVERTED = "reverted"

TABLE_NAME = "advisory_factor_graduation"
MIGRATION_ID = "20260716_advisory_factor_graduation"
SCHEMA_STATEMENTS = [
    f"""
    CREATE TABLE IF NOT EXISTS {TABLE_NAME} (
        run_date TIMESTAMPTZ NOT NULL,
        factor TEXT NOT NULL,
        family TEXT,
        horizon_days BIGINT NOT NULL,
        eligible_this_run BOOLEAN,
        consecutive_eligible_runs BIGINT,
        prev_state TEXT,
        state TEXT,
        mean_ic DOUBLE PRECISION,
        fav_ic DOUBLE PRECISION,
        unf_ic DOUBLE PRECISION,
        fdr_significant BOOLEAN,
        drift_status TEXT,
        n_dates BIGINT,
        proposed_weight DOUBLE PRECISION,
        applied_weight DOUBLE PRECISION,
        apply_enabled BOOLEAN,
        gate_reasons TEXT,
        load_ts TIMESTAMPTZ,
        UNIQUE (run_date, factor, horizon_days)
    )
    """,
]


def ensure_table() -> None:
    apply_schema_migration(
        migration_id=MIGRATION_ID,
        statements=SCHEMA_STATEMENTS,
        owner="advisory.factor_graduation",
        description="Disciplined factor-graduation state machine (bounded reversible weight, proposal-only by default).",
        metadata={"module": "advisory.factor_graduation", "tables": [TABLE_NAME]},
    )


# ------------------------------------------------------------------------------------------------
# Pure decision logic (unit-tested without DB)
# ------------------------------------------------------------------------------------------------
def evaluate_eligibility(row: dict, *, min_matured: int = MIN_MATURED_DATES) -> tuple[bool, list[str]]:
    """Every gate must pass for the candidate to be eligible this run. Returns (eligible, failing reasons)."""
    reasons: list[str] = []
    if not row.get("fdr_significant"):
        reasons.append("not_fdr_significant")
    if not row.get("ci_excludes_zero"):
        reasons.append("ci_includes_zero")
    if not row.get("wf_consistent"):
        reasons.append("walk_forward_inconsistent")
    fav, unf = row.get("fav_ic"), row.get("unf_ic")
    if fav is None or not (fav > 0):
        reasons.append("fav_ic<=0")
    if unf is None or not (unf > 0):
        reasons.append("unf_ic<=0_beta_not_alpha")
    if row.get("drift_status") != "green":
        reasons.append(f"drift_{row.get('drift_status')}")
    nd = row.get("n_dates") or 0
    if nd < min_matured:
        reasons.append(f"matured_dates<{min_matured}")
    return (len(reasons) == 0, reasons)


def next_consecutive(prev_consecutive: int, eligible_this_run: bool) -> int:
    """Consecutive-eligible-run counter: +1 when eligible, reset to 0 otherwise."""
    return (int(prev_consecutive or 0) + 1) if eligible_this_run else 0


def next_state(prev_state: str | None, *, consecutive_eligible: int, eligible_this_run: bool,
               k_graduate: int = K_GRADUATE) -> tuple[str, str]:
    """State transition. Auto-revert (active/graduating + non-eligible run) takes priority over promotion.
    Eligibility already requires drift green, so a drift-red run lands here as `not eligible_this_run`."""
    prev = prev_state or STATE_OBSERVING
    if not eligible_this_run:
        if prev in (STATE_ACTIVE, STATE_GRADUATING):
            return (STATE_REVERTED, "auto-revert: eligibility lost (edge no longer clears the gates)")
        if prev == STATE_REVERTED:
            return (STATE_REVERTED, "remains reverted: not re-eligible")
        return (STATE_OBSERVING, "not yet eligible")
    if consecutive_eligible >= k_graduate:
        return (STATE_ACTIVE, f"graduated: {consecutive_eligible} consecutive eligible runs >= {k_graduate}")
    return (STATE_GRADUATING, f"accruing: {consecutive_eligible}/{k_graduate} consecutive eligible runs")


def proposed_weight(state: str, mean_ic: float, *, cap: float = WEIGHT_CAP,
                    per_ic: float = WEIGHT_PER_IC) -> float:
    """Bounded weight an `active` factor would earn: small, IC-scaled, hard-capped. Zero unless active."""
    if state != STATE_ACTIVE or mean_ic is None or not np.isfinite(mean_ic) or mean_ic <= 0:
        return 0.0
    return float(min(cap, mean_ic * per_ic))


def applied_weight(proposed: float, *, apply_enabled: bool = APPLY_ENABLED) -> float:
    """The weight actually handed to the live system. 0 unless the operator has opted in (default OFF)."""
    return float(proposed) if apply_enabled else 0.0


# ------------------------------------------------------------------------------------------------
# DB load + run
# ------------------------------------------------------------------------------------------------
def load_latest_sweep() -> pd.DataFrame:
    """The most recent monitor snapshot (one row per factor at its latest run_date)."""
    df = sql_to_df(
        f"""
        SELECT s.* FROM {fis.TABLE_NAME} s
        JOIN (SELECT MAX(run_date) mx FROM {fis.TABLE_NAME}) m ON s.run_date = m.mx
        """
    )
    return df


def load_prior_states(before_run_date: pd.Timestamp) -> dict[str, dict]:
    """Latest graduation record per factor STRICTLY BEFORE this run (for consecutive counting + prev_state)."""
    try:
        df = sql_to_df(
            f"""
            SELECT DISTINCT ON (factor) factor, state, consecutive_eligible_runs, run_date
            FROM {TABLE_NAME}
            WHERE run_date < %(rd)s
            ORDER BY factor, run_date DESC
            """,
            params={"rd": before_run_date},
        )
    except Exception:
        return {}
    return {r["factor"]: r.to_dict() for _, r in df.iterrows()}


def run_graduation(*, dry_run: bool = False) -> dict[str, Any]:
    sweep = load_latest_sweep()
    if sweep.empty:
        return {"records": [], "note": "no monitor snapshot -- run advisory.factor_ic_sweep first"}
    run_date = pd.Timestamp(sweep["run_date"].max())
    prior = load_prior_states(run_date)
    horizon = int(sweep["horizon_days"].iloc[0]) if "horizon_days" in sweep else fis.HORIZON

    records: list[dict[str, Any]] = []
    for _, r in sweep.iterrows():
        if bool(r.get("in_use")):
            continue  # base signals are the system, not graduation candidates
        row = r.to_dict()
        eligible, reasons = evaluate_eligibility(row)
        p = prior.get(row["factor"], {})
        prev_state = p.get("state")
        consecutive = next_consecutive(p.get("consecutive_eligible_runs", 0), eligible)
        state, gate = next_state(prev_state, consecutive_eligible=consecutive, eligible_this_run=eligible)
        if not eligible and reasons:
            gate = f"{gate} [{', '.join(reasons)}]"
        pw = proposed_weight(state, row.get("mean_ic"))
        aw = applied_weight(pw)
        records.append({
            "run_date": run_date, "factor": row["factor"], "family": row.get("family"),
            "horizon_days": horizon, "eligible_this_run": bool(eligible),
            "consecutive_eligible_runs": int(consecutive), "prev_state": prev_state, "state": state,
            "mean_ic": row.get("mean_ic"), "fav_ic": row.get("fav_ic"), "unf_ic": row.get("unf_ic"),
            "fdr_significant": bool(row.get("fdr_significant")), "drift_status": row.get("drift_status"),
            "n_dates": int(row.get("n_dates") or 0), "proposed_weight": round(pw, 6),
            "applied_weight": round(aw, 6), "apply_enabled": APPLY_ENABLED, "gate_reasons": gate,
            "load_ts": pd.Timestamp.utcnow(),
        })
    if records and not dry_run:
        ensure_table()
        upsert_to_db(pd.DataFrame(records), TABLE_NAME,
                     unique_keys=["run_date", "factor", "horizon_days"], timescaledb_column="run_date")

    active = [r for r in records if r["state"] == STATE_ACTIVE]
    graduating = [r for r in records if r["state"] == STATE_GRADUATING]
    reverted = [r for r in records if r["state"] == STATE_REVERTED]
    return {"records": records, "run_date": str(run_date.date()), "apply_enabled": APPLY_ENABLED,
            "n_candidates": len(records), "active": [r["factor"] for r in active],
            "graduating": [r["factor"] for r in graduating], "reverted": [r["factor"] for r in reverted]}


def render_report(result: dict[str, Any]) -> str:
    recs = result.get("records", [])
    if not recs:
        return f"factor graduation: {result.get('note', 'no records')}"
    apply_note = "APPLY ENABLED (weights tilt the live scorer)" if result.get("apply_enabled") \
        else "proposal-only (apply flag OFF -- applied_weight forced to 0)"
    order = {STATE_ACTIVE: 0, STATE_GRADUATING: 1, STATE_REVERTED: 2, STATE_OBSERVING: 3}
    recs = sorted(recs, key=lambda r: (order.get(r["state"], 9), -(r["mean_ic"] or -9)))
    lines = [f"factor graduation  (run_date={result.get('run_date')}, candidates={result.get('n_candidates')}, "
             f"K={K_GRADUATE}, {apply_note})",
             f"{'factor':<32}{'state':<11}{'elig':>5}{'run':>5}{'IC':>8}{'unf':>8}{'wprop':>7}{'wappl':>7}  why"]
    for r in recs:
        lines.append(
            f"{r['factor']:<32}{r['state']:<11}{('Y' if r['eligible_this_run'] else '.'):>5}"
            f"{r['consecutive_eligible_runs']:>5}{fis._f(r['mean_ic']):>8}{fis._f(r['unf_ic']):>8}"
            f"{r['proposed_weight']:>7.3f}{r['applied_weight']:>7.3f}  {r['gate_reasons']}")
    lines.append("")
    lines.append(f"ACTIVE (earning weight): {result.get('active') or 'none'}")
    lines.append(f"GRADUATING (accruing):   {result.get('graduating') or 'none'}")
    lines.append(f"REVERTED (edge broke):   {result.get('reverted') or 'none'}")
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser(description="Factor graduation harness (bounded reversible, proposal-only by default).")
    ap.add_argument("--dry-run", action="store_true", help="compute + print, do not persist")
    ap.add_argument("--format", choices=["text", "json"], default="text")
    args = ap.parse_args()
    result = run_graduation(dry_run=args.dry_run)
    if args.format == "json":
        print(json.dumps(result, default=str, indent=2))
    else:
        print(render_report(result))


if __name__ == "__main__":
    main()
