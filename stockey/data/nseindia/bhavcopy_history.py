"""Historical NSE bhavcopy collector -- backfills raw daily OHLCV deep into the past.

NSE archives every day's cash-market bhavcopy: the OLD "cm{ddMMMyyyy}bhav" format is served back to ~2000s
(through 2024-07-05), and the UDiFF "BhavCopy_NSE_CM" format from 2024-07-08 onward. Both carry full OHLCV +
series + ISIN, and the archive host (nsearchives.nseindia.com) serves them to a plain UA+Referer request --
no browser/CDP needed (unlike the live daily downloader). This fills the pre-2021 equity history and the
2021-07..2025-06 hole the Dhan broker feed lacks.

Rows land in `nseindia_ohlcv` (the same table the live daily bhavcopy + the adjustment pipeline use), keyed
by (date, symbol, series), so the backfill simply extends the existing feed backwards.

SPLIT/BONUS ADJUSTMENT is recoverable FROM the bhavcopy: PREVCLOSE is the RAW prior close, so on a split/
bonus ex-date the overnight gap `open / previous_close` collapses to the corporate-action ratio (~0.20 for a
5:1 split, ~0.50 for a 2:1, etc.) with no real move behind it -- so a large round deviation flags the CA and
gives the adjustment factor per symbol, no separate CA feed required (see `split_factors`). CLI:
`python -m data.nseindia.bhavcopy_history --from 2018-01-01 --to 2018-01-31`.
"""
from __future__ import annotations

import argparse
import io
import time
import zipfile
from datetime import timedelta

import pandas as pd
import requests

from utils.db import upsert_to_db

UDIFF_CUTOVER = pd.Timestamp("2024-07-08")
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
HEADERS = {"User-Agent": UA, "Referer": "https://www.nseindia.com/all-reports", "Accept": "*/*"}
OUT_COLS = ["symbol", "series", "open", "high", "low", "close", "last", "previous_close",
            "volume", "total_value", "date", "number_of_trades", "isin"]


def _oldcm_url(d: pd.Timestamp) -> str:
    return (f"https://nsearchives.nseindia.com/content/historical/EQUITIES/{d.year}/"
            f"{d.strftime('%b').upper()}/cm{d.strftime('%d%b%Y').upper()}bhav.csv.zip")


def _udiff_url(d: pd.Timestamp) -> str:
    return f"https://nsearchives.nseindia.com/content/cm/BhavCopy_NSE_CM_0_0_0_{d.strftime('%Y%m%d')}_F_0000.csv.zip"


def _extract_csv(zip_bytes: bytes) -> bytes:
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as z:
        return z.read(z.namelist()[0])


# ------------------------------------------------------------------------------------------------
# Pure parsers -> the nseindia_ohlcv schema (unit-tested on captured fixtures)
# ------------------------------------------------------------------------------------------------
def parse_oldcm(csv_bytes: bytes) -> pd.DataFrame:
    df = pd.read_csv(io.BytesIO(csv_bytes))
    df.columns = [c.strip() for c in df.columns]
    out = pd.DataFrame({
        "symbol": df["SYMBOL"].astype(str).str.strip(), "series": df["SERIES"].astype(str).str.strip(),
        "open": df["OPEN"], "high": df["HIGH"], "low": df["LOW"], "close": df["CLOSE"], "last": df["LAST"],
        "previous_close": df["PREVCLOSE"], "volume": df["TOTTRDQTY"], "total_value": df["TOTTRDVAL"],
        "date": pd.to_datetime(df["TIMESTAMP"], format="%d-%b-%Y", utc=True),
        "number_of_trades": df["TOTALTRADES"], "isin": df["ISIN"].astype(str).str.strip()})
    return out


def parse_udiff(csv_bytes: bytes) -> pd.DataFrame:
    df = pd.read_csv(io.BytesIO(csv_bytes))
    df.columns = [c.strip() for c in df.columns]
    out = pd.DataFrame({
        "symbol": df["TckrSymb"].astype(str).str.strip(), "series": df["SctySrs"].astype(str).str.strip(),
        "open": df["OpnPric"], "high": df["HghPric"], "low": df["LwPric"], "close": df["ClsPric"],
        "last": df["LastPric"], "previous_close": df["PrvsClsgPric"], "volume": df["TtlTradgVol"],
        "total_value": df["TtlTrfVal"], "date": pd.to_datetime(df["TradDt"], utc=True),
        "number_of_trades": df["TtlNbOfTxsExctd"], "isin": df["ISIN"].astype(str).str.strip()})
    return out[out["symbol"].notna() & (out["symbol"] != "nan")]


def split_factors(day_df: pd.DataFrame, *, threshold: float = 0.35) -> pd.DataFrame:
    """Corporate-action adjustment factor per symbol FROM one day's bhavcopy: the overnight gap
    `open / previous_close`. ~1.0 = normal day; a large ROUND deviation (~0.20 for a 5:1 split, ~0.50 for a
    2:1 split / 1:1 bonus, ~0.33 for a 2:1 bonus, etc.) marks a split/bonus ex-date -- circuit limits make a
    real overnight move this large practically impossible, so it isolates CA events. `threshold` is the
    minimum |factor-1| to flag (candidates should still be confirmed against a CA feed before applying)."""
    df = day_df.copy()
    df = df[(df["previous_close"] > 0) & df["open"].notna() & (df["open"] > 0)]
    df["adj_factor"] = (df["open"] / df["previous_close"]).round(4)
    flagged = df[(df["adj_factor"] - 1.0).abs() > threshold]
    return flagged[["symbol", "date", "adj_factor", "open", "previous_close"]].reset_index(drop=True)


# ------------------------------------------------------------------------------------------------
# Fetch + collect
# ------------------------------------------------------------------------------------------------
def _session() -> requests.Session:
    s = requests.Session(); s.headers.update(HEADERS)
    try:
        s.get("https://www.nseindia.com/", timeout=15)      # best-effort cookie prime (archive works w/o it)
    except Exception:
        pass
    return s


def fetch_day(session: requests.Session, d: pd.Timestamp) -> pd.DataFrame | None:
    """Download + parse one day's bhavcopy, picking the right format for the date and falling back to the
    other on a miss. Returns None for holidays / genuinely absent days."""
    pairs = ([(_oldcm_url, parse_oldcm), (_udiff_url, parse_udiff)] if d < UDIFF_CUTOVER
             else [(_udiff_url, parse_udiff), (_oldcm_url, parse_oldcm)])
    for url_fn, parser in pairs:
        try:
            r = session.get(url_fn(d), timeout=30)
            if r.status_code == 200 and r.content[:2] == b"PK":
                return parser(_extract_csv(r.content))
        except Exception:
            continue
    return None


def collect_range(from_date: str, to_date: str, *, dry_run: bool = False, sleep: float = 0.4) -> dict:
    s = _session()
    d, end = pd.Timestamp(from_date), pd.Timestamp(to_date)
    days, rows, holidays, ca_events = 0, 0, 0, []
    while d <= end:
        if d.weekday() < 5:                                 # skip weekends; holidays 404 -> None
            df = fetch_day(s, d)
            if df is not None and not df.empty:
                df = df.dropna(subset=["symbol", "close"])
                df = df.drop_duplicates(["date", "symbol", "series"])
                if not dry_run:
                    upsert_to_db(df[OUT_COLS], "nseindia_ohlcv", unique_keys=["date", "symbol", "series"])
                days += 1; rows += len(df)
                print(f"  {d.date()}: {len(df)} rows")
            else:
                holidays += 1
            time.sleep(sleep)
        d += timedelta(days=1)
    return {"trading_days": days, "rows": rows, "non_trading_days": holidays}


def main() -> None:
    ap = argparse.ArgumentParser(description="Historical NSE bhavcopy collector (report-only backfill).")
    ap.add_argument("--from", dest="from_date", required=True)
    ap.add_argument("--to", dest="to_date", required=True)
    ap.add_argument("--dry-run", action="store_true", help="download + parse but do not write to the DB")
    ap.add_argument("--sleep", type=float, default=0.4, help="seconds between requests (be polite to NSE)")
    args = ap.parse_args()
    print(f"backfilling bhavcopy {args.from_date}..{args.to_date} (dry_run={args.dry_run})")
    result = collect_range(args.from_date, args.to_date, dry_run=args.dry_run, sleep=args.sleep)
    print(result)


if __name__ == "__main__":
    main()
