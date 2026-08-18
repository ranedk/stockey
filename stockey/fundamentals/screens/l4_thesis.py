"""L4 thesis register -- fundamental screener step 8
(docs/FUNDAMENTAL_SCREENER_PRD.md sec 8 step 8, fundamental_basic_goal.md sec 1's L4
section and sec 5's scoring). The only gate capital passes through: a human commits to
a falsifiable, dated fundamental prediction (never a price target) before any position
is sized. Nothing in this module opens or sizes a position, and nothing in it decides
FOR the human whether a thesis is worth taking -- it is pure record-keeping plus
scoring, matching stockey's data-platform boundary (docs/FUNDAMENTAL_SCREENER_PRD.md
sec 1: "the two must never be blended into one decision pipeline").

Mandatory at entry (fundamental_basic_goal.md's own wording): prediction_text,
target_date, invalidation_criteria, origin_tag are all required to create a thesis --
create_thesis() enforces this, it does not treat them as optional metadata.

A thesis MAY reference the L3 alert that prompted it (source/news_id/trigger_type,
origin_tag="systematic_screen") or stand alone (origin_tag="ad_hoc", a human's own
idea not sourced from any alert this system generated) -- both are first-class, per
the source PRD's origin_tag distinction. Either way, creating a thesis is always a
deliberate, separate human act from an alert firing -- L3 never auto-creates one.

Structured, machine-checkable predictions are optional, not required: prediction_text
is always free text (a human writes the actual falsifiable claim), but a thesis MAY
additionally carry (metric_name, metric_operator, metric_threshold) when the
prediction happens to reduce to a simple L2-state comparison (e.g. "net_debt_rscr < 50
by Q3 FY27"). check_structured_prediction() then offers a SUGGESTED read against
current L2 state -- it is explicitly not an auto-resolver. Resolution (resolved_true,
and for a false resolution, the mandatory thesis_wrong vs
thesis_right_market_hasnt_paid failure_attribution -- fundamental_basic_goal.md sec 5
is explicit these "look identical in P&L and demand opposite corrections") is always a
separate, explicit human call to resolve_thesis().

Scoring (fundamental_basic_goal.md sec 5's primary outcome measure, not returns):
forecast hit rate, failure attribution breakdown, and time-to-confirmation, computed
from whatever has actually been resolved so far -- calibration-by-signal-type is
deferred until enough resolved theses of the same signal_definition_version exist to
say anything (the source PRD's own target is "60-100 scored forecasts over two
years"; this system has zero right now).
"""

from __future__ import annotations

import hashlib
import json

import pandas as pd

from utils.db import db_session, execute_db_operation, sql_to_df, upsert_to_db

SYNC_SOURCE_NAME = "fundamentals.screens.l4_thesis"
RESULTS_TABLE = "fundamentals_l4_thesis"
STOCKEY_RUN_STATE: dict[str, object] = {}

ORIGIN_TAGS = ("systematic_screen", "ad_hoc")
FAILURE_ATTRIBUTIONS = ("thesis_wrong", "thesis_right_market_hasnt_paid")

_THESIS_TABLE_STATEMENT = """
    CREATE TABLE IF NOT EXISTS fundamentals_l4_thesis (
        thesis_id TEXT PRIMARY KEY,
        company_master_id TEXT NOT NULL,
        source_alert_source TEXT,
        source_alert_news_id TEXT,
        source_alert_trigger_type TEXT,
        origin_tag TEXT NOT NULL,
        prediction_text TEXT NOT NULL,
        target_date DATE NOT NULL,
        invalidation_criteria TEXT NOT NULL,
        signal_definition_version TEXT,
        metric_name TEXT,
        metric_operator TEXT,
        metric_threshold DOUBLE PRECISION,
        created_date DATE NOT NULL,
        status TEXT NOT NULL,
        resolved_true BOOLEAN,
        resolution_date DATE,
        resolution_notes TEXT,
        failure_attribution TEXT,
        load_ts TIMESTAMPTZ
    )
"""

_VALID_OPERATORS = ("<", "<=", ">", ">=", "==")


class ThesisValidationError(ValueError):
    """Raised when create_thesis/resolve_thesis is called without the fields
    fundamental_basic_goal.md's L4 section makes mandatory -- these are enforced, not
    merely documented as expected."""


def _ensure_thesis_table() -> None:
    def _op() -> None:
        with db_session() as (_, cur):
            cur.execute(_THESIS_TABLE_STATEMENT)

    execute_db_operation(_op, operation_name="fundamentals_l4_thesis:ensure_table")


def _make_thesis_id(company_master_id: str, prediction_text: str, created_date) -> str:
    digest = hashlib.sha256(f"{company_master_id}|{prediction_text}|{created_date}".encode("utf-8")).hexdigest()[:16]
    return f"thesis:{digest}"


def create_thesis(
    *,
    company_master_id: str,
    prediction_text: str,
    target_date,
    invalidation_criteria: str,
    origin_tag: str,
    signal_definition_version: str | None = None,
    source_alert: dict | None = None,
    metric_name: str | None = None,
    metric_operator: str | None = None,
    metric_threshold: float | None = None,
    created_date=None,
) -> dict:
    if not prediction_text or not prediction_text.strip():
        raise ThesisValidationError("prediction_text is mandatory -- a thesis must state a falsifiable claim")
    if target_date is None:
        raise ThesisValidationError("target_date is mandatory -- a thesis must be dated, not open-ended")
    if not invalidation_criteria or not invalidation_criteria.strip():
        raise ThesisValidationError("invalidation_criteria is mandatory -- must be pre-committed at entry, not decided later")
    if origin_tag not in ORIGIN_TAGS:
        raise ThesisValidationError(f"origin_tag must be one of {ORIGIN_TAGS}, got {origin_tag!r}")
    if metric_operator is not None and metric_operator not in _VALID_OPERATORS:
        raise ThesisValidationError(f"metric_operator must be one of {_VALID_OPERATORS}, got {metric_operator!r}")

    _ensure_thesis_table()
    created_date = created_date or pd.Timestamp.now(tz="UTC").date()
    thesis_id = _make_thesis_id(company_master_id, prediction_text, created_date)
    source_alert = source_alert or {}

    # BUG FOUND LIVE 2026-08-15, fixed here: thesis_id is a deterministic hash of
    # (company_master_id, prediction_text, created_date) only, and this used to
    # upsert unconditionally -- a re-submission with IDENTICAL inputs (e.g. a
    # retried API call after a network blip) would silently overwrite status/
    # resolved_true/resolution_date/resolution_notes/failure_attribution back to
    # blank, wiping a real forecast outcome from the scoring ledger this whole
    # module exists to keep honest. Reproduced live: create -> resolve(False,
    # thesis_wrong) -> create again with the same inputs silently reverted the row
    # to status=open, resolved_true=None. A resolved thesis is now immutable via
    # this path -- resolve_thesis() is the only way to change it further, matching
    # "resolution is always a separate, explicit human call" from the module
    # docstring.
    existing = sql_to_df(
        "SELECT status FROM fundamentals_l4_thesis WHERE thesis_id = %s",
        params=(thesis_id,),
    )
    if not existing.empty and existing.iloc[0]["status"] == "resolved":
        raise ThesisValidationError(
            f"thesis {thesis_id!r} is already resolved -- re-submitting identical "
            "(company_master_id, prediction_text, created_date) would silently wipe "
            "its resolution; if you meant a new thesis, change the prediction text "
            "or created_date so it gets a distinct id"
        )

    row = {
        "thesis_id": thesis_id,
        "company_master_id": company_master_id,
        "source_alert_source": source_alert.get("source"),
        "source_alert_news_id": source_alert.get("news_id"),
        "source_alert_trigger_type": source_alert.get("trigger_type"),
        "origin_tag": origin_tag,
        "prediction_text": prediction_text.strip(),
        "target_date": target_date,
        "invalidation_criteria": invalidation_criteria.strip(),
        "signal_definition_version": signal_definition_version,
        "metric_name": metric_name,
        "metric_operator": metric_operator,
        "metric_threshold": metric_threshold,
        "created_date": created_date,
        "status": "open",
        "resolved_true": None,
        "resolution_date": None,
        "resolution_notes": None,
        "failure_attribution": None,
        "load_ts": pd.Timestamp.now(tz="UTC"),
    }
    upsert_to_db(pd.DataFrame([row]), RESULTS_TABLE, unique_keys=["thesis_id"])
    return row


def resolve_thesis(
    *,
    thesis_id: str,
    resolved_true: bool,
    resolution_notes: str | None = None,
    failure_attribution: str | None = None,
    resolution_date=None,
) -> None:
    if not resolved_true and failure_attribution not in FAILURE_ATTRIBUTIONS:
        raise ThesisValidationError(
            f"resolving a thesis as false requires failure_attribution in {FAILURE_ATTRIBUTIONS} -- "
            "'thesis_wrong' and 'thesis_right_market_hasnt_paid' look identical in P&L and demand "
            "opposite corrections, this cannot be left unset"
        )
    if resolved_true and failure_attribution is not None:
        raise ThesisValidationError("failure_attribution must be None when resolved_true is True")

    resolution_date = resolution_date or pd.Timestamp.now(tz="UTC").date()

    def _update() -> None:
        with db_session() as (_, cur):
            cur.execute(
                """
                UPDATE fundamentals_l4_thesis
                   SET status = 'resolved', resolved_true = %s, resolution_date = %s,
                       resolution_notes = %s, failure_attribution = %s
                 WHERE thesis_id = %s
                """,
                (resolved_true, resolution_date, resolution_notes, failure_attribution, thesis_id),
            )

    execute_db_operation(_update, operation_name="fundamentals_l4_thesis:resolve")


def check_structured_prediction(thesis_row: dict) -> bool | None:
    """A SUGGESTED read against current L2 state for a thesis with a structured
    (metric_name, metric_operator, metric_threshold) -- never auto-resolves anything,
    see module docstring."""
    metric_name = thesis_row.get("metric_name")
    operator = thesis_row.get("metric_operator")
    threshold = thesis_row.get("metric_threshold")
    if not metric_name or not operator or threshold is None:
        return None

    ticker = str(thesis_row.get("company_master_id") or "").removeprefix("nse:")
    df = sql_to_df(
        "SELECT * FROM fundamentals_l2_state WHERE ticker = %s ORDER BY run_date DESC LIMIT 1",
        params=(ticker,),
    )
    if df.empty or metric_name not in df.columns:
        return None
    value = df.iloc[0][metric_name]
    if value is None or pd.isna(value):
        return None
    # BUG FOUND LIVE 2026-08-17: metric_name is a free-text column name a human
    # types when creating a thesis (create_thesis()'s metric_name/metric_operator/
    # metric_threshold args) -- nothing validates it actually names a NUMERIC L2
    # state column. fundamentals_l2_state has plenty of non-numeric columns
    # (trend_direction, company_name, ticker, ...); picking one of those here used
    # to raise an uncaught TypeError from `value < threshold` (str vs number)
    # instead of the graceful "can't evaluate this" None every other branch of this
    # function already returns. Currently dead code (no caller yet), but the crash
    # was real and reproducible. float(value) (not isinstance(value, (int, float)))
    # deliberately -- a real L2 numeric column can come back as numpy.int64/float64
    # from the DataFrame, neither of which is a Python int/float subclass, so an
    # isinstance check alone would wrongly reject genuinely numeric values too.
    if isinstance(value, (bool, str)):
        return None
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None

    if operator == "<":
        return bool(value < threshold)
    if operator == "<=":
        return bool(value <= threshold)
    if operator == ">":
        return bool(value > threshold)
    if operator == ">=":
        return bool(value >= threshold)
    if operator == "==":
        return bool(value == threshold)
    return None


def load_open_theses_past_target_date(*, as_of_date=None) -> pd.DataFrame:
    as_of_date = as_of_date or pd.Timestamp.now(tz="UTC").date()
    return sql_to_df(
        "SELECT * FROM fundamentals_l4_thesis WHERE status = 'open' AND target_date <= %s ORDER BY target_date ASC",
        params=(as_of_date,),
    )


def compute_quarterly_scoring(*, as_of_date=None) -> dict[str, object]:
    """fundamental_basic_goal.md sec 5's primary outcome measure -- forecast accuracy,
    not returns. Computed from whatever has actually been resolved; calibration-by-
    signal-type is deferred until enough same-signal resolved theses exist to say
    anything (source PRD's own target is 60-100 scored forecasts over two years)."""
    as_of_date = as_of_date or pd.Timestamp.now(tz="UTC").date()
    all_theses = sql_to_df("SELECT * FROM fundamentals_l4_thesis")
    if all_theses.empty:
        return {
            "as_of_date": str(as_of_date),
            "total_theses": 0,
            "open": 0,
            "resolved": 0,
            "hit_rate": None,
            "failure_attribution_breakdown": {},
            "time_to_confirmation_days": {},
        }

    resolved = all_theses[all_theses["status"] == "resolved"]
    open_count = int((all_theses["status"] == "open").sum())

    hit_rate = None
    failure_breakdown: dict[str, int] = {}
    time_to_confirmation: dict[str, object] = {}
    if not resolved.empty:
        hit_rate = round(float(resolved["resolved_true"].mean()) * 100, 1)
        false_resolved = resolved[resolved["resolved_true"] == False]  # noqa: E712 -- pandas boolean column comparison
        failure_breakdown = false_resolved["failure_attribution"].value_counts().to_dict()

        true_resolved = resolved[resolved["resolved_true"] == True]  # noqa: E712
        if not true_resolved.empty:
            days = (pd.to_datetime(true_resolved["resolution_date"]) - pd.to_datetime(true_resolved["created_date"])).dt.days
            time_to_confirmation = {
                "median_days": float(days.median()),
                "mean_days": round(float(days.mean()), 1),
                "count": int(len(days)),
            }

    return {
        "as_of_date": str(as_of_date),
        "total_theses": int(len(all_theses)),
        "open": open_count,
        "resolved": int(len(resolved)),
        "hit_rate": hit_rate,
        "failure_attribution_breakdown": failure_breakdown,
        "time_to_confirmation_days": time_to_confirmation,
    }


def main() -> int:
    global STOCKEY_RUN_STATE
    result = compute_quarterly_scoring()
    STOCKEY_RUN_STATE = {
        "source": SYNC_SOURCE_NAME,
        "rows": result["total_theses"],
        "rows_written": 0,  # scoring is read-only; thesis creation/resolution are separate deliberate calls
        "fallback_used": False,
        "state_advanced": False,
        "status": "ok",
        **result,
    }
    print(json.dumps(STOCKEY_RUN_STATE, ensure_ascii=False, default=str), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
