# calendar_creator.py
import pandas as pd, numpy as np
from datetime import date
from psycopg2.extras import execute_values
from utils.db import get_connection


MIN_DATE = "2014-01-01"
OHLCV_TABLE = "nse_ohlcv"  # <-- change
OHLCV_DATE_COL = "trade_date"  # <-- change


def _fy_year(d: pd.Timestamp) -> int:
    return d.year if d.month >= 4 else d.year - 1


def _fy_quarter(d: pd.Timestamp) -> int:
    return ((d.month - 4) % 12) // 3 + 1  # Q1=Apr-Jun ... Q4=Jan-Mar


def compute_trading_days(conn) -> pd.DataFrame:
    # 1) Historical trading days from OHLCV
    hist = pd.read_sql(
        f"SELECT DISTINCT {OHLCV_DATE_COL}::date AS dt "
        f"FROM {OHLCV_TABLE} WHERE {OHLCV_DATE_COL}::date >= %s",
        conn,
        params=[MIN_DATE],
    )

    # 2) Current/future years from holidays (Mon–Fri minus holidays)
    try:
        hol = pd.read_sql("SELECT holiday_date::date AS d FROM nseindia_holidays", conn)
    except Exception:
        hol = pd.DataFrame(columns=["d"])

    cal = []
    if not hol.empty:
        hol["d"] = pd.to_datetime(hol["d"])
        current_year = pd.Timestamp.today().year
        for y in sorted(hol["d"].dt.year.unique()):
            if y < current_year:  # only build current/future from holidays
                continue
            bdays = pd.date_range(f"{y}-01-01", f"{y}-12-31", freq="B")
            hol_y = hol.loc[hol["d"].dt.year == y, "d"].dt.normalize()
            d = pd.Index(bdays.normalize()).difference(pd.Index(hol_y))
            if len(d):
                cal.append(pd.DataFrame({"dt": d.date}))

    cal = pd.concat(cal, ignore_index=True) if cal else pd.DataFrame(columns=["dt"])

    # 3) Optional overrides (Muhurat/Sat sessions/etc.)
    try:
        ov = pd.read_sql(
            "SELECT dt::date AS dt, is_trading_day FROM exchange_day_overrides", conn
        )
    except Exception:
        ov = pd.DataFrame(columns=["dt", "is_trading_day"])

    if not ov.empty:
        add = ov[ov.is_trading_day]["dt"]
        rem = ov[~ov.is_trading_day]["dt"]
        if not add.empty:
            cal = pd.concat([cal, add.to_frame(name="dt")], ignore_index=True)
        if not cal.empty and not rem.empty:
            cal = cal[~cal["dt"].isin(rem)]

    # 4) Union historical + holiday-derived
    all_days = pd.concat(
        [hist[["dt"]], cal[["dt"]]], ignore_index=True
    ).drop_duplicates()
    if all_days.empty:
        return all_days  # nothing to do
    s = pd.to_datetime(all_days["dt"]).sort_values().reset_index(drop=True)

    next_dt = s.shift(-1)
    prev_dt = s.shift(1)

    df = pd.DataFrame({"dt": s.dt.date})
    df["is_next_day_working"] = next_dt - s == pd.Timedelta(days=1)
    df["is_previous_day_working"] = s - prev_dt == pd.Timedelta(days=1)
    df["is_month_end"] = next_dt.isna() | (next_dt.dt.month != s.dt.month)

    q = s.map(_fy_quarter)
    q_next = next_dt.map(lambda x: np.nan if pd.isna(x) else _fy_quarter(x))
    df["is_quarter_end"] = next_dt.isna() | (q != q_next)

    fy = s.map(_fy_year)
    fy_next = next_dt.map(lambda x: np.nan if pd.isna(x) else _fy_year(x))
    df["is_year_end"] = next_dt.isna() | (fy != fy_next)

    df["week_number"] = s.dt.isocalendar().week.astype(int)
    return df


def upsert_trading_days(conn, df: pd.DataFrame):
    with conn.cursor() as cur:
        cur.execute(
            """
        CREATE TABLE IF NOT EXISTS trading_days (
          dt date PRIMARY KEY,
          is_next_day_working boolean,
          is_previous_day_working boolean,
          is_month_end boolean,
          week_number smallint,
          is_quarter_end boolean,
          is_year_end boolean,
          updated_at timestamptz DEFAULT now()
        );"""
        )
        records = list(
            df[
                [
                    "dt",
                    "is_next_day_working",
                    "is_previous_day_working",
                    "is_month_end",
                    "week_number",
                    "is_quarter_end",
                    "is_year_end",
                ]
            ].itertuples(index=False, name=None)
        )
        execute_values(
            cur,
            """
            INSERT INTO trading_days
              (dt,is_next_day_working,is_previous_day_working,is_month_end,week_number,is_quarter_end,is_year_end)
            VALUES %s
            ON CONFLICT (dt) DO UPDATE SET
              is_next_day_working=EXCLUDED.is_next_day_working,
              is_previous_day_working=EXCLUDED.is_previous_day_working,
              is_month_end=EXCLUDED.is_month_end,
              week_number=EXCLUDED.week_number,
              is_quarter_end=EXCLUDED.is_quarter_end,
              is_year_end=EXCLUDED.is_year_end,
              updated_at=now();
        """,
            records,
        )
    conn.commit()


def run():
    with get_connection() as conn:
        df = compute_trading_days(conn)
        if not df.empty:
            upsert_trading_days(conn, df)


if __name__ == "__main__":
    run()
