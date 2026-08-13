"""Structured per-stock signal pointers -- fundamental screener step 15 (2026-08-13,
user request: "instead of tags, ... put the pointers here in a more structured way
(such that its easy to possibly query them)"). Deliberately NOT free-text tags: each
pointer is a typed row {signal_type, label, value, direction, as_of_date, source} so
the frontend/API can filter/group by signal_type instead of parsing sentences.

NOT a new collector or sync job -- every underlying fact already has its own refresh
step (l2_state, rating_agencies, investor_classification, sector_cycle, l3_triggers).
This module is a read-time aggregator only, same "small per-company SELECT, no new
table" shape watch_summary.py's own load_* functions already use. Built once,
consumed in two places: watch_summary.py's evidence bundle (so the narrative LLM sees
the actual FII/DII trend and investor tier, not just that an alert fired -- see this
module's own docstring cross-reference in watch_summary.py) and the screener/
frontend's per-stock signal panel (fundamentals/api/queries.py's get_watchlist_detail).

Rating-agency identity and investor-tier resolution intentionally do NOT import from
fundamentals/screens/l3_triggers.py (which needs the same two facts for its own alert
reasoning) to avoid a cross-module dependency in either direction -- both sides
resolve the same underlying columns independently. This is the one accepted
duplication in this module (a handful of lines each); see l3_triggers.py's own
_resolve_rating_action_type and this module's _resolve_rating_action for the twin
copies, both keyed off the same fundamentals_events columns."""

from __future__ import annotations

import json

import pandas as pd

from fundamentals.screens.investor_classification import effective_tier, normalize_investor_key
from fundamentals.screens.l1_universe import RPT_PCT_OF_REVENUE_THRESHOLD
from fundamentals.screens.sector_cycle import classify_growth
from utils.db import sql_to_df

SYNC_SOURCE_NAME = "fundamentals.screens.signal_pointers"


def _resolve_rating_action(event: dict) -> str | None:
    """Twin of l3_triggers.py's _resolve_rating_action_type -- see module docstring
    for why this isn't a shared import."""
    action = event.get("rating_action_type")
    if action:
        return str(action)
    raw_json = event.get("structured_extraction_json")
    if not raw_json:
        return None
    try:
        extracted = json.loads(raw_json)
    except (TypeError, ValueError):
        return None
    return extracted.get("rating_action")


def load_l2_signals_for_company(company_master_id: str) -> dict | None:
    ticker = str(company_master_id or "").removeprefix("nse:")
    df = sql_to_df(
        """
        SELECT promoter_pct, promoter_stake_direction, institutional_pct,
               institutional_stake_direction, institutional_first_entry, run_date
        FROM fundamentals_l2_state
        WHERE ticker = %s
        ORDER BY run_date DESC
        LIMIT 1
        """,
        params=(ticker,),
    )
    return df.iloc[0].to_dict() if not df.empty else None


def load_latest_rating_event_for_company(company_master_id: str) -> dict | None:
    df = sql_to_df(
        """
        SELECT rating_agency, rating_action_type, structured_extraction_json, disclosure_date, load_ts
        FROM fundamentals_events
        WHERE company_master_id = %s AND filing_type = 'rating_action'
        ORDER BY load_ts DESC
        LIMIT 1
        """,
        params=(company_master_id,),
    )
    return df.iloc[0].to_dict() if not df.empty else None


def load_latest_insider_transaction_for_company(company_master_id: str) -> dict | None:
    """Latest pit_sast row with an actual transaction (transaction_type populated --
    most BSE-detected pit_sast rows are trading-window-closure procedural notices
    with no transaction at all, same distinction evaluate_pit_sast_trigger already
    makes). 2026-08-13: closes a gap found auditing the signal-pointer board --
    rating_action and investor_entry each got a dedicated, structured pointer type,
    but insider buys/sells only ever showed up as a generic strategy_satisfied badge
    with no name/quantity/direction, even though fundamentals_events already has all
    three columns."""
    df = sql_to_df(
        """
        SELECT insider_name, quantity, transaction_type, disclosure_date, load_ts
        FROM fundamentals_events
        WHERE company_master_id = %s AND filing_type = 'pit_sast'
          AND transaction_type IS NOT NULL AND transaction_type != ''
        ORDER BY load_ts DESC
        LIMIT 1
        """,
        params=(company_master_id,),
    )
    return df.iloc[0].to_dict() if not df.empty else None


def load_latest_confirmed_auditor_change_for_company(company_master_id: str) -> dict | None:
    """Latest auditor_change event whose extraction resolved disclosure_type ==
    'confirmed_change' -- same gate evaluate_auditor_change_trigger and fundamentals/
    screens/l1_universe.py's own _auditor_change_excludes both use, so this pointer
    only ever shows a REAL change, never an incidental audit-opinion mention or a
    not-yet-approved EGM proposal. 2026-08-13: closes the gap found auditing the
    signal-pointer board -- the whole auditor-change detection+extraction pipeline
    existed for L1's exclusion gate only, the actual who/what/when was captured and
    then never shown to a human anywhere."""
    df = sql_to_df(
        """
        SELECT structured_extraction_json, disclosure_date
        FROM fundamentals_events
        WHERE company_master_id = %s AND filing_type = 'auditor_change'
          AND structured_extraction_status = 'done' AND structured_extraction_json IS NOT NULL
        ORDER BY load_ts DESC
        """,
        params=(company_master_id,),
    )
    if df.empty:
        return None
    for _, row in df.iterrows():
        try:
            extracted = json.loads(row["structured_extraction_json"])
        except (TypeError, ValueError):
            continue
        if extracted.get("disclosure_type") == "confirmed_change":
            return {"disclosure_date": row["disclosure_date"], **extracted}
    return None


def load_latest_material_rpt_for_company(company_master_id: str) -> dict | None:
    """Latest related_party_transaction event whose extraction resolved is_applicable
    and pct_of_revenue over RPT_PCT_OF_REVENUE_THRESHOLD -- same materiality gate
    evaluate_related_party_transaction_trigger and l1_universe.py's own _rpt_excludes
    use. A non-applicability declaration (the common real case) never produces a
    pointer here, same as it never excludes at L1."""
    df = sql_to_df(
        """
        SELECT structured_extraction_json, disclosure_date
        FROM fundamentals_events
        WHERE company_master_id = %s AND filing_type = 'related_party_transaction'
          AND structured_extraction_status = 'done' AND structured_extraction_json IS NOT NULL
        ORDER BY load_ts DESC
        """,
        params=(company_master_id,),
    )
    if df.empty:
        return None
    for _, row in df.iterrows():
        try:
            extracted = json.loads(row["structured_extraction_json"])
        except (TypeError, ValueError):
            continue
        pct = extracted.get("pct_of_revenue")
        if extracted.get("is_applicable") and isinstance(pct, (int, float)) and pct >= RPT_PCT_OF_REVENUE_THRESHOLD:
            return {"disclosure_date": row["disclosure_date"], **extracted}
    return None


def load_capital_raise_investor_signals_for_company(company_master_id: str) -> list[dict]:
    """Every distinct named investor across this company's capital-raise filings,
    joined to their classification tier where known -- unclassified names (not yet
    picked up by investor_classification.py's own queue-and-classify pass) are
    surfaced too, with tier=None, rather than silently dropped."""
    events_df = sql_to_df(
        """
        SELECT structured_extraction_json, disclosure_date
        FROM fundamentals_events
        WHERE company_master_id = %s AND filing_type = 'capital_raise'
          AND structured_extraction_json IS NOT NULL
        ORDER BY load_ts ASC NULLS LAST
        """,
        params=(company_master_id,),
    )
    if events_df.empty:
        return []

    tiers_df = sql_to_df("SELECT investor_key, llm_tier, override_tier FROM fundamentals_investor_classification")
    tiers_by_key = {row["investor_key"]: row for row in tiers_df.to_dict("records")} if not tiers_df.empty else {}

    seen: dict[str, dict] = {}
    for _, row in events_df.iterrows():
        try:
            extracted = json.loads(row["structured_extraction_json"])
        except (TypeError, ValueError):
            continue
        for raw_name in extracted.get("investor_names") or []:
            key = normalize_investor_key(raw_name)
            if not key or key in seen:
                continue
            tier_row = tiers_by_key.get(key)
            seen[key] = {
                "investor_name": str(raw_name).strip(),
                "tier": effective_tier(tier_row) if tier_row else None,
                "as_of_date": row.get("disclosure_date"),
            }
    return list(seen.values())


def load_sector_growth_for_company(company_master_id: str) -> dict | None:
    df = sql_to_df(
        """
        SELECT ds.sector_code, sr.description AS sector_name, sc.phase,
               sc.demand_growth_pct, sc.sample_size_confidence, sc.run_date
        FROM dim_security ds
        LEFT JOIN LATERAL (
            SELECT description FROM fundamentals_sector_reference
            WHERE code = ds.sector_code AND level = 'sector'
            ORDER BY as_of_date DESC LIMIT 1
        ) sr ON TRUE
        LEFT JOIN LATERAL (
            SELECT phase, demand_growth_pct, sample_size_confidence, run_date
            FROM fundamentals_sector_cycle
            WHERE sector_code = ds.sector_code
            ORDER BY run_date DESC LIMIT 1
        ) sc ON TRUE
        WHERE ds.company_master_id = %s AND ds.sector_code IS NOT NULL
        ORDER BY ds.last_trade_date DESC NULLS LAST, ds.effective_to DESC NULLS LAST
        LIMIT 1
        """,
        params=(company_master_id,),
    )
    return df.iloc[0].to_dict() if not df.empty else None


def load_satisfied_strategies_for_company(company_master_id: str) -> list[dict]:
    """Distinct trigger_type rows already alerted for this company -- the same
    trigger_type field the strategy-registry frontend groups companies by
    (fundamentals/api/queries.py's get_strategies/get_strategy_detail), returned here
    reversed (company -> its strategies) for the per-stock signal panel."""
    df = sql_to_df(
        """
        SELECT DISTINCT ON (trigger_type) trigger_type, alert_date, reasoning
        FROM fundamentals_l3_alerts
        WHERE company_master_id = %s
        ORDER BY trigger_type, alert_date DESC NULLS LAST
        """,
        params=(company_master_id,),
    )
    return df.to_dict("records") if not df.empty else []


def load_satisfied_strategies_by_company() -> dict[str, list[str]]:
    """Bulk (all-companies-at-once) sibling of load_satisfied_strategies_for_company
    above -- 2026-08-13, closing a gap found auditing the watchlist list page and
    daily digest email, both of which only ever showed a bare alert_count with no
    indication of WHICH strategies. One query + Python-side grouping (same "bulk
    query, dict lookup per row" pattern get_sectors()'s watched_companies grouping
    and l3_triggers.py's l2_by_ticker already use) instead of one query per company
    -- this is meant to be called once per page/digest render, not per row."""
    df = sql_to_df(
        """
        SELECT DISTINCT company_master_id, trigger_type
        FROM fundamentals_l3_alerts
        WHERE company_master_id IS NOT NULL
        """
    )
    if df.empty:
        return {}
    by_company: dict[str, list[str]] = {}
    for row in df.to_dict("records"):
        by_company.setdefault(row["company_master_id"], []).append(row["trigger_type"])
    return {cmid: sorted(triggers) for cmid, triggers in by_company.items()}


def get_stock_signal_pointers(company_master_id: str) -> list[dict]:
    """Assembles every structured pointer for one company. Each pointer:
    {signal_type, label, value, direction, as_of_date, source}. Omits a signal type
    entirely when the underlying data is missing/null rather than emitting a
    misleading "unknown" pointer -- absence of a pointer means "no data", not "no
    signal"."""
    pointers: list[dict] = []

    l2 = load_l2_signals_for_company(company_master_id)
    if l2:
        as_of = l2.get("run_date")
        if l2.get("promoter_pct") is not None and pd.notna(l2.get("promoter_pct")):
            pointers.append(
                {
                    "signal_type": "promoter_holding",
                    "label": "Promoter holding",
                    "value": l2["promoter_pct"],
                    "direction": l2.get("promoter_stake_direction"),
                    "as_of_date": as_of,
                    "source": "l2_state",
                }
            )
        if l2.get("institutional_pct") is not None and pd.notna(l2.get("institutional_pct")):
            pointers.append(
                {
                    "signal_type": "institutional_holding",
                    "label": "FII+DII holding",
                    "value": l2["institutional_pct"],
                    "direction": l2.get("institutional_stake_direction"),
                    "as_of_date": as_of,
                    "source": "l2_state",
                }
            )
        if l2.get("institutional_first_entry"):
            pointers.append(
                {
                    "signal_type": "institutional_first_entry",
                    # 2026-08-13: caveat the ~3yr screener.in lookback window here too
                    # -- l2_state.py's own docstring/headline and l3_triggers.py's
                    # reasoning both carry it now; this pointer's label was the one
                    # place still saying "first entry" with no qualifier.
                    "label": "First institutional (FII+DII) entry visible in ~3yr history",
                    "value": None,
                    "direction": "new",
                    "as_of_date": as_of,
                    "source": "l2_state",
                }
            )

    rating_event = load_latest_rating_event_for_company(company_master_id)
    if rating_event:
        action = _resolve_rating_action(rating_event)
        if action:
            pointers.append(
                {
                    "signal_type": "rating_action",
                    "label": f"Rating {action}" + (f" ({rating_event['rating_agency']})" if rating_event.get("rating_agency") else ""),
                    "value": action,
                    "direction": "up" if action == "upgraded" else "down" if action == "downgraded" else "flat",
                    "as_of_date": rating_event.get("disclosure_date"),
                    "source": rating_event.get("rating_agency") or "rating_agency",
                }
            )

    for investor in load_capital_raise_investor_signals_for_company(company_master_id):
        pointers.append(
            {
                "signal_type": "investor_entry",
                "label": f"Investor: {investor['investor_name']}" + (f" ({investor['tier']})" if investor.get("tier") else ""),
                "value": investor.get("tier"),
                "direction": None,
                "as_of_date": investor.get("as_of_date"),
                "source": "capital_raise",
            }
        )

    insider = load_latest_insider_transaction_for_company(company_master_id)
    if insider:
        transaction_type = (insider.get("transaction_type") or "").lower()
        direction = "buy" if "buy" in transaction_type else "sell" if "sell" in transaction_type else None
        if direction:
            name = insider.get("insider_name") or "unnamed insider"
            quantity = insider.get("quantity")
            quantity_note = f" ({int(quantity):,} shares)" if quantity is not None and pd.notna(quantity) else ""
            pointers.append(
                {
                    "signal_type": "insider_transaction",
                    "label": f"Insider {direction}: {name}{quantity_note}",
                    "value": direction,
                    "direction": direction,
                    "as_of_date": insider.get("disclosure_date"),
                    "source": "pit_sast",
                }
            )

    auditor_change = load_latest_confirmed_auditor_change_for_company(company_master_id)
    if auditor_change:
        direction = auditor_change.get("change_direction")
        previous_auditor = auditor_change.get("previous_auditor")
        new_auditor = auditor_change.get("new_auditor")
        detail = ""
        if previous_auditor and new_auditor:
            detail = f": {previous_auditor} -> {new_auditor}"
        elif new_auditor:
            detail = f": {new_auditor}"
        elif previous_auditor:
            detail = f": {previous_auditor} departing"
        pointers.append(
            {
                "signal_type": "auditor_change",
                "label": f"Auditor {direction or 'change'}{detail}",
                "value": direction,
                "direction": None,
                "as_of_date": auditor_change.get("disclosure_date"),
                "source": "auditor_change",
            }
        )

    rpt = load_latest_material_rpt_for_company(company_master_id)
    if rpt:
        pct = rpt.get("pct_of_revenue")
        related_party_name = rpt.get("related_party_name")
        pointers.append(
            {
                "signal_type": "related_party_transaction",
                "label": f"RPT {pct}% of revenue" + (f" ({related_party_name})" if related_party_name else ""),
                "value": pct,
                "direction": None,
                "as_of_date": rpt.get("disclosure_date"),
                "source": "related_party_transaction",
            }
        )

    sector = load_sector_growth_for_company(company_master_id)
    if sector and sector.get("demand_growth_pct") is not None and pd.notna(sector.get("demand_growth_pct")):
        growth = classify_growth(sector["demand_growth_pct"], sector.get("sample_size_confidence"))
        pointers.append(
            {
                "signal_type": "sector_growth",
                "label": f"Sector growth: {growth}" + (f" ({sector['sector_name']})" if sector.get("sector_name") else ""),
                "value": growth,
                "direction": None,
                "as_of_date": sector.get("run_date"),
                "source": "sector_cycle",
            }
        )

    for strategy in load_satisfied_strategies_for_company(company_master_id):
        pointers.append(
            {
                "signal_type": "strategy_satisfied",
                "label": f"Strategy: {strategy['trigger_type']}",
                "value": strategy["trigger_type"],
                "direction": None,
                "as_of_date": strategy.get("alert_date"),
                "source": "l3_alerts",
            }
        )

    return pointers
