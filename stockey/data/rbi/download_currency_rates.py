# Download RBI reference exchange rates -- sync version
"""Download RBI daily reference FX rates (INR per USD/GBP/EUR/JPY/AED/IDR) into `rbi_currency_rates`.

RBI serves these from an ASP.NET archive page (ReferenceRateArchive.aspx) that needs a currency checkbox
selected AND a date range submitted -- a plain HTTP postback is unreliable, so we drive the already-running
Chrome over CDP (the same pattern as `download_bank_rates.py` and the NSE/Dhan automation).

Why the previous crawler produced corrupt rows (fixed here):
  * it never checked a currency checkbox, so the search often returned no result table; and
  * it hard-coded the columns ["USD","GBP","EURO","YEN"], but the live table headers now carry unit
    suffixes and use EUR/JPY (plus new AED/IDR): "USD (INR / 1 USD)", "EUR (INR / 1 EUR)",
    "JPY (INR / 100 JPY)", ... -- so every column lookup missed and the rows came out empty/misaligned.

We now check `chkAll`, submit the range, and map columns by the currency CODE in each header cell (tolerating
the legacy EURO/YEN labels). JPY is quoted per 100 JPY and IDR per 10000 IDR exactly as RBI publishes them --
stored as-is, not silently rescaled. CLI: `python -m data.rbi.download_currency_rates --from 2014-01-01`.
"""
import argparse
import json
import time
from datetime import date, datetime, timedelta

import pandas as pd
from dateutil.relativedelta import relativedelta
from environs import Env
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright
from utils.cdp import connect_over_cdp as connect_over_cdp_guarded

from utils.fallback_telemetry import record_local_fallback_event
from utils.db import sql_to_df, upsert_to_db

env = Env()
env.read_env()

CDP_ENDPOINT = "http://localhost:9222"
SYNC_SOURCE_NAME = "data.rbi.download_currency_rates"
ARCHIVE_URL = "https://www.rbi.org.in/scripts/ReferenceRateArchive.aspx"
TABLE = "rbi_currency_rates"
# BUG FOUND LIVE 2026-08-19 (re-audit): the yearly-chunk backfill loop below had no per-run item
# cap, unlike bseindia/bhavcopy.py's own sibling fix for exactly this risk (BSE_BHAVCOPY_MAX_
# DATES_PER_RUN). A resume point far behind (e.g. after an extended CDP/site outage) would walk
# every intervening yearly chunk in a single cron invocation -- a single problematic window
# already cost 388s in a real run seen this session; a multi-year gap could turn one
# complete_data.sh slot into many minutes-to-hours, delaying every step scheduled after it.
RBI_CURRENCY_RATES_MAX_RUNTIME_SECONDS = env.int("RBI_CURRENCY_RATES_MAX_RUNTIME_SECONDS", 20 * 60)
# map a header's leading currency CODE -> tidy column; legacy EURO/YEN labels tolerated for robustness.
CURRENCY_CODES = {"USD": "usd", "GBP": "gbp", "EUR": "eur", "EURO": "eur",
                  "JPY": "jpy", "YEN": "jpy", "AED": "aed", "IDR": "idr"}
DEFAULT_START = date(2014, 1, 1)
STOCKEY_RUN_STATE: dict[str, object] = {}


def parse_rate_rows(rows: list[list[str]]) -> pd.DataFrame:
    """Pure: a 2D result-table grid (row 0 = header) -> tidy frame [date, usd, gbp, eur, jpy, aed, idr].

    Columns are matched by the currency CODE that leads each header cell ("USD (INR / 1 USD)" -> usd),
    so RBI's header/label changes don't silently corrupt the output the way the old fixed list did.
    """
    if not rows or len(rows) < 2:
        return pd.DataFrame(columns=["date"])
    header = [str(c).strip() for c in rows[0]]
    date_idx = None
    col_idx: dict[int, str] = {}
    for i, cell in enumerate(header):
        token = cell.split()[0].upper() if cell else ""
        if token == "DATE":
            date_idx = i
        elif token in CURRENCY_CODES:
            col_idx[i] = CURRENCY_CODES[token]
    if date_idx is None or not col_idx:
        # BUG FOUND LIVE 2026-08-19 (re-audit): silently returned an empty frame with no error and
        # no telemetry whenever the header didn't contain a recognized "DATE"/currency-code cell --
        # this module's own docstring documents that RBI has already relabeled this header once,
        # silently corrupting output before this fix existed. If it happens again, every chunk
        # would come back "0 rows, no error", reported as status "ok" with no trail distinguishing
        # it from "genuinely nothing new this window".
        record_local_fallback_event(
            module=SYNC_SOURCE_NAME,
            source="rbi_currency_rates:parse",
            fallback_type="rbi_currency_rates_header_not_recognized",
            severity="warn",
            reason=(
                "RBI reference-rate result table's header row had no recognized DATE/currency-code "
                "cell -- the source's header format may have changed again; this chunk parsed to 0 "
                "rows with no other error."
            ),
            error=None,
            metadata={"header": header},
        )
        return pd.DataFrame(columns=["date"])
    records = []
    for raw in rows[1:]:
        if len(raw) <= date_idx:
            continue
        rec: dict[str, object] = {"date": str(raw[date_idx]).strip()}
        for i, name in col_idx.items():
            rec[name] = raw[i] if i < len(raw) else None
        records.append(rec)
    df = pd.DataFrame(records)
    df["date"] = pd.to_datetime(df["date"], format="%d/%m/%Y", errors="coerce")
    for name in dict.fromkeys(col_idx.values()):
        df[name] = pd.to_numeric(df[name], errors="coerce")
    df = df.dropna(subset=["date"]).drop_duplicates(subset=["date"]).reset_index(drop=True)
    return df


def _empty_run_state(status: str = "running") -> dict[str, object]:
    now = datetime.now().isoformat()
    return {
        "source": SYNC_SOURCE_NAME,
        "status": status,
        "rows": 0,
        "rows_read": 0,
        "rows_written": 0,
        "attempt_count": 1,
        "download_attempts": 1,
        "failed_attempt_count": 0,
        "source_unavailable_count": 0,
        "fallback_used": False,
        "state_advanced": False,
        "started_at": now,
        "finished_at": now,
    }


def _fetch_range_with_retry(page, from_date: date, to_date: date, *, attempts: int = 3) -> list[list[str]]:
    """The RBI ASP.NET page intermittently fails to render the results table; retry a few times with a
    growing pause (reloading the form each time) before giving up on this one window."""
    last_exc = None
    for k in range(attempts):
        try:
            return _fetch_range(page, from_date, to_date)
        except (PlaywrightTimeoutError, PlaywrightError) as exc:
            last_exc = exc
            page.wait_for_timeout(3000 * (k + 1))
    raise last_exc


def _fetch_range(page, from_date: date, to_date: date) -> list[list[str]]:
    """Submit one date window on the archive form and return the result table as a 2D grid."""
    page.goto(ARCHIVE_URL)
    page.wait_for_timeout(1500)
    page.evaluate(
        """({f, t}) => {
            const all = document.getElementById('chkAll'); if (all) all.checked = true;
            document.getElementById('txtFromDate').value = f;
            document.getElementById('txtToDate').value = t;
        }""",
        {"f": from_date.strftime("%d/%m/%Y"), "t": to_date.strftime("%d/%m/%Y")},
    )
    page.click("#btnSubmit")
    # the results table is the table.tablebg whose first cell is 'Date' (the other tablebg is the form)
    page.wait_for_function(
        """() => [...document.querySelectorAll('table.tablebg')].some(t => {
            const c = t.querySelector('tr td, tr th'); return c && /^Date$/i.test((c.innerText||'').trim()); })""",
        timeout=120_000,
    )
    return page.evaluate(
        """() => {
            const t = [...document.querySelectorAll('table.tablebg')].find(t => {
                const c = t.querySelector('tr td, tr th'); return c && /^Date$/i.test((c.innerText||'').trim()); });
            if (!t) return [];
            return [...t.querySelectorAll('tr')].map(tr =>
                [...tr.querySelectorAll('td,th')].map(c => (c.innerText || '').trim()));
        }"""
    )


def download_currency_rates(playwright, from_date: date, to_date: date, *, dry_run: bool = False) -> dict[str, object]:
    """Automate the RBI reference-rate archive over CDP, chunking by 1-year windows (the page returns a
    full year, ~251 business days, per query)."""
    state = _empty_run_state()
    browser = None
    page = None
    try:
        browser = connect_over_cdp_guarded(playwright, CDP_ENDPOINT, caller="data.rbi.download_currency_rates")
        context = browser.contexts[0] if browser.contexts else browser.new_context()
        page = context.new_page()

        total = 0
        failed_chunks = 0
        time_budget_exceeded = False
        run_started = time.monotonic()
        span_min = span_max = None
        cursor = from_date
        while cursor <= to_date:
            if time.monotonic() - run_started >= RBI_CURRENCY_RATES_MAX_RUNTIME_SECONDS:
                time_budget_exceeded = True
                print(
                    f"  stopping: {RBI_CURRENCY_RATES_MAX_RUNTIME_SECONDS}s runtime budget reached "
                    f"with {cursor} .. {to_date} still remaining -- resumes from the DB max date next run",
                    flush=True,
                )
                break
            chunk_end = min(cursor + relativedelta(years=1) - timedelta(days=1), to_date)
            # a chunk that stays broken after retries is skipped (recorded), not fatal -- the rest of the
            # multi-year backfill still completes and a later run resumes the gap from the DB max date.
            try:
                rows = _fetch_range_with_retry(page, cursor, chunk_end)
            except (PlaywrightTimeoutError, PlaywrightError) as exc:
                failed_chunks += 1
                print(f"  {cursor} .. {chunk_end}: FAILED after retries ({type(exc).__name__}) -- skipping", flush=True)
                # BUG FOUND LIVE 2026-08-19 (re-audit): a skipped chunk was only ever printed, never
                # recorded via record_local_fallback_event -- no trail at all besides a cron log line.
                record_local_fallback_event(
                    module=SYNC_SOURCE_NAME,
                    source="rbi_currency_rates:chunk",
                    fallback_type="rbi_currency_rates_chunk_failed",
                    severity="warn",
                    reason="RBI reference-rate chunk failed after retries and was skipped; it stays missing and is retried on a later run.",
                    error=exc,
                    metadata={"from_date": cursor.isoformat(), "to_date": chunk_end.isoformat()},
                )
                cursor = chunk_end + timedelta(days=1)
                continue
            df = parse_rate_rows(rows)
            if not df.empty:
                if not dry_run:
                    upsert_to_db(df, TABLE, unique_keys=["date"], timescaledb_column="date")
                total += len(df)
                span_min = df["date"].min() if span_min is None else min(span_min, df["date"].min())
                span_max = df["date"].max() if span_max is None else max(span_max, df["date"].max())
            print(f"  {cursor} .. {chunk_end}: {len(df)} rows", flush=True)
            cursor = chunk_end + timedelta(days=1)
            page.wait_for_timeout(3000)  # RBI throttles rapid queries (partial tables) -- pace the submissions

        state.update({
            "status": "blocked" if time_budget_exceeded else ("ok" if failed_chunks == 0 else "partial"),
            "rows": int(total),
            "rows_read": int(total),
            "rows_written": int(total),
            "failed_attempt_count": int(failed_chunks),
            # BUG FOUND LIVE 2026-08-19 (re-audit): fallback_used was set once in the initial state
            # dict (False) and never updated even when chunks were skipped or the run stopped early.
            "fallback_used": bool(failed_chunks > 0 or time_budget_exceeded),
            "time_budget_exceeded": time_budget_exceeded,
            "from_date": span_min.date().isoformat() if span_min is not None else None,
            "to_date": span_max.date().isoformat() if span_max is not None else None,
            "state_advanced": bool(total > 0),
        })
        print(f"RBI currency rates updated: {total} rows ({failed_chunks} chunk(s) skipped)")
        return state
    except (PlaywrightTimeoutError, PlaywrightError) as exc:
        record_local_fallback_event(
            module=SYNC_SOURCE_NAME,
            source="rbi_currency_rates:browser",
            fallback_type="rbi_currency_rates_source_unavailable",
            severity="warn",
            reason="RBI reference-rate browser/CDP download failed; FX context may be stale until the source is reachable again.",
            error=exc,
            metadata={"cdp_endpoint": CDP_ENDPOINT, "status": "source_unavailable"},
        )
        print(f"RBI currency rates download skipped: {exc}")
        state.update({
            "status": "source_unavailable",
            "failed_attempt_count": 1,
            "source_unavailable_count": 1,
            "fallback_used": True,
            "error": f"{type(exc).__name__}: {exc}",
        })
        return state
    except Exception as exc:
        record_local_fallback_event(
            module=SYNC_SOURCE_NAME,
            source="rbi_currency_rates:download",
            fallback_type="rbi_currency_rates_download_failed",
            severity="warn",
            reason="RBI reference-rate download or parse failed; FX context may be stale until the failure is fixed and the job reruns.",
            error=exc,
            metadata={"cdp_endpoint": CDP_ENDPOINT, "status": "failed"},
        )
        print(f"RBI currency rates download skipped: {exc}")
        state.update({
            "status": "failed",
            "failed_attempt_count": 1,
            "fallback_used": True,
            "error": f"{type(exc).__name__}: {exc}",
        })
        return state
    finally:
        state["finished_at"] = datetime.now().isoformat()
        if page is not None:
            page.close()
        if browser is not None:
            browser.close()


def _default_from() -> date:
    """Resume from the day after the latest stored rate; first run starts at DEFAULT_START."""
    try:
        r = sql_to_df(f"SELECT MAX(date)::date AS mx FROM {TABLE}")
        if not r.empty and pd.notna(r.iloc[0]["mx"]):
            return r.iloc[0]["mx"] + timedelta(days=1)
    except Exception:
        pass
    return DEFAULT_START


def main() -> int:
    global STOCKEY_RUN_STATE
    ap = argparse.ArgumentParser(description="Download RBI reference FX rates into rbi_currency_rates via Chrome/CDP.")
    ap.add_argument("--from", dest="from_date", default=None, help="start date YYYY-MM-DD (default: resume from DB)")
    ap.add_argument("--to", dest="to_date", default=None, help="end date YYYY-MM-DD (default: today)")
    ap.add_argument("--dry-run", action="store_true", help="fetch + parse but do not write to the DB")
    args = ap.parse_args()
    frm = pd.to_datetime(args.from_date).date() if args.from_date else _default_from()
    # BUG FOUND LIVE 2026-08-19 (re-audit): date.today() computes "today" against host-local time
    # -- this host runs in UTC, not IST. Cron only ever invokes this at a time that maps to the
    # same IST calendar day (currently masking the gap), but any manual/ad-hoc invocation between
    # UTC 18:30-23:59 (IST 00:00-05:29) would compute "today" one IST day behind reality.
    to = pd.to_datetime(args.to_date).date() if args.to_date else pd.Timestamp.now(tz="Asia/Kolkata").date()
    print(f"RBI currency rates {frm} .. {to} (dry_run={args.dry_run})", flush=True)
    with sync_playwright() as p:
        STOCKEY_RUN_STATE = download_currency_rates(p, frm, to, dry_run=args.dry_run)
    print(json.dumps({"status": str(STOCKEY_RUN_STATE.get("status") or "ok"), **STOCKEY_RUN_STATE},
                     ensure_ascii=False, default=str), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
