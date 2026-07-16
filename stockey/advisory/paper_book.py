"""Always-on paper position book -- assume every daily BUY is taken, then manage each to its EXIT.

The daily advisory only emits BUYs; if you paper-trade them and never tell the system, it can never tell
you when to get OUT. This closes the loop: it takes every day's RS picks as positions, marks them daily,
and emits HOLD / TRIM / EXIT with a reason (stop hit, 20-day time cap, momentum fade, or data-gap/delist).
It also, deliberately, surfaces the messy edges of an always-trading model -- a name that stops printing a
price, a corporate action mid-hold -- as flags, which is where corrective handling comes from.

Assumptions (v1, documented): each BUY is a discrete trade held ~20 trading days or until its stop; a symbol
already open is not re-entered (no pyramiding); prices/returns use split-adjusted closes; the stop compares
today's adjusted close to the entry stop. REVIEW-ONLY: portfolio_authority=none, no broker. It changes no
live authority -- it is a paper ledger + operator view. CLI: `python -m advisory.paper_book` (daily), or
`--backfill-from YYYY-MM-DD` to populate the book by replaying the advisory.
"""
from __future__ import annotations

import argparse
import json
import os
from typing import Any

import numpy as np
import pandas as pd

from advisory.archetype_backtest import _load_benchmark
from advisory.paper_decision_loop import (HOLD_DAYS, MAX_NAMES, RS_MIN_PERCENTILE, POLICY_VERSION,
                                          _load_panel, _rs_percentile)
from utils.db import sql_to_df, upsert_to_db
from utils.schema_migrations import apply_schema_migration

TRIM_RS = float(os.getenv("PAPER_BOOK_TRIM_RS", "55"))        # momentum faded -> consider trimming
MAX_MISSING_DAYS = int(os.getenv("PAPER_BOOK_MAX_MISSING_DAYS", "3"))  # no price this long -> exit (data gap/delist)
BACKFILL_DEFAULT_DAYS = int(os.getenv("PAPER_BOOK_BACKFILL_DAYS", "40"))

TABLE_NAME = "advisory_paper_book"
MIGRATION_ID = "20260714_advisory_paper_book"
SCHEMA_STATEMENTS = [
    f"""
    CREATE TABLE IF NOT EXISTS {TABLE_NAME} (
        symbol TEXT NOT NULL,
        entry_date TIMESTAMPTZ NOT NULL,
        policy_version TEXT NOT NULL,
        entry_price DOUBLE PRECISION,
        stop_price DOUBLE PRECISION,
        entry_weight_pct DOUBLE PRECISION,
        entry_rs DOUBLE PRECISION,
        status TEXT,
        last_mark_date TIMESTAMPTZ,
        last_price DOUBLE PRECISION,
        days_held BIGINT,
        unrealized_return_pct DOUBLE PRECISION,
        missing_days BIGINT,
        last_action TEXT,
        last_action_reason TEXT,
        exit_date TIMESTAMPTZ,
        exit_price DOUBLE PRECISION,
        exit_reason TEXT,
        realized_return_pct DOUBLE PRECISION,
        realized_excess_pct DOUBLE PRECISION,
        ca_flag BOOLEAN,
        authority_scope TEXT,
        broker_execution_allowed BOOLEAN,
        load_ts TIMESTAMPTZ,
        UNIQUE (symbol, entry_date, policy_version)
    )
    """,
]


def ensure_table() -> None:
    apply_schema_migration(
        migration_id=MIGRATION_ID, statements=SCHEMA_STATEMENTS, owner="advisory.paper_book",
        description="Always-on paper position book: BUYs assumed taken, managed to EXIT (review-only).",
        metadata={"tables": [TABLE_NAME], "authority": "review_only"},
    )


# ------------------------------------------------------------------------------------------------
# Pure exit/lifecycle decision (unit-tested without DB)
# ------------------------------------------------------------------------------------------------
def decide_action(*, entry_price: float, stop_price: float, mark: float | None, days_held: int,
                  rs_now: float | None, missing_days: int, hold_days: int = HOLD_DAYS,
                  trim_rs: float = TRIM_RS, max_missing: int = MAX_MISSING_DAYS) -> tuple[str, str]:
    """Return (action, reason). action in {HOLD, TRIM, EXIT}. Hard rules (stop, time cap, delist) win over
    the soft TRIM. A missing price is tolerated for a few days, then the position is exited as a data gap."""
    if mark is None or not np.isfinite(mark):
        if missing_days + 1 >= max_missing:
            return ("EXIT", "data_gap_or_delisted")
        return ("HOLD", "no_price_today")
    if mark <= stop_price:
        return ("EXIT", "stop_hit")
    if days_held >= hold_days:
        return ("EXIT", "time_cap")
    if rs_now is not None and rs_now < trim_rs:
        return ("TRIM", "momentum_fade")
    return ("HOLD", "")


# ------------------------------------------------------------------------------------------------
# Data loads
# ------------------------------------------------------------------------------------------------
def _load_marks(start_date: pd.Timestamp) -> dict:
    """(symbol, date) -> (adj_close, ambiguous) for every EQ/BE row on/after start_date (for marking held
    names, including ones that fell out of the liquid universe)."""
    df = sql_to_df(
        """
        SELECT symbol, date, adj_close, (ca_flag = 'ambiguous') AS amb
        FROM advisory_adjusted_ohlcv_daily WHERE date >= %(s)s
        """,
        params={"s": pd.Timestamp(start_date).tz_convert("UTC") if pd.Timestamp(start_date).tzinfo else pd.Timestamp(start_date, tz="UTC")},
    )
    if df.empty:
        return {}
    df["date"] = pd.to_datetime(df["date"], utc=True, errors="coerce").dt.normalize()
    df["adj_close"] = pd.to_numeric(df["adj_close"], errors="coerce")
    return {(r.symbol, r.date): (float(r.adj_close) if pd.notna(r.adj_close) else None, bool(r.amb))
            for r in df.itertuples(index=False)}


def _picks_for_date(day: pd.DataFrame) -> pd.DataFrame:
    day = day.copy()
    day["rs_percentile"] = _rs_percentile(day)
    return day[day["rs_percentile"] >= RS_MIN_PERCENTILE].sort_values("rs_percentile", ascending=False).head(MAX_NAMES)


# ------------------------------------------------------------------------------------------------
# One trading day of book maintenance: mark + exit open positions, then ingest that day's new buys
# ------------------------------------------------------------------------------------------------
def _step_day(date: pd.Timestamp, *, panel_day: pd.DataFrame, positions: dict, marks: dict,
              bench_close: dict, td_index: dict, stop_atr_mult: float) -> list[dict[str, Any]]:
    actions: list[dict[str, Any]] = []
    rs_map = {}
    if not panel_day.empty:
        pd_ = panel_day.copy(); pd_["rs_percentile"] = _rs_percentile(pd_)
        rs_map = dict(zip(pd_["symbol"], pd_["rs_percentile"]))

    # 1) mark + exit open positions
    for key, pos in list(positions.items()):
        if pos["status"] != "open":
            continue
        mark, amb = marks.get((pos["symbol"], date), (None, False))
        days_held = td_index[date] - td_index[pos["entry_date"]]
        rs_now = rs_map.get(pos["symbol"])
        action, reason = decide_action(entry_price=pos["entry_price"], stop_price=pos["stop_price"],
                                       mark=mark, days_held=days_held, rs_now=rs_now,
                                       missing_days=pos["missing_days"])
        pos["days_held"] = days_held
        pos["ca_flag"] = pos.get("ca_flag") or bool(amb)
        if mark is None:
            pos["missing_days"] += 1
        else:
            pos["missing_days"] = 0
            pos["last_price"] = mark
            pos["last_mark_date"] = date
            pos["unrealized_return_pct"] = round((mark / pos["entry_price"] - 1) * 100, 2)
        pos["last_action"], pos["last_action_reason"] = action, reason
        if action == "EXIT":
            exit_price = mark if mark is not None else pos.get("last_price", pos["entry_price"])
            pos.update(status="exited", exit_date=date, exit_price=exit_price, exit_reason=reason)
            r = exit_price / pos["entry_price"] - 1
            pos["realized_return_pct"] = round(r * 100, 2)
            be, xe = bench_close.get(pos["entry_date"]), bench_close.get(date)
            pos["realized_excess_pct"] = round((r - (xe / be - 1)) * 100, 2) if be and xe else None
            actions.append({"action": "EXIT", "symbol": pos["symbol"], "reason": reason,
                            "return_pct": pos["realized_return_pct"], "excess_pct": pos["realized_excess_pct"],
                            "days_held": days_held})
        elif action == "TRIM":
            actions.append({"action": "TRIM", "symbol": pos["symbol"], "reason": reason,
                            "return_pct": pos.get("unrealized_return_pct"), "days_held": days_held})

    # 2) ingest today's new buys (dedup: skip symbols already open)
    open_syms = {p["symbol"] for p in positions.values() if p["status"] == "open"}
    picks = _picks_for_date(panel_day) if not panel_day.empty else pd.DataFrame()
    for p in picks.itertuples(index=False):
        if p.symbol in open_syms:
            continue
        entry = float(p.close)
        stop = round(entry * (1.0 - stop_atr_mult * float(p.atr_pct)), 4)
        key = (p.symbol, date, POLICY_VERSION)
        positions[key] = {
            "symbol": p.symbol, "entry_date": date, "policy_version": POLICY_VERSION, "entry_price": entry,
            "stop_price": stop, "entry_weight_pct": None, "entry_rs": round(float(p.rs_percentile), 2),
            "status": "open", "last_mark_date": date, "last_price": entry, "days_held": 0,
            "unrealized_return_pct": 0.0, "missing_days": 0, "last_action": "BUY", "last_action_reason": "new_entry",
            "exit_date": None, "exit_price": None, "exit_reason": None, "realized_return_pct": None,
            "realized_excess_pct": None, "ca_flag": False,
        }
        open_syms.add(p.symbol)
        actions.append({"action": "BUY", "symbol": p.symbol, "rs": round(float(p.rs_percentile), 2),
                        "entry_price": round(entry, 2), "stop_price": round(stop, 2)})
    return actions


def run_book(*, backfill_from: str | None = None, dry_run: bool = False) -> dict[str, Any]:
    panel = _load_panel(HOLD_DAYS)
    if panel.empty:
        return {"note": "empty panel", "actions": []}
    panel["date"] = pd.to_datetime(panel["date"], utc=True, errors="coerce").dt.normalize()
    bench = _load_benchmark(); bench["date"] = pd.to_datetime(bench["date"], utc=True, errors="coerce").dt.normalize()
    bench_close = dict(zip(bench["date"], pd.to_numeric(bench["bench_close"], errors="coerce")))
    trading_days = sorted(panel["date"].dropna().unique())
    td_index = {d: i for i, d in enumerate(trading_days)}

    # existing book (as dict keyed by (symbol, entry_date, policy))
    positions: dict = {}
    try:
        existing = sql_to_df(f"SELECT * FROM {TABLE_NAME}")
    except Exception:
        existing = pd.DataFrame()
    if not existing.empty:
        existing["entry_date"] = pd.to_datetime(existing["entry_date"], utc=True, errors="coerce").dt.normalize()
        existing["last_mark_date"] = pd.to_datetime(existing["last_mark_date"], utc=True, errors="coerce").dt.normalize()
        existing["exit_date"] = pd.to_datetime(existing["exit_date"], utc=True, errors="coerce").dt.normalize()
        for r in existing.to_dict("records"):
            positions[(r["symbol"], r["entry_date"], r["policy_version"])] = {**r, "missing_days": int(r.get("missing_days") or 0)}

    last_seen = max((p["last_mark_date"] for p in positions.values() if p.get("last_mark_date") is not None), default=None)
    if backfill_from:
        start = pd.Timestamp(backfill_from, tz="UTC").normalize()
    elif last_seen is not None:
        start = trading_days[min(td_index.get(last_seen, len(trading_days) - 1) + 1, len(trading_days) - 1)]
    else:
        start = trading_days[max(0, len(trading_days) - BACKFILL_DEFAULT_DAYS)]  # first run: populate a window
    days = [d for d in trading_days if d >= start]
    marks = _load_marks(start - pd.Timedelta(days=5))

    latest_actions: list[dict[str, Any]] = []
    for d in days:
        panel_day = panel[panel["date"] == d]
        latest_actions = _step_day(d, panel_day=panel_day, positions=positions, marks=marks,
                                    bench_close=bench_close, td_index=td_index, stop_atr_mult=_stop_mult())

    rows = []
    now = pd.Timestamp.utcnow()
    for pos in positions.values():
        rows.append({**{k: pos.get(k) for k in (
            "symbol", "entry_date", "policy_version", "entry_price", "stop_price", "entry_weight_pct", "entry_rs",
            "status", "last_mark_date", "last_price", "days_held", "unrealized_return_pct", "missing_days",
            "last_action", "last_action_reason", "exit_date", "exit_price", "exit_reason", "realized_return_pct",
            "realized_excess_pct", "ca_flag")},
            "authority_scope": "review_only", "broker_execution_allowed": False, "load_ts": now})
    if rows and not dry_run:
        ensure_table()
        upsert_to_db(pd.DataFrame(rows), TABLE_NAME,
                     unique_keys=["symbol", "entry_date", "policy_version"], timescaledb_column="entry_date")

    asof = days[-1] if days else None
    return {"asof": str(asof.date()) if asof is not None else None, "actions": latest_actions,
            "book": summarize_book(positions, asof)}


def _stop_mult() -> float:
    from advisory.portfolio_risk import STOP_ATR_MULT
    return STOP_ATR_MULT


def summarize_book(positions: dict, asof) -> dict[str, Any]:
    open_pos = [p for p in positions.values() if p["status"] == "open"]
    closed = [p for p in positions.values() if p["status"] == "exited"]
    # pd.notna excludes both None AND NaN -- values loaded back from the DB arrive as NaN, not None,
    # which would otherwise poison np.mean (the "excess nan%" seen in the summary).
    unreal = [p["unrealized_return_pct"] for p in open_pos if pd.notna(p.get("unrealized_return_pct"))]
    real = [p["realized_return_pct"] for p in closed if pd.notna(p.get("realized_return_pct"))]
    real_ex = [p["realized_excess_pct"] for p in closed if pd.notna(p.get("realized_excess_pct"))]
    return {
        "open": len(open_pos), "closed": len(closed),
        "avg_unrealized_pct": round(float(np.mean(unreal)), 2) if unreal else None,
        "avg_realized_pct": round(float(np.mean(real)), 2) if real else None,
        "avg_realized_excess_pct": round(float(np.mean(real_ex)), 2) if real_ex else None,
        "win_rate_pct": round(float(np.mean([1.0 if r > 0 else 0.0 for r in real])) * 100, 0) if real else None,
        "flagged": sum(1 for p in open_pos if p.get("missing_days", 0) > 0 or p.get("ca_flag")),
    }


def open_positions_view(limit: int | None = None) -> pd.DataFrame:
    df = sql_to_df(
        f"""SELECT symbol, entry_date::date AS entry_date, days_held, entry_price, last_price, stop_price,
                   unrealized_return_pct, entry_rs, last_action, last_action_reason, missing_days, ca_flag
            FROM {TABLE_NAME} WHERE status='open' ORDER BY unrealized_return_pct DESC NULLS LAST"""
    )
    return df.head(limit) if limit else df


def format_text(result: dict[str, Any]) -> str:
    b = result.get("book", {})
    acts = result.get("actions", [])
    def _n(kind):
        return sum(1 for a in acts if a["action"] == kind)
    lines = [f"PAPER BOOK (REVIEW-ONLY) | asof {result.get('asof')} | open {b.get('open')} closed {b.get('closed')}",
             f"today: BUY {_n('BUY')}  TRIM {_n('TRIM')}  EXIT {_n('EXIT')}   "
             f"| avg unrealized {b.get('avg_unrealized_pct')}%  realized {b.get('avg_realized_pct')}% "
             f"(excess {b.get('avg_realized_excess_pct')}%, win {b.get('win_rate_pct')}%)  flagged {b.get('flagged')}"]
    for a in acts:
        if a["action"] == "EXIT":
            lines.append(f"  EXIT  {a['symbol']:<12} {a['reason']:<20} ret {a.get('return_pct')}% "
                         f"excess {a.get('excess_pct')}% ({a.get('days_held')}d)")
        elif a["action"] == "TRIM":
            lines.append(f"  TRIM  {a['symbol']:<12} {a['reason']:<20} ret {a.get('return_pct')}%")
    buys = [a["symbol"] for a in acts if a["action"] == "BUY"]
    if buys:
        lines.append(f"  BUY   {', '.join(buys)}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Always-on paper position book: BUYs assumed taken, managed to EXIT (review-only).")
    parser.add_argument("--backfill-from", default=None, help="YYYY-MM-DD; replay the book from here to populate it.")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--format", choices=["text", "json"], default="text")
    parser.add_argument("--html", default=os.getenv("PAPER_BOOK_HTML_PATH", "reports/daily_book.html"),
                        help="Path for the operator book dashboard (empty = skip).")
    args = parser.parse_args(argv)
    result = run_book(backfill_from=args.backfill_from, dry_run=bool(args.dry_run))
    if args.html and not args.dry_run:
        from advisory.paper_book_report import write_book_dashboard
        result["html_path"] = write_book_dashboard(result, args.html)
    print(json.dumps(result, indent=2, default=str) if args.format == "json" else format_text(result))
    if result.get("html_path"):
        print(f"dashboard: {result['html_path']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
