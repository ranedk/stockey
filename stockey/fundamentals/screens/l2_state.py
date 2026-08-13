"""L2 watch-state store -- fundamental screener step 4 (docs/FUNDAMENTAL_SCREENER_PRD.md
sec 8 step 4, fundamental_basic_goal.md sec 1's L2 list).

Per-company state vector for names currently in fundamentals_l1_universe -- L2's own
purpose statement is "if an event hits this name, does it matter", which only makes
sense scoped to names L1 already decided to watch, not the full listed universe.

Two data sources, both screener.in, reusing fundamentals.collectors.screenerin's
plumbing:

1. One standalone cross-sectional query for `Pledged percentage` (pledge_pct).
   Confirmed live 2026-08-10 that screener.in's raw-query "extra display column"
   mechanism (see fundamentals/screens/l1_universe.py's docstring) caps out at 3 extra
   columns and stops revealing anything at all once the query also contains
   compound/multi-field expressions (L1_QUERY has several) -- appending
   "Pledged percentage >= 0" onto L1_QUERY does not surface it as a column, even
   though the company set is unaffected. A standalone single-field query
   (`Pledged percentage > 0`, ~592 companies market-wide, confirmed live) sidesteps
   this entirely and is cheap since most companies aren't in it at all -- unpledged
   (0.0) is the default for anyone not returned.
2. Per-company detail pages (`screener.in/company/<ticker>/`) for everything that
   needs a multi-period series, which a cross-sectional query's single snapshot can
   never give: the Balance Sheet table (annual Borrowings/CWIP/Fixed Assets), the
   Profit & Loss table (annual Operating Profit/Interest), and the quarterly
   Shareholding Pattern table (promoter % across ~12 quarters). Confirmed live
   2026-08-10 by inspecting a real company page's DOM -- section ids
   #balance-sheet/#profit-loss/#shareholding, each a plain server-rendered <table>,
   no AJAX needed for any of the numbers used here.

One of the source PRD's six L2 fields is explicitly out of scope for this module,
same "deferred, not silent" treatment L1 already uses for auditor/RPT:

- `sector_cycle_phase` -- inherently cross-company (source PRD: "aggregating
  gross-block growth across all listed players in the sector"), belongs to build-order
  step 9 (sector capital-cycle aggregation, fundamentals/screens/sector_cycle.py), not
  a per-company state field.

Stored as a NULL column plus a standing fallback_telemetry event every run (see
_record_deferred_fields_fallback), matching this repo's no-silent-fallback rule.

`valuation_percentile` (2026-08-13, built): originally deferred alongside
sector_cycle_phase for the same reason -- source PRD wants EV/EBITDA vs both own
history and sector. Un-deferred once two things were confirmed live: (1) screener.in
has NO historical EV/EBITDA field at all ("EVEBITDA 3years back" -> "Unknown word: ..."
error, tested directly against the raw query endpoint), so EV/EBITDA itself is a dead
end here; (2) it DOES have `Historical PE <N>Years` (a first-class registered field --
median PE over the company's own trailing history) via the same standalone-
cross-sectional-query mechanism pledge_pct already uses (VALUATION_QUERY,
fetch_valuation_levels). PE substitutes for EV/EBITDA as the valuation multiple.
Deliberately split into two separate fields rather than one blended number, same
"don't collapse distinct signals into one field" choice sector_cycle.py's own
phase/growth_classification split made:

- `valuation_vs_own_history_ratio` -- current PE / Historical PE 5Years. <1 means
  cheaper than the company's own history, >1 means pricier. None when PE is
  negative/undefined (loss-making) or no 5yr median exists -- never a guessed ratio.
- `valuation_sector_percentile` -- 0-100 rank of current PE among this run's other
  L1 companies sharing the same sector_code (dim_security, same 'nse:'+ticker
  identity bridge this module's synthetic events already use). Computed as a
  post-pass in run_l2_state_refresh after every company's row is built (it's the one
  L2 field that genuinely needs sibling companies' data, not just this company's
  own), via compute_valuation_sector_percentiles/load_sector_codes_for_tickers.

Approximations, documented like L1's:

- `debt_trajectory` / `cwip_ratio`: source PRD wants a 4-quarter (debt) or QoQ (CWIP)
  cadence; screener.in's balance sheet table is annual only (Indian companies don't
  routinely file quarterly balance sheets) -- both use a YoY (latest annual vs.
  preceding annual) proxy instead, consistent with L1's own annual-proxy calls.
- `cwip_ratio` uses latest-year Fixed Assets (net block) as the denominator, not true
  gross block -- the balance sheet table's default "Fixed Assets +" row is the
  collapsed net figure; the gross breakdown sits behind a separate AJAX call this
  module does not make (one more request per company for a secondary ratio input --
  revisit if the net-block proxy proves too noisy in review).
- `promoter_stake` direction *is* the real thing, not an approximation -- the
  shareholding table already carries quarterly promoter % directly, matching the
  source PRD's stated 4-quarter cadence exactly.

Live 2026-08-10: validated against the current 188-company L1 universe (docs/
FUNDAMENTAL_SCREENER_PRD.md sec 8 step 3).

Institutional first-entry (2026-08-13, the original fundamental_basic_goal.md L3
trigger #3 -- "quarterly shareholding pattern deltas, first institutional entry" --
left open when capital_raise (announcement-based, a different signal) was built
instead on 2026-08-12). Same shareholding table already fetched for promoter_stake,
confirmed live 2026-08-13 against a real company (HALDYNGL): screener.in carries
`FIIs` and `DIIs` as separate rows, summed here to match L1_QUERY's own "FII holding +
DII holding" convention. compute_institutional_stake() mirrors compute_promoter_stake
exactly, plus one more field: institutional_first_entry, true only when every prior
quarter in the visible window was explicitly 0 (not merely missing/None -- a gap in
the data is not proof of a zero, so it does NOT count as "prior zero") and the latest
quarter is the first nonzero. Caveat, documented not hidden: screener.in's
shareholding table only shows a trailing ~12-quarter (3-year) window, so this is
"first entry visible in ~3 years of history", not a claim about the company's entire
listed lifetime.

There is no exchange filing for this -- the "event" is L2's own quarterly refresh
observing the transition. To flow through the same fundamentals_l3_alerts pipeline as
every other trigger (fundamentals/screens/l3_triggers.py's institutional_entry
evaluator) rather than inventing a second, parallel alert mechanism, a detected
first-entry writes a SYNTHETIC row into fundamentals_events (source="l2_state",
filing_type="institutional_entry") -- see _build_institutional_entry_event_row().
news_id is keyed on the shareholding table's own latest PERIOD LABEL (e.g. "Jun
2026"), deliberately NOT run_date: L2 can refresh daily while the underlying
quarterly data is unchanged, and keying on run_date would re-fire the "first entry"
alert every single day until the next quarter actually posts. Keying on the period
label makes this idempotent across same-quarter re-runs and fire exactly once, for
the quarter the transition actually happened.
"""

from __future__ import annotations

import json
import re

import pandas as pd
from bs4 import BeautifulSoup

from fundamentals.collectors.events_store import RESULTS_TABLE as EVENTS_TABLE
from fundamentals.collectors.events_store import _ensure_events_schema
from fundamentals.collectors.screenerin import build_authenticated_session, clean_text, run_query
from fundamentals.collectors.screenerin import to_number as _screenerin_to_number
from fundamentals.screens.l1_universe import load_l1_universe_tickers
from utils.db import db_session, execute_db_operation, sql_to_df, upsert_to_db
from utils.exchange_rate_limiter import exchange_request_gate
from utils.fallback_telemetry import record_local_fallback_event

SYNC_SOURCE_NAME = "fundamentals.screens.l2_state"
RESULTS_TABLE = "fundamentals_l2_state"
# Bumped 1 -> 2 (2026-08-13): valuation_vs_own_history_ratio/valuation_sector_
# percentile are new fields on the state vector (see module docstring) -- per the
# source PRD's own versioning rule ("a backtest must reproduce what a rule was on a
# past date"), old rows (version 1, these fields absent) must stay cleanly
# distinguishable from new ones, not silently mixed under the same version number.
STATE_VECTOR_VERSION = 2
STOCKEY_RUN_STATE: dict[str, object] = {}

COMPANY_URL_TEMPLATE = "https://www.screener.in/company/{ticker}/"
PLEDGE_QUERY = "Pledged percentage > 0"
# valuation_percentile build (2026-08-13): confirmed live that screener.in has no
# historical EV/EBITDA field at all ("EVEBITDA 3years back" -> "Unknown word"), but
# DOES have "Historical PE <N>Years" (median PE over the company's own trailing
# history) as a first-class registered field -- PE substitutes for EV/EBITDA as the
# valuation multiple here. "!= 0" (not "> 0") on Price to Earning: a genuinely
# negative PE (loss-making company) is real, useful information (own-history ratio
# is meaningless for it, but it's still excluded from -- not silently zeroed into --
# the sector percentile), not something to filter out at the query level.
VALUATION_QUERY = "Price to Earning != 0 AND Historical PE 5Years > 0"

# Not silently skipped -- see module docstring. Every run logs a fallback event naming
# these, and the run summary/STOCKEY_RUN_STATE carries checks_deferred.
# valuation_percentile removed 2026-08-13 -- built, see module docstring.
DEFERRED_FIELDS = ("sector_cycle_phase",)


def _record_deferred_fields_fallback() -> None:
    record_local_fallback_event(
        module=SYNC_SOURCE_NAME,
        source="screenerin",
        fallback_type="l2_fields_not_sourced",
        severity="warn",
        reason=(
            "L2 state refresh ran without sector_cycle_phase and valuation_percentile -- "
            "both are inherently cross-company/sector fields that belong to build-order "
            "step 9 (sector aggregation), not a per-company state computation."
        ),
        error="deferred to step 9",
        metadata={"deferred_fields": list(DEFERRED_FIELDS)},
    )


def _record_company_fetch_fallback(ticker: str, error: Exception) -> None:
    record_local_fallback_event(
        module=SYNC_SOURCE_NAME,
        source="screenerin",
        fallback_type="l2_company_detail_fetch_failed",
        severity="warn",
        reason="L2 state refresh could not fetch/parse a company's detail page; it is missing from this run.",
        error=error,
        metadata={"ticker": ticker},
    )


def _record_no_universe_fallback() -> None:
    record_local_fallback_event(
        module=SYNC_SOURCE_NAME,
        source="fundamentals_l1_universe",
        fallback_type="l2_no_l1_universe",
        severity="warn",
        reason="L2 state refresh found no fundamentals_l1_universe rows to build state for.",
        error="empty L1 universe",
    )


def _num(value) -> object:
    cleaned = clean_text(value)
    if cleaned is None:
        return None
    return _screenerin_to_number(cleaned.replace("%", ""))


def _parse_period_table(table) -> dict[str, object]:
    """Parse one of screener.in's period-column tables (Balance Sheet, Profit & Loss,
    Shareholding Pattern -- all the same shape: a header row of period labels, then one
    row per line item, first cell the label, e.g. `Borrowings +`, an expandable-row
    marker this module ignores rather than following)."""
    if table is None:
        return {"periods": [], "rows": {}}
    trs = table.select("tr")
    if not trs:
        return {"periods": [], "rows": {}}
    header_cells = trs[0].find_all(["th", "td"])
    periods = [clean_text(cell.get_text(" ", strip=True)) for cell in header_cells[1:]]

    rows: dict[str, list[object]] = {}
    for tr in trs[1:]:
        cells = tr.find_all(["th", "td"])
        if not cells:
            continue
        label = clean_text(cells[0].get_text(" ", strip=True)) or ""
        label = re.sub(r"\s*\+\s*$", "", label).strip()
        if not label:
            continue
        rows[label] = [_num(cell.get_text(" ", strip=True)) for cell in cells[1:]]
    return {"periods": periods, "rows": rows}


def _value_at(period_table: dict[str, object], label: str, *, offset: int = 0):
    """offset=0 is the most recent period, offset=1 the one before it, etc."""
    values = period_table.get("rows", {}).get(label)
    if not values:
        return None
    index = len(values) - 1 - offset
    if index < 0:
        return None
    return values[index]


def compute_debt_trajectory(balance_sheet: dict[str, object], profit_loss: dict[str, object]) -> dict[str, object]:
    net_debt_latest = _value_at(balance_sheet, "Borrowings")
    net_debt_preceding = _value_at(balance_sheet, "Borrowings", offset=1)
    net_debt_yoy_delta = (
        net_debt_latest - net_debt_preceding
        if isinstance(net_debt_latest, (int, float)) and isinstance(net_debt_preceding, (int, float))
        else None
    )

    operating_profit_latest = _value_at(profit_loss, "Operating Profit")
    interest_latest = _value_at(profit_loss, "Interest")
    interest_coverage = (
        operating_profit_latest / interest_latest
        if isinstance(operating_profit_latest, (int, float))
        and isinstance(interest_latest, (int, float))
        and interest_latest != 0
        else None
    )
    debt_to_ebitda = (
        net_debt_latest / operating_profit_latest
        if isinstance(net_debt_latest, (int, float))
        and isinstance(operating_profit_latest, (int, float))
        and operating_profit_latest != 0
        else None
    )
    return {
        "net_debt_rscr": net_debt_latest,
        "net_debt_yoy_delta_rscr": net_debt_yoy_delta,
        "interest_coverage": interest_coverage,
        "debt_to_ebitda": debt_to_ebitda,
    }


def compute_cwip_ratio(balance_sheet: dict[str, object]) -> dict[str, object]:
    def ratio_at(offset: int):
        cwip = _value_at(balance_sheet, "CWIP", offset=offset)
        fixed_assets = _value_at(balance_sheet, "Fixed Assets", offset=offset)
        if not isinstance(cwip, (int, float)) or not isinstance(fixed_assets, (int, float)) or fixed_assets == 0:
            return None
        return cwip / fixed_assets

    latest_ratio = ratio_at(0)
    preceding_ratio = ratio_at(1)
    delta = latest_ratio - preceding_ratio if latest_ratio is not None and preceding_ratio is not None else None
    return {"cwip_ratio": latest_ratio, "cwip_ratio_yoy_delta": delta}


def compute_promoter_stake(shareholding: dict[str, object]) -> dict[str, object]:
    values = [v for v in shareholding.get("rows", {}).get("Promoters") or [] if isinstance(v, (int, float))]
    latest = values[-1] if values else None
    direction = None
    if len(values) >= 4:
        window = values[-4:]
        delta = window[-1] - window[0]
        if delta > 0.01:
            direction = "increasing"
        elif delta < -0.01:
            direction = "decreasing"
        else:
            direction = "flat"
    return {"promoter_pct": latest, "promoter_stake_direction": direction}


def compute_institutional_stake(shareholding: dict[str, object]) -> dict[str, object]:
    """FII + DII combined, matching L1_QUERY's own "FII holding + DII holding"
    convention -- see module docstring for the first-entry definition and its
    trailing-~12-quarter-window caveat."""
    fii_raw = shareholding.get("rows", {}).get("FIIs") or []
    dii_raw = shareholding.get("rows", {}).get("DIIs") or []
    combined: list[float | None] = []
    for fii, dii in zip(fii_raw, dii_raw):
        if isinstance(fii, (int, float)) and isinstance(dii, (int, float)):
            combined.append(fii + dii)
        else:
            combined.append(None)

    numeric = [v for v in combined if isinstance(v, (int, float))]
    latest = numeric[-1] if numeric else None

    direction = None
    if len(numeric) >= 4:
        window = numeric[-4:]
        delta = window[-1] - window[0]
        if delta > 0.01:
            direction = "increasing"
        elif delta < -0.01:
            direction = "decreasing"
        else:
            direction = "flat"

    first_entry = False
    if len(combined) >= 2:
        prior = combined[:-1]
        # Every prior quarter must be an EXPLICIT 0, not merely missing/None -- a gap
        # in the data is not proof of a zero, so it must never produce a false
        # "first entry" claim.
        prior_all_zero = bool(prior) and all(isinstance(v, (int, float)) and v == 0 for v in prior)
        latest_val = combined[-1]
        if prior_all_zero and isinstance(latest_val, (int, float)) and latest_val > 0:
            first_entry = True

    return {"institutional_pct": latest, "institutional_stake_direction": direction, "institutional_first_entry": first_entry}


def fetch_pledge_levels(session) -> dict[int, float]:
    """Companies with any promoter pledge, market-wide -- anyone not in this dict is
    treated as 0.0 (unpledged), the overwhelmingly common case. See module docstring
    for why this is a standalone query rather than reusing L1_QUERY."""
    _, companies = run_query(session, PLEDGE_QUERY)
    levels: dict[int, float] = {}
    for company in companies:
        company_id = company.get("company_id")
        pledge_pct = company.get("metrics", {}).get("pledged_pct")
        if company_id is not None and isinstance(pledge_pct, (int, float)):
            levels[company_id] = pledge_pct
    return levels


def fetch_valuation_levels(session) -> dict[int, dict[str, float]]:
    """Market-wide standalone query (same shape/reasoning as fetch_pledge_levels
    above), keyed by company_id -> {pe, historical_pe_5y}. A company absent from this
    dict (PE undefined, negative, or no 5yr median available) gets no valuation
    fields at all downstream -- not a guessed/defaulted ratio, see module docstring."""
    _, companies = run_query(session, VALUATION_QUERY)
    levels: dict[int, dict[str, float]] = {}
    for company in companies:
        company_id = company.get("company_id")
        metrics = company.get("metrics", {})
        pe = metrics.get("p_e")
        historical_pe_5y = metrics.get("5yrs_pe")
        if (
            company_id is not None
            and isinstance(pe, (int, float))
            and pe > 0  # negative/loss-making PE has no meaningful ratio against a positive historical median
            and isinstance(historical_pe_5y, (int, float))
            and historical_pe_5y > 0
        ):
            levels[company_id] = {"pe": pe, "historical_pe_5y": historical_pe_5y}
    return levels


def load_sector_codes_for_tickers(tickers: list[str]) -> dict[str, str]:
    """ticker -> sector_code via dim_security, using the same 'nse:'+ticker identity
    bridge l2_state.py's own synthetic institutional_entry events already rely on
    (confirmed live 2026-08-11 against a real company, see module docstring
    elsewhere). Bulk query, not per-company -- same market-wide-then-dict-lookup
    pattern fetch_pledge_levels/fetch_valuation_levels already use."""
    if not tickers:
        return {}
    df = sql_to_df(
        "SELECT company_master_id, sector_code FROM dim_security WHERE company_master_id = ANY(%s) AND sector_code IS NOT NULL",
        params=([f"nse:{t}" for t in tickers],),
    )
    if df.empty:
        return {}
    return {row["company_master_id"].removeprefix("nse:"): row["sector_code"] for row in df.to_dict("records")}


def compute_valuation_sector_percentiles(rows: list[dict], sector_codes: dict[str, str]) -> dict[int, float]:
    """company_id -> percentile rank (0-100) of current PE within its own sector,
    among this run's L1 companies that have both a resolved PE and a resolved
    sector_code. Higher = more expensive relative to sector peers. A company missing
    either input gets no entry here (None downstream), not a guessed rank -- and a
    sector with only one PE-eligible company always ranks that company at 100th
    percentile (pandas rank(pct=True) on a single value), which is technically
    correct but not a meaningful comparison; treat single-company sectors here with
    the same skepticism sector_cycle.py's own sample_size_confidence caveat applies."""
    eligible = [
        {"company_id": row["company_id"], "sector_code": sector_codes.get(row["ticker"]), "pe": row.get("pe")}
        for row in rows
        if sector_codes.get(row["ticker"]) and isinstance(row.get("pe"), (int, float))
    ]
    if not eligible:
        return {}
    df = pd.DataFrame(eligible)
    df["percentile"] = df.groupby("sector_code")["pe"].rank(pct=True) * 100
    return {int(company_id): round(float(pct), 1) for company_id, pct in zip(df["company_id"], df["percentile"])}


def fetch_company_detail(session, ticker: str) -> dict[str, object]:
    url = COMPANY_URL_TEMPLATE.format(ticker=ticker)
    with exchange_request_gate(domain="screenerin"):
        response = session.get(url, timeout=60)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, "html.parser")
    return {
        "balance_sheet": _parse_period_table(soup.select_one("#balance-sheet table")),
        "profit_loss": _parse_period_table(soup.select_one("#profit-loss table")),
        "shareholding": _parse_period_table(soup.select_one("#shareholding table")),
    }


def load_l1_universe() -> pd.DataFrame:
    return load_l1_universe_tickers()


# Crawl-state scheduling (2026-08-13, docs/FUNDAMENTAL_SCREENER_RESULTS_ARC.md) --
# run_l2_state_refresh used to re-fetch EVERY L1 company's screener.in detail page
# EVERY day, unconditionally, regardless of whether the underlying annual/quarterly
# source data could plausibly have changed -- real blocking risk as the universe
# grows toward the source PRD's own ~500-700 estimate. This table lets it skip
# companies with nothing plausibly new since the last crawl.
CRAWL_STATE_TABLE = "fundamentals_l2_crawl_state"
# First-cut, undocumented-in-the-source-PRD placeholder, same "easy to tune once
# reviewed" treatment every other first-cut threshold in this pipeline gets:
# comfortably inside a quarterly filing cycle (SEBI LODR's own ~45-day results
# deadline plus buffer for screener.in to ingest it), not so long L2 goes a full
# quarter without ever re-checking a company that never got a pull_crawl_forward
# nudge (e.g. its BSE detection failed that day).
CRAWL_STATE_DEFAULT_INTERVAL_DAYS = 75

_CRAWL_STATE_TABLE_STATEMENT = """
    CREATE TABLE IF NOT EXISTS fundamentals_l2_crawl_state (
        company_id BIGINT PRIMARY KEY,
        ticker TEXT,
        last_crawled_at TIMESTAMPTZ,
        next_due_at TIMESTAMPTZ NOT NULL,
        load_ts TIMESTAMPTZ
    )
"""


def _ensure_crawl_state_table() -> None:
    def _op() -> None:
        with db_session() as (_, cur):
            cur.execute(_CRAWL_STATE_TABLE_STATEMENT)

    execute_db_operation(_op, operation_name="fundamentals_l2_crawl_state:ensure_table")


def load_crawl_state() -> pd.DataFrame:
    _ensure_crawl_state_table()
    return sql_to_df(f"SELECT company_id, next_due_at FROM {CRAWL_STATE_TABLE}")  # noqa: S608 -- constant, not user input


def filter_universe_to_due(universe: pd.DataFrame) -> pd.DataFrame:
    """Companies with no crawl-state row yet (never crawled -- due immediately, not
    a guessed "not due yet") OR next_due_at <= now. A company can also become due
    early via pull_crawl_forward (called by fundamentals/collectors/
    bse_announcements.py the moment it detects a fresh results filing) -- this
    function doesn't know or care why a company is due, it just reads next_due_at."""
    if universe.empty:
        return universe
    state = load_crawl_state()
    if state.empty:
        return universe
    due_at_by_company_id = dict(zip(state["company_id"], state["next_due_at"]))
    now = pd.Timestamp.now(tz="UTC")

    def _is_due(company_id) -> bool:
        due_at = due_at_by_company_id.get(company_id)
        if due_at is None:
            return True
        return pd.Timestamp(due_at) <= now

    return universe[universe["company_id"].apply(_is_due)]


def _mark_crawled(company_id, ticker) -> None:
    """Called only after a SUCCESSFUL detail-page fetch (see run_l2_state_refresh) --
    a failed company is never marked, so it stays due for retry next run rather than
    silently skipped for CRAWL_STATE_DEFAULT_INTERVAL_DAYS. Resets to the full
    default interval unconditionally, even for a company that was just pulled
    forward by a fresh-filing detection -- a known first-cut limitation (a single
    prompt re-check, not a sustained daily retry loop until screener.in's own
    number actually updates); worth revisiting if screener.in's own ingestion lag
    turns out to routinely exceed one day."""
    now = pd.Timestamp.now(tz="UTC")
    upsert_to_db(
        pd.DataFrame(
            [
                {
                    "company_id": company_id,
                    "ticker": ticker,
                    "last_crawled_at": now,
                    "next_due_at": now + pd.Timedelta(days=CRAWL_STATE_DEFAULT_INTERVAL_DAYS),
                    "load_ts": now,
                }
            ]
        ),
        CRAWL_STATE_TABLE,
        unique_keys=["company_id"],
    )


def pull_crawl_forward(company_master_ids: list[str]) -> dict[str, object]:
    """Called by fundamentals/collectors/bse_announcements.py the moment it detects
    a fresh results event for a company -- pulls that company's next_due_at forward
    to now, so the next L2 run re-checks screener.in soon instead of waiting out the
    full CRAWL_STATE_DEFAULT_INTERVAL_DAYS. This is what ties the two streams
    together operationally (stream B's detection schedules stream A's re-crawl, per
    docs/FUNDAMENTAL_SCREENER_RESULTS_ARC.md) without merging their data. Resolves
    company_master_id -> company_id via the latest L1 universe snapshot -- a company
    not in the current L1 universe (e.g. it dropped out) is silently skipped, not an
    error, since there's no crawl state to pull forward for it."""
    if not company_master_ids:
        return {"pulled_forward": 0}
    _ensure_crawl_state_table()
    tickers = [cmid.removeprefix("nse:") for cmid in company_master_ids]
    universe = sql_to_df(
        """
        SELECT company_id, ticker FROM fundamentals_l1_universe
        WHERE ticker = ANY(%s) AND run_date = (SELECT MAX(run_date) FROM fundamentals_l1_universe)
        """,
        params=(tickers,),
    )
    if universe.empty:
        return {"pulled_forward": 0}
    now = pd.Timestamp.now(tz="UTC")
    upsert_to_db(
        pd.DataFrame(
            {
                "company_id": universe["company_id"],
                "ticker": universe["ticker"],
                "last_crawled_at": None,
                "next_due_at": now,
                "load_ts": now,
            }
        ),
        CRAWL_STATE_TABLE,
        unique_keys=["company_id"],
    )
    return {"pulled_forward": int(len(universe))}


def build_l2_state_row(
    company: dict[str, object], pledge_levels: dict[int, float], valuation_levels: dict[int, dict[str, float]], detail: dict[str, object]
) -> dict[str, object]:
    valuation = valuation_levels.get(company["company_id"])
    return {
        "company_id": company["company_id"],
        "company_name": company["company_name"],
        "ticker": company["ticker"],
        **compute_debt_trajectory(detail["balance_sheet"], detail["profit_loss"]),
        **compute_cwip_ratio(detail["balance_sheet"]),
        "pledge_pct": pledge_levels.get(company["company_id"], 0.0),
        **compute_promoter_stake(detail["shareholding"]),
        **compute_institutional_stake(detail["shareholding"]),
        "sector_cycle_phase": None,
        # "pe" is the raw driver, same "store the input alongside the derived signal"
        # convention net_debt_rscr/promoter_pct already use -- also read back by
        # compute_valuation_sector_percentiles' in-memory ranking pass below
        # (run_l2_state_refresh), which needs every company's PE at once.
        "pe": valuation["pe"] if valuation else None,
        "valuation_vs_own_history_ratio": round(valuation["pe"] / valuation["historical_pe_5y"], 2) if valuation else None,
        "valuation_sector_percentile": None,  # filled in by run_l2_state_refresh's post-pass, needs every company's PE first
    }


def _build_institutional_entry_event_row(row: dict[str, object], *, latest_period: str, load_ts) -> dict[str, object]:
    """One synthetic fundamentals_events row for a detected institutional first
    entry -- see module docstring for why this is a synthetic event (no exchange
    filing exists) and why news_id is keyed on latest_period, not run_date."""
    ticker = row["ticker"]
    return {
        "source": "l2_state",
        "news_id": f"institutional_entry:{ticker}:{latest_period}",
        "company_master_id": f"nse:{ticker}",
        "isin": None,
        "filing_type": "institutional_entry",
        # Caveat text added 2026-08-13 (gap found auditing the frontend/alert
        # surface): the module docstring above already documents "first entry
        # visible in ~3 years of history, not a claim about the company's entire
        # history" -- that caveat lived only in code comments until now, never in
        # anything a human actually reads (the headline IS what a human reads: it
        # flows straight into evaluate_institutional_entry_trigger's fallback
        # reasoning and this pointer's frontend label).
        "headline": (
            f"First institutional (FII+DII) stake detected: {row.get('institutional_pct')}% as of {latest_period} "
            "(visible in screener.in's ~3yr shareholding history -- may be a re-entry if earlier history isn't captured)"
        ),
        "subcategory": None,
        "disclosure_date": row["run_date"].date() if hasattr(row["run_date"], "date") else row["run_date"],
        "announcement_timestamp": load_ts,
        "quantity": row.get("institutional_pct"),
        "insider_name": None,
        "transaction_type": None,
        "attachment_name": None,
        "detail_url": None,
        "detection_source": "l2_state_shareholding_pattern",
        # No document exists for this filing_type -- nothing to OCR/extract, so this
        # is never "pending" in the sense rating_action/results are (and the OCR/
        # structured-extraction queues already exclude it naturally: no
        # attachment_name or rationale_pdf_url).
        "enrichment_status": "not_applicable",
        "sources": "l2_state",
        "raw_json": json.dumps({"institutional_pct": row.get("institutional_pct"), "period": latest_period, "ticker": ticker}, ensure_ascii=False, default=str),
        "load_ts": load_ts,
    }


def run_l2_state_refresh(session=None, *, limit: int | None = None) -> dict[str, object]:
    """Build one L2 state row per DUE L1-universe company, keyed by (company_id,
    run_date, state_vector_version) -- append-only across refreshes (docs/
    FUNDAMENTAL_SCREENER_PRD.md sec 2). "Due" (2026-08-13, filter_universe_to_due)
    means no crawl-state row yet, next_due_at has passed, or pull_crawl_forward
    pulled it in early after a fresh results filing -- NOT literally every L1
    company every run, which real blocking risk (see filter_universe_to_due's own
    docstring) ended once the universe grows toward the source PRD's own ~500-700
    estimate. limit is applied AFTER the due-filter, not before, so a caller asking
    for N companies gets N genuinely-due ones, not N that might all be skipped."""
    universe = load_l1_universe()
    if universe.empty:
        _record_no_universe_fallback()
        return {"rows": 0, "failed_companies": [], "checks_deferred": list(DEFERRED_FIELDS), "companies": [], "institutional_first_entries": 0, "companies_due": 0}

    universe = filter_universe_to_due(universe)
    if limit:
        universe = universe.head(limit)
    if universe.empty:
        return {"rows": 0, "failed_companies": [], "checks_deferred": list(DEFERRED_FIELDS), "companies": [], "institutional_first_entries": 0, "companies_due": 0}

    session = session or build_authenticated_session()
    pledge_levels = fetch_pledge_levels(session)
    valuation_levels = fetch_valuation_levels(session)

    run_date = pd.Timestamp.now(tz="UTC").normalize()
    load_ts = pd.Timestamp.now(tz="UTC")
    rows: list[dict[str, object]] = []
    failed_companies: list[str] = []
    institutional_entry_events: list[dict[str, object]] = []
    for _, company in universe.iterrows():
        ticker = company["ticker"]
        if not ticker:
            continue
        try:
            detail = fetch_company_detail(session, ticker)
        except Exception as exc:  # noqa: BLE001 -- one bad company must not sink the batch
            failed_companies.append(ticker)
            _record_company_fetch_fallback(ticker, exc)
            continue
        row = build_l2_state_row(company, pledge_levels, valuation_levels, detail)
        row["run_date"] = run_date
        row["state_vector_version"] = STATE_VECTOR_VERSION
        row["load_ts"] = load_ts
        rows.append(row)
        _mark_crawled(company["company_id"], ticker)

        if row.get("institutional_first_entry"):
            periods = detail.get("shareholding", {}).get("periods") or []
            latest_period = periods[-1] if periods else str(run_date.date())
            institutional_entry_events.append(_build_institutional_entry_event_row(row, latest_period=latest_period, load_ts=load_ts))

    # Sector-relative valuation percentile needs every company's PE at once (it's a
    # cross-company rank, unlike every other field above which is purely per-company)
    # -- computed as a post-pass over the rows just built, not inline in
    # build_l2_state_row, which only ever sees one company at a time.
    if rows:
        sector_codes = load_sector_codes_for_tickers([row["ticker"] for row in rows])
        sector_percentiles = compute_valuation_sector_percentiles(rows, sector_codes)
        for row in rows:
            row["valuation_sector_percentile"] = sector_percentiles.get(row["company_id"])

    if rows:
        upsert_to_db(
            pd.DataFrame(rows),
            RESULTS_TABLE,
            unique_keys=["company_id", "run_date", "state_vector_version"],
        )
    if institutional_entry_events:
        _ensure_events_schema()
        upsert_to_db(pd.DataFrame(institutional_entry_events), EVENTS_TABLE, unique_keys=["source", "news_id"])
    _record_deferred_fields_fallback()
    return {
        "rows": len(rows),
        "failed_companies": failed_companies,
        "checks_deferred": list(DEFERRED_FIELDS),
        "companies": [row["company_name"] for row in rows][:20],
        "institutional_first_entries": len(institutional_entry_events),
        "companies_due": int(len(universe)),
    }


def main() -> int:
    global STOCKEY_RUN_STATE
    result = run_l2_state_refresh()
    STOCKEY_RUN_STATE = {
        "source": SYNC_SOURCE_NAME,
        "rows": result["rows"],
        "rows_written": result["rows"],
        "state_vector_version": STATE_VECTOR_VERSION,
        "checks_deferred": result["checks_deferred"],
        "failed_companies": result["failed_companies"],
        "institutional_first_entries": result.get("institutional_first_entries", 0),
        "companies_due": result.get("companies_due", 0),
        "fallback_used": True,  # deferred fields + any per-company fetch failures are a standing, visible fallback
        "state_advanced": result["rows"] > 0,
    }
    print(json.dumps({"status": "ok", **STOCKEY_RUN_STATE}, ensure_ascii=False, default=str), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
