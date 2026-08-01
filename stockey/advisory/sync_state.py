"""Promoted to utils.sync_state (2026-07-28 pure-TA migration, Phase 2): data/ downloaders
depend on it and must not import advisory/, which is archived in a later phase. This shim keeps
advisory/'s existing callers working until the whole package is archived; new code should import
utils.sync_state directly."""
from utils.sync_state import *  # noqa: F401,F403
