"""Sector capital-cycle aggregation -- fundamental screener step 9
(docs/FUNDAMENTAL_SCREENER_PRD.md sec 8 step 9, fundamental_basic_goal.md's L2
section: sector_cycle_phase "computed by aggregating gross-block growth across all
listed players in the sector vs demand proxy; sector-level, not company-level").

Scope, per the source PRD's own sources table ("Sector capacity aggregate | Computed
from L1 universe gross block"): this aggregates WITHIN the L1 universe, not the full
market -- L1 already defines "everything this system watches", and capacity data for
188 companies via screener.in is tractable where full-market would not be.

Two inputs, both screener.in, both confirmed live 2026-08-11:

- Capacity side: `Gross block` / `Gross block preceding year` -- confirmed queryable
  as a standalone cross-sectional query (`Gross block > 0 AND Gross block preceding
  year > 0`, scoped to L1's own market-cap band to keep the result set bounded),
  intersected with L1's actual company_id set in Python -- same "standalone query,
  Python-side intersection" pattern fundamentals/screens/l2_state.py already uses for
  pledge_pct, for the same reason (screener.in's ~3-extra-column cap and its
  compound-expression column-reveal quirk make appending this to L1_QUERY unsafe).
- Demand side (proxy, not the source PRD's intended HS-code import data -- that's
  explicitly deferred to step 10): aggregate quarterly sales growth
  (`qtr_sales_var_pct`), already sitting in fundamentals_l1_universe.metrics_json from
  L1's own crawl -- no new fetch needed for this half.

Sector grouping key: fundamentals_company_sector (dim_security, then Sharpely for
BSE-only names -- fundamentals/screens/company_sector.py), decoded via
fundamentals/collectors/sector_data.py's fundamentals_sector_reference table (built
alongside this module). ~73% L1 coverage confirmed live -- companies with no
sector_code are excluded from aggregation and counted, not silently dropped.

Phase classification is a first-cut, fixed-threshold read
(capacity_growth_pct - demand_growth_pct, +-5 points), not a calibrated model --
same "documented placeholder, easy to tune once reviewed" treatment L1's market-cap
band and liquidity floor already got. Median (not mean) demand growth is used per
sector to reduce sensitivity to the wild outliers a small-base %-growth figure can
produce (confirmed live during L1's own build: a single quarter's low base can show
a scaled a company's YoY growth in the thousands of percent).

Two real limitations found live 2026-08-11 running this against the actual L1
universe, not hypothetical -- both worth knowing before reading a `phase` value,
neither silently hidden:

- Gross block is a weak-to-meaningless capacity proxy for asset-light sectors.
  Financial Services came back "capacity_expansion" and Information Technology/
  Services came back "capacity_discipline" in the live run, but none of those
  businesses are capacity-constrained by physical fixed assets the way Capital Goods/
  Chemicals/Auto Components are -- their "gross block" is mostly office premises and
  equipment, not a meaningful read on their actual operating capacity. The capital-
  cycle framework this module implements assumes capital-intensive, physical-capacity
  industries; this module does not attempt to detect and exclude asset-light sectors
  automatically (would need its own classification work), so treat `phase` as
  informative for capital-intensive sectors and treat it with real skepticism for
  services/financial/IT sectors until that gap is addressed.
- Small-N sectors give a statistically thin "aggregate". The live run had multiple
  sectors with only 1-3 L1 companies (Construction Materials, Metals & Mining,
  Telecommunication, Power, Utilities all had exactly 1) -- a single company's gross-
  block growth is not a sector read. `sample_size_confidence` ("low" below
  MIN_COMPANIES_FOR_CONFIDENCE, else "adequate") is stored on every row precisely so
  this isn't silently presented as equally reliable as a 20+ company sector.

REWORKED 2026-09-24 (operator decision: "rework, same company set"). The first cut
compared two numbers built from DIFFERENT companies over DIFFERENT horizons: capacity was
the SUM of gross block over companies with gross-block data (annual, balance sheet), demand
was the median QUARTERLY YoY sales change over every L1 company in the sector. The sum let
one large company set the whole sector's capacity growth (Chemicals read -10.25 percent on
2026-09-23), and the quarterly figure is the noisiest growth number screener.in has. Now,
per company: capacity growth = gross block vs gross block preceding year, demand growth =
screener.in "Sales growth" (latest ANNUAL YoY, the same fiscal year as the balance sheet --
verified live: 1778.06 / 896.05 = +98.4 vs its reported 98.43). Both come from ONE query,
so they are the same companies. The sector's phase reads the MEDIAN per-company gap
(capacity minus demand, `capacity_minus_demand_pts`); capacity_growth_pct and
demand_growth_pct are the medians of each side over that same paired set. growth_
classification therefore also reads annual sales growth now.

Asset-light sectors (ASSET_LIGHT_SECTOR_CODES) get no phase: a lender's or IT firm's gross
block is offices, not capacity (see the first caveat above). Their growth classification is
still computed.
"""

from __future__ import annotations

import json

import pandas as pd

from fundamentals.collectors.screenerin import build_authenticated_session, run_query
from utils.company_master import map_company_master_ids_nse_or_bse
from utils.db import sql_to_df, upsert_to_db
from utils.fallback_telemetry import record_local_fallback_event

SYNC_SOURCE_NAME = "fundamentals.screens.sector_cycle"
RESULTS_TABLE = "fundamentals_sector_cycle"
STOCKEY_RUN_STATE: dict[str, object] = {}

# Matches fundamentals/screens/l1_universe.py's own market-cap band -- the gross-block
# fetch is scoped to it purely to keep the standalone query's result set bounded
# (confirmed live: fetching it unbounded pulls in the entire market), not because the
# band itself is meaningful here; every row gets intersected against L1's actual
# company_id set regardless.
#
# 2026-09-24: "Sales growth" (latest annual YoY) added as the third revealed column. ORDER
# MATTERS -- screener.in reveals only ~3 queried columns, in query order, and the default
# Mar Cap column already carries the band, so Market Capitalization goes LAST. Verified
# live: this order returns gross_block_rscr, gross_block_py_rscr and sales_growth_pct.
GROSS_BLOCK_QUERY = (
    "Gross block > 0 AND Gross block preceding year > 0 AND Sales growth > -100 AND "
    "Market Capitalization > 100 AND Market Capitalization < 5000"
)
REQUIRED_METRICS = ("gross_block_rscr", "gross_block_py_rscr", "sales_growth_pct")

# Sectors whose gross block is not operating capacity -- no phase (2026-09-24). Financial
# Services came back "capacity_expansion" in the first live run off office premises.
# Realty: a developer's capacity is land and inventory, not gross block -- the first reworked
# run read median block growth 0.00 against 37 percent sales growth ("discipline" off nothing).
ASSET_LIGHT_SECTOR_CODES = frozenset({"IN0501", "IN0801", "IN0205"})  # Financial Services, IT, Realty

# First-cut, undocumented-in-the-source-PRD threshold -- see module docstring.
PHASE_THRESHOLD_POINTS = 5.0
# Below this many companies contributing gross-block data, a sector's aggregate is
# flagged low-confidence rather than presented as equivalent to a 20+ company sector
# -- see module docstring's "small-N sectors" caveat, found live 2026-08-11.
MIN_COMPANIES_FOR_CONFIDENCE = 5

# Growth-classification thresholds (2026-08-13, user request: "sectoral health...
# should create a classification (High Growth sector, Low Growth Sector, Medium
# Growth, No pattern etc.)"). Deliberately a SEPARATE axis from `phase` above:
# phase reads capacity_growth_pct vs demand_growth_pct (cyclical positioning --
# is the sector over/under-building relative to its own demand), while this reads
# demand_growth_pct alone (is underlying demand actually growing fast). A sector can
# be "capacity_discipline" (phase) and "high_growth" (this) at once -- that
# combination is the textbook bullish setup, which is exactly why they're kept
# independent rather than collapsed into one field. Same first-cut, fixed-threshold
# treatment phase got -- easy to tune once reviewed against real sector outcomes.
GROWTH_HIGH_THRESHOLD_PCT = 15.0
GROWTH_MEDIUM_THRESHOLD_PCT = 5.0


def _record_fallback(fallback_type: str, *, reason: str, error, severity: str = "warn", metadata=None) -> None:
    record_local_fallback_event(
        module=SYNC_SOURCE_NAME,
        source="screenerin",
        fallback_type=fallback_type,
        severity=severity,
        reason=reason,
        error=error,
        metadata=metadata or {},
    )


def load_l1_companies_with_sector() -> pd.DataFrame:
    """Latest L1 universe, joined to dim_security.sector_code (most recent row per
    company) and with qtr_sales_var_pct pulled out of metrics_json -- the demand-side
    input, already collected by L1's own crawl.

    BUG FOUND LIVE 2026-08-15, fixed here: the join used to build company_master_id
    as a naive 'nse:' || ticker SQL concatenation. fundamentals_l1_universe.ticker is
    whatever screener.in's own company-URL slug is -- sometimes a real NSE symbol,
    sometimes a raw BSE numeric scrip code (confirmed live: 43/192, 22%, of the
    current L1 universe -- e.g. ticker "524634" truly resolves to company_master_id
    'nse:ALUFLUOR' via company_master, not the naive 'nse:524634'). The naive join
    silently found no sector_code for every one of those companies. Resolved the
    same way every other identity lookup in this codebase already does it
    (map_company_master_ids_nse_or_bse, NSE-first with a BSE-ticker fallback) instead
    of a second, incorrect ad-hoc construction."""
    l1 = sql_to_df(
        """
        SELECT company_id, company_name, ticker, metrics_json
        FROM fundamentals_l1_universe
        WHERE run_date = (SELECT MAX(run_date) FROM fundamentals_l1_universe)
        """
    )
    if l1.empty:
        return l1

    l1["company_master_id"] = map_company_master_ids_nse_or_bse(l1["ticker"])

    sector_df = sql_to_df(
        """
        -- fundamentals/screens/company_sector.py: dim_security, then Sharpely (BSE-only names)
        SELECT company_master_id, sector_code FROM fundamentals_company_sector
        """
    )
    sector_by_cmid = dict(zip(sector_df["company_master_id"], sector_df["sector_code"])) if not sector_df.empty else {}
    l1["sector_code"] = l1["company_master_id"].map(sector_by_cmid)

    l1["qtr_sales_var_pct"] = l1["metrics_json"].apply(lambda raw: json.loads(raw).get("qtr_sales_var_pct") if raw else None)
    return l1.drop(columns=["metrics_json", "company_master_id"])


def fetch_gross_block_data(session=None) -> dict[int, dict[str, float]]:
    """Per screener.in company_id: gross block this and last year, and annual sales
    growth -- all three from the same query, so every row is a matched pair."""
    session = session or build_authenticated_session()
    _, companies = run_query(session, GROSS_BLOCK_QUERY)
    if companies:
        revealed = set().union(*(c.get("metrics", {}).keys() for c in companies))
        missing = [m for m in REQUIRED_METRICS if m not in revealed]
        if missing:
            # Column-reveal changed under us: fail loudly instead of writing a phase from
            # nothing (every company would silently drop out as incomplete).
            raise RuntimeError(f"screener.in did not reveal {missing} for the sector-cycle query")
    result: dict[int, dict[str, float]] = {}
    for company in companies:
        company_id = company.get("company_id")
        metrics = company.get("metrics", {})
        current = metrics.get("gross_block_rscr")
        preceding = metrics.get("gross_block_py_rscr")
        sales_growth = metrics.get("sales_growth_pct")
        if (company_id is not None and isinstance(current, (int, float))
                and isinstance(preceding, (int, float)) and preceding > 0
                and isinstance(sales_growth, (int, float))):
            result[company_id] = {
                "gross_block_current_rscr": current,
                "gross_block_preceding_rscr": preceding,
                "sales_growth_pct": float(sales_growth),
            }
    return result


def classify_phase(capacity_minus_demand_pts: float | None) -> str | None:
    """Phase from the sector's median per-company gap (capacity growth minus annual sales
    growth, in percentage points)."""
    if capacity_minus_demand_pts is None:
        return None
    if capacity_minus_demand_pts > PHASE_THRESHOLD_POINTS:
        return "capacity_expansion"
    if capacity_minus_demand_pts < -PHASE_THRESHOLD_POINTS:
        return "capacity_discipline"
    return "balanced"


def classify_growth(demand_growth_pct: float | None, sample_size_confidence: str) -> str | None:
    """"No pattern" whenever the read isn't trustworthy -- no demand data at all, OR
    the sector's sample is too thin (sample_size_confidence == 'low', same
    MIN_COMPANIES_FOR_CONFIDENCE gate `phase`'s own caveat documents) -- a 1-company
    median is not a sector growth rate, same reasoning that gates `phase` from being
    over-read on small-N sectors."""
    if demand_growth_pct is None or sample_size_confidence == "low":
        return "no_pattern"
    if demand_growth_pct >= GROWTH_HIGH_THRESHOLD_PCT:
        return "high_growth"
    if demand_growth_pct >= GROWTH_MEDIUM_THRESHOLD_PCT:
        return "medium_growth"
    return "low_growth"


def _median(values: pd.Series) -> float | None:
    values = values.dropna()
    return round(float(values.median()), 2) if not values.empty else None


def compute_sector_aggregates(l1_with_sector: pd.DataFrame, gross_block_data: dict[int, dict[str, float]]) -> pd.DataFrame:
    if l1_with_sector.empty:
        return pd.DataFrame()

    df = l1_with_sector.copy()
    for field in ("gross_block_current_rscr", "gross_block_preceding_rscr", "sales_growth_pct"):
        df[field] = df["company_id"].map(lambda cid, f=field: gross_block_data.get(cid, {}).get(f))
    df["company_capacity_growth_pct"] = (
        (df["gross_block_current_rscr"] - df["gross_block_preceding_rscr"]) / df["gross_block_preceding_rscr"] * 100
    )

    with_sector = df[df["sector_code"].notna()]
    rows = []
    for sector_code, group in with_sector.groupby("sector_code"):
        # Paired: companies with BOTH sides -- capacity and demand read the same set.
        paired = group.dropna(subset=["company_capacity_growth_pct", "sales_growth_pct"])
        capacity_growth_pct = _median(paired["company_capacity_growth_pct"])
        demand_growth_pct = _median(paired["sales_growth_pct"])
        gap = _median(paired["company_capacity_growth_pct"] - paired["sales_growth_pct"])
        sample_size_confidence = "adequate" if len(paired) >= MIN_COMPANIES_FOR_CONFIDENCE else "low"
        phase = None if sector_code in ASSET_LIGHT_SECTOR_CODES else classify_phase(gap)

        rows.append(
            {
                "sector_code": sector_code,
                "n_companies_in_l1": int(len(group)),
                "n_companies_with_gross_block": int(group["company_capacity_growth_pct"].notna().sum()),
                "n_companies_with_demand_data": int(len(paired)),
                "capacity_growth_pct": capacity_growth_pct,
                "demand_growth_pct": demand_growth_pct,
                "capacity_minus_demand_pts": gap,
                "phase": phase,
                "growth_classification": classify_growth(demand_growth_pct, sample_size_confidence),
                "sample_size_confidence": sample_size_confidence,
                "run_date": pd.Timestamp.now(tz="UTC").normalize(),
                "load_ts": pd.Timestamp.now(tz="UTC"),
            }
        )
    return pd.DataFrame(rows)


def run_sector_cycle_aggregation() -> dict[str, object]:
    l1_with_sector = load_l1_companies_with_sector()
    if l1_with_sector.empty:
        _record_fallback(
            "sector_cycle_no_l1_universe",
            reason="No fundamentals_l1_universe rows to aggregate by sector.",
            error="empty L1 universe",
        )
        return {"sectors": 0, "companies_without_sector_code": 0}

    missing_sector = l1_with_sector[l1_with_sector["sector_code"].isna()]
    if not missing_sector.empty:
        _record_fallback(
            "sector_cycle_missing_sector_code",
            reason="Some L1 companies have no dim_security.sector_code yet; excluded from sector aggregation, not silently dropped.",
            error="missing sector_code",
            metadata={"count": int(len(missing_sector)), "tickers": list(missing_sector["ticker"])[:50]},
        )

    gross_block_data = fetch_gross_block_data()
    result = compute_sector_aggregates(l1_with_sector, gross_block_data)
    if not result.empty:
        upsert_to_db(result, RESULTS_TABLE, unique_keys=["sector_code", "run_date"])

    return {"sectors": int(len(result)), "companies_without_sector_code": int(len(missing_sector))}


def main() -> int:
    global STOCKEY_RUN_STATE
    result = run_sector_cycle_aggregation()
    STOCKEY_RUN_STATE = {
        "source": SYNC_SOURCE_NAME,
        "rows": result["sectors"],
        "rows_written": result["sectors"],
        "companies_without_sector_code": result["companies_without_sector_code"],
        "fallback_used": result["companies_without_sector_code"] > 0,
        "state_advanced": result["sectors"] > 0,
        "status": "ok",
    }
    print(json.dumps(STOCKEY_RUN_STATE, ensure_ascii=False, default=str), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
