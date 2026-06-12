from datetime import datetime, timedelta
import calendar
from typing import List
import pandas as pd

from advisory.fallback_telemetry import record_local_fallback_event


def last_of_month(date: datetime) -> datetime:
    """Return the last day of the month for the given date."""
    return date.replace(day=calendar.monthrange(date.year, date.month)[1])


def daterange(
    start_date: datetime,
    end_date: datetime,
    *,
    first_of_month: bool = False,
    last_of_month: bool = False,
):
    """
    Yield datetimes in [start_date, end_date].

    - If first_of_month=True, yields the 1st day of each month in range.
    - If last_of_month=True, yields the last day of each month in range.
    - If both flags are False, yields every day (original behavior).

    The time-of-day and tzinfo are taken from start_date.
    """
    if start_date > end_date:
        return
    if first_of_month and last_of_month:
        raise ValueError("Set only one of first_of_month or last_of_month.")

    if first_of_month or last_of_month:
        y, m = start_date.year, start_date.month
        end_ym = (end_date.year, end_date.month)

        while (y, m) <= end_ym:
            day = 1 if first_of_month else calendar.monthrange(y, m)[1]
            dt = start_date.replace(year=y, month=m, day=day)
            if start_date <= dt <= end_date:
                yield dt
            # increment month
            if m == 12:
                y, m = y + 1, 1
            else:
                m += 1
        return

    # Daily (original behavior)
    total_days = (end_date - start_date).days
    for n in range(total_days + 1):
        yield start_date + timedelta(days=n)


def reverse_daterange(
    start_date: datetime,
    end_date: datetime,
    *,
    first_of_month: bool = False,
    last_of_month: bool = False,
):
    """
    Yield datetimes in [start_date, end_date] in reverse order.

    Flags behave the same as in `daterange`.
    """
    if start_date > end_date:
        return
    if first_of_month and last_of_month:
        raise ValueError("Set only one of first_of_month or last_of_month.")

    if first_of_month or last_of_month:
        y, m = end_date.year, end_date.month
        start_ym = (start_date.year, start_date.month)

        while (y, m) >= start_ym:
            day = 1 if first_of_month else calendar.monthrange(y, m)[1]
            dt = start_date.replace(year=y, month=m, day=day)
            if start_date <= dt <= end_date:
                yield dt
            # decrement month
            if m == 1:
                y, m = y - 1, 12
            else:
                m -= 1
        return

    # Daily reverse (original behavior)
    total_days = (end_date - start_date).days
    for n in range(total_days + 1):
        yield end_date - timedelta(days=n)

def pd_to_datetime(df: pd.DataFrame, col: str, formats: List[str], errors: str="raise") -> pd.DataFrame:
    fixed = False
    for fmt in formats:
        try:
            df[col] = pd.to_datetime(df[col], format=fmt, errors=errors)
            fixed = True
            break
        except ValueError as exc:
            record_local_fallback_event(
                module="utils.date",
                fallback_type="date_format_parse_failed",
                source="pd_to_datetime",
                severity="warn",
                reason="Date parser could not parse a column with one configured format and will try the next format.",
                error=exc,
                metadata={"column": col, "format": fmt},
            )
    if not fixed:
        raise ValueError("No valid date format found")
    return df


def remove_invalid_dates(df: pd.DataFrame, col: str) -> pd.DataFrame:
    df = df[df[col].astype(str).str.contains(r'\d{2}', na=False)]
    return df
