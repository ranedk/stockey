"""Operator dashboard for the always-on paper book (advisory.paper_book) -- today's actions, the open
book with a HOLD/TRIM/EXIT signal per name, and the realized track record vs NIFTY. Self-contained HTML,
theme-aware, review-only. Rendered on each daily run."""
from __future__ import annotations

import os
from typing import Any

import numpy as np
import pandas as pd

from advisory.paper_book import TABLE_NAME
from utils.db import sql_to_df

_ACTION_COLOR = {"EXIT": "neg", "TRIM": "warn", "BUY": "accent", "HOLD": "muted"}


def _closed_track() -> dict[str, Any]:
    try:
        df = sql_to_df(f"""SELECT exit_date::date d, realized_return_pct r, realized_excess_pct ex
                           FROM {TABLE_NAME} WHERE status='exited' AND realized_return_pct IS NOT NULL
                           ORDER BY exit_date""")
    except Exception:
        return {"n": 0, "series": []}
    if df.empty:
        return {"n": 0, "series": []}
    r = pd.to_numeric(df["r"], errors="coerce")
    ex = pd.to_numeric(df["ex"], errors="coerce").dropna()
    return {"n": int(len(df)), "avg_return": round(float(r.mean()), 2),
            "avg_excess": round(float(ex.mean()), 2) if len(ex) else None,
            "win": round(float((r > 0).mean()) * 100, 0),
            "series": [round(float(v), 2) for v in r.tolist()][-40:]}


def _spark(series: list[float], w: int = 220, h: int = 34) -> str:
    if not series:
        return ""
    mx = max(1e-9, max(abs(v) for v in series)); bw = w / len(series); mid = h / 2
    bars = "".join(
        f'<rect class="{"up" if v>=0 else "dn"}" x="{i*bw:.1f}" y="{(mid-(abs(v)/mx)*(h/2-1)) if v>=0 else mid:.1f}" '
        f'width="{max(1,bw-1):.1f}" height="{(abs(v)/mx)*(h/2-1):.1f}" rx="0.5"/>' for i, v in enumerate(series))
    return f'<svg class="spark" viewBox="0 0 {w} {h}" width="{w}" height="{h}" aria-label="realized returns">{bars}<line x1="0" y1="{mid}" x2="{w}" y2="{mid}"/></svg>'


def render_book_dashboard(result: dict[str, Any]) -> str:
    b = result.get("book", {})
    acts = result.get("actions", [])
    tr = _closed_track()
    openbk = sql_to_df(
        f"""SELECT symbol, entry_date::date entry, days_held, entry_price, last_price, stop_price,
                   unrealized_return_pct ur, entry_rs, last_action act, last_action_reason reason,
                   missing_days, ca_flag
            FROM {TABLE_NAME} WHERE status='open'
            ORDER BY (last_action='EXIT') DESC, (last_action='TRIM') DESC, ur DESC NULLS LAST""") if _has_table() else pd.DataFrame()

    def _acts(kind):
        return [a for a in acts if a["action"] == kind]
    exits, trims, buys = _acts("EXIT"), _acts("TRIM"), _acts("BUY")

    act_rows = ""
    for a in exits:
        act_rows += (f'<tr><td><span class="pill neg">EXIT</span></td><td class="sym">{a["symbol"]}</td>'
                     f'<td>{a["reason"].replace("_"," ")}</td><td class="n">{_pct(a.get("return_pct"))}</td>'
                     f'<td class="n">{_pct(a.get("excess_pct"))}</td><td class="n">{a.get("days_held","")}d</td></tr>')
    for a in trims:
        act_rows += (f'<tr><td><span class="pill warn">TRIM</span></td><td class="sym">{a["symbol"]}</td>'
                     f'<td>{a["reason"].replace("_"," ")}</td><td class="n">{_pct(a.get("return_pct"))}</td>'
                     f'<td class="n"></td><td class="n">{a.get("days_held","")}d</td></tr>')
    actions_block = (f'<div class="tablewrap"><table><thead><tr><th style="text-align:left">Action</th>'
                     f'<th style="text-align:left">Symbol</th><th style="text-align:left">Reason</th>'
                     f'<th>Return</th><th>vs NIFTY</th><th>Held</th></tr></thead><tbody>{act_rows}</tbody></table></div>'
                     if act_rows else '<p class="empty">No exits or trims today.</p>')
    buys_block = (f'<p class="buys"><span class="pill accent">BUY {len(buys)}</span> '
                  + ", ".join(a["symbol"] for a in buys) + '</p>') if buys else '<p class="empty">No new buys today.</p>'

    book_rows = ""
    for r in (openbk.itertuples(index=False) if not openbk.empty else []):
        act = str(r.act or "HOLD"); cls = _ACTION_COLOR.get(act, "muted")
        flag = " &#9873;" if (int(r.missing_days or 0) > 0 or bool(r.ca_flag)) else ""
        stopdist = ((float(r.last_price) / float(r.stop_price) - 1) * 100) if r.stop_price and r.last_price else None
        book_rows += (f'<tr><td class="sym">{r.symbol}{flag}</td><td class="n">{r.entry}</td>'
                      f'<td class="n">{int(r.days_held or 0)}</td><td class="n {_sign(r.ur)}">{_pct(r.ur)}</td>'
                      f'<td class="n">{_num(r.entry_rs,1)}</td><td class="n">{_pct(stopdist)}</td>'
                      f'<td><span class="pill {cls}">{act}</span></td></tr>')
    book_block = (f'<div class="tablewrap"><table><thead><tr><th style="text-align:left">Symbol</th>'
                  f'<th>Entry</th><th>Days</th><th>P&amp;L</th><th>Entry RS</th><th>To stop</th>'
                  f'<th style="text-align:left">Signal</th></tr></thead><tbody>{book_rows}</tbody></table></div>'
                  if book_rows else '<p class="empty">No open positions.</p>')

    tr_panel = (f'<span class="big {_sign(tr.get("avg_excess"))}">{_pct(tr.get("avg_excess"))}</span>'
                f'<span class="sub">avg excess/trade &middot; {tr.get("win")}% winners &middot; {tr.get("n")} closed</span>'
                f'{_spark(tr.get("series", []))}' if tr.get("n") else '<span class="sub">no closed trades yet</span>')

    return f"""<title>Paper Book &mdash; {result.get('asof')}</title>
{_CSS}
<div class="wrap">
  <div class="head"><h1>Paper Book</h1><span class="date">{result.get('asof')}</span>
    <span class="badge">Review only &middot; no broker</span></div>
  <p class="note">Every daily BUY is assumed taken and tracked to its exit &mdash; a stop, a ~20-day time cap,
  or fading momentum. This is what to do with the book <em>today</em>, for review; nothing executes on its own.</p>
  <div class="chips">
    <div class="chip"><span class="lbl">Open positions</span><span class="val">{b.get('open','&mdash;')}</span>
      <span class="sub">{b.get('closed','&mdash;')} closed to date{_flagnote(b.get('flagged'))}</span></div>
    <div class="chip"><span class="lbl">Avg unrealized</span><span class="val {_sign(b.get('avg_unrealized_pct'))}">{_pct(b.get('avg_unrealized_pct'))}</span>
      <span class="sub">across open names</span></div>
    <div class="chip wide"><span class="lbl">Closed track record vs NIFTY</span>{tr_panel}</div>
  </div>
  <h2>Today&rsquo;s actions</h2>
  {actions_block}
  {buys_block}
  <h2>Open book</h2>
  {book_block}
  <p class="foot"><b>Signals.</b> <span class="pill neg">EXIT</span> a hard rule fired (stop hit, 20-day cap,
  or the price stopped printing). <span class="pill warn">TRIM</span> momentum faded (RS below {int(_trim())}) &mdash;
  consider reducing. <span class="pill muted">HOLD</span> otherwise. &#9873; flags a stale price or a corporate
  action mid-hold to check. <b>Caveat:</b> paper only, ~13 months / one regime &mdash; evidence to watch.</p>
</div>"""


def write_book_dashboard(result: dict[str, Any], path: str) -> str:
    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    doc = ('<!doctype html>\n<html lang="en"><head><meta charset="utf-8">'
           '<meta name="viewport" content="width=device-width, initial-scale=1">'
           '<meta name="color-scheme" content="light dark"></head><body>\n'
           + render_book_dashboard(result) + '\n</body></html>\n')
    with open(path, "w") as fh:
        fh.write(doc)
    return path


# ---- helpers ----
def _has_table() -> bool:
    try:
        sql_to_df(f"SELECT 1 FROM {TABLE_NAME} LIMIT 1")
        return True
    except Exception:
        return False


def _trim() -> float:
    from advisory.paper_book import TRIM_RS
    return TRIM_RS


def _pct(v):
    if v is None or (isinstance(v, float) and not np.isfinite(v)):
        return "&mdash;"
    return f"{'+' if v >= 0 else ''}{v:.2f}%"


def _num(v, d=2):
    return "&mdash;" if v is None or (isinstance(v, float) and not np.isfinite(v)) else f"{v:.{d}f}"


def _sign(v):
    if v is None:
        return ""
    return "pos" if v >= 0 else "neg"


def _flagnote(n):
    return f' &middot; <span class="warnink">{n} flagged</span>' if n else ""


_CSS = """<style>
  :root{--bg:#F5F7F6;--surface:#FFFFFF;--ink:#131A1E;--muted:#59636B;--line:#E3E7E6;--accent:#0C6E62;
    --accent-ink:#0A5A50;--accent-soft:#E5F0EE;--pos:#2E7D5B;--neg:#B23A48;--warn:#A9761B;--warn-soft:#F5ECD9;
    --head:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Arial,sans-serif;
    --mono:ui-monospace,"SF Mono","JetBrains Mono",Menlo,Consolas,monospace;}
  @media (prefers-color-scheme:dark){:root{--bg:#0E1417;--surface:#161E22;--ink:#E6ECEA;--muted:#94A2A6;
    --line:#26323A;--accent:#5AC8B8;--accent-ink:#7FD8CB;--accent-soft:#14312E;--pos:#5FBF8F;--neg:#E0808C;
    --warn:#D6A250;--warn-soft:#2A2415;}}
  :root[data-theme="dark"]{--bg:#0E1417;--surface:#161E22;--ink:#E6ECEA;--muted:#94A2A6;--line:#26323A;
    --accent:#5AC8B8;--accent-ink:#7FD8CB;--accent-soft:#14312E;--pos:#5FBF8F;--neg:#E0808C;--warn:#D6A250;--warn-soft:#2A2415;}
  :root[data-theme="light"]{--bg:#F5F7F6;--surface:#FFFFFF;--ink:#131A1E;--muted:#59636B;--line:#E3E7E6;
    --accent:#0C6E62;--accent-ink:#0A5A50;--accent-soft:#E5F0EE;--pos:#2E7D5B;--neg:#B23A48;--warn:#A9761B;--warn-soft:#F5ECD9;}
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--ink);font-family:var(--head);font-size:15px;line-height:1.5;-webkit-font-smoothing:antialiased;}
  .wrap{max-width:58rem;margin:0 auto;padding:1.6rem 1.3rem 4rem;}
  .head{display:flex;flex-wrap:wrap;align-items:baseline;gap:.6rem 1rem;margin-bottom:.3rem;}
  h1{font-size:1.5rem;font-weight:680;letter-spacing:-.02em;margin:0;}
  h2{font-size:1rem;font-weight:660;margin:1.7rem 0 .6rem;letter-spacing:-.01em;}
  .date{font-family:var(--mono);color:var(--muted);font-size:.9rem;}
  .badge{margin-left:auto;font-size:.7rem;font-weight:700;letter-spacing:.1em;text-transform:uppercase;
    color:var(--accent-ink);background:var(--accent-soft);border:1px solid var(--accent);border-radius:999px;padding:.28rem .7rem;}
  .note{color:var(--muted);font-size:.86rem;margin:.2rem 0 1.3rem;}
  .chips{display:grid;grid-template-columns:repeat(auto-fit,minmax(9rem,1fr));gap:.7rem;}
  .chip{background:var(--surface);border:1px solid var(--line);border-radius:12px;padding:.85rem .95rem;display:flex;flex-direction:column;gap:.15rem;}
  .chip.wide{grid-column:1/-1;}
  .chip .lbl{font-size:.68rem;font-weight:700;letter-spacing:.1em;text-transform:uppercase;color:var(--muted);}
  .chip .val{font-size:1.35rem;font-weight:680;font-variant-numeric:tabular-nums;}
  .chip .big{font-size:1.5rem;font-weight:680;font-variant-numeric:tabular-nums;}
  .chip .sub{font-size:.8rem;color:var(--muted);}
  .pos{color:var(--pos);} .neg{color:var(--neg);} .warnink{color:var(--warn);}
  .spark{margin-top:.5rem;} .spark .up{fill:var(--pos);} .spark .dn{fill:var(--neg);} .spark line{stroke:var(--line);stroke-width:1;}
  .tablewrap{overflow-x:auto;border:1px solid var(--line);border-radius:12px;background:var(--surface);}
  table{border-collapse:collapse;width:100%;font-size:.9rem;}
  th,td{padding:.5rem .7rem;text-align:right;border-bottom:1px solid var(--line);white-space:nowrap;}
  th{font-size:.66rem;font-weight:700;letter-spacing:.08em;text-transform:uppercase;color:var(--muted);position:sticky;top:0;background:var(--surface);}
  td.sym{text-align:left;font-weight:650;} td.n{font-family:var(--mono);font-variant-numeric:tabular-nums;}
  tr:last-child td{border-bottom:0;} tbody tr:hover{background:var(--accent-soft);}
  .pill{font-size:.66rem;font-weight:700;letter-spacing:.05em;padding:.16rem .5rem;border-radius:999px;text-transform:uppercase;}
  .pill.neg{color:var(--neg);background:color-mix(in srgb,var(--neg) 14%,transparent);}
  .pill.warn{color:var(--warn);background:var(--warn-soft);}
  .pill.accent{color:var(--accent-ink);background:var(--accent-soft);}
  .pill.muted{color:var(--muted);background:color-mix(in srgb,var(--muted) 12%,transparent);}
  .buys{margin:.8rem 0 0;font-size:.9rem;} .buys .pill{margin-right:.5rem;}
  .empty{color:var(--muted);font-size:.88rem;margin:.6rem 0;}
  .foot{margin-top:1.5rem;color:var(--muted);font-size:.8rem;line-height:1.7;} .foot b{color:var(--ink);font-weight:650;}
  .foot .pill{vertical-align:.05em;}
</style>"""
