from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

import pandas as pd


AuthorityScope = Literal["extraction_only", "review_input_only", "research_only", "no_broker_execution"]
Provider = Literal["codex", "openai", "gemini", "mixed", "deterministic_fallback"]

ADVISORY_EVENT_EVALUATION_PROMPT_ID = "advisory_event_evaluation"
EVENT_POLICY_MANUAL_REVIEW_PROMPT_ID = "event_policy_manual_review"
COMPANY_MEMORY_REVIEW_PROMPT_ID = "company_memory_review"
PLAYBOOK_ACTION_PLAN_PROMPT_ID = "playbook_action_plan"
MANUAL_REVISION_POINTERS_PROMPT_ID = "manual_revision_pointers"
TECHNICAL_THRESHOLD_PROMOTION_REVIEW_PROMPT_ID = "technical_threshold_promotion_review"
ANNOUNCEMENT_SUMMARY_PROMPT_ID = "announcement_summary"
ANNOUNCEMENT_STRUCTURED_REPORT_PROMPT_ID = "announcement_structured_report"
OCR_PDF_PAGE_PROMPT_ID = "ocr_pdf_page"
REGIME_OVERLAY_PROPOSAL_PROMPT_ID = "regime_overlay_proposal"


@dataclass(frozen=True)
class PromptContract:
    prompt_id: str
    version: str
    title: str
    owner_area: str
    purpose: str
    authority_scope: AuthorityScope
    provider: Provider
    model_env_vars: list[str] = field(default_factory=list)
    response_schema: str | None = None
    response_schema_version: str = "v1"
    prompt_source: str | None = None
    system_prompt_source: str | None = None
    input_evidence: list[str] = field(default_factory=list)
    output_tables: list[str] = field(default_factory=list)
    fallback_behavior: str | None = None
    broker_execution_allowed: bool = False
    migration_status: Literal["registered", "caller_migrated", "inventory_only"] = "inventory_only"
    notes: list[str] = field(default_factory=list)


PROMPT_CONTRACTS: tuple[PromptContract, ...] = (
    PromptContract(
        prompt_id=ADVISORY_EVENT_EVALUATION_PROMPT_ID,
        version="ADVISORY_EVENT_EVAL_V4",
        title="Advisory event evaluation",
        owner_area="announcement_event_policy",
        purpose="Evaluate one exchange/company event with point-in-time technical and exchange evidence.",
        authority_scope="extraction_only",
        provider="mixed",
        model_env_vars=["ADVISORY_EVENT_EVAL_MODEL", "CODEX_CLI_EVENT_MODEL", "OPENAI_API_KEY"],
        response_schema="advisory.llm_event_evaluator.EventEvaluation",
        prompt_source="advisory.prompts.render_event_prompt",
        system_prompt_source="advisory.prompts.SYSTEM_PROMPT",
        input_evidence=["announcement evidence", "technical context", "exchange features", "bhavcopy evidence"],
        output_tables=["advisory_event_evaluations"],
        fallback_behavior="No deterministic event-evaluation fallback; caller records evaluation failure.",
        migration_status="caller_migrated",
        notes=["Structured event signal only; deterministic action/policy layers remain authoritative."],
    ),
    PromptContract(
        prompt_id=EVENT_POLICY_MANUAL_REVIEW_PROMPT_ID,
        version="EVENT_POLICY_MANUAL_REVIEW_V2",
        title="Event-policy manual review notes",
        owner_area="event_policy",
        purpose="Classify coarse event-policy MANUAL_REVIEW rows into bounded review-only actions and produce operator notes plus possible wait-for events.",
        authority_scope="review_input_only",
        provider="codex",
        model_env_vars=["EVENT_POLICY_LLM_MANUAL_REVIEW_MODEL"],
        response_schema="advisory.event_policy.EventPolicyManualReview",
        response_schema_version="v2",
        prompt_source="advisory.event_policy._manual_review_prompt",
        system_prompt_source="advisory.event_policy.apply_llm_manual_review",
        input_evidence=["event policy row", "event evaluation fields", "current action context"],
        output_tables=["advisory_event_policy_actions", "advisory_event_reviews"],
        fallback_behavior="Deterministic operator notes are generated when LLM is disabled or fails.",
        migration_status="registered",
        notes=[
            "Allowed final_action_type values are MANUAL_REVIEW, NO_ACTION, BUY_WATCH, and REDUCE_EXPOSURE_REVIEW.",
            "BUY_WATCH and REDUCE_EXPOSURE_REVIEW are review-only overlays and never broker-executable actions.",
        ],
    ),
    PromptContract(
        prompt_id=COMPANY_MEMORY_REVIEW_PROMPT_ID,
        version="COMPANY_MEMORY_REVIEW_V1",
        title="Company-memory review",
        owner_area="company_memory",
        purpose="Review compact historical company evidence and suggest a review-only signal for action consolidation context.",
        authority_scope="review_input_only",
        provider="codex",
        model_env_vars=["COMPANY_MEMORY_REVIEW_MODEL", "COMPANY_MEMORY_REVIEW_LLM_ENABLED"],
        response_schema="advisory.company_memory_review.CompanyMemoryReview",
        prompt_source="advisory.company_memory_review.llm_review",
        system_prompt_source="advisory.company_memory_review.llm_review",
        input_evidence=["announcement evidence", "bhavcopy evidence", "technical state", "event policy", "wait signals", "latest actions"],
        output_tables=["advisory_company_memory_reviews"],
        fallback_behavior="Deterministic company-memory review is used by default; LLM is opt-in.",
        migration_status="registered",
        notes=["Review input only; deterministic action consolidation remains authoritative."],
    ),
    PromptContract(
        prompt_id=PLAYBOOK_ACTION_PLAN_PROMPT_ID,
        version="PLAYBOOK_ACTION_PLAN_V1",
        title="Hypothesis/playbook action plan",
        owner_area="hypothesis_engine",
        purpose="Convert matched investor playbooks into cautious operator action plans and wait/check guidance.",
        authority_scope="review_input_only",
        provider="codex",
        model_env_vars=["PLAYBOOK_ACTION_MODEL"],
        response_schema="advisory.hypothesis_engine.PlaybookActionPlan",
        prompt_source="advisory.hypothesis_engine._build_action_prompt",
        system_prompt_source="advisory.hypothesis_engine.action_plan_for_match",
        input_evidence=["matched hypothesis/playbook", "news/announcement evidence", "market context"],
        output_tables=["advisory_hypothesis_action_plans", "advisory_wait_signals"],
        fallback_behavior="Deterministic action plan is generated if LLM is disabled or fails.",
        migration_status="registered",
        notes=["Trusted-overlay playbooks may influence review overlays, not direct broker execution."],
    ),
    PromptContract(
        prompt_id=MANUAL_REVISION_POINTERS_PROMPT_ID,
        version="ACTION_MANUAL_REVISION_POINTERS_V1",
        title="Action manual-revision pointers",
        owner_area="action_recommender",
        purpose="Summarize what an operator should verify before accepting or changing a final consolidated action.",
        authority_scope="review_input_only",
        provider="codex",
        model_env_vars=["ACTION_MANUAL_REVISION_POINTERS_MODEL", "ACTION_MANUAL_REVISION_POINTERS_ENABLED"],
        response_schema="advisory.action_recommender.ManualRevisionPointers",
        prompt_source="advisory.action_recommender._manual_revision_prompt",
        system_prompt_source="advisory.action_recommender.build_manual_revision_pointers",
        input_evidence=["final action row", "competing candidate rows", "reason contract"],
        output_tables=["advisory_action_recommendations"],
        fallback_behavior="Deterministic manual revision pointers are persisted when LLM is disabled or fails.",
        migration_status="registered",
        notes=["Does not override final action or recommend broker submission."],
    ),
    PromptContract(
        prompt_id=TECHNICAL_THRESHOLD_PROMOTION_REVIEW_PROMPT_ID,
        version="TECHNICAL_THRESHOLD_PROMOTION_REVIEW_V1",
        title="Technical threshold promotion review",
        owner_area="technical_calibration",
        purpose="Review calibrated threshold candidates and produce manual patch guidance.",
        authority_scope="research_only",
        provider="codex",
        model_env_vars=["TECHNICAL_THRESHOLD_PROMOTION_REVIEW_MODEL"],
        response_schema="advisory.technical_threshold_promotion.TechnicalThresholdPromotionReview",
        prompt_source="advisory.technical_threshold_promotion.build_review_prompt",
        system_prompt_source="advisory.technical_threshold_promotion.generate_promotion_review",
        input_evidence=["technical calibration summary", "candidate thresholds", "current setup thresholds"],
        output_tables=["advisory_technical_threshold_promotion_reviews"],
        fallback_behavior="Deterministic review is generated when LLM is disabled or fails.",
        migration_status="registered",
        notes=["Creates patch guidance only; config changes require separate manual review."],
    ),
    PromptContract(
        prompt_id=ANNOUNCEMENT_SUMMARY_PROMPT_ID,
        version="ANNOUNCEMENT_SUMMARY_V1",
        title="Announcement text summary",
        owner_area="announcement_ingestion",
        purpose="Produce concise faithful summaries of exchange announcement documents.",
        authority_scope="extraction_only",
        provider="mixed",
        model_env_vars=["SUMMARIZE_WITH", "CODEX_CLI_SUMMARIZE_MODEL", "OPENAI_API_KEY"],
        response_schema=None,
        prompt_source="data.announcements.pipeline.AnnouncementPipeline._generate_text",
        system_prompt_source="data.announcements.pipeline.AnnouncementPipeline._generate_text",
        input_evidence=["OCR text", "announcement metadata"],
        output_tables=["announcement_pipeline_documents", "announcement_pipeline_reports"],
        fallback_behavior="Caller records summarization failure; no investment action is created directly.",
        migration_status="caller_migrated",
        notes=["Keep summaries faithful; no unsupported inference."],
    ),
    PromptContract(
        prompt_id=ANNOUNCEMENT_STRUCTURED_REPORT_PROMPT_ID,
        version="ANNOUNCEMENT_STRUCTURED_REPORT_V1",
        title="Announcement structured report extraction",
        owner_area="announcement_ingestion",
        purpose="Extract typed report fields from announcement/OCR text into Pydantic report models.",
        authority_scope="extraction_only",
        provider="mixed",
        model_env_vars=["SUMMARIZE_WITH", "CODEX_CLI_SUMMARIZE_MODEL", "OPENAI_API_KEY"],
        response_schema="data.announcements.reports.*",
        prompt_source="data.announcements.prompts.REPORT_PROMPTS",
        system_prompt_source="data.announcements.pipeline.AnnouncementPipeline._parse_structured_response",
        input_evidence=["OCR text", "announcement metadata", "report prompt template"],
        output_tables=["announcement_pipeline_reports"],
        fallback_behavior="Caller records parse failure; recovered rows are suppressed when later parse succeeds.",
        migration_status="caller_migrated",
        notes=["Output values must be explicitly supported by document text."],
    ),
    PromptContract(
        prompt_id=OCR_PDF_PAGE_PROMPT_ID,
        version="OCR_PDF_PAGE_V1",
        title="PDF page OCR",
        owner_area="announcement_ingestion",
        purpose="Extract visible text from rendered PDF page images without summarization.",
        authority_scope="extraction_only",
        provider="mixed",
        model_env_vars=["OCR_USING", "CODEX_CLI_OCR_MODEL", "OPENAI_API_KEY"],
        response_schema=None,
        prompt_source="utils.ocr.llm_ocr.OCR_PROMPT",
        system_prompt_source=None,
        input_evidence=["rendered PDF page image"],
        output_tables=["announcement_pipeline_documents"],
        fallback_behavior="Caller records OCR failure; no investment action is created directly.",
        migration_status="caller_migrated",
        notes=["Preserve visible text faithfully."],
    ),
    PromptContract(
        prompt_id=REGIME_OVERLAY_PROPOSAL_PROMPT_ID,
        version="REGIME_OVERLAY_PROPOSAL_V1",
        title="Regime overlay proposal",
        owner_area="market_regime",
        purpose="Propose review-only market-regime overlays and candidate rules from macro, breadth, news, and event evidence.",
        authority_scope="review_input_only",
        provider="codex",
        model_env_vars=["REGIME_OVERLAY_MODEL", "REGIME_OVERLAY_LLM_ENABLED"],
        response_schema="advisory.regime_overlay.RegimeOverlayProposal",
        prompt_source="advisory.regime_overlay.build_regime_overlay_prompt",
        system_prompt_source="advisory.regime_overlay.propose_regime_overlay",
        input_evidence=["deterministic market regime", "market context summary", "macro features", "recent market news", "event evidence counts"],
        output_tables=["advisory_regime_overlay_proposals"],
        fallback_behavior="Deterministic review-only overlay proposal is persisted when LLM is disabled or fails.",
        migration_status="registered",
        notes=[
            "Creates proposed regimes/rules only; action consolidation must not consume them until explicitly promoted.",
            "Broker execution is never allowed from this prompt.",
        ],
    ),
)


def _contract_to_dict(contract: PromptContract) -> dict[str, Any]:
    payload = asdict(contract)
    payload["registered_at"] = "2026-06-08"
    return payload


def list_prompt_contracts(*, owner_area: str | None = None, authority_scope: str | None = None) -> list[dict[str, Any]]:
    rows = [_contract_to_dict(contract) for contract in PROMPT_CONTRACTS]
    if owner_area:
        normalized = str(owner_area).strip().lower()
        rows = [row for row in rows if str(row.get("owner_area") or "").lower() == normalized]
    if authority_scope:
        normalized_scope = str(authority_scope).strip().lower()
        rows = [row for row in rows if str(row.get("authority_scope") or "").lower() == normalized_scope]
    return sorted(rows, key=lambda row: (str(row.get("owner_area")), str(row.get("prompt_id"))))


def get_prompt_contract(prompt_id: str) -> dict[str, Any]:
    normalized = str(prompt_id or "").strip()
    for contract in PROMPT_CONTRACTS:
        if contract.prompt_id == normalized:
            return _contract_to_dict(contract)
    raise ValueError(f"Unknown prompt_id: {prompt_id}")


def prompt_version(prompt_id: str) -> str:
    return str(get_prompt_contract(prompt_id).get("version") or "")


def response_schema_version(prompt_id: str) -> str:
    return str(get_prompt_contract(prompt_id).get("response_schema_version") or "")


def build_prompt_registry_payload(*, owner_area: str | None = None, authority_scope: str | None = None) -> dict[str, Any]:
    contracts = list_prompt_contracts(owner_area=owner_area, authority_scope=authority_scope)
    counts: dict[str, dict[str, int]] = {"by_owner_area": {}, "by_authority_scope": {}, "by_migration_status": {}}
    for row in contracts:
        for key, bucket in [
            ("owner_area", "by_owner_area"),
            ("authority_scope", "by_authority_scope"),
            ("migration_status", "by_migration_status"),
        ]:
            value = str(row.get(key) or "unknown")
            counts[bucket][value] = counts[bucket].get(value, 0) + 1
    return {
        "generated_at": pd.Timestamp.utcnow().isoformat(),
        "status": "ok",
        "contracts": contracts,
        "summary": {
            "contract_count": len(contracts),
            **counts,
            "broker_execution_allowed_count": sum(1 for row in contracts if bool(row.get("broker_execution_allowed"))),
        },
        "notes": [
            "Registry is audit metadata. It does not execute prompts.",
            "LLM outputs remain extraction/review/research inputs unless deterministic policy accepts them.",
            "Migration status tracks whether callers are fully wired to the registry or only inventoried.",
        ],
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="List Stockey LLM/Codex prompt contracts.")
    parser.add_argument("--owner-area")
    parser.add_argument("--authority-scope")
    parser.add_argument("--prompt-id")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.prompt_id:
        payload = get_prompt_contract(args.prompt_id)
    else:
        payload = build_prompt_registry_payload(owner_area=args.owner_area, authority_scope=args.authority_scope)
    print(json.dumps(payload, indent=2, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
