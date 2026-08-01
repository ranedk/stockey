"""Promoted to utils.external_task_queue (2026-07-28 pure-TA migration, Phase 2): data/download_queue.py
depends on it and must not import advisory/, which is archived in a later phase. This shim keeps
advisory/'s existing callers (incl. `python -m advisory.external_task_queue` cron invocations) working
until the whole package is archived; new code should import utils.external_task_queue directly."""
from utils.external_task_queue import *  # noqa: F401,F403
from utils.external_task_queue import main

if __name__ == "__main__":
    raise SystemExit(main())
