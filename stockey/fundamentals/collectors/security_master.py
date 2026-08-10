"""EQUITY_L.csv ingestion -- fundamental screener step 0
(docs/FUNDAMENTAL_SCREENER_PRD.md sec 5, sec 8 step 0).

stockey's existing identity layer (dim_security / dim_security_history) is derived
ONLY from observed nseindia_ohlcv price rows (data.nseindia.security_history), so it
misses any currently-listed name that hasn't shown up in a parsed bhavcopy day yet --
recent listings, thinly-traded microcaps, or names whose ISIN changed without a
follow-up identity-break resolution. EQUITY_L.csv (the full NSE-listed universe) is a
second, independent observation source that fills that gap.

This module is additive to the existing identity pipeline, not a fork of it: it
reuses data.nseindia.security_history's exact security_id-minting and
identity-break-detection helpers (default_security_id, hash_key) rather than
duplicating that logic, then calls data.nseindia.security_dimension.run() (unmodified)
to propagate the result into dim_security the same way the pure-TA pipeline already
does. No pure-TA file is modified by this module.

Two outcomes per missing ISIN, handled differently:
  - symbol never seen before -> new dim_security_history row (mapping_source=
    "equity_master"), no ambiguity, inserted directly.
  - symbol already known under a DIFFERENT isin -> NOT inserted blind; written to
    dim_security_review_events as event_type="identity_break_candidate" via the same
    schema data.nseindia.security_history.build_review_events already uses, for the
    existing manual-review workflow (docs/identity.md) to resolve.
"""

from __future__ import annotations

import io
import json

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
    the one existing manual-review queue rather than a second one."""
    if identity_breaks.empty:
        return pd.DataFrame()

    current = sql_to_df("SELECT isin, symbol, series, security_id FROM dim_security WHERE isin IS NOT NULL")
    current_by_symbol = current.drop_duplicates(subset=["symbol"], keep="last").set_index("symbol")

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


def main() -> int:
    global STOCKEY_RUN_STATE
    ensure_identity_tables()

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

    STOCKEY_RUN_STATE = {
        "source": SYNC_SOURCE_NAME,
        "rows": len(new_rows),
        "rows_written": len(new_rows),
        "equity_l_rows": len(equity_l),
        "new_symbols": len(new_symbols),
        "identity_break_candidates": len(identity_breaks),
        "review_events_written": len(review_events),
        "company_master_id_missing": int(new_rows["company_master_id"].isna().sum()) if not new_rows.empty else 0,
        "fallback_used": False,
        "state_advanced": len(new_rows) > 0 or len(review_events) > 0,
    }
    status = "ok"
    print(json.dumps({"status": status, **STOCKEY_RUN_STATE}, ensure_ascii=False, default=str), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
