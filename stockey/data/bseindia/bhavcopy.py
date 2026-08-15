"""BSE daily EOD bhavcopy collector -- closes the price/technicals gap a fundamentals
audit found live 2026-08-15: 43 of the active fundamentals_l1_universe are genuinely
BSE-only (dhan_bse_id populated, dhan_nse_id NULL -- confirmed live, not the earlier
"10" figure, which predated recent company_master identity fixes) and had zero rows
in advisory_adjusted_ohlcv_daily, since stockey's whole OHLCV pipeline was NSE/Dhan-
only. fundamentals/screens/technicals.py had nothing to compute for them.

BSE publishes ONE CSV per trading day, market-wide (all ~4,900 listed instruments, no
per-scrip filter needed) -- confirmed live 2026-08-15:
    https://www.bseindia.com/download/BhavCopy/Equity/BhavCopy_BSE_CM_0_0_0_YYYYMMDD_F_0000.CSV
Same UDiFF column layout NSE's own UDiFF bhavcopy already uses (TradDt/ISIN/TckrSymb/
SctySrs/OpnPric/HghPric/LwPric/ClsPric/...) -- confirmed live back to 2024-01-01 (an
HTML "not found" page below that date; BSE's own switch to this format/naming, the
same era NSE made an equivalent switch, see data/nseindia/bhavcopy_history.py). Plain
`requests.get()` through exchange_request_gate(domain="bse") -- no CDP/browser needed
(confirmed live: a static file download, not an interactive page), matching
fundamentals/collectors/bse_announcements.py's own precedent and BSE_HEADERS.

One market-wide file/day, not one request per company, on purpose: at the 10s
BSE_MIN_REQUEST_INTERVAL_SECONDS floor, looping the 43 target companies individually
would cost 7+ minutes/day and would need re-scoping every time the L1 universe's
BSE-only count changes (it already moved 10->43 in one session) -- one request/day
costs the same regardless of how many BSE-only companies exist. Stores the WHOLE
day's file (all series, all ~4,900 instruments), matching NSE bhavcopy's own
"exchange-wide, not narrowed to L1" convention -- storage cost is trivial (~750KB-
840KB/day) and avoids re-scoping again the next time the BSE-only count drifts.

Identity: FinInstrmId is BSE's own numeric scrip code, which IS what company_master.
bse_ticker/bse_scrip_code/dhan_bse_id all actually store (confirmed live 2026-08-15 --
BSE's own TckrSymb ticker text, e.g. "SUNDROP", does NOT match company_master.
bse_ticker, which holds the scrip code "500215" instead). So identity resolution
below joins scrip_code (== FinInstrmId) against company_master via
attach_company_master_id(exchange="BSE"), NOT the TckrSymb text.

Series scope: confirmed live against the actual 43-company BSE-only target universe
(not guessed) -- 25/43 trade in SctySrs='X', 13/43 in 'XT', 2 in 'B', 2 in 'M'/'MT'.
BSE's 'X'/'XT' groups are NOT a trade-to-trade-only oddity to exclude here -- they are
the PRIMARY series for exactly this screener's smallcap/microcap universe. PRIMARY_
SERIES below reflects that live finding; every row from every SctySrs still lands in
bseindia_ohlcv (matching NSE's "store everything" convention), the series allowlist
is applied only downstream, at the adjustment-factors/view step (data/bseindia/
price_adjustment.py), same layering NSE uses (nseindia_ohlcv stores every series,
advisory_adjusted_ohlcv_daily's view filters to EQ/BE).

Rows land in bseindia_ohlcv, deliberately a SEPARATE table from nseindia_ohlcv (not
reused): an NSE symbol and a BSE TckrSymb could collide on plain text, and
nseindia_ohlcv's unique key (date, symbol, series) has no exchange column to
disambiguate -- scrip_code (the true BSE identity key) has no NSE equivalent to
collide with in the first place.
"""

from __future__ import annotations

import io
import json
from datetime import date, datetime, timedelta

import pandas as pd
import requests
from environs import Env

from utils.company_master import attach_company_master_id
from utils.db import db_session, execute_db_operation, sql_to_df, upsert_to_db
from utils.exchange_rate_limiter import exchange_request_gate
from utils.fallback_telemetry import record_local_fallback_event

env = Env()
env.read_env()

SYNC_SOURCE_NAME = "data.bseindia.bhavcopy"
STOCKEY_RUN_STATE: dict[str, object] = {}

# Deliberately a local constant, not imported from fundamentals/collectors/security_
# master.py's own BSE_HEADERS -- data/ is the foundation fundamentals/ builds on top
# of (docs/DATA_INVENTORY.md, CLAUDE.md's boundary section), so a data/ module must
# not depend on a fundamentals/ one, even though the two headers dicts are identical
# in substance. Matches data/nseindia/bhavcopy_history.py's own local UA/HEADERS.
_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
BSE_HEADERS = {"User-Agent": _UA, "Referer": "https://www.bseindia.com/", "Accept": "application/json, text/plain, */*"}

RESULTS_TABLE = "bseindia_ohlcv"
BSE_BHAVCOPY_DOWNLOAD_LOOKBACK_DAYS = max(env.int("BSE_BHAVCOPY_DOWNLOAD_LOOKBACK_DAYS", 365), 1)
# BSE's UDiFF-format bhavcopy is only available from this date onward (confirmed live
# 2026-08-15: dates before this return a 200 with an HTML "not found" page, not a CSV) --
# a backfill request older than this is never fetchable via this URL pattern.
BSE_BHAVCOPY_EARLIEST_DATE = date(2024, 1, 1)
# Series confirmed live against the real 43-company BSE-only target universe -- see
# module docstring. Not used to filter what's STORED (everything lands in
# bseindia_ohlcv); consumed downstream by price_adjustment.py's adjusted view.
PRIMARY_SERIES = ("A", "B", "X", "XT", "M", "MT")

OUT_COLS = [
    "scrip_code", "symbol", "series", "isin", "open", "high", "low", "close", "last",
    "previous_close", "volume", "total_value", "number_of_trades", "date", "load_ts",
]

_TABLE_STATEMENT = """
    CREATE TABLE IF NOT EXISTS bseindia_ohlcv (
        scrip_code TEXT NOT NULL,
        symbol TEXT,
        series TEXT NOT NULL,
        isin TEXT,
        open DOUBLE PRECISION,
        high DOUBLE PRECISION,
        low DOUBLE PRECISION,
        close DOUBLE PRECISION,
        last DOUBLE PRECISION,
        previous_close DOUBLE PRECISION,
        volume BIGINT,
        total_value DOUBLE PRECISION,
        number_of_trades BIGINT,
        date TIMESTAMPTZ NOT NULL,
        company_master_id TEXT,
        load_ts TIMESTAMPTZ,
        UNIQUE (date, scrip_code, series)
    )
"""


def _record_fallback(fallback_type: str, *, reason: str, error, severity: str = "warn", metadata=None) -> None:
    record_local_fallback_event(
        module=SYNC_SOURCE_NAME,
        source="bse",
        fallback_type=fallback_type,
        severity=severity,
        reason=reason,
        error=error,
        metadata=metadata or {},
    )


def ensure_ohlcv_table() -> None:
    def _op() -> None:
        with db_session() as (_, cur):
            cur.execute(_TABLE_STATEMENT)

    execute_db_operation(_op, operation_name="bseindia_ohlcv:ensure_table")


def bhavcopy_url(d: date) -> str:
    return f"https://www.bseindia.com/download/BhavCopy/Equity/BhavCopy_BSE_CM_0_0_0_{d.strftime('%Y%m%d')}_F_0000.CSV"


class BseBhavcopyNotAvailableError(RuntimeError):
    """Raised when BSE returns 200 with its HTML 'not found' shell instead of a real
    CSV (a holiday, a future date, or a date before BSE_BHAVCOPY_EARLIEST_DATE) --
    distinguished from a real fetch failure so callers don't retry it as an error."""


def fetch_bhavcopy_csv(d: date, *, session: requests.Session | None = None) -> bytes:
    """One BSE bhavcopy CSV, rate-gated. Raises BseBhavcopyNotAvailableError for a
    non-trading day (BSE serves an HTML page, not a 404, for those -- confirmed live
    2026-08-15), or the underlying requests exception for anything else."""
    sess = session or requests.Session()
    with exchange_request_gate(domain="bse"):
        response = sess.get(bhavcopy_url(d), headers=BSE_HEADERS, timeout=30)
    response.raise_for_status()
    content = response.content
    if not content.startswith(b"TradDt,"):
        raise BseBhavcopyNotAvailableError(f"BSE bhavcopy for {d} is not a CSV response (holiday or unavailable date)")
    return content


def parse_bhavcopy(csv_bytes: bytes) -> pd.DataFrame:
    """Raw BSE UDiFF CSV -> bseindia_ohlcv row shape. Pure (bytes in, DataFrame out) --
    unit-tested on a captured fixture, matching data/nseindia/bhavcopy_history.py's
    own parse_udiff()."""
    df = pd.read_csv(io.BytesIO(csv_bytes))
    df.columns = [c.strip() for c in df.columns]
    out = pd.DataFrame(
        {
            "scrip_code": df["FinInstrmId"].astype(str).str.strip(),
            "symbol": df["TckrSymb"].astype(str).str.strip(),
            "series": df["SctySrs"].astype(str).str.strip(),
            "isin": df["ISIN"].astype(str).str.strip(),
            "open": df["OpnPric"],
            "high": df["HghPric"],
            "low": df["LwPric"],
            "close": df["ClsPric"],
            "last": df["LastPric"],
            "previous_close": df["PrvsClsgPric"],
            "volume": df["TtlTradgVol"],
            "total_value": df["TtlTrfVal"],
            "number_of_trades": df["TtlNbOfTxsExctd"],
            "date": pd.to_datetime(df["TradDt"], utc=True),
        }
    )
    return out[out["scrip_code"].notna() & (out["scrip_code"] != "nan")]


def attach_identity(df: pd.DataFrame) -> pd.DataFrame:
    """scrip_code -> company_master_id, via the numeric-scrip-code join (see module
    docstring) -- not the TckrSymb text, which does not match company_master."""
    if df.empty:
        return df.assign(company_master_id=pd.Series(dtype="string"))
    return attach_company_master_id(df, ticker_column="scrip_code", exchange="BSE")


_NON_TRADING_TABLE_STATEMENT = """
    CREATE TABLE IF NOT EXISTS bseindia_non_trading_dates (
        date DATE PRIMARY KEY,
        checked_at TIMESTAMPTZ
    )
"""


def ensure_non_trading_table() -> None:
    def _op() -> None:
        with db_session() as (_, cur):
            cur.execute(_NON_TRADING_TABLE_STATEMENT)

    execute_db_operation(_op, operation_name="bseindia_non_trading_dates:ensure_table")


def load_known_non_trading_dates() -> set[date]:
    """Weekday holidays BSE has already confirmed (via a live 'not found' response)
    it will never serve a bhavcopy for -- persisted so a holiday isn't re-fetched
    every single run forever (found live 2026-08-15: without this, a holiday inside
    the lookback window never becomes "downloaded" and reappears as a missing
    candidate on every future run indefinitely)."""
    ensure_non_trading_table()
    df = sql_to_df("SELECT date FROM bseindia_non_trading_dates")
    if df.empty:
        return set()
    return {pd.Timestamp(d).date() for d in df["date"]}


def _mark_non_trading_date(d: date) -> None:
    def _op() -> None:
        with db_session() as (_, cur):
            cur.execute(
                "INSERT INTO bseindia_non_trading_dates (date, checked_at) VALUES (%s, %s) ON CONFLICT (date) DO NOTHING",
                (d, pd.Timestamp.now(tz="UTC")),
            )

    execute_db_operation(_op, operation_name="bseindia_non_trading_dates:insert")


def collect_dates(dates: list[date], *, dry_run: bool = False) -> dict[str, object]:
    """Download + parse + upsert exactly this list of dates (caller has already
    deduped against what's downloaded/known-non-trading -- this function does not
    re-derive a date range and walk it, see collect_range()/run_bse_bhavcopy_
    collection()'s own docstrings for why that distinction matters)."""
    ensure_ohlcv_table()
    session = requests.Session()
    days_written = 0
    rows_written = 0
    non_trading_days = 0
    failed_days: list[str] = []
    consecutive_failures = 0
    blocked = False

    for d in sorted(dates):
        if blocked:
            break
        try:
            csv_bytes = fetch_bhavcopy_csv(d, session=session)
        except BseBhavcopyNotAvailableError:
            non_trading_days += 1
            if not dry_run:
                _mark_non_trading_date(d)
            continue
        except Exception as exc:  # noqa: BLE001 -- classified as a failure either way
            consecutive_failures += 1
            failed_days.append(str(d))
            _record_fallback(
                "bse_bhavcopy_fetch_failed",
                reason="BSE bhavcopy fetch failed for this date; it stays missing and is retried on the next run.",
                error=exc,
                metadata={"date": str(d)},
            )
            if consecutive_failures >= 3:
                blocked = True
                _record_fallback(
                    "bse_bhavcopy_circuit_breaker_tripped",
                    reason="3 consecutive BSE bhavcopy fetch failures -- stopping this run rather than continuing to hit a possibly-blocking BSE.",
                    error="circuit breaker",
                    severity="error",
                )
            continue

        consecutive_failures = 0
        parsed = parse_bhavcopy(csv_bytes)
        if not parsed.empty:
            parsed = attach_identity(parsed)
            parsed["load_ts"] = pd.Timestamp.now(tz="UTC")
            if not dry_run:
                upsert_to_db(parsed[[*OUT_COLS, "company_master_id"]], RESULTS_TABLE, unique_keys=["date", "scrip_code", "series"])
            days_written += 1
            rows_written += len(parsed)

    return {
        "days_written": days_written,
        "rows_written": rows_written,
        "non_trading_days": non_trading_days,
        "failed_days": failed_days,
        "blocked": blocked,
    }


def collect_range(from_date: date, to_date: date, *, dry_run: bool = False) -> dict[str, object]:
    """CLI/backfill entrypoint over a plain [from_date, to_date] span -- skips
    weekends outright (BSE is closed) but otherwise makes no assumption about
    what's already downloaded (a manual backfill call is expected to name the
    exact range it wants); collect_dates() re-fetches every weekday in range
    regardless of bseindia_ohlcv's current contents. For the daily incremental
    collector that DOES need to skip already-downloaded/known-non-trading dates,
    see run_bse_bhavcopy_collection() below -- it does not call this function."""
    dates = [from_date + timedelta(days=i) for i in range((to_date - from_date).days + 1) if (from_date + timedelta(days=i)).weekday() < 5]
    return collect_dates(dates, dry_run=dry_run)


def load_downloaded_dates() -> set[date]:
    df = sql_to_df(f"SELECT DISTINCT date::date AS d FROM {RESULTS_TABLE}")
    if df.empty:
        return set()
    return {pd.Timestamp(d).date() for d in df["d"]}


def run_bse_bhavcopy_collection(*, lookback_days: int | None = None) -> dict[str, object]:
    """Daily incremental collection -- same lookback-window-of-candidate-dates shape
    as NSE's bhavcopy_downloader.py. Fetches ONLY the actual missing weekday dates
    (via collect_dates(), not collect_range()) -- an earlier version called
    collect_range(min(missing), max(missing)), which re-walks EVERY calendar day in
    that span regardless of which specific dates were actually missing; since
    weekends never get a written row, they always show up as "missing" and pull the
    span wide, so every run re-fetched almost the entire lookback window (found live
    2026-08-15, before this had ever run unattended). bseindia_ohlcv's own contents
    plus bseindia_non_trading_dates ARE the complete dedup state, checked directly --
    no separate store/redis dedup layer needed, since this is one lightweight
    request per missing date, not a heavy CDP download."""
    ensure_ohlcv_table()
    today = datetime.now().date()
    start = today - timedelta(days=lookback_days if lookback_days is not None else BSE_BHAVCOPY_DOWNLOAD_LOOKBACK_DAYS)
    start = max(start, BSE_BHAVCOPY_EARLIEST_DATE)
    end = today - timedelta(days=1)

    existing = load_downloaded_dates()
    known_non_trading = load_known_non_trading_dates()
    all_candidate_days = [start + timedelta(days=i) for i in range((end - start).days + 1)]
    missing_candidates = [
        d for d in all_candidate_days
        if d.weekday() < 5 and d not in existing and d not in known_non_trading
    ]
    if not missing_candidates:
        return {"days_written": 0, "rows_written": 0, "non_trading_days": 0, "failed_days": [], "blocked": False, "candidate_dates": 0}

    result = collect_dates(missing_candidates)
    result["candidate_dates"] = len(missing_candidates)
    return result


def main() -> int:
    global STOCKEY_RUN_STATE
    result = run_bse_bhavcopy_collection()
    STOCKEY_RUN_STATE = {
        "source": SYNC_SOURCE_NAME,
        "rows": result["rows_written"],
        "rows_written": result["rows_written"],
        "days_written": result["days_written"],
        "non_trading_days": result["non_trading_days"],
        "failed_days": result["failed_days"],
        "blocked": result["blocked"],
        "fallback_used": bool(result["failed_days"]) or result["blocked"],
        "state_advanced": result["rows_written"] > 0,
        "status": "blocked" if result["blocked"] else "ok",
    }
    print(json.dumps(STOCKEY_RUN_STATE, ensure_ascii=False, default=str), flush=True)
    # BUG FOUND LIVE 2026-08-15: this used to always `return 0` regardless of
    # `blocked` -- data.download_runner.py's own classify_run_status() only looks at
    # the process's actual exit code (not this dict's own "status" string) to decide
    # ok-vs-failed, so a circuit-breaker trip was invisible to the pipeline's status
    # classification and got recorded as a clean "ok" in advisory_sync_state.
    # Matches data/nseindia/bhavcopy_downloader.py's own established convention.
    return 1 if result["blocked"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
