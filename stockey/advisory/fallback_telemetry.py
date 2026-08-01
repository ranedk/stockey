"""Promoted to utils.fallback_telemetry (2026-07-28 pure-TA migration, Phase 2): it has no
advisory-specific logic and utils/* already depended on it, an inverted layering. This shim keeps
advisory/'s ~100 existing callers working until the whole package is archived (migration Phase 4);
new code should import utils.fallback_telemetry directly.

Aliased via sys.modules (not a `from ... import *` re-export): a wildcard re-export creates a
SECOND module object, so `monkeypatch.setattr(advisory.fallback_telemetry, "upsert_to_db", ...)`
in existing tests would patch a copy the real implementation never reads, silently no-op'ing the
patch. Aliasing sys.modules makes `advisory.fallback_telemetry` and `utils.fallback_telemetry`
the SAME object, so monkeypatching either name affects the real code path."""
import sys

from utils import fallback_telemetry as _impl

sys.modules[__name__] = _impl
