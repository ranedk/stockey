from datetime import datetime, timedelta


def daterange(start_date: datetime, end_date: datetime):
    """Yield dates in [start_date, end_date]"""
    for n in range(int((end_date - start_date).days) + 1):
        yield start_date + timedelta(n)


def reverse_daterange(start_date: datetime, end_date: datetime):
    """Yield dates in [start_date, end_date] in reverse order"""
    for n in range(int((end_date - start_date).days) + 1):
        yield end_date - timedelta(n)