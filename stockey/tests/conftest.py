import pytest


@pytest.fixture(autouse=True)
def _isolate_local_telemetry_files(monkeypatch, tmp_path):
    """Tests must never write to the real production telemetry logs. 2026-08-14 found live: at least
    three existing tests (test_indices_parser_only_considers_last_year_keys,
    test_nse_offmarket_parser_exports_file_run_state, test_download_runner_records_nonzero_exit_fallback)
    exercise record_local_fallback_event's REAL code path without monkeypatching the recorder itself or
    this file, so every test run appended synthetic events (fake keys like "indices/invalid.zip", "bad
    schema" errors) straight into logs/fallback/local_fallback_events.jsonl -- discovered while auditing
    that log for real issues in a specific time window, where the synthetic entries were indistinguishable
    from genuine production fallback events by timestamp alone. Autouse + session-independent (tmp_path is
    per-test) so no test needs to opt in, and a test that explicitly monkeypatches this attribute itself
    (a few already do) simply overrides this on top -- no conflict."""
    from utils import db, fallback_telemetry

    monkeypatch.setattr(fallback_telemetry, "LOCAL_FALLBACK_TELEMETRY_FILE", tmp_path / "local_fallback_events.jsonl")
    monkeypatch.setattr(db, "DB_RETRY_TELEMETRY_FILE", tmp_path / "db_retry_events.jsonl")
