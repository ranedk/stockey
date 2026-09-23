"""Confluence scoring -- fundamentals screener PRD §12 todo #4 (2026-08-29).

Answers "more signals, better decision, better confidence" (the operator's own
framing, 2026-08-29 conversation) WITHOUT an LLM synthesizing evidence into a
holistic buy call -- see PRD §12's own rationale: a well-argued LLM thesis is
*harder* to audit than a single-signal reaction, not easier, because it sounds more
trustworthy without being any more falsifiable. This module computes a MECHANICAL,
VERSIONED count of independent evidence axes currently agreeing on a watchlist
company -- every axis is a fixed, documented rule over data this pipeline already
collects, never an LLM judgment call. The count itself is what's falsifiable: PRD
§12 todo #7 will check, once enough L4 theses resolve, whether a high count actually
correlates with a higher forecast hit rate. Nothing here decides anything -- this is
a richer READ for the human who still writes the L4 thesis by hand.

Five axes, each independently True (supportive) / False (contradicting) / None
(insufficient data to call either way -- never silently coerced to a side):

1. fundamentals_trajectory -- debt or CWIP-ratio improving (fundamentals_l2_state),
   with neither one worsening. Read in l2_state.compute_trend_direction's own
   numeric-series vocabulary (see WORSENING/IMPROVING_TREND_DIRECTIONS below).
2. event_corroboration -- does this company's recent alert history (fundamentals_
   l3_alerts) lean toward the corroborating trigger_types or the contradicting
   ones (same classification watchlist_exit.py's own INVALIDATING_TRIGGER_TYPES
   uses, extended here to the deal-flow/pledge triggers those didn't cover yet).
3. sector_cycle -- capacity discipline (not expansion) in this company's sector
   (fundamentals_sector_cycle.phase).
4. ownership -- institutional accumulation, promoter stake not decreasing, pledge
   not rising, no recent insider/bulk-deal selling (fundamentals_l2_state +
   fundamentals_l3_alerts).
5. valuation -- cheap relative to the company's OWN history, not just cheap in
   absolute terms (fundamentals_l2_state.valuation_vs_own_history_ratio).

confluence_count is the number of True axes (out of however many were evaluable --
a company with 3 knowable axes and 3 green is NOT penalized for 2 unknowable ones).
contradicting_count is the number of False axes, tracked separately so a human can
tell "quietly unconfirmed" apart from "actively contradicted" at a glance.
"""
from __future__ import annotations

import json

import pandas as pd

from fundamentals.screens.l2_state import ensure_l2_pledge_trend_columns
from utils.company_master import build_l1_ticker_by_company_master_id
from utils.db import db_session, execute_db_operation, sql_to_df, upsert_to_db
from utils.ops_alert import send_ops_alert

RESULTS_TABLE = "fundamentals_confluence_score"
SYNC_SOURCE_NAME = "fundamentals.screens.confluence_score"
# v2 (2026-09-23): fundamentals_trajectory was DEAD in every v1 row. v1 tested
# net_debt/cwip_ratio_trend_direction for "increasing"/"decreasing", but those two
# fields come from l2_state.compute_trend_direction, which only ever emits
# accelerating/decelerating/steady_{increase,decline}, flat or reversal -- so the
# axis returned None for 0 of 137 names on every run back to at least 2026-09-15,
# and every v1 score was a count out of four axes, not five. The ownership fields
# (promoter/institutional/pledge) DO use "increasing"/"decreasing" and were fine.
# Readers pick the newest version on a run_date tie, so v1 rows stay as history.
SCORE_VERSION = 2
STOCKEY_RUN_STATE: dict[str, object] = {}

# BUG FOUND LIVE 2026-08-29 (same class as fundamentals/api/queries.py's
# _load_draft_theses_by_company's own 2026-08-18 fix): fundamentals/api/
# queries.py's get_watchlist() and fundamentals/screens/notifications.py's
# load_full_watchlist() both LEFT JOIN LATERAL this table -- on a fresh DB (this
# module has never completed a real pipeline run yet), that join 500'd every
# single /api/watchlist request with psycopg2.errors.UndefinedTable, since
# upsert_to_db's own CREATE TABLE IF NOT EXISTS only ever runs on a WRITE, never
# a read. Both callers now call _ensure_confluence_score_table() defensively
# first, same as they already do for fundamentals_l4_thesis_draft.
_TABLE_STATEMENT = """
    CREATE TABLE IF NOT EXISTS fundamentals_confluence_score (
        company_master_id TEXT NOT NULL,
        run_date TIMESTAMPTZ NOT NULL,
        score_version INTEGER NOT NULL,
        axis_fundamentals_trajectory BOOLEAN,
        axis_event_corroboration BOOLEAN,
        axis_sector_cycle BOOLEAN,
        axis_ownership BOOLEAN,
        axis_valuation BOOLEAN,
        confluence_count INTEGER NOT NULL,
        contradicting_count INTEGER NOT NULL,
        evaluable_count INTEGER NOT NULL,
        PRIMARY KEY (company_master_id, run_date, score_version)
    )
"""


def _ensure_confluence_score_table() -> None:
    def _op() -> None:
        with db_session() as (_, cur):
            cur.execute(_TABLE_STATEMENT)

    execute_db_operation(_op, operation_name="fundamentals_confluence_score:ensure_table")

# How far back an alert still counts toward event_corroboration/ownership's
# "no recent selling" check -- long enough to matter to a 12-30 month thesis
# horizon, short enough that a stale alert from last year doesn't keep
# contradicting a name forever. A plain constant, not tuned to any backtest
# (PRD §12 todo #7 is what will eventually tell us if this number matters).
ALERT_LOOKBACK_DAYS = 90

# Same corroborating/contradicting split watchlist_exit.py's own
# INVALIDATING_TRIGGER_TYPES encodes for a DIFFERENT purpose (whether to exit
# the watchlist) -- reused here for a different question (does recent alert
# history support or contradict the name), extended to the deal-flow/pledge
# triggers that module doesn't classify. results_delayed/llm_flagged/
# capital_raise/institutional_first_entry/auditor_change/related_party_
# transaction are deliberately NOT in either set -- see watchlist_exit.py's own
# reasoning for capital_raise/institutional_first_entry (structural, one-off
# facts with no natural "confirms" or "contradicts" reading); auditor_change/
# related_party_transaction are red flags at L1 admission time, not directional
# evidence once a company is already on the watchlist.
CORROBORATING_TRIGGER_TYPES = frozenset({"rating_confirms_deleveraging", "results_confirm_turnaround", "insider_buy", "bulk_deal_buy"})
CONTRADICTING_TRIGGER_TYPES = frozenset({"rating_downgrade", "results_decline", "insider_sell_surprise", "pledge_increase", "bulk_deal_sell"})

# Valuation-vs-own-history bands (dimensionless ratio: current PE / 5yr historical
# PE) -- a tolerance band around 1.0, same "avoid a razor's-edge cutoff" reasoning
# internal_stage's own FlatThresholdPct uses for its slope band.
VALUATION_CHEAP_THRESHOLD = 0.9
VALUATION_EXPENSIVE_THRESHOLD = 1.1


# l2_state.compute_trend_direction's vocabulary. A decelerating increase is still an
# increase -- debt that is growing more slowly is growing. "flat" and "reversal" (the
# sign flipped between the two latest deltas) are in neither set: no settled trend.
WORSENING_TREND_DIRECTIONS = frozenset({"accelerating_increase", "decelerating_increase", "steady_increase"})
IMPROVING_TREND_DIRECTIONS = frozenset({"accelerating_decline", "decelerating_decline", "steady_decline"})


def compute_fundamentals_trajectory_axis(l2_row: dict) -> bool | None:
    debt_dir = l2_row.get("net_debt_trend_direction")
    cwip_dir = l2_row.get("cwip_ratio_trend_direction")
    if debt_dir is None and cwip_dir is None:
        return None
    if debt_dir in WORSENING_TREND_DIRECTIONS or cwip_dir in WORSENING_TREND_DIRECTIONS:
        return False
    if debt_dir in IMPROVING_TREND_DIRECTIONS or cwip_dir in IMPROVING_TREND_DIRECTIONS:
        return True
    return None  # both flat/reversal/unknown -- no clear trend either way


def compute_ownership_axis(l2_row: dict, recent_trigger_types: set[str]) -> bool | None:
    institutional_dir = l2_row.get("institutional_stake_direction")
    promoter_dir = l2_row.get("promoter_stake_direction")
    pledge_dir = l2_row.get("pledge_pct_trend_direction")
    if promoter_dir == "decreasing":
        return False
    if pledge_dir == "increasing":
        return False
    if "insider_sell_surprise" in recent_trigger_types or "bulk_deal_sell" in recent_trigger_types:
        return False
    if institutional_dir == "increasing":
        return True
    if institutional_dir is None and promoter_dir is None:
        return None  # no shareholding data at all -- can't assess ownership
    return None  # no red flag, but no accumulation confirmed either -- neutral, not green


def compute_event_corroboration_axis(recent_trigger_types: set[str]) -> bool | None:
    has_negative = bool(recent_trigger_types & CONTRADICTING_TRIGGER_TYPES)
    has_positive = bool(recent_trigger_types & CORROBORATING_TRIGGER_TYPES)
    if has_negative:
        return False  # any contradiction present overrides -- same "worse wins" precedent as watchlist_exit's invalidation check
    if has_positive:
        return True
    return None  # no classified alert in the lookback window either way


def compute_sector_cycle_axis(phase: str | None) -> bool | None:
    if phase == "capacity_discipline":
        return True
    if phase == "capacity_expansion":
        return False
    return None  # "balanced", or no sector data at all -- genuinely neutral, not a claimed direction


def compute_valuation_axis(valuation_vs_own_history_ratio: float | None) -> bool | None:
    if valuation_vs_own_history_ratio is None:
        return None
    if valuation_vs_own_history_ratio <= VALUATION_CHEAP_THRESHOLD:
        return True
    if valuation_vs_own_history_ratio >= VALUATION_EXPENSIVE_THRESHOLD:
        return False
    return None  # roughly in line with own history -- neither cheap nor expensive enough to call


def compute_confluence_row(l2_row: dict, phase: str | None, recent_trigger_types: set[str]) -> dict[str, object]:
    axes = {
        "axis_fundamentals_trajectory": compute_fundamentals_trajectory_axis(l2_row),
        "axis_event_corroboration": compute_event_corroboration_axis(recent_trigger_types),
        "axis_sector_cycle": compute_sector_cycle_axis(phase),
        "axis_ownership": compute_ownership_axis(l2_row, recent_trigger_types),
        "axis_valuation": compute_valuation_axis(l2_row.get("valuation_vs_own_history_ratio")),
    }
    return {
        **axes,
        "confluence_count": sum(1 for v in axes.values() if v is True),
        "contradicting_count": sum(1 for v in axes.values() if v is False),
        "evaluable_count": sum(1 for v in axes.values() if v is not None),
    }


AXIS_COLUMNS = (
    "axis_fundamentals_trajectory",
    "axis_event_corroboration",
    "axis_sector_cycle",
    "axis_ownership",
    "axis_valuation",
)

# TODO.md C3 (2026-09-23): v1's trajectory axis scored 0 names for at least a week in
# a live decision path and nothing noticed. Every run now records what share of names
# each axis actually scored, and alarms on two DISTINCT classes -- kept separate so a
# noisy alarm is fixed by reclassifying it, never by loosening a threshold until it
# goes quiet:
#   dead    -- the axis scored 0 of N names. Never legitimate for a whole watchlist.
#   dropped -- the share fell below half of the previous same-version run's share.
#              Only checked when that previous share was >= AXIS_DROP_MIN_PRIOR_SHARE,
#              so an axis that was always thin cannot "drop" on noise.
AXIS_DROP_MIN_PRIOR_SHARE = 0.10
AXIS_DROP_RATIO = 0.5


def compute_axis_coverage(rows: list[dict[str, object]]) -> dict[str, float]:
    if not rows:
        return {}
    return {axis: sum(1 for r in rows if r.get(axis) is not None) / len(rows) for axis in AXIS_COLUMNS}


def load_previous_axis_coverage(run_date: pd.Timestamp) -> dict[str, float]:
    """Axis coverage of the latest earlier run at THIS score_version -- a version bump
    changes what an axis can score, so comparing across versions would alarm on the
    fix itself."""
    counts = ", ".join(f"count({axis}) AS {axis}" for axis in AXIS_COLUMNS)
    df = sql_to_df(
        f"""
        SELECT run_date, count(*) AS n, {counts}
          FROM {RESULTS_TABLE}
         WHERE score_version = %s AND run_date < %s
         GROUP BY run_date
         ORDER BY run_date DESC
         LIMIT 1
        """,  # noqa: S608 -- column and table names are module constants
        params=(SCORE_VERSION, run_date),
    )
    if df.empty or not df.iloc[0]["n"]:
        return {}
    row = df.iloc[0]
    return {axis: float(row[axis]) / float(row["n"]) for axis in AXIS_COLUMNS}


def classify_axis_coverage(current: dict[str, float], previous: dict[str, float]) -> list[dict[str, object]]:
    problems: list[dict[str, object]] = []
    for axis, share in current.items():
        if share == 0:
            problems.append({"axis": axis, "class": "dead", "share": share, "previous_share": previous.get(axis)})
            continue
        prior = previous.get(axis)
        if prior is not None and prior >= AXIS_DROP_MIN_PRIOR_SHARE and share < prior * AXIS_DROP_RATIO:
            problems.append({"axis": axis, "class": "dropped", "share": share, "previous_share": prior})
    return problems


def _alert_axis_coverage(problems: list[dict[str, object]], coverage: dict[str, float], names: int) -> None:
    lines = [f"{p['axis']}: {p['class']} -- scored {p['share']:.0%} of {names} names"
             + (f" (previous run {p['previous_share']:.0%})" if p["previous_share"] is not None else "")
             for p in problems]
    body = (
        "fundamentals_confluence_score: an axis stopped scoring.\n\n"
        + "\n".join(lines)
        + "\n\nAll axes this run:\n"
        + "\n".join(f"  {axis}: {share:.0%}" for axis, share in coverage.items())
        + f"\n\nscore_version={SCORE_VERSION}. The confluence count feeds the portfolio ruleset,"
        " so a dead axis silently changes entries and exits. See confluence_score.py's C3 note."
    )
    send_ops_alert(f"confluence axis coverage: {', '.join(sorted({p['class'] for p in problems}))}", body)


def load_active_watchlist_company_ids() -> list[str]:
    df = sql_to_df("SELECT company_master_id FROM fundamentals_watchlist WHERE status = 'active'")
    return [] if df.empty else df["company_master_id"].dropna().tolist()


def load_l2_state_by_ticker(tickers: list[str]) -> dict[str, dict]:
    """Latest fundamentals_l2_state row per ticker, only the fields the axes above
    need -- deliberately its own query rather than reusing l3_triggers.py's
    load_latest_l2_state() (a narrower column set built for that module's own
    corroboration checks), to avoid widening a function other tests/callers
    already depend on for an unrelated need.

    BUG FOUND LIVE 2026-08-29: pledge_pct_trend_direction is a new column
    (l2_state.py's compute_pledge_trend) that only exists once run_l2_state_
    refresh has written it at least once since that feature shipped --
    UndefinedColumn otherwise on a DB refreshed under the older schema. See
    l2_state.py's own ensure_l2_pledge_trend_columns docstring for the full
    story; called defensively here for the same reason get_watchlist() calls
    _ensure_confluence_score_table() before its own query."""
    if not tickers:
        return {}
    ensure_l2_pledge_trend_columns()
    df = sql_to_df(
        """
        SELECT DISTINCT ON (ticker) ticker, net_debt_trend_direction, cwip_ratio_trend_direction,
               institutional_stake_direction, promoter_stake_direction, pledge_pct_trend_direction,
               valuation_vs_own_history_ratio
        FROM fundamentals_l2_state
        WHERE ticker = ANY(%s)
        ORDER BY ticker, run_date DESC
        """,  # noqa: S608 -- fixed internal query, company list is parameterized
        params=(tickers,),
    )
    if df.empty:
        return {}
    return {row["ticker"]: row for row in df.to_dict("records")}


def load_sector_phase_by_company_id(company_master_ids: list[str]) -> dict[str, str]:
    if not company_master_ids:
        return {}
    sector_df = sql_to_df(
        """
        SELECT DISTINCT ON (company_master_id) company_master_id, sector_code
        FROM dim_security
        WHERE company_master_id = ANY(%s) AND sector_code IS NOT NULL
        ORDER BY company_master_id, last_trade_date DESC NULLS LAST, effective_to DESC NULLS LAST
        """,  # noqa: S608 -- fixed internal query, company list is parameterized
        params=(company_master_ids,),
    )
    if sector_df.empty:
        return {}
    phase_df = sql_to_df(
        """
        SELECT sector_code, phase FROM fundamentals_sector_cycle
        WHERE run_date = (SELECT MAX(run_date) FROM fundamentals_sector_cycle)
        """
    )
    if phase_df.empty:
        return {}
    phase_by_sector = dict(zip(phase_df["sector_code"], phase_df["phase"]))
    return {
        row["company_master_id"]: phase_by_sector[row["sector_code"]]
        for row in sector_df.to_dict("records")
        if row["sector_code"] in phase_by_sector
    }


def load_recent_trigger_types_by_company_id(company_master_ids: list[str]) -> dict[str, set[str]]:
    if not company_master_ids:
        return {}
    df = sql_to_df(
        f"""
        SELECT company_master_id, trigger_type FROM fundamentals_l3_alerts
        WHERE company_master_id = ANY(%s) AND alert_date >= CURRENT_DATE - INTERVAL '{int(ALERT_LOOKBACK_DAYS)} days'
        """,  # noqa: S608 -- ALERT_LOOKBACK_DAYS is a fixed internal constant, not user input; company list is parameterized
        params=(company_master_ids,),
    )
    if df.empty:
        return {}
    out: dict[str, set[str]] = {}
    for row in df.to_dict("records"):
        out.setdefault(row["company_master_id"], set()).add(row["trigger_type"])
    return out


def run_confluence_score_refresh() -> dict[str, object]:
    _ensure_confluence_score_table()
    company_ids = load_active_watchlist_company_ids()
    if not company_ids:
        return {"rows": 0, "companies_no_l2_state": 0}

    ticker_by_cmid = build_l1_ticker_by_company_master_id()
    tickers = [ticker_by_cmid[cmid] for cmid in company_ids if cmid in ticker_by_cmid]
    l2_by_ticker = load_l2_state_by_ticker(tickers)
    phase_by_cmid = load_sector_phase_by_company_id(company_ids)
    trigger_types_by_cmid = load_recent_trigger_types_by_company_id(company_ids)

    run_date = pd.Timestamp.now(tz="UTC").normalize()
    rows: list[dict[str, object]] = []
    no_l2_state = 0
    for company_master_id in company_ids:
        ticker = ticker_by_cmid.get(company_master_id)
        l2_row = l2_by_ticker.get(ticker) if ticker is not None else None
        if l2_row is None:
            no_l2_state += 1
            l2_row = {}  # every axis touching L2 fields degrades to None, not a crash -- same "no silent skip" as l3_triggers' own no_l2_state handling
        row = compute_confluence_row(
            l2_row,
            phase_by_cmid.get(company_master_id),
            trigger_types_by_cmid.get(company_master_id, set()),
        )
        row["company_master_id"] = company_master_id
        row["run_date"] = run_date
        row["score_version"] = SCORE_VERSION
        rows.append(row)

    coverage = compute_axis_coverage(rows)
    problems = classify_axis_coverage(coverage, load_previous_axis_coverage(run_date)) if rows else []
    if rows:
        upsert_to_db(pd.DataFrame(rows), RESULTS_TABLE, unique_keys=["company_master_id", "run_date", "score_version"])
    if problems:
        _alert_axis_coverage(problems, coverage, len(rows))
    return {"rows": len(rows), "companies_no_l2_state": no_l2_state, "axis_coverage": coverage, "axis_coverage_problems": problems}


def main() -> int:
    global STOCKEY_RUN_STATE
    result = run_confluence_score_refresh()
    STOCKEY_RUN_STATE = {
        "source": SYNC_SOURCE_NAME,
        "rows": result["rows"],
        "rows_written": result["rows"],
        "companies_no_l2_state": result["companies_no_l2_state"],
        "axis_coverage": result.get("axis_coverage", {}),
        "axis_coverage_problems": result.get("axis_coverage_problems", []),
        "fallback_used": result["companies_no_l2_state"] > 0,
        "state_advanced": result["rows"] > 0,
        "status": "ok",
    }
    print(json.dumps(STOCKEY_RUN_STATE, ensure_ascii=False, default=str), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
