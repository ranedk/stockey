# calendar_creator.py
import pandas as pd, numpy as np
from utils.db import sql_to_df, upsert_to_db


MIN_DATE = "2014-01-01"


def _fy_year(d: pd.Timestamp) -> int:
    return d.year if d.month >= 4 else d.year - 1


def _fy_quarter(d: pd.Timestamp) -> int:
    return ((d.month - 4) % 12) // 3 + 1  # Q1=Apr-Jun ... Q4=Jan-Mar


def compute_trading_days() -> pd.DataFrame:
    """
    Compute trading days based on historical OHLCV and holidays.
    """
    # 1) Historical trading days from OHLCV
    hist = sql_to_df(
        "SELECT DISTINCT date FROM nseindia_ohlcv WHERE date::date >= %s",
        params=[MIN_DATE],
    )

    # 2) Current/future years from holidays (Mon–Fri minus holidays)
    hol = sql_to_df("SELECT date FROM nseindia_holidays")

    cal = []
    if not hol.empty:
        hol["date"] = pd.to_datetime(hol["date"])
        current_year = pd.Timestamp.today().year
        for y in sorted(hol["date"].dt.year.unique()):
            if y < current_year:  # only build current/future from holidays
                continue
            bdays = pd.date_range(f"{y}-01-01", f"{y}-12-31", freq="B", normalize=True)
            hol_y = hol.loc[hol["date"].dt.year == y, "date"].dt.normalize()
            d = pd.Index(bdays).difference(pd.Index(hol_y))
            if len(d):
                cal.append(pd.DataFrame({"date": d}))

    cal = pd.concat(cal, ignore_index=True) if cal else pd.DataFrame(columns=["date"])
    cal["date"] = pd.to_datetime(cal["date"], utc=True)

    # 3) Lets use Diwali as exception because of 1 hour muhurat trading
    # If there are more such exceptions, we will create a new table and put those dates here
    ov = sql_to_df(
        "SELECT date FROM public.nseindia_holidays where type = 'CM' and holiday ilike '%%diwali%%'",
    )

    if not ov.empty:
        cal = pd.concat([cal, ov], ignore_index=True)

    # 4) Union historical + holiday-derived
    all_days = pd.concat([hist, cal]).drop_duplicates(subset=["date"])
    if all_days.empty:
        return all_days  # nothing to do
    s = pd.to_datetime(all_days["date"]).sort_values().reset_index(drop=True)

    next_dt = s.shift(-1)
    prev_dt = s.shift(1)

    df = pd.DataFrame({"date": s.dt.date})
    df["date"] = pd.to_datetime(df["date"])
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


def run():
    df = compute_trading_days()
    upsert_to_db(df, "dim_trading_days", unique_keys=["date"], timescaledb_column="date")


if __name__ == "__main__":
    run()
