#!/usr/bin/env bash
#
# is_cron_running.sh -- "do I need to restart cron, or is everything fine?"
#
# Answers that in one place, read-only, and exits non-zero when something needs a
# human. Started life as the manual check run repeatedly during the 2026-08-26..31
# Dhan outage.
#
#   scripts/is_cron_running.sh          # human-readable status
#   scripts/is_cron_running.sh --quiet  # exit code only, no output
#   scripts/is_cron_running.sh --json   # machine-readable; what /api/scheduler-health serves
#
# Exit: 0 = healthy   1 = something is wrong   2 = could not run the checks
#
# THE POINT: "alive" and "working" are different questions for go-crond, and only the
# second one matters. It has twice been found running as a process while scheduling
# NOTHING -- 2026-08-19 (dead 5 days, zero alerting, every check that would have caught
# it was itself a go-crond job) and again after a `builder.py` rewrite silently broke a
# live instance's scheduling for 2.5 hours. So this script never reports "up" on the
# strength of a PID: it proves execution by checking that the highest-frequency job's
# log has actually been written recently, which is end-to-end evidence that
# schedule -> fire -> run -> write still works.

set -uo pipefail

STOCKEY_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$STOCKEY_DIR" || exit 2

QUIET=0
JSON=0
for arg in "$@"; do
  case "$arg" in
    --quiet|-q) QUIET=1 ;;
    --json) JSON=1; QUIET=1 ;;
    -h|--help) sed -n '2,26p' "${BASH_SOURCE[0]}"; exit 0 ;;
    *) echo "unknown arg: $arg" >&2; exit 2 ;;
  esac
done

# Overridable so the failure paths below can actually be exercised in a test rather
# than only ever observed on a healthy machine -- an untested health check is how you
# end up trusting a false green.
# Minutes a job may hold its lock before it counts as stuck. Overridable so the warning
# path is testable -- a threshold you can only exercise by waiting six hours is one
# nobody ever verifies.
STUCK_LOCK_MINUTES="${STUCK_LOCK_MINUTES:-360}"
CRONTAB_FILE="${STOCKEY_CRONTAB_FILE:-$STOCKEY_DIR/config/stockey.generated.crontab}"
LOG_DIR="${STOCKEY_CRON_LOG_DIR:-$STOCKEY_DIR/logs/cron}"

PROBLEMS=0
RED=$'\033[31m'; YEL=$'\033[33m'; GRN=$'\033[32m'; DIM=$'\033[2m'; OFF=$'\033[0m'
[ -t 1 ] || { RED=; YEL=; GRN=; DIM=; OFF=; }

# JSON findings accumulate in a temp file rather than a variable so a subshell (the
# `while read` loop over locks, for instance) cannot lose them.
FINDINGS_FILE="$(mktemp -t is_cron_running.XXXXXX)"
trap 'rm -f "$FINDINGS_FILE"' EXIT
SECTION="general"

# Minimal JSON string escaping -- error text can contain quotes, backslashes and tabs.
jesc() { printf '%s' "$1" | sed 's/\\/\\\\/g; s/"/\\"/g; s/\t/ /g'; }
record() { printf '{"level":"%s","section":"%s","message":"%s"}\n' "$1" "$(jesc "$SECTION")" "$(jesc "$2")" >> "$FINDINGS_FILE"; }

say()  { [ "$QUIET" = "1" ] || printf '%b\n' "$*"; }
ok()   { say "  ${GRN}OK${OFF}    $*"; record ok "$*"; }
warn() { say "  ${YEL}WARN${OFF}  $*"; record warn "$*"; PROBLEMS=$((PROBLEMS+1)); }
bad()  { say "  ${RED}FAIL${OFF}  $*"; record error "$*"; PROBLEMS=$((PROBLEMS+1)); }
info() { say "  ${DIM}      $*${OFF}"; }
hdr()  { SECTION="$*"; say "\n$*"; }

# --------------------------------------------------------------- go-crond alive
hdr "scheduler"

# pgrep -x matches the process NAME exactly. Deliberately not `pgrep -f <pattern>`:
# that also matches this script's own shell, whose argv contains the pattern string --
# the self-match footgun that has bitten this repo before.
CROND_PID="$(pgrep -x go-crond | head -1)"
if [ -z "$CROND_PID" ]; then
  bad "go-crond is NOT running -- no stockey job will fire."
  info "start it:  ./start_cron.sh   (or wait <=15 min for scripts/ensure_go_crond_alive.sh)"
else
  CROND_PPID="$(ps -o ppid= -p "$CROND_PID" 2>/dev/null | tr -d ' ')"
  CROND_SINCE="$(ps -o lstart= -p "$CROND_PID" 2>/dev/null | sed 's/^ *//')"
  ok "go-crond running (pid $CROND_PID, since $CROND_SINCE)"
  # A go-crond parented to an interactive/agent shell dies when that shell exits.
  if [ "${CROND_PPID:-0}" != "1" ]; then
    warn "go-crond's parent is pid $CROND_PPID, not init -- it will die when that process exits."
    info "restart detached:  ./stop_cron.sh && setsid --fork ./start_cron.sh >> $LOG_DIR/manual_start.log 2>&1"
  else
    info "parent is init (pid 1) -- survives the shell that started it."
  fi
fi

# ------------------------------------------------------- go-crond actually firing
# Derive the shortest schedule in the crontab (*/N), then require that SOME job log
# has been written inside a couple of those intervals. This is the check that
# distinguishes "process exists" from "scheduling works".
if [ ! -r "$CRONTAB_FILE" ]; then
  bad "generated crontab missing: $CRONTAB_FILE  (run: python builder.py)"
else
  JOB_COUNT="$(grep -cE '^[0-9*]' "$CRONTAB_FILE" 2>/dev/null || echo 0)"
  MIN_INTERVAL="$(grep -oE '^\*/[0-9]+' "$CRONTAB_FILE" 2>/dev/null | tr -d '*/' | sort -n | head -1)"
  : "${MIN_INTERVAL:=60}"

  NEWEST_LOG="$(find "$LOG_DIR" -maxdepth 1 -name '*.log' -type f -printf '%T@ %p\n' 2>/dev/null | sort -rn | head -1)"
  # %T@ carries a fractional part ("1788177578.4744768040") which bash arithmetic
  # rejects outright. Strip it. Getting this wrong once made the firing check abort
  # mid-script while the summary still printed "everything is running" -- the exact
  # false-green this script exists to prevent, so the guard below is not optional.
  NEWEST_TS="${NEWEST_LOG%% *}"; NEWEST_TS="${NEWEST_TS%%.*}"
  if [ -z "$NEWEST_LOG" ] || [ -z "$NEWEST_TS" ] || ! [ "$NEWEST_TS" -eq "$NEWEST_TS" ] 2>/dev/null; then
    bad "could not determine when a job last ran (no readable logs in $LOG_DIR) -- cannot prove go-crond is firing."
  else
    NEWEST_AGE_MIN=$(( ( $(date +%s) - NEWEST_TS ) / 60 ))
    NEWEST_NAME="$(basename "${NEWEST_LOG#* }")"
    # Two missed cycles plus a minute of slack before calling it broken.
    STALE_AFTER=$(( MIN_INTERVAL * 2 + 1 ))
    if [ -n "$CROND_PID" ] && [ "$NEWEST_AGE_MIN" -gt "$STALE_AFTER" ]; then
      bad "go-crond is alive but has fired NOTHING for ${NEWEST_AGE_MIN} min (most frequent job runs every ${MIN_INTERVAL} min)."
      info "this is the silent-scheduling failure: the process survives, the schedule does not."
      info "fix:  ./restart_cron.sh"
    elif [ -n "$CROND_PID" ]; then
      ok "firing: $NEWEST_NAME written ${NEWEST_AGE_MIN} min ago (most frequent job: every ${MIN_INTERVAL} min)"
    else
      info "last job log: $NEWEST_NAME, ${NEWEST_AGE_MIN} min ago"
    fi
    info "$JOB_COUNT jobs in $(basename "$CRONTAB_FILE")"
  fi

  # builder.py rewriting the crontab under a live go-crond silently breaks scheduling.
  if command -v python3 >/dev/null 2>&1 && [ -x "$STOCKEY_DIR/scripts/resolve_python.sh" ]; then
    PY="$("$STOCKEY_DIR/scripts/resolve_python.sh" 2>/dev/null)"
    if [ -n "${PY:-}" ] && "$PY" builder.py --check-crontab >/dev/null 2>&1; then
      ok "generated crontab matches the template (no drift)"
    else
      warn "generated crontab has DRIFTED from config/stockey.crontab.template."
      info "regenerate, then restart -- a rewrite under a live go-crond kills its scheduling:"
      info "  ./stop_cron.sh && $PY builder.py && setsid --fork ./start_cron.sh"
    fi
  fi
fi

# ------------------------------------------------------------------- stale locks
hdr "job locks"
# A lock FILE existing proves nothing -- flock releases the lock when the holder exits
# but leaves the file behind forever. Reporting on the file's presence made this print
# "stockey_cron_start.lock held 1 min (a job is running)" about a lock nothing held
# (observed 2026-09-04, right after go-crond correctly released it before exec'ing).
# A health check that cries wolf is the one failure this whole script exists to avoid,
# so ask who actually HOLDS it.
#
# fuser, not `flock -n`: flock would momentarily ACQUIRE the lock to test it, and a job
# starting in that instant would find it taken and silently no-op (with_lock.sh exits
# quietly rather than queueing). A monitor must never be able to cause the outage it is
# watching for. fuser only reads /proc and takes nothing.
FOUND_LOCK=0
for l in /tmp/stockey_*.lock; do
  [ -e "$l" ] || continue
  holder="$(fuser "$l" 2>/dev/null | tr -s ' ')"
  [ -n "$holder" ] || continue          # stale file, nobody holds it -- not a finding
  FOUND_LOCK=1
  # Age comes from the HOLDER's runtime, not the lock file's mtime. Opening a file for
  # flock updates its mtime, so a lock held for nine hours still reports a brand-new
  # file -- which silently defeated the stuck-job check (caught while testing this fix
  # on 2026-09-04: a deliberately 9-hour-old lock reported "held 0 min").
  # fuser reports EVERY pid holding the file, space separated. Splitting on whitespace
  # is the whole point -- an earlier `tr -d ' '` deleted the separators first, so the
  # pids concatenated into one nonsense number, `ps -p` failed, `|| echo 0` swallowed it
  # and AGE_MIN was always 0. The stuck-lock warning below could never fire.
  # OLDEST holder, not first: with flock the parent shell and its children all hold the
  # fd, and the parent is the one whose age means "how long has this job been stuck".
  holder_pid=""; AGE_MIN=0
  for pid in $holder; do
    case "$pid" in (*[!0-9]*|"") continue;; esac
    age=$(( $(ps -o etimes= -p "$pid" 2>/dev/null || echo 0) / 60 ))
    if [ -z "$holder_pid" ] || [ "$age" -gt "$AGE_MIN" ]; then holder_pid="$pid"; AGE_MIN="$age"; fi
  done
  [ -n "$holder_pid" ] || continue      # every holder exited between fuser and ps
  # Held far longer than any job should run means the next cron run of that job is
  # silently skipped, every time, until the holder dies.
  if [ "$AGE_MIN" -gt "$STUCK_LOCK_MINUTES" ]; then
    warn "$(basename "$l") held ${AGE_MIN} min by pid ${holder_pid} -- that job is stuck and every later run of it has been silently skipped."
  else
    info "$(basename "$l") held ${AGE_MIN} min by pid ${holder_pid} (a job is running)"
  fi
done
[ "$FOUND_LOCK" = "0" ] && ok "no job locks held"

# -------------------------------------------------------------- OS-level crontab
# Host state: NOT tracked by git, does not survive a machine move, and is where the
# go-crond watchdog lives. A missing watchdog means nothing resurrects go-crond.
hdr "OS crontab (host state, not in git)"
OS_CRON="$(crontab -l 2>/dev/null)"
if [ -z "$OS_CRON" ]; then
  bad "the user crontab is EMPTY -- the go-crond watchdog is gone."
  info "reinstall: (crontab -l 2>/dev/null; echo '*/15 * * * * $STOCKEY_DIR/scripts/ensure_go_crond_alive.sh >/dev/null 2>&1') | crontab -"
else
  if printf '%s\n' "$OS_CRON" | grep -q '^[^#].*ensure_go_crond_alive.sh'; then
    ok "go-crond watchdog installed (auto-restarts a dead scheduler)"
  else
    warn "ensure_go_crond_alive.sh is NOT active -- nothing will restart go-crond if it dies."
  fi
  ACTIVE=$(printf '%s\n' "$OS_CRON" | grep -cE '^[^#[:space:]]')
  DISABLED=$(printf '%s\n' "$OS_CRON" | grep -cE '^# *[0-9*]+ ' || true)
  info "$ACTIVE active entr$([ "$ACTIVE" = "1" ] && echo y || echo ies)"
  if [ "${DISABLED:-0}" -gt 0 ]; then
    warn "${DISABLED} crontab entr$([ "$DISABLED" = "1" ] && echo y is || echo ies are) commented out -- deliberate, or left disabled after maintenance?"
    printf '%s\n' "$OS_CRON" | grep -E '^# *[0-9*]+ ' | sed 's/^/        /' | while read -r l; do info "$l"; done
  fi
fi

# ----------------------------------------------------------------------- services
hdr "services"
check_http() {  # name, url, required(yes/no), hint
  if curl -fs -m 5 "$2" >/dev/null 2>&1; then
    ok "$1"
  elif [ "$3" = "yes" ]; then
    bad "$1 is DOWN -- $4"
  else
    info "$1 is not running -- $4"
  fi
}
check_http "fundamentals API :8000" "http://localhost:8000/api/health" yes \
           "cron retries every 5 min; force now with ./all_fundamentals_api.sh"
# Chrome is host state and deliberately manual -- see utils/cdp.py. Every Playwright
# collector fails fast and loudly without it, so its absence is a real finding.
check_http "Chrome CDP :9222" "http://localhost:9222/json/version" yes \
           "start it: scripts/start_chrome_cdp.sh  (NSE/Dhan collectors cannot run without it)"

if [ "$JSON" = "1" ]; then
  # PROBLEMS counts warn+fail; status is driven by whether any hard FAIL was recorded,
  # so a warning-only run reads as "warn", not "error".
  errors=$(grep -c '"level":"error"' "$FINDINGS_FILE" 2>/dev/null || true)
  warns=$(grep -c '"level":"warn"' "$FINDINGS_FILE" 2>/dev/null || true)
  if [ "${errors:-0}" -gt 0 ]; then overall=error
  elif [ "${warns:-0}" -gt 0 ]; then overall=warn
  else overall=ok; fi
  printf '{"status":"%s","error_count":%s,"warn_count":%s,"checked_at":"%s","findings":[' \
    "$overall" "${errors:-0}" "${warns:-0}" "$(date -u '+%Y-%m-%dT%H:%M:%SZ')"
  paste -sd, "$FINDINGS_FILE" 2>/dev/null
  printf ']}\n'
  [ "${errors:-0}" -gt 0 ] && exit 1
  exit 0
fi

say ""
if [ "$PROBLEMS" = "0" ]; then
  say "  ${GRN}everything is running -- no restart needed.${OFF}"
  exit 0
fi
say "  ${YEL}${PROBLEMS} thing(s) need attention (see above).${OFF}"
exit 1
