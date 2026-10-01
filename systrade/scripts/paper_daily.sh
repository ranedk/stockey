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

# Strategy 3: the literature's momentum at 6/9/12/12-minus-1 month lookbacks, a
# blend of their books (docs/strategies/2026-09-12_momentum_lookback_blend.md).
# No reference track: drawing one would read 2022+, which this family has left unread.
go run ./cmd/paper run -strategy momentum-lookback-blend

# Strategy 4: the low-volatility anomaly as a blend of five low-risk books
# (docs/strategies/2026-09-12_low_volatility_blend.md). No reference track.
go run ./cmd/paper run -strategy low-volatility-blend

# Strategy 5: momentum and low volatility as one book of their nine variants
# (docs/strategies/2026-09-12_momentum_lowvol_combination.md). No reference track.
go run ./cmd/paper run -strategy momentum-lowvol-combination

# TODO A3 (2026-09-29): trend-speed-blend with a rank buffer -- a separate record, own clock.
go run ./cmd/paper run -strategy trend-speed-blend-buffered

# Stage 2 relative-strength leaders (LEDGER row 54, 2026-10-01)
go run ./cmd/paper run -strategy stage2-rs-leaders

# Every forward track's sheet as Dhan pre-open orders, DRY RUN: built,
# checked against the sheet and logged to systrader_exec_*. Stability-gate
# evidence (docs/RESEARCH_PROTOCOL.md, S2 and S5). Never sends — no -live.
# Industry rotation snapshot for the screener's Rotation page (docs/SECTOR_ROTATION_PRD.md);
# reporting only, ~20 s. Recomputed daily so the current week stays fresh.
go run ./cmd/rotation snapshot

go run ./cmd/dhan orders -strategy all

echo "=== paper_daily done ==="
