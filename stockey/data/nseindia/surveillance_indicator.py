"""NSE daily surveillance-indicator file -> nseindia_surveillance_indicator.

NSE publishes one CSV per trading day (nsearchives.nseindia.com/content/cm/
REG1_IND<ddmmyy>.csv, from about 2025-01; earlier dates 404) with one row per listed
security and ~60 flag columns: GSM stage, long/short-term ASM stage, ESM, insolvency
(IRP), pledge, "overall encumbered share > 50%", loss-making, and the price-movement
triggers behind ASM. Code 100 means "not flagged"; any other number is the stage or
flag value. Collected for the universe rebuild (docs/UNIVERSE_PRD.md Layer 1 rules 2
and 6); Dhan's own asm_gsm_flag marks only ~20 mostly-suspended names, not NSE's list.

The whole file is kept: the named columns below are typed, and every column is also
stored raw in `raw` (JSONB, original header -> value) so a flag nobody reads today is
not lost. A plain static download through the shared NSE rate gate -- no browser.

Candidate dates are trading days seen in nseindia_ohlcv (so holidays are never asked
for) within the lookback that are not in the table yet.

    python -m data.nseindia.surveillance_indicator                  # daily incremental
    python -m data.nseindia.surveillance_indicator --from 2025-01-01  # backfill
"""

from __future__ import annotations

import argparse
import io
import json
from datetime import date, timedelta

import pandas as pd
import requests

from utils.db import db_session, execute_db_operation, sql_to_df, upsert_to_db
from utils.fallback_telemetry import record_local_fallback_event
from utils.nse_rate_limiter import nse_request_gate

SYNC_SOURCE_NAME = "data.nseindia.surveillance_indicator"
RESULTS_TABLE = "nseindia_surveillance_indicator"
URL = "https://nsearchives.nseindia.com/content/cm/REG1_IND{ddmmyy}.csv"
HEADERS = {"User-Agent": "Mozilla/5.0", "Referer": "https://www.nseindia.com/"}
EARLIEST_DATE = date(2025, 1, 1)
DEFAULT_LOOKBACK_DAYS = 30
MAX_CONSECUTIVE_FAILURES = 5
NOT_FLAGGED = 100
STOCKEY_RUN_STATE: dict[str, object] = {}

# typed column -> NSE header (matched case-insensitively after stripping)
FLAG_COLUMNS = {
    "gsm": "GSM",
    "lt_asm": "Long_Term_Additional_Surveillance_Measure (Long Term ASM)",
    "st_asm": "Short_Term_Additional_Surveillance_Measure (Short Term ASM)",
    "esm": "ESM",
    "irp": "Insolvency_Resolution_Process(IRP)",
    "pledge": "Pledge",
    "total_pledge": "Total Pledge",
    "encumbered_over_50": "The Overall encumbered share in the scrip is more than 50 Percent.",
    "loss_making": "Loss making",
    "bz_sz_series": "Under BZ/SZ Series",
    "default_flag": "Default",
}
TEXT_COLUMNS = {"scrip_code": "ScripCode", "symbol": "Symbol", "nse_exclusive": "Nse Exclusive",
                "status": "Status", "series": "Series"}

_TABLE_STATEMENT = f"""
    CREATE TABLE IF NOT EXISTS {RESULTS_TABLE} (
        date DATE NOT NULL,
        symbol TEXT NOT NULL,
        series TEXT NOT NULL,
        scrip_code TEXT,
        nse_exclusive TEXT,
        status TEXT,
        {", ".join(f"{c} SMALLINT" for c in FLAG_COLUMNS)},
        raw JSONB,
        load_ts TIMESTAMPTZ,
        UNIQUE (date, symbol, series)
    )
"""


class SurveillanceFileMissing(Exception):
    pass


def ensure_table() -> None:
    def _op() -> None:
        with db_session() as (_, cur):
            cur.execute(_TABLE_STATEMENT)

    execute_db_operation(_op, operation_name=f"{RESULTS_TABLE}:ensure_table")


def fetch_csv(d: date, session: requests.Session) -> bytes:
    with nse_request_gate():
        resp = session.get(URL.format(ddmmyy=d.strftime("%d%m%y")), headers=HEADERS, timeout=60)
    if resp.status_code == 404:
        raise SurveillanceFileMissing(f"{d}: 404")
    resp.raise_for_status()
    if resp.content.lstrip()[:1] == b"<":
        raise SurveillanceFileMissing(f"{d}: HTML page instead of CSV")
    return resp.content


def parse_csv(content: bytes, d: date) -> pd.DataFrame:
    raw = pd.read_csv(io.BytesIO(content), dtype=str, keep_default_na=False)
    raw.columns = [c.strip() for c in raw.columns]
    raw = raw.apply(lambda s: s.str.strip())
    by_lower = {c.lower(): c for c in raw.columns}
    missing = [h for h in [*TEXT_COLUMNS.values(), *FLAG_COLUMNS.values()] if h.lower() not in by_lower]
    if missing:
        raise ValueError(f"surveillance file {d} is missing columns: {missing}")

    out = pd.DataFrame({"date": pd.Timestamp(d).date()}, index=raw.index)
    for col, header in TEXT_COLUMNS.items():
        out[col] = raw[by_lower[header.lower()]].replace("", None)
    for col, header in FLAG_COLUMNS.items():
        out[col] = pd.to_numeric(raw[by_lower[header.lower()]], errors="coerce").astype("Int64")
    keep = [c for c in raw.columns if not c.lower().startswith("filler")]
    out["raw"] = [json.dumps(r, ensure_ascii=False) for r in raw[keep].to_dict("records")]
    out = out.dropna(subset=["symbol", "series"])
    return out.drop_duplicates(["date", "symbol", "series"], keep="last").reset_index(drop=True)


def candidate_dates(*, from_date: date | None = None, lookback_days: int = DEFAULT_LOOKBACK_DAYS) -> list[date]:
    start = max(from_date or (pd.Timestamp.now(tz="Asia/Kolkata").date() - timedelta(days=lookback_days)), EARLIEST_DATE)
    sessions = sql_to_df(
        "SELECT DISTINCT date::date AS d FROM nseindia_ohlcv WHERE date >= %s AND series = 'EQ'",
        params=(start,),
    )
    have = sql_to_df(f"SELECT DISTINCT date AS d FROM {RESULTS_TABLE} WHERE date >= %s", params=(start,))
    have_set = {pd.Timestamp(x).date() for x in have["d"]} if not have.empty else set()
    return sorted(pd.Timestamp(x).date() for x in sessions["d"] if pd.Timestamp(x).date() not in have_set)


def collect(dates: list[date]) -> dict[str, object]:
    session = requests.Session()
    written, rows, missing, failed = [], 0, [], []
    consecutive = 0
    for d in dates:
        try:
            df = parse_csv(fetch_csv(d, session), d)
            df["load_ts"] = pd.Timestamp.now(tz="UTC")
            upsert_to_db(df, RESULTS_TABLE, unique_keys=["date", "symbol", "series"])
            written.append(str(d)); rows += len(df); consecutive = 0
            print(f"{d}: {len(df)} rows", flush=True)
        except SurveillanceFileMissing as exc:
            missing.append(str(d))
            print(f"{d}: not published ({exc})", flush=True)
        except Exception as exc:
            failed.append(str(d)); consecutive += 1
            print(f"{d}: FAILED {exc!r}", flush=True)
            record_local_fallback_event(
                module=SYNC_SOURCE_NAME, source="nse", fallback_type="surveillance_indicator_failed",
                severity="warn", reason=f"surveillance indicator {d} failed", error=repr(exc),
                metadata={"date": str(d)},
            )
            if consecutive >= MAX_CONSECUTIVE_FAILURES:
                break
    return {"days_written": written, "rows_written": rows, "not_published": missing, "failed_days": failed,
            "candidate_dates": len(dates)}


def main(argv: list[str] | None = None) -> int:
    global STOCKEY_RUN_STATE
    parser = argparse.ArgumentParser(description="Collect NSE's daily surveillance-indicator file.")
    parser.add_argument("--from", dest="from_date", type=date.fromisoformat)
    parser.add_argument("--lookback-days", type=int, default=DEFAULT_LOOKBACK_DAYS)
    args = parser.parse_args(argv)
    ensure_table()
    result = collect(candidate_dates(from_date=args.from_date, lookback_days=args.lookback_days))
    # The latest session's file may simply not be out yet; only an older miss is a gap.
    stale_missing = result["not_published"][:-1] if result["not_published"] else []
    STOCKEY_RUN_STATE = {
        "source": SYNC_SOURCE_NAME,
        "rows": result["rows_written"],
        "rows_written": result["rows_written"],
        "days_written": len(result["days_written"]),
        "not_published": result["not_published"],
        "failed_days": result["failed_days"],
        "fallback_used": bool(result["failed_days"] or stale_missing),
        "state_advanced": result["rows_written"] > 0,
    }
    print(json.dumps({"status": "ok" if not result["failed_days"] else "degraded", **STOCKEY_RUN_STATE},
                     ensure_ascii=False, default=str), flush=True)
    return 1 if result["failed_days"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
