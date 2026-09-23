from __future__ import annotations

from typing import Iterable, Optional, Sequence

import pandas as pd

from utils.fallback_telemetry import record_local_fallback_event
from utils.schema_migrations import apply_schema_migration

from .db import sql_to_df, upsert_to_db


COMPANY_MASTER_TABLE = "company_master"


def ensure_company_master_dhan_ids_are_bigint() -> None:
    """One-time migration (2026-08-15): dhan_bse_id/dhan_nse_id were stored as DOUBLE PRECISION --
    a LEFT merge introduces NaN for unmatched rows, which silently upcasts a pandas int64 column to
    float64, and upsert_to_db's dtype-to-column-type mapping then wrote DOUBLE PRECISION instead of
    BIGINT like the source (master_dhan_instruments.security_id). Converts both columns to BIGINT
    in place; sync_company_master() now also writes them as Int64 so this doesn't drift back."""
    apply_schema_migration(
        migration_id="20260815_company_master_dhan_ids_to_bigint",
        description="company_master.dhan_bse_id/dhan_nse_id: DOUBLE PRECISION -> BIGINT.",
        owner="utils.company_master",
        metadata={"tables": [COMPANY_MASTER_TABLE]},
        statements=[
            f"ALTER TABLE {COMPANY_MASTER_TABLE} ALTER COLUMN dhan_bse_id TYPE BIGINT USING (dhan_bse_id::BIGINT)",
            f"ALTER TABLE {COMPANY_MASTER_TABLE} ALTER COLUMN dhan_nse_id TYPE BIGINT USING (dhan_nse_id::BIGINT)",
        ],
    )


def _company_master_sql_to_df(query: str, *, params: object | None = None, operation: str) -> pd.DataFrame:
    try:
        return sql_to_df(query, params=params)
    except Exception as exc:
        record_local_fallback_event(
            module="utils.company_master",
            source=COMPANY_MASTER_TABLE,
            fallback_type="company_master_query_failed",
            severity="error",
            reason=f"company master query failed during {operation}",
            error=exc,
            metadata={"operation": operation},
        )
        raise


def sync_company_master() -> pd.DataFrame:
    ensure_company_master_dhan_ids_are_bigint()
    sharpely_columns = _company_master_sql_to_df(
        """
        SELECT column_name
        FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name = 'master_sharpely_equity'
        """,
        operation="sync_sharpely_columns",
    )
    has_sharpely_id = not sharpely_columns.empty and "sharpely_id" in set(sharpely_columns["column_name"])
    sharpely = _company_master_sql_to_df(
        f"""
        SELECT
            symbol AS nse_ticker,
            bse_ticker,
            proper_name AS company_name,
            {"sharpely_id" if has_sharpely_id else "NULL::text AS sharpely_id"}
        FROM master_sharpely_equity
        """,
        operation="sync_sharpely_equity",
    )
    if sharpely.empty:
        return sharpely

    # nse_ticker/bse_ticker specifically upper-cased (not company_name/sharpely_id, which must keep
    # their natural casing) -- 2026-08-15 found live: master_sharpely_equity is a third-party feed
    # whose casing isn't NSE-normalized (e.g. a real 'Praxis-RE1' row), while every NSE-sourced
    # caller of map_company_master_ids(exchange="NSE") cleans its own ticker with only .strip(),
    # never .upper(), because bhavcopy-derived symbols are already uppercase at the source. An
    # uppercase lookup for "PRAXIS-RE1" (what any bhavcopy-derived caller would look up) silently
    # failed to match this row under the old mixed-case value -- same failure shape as the
    # already-fixed BSE/NSE identity-resolution gap, just via inconsistent case instead of a
    # missing fallback exchange.
    sharpely["nse_ticker"] = _clean_text_series(sharpely["nse_ticker"]).str.upper()
    sharpely["bse_ticker"] = _clean_text_series(sharpely["bse_ticker"]).str.upper()
    sharpely["company_name"] = _clean_text_series(sharpely["company_name"])
    sharpely["sharpely_id"] = _clean_text_series(sharpely["sharpely_id"])

    sharpely = sharpely.dropna(subset=["nse_ticker", "bse_ticker"], how="all").copy()
    sharpely = sharpely.sort_values(["nse_ticker", "bse_ticker"], na_position="last")
    sharpely = sharpely.drop_duplicates(subset=["nse_ticker", "bse_ticker"], keep="last")

    # instrument_type IN ('ES', 'ETF'), not just 'ES' -- broadened 2026-08-20 (user-reported "a
    # lot of dhan errors") so ETFs are available for the Dhan-only-anchor addition below. Doesn't
    # change the enrichment behavior for sharpely-anchored rows above (sharpely is an
    # equity-fundamentals feed and has no ETF rows to match against either way).
    dhan_bse = _company_master_sql_to_df(
        """
        SELECT DISTINCT ON (security_id::text)
            security_id::text AS bse_ticker,
            security_id AS dhan_bse_id,
            COALESCE(NULLIF(display_name, ''), NULLIF(symbol_name, '')) AS dhan_bse_name
        FROM master_dhan_instruments
        WHERE valid_to IS NULL
          AND exch_id = 'BSE'
          AND instrument = 'EQUITY'
          AND instrument_type IN ('ES', 'ETF')
        ORDER BY security_id::text, load_ts DESC, valid_from DESC
        """,
        operation="sync_dhan_bse",
    )
    if not dhan_bse.empty:
        dhan_bse["bse_ticker"] = _clean_text_series(dhan_bse["bse_ticker"]).str.upper()

    dhan_nse = _company_master_sql_to_df(
        """
        SELECT DISTINCT ON (underlying_symbol)
            underlying_symbol AS nse_ticker,
            security_id AS dhan_nse_id,
            COALESCE(NULLIF(display_name, ''), NULLIF(symbol_name, '')) AS dhan_nse_name
        FROM master_dhan_instruments
        WHERE valid_to IS NULL
          AND exch_id = 'NSE'
          AND instrument = 'EQUITY'
          AND underlying_symbol IS NOT NULL
          AND (
                instrument_type IN ('ES', 'ETF')
                -- ...OR the symbol actually trades on NSE right now, whatever Dhan
                -- labelled it. 2026-09-02: 13 symbols in the live equity universe were
                -- excluded purely because Dhan files them as instrument_type='Other'
                -- (LIQGRWBEES, a Nippon liquid ETF, among them), leaving them with no
                -- company_master row and therefore no Dhan id and no intraday data.
                --
                -- Deliberately NOT broadening the filter to 'Other' outright: that
                -- bucket holds 747 NSE rows of which only 15 trade. The other 732 are
                -- exchange TEST instruments (011NSETEST, 021NSETEST, ...) and
                -- zero-coupon bonds (series N0, e.g. "ABCL ... 2031 SR C2") -- note a
                -- literal percent sign cannot appear anywhere in this string, even in a
                -- SQL comment: psycopg2 parses it as a parameter placeholder and the
                -- query dies with "IndexError: tuple index out of range". Admitting them
                -- would put hundreds of non-equities into the identity spine.
                --
                -- The bhavcopy is the authority on what trades on NSE; Dhan's
                -- instrument_type is only their labelling. Where the two disagree and
                -- bhavcopy says it traded this week, trust the bhavcopy.
                OR underlying_symbol IN (
                     SELECT DISTINCT symbol FROM nseindia_ohlcv
                      WHERE series IN ('EQ', 'BE')
                        AND date >= (SELECT max(date) FROM nseindia_ohlcv) - interval '7 days'
                   )
          )
        -- PREFER THE EQ SERIES. A symbol listed in both the rolling (EQ) and
        -- trade-to-trade (BE) segments has TWO Dhan security_ids, and this DISTINCT ON
        -- previously broke the tie by load_ts alone -- i.e. arbitrarily with respect to
        -- series. Measured 2026-09-02: of 227 universe symbols missing intraday data,
        -- 151 had resolved to their BE id and 145 of those had an EQ listing sitting
        -- right there. Dhan serves no intraday for the BE listing, so every one of them
        -- silently returned zero candles -- no error, no retry, just absent data.
        -- EMPOWER is the worked example: 762670 (EQ) has bars, 762673 (BE) has none,
        -- and we were asking for 762673 while the stock traded 107M shares in a month.
        --
        -- (series = 'EQ') DESC only breaks ties: a symbol that trades ONLY in BE still
        -- resolves to its BE id, because that is then the sole row.
        ORDER BY underlying_symbol, (series = 'EQ') DESC, load_ts DESC, valid_from DESC
        """,
        operation="sync_dhan_nse",
    )
    if not dhan_nse.empty:
        dhan_nse["nse_ticker"] = _clean_text_series(dhan_nse["nse_ticker"]).str.upper()

    company_master = sharpely.merge(dhan_bse, on="bse_ticker", how="left")
    company_master = company_master.merge(dhan_nse, on="nse_ticker", how="left")
    # dhan_bse_id/dhan_nse_id come from master_dhan_instruments.security_id (BIGINT) but a LEFT
    # merge introduces NaN for every unmatched row, which silently upcasts an int64 column to
    # float64 (classic pandas footgun) -- confirmed live 2026-08-15: company_master.dhan_bse_id/
    # dhan_nse_id were stored as DOUBLE PRECISION (e.g. "503696.0") instead of BIGINT like every
    # other ID column derived from this same source, a lossy-round-trip/type-drift risk for any
    # join or comparison against the real bigint ID columns. Int64 (pandas nullable) preserves
    # both the integer semantics and the NULL for unmatched rows through to upsert_to_db.
    company_master["dhan_bse_id"] = company_master["dhan_bse_id"].astype("Int64")
    company_master["dhan_nse_id"] = company_master["dhan_nse_id"].astype("Int64")
    company_master["company_master_id"] = company_master.apply(_build_company_master_id, axis=1)
    company_master = company_master[
        [
            "company_master_id",
            "company_name",
            "sharpely_id",
            "dhan_bse_id",
            "dhan_nse_id",
            "bse_ticker",
            "nse_ticker",
        ]
    ].copy()

    # BUG FOUND LIVE 2026-08-20 (user-reported: "a lot of dhan errors", all
    # "No company_master row found"): company_master is anchored ENTIRELY on
    # master_sharpely_equity -- master_dhan_instruments is only used to enrich a
    # row that already matched sharpely, never as its own anchor. Two real gaps
    # this caused, confirmed live:
    # (1) ETFs (BANKBEES, BANKPSU, BBETF0432, gold/silver funds, ...) -- Dhan
    #     classifies these instrument_type='ETF', not 'ES', and sharpely (an
    #     equity-fundamentals feed) doesn't carry funds at all -- they NEVER get a
    #     company_master row even though they're real, actively-traded NSE/BSE
    #     securities with real bhavcopy price data Dhan can and does serve OHLCV
    #     for.
    # (2) Ordinary companies Dhan already has a clean instrument_type='ES' mapping
    #     for on BOTH NSE and BSE (confirmed live: ADOR, AGL, ...) but that
    #     sharpely simply hasn't onboarded yet -- these are real companies, not an
    #     asset-class exclusion, just a coverage lag in the third-party feed this
    #     table is anchored on.
    # Confirmed live: 519 of the current EQ/BE bhavcopy universe had no
    # company_master row at all before this fix, causing "No company_master row
    # found" on every Dhan OHLCV sync attempt for every one of them, every run.
    # Added as Dhan-only rows (sharpely_id=NULL, company_master_id falls back to
    # _build_company_master_id's existing nse:/bse: scheme) for any NSE or BSE
    # Dhan instrument (instrument='EQUITY', instrument_type IN ('ES','ETF')) not
    # already covered by a sharpely-anchored row -- kept as SEPARATE per-exchange
    # rows rather than merged into one dual-listed row, deliberately: this
    # codebase has repeatedly found BSE ticker TEXT is not a reliable identity
    # key on its own (real coincidental collisions with unrelated NSE symbols,
    # see data/bseindia/bhavcopy.py's own docstring) -- joining a Dhan-only NSE
    # row to a Dhan-only BSE row purely by matching ticker text would risk
    # reintroducing exactly that class of false merge. get_company_master_equity's
    # existing per-exchange lookup (load_company_master_records) already resolves
    # a ticker-only row correctly for its own exchange, so this loses nothing for
    # the actual OHLCV-sync use case.
    covered_nse = set(company_master["nse_ticker"].dropna())
    covered_bse = set(company_master["bse_ticker"].dropna())
    dhan_only_nse = dhan_nse[~dhan_nse["nse_ticker"].isin(covered_nse)].copy() if not dhan_nse.empty else dhan_nse
    dhan_only_bse = dhan_bse[~dhan_bse["bse_ticker"].isin(covered_bse)].copy() if not dhan_bse.empty else dhan_bse
    dhan_only_rows: list[pd.DataFrame] = []
    if not dhan_only_nse.empty:
        rows = dhan_only_nse.rename(columns={"dhan_nse_name": "company_name"}).copy()
        rows["bse_ticker"] = None
        rows["sharpely_id"] = None
        rows["dhan_bse_id"] = pd.array([pd.NA] * len(rows), dtype="Int64")
        rows["dhan_nse_id"] = rows["dhan_nse_id"].astype("Int64")
        dhan_only_rows.append(rows)
    if not dhan_only_bse.empty:
        rows = dhan_only_bse.rename(columns={"dhan_bse_name": "company_name"}).copy()
        rows["nse_ticker"] = None
        rows["sharpely_id"] = None
        rows["dhan_nse_id"] = pd.array([pd.NA] * len(rows), dtype="Int64")
        rows["dhan_bse_id"] = rows["dhan_bse_id"].astype("Int64")
        dhan_only_rows.append(rows)
    if dhan_only_rows:
        additions = pd.concat(dhan_only_rows, ignore_index=True)
        additions["company_master_id"] = additions.apply(_build_company_master_id, axis=1)
        additions = additions[
            ["company_master_id", "company_name", "sharpely_id", "dhan_bse_id", "dhan_nse_id", "bse_ticker", "nse_ticker"]
        ]
        company_master = pd.concat([company_master, additions], ignore_index=True)

    # BUG FOUND LIVE 2026-08-20 (re-audit): this final dedup silently kept one row per
    # collided company_master_id with no telemetry at all -- the read-side equivalent
    # (map_company_master_ids's "company_master_ticker_collision" event, above) already
    # flags this same failure shape when resolving tickers, but the write side that
    # actually causes it stayed silent. A collision here means two distinct source rows
    # (e.g. two sharpely-less rows sharing an nse_ticker) computed the same
    # company_master_id and drop_duplicates(keep="last") discarded one of them with no
    # ORDER BY -- effectively arbitrary and, until now, invisible.
    duplicated_ids = (
        company_master.loc[company_master["company_master_id"].duplicated(keep=False), "company_master_id"]
        .unique()
        .tolist()
    )
    if duplicated_ids:
        record_local_fallback_event(
            module="utils.company_master",
            source=COMPANY_MASTER_TABLE,
            fallback_type="company_master_sync_id_collision",
            severity="warn",
            reason=(
                f"{len(duplicated_ids)} company_master_id value(s) computed for more than one "
                "distinct source row during sync; resolved via drop_duplicates(keep='last') with "
                "no ORDER BY, so the surviving row is effectively arbitrary and the other was "
                "silently dropped."
            ),
            error="duplicate company_master_id during sync",
            metadata={"collided_ids": sorted(duplicated_ids)[:20], "collided_id_count": len(duplicated_ids)},
        )

    company_master = company_master.drop_duplicates(subset=["company_master_id"], keep="last")

    upsert_to_db(company_master, COMPANY_MASTER_TABLE, ["company_master_id"])
    return company_master


def attach_company_master_id(
    df: pd.DataFrame,
    *,
    ticker_column: str,
    exchange: Optional[str] = None,
    exchange_column: Optional[str] = None,
    target_column: str = "company_master_id",
) -> pd.DataFrame:
    if df.empty:
        return df
    if ticker_column not in df.columns:
        raise KeyError(f"Ticker column not found: {ticker_column}")
    if exchange is None and exchange_column is None:
        raise ValueError("Either exchange or exchange_column is required")

    mapped = df.copy()
    mapped[ticker_column] = _clean_text_series(mapped[ticker_column])

    if exchange_column is not None:
        if exchange_column not in mapped.columns:
            raise KeyError(f"Exchange column not found: {exchange_column}")
        mapped[exchange_column] = _clean_text_series(mapped[exchange_column]).str.upper()
        nse_mask = mapped[exchange_column].eq("NSE")
        bse_mask = mapped[exchange_column].eq("BSE")
        mapped.loc[nse_mask, target_column] = map_company_master_ids(
            mapped.loc[nse_mask, ticker_column],
            exchange="NSE",
        ).values
        mapped.loc[bse_mask, target_column] = map_company_master_ids(
            mapped.loc[bse_mask, ticker_column],
            exchange="BSE",
        ).values
        return mapped

    mapped[target_column] = map_company_master_ids(mapped[ticker_column], exchange=exchange)
    return mapped


def map_company_master_ids(tickers: Iterable[object], *, exchange: str) -> pd.Series:
    # Preserve the caller's index when they pass a Series (e.g. df["symbol"] on a
    # filtered/non-contiguous-indexed df) -- list(tickers) previously discarded it in
    # favor of a fresh 0-based RangeIndex, so a caller doing df["x"] = map_company_master_ids(df["y"])
    # would silently misalign by index whenever df wasn't already 0-based-contiguous
    # (e.g. any prior row filter), scrambling which company_master_id lands on which row
    # with no error. Plain iterables (lists, etc.) have no index to preserve and keep the
    # old default-RangeIndex behavior.
    original_index = tickers.index if isinstance(tickers, pd.Series) else None
    ticker_series = pd.Series(list(tickers), dtype="string", index=original_index)
    cleaned = _clean_text_series(ticker_series)
    if cleaned.dropna().empty:
        return pd.Series(pd.NA, index=ticker_series.index, dtype="string")

    exchange_upper = exchange.upper()
    if exchange_upper == "NSE":
        column = "nse_ticker"
    elif exchange_upper == "BSE":
        column = "bse_ticker"
    else:
        raise ValueError(f"Unsupported exchange: {exchange}")

    lookup = _company_master_sql_to_df(
        f"""
        SELECT {column} AS ticker, company_master_id
        FROM {COMPANY_MASTER_TABLE}
        WHERE {column} = ANY(%s)
        """,
        params=(cleaned.dropna().unique().tolist(),),
        operation=f"map_{exchange_upper.lower()}_ids",
    )
    if lookup.empty:
        return pd.Series(pd.NA, index=ticker_series.index, dtype="string")

    # BUG FOUND LIVE 2026-08-17: two company_master rows sharing the same
    # nse_ticker/bse_ticker used to be resolved via drop_duplicates(keep="last") with
    # no visibility at all -- "last" here means whatever order the DB happened to
    # return them in (no ORDER BY on the query above), so which company_master_id a
    # colliding ticker actually maps to was effectively arbitrary and silent. Zero
    # real collisions exist today (confirmed live), but a future one would resolve
    # the exact same way with no record of it having happened.
    duplicated_tickers = lookup.loc[lookup["ticker"].duplicated(keep=False), "ticker"].unique().tolist()
    if duplicated_tickers:
        record_local_fallback_event(
            module="utils.company_master",
            source=COMPANY_MASTER_TABLE,
            fallback_type="company_master_ticker_collision",
            severity="warn",
            reason=(
                f"{len(duplicated_tickers)} {column} value(s) matched more than one company_master row; "
                "resolved via drop_duplicates(keep='last') with no ORDER BY, so the pick is effectively "
                "arbitrary, not necessarily the current/correct company."
            ),
            error="duplicate ticker mapping",
            metadata={"exchange": exchange_upper, "tickers": duplicated_tickers[:20]},
        )

    mapping = lookup.drop_duplicates(subset=["ticker"], keep="last").set_index("ticker")["company_master_id"]
    return cleaned.map(mapping).astype("string")


def map_company_master_ids_nse_or_bse(tickers: Iterable[object]) -> pd.Series:
    """Resolve tickers that may be either an NSE symbol or a BSE numeric scrip code --
    a cross-exchange source (screener.in and others) can return either for the same
    company, and this repo's identity is keyed primarily by nse_ticker. Tries NSE
    first, then falls back to bse_ticker for whatever's still unresolved.

    Confirmed live 2026-08-14: ~25% of the fundamentals screener's L1 universe (52 of
    191 tickers in one run) failed a plain exchange="NSE" match, with zero visibility
    that it had happened -- map_company_master_ids() returns NA silently, no
    fallback_telemetry event, no error. In a sample of 30, 24 actually had a real
    company_master row all along, just keyed under bse_ticker (screener.in reports
    the BSE code for some NSE-listed companies too, not only genuinely BSE-only
    ones). Use this instead of a bare exchange="NSE" call for any ticker whose
    source doesn't guarantee "this is definitely an NSE symbol" (EQUITY_L.csv-derived
    tickers, e.g. in fundamentals/collectors/security_master.py, are NSE-guaranteed
    and should keep using map_company_master_ids(..., exchange="NSE") directly)."""
    original_index = tickers.index if isinstance(tickers, pd.Series) else None
    ticker_series = pd.Series(list(tickers), dtype="string", index=original_index)
    resolved = map_company_master_ids(ticker_series, exchange="NSE")
    missing_mask = resolved.isna()
    if missing_mask.any():
        bse_resolved = map_company_master_ids(ticker_series[missing_mask], exchange="BSE")
        resolved.loc[missing_mask] = bse_resolved

    unresolved_tickers = sorted(ticker_series[resolved.isna()].dropna().unique().tolist())
    if unresolved_tickers:
        # genuinely unknown to company_master under EITHER exchange -- not a silent drop: the caller's
        # row still gets NA and (per each caller's own handling) is typically excluded downstream, but
        # this makes that exclusion visible instead of vanishing with zero trace.
        record_local_fallback_event(
            module="utils.company_master",
            source="map_company_master_ids_nse_or_bse",
            fallback_type="identity_unresolved_nse_and_bse",
            severity="warn",
            reason=f"{len(unresolved_tickers)} ticker(s) matched neither nse_ticker nor bse_ticker in company_master",
            error="no company_master match",
            metadata={"tickers": unresolved_tickers[:50]},
        )
    return resolved


def build_l1_ticker_by_company_master_id() -> dict[str, str]:
    """Reverse of map_company_master_ids_nse_or_bse: given a company_master_id, what
    fundamentals_l1_universe.ticker (screener.in's own slug -- the NSE symbol for
    most companies, but a raw BSE numeric scrip code for the ~22% BSE-only cohort)
    does it correspond to?

    BUG FOUND LIVE 2026-08-18: naive `company_master_id.removeprefix("nse:")` only
    recovers the correct L1 ticker when the company IS its own NSE symbol -- it's
    wrong for any BSE-only company, whose canonical company_master_id (e.g.
    "nse:ALUFLUOR" -- "nse:" is a namespace prefix on every company_master_id, not a
    literal NSE-listing claim) is NOT the same string as its L1-universe slug (e.g.
    "524634", the BSE scrip code screener.in uses as that company's own URL slug).
    Confirmed live: this exact naive-removeprefix mistake recurred independently
    across watch_summary.py, l3_triggers.py, llm_triage.py, portfolio_resolution.py,
    signal_pointers.py, and l2_state.py's pull_crawl_forward (plus the reverse
    construction mistake, f"nse:{ticker}", in technicals.py) -- the SAME bug
    ae8ff4b fixed by name in 3 OTHER files earlier the same session, missed here
    because each of these reads FROM a canonical id looking for the L1 ticker,
    while ae8ff4b's sites all went the other direction. One shared, correct
    resolver instead of ad-hoc string surgery at every call site: builds the
    reverse map by resolving the whole L1 universe forward (same machinery
    map_company_master_ids_nse_or_bse already uses) and inverting it. Cheap
    (L1 universe is ~200 rows) -- call fresh per use, no caller-managed cache."""
    tickers_df = sql_to_df(
        """
        SELECT ticker
        FROM fundamentals_l1_universe
        WHERE run_date = (SELECT MAX(run_date) FROM fundamentals_l1_universe)
        """
    )
    if tickers_df.empty:
        return {}
    resolved = map_company_master_ids_nse_or_bse(tickers_df["ticker"])
    # last-wins on a genuine collision (two L1 tickers resolving to the same
    # company_master_id) is the same tradeoff the forward direction already
    # accepts elsewhere in this module -- rare enough not to warrant its own event.
    return {cmid: ticker for cmid, ticker in zip(resolved, tickers_df["ticker"]) if pd.notna(cmid)}


def load_company_master_records(ticker: str, exchanges: Optional[Sequence[str]] = None) -> pd.DataFrame:
    clean_ticker = _clean_scalar(ticker)
    if clean_ticker is None:
        return pd.DataFrame()

    exchanges_upper = [value.upper() for value in exchanges] if exchanges else []
    if not exchanges_upper:
        query = f"""
            SELECT *
            FROM {COMPANY_MASTER_TABLE}
            WHERE nse_ticker = %s OR bse_ticker = %s
            ORDER BY company_master_id
        """
        return _company_master_sql_to_df(query, params=(clean_ticker, clean_ticker), operation="load_any_exchange_records")

    frames: list[pd.DataFrame] = []
    if "NSE" in exchanges_upper:
        frames.append(
            _company_master_sql_to_df(
                f"SELECT * FROM {COMPANY_MASTER_TABLE} WHERE nse_ticker = %s ORDER BY company_master_id",
                params=(clean_ticker,),
                operation="load_nse_records",
            )
        )
    if "BSE" in exchanges_upper:
        frames.append(
            _company_master_sql_to_df(
                f"SELECT * FROM {COMPANY_MASTER_TABLE} WHERE bse_ticker = %s ORDER BY company_master_id",
                params=(clean_ticker,),
                operation="load_bse_records",
            )
        )
    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True).drop_duplicates(subset=["company_master_id"], keep="last")


def _build_company_master_id(row: pd.Series) -> str:
    # CAVEAT (found live 2026-08-15, fixing the nse_ticker case-normalization bug above): since
    # company_master_id is DERIVED from nse_ticker/bse_ticker (for rows without a sharpely_id), any
    # future change to how a ticker's case/text is cleaned changes the computed ID too -- upsert_to_
    # db's ON CONFLICT (company_master_id) then INSERTs a new row under the new ID rather than
    # UPDATEing the old one, silently orphaning the old row (which this fix's own rollout did for
    # one real row, "Praxis-RE1" -> "PRAXIS-RE1"; cleaned up manually, see 2026-08-15 commit). A
    # sharpely_id-keyed row (the common case) isn't affected -- sharpely_id doesn't change case here.
    sharpely_id = _clean_scalar(row.get("sharpely_id"))
    nse_ticker = _clean_scalar(row.get("nse_ticker"))
    bse_ticker = _clean_scalar(row.get("bse_ticker"))
    if sharpely_id:
        return f"sharpely:{sharpely_id}"
    if nse_ticker:
        return f"nse:{nse_ticker}"
    if bse_ticker:
        return f"bse:{bse_ticker}"
    raise ValueError("Cannot build company_master_id without a ticker or sharpely_id")


def _clean_text_series(series: pd.Series) -> pd.Series:
    cleaned = series.astype("string").str.strip()
    return cleaned.mask(cleaned.eq("")).astype("string")


def _clean_scalar(value: object) -> Optional[str]:
    if pd.isna(value):
        return None
    text = str(value).strip()
    return text or None
