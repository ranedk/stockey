#!/usr/bin/env bash
cd /home/dev/code/trading/stockey
source .xstockey/bin/activate
echo "=== start $(date -u)"
python -u -m fundamentals.trigger_study fetch --sample full
echo "=== filings done $(date -u)"
python -u -m fundamentals.trigger_study queue --sample full
echo "=== queue done $(date -u)"
python -u -m fundamentals.trigger_study results --sample full
echo "=== results done $(date -u)"
