"""Missed-movers analyser: what moved, and where did our funnel stand on it?

Runs post-market. Finds every liquid stock that made a meaningful multi-day move (up or
down) over a lookback window, then classifies each by its relationship to our funnel on
the day the move began -- so the operator can see, in one table, whether the winners we
missed were never seen, seen-but-gate-blocked, or watched-but-never-triggered. That is the
raw material for deciding which NEW signals to build, which existing gates cost us, and
which unused signals to wire in.

Classification per mover (evaluated at the move's START date):
  no_signal            -- never in constituents: no screener/scan/hypothesis/theme saw it
  admitted_not_candidate -- in constituents but never became a candidate (admission gate)
  blocked_by_gate      -- was a candidate/rejection, hard-rejected by a named gate
  watched_not_triggered -- a live candidate (WATCH_*) that never reached BUY_TRIGGERED
  captured             -- reached PASS_NOW / BUY_TRIGGERED (we were positioned)

Review-only research: writes advisory_missed_movers_daily + a ranked report; never trades,
never changes thresholds. The regret ledger measures gates we DID apply; this measures the
whole opportunity set, including names that never entered the funnel at all.
"""
from __future__ import annotations

import argparse
import json
import os
from typing import Any

import pandas as pd

from utils.db import sql_to_df, upsert_to_db
from utils.schema_migrations import apply_schema_migration

TABLE_NAME = "advisory_missed_movers_daily"
MIGRATION_ID = "20260712_advisory_missed_movers_daily_base"
SCHEMA_STATEMENTS = [
    f"""
    CREATE TABLE IF NOT EXISTS {TABLE_NAME} (
        asof_date TIMESTAMPTZ NOT NULL,
        symbol TEXT NOT NULL,
        move_start_date TIMESTAMPTZ,
        window_days BIGINT,
        move_pct DOUBLE PRECISION,
        direction TEXT,
        max_volume_multiple DOUBLE PRECISION,
        turnover_inr DOUBLE PRECISION,
        proximity_52w_high DOUBLE PRECISION,
        classification TEXT,
        funnel_detail TEXT,
        blocking_gate TEXT,
        load_ts TIMESTAMPTZ,
        UNIQUE (asof_date, symbol, window_days)
    )
    """,
]

WINDOW_DAYS = int(os.getenv("MISSED_MOVERS_WINDOW_DAYS", "10"))
MIN_MOVE_PCT = float(os.getenv("MISSED_MOVERS_MIN_MOVE_PCT", "15.0"))
MIN_TURNOVER_INR = float(os.getenv("MISSED_MOVERS_MIN_TURNOVER_INR", "50000000"))
MIN_PRICE = float(os.getenv("MISSED_MOVERS_MIN_PRICE", "30.0"))
MAX_ROWS = int(os.getenv("MISSED_MOVERS_MAX_ROWS", "200"))


def ensure_table() -> None:
    apply_schema_migration(
        migration_id=MIGRATION_ID,
        description="Record the daily big-mover opportunity set and each mover's funnel relationship.",
        statements=SCHEMA_STATEMENTS,
        metadata={"module": "advisory.missed_movers_analyzer", "tables": [TABLE_NAME]},
    )


def find_movers(asof_date: pd.Timestamp, *, window_days: int, min_move_pct: float) -> pd.DataFrame:
    """Liquid names whose close moved >= min_move_pct (abs) over the trailing window, with the
    move's start close, the peak intraday volume multiple in the window, and 52w-high proximity."""
    return sql_to_df(
        f"""
        WITH win AS (
            SELECT symbol, date, close, volume,
                   AVG(volume) OVER (PARTITION BY symbol ORDER BY date ROWS BETWEEN 20 PRECEDING AND 1 PRECEDING) AS avgvol20,
                   MAX(close)  OVER (PARTITION BY symbol ORDER BY date ROWS BETWEEN 252 PRECEDING AND 1 PRECEDING) AS max252,
                   FIRST_VALUE(close) OVER (PARTITION BY symbol ORDER BY date
                       ROWS BETWEEN %(win)s PRECEDING AND CURRENT ROW) AS window_start_close,
                   FIRST_VALUE(date) OVER (PARTITION BY symbol ORDER BY date
                       ROWS BETWEEN %(win)s PRECEDING AND CURRENT ROW) AS window_start_date,
                   ROW_NUMBER() OVER (PARTITION BY symbol ORDER BY date DESC) AS rn
            FROM nseindia_ohlcv
            WHERE series = 'EQ' AND date <= %(asof)s AND date > %(asof)s - interval '%(lookback)s days'
        ),
        latest AS (SELECT * FROM win WHERE rn = 1),
        volmax AS (
            SELECT symbol, MAX(volume / NULLIF(avgvol20, 0)) AS max_vol_x
            FROM win WHERE date > %(asof)s - make_interval(days => %(win)s) GROUP BY symbol
        )
        SELECT l.symbol,
               l.window_start_date AS move_start_date,
               l.close,
               (l.close / NULLIF(l.window_start_close, 0) - 1) * 100 AS move_pct,
               (l.avgvol20 * l.close) AS turnover_inr,
               (l.close / NULLIF(l.max252, 0)) AS proximity_52w_high,
               v.max_vol_x
        FROM latest l
        LEFT JOIN volmax v ON v.symbol = l.symbol
        WHERE l.close >= %(min_price)s
          AND l.avgvol20 * l.close >= %(min_turnover)s
          AND ABS(l.close / NULLIF(l.window_start_close, 0) - 1) * 100 >= %(min_move)s
        ORDER BY ABS(l.close / NULLIF(l.window_start_close, 0) - 1) DESC
        LIMIT %(max_rows)s
        """,
        params={
            "asof": asof_date, "win": int(window_days), "lookback": int(window_days) + 30,
            "min_price": float(MIN_PRICE), "min_turnover": float(MIN_TURNOVER_INR),
            "min_move": float(min_move_pct), "max_rows": int(MAX_ROWS),
        },
    )


def _funnel_lookups(symbols: list[str], start: pd.Timestamp, end: pd.Timestamp) -> dict[str, dict[str, Any]]:
    """For each symbol, its best funnel state over [move_start, asof]: was it admitted, a
    candidate (best state), or hard-rejected (dominant gate)?"""
    if not symbols:
        return {}
    upper = [s.upper() for s in symbols]
    cons = sql_to_df(
        "SELECT DISTINCT UPPER(TRIM(ticker)) AS symbol, screener_slug FROM advisory_screener_constituents "
        "WHERE date >= %s AND date <= %s AND UPPER(TRIM(ticker)) = ANY(%s)",
        params=(start, end, upper),
    )
    cand = sql_to_df(
        "SELECT UPPER(TRIM(symbol)) AS symbol, candidate_state, technical_state, source_screener_slug "
        "FROM advisory_candidates WHERE asof_date >= %s AND asof_date <= %s AND UPPER(TRIM(symbol)) = ANY(%s)",
        params=(start, end, upper),
    )
    rej = sql_to_df(
        "SELECT UPPER(TRIM(symbol)) AS symbol, reason_code, COUNT(*) n "
        "FROM advisory_candidate_rejections WHERE asof_date >= %s AND asof_date <= %s "
        "AND UPPER(TRIM(symbol)) = ANY(%s) GROUP BY 1, 2",
        params=(start, end, upper),
    )
    cons_by = cons.groupby("symbol")["screener_slug"].apply(lambda s: sorted(set(s))).to_dict() if not cons.empty else {}
    cand_by: dict[str, list[str]] = {}
    tech_by: dict[str, list[str]] = {}
    if not cand.empty:
        for sym, grp in cand.groupby("symbol"):
            cand_by[sym] = sorted({str(v) for v in grp["candidate_state"].dropna()})
            tech_by[sym] = sorted({str(v) for v in grp["technical_state"].dropna()})
    rej_by: dict[str, str] = {}
    if not rej.empty:
        top = rej.sort_values("n", ascending=False).drop_duplicates("symbol")
        rej_by = dict(zip(top["symbol"], top["reason_code"]))
    out: dict[str, dict[str, Any]] = {}
    for sym in upper:
        out[sym] = {
            "screeners": cons_by.get(sym, []),
            "candidate_states": cand_by.get(sym, []),
            "technical_states": tech_by.get(sym, []),
            "top_rejection": rej_by.get(sym),
        }
    return out


_CAPTURE_STATES = {"PASS_NOW", "BUY_TRIGGERED"}
_WATCH_PREFIXES = ("WATCH", "NEAR", "READY", "ABSTAIN")


def _classify(funnel: dict[str, Any]) -> tuple[str, str, str | None]:
    states = set(funnel.get("candidate_states") or [])
    tech = set(funnel.get("technical_states") or [])
    if states & _CAPTURE_STATES or "BUY_TRIGGERED" in tech:
        return "captured", "reached BUY/PASS_NOW", None
    if states:
        if any(str(s).startswith(_WATCH_PREFIXES) for s in states):
            return "watched_not_triggered", f"candidate states={sorted(states)}", None
        return "watched_not_triggered", f"candidate states={sorted(states)}", None
    if funnel.get("top_rejection"):
        return "blocked_by_gate", f"rejected: {funnel['top_rejection']}", str(funnel["top_rejection"])
    if funnel.get("screeners"):
        return "admitted_not_candidate", f"in screeners={funnel['screeners']}", None
    return "no_signal", "never in the funnel", None


def analyze(asof_date: Any | None = None, *, window_days: int | None = None, dry_run: bool = False) -> dict[str, Any]:
    ensure_table()
    win = int(window_days if window_days is not None else WINDOW_DAYS)
    parsed = pd.to_datetime(asof_date, utc=True, errors="coerce")
    if pd.isna(parsed):
        latest = sql_to_df("SELECT MAX(date) d FROM nseindia_ohlcv WHERE series='EQ'")
        parsed = pd.to_datetime(latest.iloc[0]["d"], utc=True, errors="coerce") if not latest.empty else pd.Timestamp.utcnow()
    parsed = parsed.normalize()
    movers = find_movers(parsed, window_days=win, min_move_pct=MIN_MOVE_PCT)
    if movers.empty:
        return {"asof_date": parsed.date().isoformat(), "movers": 0, "buckets": {}, "dry_run": dry_run}
    start = pd.to_datetime(movers["move_start_date"], utc=True, errors="coerce").min()
    funnel = _funnel_lookups(movers["symbol"].astype(str).tolist(), start.normalize(), parsed)
    now = pd.Timestamp.utcnow()
    rows: list[dict[str, Any]] = []
    for m in movers.itertuples(index=False):
        sym = str(m.symbol).upper()
        cls, detail, gate = _classify(funnel.get(sym, {}))
        rows.append(
            {
                "asof_date": parsed, "symbol": sym,
                "move_start_date": pd.to_datetime(m.move_start_date, utc=True, errors="coerce"),
                "window_days": win, "move_pct": float(m.move_pct),
                "direction": "up" if float(m.move_pct) >= 0 else "down",
                "max_volume_multiple": None if pd.isna(m.max_vol_x) else float(m.max_vol_x),
                "turnover_inr": None if pd.isna(m.turnover_inr) else float(m.turnover_inr),
                "proximity_52w_high": None if pd.isna(m.proximity_52w_high) else float(m.proximity_52w_high),
                "classification": cls, "funnel_detail": detail, "blocking_gate": gate,
                "load_ts": now,
            }
        )
    frame = pd.DataFrame(rows)
    if not dry_run:
        upsert_to_db(frame, TABLE_NAME, unique_keys=["asof_date", "symbol", "window_days"], timescaledb_column="asof_date")
    up = frame[frame["direction"] == "up"]
    buckets = up["classification"].value_counts().to_dict()
    # the actionable signal: gates blocking up-movers, and the no-signal up-movers by sector proximity
    blocked = up[up["classification"] == "blocked_by_gate"]["blocking_gate"].value_counts().to_dict()
    no_signal_up = up[up["classification"] == "no_signal"]
    return {
        "asof_date": parsed.date().isoformat(), "window_days": win, "movers": int(len(frame)),
        "up_movers": int(len(up)), "buckets": buckets, "blocking_gates": blocked,
        "no_signal_up_count": int(len(no_signal_up)),
        "no_signal_up_examples": no_signal_up.nlargest(10, "move_pct")[["symbol", "move_pct", "proximity_52w_high"]].to_dict(orient="records"),
        "dry_run": dry_run,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Analyse big movers vs our funnel to guide signal development.")
    parser.add_argument("--date", default=None)
    parser.add_argument("--window-days", type=int, default=None)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--format", choices=["text", "json"], default="text")
    args = parser.parse_args(argv)
    result = analyze(args.date, window_days=args.window_days, dry_run=bool(args.dry_run))
    if args.format == "json":
        print(json.dumps(result, indent=2, default=str))
    else:
        print(f"[missed_movers] asof={result['asof_date']} window={result.get('window_days')}d "
              f"movers={result['movers']} up={result.get('up_movers', 0)}")
        print(f"  up-mover buckets: {result.get('buckets')}")
        print(f"  gates blocking up-movers: {result.get('blocking_gates')}")
        print(f"  no-signal up-movers ({result.get('no_signal_up_count', 0)}) -- candidates for a NEW signal:")
        for ex in result.get("no_signal_up_examples", []):
            prox = ex.get("proximity_52w_high")
            print(f"    {ex['symbol']:<14} +{ex['move_pct']:.1f}%  vs52wHigh={prox:.2f}" if prox else f"    {ex['symbol']:<14} +{ex['move_pct']:.1f}%")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
