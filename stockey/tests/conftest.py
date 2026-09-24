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


@pytest.fixture(autouse=True)
def _schema_migrations_never_run_from_tests(monkeypatch):
    """A test must never ALTER the live database. apply_schema_migration is called from
    many module ensure_* helpers; unstubbed, a test run applied real migrations (found in
    the 2026-09-23 data audit, when 37 tests were measured reaching the live DB). Every
    migration reads as already applied; tests of the migration machinery itself override
    these with their own monkeypatch.setattr, which runs after this and wins."""
    from utils import schema_migrations

    # Guarded at the module's DB boundary, not by replacing its functions, so tests of the
    # functions themselves still run them (and override these with their own stubs).
    monkeypatch.setattr(schema_migrations, "execute_db_operation", lambda *a, **k: None)
    monkeypatch.setattr(schema_migrations, "load_schema_migration",
                        lambda _migration_id: {"status": "applied", "checksum": ""})
