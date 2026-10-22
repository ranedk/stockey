"""LEDGER row 75 -- EXPLORATION (trials=0): do the Dhan marketplace's strategies that worked over the
last 2-3 months keep working next month? (operator, 2026-10-22: algos "work for some time, then stop";
the precondition for rotating to whatever works now.)

Data: row 72's archive (61 Nifty strategies, one vendor), each trade's P&L per lot / margin, after
row 72's costs, summed by the day it closed. Two scores per strategy at each month end:
  ret   formation-window return (sum of daily returns)
  tstat formation mean / (std / sqrt(days)), so volatile option-buying books do not dominate
Tests, each month t (formation = the 2 or 3 months before t, holding = month t):
  cross-section  Spearman rank correlation of score vs next-month return; top third minus bottom third
  time series    next-month return after a positive vs a negative formation window, pooled
  break rule     after a month whose return is below -2 sd of the strategy's formation-window monthly
                 equivalent (daily sd x sqrt(days)), next month vs all other strategy-months
Universes: all 61; selling only (credit spreads, strangles, straddles). Trades: all, and live only.
No significance claim beyond the month count stated; ~14-17 months, one vendor, listed survivors.
Run: stockey/.xstockey/bin/python research/scripts/2026-10-22_dhan_algos_persistence.py
"""

import importlib.util
import os

import numpy as np
import pandas as pd
from scipy.stats import spearmanr

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
spec = importlib.util.spec_from_file_location("rev", os.path.join(HERE, "2026-10-22_dhan_algos_review.py"))
rev = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rev)
SELLING = ("Credit Spread", "Short Strangle", "Short Straddle")


def daily_panel(data, live_only):
    cols = {}
    meta = {}
    for d in data:
        m, T = d["meta"], d["trades"] or []
        rows = []
        for x in T:
            if x.get("signalPnl") is None or not x.get("marginRequired"):
                continue
            if live_only and x["createdAt"][:10] < m["liveSince"][:10]:
                continue
            day = pd.Timestamp(x["closedOn"]).tz_convert("Asia/Kolkata").normalize().tz_localize(None)
            rows.append((day, (x["signalPnl"] - rev.trade_cost(x)) / x["marginRequired"]))
        if len(rows) < 30:
            continue
        s = pd.DataFrame(rows, columns=["d", "r"]).groupby("d")["r"].sum()
        cols[m["STRATEGY_NAME"]] = s
        meta[m["STRATEGY_NAME"]] = m["displayCategory"]
    panel = pd.DataFrame(cols).sort_index()
    # a strategy is "active" from its first to its last trade; days between with no trade are 0
    first, last = panel.apply(lambda s: s.first_valid_index()), panel.apply(lambda s: s.last_valid_index())
    for c in panel:
        panel.loc[first[c]:last[c], c] = panel.loc[first[c]:last[c], c].fillna(0.0)
    return panel, meta


def tests(panel, form_months, out, label):
    months = panel.index.to_period("M")
    ends = sorted(set(months))
    ics, spreads, ts_rows, brk = [], [], [], []
    for i in range(form_months, len(ends)):
        hold = panel[months == ends[i]]
        form = panel[(months >= ends[i - form_months]) & (months < ends[i])]
        ok = [c for c in panel if form[c].notna().mean() > 0.9 and hold[c].notna().mean() > 0.9]
        if len(ok) < 10:
            continue
        nxt = hold[ok].sum()
        f = form[ok]
        sc_ret = f.sum()
        sd = f.std()
        sc_t = f.mean() / (sd / np.sqrt(f.count())).replace(0, np.nan)
        for name, sc in (("ret", sc_ret), ("tstat", sc_t)):
            ok2 = sc.dropna().index
            rho = spearmanr(sc[ok2], nxt[ok2]).statistic
            k = max(len(ok2) // 3, 1)
            order = sc[ok2].sort_values()
            ics.append((name, ends[i], rho))
            spreads.append((name, ends[i], nxt[order.index[-k:]].mean() - nxt[order.index[:k]].mean(), nxt.mean(), len(ok2)))
        for c in ok:
            ts_rows.append((sc_ret[c] > 0, nxt[c]))
            last_m = form[c][form.index.to_period("M") == ends[i - 1]].sum()
            band = sd[c] * np.sqrt(form[c][form.index.to_period("M") == ends[i - 1]].count())
            brk.append((band > 0 and last_m < -2 * band, nxt[c]))
    if not ics:
        out.append(f"  {label}: too few months")
        return
    ic = pd.DataFrame(ics, columns=["s", "m", "rho"])
    sp = pd.DataFrame(spreads, columns=["s", "m", "spread", "avg", "n"])
    out.append(f"  {label}, formation {form_months} months: {sp.m.nunique()} test months, {int(sp.n.median())} strategies a month")
    for s in ("ret", "tstat"):
        r = ic[ic.s == s].rho
        x = sp[sp.s == s].spread
        out.append(f"    score {s:5s}: rank corr mean {r.mean():+.3f} (positive in {int((r > 0).sum())}/{len(r)} months, t {r.mean() / (r.std() / np.sqrt(len(r))):+.2f});"
                   f"  top third - bottom third next month {100 * x.mean():+.2f}% of margin (t {x.mean() / (x.std() / np.sqrt(len(x))):+.2f}),"
                   f" average strategy {100 * sp[sp.s == s].avg.mean():+.2f}%")
    t = pd.DataFrame(ts_rows, columns=["pos", "nxt"])
    out.append(f"    time series: next month after a positive window {100 * t[t.pos].nxt.mean():+.2f}% (n {t.pos.sum()}), "
               f"after a negative one {100 * t[~t.pos].nxt.mean():+.2f}% (n {(~t.pos).sum()}); "
               f"P(next > 0) {100 * (t[t.pos].nxt > 0).mean():.0f}% vs {100 * (t[~t.pos].nxt > 0).mean():.0f}%")
    b = pd.DataFrame(brk, columns=["broke", "nxt"])
    out.append(f"    break rule (last month below -2 sd): {int(b.broke.sum())} cases, next month {100 * b[b.broke].nxt.mean():+.2f}% "
               f"vs others {100 * b[~b.broke].nxt.mean():+.2f}%")


def main():
    data = rev.load()
    out = ["LEDGER 75 -- do Dhan marketplace strategies that worked recently keep working? (EXPLORATION)",
           "returns after row 72's costs, % of margin per lot; holding month = the calendar month after the window", ""]
    for live_only in (False, True):
        panel, meta = daily_panel(data, live_only)
        out.append(f"{'LIVE trades only' if live_only else 'ALL trades (incl. before go-live)'}: {panel.shape[1]} strategies, "
                   f"{panel.index.min().date()}..{panel.index.max().date()}")
        sell = [c for c in panel if meta[c] in SELLING]
        for form in (2, 3):
            tests(panel, form, out, "all strategies")
            tests(panel[sell], form, out, "selling only")
        out.append("")
    text = "\n".join(out)
    print(text)
    path = os.path.join(ROOT, "research", "reports", "2026-10-22_dhan_algos_persistence.txt")
    open(path, "w").write(text + "\n")
    print(f"written {path}")


if __name__ == "__main__":
    main()


def decay(panel, form_months, out, label, lags=(1, 2, 3, 4, 6)):
    """Cycle or fixed ranking? Rank correlation of the formation score with month t+L-1 for each lag L.
    A cycle ("works for a while, then stops") predicts a fall with the lag; a fixed ranking predicts a
    flat line. 'whole' = each strategy's average over all its OTHER months, before and after (look-ahead;
    the fixed-ranking ceiling)."""
    months = panel.index.to_period("M")
    ends = sorted(set(months))
    monthly = panel.groupby(months).sum(min_count=1)
    res = {L: [] for L in lags}
    whole = []
    for i in range(form_months, len(ends)):
        form = panel[(months >= ends[i - form_months]) & (months < ends[i])]
        ok = [c for c in panel if form[c].notna().mean() > 0.9]
        sc = form[ok].sum()
        for L in lags:
            j = i + L - 1
            if j >= len(ends):
                continue
            nxt = monthly.loc[ends[j], ok].dropna()
            if len(nxt) >= 10:
                res[L].append(spearmanr(sc[nxt.index], nxt).statistic)
        nxt = monthly.loc[ends[i], ok].dropna()
        avg = monthly[nxt.index].drop(index=ends[i]).mean()  # every OTHER month, before and after
        if len(nxt) >= 10:
            whole.append(spearmanr(avg, nxt).statistic)
    out.append(f"  {label}, formation {form_months} months -- rank corr of the score with the month L ahead:  "
               + "  ".join(f"L{L} {np.mean(v):+.3f} (n{len(v)})" for L, v in res.items() if v)
               + f"   | strategy's average over all other months (look-ahead) {np.mean(whole):+.3f}")


def main_decay():
    data = rev.load()
    out = ["\ncycle or fixed ranking? (added after the first results, same data)"]
    for live_only in (False, True):
        panel, meta = daily_panel(data, live_only)
        sell = [c for c in panel if meta[c] in SELLING]
        out.append(f"{'LIVE only' if live_only else 'ALL trades'}:")
        for form in (2, 3):
            decay(panel, form, out, "all strategies")
            decay(panel[sell], form, out, "selling only")
    text = "\n".join(out)
    print(text)
    open(os.path.join(ROOT, "research", "reports", "2026-10-22_dhan_algos_persistence.txt"), "a").write(text + "\n")


if __name__ == "__main__" and os.environ.get("DECAY"):
    main_decay()
