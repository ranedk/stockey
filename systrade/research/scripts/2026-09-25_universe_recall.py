"""Universe recall study (exploration, trials=0): would the proposed Layer 1 have held
the stocks that made money since 2019, and how much of their move was left when they
first qualified?

Layer 1 as tested (point-in-time, from price data only):
  - main board EQ series (BE trade-to-trade and SME SM/ST excluded)
  - estimated market cap >= Rs 500 cr
  - 63-session median traded value >= Rs 50 L
  - listed >= 252 sessions
NOT testable point-in-time (no history held): ASM/GSM surveillance, promoter pledge.

Market cap history is ESTIMATED: first observed nseindia_mcap (2024-02+) scaled back
by the adjusted close. Adjusted closes carry splits/bonuses; later share issuance is
not undone, so old caps are slightly overstated (the floor is tested leniently).

Winner: max over t of close_t / min(close since 2019-01-01 up to t) >= 5.
"""
import sys
import numpy as np
import pandas as pd
import psycopg2

env = {}
for line in open("/home/dev/code/trading/systrade/.env"):
    if "=" in line and not line.lstrip().startswith("#"):
        k, v = line.strip().split("=", 1)
        env[k] = v
conn = psycopg2.connect(host=env["POSTGRES_HOST"], port=env["POSTGRES_PORT"], dbname=env["POSTGRES_DB"],
                        user=env["POSTGRES_USER"], password=env["POSTGRES_PASSWORD"])
conn.set_session(readonly=True)
with conn.cursor() as cur:
    cur.execute("SET statement_timeout = '600s'")

OUT = sys.argv[1]
START, LISTED_FROM = "2019-01-01", "2016-01-01"
MCAP_FLOOR, VALUE_FLOOR, MIN_SESSIONS, WIN = 500e7, 50e5, 252, 5.0

print("loading prices...", flush=True)
px = pd.read_sql(
    f"""SELECT a.symbol, a.date::date AS date, a.series, a.adj_close, o.total_value
          FROM advisory_adjusted_ohlcv_daily a
          JOIN nseindia_ohlcv o ON o.symbol = a.symbol AND o.date = a.date AND o.series = a.series
         WHERE a.date >= '{LISTED_FROM}' AND a.series IN ('EQ','BE','SM','ST') AND a.adj_close > 0""",
    conn)
rank = {"EQ": 0, "BE": 1, "SM": 2, "ST": 3}
px["r"] = px["series"].map(rank)
px = px.sort_values(["symbol", "date", "r"]).drop_duplicates(["symbol", "date"], keep="first")
print(f"{len(px):,} rows, {px.symbol.nunique():,} symbols", flush=True)

ref = pd.read_sql(
    """SELECT DISTINCT ON (symbol) symbol, date::date AS ref_date, market_cap_rs
         FROM nseindia_mcap WHERE market_cap_rs > 0 ORDER BY symbol, date""", conn)
ref = ref.set_index("symbol")

rows = []
for sym, g in px.groupby("symbol", sort=False):
    g = g.reset_index(drop=True)
    g["sessions"] = np.arange(1, len(g) + 1)  # from 2016 (or listing): listing-age proxy
    g["value_med"] = g["total_value"].rolling(63, min_periods=40).median()
    if sym in ref.index:
        r = ref.loc[sym]
        at = g.loc[g["date"] <= r["ref_date"], "adj_close"]
        base = at.iloc[-1] if len(at) else g["adj_close"].iloc[0]
        g["mcap"] = r["market_cap_rs"] * g["adj_close"] / base
    else:
        g["mcap"] = np.nan
    g["l1"] = ((g["series"] == "EQ") & (g["mcap"] >= MCAP_FLOOR) & (g["value_med"] >= VALUE_FLOOR)
               & (g["sessions"] >= MIN_SESSIONS))

    w = g[g["date"] >= pd.Timestamp(START).date()].reset_index(drop=True)
    if len(w) < 60:
        continue
    runmin = w["adj_close"].cummin()
    drawup = w["adj_close"] / runmin
    peak_i = int(drawup.idxmax())
    mult = float(drawup.iloc[peak_i])
    if mult < WIN:
        continue
    trough_i = int(w.loc[:peak_i, "adj_close"].idxmin())
    seg = w.loc[trough_i:peak_i]
    entry = seg[seg["l1"]]
    peak_px, trough_px = w.at[peak_i, "adj_close"], w.at[trough_i, "adj_close"]
    rec = {
        "symbol": sym, "trough": w.at[trough_i, "date"], "peak": w.at[peak_i, "date"], "multiple": round(mult, 1),
        "series_at_peak": w.at[peak_i, "series"],
        "mcap_trough_cr": w.at[trough_i, "mcap"] / 1e7, "mcap_peak_cr": w.at[peak_i, "mcap"] / 1e7,
        "value_med_peak_l": w.at[peak_i, "value_med"] / 1e5,
        "in_l1_today": bool(g["l1"].iloc[-1]), "mcap_known": not np.isnan(w["mcap"].iloc[-1]),
    }
    if len(entry):
        e = entry.index[0]
        rec["entry"] = w.at[e, "date"]
        rec["captured_multiple"] = round(peak_px / w.at[e, "adj_close"], 2)
        rec["share_of_log_move"] = round(np.log(peak_px / w.at[e, "adj_close"]) / np.log(peak_px / trough_px), 3)
    else:
        rec["entry"], rec["captured_multiple"], rec["share_of_log_move"] = None, None, 0.0
        # why it never qualified: the first binding reason at the peak
        p = w.loc[peak_i]
        reasons = []
        if p["series"] != "EQ":
            reasons.append(f"series {p['series']}")
        if not (p["mcap"] >= MCAP_FLOOR):
            reasons.append("mcap unknown" if np.isnan(p["mcap"]) else "mcap < 500 cr")
        if not (p["value_med"] >= VALUE_FLOOR):
            reasons.append("value < 50 L")
        if p["sessions"] < MIN_SESSIONS:
            reasons.append("listed < 1y")
        rec["why_never"] = "; ".join(reasons)
    rows.append(rec)

df = pd.DataFrame(rows).sort_values("multiple", ascending=False)
df.to_csv(OUT, index=False)
print(f"winners (>= {WIN}x since {START}): {len(df)}", flush=True)
