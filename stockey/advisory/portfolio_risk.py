"""Portfolio & risk layer -- the asserted-but-unbuilt "absorb the leaks" (spec discovery_engine.md 8.3, 10).

The momentum->reversal->cycle arc concluded that selection edge is marginal and the cycle can't be
timed on this data, so the real leverage is NOT-LOSING: size by volatility, see the book's true
concentration, and don't be fully deployed into the next crash. The live funnel already has hard
CAPS (max 5 positions, 5%/name, 25%/sector, 100% total via LLM_DECISION_* env) but no volatility
SIZING, no correlation view, and no crash floor. This adds those three, as an ADVISORY/report layer:

  1. size_position   -- volatility-targeted sizing: risk a fixed % of capital to a stop, so a
                        high-vol name gets a smaller position. Clamped to the existing hard caps.
  2. book_metrics    -- portfolio volatility, effective-number-of-bets, diversification ratio:
                        exposes "one bet wearing many tickers" that per-name caps miss.
  3. crash_floor     -- a pre-committed (not fitted) market-state exposure multiplier: reduce when
                        the index is below its MA. Capital protection, NEVER sold as alpha (10: on
                        NIFTY it cut maxDD -15%->-9% but cost return via whipsaw in chop).

Binding guardrail: this layer DEFERS to the deterministic caps and can only be MORE conservative;
it never loosens a hard limit. Pure, point-in-time, review-only; writes nothing, no LLM.
"""
from __future__ import annotations

import argparse
import json
import os
from typing import Any

import numpy as np
import pandas as pd

# Hard ceilings owned by the deterministic layer (advisory sizing may only be <= these).
MAX_POSITION_PCT = float(os.getenv("LLM_DECISION_MAX_POSITION_PCT", "0.05"))
MAX_SECTOR_PCT = float(os.getenv("LLM_DECISION_MAX_SECTOR_EXPOSURE_PCT", "0.25"))
MAX_TOTAL_EXPOSURE_PCT = float(os.getenv("LLM_DECISION_MAX_TOTAL_EXPOSURE_PCT", "1.0"))

# Advisory sizing knobs (this layer's own).
RISK_BUDGET_PCT = float(os.getenv("PORTFOLIO_RISK_BUDGET_PCT", "0.0075"))   # capital risked per position to its stop
STOP_ATR_MULT = float(os.getenv("PORTFOLIO_RISK_STOP_ATR_MULT", "2.5"))     # stop distance in ATRs
MAX_HEAT_PCT = float(os.getenv("PORTFOLIO_RISK_MAX_HEAT_PCT", "0.06"))      # sum of open per-position risk budgets
CRASH_FLOOR_MA_DAYS = int(os.getenv("PORTFOLIO_RISK_CRASH_FLOOR_MA_DAYS", "50"))
CRASH_FLOOR_EXPOSURE = float(os.getenv("PORTFOLIO_RISK_CRASH_FLOOR_EXPOSURE", "0.30"))
TRADING_DAYS = 252


def size_position(
    capital_inr: float,
    atr_pct: float,
    *,
    risk_budget_pct: float = RISK_BUDGET_PCT,
    stop_atr_mult: float = STOP_ATR_MULT,
    max_position_pct: float = MAX_POSITION_PCT,
) -> dict[str, Any]:
    """Volatility-targeted position value. Risk ~risk_budget_pct of capital to a stop_atr_mult*ATR stop.

    ``atr_pct`` is ATR as a fraction of price (e.g. 0.04 = 4%). A wider-ATR (more volatile) name gets a
    smaller position for the same rupee risk. The result is clamped to the hard per-name cap, so this
    can only be MORE conservative than the deterministic limit, never less.
    """
    if not np.isfinite(atr_pct) or atr_pct <= 0 or capital_inr <= 0:
        return {"value_inr": 0.0, "weight_pct": 0.0, "risk_pct": 0.0, "capped_by": "untradeable"}
    stop_move = stop_atr_mult * atr_pct                      # fractional adverse move to the stop
    risk_value = capital_inr * risk_budget_pct / stop_move   # value s.t. a stop-out loses the risk budget
    cap_value = capital_inr * max_position_pct
    value = min(risk_value, cap_value)
    capped_by = "position_cap" if risk_value > cap_value else "risk_budget"
    return {
        "value_inr": round(value, 2),
        "weight_pct": round(value / capital_inr * 100, 2),
        "risk_pct": round(value * stop_move / capital_inr * 100, 3),  # actual capital-at-risk to the stop
        "capped_by": capped_by,
    }


def book_metrics(weights: dict[str, float], returns: pd.DataFrame) -> dict[str, Any]:
    """Portfolio-level risk from position weights + a daily-return panel (dates x symbols).

    effective_bets = 1/sum(w^2) (how many independent-sized bets the weights represent);
    diversification_ratio = weighted-avg single-name vol / portfolio vol (>1 => correlation is helping;
    ~1 => the names move together, i.e. one bet wearing many tickers -- which per-name caps do NOT catch).
    """
    syms = [s for s in weights if s in returns.columns]
    if not syms:
        return {"names": 0}
    w = np.array([weights[s] for s in syms], dtype="float64")
    w = w / w.sum() if w.sum() > 0 else w
    r = returns[syms].dropna(how="any")
    if len(r) < 5:
        return {"names": len(syms), "insufficient_history": True}
    cov = r.cov().to_numpy() * TRADING_DAYS
    vols = r.std(ddof=1).to_numpy() * np.sqrt(TRADING_DAYS)
    port_vol = float(np.sqrt(max(w @ cov @ w, 0.0)))
    wavg_vol = float(w @ vols)
    return {
        "names": len(syms),
        "effective_bets": round(float(1.0 / np.sum(w ** 2)), 2),
        "portfolio_vol_pct": round(port_vol * 100, 1),
        "weighted_avg_name_vol_pct": round(wavg_vol * 100, 1),
        "diversification_ratio": round(wavg_vol / port_vol, 2) if port_vol > 0 else None,
        "avg_pairwise_corr": round(float(r.corr().to_numpy()[np.triu_indices(len(syms), 1)].mean()), 2) if len(syms) > 1 else None,
    }


def portfolio_heat(risk_pcts: list[float]) -> dict[str, Any]:
    """Total open risk = sum of per-position capital-at-risk (each from size_position's risk_pct). A book
    can pass every per-name cap yet still risk too much in aggregate; MAX_HEAT_PCT bounds the sum."""
    total = sum(risk_pcts) / 100.0
    return {"total_heat_pct": round(total * 100, 2), "cap_pct": round(MAX_HEAT_PCT * 100, 2),
            "breaches_cap": total > MAX_HEAT_PCT}


def sector_exposure(weights: dict[str, float], sectors: dict[str, str]) -> dict[str, Any]:
    tot = sum(weights.values()) or 1.0
    by: dict[str, float] = {}
    for s, w in weights.items():
        by[sectors.get(s, "UNKNOWN")] = by.get(sectors.get(s, "UNKNOWN"), 0.0) + w / tot
    mx = max(by.items(), key=lambda kv: kv[1]) if by else ("", 0.0)
    return {"by_sector_pct": {k: round(v * 100, 1) for k, v in sorted(by.items(), key=lambda kv: -kv[1])},
            "max_sector": mx[0], "max_sector_pct": round(mx[1] * 100, 1),
            "breaches_cap": mx[1] > MAX_SECTOR_PCT, "cap_pct": round(MAX_SECTOR_PCT * 100, 1)}


def crash_floor_multiplier(index_close: pd.Series, *, ma_days: int = CRASH_FLOOR_MA_DAYS,
                           floor_exposure: float = CRASH_FLOOR_EXPOSURE) -> pd.Series:
    """Pre-committed exposure multiplier: full when index >= its MA, reduced when below. Uses yesterday's
    close vs yesterday's MA (no lookahead into today's decision)."""
    ma = index_close.rolling(ma_days).mean()
    below = index_close.shift(1) < ma.shift(1)
    return pd.Series(np.where(below.fillna(False), floor_exposure, 1.0), index=index_close.index)


def equity_stats(returns: pd.Series) -> dict[str, float]:
    r = returns.fillna(0.0)
    eq = (1.0 + r).cumprod()
    dd = (eq / eq.cummax() - 1.0).min()
    return {"total_return_pct": round((eq.iloc[-1] - 1.0) * 100, 1),
            "max_drawdown_pct": round(float(dd) * 100, 1),
            "ann_vol_pct": round(float(r.std(ddof=1)) * np.sqrt(TRADING_DAYS) * 100, 1)}


# ----- report -----

def _nifty() -> pd.DataFrame:
    from utils.db import sql_to_df
    n = sql_to_df("SELECT date, close FROM nseindia_indices WHERE index_name ILIKE 'nifty 50' ORDER BY date")
    n["date"] = pd.to_datetime(n["date"], utc=True, errors="coerce").dt.normalize()
    return n.dropna(subset=["date"]).reset_index(drop=True)


def _basket_returns(max_names: int = 12) -> pd.DataFrame:
    """A real recent basket (most-liquid names) to demonstrate the correlation/effective-bets view."""
    from utils.db import sql_to_df
    df = sql_to_df(
        """
        WITH recent AS (
          SELECT symbol, date, close, close*volume AS tv,
                 ROW_NUMBER() OVER (PARTITION BY symbol ORDER BY date DESC) rn
          FROM nseindia_ohlcv WHERE series='EQ')
        SELECT symbol, date, close FROM recent
        WHERE symbol IN (
          SELECT symbol FROM (
            SELECT symbol, AVG(tv) atv FROM recent WHERE rn<=120 GROUP BY symbol ORDER BY atv DESC LIMIT %(n)s) top)
          AND rn<=120
        ORDER BY date, symbol
        """,
        params={"n": max_names},
    )
    if df.empty:
        return pd.DataFrame()
    df["date"] = pd.to_datetime(df["date"], utc=True, errors="coerce").dt.normalize()
    wide = df.pivot_table(index="date", columns="symbol", values="close").sort_index()
    return wide.pct_change().dropna(how="all")


def build_report() -> dict[str, Any]:
    n = _nifty()
    out: dict[str, Any] = {"caps": {"max_position_pct": MAX_POSITION_PCT, "max_sector_pct": MAX_SECTOR_PCT,
                                    "max_total_exposure_pct": MAX_TOTAL_EXPOSURE_PCT}}
    if not n.empty:
        ret = n["close"].pct_change().fillna(0.0)
        mult = crash_floor_multiplier(n["close"]).to_numpy()
        floored = pd.Series(ret.to_numpy() * mult, index=n.index)
        out["crash_floor"] = {"buy_hold": equity_stats(ret), "with_floor": equity_stats(floored),
                              "time_in_market_pct": round(float((mult >= 1.0).mean()) * 100, 0),
                              "ma_days": CRASH_FLOOR_MA_DAYS, "floor_exposure": CRASH_FLOOR_EXPOSURE}
    out["sizing_example"] = [
        {"atr_pct": a, **size_position(1_000_000, a)} for a in (0.02, 0.035, 0.05, 0.08, 0.12)
    ]
    basket = _basket_returns()
    if not basket.empty and basket.shape[1] >= 2:
        eqw = {s: 1.0 for s in basket.columns}
        out["book_example"] = {"basket": list(basket.columns), **book_metrics(eqw, basket)}
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Portfolio & risk layer: vol sizing, book concentration, crash floor (advisory/report).")
    parser.add_argument("--format", choices=["text", "json"], default="text")
    args = parser.parse_args(argv)
    out = build_report()
    if args.format == "json":
        print(json.dumps(out, indent=2, default=str))
        return 0
    c = out["caps"]
    print(f"[portfolio_risk] hard caps deferred to: pos<={c['max_position_pct']*100:.0f}% sector<={c['max_sector_pct']*100:.0f}% "
          f"total<={c['max_total_exposure_pct']*100:.0f}%  (advisory layer can only be MORE conservative)\n")
    if "crash_floor" in out:
        cf = out["crash_floor"]; bh = cf["buy_hold"]; wf = cf["with_floor"]
        print(f"CRASH FLOOR (pre-committed: cash when NIFTY < {cf['ma_days']}DMA, else full) -- CAPITAL PROTECTION not alpha:")
        print(f"    {'':<14}{'total':>8}{'maxDD':>8}{'vol':>7}")
        print(f"    {'buy & hold':<14}{bh['total_return_pct']:>+7.1f}%{bh['max_drawdown_pct']:>7.1f}%{bh['ann_vol_pct']:>6.0f}%")
        print(f"    {'with floor':<14}{wf['total_return_pct']:>+7.1f}%{wf['max_drawdown_pct']:>7.1f}%{wf['ann_vol_pct']:>6.0f}%  (in-mkt {cf['time_in_market_pct']:.0f}%)")
        print("    -> halves drawdown+vol, but costs return in chop (whipsaw). Size it as insurance, not edge.\n")
    print("VOL-TARGETED SIZING (capital 10,00,000; risk 0.75%/trade to a 2.5*ATR stop):")
    print(f"    {'atr_pct':>8}{'value':>12}{'weight':>8}{'risk':>7}  capped_by")
    for s in out["sizing_example"]:
        print(f"    {s['atr_pct']*100:>7.1f}%{s['value_inr']:>12,.0f}{s['weight_pct']:>7.1f}%{s['risk_pct']:>6.2f}%  {s['capped_by']}")
    print("    -> volatile names get smaller positions for the same rupee risk (per-name cap binds the calm ones).\n")
    if "book_example" in out:
        b = out["book_example"]
        print(f"BOOK CONCENTRATION (equal-weight {b['names']} most-liquid names, last ~120d):")
        print(f"    effective_bets={b.get('effective_bets')}  portfolio_vol={b.get('portfolio_vol_pct')}%  "
              f"weighted_avg_name_vol={b.get('weighted_avg_name_vol_pct')}%  diversification_ratio={b.get('diversification_ratio')}  "
              f"avg_pairwise_corr={b.get('avg_pairwise_corr')}")
        print("    -> diversification_ratio near 1 = one bet wearing many tickers (per-name caps miss this).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
