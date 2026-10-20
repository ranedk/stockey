"""NSE F&O bhavcopy, INDEX derivatives only -- every index future and option contract's
daily OHLC, settlement price, open interest and volume, back as far as the archive serves.

Pure data (for systrader's Nifty options research, LEDGER row 61). It is the contract-level
truth the Dhan rolling-option minute bars (data/dhanlive/option_rolling.py) lack: actual
expiry dates, every strike, settlement prices and OI. Stock F&O (OPTSTK/FUTSTK, ~10x the
rows) is deliberately not kept.

Two archive formats, both on nsearchives.nseindia.com and fetched like the cash-market
history (data/nseindia/bhavcopy_history.py): a plain UA+Referer request through the shared
NSE rate gate, no browser.
  - old  "fo{ddMONyyyy}bhav.csv.zip"   through 2024-07-05: INSTRUMENT FUTIDX/OPTIDX
  - UDiFF "BhavCopy_NSE_FO_..."        from 2024-07-08:    FinInstrmTp IDF/IDO
Units, checked against the files 2026-10-20: CONTRACTS (old) and TtlTradgVol (UDiFF) are
both NUMBER OF CONTRACTS; VAL_INLAKH (lakh) and TtlTrfVal (rupees) are both NOTIONAL value,
(strike + premium) x quantity for options. Only UDiFF carries the lot size and the
underlying price.

    python -m data.nseindia.fo_bhavcopy --from 2021-06-01 --to 2026-10-19
Days already in the table are skipped (--refetch to re-download them).
"""
from __future__ import annotations

import argparse
import io
import sys
import time
from datetime import timedelta

import pandas as pd

from data.nseindia.bhavcopy_history import UDIFF_CUTOVER, _extract_csv, _session
from utils.db import sql_to_df, upsert_to_db
from utils.fallback_telemetry import record_local_fallback_event
from utils.nse_rate_limiter import nse_request_gate

TABLE = "nseindia_fo_bhavcopy"
KEYS = ["trade_date", "symbol", "instrument", "expiry", "strike", "option_type"]
COLS = KEYS + ["open", "high", "low", "close", "settle", "last", "prev_close", "underlying_price",
               "open_interest", "change_in_oi", "contracts", "notional_value_rs", "lot_size", "source_format"]


def old_url(d: pd.Timestamp) -> str:
    return (f"https://nsearchives.nseindia.com/content/historical/DERIVATIVES/{d.year}/"
            f"{d.strftime('%b').upper()}/fo{d.strftime('%d%b%Y').upper()}bhav.csv.zip")


def udiff_url(d: pd.Timestamp) -> str:
    return f"https://nsearchives.nseindia.com/content/fo/BhavCopy_NSE_FO_0_0_0_{d.strftime('%Y%m%d')}_F_0000.csv.zip"


def ensure_table() -> None:
    from utils.db import db_session

    with db_session() as (_conn, cur):
        cur.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {TABLE} (
                trade_date        date             NOT NULL,
                symbol            text             NOT NULL,
                instrument        text             NOT NULL,  -- FUTIDX | OPTIDX
                expiry            date             NOT NULL,
                strike            double precision NOT NULL,  -- 0 for futures
                option_type       text             NOT NULL,  -- CE | PE | XX (futures)
                open              double precision,
                high              double precision,
                low               double precision,
                close             double precision,
                settle            double precision,
                last              double precision,           -- UDiFF only
                prev_close        double precision,           -- UDiFF only
                underlying_price  double precision,           -- UDiFF only
                open_interest     bigint,
                change_in_oi      bigint,
                contracts         bigint,
                notional_value_rs double precision,
                lot_size          integer,                    -- UDiFF only
                source_format     text             NOT NULL,  -- old | udiff
                load_ts           timestamptz      NOT NULL DEFAULT now(),
                PRIMARY KEY (trade_date, symbol, instrument, expiry, strike, option_type)
            )
            """
        )
        cur.execute(f"CREATE INDEX IF NOT EXISTS {TABLE}_sym_date ON {TABLE} (symbol, trade_date)")


def parse_old(csv_bytes: bytes) -> pd.DataFrame:
    df = pd.read_csv(io.BytesIO(csv_bytes))
    df.columns = [c.strip() for c in df.columns]
    df = df[df["INSTRUMENT"].astype(str).str.strip().isin(["FUTIDX", "OPTIDX"])]
    return pd.DataFrame({
        "trade_date": pd.to_datetime(df["TIMESTAMP"].astype(str).str.strip(), format="%d-%b-%Y").dt.date,
        "symbol": df["SYMBOL"].astype(str).str.strip(),
        "instrument": df["INSTRUMENT"].astype(str).str.strip(),
        "expiry": pd.to_datetime(df["EXPIRY_DT"].astype(str).str.strip(), format="%d-%b-%Y").dt.date,
        "strike": pd.to_numeric(df["STRIKE_PR"]).fillna(0.0),
        "option_type": df["OPTION_TYP"].astype(str).str.strip().replace({"FF": "XX", "nan": "XX"}),
        "open": df["OPEN"], "high": df["HIGH"], "low": df["LOW"], "close": df["CLOSE"],
        "settle": df["SETTLE_PR"], "last": None, "prev_close": None, "underlying_price": None,
        "open_interest": df["OPEN_INT"], "change_in_oi": df["CHG_IN_OI"],
        "contracts": df["CONTRACTS"], "notional_value_rs": pd.to_numeric(df["VAL_INLAKH"]) * 1e5,
        "lot_size": None, "source_format": "old",
    })


def parse_udiff(csv_bytes: bytes) -> pd.DataFrame:
    df = pd.read_csv(io.BytesIO(csv_bytes))
    df.columns = [c.strip() for c in df.columns]
    kind = df["FinInstrmTp"].astype(str).str.strip()
    df = df[kind.isin(["IDF", "IDO"])]
    kind = df["FinInstrmTp"].astype(str).str.strip()
    return pd.DataFrame({
        "trade_date": pd.to_datetime(df["TradDt"]).dt.date,
        "symbol": df["TckrSymb"].astype(str).str.strip(),
        "instrument": kind.map({"IDF": "FUTIDX", "IDO": "OPTIDX"}),
        # the ACTUAL expiry: XpryDt is the scheduled one, before a holiday moves it
        "expiry": pd.to_datetime(df["FininstrmActlXpryDt"].fillna(df["XpryDt"])).dt.date,
        "strike": pd.to_numeric(df["StrkPric"]).fillna(0.0),
        "option_type": df["OptnTp"].fillna("XX").astype(str).str.strip(),
        "open": df["OpnPric"], "high": df["HghPric"], "low": df["LwPric"], "close": df["ClsPric"],
        "settle": df["SttlmPric"], "last": df["LastPric"], "prev_close": df["PrvsClsgPric"],
        "underlying_price": df["UndrlygPric"],
        "open_interest": df["OpnIntrst"], "change_in_oi": df["ChngInOpnIntrst"],
        "contracts": df["TtlTradgVol"], "notional_value_rs": df["TtlTrfVal"],
        "lot_size": df["NewBrdLotQty"], "source_format": "udiff",
    })


def fetch_day(session, d: pd.Timestamp) -> pd.DataFrame | None:
    """One day's index-derivative rows; None for a holiday or a day the archive does not have."""
    pairs = ([(old_url, parse_old), (udiff_url, parse_udiff)] if d < UDIFF_CUTOVER
             else [(udiff_url, parse_udiff), (old_url, parse_old)])
    for url_fn, parser in pairs:
        with nse_request_gate():
            r = session.get(url_fn(d), timeout=30)
        if r.status_code == 200 and r.content[:2] == b"PK":
            return parser(_extract_csv(r.content))
    return None


def stored_days(start: pd.Timestamp, end: pd.Timestamp) -> set:
    df = sql_to_df(f"SELECT DISTINCT trade_date FROM {TABLE} WHERE trade_date BETWEEN %s AND %s",
                   params=(start.date(), end.date()))
    return set(df["trade_date"]) if not df.empty else set()


def collect_range(from_date: str, to_date: str, *, refetch: bool = False, sleep: float = 0.4) -> dict:
    ensure_table()
    start, end = pd.Timestamp(from_date), pd.Timestamp(to_date)
    have = set() if refetch else stored_days(start, end)
    s = _session()
    days = rows = missing = failed = 0
    d = start
    while d <= end:
        if d.weekday() < 5 and d.date() not in have:
            try:
                df = fetch_day(s, d)
            except Exception as exc:  # noqa: BLE001 -- recorded; the day is retried next run
                failed += 1
                record_local_fallback_event(module="data.nseindia.fo_bhavcopy", source="nse_archive",
                                            fallback_type="fo_bhavcopy_day_failed", reason=str(d.date()), error=exc)
                print(f"  {d.date()}: FAILED {exc}", file=sys.stderr, flush=True)
                s = _session()
            else:
                if df is not None and not df.empty:
                    df = df.drop_duplicates(KEYS)
                    upsert_to_db(df[COLS], TABLE, unique_keys=KEYS)
                    days += 1
                    rows += len(df)
                    print(f"  {d.date()}: {len(df)} rows", flush=True)
                else:
                    missing += 1  # weekday holiday, or not in the archive
            time.sleep(sleep)
        d += timedelta(days=1)
    return {"trading_days": days, "rows": rows, "weekdays_without_file": missing, "failed": failed}


def main() -> int:
    ap = argparse.ArgumentParser(description="NSE F&O bhavcopy (index derivatives) backfill.")
    ap.add_argument("--from", dest="from_date", required=True)
    ap.add_argument("--to", dest="to_date", required=True)
    ap.add_argument("--refetch", action="store_true", help="re-download days already stored")
    ap.add_argument("--sleep", type=float, default=0.4, help="seconds between NSE requests")
    args = ap.parse_args()
    result = collect_range(args.from_date, args.to_date, refetch=args.refetch, sleep=args.sleep)
    print(result, flush=True)
    return 1 if result["failed"] else 0


if __name__ == "__main__":
    sys.exit(main())
