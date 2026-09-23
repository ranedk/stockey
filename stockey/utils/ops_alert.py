"""Out-of-band ops alerting: email the operator when the platform's own foundations fail.

Deliberately separate from fundamentals/screens/notifications.py even though both send
through the same SES account. Two reasons, both load-bearing:

  1. DEPENDENCY DIRECTION. The fundamentals screener is a carve-out that sits ON TOP of
     the pure-TA data platform (see CLAUDE.md's boundary rules). A data-platform health
     check importing `fundamentals.*` would invert that, and would mean the core cannot
     alert without the carve-out being importable.
  2. INDEPENDENT KILL SWITCH. WATCHLIST_ALERT_EMAIL_ENABLED turns off a daily digest.
     Nobody turning that off means "also stop telling me when Postgres dies", and the
     reverse is equally true. They get separate toggles so one can never silently
     disable the other.

This module must keep working when the things it reports on are broken, so it holds to
two rules: it never touches Postgres (fallback telemetry writes to the DB, which is
exactly what may be down), and no failure inside it is allowed to raise into the caller
-- a health check that crashes while reporting bad health reports nothing at all.
"""

from __future__ import annotations

import os
import socket
from datetime import datetime, timezone
from pathlib import Path

from environs import Env

env = Env()
env.read_env()

# Falls back to the watchlist pipeline's settings so this works out of the box on a host
# that already has SES configured, while still allowing a dedicated ops address later.
OPS_ALERT_EMAIL_ENABLED = env.bool(
    "OPS_ALERT_EMAIL_ENABLED", env.bool("WATCHLIST_ALERT_EMAIL_ENABLED", False)
)
OPS_ALERT_EMAIL_FROM = env("OPS_ALERT_EMAIL_FROM", env("WATCHLIST_ALERT_EMAIL_FROM", ""))
OPS_ALERT_EMAIL_TO = env("OPS_ALERT_EMAIL_TO", env("WATCHLIST_ALERT_EMAIL_TO", ""))
AWS_REGION = env("AWS_REGION", "")
AWS_ACCESS_KEY_ID = env("AWS_ACCESS_KEY_ID", "")
AWS_SECRET_ACCESS_KEY = env("AWS_SECRET_ACCESS_KEY", "")

# Plain file, never the database: the DB is one of the things this alerts about.
OPS_ALERT_LOG = Path(
    os.getenv("OPS_ALERT_LOG", str(Path(__file__).resolve().parents[1] / "logs" / "cron" / "ops_alert.log"))
)


def _log(message: str) -> None:
    line = f"[ops_alert] {datetime.now(timezone.utc):%Y-%m-%dT%H:%M:%SZ} {message}"
    print(line, flush=True)
    try:
        OPS_ALERT_LOG.parent.mkdir(parents=True, exist_ok=True)
        with OPS_ALERT_LOG.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except Exception:  # noqa: BLE001 -- logging must never be the thing that fails
        pass


def _recipients() -> list[str]:
    """Whitespace OR comma separated.

    The existing WATCHLIST_ALERT_EMAIL_TO is SPACE separated (four addresses on one
    line), and splitting only on commas turned the whole line into a single malformed
    address -- SES rejected it with "Domain contains control or whitespace". Accept both
    separators so neither convention can silently break the alarm.
    """
    raw = (OPS_ALERT_EMAIL_TO or "").replace(",", " ").replace(";", " ")
    return [addr for addr in raw.split() if addr]


def send_ops_alert(subject: str, body: str) -> bool:
    """Email the operator. Returns True if SES accepted it.

    Never raises. A send failure is logged to OPS_ALERT_LOG (a plain file) so that an
    outage plus a broken mail path still leaves a trace on disk rather than vanishing.
    """
    host = socket.gethostname()
    full_subject = f"[stockey/{host}] {subject}"

    if not OPS_ALERT_EMAIL_ENABLED:
        _log(f"alerting DISABLED, not sending: {full_subject}")
        return False
    to = _recipients()
    if not OPS_ALERT_EMAIL_FROM or not to:
        _log(f"no From/To configured, not sending: {full_subject}")
        return False

    try:
        import boto3

        client = boto3.client(
            "ses",
            region_name=AWS_REGION,
            aws_access_key_id=AWS_ACCESS_KEY_ID,
            aws_secret_access_key=AWS_SECRET_ACCESS_KEY,
        )
        client.send_email(
            Source=OPS_ALERT_EMAIL_FROM,
            # Bcc, matching the watchlist digest's own convention: recipients do not see
            # each other's addresses.
            Destination={"ToAddresses": [OPS_ALERT_EMAIL_FROM], "BccAddresses": to},
            Message={
                "Subject": {"Data": full_subject, "Charset": "UTF-8"},
                "Body": {"Text": {"Data": body, "Charset": "UTF-8"}},
            },
        )
    except Exception as exc:  # noqa: BLE001 -- see docstring: never raise from an alerter
        _log(f"SEND FAILED ({type(exc).__name__}: {exc}) for: {full_subject}")
        return False

    _log(f"sent to {len(to)} recipient(s): {full_subject}")
    return True
