"""North-star scorecard -- does any of this beat just owning NIFTY? (spec discovery_engine.md 8.4)

Every sub-score, edge, and archetype is a means to ONE end: net portfolio return vs buy-and-hold
NIFTY, after realistic (name-specific) costs. The spec was silent on this; the discovery machinery
can look impressive while the whole thing quietly trails the index. This builds the honest top-line.

Method -- a deliberately simple, non-overlapping paper portfolio (no leverage, no overlap artifacts):
every HOLD_DAYS trading days is a rebalance. On each rebalance date, take the archetype picks available
that day (union across archetypes, one position per symbol), equal-weight, hold exactly HOLD_DAYS,
realize net of the name-specific round-trip cost, and chain the periods into an equity curve. Periods
with no picks sit in cash (0% -- an honest drag, not a hidden benchmark substitution). Delisted picks
are filled at the conservative DELIST_RETURN, never silently dropped. NIFTY buy-and-hold over the same
span is the yardstick. Review-only; reads price, writes nothing, no LLM.
"""
from __future__ import annotations

import argparse
import json
import os
from typing import Any

import pandas as pd

from advisory.archetype_backtest import (
    ARCHETYPE_FILTERS,
    DELIST_RETURN,
    N_WINDOWS,
    _load_benchmark,
    _load_picks,
)
from advisory.cost_model import describe as describe_cost
from advisory.cost_model import round_trip_cost_fraction
from utils.db import sql_to_df

HOLD_DAYS = int(os.getenv("NORTH_STAR_HOLD_DAYS", "20"))  # must be one of the loaded horizons (5/10/20)
MAX_NAMES = int(os.getenv("NORTH_STAR_MAX_NAMES", "20"))  # equal-weight cap per rebalance
HORIZONS = (5, 10, 20)
GATED_STATES = tuple(s for s in os.getenv("NORTH_STAR_GATED_STATES", "PASS_NOW").split(",") if s)


def _load_gated_picks(states: tuple[str, ...] = GATED_STATES) -> pd.DataFrame:
    """The gated recommendation set: candidates the live funnel flagged, priced point-in-time.

    Joins advisory_candidates (the scored, gated candidate history) to OHLCV for the entry close, the
    same avgvol20*close turnover the lane backtest uses (so cost is comparable), and forward closes.
    """
    return sql_to_df(
        """
        WITH base AS (
            -- adj_close (advisory.price_adjustment) so splits/bonuses are neutralised; EQ+BE continuity;
            -- turnover from raw price (split-invariant); `ambiguous` CA flag = probable data error.
            SELECT o.symbol, o.date, a.adj_close,
                   AVG(o.volume) OVER w20 * o.close AS avg_turnover_inr,
                   LEAD(a.adj_close, 5)  OVER w AS fwd5,
                   LEAD(a.adj_close, 10) OVER w AS fwd10,
                   LEAD(a.adj_close, 20) OVER w AS fwd20,
                   MAX(CASE WHEN a.ca_flag = 'ambiguous' THEN 1 ELSE 0 END) OVER wfwd AS ambiguous_ahead
            FROM nseindia_ohlcv o
            JOIN advisory_adjusted_ohlcv_daily a ON a.symbol = o.symbol AND a.date = o.date
            WHERE o.series IN ('EQ', 'BE')
            WINDOW w AS (PARTITION BY o.symbol ORDER BY o.date),
                   w20 AS (PARTITION BY o.symbol ORDER BY o.date ROWS BETWEEN 20 PRECEDING AND 1 PRECEDING),
                   wfwd AS (PARTITION BY o.symbol ORDER BY o.date ROWS BETWEEN 1 FOLLOWING AND 20 FOLLOWING)
        )
        SELECT c.symbol, c.asof_date AS date, b.adj_close AS close, b.avg_turnover_inr,
               b.fwd5, b.fwd10, b.fwd20, b.ambiguous_ahead
        FROM advisory_candidates c
        JOIN base b ON b.symbol = c.symbol AND b.date = c.asof_date
        WHERE c.candidate_state = ANY(%(states)s)
          -- exclude research_only label-harvesting rows (they flood PASS_NOW at a low bar and are not
          -- recommendations). Typed separator; NULL (pre-migration rows) treated as live.
          AND COALESCE(c.research_only, FALSE) = FALSE
        ORDER BY c.asof_date, c.symbol
        """,
        params={"states": list(states)},
    )


def _load_all_picks() -> pd.DataFrame:
    frames = []
    for archetype in ARCHETYPE_FILTERS:
        df = _load_picks(archetype)
        if not df.empty:
            frames.append(df.assign(archetype=archetype))
    if not frames:
        return pd.DataFrame()
    picks = pd.concat(frames, ignore_index=True)
    picks["date"] = pd.to_datetime(picks["date"], utc=True, errors="coerce").dt.normalize()
    # one position per (symbol, date) even if it qualifies under several archetypes
    return picks.drop_duplicates(subset=["symbol", "date"]).reset_index(drop=True)


def _period_return(seg: pd.DataFrame, h: int, mat_lookup: dict[Any, Any]) -> tuple[float, int, int, int]:
    """Equal-weight net return for one rebalance date's picks. Returns (ret, n_held, n_delisted, n_immature)."""
    seg = seg.copy()
    fwd = seg[f"fwd{h}"]
    mat = seg["date"].map(mat_lookup)
    immature = fwd.isna() & mat.isna()
    n_immature = int(immature.sum())
    held = seg[~immature]
    if held.empty:
        return 0.0, 0, 0, n_immature
    if len(held) > MAX_NAMES:  # highest-turnover names first (most tradeable), deterministic
        held = held.sort_values("avg_turnover_inr", ascending=False).head(MAX_NAMES)
    delisted = held[f"fwd{h}"].isna()
    cost = round_trip_cost_fraction(held["avg_turnover_inr"].to_numpy(dtype="float64"))
    raw = (held[f"fwd{h}"] / held["close"] - 1.0).where(~delisted, DELIST_RETURN)
    net = (raw - cost).astype("float64")
    return float(net.mean()), int(len(held)), int(delisted.sum()), n_immature


def run_north_star(*, hold_days: int | None = None, n_windows: int | None = None) -> dict[str, Any]:
    h = int(hold_days if hold_days is not None else HOLD_DAYS)
    n_windows = int(n_windows if n_windows is not None else N_WINDOWS)
    bench = _load_benchmark()
    picks = _load_all_picks()
    if bench.empty or picks.empty:
        return {"hold_days": h, "windows": [], "overall": None}
    bench["date"] = pd.to_datetime(bench["date"], utc=True, errors="coerce").dt.normalize()
    bench = bench.sort_values("date").reset_index(drop=True)
    trading_days = sorted(bench["date"].dropna().unique())
    pos = {d: i for i, d in enumerate(trading_days)}
    mat_lookup = {d: (trading_days[i + h] if i + h < len(trading_days) else pd.NaT) for d, i in pos.items()}
    bench_close = dict(zip(bench["date"], bench[f"bench_fwd{h}"] / bench["bench_close"] - 1.0))

    # non-overlapping rebalance dates that have a full forward window
    rebal = [d for d in trading_days[::h] if pd.notna(mat_lookup.get(d))]
    by_date = {d: g for d, g in picks.groupby("date")}

    periods: list[dict[str, Any]] = []
    for d in rebal:
        seg = by_date.get(d)
        if seg is None or seg.empty:
            strat_ret, n_held, n_del, n_imm = 0.0, 0, 0, 0
        else:
            strat_ret, n_held, n_del, n_imm = _period_return(seg, h, mat_lookup)
        periods.append({"date": d, "strat_ret": strat_ret, "bench_ret": float(bench_close.get(d, 0.0) or 0.0),
                        "n_held": n_held, "delisted": n_del, "immature_skipped": n_imm})

    def _chain(rows: list[dict[str, Any]], key: str) -> float:
        eq = 1.0
        for r in rows:
            eq *= (1.0 + r[key])
        return eq - 1.0

    def _summ(rows: list[dict[str, Any]], label: str) -> dict[str, Any]:
        strat = _chain(rows, "strat_ret")
        ben = _chain(rows, "bench_ret")
        return {"label": label, "rebalances": len(rows),
                "strat_net_return_pct": round(strat * 100, 2),
                "nifty_return_pct": round(ben * 100, 2),
                "excess_pct": round((strat - ben) * 100, 2),
                "cash_periods": sum(1 for r in rows if r["n_held"] == 0),
                "avg_names": round(sum(r["n_held"] for r in rows) / len(rows), 1) if rows else 0.0,
                "delisted": sum(r["delisted"] for r in rows)}

    windows: list[dict[str, Any]] = []
    if periods:
        edges = [i * len(periods) // n_windows for i in range(n_windows)] + [len(periods)]
        for wi in range(n_windows):
            chunk = periods[edges[wi]:edges[wi + 1]]
            if chunk:
                windows.append(_summ(chunk, f"W{wi + 1}:{pd.Timestamp(chunk[0]['date']).date()}"))
    return {"hold_days": h, "cost_model": describe_cost(),
            "overall": _summ(periods, "overall") if periods else None, "windows": windows}


def run_gated_scorecard(*, states: tuple[str, ...] | None = None) -> dict[str, Any]:
    """Per-trade scorecard for the GATED recommendation set (what the live funnel actually flagged).

    The gated set is sparse and irregular (recommendations land on the days they land, not on a fixed
    grid), so a rebalance-grid equity curve would be mostly cash and misleading. The honest measure is
    per-trade: enter each recommendation on its asof_date, hold h days, net of the name-specific cost,
    and compare to NIFTY over the SAME entry->exit interval. Delist-honest; immature (tail) trades are
    excluded, not filled. Reports per horizon since the sample is small and horizon choice matters.
    """
    states = tuple(states) if states else GATED_STATES
    bench = _load_benchmark()
    picks = _load_gated_picks(states)
    meta = {"states": list(states), "cost_model": describe_cost()}
    if bench.empty or picks.empty:
        return {**meta, "n_recommendations": 0, "days": 0, "horizons": {}}
    bench["date"] = pd.to_datetime(bench["date"], utc=True, errors="coerce").dt.normalize()
    bench = bench.sort_values("date").reset_index(drop=True)
    picks["date"] = pd.to_datetime(picks["date"], utc=True, errors="coerce").dt.normalize()
    trading_days = sorted(bench["date"].dropna().unique())
    pos = {d: i for i, d in enumerate(trading_days)}
    df = picks.merge(bench, on="date", how="inner")

    horizons: dict[int, Any] = {}
    for h in HORIZONS:
        mat = df["date"].map({d: (trading_days[i + h] if i + h < len(trading_days) else pd.NaT) for d, i in pos.items()})
        missing = df[f"fwd{h}"].isna()
        immature = int((missing & mat.isna()).sum())
        keep = df[~(missing & mat.isna())].copy()
        if keep.empty:
            horizons[h] = {"trades": 0, "immature_skipped": immature}
            continue
        # splits/bonuses are already neutralised (returns on adj_close). Drop only picks with an
        # `ambiguous` CA flag in the forward window (unsnappable step = probable data error).
        ca = keep["ambiguous_ahead"].fillna(0).astype(bool) if "ambiguous_ahead" in keep.columns else pd.Series(False, index=keep.index)
        n_ca = int(ca.sum())
        keep = keep[~ca].copy()
        if keep.empty:
            horizons[h] = {"trades": 0, "immature_skipped": immature, "corp_action_skipped": n_ca}
            continue
        keep["_delisted"] = keep[f"fwd{h}"].isna()
        cost = round_trip_cost_fraction(keep["avg_turnover_inr"].to_numpy(dtype="float64"))
        raw = (keep[f"fwd{h}"] / keep["close"] - 1.0).where(~keep["_delisted"], DELIST_RETURN)
        keep["net"] = (raw - cost).astype("float64")
        keep["bench"] = keep[f"bench_fwd{h}"] / keep["bench_close"] - 1.0
        keep["excess"] = keep["net"] - keep["bench"]
        # Per-trade counts massively OVERSTATE power: 300+ trades over ~28 days heavily overlap and
        # share the same market days (spec 8.3). Collapse to one observation per decision day and
        # report that honest sample size + a day-level t on the excess. Even this overstates (adjacent
        # days overlap too), so read t as a soft ceiling, not a p-value.
        day_excess = keep.groupby("date")["excess"].mean()
        n_days = int(len(day_excess))
        day_std = float(day_excess.std(ddof=1)) if n_days > 1 else 0.0
        day_t = round(float(day_excess.mean()) / (day_std / (n_days ** 0.5)), 2) if day_std > 0 else None
        horizons[h] = {
            "trades": int(len(keep)),
            "mean_net_pct": round(float(keep["net"].mean()) * 100, 2),
            "nifty_mean_pct": round(float(keep["bench"].mean()) * 100, 2),
            "mean_excess_pct": round(float(keep["excess"].mean()) * 100, 2),
            "pct_positive": round(float((keep["net"] > 0).mean()) * 100, 1),
            "pct_beat_nifty": round(float((keep["excess"] > 0).mean()) * 100, 1),
            "mean_cost_pct": round(float(cost.mean()) * 100, 2),
            "delisted": int(keep["_delisted"].sum()),
            "immature_skipped": immature,
            "corp_action_skipped": n_ca,
            # honest, overlap-collapsed view:
            "decision_days": n_days,
            "days_excess_positive": int((day_excess > 0).sum()),
            "day_mean_excess_pct": round(float(day_excess.mean()) * 100, 2),
            "day_level_t": day_t,
        }
    return {**meta, "n_recommendations": int(len(picks)),
            "days": int(picks["date"].nunique()),
            "date_lo": str(pd.Timestamp(picks["date"].min()).date()),
            "date_hi": str(pd.Timestamp(picks["date"].max()).date()),
            "horizons": horizons}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="North-star: net strategy return vs buy-and-hold NIFTY after realistic costs.")
    parser.add_argument("--source", choices=["lane", "gated"], default="lane",
                        help="lane = raw archetype population (equity curve); gated = live recommendation set (per-trade).")
    parser.add_argument("--hold-days", type=int, default=None)
    parser.add_argument("--windows", type=int, default=None)
    parser.add_argument("--states", default=None, help="gated only: comma-separated candidate_state values (default PASS_NOW).")
    parser.add_argument("--format", choices=["text", "json"], default="text")
    args = parser.parse_args(argv)

    if args.source == "gated":
        states = tuple(s for s in args.states.split(",") if s) if args.states else None
        out = run_gated_scorecard(states=states)
        if args.format == "json":
            print(json.dumps(out, indent=2, default=str))
            return 0
        if not out["horizons"]:
            print(f"[north_star] gated set {out['states']} is EMPTY -- the live funnel emitted no such "
                  "recommendations to score. That absence IS the finding (spec discovery_engine.md 8.1/8.5).")
            return 0
        print(f"[north_star] GATED recommendation set states={out['states']}  n={out['n_recommendations']} "
              f"over {out['days']} days ({out['date_lo']} -> {out['date_hi']}); per-trade, after name-specific cost")
        print(f"    {'horizon':>7} {'trades':>6} {'net':>8} {'nifty':>8} {'excess':>8} {'>0':>6} {'beat':>6} {'cost':>6} {'delist':>6}")
        for h in HORIZONS:
            r = out["horizons"].get(h, {})
            if not r.get("trades"):
                continue
            print(f"    {h:>6}d {r['trades']:>6} {r['mean_net_pct']:>+7.2f}% {r['nifty_mean_pct']:>+7.2f}% "
                  f"{r['mean_excess_pct']:>+7.2f}% {r['pct_positive']:>5.0f}% {r['pct_beat_nifty']:>5.0f}% "
                  f"{r['mean_cost_pct']:>5.2f}% {r['delisted']:>6}")
        print("  (excess = recommendation minus NIFTY over the same hold, after cost; NEGATIVE = worse than the index)")
        print("  -- overlap-collapsed (the HONEST sample size; per-trade N overstates power, spec 8.3):")
        for h in HORIZONS:
            r = out["horizons"].get(h, {})
            if not r.get("trades"):
                continue
            t = r["day_level_t"]
            print(f"    {h:>6}d  decision-days={r['decision_days']:>2}  days_excess>0={r['days_excess_positive']}/{r['decision_days']}"
                  f"  day-mean-excess={r['day_mean_excess_pct']:+.2f}%  day-level t={'n/a' if t is None else f'{t:.2f}'}")
        print("  NOTE: PASS_NOW is the loosest proxy -- the STRICT gated BUY set (entry-confirmed / BUY action) is EMPTY.")
        print("  READ: single ~2.5-month regime; day-level t is a soft ceiling (adjacent days overlap). Encouraging, NOT proven.")
        return 0

    out = run_north_star(hold_days=args.hold_days, n_windows=args.windows)
    if args.format == "json":
        print(json.dumps(out, indent=2, default=str))
        return 0
    o = out["overall"]
    if not o:
        print("[north_star] no picks/benchmark -- nothing to score")
        return 0
    print(f"[north_star] hold={out['hold_days']}d  net strategy vs buy-and-hold NIFTY (after name-specific cost)")
    hdr = f"    {'window':<16} {'rebal':>5} {'strat_net':>10} {'nifty':>8} {'excess':>8} {'cash':>5} {'avg_n':>6} {'delist':>6}"
    print(hdr)
    for w in out["windows"] + [o]:
        print(f"    {w['label']:<16} {w['rebalances']:>5} {w['strat_net_return_pct']:>+9.2f}% "
              f"{w['nifty_return_pct']:>+7.2f}% {w['excess_pct']:>+7.2f}% {w['cash_periods']:>5} "
              f"{w['avg_names']:>6} {w['delisted']:>6}")
    print("  (excess = strategy minus NIFTY, after cost; NEGATIVE = you would be better off owning the index)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
