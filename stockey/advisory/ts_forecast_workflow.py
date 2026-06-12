from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
from typing import Any

import pandas as pd
import yaml

from advisory.ts_forecast_features import (
    DEFAULT_HORIZONS,
    DEFAULT_LOOKBACK_DAYS,
    TIMESFM_MODEL_NAME,
    build_ts_forecasts,
    persist_ts_forecasts,
    resolve_symbol_universe,
)
from advisory.fallback_telemetry import record_local_fallback_event
from data.dhanlive.ohlcv import sync_many_daily
from data.screenerin.ad_hoc_query import build_raw_screen_url, fetch_ad_hoc_payload
from utils.db import upsert_to_db
from utils.schema_migrations import apply_schema_migration
from utils.sync import parse_datetime_arg


WATCHLIST_TABLE = "advisory_ts_forecast_watchlist"
TS_FORECAST_WORKFLOW_SCHEMA_MIGRATION_ID = "20260611_advisory_ts_forecast_workflow_base"
TS_FORECAST_WORKFLOW_SCHEMA_STATEMENTS = [
    f"""
    CREATE TABLE IF NOT EXISTS {WATCHLIST_TABLE} (
        asof_date TIMESTAMPTZ NOT NULL,
        symbol TEXT NOT NULL,
        source_name TEXT,
        source_slug TEXT,
        model_name TEXT NOT NULL,
        forecast_horizon_days BIGINT NOT NULL,
        forecast_return DOUBLE PRECISION,
        probability_positive DOUBLE PRECISION,
        signal_quality DOUBLE PRECISION,
        action_hint TEXT,
        watch_status TEXT,
        watch_reason TEXT,
        raw_context_json TEXT,
        load_ts TIMESTAMPTZ,
        UNIQUE (asof_date, symbol, model_name, forecast_horizon_days)
    )
    """,
]
DEFAULT_CONFIG_PATH = Path("config/ts_forecast_screeners.yaml")
DEFAULT_MAX_SYMBOLS = int(os.getenv("TS_FORECAST_MAX_SYMBOLS", "80"))


def _normalize_asof_date(value: Any | None) -> pd.Timestamp:
    ts = pd.to_datetime(value or pd.Timestamp.utcnow(), utc=True, errors="coerce")
    if pd.isna(ts):
        raise ValueError(f"Invalid asof date: {value}")
    return ts.normalize()


def _as_symbol_list(values: list[str] | None) -> list[str]:
    out: list[str] = []
    for value in values or []:
        for part in str(value).split(","):
            symbol = part.strip().upper()
            if symbol:
                out.append(symbol)
    return sorted(set(out))


def _cap_symbols(symbols: list[str], *, max_symbols: int | None) -> list[str]:
    unique = sorted({str(symbol).strip().upper() for symbol in symbols if str(symbol).strip()})
    if max_symbols is None or int(max_symbols) <= 0:
        return unique
    return unique[: int(max_symbols)]


def _record_ts_workflow_fallback(
    *,
    source: str,
    fallback_type: str,
    reason: str,
    error: Exception | str,
    metadata: dict[str, Any] | None = None,
) -> None:
    record_local_fallback_event(
        module="advisory.ts_forecast_workflow",
        source=source,
        fallback_type=fallback_type,
        severity="warn",
        reason=reason,
        error=error,
        metadata=metadata or {},
    )


def ensure_watchlist_table() -> None:
    apply_schema_migration(
        migration_id=TS_FORECAST_WORKFLOW_SCHEMA_MIGRATION_ID,
        description="Create advisory TS forecast workflow watchlist table.",
        statements=TS_FORECAST_WORKFLOW_SCHEMA_STATEMENTS,
        metadata={"tables": [WATCHLIST_TABLE], "workflow": "ts_forecast_watchlist"},
    )


def load_default_screener(config_path: Path = DEFAULT_CONFIG_PATH) -> dict[str, str]:
    payload = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    screeners = payload.get("screeners") or []
    if not screeners:
        raise ValueError(f"No TS forecast screeners configured in {config_path}")
    first = screeners[0]
    return {
        "screener_slug": str(first.get("screener_slug") or "ts-ad-hoc-watch"),
        "query_name": str(first.get("query_name") or "TS Forecast Watch"),
        "query_text": str(first.get("query_text") or "").strip(),
    }


def symbols_from_screener(*, query_text: str, query_name: str) -> tuple[list[str], dict[str, Any]]:
    payload = fetch_ad_hoc_payload(query_text=query_text, query_name=query_name, persist=True)
    companies = payload.get("companies") or []
    symbols = sorted(
        {
            str(company.get("ticker") or "").strip().upper()
            for company in companies
            if str(company.get("ticker") or "").strip()
        }
    )
    return symbols, {
        "query_run_id": payload.get("query_run_id"),
        "query_name": payload.get("query_name"),
        "query_slug": payload.get("query_slug"),
        "row_count": len(companies),
    }


def build_ts_watchlist(
    forecasts: pd.DataFrame,
    *,
    asof_date: pd.Timestamp,
    source_name: str,
    source_slug: str,
    min_probability_positive: float = 0.58,
    min_signal_quality: float = 0.35,
) -> pd.DataFrame:
    if forecasts.empty:
        return pd.DataFrame()
    df = forecasts.copy()
    for col in ["forecast_return", "probability_positive", "signal_quality", "forecast_horizon_days"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    mask = (
        df["forecast_return"].gt(0)
        & df["probability_positive"].ge(float(min_probability_positive))
        & df["signal_quality"].ge(float(min_signal_quality))
        & df["action_hint"].astype("string").isin(["EXPERIMENTAL_POSITIVE"])
    )
    selected = df[mask].copy()
    if selected.empty:
        return pd.DataFrame()
    rows = []
    for _, row in selected.iterrows():
        rows.append(
            {
                "asof_date": asof_date,
                "symbol": str(row["symbol"]).upper(),
                "source_name": source_name,
                "source_slug": source_slug,
                "model_name": str(row["model_name"]),
                "forecast_horizon_days": int(row["forecast_horizon_days"]),
                "forecast_return": float(row["forecast_return"]),
                "probability_positive": float(row["probability_positive"]),
                "signal_quality": float(row["signal_quality"]),
                "action_hint": row.get("action_hint"),
                "watch_status": "TS_WATCH",
                "watch_reason": (
                    f"Experimental TS forecast positive: return={float(row['forecast_return']):.2%}, "
                    f"prob={float(row['probability_positive']):.2f}, quality={float(row['signal_quality']):.2f}."
                ),
                "raw_context_json": json.dumps(row.to_dict(), ensure_ascii=False, default=str, sort_keys=True),
                "load_ts": pd.Timestamp.utcnow(),
            }
        )
    return pd.DataFrame(rows)


def persist_ts_watchlist(df: pd.DataFrame) -> None:
    ensure_watchlist_table()
    if df.empty:
        return
    upsert_to_db(
        df,
        WATCHLIST_TABLE,
        unique_keys=["asof_date", "symbol", "model_name", "forecast_horizon_days"],
        timescaledb_column="asof_date",
    )


def run_workflow(
    *,
    symbols: list[str] | None = None,
    query_text: str | None = None,
    query_name: str | None = None,
    config_path: Path = DEFAULT_CONFIG_PATH,
    asof_date: pd.Timestamp | None = None,
    horizons: tuple[int, ...] = DEFAULT_HORIZONS,
    model_name: str = TIMESFM_MODEL_NAME,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
    max_symbols: int | None = DEFAULT_MAX_SYMBOLS,
    refresh_ohlcv: bool = True,
    dry_run: bool = False,
) -> dict[str, Any]:
    monitor_date = _normalize_asof_date(asof_date)
    source_name = "symbols"
    source_slug = "manual"
    screener_meta: dict[str, Any] | None = None
    warnings: list[str] = []
    symbol_universe = _as_symbol_list(symbols)
    if not symbol_universe:
        if not query_text:
            cfg = load_default_screener(config_path)
            query_text = cfg["query_text"]
            query_name = query_name or cfg["query_name"]
            source_slug = cfg["screener_slug"]
        source_name = "screenerin_ad_hoc"
        try:
            screener_symbols, screener_meta = symbols_from_screener(query_text=query_text or "", query_name=query_name or "TS Forecast Watch")
        except Exception as exc:
            planned_query_name = query_name or "TS Forecast Watch"
            planned_url = build_raw_screen_url(query_text or "")
            _record_ts_workflow_fallback(
                source="screenerin_ad_hoc",
                fallback_type="ts_forecast_workflow_screener_failed",
                reason="TS forecast workflow could not load Screener.in ad hoc symbols and fell back to the tracked Dhan/OHLCV symbol universe.",
                error=exc,
                metadata={
                    "query_name": planned_query_name,
                    "source_slug": source_slug,
                    "screener_url": planned_url,
                    "query_text_chars": len(query_text or ""),
                },
            )
            warning = (
                f"screener_failed:{type(exc).__name__}:{exc}; "
                f"query_name={planned_query_name!r}; source_slug={source_slug!r}; screener_url={planned_url!r}"
            )
            warnings.append(warning)
            print(f"[advisory.ts_forecast_workflow] {warning}; falling back to Dhan/tracked symbol universe", file=sys.stderr, flush=True)
            screener_symbols = []
            screener_meta = {
                "query_name": planned_query_name,
                "query_slug": source_slug,
                "screener_url": planned_url,
                "query_text": query_text or "",
                "row_count": 0,
                "error": warning,
            }
        symbol_universe = screener_symbols
        if screener_meta and screener_meta.get("query_slug"):
            source_slug = str(screener_meta["query_slug"])
    if not symbol_universe and not symbols:
        source_name = "dhan_ohlcv_fallback"
        source_slug = "dhan-ohlcv-fallback"
        symbol_universe = resolve_symbol_universe(None)
    symbol_universe = _cap_symbols(symbol_universe, max_symbols=max_symbols)
    if not symbol_universe:
        return {"status": "ok", "symbol_count": 0, "forecast_rows": 0, "watch_rows": 0, "screener": screener_meta, "warnings": warnings}

    refresh_result = None
    if refresh_ohlcv and not dry_run:
        refresh_result = sync_many_daily(
            symbol_universe,
            exchange="NSE",
            asset_type="stock",
            to_date=monitor_date.to_pydatetime(),
        )

    forecasts = build_ts_forecasts(
        symbols=resolve_symbol_universe(symbol_universe),
        asof_date=monitor_date,
        horizons=horizons,
        model_name=model_name,
        lookback_days=lookback_days,
    )
    watchlist = build_ts_watchlist(
        forecasts,
        asof_date=monitor_date,
        source_name=source_name,
        source_slug=source_slug,
    )
    if not dry_run:
        persist_ts_forecasts(forecasts)
        persist_ts_watchlist(watchlist)
    return {
        "status": "ok",
        "asof_date": monitor_date.isoformat(),
        "model_name": model_name,
        "symbol_count": len(symbol_universe),
        "forecast_rows": len(forecasts),
        "watch_rows": len(watchlist),
        "screener": screener_meta,
        "refresh_ohlcv": refresh_result,
        "watch_sample": watchlist.head(20).to_dict(orient="records") if not watchlist.empty else [],
        "warnings": warnings,
        "dry_run": dry_run,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the experimental TS forecast workflow: optional screener, Dhan OHLCV refresh, forecast, and TS watchlist.")
    parser.add_argument("--symbols", nargs="*", help="Symbols, comma-separated or repeated")
    parser.add_argument("--query", help="Screener.in ad hoc query text")
    parser.add_argument("--query-name", help="Friendly Screener.in query name")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG_PATH), help="TS screener config path")
    parser.add_argument("--date", type=parse_datetime_arg)
    parser.add_argument("--horizons", nargs="*", type=int, default=list(DEFAULT_HORIZONS))
    parser.add_argument("--model-name", default=TIMESFM_MODEL_NAME)
    parser.add_argument("--lookback-days", type=int, default=DEFAULT_LOOKBACK_DAYS)
    parser.add_argument("--max-symbols", type=int, default=DEFAULT_MAX_SYMBOLS, help="Cap symbols sent through the TS workflow; <=0 means no cap")
    parser.add_argument("--skip-refresh-ohlcv", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    result = run_workflow(
        symbols=args.symbols,
        query_text=args.query,
        query_name=args.query_name,
        config_path=Path(args.config),
        asof_date=pd.Timestamp(args.date, tz="UTC") if args.date else None,
        horizons=tuple(args.horizons),
        model_name=args.model_name,
        lookback_days=int(args.lookback_days),
        max_symbols=int(args.max_symbols),
        refresh_ohlcv=not bool(args.skip_refresh_ohlcv),
        dry_run=bool(args.dry_run),
    )
    print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
