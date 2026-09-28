"""Forward records for the fundamental families (TODO C6 quality, C8 value; 2026-09-28).

No backtest is possible -- fundamental history starts with the dated snapshot (TODO C4,
fundamentals/collectors/fundamentals_snapshot.py, 2026-09-27) -- so each family is a
FORWARD record: a frozen rule, applied monthly, whose holdings are written down with the
reference price at the time. In a year the record answers whether the family earns its
keep here; until then it is evidence being collected, not a result. Pre-registered in
systrader research/LEDGER.md (rows 46-47) under docs/RESEARCH_PROTOCOL.md.

Rules (operator, 2026-09-28) -- FROZEN. A change is a new version and a new record, never
an edit (the stored spec is compared on every run and a mismatch fails loudly):

  quality v1: operating companies in the universe, profitable in each of the last 2 years,
    positive 3-year free cash flow; score = mean percentile rank of 5-year average ROCE
    (high), 5-year average ROE (high), debt/equity (low).
  value v1:  the top half of quality v1's eligible set by quality score; score = mean
    percentile rank of earnings yield (high) and EV/EBIT (low, positive only).
  both: top 30 equal weight, rebalanced on the first run of each month; a held name is kept
    until it falls outside the top 60 (fewer trades, less short-term tax -- TODO A1/A2).

Inputs are read point-in-time: the newest snapshot on or before the rebalance date, and the
universe run on or before it. Reference price = latest adjusted close on or before it.

    python -m fundamentals.screens.forward_tracks            # rebalance any track that is due
    python -m fundamentals.screens.forward_tracks --report   # returns so far vs the universe
"""

from __future__ import annotations

import argparse
import json

import pandas as pd

from utils.db import db_session, sql_to_df, upsert_to_db

SYNC_SOURCE_NAME = "fundamentals.screens.forward_tracks"
SPEC_TABLE = "fundamentals_forward_track_spec"
HOLDING_TABLE = "fundamentals_forward_track_holding"
STOCKEY_RUN_STATE: dict[str, object] = {}

TRACKS: dict[str, dict] = {
    "quality": {"version": 1, "start": "2026-10-01", "top_n": 30, "keep_within": 60, "rebalance": "monthly",
                "universe": "l1_universe latest run on/before date, group=operating",
                "eligible": "profit_y1>0, profit_y2>0, fcf_3y_total>0, roce_5y/roe_5y/debt_to_equity present",
                "score": "mean pct rank: roce_5y_avg_pct high, roe_5y_avg_pct high, debt_to_equity low"},
    "value": {"version": 1, "start": "2026-10-01", "top_n": 30, "keep_within": 60, "rebalance": "monthly",
              "universe": "quality v1 eligible set, quality score >= its median",
              "eligible": "earnings_yield_pct present, ev_to_ebit > 0",
              "score": "mean pct rank: earnings_yield_pct high, ev_to_ebit low"},
}

_SPEC_DDL = f"""
    CREATE TABLE IF NOT EXISTS {SPEC_TABLE} (
        track TEXT NOT NULL, version INTEGER NOT NULL, spec_json TEXT NOT NULL,
        frozen_at TIMESTAMPTZ NOT NULL DEFAULT now(), PRIMARY KEY (track, version)
    )
"""
_HOLDING_DDL = f"""
    CREATE TABLE IF NOT EXISTS {HOLDING_TABLE} (
        track TEXT NOT NULL, version INTEGER NOT NULL, rebalance_date DATE NOT NULL,
        company_master_id TEXT NOT NULL, symbol TEXT, rank INTEGER, score DOUBLE PRECISION,
        weight DOUBLE PRECISION, action TEXT, ref_price DOUBLE PRECISION, ref_price_date DATE,
        snapshot_date DATE, load_ts TIMESTAMPTZ DEFAULT now(),
        PRIMARY KEY (track, version, rebalance_date, company_master_id)
    )
"""


def ensure_tables() -> None:
    with db_session() as (_, cur):
        cur.execute(_SPEC_DDL)
        cur.execute(_HOLDING_DDL)


def freeze_specs() -> None:
    """Store each spec on first sight; fail if a stored spec for the same version differs."""
    stored = sql_to_df(f"SELECT track, version, spec_json FROM {SPEC_TABLE}")
    have = {(r.track, int(r.version)): r.spec_json for r in stored.itertuples()} if not stored.empty else {}
    for track, spec in TRACKS.items():
        text = json.dumps(spec, sort_keys=True)
        key = (track, spec["version"])
        if key not in have:
            with db_session() as (_, cur):
                cur.execute(f"INSERT INTO {SPEC_TABLE} (track, version, spec_json) VALUES (%s, %s, %s)",
                            (track, spec["version"], text))
        elif have[key] != text:
            raise RuntimeError(f"forward track {track} v{spec['version']} changed after it was frozen; "
                               "a change is a new version (and a new record), never an edit")


# --- Inputs, point-in-time ---------------------------------------------------------------

def load_inputs(as_of) -> pd.DataFrame:
    """Operating companies in the universe run on/before `as_of`, joined to the newest
    fundamentals snapshot on/before `as_of` (within 10 days)."""
    d = str(pd.Timestamp(as_of).date())
    universe = sql_to_df(
        """
        SELECT company_id AS screener_company_id, ticker, metrics_json
          FROM fundamentals_l1_universe
         WHERE run_date = (SELECT max(run_date) FROM fundamentals_l1_universe WHERE run_date::date <= %s)
        """,
        params=(d,),
    )
    if universe.empty:
        return universe
    universe["group"] = universe["metrics_json"].map(lambda m: (json.loads(m) if m else {}).get("universe_group"))
    universe = universe[universe["group"].eq("operating")].drop(columns=["metrics_json"])
    snap = sql_to_df(
        """
        SELECT DISTINCT ON (screener_company_id) *
          FROM fundamentals_snapshot_daily
         WHERE as_of_date <= %s AND as_of_date >= %s::date - 10
         ORDER BY screener_company_id, as_of_date DESC
        """,
        params=(d, d),
    )
    if snap.empty:
        return snap
    return universe.merge(snap, on="screener_company_id", how="inner")


def _pct(s: pd.Series, *, high_is_good: bool) -> pd.Series:
    r = s.rank(pct=True, method="average")
    return r if high_is_good else 1 - r + (1 / len(s) if len(s) else 0)


def quality_eligible(df: pd.DataFrame) -> pd.DataFrame:
    need = ["profit_y1", "profit_y2", "fcf_3y_total", "roce_5y_avg_pct", "roe_5y_avg_pct", "debt_to_equity"]
    x = df.dropna(subset=[c for c in need if c in df.columns]).copy()
    if x.empty or any(c not in x.columns for c in need):
        return x.iloc[0:0]
    x = x[(x["profit_y1"] > 0) & (x["profit_y2"] > 0) & (x["fcf_3y_total"] > 0)].copy()
    x["score"] = (_pct(x["roce_5y_avg_pct"], high_is_good=True) + _pct(x["roe_5y_avg_pct"], high_is_good=True)
                  + _pct(x["debt_to_equity"], high_is_good=False)) / 3
    return x


def value_eligible(df: pd.DataFrame) -> pd.DataFrame:
    q = quality_eligible(df)
    if q.empty:
        return q
    x = q[q["score"] >= q["score"].median()].copy()
    x = x.dropna(subset=["earnings_yield_pct", "ev_to_ebit"])
    x = x[x["ev_to_ebit"] > 0].copy()
    if x.empty:
        return x
    x["score"] = (_pct(x["earnings_yield_pct"], high_is_good=True) + _pct(x["ev_to_ebit"], high_is_good=False)) / 2
    return x


SCORERS = {"quality": quality_eligible, "value": value_eligible}


def select_holdings(ranked: pd.DataFrame, held: set[str], *, top_n: int, keep_within: int) -> pd.DataFrame:
    """Keep held names still ranked within `keep_within`, then fill to `top_n` with the
    best-ranked new names. `ranked` needs company_master_id and score."""
    r = ranked.sort_values("score", ascending=False).reset_index(drop=True)
    r["rank"] = r.index + 1
    keep = r[r["company_master_id"].isin(held) & (r["rank"] <= keep_within)]
    fresh = r[~r["company_master_id"].isin(held)]
    chosen = pd.concat([keep, fresh.head(max(top_n - len(keep), 0))])
    chosen = chosen.sort_values("rank").head(max(top_n, len(keep)))
    chosen["action"] = chosen["company_master_id"].isin(held).map({True: "hold", False: "enter"})
    chosen["weight"] = 1.0 / len(chosen) if len(chosen) else 0.0
    return chosen


# --- Rebalance ---------------------------------------------------------------------------

def last_rebalance(track: str, version: int):
    df = sql_to_df(f"SELECT max(rebalance_date) AS d FROM {HOLDING_TABLE} WHERE track = %s AND version = %s",
                   params=(track, version))
    d = df.iloc[0]["d"] if not df.empty else None
    return None if d is None or pd.isna(d) else pd.Timestamp(d).date()


def is_due(last, today) -> bool:
    return last is None or (last.year, last.month) != (today.year, today.month)


def _ref_prices(symbols: list[str], as_of) -> dict[str, tuple[float, object]]:
    if not symbols:
        return {}
    df = sql_to_df(
        """
        SELECT DISTINCT ON (symbol) symbol, adj_close, date::date AS d
          FROM advisory_adjusted_ohlcv_daily
         WHERE symbol = ANY(%s) AND date <= %s::date + interval '1 day' AND date >= %s::date - interval '10 days'
         ORDER BY symbol, date DESC
        """,
        params=(symbols, str(as_of), str(as_of)),
    )
    return {r.symbol: (float(r.adj_close), r.d) for r in df.itertuples()}


def rebalance(track: str, as_of) -> dict[str, object]:
    spec = TRACKS[track]
    inputs = load_inputs(as_of)
    ranked = SCORERS[track](inputs) if not inputs.empty else inputs
    if ranked.empty:
        return {"track": track, "rebalanced": False, "reason": "no eligible companies (snapshot or universe missing)"}
    last = last_rebalance(track, spec["version"])
    held = set()
    if last is not None:
        prev = sql_to_df(f"SELECT company_master_id FROM {HOLDING_TABLE} WHERE track=%s AND version=%s AND rebalance_date=%s",
                         params=(track, spec["version"], last))
        held = set(prev["company_master_id"])
    chosen = select_holdings(ranked, held, top_n=spec["top_n"], keep_within=spec["keep_within"])
    chosen["symbol"] = chosen["company_master_id"].str.replace("nse:", "", regex=False)
    prices = _ref_prices(chosen["symbol"].tolist(), pd.Timestamp(as_of).date())
    rows = pd.DataFrame({
        "track": track, "version": spec["version"], "rebalance_date": pd.Timestamp(as_of).date(),
        "company_master_id": chosen["company_master_id"], "symbol": chosen["symbol"],
        "rank": chosen["rank"].astype(int), "score": chosen["score"].astype(float), "weight": chosen["weight"],
        "action": chosen["action"], "ref_price": chosen["symbol"].map(lambda s: prices.get(s, (None, None))[0]),
        "ref_price_date": chosen["symbol"].map(lambda s: prices.get(s, (None, None))[1]),
        "snapshot_date": chosen["as_of_date"],
    })
    upsert_to_db(rows, HOLDING_TABLE, unique_keys=["track", "version", "rebalance_date", "company_master_id"])
    return {"track": track, "rebalanced": True, "holdings": len(rows), "entered": int((rows["action"] == "enter").sum()),
            "kept": int((rows["action"] == "hold").sum()), "eligible": int(len(ranked)),
            "exited": sorted(held - set(rows["company_master_id"])), "missing_price": int(rows["ref_price"].isna().sum())}


def run_due(today=None) -> list[dict]:
    ensure_tables()
    freeze_specs()
    today = today or pd.Timestamp.now(tz="Asia/Kolkata").date()
    results = []
    for t, s in TRACKS.items():
        if today < pd.Timestamp(s["start"]).date():
            results.append({"track": t, "rebalanced": False, "reason": f"record starts {s['start']}"})
        elif is_due(last_rebalance(t, s["version"]), today):
            results.append(rebalance(t, today))
        else:
            results.append({"track": t, "rebalanced": False, "reason": "not due this month"})
    return results


# --- Report ------------------------------------------------------------------------------

def report(track: str) -> dict[str, object]:
    """Equal-weight return of each holding period, from its reference prices to the next
    rebalance (or today), against the equal-weight universe for the same period."""
    spec = TRACKS[track]
    h = sql_to_df(f"SELECT * FROM {HOLDING_TABLE} WHERE track=%s AND version=%s ORDER BY rebalance_date",
                  params=(track, spec["version"]))
    if h.empty:
        return {"track": track, "periods": []}
    dates = sorted(h["rebalance_date"].unique())
    periods = []
    for i, start in enumerate(dates):
        end = dates[i + 1] if i + 1 < len(dates) else pd.Timestamp.now(tz="Asia/Kolkata").date()
        held = h[h["rebalance_date"] == start].dropna(subset=["ref_price"])
        now = _ref_prices(held["symbol"].tolist(), end)
        rets = [now[s][0] / p - 1 for s, p in zip(held["symbol"], held["ref_price"]) if s in now]
        periods.append({"start": str(start), "end": str(end), "names": len(held),
                        "return_pct": round(100 * sum(rets) / len(rets), 2) if rets else None})
    return {"track": track, "version": spec["version"], "periods": periods}


def main(argv: list[str] | None = None) -> int:
    global STOCKEY_RUN_STATE
    parser = argparse.ArgumentParser(description="Fundamental forward records (quality, value).")
    parser.add_argument("--report", action="store_true")
    args = parser.parse_args(argv)
    if args.report:
        print(json.dumps([report(t) for t in TRACKS], default=str, indent=2), flush=True)
        return 0
    results = run_due()
    rebalanced = [r for r in results if r.get("rebalanced")]
    STOCKEY_RUN_STATE = {"source": SYNC_SOURCE_NAME, "rows": sum(r.get("holdings", 0) for r in rebalanced),
                         "rows_written": sum(r.get("holdings", 0) for r in rebalanced), "tracks": results,
                         "fallback_used": any(not r.get("rebalanced") and "no eligible" in str(r.get("reason")) for r in results),
                         "state_advanced": bool(rebalanced)}
    print(json.dumps({"status": "ok", **STOCKEY_RUN_STATE}, default=str), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
