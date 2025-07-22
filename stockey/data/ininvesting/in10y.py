from datetime import date, datetime, timedelta
from dateutil.relativedelta import relativedelta
import pandas as pd
from environs import Env
import redis
from playwright.sync_api import sync_playwright

from utils.http import get_dynamic_headers
from utils.db import upsert_to_db

env = Env()
env.read_env()

CDP_ENDPOINT = env("CDP_ENDPOINT")

REDIS_HOST = env("REDIS_HOST")
REDIS_PORT = env("REDIS_PORT")
REDIS_SET = "gsec10y:downloaded"
rop = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, decode_responses=True)

HEADERS = get_dynamic_headers()


def yearly_chunks(start: date, end: date):
    """Yield consecutive ≤1-year slices covering [start, end]."""
    cur = start
    while cur <= end:
        nxt = min(cur + relativedelta(years=1) - timedelta(days=1), end)
        yield cur, nxt
        cur = nxt + timedelta(days=1)


def chunk_has_missing_dates(chunk_start: date, chunk_end: date) -> bool:
    """True if *any* date in [chunk_start, chunk_end] is not yet in Redis."""
    # Cheap scan: walk day-by-day until you spot a gap.
    cur = chunk_start
    while cur <= chunk_end:
        if not rop.sismember(REDIS_SET, cur.strftime("%Y-%m-%d")):
            return True
        cur += timedelta(days=1)
    return False


def mark_dates_checked(start: date, end: date):
    """Add every calendar date in [start, end] to Redis in one SADD call."""
    dates = [
        (start + timedelta(days=i)).strftime("%Y-%m-%d")
        for i in range((end - start).days + 1)
    ]
    rop.sadd(REDIS_SET, *dates)


def find_first_missing(lookback_start: date, today: date) -> date | None:
    """Walk forward from lookback_start until you hit a day not in Redis."""
    cur = lookback_start
    while cur <= today and rop.sismember(REDIS_SET, cur.strftime("%Y-%m-%d")):
        cur += timedelta(days=1)
    return None if cur > today else cur


def download_gsec_data(page, from_date: date, to_date: date):
    print(f"Downloading GSEC 10y yield data for {from_date} to {to_date}")

    page.goto(
        "https://in.investing.com/rates-bonds/india-10-year-bond-yield-historical-data",
        wait_until="domcontentloaded",
        timeout=5000,
    )
    page.wait_for_timeout(5000)

    query_str = f"start-date={from_date.strftime('%Y-%m-%d')}&end-date={to_date.strftime('%Y-%m-%d')}&time-frame=Daily&add-missing-rows=false"
    url = f"https://api.investing.com/api/financialdata/historical/24014?{query_str}"

    data = page.evaluate(
        f"""
        async () => {{
            const res = await fetch("{url}", {{
                method: "GET",
                credentials: "include",
                headers: {{
                    "domain-id": "in",
                    "accept": "*/*",
                    "origin": "https://in.investing.com",
                    "referer": "https://in.investing.com/",
                    "user-agent": navigator.userAgent
                }}
            }});
            if (!res.ok) throw new Error("HTTP " + res.status);
            return await res.json();
        }}
    """
    )

    df = pd.DataFrame(data["data"])
    df["date"] = df["rowDateRaw"].apply(lambda x: datetime.fromtimestamp(int(x)).date())
    df["date"] = pd.to_datetime(df["date"])
    df = df.iloc[:, 11:]
    df.columns = [
        "last_close",
        "last_open",
        "last_max",
        "last_min",
        "change_precent",
        "date",
    ]
    for col in ["last_close", "last_open", "last_max", "last_min", "change_precent"]:
        df[col] = pd.to_numeric(df[col], errors="ignore")

    upsert_to_db(df, "ininvesting_gsec", unique_keys=["date"])
    return df


def sync_gsec_prices(today: date | None = None):
    """
    Back-fills up to 10 years the first time, then grabs only the gaps.
    Safe to invoke daily / weekly / monthly – will never refetch a day
    thats already been checked.
    """
    today = today or date.today()
    lookback_start = today - relativedelta(years=10)
    first_missing = find_first_missing(lookback_start, today)

    if first_missing is None:
        print("✅ All calendar days in the past already checked.")
        return

    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(CDP_ENDPOINT)
        context = browser.contexts[0] if browser.contexts else browser.new_context()
        page = context.new_page()

        for c_start, c_end in yearly_chunks(first_missing, today):
            if not chunk_has_missing_dates(c_start, c_end):
                continue

            c_start = c_start - timedelta(
                days=15
            )  # For safety, data may be empty for today
            download_gsec_data(page, c_start, c_end)
            mark_dates_checked(c_start, c_end)

        browser.close()


if __name__ == "__main__":
    sync_gsec_prices()
