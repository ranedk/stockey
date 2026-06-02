from __future__ import annotations

import argparse
import json
from typing import Any, Literal

import pandas as pd
from environs import Env
from pydantic import BaseModel, Field

from utils.codex_cli import run_codex_structured
from utils.db import db_session, sql_to_df, upsert_to_db
from utils.sync import parse_datetime_arg


env = Env()
env.read_env()

TABLE_NAME = "advisory_event_policy_actions"
EVALUATIONS_TABLE = "advisory_event_evaluations"
REVIEWS_TABLE = "advisory_event_reviews"
EVENT_POLICY_LLM_MANUAL_REVIEW_ENABLED = env.bool("EVENT_POLICY_LLM_MANUAL_REVIEW_ENABLED", default=True)
EVENT_POLICY_LLM_MANUAL_REVIEW_MODEL = env("EVENT_POLICY_LLM_MANUAL_REVIEW_MODEL", default="codex")
EVENT_POLICY_LLM_MANUAL_REVIEW_MAX_ROWS = env.int("EVENT_POLICY_LLM_MANUAL_REVIEW_MAX_ROWS", default=25)
EVENT_POLICY_LLM_MANUAL_REVIEW_TIMEOUT_SECONDS = env.int("EVENT_POLICY_LLM_MANUAL_REVIEW_TIMEOUT_SECONDS", default=180)

POSITIVE_CLASSES = {
    "ORDER_WIN",
    "RESULTS_POSITIVE",
    "GROWTH_ACCELERATION",
    "MARGIN_EXPANSION",
    "GUIDANCE_UPGRADE",
    "PLEDGE_DOWN",
    "PROMOTER_BUYING",
    "BUYBACK",
    "CAPEX_EXPANSION",
    "POLICY_SECTOR_POSITIVE",
}
NEGATIVE_CLASSES = {
    "RESULTS_NEGATIVE",
    "GUIDANCE_DOWNGRADE",
    "PLEDGE_UP",
    "PROMOTER_SELLING",
    "MANAGEMENT_RESIGNATION",
    "REGULATORY_NOTICE",
    "AUDITOR_GOVERNANCE",
    "POLICY_SECTOR_NEGATIVE",
}
REVIEW_CLASSES = {"DILUTION", "RESULTS_MIXED", "CAPEX_EXPANSION"}
LOW_ACTION_CLASSES = {"DIVIDEND", "ANALYST_MEET", "CORPORATE_ACTION_NEUTRAL", "OTHER"}
MATERIALITY_ORDER = {"low": 1, "medium": 2, "high": 3}
RISK_ORDER = {"none": 0, "low": 1, "medium": 2, "high": 3}


class EventPolicyManualReview(BaseModel):
    final_action_type: Literal["MANUAL_REVIEW", "NO_ACTION"]
    confidence: float = Field(ge=0.0, le=1.0)
    operator_summary: str = Field(min_length=10, max_length=800)
    possible_action: str = Field(min_length=5, max_length=400)
    wait_for_events: list[str] = Field(default_factory=list, max_length=8)
    operator_questions: list[str] = Field(default_factory=list, max_length=8)
    downgrade_reason: str | None = Field(default=None, max_length=500)
    rationale: str = Field(min_length=10, max_length=1200)


def json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _text(value: Any, default: str = "") -> str:
    if value is None:
        return default
    try:
        if pd.isna(value):
            return default
    except Exception:
        pass
    text = str(value).strip()
    if text.lower() in {"", "nan", "none", "null", "<na>"}:
        return default
    return text


def _num(value: Any, default: float = 0.0) -> float:
    out = pd.to_numeric(value, errors="coerce")
    return default if pd.isna(out) else float(out)


def _bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"1", "true", "t", "yes", "y"}:
        return True
    if text in {"0", "false", "f", "no", "n"}:
        return False
    return default


def _table_exists(table_name: str) -> bool:
    df = sql_to_df(
        """
        SELECT 1 AS exists_flag
        FROM information_schema.tables
        WHERE table_schema = 'public'
          AND table_name = %s
        LIMIT 1
        """,
        params=(table_name,),
    )
    return not df.empty


def ensure_tables() -> None:
    with db_session() as (_, cur):
        cur.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {TABLE_NAME} (
                published_on TIMESTAMPTZ NOT NULL,
                asof_date TIMESTAMPTZ,
                policy_at TIMESTAMPTZ,
                setup_id TEXT NOT NULL,
                setup_name TEXT,
                symbol TEXT NOT NULL,
                company_master_id TEXT,
                unique_id TEXT NOT NULL,
                event_source TEXT,
                event_class TEXT,
                policy_class TEXT,
                source_verdict TEXT,
                source_setup_effect TEXT,
                review_action TEXT,
                action_type TEXT,
                action_status TEXT,
                policy_score DOUBLE PRECISION,
                confidence DOUBLE PRECISION,
                action_reason TEXT,
                action_detail TEXT,
                checks_json TEXT,
                operator_notes_json TEXT,
                llm_review_json TEXT,
                llm_review_status TEXT,
                llm_review_model TEXT,
                llm_review_error TEXT,
                raw_context_json TEXT,
                load_ts TIMESTAMPTZ,
                UNIQUE (published_on, setup_id, symbol, unique_id)
            )
            """
        )
        column_defs = {
            "operator_notes_json": "TEXT",
            "llm_review_json": "TEXT",
            "llm_review_status": "TEXT",
            "llm_review_model": "TEXT",
            "llm_review_error": "TEXT",
        }
        for column, sql_type in column_defs.items():
            cur.execute(f"ALTER TABLE {TABLE_NAME} ADD COLUMN IF NOT EXISTS {column} {sql_type}")


def load_policy_inputs(
    *,
    asof_date: pd.Timestamp | None = None,
    symbols: list[str] | None = None,
    setup_ids: list[str] | None = None,
) -> pd.DataFrame:
    if not _table_exists(EVALUATIONS_TABLE):
        return pd.DataFrame()
    clauses = ["1 = 1"]
    params: dict[str, Any] = {}
    if asof_date is not None:
        clauses.append("e.asof_date = %(asof_date)s")
        params["asof_date"] = pd.to_datetime(asof_date, utc=True, errors="coerce").normalize()
    else:
        clauses.append(f"e.asof_date = (SELECT MAX(asof_date) FROM {EVALUATIONS_TABLE})")
    if symbols:
        clauses.append("UPPER(TRIM(e.symbol)) = ANY(%(symbols)s)")
        params["symbols"] = [str(value).strip().upper() for value in symbols]
    if setup_ids:
        clauses.append("UPPER(TRIM(e.setup_id)) = ANY(%(setup_ids)s)")
        params["setup_ids"] = [str(value).strip().upper() for value in setup_ids]

    review_select = ""
    review_join = ""
    if _table_exists(REVIEWS_TABLE):
        review_select = """
            , r.review_action
            , r.review_score
            , r.veto
            , r.review_reason
            , r.review_flags_json
        """
        review_join = f"""
        LEFT JOIN LATERAL (
            SELECT
                r.review_action,
                r.review_score,
                r.veto,
                r.review_reason,
                r.review_flags_json
            FROM {REVIEWS_TABLE} r
            WHERE r.published_on = e.published_on
              AND r.setup_id = e.setup_id
              AND r.symbol = e.symbol
              AND r.unique_id = e.unique_id
            ORDER BY r.reviewed_at DESC NULLS LAST
            LIMIT 1
        ) r ON TRUE
        """
    return sql_to_df(
        f"""
        SELECT
            e.published_on,
            e.asof_date,
            e.setup_id,
            e.setup_name,
            e.symbol,
            e.company_master_id,
            e.unique_id,
            e.event_source,
            e.subject,
            e.materiality,
            e.setup_effect,
            e.direction,
            e.surprise,
            e.novelty,
            e.contradiction,
            e.expected_decay_days,
            e.source_reliability,
            e.governance_risk,
            e.balance_sheet_risk,
            e.execution_risk,
            e.investable_now,
            e.verdict,
            e.event_class,
            e.state_transition_hint,
            e.score_impact,
            e.confidence,
            e.what_happened,
            e.rationale,
            e.event_tensor_json
            {review_select}
        FROM {EVALUATIONS_TABLE} e
        {review_join}
        WHERE {' AND '.join(clauses)}
        ORDER BY e.published_on DESC, e.setup_id, e.symbol
        """,
        params=params or None,
        retries=3,
    )


def policy_class_for_event(row: pd.Series) -> str:
    event_class = _text(row.get("event_class"), "OTHER").upper()
    text = " ".join(
        [
            _text(row.get("subject")),
            _text(row.get("what_happened")),
            _text(row.get("rationale")),
        ]
    ).lower()
    if event_class == "OTHER":
        if any(token in text for token in ["received orders", "export orders", "orders worth", "work order", "letter of award", "contract award"]):
            return "ORDER_WIN"
        if any(token in text for token in ["show cause", "regulatory notice", "sebi notice", "rbi notice", "investigation", "raid"]):
            return "REGULATORY_NOTICE"
        if "resignation" in text and any(token in text for token in ["auditor", "cfo", "ceo", "managing director", "independent director", "compliance officer"]):
            return "MANAGEMENT_RESIGNATION"
        if any(token in text for token in ["revenue was", "sales were", "ebitda", "pat", "profit"]) and _text(row.get("setup_effect")) == "strengthens":
            return "GROWTH_ACCELERATION"
    return event_class


def _has_high_risk(row: pd.Series) -> bool:
    return any(
        RISK_ORDER.get(_text(row.get(column), "none").lower(), 0) >= RISK_ORDER["high"]
        for column in ["governance_risk", "balance_sheet_risk", "execution_risk"]
    )


def build_policy_for_event(row: pd.Series) -> dict[str, Any]:
    policy_class = policy_class_for_event(row)
    verdict = _text(row.get("verdict"), "review_manual").lower()
    setup_effect = _text(row.get("setup_effect"), "neutral").lower()
    materiality = _text(row.get("materiality"), "low").lower()
    review_action = _text(row.get("review_action")).lower()
    score_impact = _num(row.get("score_impact"))
    confidence = _num(row.get("confidence"))
    is_veto = _bool(row.get("veto"))
    mat_score = MATERIALITY_ORDER.get(materiality, 1)
    checks: list[dict[str, Any]] = []

    action_type = "NO_ACTION"
    action_status = "context_only"
    reason = "Event is informational or too weak for action."
    policy_score = score_impact

    if is_veto or review_action == "veto":
        action_type = "REDUCE_EXPOSURE_REVIEW" if policy_class in NEGATIVE_CLASSES else "MANUAL_REVIEW"
        action_status = "blocked_by_adversarial_review"
        reason = "Adversarial review vetoed or blocked the event; operator should review exposure before acting."
        policy_score = min(policy_score, -0.5)
        checks.append({"check_type": "adversarial_review", "blocking": True, "rationale": "Reviewer veto or equivalent block is present."})
    elif policy_class in NEGATIVE_CLASSES:
        action_type = "REDUCE_EXPOSURE_REVIEW" if mat_score >= 2 or score_impact <= -0.12 else "MANUAL_REVIEW"
        action_status = "risk_overlay"
        reason = f"{policy_class} can invalidate or weaken the thesis; review exposure, stop, and event freshness."
        policy_score = min(policy_score, -0.2)
        checks.append({"check_type": "negative_event_class", "blocking": action_type == "REDUCE_EXPOSURE_REVIEW", "rationale": policy_class})
    elif policy_class == "DILUTION":
        action_type = "MANUAL_REVIEW"
        action_status = "capital_structure_review"
        reason = "Dilution/fund-raise events require manual review of use of proceeds, pricing, and promoter participation."
        policy_score = min(policy_score, -0.05)
        checks.append({"check_type": "dilution_terms", "blocking": True, "rationale": "Check dilution size and use of funds."})
    elif policy_class in POSITIVE_CLASSES:
        if verdict == "continue" and setup_effect == "strengthens" and mat_score >= 2 and confidence >= 0.55 and score_impact >= 0.12 and not _has_high_risk(row):
            action_type = "BUY_WATCH"
            action_status = "positive_watch_overlay"
            reason = f"{policy_class} is material and strengthens the setup; add as buy-watch evidence, pending technical/risk confirmation."
            checks.append({"check_type": "technical_confirmation", "blocking": True, "rationale": "Do not buy solely on the event; require technical trigger and liquidity checks."})
        else:
            action_type = "MANUAL_REVIEW"
            action_status = "positive_but_incomplete"
            reason = f"{policy_class} is potentially positive but lacks enough clean evidence for a buy-watch overlay."
            checks.append({"check_type": "evidence_quality", "blocking": True, "rationale": "Check materiality, confidence, risks, and whether the event is already priced in."})
    elif policy_class in REVIEW_CLASSES or verdict == "review_manual":
        action_type = "MANUAL_REVIEW"
        action_status = "manual_review_required"
        reason = f"{policy_class} needs operator interpretation before it can affect action strength."
        checks.append({"check_type": "manual_interpretation", "blocking": True, "rationale": "Event class is mixed or context-sensitive."})

    if action_type == "MANUAL_REVIEW" and policy_class in LOW_ACTION_CLASSES and mat_score <= 1 and confidence < 0.55 and abs(score_impact) < 0.12:
        action_type = "NO_ACTION"
        action_status = "low_information_downgraded"
        reason = "Event is low-materiality, low-confidence, and has no actionable policy edge; ignore until stronger follow-up evidence appears."
        checks.append({"check_type": "low_information_filter", "blocking": False, "rationale": "No operator review needed unless a stronger related event appears."})

    if review_action in {"penalize", "review_manual"} and action_type == "BUY_WATCH":
        action_type = "MANUAL_REVIEW"
        action_status = "downgraded_by_adversarial_review"
        reason = "Positive event was downgraded because adversarial review requires manual verification."
        checks.append({"check_type": "adversarial_review", "blocking": True, "rationale": f"review_action={review_action}"})

    raw_context = {
        "event_policy_version": 1,
        "event_class": _text(row.get("event_class")),
        "policy_class": policy_class,
        "verdict": verdict,
        "setup_effect": setup_effect,
        "materiality": materiality,
        "review_action": review_action,
        "veto": is_veto,
        "score_impact": score_impact,
        "confidence": confidence,
        "what_happened": _text(row.get("what_happened")),
        "rationale": _text(row.get("rationale")),
        "event_tensor": row.get("event_tensor_json"),
    }
    return {
        "published_on": row.get("published_on"),
        "asof_date": row.get("asof_date"),
        "policy_at": pd.Timestamp.utcnow(),
        "setup_id": _text(row.get("setup_id")),
        "setup_name": _text(row.get("setup_name")),
        "symbol": _text(row.get("symbol")).upper(),
        "company_master_id": _text(row.get("company_master_id")),
        "unique_id": _text(row.get("unique_id")),
        "event_source": _text(row.get("event_source")),
        "event_class": _text(row.get("event_class"), "OTHER").upper(),
        "policy_class": policy_class,
        "source_verdict": verdict,
        "source_setup_effect": setup_effect,
        "review_action": review_action,
        "action_type": action_type,
        "action_status": action_status,
        "policy_score": round(float(policy_score), 4),
        "confidence": round(float(confidence), 4),
        "action_reason": reason,
        "action_detail": _text(row.get("what_happened"))[:1200],
        "checks_json": json_dumps(checks),
        "operator_notes_json": json_dumps({}),
        "llm_review_json": json_dumps({}),
        "llm_review_status": "not_requested",
        "llm_review_model": None,
        "llm_review_error": None,
        "raw_context_json": json_dumps(raw_context),
        "load_ts": pd.Timestamp.utcnow(),
    }


def _manual_review_prompt(policy_row: dict[str, Any]) -> str:
    payload = {
        "instruction": (
            "Review this event-policy MANUAL_REVIEW row. "
            "If manual review is unlikely to lead to a useful action, set final_action_type to NO_ACTION. "
            "Otherwise keep MANUAL_REVIEW and provide exact operator notes, future events to wait for, and questions to answer. "
            "Do not recommend immediate broker execution."
        ),
        "policy_row": policy_row,
    }
    return json.dumps(payload, indent=2, ensure_ascii=False, default=str)


def _deterministic_operator_notes(policy_row: dict[str, Any], *, status: str, error: str | None = None) -> dict[str, Any]:
    action_type = _text(policy_row.get("action_type"), "MANUAL_REVIEW").upper()
    policy_class = _text(policy_row.get("policy_class"), "OTHER")
    notes = {
        "final_action_type": action_type,
        "confidence": _num(policy_row.get("confidence")),
        "operator_summary": _text(policy_row.get("action_reason"), "Manual review required before this event can affect an action."),
        "possible_action": "Keep this as review-only evidence until follow-up data confirms materiality and market reaction.",
        "wait_for_events": [
            "Next price/volume reaction after the event",
            "Follow-up exchange clarification or management commentary",
            "Technical trigger or support failure",
        ],
        "operator_questions": [
            "Is the event material relative to revenue, market cap, or existing thesis?",
            "Is the event already priced in?",
            "Does technical structure confirm or contradict the event?",
        ],
        "downgrade_reason": None,
        "rationale": "Deterministic fallback notes generated because LLM review was unavailable or disabled.",
        "status": status,
    }
    if policy_class in LOW_ACTION_CLASSES:
        notes["final_action_type"] = "NO_ACTION"
        notes["possible_action"] = "No action unless a stronger related event appears."
        notes["downgrade_reason"] = "Low-action event class is not worth manual review by itself."
    if error:
        notes["llm_error"] = error
    return notes


def apply_llm_manual_review(policy_row: dict[str, Any], *, model: str | None = None, use_llm: bool = True) -> dict[str, Any]:
    if str(policy_row.get("action_type") or "").upper() != "MANUAL_REVIEW":
        return policy_row
    effective_model = model or EVENT_POLICY_LLM_MANUAL_REVIEW_MODEL
    if not use_llm or str(effective_model).strip().lower() in {"", "off", "none", "disabled", "false"}:
        notes = _deterministic_operator_notes(policy_row, status="disabled")
        status = "disabled"
        error = None
    else:
        try:
            codex_model = effective_model.split(":", 1)[1] if effective_model.startswith("codex:") else None if effective_model == "codex" else effective_model
            review = run_codex_structured(
                _manual_review_prompt(policy_row),
                response_model=EventPolicyManualReview,
                model=codex_model,
                system_prompt=(
                    "You are a cautious Indian-equity event-policy reviewer. "
                    "You decide whether a manual-review event is actionable enough to remain in the operator queue. "
                    "You never submit trades and you never create broker-executable recommendations."
                ),
                max_attempts=2,
                timeout_seconds=EVENT_POLICY_LLM_MANUAL_REVIEW_TIMEOUT_SECONDS,
            )
            notes = review.model_dump()
            status = "ok"
            error = None
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            notes = _deterministic_operator_notes(policy_row, status="fallback_after_error", error=error)
            status = "fallback_after_error"

    out = dict(policy_row)
    final_action = str(notes.get("final_action_type") or out.get("action_type") or "MANUAL_REVIEW").upper()
    if final_action in {"MANUAL_REVIEW", "NO_ACTION"}:
        out["action_type"] = final_action
    if final_action == "NO_ACTION":
        out["action_status"] = "llm_downgraded_no_action" if status == "ok" else out.get("action_status")
        downgrade_reason = _text(notes.get("downgrade_reason"))
        if downgrade_reason:
            out["action_reason"] = downgrade_reason
    elif final_action != "MANUAL_REVIEW":
        out["action_status"] = "llm_reclassified_review"
        out["action_reason"] = _text(notes.get("operator_summary"), out.get("action_reason"))
    else:
        out["action_reason"] = _text(notes.get("operator_summary"), out.get("action_reason"))
    out["operator_notes_json"] = json_dumps(notes)
    out["llm_review_json"] = json_dumps(notes)
    out["llm_review_status"] = status
    out["llm_review_model"] = effective_model
    out["llm_review_error"] = error
    raw_context = json.loads(out.get("raw_context_json") or "{}")
    raw_context["operator_notes"] = notes
    out["raw_context_json"] = json_dumps(raw_context)
    return out


def build_event_policy_actions(
    events: pd.DataFrame,
    *,
    use_llm: bool | None = None,
    llm_max_rows: int | None = None,
    model: str | None = None,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    if events.empty:
        return pd.DataFrame(), {"input_rows": 0, "policy_rows": 0}
    policy_rows = [build_policy_for_event(row) for _, row in events.iterrows()]
    effective_use_llm = EVENT_POLICY_LLM_MANUAL_REVIEW_ENABLED if use_llm is None else bool(use_llm)
    max_rows = EVENT_POLICY_LLM_MANUAL_REVIEW_MAX_ROWS if llm_max_rows is None else max(0, int(llm_max_rows))
    reviewed_count = 0
    if effective_use_llm and max_rows > 0:
        for idx, row in enumerate(policy_rows):
            if str(row.get("action_type") or "").upper() != "MANUAL_REVIEW":
                continue
            if reviewed_count >= max_rows:
                row["llm_review_status"] = "skipped_limit"
                row["operator_notes_json"] = json_dumps(_deterministic_operator_notes(row, status="skipped_limit"))
                continue
            policy_rows[idx] = apply_llm_manual_review(row, model=model, use_llm=True)
            reviewed_count += 1
    elif not effective_use_llm:
        for idx, row in enumerate(policy_rows):
            if str(row.get("action_type") or "").upper() == "MANUAL_REVIEW":
                policy_rows[idx] = apply_llm_manual_review(row, model=model, use_llm=False)

    out = pd.DataFrame(policy_rows)
    for column in ["published_on", "asof_date", "policy_at", "load_ts"]:
        out[column] = pd.to_datetime(out[column], utc=True, errors="coerce")
    return out, {
        "input_rows": int(len(events)),
        "policy_rows": int(len(out)),
        "action_counts": out["action_type"].value_counts(dropna=False).to_dict(),
        "policy_class_counts": out["policy_class"].value_counts(dropna=False).head(20).to_dict(),
        "llm_manual_review_enabled": bool(effective_use_llm),
        "llm_manual_review_rows": int(reviewed_count),
    }


def persist_event_policy_actions(df: pd.DataFrame) -> None:
    ensure_tables()
    if df.empty:
        return
    upsert_to_db(
        df,
        TABLE_NAME,
        unique_keys=["published_on", "setup_id", "symbol", "unique_id"],
        timescaledb_column="published_on",
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Apply deterministic event-class policies to LLM event evaluations.")
    parser.add_argument("--date", type=parse_datetime_arg, help="Asof date in YYYY-MM-DD")
    parser.add_argument("--symbols", nargs="*")
    parser.add_argument("--setup", dest="setup_ids", nargs="*")
    parser.add_argument("--model", default=None)
    parser.add_argument("--no-llm", action="store_true")
    parser.add_argument("--llm-max-rows", type=int, default=None)
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    asof_date = pd.Timestamp(args.date, tz="UTC") if args.date else None
    events = load_policy_inputs(asof_date=asof_date, symbols=args.symbols, setup_ids=args.setup_ids)
    actions, meta = build_event_policy_actions(
        events,
        use_llm=not bool(args.no_llm),
        llm_max_rows=args.llm_max_rows,
        model=args.model,
    )
    if not args.dry_run:
        persist_event_policy_actions(actions)
    print(json.dumps({"status": "ok", "table": TABLE_NAME, **meta, "dry_run": bool(args.dry_run)}, indent=2, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
