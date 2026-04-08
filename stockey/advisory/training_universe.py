from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

import pandas as pd
import yaml

from advisory.screener_parser import INTEGER_COLUMNS, NUMERIC_COLUMNS, persist_constituents, resolve_company_master
from data.screenerin.ad_hoc_query import fetch_ad_hoc_payload
from utils.sync import parse_datetime_arg


REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_TRAINING_UNIVERSE_CONFIG = REPO_ROOT / "config" / "event_model_training_universes.yaml"


def _emit(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def load_training_universe_config(config_path: str | None = None) -> list[dict[str, Any]]:
    path = Path(config_path) if config_path else DEFAULT_TRAINING_UNIVERSE_CONFIG
    payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    universes = payload.get("universes") or []
    normalized: list[dict[str, Any]] = []
    for item in universes:
        if not isinstance(item, dict) or not item.get("query_text"):
            continue
        name = str(item.get("screener_name") or item.get("query_name") or item.get("screener_slug") or "").strip()
        slug = str(item.get("screener_slug") or item.get("query_slug") or "").strip()
        if not name or not slug:
            continue
        normalized.append(
            {
                "screener_slug": slug,
                "screener_name": name,
                "query_name": str(item.get("query_name") or name),
                "query_text": str(item.get("query_text") or "").strip(),
                "enabled": bool(item.get("enabled", True)),
            }
        )
    return [item for item in normalized if item["enabled"]]


def _normalize_training_payload(
    payload: dict[str, Any],
    *,
    screener_slug: str,
    screener_name: str,
    asof_date: pd.Timestamp,
) -> pd.DataFrame:
    companies = payload.get("companies") or []
    if not companies:
        return pd.DataFrame()
    rows: list[dict[str, Any]] = []
    for index, item in enumerate(companies, start=1):
        metrics = item.get("metrics") if isinstance(item.get("metrics"), dict) else {}
        rows.append(
            {
                "date": asof_date.normalize(),
                "screener_slug": screener_slug,
                "screener_name": screener_name,
                "screener_url": payload.get("screener_url"),
                "scanx_name": pd.NA,
                "scanx_seo_id": pd.NA,
                "scanx_id": pd.NA,
                "scanx_screener_id": pd.NA,
                "source_ticker": str(item.get("ticker") or item.get("page_slug") or "").strip().upper() or pd.NA,
                "display_name": item.get("name"),
                "rank": item.get("s_no") if item.get("s_no") is not None else index,
                "last_price": metrics.get("cmp_rs"),
                "price_change": pd.NA,
                "price_change_pct": pd.NA,
                "volume": metrics.get("volume"),
                "market_cap": metrics.get("mar_cap_rscr"),
                "pe_ratio": metrics.get("p_e"),
                "security_id": pd.NA,
                "instrument": pd.NA,
                "isin": pd.NA,
                "raw_item_json": json.dumps(item, ensure_ascii=False, sort_keys=True),
                "load_ts": pd.Timestamp.utcnow(),
            }
        )
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    df["source_ticker"] = df["source_ticker"].astype("string").str.upper()
    df = resolve_company_master(df)
    for col in NUMERIC_COLUMNS:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    for col in INTEGER_COLUMNS:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce").astype("Int64")
    if "rank" in df.columns:
        df["rank"] = pd.to_numeric(df["rank"], errors="coerce").astype("Int64")
    ordered = [
        "date",
        "screener_slug",
        "screener_name",
        "screener_url",
        "scanx_name",
        "scanx_seo_id",
        "scanx_id",
        "scanx_screener_id",
        "ticker",
        "exchange",
        "company_master_id",
        "security_id",
        "instrument",
        "isin",
        "display_name",
        "rank",
        "last_price",
        "price_change",
        "price_change_pct",
        "volume",
        "market_cap",
        "pe_ratio",
        "raw_item_json",
        "load_ts",
    ]
    for column in ordered:
        if column not in df.columns:
            df[column] = pd.NA
    return df[ordered].drop_duplicates(subset=["date", "screener_slug", "ticker", "exchange"], keep="last")


def sync_training_universes(
    *,
    asof_date: pd.Timestamp | None = None,
    config_path: str | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    effective_date = pd.to_datetime(asof_date or pd.Timestamp.utcnow(), utc=True, errors="coerce").normalize()
    universes = load_training_universe_config(config_path)
    results: list[dict[str, Any]] = []
    frames: list[pd.DataFrame] = []
    total = len(universes)
    for index, item in enumerate(universes, start=1):
        if dry_run:
            _emit(
                "[advisory.training_universe] "
                f"query {index}/{total} planned slug={item['screener_slug']} name={item['query_name']}"
            )
            results.append(
                {
                    "screener_slug": item["screener_slug"],
                    "screener_name": item["screener_name"],
                    "query_name": item["query_name"],
                    "row_count": 0,
                    "status": "planned",
                }
            )
            continue
        _emit(
            "[advisory.training_universe] "
            f"query {index}/{total} start slug={item['screener_slug']} name={item['query_name']}"
        )
        started_at = time.monotonic()
        payload = fetch_ad_hoc_payload(
            query_text=item["query_text"],
            query_name=item["query_name"],
            persist=not dry_run,
        )
        frame = _normalize_training_payload(
            payload=payload,
            screener_slug=item["screener_slug"],
            screener_name=item["screener_name"],
            asof_date=effective_date,
        )
        if not frame.empty:
            frames.append(frame)
        _emit(
            "[advisory.training_universe] "
            f"query {index}/{total} done slug={item['screener_slug']} elapsed={time.monotonic() - started_at:.2f}s rows={len(frame)}"
        )
        results.append(
            {
                "screener_slug": item["screener_slug"],
                "screener_name": item["screener_name"],
                "query_name": item["query_name"],
                "row_count": int(len(frame)),
            }
        )
    combined = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
    if not dry_run and not combined.empty:
        _emit(
            "[advisory.training_universe] "
            f"persist start screeners={len(universes)} rows={len(combined)}"
        )
        persist_started_at = time.monotonic()
        persist_constituents(combined)
        _emit(
            "[advisory.training_universe] "
            f"persist done elapsed={time.monotonic() - persist_started_at:.2f}s rows={len(combined)}"
        )
    return {
        "asof_date": effective_date.date().isoformat(),
        "screener_count": len(universes),
        "row_count": int(len(combined)),
        "results": results,
        "dry_run": bool(dry_run),
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Sync broad ad hoc training universes into advisory_screener_constituents.")
    parser.add_argument("--date", type=parse_datetime_arg, help="Optional as-of date in YYYY-MM-DD")
    parser.add_argument("--config", help="Optional training-universe config path")
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    summary = sync_training_universes(
        asof_date=pd.Timestamp(args.date, tz="UTC") if args.date else None,
        config_path=args.config,
        dry_run=bool(args.dry_run),
    )
    print(json.dumps(summary, indent=2, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
