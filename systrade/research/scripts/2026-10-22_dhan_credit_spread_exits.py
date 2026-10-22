"""LEDGER row 74's input: the exit rules of the Dhan marketplace's credit spreads, read off their
trade records (row 72's archive). Shape, take-profit and stop levels, holding time. Descriptive.

Run: stockey/.xstockey/bin/python research/scripts/2026-10-22_dhan_credit_spread_exits.py
"""

import glob
import json
import os
import tarfile
import tempfile

import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
ARCHIVE = os.path.join(ROOT, "research", "imports", "dhan_algos_2026-10-22", "trades.tar.gz")


def main():
    with tempfile.TemporaryDirectory() as tmp:
        with tarfile.open(ARCHIVE) as t:
            t.extractall(tmp, filter="data")
        fs = [json.load(open(f)) for f in glob.glob(os.path.join(tmp, "all", "*.json"))]
    rows = []
    for d in fs:
        m = d["meta"]
        if m["displayCategory"] != "Credit Spread":
            continue
        for t in d["trades"] or []:
            sells = [leg for leg in t["trades"] if leg["transactionType"] == "SELL"]
            buys = [leg for leg in t["trades"] if leg["transactionType"] == "BUY"]
            if len(sells) != 1 or len(buys) != 1:
                continue
            s, b = sells[0], buys[0]
            credit = s["entryPrice"] - b["entryPrice"]
            a = pd.Timestamp(t["createdAt"]).tz_convert("Asia/Kolkata")
            for sm in t["summary"]:
                px = {x["symbol"]: x["price"] for x in sm["trades"] if "price" in x}
                if s["symbol"] not in px or b["symbol"] not in px:
                    continue
                c = pd.Timestamp(sm["closedOn"]).tz_convert("Asia/Kolkata")
                rows.append(dict(fam=m["STRATEGY_NAME"].split("Credit Spread")[-1].strip() or "plain", title=sm["title"],
                                 width=abs(s["strikePrice"] - b["strikePrice"]), credit=credit,
                                 capture=(credit - (px[s["symbol"]] - px[b["symbol"]])) / credit,
                                 entry_h=a.hour, exit_h=c.hour + c.minute / 60, days=(c.normalize() - a.normalize()).days))
    df = pd.DataFrame(rows)
    out = [f"Dhan marketplace credit spreads: {len(df)} exits (row 72 archive) -- capture = share of the credit kept"]
    out.append(f"width {df.width.value_counts().head(3).to_dict()}; credit/width median {(df.credit / df.width).median():.2f}; "
               f"entry hour {df.entry_h.value_counts().sort_index().to_dict()}")
    for (fam, title), g in df.groupby(["fam", "title"]):
        if len(g) < 20:
            continue
        out.append(f"{fam:11s} {title:10s} n={len(g):4d} capture " + " ".join(f"p{int(100 * q)} {g.capture.quantile(q):+.2f}" for q in (.05, .25, .5, .75, .95))
                   + f"  held 0/1/2+ days {(g.days == 0).mean():.0%}/{(g.days == 1).mean():.0%}/{(g.days >= 2).mean():.0%}")
    mid = df[(df.title == "Final Exit") & (df.exit_h >= 9.5) & (df.exit_h < 15)]
    out.append(f"mid-day final exits (09:30-15:00), n={len(mid)}: capture " + " ".join(f"p{int(100 * q)} {mid.capture.quantile(q):+.2f}" for q in (.1, .25, .5, .75, .9)))
    out.append("final-exit capture histogram:\n" + pd.cut(df[df.title == "Final Exit"].capture, [-5, -1, -.7, -.5, -.25, 0, .25, .5, .75, 1.01]).value_counts().sort_index().to_string())
    text = "\n".join(out)
    print(text)
    open(os.path.join(ROOT, "research", "reports", "2026-10-22_dhan_credit_spread_exits.txt"), "w").write(text + "\n")


if __name__ == "__main__":
    main()
