"""Hypothesis screeners: each active hypothesis materializes its universe as a screener.

A hypothesis is a strategy instance whose first act is a universe: the names its thesis is
about. Today those names only surface indirectly through matched events; this module makes
them a first-class constituents source (`screener_slug=hypothesis-<id>`), so hypothesis
symbols enter the watch/candidate funnel like any other screen and every downstream gate,
the OHLCV reconcile universe, and the regret ledger's source attribution apply unchanged.

Universe per hypothesis: the operator-editable `target_universe_json.symbols` when present,
else the derived default -- distinct symbols from the hypothesis's event matches (excluding
the NIFTY placeholder). Lifecycle for free: paused/retired hypotheses stop emitting, so
their symbols fall out of the next watch snapshot.

Deliberately queries tables directly (no hypothesis_engine import) to keep the screener
build path free of heavy/LLM module imports.
"""
from __future__ import annotations

import argparse
import json
import os
from typing import Any

import pandas as pd

from advisory.fallback_telemetry import record_local_fallback_event
from utils.db import sql_to_df

HYPOTHESIS_SCREENERS_ENABLED = os.getenv("HYPOTHESIS_SCREENERS_ENABLED", "true").strip().lower() not in {"0", "false", "no"}
MAX_SYMBOLS_PER_HYPOTHESIS = int(os.getenv("HYPOTHESIS_SCREENER_MAX_SYMBOLS", "50"))
SLUG_PREFIX = "hypothesis-"

HYPOTHESES_TABLE = "advisory_hypotheses"
MATCHES_TABLE = "advisory_hypothesis_matches"
OHLCV_TABLE = "nseindia_ohlcv"
# mirrors hypothesis_engine.ACTIVE_SCAN_STATUSES (kept local to avoid importing the LLM-heavy module)
ACTIVE_STATUSES = ("testing", "validated", "active_review", "trusted_overlay", "production")


def _universe_symbols(hypothesis_id: str, target_universe_json: Any) -> list[str]:
    symbols: list[str] = []
    try:
        parsed = json.loads(target_universe_json) if target_universe_json else {}
        if isinstance(parsed, dict):
            symbols = [str(s).strip().upper() for s in (parsed.get("symbols") or []) if str(s or "").strip()]
    except Exception:
        symbols = []
    if symbols:
        return symbols[: max(1, MAX_SYMBOLS_PER_HYPOTHESIS)]
    derived = sql_to_df(
        f"""
        SELECT symbol, COUNT(*) AS n
        FROM {MATCHES_TABLE}
        WHERE hypothesis_id = %s AND UPPER(TRIM(symbol)) <> 'NIFTY' AND COALESCE(symbol, '') <> ''
        GROUP BY symbol ORDER BY n DESC, symbol
        LIMIT %s
        """,
        params=(hypothesis_id, max(1, MAX_SYMBOLS_PER_HYPOTHESIS)),
    )
    return [str(s).strip().upper() for s in derived["symbol"].tolist()] if not derived.empty else []


def build_hypothesis_constituents(*, asof_date: Any | None = None) -> pd.DataFrame:
    """Constituents rows for every active hypothesis with a non-empty universe."""
    from advisory.market_action_scan import _latest_bhavcopy_date

    scan_date = _latest_bhavcopy_date(asof_date)
    if scan_date is None:
        return pd.DataFrame()
    hypotheses = sql_to_df(
        f"""
        SELECT hypothesis_id, title, status, target_universe_json
        FROM {HYPOTHESES_TABLE}
        WHERE LOWER(COALESCE(status, '')) = ANY(%s)
        ORDER BY hypothesis_id
        """,
        params=(list(ACTIVE_STATUSES),),
    )
    if hypotheses.empty:
        return pd.DataFrame()
    now = pd.Timestamp.utcnow()
    rows: list[dict[str, Any]] = []
    all_symbols: set[str] = set()
    per_hypothesis: list[tuple[str, str, list[str]]] = []
    for hypothesis in hypotheses.itertuples(index=False):
        symbols = _universe_symbols(str(hypothesis.hypothesis_id), hypothesis.target_universe_json)
        if not symbols:
            continue
        per_hypothesis.append((str(hypothesis.hypothesis_id), str(hypothesis.title or ""), symbols))
        all_symbols.update(symbols)
    if not per_hypothesis:
        return pd.DataFrame()
    identity = sql_to_df(
        f"""
        SELECT DISTINCT ON (symbol) UPPER(TRIM(symbol)) AS symbol, company_master_id, isin
        FROM {OHLCV_TABLE}
        WHERE UPPER(TRIM(symbol)) = ANY(%s)
        ORDER BY symbol, date DESC
        """,
        params=(sorted(all_symbols),),
    )
    identity_map = {
        str(row.symbol): (row.company_master_id, row.isin) for row in identity.itertuples(index=False)
    } if not identity.empty else {}
    for hypothesis_id, title, symbols in per_hypothesis:
        slug = f"{SLUG_PREFIX}{hypothesis_id.strip().lower().replace('_', '-')}"
        for rank, symbol in enumerate(symbols, start=1):
            company_master_id, isin = identity_map.get(symbol, (None, None))
            rows.append(
                {
                    "date": scan_date,
                    "screener_slug": slug,
                    "screener_name": f"Hypothesis: {title}"[:120],
                    "screener_url": None,
                    "ticker": symbol,
                    "exchange": "NSE",
                    "company_master_id": company_master_id,
                    "security_id": None,
                    "instrument": None,
                    "isin": isin,
                    "display_name": symbol,
                    "rank": rank,
                    "raw_item_json": json.dumps(
                        {"source": "hypothesis_screener", "hypothesis_id": hypothesis_id, "title": title},
                        ensure_ascii=False,
                    ),
                    "load_ts": now,
                }
            )
    return pd.DataFrame(rows)


def safe_build_hypothesis_constituents(*, asof_date: Any | None = None) -> pd.DataFrame:
    """Wrapper for the constituents build path: any failure degrades to an empty frame with
    telemetry so screener parsing is never blocked by hypothesis screeners."""
    try:
        return build_hypothesis_constituents(asof_date=asof_date)
    except Exception as exc:
        record_local_fallback_event(
            module="advisory.hypothesis_screeners",
            source=HYPOTHESES_TABLE,
            fallback_type="hypothesis_screeners_failed",
            severity="warn",
            reason="Hypothesis screener materialization failed; constituents continue without hypothesis sources.",
            error=exc,
            metadata={"asof_date": None if asof_date is None else str(asof_date)},
        )
        return pd.DataFrame()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Materialize hypothesis universes as screener constituents.")
    parser.add_argument("--date", default=None)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--format", choices=["json", "text"], default="text")
    args = parser.parse_args(argv)
    frame = build_hypothesis_constituents(asof_date=args.date)
    if not args.dry_run and not frame.empty:
        from advisory.screener_parser import persist_constituents

        persist_constituents(frame)
    if args.format == "json":
        print(json.dumps({"rows": len(frame), "dry_run": bool(args.dry_run)}, default=str))
    else:
        print(f"[hypothesis_screeners] rows={len(frame)} dry_run={args.dry_run}")
        if not frame.empty:
            for slug, group in frame.groupby("screener_slug"):
                print(f"  {slug}: {len(group)} symbols")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
