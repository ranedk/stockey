#!/usr/bin/env bash
# Re-run the trigger study's NSE results fetch: only companies with nothing stored are asked.
cd "$(dirname "$0")/.." && source .xstockey/bin/activate
echo "=== results retry start $(date -u)"
python -u -m fundamentals.trigger_study results --sample full
echo "=== results retry done $(date -u)"
