"""Automatic regret ledger: charge every gate with the forward return of what it blocked.

The platform is defense-heavy -- a candidate must survive many veto layers to become a
BUY -- but no gate has ever been charged for what it blocked. This module closes that
loop automatically, with zero manual steps:

- ENROLL (nightly, post-advisory): every blocked or downgraded would-be BUY for the run
  date is recorded with its blocking gate -- rule-engine rejections (reason_code), watch
  states that did not convert (candidate_state), and PASS_NOW candidates that risk
  refused to size (allocation_status).
- LABEL (same nightly run): enrolled rows mature at 5/10/20 trading days with the
  symbol's forward return, the NIFTY benchmark return, and cost-adjusted excess -- the
  same benchmark-excess discipline as decision outcomes.
- SUMMARIZE: a per-gate P&L (count, hit rate, average excess foregone) surfaced on the
  operator Scorecard, so gate recalibration becomes an evidence decision.

This is measurement of already-recorded decisions (no lookahead enters any live path),
and it also enforces the operator rule that announcements must never gate by absence:
any event-related gate that starts blocking quiet-company BUYs shows up as a costed
line item.
"""
from __future__ import annotations

import argparse
import json
import os
from typing import Any

import pandas as pd

from advisory.fallback_telemetry import record_local_fallback_event
from utils.db import db_session, execute_db_operation, sql_to_df, upsert_to_db
from utils.schema_migrations import apply_schema_migration

TABLE_NAME = "advisory_regret_ledger"
REGRET_LEDGER_MIGRATION_ID = "20260707_advisory_regret_ledger_base"
REGRET_LEDGER_SCHEMA_STATEMENTS = [
    f"""
    CREATE TABLE IF NOT EXISTS {TABLE_NAME} (
        asof_date TIMESTAMPTZ NOT NULL,
        symbol TEXT NOT NULL,
        stage TEXT NOT NULL,
        gate TEXT NOT NULL,
        candidate_state TEXT,
        detail TEXT,
        entry_close DOUBLE PRECISION,
        fwd_return_5d DOUBLE PRECISION,
        fwd_return_10d DOUBLE PRECISION,
        fwd_return_20d DOUBLE PRECISION,
        benchmark_return_5d DOUBLE PRECISION,
        benchmark_return_10d DOUBLE PRECISION,
        benchmark_return_20d DOUBLE PRECISION,
        excess_after_cost_5d DOUBLE PRECISION,
        excess_after_cost_10d DOUBLE PRECISION,
        excess_after_cost_20d DOUBLE PRECISION,
        matured_5d BOOLEAN,
        matured_10d BOOLEAN,
        matured_20d BOOLEAN,
        broker_execution_allowed BOOLEAN,
        enrolled_at TIMESTAMPTZ,
        labeled_at TIMESTAMPTZ,
        load_ts TIMESTAMPTZ,
        UNIQUE (asof_date, symbol, stage, gate)
    )
    """,
]

HORIZONS = (5, 10, 20)
REGRET_LEDGER_COST_BPS = float(os.getenv("REGRET_LEDGER_COST_BPS", "25.0"))
BENCHMARK_TICKER = os.getenv("REGRET_LEDGER_BENCHMARK_TICKER", "NIFTY")
CANDIDATES_TABLE = "advisory_candidates"
REJECTIONS_TABLE = "advisory_candidate_rejections"
ALLOCATIONS_TABLE = "advisory_allocations"
PRICES_TABLE = "nseindia_ohlcv"
BENCHMARK_TABLE = "dhan_ohlcv_daily"


def ensure_table() -> None:
    apply_schema_migration(
        migration_id=REGRET_LEDGER_MIGRATION_ID,
        description="Create the regret ledger: forward benchmark-excess of gate-blocked would-be BUYs.",
        statements=REGRET_LEDGER_SCHEMA_STATEMENTS,
        metadata={"module": "advisory.regret_ledger", "tables": [TABLE_NAME]},
    )


def _text(value: Any) -> str:
    if value is None:
        return ""
    try:
        if pd.isna(value):
            return ""
    except Exception:
        pass
    return str(value).strip()


def build_enrollment_rows(asof_date: pd.Timestamp) -> pd.DataFrame:
    """All blocked/downgraded would-be BUYs for one advisory date, attributed to a gate."""
    now = pd.Timestamp.utcnow()
    rows: list[dict[str, Any]] = []

    def _add(symbol: Any, stage: str, gate: str, *, state: Any = None, detail: Any = None) -> None:
        symbol_text = _text(symbol).upper()
        gate_text = _text(gate) or "unknown"
        if not symbol_text:
            return
        rows.append(
            {
                "asof_date": asof_date,
                "symbol": symbol_text,
                "stage": stage,
                "gate": f"{stage}:{gate_text}",
                "candidate_state": _text(state) or None,
                "detail": (_text(detail) or None if detail is not None else None),
                "broker_execution_allowed": False,
                "enrolled_at": now,
                "load_ts": now,
            }
        )

    try:
        rejections = sql_to_df(
            f"""
            SELECT DISTINCT ON (symbol, reason_code) symbol, reason_code, reason_detail
            FROM {REJECTIONS_TABLE} WHERE asof_date = %s
            ORDER BY symbol, reason_code
            """,
            params=(asof_date,),
        )
        for row in rejections.itertuples(index=False):
            _add(row.symbol, "rules", row.reason_code, detail=row.reason_detail)
    except Exception as exc:
        record_local_fallback_event(
            module="advisory.regret_ledger", source=REJECTIONS_TABLE, severity="warn",
            fallback_type="regret_ledger_rejections_load_failed",
            reason="Regret ledger could not enroll rule rejections for this date.", error=exc,
            metadata={"asof_date": str(asof_date)},
        )

    try:
        candidates = sql_to_df(
            f"""
            SELECT DISTINCT ON (symbol) symbol, candidate_state, watch_reason_detail
            FROM {CANDIDATES_TABLE}
            WHERE asof_date = %s AND UPPER(candidate_state) <> 'PASS_NOW'
            ORDER BY symbol, setup_id
            """,
            params=(asof_date,),
        )
        for row in candidates.itertuples(index=False):
            state = _text(row.candidate_state).upper() or "UNKNOWN"
            _add(row.symbol, "candidates", f"state_{state.lower()}", state=state, detail=row.watch_reason_detail)
    except Exception as exc:
        record_local_fallback_event(
            module="advisory.regret_ledger", source=CANDIDATES_TABLE, severity="warn",
            fallback_type="regret_ledger_candidates_load_failed",
            reason="Regret ledger could not enroll non-converting candidates for this date.", error=exc,
            metadata={"asof_date": str(asof_date)},
        )

    try:
        allocations = sql_to_df(
            f"""
            SELECT DISTINCT ON (symbol) symbol, allocation_status, candidate_state, notes
            FROM {ALLOCATIONS_TABLE}
            WHERE asof_date = %s AND LOWER(COALESCE(allocation_status, '')) NOT IN ('allocated')
            ORDER BY symbol, published_on DESC
            """,
            params=(asof_date,),
        )
        for row in allocations.itertuples(index=False):
            _add(row.symbol, "risk", _text(row.allocation_status).lower() or "unsized",
                 state=row.candidate_state, detail=row.notes)
    except Exception as exc:
        record_local_fallback_event(
            module="advisory.regret_ledger", source=ALLOCATIONS_TABLE, severity="warn",
            fallback_type="regret_ledger_allocations_load_failed",
            reason="Regret ledger could not enroll unsized risk rows for this date.", error=exc,
            metadata={"asof_date": str(asof_date)},
        )

    if not rows:
        return pd.DataFrame()
    frame = pd.DataFrame(rows)
    return frame.drop_duplicates(subset=["asof_date", "symbol", "stage", "gate"], keep="first")


def enroll(asof_date: Any) -> int:
    ensure_table()
    parsed = pd.to_datetime(asof_date, utc=True, errors="coerce")
    if pd.isna(parsed):
        return 0
    frame = build_enrollment_rows(parsed)
    if frame.empty:
        return 0
    upsert_to_db(frame, TABLE_NAME, unique_keys=["asof_date", "symbol", "stage", "gate"], timescaledb_column="asof_date")
    return int(len(frame))


def _load_price_series(symbols: list[str], from_date: pd.Timestamp) -> pd.DataFrame:
    return sql_to_df(
        f"""
        SELECT UPPER(TRIM(symbol)) AS symbol, date, close
        FROM {PRICES_TABLE}
        WHERE series = 'EQ' AND UPPER(TRIM(symbol)) = ANY(%s) AND date >= %s
        ORDER BY symbol, date
        """,
        params=(symbols, from_date),
    )


def _load_benchmark_series(from_date: pd.Timestamp) -> pd.DataFrame:
    return sql_to_df(
        f"""
        SELECT date, close FROM {BENCHMARK_TABLE}
        WHERE UPPER(TRIM(ticker)) = %s AND date >= %s
        ORDER BY date
        """,
        params=(BENCHMARK_TICKER, from_date),
    )


def _forward_close(series: pd.DataFrame, asof: pd.Timestamp, horizon: int) -> tuple[float | None, float | None]:
    """(t0 close, t+h close) using trading rows at/after asof; None until matured."""
    if series.empty:
        return None, None
    dates = pd.to_datetime(series["date"], utc=True, errors="coerce")
    eligible = series[dates >= asof]
    if eligible.empty or len(eligible) <= horizon:
        return (float(eligible.iloc[0]["close"]) if not eligible.empty else None), None
    return float(eligible.iloc[0]["close"]), float(eligible.iloc[horizon]["close"])


def label_outcomes(*, limit: int = 2000) -> dict[str, int]:
    """Mature pending rows with forward/benchmark/excess returns (direct UPDATE per row)."""
    ensure_table()
    pending = sql_to_df(
        f"""
        SELECT asof_date, symbol, stage, gate
        FROM {TABLE_NAME}
        WHERE COALESCE(matured_20d, FALSE) = FALSE
        ORDER BY asof_date
        LIMIT %s
        """,
        params=(int(limit),),
    )
    if pending.empty:
        return {"pending": 0, "updated": 0}
    pending["asof_date"] = pd.to_datetime(pending["asof_date"], utc=True, errors="coerce")
    min_asof = pending["asof_date"].min()
    symbols = sorted(pending["symbol"].unique().tolist())
    prices = _load_price_series(symbols, min_asof)
    benchmark = _load_benchmark_series(min_asof)
    by_symbol = {symbol: group.reset_index(drop=True) for symbol, group in prices.groupby("symbol")} if not prices.empty else {}
    cost = REGRET_LEDGER_COST_BPS / 10_000.0
    now = pd.Timestamp.utcnow().to_pydatetime()
    updated = 0

    def _apply_updates() -> None:
        nonlocal updated
        with db_session() as (_conn, cur):
            for row in pending.itertuples(index=False):
                series = by_symbol.get(row.symbol, pd.DataFrame())
                sets: dict[str, Any] = {}
                for horizon in HORIZONS:
                    t0, th = _forward_close(series, row.asof_date, horizon)
                    b0, bh = _forward_close(benchmark, row.asof_date, horizon)
                    if t0 and th and b0 and bh:
                        fwd = th / t0 - 1.0
                        bench = bh / b0 - 1.0
                        sets[f"fwd_return_{horizon}d"] = fwd
                        sets[f"benchmark_return_{horizon}d"] = bench
                        sets[f"excess_after_cost_{horizon}d"] = fwd - bench - cost
                        sets[f"matured_{horizon}d"] = True
                        sets["entry_close"] = t0
                if not sets:
                    continue
                sets["labeled_at"] = now
                assignments = ", ".join(f"{column} = %s" for column in sets)
                cur.execute(
                    f"UPDATE {TABLE_NAME} SET {assignments} WHERE asof_date = %s AND symbol = %s AND stage = %s AND gate = %s",
                    (*sets.values(), row.asof_date.to_pydatetime(), row.symbol, row.stage, row.gate),
                )
                updated += 1

    execute_db_operation(_apply_updates, operation_name=f"regret_ledger:label:{TABLE_NAME}")
    return {"pending": int(len(pending)), "updated": updated}


def summarize(*, horizon: int = 10, min_matured: int = 5, limit: int = 25) -> list[dict[str, Any]]:
    """Per-gate P&L: what each gate's blocks would have returned vs benchmark after cost.

    A POSITIVE avg excess means the gate is blocking winners (it costs alpha); a NEGATIVE
    avg excess means the gate is earning its keep by refusing losers.
    """
    ensure_table()
    column = f"excess_after_cost_{int(horizon)}d"
    matured = f"matured_{int(horizon)}d"
    frame = sql_to_df(
        f"""
        SELECT gate,
               COUNT(*) FILTER (WHERE {matured}) AS matured_count,
               AVG({column}) FILTER (WHERE {matured}) AS avg_excess_after_cost,
               AVG(CASE WHEN {matured} AND {column} > 0 THEN 1.0 WHEN {matured} THEN 0.0 END) AS blocked_winner_rate
        FROM {TABLE_NAME}
        GROUP BY gate
        HAVING COUNT(*) FILTER (WHERE {matured}) >= %s
        ORDER BY AVG({column}) FILTER (WHERE {matured}) DESC NULLS LAST
        LIMIT %s
        """,
        params=(int(min_matured), int(limit)),
    )
    out: list[dict[str, Any]] = []
    for row in frame.itertuples(index=False):
        out.append(
            {
                "gate": row.gate,
                "horizon_days": int(horizon),
                "matured_count": int(row.matured_count or 0),
                "avg_excess_after_cost": None if row.avg_excess_after_cost is None else round(float(row.avg_excess_after_cost), 5),
                "blocked_winner_rate": None if row.blocked_winner_rate is None else round(float(row.blocked_winner_rate), 4),
                "reading": (
                    "blocking_winners_costs_alpha" if (row.avg_excess_after_cost or 0) > 0 else "earning_its_keep"
                ),
            }
        )
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Automatic regret ledger: enroll blocked BUYs, label forward excess, summarize per gate.")
    parser.add_argument("--date", default=None, help="Advisory date to enroll (default: latest candidates date)")
    parser.add_argument("--backfill-days", type=int, default=0, help="Also enroll every candidate date within N days")
    parser.add_argument("--skip-enroll", action="store_true")
    parser.add_argument("--skip-label", action="store_true")
    parser.add_argument("--horizon", type=int, default=10, choices=list(HORIZONS))
    parser.add_argument("--format", choices=["json", "text"], default="text")
    args = parser.parse_args(argv)

    enrolled = 0
    if not args.skip_enroll:
        if args.backfill_days > 0:
            dates = sql_to_df(
                f"SELECT DISTINCT asof_date FROM {CANDIDATES_TABLE} WHERE asof_date > now() - interval '{int(args.backfill_days)} days' ORDER BY 1",
            )
            for value in dates["asof_date"].tolist():
                enrolled += enroll(value)
        else:
            target = args.date
            if target is None:
                latest = sql_to_df(f"SELECT MAX(asof_date) AS d FROM {CANDIDATES_TABLE}")
                target = latest.iloc[0]["d"] if not latest.empty else None
            enrolled = enroll(target) if target is not None else 0

    labels = {"pending": 0, "updated": 0}
    if not args.skip_label:
        labels = label_outcomes()

    summary = summarize(horizon=args.horizon)
    payload = {"enrolled": enrolled, **labels, "horizon_days": args.horizon, "gates": summary, "broker_execution_allowed": False}
    if args.format == "json":
        print(json.dumps(payload, indent=2, default=str))
    else:
        print(f"[regret_ledger] enrolled={enrolled} pending={labels['pending']} labeled={labels['updated']} horizon={args.horizon}d")
        for gate in summary:
            print(
                f"  {gate['gate']:<44} n={gate['matured_count']:>4} "
                f"avg_excess={gate['avg_excess_after_cost']:+.2%} blocked_winners={gate['blocked_winner_rate']:.0%} {gate['reading']}"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
