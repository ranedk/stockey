"""Factor tilt -- the ONE connection from a graduated factor to live selection, and its selection-shadow.

The graduation harness (advisory.factor_graduation) can mark a factor `active` with a small, bounded,
reversible `applied_weight` (0 unless the operator opts in via FACTOR_GRADUATION_APPLY_ENABLED). This module
is the single, auditable place that weight touches the selector, and it is built so the default is a strict
no-op:

  * `blend_selection_score` returns the base column UNCHANGED when there are no active weights, so the
    review-only selector picks exactly the pure-RS top-N it does today.
  * When (and only when) a factor has graduated AND the operator has opted in AND its column is present in
    the selection panel, the score becomes a bounded rank blend: rank(base) + sum(w_f * rank(factor)). The
    RS floor gate still runs first, so a graduated factor only REFINES the ordering within the already-strong
    RS pool -- it can never pull in a weak name.

Because the regime shadow ledger measures SIZING (not selection), the honest instrument for a selection
tilt is a SELECTION shadow: for each date, compare the pure-RS top-N vs the tilted top-N (built with the
PROPOSED weights, so it measures even while apply is OFF), and report the turnover it would cause and the
forward benchmark-excess delta. That is how you SEE the effect before anything real leans on it.

Review/research-only. Consumed by advisory.paper_advisory. CLI: `python -m advisory.factor_tilt [--measure]`.
"""
from __future__ import annotations

import argparse
import json
from typing import Any

import numpy as np
import pandas as pd

from advisory import factor_graduation as fg
from utils.db import sql_to_df

SELECTION_SCORE_COL = "_tilt_selection_score"


def load_active_weights(*, prefer: str = "applied") -> dict[str, float]:
    """{factor: weight} for factors currently `active` in the latest graduation run. `prefer='applied'`
    returns the live weight (0 unless the operator opted in -> usually empty); `prefer='proposed'` returns
    the counterfactual weight the shadow uses to measure. Empty on any error / no active factors."""
    col = "applied_weight" if prefer == "applied" else "proposed_weight"
    try:
        df = sql_to_df(
            f"""
            SELECT g.factor, g.{col} AS w FROM {fg.TABLE_NAME} g
            JOIN (SELECT MAX(run_date) mx FROM {fg.TABLE_NAME}) m ON g.run_date = m.mx
            WHERE g.state = %(active)s
            """,
            params={"active": fg.STATE_ACTIVE},
        )
    except Exception:
        return {}
    out: dict[str, float] = {}
    for _, r in df.iterrows():
        w = r["w"]
        if w is not None and np.isfinite(w) and w > 0:
            out[str(r["factor"])] = float(w)
    return out


def blend_selection_score(day: pd.DataFrame, *, base_col: str, weights: dict[str, float] | None) -> pd.Series:
    """Bounded rank blend. IDENTITY (returns base_col unchanged) when there are no usable active weights,
    so selection is byte-for-byte the pure-base ranking. Factors absent from `day` are skipped (a graduated
    factor whose column the panel doesn't carry simply contributes nothing -- fail-safe, never fail-loud)."""
    usable = {f: w for f, w in (weights or {}).items() if f in day.columns} if weights else {}
    if not usable:
        return day[base_col]
    score = day[base_col].rank(pct=True)
    for f, w in usable.items():
        score = score + w * day[f].rank(pct=True)
    return score


def select_top(day: pd.DataFrame, *, base_col: str, gate_col: str, gate_min: float, n: int,
               weights: dict[str, float] | None = None) -> pd.DataFrame:
    """Apply the RS floor gate, then take the top-n by the (possibly tilted) selection score. With no active
    weights this is exactly `gated.sort_values(base_col, desc).head(n)` -- the current behavior."""
    gated = day[day[gate_col] >= gate_min].copy()
    if gated.empty:
        return gated
    gated[SELECTION_SCORE_COL] = blend_selection_score(gated, base_col=base_col, weights=weights)
    return gated.sort_values([SELECTION_SCORE_COL, base_col], ascending=False).head(n)


def measure_selection_shadow(ex: pd.DataFrame, weights: dict[str, float], *, base_col: str = "rs_percentile",
                             gate_col: str = "rs_percentile", gate_min: float = 80.0, n: int = 20) -> dict[str, Any]:
    """Per-date counterfactual: pure-base top-n vs tilted top-n (built with `weights`). Reports the turnover
    the tilt would cause and the mean forward benchmark-excess of each book (and the delta). `ex` must carry
    `date`, `excess`, base/gate cols and the factor columns. Empty `weights` -> zero effect (honest)."""
    if ex.empty:
        return {"note": "empty panel"}
    changed, base_ex, tilt_ex, dates = [], [], [], 0
    for _, day in ex.groupby("date"):
        base_pick = select_top(day, base_col=base_col, gate_col=gate_col, gate_min=gate_min, n=n, weights={})
        tilt_pick = select_top(day, base_col=base_col, gate_col=gate_col, gate_min=gate_min, n=n, weights=weights)
        if base_pick.empty:
            continue
        dates += 1
        bset, tset = set(base_pick["symbol"]), set(tilt_pick["symbol"])
        changed.append(len(tset - bset))
        base_ex.append(float(base_pick["excess"].mean()))
        tilt_ex.append(float(tilt_pick["excess"].mean()))
    if not dates:
        return {"note": "no gated dates"}
    b, t = float(np.mean(base_ex)), float(np.mean(tilt_ex))
    return {"dates": dates, "active_weights": weights, "mean_names_changed_per_date": round(float(np.mean(changed)), 3),
            "base_fwd_excess": round(b, 5), "tilt_fwd_excess": round(t, 5), "tilt_minus_base": round(t - b, 5),
            "note": "no active graduated factors -- tilt is a no-op" if not weights else "measured with PROPOSED weights"}


def run_measurement() -> dict[str, Any]:
    """Run the selection shadow with the PROPOSED graduated weights. Short-circuits on a cheap SQL check --
    an `active` factor is the only thing that produces a proposed weight, so with nothing active we skip the
    ~100s panel rebuild entirely (the weekly cron stays near-free until there is something real to measure)."""
    weights = load_active_weights(prefer="proposed")
    if not weights:
        return {"active_weights": {}, "skipped_rebuild": True,
                "note": "no active graduated factors -- nothing to measure (skipped panel rebuild)"}
    from advisory import factor_ic_sweep as fis
    from advisory import subscore_ic as sic
    panel, bench = fis.build_factor_panel()
    if panel.empty:
        return {"note": "no panel"}
    ex = sic.attach_excess(panel, bench, fis.HORIZON)
    ex = fis.attach_derived(ex)
    return measure_selection_shadow(ex, weights)


def main() -> None:
    ap = argparse.ArgumentParser(description="Factor tilt: the graduated-factor->selection connection + selection shadow.")
    ap.add_argument("--measure", action="store_true", help="build the panel and measure the tilt's selection effect (heavy)")
    ap.add_argument("--format", choices=["text", "json"], default="text")
    args = ap.parse_args()
    if args.measure:
        result = run_measurement()
    else:
        applied = load_active_weights(prefer="applied")
        proposed = load_active_weights(prefer="proposed")
        result = {"applied_weights_live": applied, "proposed_weights_shadow": proposed,
                  "note": "no active graduated factors" if not proposed else "active factors present"}
    if args.format == "json":
        print(json.dumps(result, default=str, indent=2))
    else:
        if args.measure and "dates" in result:
            print(f"selection shadow (proposed weights={result['active_weights'] or 'none'}, dates={result['dates']}): "
                  f"names changed/date={result['mean_names_changed_per_date']}, "
                  f"base excess={result['base_fwd_excess']:+.4f} -> tilt excess={result['tilt_fwd_excess']:+.4f} "
                  f"(delta {result['tilt_minus_base']:+.4f})\n{result['note']}")
        else:
            print(json.dumps(result, default=str))


if __name__ == "__main__":
    main()
