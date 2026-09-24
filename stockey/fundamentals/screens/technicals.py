"""Long-term descriptive technicals -- feeds the fundamentals screener's watch-summary
narrative (fundamentals/screens/watchlist.py) and the sectors view, per the boundary
worked out in conversation 2026-08-11: momentum and mean-reversion are DESCRIPTIVE
facts here (observable, unparametrized, no entry/exit signal), never a scored or
trading-actionable signal -- that stays exclusively in systrader's LEDGER-governed
research process (see stockey/CLAUDE.md's boundary section and
docs/FUNDAMENTAL_SCREENER_PRD.md sec 1: "the two must never be blended into one
decision pipeline"). This module answers "is this stock currently up a lot / down a
lot / stretched from its own average", nothing more -- no buy/sell/hold verdict, no
threshold-based classification, no backtest.

BUG FOUND LIVE 2026-08-22: "feeds the watchlist" wasn't actually true for any
watchlist company outside the CURRENT L1 universe snapshot -- run_technicals_refresh()
only ever scanned load_l1_tickers(), and fundamentals_watchlist is a different,
L3-alert-sourced population not gated on L1 screening membership at all. A company
that never entered L1, or has since dropped out of it, silently stopped getting
technicals forever, with no fallback event. Fixed via load_watchlist_only_tickers()
-- see its own docstring for the live-confirmed cases.

Uses advisory_adjusted_ohlcv_daily (stockey's own split/bonus-adjusted PRIMARY equity
series, keyed by bare `symbol`, not company_master_id -- confirmed live 2026-08-11),
not raw nseindia_ohlcv: momentum windows here (3/6/12 months) are long enough to
routinely cross a corporate action, unlike llm_triage.py's short 20-session price
context where raw close is fine. Coverage confirmed live: 138/188 L1 tickers have an
adjusted series (73%) -- the remainder (recent listings, series gaps) get a row with
all stats None plus a fallback event, not silently skipped.

BSE-only-company fallback (2026-08-15): a company with no NSE listing (confirmed
live -- 43 of the active L1 universe) has nothing in advisory_adjusted_ohlcv_daily
at all, since that view is entirely NSE/Dhan-sourced. load_adjusted_price_history()
falls back to bse_advisory_adjusted_ohlcv_daily (data/bseindia/price_adjustment.py)
keyed by scrip_code -- fundamentals_l1_universe.ticker for a BSE-only company IS
already its BSE scrip code (confirmed live: company_master's identity resolution
stores the scrip code in that slot for a company with no NSE ticker), so no extra
identity lookup is needed here, the same `ticker` value is used against both views.
This is a cross-package READ of a data/ table (fundamentals building on top of the
pure-TA platform, not the reverse -- see CLAUDE.md's boundary section); this module
still writes only its own fundamentals_technicals table.

Windows are in trading days (63/126/252 for 3/6/12 months, standard convention), not
calendar days -- a company needs that many rows of history for a given window to be
computed at all; shorter-history companies get partial rows (some windows None), never
padded or estimated.
"""

from __future__ import annotations

import json

import pandas as pd

from data.bseindia.price_adjustment import ensure_view as ensure_bse_view
from utils.company_master import map_company_master_ids_nse_or_bse
from utils.db import sql_to_df, upsert_to_db
from utils.fallback_telemetry import record_local_fallback_event

SYNC_SOURCE_NAME = "fundamentals.screens.technicals"
RESULTS_TABLE = "fundamentals_technicals"
STOCKEY_RUN_STATE: dict[str, object] = {}

# Trading-day approximations for calendar months -- standard convention, not tuned.
RETURN_WINDOWS_TRADING_DAYS = {"3m": 63, "6m": 126, "12m": 252}
DMA_WINDOWS = (50, 200)
# Below this many rows of adjusted-close history, no stats are computed at all (an
# empty row is still written, with a fallback event, so the gap is visible rather than
# the company just silently missing from the table).
MIN_HISTORY_ROWS = 20
# BUG FOUND LIVE 2026-08-15: load_adjusted_price_history()'s "latest row on or before
# today" query happily returns a months-old row with no indication it's stale --
# confirmed live, 1,255/3,979 symbols (32%) in advisory_adjusted_ohlcv_daily hadn't
# updated since before 2026-08-10, and one real watchlist company's "today's close"
# (shown in the digest email and used by watchlist_exit.py's price-move check) had
# been silently frozen at an April value for weeks. 7 calendar days tolerates a
# weekend/holiday lag without flagging every minor delay as stale.
STALE_PRICE_THRESHOLD_DAYS = 7


# Confirmed live 2026-08-18: a genuinely healthy day carries ~4,100-4,150 rows in
# bse_advisory_adjusted_ohlcv_daily (out of ~4,325 distinct scrip_codes ever seen).
# Set well below that real range but comfortably above "one stray row from an
# unrelated backfill" -- the exact ambiguity check_bse_price_pipeline_freshness's
# own docstring already flagged MAX(date) alone couldn't resolve.
MIN_BSE_PIPELINE_COVERAGE_ROWS = 500


def check_bse_price_pipeline_freshness(run_date) -> bool:
    """One aggregate check, not per-company -- distinguishes "the whole BSE OHLCV/
    adjustment pipeline (data/bseindia/bhavcopy.py + price_adjustment.py, both
    separate pure-TA cron jobs -- see stockey/CLAUDE.md's cron job 4,
    all_price_adjustment.sh) hasn't run recently" from "this one company genuinely
    lacks history", which the per-ticker technicals_insufficient_history event below
    can't distinguish on its own -- a company with data_points_available=0 reads
    identically whether IT has no listing history or the ENTIRE upstream table is
    empty/stale.

    BUG FOUND LIVE 2026-08-17 (structural, no live incident yet -- fundamentals/
    run_pipeline.py's own STEPS has no step that refreshes BSE OHLCV/adjustment
    data at all, an implicit dependency on a separate cron job): the fundamentals
    screener's own cron (all_fundamentals_screener.sh, 19:15 UTC) happens to run
    after all_price_adjustment.sh (18:50 UTC) today, but that ordering lives in two
    separate crontab lines, not any explicit dependency this module enforces or even
    checks -- a manual run of this pipeline, a future reordering, or a skipped
    upstream run would silently read whatever stale/empty BSE data exists with no
    distinct signal that upstream hasn't run.

    BUG FOUND LIVE 2026-08-18 (re-audit): the first version of this check only
    queried bseindia_ohlcv (cron job 1, the raw bhavcopy) and only checked
    MAX(date) -- both gaps missed the exact live scenario it exists to catch.
    bseindia_ohlcv is written by job 1; bse_advisory_adjusted_ohlcv_daily (what
    load_adjusted_price_history() actually reads) is an INNER JOIN against
    bseindia_adjustment_factors, written by job 4 -- if job 4 fails or is skipped,
    bseindia_ohlcv stays fresh, the old check returned True, and every new date
    silently drops out of the joined view anyway. Separately, MAX(date) alone
    can't tell "a genuinely healthy day" from "one stray row from an unrelated
    backfill." Confirmed live: on the actual 2026-08-17 production run,
    bseindia_ohlcv held only 3 days of history total (the 365-day backfill has
    never completed), but those 3 days were recent enough that the old check
    returned True and recorded zero fallback events that day, while 43 of 53 real
    technicals_insufficient_history events were the exact BSE-only cohort this
    check exists to distinguish. Now checks the joined VIEW itself (so a stalled
    job 4 is caught, not just a stalled job 1) and requires real row coverage on
    the latest date, not just its existence."""
    df = sql_to_df(
        """
        SELECT date, COUNT(*) AS n
        FROM bse_advisory_adjusted_ohlcv_daily
        WHERE date >= CURRENT_DATE - 30   -- bounded: a whole-view GROUP BY walks every chunk
        GROUP BY date
        ORDER BY date DESC
        LIMIT 1
        """
    )
    if df.empty:
        return False
    latest = pd.Timestamp(df.iloc[0]["date"])
    if latest.tzinfo is None:
        latest = latest.tz_localize("UTC")
    if (run_date - latest).days > STALE_PRICE_THRESHOLD_DAYS:
        return False
    return int(df.iloc[0]["n"]) >= MIN_BSE_PIPELINE_COVERAGE_ROWS


def _record_fallback(fallback_type: str, *, reason: str, error, severity: str = "warn", metadata=None) -> None:
    record_local_fallback_event(
        module=SYNC_SOURCE_NAME,
        source="advisory_adjusted_ohlcv_daily",
        fallback_type=fallback_type,
        severity=severity,
        reason=reason,
        error=error,
        metadata=metadata or {},
    )


def load_l1_tickers() -> pd.DataFrame:
    return sql_to_df(
        """
        SELECT ticker, company_name
        FROM fundamentals_l1_universe
        WHERE run_date = (SELECT MAX(run_date) FROM fundamentals_l1_universe)
        """
    )


def load_watchlist_only_tickers(covered_company_master_ids) -> pd.DataFrame:
    """Active watchlist companies NOT already covered by the current L1 universe
    snapshot.

    BUG FOUND LIVE 2026-08-22: this module's own docstring says it "feeds the
    fundamentals screener's watch-summary narrative (watchlist.py)", but until
    now run_technicals_refresh() only ever scanned load_l1_tickers() -- the L1
    screening universe, a DIFFERENT and narrower population than the watchlist
    (fundamentals_watchlist is L3-alert-sourced and not gated on L1 screening
    membership at all; a company can enter the watchlist without ever being in
    L1, or after having already left it). Once a watchlist company drops out of
    L1 (or never enters it), it silently stopped getting technicals refreshes
    forever -- no fallback event, because run_technicals_refresh() never even
    attempted it. Confirmed live: REGENCERAM (in L1 exactly once, 2026-08-13,
    zero fundamentals_technicals rows ever -- even that one day's run didn't
    produce a row for it); PIONRINV/BANARISUG/KRITINUT/BGWTATO all dropped out
    of L1 after 2026-08-13/19/20 and are frozen at that day's close, 2-9 days
    stale and climbing, with price_data_stale itself now stale (it was computed
    correctly as of the last run, nothing has re-evaluated it since).

    ticker is nse_ticker if the company has one, else bse_scrip_code (matching
    fundamentals_l1_universe's own convention -- a BSE-only ticker slot already
    holds the scrip code, see this module's docstring) -- load_adjusted_price_
    history's existing EQ->BE->BSE-scrip-code fallback chain handles either
    correctly with no changes needed there."""
    # BUG FOUND LIVE 2026-08-30 (adversarial review): fundamentals_watchlist's
    # `status` column is bootstrapped by watchlist_exit.py's
    # _bootstrap_status_columns(), called from notifications.py -- which runs
    # AFTER technicals in run_pipeline.py's STEPS. On a truly fresh DB's first-
    # ever pipeline run, this table (or its status column) may not exist yet,
    # which would otherwise crash the whole technicals step rather than just
    # degrade this one watchlist-only-companies lookup. Self-heals from the
    # second run onward (notifications.py creates/bootstraps both by then), so
    # this is a first-run-only hazard -- but the same "don't crash the whole
    # step for a missing table" tolerance every other market-wide query in this
    # pipeline already gets (e.g. l2_state.py's fetch_pledge_levels).
    try:
        df = sql_to_df(
            """
            SELECT DISTINCT w.company_master_id,
                   COALESCE(cm.nse_ticker, cm.bse_scrip_code) AS ticker
            FROM fundamentals_watchlist w
            JOIN company_master cm ON cm.company_master_id = w.company_master_id
            -- Every watchlist status, not only 'active' (2026-09-23 audit): a flagged name
            -- stopped being priced, its price went stale, and the exit check read the
            -- stale price as "cannot judge" -> active -> priced -> flagged, every ~8 days.
            """
        )
    except Exception as exc:  # noqa: BLE001 -- a missing table/column (first-ever pipeline run) must not crash the whole technicals step
        _record_fallback(
            "technicals_watchlist_only_lookup_failed",
            reason="Could not query fundamentals_watchlist for watchlist-only companies (likely a first-ever pipeline run, before notifications.py has created/bootstrapped it) -- watchlist companies outside the L1 universe were not scanned this run.",
            error=exc,
        )
        return pd.DataFrame(columns=["ticker", "company_master_id"])
    if df.empty:
        return df
    df = df[~df["company_master_id"].isin(covered_company_master_ids)]
    df = df.dropna(subset=["ticker"])
    return df[["ticker", "company_master_id"]].reset_index(drop=True)


def load_adjusted_price_history(ticker: str, *, lookback_days: int = 300) -> pd.DataFrame:
    # 2026-09-23 data audit, three fixes in one read:
    #  * DATE-BOUNDED. The view sits on a hypertable; "ORDER BY date DESC LIMIT n" with no
    #    bound walks every chunk, per ticker, every run. 2x the row count in calendar days
    #    comfortably holds `lookback_days` trading sessions.
    #  * FRESHEST SERIES WINS. EQ used to win whenever it had ANY rows, so a name moved to
    #    BE (trade-to-trade) was priced off a frozen EQ tail.
    #  * NSE SYMBOL FIRST. An L1 slug can be a BSE code for an NSE-listed company
    #    (ALUFLUOR = 524634), which priced it off the BSE series while first_seen_price
    #    came from NSE -- the price_flagged move then compared two exchanges.
    symbol = ticker
    if str(ticker).isdigit():
        nse = sql_to_df("SELECT nse_ticker FROM company_master WHERE bse_scrip_code = %s AND nse_ticker IS NOT NULL LIMIT 1",
                        params=(str(ticker),))
        if not nse.empty:
            symbol = str(nse.iloc[0]["nse_ticker"])
    nse_history = sql_to_df(
        """
        SELECT date, adj_close, series
        FROM advisory_adjusted_ohlcv_daily
        WHERE symbol = %s AND series IN ('EQ', 'BE')
          AND date >= CURRENT_DATE - %s
        ORDER BY date DESC
        """,
        params=(symbol, lookback_days * 2),
    )
    if not nse_history.empty:
        latest_by_series = nse_history.groupby("series")["date"].max()
        best = "EQ" if latest_by_series.get("EQ") is not None and latest_by_series.get("EQ") >= latest_by_series.max() else latest_by_series.idxmax()
        chosen = nse_history[nse_history["series"] == best].head(lookback_days)
        return chosen[["date", "adj_close"]].sort_values("date").reset_index(drop=True)

    # BSE-only-company fallback -- see module docstring. `ticker` here is USUALLY
    # already the BSE scrip code for BSE-only L1 companies (matching bse_advisory_
    # adjusted_ohlcv_daily's own scrip_code key directly) -- but not always.
    #
    # BUG FOUND LIVE 2026-08-22: a company can have a real nse_ticker recorded in
    # company_master with ZERO actual EQ/BE rows (an NSE identity exists but never
    # actually traded/listed there in the adjusted series) while its real price
    # history lives under a DIFFERENT identifier, the BSE scrip code. Confirmed
    # live: BGWTATO -- nse_ticker='BGWTATO' (zero advisory_adjusted_ohlcv_daily
    # rows, either series) vs bse_scrip_code='504646' (247 real bse_advisory_
    # adjusted_ohlcv_daily rows). Querying scrip_code='BGWTATO' (the old
    # behavior, reusing the same `ticker` string) can never match a numeric
    # scrip code. Resolve the TRUE scrip code via company_master instead of
    # assuming `ticker` already is one -- matches either nse_ticker or
    # bse_scrip_code, so this is correct both for L1's BSE-only convention
    # (ticker IS already the scrip code, matches via the second clause) and for
    # this mixed case (ticker is an NSE ticker with no NSE data of its own).
    scrip = sql_to_df(
        "SELECT bse_scrip_code FROM company_master WHERE nse_ticker = %s OR bse_scrip_code = %s LIMIT 1",
        params=(ticker, ticker),
    )
    if scrip.empty or pd.isna(scrip.iloc[0]["bse_scrip_code"]):
        return pd.DataFrame(columns=["date", "adj_close"])
    bse_history = sql_to_df(
        """
        SELECT date, adj_close
        FROM bse_advisory_adjusted_ohlcv_daily
        WHERE scrip_code = %s AND date >= CURRENT_DATE - %s
        ORDER BY date DESC
        LIMIT %s
        """,
        params=(str(scrip.iloc[0]["bse_scrip_code"]), lookback_days * 2, lookback_days),
    )
    return bse_history.sort_values("date").reset_index(drop=True)


def compute_technicals(history: pd.DataFrame) -> dict:
    """Pure function over an ascending-by-date (date, adj_close) frame -- returns
    descriptive stats only, None wherever there isn't enough history for a given
    window (never estimated or padded)."""
    if history.empty:
        return {"as_of_date": None, "close": None, "data_points_available": 0}

    closes = history["adj_close"]
    latest_close = float(closes.iloc[-1])
    n = len(closes)
    result: dict[str, object] = {
        "as_of_date": history["date"].iloc[-1],
        "close": latest_close,
        "data_points_available": int(n),
    }

    for label, window in RETURN_WINDOWS_TRADING_DAYS.items():
        if n > window:
            past_close = float(closes.iloc[-1 - window])
            result[f"return_{label}_pct"] = round((latest_close - past_close) / past_close * 100, 2) if past_close else None
        else:
            result[f"return_{label}_pct"] = None

    for window in DMA_WINDOWS:
        if n >= window:
            dma = float(closes.tail(window).mean())
            result[f"dma_{window}"] = round(dma, 2)
            result[f"pct_vs_dma_{window}"] = round((latest_close - dma) / dma * 100, 2) if dma else None
        else:
            result[f"dma_{window}"] = None
            result[f"pct_vs_dma_{window}"] = None

    # Mean-reversion descriptive stat: how many standard deviations the current close
    # sits from its own trailing-200-day mean -- a fact about dispersion, not a signal
    # threshold (no "buy at -2 sigma" rule lives here).
    if n >= 200:
        window_closes = closes.tail(200)
        mean_200 = float(window_closes.mean())
        std_200 = float(window_closes.std())
        result["z_score_vs_200d_mean"] = round((latest_close - mean_200) / std_200, 2) if std_200 else None
    else:
        result["z_score_vs_200d_mean"] = None

    return result


def run_technicals_refresh() -> dict[str, object]:
    ensure_bse_view()  # self-heals bseindia_ohlcv/bseindia_adjustment_factors/the view -- see module docstring
    tickers = load_l1_tickers()
    l1_universe_empty = tickers.empty
    if l1_universe_empty:
        _record_fallback(
            "technicals_no_l1_universe",
            reason="No fundamentals_l1_universe rows to compute technicals for.",
            error="empty L1 universe",
        )
        # BUG FOUND LIVE 2026-08-30 (adversarial review): used to return here
        # unconditionally -- the exact failure mode load_watchlist_only_tickers's
        # own 2026-08-22 fix was built to close (a watchlist company outside the
        # L1 universe going permanently unrefreshed) would still happen on any
        # run where the L1 universe snapshot itself was empty, since this early
        # return short-circuited BEFORE watchlist-only companies were ever
        # loaded. Fall through with an empty L1 frame instead -- watchlist-only
        # companies still get scanned; the real early-return (nothing at all to
        # do) moves below, after both sources have had a chance to contribute.
        tickers = pd.DataFrame(columns=["ticker", "company_name"])

    run_date = pd.Timestamp.now(tz="UTC").normalize()
    if not check_bse_price_pipeline_freshness(run_date):
        _record_fallback(
            "technicals_bse_price_pipeline_stale_or_never_run",
            reason=(
                "bseindia_ohlcv has no row within STALE_PRICE_THRESHOLD_DAYS (or is entirely empty) -- "
                "the separate pure-TA BSE OHLCV/adjustment cron (stockey/CLAUDE.md cron job 4) looks like "
                "it hasn't run recently. Every BSE-only company's technicals_insufficient_history event "
                "below may really be this, not a per-company gap."
            ),
            error="bseindia_ohlcv stale or empty",
        )

    # BUG FOUND LIVE 2026-08-18: the 4th file the ae8ff4b identity-resolution fix
    # missed (that commit fixed the same naive f"nse:{ticker}" construction in
    # l1_universe.py/l2_state.py/sector_cycle.py, but not here) -- ~22% of L1
    # tickers are raw BSE scrip codes, not NSE symbols, so f"nse:{ticker}" landed
    # every BSE-only company's technicals row under an id nothing else in the
    # pipeline uses. Resolved via map_company_master_ids_nse_or_bse instead, same
    # helper every other fixed call site already uses.
    tickers["company_master_id"] = map_company_master_ids_nse_or_bse(tickers["ticker"])

    # BUG FOUND LIVE 2026-08-22: see load_watchlist_only_tickers's own docstring --
    # this module's docstring promises to feed the watchlist, but scanning only
    # load_l1_tickers() left any watchlist company outside the current L1 snapshot
    # (never in L1, or dropped out since) permanently unrefreshed with no signal.
    # These rows already carry a resolved company_master_id (no map_company_master_
    # ids_nse_or_bse call needed), so they're concatenated in directly.
    watchlist_only = load_watchlist_only_tickers(set(tickers["company_master_id"].dropna()))
    if not watchlist_only.empty:
        tickers = pd.concat([tickers[["ticker", "company_master_id"]], watchlist_only], ignore_index=True)

    rows = []
    no_history = 0
    stale_tickers: list[str] = []
    for _, row in tickers.iterrows():
        ticker = row["ticker"]
        company_master_id = row["company_master_id"]
        if pd.isna(company_master_id):
            continue  # unresolved -- map_company_master_ids_nse_or_bse already recorded its own fallback event
        history = load_adjusted_price_history(ticker)
        stats = compute_technicals(history)
        if stats["data_points_available"] < MIN_HISTORY_ROWS:
            no_history += 1
            _record_fallback(
                "technicals_insufficient_history",
                reason="Fewer than MIN_HISTORY_ROWS of advisory_adjusted_ohlcv_daily history for this ticker; row written with stats=None rather than skipped.",
                error="insufficient history",
                metadata={"ticker": ticker, "rows_available": stats["data_points_available"]},
            )

        # price_data_stale: the latest available row is more than STALE_PRICE_
        # THRESHOLD_DAYS behind this run -- the price feed itself has stopped
        # updating for this ticker, distinct from "insufficient history" above
        # (which is about too FEW rows ever existing, not the latest one being old).
        # Surfaced as its own column (not just a fallback event) so downstream
        # consumers -- the digest email, watchlist_exit.py's price-move check -- can
        # tell "today's close" apart from a stale, months-old value silently served
        # as if current.
        price_data_stale = False
        as_of_date = stats.get("as_of_date")
        if as_of_date is not None:
            as_of_ts = pd.Timestamp(as_of_date)
            if as_of_ts.tzinfo is None:
                as_of_ts = as_of_ts.tz_localize("UTC")
            if (run_date - as_of_ts).days > STALE_PRICE_THRESHOLD_DAYS:
                price_data_stale = True
                stale_tickers.append(ticker)

        rows.append(
            {
                "company_master_id": company_master_id,
                "ticker": ticker,
                "run_date": run_date,
                **{k: v for k, v in stats.items() if k != "data_points_available"},
                "data_points_available": stats["data_points_available"],
                "price_data_stale": price_data_stale,
                "load_ts": pd.Timestamp.now(tz="UTC"),
            }
        )

    if stale_tickers:
        # One aggregate event, not one per ticker -- confirmed live this can affect
        # a third of the whole universe, and a fallback event per ticker at that
        # scale would itself be a form of noise (buries the signal, not surfaces it).
        _record_fallback(
            "technicals_stale_price_series",
            reason=f"{len(stale_tickers)} tickers' latest available price row is more than {STALE_PRICE_THRESHOLD_DAYS} days old -- price_data_stale=True written for each, not silently served as current.",
            error="stale price series",
            metadata={"count": len(stale_tickers), "sample_tickers": stale_tickers[:20]},
        )

    result_df = pd.DataFrame(rows)
    upsert_to_db(result_df, RESULTS_TABLE, unique_keys=["company_master_id", "run_date"])
    return {"companies": int(len(result_df)), "no_history": no_history, "stale_price": len(stale_tickers)}


def main() -> int:
    global STOCKEY_RUN_STATE
    result = run_technicals_refresh()
    STOCKEY_RUN_STATE = {
        "source": SYNC_SOURCE_NAME,
        "rows": result["companies"],
        "rows_written": result["companies"],
        "no_history": result["no_history"],
        "stale_price": result["stale_price"],
        "fallback_used": result["no_history"] > 0 or result["stale_price"] > 0,
        "state_advanced": result["companies"] > 0,
        "status": "ok",
    }
    print(json.dumps(STOCKEY_RUN_STATE, ensure_ascii=False, default=str), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
