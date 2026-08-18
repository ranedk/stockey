"""Watchlist email notifications -- fundamental screener step 12, closing out the
watchlist pipeline started by fundamentals/screens/watchlist.py (step 10),
fundamentals/screens/watch_summary.py (step 11), and fundamentals/screens/
l4_thesis_draft.py (step 11.5, added 2026-08-15 at the user's request to make L4
thesis-drafting part of the daily run and its own email).

send_daily_digest() sends exactly one consolidated email per pipeline run, listing
the ENTIRE active watchlist regardless of whether anything changed today -- a
standing end-of-day summary, not a per-change alert. (Until 2026-08-14 this module
also sent a separate email per new-candidate/narrative-change event via
notify_watchlist_events(); removed at the user's request so a run producing several
watchlist changes sends one digest, not a burst of individual emails.) Rendered as
an HTML table + narrative section + draft-theses section (2026-08-12, "more like a
dashboard than a textual email" per the user) -- a plain-text part is always included
alongside it as the universal fallback every client falls back to when HTML rendering
is off, per RFC 2046's multipart/alternative convention.

The draft-theses section is clearly labeled CANDIDATE / NOT SAVED throughout (in both
the HTML and plain-text bodies) -- l4_thesis_draft.py never writes fundamentals_l4_
thesis, and this email never implies it did. See that module's own docstring for the
full guardrail rationale; this module just renders what it already wrote.

SAFETY: sending is OFF by default (WATCHLIST_ALERT_EMAIL_ENABLED, unset/false).
Turning it on requires WATCHLIST_ALERT_EMAIL_FROM and WATCHLIST_ALERT_EMAIL_TO to be
set, and the sender identity must be verified in AWS SES (and the account out of SES
sandbox mode, or every recipient also verified) before anything actually delivers --
this module does not attempt to verify that state itself, it will simply fail loudly
(fallback-recorded, not silently swallowed) if SES rejects the send. Confirmed live
2026-08-12 against a real SES send once japlin.com's domain identity + IAM send
permissions were set up.

WATCHLIST_ALERT_EMAIL_TO holds one or more addresses separated by whitespace (env
vars can't hold a list, and SES's own SendEmail already accepts multiple recipients)
-- _parse_recipients() splits it. send_email() puts these addresses in Bcc (not To,
per the 2026-08-14 privacy fix -- multiple real people are configured here and
shouldn't see each other's addresses); WATCHLIST_ALERT_EMAIL_FROM is used as both the
Source and the (self-addressed) To, since a valid message needs a To header.

run_watchlist_notification_pipeline() is the single daily entrypoint chaining all
four steps (sync watchlist -> regenerate stale narratives -> evaluate exits -> send
the daily digest) so cron/manual invocation is one command, matching how the rest of
this fundamentals screener is run step-by-step
(`python -m fundamentals.screens.<module>`)."""

from __future__ import annotations

import html
import json

import boto3
import pandas as pd
from environs import Env

from fundamentals.screens.l4_thesis_draft import load_current_drafts_by_company, run_l4_thesis_drafting
from fundamentals.screens.signal_pointers import load_satisfied_strategies_by_company
from fundamentals.screens.watch_summary import run_watch_summary_refresh
from fundamentals.screens.watchlist import sync_watchlist_from_alerts
from fundamentals.screens.watchlist_exit import run_watchlist_exit_evaluation
from utils.db import sql_to_df
from utils.fallback_telemetry import record_local_fallback_event

env = Env()
env.read_env()

SYNC_SOURCE_NAME = "fundamentals.screens.notifications"
STOCKEY_RUN_STATE: dict[str, object] = {}

WATCHLIST_ALERT_EMAIL_ENABLED = env.bool("WATCHLIST_ALERT_EMAIL_ENABLED", False)
WATCHLIST_ALERT_EMAIL_FROM = env.str("WATCHLIST_ALERT_EMAIL_FROM", "")
# One or more addresses separated by whitespace -- see module docstring.
WATCHLIST_ALERT_EMAIL_TO = env.str("WATCHLIST_ALERT_EMAIL_TO", "")
AWS_REGION = env.str("AWS_REGION", "us-east-1")
AWS_ACCESS_KEY_ID = env.str("AWS_ACCESS_KEY_ID", "")
AWS_SECRET_ACCESS_KEY = env.str("AWS_SECRET_ACCESS_KEY", "")


def _parse_recipients(value: str) -> list[str]:
    return [addr for addr in (value or "").split() if addr]


_SES_CLIENT: "boto3.client | None" = None  # cache, same pattern as utils/store.py's S3 client


def _get_ses_client():
    global _SES_CLIENT
    if _SES_CLIENT is None:
        _SES_CLIENT = boto3.client(
            "ses",
            region_name=AWS_REGION,
            aws_access_key_id=AWS_ACCESS_KEY_ID,
            aws_secret_access_key=AWS_SECRET_ACCESS_KEY,
        )
    return _SES_CLIENT


def _record_fallback(fallback_type: str, *, reason: str, error, severity: str = "warn", metadata=None) -> None:
    record_local_fallback_event(
        module=SYNC_SOURCE_NAME,
        source="ses",
        fallback_type=fallback_type,
        severity=severity,
        reason=reason,
        error=error,
        metadata=metadata or {},
    )


def send_email(subject: str, text_body: str, html_body: str | None = None) -> dict | None:
    """Sends via SES to every address in WATCHLIST_ALERT_EMAIL_TO, as Bcc -- recipients
    don't see each other's addresses (2026-08-14; multiple real people are configured
    here). Source doubles as the To: address (a valid RFC 5322 message needs one, and
    a self-addressed To reads better than an empty one) -- the actual recipients only
    ever appear in Bcc. Always includes a plain-text part (universal fallback);
    html_body, when given, is attached alongside it as the primary rendering most
    clients show (multipart/alternative, per RFC 2046 -- a client picks whichever part
    it can render best, never both). Returns None (not an error) if sending is
    disabled -- callers that need to distinguish "disabled" from "sent" should check
    WATCHLIST_ALERT_EMAIL_ENABLED themselves, same as send_daily_digest does."""
    if not WATCHLIST_ALERT_EMAIL_ENABLED:
        return None
    client = _get_ses_client()
    body: dict = {"Text": {"Data": text_body, "Charset": "UTF-8"}}
    if html_body:
        body["Html"] = {"Data": html_body, "Charset": "UTF-8"}
    return client.send_email(
        Source=WATCHLIST_ALERT_EMAIL_FROM,
        Destination={
            "ToAddresses": [WATCHLIST_ALERT_EMAIL_FROM],
            "BccAddresses": _parse_recipients(WATCHLIST_ALERT_EMAIL_TO),
        },
        Message={"Subject": {"Data": subject, "Charset": "UTF-8"}, "Body": body},
    )


def load_full_watchlist() -> list[dict]:
    """Every ACTIVE watchlist row (2026-08-13: status='active' filter -- the daily
    digest is "here's your current watchlist", the exact place the "crowding"
    complaint this feature fixes was actually about; stale/invalidated/price_flagged
    companies stay fully queryable, just not in this standing summary), company name
    joined from the latest L1 universe run and current_price from the latest
    technicals run (same two joins fundamentals/api/queries.py's get_watchlist()
    does, kept as their own small query here rather than importing the API layer --
    screens/ modules stay independent of api/, not the other way around)."""
    df = sql_to_df(
        """
        SELECT w.company_master_id, w.first_seen_at, w.first_seen_price, w.last_alert_at,
               w.alert_count, w.narrative_text, w.suggested_watch_until, l1.company_name,
               tech.close AS current_price
        FROM fundamentals_watchlist w
        LEFT JOIN LATERAL (
            SELECT company_name FROM fundamentals_l1_universe
            WHERE ticker = REPLACE(w.company_master_id, 'nse:', '')
            ORDER BY run_date DESC LIMIT 1
        ) l1 ON TRUE
        LEFT JOIN LATERAL (
            SELECT close FROM fundamentals_technicals
            WHERE company_master_id = w.company_master_id
            ORDER BY run_date DESC LIMIT 1
        ) tech ON TRUE
        WHERE w.status = 'active'
        ORDER BY w.last_alert_at DESC NULLS LAST
        """
    )
    rows = df.to_dict("records") if not df.empty else []
    if not rows:
        return rows

    # 2026-08-13: strategies per row -- was previously just a bare alert_count in the
    # digest table, same gap fixed on the API's get_watchlist(). load_satisfied_
    # strategies_by_company() is a screens/-level shared helper (not imported from
    # api/queries.py, keeping the "screens/ stays independent of api/" direction).
    strategies_by_company = load_satisfied_strategies_by_company()
    drafts_by_company = load_current_drafts_by_company()
    for row in rows:
        row["strategies"] = strategies_by_company.get(row["company_master_id"], [])
        row["draft_thesis"] = drafts_by_company.get(row["company_master_id"])
    return rows


def _fmt_price(value) -> str:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return "n/a"
    return f"Rs.{value:,.2f}"


def _fmt_plain(value) -> str:
    return "n/a" if value is None or (isinstance(value, float) and pd.isna(value)) else str(value)


def _price_change_pct(first_seen_price, current_price) -> float | None:
    """Shared by both the HTML and plain-text renderers -- BUG FOUND LIVE
    2026-08-17: plain-text used to print only the two raw prices with no pct change
    at all, an inconsistency with the HTML table cell right next to it (which always
    computed one). Factored out so the two bodies can't silently re-diverge again."""
    if first_seen_price is None or current_price is None or pd.isna(first_seen_price) or pd.isna(current_price) or first_seen_price == 0:
        return None
    return (current_price - first_seen_price) / first_seen_price * 100


def _price_change_cell_html(first_seen_price, current_price) -> str:
    from_str = _fmt_price(first_seen_price)
    to_str = _fmt_price(current_price)
    pct = _price_change_pct(first_seen_price, current_price)
    if pct is None:
        return f"{from_str} &rarr; {to_str}"
    css_class = "pos" if pct >= 0 else "neg"
    sign = "+" if pct >= 0 else ""
    return f'{from_str} &rarr; {to_str} <span class="{css_class}">({sign}{pct:.1f}%)</span>'


def _price_change_text(first_seen_price, current_price) -> str:
    from_str = _fmt_price(first_seen_price)
    to_str = _fmt_price(current_price)
    pct = _price_change_pct(first_seen_price, current_price)
    if pct is None:
        return f"{from_str} -> {to_str}"
    sign = "+" if pct >= 0 else ""
    return f"{from_str} -> {to_str} ({sign}{pct:.1f}%)"


def _confidence_label(confidence_score) -> str:
    """Shared by both renderers -- BUG FOUND LIVE 2026-08-17: the HTML side already
    guarded a null confidence_score ("n/a"), but the plain-text side interpolated it
    directly, so a draft with no score printed the literal string "confidence
    None/100" instead."""
    if confidence_score is None or (isinstance(confidence_score, float) and pd.isna(confidence_score)):
        return "n/a"
    return f"{confidence_score}/100"


_DIGEST_HTML_STYLE = (
    "body{font-family:-apple-system,'Segoe UI',Roboto,Arial,sans-serif;background:#f8fafc;color:#0f172a;margin:0;padding:24px}"
    ".card{background:#fff;border:1px solid #e2e8f0;border-radius:8px;padding:20px;margin-bottom:16px}"
    "table{width:100%;border-collapse:collapse;font-size:14px}"
    "th{text-align:left;font-size:11px;text-transform:uppercase;letter-spacing:.03em;color:#64748b;border-bottom:1px solid #e2e8f0;padding:8px 10px}"
    "td{padding:10px;border-bottom:1px solid #f1f5f9;vertical-align:top}"
    "tr:last-child td{border-bottom:none}"
    ".pos{color:#059669;font-weight:600}"
    ".neg{color:#dc2626;font-weight:600}"
    ".badge{display:inline-block;background:#f1f5f9;color:#475569;border-radius:999px;padding:2px 8px;font-size:11px;white-space:nowrap}"
    ".narrative-block{padding:14px 0;border-bottom:1px solid #f1f5f9}"
    ".narrative-block:last-child{border-bottom:none}"
    ".narrative-ticker{font-weight:600;font-size:14px}"
    ".narrative-text{font-size:13px;color:#334155;margin-top:4px}"
    "h1{font-size:18px;margin:0 0 4px}"
    ".sub{color:#64748b;font-size:13px;margin:0 0 4px}"
    ".footer{color:#94a3b8;font-size:12px;margin-top:8px}"
    ".strategy-badge{display:inline-block;background:#eef2ff;color:#4338ca;border-radius:999px;padding:1px 7px;font-size:10px;margin:2px 4px 0 0;white-space:nowrap}"
    ".draft-banner{background:#fffbeb;border:1px solid #fde68a;color:#92400e;border-radius:6px;padding:8px 12px;font-size:12px;margin:0 0 14px}"
    ".draft-block{padding:14px 0;border-bottom:1px solid #f1f5f9}"
    ".draft-block:last-child{border-bottom:none}"
    ".draft-ticker{font-weight:600;font-size:14px}"
    ".draft-confidence{display:inline-block;border-radius:999px;padding:1px 8px;font-size:11px;font-weight:600;margin-left:8px}"
    ".draft-confidence.high{background:#dcfce7;color:#166534}"
    ".draft-confidence.mid{background:#fef3c7;color:#92400e}"
    ".draft-confidence.low{background:#fee2e2;color:#991b1b}"
    ".draft-prediction{font-size:13px;color:#0f172a;margin-top:6px}"
    ".draft-meta{font-size:12px;color:#64748b;margin-top:4px}"
    ".draft-rationale{font-size:12px;color:#334155;margin-top:6px}"
    ".draft-invalidation{font-size:12px;color:#94a3b8;margin-top:4px;font-style:italic}"
)

# Mirrors the frontend's STRATEGY_LABELS map (screener/app/utils/strategyLabels.ts) --
# 2026-08-13, same gap fix as get_watchlist()'s "strategies" field: the digest table
# only ever showed a bare alert_count, no indication of WHICH strategies. Falls back
# to the raw trigger_type for anything not listed here so a new trigger_type never
# needs a code deploy before it shows up in the email.
STRATEGY_LABELS = {
    "rating_downgrade": "Rating downgrade",
    "rating_confirms_deleveraging": "Rating confirms deleveraging",
    "insider_buy": "Insider buy",
    "insider_sell_surprise": "Insider sell (surprise)",
    "capital_raise": "Capital raise",
    "institutional_first_entry": "First institutional entry",
    "llm_flagged": "LLM-flagged",
    # Added 2026-08-13 alongside the L3 results trigger + auditor_change/RPT gap fix
    # -- these existed for a full test-suite cycle before being added here, same
    # oversight this whole audit pass was checking for.
    "results_decline": "Results decline",
    "results_confirm_turnaround": "Results confirm turnaround",
    "results_delayed": "Results delayed",
    "auditor_change": "Auditor change",
    "related_party_transaction": "Related-party transaction",
}


def _strategy_label(trigger_type: str) -> str:
    return STRATEGY_LABELS.get(trigger_type, trigger_type)


def _confidence_css_class(confidence_score) -> str:
    if confidence_score is None or (isinstance(confidence_score, float) and pd.isna(confidence_score)):
        return "mid"
    if confidence_score >= 66:
        return "high"
    if confidence_score >= 33:
        return "mid"
    return "low"


def build_daily_digest_content(watchlist_rows: list[dict]) -> tuple[str, str, str]:
    """Returns (subject, text_body, html_body). HTML is the primary rendering -- a
    compact table (watching-since, entry-vs-today's-close, event count, watch-until)
    plus a per-company narrative section below it -- the plain-text part is the
    universal fallback every client falls back to when HTML rendering is off."""
    today = pd.Timestamp.now(tz="UTC").date()
    if not watchlist_rows:
        subject = "[Watchlist] Daily digest -- nothing on the watchlist"
        text_body = "No companies are on the watchlist yet."
        html_body = f"<html><body style='font-family:sans-serif'><p>{text_body}</p></body></html>"
        return subject, text_body, html_body

    subject = f"[Watchlist] Daily digest -- {len(watchlist_rows)} companies"
    text_lines = [f"{len(watchlist_rows)} companies on the watchlist as of {today}. One consolidated summary, not a change alert.\n"]
    table_rows_html = []
    narrative_blocks_html = []
    draft_blocks_html = []

    for row in watchlist_rows:
        ticker = str(row["company_master_id"]).removeprefix("nse:")
        company_name = row.get("company_name") or "name unknown"
        narrative = row.get("narrative_text") or "(narrative not generated yet)"
        strategies = row.get("strategies") or []
        draft = row.get("draft_thesis")

        text_lines.append(f"--- {ticker} ({company_name}) ---")
        text_lines.append(
            f"Watching since {_fmt_plain(row.get('first_seen_at'))} at "
            f"{_price_change_text(row.get('first_seen_price'), row.get('current_price'))} today -- {row.get('alert_count') or 0} event(s)."
        )
        if strategies:
            text_lines.append(f"Strategies: {', '.join(_strategy_label(s) for s in strategies)}")
        text_lines.append(str(narrative))
        if row.get("suggested_watch_until"):
            text_lines.append(f"Suggested watch until: {row['suggested_watch_until']}")
        if draft:
            text_lines.append(
                f"[DRAFT L4 thesis, NOT SAVED, confidence {_confidence_label(draft.get('confidence_score'))}] "
                f"{draft.get('prediction_text')} (by {draft.get('target_date')})"
            )
            text_lines.append(f"  Rationale: {_fmt_plain(draft.get('rationale'))}")
            text_lines.append(f"  Invalidation: {_fmt_plain(draft.get('invalidation_criteria'))}")
        text_lines.append("")

        strategy_badges_html = "".join(f'<span class="strategy-badge">{html.escape(_strategy_label(s))}</span>' for s in strategies)
        table_rows_html.append(
            "<tr>"
            f'<td><strong>{html.escape(ticker)}</strong><br>'
            f'<span style="color:#64748b;font-size:12px">{html.escape(company_name)}</span>'
            f'<div>{strategy_badges_html}</div></td>'
            f"<td>{html.escape(_fmt_plain(row.get('first_seen_at')))}</td>"
            f"<td>{_price_change_cell_html(row.get('first_seen_price'), row.get('current_price'))}</td>"
            f'<td><span class="badge">{row.get("alert_count") or 0} events</span></td>'
            f"<td>{html.escape(_fmt_plain(row.get('suggested_watch_until')))}</td>"
            "</tr>"
        )
        narrative_blocks_html.append(
            '<div class="narrative-block">'
            f'<div class="narrative-ticker">{html.escape(ticker)} '
            f'<span style="color:#94a3b8;font-weight:400">-- {html.escape(company_name)}</span></div>'
            f'<div class="narrative-text">{html.escape(str(narrative))}</div>'
            "</div>"
        )
        if draft:
            confidence_score = draft.get("confidence_score")
            confidence_class = _confidence_css_class(confidence_score)
            confidence_label = _confidence_label(confidence_score)
            draft_blocks_html.append(
                '<div class="draft-block">'
                f'<span class="draft-ticker">{html.escape(ticker)}</span>'
                f'<span class="draft-confidence {confidence_class}">confidence {html.escape(confidence_label)}</span>'
                f'<div class="draft-prediction">{html.escape(str(draft.get("prediction_text") or ""))}</div>'
                f'<div class="draft-meta">Target: {html.escape(_fmt_plain(draft.get("target_date")))}</div>'
                # BUG FOUND LIVE 2026-08-17: the LLM's own rationale field
                # (fundamentals_l4_thesis_draft.rationale, already selected by
                # load_current_drafts_by_company()'s SELECT d.*) was never rendered
                # here at all -- a human reviewing a draft to commit/discard saw the
                # prediction and confidence score but not WHY the model made the call.
                f'<div class="draft-rationale">{html.escape(_fmt_plain(draft.get("rationale")))}</div>'
                f'<div class="draft-invalidation">Invalidation: {html.escape(_fmt_plain(draft.get("invalidation_criteria")))}</div>'
                "</div>"
            )

    text_lines.append("This is a descriptive screener digest, not a trade recommendation.")
    if draft_blocks_html:
        text_lines.append(
            "DRAFT L4 theses above are LLM-drafted candidates only -- nothing is saved to the real thesis "
            "register until a human reviews and commits it."
        )
    text_body = "\n".join(text_lines)

    draft_section_html = ""
    if draft_blocks_html:
        draft_section_html = (
            '<div class="card"><h1>Draft L4 Theses</h1>'
            '<div class="draft-banner">CANDIDATE ONLY &mdash; nothing here is saved to the real thesis register. '
            "Each is an LLM-drafted, falsifiable prediction with a confidence score for your review; commit or "
            "discard it yourself.</div>"
            f'{"".join(draft_blocks_html)}</div>'
        )

    html_body = (
        f'<html><head><meta charset="utf-8"><style>{_DIGEST_HTML_STYLE}</style></head><body>'
        '<div class="card"><h1>Watchlist Digest</h1>'
        f'<p class="sub">{len(watchlist_rows)} companies &middot; {today}</p>'
        "<table><tr><th>Company</th><th>Watching since</th><th>Entry &rarr; Today's close</th>"
        f"<th>Events</th><th>Watch until</th></tr>{''.join(table_rows_html)}</table></div>"
        f'<div class="card"><h1>Why</h1>{"".join(narrative_blocks_html)}</div>'
        f"{draft_section_html}"
        '<p class="footer">Descriptive screener digest, not a trade recommendation.</p>'
        "</body></html>"
    )
    return subject, text_body, html_body


def send_daily_digest() -> dict[str, object]:
    """One consolidated email per pipeline run listing the entire watchlist,
    regardless of whether anything changed today -- the only email this module
    sends, see module docstring."""
    if not WATCHLIST_ALERT_EMAIL_ENABLED:
        return {"sent": 0, "skipped_disabled": 1, "failed": 0}

    if not WATCHLIST_ALERT_EMAIL_FROM or not _parse_recipients(WATCHLIST_ALERT_EMAIL_TO):
        _record_fallback(
            "watchlist_digest_email_misconfigured",
            reason="WATCHLIST_ALERT_EMAIL_ENABLED is true but WATCHLIST_ALERT_EMAIL_FROM/TO is unset; digest not sent this run.",
            error="missing sender/recipient",
            severity="error",
        )
        return {"sent": 0, "skipped_disabled": 0, "failed": 1}

    subject, text_body, html_body = build_daily_digest_content(load_full_watchlist())
    try:
        send_email(subject, text_body, html_body)
        return {"sent": 1, "skipped_disabled": 0, "failed": 0}
    except Exception as exc:  # noqa: BLE001 -- classified as a failure, not raised
        _record_fallback(
            "watchlist_digest_email_send_failed",
            reason="SES send_email failed for the daily digest; not retried automatically.",
            error=exc,
        )
        return {"sent": 0, "skipped_disabled": 0, "failed": 1}


def run_watchlist_notification_pipeline() -> dict[str, object]:
    """Chains watchlist sync -> narrative regen -> L4 THESIS DRAFTING -> exit status
    evaluation -> daily digest, in that order. watchlist_exit runs right after
    narrative regen (2026-08-13, docs/FUNDAMENTAL_SCREENER_RESULTS_ARC.md's "we will
    crowd the watchlist" gap fix) so it evaluates against a freshly-updated
    suggested_watch_until/narrative_generated_at, and before the digest so
    send_daily_digest's own default active-only filter reflects this run's status,
    not last run's. L4 thesis drafting (2026-08-15) sits between narrative regen and
    exit evaluation -- after narrative regen so it can read the freshly-generated
    narrative_text as part of its own evidence bundle (see l4_thesis_draft.py's
    docstring), before exit evaluation/digest so a newly-drafted thesis shows up in
    the SAME run's email rather than one run behind. (Until 2026-08-14 this also ran
    notify_watchlist_events between exit evaluation and the digest, sending one email
    per changed company; removed so a run with several changes sends only the one
    consolidated digest -- see module docstring.)"""
    sync_result = sync_watchlist_from_alerts()
    summary_result = run_watch_summary_refresh()
    draft_result = run_l4_thesis_drafting()
    exit_result = run_watchlist_exit_evaluation()
    digest_result = send_daily_digest()
    return {
        "watchlist_companies": sync_result["companies"],
        "new_candidates": sync_result["new_candidates"],
        "narratives_generated": summary_result["generated"],
        "narratives_failed": summary_result["failed"],
        "narratives_blocked": summary_result["blocked"],
        "theses_drafted": draft_result["drafted"],
        "theses_draft_failed": draft_result["failed"],
        "theses_draft_blocked": draft_result["blocked"],
        "watchlist_active": exit_result["active"],
        "watchlist_invalidated": exit_result["invalidated"],
        "watchlist_price_flagged": exit_result["price_flagged"],
        "watchlist_stale": exit_result["stale"],
        "digest_sent": digest_result["sent"],
        "digest_skipped_disabled": digest_result["skipped_disabled"],
        "digest_failed": digest_result["failed"],
    }


def main() -> int:
    global STOCKEY_RUN_STATE
    result = run_watchlist_notification_pipeline()
    STOCKEY_RUN_STATE = {
        "source": SYNC_SOURCE_NAME,
        "rows": result["watchlist_companies"],
        "rows_written": result["narratives_generated"],
        **result,
        "fallback_used": bool(result["narratives_failed"] or result["theses_draft_failed"] or result["digest_failed"]),
        "state_advanced": result["narratives_generated"] > 0 or result["theses_drafted"] > 0 or result["digest_sent"] > 0,
        "status": "blocked" if (result["narratives_blocked"] or result["theses_draft_blocked"]) else "ok",
    }
    print(json.dumps(STOCKEY_RUN_STATE, ensure_ascii=False, default=str), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
