"""Promoted to utils.fallback_telemetry (2026-07-28 pure-TA migration, Phase 2): it has no
advisory-specific logic and utils/* already depended on it, an inverted layering. This shim keeps
advisory/'s ~100 existing callers working until the whole package is archived (migration Phase 4);
new code should import utils.fallback_telemetry directly."""
from utils.fallback_telemetry import *  # noqa: F401,F403
