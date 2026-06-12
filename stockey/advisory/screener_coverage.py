from __future__ import annotations

import argparse
import json
from typing import Any

import pandas as pd

from advisory.action_recommender import TABLE_NAME as ACTION_RECOMMENDATIONS_TABLE
from advisory.fallback_telemetry import record_local_fallback_event
from advisory.rule_engine import CANDIDATES_TABLE
from advisory.screener_parser import CONSTITUENTS_TABLE
from utils.db import sql_to_df


def _table_exists(table_name: str) -> bool:
    df = sql_to_df("SELECT to_regclass(%s) AS table_name", params=(table_name,))
    return not df.empty and df.iloc[0].get("table_name") is not None


def _record_json_parse_fallback(*, fallback_type: str, source: str, value: str, error: Exception) -> None:
    record_local_fallback_event(
        module="advisory.screener_coverage",
        fallback_type=fallback_type,
        source=source,
        severity="warn",
        reason="Screener coverage could not parse a stored JSON payload; using the existing empty fallback.",
        error=error,
        metadata={"source": source, "payload_length": len(value)},
    )


def _json_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError as exc:
            _record_json_parse_fallback(
                fallback_type="screener_coverage_json_dict_parse_failed",
                source="json_dict",
                value=value,
                error=exc,
            )
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _json_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError as exc:
            _record_json_parse_fallback(
                fallback_type="screener_coverage_json_list_parse_failed",
                source="json_list",
                value=value,
                error=exc,
            )
            return []
        return parsed if isinstance(parsed, list) else []
    return []


def _normalize_slug(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


def extract_screener_slugs(row: dict[str, Any]) -> list[str]:
    slugs: set[str] = set()
    for key in ["screener_slug", "source_screener_slug"]:
        slug = _normalize_slug(row.get(key))
        if slug:
            slugs.add(slug)
    for item in _json_list(row.get("source_screener_list")):
        slug = _normalize_slug(item)
        if slug:
            slugs.add(slug)

    raw_context = _json_dict(row.get("raw_context_json") or row.get("raw_context"))
    for key in ["screener_slug", "source_screener_slug"]:
        slug = _normalize_slug(raw_context.get(key))
        if slug:
            slugs.add(slug)
    for item in _json_list(raw_context.get("source_screener_list")):
        slug = _normalize_slug(item)
        if slug:
            slugs.add(slug)

    reason = _json_dict(row.get("recommendation_reason_json") or row.get("recommendation_reason"))
    evidence = reason.get("evidence") if isinstance(reason.get("evidence"), dict) else {}
    screener = evidence.get("screener") if isinstance(evidence.get("screener"), dict) else {}
    for key in ["screener_slug", "source_screener_slug"]:
        slug = _normalize_slug(screener.get(key))
        if slug:
            slugs.add(slug)
    for item in _json_list(screener.get("source_screener_list")):
        slug = _normalize_slug(item)
        if slug:
            slugs.add(slug)
    return sorted(slugs)


def _records(df: pd.DataFrame) -> list[dict[str, Any]]:
    if df.empty:
        return []
    clean = df.where(pd.notnull(df), None)
    return clean.to_dict(orient="records")


def _date_window(asof_date: str | pd.Timestamp | None, lookback_days: int) -> tuple[pd.Timestamp, pd.Timestamp]:
    end = pd.to_datetime(asof_date, utc=True, errors="coerce") if asof_date else pd.Timestamp.now(tz="UTC")
    if pd.isna(end):
        raise ValueError(f"Invalid asof_date: {asof_date}")
    end = end.normalize()
    start = end - pd.Timedelta(days=max(int(lookback_days), 0))
    return start, end


def _load_constituents(start_date: pd.Timestamp, end_date: pd.Timestamp) -> pd.DataFrame:
    if not _table_exists(CONSTITUENTS_TABLE):
        return pd.DataFrame()
    return sql_to_df(
        f"""
        SELECT
            date::date AS screener_date,
            screener_slug,
            MAX(screener_name) AS screener_name,
            COUNT(*) AS constituent_rows,
            COUNT(DISTINCT UPPER(TRIM(ticker))) AS constituent_symbols,
            MAX(load_ts) AS latest_load_ts
        FROM {CONSTITUENTS_TABLE}
        WHERE date >= %(start_date)s
          AND date <= %(end_date)s
        GROUP BY date::date, screener_slug
        ORDER BY date::date DESC, screener_slug
        """,
        params={"start_date": start_date.date(), "end_date": end_date.date()},
        retries=3,
        statement_timeout_ms=15000,
    )


def _load_candidates(start_date: pd.Timestamp, end_date: pd.Timestamp) -> pd.DataFrame:
    if not _table_exists(CANDIDATES_TABLE):
        return pd.DataFrame()
    return sql_to_df(
        f"""
        SELECT
            asof_date,
            symbol,
            setup_id,
            candidate_state,
            rule_pass,
            source_screener_slug,
            source_screener_list,
            setup_score,
            load_ts
        FROM {CANDIDATES_TABLE}
        WHERE asof_date >= %(start_date)s
          AND asof_date < %(end_exclusive)s
        """,
        params={"start_date": start_date, "end_exclusive": end_date + pd.Timedelta(days=1)},
        retries=3,
        statement_timeout_ms=15000,
    )


def _load_actions(start_date: pd.Timestamp, end_date: pd.Timestamp) -> pd.DataFrame:
    if not _table_exists(ACTION_RECOMMENDATIONS_TABLE):
        return pd.DataFrame()
    return sql_to_df(
        f"""
        SELECT
            asof_date,
            symbol,
            setup_id,
            action_code,
            action_source,
            source_action,
            reason_contract_status,
            raw_context_json,
            recommendation_reason_json,
            load_ts
        FROM {ACTION_RECOMMENDATIONS_TABLE}
        WHERE asof_date >= %(start_date)s
          AND asof_date < %(end_exclusive)s
        """,
        params={"start_date": start_date, "end_exclusive": end_date + pd.Timedelta(days=1)},
        retries=3,
        statement_timeout_ms=15000,
    )


def _explode_by_screener(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return pd.DataFrame()
    rows: list[dict[str, Any]] = []
    for row in _records(df):
        for slug in extract_screener_slugs(row):
            rows.append({**row, "screener_slug": slug})
    return pd.DataFrame(rows)


def build_screener_coverage_payload(*, asof_date: str | pd.Timestamp | None = None, lookback_days: int = 30, limit: int = 50) -> dict[str, Any]:
    start_date, end_date = _date_window(asof_date, lookback_days)
    constituents = _load_constituents(start_date, end_date)
    candidates = _explode_by_screener(_load_candidates(start_date, end_date))
    actions = _explode_by_screener(_load_actions(start_date, end_date))

    by_slug: dict[str, dict[str, Any]] = {}
    for row in _records(constituents):
        slug = str(row.get("screener_slug") or "").strip()
        if not slug:
            continue
        target = by_slug.setdefault(slug, {"screener_slug": slug})
        target["screener_name"] = target.get("screener_name") or row.get("screener_name")
        target["latest_constituent_date"] = max(
            [value for value in [target.get("latest_constituent_date"), row.get("screener_date")] if value is not None],
            default=row.get("screener_date"),
        )
        target["constituent_rows"] = int(target.get("constituent_rows") or 0) + int(row.get("constituent_rows") or 0)
        target["constituent_symbols"] = max(int(target.get("constituent_symbols") or 0), int(row.get("constituent_symbols") or 0))
        target["latest_load_ts"] = target.get("latest_load_ts") or row.get("latest_load_ts")

    if not candidates.empty:
        for slug, group in candidates.groupby("screener_slug", dropna=True):
            target = by_slug.setdefault(str(slug), {"screener_slug": str(slug)})
            states = group["candidate_state"].fillna("UNKNOWN").astype(str).str.upper()
            symbols = group["symbol"].dropna().astype(str).str.upper()
            target["candidate_rows"] = int(len(group))
            target["candidate_symbols"] = int(symbols.nunique())
            target["rule_pass_rows"] = int(pd.Series(group.get("rule_pass", pd.Series(dtype=bool))).fillna(False).astype(bool).sum())
            target["pass_now_rows"] = int((states == "PASS_NOW").sum())
            target["watch_rows"] = int(states.str.startswith("WATCH").sum())
            target["abstain_rows"] = int((states == "ABSTAIN").sum())
            target["avg_setup_score"] = None if "setup_score" not in group else float(pd.to_numeric(group["setup_score"], errors="coerce").mean(skipna=True))

    if not actions.empty:
        for slug, group in actions.groupby("screener_slug", dropna=True):
            target = by_slug.setdefault(str(slug), {"screener_slug": str(slug)})
            codes = group["action_code"].fillna("UNKNOWN").astype(str).str.upper()
            symbols = group["symbol"].dropna().astype(str).str.upper()
            action_counts = codes.value_counts().to_dict()
            target["action_rows"] = int(len(group))
            target["action_symbols"] = int(symbols.nunique())
            target["action_counts"] = {str(key): int(value) for key, value in action_counts.items()}
            target["positive_action_rows"] = int(codes.isin(["BUY", "BUY_MORE"]).sum())
            target["exit_action_rows"] = int(codes.isin(["SELL", "PARTIAL_SELL"]).sum())
            target["manual_review_rows"] = int((codes == "MANUAL_REVIEW").sum())
            target["watch_action_rows"] = int((codes == "WATCH").sum())

    rows = list(by_slug.values())
    for row in rows:
        constituent_symbols = int(row.get("constituent_symbols") or 0)
        candidate_symbols = int(row.get("candidate_symbols") or 0)
        action_symbols = int(row.get("action_symbols") or 0)
        row["candidate_symbol_coverage_pct"] = round((candidate_symbols / constituent_symbols) * 100, 2) if constituent_symbols else None
        row["action_symbol_coverage_pct"] = round((action_symbols / constituent_symbols) * 100, 2) if constituent_symbols else None
        row["positive_action_rate_pct"] = round((int(row.get("positive_action_rows") or 0) / max(int(row.get("action_rows") or 0), 1)) * 100, 2) if int(row.get("action_rows") or 0) else 0.0

    rows.sort(
        key=lambda item: (
            int(item.get("action_symbols") or 0),
            int(item.get("candidate_symbols") or 0),
            int(item.get("constituent_symbols") or 0),
            str(item.get("screener_slug") or ""),
        ),
        reverse=True,
    )
    limit = min(max(int(limit), 1), 200)
    page = rows[:limit]
    return {
        "status": "ok",
        "generated_at": pd.Timestamp.utcnow().isoformat(),
        "asof_date": end_date.isoformat(),
        "lookback_days": int(lookback_days),
        "window": {"start_date": start_date.date().isoformat(), "end_date": end_date.date().isoformat()},
        "summary": {
            "screener_count": len(rows),
            "returned_count": len(page),
            "constituent_rows": int(constituents["constituent_rows"].sum()) if not constituents.empty else 0,
            "candidate_rows": int(len(candidates)),
            "action_rows": int(len(actions)),
        },
        "screeners": page,
        "notes": [
            "Coverage is read-only and point-in-time over the selected window.",
            "Action coverage uses screener provenance preserved in action/candidate context; rows without screener provenance are not attributed.",
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Summarize Screener.in contribution to candidates and final action recommendations.")
    parser.add_argument("--asof-date")
    parser.add_argument("--lookback-days", type=int, default=30)
    parser.add_argument("--limit", type=int, default=50)
    args = parser.parse_args()
    print(json.dumps(build_screener_coverage_payload(asof_date=args.asof_date, lookback_days=args.lookback_days, limit=args.limit), indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
