"""Expired + live index options, 1-minute, from Dhan's rolling-option endpoint.

Pure data (TODO: systrader's Nifty options research, LEDGER row 61). Dhan serves
minute bars for options BY POSITION rather than by contract: the nearest/next
weekly or monthly expiry (expiryFlag WEEK|MONTH, expiryCode 1 = nearest, 2 = next),
at a strike N steps from the money (ATM-10 .. ATM+10), call or put. Each minute
carries the strike it referred to at that moment, the spot, OHLC, IV, OI and volume,
so a fixed-strike series is recoverable wherever the strike stayed within +-10 steps.

Measured live 2026-10-20 (not what the docs say, in two places):
  - toDate is INCLUSIVE here (fromDate 09-01, toDate 09-03 returned 09-03's bars);
  - one call takes at most 31 calendar days (08-01..09-01 ok, ..09-02 -> 504);
  - history reaches back 5 years (June 2021 served); bars run to 15:39 IST from
    2026, to 15:29 before;
  - the response names no expiry DATE -- join on NSE's F&O bhavcopy for that.
504s from Dhan's CloudFront edge happen and are retried here.

Speed (measured 2026-10-20): Dhan answers in 5-140 s per call and the time varies
run to run, roughly 1-2 s per session of data whatever the window. One call at a
time would take ~2 days for 5 years, so --workers requests run in flight at once.
The 5 req/sec limit is on request STARTS: each start still passes stockey's shared
cross-process Dhan gate (client._pace_dhan_request), so this job and the nightly
Dhan jobs together stay under the cap, but the gate is not held for the whole slow
response. The token is read from the cache only; when it is missing or expired the
job waits for all_dhan_auth_ensure.sh and never starts a login (consent rationing).

Resumable: every (series, window) that came back complete is recorded in
dhan_option_rolling_state and skipped on later runs (--ignore-state re-fetches).
Rows are immutable history, so the upsert is DO NOTHING -- a re-run costs reads,
not WAL. The table is created WITHOUT compression on purpose: compressing after the
backfill avoids the decompress/recompress WAL storm of 2026-08-23.

    python -m data.dhanlive.option_rolling --underlying NIFTY --years 5
"""
from __future__ import annotations

import argparse
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta

import pandas as pd
import requests

from data.dhanlive.auth import load_cached_access_token
from data.dhanlive.client import DhanAPIError, DhanHistoricalClient, _pace_dhan_request
from data.dhanlive.ohlcv import _ist_now, is_no_data_error
from utils.db import db_session, sql_to_df, upsert_to_db
from utils.fallback_telemetry import record_local_fallback_event

TABLE = "dhan_option_rolling"
STATE_TABLE = "dhan_option_rolling_state"
ENDPOINT = "/charts/rollingoption"
MAX_SPAN_DAYS = 30  # fromDate..toDate inclusive: 31 calendar days, the measured ceiling
WINDOW_DAYS = 14  # what the backfill asks for: shorter calls time out less (504) than 31-day ones
FIELDS = ["open", "high", "low", "close", "iv", "volume", "strike", "oi", "spot"]
SIDES = {"CALL": "CE", "PUT": "PE"}
DEFAULT_SERIES = (("WEEK", 1), ("WEEK", 2), ("MONTH", 1))
SERVER_RETRIES = 4
SERVER_BACKOFF_SECONDS = 5.0
RATE_LIMIT_RETRIES = 6
TOKEN_WAIT_SECONDS = 300
REQUEST_TIMEOUT_SECONDS = 300


def ensure_tables() -> None:
    with db_session() as (_conn, cur):
        cur.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {TABLE} (
                underlying             text        NOT NULL,
                underlying_security_id bigint      NOT NULL,
                expiry_flag            text        NOT NULL,
                expiry_code            smallint    NOT NULL,
                strike_offset          smallint    NOT NULL,
                option_type            text        NOT NULL,
                interval_minutes       smallint    NOT NULL,
                ts                     timestamptz NOT NULL,
                strike                 double precision,
                spot                   double precision,
                open                   double precision,
                high                   double precision,
                low                    double precision,
                close                  double precision,
                iv                     double precision,
                oi                     bigint,
                volume                 bigint,
                load_ts                timestamptz NOT NULL DEFAULT now(),
                PRIMARY KEY (underlying_security_id, expiry_flag, expiry_code, strike_offset,
                             option_type, interval_minutes, ts)
            )
            """
        )
        cur.execute(
            f"SELECT create_hypertable('{TABLE}', 'ts', chunk_time_interval => INTERVAL '30 days', "
            f"if_not_exists => TRUE, migrate_data => TRUE)"
        )
        cur.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {STATE_TABLE} (
                underlying_security_id bigint      NOT NULL,
                expiry_flag            text        NOT NULL,
                expiry_code            smallint    NOT NULL,
                strike_offset          smallint    NOT NULL,
                option_type            text        NOT NULL,
                interval_minutes       smallint    NOT NULL,
                window_start           date        NOT NULL,
                window_end             date        NOT NULL,
                rows_fetched           integer     NOT NULL,
                completed_at           timestamptz NOT NULL DEFAULT now(),
                PRIMARY KEY (underlying_security_id, expiry_flag, expiry_code, strike_offset,
                             option_type, interval_minutes, window_start)
            )
            """
        )


def resolve_underlying(symbol: str) -> int:
    """The index's Dhan security id, from the instrument master (segment I = NSE index)."""
    df = sql_to_df(
        "SELECT DISTINCT security_id FROM master_dhan_instruments "
        "WHERE segment = 'I' AND instrument = 'INDEX' AND symbol_name = %s",
        params=(symbol.upper(),),
    )
    if len(df) != 1:
        raise SystemExit(f"{symbol}: expected one NSE index security id in master_dhan_instruments, found {len(df)}")
    return int(df.iloc[0]["security_id"])


def strike_label(offset: int) -> str:
    return "ATM" if offset == 0 else f"ATM{offset:+d}"


def windows(start: date, end: date, span: int = MAX_SPAN_DAYS) -> list[tuple[date, date]]:
    """Consecutive [from, to] spans, inclusive, at most `span` days apart."""
    span = min(span, MAX_SPAN_DAYS)
    out = []
    s = start
    while s <= end:
        e = min(s + timedelta(days=span), end)
        out.append((s, e))
        s = e + timedelta(days=1)
    return out


def frame_from_payload(payload: dict, side: str) -> pd.DataFrame:
    """One side's arrays as rows; empty when Dhan sent none."""
    data = payload.get("data") if isinstance(payload, dict) else None
    block = (data or {}).get(side.lower()) if isinstance(data, dict) else None
    if not block or not block.get("timestamp"):
        return pd.DataFrame()
    n = len(block["timestamp"])
    df = pd.DataFrame({"ts": pd.to_datetime(block["timestamp"], unit="s", utc=True)})
    for f in FIELDS:
        vals = block.get(f) or []
        df[f] = vals if len(vals) == n else [None] * n
    return df


class Poster:
    """POSTs to Dhan from several threads: request starts paced through the shared gate,
    one requests.Session per thread, token from the cache only."""

    def __init__(self) -> None:
        self._local = threading.local()
        self._token_lock = threading.Lock()
        self._token = self._read_token()

    @staticmethod
    def _read_token() -> str:
        while True:
            token = load_cached_access_token()
            if token:
                return token
            print(f"no fresh cached Dhan token; waiting {TOKEN_WAIT_SECONDS}s for all_dhan_auth_ensure.sh",
                  file=sys.stderr, flush=True)
            time.sleep(TOKEN_WAIT_SECONDS)

    def _session(self) -> requests.Session:
        sess = getattr(self._local, "session", None)
        if sess is None:
            sess = requests.Session()
            self._local.session = sess
        return sess

    def post(self, path: str, body: dict) -> dict:
        rate_tries = 0
        while True:
            token = self._token
            headers = {"Accept": "application/json", "Content-Type": "application/json", "access-token": token}
            with _pace_dhan_request():
                pass  # spaces the START against every other Dhan caller; the response is awaited outside
            resp = self._session().post(DhanHistoricalClient.BASE_URL + path, json=body, headers=headers,
                                        timeout=REQUEST_TIMEOUT_SECONDS)
            if resp.status_code == 429 and rate_tries < RATE_LIMIT_RETRIES:
                rate_tries += 1
                time.sleep(3.0 * rate_tries)
                continue
            if DhanHistoricalClient._is_auth_failure(resp):
                with self._token_lock:
                    if self._token == token:  # first thread to see it waits for the auth job's new token
                        fresh = load_cached_access_token()
                        while not fresh or fresh == token:
                            print(f"Dhan token rejected; waiting {TOKEN_WAIT_SECONDS}s for a refreshed cache",
                                  file=sys.stderr, flush=True)
                            time.sleep(TOKEN_WAIT_SECONDS)
                            fresh = load_cached_access_token()
                        self._token = fresh
                continue
            try:
                payload = resp.json()
            except ValueError:
                payload = {"raw_text": resp.text[:300]}
            if resp.ok:
                return payload
            msg = payload.get("errorMessage") or payload.get("message") if isinstance(payload, dict) else None
            raise DhanAPIError(f"Dhan API request failed with status {resp.status_code}: {msg or str(payload)[:300]}")


def fetch_window(poster: Poster, *, security_id: int, flag: str, code: int, offset: int,
                 option: str, interval: int, start: date, end: date) -> pd.DataFrame:
    body = {
        "exchangeSegment": "NSE_FNO",
        "interval": interval,
        "securityId": security_id,
        "instrument": "OPTIDX",
        "expiryFlag": flag,
        "expiryCode": code,
        "strike": strike_label(offset),
        "drvOptionType": option,
        "requiredData": FIELDS,
        "fromDate": start.isoformat(),
        "toDate": end.isoformat(),
    }
    for attempt in range(1, SERVER_RETRIES + 1):
        try:
            payload = poster.post(ENDPOINT, body)
            break
        except (DhanAPIError, requests.RequestException) as exc:
            if isinstance(exc, DhanAPIError) and is_no_data_error(exc):
                return pd.DataFrame()
            transient = isinstance(exc, requests.RequestException) or any(
                f"status {s}" in str(exc) for s in (500, 502, 503, 504))
            if not transient or attempt == SERVER_RETRIES:
                raise
            time.sleep(SERVER_BACKOFF_SECONDS * attempt)
    return frame_from_payload(payload, SIDES[option])


def load_done(security_id: int, interval: int) -> set[tuple]:
    df = sql_to_df(
        f"SELECT expiry_flag, expiry_code, strike_offset, option_type, window_start FROM {STATE_TABLE} "
        f"WHERE underlying_security_id = %s AND interval_minutes = %s",
        params=(security_id, interval),
    )
    return {(r.expiry_flag, int(r.expiry_code), int(r.strike_offset), r.option_type, r.window_start)
            for r in df.itertuples()}


def mark_done(rows: list[tuple]) -> None:
    if not rows:
        return
    with db_session() as (_conn, cur):
        cur.executemany(
            f"""
            INSERT INTO {STATE_TABLE} (underlying_security_id, expiry_flag, expiry_code, strike_offset,
                                       option_type, interval_minutes, window_start, window_end, rows_fetched)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (underlying_security_id, expiry_flag, expiry_code, strike_offset, option_type,
                         interval_minutes, window_start)
            DO UPDATE SET window_end = EXCLUDED.window_end, rows_fetched = EXCLUDED.rows_fetched,
                          completed_at = now()
            """,
            rows,
        )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Backfill / refresh Dhan rolling-option minute bars for one index.")
    ap.add_argument("--underlying", required=True, help="NSE index symbol in master_dhan_instruments, e.g. NIFTY")
    ap.add_argument("--years", type=float, default=5.0)
    ap.add_argument("--from-date", help="YYYY-MM-DD, overrides --years")
    ap.add_argument("--max-offset", type=int, default=10, help="strikes from ATM-N to ATM+N")
    ap.add_argument("--series", default=",".join(f"{f}{c}" for f, c in DEFAULT_SERIES),
                    help="comma list of WEEK1/WEEK2/MONTH1/MONTH2")
    ap.add_argument("--interval", type=int, default=1)
    ap.add_argument("--window-days", type=int, default=WINDOW_DAYS)
    ap.add_argument("--workers", type=int, default=4, help="requests in flight at once (starts stay paced)")
    ap.add_argument("--ignore-state", action="store_true")
    args = ap.parse_args(argv)

    series = []
    for s in args.series.split(","):
        s = s.strip().upper()
        flag, code = s.rstrip("0123456789"), s[len(s.rstrip("0123456789")):]
        if flag not in ("WEEK", "MONTH") or not code:
            raise SystemExit(f"bad series {s!r}: use WEEK1, WEEK2, MONTH1, ...")
        series.append((flag, int(code)))

    ensure_tables()
    security_id = resolve_underlying(args.underlying)
    today = _ist_now().date()
    end = today - timedelta(days=1)  # today's session is not finished
    start = date.fromisoformat(args.from_date) if args.from_date else end - timedelta(days=int(args.years * 365.25))
    done = set() if args.ignore_state else load_done(security_id, args.interval)
    poster = Poster()
    offsets = range(-args.max_offset, args.max_offset + 1)
    wins = windows(start, end, args.window_days)
    total = len(wins) * len(series) * len(offsets) * len(SIDES)
    print(f"{args.underlying} (security id {security_id}): {start}..{end}, {len(wins)} windows x {len(series)} series "
          f"x {len(offsets)} strikes x 2 sides = {total} calls, {len(done)} already done", flush=True)

    calls = failures = rows_total = 0
    t0 = time.monotonic()

    def one(task):
        flag, code, off, option, side, ws, we = task
        try:
            df = fetch_window(poster, security_id=security_id, flag=flag, code=code, offset=off,
                              option=option, interval=args.interval, start=ws, end=we)
            return task, df, None
        except Exception as exc:  # noqa: BLE001 -- recorded; the window is retried next run
            return task, None, exc

    with ThreadPoolExecutor(max_workers=max(args.workers, 1)) as pool:
        for ws, we in reversed(wins):  # newest first: the most useful data lands first
            tasks = [(flag, code, off, option, side, ws, we)
                     for flag, code in series for off in offsets for option, side in SIDES.items()
                     if (flag, code, off, side, ws) not in done]
            if not tasks:
                continue
            frames, finished = [], []
            for (flag, code, off, option, side, _ws, _we), df, exc in pool.map(one, tasks):
                calls += 1
                if exc is not None:
                    failures += 1
                    record_local_fallback_event(
                        module="data.dhanlive.option_rolling", source="dhan_api",
                        fallback_type="option_rolling_window_failed", severity="warn",
                        reason=f"{args.underlying} {flag}{code} {strike_label(off)} {side} {ws}..{we}",
                        error=exc,
                    )
                    print(f"  FAILED {flag}{code} {strike_label(off)} {side} {ws}..{we}: {exc}", file=sys.stderr, flush=True)
                    continue
                if not df.empty:
                    df.insert(0, "underlying", args.underlying.upper())
                    df.insert(1, "underlying_security_id", security_id)
                    df.insert(2, "expiry_flag", flag)
                    df.insert(3, "expiry_code", code)
                    df.insert(4, "strike_offset", off)
                    df.insert(5, "option_type", side)
                    df.insert(6, "interval_minutes", args.interval)
                    frames.append(df)
                finished.append((security_id, flag, code, off, side, args.interval, ws, we, len(df)))
            if frames:
                batch = pd.concat(frames, ignore_index=True)
                batch = batch.drop_duplicates(subset=["expiry_flag", "expiry_code", "strike_offset", "option_type", "ts"])
                upsert_to_db(batch, TABLE, ["underlying_security_id", "expiry_flag", "expiry_code", "strike_offset",
                                            "option_type", "interval_minutes", "ts"],
                             timescaledb_column="ts", on_conflict="nothing")
                rows_total += len(batch)
            mark_done(finished)  # only after the rows are stored
            elapsed = time.monotonic() - t0
            print(f"{ws}..{we}: {len(finished)}/{len(tasks)} series, {sum(f[-1] for f in finished):,} rows; "
                  f"total {rows_total:,} rows, {calls}/{total} calls in {elapsed/60:.0f} min, {failures} failed", flush=True)
    print(f"done: {rows_total:,} rows, {calls} calls, {failures} failed (re-run to retry them)", flush=True)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
