from __future__ import annotations

import argparse
import json
from typing import Any, Literal

import pandas as pd
from environs import Env
from openai import OpenAI
from pydantic import BaseModel, Field

from advisory.prompts import ADVISORY_EVENT_PROMPT_VERSION, SYSTEM_PROMPT, render_event_prompt
from utils.db import db_session, sql_to_df, upsert_to_db
from utils.sync import parse_datetime_arg


env = Env()
env.read_env()

EVALUATIONS_TABLE = "advisory_event_evaluations"
RISKS_TABLE = "advisory_event_risks"
NEWS_EVENTS_TABLE = "advisory_news_events"
DEFAULT_MODEL = env("ADVISORY_EVENT_EVAL_MODEL", default="gpt-5-mini-2025-08-07")
_MAX_DOC_TEXT_CHARS = 12000
_MAX_JSON_TEXT_CHARS = 6000


class EventRisk(BaseModel):
    risk_type: Literal["governance", "balance_sheet", "execution", "compliance", "other"]
    severity: Literal["low", "medium", "high"]
    title: str = Field(min_length=3, max_length=120)
    detail: str = Field(min_length=5, max_length=600)
    evidence: list[str] = Field(default_factory=list, max_length=5)


class EventEvaluation(BaseModel):
    what_happened: str = Field(min_length=10, max_length=1200)
    sentiment: Literal["positive", "negative", "mixed", "neutral"]
    materiality: Literal["low", "medium", "high"]
    setup_effect: Literal["strengthens", "weakens", "neutral", "contradicts"]
    direction: Literal["positive", "negative", "mixed", "neutral"]
    surprise: float = Field(ge=0.0, le=1.0)
    novelty: float = Field(ge=0.0, le=1.0)
    contradiction: float = Field(ge=0.0, le=1.0)
    expected_decay_days: int = Field(ge=0, le=3650)
    source_reliability: Literal["low", "medium", "high"]
    affected_sectors: list[str] = Field(default_factory=list, max_length=12)
    affected_peers: list[str] = Field(default_factory=list, max_length=20)
    governance_risk: Literal["none", "low", "medium", "high"]
    balance_sheet_risk: Literal["none", "low", "medium", "high"]
    execution_risk: Literal["none", "low", "medium", "high"]
    investable_now: bool
    verdict: Literal["continue", "reject", "review_manual"]
    event_class: Literal[
        "RESULTS_POSITIVE",
        "RESULTS_NEGATIVE",
        "ORDER_WIN",
        "CAPEX_EXPANSION",
        "GUIDANCE_UPGRADE",
        "GUIDANCE_DOWNGRADE",
        "PLEDGE_UP",
        "PLEDGE_DOWN",
        "DILUTION",
        "AUDITOR_GOVERNANCE",
        "POLICY_SECTOR_POSITIVE",
        "POLICY_SECTOR_NEGATIVE",
        "OTHER",
    ]
    state_transition_hint: Literal[
        "UPGRADE_TO_PASS_NOW",
        "DOWNGRADE_TO_REJECT",
        "RAISE_SCORE_ONLY",
        "CUT_SCORE_ONLY",
        "NO_CHANGE",
        "REVIEW_MANUAL",
    ]
    score_impact: float = Field(ge=-1.0, le=1.0)
    confidence: float = Field(ge=0.0, le=1.0)
    rationale: str = Field(min_length=10, max_length=1600)
    source_trace: list[str] = Field(default_factory=list, max_length=8)
    key_risks: list[EventRisk] = Field(default_factory=list, max_length=8)


def normalize_timestamp(series: pd.Series) -> pd.Series:
    return pd.to_datetime(series, utc=True, errors="coerce").dt.normalize()


def json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _table_exists(table_name: str) -> bool:
    try:
        df = sql_to_df(
            """
            SELECT 1 AS exists_flag
            FROM information_schema.tables
            WHERE table_name = %s
            LIMIT 1
            """,
            params=(table_name,),
        )
    except Exception:
        return False
    return not df.empty


def trim_text(value: Any, limit: int) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    return text[:limit]


def normalize_jsonish(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, (dict, list)):
        return value
    text = str(value).strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text


def normalize_float(value: Any, *, minimum: float, maximum: float, default: float = 0.0) -> float:
    numeric = pd.to_numeric(value, errors="coerce")
    if pd.isna(numeric):
        return default
    return round(float(max(minimum, min(maximum, float(numeric)))), 4)


def normalize_int(value: Any, *, minimum: int, maximum: int, default: int) -> int:
    numeric = pd.to_numeric(value, errors="coerce")
    if pd.isna(numeric):
        return default
    return int(max(minimum, min(maximum, int(numeric))))


def normalize_string_list(value: Any, *, limit: int, item_limit: int = 80) -> list[str]:
    normalized = normalize_jsonish(value)
    if normalized is None:
        return []
    if isinstance(normalized, list):
        raw_items = normalized
    elif isinstance(normalized, str):
        raw_items = [part.strip() for part in normalized.split(",")]
    else:
        raw_items = [normalized]
    out: list[str] = []
    seen: set[str] = set()
    for item in raw_items:
        text = trim_text(item, item_limit)
        if not text:
            continue
        key = text.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(text)
        if len(out) >= limit:
            break
    return out


def _flatten_jsonish_text(value: Any) -> str:
    normalized = normalize_jsonish(value)
    if normalized is None:
        return ""
    if isinstance(normalized, list):
        return " ".join(str(item) for item in normalized if item is not None)
    if isinstance(normalized, dict):
        parts: list[str] = []
        for key, item in normalized.items():
            parts.append(str(key))
            if item is not None:
                parts.append(str(item))
        return " ".join(parts)
    return str(normalized)


def _contains_any(text: str, tokens: list[str]) -> bool:
    return any(token in text for token in tokens)


VALID_EVENT_CLASSES = {
    "RESULTS_POSITIVE",
    "RESULTS_NEGATIVE",
    "ORDER_WIN",
    "CAPEX_EXPANSION",
    "GUIDANCE_UPGRADE",
    "GUIDANCE_DOWNGRADE",
    "PLEDGE_UP",
    "PLEDGE_DOWN",
    "DILUTION",
    "AUDITOR_GOVERNANCE",
    "POLICY_SECTOR_POSITIVE",
    "POLICY_SECTOR_NEGATIVE",
    "OTHER",
}


def canonicalize_event_class(value: Any) -> str | None:
    text = trim_text(value, 100)
    if not text:
        return None
    normalized = text.strip().upper().replace("-", "_").replace(" ", "_")
    alias_map = {
        "RESULT_POSITIVE": "RESULTS_POSITIVE",
        "RESULT_POSITIVE_S": "RESULTS_POSITIVE",
        "POSITIVE_RESULTS": "RESULTS_POSITIVE",
        "RESULTS_UPGRADE": "RESULTS_POSITIVE",
        "RESULT_NEGATIVE": "RESULTS_NEGATIVE",
        "NEGATIVE_RESULTS": "RESULTS_NEGATIVE",
        "ORDER": "ORDER_WIN",
        "CONTRACT_WIN": "ORDER_WIN",
        "WORK_ORDER": "ORDER_WIN",
        "CAPEX": "CAPEX_EXPANSION",
        "GUIDANCE_POSITIVE": "GUIDANCE_UPGRADE",
        "GUIDANCE_NEGATIVE": "GUIDANCE_DOWNGRADE",
        "PROMOTER_PLEDGE_UP": "PLEDGE_UP",
        "PROMOTER_PLEDGE_DOWN": "PLEDGE_DOWN",
        "FUND_RAISE": "DILUTION",
        "ESOP_DILUTION": "DILUTION",
        "AUDITOR": "AUDITOR_GOVERNANCE",
        "GOVERNANCE": "AUDITOR_GOVERNANCE",
        "POLICY_POSITIVE": "POLICY_SECTOR_POSITIVE",
        "POLICY_NEGATIVE": "POLICY_SECTOR_NEGATIVE",
    }
    normalized = alias_map.get(normalized, normalized)
    return normalized if normalized in VALID_EVENT_CLASSES else None


def classify_event_type(event_row: pd.Series, parsed: EventEvaluation) -> str:
    subject_text = trim_text(event_row.get("subject"), 500) or ""
    category_text = trim_text(event_row.get("filed_under_category"), 500) or ""
    summary_text = trim_text(event_row.get("concise_summary_text"), 3000) or ""
    categories_text = _flatten_jsonish_text(event_row.get("categories_json"))
    strong_haystack = " ".join([subject_text, category_text, categories_text]).lower()
    full_haystack = " ".join([subject_text, category_text, summary_text, categories_text]).lower()

    if _contains_any(full_haystack, ["auditor", "forensic", "governance", "fraud", "whistleblower"]):
        return "AUDITOR_GOVERNANCE"
    if "pledge" in full_haystack:
        return "PLEDGE_UP" if parsed.sentiment == "negative" else "PLEDGE_DOWN"
    if _contains_any(full_haystack, ["guidance", "outlook", "revised estimate", "margin guidance"]):
        return "GUIDANCE_UPGRADE" if parsed.sentiment == "positive" else "GUIDANCE_DOWNGRADE"
    if _contains_any(
        full_haystack,
        [
            "preferential",
            "qip",
            "warrant",
            "warrants",
            "dilution",
            "rights issue",
            "allotment of shares",
            "esop",
            "esos",
            "esps",
            "stock option",
            "employee stock option",
            "fund raise",
            "fundraise",
            "issue of equity",
        ],
    ):
        return "DILUTION"
    if _contains_any(
        full_haystack,
        [
            "financial results",
            "quarterly results",
            "annual results",
            "earnings",
            "results",
            "q1",
            "q2",
            "q3",
            "q4",
        ],
    ):
        return "RESULTS_POSITIVE" if parsed.sentiment == "positive" else "RESULTS_NEGATIVE"
    if _contains_any(
        full_haystack,
        [
            "capex",
            "capacity expansion",
            "capacity increase",
            "new plant",
            "brownfield",
            "greenfield",
            "commissioning",
            "commercial production",
        ],
    ):
        return "CAPEX_EXPANSION"

    policy_tokens = ["policy", "tariff", "duty", "regulation", "regulatory", "gst", "export duty", "import duty"]
    policy_exclusions = ["nclt order", "court order", "order no.", "change in director", "board meeting"]
    if _contains_any(full_haystack, policy_tokens) and not _contains_any(full_haystack, policy_exclusions):
        return "POLICY_SECTOR_POSITIVE" if parsed.sentiment == "positive" else "POLICY_SECTOR_NEGATIVE"

    order_tokens = ["order win", "order award", "work order", "purchase order", "contract award", "letter of award", "loa", "contract win"]
    order_exclusions = [
        "change in director",
        "director",
        "board meeting",
        "amalgamation",
        "scheme of amalgamation",
        "nclt",
        "court order",
        "first motion",
        "order no.",
        "general updates",
        "disclosure of material issue",
    ]
    if (
        _contains_any(full_haystack, order_tokens)
        or (
            _contains_any(full_haystack, ["order", "contract", "award"])
            and not _contains_any(full_haystack, order_exclusions)
            and _contains_any(strong_haystack, ["contract", "order", "award", "work order", "letter of award"])
        )
    ):
        return "ORDER_WIN"
    return "OTHER"


def normalize_score_impact(parsed: EventEvaluation) -> float:
    base = 0.0
    materiality = {"low": 0.10, "medium": 0.20, "high": 0.35}.get(parsed.materiality, 0.0)
    if parsed.setup_effect == "strengthens":
        base += materiality
    elif parsed.setup_effect == "neutral":
        base += 0.0
    elif parsed.setup_effect == "weakens":
        base -= materiality
    elif parsed.setup_effect == "contradicts":
        base -= max(materiality, 0.35)

    if parsed.sentiment == "positive":
        base += 0.05
    elif parsed.sentiment == "negative":
        base -= 0.05
    elif parsed.sentiment == "mixed":
        base -= 0.02

    if parsed.setup_effect == "strengthens":
        base += max(0.0, parsed.surprise - 0.50) * 0.12
        base += max(0.0, parsed.novelty - 0.50) * 0.10
    elif parsed.setup_effect in {"weakens", "contradicts"}:
        base -= max(0.0, parsed.surprise - 0.50) * 0.12
        base -= max(0.0, parsed.novelty - 0.50) * 0.10
    base -= parsed.contradiction * 0.18

    if parsed.setup_effect != "neutral":
        if parsed.source_reliability == "high":
            base += 0.03 if parsed.setup_effect == "strengthens" else -0.03
        elif parsed.source_reliability == "low":
            base -= 0.05 if parsed.setup_effect == "strengthens" else 0.05

    if parsed.setup_effect == "strengthens":
        if parsed.expected_decay_days <= 2:
            base -= 0.03
        elif parsed.expected_decay_days >= 90:
            base += 0.03
    elif parsed.setup_effect in {"weakens", "contradicts"} and parsed.expected_decay_days >= 90:
        base -= 0.03

    if parsed.governance_risk == "high":
        base = min(base, -0.60)
    elif parsed.governance_risk == "medium":
        base -= 0.15

    if parsed.balance_sheet_risk == "high":
        base -= 0.20
    elif parsed.balance_sheet_risk == "medium":
        base -= 0.10

    if parsed.execution_risk == "high":
        base -= 0.15
    elif parsed.execution_risk == "medium":
        base -= 0.05

    return round(max(-1.0, min(1.0, base)), 4)


def derive_state_transition_hint(parsed: EventEvaluation, event_class: str, score_impact: float) -> str:
    if parsed.verdict == "review_manual":
        return "REVIEW_MANUAL"

    severe_negative_event = event_class in {
        "AUDITOR_GOVERNANCE",
        "PLEDGE_UP",
        "GUIDANCE_DOWNGRADE",
        "RESULTS_NEGATIVE",
        "POLICY_SECTOR_NEGATIVE",
    }
    if parsed.verdict == "reject":
        return "DOWNGRADE_TO_REJECT"
    if severe_negative_event and (
        parsed.setup_effect in {"weakens", "contradicts"}
        or score_impact <= -0.15
        or parsed.governance_risk in {"medium", "high"}
        or parsed.balance_sheet_risk == "high"
    ):
        return "DOWNGRADE_TO_REJECT"
    if parsed.investable_now and parsed.setup_effect == "strengthens" and parsed.materiality in {"medium", "high"} and score_impact >= 0.15:
        return "UPGRADE_TO_PASS_NOW"
    if score_impact >= 0.12:
        return "RAISE_SCORE_ONLY"
    if parsed.setup_effect in {"weakens", "contradicts"} or score_impact <= -0.12:
        return "CUT_SCORE_ONLY"
    return "NO_CHANGE"


def normalize_event_evaluation(event_row: pd.Series, parsed: EventEvaluation) -> tuple[str, str, float]:
    event_class = canonicalize_event_class(parsed.event_class) or classify_event_type(event_row, parsed)
    score_impact = normalize_score_impact(parsed)
    transition_hint = derive_state_transition_hint(parsed, event_class, score_impact)
    return event_class, transition_hint, score_impact


def build_event_tensor(parsed: EventEvaluation, *, event_class: str, score_impact: float, state_transition_hint: str) -> dict[str, Any]:
    return {
        "event_type": event_class,
        "direction": parsed.direction,
        "surprise": normalize_float(parsed.surprise, minimum=0.0, maximum=1.0, default=0.0),
        "novelty": normalize_float(parsed.novelty, minimum=0.0, maximum=1.0, default=0.0),
        "contradiction": normalize_float(parsed.contradiction, minimum=0.0, maximum=1.0, default=0.0),
        "materiality": parsed.materiality,
        "setup_effect": parsed.setup_effect,
        "expected_decay_days": normalize_int(parsed.expected_decay_days, minimum=0, maximum=3650, default=5),
        "source_reliability": parsed.source_reliability,
        "affected_sectors": normalize_string_list(parsed.affected_sectors, limit=12),
        "affected_peers": normalize_string_list(parsed.affected_peers, limit=20),
        "confidence": normalize_float(parsed.confidence, minimum=0.0, maximum=1.0, default=0.0),
        "score_impact": normalize_float(score_impact, minimum=-1.0, maximum=1.0, default=0.0),
        "state_transition_hint": state_transition_hint,
    }


def load_watch_events(
    *,
    asof_date: pd.Timestamp | None = None,
    symbols: list[str] | None = None,
    setup_ids: list[str] | None = None,
    include_evaluated: bool = False,
    limit: int | None = None,
) -> pd.DataFrame:
    announcement_exists = _table_exists("advisory_watch_events")
    news_exists = _table_exists(NEWS_EVENTS_TABLE)
    if not announcement_exists and not news_exists:
        return pd.DataFrame()

    clauses = ["e.event_status = 'triggered'"]
    params: list[object] = []
    if asof_date is not None:
        clauses.append("e.asof_date = %s")
        params.append(asof_date)
    else:
        max_date_queries: list[str] = []
        if announcement_exists:
            max_date_queries.append("SELECT MAX(asof_date) AS asof_date FROM advisory_watch_events")
        if news_exists:
            max_date_queries.append(f"SELECT MAX(asof_date) AS asof_date FROM {NEWS_EVENTS_TABLE}")
        clauses.append(f"e.asof_date = (SELECT MAX(asof_date) FROM ({' UNION ALL '.join(max_date_queries)}) latest_dates)")
    if symbols:
        clauses.append("e.symbol = ANY(%s)")
        params.append([value.upper() for value in symbols])
    if setup_ids:
        clauses.append("e.setup_id = ANY(%s)")
        params.append([value.upper() for value in setup_ids])
    limit_sql = f"LIMIT {int(limit)}" if limit and limit > 0 else ""
    try:
        evaluation_table_exists = sql_to_df(
            """
            SELECT 1 AS exists_flag
            FROM information_schema.tables
            WHERE table_name = %s
            LIMIT 1
            """,
            params=(EVALUATIONS_TABLE,),
        )
    except Exception:
        return pd.DataFrame()
    if evaluation_table_exists.empty:
        join_sql = ""
        select_sql = ""
    else:
        join_sql = f"""
        LEFT JOIN {EVALUATIONS_TABLE} x
          ON x.setup_id = e.setup_id
         AND x.symbol = e.symbol
         AND x.unique_id = e.unique_id
        """
        select_sql = """
            , x.evaluation_status AS existing_evaluation_status
            , x.evaluated_at AS existing_evaluated_at
        """
        if not include_evaluated:
            clauses.append("x.unique_id IS NULL")

    event_sources: list[str] = []
    if announcement_exists:
        event_sources.append(
            """
            SELECT
                e.asof_date,
                e.setup_id,
                e.setup_name,
                e.symbol,
                e.company_master_id,
                e.unique_id,
                e.exchange,
                e.subject,
                e.filed_under_category,
                e.parse_status,
                e.concise_summary_text,
                e.categories_json,
                e.watch_reasons_json,
                e.event_status,
                e.published_on,
                'announcement'::text AS event_source,
                NULL::text AS source_url
            FROM advisory_watch_events e
            """
        )
    if news_exists:
        event_sources.append(
            f"""
            SELECT
                e.asof_date,
                e.setup_id,
                e.setup_name,
                e.symbol,
                e.company_master_id,
                e.unique_id,
                NULL::text AS exchange,
                e.subject,
                e.feed_name AS filed_under_category,
                'rss'::text AS parse_status,
                e.concise_summary_text,
                e.categories_json,
                e.watch_reasons_json,
                e.event_status,
                e.published_on,
                COALESCE(e.event_source, 'economic_times_rss') AS event_source,
                e.source_url
            FROM {NEWS_EVENTS_TABLE} e
            """
        )

    df = sql_to_df(
        f"""
        SELECT
            e.*
            {select_sql}
        FROM (
            {' UNION ALL '.join(event_sources)}
        ) e
        {join_sql}
        WHERE {' AND '.join(clauses)}
        ORDER BY e.published_on, e.setup_id, e.symbol, e.unique_id
        {limit_sql}
        """,
        params=tuple(params) if params else None,
    )
    if df.empty:
        return df
    df["asof_date"] = normalize_timestamp(df["asof_date"])
    df["published_on"] = pd.to_datetime(df["published_on"], utc=True, errors="coerce")
    df["symbol"] = df["symbol"].astype("string").str.upper()
    return df


def load_documents(unique_ids: list[str]) -> pd.DataFrame:
    if not unique_ids:
        return pd.DataFrame()
    df = sql_to_df(
        """
        SELECT
            unique_id,
            company_master_id,
            ticker,
            company_name,
            exchange,
            subject,
            filed_under_category,
            published_on,
            parse_status,
            concise_summary_text,
            text,
            categories_json,
            parsed_reports_json
        FROM announcement_pipeline_documents
        WHERE unique_id = ANY(%s)
        """,
        params=(unique_ids,),
    )
    if df.empty:
        return df
    df["published_on"] = pd.to_datetime(df["published_on"], utc=True, errors="coerce")
    return df


def load_point_in_time_context(symbol: str, published_on: pd.Timestamp) -> dict[str, Any]:
    daily_cutoff = pd.to_datetime(published_on, utc=True, errors="coerce").normalize()
    df = sql_to_df(
        """
        SELECT
            tech.asof_date AS technical_asof_date,
            tech.adj_close,
            tech.dma_20,
            tech.dma_50,
            tech.dma_200,
            tech.avg_traded_value_20d,
            tech.rs_vs_benchmark,
            tech.rs_vs_sector,
            tech.dist_52w_high,
            tech.breakout_extension_pct,
            tech.sector_code,
            tech.sector_peer_count,
            fund.asof_date AS fundamentals_asof_date,
            fund.fundamentals_freshness_status,
            fund.total_revenue_qoq_growth,
            fund.ebitda_qoq_growth,
            fund.profit_after_tax_qoq_growth,
            fund.total_revenue_qoq_growth_vs_sector,
            fund.ebitda_qoq_growth_vs_sector,
            fund.profit_after_tax_qoq_growth_vs_sector,
            fund.debt_to_equity,
            fund.debt_to_equity_vs_sector,
            fund.promoter_total,
            fund.promoter_total_vs_sector,
            fund.fii,
            fund.fii_vs_sector,
            fund.sector_peer_fundamental_count,
            regime.asof_date AS regime_asof_date,
            regime.regime_name,
            regime.regime_notes
        FROM (SELECT 1) base
        LEFT JOIN LATERAL (
            SELECT *
            FROM advisory_technical_daily
            WHERE symbol = %(symbol)s
              AND asof_date < %(daily_cutoff)s
            ORDER BY asof_date DESC
            LIMIT 1
        ) tech ON true
        LEFT JOIN LATERAL (
            SELECT *
            FROM advisory_fundamentals_daily
            WHERE symbol = %(symbol)s
              AND asof_date < %(daily_cutoff)s
            ORDER BY asof_date DESC
            LIMIT 1
        ) fund ON true
        LEFT JOIN LATERAL (
            SELECT *
            FROM advisory_market_regime
            WHERE asof_date < %(daily_cutoff)s
            ORDER BY asof_date DESC
            LIMIT 1
        ) regime ON true
        """,
        params={"symbol": symbol.upper(), "daily_cutoff": daily_cutoff},
    )
    if df.empty:
        return {}
    return {
        key: (value.isoformat() if isinstance(value, pd.Timestamp) else value)
        for key, value in df.iloc[0].to_dict().items()
        if not pd.isna(value)
    }


def load_exchange_context(symbol: str, published_on: pd.Timestamp, *, lookback_days: int = 30, max_events: int = 8) -> dict[str, Any]:
    out: dict[str, Any] = {"recent_events": []}
    symbol = symbol.upper()
    daily_cutoff = pd.to_datetime(published_on, utc=True, errors="coerce").normalize()
    try:
        if _table_exists("advisory_exchange_features_daily"):
            features = sql_to_df(
                """
                SELECT
                    asof_date AS exchange_asof_date,
                    latest_exchange_event_date,
                    latest_exchange_event_source,
                    latest_exchange_event_type,
                    latest_exchange_event_summary,
                    deal_net_value_20d,
                    deal_cluster_count_20d,
                    insider_net_value_90d,
                    insider_event_count_90d,
                    short_selling_quantity_20d,
                    short_selling_event_count_20d,
                    upcoming_earnings_14d,
                    days_to_earnings,
                    corporate_action_count_30d,
                    exchange_accumulation_score,
                    exchange_distribution_score,
                    exchange_event_score
                FROM advisory_exchange_features_daily
                WHERE symbol = %(symbol)s
                  AND asof_date < %(daily_cutoff)s
                ORDER BY asof_date DESC
                LIMIT 1
                """,
                params={"symbol": symbol, "daily_cutoff": daily_cutoff},
            )
            if not features.empty:
                out["features"] = {
                    key: (value.isoformat() if isinstance(value, pd.Timestamp) else value)
                    for key, value in features.iloc[0].to_dict().items()
                    if not pd.isna(value)
                }
        if _table_exists("advisory_exchange_events"):
            events = sql_to_df(
                """
                SELECT
                    known_on,
                    event_date,
                    event_source,
                    event_type,
                    participant,
                    side,
                    quantity,
                    price,
                    value_inr,
                    holding_pct_before,
                    holding_pct_after,
                    event_summary
                FROM advisory_exchange_events
                WHERE symbol = %(symbol)s
                  AND known_on <= %(published_on)s
                  AND known_on >= %(start_on)s
                ORDER BY known_on DESC, value_inr DESC NULLS LAST
                LIMIT %(max_events)s
                """,
                params={
                    "symbol": symbol,
                    "published_on": published_on,
                    "start_on": published_on - pd.Timedelta(days=int(lookback_days)),
                    "max_events": int(max_events),
                },
            )
            if not events.empty:
                out["recent_events"] = [
                    {
                        key: (value.isoformat() if isinstance(value, pd.Timestamp) else value)
                        for key, value in row.items()
                        if not pd.isna(value)
                    }
                    for row in events.to_dict(orient="records")
                ]
    except Exception as exc:
        out["error"] = str(exc)
    return out


def build_payload(event_row: pd.Series, document_row: pd.Series | None) -> dict[str, Any]:
    published_on = pd.to_datetime(event_row["published_on"], utc=True, errors="coerce")
    stock_context = load_point_in_time_context(str(event_row["symbol"]), published_on)
    exchange_context = load_exchange_context(str(event_row["symbol"]), published_on)

    document_payload = {
        "unique_id": event_row["unique_id"],
        "event_source": event_row.get("event_source"),
        "company_master_id": event_row.get("company_master_id"),
        "symbol": event_row.get("symbol"),
        "exchange": event_row.get("exchange"),
        "source_url": event_row.get("source_url"),
        "subject": event_row.get("subject"),
        "filed_under_category": event_row.get("filed_under_category"),
        "published_on": published_on.isoformat() if not pd.isna(published_on) else None,
        "parse_status": event_row.get("parse_status"),
        "concise_summary_text": trim_text(event_row.get("concise_summary_text"), 2000),
        "categories_json": normalize_jsonish(event_row.get("categories_json")),
        "watch_reasons_json": normalize_jsonish(event_row.get("watch_reasons_json")),
    }

    if document_row is not None:
        document_payload.update(
            {
                "company_name": document_row.get("company_name"),
                "document_parse_status": document_row.get("parse_status"),
                "document_summary_text": trim_text(document_row.get("concise_summary_text"), 2000),
                "document_text_excerpt": trim_text(document_row.get("text"), _MAX_DOC_TEXT_CHARS),
                "document_categories_json": normalize_jsonish(document_row.get("categories_json")),
                "parsed_reports_json": normalize_jsonish(
                    trim_text(document_row.get("parsed_reports_json"), _MAX_JSON_TEXT_CHARS)
                ),
            }
        )

    return {
        "prompt_version": ADVISORY_EVENT_PROMPT_VERSION,
        "setup_context": {
            "asof_date": event_row["asof_date"].isoformat() if not pd.isna(event_row["asof_date"]) else None,
            "setup_id": event_row.get("setup_id"),
            "setup_name": event_row.get("setup_name"),
            "watch_reasons_json": normalize_jsonish(event_row.get("watch_reasons_json")),
        },
        "market_context": stock_context,
        "exchange_context": exchange_context,
        "document_context": document_payload,
    }


class AdvisoryEventEvaluator:
    def __init__(self, *, model: str = DEFAULT_MODEL, openai_client: OpenAI | None = None) -> None:
        self.model = model
        self.openai_client = openai_client

    def _get_openai_client(self) -> OpenAI:
        if self.openai_client is None:
            api_key = env("OPENAI_API_KEY", default=None)
            if not api_key:
                raise ValueError("OPENAI_API_KEY is not configured")
            self.openai_client = OpenAI(api_key=api_key)
        return self.openai_client

    def evaluate_payload(self, payload: dict[str, Any]) -> EventEvaluation:
        completion = self._get_openai_client().beta.chat.completions.parse(
            model=self.model,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": render_event_prompt(payload)},
            ],
            response_format=EventEvaluation,
        )
        return completion.choices[0].message.parsed


def build_outputs(
    events: pd.DataFrame,
    *,
    model: str,
) -> tuple[pd.DataFrame, pd.DataFrame, dict[str, Any]]:
    if events.empty:
        return pd.DataFrame(), pd.DataFrame(), {"input_event_count": 0, "evaluated_count": 0, "error_count": 0}

    docs = load_documents(events["unique_id"].dropna().astype(str).unique().tolist())
    docs_by_id = {
        str(row["unique_id"]): row
        for _, row in docs.iterrows()
    }
    evaluator = AdvisoryEventEvaluator(model=model)
    evaluation_rows: list[dict[str, Any]] = []
    risk_rows: list[dict[str, Any]] = []
    error_count = 0

    for _, event_row in events.iterrows():
        doc_row = docs_by_id.get(str(event_row["unique_id"]))
        payload = build_payload(event_row, doc_row)
        try:
            parsed = evaluator.evaluate_payload(payload)
            evaluation_status = "completed"
        except Exception as exc:
            error_count += 1
            parsed = EventEvaluation(
                what_happened=trim_text(event_row.get("concise_summary_text") or event_row.get("subject"), 1200)
                or "Document could not be evaluated automatically.",
                sentiment="neutral",
                materiality="medium",
                setup_effect="neutral",
                direction="neutral",
                surprise=0.0,
                novelty=0.0,
                contradiction=0.5,
                expected_decay_days=5,
                source_reliability="low",
                affected_sectors=[],
                affected_peers=[],
                governance_risk="none",
                balance_sheet_risk="none",
                execution_risk="none",
                investable_now=False,
                verdict="review_manual",
                event_class="OTHER",
                state_transition_hint="REVIEW_MANUAL",
                score_impact=0.0,
                confidence=0.0,
                rationale=f"Automatic LLM evaluation failed: {exc}",
                source_trace=["llm_error"],
                key_risks=[],
            )
            evaluation_status = "error"

        event_class, state_transition_hint, score_impact = normalize_event_evaluation(event_row, parsed)
        event_tensor = build_event_tensor(
            parsed,
            event_class=event_class,
            score_impact=score_impact,
            state_transition_hint=state_transition_hint,
        )

        evaluation_rows.append(
            {
                "asof_date": event_row["asof_date"],
                "published_on": event_row["published_on"],
                "evaluated_at": pd.Timestamp.utcnow(),
                "setup_id": event_row["setup_id"],
                "setup_name": event_row.get("setup_name"),
                "symbol": event_row["symbol"],
                "company_master_id": event_row.get("company_master_id"),
                "unique_id": event_row["unique_id"],
                "event_source": event_row.get("event_source"),
                "subject": event_row.get("subject"),
                "filed_under_category": event_row.get("filed_under_category"),
                "parse_status": event_row.get("parse_status"),
                "evaluation_status": evaluation_status,
                "sentiment": parsed.sentiment,
                "materiality": parsed.materiality,
                "setup_effect": parsed.setup_effect,
                "direction": parsed.direction,
                "surprise": event_tensor["surprise"],
                "novelty": event_tensor["novelty"],
                "contradiction": event_tensor["contradiction"],
                "expected_decay_days": event_tensor["expected_decay_days"],
                "source_reliability": parsed.source_reliability,
                "affected_sectors_json": json_dumps(event_tensor["affected_sectors"]),
                "affected_peers_json": json_dumps(event_tensor["affected_peers"]),
                "governance_risk": parsed.governance_risk,
                "balance_sheet_risk": parsed.balance_sheet_risk,
                "execution_risk": parsed.execution_risk,
                "investable_now": parsed.investable_now,
                "verdict": parsed.verdict,
                "event_class": event_class,
                "state_transition_hint": state_transition_hint,
                "score_impact": score_impact,
                "confidence": parsed.confidence,
                "what_happened": parsed.what_happened,
                "rationale": parsed.rationale,
                "event_tensor_json": json_dumps(event_tensor),
                "source_trace_json": json_dumps(parsed.source_trace),
                "context_snapshot_json": json_dumps(payload),
                "model_name": model,
                "prompt_version": ADVISORY_EVENT_PROMPT_VERSION,
                "load_ts": pd.Timestamp.utcnow(),
            }
        )

        for idx, risk in enumerate(parsed.key_risks, start=1):
            risk_rows.append(
                {
                    "published_on": event_row["published_on"],
                    "asof_date": event_row["asof_date"],
                    "setup_id": event_row["setup_id"],
                    "symbol": event_row["symbol"],
                    "unique_id": event_row["unique_id"],
                    "event_source": event_row.get("event_source"),
                    "risk_idx": idx,
                    "risk_type": risk.risk_type,
                    "severity": risk.severity,
                    "title": risk.title,
                    "detail": risk.detail,
                    "evidence_json": json_dumps(risk.evidence),
                    "load_ts": pd.Timestamp.utcnow(),
                }
            )

    meta = {
        "input_event_count": int(len(events)),
        "evaluated_count": int(len(evaluation_rows)),
        "error_count": int(error_count),
        "document_count": int(len(docs)),
        "model_name": model,
        "prompt_version": ADVISORY_EVENT_PROMPT_VERSION,
    }
    return pd.DataFrame(evaluation_rows), pd.DataFrame(risk_rows), meta


def delete_existing_risks(evaluations: pd.DataFrame) -> None:
    if evaluations.empty:
        return
    keys = evaluations[["setup_id", "symbol", "unique_id"]].drop_duplicates().itertuples(index=False, name=None)
    with db_session() as (_, cur):
        cur.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {RISKS_TABLE} (
                published_on TIMESTAMPTZ,
                asof_date TIMESTAMPTZ,
                setup_id TEXT,
                symbol TEXT,
                unique_id TEXT,
                risk_idx BIGINT
            )
            """
        )
        for setup_id, symbol, unique_id in keys:
            cur.execute(
                f"DELETE FROM {RISKS_TABLE} WHERE setup_id = %s AND symbol = %s AND unique_id = %s",
                (setup_id, symbol, unique_id),
            )


def ensure_output_tables() -> None:
    with db_session() as (_, cur):
        cur.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {EVALUATIONS_TABLE} (
                published_on TIMESTAMPTZ NOT NULL,
                asof_date TIMESTAMPTZ,
                evaluated_at TIMESTAMPTZ,
                setup_id TEXT NOT NULL,
                setup_name TEXT,
                symbol TEXT NOT NULL,
                company_master_id TEXT,
                unique_id TEXT NOT NULL,
                event_source TEXT,
                subject TEXT,
                filed_under_category TEXT,
                parse_status TEXT,
                evaluation_status TEXT,
                sentiment TEXT,
                materiality TEXT,
                setup_effect TEXT,
                direction TEXT,
                surprise DOUBLE PRECISION,
                novelty DOUBLE PRECISION,
                contradiction DOUBLE PRECISION,
                expected_decay_days INTEGER,
                source_reliability TEXT,
                affected_sectors_json TEXT,
                affected_peers_json TEXT,
                governance_risk TEXT,
                balance_sheet_risk TEXT,
                execution_risk TEXT,
                investable_now BOOLEAN,
                verdict TEXT,
                event_class TEXT,
                state_transition_hint TEXT,
                score_impact DOUBLE PRECISION,
                confidence DOUBLE PRECISION,
                what_happened TEXT,
                rationale TEXT,
                event_tensor_json TEXT,
                source_trace_json TEXT,
                context_snapshot_json TEXT,
                model_name TEXT,
                prompt_version TEXT,
                load_ts TIMESTAMPTZ,
                UNIQUE (published_on, setup_id, symbol, unique_id)
            )
            """
        )
        cur.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {RISKS_TABLE} (
                published_on TIMESTAMPTZ NOT NULL,
                asof_date TIMESTAMPTZ,
                setup_id TEXT NOT NULL,
                symbol TEXT NOT NULL,
                unique_id TEXT NOT NULL,
                event_source TEXT,
                risk_idx BIGINT NOT NULL,
                risk_type TEXT,
                severity TEXT,
                title TEXT,
                detail TEXT,
                evidence_json TEXT,
                load_ts TIMESTAMPTZ,
                UNIQUE (published_on, setup_id, symbol, unique_id, risk_idx)
            )
            """
        )
        cur.execute(f"ALTER TABLE {EVALUATIONS_TABLE} ADD COLUMN IF NOT EXISTS event_source TEXT")
        cur.execute(f"ALTER TABLE {EVALUATIONS_TABLE} ADD COLUMN IF NOT EXISTS event_class TEXT")
        cur.execute(f"ALTER TABLE {EVALUATIONS_TABLE} ADD COLUMN IF NOT EXISTS state_transition_hint TEXT")
        cur.execute(f"ALTER TABLE {EVALUATIONS_TABLE} ADD COLUMN IF NOT EXISTS score_impact DOUBLE PRECISION")
        cur.execute(f"ALTER TABLE {EVALUATIONS_TABLE} ADD COLUMN IF NOT EXISTS direction TEXT")
        cur.execute(f"ALTER TABLE {EVALUATIONS_TABLE} ADD COLUMN IF NOT EXISTS surprise DOUBLE PRECISION")
        cur.execute(f"ALTER TABLE {EVALUATIONS_TABLE} ADD COLUMN IF NOT EXISTS novelty DOUBLE PRECISION")
        cur.execute(f"ALTER TABLE {EVALUATIONS_TABLE} ADD COLUMN IF NOT EXISTS contradiction DOUBLE PRECISION")
        cur.execute(f"ALTER TABLE {EVALUATIONS_TABLE} ADD COLUMN IF NOT EXISTS expected_decay_days INTEGER")
        cur.execute(f"ALTER TABLE {EVALUATIONS_TABLE} ADD COLUMN IF NOT EXISTS source_reliability TEXT")
        cur.execute(f"ALTER TABLE {EVALUATIONS_TABLE} ADD COLUMN IF NOT EXISTS affected_sectors_json TEXT")
        cur.execute(f"ALTER TABLE {EVALUATIONS_TABLE} ADD COLUMN IF NOT EXISTS affected_peers_json TEXT")
        cur.execute(f"ALTER TABLE {EVALUATIONS_TABLE} ADD COLUMN IF NOT EXISTS event_tensor_json TEXT")
        cur.execute(f"ALTER TABLE {RISKS_TABLE} ADD COLUMN IF NOT EXISTS event_source TEXT")


def persist_outputs(evaluations: pd.DataFrame, risks: pd.DataFrame) -> None:
    ensure_output_tables()
    if not evaluations.empty:
        upsert_to_db(
            evaluations,
            EVALUATIONS_TABLE,
            unique_keys=["published_on", "setup_id", "symbol", "unique_id"],
            timescaledb_column="published_on",
        )
    delete_existing_risks(evaluations)
    if not risks.empty:
        upsert_to_db(
            risks,
            RISKS_TABLE,
            unique_keys=["published_on", "setup_id", "symbol", "unique_id", "risk_idx"],
            timescaledb_column="published_on",
        )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run LLM evaluation for triggered advisory announcement watch events.")
    parser.add_argument("--date", type=parse_datetime_arg, help="Watch-event asof date in YYYY-MM-DD")
    parser.add_argument("--symbols", nargs="*", help="Optional symbols")
    parser.add_argument("--setup", dest="setup_ids", nargs="*", help="Optional setup ids")
    parser.add_argument("--limit", type=int, help="Optional max number of events to evaluate")
    parser.add_argument("--model", default=DEFAULT_MODEL, help="OpenAI model name")
    parser.add_argument("--include-evaluated", action="store_true", help="Re-evaluate rows already present in advisory_event_evaluations")
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def summarize(evaluations: pd.DataFrame, risks: pd.DataFrame, meta: dict[str, Any]) -> dict[str, Any]:
    return {
        "status": "ok",
        "evaluations_table": EVALUATIONS_TABLE,
        "risks_table": RISKS_TABLE,
        **meta,
        "risk_row_count": int(len(risks)),
        "sample": evaluations.head(10).to_dict(orient="records") if not evaluations.empty else [],
    }


def main() -> int:
    args = parse_args()
    asof_date = pd.Timestamp(args.date, tz="UTC") if args.date else None
    events = load_watch_events(
        asof_date=asof_date,
        symbols=args.symbols,
        setup_ids=args.setup_ids,
        include_evaluated=bool(args.include_evaluated),
        limit=args.limit,
    )
    evaluations, risks, meta = build_outputs(events, model=args.model)
    if not args.dry_run:
        persist_outputs(evaluations, risks)
    result = summarize(evaluations, risks, meta)
    result["dry_run"] = bool(args.dry_run)
    print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
