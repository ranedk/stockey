"""
us_macro_fred.py
----------------
Download & cache core U-S macro time-series from FRED.

Dependencies
------------
pip install pandas pandas-datareader pyarrow  # pyarrow for Parquet

Optional: set an env-var  FRED_API_KEY=your_key  (recommended by FRED,
but pandas-datareader still works anonymously for these series).
"""

from __future__ import annotations

import io
import json
from environs import Env
from datetime import date, timedelta
from typing import Dict
from urllib.parse import urlencode

import pandas as pd

from advisory.fallback_telemetry import record_local_fallback_event
from utils.db import table_has_date, upsert_to_db
from utils.http import get_with_retries

env = Env()
env.read_env()

__FRED_SERIES: Dict[str, str] = {
    # Rates & risk-sentiment
    "DGS10": "ust10y_yield",  # 10-year Treasury constant-maturity
    "EFFR": "fedfunds_eff",  # Effective Fed-funds rate (daily)
    "VIXCLS": "vix_close",  # CBOE VIX close
    # Growth & inflation
    "PAYEMS": "nonfarm_payrolls",  # NFP (level, ‘000)
    "CPIAUCSL": "cpi_headline",  # CPI SA
    "CPILFESL": "cpi_core",  # CPI core SA
    # USD & commodities
    "DTWEXBGS": "broad_usd_index",  # Trade-weighted dollar (goods only)
    "DCOILWTICO": "wti_crude_spot",  # WTI crude spot $/bbl
    "NGDPRNSAXDCINQ": "india_gdp",  # India GDP numbers
    "DEXINUS": "inr_usd_spot",  # INR USD Spot price
}

FRED_CSV_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv"
FRED_TIMEOUT_SECONDS = 12
FRED_RETRIES = 3
FRED_BACKOFF_FACTOR = 0.5
ISM_TIMEOUT_SECONDS = 20
ISM_RETRIES = 3
FRED_MACRO_LOOKBACK_DAYS = max(env.int("FRED_US_MACRO_LOOKBACK_DAYS", 365), 1)
SYNC_SOURCE_NAME = "data.fred.us_macro"
STOCKEY_RUN_STATE: dict[str, object] = {}
LAST_FRED_SERIES_STATE: dict[str, object] = {}


def _record_macro_leg_fallback(*, leg: str, error: Exception) -> None:
    record_local_fallback_event(
        module=SYNC_SOURCE_NAME,
        fallback_type="fred_macro_leg_failed",
        source=f"{SYNC_SOURCE_NAME}:{leg}",
        severity="warn",
        reason=(
            "US macro sync leg failed; macro/regime context may be partial while the remaining "
            "macro legs continue."
        ),
        error=error,
        metadata={"leg": leg},
    )


def _fetch_single_fred_series(
    series_id: str,
    *,
    start: date,
    end: date,
) -> pd.Series | None:
    url = f"{FRED_CSV_URL}?{urlencode({'id': series_id, 'cosd': start.isoformat(), 'coed': end.isoformat()})}"
    response = get_with_retries(
        url,
        timeout=FRED_TIMEOUT_SECONDS,
        retries=FRED_RETRIES,
        backoff_factor=FRED_BACKOFF_FACTOR,
        from_cache=False,
    )
    df = pd.read_csv(io.StringIO(response.text))
    if df.empty:
        return None
    df.columns = [str(col).strip().upper() for col in df.columns]
    date_column = "DATE" if "DATE" in df.columns else "OBSERVATION_DATE"
    value_column = series_id.upper() if series_id.upper() in df.columns else "VALUE"
    if date_column not in df.columns or value_column not in df.columns:
        return None
    df[date_column] = pd.to_datetime(df[date_column], errors="coerce")
    df[value_column] = pd.to_numeric(df[value_column], errors="coerce")
    df = df.dropna(subset=[date_column])
    if df.empty:
        return None
    series_data = df.set_index(date_column)[value_column]
    series_data.name = series_id
    return series_data


def fetch_fred_series(
    series: Dict[str, str] = __FRED_SERIES,
    resample: str | None = "D",  # "D"→daily, None→native freq.
) -> pd.DataFrame:
    """
    Returns a DataFrame with friendly column names, one column per series.
    Missing values (weekends, holidays) are forward-filled after resampling.
    """
    global LAST_FRED_SERIES_STATE
    today = date.today()
    state: dict[str, object] = {
        "source": f"{SYNC_SOURCE_NAME}:fred",
        "series_count": int(len(series)),
        "succeeded_series_count": 0,
        "failed_series_count": 0,
        "attempt_count": int(len(series)),
        "download_attempts": int(len(series)),
        "failed_attempt_count": 0,
        "source_unavailable_count": 0,
        "fallback_count": 0,
        "fallback_used": False,
        "no_data_count": 0,
        "rows": 0,
        "rows_read": 0,
        "rows_written": 0,
        "state_advanced": False,
    }
    try:
        found, latest_date = table_has_date("macro_usa", "date", today)
    except Exception as exc:
        fallback_latest_date = today - timedelta(days=FRED_MACRO_LOOKBACK_DAYS)
        record_local_fallback_event(
            module=SYNC_SOURCE_NAME,
            source="macro_usa:date",
            fallback_type="fred_macro_table_date_fallback",
            severity="warn",
            reason=(
                "FRED macro latest-date lookup failed; using configured lookback window before fetching "
                "macro series."
            ),
            error=exc,
            metadata={
                "table": "macro_usa",
                "date_column": "date",
                "fallback_latest_date": fallback_latest_date.isoformat(),
                "lookback_days": FRED_MACRO_LOOKBACK_DAYS,
            },
        )
        print(f"FRED table_has_date fallback for macro_usa: {exc.__class__.__name__}: {exc}", flush=True)
        found = False
        latest_date = fallback_latest_date
        state["fallback_count"] = 1
        state["fallback_used"] = True
        state["fallback_reason"] = f"{type(exc).__name__}: {exc}"

    start = latest_date - timedelta(
        days=15
    )  # Some values like inr_usd get updated later
    end = date.today()
    state["from_date"] = start.isoformat()
    state["to_date"] = end.isoformat()

    if found:
        # No need to query further if table has latest data
        state["status"] = "skipped_current"
        state["no_data_count"] = int(len(series))
        LAST_FRED_SERIES_STATE = state
        return
    if latest_date >= today:
        # latest_date is today, no need to query further
        state["status"] = "skipped_current"
        state["no_data_count"] = int(len(series))
        LAST_FRED_SERIES_STATE = state
        return

    print("Pulling data %s to %s" % (start, end), flush=True)

    series_frames = []
    failed_series = []
    for series_id, friendly_name in series.items():
        print(f"Fetching FRED series {series_id} -> {friendly_name}", flush=True)
        try:
            series_data = _fetch_single_fred_series(series_id, start=start, end=end)
        except Exception as exc:
            failed_series.append(series_id)
            record_local_fallback_event(
                module=SYNC_SOURCE_NAME,
                source=f"fred:{series_id}",
                fallback_type="fred_series_fetch_failed",
                severity="warn",
                reason=(
                    "FRED series fetch failed; macro context will be partial while remaining series continue."
                ),
                error=exc,
                metadata={
                    "series_id": series_id,
                    "friendly_name": friendly_name,
                    "from_date": start.isoformat(),
                    "to_date": end.isoformat(),
                },
            )
            print(f"FRED series fetch failed for {series_id}: {exc}", flush=True)
            continue
        if series_data is None:
            failed_series.append(series_id)
            print(f"FRED series returned no usable data for {series_id}", flush=True)
            continue
        print(f"Fetched FRED series {series_id} rows={len(series_data)}", flush=True)
        series_frames.append(series_data.rename(friendly_name))

    if not series_frames:
        print("FRED fetch skipped: no series data available after retries", flush=True)
        state.update(
            {
                "status": "source_unavailable" if failed_series else "no_data",
                "failed_series_count": int(len(failed_series)),
                "failed_attempt_count": int(len(failed_series)),
                "source_unavailable_count": int(len(failed_series)),
                "no_data_count": int(max(len(series) - len(failed_series), 0)),
            }
        )
        if failed_series:
            state["failed_series"] = sorted(failed_series)
        LAST_FRED_SERIES_STATE = state
        return pd.DataFrame()

    df = pd.concat(series_frames, axis=1).sort_index()

    if resample:
        df = df.resample(resample).last().ffill()

    df = df.reset_index()
    df.columns = ["date" if str(col).lower() in {"date", "observation_date"} else str(col) for col in df.columns]

    if "india_gdp" in df.columns:
        df_india_gdp = df[["date", "india_gdp"]].dropna(subset=["india_gdp"], how="all")
        if not df_india_gdp.empty:
            upsert_to_db(
                df_india_gdp,
                "macro_india_gdp",
                unique_keys=["date"],
                timescaledb_column="date",
            )

    usa_columns = [col for col in df.columns if col not in {"date", "india_gdp"}]
    if usa_columns:
        df_usa = df[["date", *usa_columns]]
        upsert_to_db(
            df_usa,
            "macro_usa",
            unique_keys=["date"],
            timescaledb_column="date",
        )

    if failed_series:
        print(f"FRED completed with partial failures: {', '.join(sorted(failed_series))}", flush=True)

    state.update(
        {
            "status": "partial" if failed_series else "ok",
            "succeeded_series_count": int(len(series_frames)),
            "failed_series_count": int(len(failed_series)),
            "failed_attempt_count": int(len(failed_series)),
            "source_unavailable_count": int(len(failed_series)),
            "rows": int(len(df)),
            "rows_read": int(len(df)),
            "rows_written": int(len(df)),
            "state_advanced": bool(len(df) > 0),
        }
    )
    if failed_series:
        state["failed_series"] = sorted(failed_series)
    LAST_FRED_SERIES_STATE = state
    return df


def fetch_ism_manufacturing():
    headers = {
        "accept": "*/*",
        "accept-language": "en-US,en;q=0.9,uz;q=0.8",
        "origin": "https://www.fxstreet.com",
        "priority": "u=1, i",
        "referer": "https://www.fxstreet.com/",
        "sec-ch-ua": '"Not(A:Brand";v="99", "Google Chrome";v="133", "Chromium";v="133"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Linux"',
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "cross-site",
        "user-agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36",
    }

    print("Fetching ISM manufacturing history", flush=True)
    response = get_with_retries(
        "https://calendar-api.fxsstatic.com/en/api/v1/events/2e1d69f3-8273-4096-b01b-8d2034d4fade/historical",
        headers=headers,
        timeout=ISM_TIMEOUT_SECONDS,
        retries=ISM_RETRIES,
        backoff_factor=0.5,
        from_cache=False,
    )
    ism = response.json()
    df = pd.DataFrame.from_dict(ism)
    df = df.drop(columns=["id", "ratioDeviation"]).rename(columns={"dateUtc": "date", "periodDateUtc": "for_month"})
    df['date'] = pd.to_datetime(df['date'])

    upsert_to_db(
        df,
        "macro_usa_ism",
        unique_keys=["date"],
        timescaledb_column="date",
    )
    print(f"Fetched ISM manufacturing rows={len(df)}", flush=True)
    return df


def run_macro_sync() -> dict[str, object]:
    state: dict[str, object] = {
        "source": SYNC_SOURCE_NAME,
        "rows": 0,
        "rows_read": 0,
        "rows_written": 0,
        "leg_count": 2,
        "succeeded_leg_count": 0,
        "failed_leg_count": 0,
        "attempt_count": 2,
        "download_attempts": 2,
        "failed_attempt_count": 0,
        "source_unavailable_count": 0,
        "fallback_count": 0,
        "fallback_used": False,
        "state_advanced": False,
    }
    failed_legs: list[dict[str, str]] = []

    try:
        fred_df = fetch_fred_series()
    except Exception as exc:
        _record_macro_leg_fallback(leg="fred", error=exc)
        print(f"FRED macro download skipped: {exc}", flush=True)
        fred_df = pd.DataFrame()
        failed_legs.append({"leg": "fred", "error": f"{type(exc).__name__}: {exc}"})
    else:
        fred_state = dict(LAST_FRED_SERIES_STATE)
        state["fallback_count"] = int(state["fallback_count"]) + int(fred_state.get("fallback_count") or 0)
        state["fallback_used"] = bool(state["fallback_used"]) or bool(fred_state.get("fallback_used"))
        state["source_unavailable_count"] = int(state["source_unavailable_count"]) + int(
            fred_state.get("source_unavailable_count") or 0
        )
        if str(fred_state.get("status") or "") in {"ok", "partial", "skipped_current"}:
            state["succeeded_leg_count"] = int(state["succeeded_leg_count"]) + 1
    try:
        ism_df = fetch_ism_manufacturing()
    except Exception as exc:
        _record_macro_leg_fallback(leg="ism", error=exc)
        print(f"ISM manufacturing download skipped: {exc}", flush=True)
        ism_df = pd.DataFrame()
        failed_legs.append({"leg": "ism", "error": f"{type(exc).__name__}: {exc}"})
    else:
        state["succeeded_leg_count"] = int(state["succeeded_leg_count"]) + 1

    if failed_legs:
        state["failed_legs"] = failed_legs
    state["failed_leg_count"] = int(len(failed_legs))
    state["failed_attempt_count"] = int(state["failed_attempt_count"]) + int(len(failed_legs))

    fred_rows = int(len(fred_df)) if isinstance(fred_df, pd.DataFrame) else 0
    ism_rows = int(len(ism_df)) if isinstance(ism_df, pd.DataFrame) else 0
    state["rows"] = fred_rows + ism_rows
    state["rows_read"] = fred_rows + ism_rows
    state["rows_written"] = fred_rows + ism_rows
    state["fred_rows"] = fred_rows
    state["ism_rows"] = ism_rows
    state["state_advanced"] = bool(fred_rows + ism_rows > 0)
    if state["failed_leg_count"]:
        state["status"] = "partial" if state["succeeded_leg_count"] else "failed"
    else:
        state["status"] = "ok"
    return state


def main() -> int:
    global STOCKEY_RUN_STATE
    STOCKEY_RUN_STATE = run_macro_sync()
    status = str(STOCKEY_RUN_STATE.get("status") or "ok")
    print(
        json.dumps({"status": status, **STOCKEY_RUN_STATE}, ensure_ascii=False, default=str),
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
