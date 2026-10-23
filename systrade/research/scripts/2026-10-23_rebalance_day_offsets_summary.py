"""LEDGER row 81: summarise cmd/paper -dry replays of each book from 21 shifted start days (see the report)."""
import glob
import os
import re
import sys

import pandas as pd

rows = []
for f in glob.glob(os.path.join(sys.argv[1], "*.txt")):
    st, k = os.path.basename(f)[:-4].rsplit("_", 1)
    rec = {"book": st, "offset": int(k)}
    for line in open(f):
        m = re.match(r"(strategy|equal-weight|random-ranking)\s+([\d.]+)\s+([-\d.]+)%\s+([\d.]+)%\s+([-\d.]+)%", line)
        if m:
            rec[m.group(1) + "_ret"] = float(m.group(3))
            if m.group(1) == "strategy":
                rec["maxdd"] = float(m.group(5))
    rows.append(rec)
df = pd.DataFrame(rows)
df["vs_ew"] = df["strategy_ret"] - df["equal-weight_ret"]
g = df.groupby("book")
out = pd.DataFrame({"n": g.size(), "ret_min": g.strategy_ret.min(), "ret_median": g.strategy_ret.median(),
                    "ret_max": g.strategy_ret.max(), "spread": g.strategy_ret.max() - g.strategy_ret.min(),
                    "dd_worst": g.maxdd.min(), "dd_best": g.maxdd.max(),
                    "vs_ew_min": g.vs_ew.min(), "vs_ew_max": g.vs_ew.max(),
                    "offset0_ret": df[df.offset == 0].set_index("book").strategy_ret})
print(out.round(2).to_string())
print("\nequal-weight return across offsets (start-date noise only):",
      df["equal-weight_ret"].min(), "..", df["equal-weight_ret"].max())
df.sort_values(["book", "offset"]).to_csv(os.path.join(sys.argv[1], "all.csv"), index=False)  # one row per book x start
