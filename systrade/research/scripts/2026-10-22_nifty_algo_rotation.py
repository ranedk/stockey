"""LEDGER row 76 -- rotating among the 36-member Nifty algo library: run what is working, drop it when it
breaks (research/preregistrations/2026-10-22_nifty_algo_rotation.md, trials=3).

  R1  top 3 by 63-session Sharpe (positive only), held until cumulative return since selection
      < -2 x formation daily sd x sqrt(sessions since selection), then replaced by the current best
  R2  as R1, 42-session window
  R3  top 3 by 63-session Sharpe on the first session of each month, no break rule
Controls: EW of all eligible members; 1,000 random-pick draws per policy (same switch days, slot counts).

Timing: a decision before session t sees results through t-1; the O members' results (they close a
session later) only through t-2. Members are scaled to 10% annualised vol by trailing 60-session vol
of what was known, capped 3x. Sharpe and break checks are scale-free and use the raw series.
Interpretations fixed before any result (the pre-registration is silent on them):
  - an EMPTY slot (fewer than 3 qualified) is filled on the next session a positive-Sharpe non-held
    member exists; "no other switching" is read as: a held member is only ever removed by a break;
  - trading starts on the first session with 63 known sessions for every member (O: 64);
  - max drawdown is on the compounded account at the 10%-vol scale.

Run: stockey/.xstockey/bin/python research/scripts/2026-10-22_nifty_algo_rotation.py --period insample
"""

import argparse
import importlib.util
import os

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
SLOTS, TARGET, CAP, VOLWIN, SWITCH = 3, 0.10, 3.0, 60, 0.0003
POLICIES = {"R1 cycle, 3-month window": ("break", 63), "R2 cycle, 2-month window": ("break", 42),
            "R3 monthly top 3, no break": ("monthly", 63)}


def load(name, file):
    spec = importlib.util.spec_from_file_location(name, os.path.join(HERE, file))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


class Book:
    """Raw member returns plus everything a decision before each session may know."""

    def __init__(self, raw, tpos):
        self.raw = raw
        self.names = list(raw.columns)
        self.lag = np.array([2 if n.startswith("O-") else 1 for n in self.names])
        R = raw.values
        n, m = R.shape
        self.K = np.full((n, m), np.nan)  # K[t, j] = latest result known before session t
        for j, L in enumerate(self.lag):
            self.K[L:, j] = R[:n - L, j]
        vol = pd.DataFrame(self.K).rolling(VOLWIN).std().values * np.sqrt(252)
        with np.errstate(divide="ignore", invalid="ignore"):
            self.scale = np.where(vol > 0, np.minimum(TARGET / vol, CAP), np.nan)
        self.scaled = np.nan_to_num(R * self.scale)
        self.tpos = np.zeros((n, m))  # |position| x notional for the T members, for the switching cost
        for j, nm in enumerate(self.names):
            if nm in tpos:
                self.tpos[:, j] = np.nan_to_num(tpos[nm].reindex(raw.index).abs().values)
        self.days = raw.index
        self.memo = {}
        self.csum = np.vstack([np.zeros((1, m)), np.cumsum(np.nan_to_num(R), axis=0)])

    def sharpe(self, t, win):
        key = (t, win)
        if key not in self.memo:
            self.memo[key] = self._sharpe(t, win)
        return self.memo[key]

    def _sharpe(self, t, win):
        w = self.K[t - win + 1:t + 1]
        if t - win + 1 < 0 or np.isnan(w).any(axis=0).all():
            return np.full(len(self.names), np.nan), np.full(len(self.names), np.nan)
        mu, sd = w.mean(axis=0), w.std(axis=0, ddof=1)
        with np.errstate(divide="ignore", invalid="ignore"):
            sh = np.where(sd > 0, mu / sd, np.nan)
        sh[np.isnan(w).any(axis=0) | np.isnan(self.scale[t])] = np.nan
        return sh, sd

    def since(self, j, s, t):
        """Raw cumulative return of member j over sessions s .. (last known before t), and the count."""
        last = t - self.lag[j]  # inclusive
        if last < s:
            return 0.0, 0
        return self.csum[last + 1, j] - self.csum[s, j], last - s + 1


def run_policy(book, kind, win, start, rng=None, script=None):
    """Returns daily returns, the switch script (session -> number of slots filled) and holding records.
    With rng and script, replays the same switch days / counts with random picks among eligible members."""
    n, m = book.scaled.shape
    held = []  # [member, selected at session, formation sd]
    rets, events, records, picks = np.zeros(n), {}, [], []
    month = None
    for t in range(start, n):
        sh, sd = book.sharpe(t, win)
        elig = ~np.isnan(sh)
        fill = 0
        if kind == "monthly":
            mo = (book.days[t].year, book.days[t].month)
            if mo != month:
                month = mo
                if rng is None:
                    order = [j for j in np.argsort(-np.nan_to_num(sh, nan=-np.inf)) if elig[j] and sh[j] > 0][:SLOTS]
                else:
                    k = script.get(t, 0)
                    order = list(rng.choice(np.flatnonzero(elig), size=k, replace=False)) if k else []
                old = {h[0] for h in held}
                records += [(book.names[h[0]], h[1], t, "month end") for h in held]
                held = [[j, t, sd[j]] for j in order]
                events[t] = len(order)
                cost_in = [j for j in order if j not in old]
                cost_out = [j for j in old if j not in order]
            else:
                cost_in, cost_out = [], []
        else:
            cost_out = []
            if rng is None:
                for h in list(held):
                    c, k = book.since(h[0], h[1], t)
                    if k > 0 and c < -2 * h[2] * np.sqrt(k):
                        held.remove(h)
                        cost_out.append(h[0])
                        records.append((book.names[h[0]], h[1], t, "break"))
                free = SLOTS - len(held)
                hs = {h[0] for h in held}
                cand = [j for j in np.argsort(-np.nan_to_num(sh, nan=-np.inf)) if elig[j] and sh[j] > 0 and j not in hs]
                order = cand[:free]
            else:
                drop = script.get(t, (0, 0))[0]
                if drop:
                    out = list(rng.choice(len(held), size=drop, replace=False))
                    cost_out = [held[i][0] for i in out]
                    held = [h for i, h in enumerate(held) if i not in out]
                k = script.get(t, (0, 0))[1]
                hs = {h[0] for h in held}
                pool = [j for j in np.flatnonzero(elig) if j not in hs]
                order = list(rng.choice(pool, size=k, replace=False)) if k else []
            for j in order:
                held.append([j, t, sd[j]])
            if (cost_out or order) and rng is None:
                events[t] = (len(cost_out), len(order))
            cost_in = order
        w = 1.0 / SLOTS
        r = sum(w * book.scaled[t, h[0]] for h in held)
        r -= sum(w * book.scale[t, j] * book.tpos[t, j] * SWITCH for j in cost_in if not np.isnan(book.scale[t, j]))
        r -= sum(w * book.scale[t - 1, j] * book.tpos[t - 1, j] * SWITCH for j in cost_out
                 if not np.isnan(book.scale[t - 1, j]))
        rets[t] = r
        picks.append([book.names[h[0]] for h in held])
    for h in held:
        records.append((book.names[h[0]], h[1], n, "end"))
    return rets[start:], events, records, picks


def sharpe(x):
    return x.mean() / x.std() * np.sqrt(252) if x.std() > 0 else 0.0


def block_p(x, reps=10000, block=20, seed=1):
    """One-sided circular block bootstrap of the demeaned series: P(mean >= observed | true mean 0)."""
    rng = np.random.default_rng(seed)
    n, y = len(x), x - x.mean()
    nb = int(np.ceil(n / block))
    starts = rng.integers(0, n, size=(reps, nb))
    idx = (starts[:, :, None] + np.arange(block)[None, None, :]).reshape(reps, -1)[:, :n] % n
    return float((y[idx].mean(axis=1) >= x.mean()).mean())


def maxdd(x):
    eq = np.cumprod(1 + x)
    return float((eq / np.maximum.accumulate(eq) - 1).min())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--period", choices=["insample"], required=True)
    ap.add_argument("--draws", type=int, default=1000)
    args = ap.parse_args()
    lib_mod = load("lib", "2026-10-22_nifty_algo_library.py")
    lo, hi = lib_mod.PERIODS[args.period]
    raw = pd.read_csv(os.path.join(ROOT, "research", "reports", f"2026-10-22_nifty_algo_library_{args.period}.csv"),
                      index_col="day", parse_dates=["day"])
    raw.index = raw.index.date
    raw = raw[lib_mod.members()]
    _, tpos = lib_mod.trend_members(lo, hi)
    book = Book(raw, tpos)
    start = 63 + 1  # 63 known sessions for every member, O lagged one more
    while np.isnan(book.scale[start]).any():
        start += 1
    days = book.days[start:]
    out = [f"LEDGER 76 -- Nifty algo rotation, {args.period}: library {raw.index[0]}..{raw.index[-1]}, "
           f"trading {days[0]}..{days[-1]} ({len(days)} sessions); 36 members at 10% vol each", ""]
    ew = np.nanmean(np.where(np.isnan(book.scale), np.nan, book.scaled)[start:], axis=1)
    res = {"EW (control)": ew}
    detail = {}
    for name, (kind, win) in POLICIES.items():
        r, ev, rec, picks = run_policy(book, kind, win, start)
        rng = np.random.default_rng(1)
        rand = np.array([sharpe(run_policy(book, kind, win, start, rng=rng, script=ev)[0]) for _ in range(args.draws)])
        res[name] = r
        detail[name] = (ev, rec, picks, rand)
    yrs = pd.Series(days).map(lambda d: d.year).values
    ps = {k: block_p(v) for k, v in res.items()}
    pol = list(POLICIES)
    qs = dict(zip(pol, load("r64", "2026-10-20_nifty_option_selling.py").bh([ps[k] for k in pol])))
    out.append(f"{'':28s} {'mean %/yr':>9s} {'vol':>6s} {'Sharpe':>7s} {'maxDD':>7s}  {'2022':>6s} {'2023':>6s} {'2024':>6s}  p (block)")
    for k, x in res.items():
        by = {y: 100 * (np.prod(1 + x[yrs == y]) - 1) for y in (2022, 2023, 2024)}
        out.append(f"{k:28s} {100 * 252 * x.mean():+9.2f} {100 * x.std() * np.sqrt(252):5.1f}% {sharpe(x):+7.2f} "
                   f"{100 * maxdd(x):6.1f}%  {by[2022]:+6.1f} {by[2023]:+6.1f} {by[2024]:+6.1f}  {ps[k]:.4f}")
    out.append("\nVERDICT (pass = mean > 0 with BH q < 0.10, random p < 0.10, Sharpe > EW, >= 2 of 3 years positive, maxDD within 35%):")
    for k in pol:
        x, rand = res[k], detail[k][3]
        rp = float((rand >= sharpe(x)).mean())
        ny = sum(np.prod(1 + x[yrs == y]) > 1 for y in (2022, 2023, 2024))
        ok = x.mean() > 0 and qs[k] < 0.10 and rp < 0.10 and sharpe(x) > sharpe(ew) and ny >= 2 and maxdd(x) >= -0.35
        out.append(f"  {k:28s} Sharpe {sharpe(x):+.2f} (EW {sharpe(ew):+.2f})  q {qs[k]:.4f}  random p {rp:.3f} "
                   f"(random median Sharpe {np.median(rand):+.2f})  years positive {ny}/3  maxDD {100 * maxdd(x):.1f}%  -> {'PASS' if ok else 'FAIL'}")
    out.append("\nreported, not gated:")
    for k in pol:
        ev, rec, picks, _ = detail[k]
        fam = pd.Series([p[0] for day in picks for p in day]).value_counts(normalize=True)
        line = f"  {k}: {len(ev)} switch days; picked by family " + ", ".join(f"{f} {100 * v:.0f}%" for f, v in fam.items())
        line += f"; cash {100 * np.mean([SLOTS - len(p) for p in picks]) / SLOTS:.0f}% of slot-days"
        out.append(line)
        if rec:
            life = pd.DataFrame(rec, columns=["m", "s", "e", "how"])
            life["n"] = life.e - life.s
            br = life[life.how == "break"]
            out.append(f"     holdings {len(life)}; broken {len(br)}, median life before a break {br.n.median() if len(br) else float('nan'):.0f} "
                       f"sessions; most held: " + ", ".join(f"{m} {n}" for m, n in life.groupby("m").n.sum().sort_values(ascending=False).head(5).items()))
        byyr = pd.DataFrame({"y": [d.year for d in days], "f": [",".join(sorted({x[0] for x in p})) for p in picks]})
        out.append("     families held by year: " + "  ".join(f"{y}: {g.f.value_counts().index[0] or 'cash'}" for y, g in byyr.groupby("y")))
    d = res[pol[0]] - res[pol[2]]
    out.append(f"  R1 - R3 paired: {100 * 252 * d.mean():+.2f}%/yr, t {d.mean() / (d.std() / np.sqrt(len(d))):+.2f}")
    text = "\n".join(out)
    print(text)
    path = os.path.join(ROOT, "research", "reports", f"2026-10-22_nifty_algo_rotation_{args.period}.txt")
    open(path, "w").write(text + "\n")
    print(f"\nwritten {path}")


if __name__ == "__main__":
    main()
