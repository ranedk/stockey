"""Operator-facing daily advisory -- the working edge, surfaced (2026-07-14).

The paper decision loop (RS>=80, vol-target sized, regime-floored, scored vs NIFTY after cost) is the one
part of the system with a measured edge, but it lives as a research table and its scoring loop SKIPS the
latest (unmatured) date -- so today's live picks are never surfaced. This builds the REVIEW-ONLY daily
advisory the operator can actually act on: today's RS picks, each vol-target sized and scaled by the
DATA-SELECTED regime floor (advisory.regime_shadow_ledger's max-growth-s.t.-ruin-guard config), with an
ATR stop and a name-specific cost estimate, plus the current market context (breadth, deployment).

Authority: REVIEW-ONLY by contract -- portfolio_authority=none, broker_execution_allowed=false,
full_advisory_required=true. It computes NO forward outcome (today's picks have not matured); the shadow
ledger / paper loop remain the forward scorecard. No broker path, no auto-execution. CLI: `python -m advisory.paper_advisory`.
"""
from __future__ import annotations

import argparse
import json
from typing import Any

import numpy as np
import pandas as pd

from advisory.cost_model import round_trip_cost_fraction
from advisory.factor_tilt import load_active_weights, select_top
from advisory.paper_decision_loop import (CAPITAL_INR, HOLD_DAYS, MAX_NAMES, POLICY_VERSION,
                                          RS_MIN_PERCENTILE, _load_panel, _rs_percentile)
from advisory.portfolio_risk import (STOP_ATR_MULT, breadth_floor_multiplier, crash_floor_multiplier,
                                     size_position)
from utils.db import sql_to_df, upsert_to_db
from utils.schema_migrations import apply_schema_migration

TABLE_NAME = "advisory_daily_advisory"
MIGRATION_ID = "20260714_advisory_daily_advisory"
SCHEMA_STATEMENTS = [
    f"""
    CREATE TABLE IF NOT EXISTS {TABLE_NAME} (
        asof_date TIMESTAMPTZ NOT NULL,
        symbol TEXT NOT NULL,
        policy_version TEXT NOT NULL,
        rank BIGINT,
        rs_percentile DOUBLE PRECISION,
        atr_pct DOUBLE PRECISION,
        entry_price DOUBLE PRECISION,
        stop_price DOUBLE PRECISION,
        vol_target_weight_pct DOUBLE PRECISION,
        deployment_exposure DOUBLE PRECISION,
        advisory_weight_pct DOUBLE PRECISION,
        risk_pct DOUBLE PRECISION,
        cost_fraction DOUBLE PRECISION,
        avg_turnover_inr DOUBLE PRECISION,
        regime_signal TEXT,
        portfolio_authority TEXT,
        broker_execution_allowed BOOLEAN,
        full_advisory_required BOOLEAN,
        authority_scope TEXT,
        load_ts TIMESTAMPTZ,
        UNIQUE (asof_date, symbol, policy_version)
    )
    """,
]


def ensure_table() -> None:
    apply_schema_migration(
        migration_id=MIGRATION_ID,
        statements=SCHEMA_STATEMENTS,
        owner="advisory.paper_advisory",
        description="Review-only daily advisory: today's RS picks, regime-floored vol-target sizing, ATR stop.",
        metadata={"tables": [TABLE_NAME], "authority": "review_only"},
    )


def current_exposure(asof_date: pd.Timestamp) -> tuple[float, dict[str, Any]]:
    """Today's deployment multiplier from the DATA-SELECTED floor config (advisory_regime_floor_config).
    Falls open to full exposure if the selection or the underlying series is unavailable."""
    try:
        cfg = sql_to_df(f"SELECT selected_signal, selected_threshold, selected_exposure "
                        f"FROM advisory_regime_floor_config ORDER BY run_date DESC LIMIT 1")
    except Exception:
        cfg = pd.DataFrame()
    if cfg.empty:
        return 1.0, {"signal": "none", "multiplier": 1.0, "note": "no floor config"}
    signal = str(cfg.iloc[0]["selected_signal"])
    threshold = cfg.iloc[0]["selected_threshold"]
    exposure = float(cfg.iloc[0]["selected_exposure"])
    if signal == "breadth":
        from advisory.market_breadth import load_breadth
        br = load_breadth()
        if br.empty:
            return 1.0, {"signal": "breadth", "multiplier": 1.0, "note": "no breadth series"}
        pct = br.set_index("date")["pct_above_50"]
        mult = breadth_floor_multiplier(pct, healthy_pct=float(threshold), floor_exposure=exposure)
        m = float(mult.reindex([asof_date]).iloc[0]) if asof_date in mult.index else float(mult.iloc[-1])
        latest_breadth = float(pct.reindex([asof_date]).iloc[0]) if asof_date in pct.index else float(pct.iloc[-1])
        return m, {"signal": "breadth", "threshold": float(threshold), "floor_exposure": exposure,
                   "breadth_pct_above_50": round(latest_breadth, 3), "multiplier": round(m, 3)}
    if signal == "none":
        return 1.0, {"signal": "none", "multiplier": 1.0}
    return exposure if exposure else 1.0, {"signal": signal, "multiplier": exposure}  # nifty handled by ledger; conservative


def build_advisory_rows(picks: pd.DataFrame, *, exposure: float, capital_inr: float,
                        asof_date: pd.Timestamp, regime_signal: str) -> list[dict[str, Any]]:
    """Pure: size each pick (vol-target x regime exposure), ATR stop, cost. Review-only authority stamped."""
    rows: list[dict[str, Any]] = []
    for rank, p in enumerate(picks.itertuples(index=False), start=1):
        atr_pct = float(p.atr_pct)
        sizing = size_position(capital_inr, atr_pct)
        entry = float(p.close)
        rows.append({
            "asof_date": asof_date, "symbol": p.symbol, "policy_version": POLICY_VERSION, "rank": rank,
            "rs_percentile": round(float(p.rs_percentile), 2), "atr_pct": round(atr_pct, 4),
            "entry_price": round(entry, 2), "stop_price": round(entry * (1.0 - STOP_ATR_MULT * atr_pct), 2),
            "vol_target_weight_pct": sizing["weight_pct"], "deployment_exposure": round(exposure, 3),
            "advisory_weight_pct": round(sizing["weight_pct"] * exposure, 3), "risk_pct": sizing["risk_pct"],
            "cost_fraction": round(float(round_trip_cost_fraction(float(p.avg_turnover_inr))), 5),
            "avg_turnover_inr": float(p.avg_turnover_inr), "regime_signal": regime_signal,
            # REVIEW-ONLY authority contract (CLAUDE.md): never broker-capable from this surface.
            "portfolio_authority": "none", "broker_execution_allowed": False,
            "full_advisory_required": True, "authority_scope": "review_only",
        })
    return rows


def build_daily_advisory(*, asof_date: pd.Timestamp | None = None, capital_inr: float = CAPITAL_INR,
                         dry_run: bool = False) -> dict[str, Any]:
    panel = _load_panel(HOLD_DAYS)
    if panel.empty:
        return {"picks": [], "note": "empty panel"}
    panel["date"] = pd.to_datetime(panel["date"], utc=True, errors="coerce").dt.normalize()
    latest = pd.Timestamp(asof_date).normalize() if asof_date is not None else panel["date"].max()
    day = panel[panel["date"] == latest].copy()
    if day.empty:
        return {"picks": [], "note": f"no panel rows for {latest.date()}"}
    day["rs_percentile"] = _rs_percentile(day)
    # Selection is pure-RS top-N by default. If a factor has GRADUATED and the operator has opted in
    # (FACTOR_GRADUATION_APPLY_ENABLED), its bounded weight refines the ordering within the RS pool only;
    # otherwise load_active_weights returns {} and select_top is exactly the pure-RS selection below.
    tilt_weights = load_active_weights(prefer="applied")
    picks = select_top(day, base_col="rs_percentile", gate_col="rs_percentile",
                       gate_min=RS_MIN_PERCENTILE, n=MAX_NAMES, weights=tilt_weights)

    exposure, context = current_exposure(latest)
    rows = build_advisory_rows(picks, exposure=exposure, capital_inr=capital_inr, asof_date=latest,
                               regime_signal=str(context.get("signal", "none")))
    if rows and not dry_run:
        ensure_table()
        for r in rows:
            r["load_ts"] = pd.Timestamp.utcnow()
        upsert_to_db(pd.DataFrame(rows), TABLE_NAME,
                     unique_keys=["asof_date", "symbol", "policy_version"], timescaledb_column="asof_date")
    deployed = round(float(sum(r["advisory_weight_pct"] for r in rows)), 2)
    return {"asof_date": str(latest.date()), "policy_version": POLICY_VERSION, "capital_inr": capital_inr,
            "context": context, "exposure": round(exposure, 3), "n_picks": len(rows),
            "book_deployment_pct": deployed, "cash_pct": round(100.0 - deployed, 2), "picks": rows}


def format_text_report(result: dict[str, Any]) -> str:
    if not result.get("picks"):
        return f"daily advisory: {result.get('note', 'no picks')}"
    ctx = result["context"]
    br = f", breadth {ctx.get('breadth_pct_above_50')}" if ctx.get("signal") == "breadth" else ""
    lines = [
        f"DAILY ADVISORY (REVIEW-ONLY -- no broker) | {result['asof_date']} | policy {result['policy_version']} "
        f"| capital Rs {result['capital_inr']:,.0f}",
        f"regime: {ctx.get('signal')} floor{br} -> deployment exposure {result['exposure']} "
        f"| book deployed {result['book_deployment_pct']}%  cash {result['cash_pct']}%  ({result['n_picks']} names)",
        f"{'#':>2} {'symbol':<14}{'RS':>6}{'wt%':>7}{'entry':>10}{'stop':>10}{'ATR%':>7}{'cost%':>7}",
    ]
    for r in result["picks"]:
        lines.append(f"{r['rank']:>2} {r['symbol']:<14}{r['rs_percentile']:>6.1f}{r['advisory_weight_pct']:>7.2f}"
                     f"{r['entry_price']:>10.2f}{r['stop_price']:>10.2f}{r['atr_pct'] * 100:>7.2f}"
                     f"{r['cost_fraction'] * 100:>7.2f}")
    lines.append("authority: portfolio_authority=none, broker_execution_allowed=false, full_advisory_required=true")
    return "\n".join(lines)


# ---------------------------------------------------------------------------------------------------
# Track record vs NIFTY (so the operator sees how recent picks actually did, next to today's list)
# ---------------------------------------------------------------------------------------------------
def track_record() -> dict[str, Any]:
    """Per-decision-date mean excess-vs-NIFTY of matured paper-loop trades (overlap-collapsed, day-level
    per s8.3). Returns the headline numbers + a short series for a sparkline. Empty on any failure."""
    try:
        df = sql_to_df(
            """
            SELECT asof_date::date AS d, AVG(excess_return) AS ex, COUNT(*) AS n
            FROM advisory_paper_decision_loop
            WHERE evaluation_status = 'evaluated' AND excess_return IS NOT NULL
            GROUP BY 1 ORDER BY 1
            """
        )
    except Exception:
        return {"n_dates": 0, "series": []}
    if df.empty:
        return {"n_dates": 0, "series": []}
    ex = pd.to_numeric(df["ex"], errors="coerce").astype(float)
    series = [round(float(v) * 100, 2) for v in ex.tolist()][-40:]
    return {
        "n_dates": int(len(df)), "n_trades": int(pd.to_numeric(df["n"]).sum()),
        "mean_excess_pct": round(float(ex.mean()) * 100, 2),
        "pct_dates_beat_nifty": round(float((ex > 0).mean()) * 100, 0),
        "series": series,
        "last_date": str(df["d"].iloc[-1]),
    }


def _sparkline(series: list[float], *, w: int = 220, h: int = 34) -> str:
    """Up/down bars of per-date excess vs NIFTY -- honest about consistency, no fake equity curve."""
    if not series:
        return ""
    n = len(series)
    mx = max(1e-9, max(abs(v) for v in series))
    bw = w / n
    mid = h / 2
    bars = []
    for i, v in enumerate(series):
        bh = (abs(v) / mx) * (h / 2 - 1)
        y = mid - bh if v >= 0 else mid
        cls = "up" if v >= 0 else "dn"
        bars.append(f'<rect class="{cls}" x="{i*bw:.1f}" y="{y:.1f}" width="{max(1,bw-1):.1f}" height="{bh:.1f}" rx="0.5"/>')
    return (f'<svg class="spark" viewBox="0 0 {w} {h}" width="{w}" height="{h}" role="img" '
            f'aria-label="recent excess vs NIFTY">{"".join(bars)}'
            f'<line x1="0" y1="{mid}" x2="{w}" y2="{mid}"/></svg>')


def render_dashboard(result: dict[str, Any], track: dict[str, Any]) -> str:
    """Self-contained operator dashboard (inner content; wrap for a standalone file or an artifact)."""
    ctx = result.get("context", {})
    breadth = ctx.get("breadth_pct_above_50")
    breadth_lbl = "&mdash;" if breadth is None else f"{breadth*100:.0f}%"
    breadth_state = "" if breadth is None else ("healthy" if breadth >= 0.5 else "narrow")
    tr_mean = track.get("mean_excess_pct")
    tr_cls = "pos" if (tr_mean or 0) >= 0 else "neg"
    rows = "".join(
        f'<tr><td class="r">{r["rank"]}</td><td class="sym">{r["symbol"]}</td>'
        f'<td class="n">{r["rs_percentile"]:.1f}</td><td class="n b">{r["advisory_weight_pct"]:.2f}</td>'
        f'<td class="n">{r["entry_price"]:.2f}</td><td class="n">{r["stop_price"]:.2f}</td>'
        f'<td class="n">{r["atr_pct"]*100:.1f}</td><td class="n">{r["cost_fraction"]*100:.2f}</td></tr>'
        for r in result.get("picks", [])
    )
    tr_panel = (
        f'<div class="chip wide"><span class="lbl">Recent picks vs NIFTY</span>'
        f'<span class="big {tr_cls}">{("+" if (tr_mean or 0)>=0 else "")}{tr_mean}%</span>'
        f'<span class="sub">avg per rebalance &middot; {track.get("pct_dates_beat_nifty")}% of dates beat the index '
        f'&middot; {track.get("n_dates")} matured</span>{_sparkline(track.get("series", []))}</div>'
        if track.get("n_dates") else
        '<div class="chip wide"><span class="lbl">Recent picks vs NIFTY</span><span class="sub">no matured results yet</span></div>'
    )
    return f"""<title>Daily Advisory &mdash; {result.get('asof_date')}</title>
<style>
  :root{{--bg:#F5F7F6;--surface:#FFFFFF;--raised:#FBFCFB;--ink:#131A1E;--muted:#59636B;--line:#E3E7E6;
    --accent:#0C6E62;--accent-ink:#0A5A50;--accent-soft:#E5F0EE;--pos:#2E7D5B;--neg:#B23A48;--warn:#A9761B;
    --head:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Arial,sans-serif;
    --mono:ui-monospace,"SF Mono","JetBrains Mono",Menlo,Consolas,monospace;}}
  @media (prefers-color-scheme:dark){{:root{{--bg:#0E1417;--surface:#161E22;--raised:#1B242A;--ink:#E6ECEA;
    --muted:#94A2A6;--line:#26323A;--accent:#5AC8B8;--accent-ink:#7FD8CB;--accent-soft:#14312E;
    --pos:#5FBF8F;--neg:#E0808C;--warn:#D6A250;}}}}
  :root[data-theme="dark"]{{--bg:#0E1417;--surface:#161E22;--raised:#1B242A;--ink:#E6ECEA;--muted:#94A2A6;
    --line:#26323A;--accent:#5AC8B8;--accent-ink:#7FD8CB;--accent-soft:#14312E;--pos:#5FBF8F;--neg:#E0808C;--warn:#D6A250;}}
  :root[data-theme="light"]{{--bg:#F5F7F6;--surface:#FFFFFF;--raised:#FBFCFB;--ink:#131A1E;--muted:#59636B;
    --line:#E3E7E6;--accent:#0C6E62;--accent-ink:#0A5A50;--accent-soft:#E5F0EE;--pos:#2E7D5B;--neg:#B23A48;--warn:#A9761B;}}
  *{{box-sizing:border-box}}
  body{{margin:0;background:var(--bg);color:var(--ink);font-family:var(--head);font-size:15px;line-height:1.5;
    -webkit-font-smoothing:antialiased;}}
  .wrap{{max-width:56rem;margin:0 auto;padding:1.6rem 1.3rem 4rem;}}
  .head{{display:flex;flex-wrap:wrap;align-items:baseline;gap:.6rem 1rem;margin-bottom:.3rem;}}
  h1{{font-size:1.5rem;font-weight:680;letter-spacing:-.02em;margin:0;}}
  .date{{font-family:var(--mono);color:var(--muted);font-size:.9rem;}}
  .badge{{margin-left:auto;font-size:.7rem;font-weight:700;letter-spacing:.1em;text-transform:uppercase;
    color:var(--accent-ink);background:var(--accent-soft);border:1px solid var(--accent);border-radius:999px;padding:.28rem .7rem;}}
  .note{{color:var(--muted);font-size:.86rem;margin:.2rem 0 1.3rem;}}
  .chips{{display:grid;grid-template-columns:repeat(auto-fit,minmax(9rem,1fr));gap:.7rem;margin-bottom:1.3rem;}}
  .chip{{background:var(--surface);border:1px solid var(--line);border-radius:12px;padding:.85rem .95rem;
    display:flex;flex-direction:column;gap:.15rem;}}
  .chip.wide{{grid-column:1/-1;}}
  .chip .lbl{{font-size:.68rem;font-weight:700;letter-spacing:.1em;text-transform:uppercase;color:var(--muted);}}
  .chip .big{{font-size:1.5rem;font-weight:680;font-variant-numeric:tabular-nums;}}
  .chip .val{{font-size:1.25rem;font-weight:650;font-variant-numeric:tabular-nums;}}
  .chip .sub{{font-size:.8rem;color:var(--muted);}}
  .pos{{color:var(--pos);}} .neg{{color:var(--neg);}}
  .spark{{margin-top:.5rem;}} .spark .up{{fill:var(--pos);}} .spark .dn{{fill:var(--neg);}}
  .spark line{{stroke:var(--line);stroke-width:1;}}
  .tablewrap{{overflow-x:auto;border:1px solid var(--line);border-radius:12px;background:var(--surface);}}
  table{{border-collapse:collapse;width:100%;font-size:.9rem;}}
  th,td{{padding:.5rem .7rem;text-align:right;border-bottom:1px solid var(--line);white-space:nowrap;}}
  th{{font-size:.66rem;font-weight:700;letter-spacing:.08em;text-transform:uppercase;color:var(--muted);
    position:sticky;top:0;background:var(--surface);}}
  td.sym{{text-align:left;font-weight:650;letter-spacing:-.01em;}}
  td.r{{text-align:left;color:var(--muted);font-variant-numeric:tabular-nums;}}
  td.n{{font-family:var(--mono);font-variant-numeric:tabular-nums;}}
  td.b{{color:var(--accent-ink);font-weight:650;}}
  tr:last-child td{{border-bottom:0;}}
  tbody tr:hover{{background:var(--accent-soft);}}
  .foot{{margin-top:1.4rem;color:var(--muted);font-size:.8rem;line-height:1.6;}}
  .foot b{{color:var(--ink);font-weight:650;}}
</style>
<div class="wrap">
  <div class="head">
    <h1>Daily Advisory</h1><span class="date">{result.get('asof_date')}</span>
    <span class="badge">Review only &middot; no broker</span>
  </div>
  <p class="note">Own the strongest relative-strength names, risk-sized and scaled by market health. A
  research advisory to review &mdash; not an instruction, and nothing executes on its own.</p>
  <div class="chips">
    <div class="chip"><span class="lbl">Market breadth</span><span class="val">{breadth_lbl}</span>
      <span class="sub">{breadth_state} &middot; {'full' if result.get('exposure',1)>=1 else 'reduced'} deployment</span></div>
    <div class="chip"><span class="lbl">Book deployed</span><span class="val">{result.get('book_deployment_pct')}%</span>
      <span class="sub">{result.get('cash_pct')}% cash &middot; {result.get('n_picks')} names</span></div>
    <div class="chip"><span class="lbl">Deployment dial</span><span class="val">&times;{result.get('exposure')}</span>
      <span class="sub">{ctx.get('signal','')} floor</span></div>
    {tr_panel}
  </div>
  <div class="tablewrap"><table>
    <thead><tr><th class="r" style="text-align:left">#</th><th style="text-align:left">Symbol</th>
      <th>RS</th><th>Weight %</th><th>Entry</th><th>Stop</th><th>ATR %</th><th>Cost %</th></tr></thead>
    <tbody>{rows}</tbody>
  </table></div>
  <p class="foot"><b>How to read this.</b> Weight % is the share of the book for each name (volatility-sized,
  scaled by the market-health dial, capped 5%). Stop is ~2.5&times; the stock's typical daily range below entry.
  Hold is mechanical (about 20 trading days or a stop-out). <b>Caveat:</b> the edge is measured on ~13 months
  &mdash; one market regime &mdash; so treat it as evidence to watch, not a guarantee.</p>
</div>"""


def _standalone(inner: str) -> str:
    return ('<!doctype html>\n<html lang="en"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width, initial-scale=1">'
            '<meta name="color-scheme" content="light dark"></head><body>\n' + inner + '\n</body></html>\n')


def write_dashboard(result: dict[str, Any], track: dict[str, Any], path: str) -> str:
    import os
    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    with open(path, "w") as fh:
        fh.write(_standalone(render_dashboard(result, track)))
    return path


def main(argv: list[str] | None = None) -> int:
    import os
    parser = argparse.ArgumentParser(description="Review-only daily advisory from the paper-loop RS book (no broker).")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--asof", default=None, help="YYYY-MM-DD (default = latest panel date).")
    parser.add_argument("--format", choices=["text", "json"], default="text")
    parser.add_argument("--html", default=os.getenv("PAPER_ADVISORY_HTML_PATH", "reports/daily_advisory.html"),
                        help="Path for the operator HTML dashboard (empty string to skip).")
    args = parser.parse_args(argv)
    asof = pd.Timestamp(args.asof, tz="UTC") if args.asof else None
    result = build_daily_advisory(asof_date=asof, dry_run=bool(args.dry_run))
    if args.html and result.get("picks"):
        out = write_dashboard(result, track_record(), args.html)
        result["html_path"] = out
    print(json.dumps(result, indent=2, default=str) if args.format == "json" else format_text_report(result))
    if result.get("html_path"):
        print(f"dashboard: {result['html_path']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
