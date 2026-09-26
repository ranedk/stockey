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
# Sharing the machine (2026-09-26): the local model otherwise takes every idle core with
# ~65 threads and several GB of RAM.
#  - nice 10 / ionice idle: CPU and disk go to anything else that wants them first.
#  - OCR_THREADS (default 12 of 16 cores) caps torch's thread pool, so a few cores stay free
#    for Chrome, Postgres and the collectors even while a page is being read.
#  - oom_score_adj 800: if memory ever runs out, the kernel kills this job, not Postgres or
#    Chrome. A killed run loses nothing -- its document stays pending for the next run.
OCR_THREADS="${OCR_THREADS:-12}"
export OMP_NUM_THREADS="${OCR_THREADS}" MKL_NUM_THREADS="${OCR_THREADS}"
echo 800 > /proc/self/oom_score_adj 2>/dev/null || true
exec nice -n 10 ionice -c 3 "${SCRIPT_DIR}/scripts/run_with_markers.sh" "fundamentals_ocr" "${PYTHON_BIN}" -m fundamentals.collectors.ocr_pipeline "$@"
