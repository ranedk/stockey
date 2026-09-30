"""LLM adjudication over the mechanical ruleset -- docs/PORTFOLIO_RULESET_PRD.md.

The adjudicator has ASYMMETRIC power, and the asymmetry is the design:

  ENTRY  -- may only REJECT a candidate the ruleset passed. It can never add a name.
            The ruleset stays the upper bound on what enters; worst case is a trade not
            taken, which is bounded.
  EXIT   -- may only DEFER, and only for exits that are about the thesis rather than
            about capital:
              stop_loss           -> NEVER deferrable. Unconditional.
              thesis_invalidation -> deferrable ONE cycle, must re-justify next run.
              target_date         -> deferrable.

Why exits are not the mirror image of entries: vetoing an exit means staying in a
position the rule says to leave. That is not a subset of the rule, it is an EXTENSION of
exposure, with unbounded downside -- the classic way a systematic process degrades, and
what systrader's Law 19 exists to prevent. Every argument for holding through a stop is
available at every price, which is precisely why a stop must not be arguable.

The stop LEVEL is likewise not the adjudicator's to choose (see portfolio_ruleset.
compute_stop_pct). If the model picked it, it could neuter the one unvetoable rule by
always choosing the widest allowed value -- the ban would survive in letter and not in
spirit.

EVERY decision records model + prompt version. Without those, the first model or prompt
change silently turns one strategy's sample into two, and the paired taken-vs-rejected
comparison the PRD relies on stops meaning anything.
"""
from __future__ import annotations

import json

import pandas as pd
from environs import Env
from openai import OpenAI

from utils.db import db_session, sql_to_df
from utils.schema_migrations import apply_schema_migration

env = Env()
env.read_env()

SYNC_SOURCE_NAME = "fundamentals.screens.portfolio_adjudicator"
DEFAULT_MODEL = env("PORTFOLIO_ADJUDICATOR_MODEL", "gpt-5.4-mini")
# 4 (2026-09-30): ruleset v3 candidates carry the story score, its strongest readings and the
# latest LLM story read instead of confluence axes; the prompt names both kinds of evidence.
PROMPT_VERSION = 4

# The adjudicator writes the thesis (prediction, target date, invalidation criteria) for
# every name it accepts -- the PRD's architecture step "L4 thesis written". It does NOT go
# into a separate register: the human one (fundamentals_l4_thesis) was deleted 2026-09-04
# and must not come back -- it was a manual gate nothing in the nightly run could satisfy.
# portfolio_resolution.py grades these forecasts unattended.
#
# target_date is CLAMPED, for the same reason the stop level is not the adjudicator's to
# choose: an unclamped model can push the date far enough out that the target_date exit
# can never fire, neutering a rule in a letter-abiding way.
MIN_HOLD_DAYS = env.int("PORTFOLIO_MIN_HOLD_DAYS", 60)
MAX_HOLD_DAYS = env.int("PORTFOLIO_MAX_HOLD_DAYS", 365)

METRIC_OPERATORS = ("<", "<=", ">", ">=", "==")


def numeric_l2_metrics() -> list[str]:
    """The numeric columns of fundamentals_l2_state, read live.

    Derived from information_schema rather than hardcoded, per this repo's "no static
    registry" principle -- an L2 column added later becomes available to the adjudicator
    without a code change, and a column REMOVED stops being offered instead of silently
    producing an unresolvable forecast.
    """
    df = sql_to_df(
        """
        SELECT column_name FROM information_schema.columns
         WHERE table_name = 'fundamentals_l2_state'
           AND data_type IN ('double precision','integer','numeric','bigint','real')
         ORDER BY column_name
        """
    )
    skip = {"company_id", "state_vector_version"}   # identifiers, not metrics
    return [c for c in df["column_name"] if c not in skip]


DEFERRABLE_EXIT_REASONS = ("thesis_invalidation", "target_date", "story_fading")
# v3 (2026-09-30): a trailing stop is a stop, and a broken thesis (a flaw on the live score or a
# hard negative alert since entry) exits at once -- PRD 5.3 rows 1, 3, 4.
UNCONDITIONAL_EXIT_REASONS = ("stop_loss", "trailing_stop", "thesis_broken")
MAX_DEFERRALS = 1  # "defer, not cancel" -- a veto that persists silently IS no exit rule

ENTRY_SYSTEM_PROMPT = """You review candidates a mechanical ruleset has ALREADY passed for a \
fundamental swing/long-term portfolio.

Your only power is to REJECT. You cannot add companies, change position sizes, or set \
stop-losses -- those are decided mechanically and are not yours to influence.

Reject only when the signal DETAIL contradicts what the rule inferred. The rule passed the \
name either on its story score (how exceptional its best fundamental reading is within its \
peer group, with no deal-breaker flaw) or on confluence axes. Legitimate reasons: the \
supporting reading or axis is technically true but driven by a one-off (an asset sale \
flattering the debt trajectory, a single lumpy quarter), the narrative or the story read \
describes a situation the numbers cannot see (pending litigation, promoter exit, an \
accounting concern), or the evidence is so thin that "nothing contradicts" reflects absence \
of data rather than genuine health.

Do NOT reject because you would prefer a different rule, because you dislike the sector, \
or because you would rather wait for a better price. The ruleset already made those \
choices; second-guessing them here is how a versioned rule silently becomes an \
unversioned one.

When in doubt, PASS. A rejection needs a specific, falsifiable reason -- something a \
human could later check and say "that was right" or "that was wrong".

For every candidate you ACCEPT you must also commit to a falsifiable thesis:
  prediction_text        -- a FUNDAMENTAL claim, never a price target. "Net debt falls \
below 2x EBITDA by Q3 FY27", not "the stock reaches 500".
  target_date            -- YYYY-MM-DD, the date by which the prediction should be \
checkable. Between 60 and 365 days out.
  invalidation_criteria  -- what observation would tell you the thesis is WRONG, stated \
so that a reader in six months can check it without you.

You must ALSO reduce the prediction to a machine-checkable form wherever it honestly \
reduces: metric_name (one of the L2 state columns listed in the user message, exactly as \
spelled), metric_operator (one of < <= > >= ==), metric_threshold (a number). This is what \
lets the forecast be graded by DATA rather than by a model reading its own prose. If the \
prediction genuinely does not reduce to one of those columns, set all three to null and say \
so -- inventing a column name, or bending the prediction to fit a column that does not \
measure it, is worse than leaving it unstructured.

If you REJECT, set every one of these to null. Write them for accepts as if you will be \
scored on them, because the record is kept and they will be."""

EXIT_SYSTEM_PROMPT = """You review exits a mechanical rule has triggered on an open position.

You may only DEFER an exit by one evaluation cycle, and only when the trigger is about \
the THESIS rather than about capital. You will never be asked about a stop-loss: those \
are unconditional.

Defer only when the triggering condition looks like noise rather than a real change -- \
e.g. an invalidation criterion tripped on a single restated figure, or a sector axis \
flipped for reasons unrelated to the company. Materially nothing has changed.

Do NOT defer because the position is losing money and you expect recovery, because the \
story is still appealing, or because exiting feels premature. Those are the arguments \
that turn a temporary hold into a permanent one.

When in doubt, EXIT. You will be asked again next cycle if the position survives, and \
you must re-justify any deferral then."""

ENTRY_SCHEMA = {
    "type": "object",
    "properties": {
        "decision": {"type": "string", "enum": ["accept", "reject"]},
        "reason": {"type": "string"},
        "concern_axis": {"type": ["string", "null"]},
        "prediction_text": {"type": ["string", "null"]},
        "target_date": {"type": ["string", "null"]},
        "invalidation_criteria": {"type": ["string", "null"]},
        "metric_name": {"type": ["string", "null"]},
        "metric_operator": {"type": ["string", "null"], "enum": ["<", "<=", ">", ">=", "==", None]},
        "metric_threshold": {"type": ["number", "null"]},
    },
    "required": ["decision", "reason", "concern_axis", "prediction_text",
                 "target_date", "invalidation_criteria",
                 "metric_name", "metric_operator", "metric_threshold"],
    "additionalProperties": False,
}

EXIT_SCHEMA = {
    "type": "object",
    "properties": {
        "decision": {"type": "string", "enum": ["exit", "defer"]},
        "reason": {"type": "string"},
    },
    "required": ["decision", "reason"],
    "additionalProperties": False,
}


def _ensure_tables() -> None:
    """Defensive DDL in the READER path too.

    upsert_to_db's CREATE TABLE IF NOT EXISTS only fires on a WRITE, so a reader that
    runs before the first write gets UndefinedTable. That has bitten this repo three
    times (see confluence_score.py's own note); these tables are created here rather
    than relied upon.
    """
    with db_session() as (_conn, cur):
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS fundamentals_portfolio_position (
                position_id       text PRIMARY KEY,
                company_master_id text NOT NULL,
                ticker            text NOT NULL,
                ruleset_version   integer NOT NULL,
                -- 'shadow' rows are candidates the adjudicator REJECTED, tracked as if
                -- taken. They are what makes the adjudicator itself falsifiable: a
                -- paired taken-vs-rejected comparison under one ruleset over one period
                -- says far more on a small sample than any absolute hit rate. Never
                -- counted as real P&L.
                kind              text NOT NULL CHECK (kind IN ('real','shadow')),
                status            text NOT NULL CHECK (status IN ('open','closed')),
                opened_at         timestamptz NOT NULL DEFAULT now(),
                entry_price       double precision,
                stop_pct          double precision,
                stop_basis        text,
                confluence_count  integer,
                contradicting_count integer,
                evaluable_count   integer,
                stage_at_entry    integer,
                adjudicator_model text,
                adjudicator_prompt_version integer,
                adjudicator_reason text,
                closed_at         timestamptz,
                close_reason      text,
                exit_price        double precision,
                deferral_count    integer NOT NULL DEFAULT 0,
                last_deferred_at  timestamptz,
                -- The thesis the adjudicator committed to at entry. Kept HERE and not in
                -- a separate register on purpose: the human one was deleted 2026-09-04
                -- and there is no human act left to gate on.
                -- target_date is the only one of the three that is a live exit
                -- trigger; the other two are the audit record that makes a position's
                -- reasoning checkable six months later.
                -- kind and entry_decision answer DIFFERENT questions and must not be
                -- collapsed. kind = "is real money at risk"; entry_decision = "what did
                -- the adjudicator say". In record-only mode every row is kind='shadow'
                -- whether it was accepted or rejected, so keying the paired
                -- taken-vs-rejected comparison off kind would silently compare the
                -- rollout phase against itself. It is entry_decision that pairs.
                entry_decision        text CHECK (entry_decision IN ('accept','reject')),
                -- L5 sizing as of ENTRY, frozen. Recomputing it later would silently
                -- restate history every time ADV moved, and the question a review asks is
                -- "what did we commit", not "what would we commit today".
                position_size_rs      double precision,
                adv_cap_rs            double precision,
                sizing_basis          text,
                prediction_text       text,
                -- The prediction reduced to a machine-checkable comparison against
                -- fundamentals_l2_state, so the forecast is graded by DATA rather than by
                -- a model reading its own prose. NULL where the prediction honestly does
                -- not reduce; portfolio_resolution.py then falls back to a judged read
                -- and records WHICH path was used, so the two hit rates stay separable.
                metric_name           text,
                metric_operator       text,
                metric_threshold      double precision,
                metric_rejected_reason text,
                -- Forecast outcome. Independent of whether the POSITION is still open: a
                -- name stopped out in month two still has a forecast that resolves at its
                -- target date, and that separation is exactly what distinguishes
                -- "thesis_wrong" from "thesis_right_market_hasnt_paid".
                resolved_true         boolean,
                resolution_date       date,
                resolution_method     text,
                resolution_notes      text,
                failure_attribution   text,
                target_date           date,
                invalidation_criteria text,
                target_date_basis     text
            )
            """
        )
        # The table predates these four columns (first positions written 2026-09-04).
        for column, coltype in (("bucket", "text"), ("bucket_capital_rs", "double precision"),
                                ("entry_decision", "text"), ("prediction_text", "text"),
                                ("position_size_rs", "double precision"),
                                ("adv_cap_rs", "double precision"), ("sizing_basis", "text"),
                                ("metric_name", "text"), ("metric_operator", "text"),
                                ("metric_threshold", "double precision"),
                                ("metric_rejected_reason", "text"),
                                ("resolved_true", "boolean"), ("resolution_date", "date"),
                                ("resolution_method", "text"), ("resolution_notes", "text"),
                                ("failure_attribution", "text"), ("target_date", "date"),
                                ("invalidation_criteria", "text"),
                                ("target_date_basis", "text"),
                                # 2026-09-23 data audit. score_version: which confluence
                                # scorer produced the entry counts -- without it a scorer fix
                                # reads as "a contradiction appeared since entry" (ASHIANA
                                # was closed exactly that way the day v2 shipped).
                                # baseline_*: the counts the exit compares against, re-set
                                # when the scorer version changes (entry counts stay frozen).
                                # entry_price_date: the bar entry_price came from; without it
                                # a stale entry close (seen 2026-09-04..15) is undetectable.
                                ("score_version", "integer"),
                                ("baseline_score_version", "integer"),
                                ("baseline_contradicting_count", "integer"),
                                ("entry_price_date", "date"),
                                # ruleset v3 (portfolio_v3.py, 2026-09-30): the weight and story
                                # the entry was sized on, and the state its exits ratchet.
                                ("target_weight", "double precision"),
                                ("story_score_at_entry", "double precision"),
                                ("primary_dimension_at_entry", "text"), ("sector_code", "text"),
                                ("daily_vol_pct", "double precision"), ("trail_high", "double precision"),
                                ("trim_count", "integer")):
            cur.execute(
                "ALTER TABLE fundamentals_portfolio_position "
                "ADD COLUMN IF NOT EXISTS " + column + " " + coltype
            )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS fundamentals_portfolio_decision (
                decision_id     bigserial PRIMARY KEY,
                decided_at      timestamptz NOT NULL DEFAULT now(),
                phase           text NOT NULL CHECK (phase IN ('entry','exit','resolution')),
                company_master_id text NOT NULL,
                ruleset_version integer NOT NULL,
                decision        text NOT NULL,
                reason          text,
                model           text,
                prompt_version  integer,
                payload_json    text
            )
            """
        )
    # Forecast resolutions were written as phase='exit' (decision forecast_true/false),
    # polluting the exit vocabulary; they get their own phase (2026-09-23 audit).
    apply_schema_migration(
        migration_id="20260923_portfolio_decision_resolution_phase",
        description="fundamentals_portfolio_decision.phase: allow 'resolution'.",
        owner=SYNC_SOURCE_NAME,
        metadata={"tables": ["fundamentals_portfolio_decision"]},
        statements=[
            "ALTER TABLE fundamentals_portfolio_decision DROP CONSTRAINT IF EXISTS fundamentals_portfolio_decision_phase_check",
            "ALTER TABLE fundamentals_portfolio_decision ADD CONSTRAINT fundamentals_portfolio_decision_phase_check "
            "CHECK (phase IN ('entry','exit','resolution'))",
        ],
    )


_PROMPT_VERSION_FROM_MODEL = object()


def _record_decision(*, phase, company_master_id, ruleset_version, decision, reason, model, payload,
                     prompt_version=_PROMPT_VERSION_FROM_MODEL) -> None:
    # The entry prompt's version used to be written on EVERY row -- stop-loss exits and
    # forecast resolutions included, where no model (or a different prompt) ran
    # (2026-09-23 audit). Default: this module's prompt version when a model decided,
    # NULL when none did; callers with their own prompt pass it explicitly.
    if prompt_version is _PROMPT_VERSION_FROM_MODEL:
        prompt_version = PROMPT_VERSION if model else None
    with db_session() as (_conn, cur):
        cur.execute(
            """
            INSERT INTO fundamentals_portfolio_decision
                   (phase, company_master_id, ruleset_version, decision, reason, model, prompt_version, payload_json)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (phase, company_master_id, ruleset_version, decision, reason, model,
             prompt_version, json.dumps(payload, default=str)),
        )


def _validate_metric(result: dict, allowed: list) -> dict:
    """Drop a structured metric the model made up.

    metric_name is free text from a model, and nothing else checks that it names a real
    NUMERIC L2 column -- the exact bug that used to crash the old human register's
    resolver with a str-vs-number TypeError. An invented column here is worse than no
    metric: it produces a forecast that LOOKS machine-checkable and silently never
    resolves. So an unrecognised name is discarded, loudly, rather than stored.
    """
    name = result.get("metric_name")
    operator = result.get("metric_operator")
    threshold = result.get("metric_threshold")
    if name is None and operator is None and threshold is None:
        return {"metric_rejected_reason": None}
    if not allowed:
        # FAIL CLOSED (review finding, 2026-09-04). This used to read `if allowed and
        # name not in allowed`, so an EMPTY allow-list -- which is what a failed or empty
        # information_schema read returns -- disabled validation entirely and let any
        # invented column through. A stored metric that names nothing real looks
        # machine-checkable and then silently never resolves, which is strictly worse
        # than having no metric at all.
        return {"metric_name": None, "metric_operator": None, "metric_threshold": None,
                "metric_rejected_reason": "no L2 metric list available; refusing to trust "
                                          "an unvalidated metric name"}
    if name not in allowed:
        return {"metric_name": None, "metric_operator": None, "metric_threshold": None,
                "metric_rejected_reason": f"{name!r} is not a numeric fundamentals_l2_state column"}
    if operator not in METRIC_OPERATORS:
        return {"metric_name": None, "metric_operator": None, "metric_threshold": None,
                "metric_rejected_reason": f"{operator!r} is not a supported operator"}
    try:
        threshold = float(threshold)
    except (TypeError, ValueError):
        return {"metric_name": None, "metric_operator": None, "metric_threshold": None,
                "metric_rejected_reason": f"threshold {threshold!r} is not a number"}
    return {"metric_threshold": threshold, "metric_rejected_reason": None}


def _ist_today():
    """The market's calendar day, not the server's -- see portfolio_resolution._ist_today."""
    return (pd.Timestamp.now(tz="UTC") + pd.Timedelta(hours=5, minutes=30)).normalize().tz_localize(None)


def _clamp_target_date(raw):
    """Force the model's target date into the allowed hold window.

    An unparseable or absent date is not an error to raise -- it becomes the MIDPOINT of
    the window, the same "unknown gets the middle, never the most permissive end" rule the
    stop sizing uses.
    """
    today = _ist_today()
    floor = today + pd.Timedelta(days=MIN_HOLD_DAYS)
    ceiling = today + pd.Timedelta(days=MAX_HOLD_DAYS)
    try:
        parsed = pd.Timestamp(str(raw)).normalize()
        if pd.isna(parsed):
            raise ValueError(raw)
    except (ValueError, TypeError):
        midpoint = today + pd.Timedelta(days=(MIN_HOLD_DAYS + MAX_HOLD_DAYS) // 2)
        return midpoint, "window midpoint (adjudicator gave no usable target date)"
    if parsed < floor:
        return floor, "clamped up to the %d-day floor (model said %s)" % (MIN_HOLD_DAYS, parsed.date())
    if parsed > ceiling:
        return ceiling, "clamped down to the %d-day ceiling (model said %s)" % (MAX_HOLD_DAYS, parsed.date())
    return parsed, "as set by the adjudicator"


def adjudicate_entry(candidate: dict, *, model: str = DEFAULT_MODEL) -> dict:
    """Accept or reject one candidate. Never raises into the caller: an adjudicator that
    crashes must not silently stop the whole run, and 'could not decide' is not the same
    as 'reject' -- an API failure defaults to ACCEPT, because the mechanical rule already
    passed this name and the LLM is only ever a filter on top of it."""
    payload = {
        "ticker": candidate["ticker"],
        "axes": candidate.get("axes"),
        "confluence_count": candidate.get("confluence_count"),
        "contradicting_count": candidate.get("contradicting_count"),
        "evaluable_count": candidate.get("evaluable_count"),
        # ruleset v3: the story the rule passed the name on
        "story_score": candidate.get("story_score"),
        "primary_story": candidate.get("primary_dimension"),
        "story_read": candidate.get("story_read"),
        "stage": candidate["stage"],
        "narrative": (candidate.get("narrative_text") or "")[:4000],
        "stop": candidate.get("stop"),
        "available_l2_metrics": candidate.get("available_l2_metrics") or [],
    }
    try:
        client = OpenAI(api_key=env("OPENAI_API_KEY"))
        response = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": ENTRY_SYSTEM_PROMPT},
                {"role": "user", "content": json.dumps(payload, default=str)},
            ],
            response_format={
                "type": "json_schema",
                "json_schema": {"name": "entry_adjudication", "schema": ENTRY_SCHEMA, "strict": True},
            },
        )
        result = json.loads(response.choices[0].message.content)
    except Exception as exc:  # noqa: BLE001 -- see docstring
        result = {
            "decision": "accept",
            "reason": f"adjudicator unavailable ({type(exc).__name__}); mechanical rule stands",
            "concern_axis": None,
            "_no_model": True,
        }
    # A default accept is not a model's accept: recording the model name on it made an
    # outage look like adjudication in the paired comparison (2026-09-23 audit).
    no_model = result.pop("_no_model", False)
    result["model"] = None if no_model else model
    result["prompt_version"] = None if no_model else PROMPT_VERSION
    if result["decision"] == "accept":
        result.update(_validate_metric(result, payload["available_l2_metrics"]))
        target, basis = _clamp_target_date(result.get("target_date"))
        result["target_date"] = target.date().isoformat()
        result["target_date_basis"] = basis
    else:
        # A vetoed candidate gets a MECHANICAL horizon (2026-09-24, operator decision): it
        # carries no forecast, so without a target date it never took the target-date exit
        # and never resolved, and the accepted-vs-vetoed comparison -- the only test of
        # whether the veto adds value -- could only ever contain accepts. The midpoint of
        # the accept window, so both arms are compared on price over a like horizon.
        result["target_date"] = vetoed_horizon().date().isoformat()
        result["target_date_basis"] = VETO_HORIZON_BASIS
    return result


VETO_HORIZON_DAYS = (MIN_HOLD_DAYS + MAX_HOLD_DAYS) // 2
VETO_HORIZON_BASIS = (f"mechanical horizon for a vetoed candidate: {VETO_HORIZON_DAYS} days, the midpoint "
                      f"of the {MIN_HOLD_DAYS}-{MAX_HOLD_DAYS} day accept window; price comparison only, no forecast")


def vetoed_horizon(opened=None) -> pd.Timestamp:
    base = pd.Timestamp(opened) if opened is not None else pd.Timestamp.now(tz="UTC")
    if base.tzinfo is None:
        base = base.tz_localize("UTC")
    ist_day = (base + pd.Timedelta(hours=5, minutes=30)).normalize().tz_localize(None)
    return ist_day + pd.Timedelta(days=VETO_HORIZON_DAYS)


def adjudicate_exit(position: dict, exit_reason: str, *, model: str = DEFAULT_MODEL) -> dict:
    """Confirm or defer one exit.

    A stop-loss is never routed here at all -- the caller must not ask, and this refuses
    if it does. That belt-and-braces matters: the moment a stop becomes arguable, it
    stops being a stop.
    """
    if exit_reason in UNCONDITIONAL_EXIT_REASONS:
        return {
            "decision": "exit",
            "reason": f"{exit_reason} is unconditional and is never adjudicated",
            "model": None,
            "prompt_version": PROMPT_VERSION,
        }
    if int(position.get("deferral_count") or 0) >= MAX_DEFERRALS:
        return {
            "decision": "exit",
            "reason": f"already deferred {position.get('deferral_count')} time(s); "
                      f"MAX_DEFERRALS={MAX_DEFERRALS} reached -- defer means one cycle, not indefinitely",
            "model": None,
            "prompt_version": PROMPT_VERSION,
        }

    # 2026-09-23 data audit: the model used to see only entry-time counts and the reason
    # NAME -- not the trigger's detail, today's axes, today's stage or the thesis it was
    # meant to weigh the trigger against. Told "when in doubt, EXIT" with nothing to
    # doubt, it deferred 0 of 10 exits. It now gets the evidence the trigger fired on.
    payload = {k: position.get(k) for k in
               ("ticker", "opened_at", "entry_price", "stop_pct", "confluence_count",
                "contradicting_count", "evaluable_count", "stage_at_entry", "deferral_count",
                "detail", "price", "score_version_at_entry", "current_score",
                "stage_now", "prediction_text", "invalidation_criteria", "target_date",
                "current_story", "story_score_at_entry")}
    payload["exit_reason"] = exit_reason
    try:
        client = OpenAI(api_key=env("OPENAI_API_KEY"))
        response = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": EXIT_SYSTEM_PROMPT},
                {"role": "user", "content": json.dumps(payload, default=str)},
            ],
            response_format={
                "type": "json_schema",
                "json_schema": {"name": "exit_adjudication", "schema": EXIT_SCHEMA, "strict": True},
            },
        )
        result = json.loads(response.choices[0].message.content)
    except Exception as exc:  # noqa: BLE001
        # Unavailable adjudicator defaults to EXIT here, the opposite of the entry
        # default. Both defaults fail towards LESS exposure, which is the safe direction.
        # model=None: no model decided this, and the decision log must not say one did.
        return {"decision": "exit", "reason": f"adjudicator unavailable ({type(exc).__name__}); mechanical exit stands",
                "model": None, "prompt_version": None}
    result["model"] = model
    result["prompt_version"] = PROMPT_VERSION
    return result
