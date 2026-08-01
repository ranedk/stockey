"""Promoted to utils.advisory_date (2026-07-28 pure-TA migration, Phase 2): data/dhanlive/ohlcv_reconcile.py
depends on it and must not import advisory/, which is archived in a later phase. This shim keeps
advisory/'s existing callers (incl. `python -m advisory.advisory_date` cron invocations) working
until the whole package is archived; new code should import utils.advisory_date directly."""
from utils.advisory_date import *  # noqa: F401,F403
from utils.advisory_date import main

if __name__ == "__main__":
    raise SystemExit(main())
