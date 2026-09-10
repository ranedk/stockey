#!/usr/bin/env bash
# Daily forward record for the frozen paper strategies (docs/strategies/,
# paper.Specs).
#
# Recomputes each track from its start date and rewrites the tables, so this is
# safe to run twice, safe to miss a day, and self-healing: a gap fills itself in
# on the next run. It writes only systrader-owned systrader_paper_* tables.
#
# Runs after scripts/sync_from_stockey.sh has landed the day's adjusted prices —
# without them the tracker simply finds no new trading day and changes nothing,
# which is the correct behaviour on a holiday.
set -euo pipefail

cd "$(dirname "$0")/.."
export PATH="$HOME/go-sdk/bin:$HOME/go/bin:$PATH"

echo "=== paper_daily $(date -u +%Y-%m-%dT%H:%M:%SZ) ==="

# The forward record: the only evidence that counts.
go run ./cmd/paper run

# The in-sample reference curve the screener shows beside it, clearly labelled
# there as a backtest. Recomputed too, so the two always end on the same date.
go run ./cmd/paper run -name trend-quintile-reference -start 2022-01-01

# Strategy 2: the same book with its speeds blended (Law 5), a parallel record
# from 2026-09-10 (docs/strategies/2026-09-10_trend_speed_blend.md).
go run ./cmd/paper run -strategy trend-speed-blend
go run ./cmd/paper run -strategy trend-speed-blend -name trend-speed-blend-reference -start 2022-01-01

echo "=== paper_daily done ==="
