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
  daily run", not a separately scheduled job.

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

import json

import boto3
from environs import Env

from fundamentals.screens.watch_summary import run_watch_summary_refresh
from fundamentals.screens.watchlist import sync_watchlist_from_alerts
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


def send_email(subject: str, body: str) -> dict | None:
    """Sends via SES to every address in WATCHLIST_ALERT_EMAIL_TO. Returns None (not
    an error) if sending is disabled -- callers that need to distinguish "disabled"
    from "sent" should check WATCHLIST_ALERT_EMAIL_ENABLED themselves, same as
    notify_watchlist_events/send_daily_digest do."""
    if not WATCHLIST_ALERT_EMAIL_ENABLED:
        return None
    client = _get_ses_client()
    return client.send_email(
        Source=WATCHLIST_ALERT_EMAIL_FROM,
        Destination={"ToAddresses": _parse_recipients(WATCHLIST_ALERT_EMAIL_TO)},
        Message={"Subject": {"Data": subject}, "Body": {"Text": {"Data": body}}},
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
    """Every current watchlist row, company name joined from the latest L1 universe
    run -- same join fundamentals/api/queries.py's get_watchlist() does, kept as its
    own small query here rather than importing the API layer (screens/ modules stay
    independent of api/, not the other way around)."""
    df = sql_to_df(
        """
        SELECT w.company_master_id, w.first_seen_at, w.first_seen_price, w.last_alert_at,
               w.alert_count, w.narrative_text, w.suggested_watch_until, l1.company_name
        FROM fundamentals_watchlist w
        LEFT JOIN LATERAL (
            SELECT company_name FROM fundamentals_l1_universe
            WHERE ticker = REPLACE(w.company_master_id, 'nse:', '')
            ORDER BY run_date DESC LIMIT 1
        ) l1 ON TRUE
        ORDER BY w.last_alert_at DESC NULLS LAST
        """
    )
    return df.to_dict("records") if not df.empty else []


def build_daily_digest_content(watchlist_rows: list[dict]) -> tuple[str, str]:
    if not watchlist_rows:
        return "[Watchlist] Daily digest -- nothing on the watchlist", "No companies are on the watchlist yet."

    subject = f"[Watchlist] Daily digest -- {len(watchlist_rows)} companies"
    lines = [f"{len(watchlist_rows)} companies on the watchlist as of today. One consolidated summary, not a change alert.\n"]
    for row in watchlist_rows:
        ticker = str(row["company_master_id"]).removeprefix("nse:")
        lines.append(f"--- {ticker} ({row.get('company_name') or 'name unknown'}) ---")
        lines.append(f"Watching since {row.get('first_seen_at')} at {row.get('first_seen_price')} -- {row.get('alert_count')} event(s).")
        lines.append(str(row.get("narrative_text") or "(narrative not generated yet)"))
        if row.get("suggested_watch_until"):
            lines.append(f"Suggested watch until: {row['suggested_watch_until']}")
        lines.append("")

    lines.append("This is a descriptive screener digest, not a trade recommendation.")
    return subject, "\n".join(lines)


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

    subject, body = build_daily_digest_content(load_full_watchlist())
    try:
        send_email(subject, body)
        return {"sent": 1, "skipped_disabled": 0, "failed": 0}
    except Exception as exc:  # noqa: BLE001 -- classified as a failure, not raised
        _record_fallback(
            "watchlist_digest_email_send_failed",
            reason="SES send_email failed for the daily digest; not retried automatically.",
            error=exc,
        )
        return {"sent": 0, "skipped_disabled": 0, "failed": 1}


def run_watchlist_notification_pipeline() -> dict[str, object]:
    sync_result = sync_watchlist_from_alerts()
    summary_result = run_watch_summary_refresh()
    notify_result = notify_watchlist_events(summary_result.get("narrative_events", []))
    digest_result = send_daily_digest()
    return {
        "watchlist_companies": sync_result["companies"],
        "new_candidates": sync_result["new_candidates"],
        "narratives_generated": summary_result["generated"],
        "narratives_failed": summary_result["failed"],
        "narratives_blocked": summary_result["blocked"],
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
