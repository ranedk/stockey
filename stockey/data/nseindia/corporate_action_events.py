"""Derive events_dividend + events_capital_change from the NSE bhavcopy corporate-actions feed.

Both tables used to come from a third-party (mintbox/sharpely) API that was removed in Aug 2025. We now
own the source: nseindia_corporate_actions_bc_raw is NSE's official Bc feed (complete 2013->present,
parsed + validated by the price-adjustment work), so these are pure, point-in-time derivations with no
external dependency and no browser.

  * events_capital_change -- rows carrying a bonus/split/FV-change (reuses the same PURPOSE->ratio parser
    price_adjustment uses), with the event type and the price factor (Y/(X+Y) for a bonus X:Y, B/A for an
    FV split A->B).
  * events_dividend -- rows carrying a dividend, with the per-share amount parsed from the PURPOSE text
    ("DIV - RS 2 PER SH" -> 2.0; "INTERIM DIVIDEND" -> amount NULL) and the type (interim/final/special).

CLI: `python -m data.nseindia.corporate_action_events`.
"""
from __future__ import annotations

import argparse
import json
import re
from typing import Any

import pandas as pd

from advisory.price_adjustment import _events_from_subject, _factor_for_events
from utils.db import sql_to_df, upsert_to_db

SOURCE = "nse_bhavcopy_ca"
DIVIDEND_TABLE = "events_dividend"
CAPITAL_CHANGE_TABLE = "events_capital_change"

# a subdivision IS a split (handled as a capital change), not a dividend, even though it contains "DIV".
_SUBDIV_RE = re.compile(r"SUB[\s-]*DIV")
_DIV_AMOUNT_RE = re.compile(r"(?:RS|RE)\.?\s*([0-9]+(?:\.[0-9]+)?)")


def parse_dividend(subject: str) -> dict[str, Any] | None:
    """Parse a dividend event from a Bc PURPOSE string, or None if it isn't a dividend.

    amount is the per-share rupee value ("DIV - RS 2 PER SH" -> 2.0), or None when NSE states only
    "DIVIDEND"/"INTERIM DIVIDEND" with no figure. type is interim / special / final.
    """
    up = str(subject).upper()
    if _SUBDIV_RE.search(up):          # subdivision = split -> not a dividend
        return None
    if "DIV" not in up:
        return None
    m = _DIV_AMOUNT_RE.search(up)
    amount = float(m.group(1)) if m else None
    if "INT" in up:                    # INTERIM DIVIDEND / INTDIV
        dtype = "interim"
    elif "SPECIAL" in up:
        dtype = "special"
    else:
        dtype = "final"
    return {"dividend_amount": amount, "dividend_type": dtype}


def classify_capital_change(subject: str) -> dict[str, Any] | None:
    """Parse a bonus/split/FV-change event set from a PURPOSE string, or None. Returns the canonical
    event type, the combined price factor, and the parsed events for provenance."""
    events = _events_from_subject(subject)
    if not events:
        return None
    kinds = sorted({kind for kind, _a, _b in events})
    return {
        "event_type": "+".join(kinds),                 # 'bonus', 'split', or 'bonus+split'
        "price_factor": _factor_for_events(events),
        "events": json.dumps(sorted(events)),
    }


def build_events(*, dry_run: bool = False) -> dict[str, Any]:
    """Populate events_dividend + events_capital_change from the whole CA feed. Returns a summary."""
    raw = sql_to_df(
        "SELECT symbol, date, record_date, series, subject "
        "FROM nseindia_corporate_actions_bc_raw WHERE subject IS NOT NULL AND symbol IS NOT NULL"
    )
    if raw.empty:
        return {"dividend_rows": 0, "capital_change_rows": 0}
    raw["date"] = pd.to_datetime(raw["date"], utc=True, errors="coerce")
    raw["record_date"] = pd.to_datetime(raw["record_date"], utc=True, errors="coerce")

    div_records: list[dict[str, Any]] = []
    cap_records: list[dict[str, Any]] = []
    for row in raw.itertuples(index=False):
        if pd.isna(row.date):
            continue
        base = {"symbol": row.symbol, "ex_date": row.date, "record_date": row.record_date,
                "series": row.series, "subject": row.subject, "source": SOURCE}
        cap = classify_capital_change(row.subject)
        if cap is not None:
            cap_records.append({**base, **cap})
        div = parse_dividend(row.subject)
        if div is not None:
            div_records.append({**base, **div})

    div_df = pd.DataFrame(div_records).drop_duplicates(subset=["symbol", "ex_date", "subject"]) if div_records else pd.DataFrame()
    cap_df = pd.DataFrame(cap_records).drop_duplicates(subset=["symbol", "ex_date", "subject"]) if cap_records else pd.DataFrame()

    if not dry_run:
        if not div_df.empty:
            upsert_to_db(div_df, DIVIDEND_TABLE, unique_keys=["symbol", "ex_date", "subject"], timescaledb_column="ex_date")
        if not cap_df.empty:
            upsert_to_db(cap_df, CAPITAL_CHANGE_TABLE, unique_keys=["symbol", "ex_date", "subject"], timescaledb_column="ex_date")

    return {
        "dividend_rows": int(len(div_df)),
        "capital_change_rows": int(len(cap_df)),
        "dividend_with_amount": int(div_df["dividend_amount"].notna().sum()) if not div_df.empty else 0,
        "capital_change_types": (cap_df["event_type"].value_counts().to_dict() if not cap_df.empty else {}),
        "symbols": int(pd.concat([div_df.get("symbol", pd.Series(dtype=str)), cap_df.get("symbol", pd.Series(dtype=str))]).nunique()),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="Derive events_dividend + events_capital_change from the NSE Bc CA feed.")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    print(json.dumps(build_events(dry_run=bool(args.dry_run)), indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
