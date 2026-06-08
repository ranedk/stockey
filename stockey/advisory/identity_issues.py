from __future__ import annotations

import json
from typing import Any

import pandas as pd

from utils.db import db_session, sql_to_df


IDENTITY_ISSUES_TABLE = "advisory_identity_issues"


def _text(value: Any) -> str | None:
    try:
        if pd.isna(value):
            return None
    except Exception:
        pass
    text = str(value).strip()
    return text or None


def _json_dumps(value: Any) -> str:
    return json.dumps(value, sort_keys=True, default=str, ensure_ascii=False)


def ensure_identity_issues_table() -> None:
    with db_session() as (_, cur):
        cur.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {IDENTITY_ISSUES_TABLE} (
                issue_key TEXT PRIMARY KEY,
                first_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                last_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                status TEXT NOT NULL DEFAULT 'open',
                issue_type TEXT NOT NULL,
                symbol TEXT,
                requested_exchange TEXT,
                asset_type TEXT,
                company_master_id TEXT,
                source TEXT,
                error_text TEXT,
                exchanges_tried_json TEXT,
                fallback_tried_json TEXT,
                suggested_action TEXT,
                context_json TEXT,
                resolved_at TIMESTAMPTZ,
                load_ts TIMESTAMPTZ NOT NULL DEFAULT now()
            )
            """
        )
        cur.execute(f"ALTER TABLE {IDENTITY_ISSUES_TABLE} ADD COLUMN IF NOT EXISTS source TEXT")
        cur.execute(f"ALTER TABLE {IDENTITY_ISSUES_TABLE} ADD COLUMN IF NOT EXISTS resolved_at TIMESTAMPTZ")
        cur.execute(
            f"""
            CREATE INDEX IF NOT EXISTS idx_{IDENTITY_ISSUES_TABLE}_open
                ON {IDENTITY_ISSUES_TABLE} (status, last_seen_at DESC)
            """
        )


def record_dhan_identity_issue(
    *,
    symbol: str,
    requested_exchange: str,
    asset_type: str = "stock",
    company: pd.Series | dict[str, Any] | None = None,
    fallback_tried: list[dict[str, Any]] | None = None,
    source: str = "resolve_dhan_identity",
    error_text: str | None = None,
) -> dict[str, Any]:
    symbol_text = (_text(symbol) or "").upper()
    exchange_text = (_text(requested_exchange) or "").upper()
    asset_type_text = (_text(asset_type) or "stock").lower()
    company_payload = dict(company) if company is not None else {}
    company_master_id = _text(company_payload.get("company_master_id"))
    exchanges_tried = [exchange_text]
    for fallback in fallback_tried or []:
        fallback_exchange = (_text(fallback.get("exchange")) or "").upper()
        if fallback_exchange and fallback_exchange not in exchanges_tried:
            exchanges_tried.append(fallback_exchange)
    issue_key = f"dhan_security_id_missing:{asset_type_text}:{exchange_text}:{symbol_text}"
    suggested_action = (
        f"Refresh Dhan scrip master and company master, then verify {exchange_text}:{symbol_text} mapping. "
        f"Run `python -m data.dhanlive.scrip_master` and the company master sync before rerunning the failed downloader/advisory step."
    )
    row = {
        "issue_key": issue_key,
        "issue_type": "dhan_security_id_missing",
        "symbol": symbol_text,
        "requested_exchange": exchange_text,
        "asset_type": asset_type_text,
        "company_master_id": company_master_id,
        "source": source,
        "error_text": error_text or f"No Dhan security id mapped for {exchange_text}:{symbol_text}",
        "exchanges_tried_json": _json_dumps(exchanges_tried),
        "fallback_tried_json": _json_dumps(fallback_tried or []),
        "suggested_action": suggested_action,
        "context_json": _json_dumps({"company_master": company_payload}),
    }
    ensure_identity_issues_table()
    with db_session() as (_, cur):
        cur.execute(
            f"""
            INSERT INTO {IDENTITY_ISSUES_TABLE} (
                issue_key,
                first_seen_at,
                last_seen_at,
                status,
                issue_type,
                symbol,
                requested_exchange,
                asset_type,
                company_master_id,
                source,
                error_text,
                exchanges_tried_json,
                fallback_tried_json,
                suggested_action,
                context_json,
                resolved_at,
                load_ts
            )
            VALUES (
                %(issue_key)s,
                now(),
                now(),
                'open',
                %(issue_type)s,
                %(symbol)s,
                %(requested_exchange)s,
                %(asset_type)s,
                %(company_master_id)s,
                %(source)s,
                %(error_text)s,
                %(exchanges_tried_json)s,
                %(fallback_tried_json)s,
                %(suggested_action)s,
                %(context_json)s,
                NULL,
                now()
            )
            ON CONFLICT (issue_key) DO UPDATE SET
                last_seen_at = EXCLUDED.last_seen_at,
                status = 'open',
                symbol = EXCLUDED.symbol,
                requested_exchange = EXCLUDED.requested_exchange,
                asset_type = EXCLUDED.asset_type,
                company_master_id = EXCLUDED.company_master_id,
                source = EXCLUDED.source,
                error_text = EXCLUDED.error_text,
                exchanges_tried_json = EXCLUDED.exchanges_tried_json,
                fallback_tried_json = EXCLUDED.fallback_tried_json,
                suggested_action = EXCLUDED.suggested_action,
                context_json = EXCLUDED.context_json,
                resolved_at = NULL,
                load_ts = EXCLUDED.load_ts
            """,
            row,
        )
    return row


def load_open_identity_issues(*, limit: int = 100) -> pd.DataFrame:
    ensure_identity_issues_table()
    return sql_to_df(
        f"""
        SELECT *
        FROM {IDENTITY_ISSUES_TABLE}
        WHERE COALESCE(status, 'open') IN ('open', 'active')
        ORDER BY last_seen_at DESC NULLS LAST, first_seen_at DESC NULLS LAST
        LIMIT %s
        """,
        params=(max(1, int(limit)),),
        retries=3,
    )
