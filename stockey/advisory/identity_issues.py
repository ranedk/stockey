"""Promoted to utils.identity_issues (2026-07-28 pure-TA migration, Phase 2): data/dhanlive/dhan_db.py
depends on it and must not import advisory/, which is archived in a later phase. This shim keeps
advisory/'s existing callers (incl. `python -m advisory.identity_issues` cron invocations) working
until the whole package is archived; new code should import utils.identity_issues directly.

Aliased via sys.modules (not a `from ... import *` re-export): a wildcard re-export creates a
SECOND module object, so tests that monkeypatch `advisory.identity_issues.db_session` would patch
a copy the real implementation never reads. Aliasing sys.modules makes `advisory.identity_issues`
and `utils.identity_issues` the SAME object, so monkeypatching either name affects the real code
path."""
import sys

from utils import identity_issues as _impl

sys.modules[__name__] = _impl

if __name__ == "__main__":
    raise SystemExit(_impl.main())
