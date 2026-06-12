from __future__ import annotations

import argparse
import json

import numpy as np
import pandas as pd

from advisory.data_sync import ensure_advisory_symbol_inputs
from advisory.fallback_telemetry import record_local_fallback_event
from advisory.peer_sync import sync_peer_data
from features.tutils import get_max_date
from utils.company_master import map_company_master_ids
from utils.db import sql_to_df, upsert_to_db
from utils.sync import load_tracked_symbols, parse_datetime_arg


TABLE_NAME = "advisory_fundamentals_daily"
DEFAULT_STATEMENT_LAG_DAYS = 45
DEFAULT_SHAREHOLDING_LAG_DAYS = 21
MIN_PEER_FUNDAMENTAL_COUNT = 3


def _record_fundamental_snapshot_fallback(
    *,
    fallback_type: str,
    source: str,
    reason: str,
    error: Exception,
    metadata: dict[str, object] | None = None,
) -> None:
    record_local_fallback_event(
        module="advisory.fundamental_snapshot",
        fallback_type=fallback_type,
        source=source,
        severity="warn",
        reason=reason,
        error=error,
        metadata=metadata or {},
    )


def _source_sql_to_df(
    query: str,
    *,
    params: tuple[object, ...] | None,
    fallback_type: str,
    source: str,
    reason: str,
    metadata: dict[str, object] | None = None,
) -> pd.DataFrame:
    try:
        return sql_to_df(query, params=params)
    except Exception as exc:
        _record_fundamental_snapshot_fallback(
            fallback_type=fallback_type,
            source=source,
            reason=reason,
            error=exc,
            metadata=metadata,
        )
        raise


def normalize_timestamp(series: pd.Series) -> pd.Series:
    return pd.to_datetime(series, utc=True, errors="coerce").dt.normalize()


def resolve_universe(symbols: list[str] | None) -> pd.DataFrame:
    tickers = load_tracked_symbols(symbols)
    if not tickers:
        query = """
            SELECT DISTINCT company_master_id, symbol
            FROM stmt_income
            WHERE company_master_id IS NOT NULL
            ORDER BY symbol
        """
        universe = _source_sql_to_df(
            query,
            params=None,
            fallback_type="fundamental_snapshot_universe_load_failed",
            source="stmt_income",
            reason="Fundamental snapshot could not load the default fundamentals universe.",
        )
        if universe.empty:
            return universe
        universe["symbol"] = universe["symbol"].astype("string").str.upper()
        return universe

    try:
        company_master_ids = map_company_master_ids(tickers, exchange="NSE")
    except Exception as exc:
        _record_fundamental_snapshot_fallback(
            fallback_type="fundamental_snapshot_company_master_mapping_failed",
            source="company_master",
            reason="Fundamental snapshot could not map requested symbols to company master ids.",
            error=exc,
            metadata={"symbol_count": len(tickers)},
        )
        raise
    universe = pd.DataFrame(
        {
            "symbol": pd.Series(tickers, dtype="string"),
            "company_master_id": company_master_ids.astype("string"),
        }
    )
    return universe.dropna(subset=["company_master_id"]).drop_duplicates(
        subset=["company_master_id"],
        keep="last",
    )


def load_latest_peer_memberships(anchor_symbols: list[str]) -> pd.DataFrame:
    if not anchor_symbols:
        return pd.DataFrame(columns=["anchor_symbol", "peer_symbol"])
    df = _source_sql_to_df(
        """
        SELECT p.anchor_symbol, p.peer_symbol
        FROM sharpely_stock_peers p
        JOIN (
            SELECT anchor_symbol, MAX(as_on_date) AS max_as_on_date
            FROM sharpely_stock_peers
            WHERE anchor_symbol = ANY(%s)
            GROUP BY anchor_symbol
        ) latest
          ON latest.anchor_symbol = p.anchor_symbol
         AND latest.max_as_on_date = p.as_on_date
        WHERE p.anchor_symbol = ANY(%s)
          AND COALESCE(p.is_self_peer, FALSE) = FALSE
          AND COALESCE(p.nse_active, 1) = 1
          AND COALESCE(p.is_exclusion_list, 0) = 0
        ORDER BY p.anchor_symbol, p.peer_rank, p.peer_symbol
        """,
        params=(anchor_symbols, anchor_symbols),
        fallback_type="fundamental_snapshot_peer_membership_load_failed",
        source="sharpely_stock_peers",
        reason="Fundamental snapshot could not load latest peer membership.",
        metadata={"symbol_count": len(anchor_symbols)},
    )
    if df.empty:
        return df
    for col in ["anchor_symbol", "peer_symbol"]:
        df[col] = df[col].astype("string").str.strip().str.upper()
    return df.drop_duplicates(subset=["anchor_symbol", "peer_symbol"], keep="last")


def expand_symbols_with_peers(symbols: list[str] | None) -> tuple[list[str] | None, list[str]]:
    anchor_symbols = load_tracked_symbols(symbols)
    if not anchor_symbols:
        return symbols, []
    memberships = load_latest_peer_memberships(anchor_symbols)
    if memberships.empty:
        return anchor_symbols, anchor_symbols
    expanded = sorted(
        {
            *anchor_symbols,
            *memberships["peer_symbol"].dropna().astype(str).tolist(),
        }
    )
    return expanded, anchor_symbols


def load_target_dates(
    universe: pd.DataFrame,
    *,
    from_date: pd.Timestamp | None = None,
    to_date: pd.Timestamp | None = None,
    rebuild: bool = False,
) -> pd.DataFrame:
    if universe.empty:
        return pd.DataFrame()

    effective_to_date = to_date or pd.Timestamp.now(tz="UTC").normalize()
    if rebuild:
        lower_bound = from_date
    else:
        try:
            max_date = get_max_date(TABLE_NAME, "asof_date")
        except Exception as exc:
            _record_fundamental_snapshot_fallback(
                fallback_type="fundamental_snapshot_max_date_lookup_failed",
                source=TABLE_NAME,
                reason="Fundamental snapshot could not read the latest persisted as-of date.",
                error=exc,
                metadata={"rebuild": bool(rebuild)},
            )
            raise
        lower_bound = from_date or (
            max_date + pd.Timedelta(days=1) if max_date is not None else None
        )

    clauses = []
    params: list[object] = []
    if lower_bound is not None:
        clauses.append("date >= %s")
        params.append(lower_bound)
    clauses.append("date <= %s")
    params.append(effective_to_date)

    dates = _source_sql_to_df(
        f"""
        SELECT date
        FROM dim_trading_days
        WHERE {' AND '.join(clauses)}
        ORDER BY date
        """,
        params=tuple(params),
        fallback_type="fundamental_snapshot_trading_days_load_failed",
        source="dim_trading_days",
        reason="Fundamental snapshot could not load target trading days.",
        metadata={"from_date": str(lower_bound) if lower_bound is not None else None, "to_date": str(effective_to_date)},
    )
    if dates.empty:
        return dates
    dates["asof_date"] = normalize_timestamp(dates["date"])
    dates = dates[["asof_date"]].drop_duplicates().sort_values("asof_date")

    universe_frame = universe[["company_master_id", "symbol"]].drop_duplicates().copy()
    universe_frame["_join_key"] = 1
    dates["_join_key"] = 1
    return (
        universe_frame.merge(dates, on="_join_key", how="inner")
        .drop(columns="_join_key")
        .sort_values(["company_master_id", "asof_date"])
        .reset_index(drop=True)
    )


def load_release_calendar(universe: pd.DataFrame) -> pd.DataFrame:
    if universe.empty:
        return pd.DataFrame(columns=["company_master_id", "statement_date", "release_date"])
    company_ids = universe["company_master_id"].dropna().astype(str).unique().tolist()
    events = _source_sql_to_df(
        """
        SELECT
            company_master_id,
            date AS statement_date,
            reporting_date,
            consolidated
        FROM nseindia_earnings_events
        WHERE company_master_id = ANY(%s)
        ORDER BY company_master_id, date, reporting_date
        """,
        params=(company_ids,),
        fallback_type="fundamental_snapshot_release_calendar_load_failed",
        source="nseindia_earnings_events",
        reason="Fundamental snapshot could not load earnings release calendar.",
        metadata={"company_count": len(company_ids)},
    )
    if events.empty:
        return pd.DataFrame(columns=["company_master_id", "statement_date", "release_date"])

    events["statement_date"] = normalize_timestamp(events["statement_date"])
    events["reporting_date"] = pd.to_datetime(events["reporting_date"], utc=True, errors="coerce")
    events["consolidated_rank"] = np.where(
        events["consolidated"].astype("string").str.contains("Consolidated", case=False, na=False),
        0,
        1,
    )
    events = events.sort_values(
        ["company_master_id", "statement_date", "consolidated_rank", "reporting_date"]
    )
    events = events.drop_duplicates(
        subset=["company_master_id", "statement_date"],
        keep="first",
    )
    return events[["company_master_id", "statement_date", "reporting_date"]].rename(
        columns={"reporting_date": "release_date"}
    )


def load_statement_snapshot(universe: pd.DataFrame) -> pd.DataFrame:
    if universe.empty:
        return pd.DataFrame()
    company_ids = universe["company_master_id"].dropna().astype(str).unique().tolist()
    income = _source_sql_to_df(
        """
        SELECT
            company_master_id,
            symbol,
            date,
            period_length,
            total_revenue,
            ebitda,
            profit_after_tax,
            operating_profit,
            eps_diluted
        FROM stmt_income
        WHERE company_master_id = ANY(%s)
          AND period_length = '3 Months'
        ORDER BY company_master_id, date
        """,
        params=(company_ids,),
        fallback_type="fundamental_snapshot_income_load_failed",
        source="stmt_income",
        reason="Fundamental snapshot could not load quarterly income statements.",
        metadata={"company_count": len(company_ids)},
    )
    if income.empty:
        return income
    balance = _source_sql_to_df(
        """
        SELECT
            company_master_id,
            date,
            total_shareholders_equity,
            long_term_debt,
            short_term_debt_and_cpltd,
            net_debt
        FROM stmt_balancesheet
        WHERE company_master_id = ANY(%s)
        ORDER BY company_master_id, date
        """,
        params=(company_ids,),
        fallback_type="fundamental_snapshot_balance_load_failed",
        source="stmt_balancesheet",
        reason="Fundamental snapshot could not load balance sheet context.",
        metadata={"company_count": len(company_ids)},
    )
    cashflow = _source_sql_to_df(
        """
        SELECT
            company_master_id,
            date,
            net_cash_from_operating_activities,
            capital_expenditures_net,
            free_cash_flow_to_equity
        FROM stmt_cashflow
        WHERE company_master_id = ANY(%s)
        ORDER BY company_master_id, date
        """,
        params=(company_ids,),
        fallback_type="fundamental_snapshot_cashflow_load_failed",
        source="stmt_cashflow",
        reason="Fundamental snapshot could not load cashflow context.",
        metadata={"company_count": len(company_ids)},
    )

    for frame in [income, balance, cashflow]:
        if not frame.empty:
            frame["date"] = normalize_timestamp(frame["date"])

    merged = income.merge(
        balance.drop_duplicates(subset=["company_master_id", "date"], keep="last"),
        on=["company_master_id", "date"],
        how="left",
    )
    merged = merged.merge(
        cashflow.drop_duplicates(subset=["company_master_id", "date"], keep="last"),
        on=["company_master_id", "date"],
        how="left",
    )

    releases = load_release_calendar(universe)
    merged = merged.merge(
        releases,
        left_on=["company_master_id", "date"],
        right_on=["company_master_id", "statement_date"],
        how="left",
    )
    merged["release_date"] = pd.to_datetime(merged["release_date"], utc=True, errors="coerce")
    fallback_release = merged["date"] + pd.to_timedelta(DEFAULT_STATEMENT_LAG_DAYS, unit="D")
    merged["release_date"] = merged["release_date"].fillna(fallback_release)
    merged["release_date"] = merged["release_date"].dt.normalize()
    merged = merged.drop(columns=["statement_date"], errors="ignore")

    merged = merged.sort_values(["company_master_id", "date"]).reset_index(drop=True)
    for col in ["total_revenue", "ebitda", "profit_after_tax"]:
        prev = merged.groupby("company_master_id")[col].shift(1)
        merged[f"{col}_qoq_growth"] = np.where(
            prev.replace(0, np.nan).notna(),
            (merged[col] - prev) / prev.abs().replace(0, np.nan),
            np.nan,
        )
    debt_base = merged["total_shareholders_equity"].replace(0, np.nan).abs()
    merged["gross_debt"] = (
        merged["long_term_debt"].fillna(0) + merged["short_term_debt_and_cpltd"].fillna(0)
    )
    merged["debt_to_equity"] = merged["gross_debt"] / debt_base
    merged["net_debt_to_equity"] = merged["net_debt"] / debt_base
    return merged


def load_shareholding_snapshot(universe: pd.DataFrame) -> pd.DataFrame:
    if universe.empty:
        return pd.DataFrame()
    company_ids = universe["company_master_id"].dropna().astype(str).unique().tolist()
    shareholding = _source_sql_to_df(
        """
        SELECT
            company_master_id,
            symbol,
            date,
            sh_code,
            sh_per
        FROM shareholding_category
        WHERE company_master_id = ANY(%s)
          AND sh_code IN ('promoter_total', 'fii', 'institutions_tot', 'non_promoter_tot')
        ORDER BY company_master_id, date
        """,
        params=(company_ids,),
        fallback_type="fundamental_snapshot_shareholding_load_failed",
        source="shareholding_category",
        reason="Fundamental snapshot could not load shareholding context.",
        metadata={"company_count": len(company_ids)},
    )
    if shareholding.empty:
        return shareholding
    shareholding["date"] = normalize_timestamp(shareholding["date"])
    wide = (
        shareholding.pivot_table(
            index=["company_master_id", "symbol", "date"],
            columns="sh_code",
            values="sh_per",
            aggfunc="last",
        )
        .reset_index()
        .rename_axis(columns=None)
    )
    wide["release_date"] = wide["date"] + pd.to_timedelta(DEFAULT_SHAREHOLDING_LAG_DAYS, unit="D")
    wide = wide.sort_values(["company_master_id", "date"]).reset_index(drop=True)
    for col in ["promoter_total", "fii", "institutions_tot", "non_promoter_tot"]:
        if col in wide.columns:
            wide[f"{col}_qoq_change"] = wide.groupby("company_master_id")[col].diff()
    return wide


def merge_company_asof(
    target: pd.DataFrame,
    source: pd.DataFrame,
    *,
    source_prefix: str,
) -> pd.DataFrame:
    if source.empty:
        return target
    merged_frames: list[pd.DataFrame] = []
    for company_master_id, group in target.groupby("company_master_id", dropna=False):
        source_group = source[source["company_master_id"] == company_master_id]
        group = group.sort_values("asof_date")
        if source_group.empty:
            merged_frames.append(group)
            continue
        merged = pd.merge_asof(
            group,
            source_group.sort_values("release_date"),
            left_on="asof_date",
            right_on="release_date",
            direction="backward",
            allow_exact_matches=True,
        )
        if "symbol_x" in merged.columns:
            merged["symbol"] = merged["symbol_x"]
            merged = merged.drop(columns=["symbol_x"], errors="ignore")
        if "symbol_y" in merged.columns:
            merged = merged.drop(columns=["symbol_y"], errors="ignore")
        if "company_master_id_x" in merged.columns:
            merged["company_master_id"] = merged["company_master_id_x"]
            merged = merged.drop(columns=["company_master_id_x"], errors="ignore")
        if "company_master_id_y" in merged.columns:
            merged = merged.drop(columns=["company_master_id_y"], errors="ignore")
        if "release_date" in merged.columns:
            merged = merged.rename(columns={"release_date": f"{source_prefix}_release_date"})
        merged_frames.append(merged)
    return pd.concat(merged_frames, ignore_index=True)


def attach_peer_relative_features(
    base: pd.DataFrame,
    *,
    anchor_symbols: list[str] | None = None,
) -> pd.DataFrame:
    if base.empty or "symbol" not in base.columns:
        return base

    symbols = (
        sorted(base["symbol"].dropna().astype("string").str.upper().unique().tolist())
        if not anchor_symbols
        else sorted({str(symbol).upper() for symbol in anchor_symbols})
    )
    memberships = load_latest_peer_memberships(symbols)
    if memberships.empty:
        base["sector_peer_fundamental_count"] = np.nan
        return base

    peer_columns = [
        "total_revenue_qoq_growth",
        "ebitda_qoq_growth",
        "profit_after_tax_qoq_growth",
        "debt_to_equity",
        "net_debt_to_equity",
        "promoter_total",
        "fii",
        "institutions_tot",
    ]
    available_peer_columns = [col for col in peer_columns if col in base.columns]
    if not available_peer_columns:
        base["sector_peer_fundamental_count"] = np.nan
        return base

    peer_frame = base[["asof_date", "symbol", *available_peer_columns]].copy()
    peer_frame["symbol"] = peer_frame["symbol"].astype("string").str.upper()
    peer_frame = peer_frame.rename(columns={"symbol": "peer_symbol"})
    peer_frame["peer_statement_present"] = peer_frame.get("total_revenue_qoq_growth").notna()

    peer_agg = (
        memberships.merge(peer_frame, on="peer_symbol", how="inner")
        .groupby(["anchor_symbol", "asof_date"], dropna=False)
        .agg(
            sector_peer_fundamental_count=("peer_statement_present", "sum"),
            **{
                f"peer_{column}_median": (column, "median")
                for column in available_peer_columns
            },
        )
        .reset_index()
    )
    out = base.merge(
        peer_agg,
        left_on=["symbol", "asof_date"],
        right_on=["anchor_symbol", "asof_date"],
        how="left",
    ).drop(columns=["anchor_symbol"], errors="ignore")

    for column in available_peer_columns:
        out[f"{column}_vs_sector"] = np.where(
            out["sector_peer_fundamental_count"] >= MIN_PEER_FUNDAMENTAL_COUNT,
            out[column] - out[f"peer_{column}_median"],
            np.nan,
        )
    return out


def build_fundamental_snapshot(
    *,
    symbols: list[str] | None = None,
    from_date: pd.Timestamp | None = None,
    to_date: pd.Timestamp | None = None,
    rebuild: bool = False,
) -> pd.DataFrame:
    universe = resolve_universe(symbols)
    if universe.empty:
        return pd.DataFrame()

    target = load_target_dates(
        universe,
        from_date=from_date,
        to_date=to_date,
        rebuild=rebuild,
    )
    if target.empty:
        return target

    statements = load_statement_snapshot(universe)
    shareholding = load_shareholding_snapshot(universe)

    out = merge_company_asof(target, statements, source_prefix="statement")
    out = merge_company_asof(out, shareholding, source_prefix="shareholding")
    out = attach_peer_relative_features(out, anchor_symbols=symbols)

    if "statement_release_date" in out.columns:
        out["statement_age_days"] = (
            out["asof_date"] - out["statement_release_date"]
        ).dt.days.where(out["statement_release_date"].notna())
    else:
        out["statement_age_days"] = pd.NA
    if "shareholding_release_date" in out.columns:
        out["shareholding_age_days"] = (
            out["asof_date"] - out["shareholding_release_date"]
        ).dt.days.where(out["shareholding_release_date"].notna())
    else:
        out["shareholding_age_days"] = pd.NA

    out["fundamentals_freshness_status"] = np.where(
        out["statement_age_days"].fillna(9999) <= 120,
        "FRESH",
        np.where(out["statement_age_days"].notna(), "STALE", "MISSING"),
    )
    out["load_ts"] = pd.Timestamp.utcnow()

    duplicate_safe = out.drop_duplicates(
        subset=["asof_date", "company_master_id"],
        keep="last",
    )
    return duplicate_safe.reset_index(drop=True)


def persist_fundamental_snapshot(df: pd.DataFrame) -> None:
    if df.empty:
        return
    upsert_to_db(
        df,
        TABLE_NAME,
        unique_keys=["asof_date", "company_master_id"],
        timescaledb_column="asof_date",
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build a daily point-in-time advisory fundamentals snapshot."
    )
    parser.add_argument("--symbols", nargs="*", help="Symbols, comma-separated or repeated")
    parser.add_argument("--from-date", type=parse_datetime_arg)
    parser.add_argument("--to-date", type=parse_datetime_arg)
    parser.add_argument("--rebuild", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--skip-peer-sync",
        action="store_true",
        help="Do not refresh Sharpely peer snapshots before building peer-relative fundamentals.",
    )
    return parser.parse_args()


def summarize(df: pd.DataFrame) -> dict[str, object]:
    if df.empty:
        return {
            "status": "ok",
            "table": TABLE_NAME,
            "row_count": 0,
            "company_count": 0,
            "date_min": None,
            "date_max": None,
            "sample": [],
        }
    preview_cols = [
        "asof_date",
        "company_master_id",
        "symbol",
        "total_revenue",
        "total_revenue_qoq_growth",
        "ebitda",
        "profit_after_tax",
        "debt_to_equity",
        "sector_peer_fundamental_count",
        "total_revenue_qoq_growth_vs_sector",
        "profit_after_tax_qoq_growth_vs_sector",
        "debt_to_equity_vs_sector",
        "promoter_total",
        "fii",
        "fundamentals_freshness_status",
    ]
    preview_cols = [col for col in preview_cols if col in df.columns]
    return {
        "status": "ok",
        "table": TABLE_NAME,
        "row_count": int(len(df)),
        "company_count": int(df["company_master_id"].nunique()),
        "date_min": df["asof_date"].min().date().isoformat(),
        "date_max": df["asof_date"].max().date().isoformat(),
        "sample": df[preview_cols].head(5).to_dict(orient="records"),
    }


def main() -> int:
    args = parse_args()
    requested_symbols = load_tracked_symbols(args.symbols)
    peer_sync_result: dict[str, object] | None = None
    data_sync_result: dict[str, object] | None = None
    if requested_symbols and not args.skip_peer_sync and not args.dry_run:
        data_sync_result = ensure_advisory_symbol_inputs(
            requested_symbols,
            to_date=args.to_date,
        )
        peer_sync_result = sync_peer_data(
            symbols=requested_symbols,
            to_date=args.to_date,
        )
    expanded_symbols, anchor_symbols = expand_symbols_with_peers(args.symbols)
    df = build_fundamental_snapshot(
        symbols=expanded_symbols,
        from_date=pd.Timestamp(args.from_date, tz="UTC") if args.from_date else None,
        to_date=pd.Timestamp(args.to_date, tz="UTC") if args.to_date else None,
        rebuild=args.rebuild,
    )
    if anchor_symbols and not df.empty and "symbol" in df.columns:
        anchor_set = {symbol.upper() for symbol in anchor_symbols}
        df = df[df["symbol"].astype("string").str.upper().isin(anchor_set)].reset_index(drop=True)
    if not args.dry_run:
        persist_fundamental_snapshot(df)
    result = summarize(df)
    result["dry_run"] = bool(args.dry_run)
    result["data_sync"] = data_sync_result
    result["peer_sync"] = peer_sync_result
    print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
