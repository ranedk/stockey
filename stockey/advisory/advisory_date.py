"""Promoted to utils.advisory_date (2026-07-28 pure-TA migration, Phase 2): data/dhanlive/ohlcv_reconcile.py
depends on it and must not import advisory/, which is archived in a later phase. This shim keeps
advisory/'s existing callers (incl. `python -m advisory.advisory_date` cron invocations) working
until the whole package is archived; new code should import utils.advisory_date directly.

Aliased via sys.modules (not a `from ... import *` re-export): a wildcard re-export creates a
SECOND module object, so tests that monkeypatch `advisory.advisory_date.sql_to_df` would patch a
copy the real implementation never reads. Aliasing sys.modules makes `advisory.advisory_date` and
`utils.advisory_date` the SAME object, so monkeypatching either name affects the real code path."""
import sys

from utils import advisory_date as _impl

sys.modules[__name__] = _impl

if __name__ == "__main__":
    raise SystemExit(_impl.main())
