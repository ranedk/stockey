from __future__ import annotations

import argparse
import json
from typing import Any

import pandas as pd

from advisory.event_evidence_store import build_event_evidence_store
from advisory.event_evidence_store import ensure_announcement_context_overlay_table
from advisory.event_evidence_store import ensure_bhavcopy_context_overlay_table
from advisory.exchange_context_overlays import build_exchange_context_overlays, persist_exchange_context_overlays
from advisory.exchange_context_overlays import ensure_exchange_context_overlay_table
from advisory.fallback_telemetry import record_local_fallback_event
from advisory.macro_context_overlays import build_macro_context_overlays, persist_macro_context_overlays
from advisory.macro_context_overlays import ensure_macro_context_overlay_table
from advisory.news_theme_engine import build_theme_context_overlays, persist_theme_context_overlays
from advisory.news_theme_engine import ensure_theme_context_overlay_table
from advisory.signal_refresh import inspect_context_overlay_signal_inputs
from utils.sync import parse_datetime_arg


DEFAULT_EXCHANGE_LOOKBACK_DAYS = 30
STOCKEY_RUN_STATE: dict[str, Any] = {}


def _json_ready(value: Any) -> Any:
    if isinstance(value, pd.Timestamp):
        return None if pd.isna(value) else value.isoformat()
    if isinstance(value, dict):
        return {str(key): _json_ready(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_ready(item) for item in value]
    try:
        if pd.isna(value):
            return None
    except Exception as exc:
        record_local_fallback_event(
            module="advisory.context_overlay_refresh",
            fallback_type="context_overlay_refresh_json_ready_missing_check_failed",
            source="json_ready",
            severity="warn",
            reason="Context overlay refresh could not evaluate missingness for a JSON summary value and kept the original value.",
            error=exc,
            metadata={"value_type": type(value).__name__},
        )
    return value


def _normalize_date(value: Any) -> pd.Timestamp:
    ts = pd.to_datetime(value if value is not None else pd.Timestamp.utcnow(), utc=True, errors="coerce")
    if pd.isna(ts):
        ts = pd.Timestamp.utcnow()
    return ts.normalize()


def _date_range(*, from_date: Any = None, to_date: Any = None) -> list[pd.Timestamp]:
    end = _normalize_date(to_date)
    start = _normalize_date(from_date) if from_date is not None else end
    if start > end:
        return []
    return list(pd.date_range(start=start, end=end, freq="D", tz="UTC"))


def _frame_summary(df: pd.DataFrame, *, sample: int = 3) -> dict[str, Any]:
    if df.empty:
        return {"rows": 0, "sample": []}
    clean = df.head(max(0, int(sample))).copy().astype(object).where(pd.notna(df.head(max(0, int(sample)))), None)
    return {
        "rows": int(len(df)),
        "sample": [{key: _json_ready(value) for key, value in row.items()} for row in clean.to_dict(orient="records")],
    }


def ensure_context_overlay_tables(
    *,
    include_event_evidence: bool = True,
    include_theme: bool = True,
    include_macro: bool = True,
    include_exchange: bool = True,
) -> dict[str, Any]:
    ensured: list[str] = []
    if include_event_evidence:
        ensure_bhavcopy_context_overlay_table()
        ensure_announcement_context_overlay_table()
        ensured.extend(["bhavcopy", "announcements"])
    if include_theme:
        ensure_theme_context_overlay_table()
        ensured.append("theme")
    if include_macro:
        ensure_macro_context_overlay_table()
        ensured.append("macro")
    if include_exchange:
        ensure_exchange_context_overlay_table()
        ensured.append("exchange")
    return {"status": "ok", "ensured": ensured, "ensured_count": len(ensured)}


def refresh_context_overlays(
    *,
    from_date: Any = None,
    to_date: Any = None,
    dry_run: bool = False,
    include_event_evidence: bool = True,
    include_theme: bool = True,
    include_macro: bool = True,
    include_exchange: bool = True,
    event_lookback_days: int = 365,
    exchange_lookback_days: int = DEFAULT_EXCHANGE_LOOKBACK_DAYS,
) -> dict[str, Any]:
    dates = _date_range(from_date=from_date, to_date=to_date)
    if not dates:
        return {
            "status": "ok",
            "dry_run": bool(dry_run),
            "from_date": None,
            "to_date": None,
            "date_count": 0,
            "rows": 0,
            "rows_written": 0,
            "date_results": [],
        }

    schema_setup = {"status": "skipped", "reason": "dry_run_no_schema_mutation", "ensured": [], "ensured_count": 0}
    if not dry_run:
        schema_setup = ensure_context_overlay_tables(
            include_event_evidence=include_event_evidence,
            include_theme=include_theme,
            include_macro=include_macro,
            include_exchange=include_exchange,
        )
    pre_refresh_diagnostics = inspect_context_overlay_signal_inputs(asof_date=dates[-1])

    event_payload: dict[str, Any] | None = None
    if include_event_evidence:
        event_payload = build_event_evidence_store(
            from_date=dates[0],
            to_date=dates[-1],
            rebuild=False,
            lookback_days=max(1, int(event_lookback_days)),
            include_bhavcopy=True,
            include_announcements=True,
            dry_run=bool(dry_run),
        )

    date_results: list[dict[str, Any]] = []
    total_rows = 0
    total_written = 0
    for asof_date in dates:
        result: dict[str, Any] = {"asof_date": asof_date.isoformat()}
        if include_theme:
            theme_df, theme_meta = build_theme_context_overlays(asof_date=asof_date)
            if not dry_run:
                persist_theme_context_overlays(theme_df)
            result["theme"] = {**_frame_summary(theme_df), "meta": _json_ready(theme_meta)}
            total_rows += int(len(theme_df))
            total_written += 0 if dry_run else int(len(theme_df))
        if include_macro:
            macro_df, macro_meta = build_macro_context_overlays(asof_date=asof_date)
            if not dry_run:
                persist_macro_context_overlays(macro_df)
            result["macro"] = {**_frame_summary(macro_df), "meta": _json_ready(macro_meta)}
            total_rows += int(len(macro_df))
            total_written += 0 if dry_run else int(len(macro_df))
        if include_exchange:
            exchange_df, exchange_meta = build_exchange_context_overlays(
                asof_date=asof_date,
                lookback_days=max(1, int(exchange_lookback_days)),
            )
            if not dry_run:
                persist_exchange_context_overlays(exchange_df)
            result["exchange"] = {**_frame_summary(exchange_df), "meta": _json_ready(exchange_meta)}
            total_rows += int(len(exchange_df))
            total_written += 0 if dry_run else int(len(exchange_df))
        date_results.append(result)

    event_rows = 0
    event_written = 0
    if event_payload:
        bhavcopy = event_payload.get("bhavcopy") if isinstance(event_payload.get("bhavcopy"), dict) else {}
        announcements = event_payload.get("announcements") if isinstance(event_payload.get("announcements"), dict) else {}
        event_rows = int(bhavcopy.get("context_overlay_rows") or 0) + int(announcements.get("context_overlay_rows") or 0)
        event_written = 0 if dry_run else event_rows
        total_rows += event_rows
        total_written += event_written

    post_refresh_diagnostics = inspect_context_overlay_signal_inputs(asof_date=dates[-1])

    return {
        "status": "ok",
        "dry_run": bool(dry_run),
        "from_date": dates[0].isoformat(),
        "to_date": dates[-1].isoformat(),
        "date_count": int(len(dates)),
        "schema_setup": _json_ready(schema_setup),
        "rows": int(total_rows),
        "rows_written": int(total_written),
        "event_context_overlay_rows": int(event_rows),
        "event_context_overlay_rows_written": int(event_written),
        "event_evidence": _json_ready(event_payload),
        "pre_refresh_diagnostics": _json_ready(pre_refresh_diagnostics),
        "post_refresh_diagnostics": _json_ready(post_refresh_diagnostics),
        "date_results": date_results,
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Refresh review-only context overlay tables from already-ingested data.")
    parser.add_argument("--from-date", type=parse_datetime_arg)
    parser.add_argument("--to-date", type=parse_datetime_arg)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--skip-event-evidence", action="store_true")
    parser.add_argument("--skip-theme", action="store_true")
    parser.add_argument("--skip-macro", action="store_true")
    parser.add_argument("--skip-exchange", action="store_true")
    parser.add_argument("--event-lookback-days", type=int, default=365)
    parser.add_argument("--exchange-lookback-days", type=int, default=DEFAULT_EXCHANGE_LOOKBACK_DAYS)
    parser.add_argument("--format", choices=["json", "text"], default="json")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    global STOCKEY_RUN_STATE
    args = parse_args(argv)
    payload = refresh_context_overlays(
        from_date=args.from_date,
        to_date=args.to_date,
        dry_run=bool(args.dry_run),
        include_event_evidence=not bool(args.skip_event_evidence),
        include_theme=not bool(args.skip_theme),
        include_macro=not bool(args.skip_macro),
        include_exchange=not bool(args.skip_exchange),
        event_lookback_days=max(1, int(args.event_lookback_days)),
        exchange_lookback_days=max(1, int(args.exchange_lookback_days)),
    )
    STOCKEY_RUN_STATE = {
        "from_date": payload.get("from_date"),
        "to_date": payload.get("to_date"),
        "rows": int(payload.get("rows") or 0),
        "rows_written": int(payload.get("rows_written") or 0),
        "rows_read": int(payload.get("rows") or 0),
        "date_count": int(payload.get("date_count") or 0),
        "fallback_used": False,
        "dry_run": bool(payload.get("dry_run")),
        "state_advanced": not bool(payload.get("dry_run")),
    }
    if args.format == "json":
        print(json.dumps(payload, indent=2, ensure_ascii=False, default=str))
    else:
        print(
            "context_overlay_refresh "
            f"dates={payload.get('date_count')} rows={payload.get('rows')} "
            f"written={payload.get('rows_written')} dry_run={payload.get('dry_run')}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
