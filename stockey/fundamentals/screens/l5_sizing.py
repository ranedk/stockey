"""L5 position sizing -- fundamentals screener PRD §12 todo #8 (2026-08-29), the
never-built L5 layer from fundamental_basic_goal.md (§L5: "Portfolio construction").

A CALCULATOR, not a decision-maker or an executor: it computes a position size for
a company that already has an OPEN fundamentals_l4_thesis row -- i.e. a human has
already done the one deliberate act this whole pipeline gates capital on (PRD §1/
§3.3: "no LLM makes a capital decision... L4 is the only gate capital passes
through"). No thesis, no size -- return None, never guess at a number for a
company nobody has committed to yet. Nothing here writes to the database or places
an order; a human reads the number and decides what to do with it, same as every
other read-only screen in this package.

Two mechanical rules, both taken directly from the source spec, neither tuned or
LLM-judged:
- "Size off ADV, not conviction" (fundamental_basic_goal.md §L5) -- the position is
  capped at a fixed fraction of the company's own average daily traded VALUE
  (avg_vol_1mth x cmp_rs, both already collected at L1 admission -- no new data
  source needed), so a position can always be exited without being the trade that
  moves the market. This is a LIQUIDITY ceiling, never a reason to size UP.
- "12-20 concurrent positions... below ~10, a 20% hit rate gives a material chance
  of holding zero winners over a cycle" -- the other half of the size is an
  equal-capital split across a target position count, an operator-supplied
  parameter (no default baked in here -- this module does not know the operator's
  actual sleeve capital or how many names they intend to hold, and guessing either
  would be exactly the kind of silent assumption this pipeline avoids everywhere
  else).

The recommended size is the SMALLER of the two -- equal-weight is the plan, ADV is
the ceiling that can only pull it down, never up. Deliberately NOT implementing the
source spec's optional 200-DMA reclaim entry gate here (PRD §12 todo #8 scope is
sizing; that gate is a timing/entry rule, a separate concern, and the source spec
itself calls it optional)."""
from __future__ import annotations

import json

from utils.company_master import build_l1_ticker_by_company_master_id
from utils.db import sql_to_df

SYNC_SOURCE_NAME = "fundamentals.screens.l5_sizing"

# Fraction of a company's own average daily traded value a single position may
# occupy -- a plain constant, not backtested (this whole module is a calculator
# for a layer the source spec always intended to exist; PRD §12 todo #7's
# calibration loop is what will eventually tell us if a DIFFERENT cap does
# better, not a re-guess here).
DEFAULT_MAX_PCT_OF_ADV = 0.10


def compute_position_size(
    *, equal_weight_capital_rs: float, adv_value_rs: float | None, max_pct_of_adv: float = DEFAULT_MAX_PCT_OF_ADV
) -> dict[str, object]:
    """Pure arithmetic -- no DB access, no company identity, so every rounding/
    tie-breaking decision is independently testable without mocking anything.
    adv_value_rs=None (no liquidity data at all) means the ADV cap can't be
    computed -- falls back to equal_weight_capital_rs alone rather than silently
    treating unknown liquidity as infinite (which a missing cap would otherwise
    imply) or zero (which would wrongly zero out the position)."""
    if adv_value_rs is None:
        return {
            "equal_weight_capital_rs": round(equal_weight_capital_rs, 2),
            "adv_cap_rs": None,
            "recommended_size_rs": round(equal_weight_capital_rs, 2),
            "binding_constraint": "equal_weight_no_adv_data",
        }
    adv_cap_rs = adv_value_rs * max_pct_of_adv
    if adv_cap_rs < equal_weight_capital_rs:
        return {
            "equal_weight_capital_rs": round(equal_weight_capital_rs, 2),
            "adv_cap_rs": round(adv_cap_rs, 2),
            "recommended_size_rs": round(adv_cap_rs, 2),
            "binding_constraint": "adv_liquidity_cap",
        }
    return {
        "equal_weight_capital_rs": round(equal_weight_capital_rs, 2),
        "adv_cap_rs": round(adv_cap_rs, 2),
        "recommended_size_rs": round(equal_weight_capital_rs, 2),
        "binding_constraint": "equal_weight",
    }


def load_open_thesis(company_master_id: str) -> dict | None:
    """The single OPEN thesis for this company, if any -- the human gate this
    whole module refuses to size around the absence of. A company can only ever
    have one open thesis at a time in practice (create_thesis's own idempotency
    key), but this reads defensively (LIMIT 1, most recent) rather than assuming."""
    df = sql_to_df(
        "SELECT thesis_id, created_date FROM fundamentals_l4_thesis WHERE company_master_id = %s AND status = 'open' ORDER BY created_date DESC LIMIT 1",
        params=(company_master_id,),
    )
    if df.empty:
        return None
    return df.iloc[0].to_dict()


def load_adv_inputs(company_master_id: str) -> dict | None:
    """avg_vol_1mth x cmp_rs from the LATEST fundamentals_l1_universe row for this
    company -- both already collected at L1 admission (l1_universe.py's own
    metrics_json), no new data source needed. None if this company has no L1 row
    (e.g. an ad_hoc thesis on a name that never passed L1 screening) or the metrics
    are missing/non-numeric -- never a guessed/defaulted ADV."""
    ticker_by_cmid = build_l1_ticker_by_company_master_id()
    ticker = ticker_by_cmid.get(company_master_id)
    if ticker is None:
        return None
    df = sql_to_df(
        "SELECT metrics_json FROM fundamentals_l1_universe WHERE ticker = %s ORDER BY run_date DESC LIMIT 1",
        params=(ticker,),
    )
    if df.empty or not df.iloc[0]["metrics_json"]:
        return None
    try:
        metrics = json.loads(df.iloc[0]["metrics_json"])
    except (TypeError, ValueError):
        return None
    avg_vol_1mth = metrics.get("avg_vol_1mth")
    cmp_rs = metrics.get("cmp_rs")
    if not isinstance(avg_vol_1mth, (int, float)) or not isinstance(cmp_rs, (int, float)):
        return None
    return {"avg_vol_1mth": avg_vol_1mth, "cmp_rs": cmp_rs, "adv_value_rs": avg_vol_1mth * cmp_rs}


def get_position_size_recommendation(
    company_master_id: str, *, total_capital_rs: float, target_position_count: int, max_pct_of_adv: float = DEFAULT_MAX_PCT_OF_ADV
) -> dict | None:
    """None if there's no OPEN L4 thesis for this company -- the calculator refuses
    to size a position nobody has committed to. total_capital_rs and
    target_position_count are REQUIRED, no defaults: this module has no business
    guessing the operator's actual sleeve capital or intended position count, and
    a wrong guess here would be a silent, consequential assumption -- exactly what
    this pipeline avoids everywhere else (see e.g. utils/universe.py's own "no
    static registry" principle)."""
    thesis = load_open_thesis(company_master_id)
    if thesis is None:
        return None
    if target_position_count <= 0:
        raise ValueError("target_position_count must be positive")

    adv_inputs = load_adv_inputs(company_master_id)
    sizing = compute_position_size(
        equal_weight_capital_rs=total_capital_rs / target_position_count,
        adv_value_rs=adv_inputs["adv_value_rs"] if adv_inputs else None,
        max_pct_of_adv=max_pct_of_adv,
    )
    return {
        "company_master_id": company_master_id,
        "thesis_id": thesis["thesis_id"],
        "total_capital_rs": total_capital_rs,
        "target_position_count": target_position_count,
        "max_pct_of_adv": max_pct_of_adv,
        "avg_vol_1mth": adv_inputs["avg_vol_1mth"] if adv_inputs else None,
        "cmp_rs": adv_inputs["cmp_rs"] if adv_inputs else None,
        **sizing,
    }
