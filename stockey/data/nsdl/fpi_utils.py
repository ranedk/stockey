import calendar
import re
from datetime import date, datetime

import pandas as pd
from psycopg2 import sql
from psycopg2.extras import RealDictCursor

from utils.db import get_connection

MONTH_PAT = re.compile(r"^Total for ([A-Za-z]+)$")
YEAR_PAT = re.compile(r"^Total for (\d{4})$")
MONTH2NUM = {m.lower(): i for i, m in enumerate(calendar.month_name) if m}


def get_last_date(year: int, month: int) -> date:
    last_day = calendar.monthrange(year, month)[1]
    return date(year, month, last_day)


def _last_day_of_month(year: int, month: int) -> pd.Timestamp:
    """Return the final calendar day of the given month/year."""
    return pd.Timestamp(year, month, 1) + pd.offsets.MonthEnd(0)


def to_snake(s: str) -> str:
    return re.sub(r"[^0-9a-z]+", "_", s.strip().lower()).strip("_")


def _infer_year(df: pd.DataFrame, idx: int, month: int) -> int:
    """
    Given the row index holding 'Total for <month>', look backward first,
    then forward, for the nearest real date in the same month and take its year.
    """
    # ← look backward
    for j in range(idx - 1, -1, -1):
        try:
            d = pd.to_datetime(df.at[j, "date"], format="%d-%b-%Y")
            if d.month == month:
                return d.year
        except ValueError:
            pass

    # → look forward (rarely needed)
    for j in range(idx + 1, len(df)):
        try:
            d = pd.to_datetime(df.at[j, "date"], format="%d-%b-%Y")
            if d.month == month:
                return d.year
        except ValueError:
            pass

    raise ValueError(f"Couldn’t infer year for monthly total at row {idx}")


def fix_date(df: pd.DataFrame) -> pd.DataFrame:
    """
    • “Total for <Month>” → last day of that month (year inferred)
      and instrument → 'total_month_<instrument>'
    • “Total for <YYYY>”  → 31-Dec-YYYY
      and instrument → 'total_year_<instrument>'
    Entire date column is returned as dtype datetime64[ns].
    """
    out = df.copy()

    for idx, val in out["date"].items():
        # ---- monthly totals -------------------------------------------------
        m = MONTH_PAT.match(val)
        if m:
            month_txt = m.group(1).lower()
            month_no = MONTH2NUM[month_txt]
            year_no = _infer_year(out, idx, month_no)
            out.at[idx, "date"] = _last_day_of_month(year_no, month_no)
            out.at[idx, "instrument"] = "total_month_" + out.at[idx, "instrument"]
            continue

        # ---- yearly totals --------------------------------------------------
        y = YEAR_PAT.match(val)
        if y:
            yr = int(y.group(1))
            out.at[idx, "date"] = pd.Timestamp(yr, 12, 31)
            out.at[idx, "instrument"] = "total_year_" + out.at[idx, "instrument"]

    # final dtype coercion (catches the ordinary day-level rows too)
    out["date"] = pd.to_datetime(out["date"])
    return out


def downloaded_for(
    target: date,
) -> bool:
    with get_connection() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        query = sql.SQL(
            """
            SELECT
                EXISTS(SELECT 1
                    FROM  "fii_investments"
                    WHERE  "date" = %s
                    AND "instrument" = 'equity_sub_total'
                    LIMIT  1)           AS has_target,
                MAX("date")   AS latest_date
            FROM "fii_investments"
            WHERE "instrument" = 'equity_sub_total'
            """
        )

        with conn.cursor() as cur:
            cur.execute(query, (target,))
            has_target, latest_date = cur.fetchone()

        if latest_date and isinstance(latest_date, datetime):
            latest_date = latest_date.date()
        return has_target, latest_date
