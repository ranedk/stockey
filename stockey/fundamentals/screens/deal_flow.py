"""Bulk/block-deal detection -- fundamentals screener PRD §12 todo #3 (2026-08-29).

Turns raw NSE block/bulk-deal rows (data/nseindia/offmarket.py + offmarket_parser.py,
revived todo #1 -- nseindia_block_deals/nseindia_bulk_deals, both pure-TA tables) into
fundamentals_events rows the SAME way L2 state already does for institutional_entry/
pledge_increase (fundamentals/screens/l2_state.py): no exchange filing exists for a
secondary-market trade, so this module synthesizes the event itself and hands it to
the SAME l3_triggers evaluation machinery every BSE-filed event goes through, rather
than building a second, bespoke alert path.

Two new filing_types this produces, both ALWAYS alert-worthy -- same reasoning as
capital_raise ("company getting money through any means is an important signal"):
NSE's own disclosure THRESHOLD (a deal must already be large enough to qualify as
"bulk"/"block") is itself the size filter, so no further gating on investor tier is
applied at the event level. Investor tier is folded into the alert's REASONING when
known (exactly capital_raise's own pattern via _resolve_investor_tiers_for_event /
_summarize_investor_tiers in l3_triggers.py, widened to include these two types).

- bulk_deal_buy: a named client bought via a disclosed block/bulk deal.
- bulk_deal_sell: a named client sold via a disclosed block/bulk deal.

Deliberately does NOT attempt to detect "promoter selling" specifically: NSE's
block/bulk-deal disclosure gives only a free-text client_name, with no promoter/KMP/
institution category flag (unlike SEBI PIT/SAST filings, which DO carry that
category and already power insider_buy/insider_sell_surprise -- see l3_triggers.py).
Matching client_name against a company's actual promoter name would need promoter
NAME strings this pipeline doesn't store anywhere (only promoter_pct, a percentage);
building that heuristic on data never actually observed live would be guessing, not
detecting. Documented as an explicit gap (docs/DATA_INVENTORY.md), not silently
half-built.

short_selling is deliberately NOT covered here either: nseindia_short_selling has no
client_name at all (an anonymous market-wide aggregate) -- a different kind of
signal ("how much short interest") than "which named investor moved", needing its
own baseline/spike-threshold design. Out of scope for this todo.

Scoped to the CURRENT L1 universe only (same "if an event hits this name, does it
matter" philosophy L2 state uses) -- block/bulk deals are disclosed market-wide,
most of it irrelevant to a ~188-company universe.

Idempotent by construction: news_id is a deterministic hash of the deal's own
natural key, so re-scanning the same historical deal on a later run (the lookback
window deliberately overlaps run-to-run) just re-upserts an identical row, a no-op --
same convention as every other fundamentals table in this pipeline.
"""
from __future__ import annotations

import json

import pandas as pd

from fundamentals.collectors.events_store import _ensure_events_schema
from utils.company_master import build_l1_ticker_by_company_master_id
from utils.db import sql_to_df, upsert_to_db

EVENTS_TABLE = "fundamentals_events"
SYNC_SOURCE_NAME = "fundamentals.screens.deal_flow"
STOCKEY_RUN_STATE: dict[str, object] = {}

# Incremental catch-up window once a watermark exists (see load_last_processed_date)
# -- a normal run only needs to look back a few days.
DEAL_FLOW_LOOKBACK_DAYS = 10
# First-ever run (no watermark yet), or after a gap longer than this module has ever
# processed: matches offmarket.py's own NSE_OFFMARKET_DOWNLOAD_LOOKBACK_DAYS default,
# so a fresh revival's full backfill isn't permanently unreachable.
DEAL_FLOW_INITIAL_LOOKBACK_DAYS = 365
# Small safety margin subtracted from the watermark itself -- covers a late-arriving
# deal for a date this module already scanned (e.g. offmarket_parser.py processing a
# file out of order).
DEAL_FLOW_WATERMARK_OVERLAP_DAYS = 2


def load_l1_company_master_ids() -> set[str]:
    return set(build_l1_ticker_by_company_master_id().keys())


def load_last_processed_date():
    """Highest disclosure_date already turned into a source='deal_flow' event --
    the watermark this module scans forward from.

    BUG FOUND LIVE 2026-08-30 (adversarial review, HIGH): before this, every run
    filtered by a FIXED trailing DEAL_FLOW_LOOKBACK_DAYS window measured from
    CURRENT_DATE, regardless of what had actually been processed. That silently
    and PERMANENTLY drops any deal older than the window with no re-scan
    mechanism -- concretely, the very first run after reviving offmarket.py (a
    365-day backfill landing in nseindia_block_deals/bulk_deals in one go) would
    only ever turn the most recent ~10 days of that into alerts; the other ~355
    days would never be reachable again. The same gap recurs any time offmarket.py
    itself falls behind by more than the window (its own retry/circuit-breaker
    design explicitly anticipates multi-day NSE blocks). Using the max disclosure_
    date already recorded as the starting point means a stall of ANY length
    self-heals on the next run that finds new deals, instead of silently
    truncating to a fixed number of days no matter how far behind the real gap is.
    None (no watermark yet) is the deliberate first-run signal DEAL_FLOW_INITIAL_
    LOOKBACK_DAYS below reads for its own default."""
    df = sql_to_df("SELECT MAX(disclosure_date) AS max_date FROM fundamentals_events WHERE source = 'deal_flow'")
    if df.empty or pd.isna(df.iloc[0]["max_date"]):
        return None
    return df.iloc[0]["max_date"]


def load_recent_deals(company_master_ids: set[str], *, last_processed_date=None) -> pd.DataFrame:
    """Block + bulk deals (identical shape) for L1-universe companies only.
    last_processed_date=None (first-ever run, or nothing recorded yet) scans the
    wide DEAL_FLOW_INITIAL_LOOKBACK_DAYS window; otherwise scans from
    (last_processed_date - DEAL_FLOW_WATERMARK_OVERLAP_DAYS) forward -- however
    long that gap actually is, not clipped to a fixed window -- so a multi-week
    NSE stall self-heals in one run once new deals land, rather than being
    permanently unreachable. short_selling is deliberately excluded -- see module
    docstring. `::date` cast on the TIMESTAMPTZ `date` column, same defensive
    convention every other NSE-date comparison in this codebase uses (e.g.
    data/nseindia/indices_downloader.py, scripts/price_data_sanity.py) --
    comparing a timestamp directly against a DATE-typed boundary is sensitive to
    the DB session's timezone setting relative to IST wall-clock."""
    if not company_master_ids:
        return pd.DataFrame()
    # DEAL_FLOW_INITIAL_LOOKBACK_DAYS/DEAL_FLOW_WATERMARK_OVERLAP_DAYS are fixed
    # internal constants (not user input) embedded directly, same as
    # DEAL_FLOW_LOOKBACK_DAYS elsewhere -- only the genuine runtime value
    # (last_processed_date) is parameterized. (Embedding a value inside a quoted
    # INTERVAL '%s days' literal is a real psycopg2 footgun -- keep constants out
    # of the parameter list entirely instead.)
    if last_processed_date is None:
        start_date_sql = f"CURRENT_DATE - INTERVAL '{int(DEAL_FLOW_INITIAL_LOOKBACK_DAYS)} days'"
        extra_params: tuple = ()
    else:
        start_date_sql = f"%s::date - INTERVAL '{int(DEAL_FLOW_WATERMARK_OVERLAP_DAYS)} days'"
        extra_params = (last_processed_date,)
    ids = list(company_master_ids)
    branch_params = (ids, *extra_params)
    query = f"""
        SELECT date, symbol, client_name, buysell, quantity, price, company_master_id, 'block_deals' AS deal_source
        FROM nseindia_block_deals
        WHERE company_master_id = ANY(%s) AND date::date >= {start_date_sql}
        UNION ALL
        SELECT date, symbol, client_name, buysell, quantity, price, company_master_id, 'bulk_deals' AS deal_source
        FROM nseindia_bulk_deals
        WHERE company_master_id = ANY(%s) AND date::date >= {start_date_sql}
    """  # noqa: S608 -- start_date_sql is built from fixed internal constants, not user input; real values are parameterized
    return sql_to_df(query, params=(*branch_params, *branch_params))


def _build_deal_flow_event_row(deal: dict, *, load_ts: pd.Timestamp) -> dict[str, object] | None:
    buysell = str(deal.get("buysell") or "").strip().upper()
    if not buysell:
        return None
    filing_type = "bulk_deal_buy" if buysell.startswith("B") else "bulk_deal_sell"
    client_name = str(deal.get("client_name") or "").strip()
    date_value = deal.get("date")
    date_label = date_value.date() if hasattr(date_value, "date") else date_value
    action = "Bought" if filing_type == "bulk_deal_buy" else "Sold"
    deal_source_label = str(deal.get("deal_source") or "").replace("_", " ")
    return {
        "source": "deal_flow",
        "news_id": f"{deal.get('deal_source')}:{deal.get('symbol')}:{date_label}:{client_name}:{buysell}:{deal.get('quantity')}:{deal.get('price')}",
        "company_master_id": deal.get("company_master_id"),
        "isin": None,
        "filing_type": filing_type,
        "headline": f"{action} {deal.get('quantity')} shares via {deal_source_label} at {deal.get('price')} -- {client_name or 'unnamed client'}",
        "subcategory": deal.get("deal_source"),
        "disclosure_date": date_label,
        "announcement_timestamp": load_ts,
        "quantity": deal.get("quantity"),
        "insider_name": None,
        "transaction_type": buysell,
        "attachment_name": None,  # left NULL on purpose -- see load_candidate_events' generic fallback branch
        "detail_url": None,
        "detection_source": f"nseindia_{deal.get('deal_source')}",
        "enrichment_status": "not_applicable",  # no document exists for this filing_type, same as institutional_entry/pledge_increase
        "sources": "deal_flow",
        # investor_names populated so investor_classification.py's existing
        # load_unclassified_investor_names() discovers this name too, growing the
        # SAME tier database capital_raise investors already feed (its query is
        # widened to include these two filing_types -- see that module).
        "structured_extraction_json": json.dumps({"investor_names": [client_name]}, ensure_ascii=False, default=str) if client_name else None,
        "structured_extraction_status": "done" if client_name else None,
        "raw_json": json.dumps(
            {k: (v.isoformat() if hasattr(v, "isoformat") else v) for k, v in deal.items()}, ensure_ascii=False, default=str
        ),
        "load_ts": load_ts,
    }


def run_deal_flow_detection() -> dict[str, object]:
    company_master_ids = load_l1_company_master_ids()
    if not company_master_ids:
        return {"deals_seen": 0, "events_written": 0, "buy_events": 0, "sell_events": 0}

    last_processed_date = load_last_processed_date()
    deals = load_recent_deals(company_master_ids, last_processed_date=last_processed_date)
    if deals.empty:
        return {"deals_seen": 0, "events_written": 0, "buy_events": 0, "sell_events": 0}

    load_ts = pd.Timestamp.now(tz="UTC")
    events = [row for row in (_build_deal_flow_event_row(d, load_ts=load_ts) for d in deals.to_dict("records")) if row is not None]
    if not events:
        return {"deals_seen": int(len(deals)), "events_written": 0, "buy_events": 0, "sell_events": 0}

    _ensure_events_schema()
    upsert_to_db(pd.DataFrame(events), EVENTS_TABLE, unique_keys=["source", "news_id"])
    buy_events = sum(1 for e in events if e["filing_type"] == "bulk_deal_buy")
    return {
        "deals_seen": int(len(deals)),
        "events_written": len(events),
        "buy_events": buy_events,
        "sell_events": len(events) - buy_events,
    }


def main() -> int:
    global STOCKEY_RUN_STATE
    result = run_deal_flow_detection()
    STOCKEY_RUN_STATE = {
        "source": SYNC_SOURCE_NAME,
        "rows": result["events_written"],
        "rows_read": result["deals_seen"],
        "rows_written": result["events_written"],
        "buy_events": result["buy_events"],
        "sell_events": result["sell_events"],
        "state_advanced": result["events_written"] > 0,
        "status": "ok",
    }
    print(json.dumps(STOCKEY_RUN_STATE, ensure_ascii=False, default=str), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
