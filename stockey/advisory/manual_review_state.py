from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import pandas as pd
from environs import Env

from advisory.wait_signals import WAIT_SIGNALS_TABLE, build_event_condition, infer_event_condition_type, make_signal_id, persist_wait_signals


env = Env()
env.read_env()

MANUAL_REVIEW_DECISIONS_TABLE = "advisory_manual_review_decisions"
MANUAL_REVIEW_WAIT_SIGNAL_DAYS = env.int("MANUAL_REVIEW_WAIT_SIGNAL_DAYS", 30)

CLOSING_DECISIONS = {"approve_for_manual_config", "ignore", "downgrade_to_no_action", "mark_fixed"}
ANNOTATION_DECISIONS = {"needs_more_data", "add_operator_note"}
WAITING_DECISIONS = {"watch_for_event"}
ALLOWED_DECISIONS = CLOSING_DECISIONS | ANNOTATION_DECISIONS | WAITING_DECISIONS


@dataclass(frozen=True)
class ManualReviewDecisionEffect:
    decision: str
    next_state: str
    closes_item: bool
    creates_wait_signal: bool
    mutates_portfolio: bool
    mutates_action_recommendation: bool
    submits_order: bool
    description: str


@dataclass(frozen=True)
class ManualReviewRuntimeState:
    state: str
    active: bool
    closes_item: bool
    reopened_by_wait_signal: bool
    suppression_reason: str | None = None


DECISION_EFFECTS: dict[str, ManualReviewDecisionEffect] = {
    "needs_more_data": ManualReviewDecisionEffect(
        decision="needs_more_data",
        next_state="open_needs_more_data",
        closes_item=False,
        creates_wait_signal=False,
        mutates_portfolio=False,
        mutates_action_recommendation=False,
        submits_order=False,
        description="Annotates the review item and keeps it open until more evidence is available.",
    ),
    "watch_for_event": ManualReviewDecisionEffect(
        decision="watch_for_event",
        next_state="waiting_for_event",
        closes_item=False,
        creates_wait_signal=True,
        mutates_portfolio=False,
        mutates_action_recommendation=False,
        submits_order=False,
        description="Annotates the item and creates an active wait signal from the follow-up event text.",
    ),
    "add_operator_note": ManualReviewDecisionEffect(
        decision="add_operator_note",
        next_state="annotated",
        closes_item=False,
        creates_wait_signal=False,
        mutates_portfolio=False,
        mutates_action_recommendation=False,
        submits_order=False,
        description="Adds operator context only; it does not close the item.",
    ),
    "approve_for_manual_config": ManualReviewDecisionEffect(
        decision="approve_for_manual_config",
        next_state="closed_approved_for_manual_config",
        closes_item=True,
        creates_wait_signal=False,
        mutates_portfolio=False,
        mutates_action_recommendation=False,
        submits_order=False,
        description="Closes the item as approved for a later manual code/config change. No config is changed automatically.",
    ),
    "ignore": ManualReviewDecisionEffect(
        decision="ignore",
        next_state="closed_ignored",
        closes_item=True,
        creates_wait_signal=False,
        mutates_portfolio=False,
        mutates_action_recommendation=False,
        submits_order=False,
        description="Closes the item as noise or not worth further review.",
    ),
    "downgrade_to_no_action": ManualReviewDecisionEffect(
        decision="downgrade_to_no_action",
        next_state="closed_no_action",
        closes_item=True,
        creates_wait_signal=False,
        mutates_portfolio=False,
        mutates_action_recommendation=False,
        submits_order=False,
        description="Closes the item and records an explicit no-action operator decision. It does not change portfolio/actions.",
    ),
    "mark_fixed": ManualReviewDecisionEffect(
        decision="mark_fixed",
        next_state="closed_fixed",
        closes_item=True,
        creates_wait_signal=False,
        mutates_portfolio=False,
        mutates_action_recommendation=False,
        submits_order=False,
        description="Closes an operational issue after it has been fixed or a rerun succeeded.",
    ),
}


def json_ready(value: Any) -> Any:
    if isinstance(value, pd.Timestamp):
        return None if pd.isna(value) else value.isoformat()
    if isinstance(value, dict):
        return {str(k): json_ready(v) for k, v in value.items()}
    if isinstance(value, list):
        return [json_ready(v) for v in value]
    try:
        if pd.isna(value):
            return None
    except Exception:
        pass
    return value


def text(value: Any, default: str | None = None) -> str | None:
    if value is None:
        return default
    try:
        if pd.isna(value):
            return default
    except Exception:
        pass
    out = str(value).strip()
    if out.lower() in {"", "nan", "none", "null", "<na>"}:
        return default
    return out


def validate_decision(decision: str) -> str:
    normalized = str(decision or "").strip().lower()
    if normalized not in ALLOWED_DECISIONS:
        raise ValueError(f"decision must be one of: {', '.join(sorted(ALLOWED_DECISIONS))}")
    return normalized


def decision_effect(decision: str) -> ManualReviewDecisionEffect:
    return DECISION_EFFECTS[validate_decision(decision)]


def decision_closes_item(decision: str) -> bool:
    return decision_effect(decision).closes_item


def runtime_state_for_decision(decision: str, *, has_matched_wait_signal: bool = False) -> ManualReviewRuntimeState:
    effect = decision_effect(decision)
    if effect.closes_item:
        return ManualReviewRuntimeState(
            state=effect.next_state,
            active=False,
            closes_item=True,
            reopened_by_wait_signal=False,
            suppression_reason="closed_by_operator",
        )
    if effect.creates_wait_signal and has_matched_wait_signal:
        return ManualReviewRuntimeState(
            state="reopened_wait_signal_matched",
            active=False,
            closes_item=False,
            reopened_by_wait_signal=True,
            suppression_reason="reopened_as_wait_signal_followup",
        )
    return ManualReviewRuntimeState(
        state=effect.next_state,
        active=True,
        closes_item=False,
        reopened_by_wait_signal=False,
    )


def _manual_wait_signal_keywords(value: str) -> list[str]:
    stopwords = {
        "and",
        "the",
        "for",
        "with",
        "from",
        "that",
        "this",
        "then",
        "than",
        "company",
        "stock",
        "price",
        "event",
        "wait",
        "follow",
        "follow-up",
    }
    raw_text = str(value or "")
    parts = [part.strip().lower() for part in raw_text.replace("\n", ";").split(";") if part.strip()]
    words = [
        "".join(char for char in word.lower() if char.isalnum() or char in {"-", "_"}).strip("-_")
        for word in raw_text.replace("/", " ").replace(",", " ").replace(";", " ").split()
    ]
    keywords = [part for part in parts if len(part) >= 4]
    keywords.extend(word for word in words if len(word) >= 5 and word not in stopwords)
    out: list[str] = []
    seen: set[str] = set()
    for keyword in keywords:
        normalized = " ".join(keyword.split())
        if normalized and normalized not in seen:
            out.append(normalized)
            seen.add(normalized)
    return out[:16]


def build_decision_row(
    *,
    item_id: str,
    item: dict[str, Any],
    decision: str,
    rationale: str | None,
    follow_up_event: str | None,
    operator_id: str | None,
    decided_at: pd.Timestamp,
    note: dict[str, Any] | None = None,
) -> pd.DataFrame:
    normalized_decision = validate_decision(decision)
    note_payload = dict(note or {})
    if follow_up_event is not None:
        note_payload["follow_up_event"] = text(follow_up_event)
    row = {
        "decided_at": decided_at,
        "item_id": item_id,
        "item_type": text(item.get("item_type")),
        "source_table": text(item.get("source_table")),
        "source_key": text(item.get("source_key")),
        "symbol": text(item.get("symbol")),
        "unique_id": text(item.get("unique_id")),
        "setup_id": text(item.get("setup_id")),
        "decision": normalized_decision,
        "operator_id": text(operator_id),
        "rationale": rationale,
        "follow_up_event": text(follow_up_event),
        "note_json": json.dumps(json_ready(note_payload), ensure_ascii=False, sort_keys=True, default=str),
        "item_snapshot_json": json.dumps(json_ready(item), ensure_ascii=False, sort_keys=True, default=str),
        "load_ts": decided_at,
    }
    return pd.DataFrame([row])


def build_wait_signal_for_decision(
    *,
    item_id: str,
    item: dict[str, Any],
    decision: str,
    rationale: str | None,
    follow_up_event: str | None,
    decided_at: pd.Timestamp,
    operator_id: str | None,
) -> dict[str, Any] | None:
    effect = decision_effect(decision)
    if not effect.creates_wait_signal:
        return None
    wait_text = text(follow_up_event)
    if not wait_text:
        return None
    symbol = (text(item.get("symbol")) or "").upper() or None
    valid_until = decided_at + pd.Timedelta(days=max(1, int(MANUAL_REVIEW_WAIT_SIGNAL_DAYS)))
    condition_type = infer_event_condition_type(wait_text)
    condition = build_event_condition(
        condition_type=condition_type,
        keywords=_manual_wait_signal_keywords(wait_text),
        sources=["news", "announcement", "announcement_document"],
        manual_review_item_id=item_id,
        operator_id=text(operator_id),
        rationale=rationale,
        follow_up_event=wait_text,
    )
    row = {
        "created_at": decided_at,
        "hypothesis_id": "manual_review",
        "hypothesis_title": "Operator manual-review follow-up",
        "source_table": MANUAL_REVIEW_DECISIONS_TABLE,
        "source_key": item_id,
        "symbol": symbol,
        "scope": "symbol" if symbol else "market",
        "signal_type": condition_type,
        "status": "active",
        "priority": 75,
        "expected_action": "MANUAL_REVIEW",
        "operator_summary": rationale or text(item.get("operator_summary")) or "Operator asked to watch for follow-up evidence.",
        "wait_question": wait_text,
        "condition_json": json.dumps(condition, ensure_ascii=False, sort_keys=True, default=str),
        "valid_from": decided_at,
        "valid_until": valid_until,
        "generated_by": "manual_review_decision",
        "load_ts": decided_at,
    }
    row["signal_id"] = make_signal_id(row)
    return row


def persist_wait_signal_for_decision(wait_signal: dict[str, Any] | None) -> dict[str, Any] | None:
    if not wait_signal:
        return None
    persist_wait_signals(pd.DataFrame([wait_signal]))
    condition = json.loads(str(wait_signal.get("condition_json") or "{}"))
    valid_until = pd.to_datetime(wait_signal.get("valid_until"), utc=True, errors="coerce")
    return {
        "signal_id": wait_signal["signal_id"],
        "table": WAIT_SIGNALS_TABLE,
        "valid_until": None if pd.isna(valid_until) else valid_until.isoformat(),
        "condition_type": condition.get("condition_type"),
        "issue_reason": condition.get("issue_reason"),
        "keywords": condition.get("keywords") or [],
    }


def apply_decision_side_effects(
    *,
    item_id: str,
    item: dict[str, Any],
    decision: str,
    rationale: str | None,
    follow_up_event: str | None,
    decided_at: pd.Timestamp,
    operator_id: str | None,
) -> dict[str, Any]:
    effect = decision_effect(decision)
    wait_signal_row = build_wait_signal_for_decision(
        item_id=item_id,
        item=item,
        decision=decision,
        rationale=rationale,
        follow_up_event=follow_up_event,
        decided_at=decided_at,
        operator_id=operator_id,
    )
    wait_signal = persist_wait_signal_for_decision(wait_signal_row)
    return {
        "next_state": effect.next_state,
        "closing_decision": effect.closes_item,
        "creates_wait_signal": effect.creates_wait_signal,
        "mutates_portfolio": effect.mutates_portfolio,
        "mutates_action_recommendation": effect.mutates_action_recommendation,
        "submits_order": effect.submits_order,
        "effect_description": effect.description,
        "wait_signal": wait_signal,
    }
