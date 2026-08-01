"""Promoted to utils.external_task_queue (2026-07-28 pure-TA migration, Phase 2): data/download_queue.py
depends on it and must not import advisory/, which is archived in a later phase. This shim keeps
advisory/'s existing callers (incl. `python -m advisory.external_task_queue` cron invocations)
working until the whole package is archived; new code should import utils.external_task_queue
directly.

Aliased via sys.modules (not a `from ... import *` re-export): a wildcard re-export creates a
SECOND module object, so tests that monkeypatch `advisory.external_task_queue.db_session` would
patch a copy the real implementation never reads. Aliasing sys.modules makes
`advisory.external_task_queue` and `utils.external_task_queue` the SAME object, so monkeypatching
either name affects the real code path."""
import sys

from utils import external_task_queue as _impl

sys.modules[__name__] = _impl

if __name__ == "__main__":
    raise SystemExit(_impl.main())
