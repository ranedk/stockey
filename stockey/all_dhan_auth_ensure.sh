#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON_BIN="$("${SCRIPT_DIR}/scripts/resolve_python.sh")"

# Refresh the Dhan access token BEFORE the morning catch-up jobs run.
#
# BUG FOUND LIVE 2026-09-04: Dhan tokens expire daily around 20:35 UTC (02:05 IST), and
# the morning catch-up reconcile runs at 02:10 UTC -- five minutes AFTER expiry. The
# evening chain (23:15/23:40 IST = 17:45/18:10 UTC) sits comfortably inside the token's
# life and worked fine; the morning pass did not, and failed ~950 of 1500 symbols.
#
# The client's own auto-refresh could not save it: _is_auth_error() only treats a 400 as
# an auth failure when the body contains "access token" AND "invalid"/"expired", but an
# expired token on the DAILY endpoint comes back as
#   400 "Missing required fields, bad values for parameters etc."
# which matches nothing, so no refresh was ever attempted and every symbol failed
# identically from the very first call (the failure list started at 20MICRONS -- i.e.
# alphabetically first, not a bad-symbol pattern).
#
# Fixing the predicate was the other option and was rejected: "Missing required fields"
# is a genuinely ambiguous message that also covers real parameter errors, so treating it
# as an auth failure would trigger a browser re-login on every malformed request -- and
# Dhan blocks accounts for too many login attempts. Refreshing on a schedule, before the
# work, is unambiguous.
# --min-fresh-minutes 720: a Dhan token lives 24h, this runs once a weekday morning, and
# the evening Dhan chain runs ~17:45-18:10 UTC. At the default (10 min) a token with any
# life left is kept, so a token minted at, say, 07:48 IST was NOT refreshed at 07:35 the
# next morning, died 13 minutes later, and took that whole day's Dhan jobs with it
# (2026-09-18: the reconcile got 23 of 1500 symbols). Refreshing anything under 12h old
# makes every weekday start with a full-day token. Dhan's RenewToken endpoint would be the
# cheaper fix (no consent, no browser) but answers "Renewal of token not allowed for this
# token type" for consent-flow tokens like ours (checked 2026-09-19).
exec "${SCRIPT_DIR}/scripts/run_with_markers.sh" "dhan_auth_ensure" \
  "${PYTHON_BIN}" -m data.dhanlive.auth_cli ensure --auto-login --min-fresh-minutes 720
