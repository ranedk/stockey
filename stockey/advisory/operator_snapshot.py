from __future__ import annotations

import argparse
import hashlib
import json
from datetime import date, datetime
from pathlib import Path
from typing import Any

import pandas as pd
from environs import Env

from advisory.current_prices import refresh_current_prices
from advisory.fallback_telemetry import record_local_fallback_event
from advisory.live_dashboard import DEFAULT_OUTPUT_DIR, build_live_dashboard_payload
from advisory.performance_slowlog import slow_operation
from utils.db import sql_to_df, upsert_to_db


env = Env()
env.read_env()

TABLE_NAME = "advisory_operator_snapshots"
SECTION_TABLE_NAME = "advisory_operator_snapshot_sections"
SNAPSHOT_NAME = "operator_dashboard_v1"
DEFAULT_MAX_AGE_SECONDS = env.int("OPERATOR_SNAPSHOT_MAX_AGE_SECONDS", 86400)


def _record_snapshot_fallback(
    fallback_type: str,
    *,
    source: str,
    reason: str,
    error: Exception,
    metadata: dict[str, Any] | None = None,
) -> None:
    record_local_fallback_event(
        module="advisory.operator_snapshot",
        fallback_type=fallback_type,
        source=source,
        severity="warn",
        reason=reason,
        error=error,
        metadata=metadata or {},
    )


def _json_default(value: Any) -> str:
    if isinstance(value, (pd.Timestamp, datetime, date)):
        return value.isoformat()
    return str(value)


def _parse_asof_date(value: str | pd.Timestamp | None) -> pd.Timestamp | None:
    if value is None or isinstance(value, pd.Timestamp):
        return value
    ts = pd.to_datetime(value, utc=True, errors="coerce")
    if pd.isna(ts):
        raise ValueError(f"Invalid asof_date: {value}")
    return ts.normalize()


def snapshot_key(asof_date: pd.Timestamp | None) -> str:
    if asof_date is None:
        return "latest"
    ts = pd.to_datetime(asof_date, utc=True).normalize()
    return ts.strftime("%Y-%m-%d")


def _section_counts(payload: dict[str, Any]) -> dict[str, int]:
    out: dict[str, int] = {}
    for key, value in payload.items():
        if isinstance(value, list):
            out[key] = len(value)
        elif isinstance(value, dict):
            out[key] = len(value)
    return out


def _payload_to_row(payload: dict[str, Any], *, asof_date: pd.Timestamp | None) -> dict[str, Any]:
    payload_json = json.dumps(payload, ensure_ascii=False, default=_json_default, allow_nan=False)
    payload_bytes = len(payload_json.encode("utf-8"))
    return {
        "snapshot_name": SNAPSHOT_NAME,
        "snapshot_key": snapshot_key(asof_date),
        "asof_date": asof_date,
        "generated_at": pd.Timestamp.utcnow(),
        "payload_json": payload_json,
        "payload_sha256": hashlib.sha256(payload_json.encode("utf-8")).hexdigest(),
        "payload_bytes": payload_bytes,
        "section_counts_json": json.dumps(_section_counts(payload), ensure_ascii=True, default=str),
    }


def _payload_to_section_rows(payload: dict[str, Any], *, asof_date: pd.Timestamp | None) -> list[dict[str, Any]]:
    generated_at = pd.Timestamp.utcnow()
    key = snapshot_key(asof_date)
    rows: list[dict[str, Any]] = []
    for section_name, section_payload in payload.items():
        if section_name.startswith("_"):
            continue
        if not isinstance(section_payload, (list, dict)):
            continue
        section_json = json.dumps(section_payload, ensure_ascii=False, default=_json_default, allow_nan=False)
        rows.append(
            {
                "snapshot_name": SNAPSHOT_NAME,
                "snapshot_key": key,
                "section_name": section_name,
                "asof_date": asof_date,
                "generated_at": generated_at,
                "payload_json": section_json,
                "payload_bytes": len(section_json.encode("utf-8")),
                "payload_sha256": hashlib.sha256(section_json.encode("utf-8")).hexdigest(),
            }
        )
    return rows


def _symbols_from_payload(payload: dict[str, Any]) -> list[str]:
    symbols: set[str] = set()
    for key in [
        "top_action_recommendations",
        "action_recommendations",
        "alerts",
        "portfolio",
        "current_recommendations",
        "today_recommendations",
        "watch_recommendations",
        "watchlist",
    ]:
        rows = payload.get(key)
        if not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, dict):
                continue
            symbol = str(row.get("symbol") or row.get("ticker") or "").strip().upper()
            if symbol:
                symbols.add(symbol)
    return sorted(symbols)


def build_operator_snapshot(
    *,
    asof_date: str | pd.Timestamp | None = None,
    output_dir: str | Path = DEFAULT_OUTPUT_DIR,
    persist: bool = True,
) -> dict[str, Any]:
    parsed_asof = _parse_asof_date(asof_date)
    with slow_operation(
        "operator_snapshot",
        "build_live_dashboard_payload",
        threshold_ms=env.float("OPERATOR_SNAPSHOT_SLOW_BUILD_MS", 5000.0),
        details={"snapshot_name": SNAPSHOT_NAME, "snapshot_key": snapshot_key(parsed_asof)},
    ):
        payload = build_live_dashboard_payload(asof_date=parsed_asof, output_dir=output_dir)
    price_cache: dict[str, Any] = {"status": "skipped", "reason": "no_payload_symbols"}
    symbols = _symbols_from_payload(payload)
    if symbols:
        try:
            price_cache = refresh_current_prices(symbols=symbols, persist=persist)
        except Exception as exc:
            _record_snapshot_fallback(
                "operator_snapshot_price_cache_refresh_failed",
                source="advisory_current_prices",
                reason="Operator snapshot could not refresh current prices; snapshot was still generated but price freshness may be degraded.",
                error=exc,
                metadata={"symbol_count": len(symbols), "persist": bool(persist)},
            )
            price_cache = {"status": "error", "error": f"{type(exc).__name__}: {exc}", "requested_symbols": len(symbols)}
    row = _payload_to_row(payload, asof_date=parsed_asof)
    if persist:
        upsert_to_db(pd.DataFrame([row]), TABLE_NAME, unique_keys=["snapshot_name", "snapshot_key"])
        section_rows = _payload_to_section_rows(payload, asof_date=parsed_asof)
        if section_rows:
            upsert_to_db(pd.DataFrame(section_rows), SECTION_TABLE_NAME, unique_keys=["snapshot_name", "snapshot_key", "section_name"])
    return {
        "status": "ok",
        "snapshot_name": row["snapshot_name"],
        "snapshot_key": row["snapshot_key"],
        "asof_date": None if parsed_asof is None else parsed_asof.isoformat(),
        "generated_at": row["generated_at"].isoformat(),
        "payload_bytes": row["payload_bytes"],
        "payload_sha256": row["payload_sha256"],
        "section_counts": json.loads(row["section_counts_json"]),
        "price_cache": price_cache,
        "persisted": bool(persist),
    }


def load_operator_snapshot(
    *,
    asof_date: str | pd.Timestamp | None = None,
    max_age_seconds: int | None = DEFAULT_MAX_AGE_SECONDS,
) -> dict[str, Any] | None:
    parsed_asof = _parse_asof_date(asof_date)
    key = snapshot_key(parsed_asof)
    clauses = ["snapshot_name = %s", "snapshot_key = %s"]
    params: list[Any] = [SNAPSHOT_NAME, key]
    if max_age_seconds is not None and int(max_age_seconds) > 0:
        clauses.append("generated_at >= NOW() - (%s * INTERVAL '1 second')")
        params.append(int(max_age_seconds))
    query = f"""
        SELECT payload_json, generated_at, payload_bytes, payload_sha256
        FROM {TABLE_NAME}
        WHERE {' AND '.join(clauses)}
        ORDER BY generated_at DESC
        LIMIT 1
    """
    try:
        df = sql_to_df(query, params=tuple(params), retries=2)
    except Exception as exc:
        _record_snapshot_fallback(
            "operator_snapshot_load_failed",
            source=TABLE_NAME,
            reason="Operator API could not load the DB-backed dashboard snapshot; callers may fall back to live payload building or stale/missing UI data.",
            error=exc,
            metadata={"snapshot_key": key, "max_age_seconds": max_age_seconds},
        )
        return None
    if df.empty:
        return None
    payload = json.loads(str(df.iloc[0]["payload_json"]))
    payload["_snapshot"] = {
        "source": "db_snapshot",
        "snapshot_name": SNAPSHOT_NAME,
        "snapshot_key": key,
        "generated_at": str(df.iloc[0]["generated_at"]),
        "payload_bytes": int(df.iloc[0]["payload_bytes"] or 0),
        "payload_sha256": str(df.iloc[0]["payload_sha256"] or ""),
    }
    return payload


def load_operator_snapshot_sections(
    section_names: list[str],
    *,
    asof_date: str | pd.Timestamp | None = None,
    max_age_seconds: int | None = DEFAULT_MAX_AGE_SECONDS,
) -> dict[str, Any] | None:
    names = sorted({str(name or "").strip() for name in section_names if str(name or "").strip()})
    if not names:
        return None
    parsed_asof = _parse_asof_date(asof_date)
    key = snapshot_key(parsed_asof)
    clauses = ["snapshot_name = %s", "snapshot_key = %s", "section_name = ANY(%s)"]
    params: list[Any] = [SNAPSHOT_NAME, key, names]
    if max_age_seconds is not None and int(max_age_seconds) > 0:
        clauses.append("generated_at >= NOW() - (%s * INTERVAL '1 second')")
        params.append(int(max_age_seconds))
    query = f"""
        SELECT section_name, payload_json, generated_at, asof_date, payload_bytes, payload_sha256
        FROM {SECTION_TABLE_NAME}
        WHERE {' AND '.join(clauses)}
        ORDER BY generated_at DESC
    """
    try:
        df = sql_to_df(query, params=tuple(params), retries=2)
    except Exception as exc:
        _record_snapshot_fallback(
            "operator_snapshot_section_load_failed",
            source=SECTION_TABLE_NAME,
            reason="Operator API could not load DB-backed section snapshots; callers may fall back to larger live payloads or stale/missing UI sections.",
            error=exc,
            metadata={"snapshot_key": key, "section_names": names, "max_age_seconds": max_age_seconds},
        )
        return None
    if df.empty:
        return None
    payload: dict[str, Any] = {}
    latest_generated_at = None
    total_bytes = 0
    for row in df.to_dict(orient="records"):
        section_name = str(row.get("section_name") or "")
        if section_name in payload:
            continue
        payload[section_name] = json.loads(str(row.get("payload_json") or "null"))
        latest_generated_at = row.get("generated_at") if latest_generated_at is None else latest_generated_at
        total_bytes += int(row.get("payload_bytes") or 0)
        if "asof_date" not in payload:
            asof_value = row.get("asof_date")
            payload["asof_date"] = None if pd.isna(asof_value) else str(asof_value)
    missing = [name for name in names if name not in payload]
    if missing:
        return None
    payload["generated_at"] = None if latest_generated_at is None else str(latest_generated_at)
    payload["_snapshot"] = {
        "source": "db_section_snapshot",
        "snapshot_name": SNAPSHOT_NAME,
        "snapshot_key": key,
        "generated_at": payload["generated_at"],
        "payload_bytes": total_bytes,
        "sections": names,
    }
    return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build the DB-backed operator frontend snapshot.")
    parser.add_argument("--asof-date", default=None)
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    parser.add_argument("--dry-run", action="store_true", help="Build but do not persist.")
    args = parser.parse_args(argv)
    result = build_operator_snapshot(
        asof_date=args.asof_date,
        output_dir=args.output_dir,
        persist=not bool(args.dry_run),
    )
    print(json.dumps(result, indent=2, default=_json_default))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
