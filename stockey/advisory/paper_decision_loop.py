"""Coherent paper decision loop -- a clean, forward-tracked path that finally generates real outcomes.

The live funnel produces no BUYs (architectural: no candidate->approved->BUY promotion bridge, spec 9/12),
so it yields no forward track record to learn from. This loop deliberately BYPASSES the funnel and commits
simple, documented paper decisions, then scores them forward vs NIFTY after realistic cost. It is built
only from what survived the whole arc:

  SELECT  -- relative strength (the one cross-regime-robust signal, spec 9), computed point-in-time from
            OHLCV (the persisted RS panel keeps only ~4 days), liquid names only.
  SIZE    -- advisory.portfolio_risk.size_position (vol-targeted, capped) x the crash-floor multiplier.
  COST    -- advisory.cost_model.round_trip_cost_fraction (name-specific, not a flat bps).
  SCORE   -- per-trade net return vs NIFTY over the same hold, overlap-collapsed to decision-days (8.3).

Research-only authority: writes ONE table, never the action queue, never broker. Point-in-time (features
up to the decision date; forward window is the outcome). Excludes research_only label-harvesting rows by
construction (it does not read candidates at all). NOT a proven strategy -- a documented ruleset whose job
is to accumulate honest forward outcomes.
"""
from __future__ import annotations

import argparse
import json
import os
from typing import Any

import numpy as np
import pandas as pd

from advisory.cost_model import describe as describe_cost
from advisory.cost_model import round_trip_cost_fraction
from advisory.north_star import _load_benchmark
from advisory.portfolio_risk import crash_floor_multiplier, size_position
from utils.db import sql_to_df, upsert_to_db
from utils.schema_migrations import apply_schema_migration

POLICY_VERSION = os.getenv("PAPER_LOOP_POLICY_VERSION", "rs_v1")
HOLD_DAYS = int(os.getenv("PAPER_LOOP_HOLD_DAYS", "20"))
RS_MIN_PERCENTILE = float(os.getenv("PAPER_LOOP_RS_MIN_PERCENTILE", "80"))
MAX_NAMES = int(os.getenv("PAPER_LOOP_MAX_NAMES", "20"))
MIN_TURNOVER_INR = float(os.getenv("PAPER_LOOP_MIN_TURNOVER_INR", "50000000"))  # 5cr
MIN_PRICE = float(os.getenv("PAPER_LOOP_MIN_PRICE", "30"))
CAPITAL_INR = float(os.getenv("PAPER_LOOP_CAPITAL_INR", "1000000"))  # 10 lakh notional book
SAMPLE_STEP_DAYS = int(os.getenv("PAPER_LOOP_SAMPLE_STEP_DAYS", "5"))
DELIST_RETURN = float(os.getenv("BACKTEST_DELIST_RETURN", "-0.5"))
# NB: splits/bonuses are handled by reading adj_close (advisory.price_adjustment); only an `ambiguous`
# CA flag (unsnappable step = probable data error) is excluded. No single-day-step guard needed here.

TABLE_NAME = "advisory_paper_decision_loop"
MIGRATION_ID = "20260713_advisory_paper_decision_loop_base"
SCHEMA_STATEMENTS = [
    f"""
    CREATE TABLE IF NOT EXISTS {TABLE_NAME} (
        asof_date TIMESTAMPTZ NOT NULL,
        symbol TEXT NOT NULL,
        policy_version TEXT NOT NULL,
        hold_days BIGINT NOT NULL,
        rs_percentile DOUBLE PRECISION,
        atr_pct DOUBLE PRECISION,
        avg_turnover_inr DOUBLE PRECISION,
        weight_pct DOUBLE PRECISION,
        risk_pct DOUBLE PRECISION,
        floor_multiplier DOUBLE PRECISION,
        entry_price DOUBLE PRECISION,
        exit_price DOUBLE PRECISION,
        cost_fraction DOUBLE PRECISION,
        net_return DOUBLE PRECISION,
        benchmark_return DOUBLE PRECISION,
        excess_return DOUBLE PRECISION,
        evaluation_status TEXT,
        delisted BOOLEAN,
        authority_scope TEXT,
        broker_execution_allowed BOOLEAN,
        load_ts TIMESTAMPTZ,
        UNIQUE (asof_date, symbol, policy_version, hold_days)
    )
    """,
]


def ensure_table() -> None:
    apply_schema_migration(
        migration_id=MIGRATION_ID,
        statements=SCHEMA_STATEMENTS,
        owner="advisory.paper_decision_loop",
        description="Research-only paper decision loop: RS-selected, vol-sized, crash-floored, scored vs NIFTY.",
        metadata={"tables": [TABLE_NAME], "workflow": "paper_decision_loop", "authority": "research_only"},
    )


def _load_panel(hold_days: int) -> pd.DataFrame:
    """Point-in-time panel on SPLIT-ADJUSTED prices. Returns/RS use adj_close (from advisory_adjusted_ohlcv_
    daily, derived by advisory.price_adjustment) so a split/bonus in the trailing or forward window is
    neutralised -- not counted as a fake move. Turnover and ATR% use raw prices (both split-invariant).
    EQ+BE is one continuous instrument (T2T migrations kept). A name with an `ambiguous` CA flag (possible
    data error, not adjusted) in the forward window is marked so the caller can leave it unscored."""
    return sql_to_df(
        """
        WITH base AS (
          SELECT o.symbol, o.date, o.close AS raw_close, a.adj_close,
                 AVG(o.volume) OVER w20 * o.close AS avg_turnover_inr,
                 AVG((o.high - o.low) / NULLIF(o.close, 0)) OVER w20 AS atr_pct,
                 LAG(a.adj_close, 63)  OVER w AS ac63,
                 LAG(a.adj_close, 126) OVER w AS ac126,
                 LAG(a.adj_close, 252) OVER w AS ac252,
                 LEAD(a.adj_close, %(hold)s) OVER w AS adj_fwd,
                 LEAD(o.date, %(hold)s)      OVER w AS fwd_date,
                 COUNT(*) OVER w252 AS nprev,
                 MAX(CASE WHEN a.ca_flag = 'ambiguous' THEN 1 ELSE 0 END) OVER wfwd AS ambiguous_ahead
          FROM nseindia_ohlcv o
          JOIN advisory_adjusted_ohlcv_daily a ON a.symbol = o.symbol AND a.date = o.date
          WHERE o.series IN ('EQ', 'BE')
          WINDOW w AS (PARTITION BY o.symbol ORDER BY o.date),
                 w20 AS (PARTITION BY o.symbol ORDER BY o.date ROWS BETWEEN 20 PRECEDING AND 1 PRECEDING),
                 w252 AS (PARTITION BY o.symbol ORDER BY o.date ROWS BETWEEN 252 PRECEDING AND 1 PRECEDING),
                 wfwd AS (PARTITION BY o.symbol ORDER BY o.date ROWS BETWEEN 1 FOLLOWING AND %(hold)s FOLLOWING)
        )
        SELECT symbol, date, adj_close AS close, avg_turnover_inr, atr_pct,
               adj_fwd AS fwd, fwd_date, ambiguous_ahead,
               adj_close / NULLIF(ac63, 0)  - 1 AS ret63,
               adj_close / NULLIF(ac126, 0) - 1 AS ret126,
               adj_close / NULLIF(ac252, 0) - 1 AS ret252
        FROM base
        WHERE nprev >= 120 AND ac63 IS NOT NULL AND raw_close >= %(min_price)s
          AND avg_turnover_inr >= %(min_turnover)s AND atr_pct > 0
        ORDER BY date, symbol
        """,
        params={"hold": int(hold_days), "min_price": MIN_PRICE, "min_turnover": MIN_TURNOVER_INR},
    )


def _rs_percentile(day: pd.DataFrame) -> pd.Series:
    """Cross-sectional RS percentile for one date: blend of 63/126/252d return ranks (relative_strength
    spirit). Uses whichever horizons are available -- early in a short history the 252d return is missing
    for every name, so blending only the present components (skipna) keeps RS defined instead of all-NaN."""
    ranks = pd.DataFrame({
        "r63": day["ret63"].rank(pct=True),
        "r126": day["ret126"].rank(pct=True),
        "r252": day["ret252"].rank(pct=True),
    })
    return ranks.mean(axis=1).rank(pct=True) * 100.0  # mean skips NaN components by default


def run_paper_loop(*, hold_days: int | None = None, panel: pd.DataFrame | None = None,
                   bench: pd.DataFrame | None = None) -> dict[str, Any]:
    h = int(hold_days if hold_days is not None else HOLD_DAYS)
    panel = _load_panel(h) if panel is None else panel
    bench = _load_benchmark() if bench is None else bench
    if panel.empty or bench.empty:
        return {"policy_version": POLICY_VERSION, "hold_days": h, "decisions": pd.DataFrame(), "horizon": {}}
    panel["date"] = pd.to_datetime(panel["date"], utc=True, errors="coerce").dt.normalize()
    if "fwd_date" in panel.columns:
        panel["fwd_date"] = pd.to_datetime(panel["fwd_date"], utc=True, errors="coerce").dt.normalize()
    bench["date"] = pd.to_datetime(bench["date"], utc=True, errors="coerce").dt.normalize()
    bench = bench.sort_values("date").reset_index(drop=True)
    trading_days = sorted(bench["date"].dropna().unique())
    pos = {d: i for i, d in enumerate(trading_days)}
    # Benchmark over the stock's ACTUAL holding window (entry -> the stock's own exit date), so a gapped/
    # halted name whose 20 rows span more calendar days is compared to NIFTY over the SAME span, not
    # NIFTY's shorter 20-row window (which would inflate the excess). Falls back to the row-offset return.
    bench_close_by_date = dict(zip(bench["date"], bench["bench_close"]))
    bench_ret_rowoffset = dict(zip(bench["date"], bench[f"bench_fwd{h}"] / bench["bench_close"] - 1.0))
    floor = dict(zip(bench["date"], crash_floor_multiplier(bench["bench_close"]).to_numpy()))
    mat = {d: (trading_days[i + h] if i + h < len(trading_days) else pd.NaT) for d, i in pos.items()}

    sampled = set(trading_days[::max(1, SAMPLE_STEP_DAYS)])
    rows: list[dict[str, Any]] = []
    now = pd.Timestamp.utcnow()
    for date, day in panel[panel["date"].isin(sampled)].groupby("date"):
        if date not in pos:
            continue
        day = day.copy()
        day["rs_percentile"] = _rs_percentile(day)
        picks = day[day["rs_percentile"] >= RS_MIN_PERCENTILE].sort_values("rs_percentile", ascending=False).head(MAX_NAMES)
        if picks.empty:
            continue
        matured = pd.notna(mat.get(date))
        floor_mult = float(floor.get(date, 1.0))
        entry_bench = bench_close_by_date.get(date)
        for pick in picks.itertuples(index=False):
            has_fwd = np.isfinite(pick.fwd)
            if not has_fwd and not matured:
                continue  # decision too recent to have matured -- a live pending decision, nothing to score yet
            # NIFTY return over the stock's ACTUAL window (entry -> its own exit date); row-offset fallback.
            fwd_date = getattr(pick, "fwd_date", None)
            exit_bench = bench_close_by_date.get(fwd_date) if pd.notna(fwd_date) else None
            if entry_bench and exit_bench:
                b_ret = float(exit_bench) / float(entry_bench) - 1.0
            else:
                b_ret = float(bench_ret_rowoffset.get(date, 0.0) or 0.0)
            sizing = size_position(CAPITAL_INR, float(pick.atr_pct))
            cost = round_trip_cost_fraction(float(pick.avg_turnover_inr))
            base = {
                "asof_date": date, "symbol": pick.symbol, "policy_version": POLICY_VERSION, "hold_days": h,
                "rs_percentile": round(float(pick.rs_percentile), 2), "atr_pct": round(float(pick.atr_pct), 4),
                "avg_turnover_inr": float(pick.avg_turnover_inr), "weight_pct": sizing["weight_pct"] * floor_mult,
                "risk_pct": sizing["risk_pct"], "floor_multiplier": floor_mult, "entry_price": float(pick.close),
                "cost_fraction": round(float(cost), 5), "benchmark_return": round(b_ret, 5),
                "authority_scope": "research_only", "broker_execution_allowed": False, "load_ts": now,
            }
            if not has_fwd:
                # Matured window, but THIS name's OHLCV ends before the exit (coverage gap OR real delisting).
                # We CANNOT tell which, so do NOT fabricate a return: filling -50% wrongly penalizes a data gap,
                # and silently dropping it hides a possible blowup (survivorship). Record it unscored; the
                # scorecard reports it separately and brackets the true result with a worst-case fill.
                rows.append({**base, "exit_price": None, "net_return": None, "excess_return": None,
                             "evaluation_status": "unscored_data_ends", "delisted": False})
                continue
            if bool(getattr(pick, "ambiguous_ahead", 0)):
                # split/bonus in the hold is already neutralised (returns are on adj_close). Only an
                # `ambiguous` CA flag remains -- a circuit-breaching step that did NOT snap to a round
                # ratio, i.e. a probable DATA ERROR. Leave it unscored rather than trust a bad price.
                rows.append({**base, "exit_price": float(pick.fwd), "net_return": None, "excess_return": None,
                             "evaluation_status": "unscored_corporate_action", "delisted": False})
                continue
            net = float(pick.fwd) / float(pick.close) - 1.0 - float(cost)
            rows.append({**base, "exit_price": float(pick.fwd), "net_return": round(net, 5),
                         "excess_return": round(net - b_ret, 5), "evaluation_status": "evaluated", "delisted": False})
    decisions = pd.DataFrame(rows)
    return {"policy_version": POLICY_VERSION, "hold_days": h, "cost_model": describe_cost(),
            "decisions": decisions, "horizon": _summarize(decisions)}


def _summarize(decisions: pd.DataFrame) -> dict[str, Any]:
    if decisions.empty:
        return {}
    ev = decisions[decisions["evaluation_status"] == "evaluated"].copy()
    status = decisions["evaluation_status"]
    unscored_data = int((status == "unscored_data_ends").sum())
    unscored_ca = int((status == "unscored_corporate_action").sum())
    unscored = unscored_data + unscored_ca
    if ev.empty:
        return {"trades": 0, "unscored_data_ends": unscored_data, "unscored_corporate_action": unscored_ca}
    ex = ev["excess_return"]
    day = ev.groupby("asof_date")["excess_return"].mean()
    n_days = int(len(day))
    day_std = float(day.std(ddof=1)) if n_days > 1 else 0.0
    # worst-case bound: fill ALL unscored names at DELIST_RETURN (as if real blowups) and re-mean. The
    # true result lies between the evaluated-only mean and this pessimistic bound.
    wc = np.concatenate([ex.to_numpy(), np.full(unscored, DELIST_RETURN) - float(decisions["benchmark_return"].mean())])
    return {
        "trades": int(len(ev)),
        "decision_days": n_days,
        "mean_net_pct": round(float(ev["net_return"].mean()) * 100, 2),
        "mean_benchmark_pct": round(float(ev["benchmark_return"].mean()) * 100, 2),
        "mean_excess_pct": round(float(ex.mean()) * 100, 2),
        "pct_beat_nifty": round(float((ex > 0).mean()) * 100, 1),
        "days_excess_positive": int((day > 0).sum()),
        "day_mean_excess_pct": round(float(day.mean()) * 100, 2),
        "day_level_t": round(float(day.mean()) / (day_std / (n_days ** 0.5)), 2) if day_std > 0 else None,
        "avg_names_per_day": round(len(ev) / n_days, 1) if n_days else 0.0,
        "unscored_data_ends": unscored_data,
        "unscored_corporate_action": unscored_ca,
        "unscored_pct": round(unscored / (len(ev) + unscored) * 100, 1) if (len(ev) + unscored) else 0.0,
        "worst_case_mean_excess_pct": round(float(np.mean(wc)) * 100, 2),
    }


def persist(decisions: pd.DataFrame) -> None:
    if decisions.empty:
        return
    ensure_table()
    upsert_to_db(decisions, TABLE_NAME, unique_keys=["asof_date", "symbol", "policy_version", "hold_days"],
                 timescaledb_column="asof_date")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Research-only paper decision loop: RS-selected, vol-sized, crash-floored, scored vs NIFTY.")
    parser.add_argument("--hold-days", type=int, default=None)
    parser.add_argument("--persist", action="store_true", help="Write committed decisions to the paper table.")
    parser.add_argument("--format", choices=["text", "json"], default="text")
    args = parser.parse_args(argv)
    out = run_paper_loop(hold_days=args.hold_days)
    decisions = out["decisions"]
    if args.persist and not decisions.empty:
        persist(decisions)
    if args.format == "json":
        payload = {k: v for k, v in out.items() if k != "decisions"}
        payload["n_decisions"] = int(len(decisions))
        print(json.dumps(payload, indent=2, default=str))
        return 0
    s = out["horizon"]
    if not s:
        print(f"[paper_decision_loop] policy={out['policy_version']} hold={out['hold_days']}d -- no matured decisions to score")
        return 0
    print(f"[paper_decision_loop] policy={out['policy_version']} hold={out['hold_days']}d  RS>={RS_MIN_PERCENTILE:.0f}pct, "
          f"top {MAX_NAMES}, liquid>={MIN_TURNOVER_INR/1e7:.0f}cr; per-trade, after name-specific cost (research-only)")
    print(f"    evaluated trades={s['trades']} over {s['decision_days']} decision-days, avg {s['avg_names_per_day']}/day")
    print(f"    net={s['mean_net_pct']:+.2f}%  nifty={s['mean_benchmark_pct']:+.2f}%  excess={s['mean_excess_pct']:+.2f}%  beat={s['pct_beat_nifty']:.0f}%")
    t = s["day_level_t"]
    print(f"    overlap-collapsed (honest): decision-days={s['decision_days']} days_excess>0={s['days_excess_positive']}/{s['decision_days']} "
          f"day-mean-excess={s['day_mean_excess_pct']:+.2f}% day-level t={'n/a' if t is None else f'{t:.2f}'}")
    print(f"    UNSCORED (NOT fabricated): data-ends={s['unscored_data_ends']}, corp-action-artifact={s['unscored_corporate_action']} "
          f"({s['unscored_pct']:.0f}% of picks); true excess between {s['mean_excess_pct']:+.2f}% (exclude) and "
          f"{s['worst_case_mean_excess_pct']:+.2f}% (all-blowup bound)")
    print("    (excess = paper decision minus NIFTY over the same hold, after cost; NOT a proven strategy -- accumulating honest forward outcomes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
