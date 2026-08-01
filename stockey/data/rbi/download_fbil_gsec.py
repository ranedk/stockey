import tempfile
import time
import json
from datetime import date, datetime

import numpy as np
import pandas as pd
import redis
import requests
from environs import Env

from utils.fallback_telemetry import record_local_fallback_event
from utils.db import upsert_to_db
from utils.http import get_dynamic_headers
from utils.date import daterange
from utils.sync import get_redis_client

env = Env()
env.read_env()

REDIS_HOST = env("REDIS_HOST")
REDIS_PORT = env("REDIS_PORT")
DOWNLOADED = "fbilgec:downloaded"
rop = get_redis_client(REDIS_HOST, int(REDIS_PORT))


HEADERS = get_dynamic_headers()
FBIL_GSEC_LOOKBACK_DAYS = max(env.int("RBI_FBIL_GSEC_LOOKBACK_DAYS", 365), 1)
SYNC_SOURCE_NAME = "data.rbi.download_fbil_gsec"
STOCKEY_RUN_STATE: dict[str, object] = {}


def _record_fbil_gsec_fallback(
    *,
    fallback_type: str,
    reason: str,
    error: Exception | None = None,
    fdate: date | datetime | None = None,
    metadata: dict[str, object] | None = None,
) -> None:
    record_local_fallback_event(
        module=SYNC_SOURCE_NAME,
        source="rbi_fbil_gsec",
        fallback_type=fallback_type,
        severity="warn",
        reason=reason,
        error=error,
        metadata={
            "date": pd.Timestamp(fdate).date().isoformat() if fdate is not None else None,
            **(metadata or {}),
        },
    )


def get_cookies():
    response = requests.get("https://www.fbil.org.in/", headers=HEADERS)
    return response.cookies.get_dict()


def try_parsing_date(text):
    for fmt in ("%d-%b-%Y", "%d %b, %Y", "%d/%b/%Y", "%d/%b/%y"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError as exc:
            _record_fbil_gsec_fallback(
                fallback_type="fbil_gsec_date_format_parse_failed",
                source="rbi_fbil_gsec",
                reason="RBI FBIL G-sec date parser rejected one candidate format and will try the next supported format.",
                error=exc,
                metadata={"raw_date": str(text), "format": fmt},
            )
    raise ValueError("no valid date format found")


def parse_xls(xls_path, fdate):
    df = pd.read_excel(xls_path, sheet_name="G-Sec")
    trade_date = df.iloc[1, 2]
    try:
        if not isinstance(trade_date, (date, datetime)):
            trade_date = try_parsing_date(trade_date)
    except (ValueError, TypeError) as exc:
        _record_fbil_gsec_fallback(
            fallback_type="fbil_gsec_trade_date_parse_failed",
            reason="FBIL G-sec sheet trade date could not be parsed; using requested file date.",
            error=exc,
            fdate=fdate,
            metadata={"raw_trade_date": str(trade_date)},
        )
        trade_date = fdate

    expected_cols = [
        "isin",
        "coupon_pct",
        "maturity_date",
        "clean_price",
        "ytm_sa",
        "remark1",
        "remark2",
        "liquidity_signal",
    ]
    df_quote = pd.read_excel(xls_path, sheet_name="G-Sec", skiprows=5)
    df_quote = df_quote.dropna(axis=1, how="all")
    df_quote = df_quote.iloc[:, : len(expected_cols)]
    df_quote.columns = expected_cols[: df_quote.shape[1]]

    for col in expected_cols[df_quote.shape[1] :]:
        df_quote[col] = np.nan
    df_quote = df_quote[expected_cols]
    df_quote = df_quote.dropna(subset=["isin", "coupon_pct"])

    try:
        df_par = pd.read_excel(xls_path, sheet_name="Par Yield", skiprows=5)
    except ValueError as exc:
        _record_fbil_gsec_fallback(
            fallback_type="fbil_gsec_par_yield_sheet_fallback",
            reason="FBIL G-sec workbook did not contain 'Par Yield'; trying legacy 'Par-Yield' sheet.",
            error=exc,
            fdate=fdate,
        )
        df_par = pd.read_excel(xls_path, sheet_name="Par-Yield", skiprows=5)
    df_par = df_par.iloc[:, :3]
    df_par.columns = [
        "tenor_years",
        "par_yield_sa",
        "par_yield_ann",
    ]
    df_par = df_par.dropna(axis=1, how="all")

    # Convert types
    df_quote["coupon_pct"] = pd.to_numeric(df_quote["coupon_pct"], errors="coerce")
    df_quote["clean_price"] = df_quote["clean_price"].astype(float)
    df_quote["ytm_sa"] = df_quote["ytm_sa"].astype(float)
    df_quote["maturity_date"] = pd.to_datetime(
        df_quote["maturity_date"], format="%d-%b-%Y"
    )
    df_quote = df_quote.dropna(subset=["coupon_pct"])
    df_quote = df_quote.drop_duplicates("isin", keep="last")

    df_par = df_par.dropna(axis=1, how="all")
    df_par = df_par.dropna(subset=["tenor_years", "par_yield_sa", "par_yield_ann"])
    df_par["tenor_years"] = df_par["tenor_years"].astype(float)
    df_par[["par_yield_sa", "par_yield_ann"]] = df_par[
        ["par_yield_sa", "par_yield_ann"]
    ].astype(float)

    df_quote.insert(0, "trade_date", trade_date)
    df_par.insert(0, "trade_date", trade_date)

    return df_quote, df_par


def download_gsec(fdate: date, cookies):
    if fdate.strftime("%a").lower() in ["sat", "sun"]:
        return {
            "date": fdate.isoformat(),
            "status": "skipped_weekend",
            "rows": 0,
            "quote_rows": 0,
            "par_rows": 0,
        }

    formatted_date = fdate.strftime("%Y-%m-%d")
    print("GSec for ", formatted_date)

    params = {
        #'date': '2025-06-05', # format date
        "date": formatted_date
    }
    url = "https://www.fbil.org.in/wasdm/gsec/downloadPublished"
    s = requests.Session()
    response = s.get(
        url, params=params, headers=HEADERS, cookies=cookies, stream=True, timeout=30
    )
    if response.status_code != 200:
        print("Skipping (with error) GSec for ", formatted_date, fdate.strftime("%a"))
        return {
            "date": formatted_date,
            "status": "source_unavailable",
            "status_code": int(response.status_code),
            "rows": 0,
            "quote_rows": 0,
            "par_rows": 0,
        }

    with tempfile.NamedTemporaryFile(suffix=".xls", delete=False) as tmp:
        tmp.write(response.content)
        print(tmp.name)
        df_quote, df_par = parse_xls(tmp.name, fdate)

    df_quote = df_quote.rename(columns={"trade_date": "date"})
    df_par = df_par.rename(columns={"trade_date": "date"})

    df_quote["date"] = pd.to_datetime(df_quote["date"], format="%Y-%m-%d")
    df_par["date"] = pd.to_datetime(df_par["date"], format="%Y-%m-%d")

    upsert_to_db(
        df_quote,
        "fbil_gsec_quote",
        unique_keys=["date", "isin"],
        timescaledb_column="date",
    )
    upsert_to_db(
        df_par,
        "fbil_gsec_par",
        unique_keys=["date", "tenor_years"],
        timescaledb_column="date",
    )

    rop.set(DOWNLOADED, formatted_date)
    time.sleep(1)
    print("Downloaded GSec for ", formatted_date)
    return {
        "date": formatted_date,
        "status": "downloaded",
        "rows": int(len(df_quote) + len(df_par)),
        "quote_rows": int(len(df_quote)),
        "par_rows": int(len(df_par)),
    }


def download_all_gsec_data() -> dict[str, object]:
    cookies = get_cookies()
    today = datetime.now()
    from_date = rop.get(DOWNLOADED)
    if from_date:
        from_date = datetime.strptime(from_date, "%Y-%m-%d")
    else:
        from_date = datetime.combine(date.today(), datetime.min.time()) - pd.Timedelta(
            days=FBIL_GSEC_LOOKBACK_DAYS
        )

    state: dict[str, object] = {
        "source": SYNC_SOURCE_NAME,
        "rows": 0,
        "rows_read": 0,
        "rows_written": 0,
        "from_date": pd.Timestamp(from_date).date().isoformat(),
        "to_date": pd.Timestamp(today).date().isoformat(),
        "date_count": 0,
        "downloaded_date_count": 0,
        "skipped_weekend_count": 0,
        "failed_date_count": 0,
        "attempt_count": 0,
        "download_attempts": 0,
        "failed_attempt_count": 0,
        "source_unavailable_count": 0,
        "fallback_used": False,
        "state_advanced": False,
    }
    for fdate in daterange(from_date, today):
        state["date_count"] = int(state["date_count"]) + 1
        try:
            result = download_gsec(fdate, cookies=cookies) or {
                "date": pd.Timestamp(fdate).date().isoformat(),
                "status": "no_result",
                "rows": 0,
            }
        except Exception as exc:
            _record_fbil_gsec_fallback(
                fallback_type="fbil_gsec_download_failed",
                reason="FBIL G-sec date download or parse failed; stopping catch-up at this date.",
                error=exc,
                fdate=fdate,
            )
            state["attempt_count"] = int(state["attempt_count"]) + 1
            state["download_attempts"] = int(state["download_attempts"]) + 1
            state["failed_attempt_count"] = int(state["failed_attempt_count"]) + 1
            state["failed_date_count"] = int(state["failed_date_count"]) + 1
            state["source_unavailable_count"] = int(state["source_unavailable_count"]) + 1
            failed_dates = state.setdefault("failed_dates", [])
            if isinstance(failed_dates, list):
                failed_dates.append(
                    {
                        "date": pd.Timestamp(fdate).date().isoformat(),
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                )
            break
        status = str(result.get("status") or "")
        if status == "downloaded":
            rows = int(result.get("rows") or 0)
            state["attempt_count"] = int(state["attempt_count"]) + 1
            state["download_attempts"] = int(state["download_attempts"]) + 1
            state["downloaded_date_count"] = int(state["downloaded_date_count"]) + 1
            state["rows"] = int(state["rows"]) + rows
            state["rows_written"] = int(state["rows_written"]) + rows
            state["state_advanced"] = bool(state["state_advanced"]) or rows > 0
        elif status == "skipped_weekend":
            state["skipped_weekend_count"] = int(state["skipped_weekend_count"]) + 1
        else:
            state["attempt_count"] = int(state["attempt_count"]) + 1
            state["download_attempts"] = int(state["download_attempts"]) + 1
            state["failed_attempt_count"] = int(state["failed_attempt_count"]) + 1
            state["failed_date_count"] = int(state["failed_date_count"]) + 1
            state["source_unavailable_count"] = int(state["source_unavailable_count"]) + 1
    state["rows_read"] = int(state["date_count"])
    return state


def main() -> int:
    global STOCKEY_RUN_STATE
    STOCKEY_RUN_STATE = download_all_gsec_data()
    status = "partial" if int(STOCKEY_RUN_STATE.get("failed_date_count") or 0) else "ok"
    print(
        json.dumps({"status": status, **STOCKEY_RUN_STATE}, ensure_ascii=False, default=str),
        flush=True,
    )
    return 0


if __name__ == "__main__":
    # parse_xls("")
    # parse_xls("")
    raise SystemExit(main())
