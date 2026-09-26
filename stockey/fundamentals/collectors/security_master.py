"""Security master -- fundamental screener step 0
(docs/FUNDAMENTAL_SCREENER_PRD.md sec 5, sec 8 step 0). Two independent pieces:

1. EQUITY_L.csv ingestion. stockey's existing identity layer (dim_security /
   dim_security_history) is derived ONLY from observed nseindia_ohlcv price rows
   (data.nseindia.security_history), so it misses any currently-listed name that
   hasn't shown up in a parsed bhavcopy day yet -- recent listings, thinly-traded
   microcaps, or names whose ISIN changed without a follow-up identity-break
   resolution. EQUITY_L.csv (the full NSE-listed universe) is a second, independent
   observation source that fills that gap.

   This is additive to the existing identity pipeline, not a fork of it: it reuses
   data.nseindia.security_history's exact security_id-minting and
   identity-break-detection helpers (default_security_id, hash_key) rather than
   duplicating that logic, then calls data.nseindia.security_dimension.run()
   (unmodified) to propagate the result into dim_security the same way the pure-TA
   pipeline already does. No pure-TA file is modified by this module.

   Two outcomes per missing ISIN, handled differently:
     - symbol never seen before -> new dim_security_history row (mapping_source=
       "equity_master"), no ambiguity, inserted directly.
     - symbol already known under a DIFFERENT isin -> NOT inserted blind; written to
       dim_security_review_events as event_type="identity_break_candidate" via the
       same schema data.nseindia.security_history.build_review_events already uses,
       for the existing manual-review workflow (docs/identity.md) to resolve.

2. BSE numeric scrip code backfill. BSE's APIs key on a numeric scrip code (e.g.
   543235), not the ticker (bse_ticker, already in company_master) -- fundamentals'
   BSE crawlers (announcements, PIT/SAST) need this to query BSE at all. Looked up
   by ISIN (never by name -- ISIN is the one unambiguous key, per docs sec 4) against
   BSE's public smart-search endpoint, confirmed live 2026-08-10:
   api.bseindia.com/BseIndiaAPI/api/PeerSmartSearch/w?Type=SS&text=<ISIN>. No cookie
   gate (matches the source PRD sec 3.2's characterization of BSE vs NSE), but rate
   gated the same as everything else -- this is still bseindia.com, untested for how
   fast it blocks, and the NSE lesson (2026-08-09/10) is not to find out the hard way.
"""

from __future__ import annotations

import io
import json
import re

import pandas as pd
import requests
from environs import Env

from data.nseindia import security_dimension
from data.nseindia.security_history import (
    default_security_id,
    ensure_identity_tables,
    hash_key,
    load_security_overrides,
)
from utils import store
from utils.company_master import map_company_master_ids
from utils.db import sql_to_df, upsert_to_db
from utils.exchange_rate_limiter import exchange_request_gate
from utils.fallback_telemetry import record_local_fallback_event
from utils.nse_rate_limiter import nse_request_gate

env = Env()
env.read_env()

EQUITY_L_URL = "https://nsearchives.nseindia.com/content/equities/EQUITY_L.csv"
UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
HEADERS = {"User-Agent": UA, "Referer": "https://www.nseindia.com/all-reports", "Accept": "*/*"}

BSE_SEARCH_URL = "https://api.bseindia.com/BseIndiaAPI/api/PeerSmartSearch/w"
BSE_HEADERS = {"User-Agent": UA, "Referer": "https://www.bseindia.com/", "Accept": "application/json, text/plain, */*"}
# BSE's smart-search response is an HTML fragment per match, e.g.:
#   <li ... ng-click="liclick('543235','ANGEL ONE LTD')">...<span>ANGELONE&nbsp;&nbsp;&nbsp;INE732I01021&nbsp;&nbsp;&nbsp;543235</span>...</li>
# with the matched search term sometimes wrapped in <strong>...</strong> (e.g. an
# ISIN search highlights the ISIN) -- confirmed against 3 live responses 2026-08-10.
_BSE_LI_BLOCK_RE = re.compile(r"liclick\('(?P<scrip_code>\d+)','(?P<name>[^']*)'\)(?P<rest>.*?)</li>", re.DOTALL)
_BSE_SPAN_RE = re.compile(r"<span>(?P<content>.*?)</span>", re.DOTALL)
_HTML_TAG_RE = re.compile(r"<[^>]+>")

SYNC_SOURCE_NAME = "fundamentals.collectors.security_master"
STOCKEY_RUN_STATE: dict[str, object] = {}


def fetch_equity_l() -> pd.DataFrame:
    """Download EQUITY_L.csv through the shared NSE rate gate, archive the raw bytes
    to S3 for audit trail, and return it as a cleaned DataFrame."""
    with nse_request_gate():
        response = requests.get(EQUITY_L_URL, headers=HEADERS, timeout=30)
    response.raise_for_status()

    snapshot_date = pd.Timestamp.now(tz="UTC").strftime("%Y-%m-%d")
    store.save_file_content(
        key=f"fundamentals/security_master/EQUITY_L_{snapshot_date}.csv",
        content=response.content,
    )

    df = pd.read_csv(io.BytesIO(response.content))
    df.columns = [c.strip() for c in df.columns]
    df["SYMBOL"] = df["SYMBOL"].astype("string").str.strip()
    df["SERIES"] = df["SERIES"].astype("string").str.strip()
    df["ISIN NUMBER"] = df["ISIN NUMBER"].astype("string").str.strip()
    return df


def classify_gaps(equity_l: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Diff EQUITY_L.csv's ISINs against dim_security's current ISIN set. Returns
    (new_symbols, identity_break_candidates) -- new_symbols are ISINs whose SYMBOL
    has never been seen before (safe to insert directly); identity_break_candidates
    are ISINs for a symbol dim_security already knows under a different ISIN (needs
    manual review, never inserted blind -- see docs/identity.md's rename_candidate /
    identity_break_candidate heuristics, the same vocabulary reused here)."""
    current = sql_to_df("SELECT DISTINCT isin, symbol FROM dim_security WHERE isin IS NOT NULL")
    known_isins = set(current["isin"]) if not current.empty else set()
    known_symbols = set(current["symbol"]) if not current.empty else set()

    missing = equity_l[~equity_l["ISIN NUMBER"].isin(known_isins)].copy()
    is_known_symbol = missing["SYMBOL"].isin(known_symbols)
    new_symbols = missing[~is_known_symbol].copy()
    identity_breaks = missing[is_known_symbol].copy()
    return new_symbols, identity_breaks


def build_new_history_rows(new_symbols: pd.DataFrame) -> pd.DataFrame:
    """Mint dim_security_history rows for genuinely-new symbols, using the exact
    same security_id scheme data.nseindia.security_history uses so both observation
    sources produce identity_ids in the same namespace."""
    if new_symbols.empty:
        return pd.DataFrame()

    df = pd.DataFrame(
        {
            "symbol": new_symbols["SYMBOL"],
            "series": new_symbols["SERIES"],
            "isin": new_symbols["ISIN NUMBER"],
        }
    )
    df["effective_from"] = pd.to_datetime(
        new_symbols["DATE OF LISTING"], format="%d-%b-%Y", utc=True, errors="coerce"
    )
    # No observed price rows yet -- "still listed as of this snapshot" is the only
    # honest effective_to; a real trading window will supersede this once
    # data.nseindia.security_history observes it in nseindia_ohlcv.
    df["effective_to"] = pd.Timestamp.now(tz="UTC").normalize()
    df["price_row_count"] = 0
    df["raw_security_key"] = df["symbol"] + ":" + df["series"]
    df["security_id"] = df.apply(
        lambda row: default_security_id(row["symbol"], row["series"], row["isin"]), axis=1
    )
    df["predecessor_security_id"] = pd.NA
    df["successor_security_id"] = pd.NA
    df["relation_type"] = "observed_isin"
    df["mapping_source"] = "equity_master"
    df["confidence"] = 0.95  # slightly below security_history's 0.98: listing record, not an observed trade

    overrides = load_security_overrides()
    if not overrides.empty:
        from data.nseindia.security_history import match_override

        for idx, row in df.iterrows():
            override = match_override(row, overrides)
            if override is None:
                continue
            df.at[idx, "security_id"] = override["security_id"]
            df.at[idx, "predecessor_security_id"] = override["predecessor_security_id"]
            df.at[idx, "successor_security_id"] = override["successor_security_id"]
            df.at[idx, "relation_type"] = override["relation_type"] or "manual_override"
            df.at[idx, "mapping_source"] = "manual_override"
            df.at[idx, "confidence"] = override["confidence"] if pd.notna(override["confidence"]) else 1.0

    df["company_master_id"] = map_company_master_ids(df["symbol"], exchange="NSE")
    return df


def build_identity_break_events(identity_breaks: pd.DataFrame) -> pd.DataFrame:
    """Write dim_security_review_events rows for symbols whose EQUITY_L.csv ISIN
    disagrees with dim_security's current one -- same event_type and event_key
    scheme data.nseindia.security_history.build_review_events uses, so these land in
    the one existing manual-review queue rather than a second one.

    BUG FOUND LIVE 2026-08-17: "current" used to mean "whichever row Postgres happened
    to return last for that symbol" -- SELECT with no ORDER BY, then pandas
    drop_duplicates(keep="last") over that undefined order. Every sibling query in this
    module that needs "the current row per symbol" (see backfill_bse_scrip_codes above)
    already uses DISTINCT ON (...) ORDER BY last_trade_date DESC NULLS LAST,
    effective_to DESC NULLS LAST to pick deterministically; this one didn't. 20 real
    symbols have more than one dim_security row and would have been affected -- no
    wrong review event confirmed yet, but the picked "prior" isin/security_id was
    exposed to Postgres's own unspecified row order, not the actual current row.

    BUG FOUND LIVE 2026-08-18 (re-audit): that fix still had no FINAL tiebreaker --
    last_trade_date/effective_to alone are still non-deterministic on a true tie.
    Live: 12 symbols tied on both (e.g. AARTISURF's EQ and P1 rows share the exact
    same dates), 3 real identity-break pairs already double-recorded under different
    prior ISINs as a result. Adds "series = 'EQ'" as a preference (the canonical
    equity listing -- same convention fundamentals/screens/technicals.py and
    watchlist.py already use for "the" price series over a partly-paid/rights
    variant), then isin as a final, fully deterministic tiebreaker."""
    if identity_breaks.empty:
        return pd.DataFrame()

    current = sql_to_df(
        """
        SELECT DISTINCT ON (symbol) isin, symbol, series, security_id
        FROM dim_security
        WHERE isin IS NOT NULL
        ORDER BY symbol, last_trade_date DESC NULLS LAST, effective_to DESC NULLS LAST, (series = 'EQ') DESC, isin
        """
    )
    current_by_symbol = current.set_index("symbol")

    events: list[dict[str, object]] = []
    now = pd.Timestamp.now(tz="UTC")
    for _, row in identity_breaks.iterrows():
        symbol = row["SYMBOL"]
        series = row["SERIES"]
        new_isin = row["ISIN NUMBER"]
        prior = current_by_symbol.loc[symbol] if symbol in current_by_symbol.index else None
        prior_isin = prior["isin"] if prior is not None else None
        prior_security_id = prior["security_id"] if prior is not None else None
        new_security_id = default_security_id(symbol, series, new_isin)
        events.append(
            {
                "event_key": hash_key("identity_break_candidate", symbol, series, prior_isin, new_isin),
                "event_type": "identity_break_candidate",
                "symbol": symbol,
                "series": series,
                "isin": prior_isin,
                "related_symbol": symbol,
                "related_series": series,
                "related_isin": new_isin,
                "first_seen": now,
                "last_seen": now,
                "security_id": prior_security_id,
                "related_security_id": new_security_id,
                "confidence": 0.90,
                "reason": "EQUITY_L.csv shows a different ISIN for this symbol than dim_security's current record",
                "needs_review": True,
            }
        )
    return pd.DataFrame(events)


def parse_bse_search_response(text: str) -> list[dict[str, str | None]]:
    """Extract (scrip_code, company_name, symbol, isin) from every <li> match in a
    BSE smart-search response. Strips HTML tags from the <span> content before
    splitting on whitespace so a highlighted <strong>ISIN</strong> (as happens when
    searching by ISIN) parses the same as a plain match."""
    results: list[dict[str, str | None]] = []
    for block in _BSE_LI_BLOCK_RE.finditer(text):
        span_match = _BSE_SPAN_RE.search(block.group("rest"))
        if span_match is None:
            continue
        span_text = _HTML_TAG_RE.sub("", span_match.group("content")).replace("&nbsp;", " ")
        parts = [p for p in span_text.split() if p]
        results.append(
            {
                "scrip_code": block.group("scrip_code"),
                "company_name": block.group("name"),
                "symbol": parts[0] if len(parts) > 0 else None,
                "isin": parts[1] if len(parts) > 1 else None,
            }
        )
    return results


def lookup_bse_scrip_code(isin: str) -> dict[str, str | None] | None:
    """Look up one ISIN's BSE scrip code through the shared cross-process BSE rate
    gate. Only trusts an exact ISIN match in the response (defensive: a fuzzy/partial
    match would silently attach the wrong scrip code to a company)."""
    with exchange_request_gate(domain="bse"):
        response = requests.get(
            BSE_SEARCH_URL, params={"Type": "SS", "text": isin}, headers=BSE_HEADERS, timeout=20
        )
    response.raise_for_status()
    matches = [m for m in parse_bse_search_response(response.text) if m["isin"] == isin]
    return matches[0] if matches else None


def copy_bse_scrip_code_from_ticker() -> int:
    """Fast path, zero BSE requests: company_master.bse_ticker is already the numeric
    BSE scrip code for every populated row observed so far (confirmed live 2026-08-10:
    5429/5429 non-null bse_ticker values are purely numeric -- whatever originally
    populated bse_ticker, likely Sharpely's own master, already used BSE's scrip code
    as the identifier). Copy directly rather than re-deriving the same value through a
    rate-limited live lookup -- the live path (backfill_bse_scrip_codes) is only worth
    it for the genuine gap: companies with no bse_ticker at all."""
    df = sql_to_df(
        """
        SELECT company_master_id, bse_ticker AS bse_scrip_code
        FROM company_master
        WHERE bse_ticker IS NOT NULL AND bse_ticker != ''
          AND bse_ticker ~ '^[0-9]+$'
          AND (bse_scrip_code IS NULL OR bse_scrip_code = '')
        """
    )
    if df.empty:
        return 0
    upsert_to_db(df, "company_master", unique_keys=["company_master_id"])
    return len(df)


def load_bse_scrip_code_targets(limit: int | None = None) -> pd.DataFrame:
    """One row per company (its single most recent ISIN, via dim_security) that
    doesn't have a bse_scrip_code in company_master yet -- a company with multiple
    historical ISINs (renames) must not generate one lookup per ISIN, that would
    waste BSE requests on superseded identities with no value. Handles the bootstrap
    case where the column doesn't exist at all yet (upsert_to_db adds it dynamically
    on first write, same pattern master_dhan_instruments already uses).

    Same non-deterministic-tie gap as build_identity_break_events' own DISTINCT ON
    query (re-audit 2026-08-18, see its docstring) -- last_trade_date/effective_to
    alone can tie (e.g. AARTISURF's EQ and P1 rows), so this sibling query needs the
    identical "series = 'EQ'", then isin, final tiebreaker to pick deterministically."""
    has_column = not sql_to_df(
        "SELECT 1 FROM information_schema.columns "
        "WHERE table_name = 'company_master' AND column_name = 'bse_scrip_code'"
    ).empty
    filter_clause = "AND (cm.bse_scrip_code IS NULL OR cm.bse_scrip_code = '')" if has_column else ""
    limit_clause = f"LIMIT {int(limit)}" if limit else ""
    return sql_to_df(
        f"""
        SELECT DISTINCT ON (ds.company_master_id) ds.company_master_id, ds.isin
        FROM dim_security ds
        LEFT JOIN company_master cm ON cm.company_master_id = ds.company_master_id
        WHERE ds.isin IS NOT NULL AND ds.company_master_id IS NOT NULL
        {filter_clause}
        ORDER BY ds.company_master_id, ds.last_trade_date DESC NULLS LAST, ds.effective_to DESC NULLS LAST, (ds.series = 'EQ') DESC, ds.isin
        {limit_clause}
        """
    )


def backfill_bse_scrip_codes(limit: int | None = None) -> dict[str, object]:
    """Fast free copy first (copy_bse_scrip_code_from_ticker, zero BSE requests), then
    live BSE lookups only for the genuine gap (companies with no bse_ticker at all),
    bounded by `limit` per run (rate-gated at BSE_MIN_REQUEST_INTERVAL_SECONDS/request
    -- a full live backfill is a multi-hour job by design, meant to run incrementally
    over several cron cycles, not synchronously in one sitting)."""
    copied = copy_bse_scrip_code_from_ticker()

    targets = load_bse_scrip_code_targets(limit=limit)
    found_rows: list[dict[str, object]] = []
    not_found = 0
    for row in targets.itertuples():
        result = lookup_bse_scrip_code(row.isin)
        if result is None or not result.get("scrip_code"):
            not_found += 1
            continue
        found_rows.append({"company_master_id": row.company_master_id, "bse_scrip_code": result["scrip_code"]})

    if found_rows:
        upsert_to_db(pd.DataFrame(found_rows), "company_master", unique_keys=["company_master_id"])

    return {
        "copied_from_existing_ticker": copied,
        "live_lookup_targets_considered": len(targets),
        "live_lookup_found": len(found_rows),
        "live_lookup_not_found": not_found,
    }


def run_equity_l_reconciliation() -> dict[str, object]:
    equity_l = fetch_equity_l()
    new_symbols, identity_breaks = classify_gaps(equity_l)

    new_rows = build_new_history_rows(new_symbols)
    review_events = build_identity_break_events(identity_breaks)

    if not new_rows.empty:
        upsert_to_db(
            new_rows,
            "dim_security_history",
            unique_keys=["security_id", "symbol", "series", "isin", "effective_from"],
        )
    if not review_events.empty:
        upsert_to_db(review_events, "dim_security_review_events", unique_keys=["event_key"])
    if not new_rows.empty:
        # propagate into dim_security the same way the pure-TA pipeline already does --
        # unmodified, reused as-is.
        security_dimension.run()

    return {
        "rows": len(new_rows),
        "rows_written": len(new_rows),
        "equity_l_rows": len(equity_l),
        "new_symbols": len(new_symbols),
        "identity_break_candidates": len(identity_breaks),
        "review_events_written": len(review_events),
        "company_master_id_missing": int(new_rows["company_master_id"].isna().sum()) if not new_rows.empty else 0,
        "state_advanced": len(new_rows) > 0 or len(review_events) > 0,
    }


def main() -> int:
    import argparse

    global STOCKEY_RUN_STATE
    parser = argparse.ArgumentParser(description="Security master: EQUITY_L.csv reconciliation + BSE scrip code backfill.")
    parser.add_argument(
        "--only",
        choices=["equity-l", "bse-scrip-codes"],
        default=None,
        help="Run only one step. Default: both, EQUITY_L.csv first.",
    )
    parser.add_argument(
        "--bse-limit",
        type=int,
        default=200,
        help=(
            "Max BSE scrip-code lookups this run (rate-gated at "
            "BSE_MIN_REQUEST_INTERVAL_SECONDS/lookup -- a full backfill is a multi-hour "
            "job by design; this bounds one run so it fits a daily cron slot and picks "
            "up where it left off next run). 0 = unbounded."
        ),
    )
    args = parser.parse_args()

    ensure_identity_tables()
    run_equity_l = args.only in (None, "equity-l")
    run_bse = args.only in (None, "bse-scrip-codes")

    equity_l_state: dict[str, object] = {}
    bse_state: dict[str, object] = {}
    if run_equity_l:
        equity_l_state = run_equity_l_reconciliation()
    if run_bse:
        bse_state = backfill_bse_scrip_codes(limit=args.bse_limit or None)

    STOCKEY_RUN_STATE = {
        "source": SYNC_SOURCE_NAME,
        "rows": equity_l_state.get("rows", 0),
        "rows_written": equity_l_state.get("rows_written", 0),
        "fallback_used": False,
        "state_advanced": bool(equity_l_state.get("state_advanced")) or bse_state.get("found", 0) > 0,
        **({f"equity_l_{k}": v for k, v in equity_l_state.items()} if equity_l_state else {}),
        **({f"bse_scrip_code_{k}": v for k, v in bse_state.items()} if bse_state else {}),
    }
    status = "ok"
    print(json.dumps({"status": status, **STOCKEY_RUN_STATE}, ensure_ascii=False, default=str), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
