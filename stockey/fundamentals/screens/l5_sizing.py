"""L5 position sizing -- fundamentals screener PRD §12 todo #8 (2026-08-29), the
never-built L5 layer from fundamental_basic_goal.md (§L5: "Portfolio construction").

A CALCULATOR, not a decision-maker or an executor: it computes a position size for
a company that already has an OPEN position in fundamentals_portfolio_position --
i.e. the mechanical ruleset passed it and the entry adjudicator did not veto it.
No open position, no size -- return None, never guess at a number for a company
nothing has committed to yet.

CHANGED 2026-09-04: this gate used to be an OPEN fundamentals_l4_thesis row, the
one deliberate human act the pipeline gated capital on. The operator removed the
human forecast entirely ("only machine created portfolio and forecast"), so the
gate moved to the machine portfolio rather than being deleted -- an ungated sizer
would happily size any company on the watchlist, which is a much larger change
than the one that was asked for. Nothing here writes to the database or places an
order.

Two mechanical rules, both taken directly from the source spec, neither tuned or
LLM-judged:
- "Size off ADV, not conviction" (fundamental_basic_goal.md §L5) -- the position is
  capped at a fixed fraction of the company's own average daily traded VALUE
  (avg_vol_1mth x cmp_rs, both already collected at L1 admission -- no new data
  source needed), so a position can always be exited without being the trade that
  moves the market. This is a LIQUIDITY ceiling, never a reason to size UP.
- A FLAT allocation per position, operator-set 2026-09-04: Rs 1,00,000 per name, up
  to 100 names. This replaced an equal-capital split across a target position count.
  The difference matters: under a split, every new name shrinks every existing one,
  so position size depends on how many OTHER candidates happened to pass that day --
  a name's allocation moving because an unrelated company qualified is not a
  decision anyone made. A flat allocation makes each position independent, and the
  book simply grows until it hits its ceiling.

The recommended size is the SMALLER of the flat allocation and the ADV cap -- the
flat allocation is the plan, ADV is a LIQUIDITY ceiling that can only pull it down,
never up. Deliberately NOT implementing the
source spec's optional 200-DMA reclaim entry gate here (PRD §12 todo #8 scope is
sizing; that gate is a timing/entry rule, a separate concern, and the source spec
itself calls it optional)."""
from __future__ import annotations

import json

from environs import Env

from utils.company_master import build_l1_ticker_by_company_master_id
from utils.db import sql_to_df

env = Env()
env.read_env()

SYNC_SOURCE_NAME = "fundamentals.screens.l5_sizing"

# Operator-set 2026-09-04: "assume we invest 1 Lacs in each ... we can go upto 100
# stocks". 100 x Rs 1L = Rs 1 crore is therefore the maximum the book can ever deploy,
# which makes MAX_POSITIONS a CAPITAL constraint rather than a diversification heuristic.
# That distinction decides how it is enforced: a full book stops entering and says which
# names it turned away (see portfolio_runner), it does not quietly truncate the candidate
# list -- a silent truncation would make entry depend on iteration order.
CAPITAL_PER_POSITION_RS = env.float("PORTFOLIO_CAPITAL_PER_POSITION_RS", 100000.0)
MAX_POSITIONS = env.int("PORTFOLIO_MAX_POSITIONS", 100)

# Fraction of a company's own average daily traded value a single position may
# occupy -- a plain constant, not backtested (this whole module is a calculator
# for a layer the source spec always intended to exist; PRD §12 todo #7's
# calibration loop is what will eventually tell us if a DIFFERENT cap does
# better, not a re-guess here).
DEFAULT_MAX_PCT_OF_ADV = 0.10


def compute_position_size(
    *, target_capital_rs: float, adv_value_rs: float | None, max_pct_of_adv: float = DEFAULT_MAX_PCT_OF_ADV
) -> dict[str, object]:
    """Pure arithmetic -- no DB access, no company identity, so every rounding/
    tie-breaking decision is independently testable without mocking anything.
    adv_value_rs=None (no liquidity data at all) means the ADV cap can't be
    computed -- falls back to target_capital_rs alone rather than silently
    treating unknown liquidity as infinite (which a missing cap would otherwise
    imply) or zero (which would wrongly zero out the position)."""
    if adv_value_rs is None:
        return {
            "target_capital_rs": round(target_capital_rs, 2),
            "adv_cap_rs": None,
            "recommended_size_rs": round(target_capital_rs, 2),
            "binding_constraint": "flat_allocation_no_adv_data",
        }
    adv_cap_rs = adv_value_rs * max_pct_of_adv
    if adv_cap_rs < target_capital_rs:
        return {
            "target_capital_rs": round(target_capital_rs, 2),
            "adv_cap_rs": round(adv_cap_rs, 2),
            "recommended_size_rs": round(adv_cap_rs, 2),
            "binding_constraint": "adv_liquidity_cap",
        }
    return {
        "target_capital_rs": round(target_capital_rs, 2),
        "adv_cap_rs": round(adv_cap_rs, 2),
        "recommended_size_rs": round(target_capital_rs, 2),
        "binding_constraint": "flat_allocation",
    }


def load_open_thesis(company_master_id: str) -> dict | None:
    """The single OPEN position for this company, if any -- the gate this module
    refuses to size around the absence of.

    Only entry_decision='accept' counts. A VETOED name is still recorded as a position
    (that is what makes the adjudicator falsifiable) but sizing one would put real
    capital behind a name the adjudicator rejected -- the exact opposite of what the
    veto means."""
    df = sql_to_df(
        "SELECT position_id AS thesis_id, opened_at AS created_date "
        "  FROM fundamentals_portfolio_position "
        " WHERE company_master_id = %s AND status = 'open' AND entry_decision = 'accept' "
        " ORDER BY opened_at DESC LIMIT 1",
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
    company_master_id: str, *, capital_per_position_rs: float | None = None,
    max_pct_of_adv: float = DEFAULT_MAX_PCT_OF_ADV
) -> dict | None:
    """None if there's no OPEN, ACCEPTED position for this company.

    capital_per_position_rs defaults to the operator-set CAPITAL_PER_POSITION_RS. Unlike
    the previous total_capital/position_count pair this is NOT a guess about the
    operator's sleeve -- it is a figure they set directly, so a default here is a
    recorded decision rather than a silent assumption."""
    thesis = load_open_thesis(company_master_id)
    if thesis is None:
        return None
    target_capital_rs = (CAPITAL_PER_POSITION_RS if capital_per_position_rs is None
                         else float(capital_per_position_rs))
    if target_capital_rs <= 0:
        raise ValueError("capital_per_position_rs must be positive")

    adv_inputs = load_adv_inputs(company_master_id)
    sizing = compute_position_size(
        target_capital_rs=target_capital_rs,
        adv_value_rs=adv_inputs["adv_value_rs"] if adv_inputs else None,
        max_pct_of_adv=max_pct_of_adv,
    )
    return {
        "company_master_id": company_master_id,
        "position_id": thesis["thesis_id"],
        "capital_per_position_rs": target_capital_rs,
        "max_positions": MAX_POSITIONS,
        "max_pct_of_adv": max_pct_of_adv,
        "avg_vol_1mth": adv_inputs["avg_vol_1mth"] if adv_inputs else None,
        "cmp_rs": adv_inputs["cmp_rs"] if adv_inputs else None,
        **sizing,
    }
