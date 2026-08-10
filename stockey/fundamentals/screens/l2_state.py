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

Two of the source PRD's six L2 fields are explicitly out of scope for this module,
same "deferred, not silent" treatment L1 already uses for auditor/RPT:

- `sector_cycle_phase` -- inherently cross-company (source PRD: "aggregating
  gross-block growth across all listed players in the sector"), belongs to build-order
  step 9 (sector capital-cycle aggregation), not a per-company state field.
- `valuation_percentile` -- source PRD wants EV/EBITDA vs *both* own history and
  sector; the sector half has the same step-9 dependency as above, and computing an
  own-history-only percentile now would be a weaker, differently-shaped metric wearing
  the same name. Deferred as one unit rather than half-built.

Both stored as NULL columns plus a standing fallback_telemetry event every run (see
_record_deferred_fields_fallback), matching this repo's no-silent-fallback rule.

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
"""

from __future__ import annotations

import json
import re

import pandas as pd
from bs4 import BeautifulSoup

from fundamentals.collectors.screenerin import build_authenticated_session, clean_text, run_query
from fundamentals.collectors.screenerin import to_number as _screenerin_to_number
from fundamentals.screens.l1_universe import load_l1_universe_tickers
from utils.db import upsert_to_db
from utils.exchange_rate_limiter import exchange_request_gate
from utils.fallback_telemetry import record_local_fallback_event

SYNC_SOURCE_NAME = "fundamentals.screens.l2_state"
RESULTS_TABLE = "fundamentals_l2_state"
STATE_VECTOR_VERSION = 1
STOCKEY_RUN_STATE: dict[str, object] = {}

COMPANY_URL_TEMPLATE = "https://www.screener.in/company/{ticker}/"
PLEDGE_QUERY = "Pledged percentage > 0"

# Not silently skipped -- see module docstring. Every run logs a fallback event naming
# these, and the run summary/STOCKEY_RUN_STATE carries checks_deferred.
DEFERRED_FIELDS = ("sector_cycle_phase", "valuation_percentile")


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


def build_l2_state_row(company: dict[str, object], pledge_levels: dict[int, float], detail: dict[str, object]) -> dict[str, object]:
    return {
        "company_id": company["company_id"],
        "company_name": company["company_name"],
        "ticker": company["ticker"],
        **compute_debt_trajectory(detail["balance_sheet"], detail["profit_loss"]),
        **compute_cwip_ratio(detail["balance_sheet"]),
        "pledge_pct": pledge_levels.get(company["company_id"], 0.0),
        **compute_promoter_stake(detail["shareholding"]),
        "sector_cycle_phase": None,
        "valuation_percentile": None,
    }


def run_l2_state_refresh(session=None, *, limit: int | None = None) -> dict[str, object]:
    """Build one L2 state row per L1-universe company, keyed by (company_id, run_date,
    state_vector_version) -- append-only across refreshes (docs/
    FUNDAMENTAL_SCREENER_PRD.md sec 2)."""
    universe = load_l1_universe()
    if limit:
        universe = universe.head(limit)
    if universe.empty:
        _record_no_universe_fallback()
        return {"rows": 0, "failed_companies": [], "checks_deferred": list(DEFERRED_FIELDS), "companies": []}

    session = session or build_authenticated_session()
    pledge_levels = fetch_pledge_levels(session)

    run_date = pd.Timestamp.now(tz="UTC").normalize()
    load_ts = pd.Timestamp.now(tz="UTC")
    rows: list[dict[str, object]] = []
    failed_companies: list[str] = []
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
        row = build_l2_state_row(company, pledge_levels, detail)
        row["run_date"] = run_date
        row["state_vector_version"] = STATE_VECTOR_VERSION
        row["load_ts"] = load_ts
        rows.append(row)

    if rows:
        upsert_to_db(
            pd.DataFrame(rows),
            RESULTS_TABLE,
            unique_keys=["company_id", "run_date", "state_vector_version"],
        )
    _record_deferred_fields_fallback()
    return {
        "rows": len(rows),
        "failed_companies": failed_companies,
        "checks_deferred": list(DEFERRED_FIELDS),
        "companies": [row["company_name"] for row in rows][:20],
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
        "fallback_used": True,  # deferred fields + any per-company fetch failures are a standing, visible fallback
        "state_advanced": result["rows"] > 0,
    }
    print(json.dumps({"status": "ok", **STOCKEY_RUN_STATE}, ensure_ascii=False, default=str), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
