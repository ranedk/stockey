#!/usr/bin/env bash
set -euo pipefail

# Cron-driven liveness check for cmd/api (systrader's own API, serving
# screener/'s stage-analysis page): if it's already answering /api/health,
# `mage api:ensure` no-ops immediately; otherwise it builds and starts a
# fresh instance. Register in the OS-level user crontab every few minutes
# (`crontab -e`), flock-guarded the same way scripts/sync_from_stockey.sh's
# own crontab entry already is -- two ticks racing while the server is
# mid-start must not both try to bind :8090.
#
# A thin wrapper, not a bare crontab line calling mage directly, because
# cron's PATH doesn't include the Go toolchain or GOPATH/bin on this host --
# set explicitly here rather than baked into the crontab line itself, so the
# crontab entry stays readable.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export PATH="/home/dev/go-sdk/bin:/home/dev/go/bin:${PATH}"

cd "${SCRIPT_DIR}"
exec mage api:ensure
