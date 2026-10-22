"""Forward paper record for E1, the expiry-day stopped straddle basket (LEDGER row 78, passed its
check 2026-10-22; docs/strategies/2026-10-22_e1_expiry_straddle.md).

Rules frozen at the pre-registration (research/preregistrations/2026-10-22_nifty_expiry_straddle_and_orb.md):
on each weekly expiry session, the six library members S-0920/1000/1100 x stop +25%/+50% -- exactly
research/scripts/2026-10-22_nifty_algo_library.py's short_straddle() -- averaged equally.

Like cmd/paper, every run recomputes the whole record from START and rewrites
systrader_paper_option_e1, so it is safe to rerun and a missed day fills itself in. An expiry session
whose minute bars have not landed yet (stockey's all_option_data_daily.sh runs the next morning) is
left out and picked up on a later run. Nothing is ever sent to a broker.

Run: stockey/.xstockey/bin/python scripts/paper_e1_expiry_straddle.py
"""

import importlib.util
import os
from datetime import datetime, timezone

import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
START = pd.Timestamp("2026-10-23").date()  # first session after the rules were frozen and checked
TABLE = "systrader_paper_option_e1"
MEMBERS = [("0920", 1.25), ("0920", 1.5), ("1000", 1.25), ("1000", 1.5), ("1100", 1.25), ("1100", 1.5)]

spec = importlib.util.spec_from_file_location(
    "lib", os.path.join(ROOT, "research", "scripts", "2026-10-22_nifty_algo_library.py"))
lib = importlib.util.module_from_spec(spec)
spec.loader.exec_module(lib)
r64, r73 = lib.r64, lib.r73


def main():
    sessions, _ = r64.sessions_and_closes()
    last = sessions[-1]
    # only contracts that have already expired: a future label would map onto the last session we hold
    cmap = {x: s for x, s in r73.contract_map(sessions, str(START), str(last)).items() if x <= last}
    lots = r73.lots()
    rows, pending = [], []
    for label, d in sorted(cmap.items()):
        if d < START:
            continue
        b = lib.day_bars(d)
        if b.empty or b["end"].max() < r73.ts(d, lib.OUT):
            pending.append(d)
            continue
        lot = lots.get(label)
        for e, stop in MEMBERS:
            k, spot = lib.atm_at(b, r73.ts(d, lib.ENTRIES[e]))
            res = lib.short_straddle(d, b, lib.ENTRIES[e], stop, lot) if lot else None
            rows.append({"session": d, "expiry_label": label, "member": f"S-{e}-{int(round((stop - 1) * 100))}",
                         "strike": k, "spot": spot, "lot_size": lot,
                         "net_bps": None if res is None else 1e4 * res["r"],
                         "missing_legs": None if res is None else res["missing"],
                         "computed_at": datetime.now(timezone.utc)})
    cur = r64.CONN.cursor()
    cur.execute(f"""CREATE TABLE IF NOT EXISTS {TABLE} (
        session date NOT NULL, expiry_label date NOT NULL, member text NOT NULL, strike double precision,
        spot double precision, lot_size double precision, net_bps double precision, missing_legs integer,
        computed_at timestamptz NOT NULL, PRIMARY KEY (session, member))""")
    cur.execute(f"DELETE FROM {TABLE}")
    for r in rows:
        cur.execute(f"INSERT INTO {TABLE} VALUES (%(session)s, %(expiry_label)s, %(member)s, %(strike)s, %(spot)s, "
                    f"%(lot_size)s, %(net_bps)s, %(missing_legs)s, %(computed_at)s)", r)
    r64.CONN.commit()
    df = pd.DataFrame(rows)
    print(f"E1 paper: start {START}, sessions through {last}; expiry sessions recorded "
          f"{df['session'].nunique() if len(df) else 0}, waiting for data {pending}")
    if len(df):
        basket = df.groupby("session")["net_bps"].mean()
        print(basket.round(2).to_string())
        print(f"basket mean {basket.mean():+.2f} bps of notional per expiry (check period: +2.73), "
              f"n {len(basket)}, positive {int((basket > 0).sum())}; members not traded "
              f"{int(df['net_bps'].isna().sum())}")


if __name__ == "__main__":
    np.seterr(all="ignore")
    main()
