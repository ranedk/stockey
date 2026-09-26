#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON_BIN="$("${SCRIPT_DIR}/scripts/resolve_python.sh")"

# OCR of filed documents as its own continuous job (docs/UNIVERSE_PRD.md section 6,
# 2026-09-25). It used to be a step inside the nightly fundamentals pipeline, where it
# took 1.8-3h at ~196 companies and would have overrun at ~1,300. Now cron starts it every
# 30 minutes under a lock (a long run is never doubled); each run stops after
# FUNDAMENTALS_OCR_MAX_RUNTIME_SECONDS and the next picks up where it left off. The
# nightly pipeline's structured_extraction step scores whatever OCR has finished.
#
# Deliberately NOT gated on .pause_fundamentals: it only reads documents already
# collected and makes no judgement, so catching up during a pause is what we want.
# Runs at low CPU priority so the local OCR model never slows the price jobs.
exec nice -n 10 "${SCRIPT_DIR}/scripts/run_with_markers.sh" "fundamentals_ocr" "${PYTHON_BIN}" -m fundamentals.collectors.ocr_pipeline "$@"
