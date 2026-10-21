"""LEDGER row 65 -- EXPLORATION (trials=0): Nifty around known events (TODO G-goal step 1).

Which weeks should an option seller skip, and which may be better to sell? For each named
event -- RBI policy decisions, Union Budgets, general-election results (and exit polls),
weekly expiry days -- measure how much Nifty moved against what India VIX was charging:

  1. Event day: close-to-close move vs the move VIX implied at the previous close.
     Variance ratio = mean(move^2) / mean(implied^2): above 1, option sellers were
     underpaid on those days; below 1, overpaid. Same for every other day as the baseline.
  2. Row 64's window: two sessions before a weekly expiry to the expiry close, split by
     whether an RBI / Budget / election day falls inside it.
  3. VIX itself: how much it rises into the event and drops after.
  4. Expiry day intraday: 09:30 to the close (Dhan rolling-option spot, 2021-10+), expiry
     days vs other days.

Everything stops at 2024-10-31: 2024-11..2026-10 is the untouched check period for the
selective option-selling test (G-goal step 2) and is not looked at here.
No significance claims; nothing here picks a parameter by itself.

Run: stockey/.xstockey/bin/python research/scripts/2026-10-21_nifty_event_study.py
"""

import os
from datetime import time

import numpy as np
import pandas as pd
import psycopg2

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
END = pd.Timestamp("2024-10-31").date()  # check period starts after this; do not move
JUNK_INDEX_DATES = {pd.Timestamp("2023-06-04").date(), pd.Timestamp("2023-11-04").date()}  # TODO C11

# Policy decision days (the announcement day). Pre-MPC 2015-16 decisions included. Off-cycle
# decisions (2020-03-27, 2020-05-22, 2022-05-04) included: they are the surprises.
RBI = """2015-12-01 2016-02-02 2016-04-05 2016-06-07 2016-08-09 2016-10-04 2016-12-07
2017-02-08 2017-04-06 2017-06-07 2017-08-02 2017-10-04 2017-12-06
2018-02-07 2018-04-05 2018-06-06 2018-08-01 2018-10-05 2018-12-05
2019-02-07 2019-04-04 2019-06-06 2019-08-07 2019-10-04 2019-12-05
2020-02-06 2020-03-27 2020-05-22 2020-08-06 2020-10-09 2020-12-04
2021-02-05 2021-04-07 2021-06-04 2021-08-06 2021-10-08 2021-12-08
2022-02-10 2022-04-08 2022-05-04 2022-06-08 2022-08-05 2022-09-30 2022-12-07
2023-02-08 2023-04-06 2023-06-08 2023-08-10 2023-10-06 2023-12-08
2024-02-08 2024-04-05 2024-06-07 2024-08-08 2024-10-09""".split()
BUDGET = """2016-02-29 2017-02-01 2018-02-01 2019-02-01 2019-07-05 2020-02-01 2021-02-01
2022-02-01 2023-02-01 2024-02-01 2024-07-23""".split()
ELECTION = "2019-05-20 2019-05-23 2024-06-03 2024-06-04".split()  # exit-poll Monday, results day
PERIODS = [("2016-01..2021-09", "2016-01-01", "2021-09-30"),
           ("2021-10..2024-10", "2021-10-01", "2024-10-31"),
           ("all to 2024-10", "2016-01-01", "2024-10-31")]


def env():
    out = {}
    with open(os.path.join(ROOT, ".env")) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                out[k] = v.strip().strip('"').strip("'")
    return out


E = env()
CONN = psycopg2.connect(host=E["POSTGRES_HOST"], port=E["POSTGRES_PORT"], dbname=E["POSTGRES_DB"],
                        user=E["POSTGRES_USER"], password=E["POSTGRES_PASSWORD"])


def q(sql, params=None):
    import warnings
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        return pd.read_sql(sql, CONN, params=params)


def daily():
    df = q("SELECT date::date AS d, index_name, close FROM nseindia_indices "
           "WHERE index_name IN ('Nifty 50', 'India VIX') AND date >= '2015-10-01' AND date <= %(e)s",
           {"e": END})
    df = df[~df["d"].isin(JUNK_INDEX_DATES)].drop_duplicates(["d", "index_name"])
    w = df.pivot(index="d", columns="index_name", values="close").dropna()
    w.columns = ["vix", "nifty"] if list(w.columns) == ["India VIX", "Nifty 50"] else w.columns
    w = w.rename(columns={"Nifty 50": "nifty", "India VIX": "vix"}).sort_index()
    w["ret"] = np.log(w["nifty"]).diff()
    w["imp"] = w["vix"].shift(1) / 100 / np.sqrt(252)  # one-day move VIX implied at the previous close
    return w


def to_session(dates, sessions):
    """Each event onto the first session on or after it (a weekend announcement lands Monday)."""
    out = set()
    idx = pd.Index(sessions)
    for s in dates:
        d = pd.Timestamp(s).date()
        i = idx.searchsorted(d)
        if i < len(idx):
            out.add(idx[i])
    return out


def weekly_expiries():
    df = q("SELECT DISTINCT expiry FROM nseindia_fo_bhavcopy WHERE symbol = 'NIFTY' AND instrument = 'OPTIDX' "
           "AND trade_date BETWEEN '2019-01-01' AND %(e)s AND expiry <= %(e)s ORDER BY expiry", {"e": END})
    return set(df["expiry"])


def vr(x):
    """Variance ratio, mean |move|/implied, share of days beyond 2x implied, count."""
    x = x.dropna(subset=["ret", "imp"])
    if x.empty:
        return "   n=0"
    z = x["ret"] / x["imp"]
    return (f"n={len(x):4d}  variance ratio {np.mean(x['ret']**2) / np.mean(x['imp']**2):5.2f}  "
            f"mean |move|/implied {z.abs().mean():4.2f} (fair 0.80)  beyond 2x {100*(z.abs()>2).mean():4.1f}%")


def boot_ci(ret, imp, reps=2000, seed=1):
    rng = np.random.default_rng(seed)
    r2, i2 = np.asarray(ret) ** 2, np.asarray(imp) ** 2
    k = rng.integers(0, len(r2), size=(reps, len(r2)))
    v = r2[k].mean(axis=1) / i2[k].mean(axis=1)
    return np.percentile(v, [5, 95])


def main():
    out = []
    w = daily()
    sessions = list(w.index)
    ev = {"RBI": to_session(RBI, sessions), "Budget": to_session(BUDGET, sessions),
          "Election": to_session(ELECTION, sessions)}
    exp = {d for d in weekly_expiries() if d in w.index}
    named = ev["RBI"] | ev["Budget"] | ev["Election"]
    w["kind"] = "other"
    w.loc[w.index.isin(exp), "kind"] = "Expiry"
    for k in ["RBI", "Budget", "Election"]:  # an event on an expiry day counts as the event
        w.loc[w.index.isin(ev[k]), "kind"] = k

    out.append("LEDGER 65 -- Nifty around known events, EXPLORATION, data to 2024-10-31 only")
    out.append(f"sessions {len(w)}, RBI {len(ev['RBI'])}, Budget {len(ev['Budget'])}, "
               f"Election {len(ev['Election'])}, weekly expiries {len(exp)} (from 2019-02)")

    out.append("\n1. EVENT DAY: close-to-close move vs what VIX implied the evening before")
    out.append("   variance ratio > 1: sellers underpaid that day; < 1: overpaid")
    for label, lo, hi in PERIODS:
        x = w[(w.index >= pd.Timestamp(lo).date()) & (w.index <= pd.Timestamp(hi).date())]
        out.append(f"  {label}")
        for k in ["RBI", "Budget", "Election", "Expiry", "other"]:
            g = x[x["kind"] == k].dropna(subset=["ret", "imp"])
            ci = f"  90% CI {boot_ci(g['ret'], g['imp'])[0]:.2f}-{boot_ci(g['ret'], g['imp'])[1]:.2f}" if len(g) >= 5 else ""
            out.append(f"    {k:<9}{vr(g)}{ci}")

    out.append("\n   biggest event-day moves (multiples of the implied move):")
    e = w[w["kind"].isin(["RBI", "Budget", "Election"])].dropna(subset=["ret", "imp"]).copy()
    e["z"] = e["ret"] / e["imp"]
    for d, r in e.reindex(e["z"].abs().sort_values(ascending=False).index).head(8).iterrows():
        out.append(f"    {d} {r['kind']:<9} Nifty {100*r['ret']:+5.2f}%  = {r['z']:+5.1f}x implied (VIX {r['vix']:.1f})")

    out.append("\n2. ROW 64'S WINDOW: close two sessions before a weekly expiry -> expiry close")
    out.append("   implied = VIX at entry x sqrt(2/252); split by a named event inside the window")
    rows = []
    for x in sorted(exp):
        i = sessions.index(x)
        if i < 2:
            continue
        a = sessions[i - 2]
        inside = [k for k in ["RBI", "Budget", "Election"] if any(d in ev[k] for d in sessions[i - 1:i + 1])]
        rows.append({"expiry": x, "ret": np.log(w.at[x, "nifty"] / w.at[a, "nifty"]),
                     "imp": w.at[a, "vix"] / 100 * np.sqrt(2 / 252), "vix": w.at[a, "vix"],
                     "event": inside[0] if inside else "none"})
    wk = pd.DataFrame(rows)
    for label, lo, hi in PERIODS[1:] + [("2019-02..2021-09", "2019-02-01", "2021-09-30")]:
        x = wk[(wk["expiry"] >= pd.Timestamp(lo).date()) & (wk["expiry"] <= pd.Timestamp(hi).date())]
        out.append(f"  {label}")
        for k in ["RBI", "Budget", "Election", "none"]:
            out.append(f"    {k:<9}{vr(x[x['event'] == k])}")
        # VIX level at entry: is a high-VIX week a better one to sell?
        x = x.copy()
        x["vixq"] = pd.qcut(x["vix"], 3, labels=["low VIX", "mid VIX", "high VIX"])
        for k, g in x.groupby("vixq", observed=True):
            out.append(f"    {k:<9}{vr(g)}  (VIX {g['vix'].min():.1f}-{g['vix'].max():.1f})")

    out.append("\n3. VIX AROUND THE EVENT (points; all days' average change shown as the baseline)")
    w["dv"] = w["vix"].diff()
    pos = {d: i for i, d in enumerate(sessions)}
    base = w["dv"].mean()
    out.append(f"   all days: mean daily VIX change {base:+.3f}")
    for k in ["RBI", "Budget", "Election", "Expiry"]:
        days = ev[k] if k != "Expiry" else exp
        pre, on, post = [], [], []
        for d in days:
            i = pos.get(d)
            if i is None or i < 6 or i + 1 >= len(sessions):
                continue
            v = w["vix"].values
            pre.append(v[i - 1] - v[i - 6])
            on.append(v[i] - v[i - 1])
            post.append(v[i + 1] - v[i])
        out.append(f"   {k:<9} n={len(on):3d}  5 sessions before {np.mean(pre):+5.2f} (baseline {5*base:+.2f})  "
                   f"event day {np.mean(on):+5.2f}  next day {np.mean(post):+5.2f}")

    out.append("\n4. EXPIRY DAY INTRADAY: 09:30 -> last bar, Nifty spot from the rolling-option data, 2021-10..2024-10")
    sp = q("SELECT ts, spot FROM dhan_option_rolling WHERE underlying = 'NIFTY' AND expiry_flag = 'WEEK' "
           "AND expiry_code = 1 AND interval_minutes = 1 AND strike_offset = 0 AND option_type = 'CE' "
           "AND ts >= '2021-10-01' AND ts < %(e)s", {"e": pd.Timestamp(END) + pd.Timedelta(days=1)})
    sp["ts"] = pd.to_datetime(sp["ts"], utc=True).dt.tz_convert("Asia/Kolkata")
    sp["d"] = sp["ts"].dt.date
    sp["t"] = sp["ts"].dt.time
    sp = sp[sp["spot"] > 0].sort_values("ts")
    o = sp[sp["t"] <= time(9, 29)].groupby("d")["spot"].last()  # bar stamped 09:29 closes at 09:30
    c = sp.groupby("d")["spot"].last()
    m = pd.DataFrame({"o": o, "c": c}).dropna()
    m["ret"] = np.log(m["c"] / m["o"])
    m["imp"] = w["imp"].reindex(m.index) * np.sqrt(6 / 6.25)  # 09:30-15:30 of a 6.25 h session, VIX prev close
    m["kind"] = np.where(m.index.isin(exp), "Expiry", "other")
    for k in ["Expiry", "other"]:
        out.append(f"    {k:<9}{vr(m[m['kind'] == k])}")
    out.append("    (the intraday share of a day's variance is under the session share, so 'other' shows the"
               " normal level to compare against, not 1.0)")

    out.append("\n5. ROW 64'S WINDOW AT REAL OPTION PRICES: ATM straddle at 15:15 two sessions out vs the")
    out.append("   expiry payout |close - strike|, 2021-10..2024-10 (row 64's own in-sample, already seen)")
    out.append("   payout/premium above 1: sellers lost before costs; VIX ratio = the straddle's price vs VIX's")
    import importlib.util
    spec = importlib.util.spec_from_file_location("r64", os.path.join(HERE, "2026-10-20_nifty_option_selling.py"))
    r64 = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(r64)
    srows = []
    for _, r in wk[wk["expiry"] >= pd.Timestamp("2021-10-21").date()].iterrows():
        x = r["expiry"]
        a = sessions[sessions.index(x) - 2]
        chain, atm = r64.chain_at(a, time(15, 15))
        if atm is None or ("CE", atm) not in chain or ("PE", atm) not in chain:
            continue
        prem = chain[("CE", atm)][0] + chain[("PE", atm)][0]
        spot_fair = 0.8 * r["vix"] / 100 * np.sqrt(2 / 252) * w.at[a, "nifty"]  # straddle at VIX, ~0.8 sigma S
        srows.append({"event": r["event"], "vix": r["vix"], "prem": prem,
                      "pay": abs(w.at[x, "nifty"] - atm), "vixratio": prem / spot_fair})
    s = pd.DataFrame(srows)
    s["vixq"] = pd.qcut(s["vix"], 3, labels=["low VIX", "mid VIX", "high VIX"])

    def line(g):
        return (f"n={len(g):3d}  payout/premium {g['pay'].sum() / g['prem'].sum():4.2f}  "
                f"premium {g['prem'].mean():5.1f} pts  straddle priced at {g['vixratio'].mean():4.2f}x VIX's")
    out.append(f"    all      {line(s)}")
    for k, g in s.groupby("event"):
        out.append(f"    {k:<9}{line(g)}")
    for k, g in s.groupby("vixq", observed=True):
        out.append(f"    {k:<9}{line(g)}")

    text = "\n".join(out)
    print(text)
    path = os.path.join(ROOT, "research", "reports", "2026-10-21_nifty_event_study.txt")
    open(path, "w").write(text + "\n")
    print(f"\nwritten {path}")


if __name__ == "__main__":
    main()
