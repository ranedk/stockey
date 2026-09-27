"""Point-in-time reads: what did stockey know about a company on date D (TODO C4, 2026-09-27).

Every fundamental input is stored under the date it was fetched and never overwritten, so
"known on D" is the newest row on or before D in each dated table, and for filings, the
ones DETECTED by the end of D (fundamentals_events.detected_at -- not disclosure_date,
which is when the company filed, and not load_ts, which later merges rewrite).

    known_as_of("nse:TCS", "2026-10-15")   -> one company, every source
    snapshot_panel("2026-10-15")           -> every company's fundamentals as of D (for
                                              quality / value rules, C6 / C8)

History starts when each table did: the market-wide snapshot on 2026-09-27, Layer 2
inputs on 2026-09-25, L2 state and the pledge/valuation snapshot in 2026-08. Before a
table's first date the section is None -- never a later value passed off as earlier.
Industry labels from Sharpely carry no history, so only the BSE labels are point-in-time.
"""

from __future__ import annotations

import json

import pandas as pd

from utils.db import sql_to_df


def _end_of_day_ist(as_of) -> pd.Timestamp:
    return pd.Timestamp(str(pd.Timestamp(as_of).date()), tz="Asia/Kolkata") + pd.Timedelta(days=1)


def _one(sql: str, params: tuple) -> dict | None:
    df = sql_to_df(sql, params=params)
    if df.empty:
        return None
    return json.loads(df.iloc[[0]].to_json(orient="records", date_format="iso"))[0]


def known_as_of(company_master_id: str, as_of) -> dict[str, object]:
    d = str(pd.Timestamp(as_of).date())
    snapshot = _one(
        "SELECT * FROM fundamentals_snapshot_daily WHERE company_master_id = %s AND as_of_date <= %s "
        "ORDER BY as_of_date DESC LIMIT 1", (company_master_id, d))
    screener_id = snapshot.get("screener_company_id") if snapshot else None
    if screener_id is None:
        ids = sql_to_df("SELECT screener_company_id FROM fundamentals_snapshot_daily WHERE company_master_id = %s LIMIT 1",
                        params=(company_master_id,))
        screener_id = int(ids.iloc[0, 0]) if not ids.empty else None
    symbol = company_master_id.split(":", 1)[1] if company_master_id.startswith("nse:") else None
    layer2 = _one(
        "SELECT * FROM fundamentals_universe_layer2_inputs WHERE company_master_id = %s AND run_date <= %s "
        "ORDER BY run_date DESC LIMIT 1", (company_master_id, d))
    isin = (layer2 or {}).get("isin")
    events = sql_to_df(
        "SELECT source, news_id, filing_type, disclosure_date, detected_at, rule_trigger_status, headline "
        "FROM fundamentals_events WHERE company_master_id = %s AND detected_at < %s ORDER BY detected_at",
        params=(company_master_id, _end_of_day_ist(as_of).to_pydatetime()))
    return {
        "company_master_id": company_master_id,
        "as_of": d,
        "snapshot": snapshot,
        "l2_state": _one(
            "SELECT * FROM fundamentals_l2_state WHERE company_id = %s AND run_date::date <= %s "
            "ORDER BY run_date DESC, state_vector_version DESC LIMIT 1", (screener_id, d)) if screener_id else None,
        "pledge_valuation": _one(
            "SELECT * FROM fundamentals_l2_market_snapshot WHERE company_id = %s AND run_date::date <= %s "
            "ORDER BY run_date DESC LIMIT 1", (screener_id, d)) if screener_id else None,
        "layer2": layer2,
        "in_universe": _in_universe(screener_id, d) if screener_id else None,
        "industry": _one(
            "SELECT * FROM fundamentals_industry_classification WHERE isin = %s AND fetch_date <= %s "
            "ORDER BY fetch_date DESC LIMIT 1", (isin, d)) if isin else None,
        "surveillance": _one(
            "SELECT date, gsm, lt_asm, st_asm, esm, irp, encumbered_over_50 FROM nseindia_surveillance_indicator "
            "WHERE symbol = %s AND series = 'EQ' AND date <= %s AND date >= %s::date - 15 "
            "ORDER BY date DESC LIMIT 1", (symbol, d, d)) if symbol else None,
        "market_cap": _one(
            "SELECT date, market_cap_rs FROM nseindia_mcap WHERE symbol = %s AND series = 'EQ' "
            "AND date <= (%s::date + 1) AND date >= %s::date - 15 ORDER BY date DESC LIMIT 1",
            (symbol, d, d)) if symbol else None,
        "filings_detected": json.loads(events.to_json(orient="records", date_format="iso")) if not events.empty else [],
    }


def _in_universe(screener_id: int, d: str) -> bool | None:
    """Whether the company was in the universe on the latest run on or before D."""
    df = sql_to_df(
        """
        WITH run AS (SELECT max(run_date) AS r FROM fundamentals_l1_universe WHERE run_date::date <= %s)
        SELECT (SELECT r FROM run) AS run_date,
               EXISTS (SELECT 1 FROM fundamentals_l1_universe u, run
                        WHERE u.run_date = run.r AND u.company_id = %s) AS member
        """,
        params=(d, screener_id),
    )
    if df.empty or pd.isna(df.iloc[0]["run_date"]):
        return None
    return bool(df.iloc[0]["member"])


def snapshot_panel(as_of) -> pd.DataFrame:
    """Every company's newest fundamentals snapshot on or before D -- the input a
    quality or value rule ranks. Rows carry as_of_date so staleness is visible."""
    return sql_to_df(
        """
        SELECT DISTINCT ON (screener_company_id) *
          FROM fundamentals_snapshot_daily
         WHERE as_of_date <= %s AND as_of_date >= %s::date - 10
         ORDER BY screener_company_id, as_of_date DESC
        """,
        params=(str(pd.Timestamp(as_of).date()), str(pd.Timestamp(as_of).date())),
    )
