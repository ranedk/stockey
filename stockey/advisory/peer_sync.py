from __future__ import annotations

import argparse
import json
from datetime import datetime

import pandas as pd

from advisory.fallback_telemetry import record_local_fallback_event
from data.dhanlive.client import DhanAPIError
from data.dhanlive.ohlcv import sync_daily_ohlcv
from data.sharpelydata.sharpely_data import sync_sharpely_data
from utils.sync import get_db_max_date, load_tracked_symbols, normalize_date_window, parse_datetime_arg
from utils.db import sql_to_df


def _record_peer_sync_fallback(
    *,
    fallback_type: str,
    reason: str,
    error: Exception | str,
    symbol: str | None = None,
    severity: str = "warn",
    metadata: dict[str, object] | None = None,
) -> None:
    record_local_fallback_event(
        module="advisory.peer_sync",
        source="dhan_ohlcv_daily",
        fallback_type=fallback_type,
        severity=severity,
        reason=reason,
        error=error,
        symbol=symbol,
        metadata=metadata or {},
    )


def load_latest_peer_symbols(anchor_symbols: list[str]) -> dict[str, list[str]]:
    if not anchor_symbols:
        return {}
    df = sql_to_df(
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
    )
    if df.empty:
        return {symbol: [] for symbol in anchor_symbols}
    grouped: dict[str, list[str]] = {symbol: [] for symbol in anchor_symbols}
    for anchor_symbol, group in df.groupby("anchor_symbol", dropna=False):
        grouped[str(anchor_symbol)] = (
            group["peer_symbol"].astype("string").dropna().str.upper().drop_duplicates().tolist()
        )
    return grouped


def ensure_peer_membership(anchor_symbols: list[str], to_date: datetime) -> None:
    # sync_sharpely_data refreshes meta and peers only when the saved snapshot is older than target date
    sync_sharpely_data(anchor_symbols, from_date=None, to_date=to_date)


def sync_peer_ohlcv(peer_symbols: list[str], to_date: datetime) -> list[dict[str, object]]:
    results: list[dict[str, object]] = []
    target_ts = pd.Timestamp(to_date)
    if target_ts.tzinfo is not None:
        target_ts = target_ts.tz_convert("UTC").tz_localize(None)
    target_day = target_ts.normalize().to_pydatetime()
    for symbol in peer_symbols:
        latest = get_db_max_date(
            "dhan_ohlcv_daily",
            filters={"ticker": symbol, "asset_type": "stock", "exchange": "NSE"},
        )
        from_date = None
        if latest is not None:
            from_date = pd.Timestamp(latest).normalize().to_pydatetime()
            from_date = from_date + pd.Timedelta(days=1)
        if from_date is not None and from_date > target_day:
            results.append(
                {
                    "symbol": symbol,
                    "action": "skip",
                    "reason": "ohlcv_up_to_date",
                }
            )
            continue
        try:
            df = sync_daily_ohlcv(
                symbol,
                exchange="NSE",
                asset_type="stock",
                from_date=from_date,
                to_date=target_day,
            )
            results.append(
                {
                    "symbol": symbol,
                    "action": "sync",
                    "rows": int(len(df)),
                    "from_date": None if df.empty else str(df["date"].min()),
                    "to_date": None if df.empty else str(df["date"].max()),
                }
            )
        except DhanAPIError as exc:
            error_text = str(exc).lower()
            if "no data present" in error_text:
                _record_peer_sync_fallback(
                    fallback_type="peer_ohlcv_no_new_data",
                    reason="Peer OHLCV sync found no new Dhan data for one peer symbol.",
                    error=exc,
                    symbol=symbol,
                    severity="info",
                    metadata={
                        "target_day": str(target_day.date()),
                        "from_date": None if from_date is None else str(pd.Timestamp(from_date).date()),
                    },
                )
                results.append(
                    {
                        "symbol": symbol,
                        "action": "skip",
                        "reason": "ohlcv_no_new_data",
                    }
                )
                continue
            _record_peer_sync_fallback(
                fallback_type="peer_ohlcv_sync_failed",
                reason="Peer OHLCV sync failed for one peer symbol, leaving peer/relative-strength context partial.",
                error=exc,
                symbol=symbol,
                metadata={
                    "target_day": str(target_day.date()),
                    "from_date": None if from_date is None else str(pd.Timestamp(from_date).date()),
                },
            )
            results.append(
                {
                    "symbol": symbol,
                    "action": "error",
                    "stage": "ohlcv",
                    "error": str(exc),
                }
            )
        except Exception as exc:
            _record_peer_sync_fallback(
                fallback_type="peer_ohlcv_sync_failed",
                reason="Peer OHLCV sync failed for one peer symbol, leaving peer/relative-strength context partial.",
                error=exc,
                symbol=symbol,
                metadata={
                    "target_day": str(target_day.date()),
                    "from_date": None if from_date is None else str(pd.Timestamp(from_date).date()),
                },
            )
            results.append(
                {
                    "symbol": symbol,
                    "action": "error",
                    "stage": "ohlcv",
                    "error": str(exc),
                }
            )
    return results


def sync_peer_data(
    *,
    symbols: list[str] | None,
    from_date: datetime | None = None,
    to_date: datetime | None = None,
) -> dict[str, object]:
    anchor_symbols = load_tracked_symbols(symbols)
    if not anchor_symbols:
        return {
            "status": "ok",
            "anchor_symbols": [],
            "peer_symbols": [],
            "peer_count": 0,
            "ohlcv_results": [],
        }

    _, effective_to_date = normalize_date_window(from_date, to_date)
    ensure_peer_membership(anchor_symbols, effective_to_date)
    peer_map = load_latest_peer_symbols(anchor_symbols)
    peer_symbols = sorted(
        {
            peer
            for peers in peer_map.values()
            for peer in peers
        }
    )
    fundamentals_results: list[dict[str, object]] = []
    if peer_symbols:
        for peer_symbol in peer_symbols:
            try:
                sync_sharpely_data([peer_symbol], from_date=from_date, to_date=effective_to_date)
                fundamentals_results.append(
                    {
                        "symbol": peer_symbol,
                        "action": "sync",
                        "stage": "sharpely",
                    }
                )
            except Exception as exc:
                _record_peer_sync_fallback(
                    fallback_type="peer_fundamentals_sync_failed",
                    reason="Peer fundamentals sync failed for one peer symbol, leaving peer context partial.",
                    error=exc,
                    symbol=peer_symbol,
                    metadata={
                        "effective_to_date": str(pd.Timestamp(effective_to_date).date()),
                        "from_date": None if from_date is None else str(pd.Timestamp(from_date).date()),
                    },
                )
                fundamentals_results.append(
                    {
                        "symbol": peer_symbol,
                        "action": "error",
                        "stage": "sharpely",
                        "error": str(exc),
                    }
                )
    ohlcv_results = sync_peer_ohlcv(peer_symbols, effective_to_date)
    return {
        "status": "ok",
        "anchor_symbols": anchor_symbols,
        "peer_map": peer_map,
        "peer_symbols": peer_symbols,
        "peer_count": len(peer_symbols),
        "fundamentals_results": fundamentals_results,
        "ohlcv_results": ohlcv_results,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Incrementally sync peer membership, peer fundamentals, and peer OHLCV."
    )
    parser.add_argument("--symbols", nargs="*", help="Anchor symbols, comma-separated or repeated")
    parser.add_argument("--from-date", dest="from_date", help="Start date in YYYY-MM-DD")
    parser.add_argument("--to-date", dest="to_date", help="End date in YYYY-MM-DD")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    result = sync_peer_data(
        symbols=args.symbols,
        from_date=parse_datetime_arg(args.from_date),
        to_date=parse_datetime_arg(args.to_date),
    )
    print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
