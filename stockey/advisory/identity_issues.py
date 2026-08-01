"""Promoted to utils.identity_issues (2026-07-28 pure-TA migration, Phase 2): data/dhanlive/dhan_db.py
depends on it and must not import advisory/, which is archived in a later phase. This shim keeps
advisory/'s existing callers (incl. `python -m advisory.identity_issues` cron invocations) working
until the whole package is archived; new code should import utils.identity_issues directly."""
from utils.identity_issues import *  # noqa: F401,F403
from utils.identity_issues import main

if __name__ == "__main__":
    raise SystemExit(main())
