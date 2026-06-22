from __future__ import annotations

import argparse
import hashlib
import json
from typing import Any

import pandas as pd

from advisory.fallback_telemetry import record_local_fallback_event
from utils.db import sql_to_df, upsert_to_db
from utils.schema_migrations import apply_schema_migration
from utils.sync import parse_datetime_arg


BHAVCOPY_EVIDENCE_TABLE = "advisory_bhavcopy_evidence_daily"
ANNOUNCEMENT_EVIDENCE_TABLE = "advisory_announcement_evidence"
BHAVCOPY_CONTEXT_OVERLAYS_TABLE = "advisory_bhavcopy_context_overlays"
ANNOUNCEMENT_CONTEXT_OVERLAYS_TABLE = "advisory_announcement_context_overlays"
DEFAULT_LOOKBACK_DAYS = 365
STOCKEY_RUN_STATE: dict[str, Any] = {}

BHAVCOPY_CONTEXT_OVERLAY_SCHEMA_MIGRATION_ID = "20260620_advisory_bhavcopy_context_overlays_base"
BHAVCOPY_CONTEXT_OVERLAY_SCHEMA_STATEMENTS = [
    f"""
    CREATE TABLE IF NOT EXISTS {BHAVCOPY_CONTEXT_OVERLAYS_TABLE} (
        asof_date TIMESTAMPTZ NOT NULL,
        overlay_id TEXT NOT NULL,
        symbol TEXT NOT NULL,
        company_master_id TEXT,
        direction TEXT NOT NULL,
        pressure_score DOUBLE PRECISION,
        evidence_score DOUBLE PRECISION,
        deal_pressure TEXT,
        deal_net_value_inr DOUBLE PRECISION,
        short_selling_quantity DOUBLE PRECISION,
        circuit_hit_count DOUBLE PRECISION,
        turnover_value_inr DOUBLE PRECISION,
        avg_turnover_value_20d DOUBLE PRECISION,
        watch_reason_detail TEXT,
        matched_sources_json TEXT,
        authority_scope TEXT NOT NULL DEFAULT 'watchlist_pressure_only',
        production_status TEXT NOT NULL DEFAULT 'active',
        load_ts TIMESTAMPTZ NOT NULL,
        UNIQUE (asof_date, overlay_id)
    )
    """,
]
ANNOUNCEMENT_CONTEXT_OVERLAY_SCHEMA_MIGRATION_ID = "20260620_advisory_announcement_context_overlays_base"
ANNOUNCEMENT_CONTEXT_OVERLAY_SCHEMA_STATEMENTS = [
    f"""
    CREATE TABLE IF NOT EXISTS {ANNOUNCEMENT_CONTEXT_OVERLAYS_TABLE} (
        published_on TIMESTAMPTZ NOT NULL,
        asof_date TIMESTAMPTZ NOT NULL,
        overlay_id TEXT NOT NULL,
        evidence_id TEXT NOT NULL,
        unique_id TEXT,
        symbol TEXT NOT NULL,
        company_master_id TEXT,
        event_class TEXT,
        direction TEXT NOT NULL,
        pressure_score DOUBLE PRECISION,
        materiality DOUBLE PRECISION,
        surprise DOUBLE PRECISION,
        novelty DOUBLE PRECISION,
        contradiction DOUBLE PRECISION,
        confidence DOUBLE PRECISION,
        verdict TEXT,
        source_reliability TEXT,
        watch_reason_detail TEXT,
        matched_sources_json TEXT,
        authority_scope TEXT NOT NULL DEFAULT 'watchlist_pressure_only',
        production_status TEXT NOT NULL DEFAULT 'active',
        load_ts TIMESTAMPTZ NOT NULL,
        UNIQUE (published_on, overlay_id)
    )
    """,
]
ANNOUNCEMENT_CONTEXT_OVERLAY_TAXONOMY_SCHEMA_MIGRATION_ID = "20260621_advisory_announcement_context_overlays_taxonomy_contract"
ANNOUNCEMENT_CONTEXT_OVERLAY_TAXONOMY_SCHEMA_STATEMENTS = [
    f"ALTER TABLE {ANNOUNCEMENT_CONTEXT_OVERLAYS_TABLE} ADD COLUMN IF NOT EXISTS announcement_storage_form TEXT",
    f"ALTER TABLE {ANNOUNCEMENT_CONTEXT_OVERLAYS_TABLE} ADD COLUMN IF NOT EXISTS llm_evidence_mode TEXT",
    f"ALTER TABLE {ANNOUNCEMENT_CONTEXT_OVERLAYS_TABLE} ADD COLUMN IF NOT EXISTS llm_review_ready BOOLEAN",
    f"ALTER TABLE {ANNOUNCEMENT_CONTEXT_OVERLAYS_TABLE} ADD COLUMN IF NOT EXISTS raw_archive_required BOOLEAN",
]
ANNOUNCEMENT_EVIDENCE_SCHEMA_MIGRATION_ID = "20260621_advisory_announcement_evidence_taxonomy_contract"
ANNOUNCEMENT_EVIDENCE_SCHEMA_STATEMENTS = [
    f"""
    CREATE TABLE IF NOT EXISTS {ANNOUNCEMENT_EVIDENCE_TABLE} (
        published_on TIMESTAMPTZ NOT NULL,
        evidence_id TEXT NOT NULL,
        unique_id TEXT,
        asof_date TIMESTAMPTZ,
        symbol TEXT,
        company_master_id TEXT,
        exchange TEXT,
        company_name TEXT,
        subject TEXT,
        filed_under_category TEXT,
        exchange_published_on TIMESTAMPTZ,
        attachment_url TEXT,
        attachment_name TEXT,
        parse_status TEXT,
        ocr_status TEXT,
        pdf_status TEXT,
        last_error TEXT,
        raw_s3_key TEXT,
        pdf_s3_key TEXT,
        ocr_s3_key TEXT,
        full_ocr_s3_key TEXT,
        audio_transcript_s3_key TEXT,
        concise_summary_s3_key TEXT,
        number_of_pages DOUBLE PRECISION,
        evidence_chars DOUBLE PRECISION,
        evidence_excerpt TEXT,
        event_source TEXT,
        event_class TEXT,
        direction TEXT,
        materiality DOUBLE PRECISION,
        surprise DOUBLE PRECISION,
        novelty DOUBLE PRECISION,
        contradiction DOUBLE PRECISION,
        confidence DOUBLE PRECISION,
        verdict TEXT,
        what_happened TEXT,
        rationale TEXT,
        event_tensor_json TEXT,
        prompt_version TEXT,
        has_text_evidence BOOLEAN,
        has_s3_evidence BOOLEAN,
        evidence_summary TEXT,
        source_reliability TEXT,
        announcement_storage_form TEXT,
        announcement_storage_reason TEXT,
        llm_evidence_mode TEXT,
        llm_review_ready BOOLEAN,
        raw_archive_required BOOLEAN,
        load_ts TIMESTAMPTZ,
        UNIQUE (published_on, evidence_id)
    )
    """,
    f"ALTER TABLE {ANNOUNCEMENT_EVIDENCE_TABLE} ADD COLUMN IF NOT EXISTS announcement_storage_form TEXT",
    f"ALTER TABLE {ANNOUNCEMENT_EVIDENCE_TABLE} ADD COLUMN IF NOT EXISTS announcement_storage_reason TEXT",
    f"ALTER TABLE {ANNOUNCEMENT_EVIDENCE_TABLE} ADD COLUMN IF NOT EXISTS llm_evidence_mode TEXT",
    f"ALTER TABLE {ANNOUNCEMENT_EVIDENCE_TABLE} ADD COLUMN IF NOT EXISTS llm_review_ready BOOLEAN",
    f"ALTER TABLE {ANNOUNCEMENT_EVIDENCE_TABLE} ADD COLUMN IF NOT EXISTS raw_archive_required BOOLEAN",
]


def _record_event_evidence_fallback(
    *,
    fallback_type: str,
    source: str,
    reason: str,
    error: Exception,
    metadata: dict[str, Any] | None = None,
) -> None:
    record_local_fallback_event(
        module="advisory.event_evidence_store",
        fallback_type=fallback_type,
        source=source,
        severity="warn",
        reason=reason,
        error=error,
        metadata=metadata or {},
    )


def _sql_num(expression: str) -> str:
    return f"NULLIF(regexp_replace(({expression})::text, '[^0-9.\\-]', '', 'g'), '')::double precision"


def _json_ready(value: Any) -> Any:
    if isinstance(value, pd.Timestamp):
        return None if pd.isna(value) else value.isoformat()
    try:
        if pd.isna(value):
            return None
    except Exception as exc:
        _record_event_evidence_fallback(
            fallback_type="event_evidence_json_ready_missing_check_failed",
            source="event_evidence_payload",
            reason="Compact event evidence could not evaluate a value for missingness while preparing JSON output and kept the original value.",
            error=exc,
            metadata={"value_type": type(value).__name__},
        )
    return value


def _records(df: pd.DataFrame, *, limit: int = 5) -> list[dict[str, Any]]:
    if df.empty:
        return []
    clean = df.tail(max(0, int(limit))).copy().astype(object).where(pd.notna(df.tail(max(0, int(limit)))), None)
    return [{key: _json_ready(value) for key, value in row.items()} for row in clean.to_dict(orient="records")]


def _value_counts(df: pd.DataFrame, column: str) -> dict[str, int]:
    if df.empty or column not in df.columns:
        return {}
    return {
        str(key): int(value)
        for key, value in df[column].dropna().astype(str).value_counts().sort_index().to_dict().items()
    }


def _truthy_series(series: pd.Series) -> pd.Series:
    return series.map(lambda value: str(value).strip().lower() in {"1", "true", "yes", "y", "on"})


def table_exists(table_name: str) -> bool:
    try:
        df = sql_to_df(
            """
            SELECT 1 AS exists_flag
            FROM information_schema.tables
            WHERE table_schema = 'public'
              AND table_name = %s
            LIMIT 1
            """,
            params=(table_name,),
            retries=2,
            statement_timeout_ms=5000,
        )
    except Exception as exc:
        _record_event_evidence_fallback(
            fallback_type="event_evidence_table_lookup_failed",
            source=table_name,
            reason="Compact event evidence could not check whether a source or target table exists.",
            error=exc,
            metadata={"table_name": table_name},
        )
        return False
    return not df.empty


def table_columns(table_name: str) -> set[str]:
    try:
        df = sql_to_df(
            """
            SELECT column_name
            FROM information_schema.columns
            WHERE table_schema = 'public'
              AND table_name = %s
            """,
            params=(table_name,),
            retries=2,
            statement_timeout_ms=5000,
        )
    except Exception as exc:
        _record_event_evidence_fallback(
            fallback_type="event_evidence_schema_lookup_failed",
            source=table_name,
            reason="Compact event evidence could not inspect source or target table columns.",
            error=exc,
            metadata={"table_name": table_name},
        )
        return set()
    return set(df["column_name"].astype(str).tolist()) if not df.empty else set()


def _max_date(table_name: str, column: str) -> pd.Timestamp | None:
    if not table_exists(table_name):
        return None
    try:
        df = sql_to_df(
            f'SELECT MAX("{column}") AS max_date FROM "{table_name}"',
            retries=2,
            statement_timeout_ms=5000,
        )
    except Exception as exc:
        _record_event_evidence_fallback(
            fallback_type="event_evidence_max_date_lookup_failed",
            source=table_name,
            reason="Compact event evidence could not read the existing target max date; full lookback fallback may be used.",
            error=exc,
            metadata={"table_name": table_name, "date_column": column},
        )
        return None
    if df.empty:
        return None
    ts = pd.to_datetime(df.iloc[0].get("max_date"), utc=True, errors="coerce")
    return None if pd.isna(ts) else ts.normalize()


def _normalize_date(value: Any) -> pd.Timestamp | None:
    if value is None:
        return None
    ts = pd.to_datetime(value, utc=True, errors="coerce")
    if pd.isna(ts):
        return None
    return ts.normalize()


def resolve_date_range(
    *,
    table_name: str,
    date_column: str,
    from_date: Any = None,
    to_date: Any = None,
    rebuild: bool = False,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
) -> tuple[pd.Timestamp, pd.Timestamp]:
    effective_to = _normalize_date(to_date) or pd.Timestamp.utcnow().normalize()
    effective_from = _normalize_date(from_date)
    if effective_from is None and not rebuild:
        max_date = _max_date(table_name, date_column)
        if max_date is not None:
            effective_from = max_date + pd.Timedelta(days=1)
    if effective_from is None:
        effective_from = effective_to - pd.Timedelta(days=max(1, int(lookback_days)))
    return effective_from, effective_to


def _classify_deal_pressure(row: pd.Series) -> str:
    net_deal = float(row.get("deal_net_value_inr") or 0.0)
    short_qty = float(row.get("short_selling_quantity") or 0.0)
    circuit = int(row.get("circuit_hit_count") or 0)
    if circuit > 0:
        return "circuit_risk"
    if net_deal > 10_000_000 and short_qty <= 0:
        return "accumulation"
    if net_deal < -10_000_000 or short_qty > 0:
        return "distribution_or_pressure"
    return "neutral"


def _score_bhavcopy_row(row: pd.Series) -> float:
    score = 0.0
    turnover = float(row.get("turnover_value_inr") or 0.0)
    avg_turnover = float(row.get("avg_turnover_value_20d") or 0.0)
    net_deal = float(row.get("deal_net_value_inr") or 0.0)
    short_qty = float(row.get("short_selling_quantity") or 0.0)
    circuit = int(row.get("circuit_hit_count") or 0)
    if avg_turnover > 0 and turnover > 1.5 * avg_turnover:
        score += 0.15
    if net_deal > 0:
        score += min(0.35, net_deal / 100_000_000.0 * 0.05)
    if net_deal < 0:
        score -= min(0.35, abs(net_deal) / 100_000_000.0 * 0.05)
    if short_qty > 0:
        score -= min(0.25, short_qty / 1_000_000.0 * 0.03)
    if circuit > 0:
        score -= 0.20
    return round(max(-1.0, min(1.0, score)), 6)


def ensure_bhavcopy_context_overlay_table() -> None:
    apply_schema_migration(
        migration_id=BHAVCOPY_CONTEXT_OVERLAY_SCHEMA_MIGRATION_ID,
        statements=BHAVCOPY_CONTEXT_OVERLAY_SCHEMA_STATEMENTS,
        owner="advisory.event_evidence_store",
        description="Create review-only bhavcopy context overlay table.",
        metadata={"tables": [BHAVCOPY_CONTEXT_OVERLAYS_TABLE], "workflow": "bhavcopy_context_overlays"},
    )


def ensure_announcement_context_overlay_table() -> None:
    apply_schema_migration(
        migration_id=ANNOUNCEMENT_CONTEXT_OVERLAY_SCHEMA_MIGRATION_ID,
        statements=ANNOUNCEMENT_CONTEXT_OVERLAY_SCHEMA_STATEMENTS,
        owner="advisory.event_evidence_store",
        description="Create review-only announcement context overlay table.",
        metadata={"tables": [ANNOUNCEMENT_CONTEXT_OVERLAYS_TABLE], "workflow": "announcement_context_overlays"},
    )
    apply_schema_migration(
        migration_id=ANNOUNCEMENT_CONTEXT_OVERLAY_TAXONOMY_SCHEMA_MIGRATION_ID,
        statements=ANNOUNCEMENT_CONTEXT_OVERLAY_TAXONOMY_SCHEMA_STATEMENTS,
        owner="advisory.event_evidence_store",
        description="Add compact announcement taxonomy contract columns to announcement context overlays.",
        metadata={"tables": [ANNOUNCEMENT_CONTEXT_OVERLAYS_TABLE], "workflow": "announcement_context_overlays"},
    )


def ensure_announcement_evidence_table() -> None:
    apply_schema_migration(
        migration_id=ANNOUNCEMENT_EVIDENCE_SCHEMA_MIGRATION_ID,
        statements=ANNOUNCEMENT_EVIDENCE_SCHEMA_STATEMENTS,
        owner="advisory.event_evidence_store",
        description="Create compact announcement evidence table and add storage/LLM-readiness taxonomy columns.",
        metadata={"tables": [ANNOUNCEMENT_EVIDENCE_TABLE], "workflow": "announcement_evidence"},
    )


def _num_or_zero(value: Any) -> float:
    numeric = pd.to_numeric(value, errors="coerce")
    if pd.isna(numeric):
        return 0.0
    return float(numeric)


def _classify_bhavcopy_overlay(row: pd.Series) -> dict[str, Any] | None:
    evidence_score = _num_or_zero(row.get("evidence_score"))
    deal_pressure = str(row.get("deal_pressure") or "").strip().lower()
    deal_net = _num_or_zero(row.get("deal_net_value_inr"))
    short_qty = _num_or_zero(row.get("short_selling_quantity"))
    circuit_hits = _num_or_zero(row.get("circuit_hit_count"))
    turnover = _num_or_zero(row.get("turnover_value_inr"))
    avg_turnover = _num_or_zero(row.get("avg_turnover_value_20d"))

    if circuit_hits > 0:
        return {
            "direction": "negative",
            "pressure_score": min(1.0, 0.55 + min(0.30, circuit_hits * 0.05)),
            "watch_reason_detail": "Bhavcopy evidence shows circuit-hit risk; treat as review-only liquidity/price-action pressure.",
        }
    if deal_pressure == "accumulation" or evidence_score >= 0.15:
        turnover_boost = 0.1 if avg_turnover > 0 and turnover >= 1.5 * avg_turnover else 0.0
        return {
            "direction": "positive",
            "pressure_score": min(1.0, max(0.35, abs(evidence_score)) + turnover_boost),
            "watch_reason_detail": "Bhavcopy evidence shows accumulation or abnormal participation; technical confirmation is still required.",
        }
    if deal_pressure == "distribution_or_pressure" or evidence_score <= -0.15 or short_qty > 0 or deal_net < -10_000_000:
        return {
            "direction": "negative",
            "pressure_score": min(1.0, max(0.35, abs(evidence_score), 0.45 if short_qty > 0 else 0.0)),
            "watch_reason_detail": "Bhavcopy evidence shows distribution, short pressure, or abnormal selling; review exposure and confirmation.",
        }
    if avg_turnover > 0 and turnover >= 2.0 * avg_turnover:
        return {
            "direction": "watch",
            "pressure_score": 0.30,
            "watch_reason_detail": "Bhavcopy evidence shows abnormal turnover without clear directional pressure; monitor for confirming news or technical trigger.",
        }
    return None


POSITIVE_ANNOUNCEMENT_CLASSES = {
    "APPROVAL",
    "BUYBACK",
    "CAPEX",
    "COMMERCIAL_PRODUCTION",
    "CONTRACT_WIN",
    "CREDIT_RATING_UPGRADE",
    "EXPANSION",
    "GUIDANCE_UPGRADE",
    "LARGE_ORDER",
    "MARGIN_EXPANSION",
    "ORDER_WIN",
    "POLICY_SECTOR_POSITIVE",
    "PROMOTER_BUYING",
    "RESULTS_POSITIVE",
}
NEGATIVE_ANNOUNCEMENT_CLASSES = {
    "AUDITOR_RESIGNATION",
    "CREDIT_RATING_DOWNGRADE",
    "DEFAULT",
    "DIRECTOR_RESIGNATION",
    "DILUTION",
    "FRAUD",
    "GUIDANCE_DOWNGRADE",
    "INSOLVENCY",
    "LITIGATION",
    "PENALTY",
    "PLEDGE",
    "POLICY_SECTOR_NEGATIVE",
    "PROMOTER_SELLING",
    "REGULATORY_NOTICE",
    "RESULTS_NEGATIVE",
}
WATCH_ANNOUNCEMENT_CLASSES = {
    "ANALYST_MEET",
    "BOARD_MEETING",
    "CHANGE_IN_MANAGEMENT",
    "DIVIDEND",
    "GENERAL_UPDATE",
    "INVESTOR_PRESENTATION",
    "OTHER",
}


def _classify_announcement_overlay(row: pd.Series) -> dict[str, Any] | None:
    event_class = str(row.get("event_class") or "").strip().upper()
    raw_direction = str(row.get("direction") or "").strip().lower()
    verdict = str(row.get("verdict") or "").strip().lower()
    materiality = _num_or_zero(row.get("materiality"))
    surprise = _num_or_zero(row.get("surprise"))
    novelty = _num_or_zero(row.get("novelty"))
    contradiction = _num_or_zero(row.get("contradiction"))
    confidence = _num_or_zero(row.get("confidence"))

    direction = ""
    if raw_direction in {"positive", "negative", "watch"}:
        direction = raw_direction
    elif event_class in POSITIVE_ANNOUNCEMENT_CLASSES:
        direction = "positive"
    elif event_class in NEGATIVE_ANNOUNCEMENT_CLASSES:
        direction = "negative"
    elif verdict in {"review_manual", "watch", "manual_review"} or event_class in WATCH_ANNOUNCEMENT_CLASSES:
        direction = "watch"

    if not direction:
        return None

    directional_weight = 0.18 if direction in {"positive", "negative"} else 0.08
    contradiction_weight = 0.12 if direction == "negative" else 0.05
    pressure = (
        0.18
        + directional_weight
        + 0.30 * max(0.0, materiality)
        + 0.18 * max(0.0, confidence)
        + 0.12 * abs(surprise)
        + 0.08 * max(0.0, novelty)
        + contradiction_weight * max(0.0, contradiction)
    )
    pressure = round(max(0.0, min(1.0, pressure)), 6)

    if direction == "watch" and pressure < 0.35 and materiality < 0.25:
        return None
    if direction in {"positive", "negative"} and confidence < 0.35 and materiality < 0.20:
        return None

    if direction == "positive":
        reason = "Company announcement creates positive symbol-level watch pressure; technical, liquidity, and risk gates still decide entry."
    elif direction == "negative":
        reason = "Company announcement creates negative symbol-level watch pressure; review exposure, thesis validity, and exit evidence."
    else:
        reason = "Company announcement is material but direction is not decisive; monitor for follow-up evidence before escalation."
    return {"direction": direction, "pressure_score": pressure, "watch_reason_detail": reason}


def build_announcement_context_overlays_from_evidence(evidence: pd.DataFrame, *, asof_date: Any = None) -> pd.DataFrame:
    if evidence.empty:
        return pd.DataFrame()
    frame = evidence.copy()
    frame["published_on"] = pd.to_datetime(frame["published_on"], utc=True, errors="coerce")
    effective_asof = _normalize_date(asof_date) or frame["published_on"].max().normalize()
    frame = frame[frame["published_on"].dt.normalize() <= effective_asof].copy()
    if "llm_review_ready" in frame.columns:
        frame = frame[_truthy_series(frame["llm_review_ready"])].copy()
    if "announcement_storage_form" in frame.columns:
        frame = frame[frame["announcement_storage_form"].astype("string").str.strip().ne("archived_raw_reference_only")].copy()
    if frame.empty:
        return pd.DataFrame()
    load_ts = pd.Timestamp.utcnow()
    rows: list[dict[str, Any]] = []
    for _, row in frame.iterrows():
        symbol = str(row.get("symbol") or "").strip().upper()
        evidence_id = str(row.get("evidence_id") or "").strip()
        published_on = pd.to_datetime(row.get("published_on"), utc=True, errors="coerce")
        if not symbol or not evidence_id or pd.isna(published_on):
            continue
        classification = _classify_announcement_overlay(row)
        if not classification:
            continue
        event_class = str(row.get("event_class") or "OTHER").strip().upper() or "OTHER"
        overlay_id = f"{published_on.strftime('%Y%m%d%H%M%S')}:{symbol}:{evidence_id}:{classification['direction']}"
        matched = {
            "source": "announcement_evidence",
            "unique_id": row.get("unique_id"),
            "subject": row.get("subject"),
            "event_class": event_class,
            "direction": classification["direction"],
            "verdict": row.get("verdict"),
            "what_happened": row.get("what_happened"),
            "rationale": row.get("rationale"),
            "source_reliability": row.get("source_reliability"),
            "attachment_name": row.get("attachment_name"),
            "attachment_url": row.get("attachment_url"),
            "announcement_storage_form": row.get("announcement_storage_form"),
            "llm_evidence_mode": row.get("llm_evidence_mode"),
            "llm_review_ready": row.get("llm_review_ready"),
            "raw_archive_required": row.get("raw_archive_required"),
        }
        rows.append(
            {
                "published_on": published_on,
                "asof_date": published_on.normalize(),
                "overlay_id": overlay_id,
                "evidence_id": evidence_id,
                "unique_id": row.get("unique_id"),
                "symbol": symbol,
                "company_master_id": row.get("company_master_id"),
                "event_class": event_class,
                "direction": classification["direction"],
                "pressure_score": classification["pressure_score"],
                "materiality": pd.to_numeric(row.get("materiality"), errors="coerce"),
                "surprise": pd.to_numeric(row.get("surprise"), errors="coerce"),
                "novelty": pd.to_numeric(row.get("novelty"), errors="coerce"),
                "contradiction": pd.to_numeric(row.get("contradiction"), errors="coerce"),
                "confidence": pd.to_numeric(row.get("confidence"), errors="coerce"),
                "verdict": row.get("verdict"),
                "source_reliability": row.get("source_reliability"),
                "announcement_storage_form": row.get("announcement_storage_form"),
                "llm_evidence_mode": row.get("llm_evidence_mode"),
                "llm_review_ready": str(row.get("llm_review_ready")).strip().lower() in {"1", "true", "yes", "y", "on"},
                "raw_archive_required": str(row.get("raw_archive_required")).strip().lower() in {"1", "true", "yes", "y", "on"},
                "watch_reason_detail": classification["watch_reason_detail"],
                "matched_sources_json": json.dumps([matched], ensure_ascii=False, sort_keys=True, default=str),
                "authority_scope": "watchlist_pressure_only",
                "production_status": "active",
                "load_ts": load_ts,
            }
        )
    if not rows:
        return pd.DataFrame()
    out = pd.DataFrame(rows)
    for col in ["published_on", "asof_date", "load_ts"]:
        out[col] = pd.to_datetime(out[col], utc=True, errors="coerce")
    for col in ["pressure_score", "materiality", "surprise", "novelty", "contradiction", "confidence"]:
        out[col] = pd.to_numeric(out[col], errors="coerce")
    return out.drop_duplicates(subset=["published_on", "overlay_id"], keep="last")


def build_bhavcopy_context_overlays_from_evidence(evidence: pd.DataFrame, *, asof_date: Any = None) -> pd.DataFrame:
    if evidence.empty:
        return pd.DataFrame()
    frame = evidence.copy()
    frame["asof_date"] = pd.to_datetime(frame["asof_date"], utc=True, errors="coerce")
    effective_asof = _normalize_date(asof_date) or frame["asof_date"].max()
    frame = frame[frame["asof_date"] <= effective_asof].copy()
    if frame.empty:
        return pd.DataFrame()
    load_ts = pd.Timestamp.utcnow()
    rows: list[dict[str, Any]] = []
    for _, row in frame.iterrows():
        symbol = str(row.get("symbol") or "").strip().upper()
        if not symbol:
            continue
        classification = _classify_bhavcopy_overlay(row)
        if not classification:
            continue
        row_asof = pd.to_datetime(row.get("asof_date"), utc=True, errors="coerce").normalize()
        overlay_id = f"{row_asof.date()}:{symbol}:{classification['direction']}:{str(row.get('deal_pressure') or 'activity').upper()}"
        matched = {
            "source": "bhavcopy_evidence",
            "deal_pressure": row.get("deal_pressure"),
            "evidence_score": _json_ready(row.get("evidence_score")),
            "deal_net_value_inr": _json_ready(row.get("deal_net_value_inr")),
            "short_selling_quantity": _json_ready(row.get("short_selling_quantity")),
            "circuit_hit_count": _json_ready(row.get("circuit_hit_count")),
            "turnover_value_inr": _json_ready(row.get("turnover_value_inr")),
            "avg_turnover_value_20d": _json_ready(row.get("avg_turnover_value_20d")),
        }
        rows.append(
            {
                "asof_date": row_asof,
                "overlay_id": overlay_id,
                "symbol": symbol,
                "company_master_id": row.get("company_master_id"),
                "direction": classification["direction"],
                "pressure_score": round(float(classification["pressure_score"]), 6),
                "evidence_score": pd.to_numeric(row.get("evidence_score"), errors="coerce"),
                "deal_pressure": row.get("deal_pressure"),
                "deal_net_value_inr": pd.to_numeric(row.get("deal_net_value_inr"), errors="coerce"),
                "short_selling_quantity": pd.to_numeric(row.get("short_selling_quantity"), errors="coerce"),
                "circuit_hit_count": pd.to_numeric(row.get("circuit_hit_count"), errors="coerce"),
                "turnover_value_inr": pd.to_numeric(row.get("turnover_value_inr"), errors="coerce"),
                "avg_turnover_value_20d": pd.to_numeric(row.get("avg_turnover_value_20d"), errors="coerce"),
                "watch_reason_detail": classification["watch_reason_detail"],
                "matched_sources_json": json.dumps([matched], ensure_ascii=False, sort_keys=True, default=str),
                "authority_scope": "watchlist_pressure_only",
                "production_status": "active",
                "load_ts": load_ts,
            }
        )
    if not rows:
        return pd.DataFrame()
    out = pd.DataFrame(rows)
    for col in ["asof_date", "load_ts"]:
        out[col] = pd.to_datetime(out[col], utc=True, errors="coerce")
    for col in [
        "pressure_score",
        "evidence_score",
        "deal_net_value_inr",
        "short_selling_quantity",
        "circuit_hit_count",
        "turnover_value_inr",
        "avg_turnover_value_20d",
    ]:
        out[col] = pd.to_numeric(out[col], errors="coerce")
    return out.drop_duplicates(subset=["asof_date", "overlay_id"], keep="last")


def build_bhavcopy_evidence(
    *,
    from_date: Any = None,
    to_date: Any = None,
    rebuild: bool = False,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
) -> pd.DataFrame:
    start_date, end_date = resolve_date_range(
        table_name=BHAVCOPY_EVIDENCE_TABLE,
        date_column="asof_date",
        from_date=from_date,
        to_date=to_date,
        rebuild=rebuild,
        lookback_days=lookback_days,
    )
    if start_date > end_date:
        return pd.DataFrame()
    warmup_start = start_date - pd.Timedelta(days=30)
    print(f"[advisory.event_evidence_store] bhavcopy start from={start_date.date()} to={end_date.date()}", flush=True)
    query = f"""
        WITH base AS (
            SELECT
                date::date AS asof_date,
                upper(symbol::text) AS symbol,
                max(company_master_id::text) AS company_master_id,
                max({_sql_num("close")}) AS close,
                sum({_sql_num("volume")}) AS volume,
                sum({_sql_num("total_value")}) AS turnover_value_inr,
                sum({_sql_num("number_of_trades")}) AS number_of_trades,
                max({_sql_num("previous_close")}) AS previous_close
            FROM nseindia_ohlcv
            WHERE date BETWEEN %(warmup_start)s AND %(end_date)s
              AND upper(coalesce(series::text, 'EQ')) = 'EQ'
            GROUP BY date::date, upper(symbol::text)
        ),
        rolling AS (
            SELECT
                *,
                avg(turnover_value_inr) OVER (PARTITION BY symbol ORDER BY asof_date ROWS BETWEEN 19 PRECEDING AND CURRENT ROW) AS avg_turnover_value_20d,
                avg(volume) OVER (PARTITION BY symbol ORDER BY asof_date ROWS BETWEEN 19 PRECEDING AND CURRENT ROW) AS avg_volume_20d,
                stddev_pop(close) OVER (PARTITION BY symbol ORDER BY asof_date ROWS BETWEEN 19 PRECEDING AND CURRENT ROW) AS close_volatility_20d
            FROM base
        ),
        cmvolt AS (
            SELECT
                date::date AS asof_date,
                upper(symbol::text) AS symbol,
                max({_sql_num("annualized_volatility")}) AS annualized_volatility,
                max({_sql_num("current_day_daily_volatility")}) AS current_day_daily_volatility
            FROM nseindia_cmvolt
            WHERE date BETWEEN %(start_date)s AND %(end_date)s
            GROUP BY date::date, upper(symbol::text)
        ),
        var_margin AS (
            SELECT
                for_date::date AS asof_date,
                upper(symbol::text) AS symbol,
                max({_sql_num("applicable_margin")}) AS applicable_margin,
                max({_sql_num("var_margin")}) AS var_margin,
                max({_sql_num("extreme_loss_rate")}) AS extreme_loss_rate
            FROM nseindia_var1
            WHERE for_date BETWEEN %(start_date)s AND %(end_date)s
            GROUP BY for_date::date, upper(symbol::text)
        ),
        deals AS (
            SELECT
                date::date AS asof_date,
                upper(symbol::text) AS symbol,
                sum(CASE WHEN upper(coalesce(buysell::text, '')) LIKE 'BUY%%' THEN {_sql_num("quantity")} * {_sql_num("price")} ELSE 0 END) AS block_buy_value_inr,
                sum(CASE WHEN upper(coalesce(buysell::text, '')) LIKE 'SELL%%' THEN {_sql_num("quantity")} * {_sql_num("price")} ELSE 0 END) AS block_sell_value_inr,
                count(*) AS block_deal_count,
                0::double precision AS bulk_buy_value_inr,
                0::double precision AS bulk_sell_value_inr,
                0::bigint AS bulk_deal_count
            FROM nseindia_block_deals
            WHERE date BETWEEN %(start_date)s AND %(end_date)s
            GROUP BY date::date, upper(symbol::text)
            UNION ALL
            SELECT
                date::date AS asof_date,
                upper(symbol::text) AS symbol,
                0::double precision AS block_buy_value_inr,
                0::double precision AS block_sell_value_inr,
                0::bigint AS block_deal_count,
                sum(CASE WHEN upper(coalesce(buysell::text, '')) LIKE 'BUY%%' THEN {_sql_num("quantity")} * {_sql_num("price")} ELSE 0 END) AS bulk_buy_value_inr,
                sum(CASE WHEN upper(coalesce(buysell::text, '')) LIKE 'SELL%%' THEN {_sql_num("quantity")} * {_sql_num("price")} ELSE 0 END) AS bulk_sell_value_inr,
                count(*) AS bulk_deal_count
            FROM nseindia_bulk_deals
            WHERE date BETWEEN %(start_date)s AND %(end_date)s
            GROUP BY date::date, upper(symbol::text)
        ),
        deal_agg AS (
            SELECT
                asof_date,
                symbol,
                sum(block_buy_value_inr) AS block_buy_value_inr,
                sum(block_sell_value_inr) AS block_sell_value_inr,
                sum(block_deal_count) AS block_deal_count,
                sum(bulk_buy_value_inr) AS bulk_buy_value_inr,
                sum(bulk_sell_value_inr) AS bulk_sell_value_inr,
                sum(bulk_deal_count) AS bulk_deal_count
            FROM deals
            GROUP BY asof_date, symbol
        ),
        shorts AS (
            SELECT
                date::date AS asof_date,
                upper(symbol::text) AS symbol,
                sum({_sql_num("quantity")}) AS short_selling_quantity,
                count(*) AS short_selling_count
            FROM nseindia_short_selling
            WHERE date BETWEEN %(start_date)s AND %(end_date)s
            GROUP BY date::date, upper(symbol::text)
        ),
        circuits AS (
            SELECT
                date::date AS asof_date,
                upper(symbol::text) AS symbol,
                count(*) AS circuit_hit_count,
                string_agg(DISTINCT circuit_hit::text, ', ' ORDER BY circuit_hit::text) AS circuit_hit_types
            FROM nseindia_circuit_hit
            WHERE date BETWEEN %(start_date)s AND %(end_date)s
            GROUP BY date::date, upper(symbol::text)
        )
        SELECT
            r.asof_date,
            r.symbol,
            r.company_master_id,
            r.close,
            r.previous_close,
            CASE WHEN r.previous_close IS NULL OR r.previous_close = 0 THEN NULL ELSE (r.close / r.previous_close) - 1 END AS daily_return,
            r.volume,
            r.avg_volume_20d,
            r.turnover_value_inr,
            r.avg_turnover_value_20d,
            r.number_of_trades,
            r.close_volatility_20d,
            c.annualized_volatility,
            c.current_day_daily_volatility,
            v.applicable_margin,
            v.var_margin,
            v.extreme_loss_rate,
            coalesce(d.block_buy_value_inr, 0) AS block_buy_value_inr,
            coalesce(d.block_sell_value_inr, 0) AS block_sell_value_inr,
            coalesce(d.block_deal_count, 0) AS block_deal_count,
            coalesce(d.bulk_buy_value_inr, 0) AS bulk_buy_value_inr,
            coalesce(d.bulk_sell_value_inr, 0) AS bulk_sell_value_inr,
            coalesce(d.bulk_deal_count, 0) AS bulk_deal_count,
            coalesce(d.block_buy_value_inr, 0) + coalesce(d.bulk_buy_value_inr, 0)
              - coalesce(d.block_sell_value_inr, 0) - coalesce(d.bulk_sell_value_inr, 0) AS deal_net_value_inr,
            coalesce(s.short_selling_quantity, 0) AS short_selling_quantity,
            coalesce(s.short_selling_count, 0) AS short_selling_count,
            coalesce(ci.circuit_hit_count, 0) AS circuit_hit_count,
            ci.circuit_hit_types
        FROM rolling r
        LEFT JOIN cmvolt c ON c.asof_date = r.asof_date AND c.symbol = r.symbol
        LEFT JOIN var_margin v ON v.asof_date = r.asof_date AND v.symbol = r.symbol
        LEFT JOIN deal_agg d ON d.asof_date = r.asof_date AND d.symbol = r.symbol
        LEFT JOIN shorts s ON s.asof_date = r.asof_date AND s.symbol = r.symbol
        LEFT JOIN circuits ci ON ci.asof_date = r.asof_date AND ci.symbol = r.symbol
        WHERE r.asof_date BETWEEN %(start_date)s AND %(end_date)s
        ORDER BY r.asof_date, r.symbol
    """
    try:
        df = sql_to_df(
            query,
            params={"warmup_start": warmup_start.date(), "start_date": start_date.date(), "end_date": end_date.date()},
            retries=4,
            statement_timeout_ms=0,
            chunksize=50000,
        )
    except Exception as exc:
        _record_event_evidence_fallback(
            fallback_type="event_evidence_bhavcopy_source_load_failed",
            source="nseindia_ohlcv,nseindia_cmvolt,nseindia_var1,nseindia_block_deals,nseindia_bulk_deals,nseindia_short_selling,nseindia_circuit_hit",
            reason="Compact bhavcopy evidence source query failed; evidence store refresh cannot safely continue for bhavcopy.",
            error=exc,
            metadata={"start_date": start_date.date().isoformat(), "end_date": end_date.date().isoformat(), "warmup_start": warmup_start.date().isoformat()},
        )
        raise
    if df.empty:
        return df
    numeric_cols = [col for col in df.columns if col not in {"asof_date", "symbol", "company_master_id", "circuit_hit_types"}]
    for col in numeric_cols:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df["asof_date"] = pd.to_datetime(df["asof_date"], utc=True, errors="coerce").dt.normalize()
    df["symbol"] = df["symbol"].astype("string").str.upper()
    df["deal_pressure"] = df.apply(_classify_deal_pressure, axis=1)
    df["evidence_score"] = df.apply(_score_bhavcopy_row, axis=1)
    df["evidence_summary"] = df.apply(
        lambda row: (
            f"turnover={float(row.get('turnover_value_inr') or 0):.0f}; "
            f"deal_net={float(row.get('deal_net_value_inr') or 0):.0f}; "
            f"short_qty={float(row.get('short_selling_quantity') or 0):.0f}; "
            f"circuit_hits={int(row.get('circuit_hit_count') or 0)}; "
            f"pressure={row.get('deal_pressure')}"
        ),
        axis=1,
    )
    df["load_ts"] = pd.Timestamp.utcnow()
    return df.drop_duplicates(subset=["asof_date", "symbol"], keep="last")


def _evidence_id(unique_id: Any, published_on: Any) -> str:
    payload = f"{unique_id}|{published_on}"
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()


def _text_len(value: Any) -> int:
    text = str(value or "").strip()
    return 0 if text.lower() in {"none", "nan", "nat"} else len(text)


def classify_announcement_storage_form(row: pd.Series) -> dict[str, Any]:
    event_class = str(row.get("event_class") or "").strip().upper()
    direction = str(row.get("direction") or "").strip().lower()
    verdict = str(row.get("verdict") or "").strip().lower()
    has_event_tensor = any(
        [
            event_class,
            direction in {"positive", "negative", "neutral", "watch"},
            verdict,
            _text_len(row.get("event_tensor_json")) > 10,
        ]
    )
    summary_len = max(
        _text_len(row.get("what_happened")),
        _text_len(row.get("rationale")),
        _text_len(row.get("evidence_summary")),
        _text_len(row.get("evidence_excerpt")),
        _text_len(row.get("subject")),
    )
    has_compact_summary = summary_len >= 30
    has_archive_pointer = bool(row.get("attachment_url")) or bool(row.get("has_s3_evidence"))

    if has_event_tensor and has_compact_summary:
        return {
            "announcement_storage_form": "structured_event_plus_summary",
            "announcement_storage_reason": "LLM/event tensor and compact text summary are available for review-only decision support.",
            "llm_evidence_mode": "event_tensor_plus_summary",
            "llm_review_ready": True,
            "raw_archive_required": bool(has_archive_pointer),
        }
    if has_event_tensor:
        return {
            "announcement_storage_form": "compact_structured_event",
            "announcement_storage_reason": "Structured event fields are available; use compact tensor first and fetch raw archive only for audit.",
            "llm_evidence_mode": "event_tensor_only",
            "llm_review_ready": True,
            "raw_archive_required": bool(has_archive_pointer),
        }
    if has_compact_summary:
        return {
            "announcement_storage_form": "structured_event_plus_summary",
            "announcement_storage_reason": "Compact text is available but structured event fields are missing; LLM review should extract a tensor before policy use.",
            "llm_evidence_mode": "summary_requires_event_extraction",
            "llm_review_ready": True,
            "raw_archive_required": bool(has_archive_pointer),
        }
    return {
        "announcement_storage_form": "archived_raw_reference_only",
        "announcement_storage_reason": "No reliable compact tensor or summary is available; keep metadata/raw pointers only and do not use for decisioning until extracted.",
        "llm_evidence_mode": "archive_reference_only",
        "llm_review_ready": False,
        "raw_archive_required": True,
    }


def build_announcement_evidence(
    *,
    from_date: Any = None,
    to_date: Any = None,
    rebuild: bool = False,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
) -> pd.DataFrame:
    start_date, end_date = resolve_date_range(
        table_name=ANNOUNCEMENT_EVIDENCE_TABLE,
        date_column="published_on",
        from_date=from_date,
        to_date=to_date,
        rebuild=rebuild,
        lookback_days=lookback_days,
    )
    if start_date > end_date:
        return pd.DataFrame()
    print(f"[advisory.event_evidence_store] announcements start from={start_date.date()} to={end_date.date()}", flush=True)
    has_evaluations = table_exists("advisory_event_evaluations")
    eval_join = ""
    eval_columns = """
        NULL::text AS event_source,
        NULL::text AS event_class,
        NULL::text AS direction,
        NULL::double precision AS materiality,
        NULL::double precision AS surprise,
        NULL::double precision AS novelty,
        NULL::double precision AS contradiction,
        NULL::double precision AS confidence,
        NULL::text AS verdict,
        NULL::text AS what_happened,
        NULL::text AS rationale,
        NULL::text AS event_tensor_json,
        NULL::text AS prompt_version
    """
    if has_evaluations:
        eval_join = """
            LEFT JOIN (
                SELECT DISTINCT ON (unique_id)
                    unique_id,
                    event_source,
                    event_class,
                    direction,
                    materiality,
                    surprise,
                    novelty,
                    contradiction,
                    confidence,
                    verdict,
                    what_happened,
                    rationale,
                    event_tensor_json,
                    prompt_version,
                    evaluated_at
                FROM advisory_event_evaluations
                WHERE published_on BETWEEN %(start_date)s AND (%(end_date)s::date + interval '1 day')
                ORDER BY unique_id, evaluated_at DESC NULLS LAST, load_ts DESC NULLS LAST
            ) e ON e.unique_id = d.unique_id
        """
        eval_columns = """
            e.event_source,
            e.event_class,
            e.direction,
            e.materiality,
            e.surprise,
            e.novelty,
            e.contradiction,
            e.confidence,
            e.verdict,
            e.what_happened,
            e.rationale,
            e.event_tensor_json,
            e.prompt_version
        """
    query = f"""
        SELECT
            d.unique_id,
            upper(d.ticker::text) AS symbol,
            d.company_master_id,
            d.exchange,
            d.company_name,
            d.subject,
            d.filed_under_category,
            d.published_on,
            d.exchange_published_on,
            d.attachment_url,
            d.attachment_name,
            d.parse_status,
            d.ocr_status,
            d.pdf_status,
            d.last_error,
            d.raw_s3_key,
            d.pdf_s3_key,
            d.ocr_s3_key,
            d.full_ocr_s3_key,
            d.audio_transcript_s3_key,
            d.concise_summary_s3_key,
            d.number_of_pages,
            coalesce({_sql_num("d.concise_summary_chars")}, {_sql_num("d.full_ocr_chars")}, {_sql_num("d.ocr_chars")}, 0) AS evidence_chars,
            left(coalesce(d.concise_summary_text, d.concise_summary_excerpt, d.full_ocr_excerpt, d.ocr_excerpt, d.text, ''), 2000) AS evidence_excerpt,
            {eval_columns}
        FROM announcement_pipeline_documents d
        {eval_join}
        WHERE d.published_on BETWEEN %(start_date)s AND (%(end_date)s::date + interval '1 day')
        ORDER BY d.published_on, d.ticker, d.unique_id
    """
    try:
        df = sql_to_df(
            query,
            params={"start_date": start_date.date(), "end_date": end_date.date()},
            retries=4,
            statement_timeout_ms=0,
            chunksize=20000,
        )
    except Exception as exc:
        _record_event_evidence_fallback(
            fallback_type="event_evidence_announcement_source_load_failed",
            source="announcement_pipeline_documents,advisory_event_evaluations",
            reason="Compact announcement evidence source query failed; evidence store refresh cannot safely continue for announcements.",
            error=exc,
            metadata={"start_date": start_date.date().isoformat(), "end_date": end_date.date().isoformat(), "has_evaluations": bool(has_evaluations)},
        )
        raise
    if df.empty:
        return df
    df["published_on"] = pd.to_datetime(df["published_on"], utc=True, errors="coerce")
    df["asof_date"] = df["published_on"].dt.normalize()
    df["symbol"] = df["symbol"].astype("string").str.upper()
    for col in ["materiality", "surprise", "novelty", "contradiction", "confidence", "evidence_chars"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    df["has_text_evidence"] = df["evidence_excerpt"].astype("string").str.strip().ne("")
    df["has_s3_evidence"] = df[["raw_s3_key", "pdf_s3_key", "ocr_s3_key", "full_ocr_s3_key", "audio_transcript_s3_key", "concise_summary_s3_key"]].notna().any(axis=1)
    df["evidence_id"] = [_evidence_id(row.get("unique_id"), row.get("published_on")) for row in df.to_dict(orient="records")]
    df["evidence_summary"] = df.apply(
        lambda row: str(row.get("what_happened") or row.get("evidence_excerpt") or row.get("subject") or "")[:500],
        axis=1,
    )
    storage_contracts = df.apply(classify_announcement_storage_form, axis=1)
    storage_df = pd.DataFrame(storage_contracts.tolist(), index=df.index)
    for col in storage_df.columns:
        df[col] = storage_df[col]
    df["source_reliability"] = df.apply(
        lambda row: "high" if str(row.get("parse_status") or "").lower() in {"completed", "parsed", "success", "ok"} or bool(row.get("has_s3_evidence")) else "low",
        axis=1,
    )
    df["load_ts"] = pd.Timestamp.utcnow()
    return df.drop_duplicates(subset=["evidence_id"], keep="last")


def persist_bhavcopy_evidence(df: pd.DataFrame) -> None:
    if df.empty:
        return
    upsert_to_db(df, BHAVCOPY_EVIDENCE_TABLE, unique_keys=["asof_date", "symbol"], timescaledb_column="asof_date")


def persist_announcement_evidence(df: pd.DataFrame) -> None:
    ensure_announcement_evidence_table()
    if df.empty:
        return
    upsert_to_db(df, ANNOUNCEMENT_EVIDENCE_TABLE, unique_keys=["published_on", "evidence_id"], timescaledb_column="published_on")


def backfill_announcement_evidence_storage_contract(*, limit: int = 5000) -> int:
    ensure_announcement_evidence_table()
    try:
        df = sql_to_df(
            f"""
            SELECT
                published_on,
                evidence_id,
                subject,
                attachment_url,
                raw_s3_key,
                pdf_s3_key,
                ocr_s3_key,
                full_ocr_s3_key,
                audio_transcript_s3_key,
                concise_summary_s3_key,
                evidence_excerpt,
                evidence_summary,
                event_class,
                direction,
                verdict,
                what_happened,
                rationale,
                event_tensor_json,
                has_s3_evidence
            FROM {ANNOUNCEMENT_EVIDENCE_TABLE}
            WHERE announcement_storage_form IS NULL
               OR llm_evidence_mode IS NULL
               OR llm_review_ready IS NULL
               OR raw_archive_required IS NULL
            ORDER BY published_on DESC NULLS LAST, evidence_id
            LIMIT %(limit)s
            """,
            params={"limit": max(1, int(limit))},
            retries=3,
            statement_timeout_ms=30000,
        )
    except Exception as exc:
        _record_event_evidence_fallback(
            fallback_type="event_evidence_announcement_taxonomy_backfill_load_failed",
            source=ANNOUNCEMENT_EVIDENCE_TABLE,
            reason="Compact announcement evidence taxonomy backfill could not load rows with missing storage contract fields.",
            error=exc,
            metadata={"limit": int(limit)},
        )
        raise
    if df.empty:
        return 0
    contracts = df.apply(classify_announcement_storage_form, axis=1)
    contract_df = pd.DataFrame(contracts.tolist(), index=df.index)
    out = pd.concat([df[["published_on", "evidence_id"]].reset_index(drop=True), contract_df.reset_index(drop=True)], axis=1)
    out["load_ts"] = pd.Timestamp.utcnow()
    upsert_to_db(out, ANNOUNCEMENT_EVIDENCE_TABLE, unique_keys=["published_on", "evidence_id"], timescaledb_column="published_on")
    return int(len(out))


def persist_bhavcopy_context_overlays(df: pd.DataFrame) -> None:
    ensure_bhavcopy_context_overlay_table()
    if df.empty:
        return
    upsert_to_db(df, BHAVCOPY_CONTEXT_OVERLAYS_TABLE, unique_keys=["asof_date", "overlay_id"], timescaledb_column="asof_date")


def persist_announcement_context_overlays(df: pd.DataFrame) -> None:
    ensure_announcement_context_overlay_table()
    if df.empty:
        return
    upsert_to_db(df, ANNOUNCEMENT_CONTEXT_OVERLAYS_TABLE, unique_keys=["published_on", "overlay_id"], timescaledb_column="published_on")


def build_event_evidence_store(
    *,
    from_date: Any = None,
    to_date: Any = None,
    rebuild: bool = False,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
    include_bhavcopy: bool = True,
    include_announcements: bool = True,
    dry_run: bool = False,
) -> dict[str, Any]:
    bhavcopy = pd.DataFrame()
    bhavcopy_overlays = pd.DataFrame()
    announcements = pd.DataFrame()
    announcement_overlays = pd.DataFrame()
    if include_bhavcopy:
        bhavcopy = build_bhavcopy_evidence(from_date=from_date, to_date=to_date, rebuild=rebuild, lookback_days=lookback_days)
        bhavcopy_overlays = build_bhavcopy_context_overlays_from_evidence(bhavcopy, asof_date=to_date)
        if not dry_run:
            persist_bhavcopy_evidence(bhavcopy)
            persist_bhavcopy_context_overlays(bhavcopy_overlays)
    if include_announcements:
        if not dry_run:
            ensure_announcement_evidence_table()
            backfill_announcement_evidence_storage_contract()
        announcements = build_announcement_evidence(from_date=from_date, to_date=to_date, rebuild=rebuild, lookback_days=lookback_days)
        announcement_overlays = build_announcement_context_overlays_from_evidence(announcements, asof_date=to_date)
        if not dry_run:
            persist_announcement_evidence(announcements)
            persist_announcement_context_overlays(announcement_overlays)
    return {
        "status": "ok",
        "dry_run": bool(dry_run),
        "tables": {
            "bhavcopy": BHAVCOPY_EVIDENCE_TABLE,
            "bhavcopy_context_overlays": BHAVCOPY_CONTEXT_OVERLAYS_TABLE,
            "announcements": ANNOUNCEMENT_EVIDENCE_TABLE,
            "announcement_context_overlays": ANNOUNCEMENT_CONTEXT_OVERLAYS_TABLE,
        },
        "bhavcopy": {
            "rows": int(len(bhavcopy)),
            "context_overlay_rows": int(len(bhavcopy_overlays)),
            "symbols": 0 if bhavcopy.empty else int(bhavcopy["symbol"].nunique()),
            "date_min": None if bhavcopy.empty else _json_ready(bhavcopy["asof_date"].min()),
            "date_max": None if bhavcopy.empty else _json_ready(bhavcopy["asof_date"].max()),
            "sample": _records(bhavcopy),
            "overlay_sample": _records(bhavcopy_overlays),
        },
        "announcements": {
            "rows": int(len(announcements)),
            "context_overlay_rows": int(len(announcement_overlays)),
            "symbols": 0 if announcements.empty else int(announcements["symbol"].nunique()),
            "date_min": None if announcements.empty else _json_ready(announcements["published_on"].min()),
            "date_max": None if announcements.empty else _json_ready(announcements["published_on"].max()),
            "storage_form_counts": _value_counts(announcements, "announcement_storage_form"),
            "llm_evidence_mode_counts": _value_counts(announcements, "llm_evidence_mode"),
            "sample": _records(announcements),
            "overlay_sample": _records(announcement_overlays),
        },
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build compact event evidence tables from bhavcopy and announcement sources.")
    parser.add_argument("--from-date", type=parse_datetime_arg)
    parser.add_argument("--to-date", type=parse_datetime_arg)
    parser.add_argument("--rebuild", action="store_true")
    parser.add_argument("--lookback-days", type=int, default=DEFAULT_LOOKBACK_DAYS)
    parser.add_argument("--skip-bhavcopy", action="store_true")
    parser.add_argument("--skip-announcements", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def main() -> int:
    global STOCKEY_RUN_STATE
    args = parse_args()
    payload = build_event_evidence_store(
        from_date=args.from_date,
        to_date=args.to_date,
        rebuild=bool(args.rebuild),
        lookback_days=max(1, int(args.lookback_days)),
        include_bhavcopy=not bool(args.skip_bhavcopy),
        include_announcements=not bool(args.skip_announcements),
        dry_run=bool(args.dry_run),
    )
    bhavcopy = payload.get("bhavcopy") if isinstance(payload.get("bhavcopy"), dict) else {}
    announcements = payload.get("announcements") if isinstance(payload.get("announcements"), dict) else {}
    STOCKEY_RUN_STATE = {
        "from_date": min([value for value in [bhavcopy.get("date_min"), announcements.get("date_min")] if value], default=None),
        "to_date": max([value for value in [bhavcopy.get("date_max"), announcements.get("date_max")] if value], default=None),
        "rows": int(bhavcopy.get("rows") or 0) + int(announcements.get("rows") or 0),
        "rows_written": (
            0
            if payload.get("dry_run")
            else int(bhavcopy.get("rows") or 0)
            + int(bhavcopy.get("context_overlay_rows") or 0)
            + int(announcements.get("rows") or 0)
            + int(announcements.get("context_overlay_rows") or 0)
        ),
        "rows_read": int(bhavcopy.get("rows") or 0) + int(announcements.get("rows") or 0),
        "fallback_used": False,
        "bhavcopy_rows": int(bhavcopy.get("rows") or 0),
        "bhavcopy_context_overlay_rows": int(bhavcopy.get("context_overlay_rows") or 0),
        "announcement_rows": int(announcements.get("rows") or 0),
        "announcement_context_overlay_rows": int(announcements.get("context_overlay_rows") or 0),
        "dry_run": bool(payload.get("dry_run")),
    }
    print(json.dumps(payload, indent=2, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    _exit_code = main()
    if _exit_code:
        raise SystemExit(_exit_code)
