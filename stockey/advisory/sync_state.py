"""Promoted to utils.sync_state (2026-07-28 pure-TA migration, Phase 2): data/ downloaders
depend on it and must not import advisory/, which is archived in a later phase. This shim keeps
advisory/'s existing callers working until the whole package is archived; new code should import
utils.sync_state directly.

Aliased via sys.modules (not a `from ... import *` re-export): a wildcard re-export creates a
SECOND module object, so tests that `monkeypatch.setattr(advisory.sync_state, "sql_to_df", ...)`
would patch a copy the real implementation never reads. Aliasing sys.modules makes
`advisory.sync_state` and `utils.sync_state` the SAME object, so monkeypatching either name
affects the real code path."""
import sys

from utils import sync_state as _impl

sys.modules[__name__] = _impl
