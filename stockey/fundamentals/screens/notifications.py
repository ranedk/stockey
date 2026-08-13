"""Watchlist email notifications -- fundamental screener step 12, closing out the
watchlist pipeline started by fundamentals/screens/watchlist.py (step 10) and
fundamentals/screens/watch_summary.py (step 11). User-confirmed trigger (conversation
2026-08-11): "send email on new watchlist candidate as well as when narrative
changes."

Both triggers collapse to one input: watch_summary.py's own narrative_events list
already carries is_new_candidate and narrative_changed per company, computed by
comparing the newly generated narrative text against whatever was stored before (see
that module's docstring). A brand-new candidate's first-ever narrative generation IS
both events at once -- there is no meaningful "candidate added, no explanation yet"
email to send separately, so this module sends exactly one email per event, its
subject line distinguishing "new candidate" from "narrative updated". An event whose
narrative regenerated but produced byte-identical text (narrative_changed=False, see
watch_summary.py's own test for that edge case) sends nothing -- no new information,
no email.

SAFETY: sending is OFF by default (WATCHLIST_ALERT_EMAIL_ENABLED, unset/false).
Turning it on requires WATCHLIST_ALERT_EMAIL_FROM and WATCHLIST_ALERT_EMAIL_TO to be
set, and the sender identity must be verified in AWS SES (and the account out of SES
sandbox mode, or every recipient also verified) before anything actually delivers --
this module does not attempt to verify that state itself, it will simply fail loudly
(fallback-recorded, not silently swallowed) if SES rejects the send. Confirmed live
2026-08-12 against a real SES send once japlin.com's domain identity + IAM send
permissions were set up.

WATCHLIST_ALERT_EMAIL_TO holds one or more addresses separated by whitespace (env
vars can't hold a list, and SES's own SendEmail already accepts multiple
ToAddresses) -- _parse_recipients() splits it; every send in this module (per-event
and the daily digest below) goes to the full parsed list.

Two independent things send mail, both gated on the same WATCHLIST_ALERT_EMAIL_*
config:
- notify_watchlist_events(): per-event, only on real change (new candidate or
  narrative text that actually changed) -- see below.
- send_daily_digest() (2026-08-12): one consolidated email per pipeline run listing
  the ENTIRE watchlist, regardless of whether anything changed today -- a standing
  end-of-day summary, not a change alert. Both fire from
  run_watchlist_notification_pipeline() each time the daily fundamentals pipeline
  runs (cron: 19:15 weekdays), so "day end" here means "after the pipeline's own
  daily run", not a separately scheduled job. Rendered as an HTML table + narrative
  section (2026-08-12, "more like a dashboard than a textual email" per the user) --
  a plain-text part is always included alongside it as the universal fallback every
  client falls back to when HTML rendering is off, per RFC 2046's multipart/
  alternative convention.

Both triggers collapse to one input: watch_summary.py's own narrative_events list
already carries is_new_candidate and narrative_changed per company, computed by
comparing the newly generated narrative text against whatever was stored before (see
that module's docstring). A brand-new candidate's first-ever narrative generation IS
both events at once -- there is no meaningful "candidate added, no explanation yet"
email to send separately, so notify_watchlist_events() sends exactly one email per
event, its subject line distinguishing "new candidate" from "narrative updated". An
event whose narrative regenerated but produced byte-identical text
(narrative_changed=False, see watch_summary.py's own test for that edge case) sends
nothing -- no new information, no email.

run_watchlist_notification_pipeline() is the single daily entrypoint chaining all
four steps (sync watchlist -> regenerate stale narratives -> notify on what changed
-> send the daily digest) so cron/manual invocation is one command, matching how the
rest of this fundamentals screener is run step-by-step
(`python -m fundamentals.screens.<module>`)."""

from __future__ import annotations

import html
import json

import boto3
import pandas as pd
from environs import Env

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


def build_email_content(event: dict) -> tuple[str, str]:
    company_master_id = event["company_master_id"]
    ticker = str(company_master_id).removeprefix("nse:")
    if event.get("is_new_candidate"):
        subject = f"[Watchlist] New candidate: {ticker}"
        intro = "This company was just added to the watchlist based on a new fundamental alert."
    else:
        subject = f"[Watchlist] Narrative updated: {ticker}"
        intro = "The watch narrative for this company changed based on a new fundamental alert."

    body = (
        f"{intro}\n\n"
        f"{event.get('narrative_text') or '(no narrative text)'}\n\n"
        f"Suggested watch until: {event.get('suggested_watch_until') or 'n/a'}\n"
        f"Confidence: {event.get('confidence') or 'n/a'}\n"
        "\n"
        "This is a descriptive screener alert, not a trade recommendation. Review and "
        "add to portfolio manually if warranted."
    )
    return subject, body


def send_email(subject: str, text_body: str, html_body: str | None = None) -> dict | None:
    """Sends via SES to every address in WATCHLIST_ALERT_EMAIL_TO. Always includes a
    plain-text part (universal fallback); html_body, when given, is attached
    alongside it as the primary rendering most clients show (multipart/alternative,
    per RFC 2046 -- a client picks whichever part it can render best, never both).
    Returns None (not an error) if sending is disabled -- callers that need to
    distinguish "disabled" from "sent" should check WATCHLIST_ALERT_EMAIL_ENABLED
    themselves, same as notify_watchlist_events/send_daily_digest do."""
    if not WATCHLIST_ALERT_EMAIL_ENABLED:
        return None
    client = _get_ses_client()
    body: dict = {"Text": {"Data": text_body, "Charset": "UTF-8"}}
    if html_body:
        body["Html"] = {"Data": html_body, "Charset": "UTF-8"}
    return client.send_email(
        Source=WATCHLIST_ALERT_EMAIL_FROM,
        Destination={"ToAddresses": _parse_recipients(WATCHLIST_ALERT_EMAIL_TO)},
        Message={"Subject": {"Data": subject, "Charset": "UTF-8"}, "Body": body},
    )


def notify_watchlist_events(narrative_events: list[dict]) -> dict[str, object]:
    changed_events = [e for e in narrative_events if e.get("narrative_changed")]
    if not changed_events:
        return {"sent": 0, "skipped_disabled": 0, "failed": 0}

    if not WATCHLIST_ALERT_EMAIL_ENABLED:
        return {"sent": 0, "skipped_disabled": len(changed_events), "failed": 0}

    if not WATCHLIST_ALERT_EMAIL_FROM or not _parse_recipients(WATCHLIST_ALERT_EMAIL_TO):
        _record_fallback(
            "watchlist_email_misconfigured",
            reason="WATCHLIST_ALERT_EMAIL_ENABLED is true but WATCHLIST_ALERT_EMAIL_FROM/TO is unset; no emails sent this run.",
            error="missing sender/recipient",
            severity="error",
        )
        return {"sent": 0, "skipped_disabled": 0, "failed": len(changed_events)}

    sent = 0
    failed = 0
    for event in changed_events:
        subject, body = build_email_content(event)
        try:
            send_email(subject, body)
            sent += 1
        except Exception as exc:  # noqa: BLE001 -- one company's SES failure must not block the rest
            failed += 1
            _record_fallback(
                "watchlist_email_send_failed",
                reason="SES send_email failed for this watchlist event; not retried automatically.",
                error=exc,
                metadata={"company_master_id": event.get("company_master_id")},
            )

    return {"sent": sent, "skipped_disabled": 0, "failed": failed}


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
    for row in rows:
        row["strategies"] = strategies_by_company.get(row["company_master_id"], [])
    return rows


def _fmt_price(value) -> str:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return "n/a"
    return f"Rs.{value:,.2f}"


def _fmt_plain(value) -> str:
    return "n/a" if value is None or (isinstance(value, float) and pd.isna(value)) else str(value)


def _price_change_cell_html(first_seen_price, current_price) -> str:
    from_str = _fmt_price(first_seen_price)
    to_str = _fmt_price(current_price)
    if from_str == "n/a" or to_str == "n/a" or first_seen_price == 0:
        return f"{from_str} &rarr; {to_str}"
    pct = (current_price - first_seen_price) / first_seen_price * 100
    css_class = "pos" if pct >= 0 else "neg"
    sign = "+" if pct >= 0 else ""
    return f'{from_str} &rarr; {to_str} <span class="{css_class}">({sign}{pct:.1f}%)</span>'


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

    for row in watchlist_rows:
        ticker = str(row["company_master_id"]).removeprefix("nse:")
        company_name = row.get("company_name") or "name unknown"
        narrative = row.get("narrative_text") or "(narrative not generated yet)"
        strategies = row.get("strategies") or []

        text_lines.append(f"--- {ticker} ({company_name}) ---")
        text_lines.append(
            f"Watching since {_fmt_plain(row.get('first_seen_at'))} at {_fmt_price(row.get('first_seen_price'))}, "
            f"today's close {_fmt_price(row.get('current_price'))} -- {row.get('alert_count') or 0} event(s)."
        )
        if strategies:
            text_lines.append(f"Strategies: {', '.join(_strategy_label(s) for s in strategies)}")
        text_lines.append(str(narrative))
        if row.get("suggested_watch_until"):
            text_lines.append(f"Suggested watch until: {row['suggested_watch_until']}")
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

    text_lines.append("This is a descriptive screener digest, not a trade recommendation.")
    text_body = "\n".join(text_lines)

    html_body = (
        f'<html><head><meta charset="utf-8"><style>{_DIGEST_HTML_STYLE}</style></head><body>'
        '<div class="card"><h1>Watchlist Digest</h1>'
        f'<p class="sub">{len(watchlist_rows)} companies &middot; {today}</p>'
        "<table><tr><th>Company</th><th>Watching since</th><th>Entry &rarr; Today's close</th>"
        f"<th>Events</th><th>Watch until</th></tr>{''.join(table_rows_html)}</table></div>"
        f'<div class="card"><h1>Why</h1>{"".join(narrative_blocks_html)}</div>'
        '<p class="footer">Descriptive screener digest, not a trade recommendation.</p>'
        "</body></html>"
    )
    return subject, text_body, html_body


def send_daily_digest() -> dict[str, object]:
    """One consolidated email per pipeline run listing the entire watchlist,
    regardless of whether anything changed today -- distinct from
    notify_watchlist_events()'s per-change alerts, see module docstring."""
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
    """Chains watchlist sync -> narrative regen -> EXIT STATUS EVALUATION -> notify
    -> daily digest, in that order. watchlist_exit runs right after narrative regen
    (2026-08-13, docs/FUNDAMENTAL_SCREENER_RESULTS_ARC.md's "we will crowd the
    watchlist" gap fix) so it evaluates against a freshly-updated suggested_watch_
    until/narrative_generated_at, and before notify/digest so send_daily_digest's own
    default active-only filter reflects this run's status, not last run's. notify_
    watchlist_events is deliberately NOT status-filtered -- a per-event change alert
    (including one that happens to also be an invalidating event) should still reach
    a human regardless of the company's resulting status; only the daily digest's
    "here's your current watchlist" view filters to active by default."""
    sync_result = sync_watchlist_from_alerts()
    summary_result = run_watch_summary_refresh()
    exit_result = run_watchlist_exit_evaluation()
    notify_result = notify_watchlist_events(summary_result.get("narrative_events", []))
    digest_result = send_daily_digest()
    return {
        "watchlist_companies": sync_result["companies"],
        "new_candidates": sync_result["new_candidates"],
        "narratives_generated": summary_result["generated"],
        "narratives_failed": summary_result["failed"],
        "narratives_blocked": summary_result["blocked"],
        "watchlist_active": exit_result["active"],
        "watchlist_invalidated": exit_result["invalidated"],
        "watchlist_price_flagged": exit_result["price_flagged"],
        "watchlist_stale": exit_result["stale"],
        "emails_sent": notify_result["sent"],
        "emails_skipped_disabled": notify_result["skipped_disabled"],
        "emails_failed": notify_result["failed"],
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
        "fallback_used": bool(result["narratives_failed"] or result["emails_failed"] or result["digest_failed"]),
        "state_advanced": result["narratives_generated"] > 0 or result["emails_sent"] > 0 or result["digest_sent"] > 0,
        "status": "blocked" if result["narratives_blocked"] else "ok",
    }
    print(json.dumps(STOCKEY_RUN_STATE, ensure_ascii=False, default=str), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
