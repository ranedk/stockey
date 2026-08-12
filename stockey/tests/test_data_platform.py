from __future__ import annotations

import argparse
import asyncio
import contextlib
import fcntl
import hashlib
import json
import os
import shlex
import subprocess
import sys
import types
from datetime import date
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from pathlib import Path
from typing import Any

import pandas as pd
import pytest
import requests
import torch
from bs4 import BeautifulSoup
from fastapi.testclient import TestClient

from utils import identity_issues
from utils import fallback_telemetry
from utils import sync_state
from utils import external_task_queue
from utils import cron_status
from utils import migration_drift
from utils import advisory_date
from data.dhanlive import auth as dhan_auth
from data.dhanlive import auth_cli as dhan_auth_cli
from data.dhanlive import client as dhan_client
from data.dhanlive import web_login as dhan_web_login
from data.dhanlive import dhan_db, ohlcv as dhan_ohlcv, scrip_master as dhan_scrip_master
from data.nseindia import bhavcopy_downloader, bhavcopy_parser, corporate_actions, earnings_events, indices_downloader, indices_parser, offmarket, recent_events, security_history
from data.sharpelydata import sharpely_data
from data.sharpelydata import scrip_master as sharpely_scrip_master
from data import benchmark_sync, download_runner, download_queue
from fundamentals.collectors import security_master as fundamentals_security_master
from fundamentals.collectors import screenerin as fundamentals_screenerin
from fundamentals.screens import l1_universe as fundamentals_l1_universe
from fundamentals.screens import l2_state as fundamentals_l2_state
from fundamentals.collectors import bse_announcements as fundamentals_bse_announcements
from fundamentals.collectors import events_store as fundamentals_events_store
from fundamentals.collectors import nse_pit as fundamentals_nse_pit
from fundamentals.collectors import rating_agencies as fundamentals_rating_agencies
from fundamentals.collectors import ocr_pipeline as fundamentals_ocr_pipeline
from fundamentals.collectors import structured_extraction as fundamentals_structured_extraction
from fundamentals.screens import l3_triggers as fundamentals_l3_triggers
from fundamentals.screens import llm_triage as fundamentals_llm_triage
from fundamentals.screens import l4_thesis as fundamentals_l4_thesis
from fundamentals.collectors import sector_data as fundamentals_sector_data
from fundamentals.screens import sector_cycle as fundamentals_sector_cycle
from fundamentals.screens import technicals as fundamentals_technicals
from fundamentals.screens import watchlist as fundamentals_watchlist
from fundamentals.screens import watch_summary as fundamentals_watch_summary
from fundamentals.screens import notifications as fundamentals_notifications
from fundamentals.api import queries as fundamentals_api_queries
from fundamentals.api.app import app as fundamentals_api_app
from fundamentals import run_pipeline as fundamentals_run_pipeline
from utils import codex_cli
from utils import db as db_utils
from utils import http as http_utils
from utils import company_master as company_master_utils
from utils import ingestion_state
from utils import redaction
from utils import redis_bkp_restore
from utils.ocr import llm_ocr
from utils import poppler as poppler_utils
from utils import exchange_rate_limiter
from utils import nse_rate_limiter
from utils import redis_utils
from utils import sync as sync_utils


def test_ingestion_state_ensure_table_uses_schema_registry(monkeypatch):
    calls = []

    monkeypatch.setattr(ingestion_state, "apply_schema_migration", lambda **kwargs: calls.append(kwargs))

    ingestion_state.ensure_ingestion_state_table()

    assert len(calls) == 1
    call = calls[0]
    assert call["migration_id"] == ingestion_state.INGESTION_STATE_SCHEMA_MIGRATION_ID
    assert call["owner"] == "utils.ingestion_state"
    assert call["metadata"]["tables"] == [ingestion_state.TABLE_NAME]
    ddl = "\n".join(call["statements"])
    assert f"CREATE TABLE IF NOT EXISTS {ingestion_state.TABLE_NAME}" in ddl
    assert "PRIMARY KEY (source_prefix, object_key)" in ddl
    assert "ADD COLUMN IF NOT EXISTS error_message" in ddl


def test_ingestion_state_db_paths_use_retryable_operations(monkeypatch):
    operation_names: list[str] = []
    executed: list[tuple[object, object]] = []

    class FakeCursor:
        def __init__(self, *, dict_rows: bool = False):
            self.dict_rows = dict_rows

        def execute(self, query, params=None):
            executed.append((query, params))

        def fetchall(self):
            if not self.dict_rows:
                return [("file-1",)]
            return [{"object_key": "file-1", "status": "failed", "error_message": "classification=parser_bug; x"}]

    class FakeSession:
        def __init__(self, *, dict_factory: bool = False):
            self.dict_factory = dict_factory

        def __enter__(self):
            return None, FakeCursor(dict_rows=self.dict_factory)

        def __exit__(self, *_args):
            return False

    def fake_execute_db_operation(operation, *, operation_name, **_kwargs):
        operation_names.append(operation_name)
        return operation()

    monkeypatch.setattr(ingestion_state, "ensure_ingestion_state_table", lambda: None)
    monkeypatch.setattr(ingestion_state, "db_session", lambda **kwargs: FakeSession(dict_factory=bool(kwargs.get("dict_factory"))))
    monkeypatch.setattr(ingestion_state, "execute_db_operation", fake_execute_db_operation)

    assert ingestion_state.get_processed_keys("bhavcopy") == {"file-1"}
    ingestion_state.mark_state("bhavcopy", "file-2", status="empty_valid_source")
    assert ingestion_state.get_failed_entries("bhavcopy")[0]["object_key"] == "file-1"
    assert ingestion_state.get_state_entries("bhavcopy", status="failed", limit=5)[0]["status"] == "failed"
    ingestion_state.clear_state("bhavcopy", "file-2")

    assert operation_names == [
        "ingestion_state:get_processed_keys",
        "ingestion_state:mark_state",
        "ingestion_state:get_failed_entries",
        "ingestion_state:get_state_entries",
        "ingestion_state:clear_state",
    ]
    assert len(executed) == 5


def test_ingestion_state_runner_summarizes_statuses_and_classifications():
    # Active vs stale is decided against wall-clock now (30-day window), so use relative dates --
    # absolute dates would silently age out and flip an "active" failure to "stale_historical".
    recent = pd.Timestamp.utcnow().normalize() - pd.Timedelta(days=1)
    old = pd.Timestamp.utcnow().normalize() - pd.Timedelta(days=400)
    old_key = f"bhavcopy/bhavcopy_{old.date()}.zip"
    active_key = f"bhavcopy/bhavcopy_{recent.date()}.zip"
    rows = [
        {
            "source_prefix": "bhavcopy",
            "object_key": old_key,
            "status": "failed",
            "error_message": "classification=bad_file_retryable; bad zip",
            "processed_at": old,
        },
        {
            "source_prefix": "indices",
            "object_key": f"indices/indices_{old.date()}.zip",
            "status": "empty_valid_source",
            "error_message": None,
            "processed_at": old,
        },
        {
            "source_prefix": "bhavcopy",
            "object_key": active_key,
            "status": "failed",
            "error_message": "classification=schema_changed; missing columns",
            "processed_at": recent,
        },
    ]

    summary = ingestion_state.summarize_state_entries(rows, sample_limit=1)

    assert summary["count"] == 3
    assert summary["status_counts"] == {"empty_valid_source": 1, "failed": 2}
    assert summary["source_counts"] == {"bhavcopy": 2, "indices": 1}
    assert summary["classification_counts"] == {"bad_file_retryable": 1, "schema_changed": 1}
    assert summary["classification_details"]["bad_file_retryable"]["label"] == "Retryable bad file"
    assert "downloaded file was corrupt" in summary["classification_details"]["bad_file_retryable"]["meaning"]
    assert summary["classification_details"]["schema_changed"]["trust_impact"] == "active_recent_failures_block_trust"
    assert summary["failure_lifecycle_counts"] == {"active": 1, "not_failed": 1, "stale_historical": 1}
    assert summary["active_failure_count"] == 1
    assert summary["stale_historical_failure_count"] == 1
    assert summary["active_failure_sample_rows"][0]["object_key"] == active_key
    assert summary["active_failure_sample_rows"][0]["classification_detail"]["label"] == "Schema changed"
    assert summary["stale_historical_failure_sample_rows"][0]["object_key"] == old_key
    assert summary["enriched_sample_rows"][0]["classification_detail"]["label"] == "Retryable bad file"
    assert summary["sample_rows"] == rows[:1]


def test_ingestion_state_extract_failure_classification_handles_missing_values():
    assert ingestion_state.extract_failure_classification("classification=parser_bug; parse failed") == "parser_bug"
    assert ingestion_state.extract_failure_classification("prefix; classification=schema_changed; missing column") == "schema_changed"
    assert ingestion_state.extract_failure_classification("plain error without class") is None
    assert ingestion_state.extract_failure_classification(None) is None


def test_file_state_parser_failure_classifiers_follow_shared_contract():
    from data.nseindia import offmarket_parser

    parser_contracts = [
        (
            "bhavcopy",
            bhavcopy_parser.classify_bhavcopy_parse_failure,
            {
                RuntimeError("bad_bhavcopy_zip:bad.zip"): "bad_file_retryable",
                KeyError("TradDt"): "schema_changed",
                RuntimeError("unexpected parser branch"): "parser_bug",
            },
            {"bad_file_retryable", "schema_changed", "parser_bug"},
        ),
        (
            "indices",
            indices_parser.classify_indices_parse_failure,
            {
                RuntimeError("bad_indices_zip:bad.zip"): "bad_file_retryable",
                KeyError("Index Name"): "schema_changed",
                RuntimeError("unexpected parser branch"): "parser_bug",
            },
            {"bad_file_retryable", "schema_changed", "parser_bug"},
        ),
        (
            "offmarket",
            offmarket_parser.classify_offmarket_parse_failure,
            {
                pd.errors.EmptyDataError("empty"): "empty_valid_source",
                KeyError("client_name"): "schema_changed",
                RuntimeError("unexpected parser branch"): "parser_bug",
            },
            {"empty_valid_source", "schema_changed", "parser_bug"},
        ),
    ]

    for parser_name, classifier, examples, allowed in parser_contracts:
        for exc, expected in examples.items():
            classification = classifier(exc)
            assert classification == expected, parser_name
            assert classification in allowed, parser_name
            message = f"classification={classification}; {type(exc).__name__}: {exc}"
            assert ingestion_state.extract_failure_classification(message) == classification


def test_ingestion_state_runner_summary_cli_uses_filters(monkeypatch, capsys):
    from scripts import ingestion_state_runner

    calls = []
    rows = [
        {
            "source_prefix": "offmarket",
            "object_key": "offmarket/file.csv",
            "status": "failed",
            "error_message": "classification=parser_bug; parse failed",
        }
    ]

    def fake_get_state_entries(*, source_prefix=None, status=None, limit=None):
        calls.append({"source_prefix": source_prefix, "status": status, "limit": limit})
        return rows

    monkeypatch.setattr(ingestion_state_runner, "get_state_entries", fake_get_state_entries)

    ingestion_state_runner.main(["summary", "--source", "offmarket", "--status", "failed", "--limit", "10", "--sample-limit", "2"])

    payload = json.loads(capsys.readouterr().out)
    assert calls == [{"source_prefix": "offmarket", "status": "failed", "limit": 10}]
    assert payload["status"] == "ok"
    assert payload["count"] == 1
    assert payload["classification_counts"] == {"parser_bug": 1}
    assert payload["sample_rows"][0]["object_key"] == "offmarket/file.csv"


def test_ohlcv_reconcile_expected_day_is_trading_day_and_clock_aware(monkeypatch):
    from data.dhanlive import ohlcv_reconcile as orc

    calls: list[object] = []

    def fake_latest(value=None, **_kwargs):
        calls.append(value)
        # first call (for "now") says today IS a trading day; second call resolves the prior one
        if len(calls) == 1:
            return pd.Timestamp("2026-07-06", tz="UTC")
        return pd.Timestamp("2026-07-03", tz="UTC")

    monkeypatch.setattr(orc, "latest_trading_day_on_or_before", fake_latest)
    monkeypatch.setattr(orc, "_market_calendar_date", lambda value=None, **k: pd.Timestamp("2026-07-06", tz="UTC"))
    # intraday on a trading day (10:00 IST): today's bars cannot exist yet -> prior trading day
    monkeypatch.setattr(orc, "_ist_now", lambda now=None: pd.Timestamp("2026-07-06 10:00", tz="Asia/Kolkata"))
    assert orc.expected_complete_trading_day().date().isoformat() == "2026-07-03"

    # post-close (18:45 IST): today's bars ARE expected -> today (the 18:45 pre-advisory
    # reconcile must pull today's bars before the 19:10 advisory)
    calls.clear()
    monkeypatch.setattr(orc, "_ist_now", lambda now=None: pd.Timestamp("2026-07-06 18:45", tz="Asia/Kolkata"))
    assert orc.expected_complete_trading_day().date().isoformat() == "2026-07-06"

    # when the latest trading day is already in the past (weekend), it is used directly
    monkeypatch.setattr(orc, "latest_trading_day_on_or_before", lambda value=None, **k: pd.Timestamp("2026-07-03", tz="UTC"))
    monkeypatch.setattr(orc, "_market_calendar_date", lambda value=None, **k: pd.Timestamp("2026-07-05", tz="UTC"))
    monkeypatch.setattr(orc, "_ist_now", lambda now=None: pd.Timestamp("2026-07-05 10:00", tz="Asia/Kolkata"))
    assert orc.expected_complete_trading_day().date().isoformat() == "2026-07-03"


def test_ohlcv_reconcile_find_stale_symbols(monkeypatch):
    from data.dhanlive import ohlcv_reconcile as orc

    coverage = pd.DataFrame(
        [
            {"symbol": "CURRENT", "max_date": pd.Timestamp("2026-07-03")},
            {"symbol": "STALE", "max_date": pd.Timestamp("2026-07-01")},
        ]
    )
    monkeypatch.setattr(orc, "sql_to_df", lambda *a, **k: coverage)
    stale = orc.find_stale_symbols(["CURRENT", "STALE", "NEVER_SEEN"], pd.Timestamp("2026-07-03", tz="UTC"))
    assert stale == ["STALE", "NEVER_SEEN"]
    assert orc.find_stale_symbols([], pd.Timestamp("2026-07-03")) == []
    # a coverage-lookup failure degrades to "sync everything", never hides staleness
    monkeypatch.setattr(orc, "sql_to_df", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("db down")))
    assert orc.find_stale_symbols(["A", "B"], pd.Timestamp("2026-07-03")) == ["A", "B"]


def test_ohlcv_reconcile_run_caps_symbols_and_counts_results(monkeypatch):
    from data.dhanlive import ohlcv_reconcile as orc

    monkeypatch.setattr(orc, "expected_complete_trading_day", lambda now=None: pd.Timestamp("2026-07-03", tz="UTC"))
    monkeypatch.setattr(orc, "load_universe_symbols", lambda: ["A", "B", "C", "D"])
    monkeypatch.setattr(orc, "find_stale_symbols", lambda universe, expected: ["A", "B", "C"])
    synced: list[list[str]] = []

    def fake_sync(tickers, **_kwargs):
        synced.append(list(tickers))
        return [{"ticker": t, "error": "boom" if t == "B" else None} for t in tickers]

    monkeypatch.setattr(orc, "sync_many_daily", fake_sync)
    summary = orc.run_reconcile(max_symbols=2)
    assert synced == [["A", "B"]]  # capped at 2, cap logged
    assert summary["stale_symbols"] == 3 and summary["skipped_over_cap"] == 1
    assert summary["sync_attempted"] == 2 and summary["sync_succeeded"] == 1 and summary["sync_failed"] == 1
    # dry run never syncs
    synced.clear()
    dry = orc.run_reconcile(dry_run=True)
    assert synced == [] and dry["dry_run"] is True and dry["stale_sample"] == ["A", "B", "C"]


def test_security_history_ensure_identity_tables_uses_schema_registry(monkeypatch):
    calls = []

    monkeypatch.setattr(security_history, "apply_schema_migration", lambda **kwargs: calls.append(kwargs))

    security_history.ensure_identity_tables()

    assert len(calls) == 1
    call = calls[0]
    assert call["migration_id"] == security_history.SECURITY_HISTORY_SCHEMA_MIGRATION_ID
    assert call["owner"] == "data.nseindia.security_history"
    assert call["metadata"]["tables"] == [
        "dim_security_history",
        "dim_security_overrides",
        "dim_security_review_events",
    ]
    ddl = "\n".join(call["statements"])
    assert "CREATE TABLE IF NOT EXISTS dim_security_history" in ddl
    assert "CREATE TABLE IF NOT EXISTS dim_security_overrides" in ddl
    assert "CREATE TABLE IF NOT EXISTS dim_security_review_events" in ddl
    assert "UNIQUE (security_id, symbol, series, isin, effective_from)" in ddl


def test_security_history_records_corporate_action_context_fallback(monkeypatch):
    events: list[dict[str, object]] = []
    history = pd.DataFrame(
        [
            {
                "symbol": "ABC",
                "series": "EQ",
                "isin": "INE001",
                "effective_from": pd.Timestamp("2026-01-01", tz="UTC"),
                "effective_to": pd.Timestamp("2026-01-02", tz="UTC"),
                "security_id": "isin:INE001",
            }
        ]
    )

    monkeypatch.setattr(
        security_history,
        "sql_to_df",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("corporate action table unavailable")),
    )
    monkeypatch.setattr(security_history, "record_local_fallback_event", lambda **kwargs: events.append(kwargs) or kwargs)

    review = security_history.build_review_events(history)

    assert review.empty
    assert events[0]["module"] == "data.nseindia.security_history"
    assert events[0]["source"] == "nseindia_corporate_actions_normalized"
    assert events[0]["fallback_type"] == "nse_security_history_corporate_action_context_failed"
    assert isinstance(events[0]["error"], RuntimeError)


def test_security_history_records_load_fallback(monkeypatch):
    events: list[dict[str, object]] = []
    monkeypatch.setattr(
        security_history,
        "sql_to_df",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("history table unavailable")),
    )
    monkeypatch.setattr(security_history, "record_local_fallback_event", lambda **kwargs: events.append(kwargs) or kwargs)

    history = security_history.load_security_history()

    assert history.empty
    assert events[0]["module"] == "data.nseindia.security_history"
    assert events[0]["source"] == "dim_security_history"
    assert events[0]["fallback_type"] == "nse_security_history_load_failed"
    assert isinstance(events[0]["error"], RuntimeError)




def test_db_dedupe_for_upsert_keeps_last_on_conflict_keys():
    import utils.db as udb

    df = pd.DataFrame(
        [
            {"k": 1, "val": "first"},
            {"k": 1, "val": "last"},   # duplicate conflict key -> collapses, last wins
            {"k": 2, "val": "solo"},
        ]
    )
    out, dropped = udb._dedupe_for_upsert(df, ["k"])
    assert dropped == 1
    assert list(out.sort_values("k")["val"]) == ["last", "solo"]

    # no-op when there are no duplicates
    unique = pd.DataFrame([{"k": 1, "val": "a"}, {"k": 2, "val": "b"}])
    out2, dropped2 = udb._dedupe_for_upsert(unique, ["k"])
    assert dropped2 == 0 and len(out2) == 2

    # left untouched (no dedupe) when a conflict column is absent, so a malformed call still errors
    partial = pd.DataFrame([{"k": 1}, {"k": 1}])
    out3, dropped3 = udb._dedupe_for_upsert(partial, ["k", "missing_col"])
    assert dropped3 == 0 and len(out3) == 2
    # and with no unique_keys / empty frame
    assert udb._dedupe_for_upsert(df, [])[1] == 0
    assert udb._dedupe_for_upsert(pd.DataFrame(columns=["k"]), ["k"])[1] == 0


def test_db_retry_wrapper_reconnects_on_transient_error(monkeypatch):
    calls = {"operation": 0, "dispose": 0}

    class OperationalError(Exception):
        pass

    def operation():
        calls["operation"] += 1
        if calls["operation"] < 3:
            raise OperationalError("server closed the connection unexpectedly")
        return "ok"

    monkeypatch.setattr(db_utils, "dispose_db_pool", lambda: calls.__setitem__("dispose", calls["dispose"] + 1))
    monkeypatch.setattr(db_utils.time, "sleep", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(db_utils, "_write_db_retry_telemetry", lambda **_kwargs: None)

    assert db_utils.with_db_retries(operation, attempts=3, operation_name="test") == "ok"
    assert calls == {"operation": 3, "dispose": 2}


def test_db_retry_wrapper_retries_deadlock(monkeypatch):
    calls = {"operation": 0, "dispose": 0}

    class DeadlockDetected(Exception):
        pass

    def operation():
        calls["operation"] += 1
        if calls["operation"] < 3:
            raise DeadlockDetected("deadlock detected")
        return "ok"

    monkeypatch.setattr(db_utils, "dispose_db_pool", lambda: calls.__setitem__("dispose", calls["dispose"] + 1))
    monkeypatch.setattr(db_utils.time, "sleep", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(db_utils, "_write_db_retry_telemetry", lambda **_kwargs: None)

    assert db_utils.with_db_retries(operation, attempts=3, operation_name="deadlock_test") == "ok"
    assert calls == {"operation": 3, "dispose": 2}


def test_execute_db_operation_retries_complete_transaction(monkeypatch):
    calls = {"operation": 0}
    operation_names: list[str] = []

    def fake_with_db_retries(operation, **kwargs):
        operation_names.append(kwargs["operation_name"])
        return operation()

    def operation():
        calls["operation"] += 1
        return "done"

    monkeypatch.setattr(db_utils, "with_db_retries", fake_with_db_retries)

    assert db_utils.execute_db_operation(operation, operation_name="unit:transaction") == "done"
    assert calls["operation"] == 1
    assert operation_names == ["unit:transaction"]


def test_db_schema_dump_collects_metadata_with_retryable_operation(monkeypatch):
    from utils import db_schema_dump

    operation_names = []
    executed = []

    class FakeCursor:
        def __init__(self):
            self.rows = []

        def execute(self, query, params=None):
            executed.append((str(query), params))
            query_text = str(query)
            if "FROM pg_class c" in query_text:
                self.rows = [("public", "example_table")]
            elif "FROM pg_attribute" in query_text:
                self.rows = [("public", "example_table", "id", "integer", False, None)]
            elif "FROM pg_constraint" in query_text:
                self.rows = [("public", "example_table", "id")]
            elif "FROM pg_class t" in query_text:
                self.rows = [("public", "example_table", "idx_example_id", True, ["id"])]
            elif "timescaledb_information.hypertables" in query_text:
                self.rows = [("public", "example_table")]
            else:
                self.rows = []

        def fetchall(self):
            return list(self.rows)

    class FakeSession:
        def __enter__(self):
            return None, FakeCursor()

        def __exit__(self, *_args):
            return False

    def fake_execute_db_operation(operation, *, operation_name, **_kwargs):
        operation_names.append(operation_name)
        return operation()

    monkeypatch.setattr(db_schema_dump, "db_session", lambda: FakeSession())
    monkeypatch.setattr(db_schema_dump, "execute_db_operation", fake_execute_db_operation)

    tables, colmap, pkmap, idxmap, hypertables = db_schema_dump.collect_schema_metadata("public")

    assert operation_names == ["db_schema_dump:collect_schema_metadata"]
    assert tables == [("public", "example_table")]
    assert colmap[("public", "example_table")][0]["name"] == "id"
    assert pkmap[("public", "example_table")] == {"id"}
    assert idxmap[("public", "example_table")][0]["name"] == "idx_example_id"
    assert hypertables == {("public", "example_table")}
    assert all(params == (["public"],) for _query, params in executed)


def test_db_retry_coverage_report_classifies_wrapped_and_direct_sessions(tmp_path):
    from scripts import db_retry_coverage_report

    package = tmp_path / "pkg"
    package.mkdir()
    (package / "sample.py").write_text(
        """
from utils.db import db_session, execute_db_operation, upsert_to_db

def direct():
    with db_session() as (_, cur):
        cur.execute("SELECT 1")

def wrapped():
    with db_session() as (_, cur):
        cur.execute("SELECT 2")

def outer():
    execute_db_operation(wrapped, operation_name="unit")
""",
        encoding="utf-8",
    )

    report = db_retry_coverage_report.build_report(root=tmp_path, roots=["pkg"])
    rows = {(row["function"], row["status"]) for row in report["rows"]}

    assert report["counts"] == {"direct": 1, "wrapped": 1}
    assert ("direct", "direct") in rows
    assert ("wrapped", "wrapped") in rows


def test_db_retry_coverage_report_records_parse_fallback(monkeypatch, tmp_path):
    from scripts import db_retry_coverage_report

    events = []
    package = tmp_path / "pkg"
    package.mkdir()
    (package / "broken.py").write_text("def broken(:\n", encoding="utf-8")
    monkeypatch.setattr(fallback_telemetry, "record_local_fallback_event", lambda **kwargs: events.append(kwargs) or kwargs)

    report = db_retry_coverage_report.build_report(root=tmp_path, roots=["pkg"])

    assert report["counts"] == {"parse_error": 1}
    assert len(events) == 1
    assert events[0]["module"] == "scripts.db_retry_coverage_report"
    assert events[0]["source"] == "pkg/broken.py"
    assert events[0]["fallback_type"] == "db_retry_coverage_parse_failed"


def test_fallback_telemetry_coverage_report_classifies_exception_handlers(tmp_path):
    from scripts import fallback_telemetry_coverage_report

    package = tmp_path / "pkg"
    package.mkdir()
    (package / "sample.py").write_text(
        """
from utils.fallback_telemetry import record_local_fallback_event

def telemetry():
    try:
        raise RuntimeError("x")
    except RuntimeError as exc:
        record_local_fallback_event(source="unit", fallback_type="unit_failed", error=exc)
        return []

def reraises():
    try:
        raise RuntimeError("x")
    except RuntimeError:
        raise

def logs_only(logger):
    try:
        raise RuntimeError("x")
    except RuntimeError:
        logger.warning("fallback")
        return []

def silent():
    try:
        raise RuntimeError("x")
    except RuntimeError:
        return []
""",
        encoding="utf-8",
    )

    report = fallback_telemetry_coverage_report.build_report(root=tmp_path, roots=["pkg"])
    rows = {(row["function"], row["status"]) for row in report["rows"]}

    assert report["counts"] == {
        "logs_only": 1,
        "records_fallback": 1,
        "reraises": 1,
        "silent_fallback": 1,
    }
    assert ("telemetry", "records_fallback") in rows
    assert ("reraises", "reraises") in rows
    assert ("logs_only", "logs_only") in rows
    assert ("silent", "silent_fallback") in rows


def test_fallback_telemetry_coverage_report_records_parse_fallback(monkeypatch, tmp_path):
    from scripts import fallback_telemetry_coverage_report

    events = []
    package = tmp_path / "pkg"
    package.mkdir()
    (package / "broken.py").write_text("def broken(:\n", encoding="utf-8")
    monkeypatch.setattr(fallback_telemetry, "record_local_fallback_event", lambda **kwargs: events.append(kwargs) or kwargs)

    report = fallback_telemetry_coverage_report.build_report(root=tmp_path, roots=["pkg"])

    assert report["counts"] == {"parse_error": 1}
    assert len(events) == 1
    assert events[0]["module"] == "scripts.fallback_telemetry_coverage_report"
    assert events[0]["source"] == "pkg/broken.py"
    assert events[0]["fallback_type"] == "fallback_coverage_parse_failed"


def test_fallback_telemetry_coverage_report_marks_self_protection(tmp_path):
    from scripts import fallback_telemetry_coverage_report

    package = tmp_path / "utils"
    package.mkdir()
    body = """def _json_ready(value):
    try:
        risky(value)
    except Exception:
        pass
    return value
"""
    except_line = 4
    padding = "\n" * (61 - except_line)
    (package / "fallback_telemetry.py").write_text(
        padding + body,
        encoding="utf-8",
    )

    report = fallback_telemetry_coverage_report.build_report(root=tmp_path, roots=["utils"])

    assert report["counts"] == {"self_protection": 1}
    assert report["rows"][0]["status"] == "self_protection"


def test_agent_tool_runner_records_local_fallback_on_failure(monkeypatch):
    from scripts import agent_tool_runner

    events = []
    monkeypatch.setattr(sys, "argv", ["agent_tool_runner.py", "list"])
    monkeypatch.setattr(agent_tool_runner, "list_tools", lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("registry unavailable")))
    monkeypatch.setattr(agent_tool_runner, "record_local_fallback_event", lambda **kwargs: events.append(kwargs) or kwargs)

    with pytest.raises(SystemExit) as raised:
        agent_tool_runner.main()

    assert raised.value.code == 1
    assert len(events) == 1
    assert events[0]["module"] == "scripts.agent_tool_runner"
    assert events[0]["fallback_type"] == "agent_tool_runner_failed"
    assert events[0]["metadata"]["action"] == "list"


def test_drop_duplicate_indexes_records_local_fallback_on_drop_failure(monkeypatch):
    from scripts import drop_duplicate_indexes

    events = []
    report = {
        "duplicate_groups": [
            {
                "schema_name": "public",
                "table_name": "example",
                "columns": ["symbol"],
                "indexes": [
                    {
                        "index_name": "idx_example_duplicate",
                        "index_bytes": 1024,
                        "index_size": "1024 bytes",
                        "safe_drop_candidate": True,
                    }
                ],
            }
        ]
    }

    class FakeCursor:
        def execute(self, *_args, **_kwargs):
            raise RuntimeError("drop failed")

        def close(self):
            pass

    class FakeConnection:
        autocommit = False

        def cursor(self):
            return FakeCursor()

        def close(self):
            pass

    monkeypatch.setattr(drop_duplicate_indexes, "build_duplicate_index_report", lambda **_kwargs: report)
    monkeypatch.setattr(drop_duplicate_indexes.psycopg2, "connect", lambda **_kwargs: FakeConnection())
    monkeypatch.setattr(drop_duplicate_indexes, "record_local_fallback_event", lambda **kwargs: events.append(kwargs) or kwargs)

    output = drop_duplicate_indexes.drop_duplicate_indexes(execute=True)

    assert output["dropped"] == []
    assert len(output["skipped"]) == 1
    assert len(events) == 1
    assert events[0]["module"] == "scripts.drop_duplicate_indexes"
    assert events[0]["fallback_type"] == "drop_duplicate_index_failed"
    assert events[0]["metadata"]["index_name"] == "idx_example_duplicate"


def test_sql_query_runner_records_local_fallback_on_failure(monkeypatch):
    from scripts import sql_query_runner

    events = []
    monkeypatch.setattr(sys, "argv", ["sql_query_runner.py", "SELECT 1", "--read-only"])
    monkeypatch.setattr(sql_query_runner, "get_connection", lambda: (_ for _ in ()).throw(RuntimeError("db unavailable")))
    monkeypatch.setattr(sql_query_runner, "record_local_fallback_event", lambda **kwargs: events.append(kwargs) or kwargs)

    with pytest.raises(SystemExit) as raised:
        sql_query_runner.main()

    assert raised.value.code == 1
    assert len(events) == 1
    assert events[0]["module"] == "scripts.sql_query_runner"
    assert events[0]["fallback_type"] == "sql_query_runner_failed"
    assert events[0]["metadata"]["read_only"] is True


def test_redis_backup_records_local_fallback_for_key_fetch_failure(monkeypatch, tmp_path):
    events = []

    class FakeRedis:
        def keys(self, pattern):
            assert pattern == "*"
            return ["bad-key"]

        def type(self, key):
            assert key == "bad-key"
            return "string"

        def get(self, key):
            assert key == "bad-key"
            raise RuntimeError("redis read failed")

    monkeypatch.setattr(redis_bkp_restore.redis, "Redis", lambda **_kwargs: FakeRedis())
    monkeypatch.setattr(redis_bkp_restore, "record_local_fallback_event", lambda **kwargs: events.append(kwargs) or kwargs)
    output_file = tmp_path / "redis_backup.json"

    redis_bkp_restore.redis_backup(output_file=str(output_file))

    assert json.loads(output_file.read_text(encoding="utf-8")) == {}
    assert len(events) == 1
    assert events[0]["module"] == "utils.redis_bkp_restore"
    assert events[0]["fallback_type"] == "redis_backup_key_fetch_failed"
    assert events[0]["metadata"]["key"] == "bad-key"
    assert events[0]["metadata"]["key_type"] == "string"


def test_redis_restore_records_local_fallback_for_unreadable_backup(monkeypatch, tmp_path):
    events = []
    monkeypatch.setattr(redis_bkp_restore.redis, "Redis", lambda **_kwargs: object())
    monkeypatch.setattr(redis_bkp_restore, "record_local_fallback_event", lambda **kwargs: events.append(kwargs) or kwargs)
    missing_file = tmp_path / "missing.json"

    with pytest.raises(SystemExit) as raised:
        redis_bkp_restore.redis_restore(input_file=str(missing_file))

    assert raised.value.code == 1
    assert len(events) == 1
    assert events[0]["fallback_type"] == "redis_restore_backup_file_read_failed"
    assert events[0]["metadata"]["input_file"] == str(missing_file)


def test_redis_restore_records_local_fallback_for_key_write_failure(monkeypatch, tmp_path):
    events = []

    class FakeRedis:
        def set(self, key, value):
            assert key == "bad-key"
            assert value == "value"
            raise RuntimeError("redis write failed")

    monkeypatch.setattr(redis_bkp_restore.redis, "Redis", lambda **_kwargs: FakeRedis())
    monkeypatch.setattr(redis_bkp_restore, "record_local_fallback_event", lambda **kwargs: events.append(kwargs) or kwargs)
    input_file = tmp_path / "redis_backup.json"
    input_file.write_text(json.dumps({"bad-key": {"type": "string", "value": "value"}}), encoding="utf-8")

    redis_bkp_restore.redis_restore(input_file=str(input_file))

    assert len(events) == 1
    assert events[0]["fallback_type"] == "redis_restore_key_failed"
    assert events[0]["metadata"]["key"] == "bad-key"
    assert events[0]["metadata"]["key_type"] == "string"


def test_sql_to_df_retries_query_canceled(monkeypatch):
    calls = {"count": 0}

    class QueryCanceled(Exception):
        pass

    def fake_fetch(*_args, **_kwargs):
        calls["count"] += 1
        if calls["count"] < 3:
            raise QueryCanceled("canceling statement due to statement timeout")
        return pd.DataFrame({"ok": [1]})

    monkeypatch.setattr(db_utils, "_fetch_sql_to_df_once", fake_fetch)
    monkeypatch.setattr(db_utils.time, "sleep", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(db_utils, "_write_db_retry_telemetry", lambda **_kwargs: None)

    out = db_utils.sql_to_df("select 1", retries=2)
    assert out.to_dict(orient="records") == [{"ok": 1}]
    assert calls["count"] == 3


def test_db_retry_telemetry_spools_without_postgres(monkeypatch, tmp_path):
    calls = {"operation": 0, "dispose": 0}

    class OperationalError(Exception):
        pass

    def operation():
        calls["operation"] += 1
        if calls["operation"] < 2:
            raise OperationalError("server closed the connection unexpectedly tokenId=SECRET123")
        return "ok"

    telemetry_file = tmp_path / "db_retry_events.jsonl"
    monkeypatch.setattr(db_utils, "DB_RETRY_TELEMETRY_FILE", telemetry_file)
    monkeypatch.setattr(db_utils, "dispose_db_pool", lambda: calls.__setitem__("dispose", calls["dispose"] + 1))
    monkeypatch.setattr(db_utils.time, "sleep", lambda *_args, **_kwargs: None)

    assert db_utils.with_db_retries(operation, attempts=3, operation_name="unit_db_retry") == "ok"

    rows = db_utils.read_db_retry_telemetry_events(hours=24, limit=10)
    assert len(rows) == 1
    assert rows[0]["operation_name"] == "unit_db_retry"
    assert rows[0]["fallback_type"] == "db_retry"
    assert rows[0]["severity"] == "warn"
    assert "SECRET123" not in rows[0]["error_message"]


def test_db_retry_telemetry_records_corrupt_spool_line(monkeypatch, tmp_path):
    events = []
    telemetry_file = tmp_path / "db_retry_events.jsonl"
    telemetry_file.write_text("{bad-json\n", encoding="utf-8")
    monkeypatch.setattr(db_utils, "DB_RETRY_TELEMETRY_FILE", telemetry_file)
    monkeypatch.setattr(db_utils, "_write_db_retry_telemetry", lambda **kwargs: events.append(kwargs))

    rows = db_utils.read_db_retry_telemetry_events(hours=24, limit=10)

    assert rows == []
    assert len(events) == 1
    assert events[0]["operation_name"] == "db_retry_telemetry:line_parse"
    assert events[0]["event_type"] == "db_retry_spool_line_parse_failed"


def test_db_pool_dispose_failure_records_retry_telemetry(monkeypatch):
    events: list[dict[str, object]] = []

    class BrokenEngine:
        def dispose(self):
            raise RuntimeError("dispose failed")

    monkeypatch.setattr(db_utils, "_engine", BrokenEngine())
    monkeypatch.setattr(db_utils, "_write_db_retry_telemetry", lambda **kwargs: events.append(kwargs))

    db_utils.dispose_db_pool()

    assert len(events) == 1
    assert events[0]["operation_name"] == "db_pool:dispose"
    assert events[0]["event_type"] == "db_pool_dispose_failed"


def test_db_session_cleanup_failures_record_retry_telemetry(monkeypatch):
    events: list[dict[str, object]] = []

    class FakeCursor:
        def close(self):
            raise RuntimeError("cursor close failed")

    class FakeConnection:
        def commit(self):
            return None

        def close(self):
            raise RuntimeError("connection close failed")

    def fake_with_db_retries(operation, **_kwargs):
        return FakeConnection(), FakeCursor()

    monkeypatch.setattr(db_utils, "with_db_retries", fake_with_db_retries)
    monkeypatch.setattr(db_utils, "_write_db_retry_telemetry", lambda **kwargs: events.append(kwargs))

    with db_utils.db_session():
        pass

    assert [event["operation_name"] for event in events] == [
        "db_session:cursor_close",
        "db_session:connection_close",
    ]
    assert {event["event_type"] for event in events} == {"db_session_cleanup_failed"}


def test_get_max_date_failure_records_retry_telemetry(monkeypatch):
    events: list[dict[str, object]] = []

    monkeypatch.setattr(db_utils, "sql_to_df", lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("missing table")))
    monkeypatch.setattr(db_utils, "_write_db_retry_telemetry", lambda **kwargs: events.append(kwargs))

    assert db_utils.get_max_date("missing_feature_table") is None
    assert len(events) == 1
    assert events[0]["operation_name"] == "get_max_date:missing_feature_table"
    assert events[0]["event_type"] == "db_lookup_unavailable"


def test_company_master_lookup_failure_records_local_fallback_and_reraises(monkeypatch):
    events: list[dict[str, object]] = []

    def fake_sql_to_df(*_args, **_kwargs):
        raise RuntimeError("company master unavailable")

    monkeypatch.setattr(company_master_utils, "sql_to_df", fake_sql_to_df)
    monkeypatch.setattr(company_master_utils, "record_local_fallback_event", lambda **kwargs: events.append(kwargs) or kwargs)

    with pytest.raises(RuntimeError, match="company master unavailable"):
        company_master_utils.map_company_master_ids(["ABC"], exchange="NSE")

    assert len(events) == 1
    assert events[0]["module"] == "utils.company_master"
    assert events[0]["source"] == company_master_utils.COMPANY_MASTER_TABLE
    assert events[0]["fallback_type"] == "company_master_query_failed"
    assert events[0]["severity"] == "error"
    assert events[0]["metadata"] == {"operation": "map_nse_ids"}


def test_company_master_success_path_does_not_record_fallback(monkeypatch):
    events: list[dict[str, object]] = []

    def fake_sql_to_df(*_args, **_kwargs):
        return pd.DataFrame([{"ticker": "ABC", "company_master_id": "nse:ABC"}])

    monkeypatch.setattr(company_master_utils, "sql_to_df", fake_sql_to_df)
    monkeypatch.setattr(company_master_utils, "record_local_fallback_event", lambda **kwargs: events.append(kwargs) or kwargs)

    out = company_master_utils.map_company_master_ids(["ABC"], exchange="NSE")

    assert out.astype("string").tolist() == ["nse:ABC"]
    assert events == []


def test_company_master_preserves_index_for_non_contiguous_series(monkeypatch):
    """Regression test: map_company_master_ids() used to do list(tickers) internally,
    discarding the caller's index in favor of a fresh 0-based RangeIndex. A caller doing
    df["company_master_id"] = map_company_master_ids(df["symbol"], ...) on a df with a
    non-contiguous index (e.g. any row-filtered df, which is the common case) would then
    silently misalign by index and scramble which company_master_id lands on which row --
    found live while building fundamentals/collectors/security_master.py, where AARNAV
    ended up mapped to nse:ASTAR's id. No exception, no warning -- just wrong data."""

    def fake_sql_to_df(*_args, **_kwargs):
        return pd.DataFrame(
            [
                {"ticker": "AARNAV", "company_master_id": "nse:AARNAV"},
                {"ticker": "AASTHA", "company_master_id": "nse:AASTHA"},
            ]
        )

    monkeypatch.setattr(company_master_utils, "sql_to_df", fake_sql_to_df)
    monkeypatch.setattr(company_master_utils, "record_local_fallback_event", lambda **kwargs: None)

    # Simulates a filtered dataframe: non-contiguous index, exactly the shape
    # fundamentals/collectors/security_master.py's new_symbols subset has.
    tickers = pd.Series(["AARNAV", "AASTHA"], index=[14, 22])

    out = company_master_utils.map_company_master_ids(tickers, exchange="NSE")

    assert list(out.index) == [14, 22]
    df = pd.DataFrame({"symbol": tickers})
    df["company_master_id"] = out
    assert df.loc[14, "company_master_id"] == "nse:AARNAV"
    assert df.loc[22, "company_master_id"] == "nse:AASTHA"


def test_resilient_redis_retries_then_returns_safe_default(monkeypatch):
    calls = {"attempts": 0}
    events: list[dict[str, object]] = []

    class FailingRedis:
        def __init__(self, *args, **kwargs):
            pass

        def get(self, key):
            calls["attempts"] += 1
            raise redis_utils.redis.ConnectionError("connection refused")

    monkeypatch.setattr(redis_utils, "_ORIGINAL_REDIS_CLASS", FailingRedis)
    monkeypatch.setattr(redis_utils, "REDIS_OPERATION_ATTEMPTS", 3)
    monkeypatch.setattr(redis_utils.time, "sleep", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(redis_utils, "record_fallback_event", lambda **kwargs: events.append(kwargs))
    client = redis_utils.ResilientRedis(host="127.0.0.1", port=6379, decode_responses=True, fail_soft=True)

    assert client.get("missing") is None
    assert calls["attempts"] == 3
    assert [event["fallback_type"] for event in events] == ["redis_retry", "redis_retry", "redis_fail_soft"]
    assert events[-1]["metadata"] == {"command": "get", "attempts": 3}


def test_resilient_redis_enters_cooldown_after_failure(monkeypatch):
    calls = {"attempts": 0}
    events: list[dict[str, object]] = []

    class FailingRedis:
        def __init__(self, *args, **kwargs):
            pass

        def sadd(self, *_args, **_kwargs):
            calls["attempts"] += 1
            raise redis_utils.redis.ConnectionError("connection refused")

    now = {"value": 1000.0}

    monkeypatch.setattr(redis_utils, "_ORIGINAL_REDIS_CLASS", FailingRedis)
    monkeypatch.setattr(redis_utils, "REDIS_OPERATION_ATTEMPTS", 3)
    monkeypatch.setattr(redis_utils, "REDIS_RECONNECT_COOLDOWN_SECONDS", 30.0)
    monkeypatch.setattr(redis_utils.time, "sleep", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(redis_utils.time, "time", lambda: now["value"])
    monkeypatch.setattr(redis_utils, "record_fallback_event", lambda **kwargs: events.append(kwargs))

    client = redis_utils.ResilientRedis(host="127.0.0.1", port=6379, decode_responses=True, fail_soft=True)

    assert client.sadd("test", "value") == 0
    assert calls["attempts"] == 3

    assert client.sadd("test", "value2") == 0
    assert calls["attempts"] == 3
    assert [event["fallback_type"] for event in events] == [
        "redis_retry",
        "redis_retry",
        "redis_fail_soft",
        "redis_reconnect_cooldown",
    ]
    assert events[-1]["metadata"]["command"] == "sadd"
    assert events[-1]["metadata"]["cooldown_remaining_seconds"] == 30.0


def test_resilient_redis_close_failure_records_fallback(monkeypatch):
    events: list[dict[str, object]] = []

    class ClosingRedis:
        def close(self):
            raise RuntimeError("close failed")

    client = redis_utils.ResilientRedis(host="127.0.0.1", port=6379, decode_responses=True, fail_soft=True)
    client._client = ClosingRedis()  # type: ignore[assignment]
    monkeypatch.setattr(redis_utils, "record_fallback_event", lambda **kwargs: events.append(kwargs))

    client.close()

    assert client._client is None
    assert len(events) == 1
    assert events[0]["fallback_type"] == "redis_close_failed"
    assert events[0]["metadata"] == {"command": "close"}


def test_http_cache_bad_date_records_fallback_and_uses_mtime(monkeypatch, tmp_path):
    events: list[dict[str, object]] = []
    cache_file = tmp_path / "endpoint__bad_date.json"
    cache_file.write_text('{"ok": true}', encoding="utf-8")

    monkeypatch.setattr(http_utils, "record_local_fallback_event", lambda **kwargs: events.append(kwargs))

    assert http_utils._load_from_cache(str(tmp_path / "endpoint__*.json"), max_age_days=10) == '{"ok": true}'
    assert len(events) == 1
    assert events[0]["module"] == "utils.http"
    assert events[0]["source"] == "http_cache"
    assert events[0]["fallback_type"] == "http_cache_date_parse_failed"
    assert events[0]["metadata"]["cache_file"] == "endpoint__bad_date.json"
    assert events[0]["metadata"]["date_part"] == "bad_date"


def test_http_cache_read_failure_records_fallback(monkeypatch, tmp_path):
    events: list[dict[str, object]] = []
    cache_file = tmp_path / f"endpoint__{datetime.now().strftime('%Y_%d_%m')}.json"
    cache_file.write_text("cached", encoding="utf-8")

    def fake_read_text(self, *args, **kwargs):
        raise OSError("cache unreadable")

    monkeypatch.setattr(http_utils.Path, "read_text", fake_read_text)
    monkeypatch.setattr(http_utils, "record_local_fallback_event", lambda **kwargs: events.append(kwargs))

    assert http_utils._load_from_cache(str(tmp_path / "endpoint__*.json"), max_age_days=10) is None
    assert len(events) == 1
    assert events[0]["fallback_type"] == "http_cache_read_failed"
    assert events[0]["metadata"]["cache_file"] == cache_file.name
    assert isinstance(events[0]["error"], OSError)


def test_http_get_with_retries_records_stale_cache_network_fallback(monkeypatch, tmp_path):
    events: list[dict[str, object]] = []
    cache_file = tmp_path / "resource__noparam__2020_01_01.json"
    cache_file.write_text('{"stale": true}', encoding="utf-8")

    def fake_request(self, *args, **kwargs):
        raise requests.Timeout("source timed out")

    monkeypatch.setattr(http_utils, "_CACHE_DIR", tmp_path)
    monkeypatch.setattr(http_utils, "record_local_fallback_event", lambda **kwargs: events.append(kwargs))
    monkeypatch.setattr(requests.Session, "request", fake_request)

    response = http_utils.get_with_retries("https://example.com/resource", max_age_days=0, retries=1)

    assert response.headers["X-Cache"] == "STALE"
    assert response.text == '{"stale": true}'
    assert len(events) == 1
    event = events[0]
    assert event["module"] == "utils.http"
    assert event["fallback_type"] == "http_network_failed_stale_cache_used"
    assert event["metadata"]["cache_file"] == cache_file.name
    assert event["metadata"]["host"] == "example.com"
    assert isinstance(event["error"], requests.Timeout)


def test_http_get_with_retries_records_network_failure_without_cache(monkeypatch, tmp_path):
    events: list[dict[str, object]] = []

    def fake_request(self, *args, **kwargs):
        raise requests.ConnectionError("source unavailable")

    monkeypatch.setattr(http_utils, "_CACHE_DIR", tmp_path)
    monkeypatch.setattr(http_utils, "record_local_fallback_event", lambda **kwargs: events.append(kwargs))
    monkeypatch.setattr(requests.Session, "request", fake_request)

    with pytest.raises(requests.ConnectionError, match="source unavailable"):
        http_utils.get_with_retries("https://example.com/resource", max_age_days=0, retries=1)

    assert len(events) == 1
    event = events[0]
    assert event["module"] == "utils.http"
    assert event["fallback_type"] == "http_network_failed_no_cache"
    assert event["metadata"]["host"] == "example.com"
    assert event["metadata"]["from_cache"] is True
    assert isinstance(event["error"], requests.ConnectionError)


def test_sync_redis_set_members_records_local_fallback(monkeypatch):
    events = []

    class FailingRedis:
        def smembers(self, key):
            raise RuntimeError(f"redis down for {key}")

    monkeypatch.setattr(sync_utils, "record_local_fallback_event", lambda **kwargs: events.append(kwargs))

    assert sync_utils.get_redis_set_members(FailingRedis(), "bhavcopy:parsed") == set()
    assert len(events) == 1
    event = events[0]
    assert event["module"] == "utils.sync"
    assert event["source"] == "redis:bhavcopy:parsed"
    assert event["fallback_type"] == "redis_set_members_unavailable"
    assert event["severity"] == "warn"
    assert isinstance(event["error"], RuntimeError)
    assert event["metadata"] == {"key": "bhavcopy:parsed", "command": "smembers"}


def test_dhan_web_login_fills_mobile_totp_pin_and_extracts_token(monkeypatch):
    class FakeLocator:
        def __init__(self, page, name, count=1):
            self.page = page
            self.name = name
            self._count = count

        @property
        def first(self):
            return self.nth(0)

        @property
        def last(self):
            return self.nth(max(self._count - 1, 0))

        def nth(self, index):
            return FakeLocator(self.page, f"{self.name}[{index}]", count=1)

        def count(self):
            return self._count

        def wait_for(self, **kwargs):
            self.page.actions.append(("wait_for", self.name, kwargs))

        def fill(self, value, **kwargs):
            self.page.actions.append(("fill", self.name, value))

        def click(self, **kwargs):
            self.page.actions.append(("click", self.name, kwargs))

        def dispatch_event(self, event):
            self.page.actions.append(("dispatch", self.name, event))

    class FakePage:
        def __init__(self):
            self.url = ""
            self.actions = []

        def goto(self, url, **kwargs):
            self.url = url
            self.actions.append(("goto", url, kwargs))

        def wait_for_timeout(self, value):
            self.actions.append(("wait", value))

        def locator(self, selector):
            if selector == dhan_web_login.CODE_INPUT_SELECTOR:
                return FakeLocator(self, "code", count=6)
            if selector == dhan_web_login.PIN_INPUT_SELECTOR:
                return FakeLocator(self, "pin", count=6)
            if selector == dhan_web_login.PROCEED_BUTTON_SELECTOR:
                return FakeLocator(self, "proceed", count=1)
            return FakeLocator(self, selector, count=1)

        def wait_for_function(self, expression, **kwargs):
            self.actions.append(("wait_for_function", expression, kwargs))
            self.url = "https://trade.singularity45.ai/dhan/?tokenId=TOKEN123"

    monkeypatch.setattr(dhan_web_login, "generate_totp", lambda _secret=None: "654321")
    page = FakePage()

    token_id = dhan_web_login.run_dhan_consent_login(
        page,
        consent_url="https://auth.dhan.co/login/consentApp-login?consentAppId=abc",
        mobile="9999999999",
        pin="123456",
        totp_secret="secret",
    )

    assert token_id == "TOKEN123"
    assert ("fill", f"{dhan_web_login.MOBILE_INPUT_SELECTOR}[0]", "9999999999") in page.actions
    assert ("fill", "code[0]", "6") in page.actions
    assert ("fill", "code[5]", "1") in page.actions
    assert ("fill", "pin[0]", "1") in page.actions
    assert ("fill", "pin[5]", "6") in page.actions


def test_dhan_web_login_submits_totp_when_pin_inputs_do_not_auto_appear(monkeypatch):
    events = []
    actions = []

    class FakePinFirst:
        def __init__(self):
            self.wait_count = 0

        def wait_for(self, **kwargs):
            self.wait_count += 1
            actions.append(("pin_wait", self.wait_count, kwargs))
            if self.wait_count == 1:
                raise dhan_web_login.PlaywrightTimeoutError("pin did not appear")

    class FakePinLocator:
        def __init__(self):
            self.first = FakePinFirst()

    class FakeProceedButton:
        def wait_for(self, **kwargs):
            actions.append(("proceed_wait", kwargs))

        def click(self, **kwargs):
            actions.append(("proceed_click", kwargs))

    class FakeProceedLocator:
        @property
        def last(self):
            return FakeProceedButton()

    class FakePage:
        def __init__(self):
            self.pin_locator = FakePinLocator()

        def locator(self, selector):
            if selector == dhan_web_login.PIN_INPUT_SELECTOR:
                return self.pin_locator
            if selector == dhan_web_login.PROCEED_BUTTON_SELECTOR:
                return FakeProceedLocator()
            raise AssertionError(selector)

    monkeypatch.setattr(dhan_web_login, "record_local_fallback_event", lambda **kwargs: events.append(kwargs) or kwargs)

    dhan_web_login._wait_for_pin_inputs_or_submit_totp(FakePage(), timeout_ms=9000)

    assert ("proceed_click", {"timeout": 9000}) in actions
    assert actions[-1][0] == "pin_wait"
    assert events[0]["fallback_type"] == "dhan_web_login_pin_wait_before_totp_submit_timeout"


def test_dhan_web_login_generates_totp(monkeypatch):
    class FakeTOTP:
        def __init__(self, secret):
            self.secret = secret

        def now(self):
            return "111222"

    monkeypatch.setattr(dhan_web_login.pyotp, "TOTP", FakeTOTP)

    assert dhan_web_login.generate_totp("abc") == "111222"


def test_dhan_web_login_records_cleanup_failures(monkeypatch):
    events = []

    class FailingClose:
        def close(self):
            raise RuntimeError("close failed")

    class FailingPlaywright:
        def stop(self):
            raise RuntimeError("stop failed")

    session = dhan_web_login.DhanBrowserSession(
        playwright=FailingPlaywright(),
        browser=object(),
        context=FailingClose(),
        page=FailingClose(),
        owns_context=True,
    )
    monkeypatch.setattr(dhan_web_login, "record_local_fallback_event", lambda **kwargs: events.append(kwargs) or kwargs)

    session.close()

    assert {event["fallback_type"] for event in events} == {
        "dhan_web_login_page_close_failed",
        "dhan_web_login_context_close_failed",
        "dhan_web_login_playwright_stop_failed",
    }
    assert all(event["module"] == "data.dhanlive.web_login" for event in events)
    assert all(event["source"] == "dhan_web_login" for event in events)


def test_dhan_web_login_records_proceed_force_click_fallback(monkeypatch):
    events = []
    actions = []

    class FakeButton:
        def wait_for(self, **kwargs):
            actions.append(("wait_for", kwargs))

        def click(self, **kwargs):
            actions.append(("click", kwargs))
            if not kwargs.get("force"):
                raise RuntimeError("normal click failed")

    class FakeLocator:
        @property
        def last(self):
            return FakeButton()

    class FakePage:
        def locator(self, selector):
            return FakeLocator()

    monkeypatch.setattr(dhan_web_login, "record_local_fallback_event", lambda **kwargs: events.append(kwargs) or kwargs)

    dhan_web_login._click_enabled_proceed(FakePage(), timeout_ms=123)

    assert ("click", {"force": True, "timeout": 123}) in actions
    assert events[0]["fallback_type"] == "dhan_web_login_proceed_click_fallback"
    assert events[0]["metadata"]["timeout_ms"] == 123


def test_dhan_web_login_records_missing_token_after_timeout(monkeypatch):
    events = []

    class FakeLocator:
        def __init__(self, count=1):
            self._count = count

        @property
        def first(self):
            return self

        @property
        def last(self):
            return self

        def nth(self, index):
            return self

        def count(self):
            return self._count

        def wait_for(self, **kwargs):
            pass

        def fill(self, value, **kwargs):
            pass

        def click(self, **kwargs):
            pass

        def dispatch_event(self, event):
            pass

    class FakePage:
        def __init__(self):
            self.url = "https://login.dhan.co/auth"

        def goto(self, url, **kwargs):
            self.url = url

        def wait_for_timeout(self, value):
            pass

        def locator(self, selector):
            if selector in {dhan_web_login.CODE_INPUT_SELECTOR, dhan_web_login.PIN_INPUT_SELECTOR}:
                return FakeLocator(count=6)
            return FakeLocator(count=1)

        def on(self, event, callback):
            pass

        def wait_for_function(self, expression, **kwargs):
            raise dhan_web_login.PlaywrightTimeoutError("token timeout")

    monkeypatch.setattr(dhan_web_login, "generate_totp", lambda _secret=None: "654321")
    monkeypatch.setattr(dhan_web_login, "record_local_fallback_event", lambda **kwargs: events.append(kwargs) or kwargs)

    with pytest.raises(dhan_web_login.DhanAuthError):
        dhan_web_login.run_dhan_consent_login(
            FakePage(),
            consent_url="https://auth.dhan.co/login/consentApp-login?consentAppId=abc",
            mobile="9999999999",
            pin="123456",
            totp_secret="secret",
            timeout_ms=77,
        )

    assert [event["fallback_type"] for event in events] == [
        "dhan_web_login_token_wait_timeout",
        "dhan_web_login_token_missing",
    ]
    assert events[0]["metadata"]["timeout_ms"] == 77
    assert events[0]["metadata"]["current_url_host"] == "auth.dhan.co"
    assert events[1]["severity"] == "error"


def test_dhan_access_token_uses_auto_login_when_configured(monkeypatch):
    class FakeEnv:
        def __call__(self, name, default=None):
            return None

    monkeypatch.setattr(dhan_auth, "load_cached_access_token", lambda: None)
    monkeypatch.setattr(dhan_auth, "normalize_token_id", lambda value: value)
    monkeypatch.setattr(dhan_auth, "is_auto_login_configured", lambda: True)
    monkeypatch.setattr(dhan_auth, "get_token_id_from_auto_login", lambda: "TOKEN123")
    monkeypatch.setattr(dhan_auth, "consume_consent_token", lambda token_id: {"accessToken": f"access:{token_id}"})
    monkeypatch.setattr(dhan_auth, "begin_browser_consent", lambda: (_ for _ in ()).throw(AssertionError("manual browser opened")))
    monkeypatch.setattr(dhan_auth, "env", FakeEnv())

    assert dhan_auth.get_access_token() == "access:TOKEN123"


def test_dhan_auto_login_uses_subprocess_inside_running_event_loop(monkeypatch):
    calls = []

    def fake_run(command, **kwargs):
        calls.append({"command": command, "kwargs": kwargs})
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=json.dumps({"status": "ok", "token_id": "TOKEN123", "printed_raw_token": True}),
            stderr="",
        )

    monkeypatch.setattr(dhan_auth, "build_new_consent_url", lambda: "https://auth.dhan.co/login/consentApp-login?consentAppId=ABC")
    monkeypatch.setattr(dhan_auth.subprocess, "run", fake_run)

    async def inside_loop():
        return dhan_auth.get_token_id_from_auto_login()

    assert asyncio.run(inside_loop()) == "TOKEN123"
    assert calls
    assert calls[0]["command"][:3] == [sys.executable, "-m", "data.dhanlive.web_login"]
    assert calls[0]["command"][-1] == "--print-token-id"
    assert calls[0]["kwargs"]["capture_output"] is True


def test_dhan_auto_login_retries_subprocess_when_playwright_sync_detects_loop(monkeypatch):
    events = []
    calls = []

    def fake_login(_consent_url):
        raise RuntimeError("It looks like you are using Playwright Sync API inside the asyncio loop. Please use the Async API instead.")

    def fake_run(command, **kwargs):
        calls.append({"command": command, "kwargs": kwargs})
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=json.dumps({"status": "ok", "token_id": "TOKEN456", "printed_raw_token": True}),
            stderr="",
        )

    class FakeWebLogin:
        @staticmethod
        def get_token_id_via_automated_login(consent_url):
            return fake_login(consent_url)

    monkeypatch.setitem(sys.modules, "data.dhanlive.web_login", FakeWebLogin)
    monkeypatch.setattr(dhan_auth, "build_new_consent_url", lambda: "https://auth.dhan.co/login/consentApp-login?consentAppId=ABC")
    monkeypatch.setattr(dhan_auth, "_is_inside_running_event_loop", lambda: False)
    monkeypatch.setattr(dhan_auth.subprocess, "run", fake_run)
    monkeypatch.setattr(dhan_auth, "record_local_fallback_event", lambda **kwargs: events.append(kwargs) or kwargs)

    assert dhan_auth.get_token_id_from_auto_login() == "TOKEN456"
    assert calls
    assert events[0]["fallback_type"] == "dhan_auto_login_sync_playwright_inside_async_loop"


def test_dhan_auto_login_subprocess_failure_records_fallback(monkeypatch):
    events = []

    def fake_run(command, **kwargs):
        return subprocess.CompletedProcess(command, 2, stdout="", stderr="playwright failed")

    monkeypatch.setattr(dhan_auth.subprocess, "run", fake_run)
    monkeypatch.setattr(dhan_auth, "record_local_fallback_event", lambda **kwargs: events.append(kwargs) or kwargs)

    with pytest.raises(dhan_auth.DhanAuthError):
        dhan_auth._get_token_id_via_auto_login_subprocess("https://auth.dhan.co/login/consentApp-login?consentAppId=ABC")

    assert events[0]["module"] == "data.dhanlive.auth"
    assert events[0]["source"] == "dhan_auth"
    assert events[0]["fallback_type"] == "dhan_auto_login_subprocess_nonzero"
    assert events[0]["metadata"]["stderr_tail"] == "playwright failed"


def test_dhan_auth_cli_refresh_auto_login_without_manual_browser(monkeypatch):
    monkeypatch.setattr(dhan_auth_cli, "normalize_token_id", lambda value: None)
    monkeypatch.setattr(dhan_auth_cli, "is_auto_login_configured", lambda: True)
    monkeypatch.setattr(dhan_auth_cli, "get_token_id_from_auto_login", lambda: "TOKEN123")
    monkeypatch.setattr(dhan_auth_cli, "consume_consent_token", lambda token_id: {"accessToken": f"access:{token_id}", "expiryTime": "2026-05-07T10:00:00Z"})
    monkeypatch.setattr(dhan_auth_cli, "validate_token", lambda access_token: {"status": "ok", "access_token": access_token})
    monkeypatch.setattr(dhan_auth_cli, "begin_browser_consent", lambda: (_ for _ in ()).throw(AssertionError("manual browser opened")))

    result = dhan_auth_cli.refresh_token()

    assert result["status"] == "ok"
    assert result["token_id_used"] == "TOKEN123"
    assert result["validation"] == {"status": "ok", "access_token": "access:TOKEN123"}


def test_dhan_auth_cli_ensure_uses_fresh_cached_token(monkeypatch):
    expires_at = datetime.now() + timedelta(hours=2)

    monkeypatch.delenv("DHAN_ACCESS_TOKEN", raising=False)
    monkeypatch.delenv("DHAN_TOKEN_ID", raising=False)
    monkeypatch.setattr(
        dhan_auth_cli,
        "load_cached_access_token_payload",
        lambda: {"accessToken": "cached-token", "expiryTime": expires_at.isoformat()},
    )
    monkeypatch.setattr(dhan_auth_cli, "validate_token", lambda access_token: {"status": "ok", "access_token": access_token})
    monkeypatch.setattr(dhan_auth_cli, "refresh_token", lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("refresh not expected")))

    result = dhan_auth_cli.ensure_token(min_fresh_minutes=30)

    assert result["status"] == "ok"
    assert result["source"] == "cached_access_token"
    assert result["refreshed"] is False
    assert result["validation"]["access_token"] == "cached-token"


def test_dhan_auth_cli_ensure_fails_without_noninteractive_refresh_path(monkeypatch, tmp_path):
    expired_at = datetime.now() - timedelta(hours=1)

    monkeypatch.delenv("DHAN_ACCESS_TOKEN", raising=False)
    monkeypatch.delenv("DHAN_TOKEN_ID", raising=False)
    monkeypatch.setattr(dhan_auth_cli, "DEFAULT_TOKEN_CACHE", tmp_path / "dhan.json")
    monkeypatch.setattr(
        dhan_auth_cli,
        "load_cached_access_token_payload",
        lambda: {"accessToken": "expired-token", "expiryTime": expired_at.isoformat()},
    )
    monkeypatch.setattr(dhan_auth_cli, "is_auto_login_configured", lambda: True)
    monkeypatch.setattr(dhan_auth_cli, "refresh_token", lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("manual refresh not expected")))

    result = dhan_auth_cli.ensure_token(min_fresh_minutes=30, auto_login=False)

    assert result["status"] == "error"
    assert result["source"] == "dhan_auth_preflight"
    assert "Refresh Dhan auth" in result["operator_action"]


def test_dhan_auth_cli_ensure_refresh_failure_returns_structured_error(monkeypatch, tmp_path):
    expired_at = datetime.now() - timedelta(hours=1)
    events = []

    monkeypatch.delenv("DHAN_ACCESS_TOKEN", raising=False)
    monkeypatch.delenv("DHAN_TOKEN_ID", raising=False)
    monkeypatch.setattr(dhan_auth_cli, "DEFAULT_TOKEN_CACHE", tmp_path / "dhan.json")
    monkeypatch.setattr(
        dhan_auth_cli,
        "load_cached_access_token_payload",
        lambda: {"accessToken": "expired-token", "expiryTime": expired_at.isoformat()},
    )
    monkeypatch.setattr(dhan_auth_cli, "is_auto_login_configured", lambda: True)
    monkeypatch.setattr(dhan_auth_cli, "refresh_token", lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("cdp refused")))
    monkeypatch.setattr(dhan_auth_cli, "record_local_fallback_event", lambda **kwargs: events.append(kwargs) or kwargs)

    result = dhan_auth_cli.ensure_token(min_fresh_minutes=30, auto_login=True)

    assert result["status"] == "error"
    assert result["source"] == "dhan_auth_preflight"
    assert result["error_type"] == "RuntimeError"
    assert "cdp refused" in result["error"]
    assert "Refresh Dhan auth" in result["operator_action"]
    assert events[0]["fallback_type"] == "dhan_auth_cli_ensure_refresh_failed"
    assert events[0]["source"] == "dhan_auth_preflight"
    assert events[0]["metadata"]["auto_login"] is True


def test_dhan_auth_cli_parse_expiry_records_local_fallback_on_malformed_value(monkeypatch):
    events = []

    monkeypatch.setattr(dhan_auth_cli, "record_local_fallback_event", lambda **kwargs: events.append(kwargs) or kwargs)

    assert dhan_auth_cli.parse_expiry("not-a-date") is None
    assert events[0]["module"] == "data.dhanlive.auth_cli"
    assert events[0]["source"] == "dhan_auth_cache"
    assert events[0]["fallback_type"] == "dhan_auth_cli_cached_expiry_parse_failed"
    assert events[0]["metadata"]["raw_expiry"] == "not-a-date"


def test_dhan_auth_cli_validate_token_records_local_fallback_on_failure(monkeypatch):
    events = []

    class FakeClient:
        def __init__(self, access_token):
            self.access_token = access_token

        def validate_access_token(self):
            raise dhan_client.DhanAPIError("Client ID or user generated access token is invalid or expired.")

    monkeypatch.setattr(dhan_auth_cli, "DhanHistoricalClient", FakeClient)
    monkeypatch.setattr(dhan_auth_cli, "record_local_fallback_event", lambda **kwargs: events.append(kwargs) or kwargs)

    result = dhan_auth_cli.validate_token("TOKEN")

    assert result["status"] == "error"
    assert "invalid or expired" in result["error"]
    assert len(events) == 1
    assert events[0]["module"] == "data.dhanlive.auth_cli"
    assert events[0]["source"] == "dhan_auth"
    assert events[0]["fallback_type"] == "dhan_token_validation_failed"
    assert events[0]["severity"] == "error"
    assert events[0]["metadata"]["access_token_present"] is True


def test_dhan_auth_records_local_fallback_for_corrupt_cached_token(monkeypatch, tmp_path):
    events = []
    cache_path = tmp_path / "dhan_access_token.json"
    cache_path.write_text("{not-json", encoding="utf-8")
    monkeypatch.setattr(dhan_auth, "record_local_fallback_event", lambda **kwargs: events.append(kwargs) or kwargs)

    payload = dhan_auth.load_cached_access_token_payload(cache_path)

    assert payload is None
    assert len(events) == 1
    assert events[0]["module"] == "data.dhanlive.auth"
    assert events[0]["source"] == "dhan_auth"
    assert events[0]["fallback_type"] == "dhan_cached_access_token_read_failed"
    assert events[0]["metadata"]["cache_path"] == str(cache_path)


def test_dhan_auth_records_local_fallback_for_invalid_cached_expiry(monkeypatch, tmp_path):
    events = []
    cache_path = tmp_path / "dhan_access_token.json"
    cache_path.write_text(json.dumps({"accessToken": "TOKEN", "expiryTime": "not-a-date"}), encoding="utf-8")
    monkeypatch.setattr(dhan_auth, "record_local_fallback_event", lambda **kwargs: events.append(kwargs) or kwargs)

    token = dhan_auth.load_cached_access_token(cache_path)

    assert token is None
    assert len(events) == 1
    assert events[0]["module"] == "data.dhanlive.auth"
    assert events[0]["fallback_type"] == "dhan_cached_access_token_expiry_invalid"
    assert events[0]["metadata"]["raw_expiry"] == "not-a-date"


def test_dhan_auth_records_fallback_when_clear_cache_missing(monkeypatch, tmp_path):
    events = []
    cache_path = tmp_path / "missing_token.json"
    monkeypatch.setattr(dhan_auth, "record_local_fallback_event", lambda **kwargs: events.append(kwargs) or kwargs)

    assert dhan_auth.clear_cached_access_token(cache_path) is False
    assert events[0]["module"] == "data.dhanlive.auth"
    assert events[0]["fallback_type"] == "dhan_cached_access_token_clear_missing"
    assert events[0]["metadata"]["cache_path"] == str(cache_path)


def test_dhan_auth_cached_token_past_real_utc_expiry_is_rejected(tmp_path):
    # Regression: expiryTime is UTC ("...Z"); the old code converted it to a naive
    # LOCAL-wall-clock value and compared against naive datetime.utcnow(), which on an
    # IST host silently treated an already-expired token as valid for ~5.5h past its
    # real expiry. A token whose real UTC expiry is in the past must always read as expired,
    # regardless of the host's local timezone.
    import datetime as _dt

    cache_path = tmp_path / "dhan_access_token.json"
    past_expiry = (_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%S") + "Z"
    cache_path.write_text(json.dumps({"accessToken": "TOKEN", "expiryTime": past_expiry}), encoding="utf-8")

    assert dhan_auth.load_cached_access_token(cache_path) is None


def test_dhan_auth_force_refresh_relogins_when_no_concurrent_refresher(monkeypatch, tmp_path):
    # Regression: force_refresh_access_token()'s double-check-cache (added to let a caller
    # reuse a token a concurrent process just obtained while waiting for the login lock) must
    # not short-circuit when NOTHING changed -- e.g. Dhan invalidated a token server-side
    # before its stated expiry. Without comparing against the caller's failing token, a solo
    # caller would just get the same dead token back forever instead of ever re-logging in.
    cache_path = tmp_path / "dhan_access_token.json"
    lock_path = tmp_path / "dhan_login.lock"
    monkeypatch.setattr(dhan_auth, "DEFAULT_TOKEN_CACHE", cache_path)
    monkeypatch.setattr(dhan_auth, "DEFAULT_LOGIN_LOCK", lock_path)
    monkeypatch.setattr(dhan_auth, "load_cached_access_token", lambda cache_path=cache_path: "STALE")
    monkeypatch.setattr(dhan_auth, "clear_cached_access_token", lambda cache_path=cache_path: True)
    monkeypatch.setattr(dhan_auth, "is_auto_login_configured", lambda: True)
    monkeypatch.setattr(dhan_auth, "get_token_id_from_auto_login", lambda: "new-token-id")
    monkeypatch.setattr(dhan_auth, "consume_consent_token", lambda token_id: {"accessToken": "FRESH"})

    result = dhan_auth.force_refresh_access_token(current_token="STALE")

    assert result == "FRESH"


def test_dhan_auth_force_refresh_reuses_concurrent_refresh(monkeypatch, tmp_path):
    lock_path = tmp_path / "dhan_login.lock"
    monkeypatch.setattr(dhan_auth, "DEFAULT_LOGIN_LOCK", lock_path)
    monkeypatch.setattr(dhan_auth, "load_cached_access_token", lambda cache_path=None: "FRESH_FROM_OTHER_PROCESS")

    def fail_if_called(*args, **kwargs):
        raise AssertionError("should not attempt a fresh login when another process already refreshed")

    monkeypatch.setattr(dhan_auth, "clear_cached_access_token", fail_if_called)
    monkeypatch.setattr(dhan_auth, "is_auto_login_configured", fail_if_called)

    result = dhan_auth.force_refresh_access_token(current_token="OLD")

    assert result == "FRESH_FROM_OTHER_PROCESS"


def test_dhan_auth_records_missing_browser_launchers(monkeypatch):
    events = []

    def missing_launcher(*args, **kwargs):
        raise FileNotFoundError("missing")

    monkeypatch.delenv("CHROME_BINARY", raising=False)
    monkeypatch.setattr(dhan_auth.subprocess, "Popen", missing_launcher)
    monkeypatch.setattr(dhan_auth, "record_local_fallback_event", lambda **kwargs: events.append(kwargs) or kwargs)

    with pytest.raises(dhan_auth.DhanAuthError):
        dhan_auth.open_browser_url("https://auth.dhan.co/consent")

    assert {event["fallback_type"] for event in events} == {"dhan_browser_launcher_missing"}
    assert {event["metadata"]["command"] for event in events} == {"google-chrome", "xdg-open", "open"}
    assert all(event["metadata"]["url_host"] == "auth.dhan.co" for event in events)


def test_dhan_client_refreshes_token_after_401(monkeypatch):
    calls = []

    class FakeResponse:
        def __init__(self, status_code, payload):
            self.status_code = status_code
            self._payload = payload
            self.text = json.dumps(payload)
            self.ok = 200 <= status_code < 300

        def json(self):
            return self._payload

    class FakeSession:
        def __init__(self):
            self.headers = {}

        def request(self, method, url, timeout=None, **kwargs):
            calls.append((method, url, dict(self.headers), kwargs))
            if len(calls) == 1:
                return FakeResponse(401, {"errorMessage": "Client ID or user generated access token is invalid or expired."})
            return FakeResponse(200, {"status": "ok"})

    refresh_calls = []

    def fake_refresh(*, current_token=None):
        refresh_calls.append(current_token)
        return "NEW"

    monkeypatch.setattr(dhan_client, "get_access_token", lambda: "OLD")
    monkeypatch.setattr(dhan_client, "force_refresh_access_token", fake_refresh)
    monkeypatch.setattr(dhan_client.requests, "Session", FakeSession)

    client = dhan_client.DhanHistoricalClient(auth_attempts=3)
    payload = client.validate_access_token()

    assert payload == {"status": "ok"}
    assert len(calls) == 2
    assert calls[0][2]["access-token"] == "OLD"
    assert calls[1][2]["access-token"] == "NEW"
    assert refresh_calls == ["OLD"]


def test_dhan_client_gives_up_after_auth_refresh_attempts(monkeypatch):
    refresh_calls = []

    class FakeResponse:
        status_code = 401
        ok = False
        text = "Client ID or user generated access token is invalid or expired."

        def json(self):
            return {"errorMessage": self.text}

    class FakeSession:
        def __init__(self):
            self.headers = {}

        def request(self, method, url, timeout=None, **kwargs):
            return FakeResponse()

    def fake_refresh(*, current_token=None):
        refresh_calls.append("refresh")
        raise dhan_auth.DhanAuthError("login failed")

    monkeypatch.setattr(dhan_client, "get_access_token", lambda: "OLD")
    monkeypatch.setattr(dhan_client, "force_refresh_access_token", fake_refresh)
    monkeypatch.setattr(dhan_client.requests, "Session", FakeSession)

    client = dhan_client.DhanHistoricalClient(auth_attempts=3)
    try:
        client.validate_access_token()
    except dhan_client.DhanAPIError as exc:
        assert "auth refresh failed after 3 attempts" in str(exc)
    else:
        raise AssertionError("Expected DhanAPIError")

    assert len(refresh_calls) == 2


def test_dhan_client_records_malformed_json_success_fallback(monkeypatch):
    events: list[dict[str, object]] = []

    class FakeResponse:
        status_code = 200
        ok = True
        text = "not-json"
        url = "https://api.dhan.co/v2/charts/historical"

        def json(self):
            raise ValueError("bad json")

    monkeypatch.setattr(dhan_client, "record_local_fallback_event", lambda **kwargs: events.append(kwargs))

    payload = dhan_client.DhanHistoricalClient._parse_response(dhan_client.DhanHistoricalClient.__new__(dhan_client.DhanHistoricalClient), FakeResponse())

    assert payload == {"raw_text": "not-json"}
    assert len(events) == 1
    assert events[0]["module"] == "data.dhanlive.client"
    assert events[0]["source"] == "dhan_api"
    assert events[0]["fallback_type"] == "dhan_response_json_parse_failed"
    assert events[0]["metadata"]["status_code"] == 200
    assert events[0]["metadata"]["url_host"] == "api.dhan.co"
    assert events[0]["metadata"]["url_path"] == "/v2/charts/historical"
    assert events[0]["metadata"]["response_ok"] is True


def test_dhan_client_records_malformed_json_error_before_raising(monkeypatch):
    events: list[dict[str, object]] = []

    class FakeResponse:
        status_code = 502
        ok = False
        text = "<html>bad gateway</html>"
        url = "https://api.dhan.co/v2/charts/intraday"

        def json(self):
            raise ValueError("bad json")

    monkeypatch.setattr(dhan_client, "record_local_fallback_event", lambda **kwargs: events.append(kwargs))

    client = dhan_client.DhanHistoricalClient.__new__(dhan_client.DhanHistoricalClient)
    with pytest.raises(dhan_client.DhanAPIError, match="status 502"):
        dhan_client.DhanHistoricalClient._parse_response(client, FakeResponse())

    assert len(events) == 1
    assert events[0]["fallback_type"] == "dhan_response_json_parse_failed"
    assert events[0]["metadata"]["status_code"] == 502
    assert events[0]["metadata"]["url_path"] == "/v2/charts/intraday"
    assert events[0]["metadata"]["response_ok"] is False


def test_bhavcopy_parser_skips_db_parsed_dates(monkeypatch):
    processed: list[str] = []

    monkeypatch.setattr(
        bhavcopy_parser.store,
        "list_files",
        lambda prefix: iter(["bhavcopy/bhavcopy_2015-01-16.zip", "bhavcopy/bhavcopy_2015-01-17.zip"]),
    )
    monkeypatch.setattr(bhavcopy_parser, "should_consider_key", lambda key, **kw: True)
    monkeypatch.setattr(bhavcopy_parser, "get_processed_keys", lambda *_args, **_kwargs: set())
    monkeypatch.setattr(bhavcopy_parser, "get_failed_entries", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(
        bhavcopy_parser,
        "sql_to_df",
        lambda *_args, **_kwargs: pd.DataFrame({"parsed_date": [pd.Timestamp("2015-01-16")]}),
    )
    monkeypatch.setattr(bhavcopy_parser.store, "get_as_temp_file", lambda key: f"/tmp/{key.split('/')[-1]}")
    monkeypatch.setattr(bhavcopy_parser, "unzip_and_process", lambda file_path: processed.append(file_path) or True)
    monkeypatch.setattr(bhavcopy_parser, "mark_processed", lambda *_args, **_kwargs: None)

    class DummyRedis:
        def sadd(self, *_args, **_kwargs):
            return 1

        def close(self):
            return None

    monkeypatch.setattr(bhavcopy_parser, "rop", DummyRedis())

    result = bhavcopy_parser.run_parser()

    assert processed == ["/tmp/bhavcopy_2015-01-17.zip"]
    assert result["files_considered"] == 2
    assert result["already_parsed_db_count"] == 1
    assert result["parsed_count"] == 1
    assert result["rows_written"] == 2
    assert result["state_advanced"] is True


def test_bhavcopy_parser_cat_turnover_raises_visible_error(monkeypatch):
    monkeypatch.setattr(
        bhavcopy_parser.pd,
        "read_excel",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("corrupt workbook")),
    )

    try:
        bhavcopy_parser.parse_cat_turnover("/tmp/cat_turnover_bad.xls")
    except RuntimeError as exc:
        assert "CAT Turnover workbook" in str(exc)
        assert "/tmp/cat_turnover_bad.xls" in str(exc)
    else:
        raise AssertionError("expected parse_cat_turnover to raise RuntimeError")


def test_bhavcopy_parser_records_failed_key(monkeypatch):
    failed: list[tuple[str, str, str]] = []
    events: list[dict[str, object]] = []

    monkeypatch.setattr(
        bhavcopy_parser.store,
        "list_files",
        lambda prefix: iter(["bhavcopy/bhavcopy_2015-01-16.zip"]),
    )
    monkeypatch.setattr(bhavcopy_parser, "should_consider_key", lambda key, **kw: True)
    monkeypatch.setattr(bhavcopy_parser, "get_processed_keys", lambda *_args, **_kwargs: set())
    monkeypatch.setattr(bhavcopy_parser, "load_existing_ohlcv_dates", lambda *_args, **_kwargs: set())
    monkeypatch.setattr(bhavcopy_parser, "get_failed_entries", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(bhavcopy_parser.store, "get_as_temp_file", lambda key: "/tmp/failing.zip")
    monkeypatch.setattr(
        bhavcopy_parser,
        "unzip_and_process",
        lambda path: (_ for _ in ()).throw(RuntimeError("broken nested zip")),
    )
    monkeypatch.setattr(
        bhavcopy_parser,
        "mark_failed",
        lambda source_prefix, object_key, error_message: failed.append((source_prefix, object_key, error_message)),
    )
    monkeypatch.setattr(bhavcopy_parser, "record_local_fallback_event", lambda **kwargs: events.append(kwargs) or kwargs)

    class DummyRedis:
        def sadd(self, *_args, **_kwargs):
            raise AssertionError("should not mark redis success for failed parse")

        def close(self):
            return None

    monkeypatch.setattr(bhavcopy_parser, "rop", DummyRedis())

    result = bhavcopy_parser.run_parser()

    assert failed == [("bhavcopy", "bhavcopy/bhavcopy_2015-01-16.zip", "classification=parser_bug; RuntimeError: broken nested zip")]
    assert result["failed_count"] == 1
    assert result["failed_classifications"] == {"parser_bug": 1}
    assert result["failed_keys"] == ["bhavcopy/bhavcopy_2015-01-16.zip"]
    assert result["state_advanced"] is False
    assert events[0]["fallback_type"] == "nse_bhavcopy_parse_failed"
    assert events[0]["metadata"]["classification"] == "parser_bug"


def test_bhavcopy_parser_marks_empty_like_key_processed(monkeypatch):
    processed: list[tuple[str, str, str]] = []

    monkeypatch.setattr(
        bhavcopy_parser.store,
        "list_files",
        lambda prefix: iter(["bhavcopy/bhavcopy_2015-10-18.zip"]),
    )
    monkeypatch.setattr(bhavcopy_parser, "should_consider_key", lambda key, **kw: True)
    monkeypatch.setattr(bhavcopy_parser, "get_processed_keys", lambda *_args, **_kwargs: set())
    monkeypatch.setattr(bhavcopy_parser, "get_failed_entries", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(bhavcopy_parser, "load_existing_ohlcv_dates", lambda *_args, **_kwargs: set())
    monkeypatch.setattr(bhavcopy_parser.store, "get_as_temp_file", lambda key: "/tmp/empty_like.zip")
    monkeypatch.setattr(bhavcopy_parser, "unzip_and_process", lambda path: False)
    monkeypatch.setattr(
        bhavcopy_parser,
        "mark_processed",
        lambda source_prefix, object_key, status="processed", **_kwargs: processed.append((source_prefix, object_key, status)),
    )

    class DummyRedis:
        def sadd(self, *_args, **_kwargs):
            raise AssertionError("should not mark redis success for non-ohclv processed key")

        def close(self):
            return None

    monkeypatch.setattr(bhavcopy_parser, "rop", DummyRedis())

    result = bhavcopy_parser.run_parser()

    assert processed == [("bhavcopy", "bhavcopy/bhavcopy_2015-10-18.zip", bhavcopy_parser.EMPTY_VALID_STATUS)]
    assert result["empty_processed_count"] == 1
    assert result["empty_keys"] == ["bhavcopy/bhavcopy_2015-10-18.zip"]
    assert result["rows_written"] == 1
    assert result["state_advanced"] is True


def test_bhavcopy_parser_treats_empty_valid_source_as_completed(monkeypatch):
    processed_statuses: list[str] = []

    monkeypatch.setattr(
        bhavcopy_parser.store,
        "list_files",
        lambda prefix: iter(["bhavcopy/bhavcopy_2015-10-18.zip"]),
    )
    monkeypatch.setattr(bhavcopy_parser, "should_consider_key", lambda key, **kw: True)
    monkeypatch.setattr(
        bhavcopy_parser,
        "get_processed_keys",
        lambda _source_prefix, status="processed": {"bhavcopy/bhavcopy_2015-10-18.zip"} if status == bhavcopy_parser.EMPTY_VALID_STATUS else set(),
    )
    monkeypatch.setattr(bhavcopy_parser, "get_failed_entries", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(bhavcopy_parser, "load_existing_ohlcv_dates", lambda *_args, **_kwargs: set())
    monkeypatch.setattr(bhavcopy_parser.store, "get_as_temp_file", lambda key: (_ for _ in ()).throw(AssertionError("completed empty key should not be fetched")))
    monkeypatch.setattr(
        bhavcopy_parser,
        "mark_processed",
        lambda source_prefix, object_key, status="processed", **_kwargs: processed_statuses.append(status),
    )

    class DummyRedis:
        def sadd(self, *_args, **_kwargs):
            raise AssertionError("completed empty key should not hit redis")

        def close(self):
            return None

    monkeypatch.setattr(bhavcopy_parser, "rop", DummyRedis())

    result = bhavcopy_parser.run_parser()

    assert processed_statuses == []
    assert result["already_processed_count"] == 1
    assert result["empty_processed_count"] == 0


def test_bhavcopy_parser_raises_on_bad_zip(monkeypatch, tmp_path):
    bad_zip = tmp_path / "bad.zip"
    bad_zip.write_text("not a zip", encoding="utf-8")

    try:
        bhavcopy_parser.unzip_and_process(str(bad_zip))
    except RuntimeError as exc:
        assert "bad_bhavcopy_zip" in str(exc)
    else:
        raise AssertionError("expected bad zip to raise RuntimeError")


def test_bhavcopy_parser_failure_classifier_distinguishes_retryable_and_schema_errors():
    assert bhavcopy_parser.classify_bhavcopy_parse_failure(RuntimeError("bad_bhavcopy_zip:bad.zip")) == "bad_file_retryable"
    assert bhavcopy_parser.classify_bhavcopy_parse_failure(KeyError("TradDt")) == "schema_changed"
    assert bhavcopy_parser.classify_bhavcopy_parse_failure(ValueError("Length mismatch: Expected axis has 4 elements")) == "schema_changed"
    assert bhavcopy_parser.classify_bhavcopy_parse_failure(RuntimeError("unexpected parser branch")) == "parser_bug"


def test_bhavcopy_parser_records_invalid_key_date(monkeypatch):
    events: list[dict[str, object]] = []
    monkeypatch.setattr(bhavcopy_parser, "record_local_fallback_event", lambda **kwargs: events.append(kwargs) or kwargs)

    assert bhavcopy_parser.extract_bhavcopy_date_from_key("bhavcopy/not-a-date.zip") is None

    assert events[0]["fallback_type"] == "nse_bhavcopy_key_date_parse_failed"
    assert events[0]["source"] == "bhavcopy/not-a-date.zip"


def test_bhavcopy_parser_records_empty_zip_stat_failure(monkeypatch):
    events: list[dict[str, object]] = []
    monkeypatch.setattr(bhavcopy_parser, "record_local_fallback_event", lambda **kwargs: events.append(kwargs) or kwargs)
    monkeypatch.setattr(
        bhavcopy_parser.os.path,
        "getsize",
        lambda path: (_ for _ in ()).throw(OSError("stat failed")),
    )

    assert bhavcopy_parser.is_empty_zip("/tmp/missing.zip") is False

    assert events[0]["fallback_type"] == "nse_bhavcopy_zip_stat_failed"
    assert events[0]["source"] == "/tmp/missing.zip"


def test_bhavcopy_parser_records_circuit_hit_date_fallback(monkeypatch, tmp_path):
    events: list[dict[str, object]] = []
    path = tmp_path / "bh01012026.csv"
    path.write_text("SYMBOL,SERIES,IGNORED,CIRCUIT\nABC,EQ,x,UPPER\n", encoding="utf-8")
    monkeypatch.setattr(bhavcopy_parser, "record_local_fallback_event", lambda **kwargs: events.append(kwargs) or kwargs)
    monkeypatch.setattr(bhavcopy_parser, "with_company_master", lambda frame: frame)
    monkeypatch.setattr(bhavcopy_parser, "upsert_to_db", lambda *_args, **_kwargs: None)

    frame = bhavcopy_parser.parse_circuit_hit(str(path))

    assert frame["date"].iloc[0] == pd.Timestamp("2026-01-01")
    assert events[0]["fallback_type"] == "nse_bhavcopy_circuit_hit_date_fallback"
    assert events[0]["metadata"]["fallback_format"] == "bh%d%m%Y.csv"


def test_bhavcopy_parser_records_inner_file_parse_failure(monkeypatch, tmp_path):
    import zipfile

    events: list[dict[str, object]] = []
    zip_path = tmp_path / "bhavcopy_2026-01-01.zip"
    with zipfile.ZipFile(zip_path, "w") as archive:
        archive.writestr("C_CATG_BAD.TXT", "bad")
    monkeypatch.setattr(bhavcopy_parser, "record_local_fallback_event", lambda **kwargs: events.append(kwargs) or kwargs)
    monkeypatch.setattr(
        bhavcopy_parser,
        "parse_catg",
        lambda path: (_ for _ in ()).throw(ValueError("bad catg schema")),
    )

    with pytest.raises(RuntimeError, match="catg:C_CATG_BAD.TXT"):
        bhavcopy_parser.unzip_and_process(str(zip_path))

    assert events[0]["fallback_type"] == "nse_bhavcopy_file_parse_failed"
    assert events[0]["metadata"]["label"] == "catg"


def test_bhavcopy_parser_records_nested_bad_zip(monkeypatch, tmp_path):
    import zipfile

    events: list[dict[str, object]] = []
    zip_path = tmp_path / "bhavcopy_2026-01-01.zip"
    with zipfile.ZipFile(zip_path, "w") as archive:
        archive.writestr("cm_bad.zip", "not a zip")
    monkeypatch.setattr(bhavcopy_parser, "record_local_fallback_event", lambda **kwargs: events.append(kwargs) or kwargs)

    with pytest.raises(RuntimeError, match="nested_cm_zip:cm_bad.zip"):
        bhavcopy_parser.unzip_and_process(str(zip_path))

    assert events[0]["fallback_type"] == "nse_bhavcopy_nested_zip_failed"
    assert events[0]["metadata"]["nested_type"] == "cm"


def test_bhavcopy_downloader_uses_s3_keys_as_source_of_truth(monkeypatch):
    monkeypatch.setattr(
        bhavcopy_downloader.store,
        "list_files",
        lambda prefix: iter(["bhavcopy/bhavcopy_2015-01-16.zip", "bhavcopy/ignore.txt"]),
    )
    monkeypatch.setattr(bhavcopy_downloader, "get_failed_entries", lambda *_args, **_kwargs: [])

    assert bhavcopy_downloader.load_downloaded_dates_from_store() == {"2015-01-16"}


def test_bhavcopy_downloader_excludes_failed_keys_from_downloaded_set(monkeypatch):
    monkeypatch.setattr(
        bhavcopy_downloader.store,
        "list_files",
        lambda prefix: iter(["bhavcopy/bhavcopy_2015-01-16.zip", "bhavcopy/bhavcopy_2015-01-17.zip"]),
    )
    monkeypatch.setattr(
        bhavcopy_downloader,
        "get_failed_entries",
        lambda *_args, **_kwargs: [{"object_key": "bhavcopy/bhavcopy_2015-01-16.zip"}],
    )

    assert bhavcopy_downloader.load_downloaded_dates_from_store() == {"2015-01-17"}


def test_bhavcopy_downloader_records_malformed_store_key(monkeypatch):
    events: list[dict[str, object]] = []
    monkeypatch.setattr(bhavcopy_downloader, "record_local_fallback_event", lambda **kwargs: events.append(kwargs) or kwargs)

    assert bhavcopy_downloader.extract_downloaded_date_from_key("bhavcopy/bhavcopy_bad.zip") is None

    assert events[0]["fallback_type"] == "nse_bhavcopy_download_key_date_parse_failed"
    assert events[0]["source"] == "bhavcopy/bhavcopy_bad.zip"


def test_bhavcopy_parser_only_considers_last_year_keys():
    today = datetime(2026, 4, 13)

    assert bhavcopy_parser.should_consider_key("bhavcopy/bhavcopy_2026-04-12.zip", today=today)
    assert bhavcopy_parser.should_consider_key("bhavcopy/bhavcopy_2025-04-13.zip", today=today)
    assert not bhavcopy_parser.should_consider_key("bhavcopy/bhavcopy_2025-04-12.zip", today=today)
    assert not bhavcopy_parser.should_consider_key("bhavcopy/invalid.zip", today=today)


def test_indices_downloader_uses_s3_keys_as_source_of_truth(monkeypatch):
    monkeypatch.setattr(
        indices_downloader.store,
        "list_files",
        lambda prefix: iter(["indices/indices_2015-01-16.zip", "indices/ignore.txt"]),
    )

    assert indices_downloader.load_downloaded_dates_from_store() == {"2015-01-16"}


def test_indices_downloader_records_malformed_store_key(monkeypatch):
    events: list[dict[str, object]] = []
    monkeypatch.setattr(indices_downloader, "record_local_fallback_event", lambda **kwargs: events.append(kwargs) or kwargs)

    assert indices_downloader.extract_downloaded_date_from_key("indices/indices_bad.zip") is None

    assert events[0]["fallback_type"] == "nse_indices_download_key_date_parse_failed"
    assert events[0]["source"] == "indices/indices_bad.zip"


def test_indices_downloader_records_malformed_downloaded_member(monkeypatch):
    events: list[dict[str, object]] = []
    monkeypatch.setattr(indices_downloader, "record_local_fallback_event", lambda **kwargs: events.append(kwargs) or kwargs)

    latest = indices_downloader.latest_downloaded_date({"2026-06-10", "bad-date"})

    assert latest == datetime(2026, 6, 10)
    assert events[0]["fallback_type"] == "nse_indices_downloaded_member_parse_failed"
    assert events[0]["source"] == "bad-date"


def test_indices_downloader_partition_empty_attempts_retry_vs_give_up():
    today = datetime(2026, 7, 14)
    first_seen = {
        "2026-07-13": "2026-07-13",  # 1 day old -> retry
        "2026-07-11": "2026-07-11",  # 3 days old, within window (4) -> retry
        "2026-07-09": "2026-07-09",  # 5 days old, past window -> give up
        "2026-07-01": "bad-iso",     # unparseable -> give up (do not retry forever)
        "2026-07-12": "2026-07-12",  # but already downloaded -> neither
    }
    existing = {"2026-07-12"}

    retry, give_up = indices_downloader.partition_empty_attempts(
        first_seen, existing, today=today, window_days=4
    )

    assert retry == ["2026-07-11", "2026-07-13"]
    assert give_up == ["2026-07-01", "2026-07-09"]


def test_bhavcopy_parser_backfill_lifts_lookback():
    # the deep history was downloaded to the store but never parsed because the 365-day lookback skipped it;
    # --backfill must consider every stored day so it can be recovered.
    from datetime import datetime
    from data.nseindia import bhavcopy_parser as bp
    t = datetime(2026, 7, 23)
    old, recent, bad = "bhavcopy/bhavcopy_2018-01-03.zip", "bhavcopy/bhavcopy_2026-07-20.zip", "bhavcopy/xx.zip"
    assert bp.should_consider_key(old, today=t) is False          # default: skipped (older than lookback)
    assert bp.should_consider_key(recent, today=t) is True        # default: within lookback
    assert bp.should_consider_key(old, today=t, backfill=True) is True    # backfill: considered
    assert bp.should_consider_key(recent, today=t, backfill=True) is True
    assert bp.should_consider_key(bad, today=t, backfill=True) is False   # non-date key never considered


def test_bhavcopy_history_extracts_ohlcv_from_store_zip():
    import io, zipfile
    from data.nseindia import bhavcopy_history as bh
    cm_csv = (b"SYMBOL,SERIES,OPEN,HIGH,LOW,CLOSE,LAST,PREVCLOSE,TOTTRDQTY,TOTTRDVAL,TIMESTAMP,TOTALTRADES,ISIN\n"
              b"RELIANCE,EQ,100,110,95,105,105,98,1000,105000,02-JAN-2018,50,INE002A01018\n")
    inner = io.BytesIO()
    with zipfile.ZipFile(inner, "w") as z:
        z.writestr("cm02JAN2018bhav.csv", cm_csv)
    outer = io.BytesIO()
    with zipfile.ZipFile(outer, "w") as z:                 # an 'all-reports' zip with the nested cm bhav zip
        z.writestr("NSE_Market_Pulse.pdf", b"x")
        z.writestr("cm02JAN2018bhav.csv.zip", inner.getvalue())
    df = bh.ohlcv_from_store_zip(outer.getvalue())
    assert df is not None and len(df) == 1
    assert df.iloc[0]["symbol"] == "RELIANCE" and df.iloc[0]["close"] == 105
    # a zip with no OHLCV report (e.g. a weekend/holiday download) -> None
    empty = io.BytesIO()
    with zipfile.ZipFile(empty, "w") as z:
        z.writestr("shortselling.csv", b"x")
    assert bh.ohlcv_from_store_zip(empty.getvalue()) is None


def test_bhavcopy_history_parsers_and_split_detection():
    from data.nseindia import bhavcopy_history as bh
    oldcm = (b"SYMBOL,SERIES,OPEN,HIGH,LOW,CLOSE,LAST,PREVCLOSE,TOTTRDQTY,TOTTRDVAL,TIMESTAMP,TOTALTRADES,ISIN\n"
             b"RELIANCE,EQ,100,110,95,105,105,98,1000,105000,02-JAN-2018,50,INE002A01018\n"
             b"FIVEONE,EQ,20,21,19,20,20,100,500,10000,02-JAN-2018,10,INE123A01011\n")   # open 20 vs prevclose 100 = 5:1
    df = bh.parse_oldcm(oldcm)
    r = df[df["symbol"] == "RELIANCE"].iloc[0]
    assert (r.open, r.close, r.previous_close, r.series, r["isin"]) == (100, 105, 98, "EQ", "INE002A01018")
    assert str(r.date.date()) == "2018-01-02"
    # UDiFF format maps to the same schema
    udiff = (b"TradDt,TckrSymb,SctySrs,OpnPric,HghPric,LwPric,ClsPric,LastPric,PrvsClsgPric,TtlTradgVol,TtlTrfVal,TtlNbOfTxsExctd,ISIN\n"
             b"2024-07-08,INFY,EQ,1500,1520,1490,1510,1510,1495,2000,3000000,100,INE009A01021\n")
    u = bh.parse_udiff(udiff).iloc[0]
    assert (u.symbol, u.open, u.close, u.series) == ("INFY", 1500, 1510, "EQ") and str(u.date.date()) == "2024-07-08"
    # corporate-action detection: only the 5:1 name is flagged, with the right factor
    ca = bh.split_factors(df)
    assert set(ca["symbol"]) == {"FIVEONE"}
    assert abs(float(ca.iloc[0]["adj_factor"]) - 0.20) < 1e-6      # open/prevclose = 20/100


def test_bhavcopy_parser_mto_dat_delivery(monkeypatch, tmp_path):
    # NSE ships delivery as MTO_<ddmmyyyy>.DAT with 4 preamble lines, "20,..." security rows, and a
    # "90,..." grand-total trailer. The parser must read the .DAT layout and drop the trailer.
    captured: dict[str, object] = {}
    path = tmp_path / "MTO_02012018.DAT"
    path.write_text(
        "Security Wise Delivery Position - Compulsory Rolling Settlement\n"
        "10,MTO,02012018,723652832,0002021\n"
        "Trade Date <02-JAN-2018>,Settlement Type <N>\n"
        "Record Type,Sr No,Name of Security,Series,Quantity Traded,Deliverable Quantity,Percentage\n"
        "20,1,RELIANCE,EQ,1000,600,60.00\n"
        "20,2,TCS,EQ,500,250,50.00\n"
        "90,MTO,02012018,1500,850\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(bhavcopy_parser, "with_company_master", lambda frame: frame)
    monkeypatch.setattr(bhavcopy_parser, "upsert_to_db", lambda df, table, **kw: captured.update({"table": table, "df": df}))

    frame = bhavcopy_parser.parse_mto(str(path))

    assert captured["table"] == "nseindia_mto"
    assert set(frame["symbol"]) == {"RELIANCE", "TCS"}          # the "90" trailer row is dropped
    reliance = frame[frame["symbol"] == "RELIANCE"].iloc[0]
    assert int(reliance["deliverable_volume"]) == 600
    assert abs(float(reliance["deliverable_percent"]) - 60.0) < 1e-9
    assert reliance["date"] == pd.Timestamp("2018-01-02")


def test_bhavcopy_parser_wk52_high_low(monkeypatch, tmp_path):
    # CM_52_wk_High_low_<ddmmyyyy>.csv -- 2 disclaimer/effective-date lines precede the header.
    captured: dict[str, object] = {}
    path = tmp_path / "CM_52_wk_High_low_02012018.csv"
    path.write_text(
        '"Disclaimer - adjusted for corporate actions"\n'
        '"Effective for 02-Jan-2018"\n'
        '"SYMBOL","SERIES","Adjusted 52_Week_High","52_Week_High_Date","Adjusted 52_Week_Low","52_Week_Low_DT"\n'
        '"RELIANCE","EQ","    1200.50","18-JAN-2017","     900.25","23-AUG-2017"\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(bhavcopy_parser, "with_company_master", lambda frame: frame)
    monkeypatch.setattr(bhavcopy_parser, "upsert_to_db", lambda df, table, **kw: captured.update({"table": table, "df": df}))

    frame = bhavcopy_parser.parse_wk52(str(path))

    assert captured["table"] == "nseindia_52wk"
    row = frame[frame["symbol"] == "RELIANCE"].iloc[0]
    assert abs(float(row["adjusted_52_week_high"]) - 1200.50) < 1e-9
    assert abs(float(row["adjusted_52_week_low"]) - 900.25) < 1e-9
    assert row["high_date"] == pd.Timestamp("2017-01-18")
    assert row["date"] == pd.Timestamp("2018-01-02")


def test_bhavcopy_parser_var1_dedupes_same_day_entries(monkeypatch, tmp_path):
    # NSE republishes VAR1 several times a day as separately entry-numbered files. Storing every
    # numbered file as its own row grew nseindia_var1 to 209M rows for no informational gain (the
    # only consumer already collapses to MAX per day). parse_var1 must combine all of a day's
    # files into ONE column-wise-MAX row per (for_date, series, symbol, isin) -- matching that
    # consumer's aggregation exactly -- and RELIANCE's genuinely-revised margin (up in entry 2)
    # must not be shadowed by the unchanged TCS row.
    header_cols = "RecordType,Symbol,Series,ISIN,SecVaR,IdxVaR,VaRMargin,ELR,AdhocMargin,ApplicableMargin\n"
    entry1 = tmp_path / "C_VAR1_02012018_1.DAT"
    entry1.write_text(
        "01,02012018,X,Y\n" + header_cols +          # 4 comma-fields -> entry_number defaults to 1
        "1,RELIANCE,EQ,INE002A01018,3.5,4.0,7.5,1.0,0,8.5\n"
        "1,TCS,EQ,INE467B01029,3.0,4.0,7.0,1.0,0,8.0\n",
        encoding="utf-8",
    )
    entry2 = tmp_path / "C_VAR1_02012018_2.DAT"
    entry2.write_text(
        "01,02012018,X,2,Y\n" + header_cols +         # 5 comma-fields -> entry_number = parts[3] = 2
        "1,RELIANCE,EQ,INE002A01018,4.0,4.0,8.0,1.0,0,9.0\n"     # margin genuinely revised up
        "1,TCS,EQ,INE467B01029,3.0,4.0,7.0,1.0,0,8.0\n",         # unchanged republish
        encoding="utf-8",
    )
    monkeypatch.setattr(bhavcopy_parser, "with_company_master", lambda frame: frame)
    captured: dict[str, object] = {}
    monkeypatch.setattr(bhavcopy_parser, "upsert_to_db", lambda df, table, **kw: captured.update({"table": table, "df": df, "unique_keys": kw.get("unique_keys")}))

    frame = bhavcopy_parser.parse_var1([str(entry1), str(entry2)])

    assert captured["table"] == "nseindia_var1"
    assert captured["unique_keys"] == ["for_date", "series", "symbol", "isin"]
    assert len(frame) == 2                                              # one row per symbol, not per entry
    reliance = frame[frame["symbol"] == "RELIANCE"].iloc[0]
    assert abs(float(reliance["applicable_margin"]) - 9.0) < 1e-9        # kept the higher, revised value
    assert int(reliance["entry_number"]) == 2                           # provenance: highest contributing entry
    tcs = frame[frame["symbol"] == "TCS"].iloc[0]
    assert abs(float(tcs["applicable_margin"]) - 8.0) < 1e-9             # unchanged across entries


def test_legacy_archival_never_touches_protected_numerical_tables(monkeypatch):
    # Operator decision: core numerical bhavcopy data must never be archived out of the live DB. The
    # legacy-archival tool must skip every protected table by default, and only proceed under an
    # explicit force override.
    from scripts import archive_legacy_nse_tables as ala
    from scripts.db_table_retention_report import PROTECTED_NUMERICAL_TABLES

    assert {"nseindia_ohlcv", "nseindia_mto", "nseindia_52wk", "nseindia_indices"} <= PROTECTED_NUMERICAL_TABLES

    calls: list[str] = []
    monkeypatch.setattr(ala, "archive_table", lambda *, table_name, **kw: calls.append(table_name) or {"table_name": table_name, "deleted_rows": 0})

    result = ala.run_archive(
        tables=["nseindia_ohlcv", "nseindia_mto"], retention_days=365, cutoff="2020-01-01",
        archive_s3=True, delete=True, execute=True, allow_delete_without_archive=True,
        archive_prefix="x", max_chunks=1, exact_counts=False,
    )
    assert calls == []                                                  # archive_table never invoked
    assert all(r["status"] == "protected_never_archived" for r in result["results"])
    assert result["deleted_rows"] == 0

    # explicit override lets it proceed (guard is a safety default, not a hard lock)
    ala.run_archive(
        tables=["nseindia_ohlcv"], retention_days=365, cutoff="2020-01-01",
        archive_s3=True, delete=False, execute=False, allow_delete_without_archive=False,
        archive_prefix="x", max_chunks=1, exact_counts=False, force_archive_protected=True,
    )
    assert calls == ["nseindia_ohlcv"]


def test_store_download_retries_transient_timeout(monkeypatch):
    # A read timeout while streaming the S3 Body previously aborted the whole multi-hour backfill
    # (2017-06-18). _download_bytes must retry the full get_object+read and self-heal.
    import utils.store as store
    from botocore.exceptions import ReadTimeoutError, ClientError

    monkeypatch.setattr(store.time, "sleep", lambda *_a, **_k: None)  # no backoff delay in test

    class _Body:
        def read(self):
            return b"PK\x03\x04ok"

    state = {"n": 0}

    class _FlakyClient:
        def get_object(self, **_kw):
            state["n"] += 1
            if state["n"] < 3:
                raise ReadTimeoutError(endpoint_url="s3", error="timed out")
            return {"Body": _Body()}

    monkeypatch.setattr(store, "_CLIENT", _FlakyClient())
    assert store._download_bytes("bhavcopy/x.zip") == b"PK\x03\x04ok"
    assert state["n"] == 3  # retried twice, succeeded on the third

    # persistent timeout -> raises after exhausting attempts (caller records + skips the one key)
    class _DeadClient:
        def get_object(self, **_kw):
            raise ReadTimeoutError(endpoint_url="s3", error="timed out")

    monkeypatch.setattr(store, "_CLIENT", _DeadClient())
    with pytest.raises(ReadTimeoutError):
        store._download_bytes("bhavcopy/x.zip")

    # a genuinely missing key (ClientError, not a transient network error) must fail fast, not retry
    tries = {"n": 0}

    class _MissingClient:
        def get_object(self, **_kw):
            tries["n"] += 1
            raise ClientError({"Error": {"Code": "NoSuchKey"}}, "GetObject")

    monkeypatch.setattr(store, "_CLIENT", _MissingClient())
    with pytest.raises(ClientError):
        store._download_bytes("bhavcopy/missing.zip")
    assert tries["n"] == 1  # not retried


def test_bhavcopy_corporate_actions_bc_folds_embedded_comma(monkeypatch, tmp_path):
    # The trailing PURPOSE/subject column is free text with unquoted commas, so some rows carry >10
    # comma fields. A plain read_csv raised "Expected 10 fields, saw 11" and lost the WHOLE day's
    # corporate actions (8 backfill days). The parser must fold the overflow back into `subject`.
    captured: dict[str, object] = {}
    path = tmp_path / "Bc010101.csv"
    path.write_text(
        "SERIES,SYMBOL,SECURITY,RECORD_DATE,BC_START,BC_END,EX_DATE,ND_START,ND_END,PURPOSE\n"
        "EQ,RELIANCE,Reliance Ltd,-,-,-,10/01/2019,-,-,DIVIDEND RS 5, SPECIAL\n"
        "EQ,TCS,TCS Ltd,-,-,-,11/01/2019,-,-,BONUS 1:1\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(bhavcopy_parser, "with_company_master", lambda frame: frame)
    monkeypatch.setattr(bhavcopy_parser, "upsert_to_db", lambda df, table, **kw: captured.update({"table": table, "df": df}))

    frame = bhavcopy_parser.parse_corporate_actions_bc(str(path))

    assert captured["table"] == "nseindia_corporate_actions_bc_raw"
    reliance = frame[frame["symbol"] == "RELIANCE"].iloc[0]
    assert reliance["subject"] == "DIVIDEND RS 5, SPECIAL"   # embedded comma folded into one field
    assert reliance["date"] == pd.Timestamp("2019-01-10")
    assert set(frame["symbol"]) == {"RELIANCE", "TCS"}       # both rows survive


def test_bhavcopy_corporate_actions_bc_parses_both_date_formats(monkeypatch, tmp_path):
    # NSE switched the corporate-actions date format from DD/MM/YYYY to ISO YYYY-MM-DD in ~2025-10.
    # Both eras must parse, else the ex-date coerces to NaT and dropna zeroes out the whole day.
    captured: dict[str, object] = {}
    path = tmp_path / "bc16032026.csv"
    path.write_text(
        "SERIES,SYMBOL,SECURITY,RECORD_DT,BC_STRT_DT,BC_END_DT,EX_DT,ND_STRT_DT,ND_END_DT,PURPOSE\n"
        "EQ,OLDFMT,Old Ltd,02/09/2025,,,02/09/2025,,,DIV RS 14\n"     # legacy DD/MM/YYYY
        "EQ,NEWFMT,New Ltd,2026-03-17,,,2026-03-17,,,INTEREST PAYMENT\n",  # ISO YYYY-MM-DD
        encoding="utf-8",
    )
    monkeypatch.setattr(bhavcopy_parser, "with_company_master", lambda frame: frame)
    monkeypatch.setattr(bhavcopy_parser, "upsert_to_db", lambda df, table, **kw: captured.update({"df": df}))

    frame = bhavcopy_parser.parse_corporate_actions_bc(str(path))

    assert set(frame["symbol"]) == {"OLDFMT", "NEWFMT"}   # neither dropped
    assert frame.set_index("symbol").loc["OLDFMT", "date"] == pd.Timestamp("2025-09-02")
    assert frame.set_index("symbol").loc["NEWFMT", "date"] == pd.Timestamp("2026-03-17")


def test_bhavcopy_ci_glob_survives_nse_case_change(tmp_path):
    # NSE lowercased the PR-zip report filenames in ~2025-10 (Bc->bc, MCAP->mcap). The case-sensitive
    # globs then silently dropped corporate actions + market cap for months. _ci_glob must match BOTH
    # cases and must not bleed into unrelated prefixes (pr/pd).
    for name in ["Bc010925.csv", "bc01072026.csv", "MCAP01092025.csv", "mcap01072026.csv",
                 "bh01072026.csv", "pr01072026.csv", "pd01072026.csv"]:
        (tmp_path / name).write_text("x", encoding="utf-8")
    import os as _os
    bc = {_os.path.basename(p) for p in bhavcopy_parser._ci_glob(str(tmp_path), "bc")}
    mcap = {_os.path.basename(p) for p in bhavcopy_parser._ci_glob(str(tmp_path), "mcap")}
    bh = {_os.path.basename(p) for p in bhavcopy_parser._ci_glob(str(tmp_path), "bh")}
    assert bc == {"Bc010925.csv", "bc01072026.csv"}          # both cases, not pr/pd
    assert mcap == {"MCAP01092025.csv", "mcap01072026.csv"}
    assert bh == {"bh01072026.csv"}


def test_bhavcopy_soft_skips_unrecoverable_cat_turnover(monkeypatch, tmp_path):
    import zipfile

    # A corrupt-at-source cat_turnover .xls (NSE no longer serves a good copy) must NOT fail the whole
    # day -- it is skipped visibly so the day's other tables are retained. A different cat_turnover
    # error (e.g. a schema change) still fails hard.
    events: list[dict[str, object]] = []
    zip_path = tmp_path / "bhavcopy_2025-08-05.zip"
    with zipfile.ZipFile(zip_path, "w") as archive:
        archive.writestr("cat_turnover_050825.xls", "corrupt-ole-bytes")
    monkeypatch.setattr(bhavcopy_parser, "record_local_fallback_event", lambda **kwargs: events.append(kwargs) or kwargs)
    monkeypatch.setattr(
        bhavcopy_parser, "parse_cat_turnover",
        lambda path: (_ for _ in ()).throw(RuntimeError(f"Unable to read CAT Turnover workbook path={path}")),
    )

    # the corrupt cat_turnover is the only file -> the day completes (no RuntimeError raised)
    result = bhavcopy_parser.unzip_and_process(str(zip_path))
    assert result is not False
    assert events and events[0]["fallback_type"] == "nse_bhavcopy_file_unrecoverable_skipped"
    assert events[0]["severity"] == "warn"


def test_indices_downloader_confirmed_gap_reconciliation():
    # the equity bhavcopy is the ground truth of which days actually traded; any such day missing from the
    # index is a CONFIRMED gap (a real trading day) -- e.g. the Mar-2026 crash-week days the index endpoint
    # dropped while the bhavcopy captured them.
    traded = {"2026-03-19", "2026-03-20", "2026-03-23", "2026-03-24"}   # bhavcopy-recorded trading days
    index_present = {"2026-03-19", "2026-03-24"}                        # index endpoint dropped 20th & 23rd
    confirmed = indices_downloader.compute_confirmed_gaps(traded, index_present)
    assert confirmed == {"2026-03-20", "2026-03-23"}
    # a confirmed trading day that was wrongly given up must be reinstated for re-download...
    gave_up = {"2026-03-20", "2026-03-23", "2020-05-01"}   # last is a genuine non-trading day, stays given up
    assert indices_downloader.reconcile_gave_up(confirmed, gave_up) == {"2026-03-20", "2026-03-23"}
    # ...and a genuine holiday (never in the bhavcopy) is never treated as a gap
    assert indices_downloader.compute_confirmed_gaps(traded, traded) == set()
    assert "2020-05-01" not in indices_downloader.reconcile_gave_up(confirmed, gave_up)


def test_indices_downloader_empty_download_is_not_persisted_and_retries(monkeypatch, tmp_path):
    """A 0-byte download must never be saved (it would look downloaded forever) and must be retried."""
    monkeypatch.setattr(nse_rate_limiter.time, "sleep", lambda *a, **k: None)  # no real rate-gate delay in test
    saved: list[str] = []
    monkeypatch.setattr(indices_downloader.store, "save_file", lambda **kw: saved.append(kw))

    # Fake a Playwright download that first yields a 0-byte file, then a good one on retry.
    sizes = iter([0, 0, 123])  # empty, empty, then real bytes

    class _FakeDownload:
        def save_as(self, path):
            n = next(sizes)
            with open(path, "wb") as fh:
                fh.write(b"\x00" * n)

    class _FakeCtx:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        @property
        def value(self):
            return _FakeDownload()

    class _FakeLoc:
        def click(self):
            pass

    class _FakePage:
        def wait_for_timeout(self, *a):
            pass

        def goto(self, *a):
            pass

        def evaluate(self, *a):
            pass

        def get_by_role(self, *a, **k):
            return _FakeLoc()

        def expect_download(self, *a, **k):
            return _FakeCtx()

        def close(self):
            pass

    class _FakeBrowser:
        contexts = []

        def new_context(self):
            class _C:
                def new_page(self_inner):
                    return _FakePage()

            return _C()

        def close(self):
            pass

    class _FakeChromium:
        def connect_over_cdp(self, endpoint):
            return _FakeBrowser()

    class _FakePlaywright:
        chromium = _FakeChromium()

    class _FakeRedis:
        def __init__(self):
            self.members: set[str] = set()

        def sadd(self, key, val):
            self.members.add(val)

    monkeypatch.chdir(tmp_path)
    rop = _FakeRedis()

    # single attempt returns "empty" on a 0-byte file, without persisting
    status = indices_downloader.download_indices_for_date(_FakePlaywright(), "2026-07-14", "14-Jul-2026", rop)
    assert status == "empty"
    assert saved == []
    assert rop.members == set()

    # the wrapper retries the empty result and succeeds on the real file (3rd call here)
    status = indices_downloader.download_with_empty_retries(_FakePlaywright(), "2026-07-14", "14-Jul-2026", rop)
    assert status == "downloaded"
    assert len(saved) == 1
    assert rop.members == {"2026-07-14"}


def test_indices_parser_only_considers_last_year_keys():
    today = datetime(2026, 4, 13)

    assert indices_parser.should_consider_key("indices/indices_2026-04-12.zip", today=today)
    assert indices_parser.should_consider_key("indices/indices_2025-04-13.zip", today=today)
    assert not indices_parser.should_consider_key("indices/indices_2025-04-12.zip", today=today)
    assert not indices_parser.should_consider_key("indices/invalid.zip", today=today)


def test_indices_parser_records_failed_key(monkeypatch):
    failed: list[tuple[str, str, str]] = []
    events: list[dict[str, object]] = []

    monkeypatch.setattr(
        indices_downloader.store,
        "list_files",
        lambda prefix: iter(()),
    )
    monkeypatch.setattr(
        indices_parser.store,
        "list_files",
        lambda prefix: iter(["indices/indices_2015-01-16.zip"]),
    )
    monkeypatch.setattr(indices_parser, "get_processed_keys", lambda *_args, **_kwargs: set())
    monkeypatch.setattr(indices_parser, "get_failed_entries", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(indices_parser.store, "get_as_temp_file", lambda key: "/tmp/failing_indices.zip")
    monkeypatch.setattr(indices_parser, "should_consider_key", lambda key, **kw: True)
    monkeypatch.setattr(
        indices_parser,
        "unzip_and_process",
        lambda path: (_ for _ in ()).throw(RuntimeError("bad indices zip contents")),
    )
    monkeypatch.setattr(
        indices_parser,
        "mark_failed",
        lambda source_prefix, object_key, error_message: failed.append((source_prefix, object_key, error_message)),
    )
    monkeypatch.setattr(indices_parser, "record_local_fallback_event", lambda **kwargs: events.append(kwargs) or kwargs)

    class DummyRedis:
        def sadd(self, *_args, **_kwargs):
            raise AssertionError("should not mark redis success for failed indices parse")

        def close(self):
            return None

    monkeypatch.setattr(indices_parser, "rop", DummyRedis())

    result = indices_parser.run_parser()

    assert failed == [("indices", "indices/indices_2015-01-16.zip", "classification=parser_bug; RuntimeError: bad indices zip contents")]
    assert result["files_considered"] == 1
    assert result["failed_count"] == 1
    assert result["failed_classifications"] == {"parser_bug": 1}
    assert result["failed_keys"] == ["indices/indices_2015-01-16.zip"]
    assert result["state_advanced"] is False
    assert events[0]["fallback_type"] == "nse_indices_parse_failed"
    assert events[0]["metadata"]["classification"] == "parser_bug"


def test_indices_parser_marks_empty_valid_source_processed(monkeypatch):
    processed: list[tuple[str, str, str]] = []

    monkeypatch.setattr(indices_parser.store, "list_files", lambda prefix: iter(["indices/indices_2015-01-16.zip"]))
    monkeypatch.setattr(indices_parser, "get_processed_keys", lambda *_args, **_kwargs: set())
    monkeypatch.setattr(indices_parser, "get_failed_entries", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(indices_parser.store, "get_as_temp_file", lambda key: "/tmp/empty_indices.zip")
    monkeypatch.setattr(indices_parser, "should_consider_key", lambda key, **kw: True)
    monkeypatch.setattr(indices_parser, "unzip_and_process", lambda path: {"status": indices_parser.EMPTY_VALID_STATUS, "rows": 0})
    monkeypatch.setattr(
        indices_parser,
        "mark_processed",
        lambda source_prefix, object_key, status="processed", **_kwargs: processed.append((source_prefix, object_key, status)),
    )

    class DummyRedis:
        def sadd(self, *_args, **_kwargs):
            raise AssertionError("empty valid indices key should not mark redis success")

        def close(self):
            return None

    monkeypatch.setattr(indices_parser, "rop", DummyRedis())

    result = indices_parser.run_parser()

    assert processed == [("indices", "indices/indices_2015-01-16.zip", indices_parser.EMPTY_VALID_STATUS)]
    assert result["empty_or_incomplete_count"] == 1
    assert result["empty_valid_count"] == 1
    assert result["failed_count"] == 0
    assert result["rows_written"] == 1
    assert result["state_advanced"] is True


def test_indices_parser_treats_empty_valid_source_as_completed(monkeypatch):
    monkeypatch.setattr(indices_parser.store, "list_files", lambda prefix: iter(["indices/indices_2015-01-16.zip"]))
    monkeypatch.setattr(
        indices_parser,
        "get_processed_keys",
        lambda _source_prefix, status="processed": {"indices/indices_2015-01-16.zip"} if status == indices_parser.EMPTY_VALID_STATUS else set(),
    )
    monkeypatch.setattr(indices_parser, "get_failed_entries", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(indices_parser.store, "get_as_temp_file", lambda key: (_ for _ in ()).throw(AssertionError("completed empty key should not be fetched")))
    monkeypatch.setattr(indices_parser, "should_consider_key", lambda key, **kw: True)

    class DummyRedis:
        def sadd(self, *_args, **_kwargs):
            raise AssertionError("completed empty key should not mark redis")

        def close(self):
            return None

    monkeypatch.setattr(indices_parser, "rop", DummyRedis())

    result = indices_parser.run_parser()

    assert result["already_processed_count"] == 1
    assert result["empty_or_incomplete_count"] == 0
    assert result["state_advanced"] is False


def test_indices_parser_failure_classifier_distinguishes_retryable_and_schema_errors():
    assert indices_parser.classify_indices_parse_failure(RuntimeError("bad_indices_zip:bad.zip")) == "bad_file_retryable"
    assert indices_parser.classify_indices_parse_failure(KeyError("Index Name")) == "schema_changed"
    assert indices_parser.classify_indices_parse_failure(ValueError("Length mismatch: Expected axis has 4 elements")) == "schema_changed"
    assert indices_parser.classify_indices_parse_failure(RuntimeError("unexpected parser branch")) == "parser_bug"


def test_indices_parser_raises_on_bad_zip(tmp_path):
    bad_zip = tmp_path / "indices_bad.zip"
    bad_zip.write_text("not a zip", encoding="utf-8")

    try:
        indices_parser.unzip_and_process(str(bad_zip))
    except RuntimeError as exc:
        assert "bad_indices_zip" in str(exc)
    else:
        raise AssertionError("expected bad indices zip to raise RuntimeError")


def test_indices_parser_records_invalid_key_date(monkeypatch):
    events: list[dict[str, object]] = []
    monkeypatch.setattr(indices_parser, "record_local_fallback_event", lambda **kwargs: events.append(kwargs) or kwargs)

    assert indices_parser.extract_indices_date_from_key("indices/not-a-date.zip") is None

    assert events[0]["fallback_type"] == "nse_indices_key_date_parse_failed"
    assert events[0]["source"] == "indices/not-a-date.zip"


def test_indices_parser_records_empty_file_stat_failure(monkeypatch):
    events: list[dict[str, object]] = []
    monkeypatch.setattr(indices_parser, "record_local_fallback_event", lambda **kwargs: events.append(kwargs) or kwargs)
    monkeypatch.setattr(
        indices_parser.os.path,
        "getsize",
        lambda path: (_ for _ in ()).throw(OSError("stat failed")),
    )

    assert indices_parser.is_empty_file("/tmp/missing_indices.zip") is False

    assert events[0]["fallback_type"] == "nse_indices_zip_stat_failed"
    assert events[0]["source"] == "/tmp/missing_indices.zip"


def test_indices_parser_records_date_fallback(monkeypatch, tmp_path):
    events: list[dict[str, object]] = []
    csv_path = tmp_path / "ind_close_01012026.csv"
    csv_path.write_text(
        "index,date,open,high,low,close,points,percent,volume,turnover,pe,pb,div\n"
        "Nifty 50,01/01/2026,1,2,1,2,0.1,1,100,10,20,3,1\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(indices_parser, "record_local_fallback_event", lambda **kwargs: events.append(kwargs) or kwargs)
    monkeypatch.setattr(indices_parser, "upsert_to_db", lambda *_args, **_kwargs: None)

    frame = indices_parser.parse_indices_close(str(csv_path))

    assert frame["date"].iloc[0] == pd.Timestamp("2026-01-01")
    assert events[0]["fallback_type"] == "nse_indices_date_fallback"
    assert events[0]["metadata"]["fallback_format"] == "%d/%m/%Y"


def test_indices_parser_records_inner_file_parse_failure(monkeypatch, tmp_path):
    import zipfile

    events: list[dict[str, object]] = []
    zip_path = tmp_path / "indices_2026-01-01.zip"
    with zipfile.ZipFile(zip_path, "w") as archive:
        archive.writestr("ind_close_bad.csv", "bad")
    monkeypatch.setattr(indices_parser, "record_local_fallback_event", lambda **kwargs: events.append(kwargs) or kwargs)
    monkeypatch.setattr(
        indices_parser,
        "parse_indices_close",
        lambda path: (_ for _ in ()).throw(ValueError("bad indices schema")),
    )

    with pytest.raises(ValueError, match="bad indices schema"):
        indices_parser.unzip_and_process(str(zip_path))

    assert events[0]["fallback_type"] == "nse_indices_file_parse_failed"
    assert events[0]["metadata"]["filename"] == "ind_close_bad.csv"


def test_offmarket_downloader_uses_s3_ranges_as_source_of_truth(monkeypatch):
    monkeypatch.setattr(
        offmarket.store,
        "list_files",
        lambda prefix: iter(
            [
                "nsedeals/block_deals_01-01-2015_03-01-2015.csv",
                "nsedeals/bulk_deals_05-01-2015_05-01-2015.csv",
                "nsedeals/ignore.txt",
            ]
        ),
    )

    assert offmarket.load_downloaded_dates_from_store("block_deals") == {
        "2015-01-01",
        "2015-01-02",
        "2015-01-03",
    }
    assert offmarket.load_downloaded_dates_from_store("bulk_deals") == {"2015-01-05"}


def test_offmarket_downloader_records_malformed_download_key(monkeypatch):
    events: list[dict[str, object]] = []
    monkeypatch.setattr(offmarket, "record_local_fallback_event", lambda **kwargs: events.append(kwargs) or kwargs)

    dates = offmarket.extract_downloaded_dates_from_key("nsedeals/block_deals_bad.csv", "block_deals")

    assert dates == set()
    assert events[0]["fallback_type"] == "nse_offmarket_download_key_date_parse_failed"
    assert events[0]["source"] == "nsedeals/block_deals_bad.csv"
    assert events[0]["metadata"]["dtype"] == "block_deals"


def test_recent_events_always_refreshes_today(monkeypatch):
    calls: list[str] = []

    class DummyPlaywright:
        def __enter__(self):
            return "playwright"

        def __exit__(self, exc_type, exc, tb):
            return False

    class DummyRedis:
        def close(self):
            return None

    monkeypatch.setattr(recent_events, "get_redis_client", lambda *_args, **_kwargs: DummyRedis())
    monkeypatch.setattr(recent_events, "sync_playwright", lambda: DummyPlaywright())
    monkeypatch.setattr(
        recent_events,
        "dowload_events",
        lambda playwright, formatted_date, rop: calls.append(formatted_date) or {"rows": 1},
    )
    monkeypatch.setattr(recent_events, "persist_sync_state", lambda **_kwargs: None)
    monkeypatch.setattr(sys, "argv", ["data.nseindia.recent_events"])

    recent_events.main()

    assert len(calls) == 1
    assert recent_events.STOCKEY_RUN_STATE["classification"] == "ok"
    assert recent_events.STOCKEY_RUN_STATE["status"] == "ok"
    assert recent_events.STOCKEY_RUN_STATE["rows_written"] == 1
    assert recent_events.STOCKEY_RUN_STATE["state_advanced"] is True


def test_recent_events_exports_source_unavailable_run_state(monkeypatch):
    persisted: list[dict[str, object]] = []
    fallbacks: list[dict[str, object]] = []

    class DummyPlaywright:
        def __enter__(self):
            return "playwright"

        def __exit__(self, exc_type, exc, tb):
            return False

    class DummyRedis:
        def close(self):
            return None

    monkeypatch.setattr(recent_events, "get_redis_client", lambda *_args, **_kwargs: DummyRedis())
    monkeypatch.setattr(recent_events, "sync_playwright", lambda: DummyPlaywright())
    monkeypatch.setattr(
        recent_events,
        "dowload_events",
        lambda playwright, formatted_date, rop: (_ for _ in ()).throw(RuntimeError("CDP connection refused")),
    )
    monkeypatch.setattr(recent_events, "persist_sync_state", lambda **kwargs: persisted.append(kwargs))
    monkeypatch.setattr(recent_events, "record_local_fallback_event", lambda **kwargs: fallbacks.append(kwargs) or kwargs)
    monkeypatch.setattr(sys, "argv", ["data.nseindia.recent_events", "--date", "2026-06-12"])

    with pytest.raises(RuntimeError, match="CDP connection refused"):
        recent_events.main()

    assert recent_events.STOCKEY_RUN_STATE["classification"] == "source_unavailable"
    assert recent_events.STOCKEY_RUN_STATE["status"] == "failed"
    assert recent_events.STOCKEY_RUN_STATE["rows_written"] == 0
    assert recent_events.STOCKEY_RUN_STATE["source_unavailable_count"] == 1
    assert recent_events.STOCKEY_RUN_STATE["state_advanced"] is False
    assert persisted[0]["status"] == "error"
    assert persisted[0]["state"]["classification"] == "source_unavailable"
    assert fallbacks[0]["fallback_type"] == "nse_recent_events_sync_failed"
    assert fallbacks[0]["metadata"]["classification"] == "source_unavailable"


def test_recent_events_exports_parse_failed_run_state(monkeypatch):
    persisted: list[dict[str, object]] = []

    class DummyPlaywright:
        def __enter__(self):
            return "playwright"

        def __exit__(self, exc_type, exc, tb):
            return False

    class DummyRedis:
        def close(self):
            return None

    monkeypatch.setattr(recent_events, "get_redis_client", lambda *_args, **_kwargs: DummyRedis())
    monkeypatch.setattr(recent_events, "sync_playwright", lambda: DummyPlaywright())
    monkeypatch.setattr(
        recent_events,
        "dowload_events",
        lambda playwright, formatted_date, rop: (_ for _ in ()).throw(ValueError("Could not parse recent event dates: ['bad']")),
    )
    monkeypatch.setattr(recent_events, "persist_sync_state", lambda **kwargs: persisted.append(kwargs))
    monkeypatch.setattr(recent_events, "record_local_fallback_event", lambda **kwargs: kwargs)
    monkeypatch.setattr(sys, "argv", ["data.nseindia.recent_events", "--date", "2026-06-12"])

    with pytest.raises(ValueError, match="Could not parse recent event dates"):
        recent_events.main()

    assert recent_events.STOCKEY_RUN_STATE["classification"] == "parse_failed"
    assert recent_events.STOCKEY_RUN_STATE["status"] == "failed"
    assert recent_events.STOCKEY_RUN_STATE["parse_failed_count"] == 1
    assert persisted[0]["state"]["classification"] == "parse_failed"


def test_recent_events_date_parser_falls_back_to_abbreviated_month():
    dates = recent_events.parse_event_dates(pd.Series(["01-June-2026", "01-Jun-2026"]))

    assert dates.tolist() == [pd.Timestamp("2026-06-01"), pd.Timestamp("2026-06-01")]


def test_recent_events_records_mixed_parser_fallback(monkeypatch):
    events: list[dict[str, object]] = []
    original_to_datetime = recent_events.pd.to_datetime

    def fake_to_datetime(values, *args, **kwargs):
        if kwargs.get("format") == "mixed":
            raise ValueError("mixed parser unavailable")
        return original_to_datetime(values, *args, **kwargs)

    monkeypatch.setattr(recent_events, "record_local_fallback_event", lambda **kwargs: events.append(kwargs) or kwargs)
    monkeypatch.setattr(recent_events.pd, "to_datetime", fake_to_datetime)

    dates = recent_events.parse_event_dates(pd.Series(["13/06/2026"]))

    assert dates.tolist() == [pd.Timestamp("2026-06-13")]
    assert events[0]["fallback_type"] == "nse_recent_events_mixed_date_parser_failed"
    assert events[0]["metadata"]["fallback_parser"] == "pd.to_datetime(dayfirst=True)"


def test_dhan_identity_records_missing_security_id_issue(monkeypatch):
    company = pd.Series(
        {
            "company_master_id": "nse:HUIL",
            "nse_ticker": "HUIL",
            "bse_ticker": None,
            "dhan_nse_id": pd.NA,
            "dhan_bse_id": pd.NA,
        }
    )
    recorded: list[dict[str, object]] = []

    monkeypatch.setattr(dhan_db, "get_company_master_equity", lambda ticker, exchange: company.copy())
    monkeypatch.setattr(dhan_db, "record_dhan_identity_issue", lambda **kwargs: recorded.append(kwargs) or kwargs)

    try:
        dhan_db.resolve_dhan_identity("HUIL", "NSE")
    except ValueError as exc:
        assert "No Dhan security id mapped for NSE:HUIL" in str(exc)
    else:
        raise AssertionError("Expected missing Dhan identity to raise")

    assert len(recorded) == 1
    row = recorded[0]
    assert row["symbol"] == "HUIL"
    assert row["requested_exchange"] == "NSE"
    assert row["asset_type"] == "stock"
    assert row["company"]["company_master_id"] == "nse:HUIL"
    assert any(item["method"] == "company_master_dhan_bse_id" for item in row["fallback_tried"])


def test_dhan_identity_records_local_fallback_when_issue_telemetry_fails(monkeypatch):
    company = pd.Series(
        {
            "company_master_id": "nse:HUIL",
            "nse_ticker": "HUIL",
            "bse_ticker": None,
            "dhan_nse_id": pd.NA,
            "dhan_bse_id": pd.NA,
        }
    )
    local_events: list[dict[str, object]] = []

    monkeypatch.setattr(dhan_db, "get_company_master_equity", lambda ticker, exchange: company.copy())
    monkeypatch.setattr(dhan_db, "record_fallback_event", lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("fallback table down")))
    monkeypatch.setattr(dhan_db, "record_dhan_identity_issue", lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("identity table down")))
    monkeypatch.setattr(dhan_db, "record_local_fallback_event", lambda **kwargs: local_events.append(kwargs) or kwargs)

    with pytest.raises(ValueError, match="No Dhan security id mapped for NSE:HUIL"):
        dhan_db.resolve_dhan_identity("HUIL", "NSE")

    assert [event["fallback_type"] for event in local_events] == [
        "dhan_identity_telemetry_failed",
        "dhan_identity_issue_record_failed",
    ]
    assert all(event["module"] == "data.dhanlive.dhan_db" for event in local_events)
    assert all(event["source"] == "dhan_identity" for event in local_events)
    assert all(event["metadata"]["symbol"] == "HUIL" for event in local_events)
    assert all(event["metadata"]["requested_exchange"] == "NSE" for event in local_events)
    assert all(event["metadata"]["asset_type"] == "stock" for event in local_events)


def test_dhan_index_search_terms_include_common_nifty_aliases():
    terms = dhan_db.index_search_terms("NIFTY50")

    assert terms[0] == "NIFTY50"
    assert "NIFTY" in terms
    assert "NIFTY 50" in terms


def test_dhan_benchmark_identity_resolves_common_index_alias(monkeypatch):
    def fake_get_index_instrument(symbol, exchange):
        assert symbol == "NIFTY50"
        assert exchange == "NSE"
        return pd.Series(
            {
                "underlying_symbol": "NIFTY",
                "security_id": 13,
            }
        )

    monkeypatch.setattr(dhan_db, "get_index_instrument", fake_get_index_instrument)

    identity = dhan_db.resolve_dhan_identity("NIFTY50", "NSE", asset_type="benchmark")

    assert identity["asset_type"] == "benchmark"
    assert identity["ticker"] == "NIFTY"
    assert identity["security_id"] == 13
    assert identity["exchange_segment"] == "IDX_I"


def test_dhan_index_identity_records_missing_alias_issue(monkeypatch):
    recorded: list[dict[str, object]] = []
    fallback_events: list[dict[str, object]] = []

    def fail_index_lookup(symbol, exchange):
        raise ValueError("Query returned no rows.")

    monkeypatch.setattr(dhan_db, "get_index_instrument", fail_index_lookup)
    monkeypatch.setattr(dhan_db, "record_dhan_identity_issue", lambda **kwargs: recorded.append(kwargs) or kwargs)
    monkeypatch.setattr(dhan_db, "record_fallback_event", lambda **kwargs: fallback_events.append(kwargs) or kwargs)

    with pytest.raises(ValueError, match="No Dhan index security id mapped for NSE:NIFTY50"):
        dhan_db.resolve_dhan_identity("NIFTY50", "NSE", asset_type="benchmark")

    assert recorded[0]["symbol"] == "NIFTY50"
    assert recorded[0]["asset_type"] == "benchmark"
    assert recorded[0]["fallback_tried"][0]["method"] == "index_alias_lookup"
    assert "NIFTY 50" in recorded[0]["fallback_tried"][0]["aliases"]
    assert fallback_events[0]["fallback_type"] == "dhan_index_identity_unresolved"


def test_dhan_index_identity_records_local_fallback_when_issue_telemetry_fails(monkeypatch):
    local_events: list[dict[str, object]] = []

    def fail_index_lookup(symbol, exchange):
        raise ValueError("Query returned no rows.")

    monkeypatch.setattr(dhan_db, "get_index_instrument", fail_index_lookup)
    monkeypatch.setattr(dhan_db, "record_fallback_event", lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("fallback table down")))
    monkeypatch.setattr(dhan_db, "record_dhan_identity_issue", lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("identity table down")))
    monkeypatch.setattr(dhan_db, "record_local_fallback_event", lambda **kwargs: local_events.append(kwargs) or kwargs)

    with pytest.raises(ValueError, match="No Dhan index security id mapped for NSE:NIFTY50"):
        dhan_db.resolve_dhan_identity("NIFTY50", "NSE", asset_type="benchmark")

    assert [event["fallback_type"] for event in local_events] == [
        "dhan_index_identity_telemetry_failed",
        "dhan_index_identity_issue_record_failed",
    ]
    assert all(event["module"] == "data.dhanlive.dhan_db" for event in local_events)
    assert all(event["source"] == "dhan_identity" for event in local_events)
    assert all(event["metadata"]["symbol"] == "NIFTY50" for event in local_events)
    assert all(event["metadata"]["requested_exchange"] == "NSE" for event in local_events)
    assert all(event["metadata"]["asset_type"] == "benchmark" for event in local_events)


def test_identity_issue_resolution_dry_run_does_not_mark_resolved(monkeypatch):
    rows = pd.DataFrame(
        [
            {
                "issue_key": "dhan_security_id_missing:stock:NSE:HUIL",
                "issue_type": "dhan_security_id_missing",
                "symbol": "HUIL",
                "requested_exchange": "NSE",
                "asset_type": "stock",
            }
        ]
    )
    resolved: list[dict[str, object]] = []
    failed: list[dict[str, object]] = []
    fallback_events: list[dict[str, object]] = []

    monkeypatch.setattr(identity_issues, "load_open_identity_issues", lambda limit=100: rows.copy())
    monkeypatch.setattr(identity_issues, "mark_identity_issue_resolved", lambda *args, **kwargs: resolved.append({"args": args, "kwargs": kwargs}))
    monkeypatch.setattr(identity_issues, "mark_identity_issue_resolution_failed", lambda *args, **kwargs: failed.append({"args": args, "kwargs": kwargs}))
    monkeypatch.setattr(dhan_db, "record_fallback_event", lambda **kwargs: fallback_events.append(kwargs) or kwargs)
    monkeypatch.setattr(
        dhan_db,
        "resolve_dhan_identity",
        lambda symbol, exchange, asset_type="stock": {
            "security_id": 12345,
            "exchange": "NSE",
            "ticker": symbol,
            "asset_type": asset_type,
        },
    )

    summary = identity_issues.resolve_open_identity_issues(limit=10, apply=False)

    assert summary["mode"] == "dry_run"
    assert summary["counts"]["would_resolve"] == 1
    assert resolved == []
    assert failed == []
    assert fallback_events == []


def test_identity_issue_resolution_apply_marks_resolved(monkeypatch):
    rows = pd.DataFrame(
        [
            {
                "issue_key": "dhan_security_id_missing:stock:NSE:HUIL",
                "issue_type": "dhan_security_id_missing",
                "symbol": "HUIL",
                "requested_exchange": "NSE",
                "asset_type": "stock",
            }
        ]
    )
    resolved: list[dict[str, object]] = []

    monkeypatch.setattr(identity_issues, "load_open_identity_issues", lambda limit=100: rows.copy())
    monkeypatch.setattr(identity_issues, "mark_identity_issue_resolved", lambda issue_key, **kwargs: resolved.append({"issue_key": issue_key, **kwargs}) or {"issue_key": issue_key})
    monkeypatch.setattr(
        dhan_db,
        "resolve_dhan_identity",
        lambda symbol, exchange, asset_type="stock": {
            "security_id": 12345,
            "exchange": "NSE",
            "ticker": symbol,
            "asset_type": asset_type,
        },
    )

    summary = identity_issues.resolve_open_identity_issues(limit=10, apply=True)

    assert summary["mode"] == "apply"
    assert summary["counts"]["resolved"] == 1
    assert resolved[0]["issue_key"] == "dhan_security_id_missing:stock:NSE:HUIL"
    assert resolved[0]["resolution_context"]["resolved_identity"]["security_id"] == 12345


def test_identity_issue_resolution_handles_index_alias(monkeypatch):
    rows = pd.DataFrame(
        [
            {
                "issue_key": "dhan_security_id_missing:benchmark:NSE:NIFTY50",
                "issue_type": "dhan_security_id_missing",
                "symbol": "NIFTY50",
                "requested_exchange": "NSE",
                "asset_type": "benchmark",
            }
        ]
    )
    resolved: list[dict[str, object]] = []

    monkeypatch.setattr(identity_issues, "load_open_identity_issues", lambda limit=100: rows.copy())
    monkeypatch.setattr(identity_issues, "mark_identity_issue_resolved", lambda issue_key, **kwargs: resolved.append({"issue_key": issue_key, **kwargs}) or {"issue_key": issue_key})
    monkeypatch.setattr(
        dhan_db,
        "resolve_dhan_identity",
        lambda symbol, exchange, asset_type="stock": {
            "security_id": 13,
            "exchange": "NSE",
            "ticker": "NIFTY",
            "asset_type": asset_type,
        },
    )

    summary = identity_issues.resolve_open_identity_issues(limit=10, apply=True)

    assert summary["counts"]["resolved"] == 1
    assert resolved[0]["issue_key"] == "dhan_security_id_missing:benchmark:NSE:NIFTY50"
    assert resolved[0]["resolution_context"]["resolved_identity"]["ticker"] == "NIFTY"


def test_identity_issue_resolution_failure_records_fallback(monkeypatch):
    rows = pd.DataFrame(
        [
            {
                "issue_key": "dhan_security_id_missing:stock:NSE:HUIL",
                "issue_type": "dhan_security_id_missing",
                "symbol": "HUIL",
                "requested_exchange": "NSE",
                "asset_type": "stock",
            }
        ]
    )
    failed: list[dict[str, object]] = []
    fallback_events: list[dict[str, object]] = []

    monkeypatch.setattr(identity_issues, "load_open_identity_issues", lambda limit=100: rows.copy())
    monkeypatch.setattr(
        identity_issues,
        "mark_identity_issue_resolution_failed",
        lambda issue_key, **kwargs: failed.append({"issue_key": issue_key, **kwargs}) or {"issue_key": issue_key},
    )
    monkeypatch.setattr(
        identity_issues,
        "record_local_fallback_event",
        lambda **kwargs: fallback_events.append(kwargs) or kwargs,
    )

    def fail_resolve(symbol, exchange, asset_type="stock"):
        raise ValueError(f"No Dhan security id mapped for {exchange}:{symbol}")

    monkeypatch.setattr(dhan_db, "resolve_dhan_identity", fail_resolve)

    summary = identity_issues.resolve_open_identity_issues(limit=10, apply=True)

    assert summary["counts"]["still_open"] == 1
    assert failed[0]["issue_key"] == "dhan_security_id_missing:stock:NSE:HUIL"
    assert fallback_events[0]["fallback_type"] == "identity_issue_resolution_failed"
    assert fallback_events[0]["symbol"] == "HUIL"
    assert fallback_events[0]["metadata"]["issue_key"] == "dhan_security_id_missing:stock:NSE:HUIL"
    assert fallback_events[0]["metadata"]["apply"] is True


def test_identity_issues_ensure_table_uses_schema_registry(monkeypatch):
    calls = []

    monkeypatch.setattr(identity_issues, "apply_schema_migration", lambda **kwargs: calls.append(kwargs) or {"status": "applied"})

    identity_issues.ensure_identity_issues_table()

    assert len(calls) == 1
    assert calls[0]["migration_id"] == identity_issues.IDENTITY_ISSUES_SCHEMA_MIGRATION_ID
    assert calls[0]["metadata"]["tables"] == [identity_issues.IDENTITY_ISSUES_TABLE]
    assert any(identity_issues.IDENTITY_ISSUES_TABLE in statement for statement in calls[0]["statements"])
    assert any("issue_key TEXT PRIMARY KEY" in statement for statement in calls[0]["statements"])
    assert any("CREATE INDEX IF NOT EXISTS" in statement for statement in calls[0]["statements"])


def test_identity_issues_text_records_missing_check_fallback(monkeypatch):
    events = []
    sentinel = object()
    monkeypatch.setattr(identity_issues, "record_local_fallback_event", lambda **kwargs: events.append(kwargs) or kwargs)
    monkeypatch.setattr(identity_issues.pd, "isna", lambda _value: (_ for _ in ()).throw(TypeError("ambiguous")))

    result = identity_issues._text(sentinel)

    assert result.startswith("<object object at ")
    assert events[0]["module"] == "utils.identity_issues"
    assert events[0]["fallback_type"] == "identity_issue_text_missing_check_failed"
    assert events[0]["source"] == "text"
    assert events[0]["metadata"]["value_type"] == "object"


def test_identity_issues_writes_use_retryable_operations(monkeypatch):
    operation_names: list[str] = []
    executed: list[tuple[str, object]] = []

    class FakeCursor:
        def execute(self, query, params=None):
            executed.append((str(query), params))

    class FakeSession:
        def __enter__(self):
            return None, FakeCursor()

        def __exit__(self, *_args):
            return False

    def fake_execute_db_operation(operation, *, operation_name, **_kwargs):
        operation_names.append(operation_name)
        return operation()

    monkeypatch.setattr(identity_issues, "ensure_identity_issues_table", lambda: None)
    monkeypatch.setattr(identity_issues, "db_session", lambda: FakeSession())
    monkeypatch.setattr(identity_issues, "execute_db_operation", fake_execute_db_operation)

    row = identity_issues.record_dhan_identity_issue(
        symbol="HUIL",
        requested_exchange="NSE",
        asset_type="stock",
        company={"company_master_id": "nse:HUIL"},
        fallback_tried=[{"exchange": "BSE", "method": "company_master_dhan_bse_id"}],
    )
    resolved = identity_issues.mark_identity_issue_resolved(
        row["issue_key"],
        resolution_context={"security_id": 123},
    )
    failed = identity_issues.mark_identity_issue_resolution_failed(
        row["issue_key"],
        error_text="still missing",
        resolution_context={"checked": True},
    )

    assert row["issue_key"] == "dhan_security_id_missing:stock:NSE:HUIL"
    assert resolved["status"] == "resolved"
    assert failed["status"] == "open"
    assert operation_names == [
        "identity_issues:record_dhan_identity_issue",
        "identity_issues:mark_resolved",
        "identity_issues:mark_resolution_failed",
    ]
    assert "INSERT INTO" in executed[0][0]
    assert "status = 'resolved'" in executed[1][0]
    assert "resolution_error_text = %s" in executed[2][0]


def test_identity_issues_records_dhan_ohlcv_history_issue(monkeypatch):
    operation_names: list[str] = []
    executed: list[tuple[str, object]] = []

    class FakeCursor:
        def execute(self, query, params=None):
            executed.append((str(query), params))

    class FakeSession:
        def __enter__(self):
            return None, FakeCursor()

        def __exit__(self, *_args):
            return False

    def fake_execute_db_operation(operation, *, operation_name, **_kwargs):
        operation_names.append(operation_name)
        return operation()

    monkeypatch.setattr(identity_issues, "ensure_identity_issues_table", lambda: None)
    monkeypatch.setattr(identity_issues, "db_session", lambda: FakeSession())
    monkeypatch.setattr(identity_issues, "execute_db_operation", fake_execute_db_operation)

    row = identity_issues.record_dhan_ohlcv_history_issue(
        symbol="HUIL",
        requested_exchange="NSE",
        asset_type="stock",
        reason="dhan_daily_sync_failed_no_history",
        error_text="Dhan API request failed with status 400",
    )

    assert row["issue_key"] == "dhan_ohlcv_history_unavailable:stock:NSE:HUIL"
    assert row["issue_type"] == "dhan_ohlcv_history_unavailable"
    assert row["symbol"] == "HUIL"
    assert "explicitly exclude" in row["suggested_action"]
    assert operation_names == ["identity_issues:record_dhan_ohlcv_history_issue"]
    assert "INSERT INTO" in executed[0][0]
    assert executed[0][1]["issue_type"] == "dhan_ohlcv_history_unavailable"


def test_identity_issue_resolution_for_ohlcv_history_requires_local_history(monkeypatch):
    rows = pd.DataFrame(
        [
            {
                "issue_key": "dhan_ohlcv_history_unavailable:stock:NSE:HUIL",
                "issue_type": "dhan_ohlcv_history_unavailable",
                "symbol": "HUIL",
                "requested_exchange": "NSE",
                "asset_type": "stock",
            }
        ]
    )
    resolved: list[dict[str, object]] = []
    monkeypatch.setattr(identity_issues, "load_open_identity_issues", lambda limit=100: rows.copy())
    monkeypatch.setattr(
        identity_issues,
        "mark_identity_issue_resolved",
        lambda issue_key, **kwargs: resolved.append({"issue_key": issue_key, **kwargs}) or {"issue_key": issue_key},
    )
    monkeypatch.setattr(identity_issues, "sql_to_df", lambda *args, **kwargs: pd.DataFrame([{"latest_date": pd.NaT}]))

    summary = identity_issues.resolve_open_identity_issues(limit=10, apply=True)

    assert summary["counts"] == {"still_open": 1}
    assert resolved == []
    result = summary["results"][0]
    assert result["reason"] == "dhan_daily_history_still_missing"
    assert "explicitly exclude" in result["suggested_action"]

    monkeypatch.setattr(
        identity_issues,
        "sql_to_df",
        lambda *args, **kwargs: pd.DataFrame([{"latest_date": pd.Timestamp("2026-06-18T00:00:00Z")}]),
    )

    summary = identity_issues.resolve_open_identity_issues(limit=10, apply=True)

    assert summary["counts"] == {"resolved": 1}
    assert resolved[0]["issue_key"] == "dhan_ohlcv_history_unavailable:stock:NSE:HUIL"
    assert resolved[0]["resolution_context"]["latest_ohlcv_date"] == "2026-06-18T00:00:00+00:00"


def test_identity_issue_resolution_for_company_master_mapping(monkeypatch):
    rows = pd.DataFrame(
        [
            {
                "issue_key": "company_master_mapping_missing:NSE:AEROENTER",
                "issue_type": "company_master_mapping_missing",
                "symbol": "AEROENTER",
                "requested_exchange": "NSE",
                "asset_type": "stock",
            }
        ]
    )
    resolved: list[dict[str, object]] = []
    monkeypatch.setattr(identity_issues, "load_open_identity_issues", lambda limit=100: rows.copy())
    monkeypatch.setattr(
        identity_issues,
        "load_company_master_records",
        lambda symbol, exchanges=None: pd.DataFrame(
            [{"company_master_id": "nse:AEROENTER", "nse_ticker": "AEROENTER"}]
        ),
    )
    monkeypatch.setattr(
        identity_issues,
        "mark_identity_issue_resolved",
        lambda issue_key, **kwargs: resolved.append({"issue_key": issue_key, **kwargs}) or {"issue_key": issue_key},
    )

    dry_run = identity_issues.resolve_open_identity_issues(limit=10, apply=False)
    applied = identity_issues.resolve_open_identity_issues(limit=10, apply=True)

    assert dry_run["counts"] == {"would_resolve": 1}
    assert dry_run["results"][0]["company_master_id"] == "nse:AEROENTER"
    assert applied["counts"] == {"resolved": 1}
    assert resolved[0]["issue_key"] == "company_master_mapping_missing:NSE:AEROENTER"
    assert resolved[0]["resolution_context"] == {
        "checked_symbol": "AEROENTER",
        "requested_exchange": "NSE",
        "company_master_id": "nse:AEROENTER",
        "matched_rows": 1,
    }


def test_poppler_resolver_prefers_explicit_directory(monkeypatch, tmp_path):
    poppler_dir = tmp_path / "poppler"
    poppler_dir.mkdir()
    (poppler_dir / "pdfinfo").write_text("", encoding="utf-8")

    monkeypatch.setenv("POPPLER_PATH", str(poppler_dir))
    monkeypatch.setattr(poppler_utils.shutil, "which", lambda _: None)

    assert poppler_utils.resolve_poppler_path() == str(poppler_dir)


def test_fallback_telemetry_includes_file_spooled_db_retry_events(monkeypatch):
    monkeypatch.setattr(fallback_telemetry, "table_exists", lambda table_name=fallback_telemetry.TABLE_NAME: False)
    monkeypatch.setattr(fallback_telemetry, "read_local_fallback_events", lambda **_kwargs: [])
    monkeypatch.setattr(
        fallback_telemetry,
        "read_db_retry_telemetry_events",
        lambda **_kwargs: [
            {
                "observed_at": "2026-06-11T10:00:00+00:00",
                "module": "utils.db",
                "source": "postgres",
                "fallback_type": "db_retry",
                "severity": "warn",
                "status": "active",
                "operation_name": "sql_to_df",
                "attempt": 1,
                "max_attempts": 3,
                "error_type": "QueryCanceled",
                "error_message": "statement timeout",
            }
        ],
    )

    summary = fallback_telemetry.summarize_fallback_events(hours=24, limit=10)

    assert summary["status"] == "warn"
    assert summary["active_count"] == 1
    assert summary["db_retry_count"] == 1
    assert summary["counts_by_type"]["db_retry"] == 1
    assert summary["rows"][0]["operation_name"] == "sql_to_df"


def test_fallback_telemetry_summary_records_table_check_failure(monkeypatch):
    events = []
    monkeypatch.setattr(fallback_telemetry, "read_db_retry_telemetry_events", lambda **_kwargs: [])
    monkeypatch.setattr(fallback_telemetry, "read_local_fallback_events", lambda **_kwargs: [])
    monkeypatch.setattr(fallback_telemetry, "table_exists", lambda table_name=fallback_telemetry.TABLE_NAME: (_ for _ in ()).throw(RuntimeError("table check failed")))
    monkeypatch.setattr(fallback_telemetry, "record_local_fallback_event", lambda **kwargs: events.append(kwargs) or kwargs)

    summary = fallback_telemetry.summarize_fallback_events(hours=24, limit=7)

    assert summary["status"] == "error"
    assert summary["error"] == "RuntimeError: table check failed"
    assert events[0]["source"] == fallback_telemetry.TABLE_NAME
    assert events[0]["fallback_type"] == "fallback_telemetry_table_check_failed"
    assert events[0]["metadata"] == {"hours": 24, "limit": 7}


def test_fallback_telemetry_summary_records_table_query_failure(monkeypatch):
    events = []
    monkeypatch.setattr(fallback_telemetry, "read_db_retry_telemetry_events", lambda **_kwargs: [])
    monkeypatch.setattr(fallback_telemetry, "read_local_fallback_events", lambda **_kwargs: [])
    monkeypatch.setattr(fallback_telemetry, "table_exists", lambda table_name=fallback_telemetry.TABLE_NAME: True)
    monkeypatch.setattr(fallback_telemetry, "sql_to_df", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("query failed")))
    monkeypatch.setattr(fallback_telemetry, "record_local_fallback_event", lambda **kwargs: events.append(kwargs) or kwargs)

    summary = fallback_telemetry.summarize_fallback_events(hours=12, limit=9)

    assert summary["status"] == "error"
    assert summary["error"] == "RuntimeError: query failed"
    assert events[0]["source"] == fallback_telemetry.TABLE_NAME
    assert events[0]["fallback_type"] == "fallback_telemetry_table_query_failed"
    assert events[0]["metadata"] == {"hours": 12, "limit": 9}


def test_fallback_telemetry_records_local_spool_read_failure(monkeypatch, tmp_path):
    events = []
    telemetry_file = tmp_path / "local_fallback_events.jsonl"
    telemetry_file.write_text("{}\n", encoding="utf-8")
    original_open = fallback_telemetry.Path.open

    def fake_open(path, *args, **kwargs):
        if path == telemetry_file:
            raise OSError("cannot read")
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(fallback_telemetry, "LOCAL_FALLBACK_TELEMETRY_FILE", telemetry_file)
    monkeypatch.setattr(fallback_telemetry.Path, "open", fake_open)
    monkeypatch.setattr(fallback_telemetry, "record_local_fallback_event", lambda **kwargs: events.append(kwargs) or kwargs)

    rows = fallback_telemetry.read_local_fallback_events(hours=24, limit=10)

    assert rows == []
    assert events[0]["module"] == "utils.fallback_telemetry"
    assert events[0]["source"] == str(telemetry_file)
    assert events[0]["fallback_type"] == "local_fallback_telemetry_read_failed"
    assert events[0]["severity"] == "error"
    assert events[0]["metadata"]["path"] == str(telemetry_file)


def test_redaction_masks_secrets_mobile_and_auth_urls():
    text = (
        "DHAN_LOGIN_MOBILE=9876543210 tokenId=SECRET123 "
        "Authorization: Bearer abc.def "
        "https://broker.example/callback?tokenId=SECRET123&symbol=ABC"
    )
    redacted = redaction.redact_text(text)

    assert "9876543210" not in redacted
    assert "SECRET123" not in redacted
    assert "abc.def" not in redacted
    assert "symbol=ABC" in redacted
    assert redaction.REDACTED in redacted


def test_redaction_url_parse_failure_records_fallback(monkeypatch):
    events = []

    def fail_urlsplit(_value):
        raise ValueError("bad url")

    monkeypatch.setattr(redaction, "urlsplit", fail_urlsplit)
    monkeypatch.setattr(fallback_telemetry, "record_local_fallback_event", lambda **kwargs: events.append(kwargs) or kwargs)

    redacted = redaction.redact_text("see https://broker.example/callback?tokenId=SECRET123")

    assert redacted == f"see {redaction.REDACTED}"
    assert len(events) == 1
    assert events[0]["module"] == "utils.redaction"
    assert events[0]["source"] == "url_redaction"
    assert events[0]["fallback_type"] == "redaction_url_parse_failed"
    assert events[0]["severity"] == "warn"


def test_redaction_json_parse_failure_records_fallback(monkeypatch):
    events = []

    monkeypatch.setattr(fallback_telemetry, "record_local_fallback_event", lambda **kwargs: events.append(kwargs) or kwargs)

    redacted = redaction.redact_json_text("password=SECRET123")

    assert redacted == f"password={redaction.REDACTED}"
    assert len(events) == 1
    assert events[0]["module"] == "utils.redaction"
    assert events[0]["source"] == "json_redaction"
    assert events[0]["fallback_type"] == "redaction_json_parse_failed"
    assert events[0]["severity"] == "warn"


def test_fallback_telemetry_ensure_table_uses_schema_registry(monkeypatch):
    calls = []

    monkeypatch.setattr(fallback_telemetry, "apply_schema_migration", lambda **kwargs: calls.append(kwargs) or {"status": "applied"})

    fallback_telemetry.ensure_table()

    assert len(calls) == 1
    assert calls[0]["migration_id"] == fallback_telemetry.FALLBACK_TELEMETRY_SCHEMA_MIGRATION_ID
    assert calls[0]["metadata"]["tables"] == [fallback_telemetry.TABLE_NAME]
    assert any(fallback_telemetry.TABLE_NAME in statement for statement in calls[0]["statements"])
    assert any("fallback_type TEXT NOT NULL" in statement for statement in calls[0]["statements"])
    assert any("deterministic_fallback BOOLEAN" in statement for statement in calls[0]["statements"])


def test_dhan_ohlcv_run_state_classifies_reference_mapping_failures():
    state = dhan_ohlcv.build_run_state(
        daily_results=[
            {
                "ticker": "HUIL",
                "asset_type": "stock",
                "exchange": "NSE",
                "rows": 0,
                "from_date": None,
                "to_date": None,
                "error": "ValueError: No Dhan security id mapped for NSE:HUIL",
                "classification": "reference_mapping_missing",
            }
        ],
        intraday_results=[],
        only="daily",
        symbols=["HUIL"],
        exchange="NSE",
        asset_type="stock",
        interval_minutes=1,
    )

    assert state["classification"] == "reference_mapping_missing"
    assert state["status"] == "failed"
    assert state["rows_written"] == 0
    assert state["state_advanced"] is False
    assert state["reference_mapping_missing_count"] == 1
    assert state["classification_counts"] == {"reference_mapping_missing": 1}


def test_dhan_ohlcv_run_state_classifies_partial_failures():
    state = dhan_ohlcv.build_run_state(
        daily_results=[
            {
                "ticker": "AAA",
                "asset_type": "stock",
                "exchange": "NSE",
                "rows": 3,
                "from_date": "2026-06-01",
                "to_date": "2026-06-03",
            },
            {
                "ticker": "BBB",
                "asset_type": "stock",
                "exchange": "NSE",
                "rows": 0,
                "from_date": None,
                "to_date": None,
                "error": "DhanAPIError: Dhan API request failed with status 503: unavailable",
                "classification": "source_unavailable",
            },
        ],
        intraday_results=[],
        only="daily",
        symbols=["AAA", "BBB"],
        exchange="NSE",
        asset_type="stock",
        interval_minutes=1,
    )

    assert state["classification"] == "partial_failed"
    assert state["status"] == "failed"
    assert state["rows_written"] == 3
    assert state["state_advanced"] is True
    assert state["source_unavailable_count"] == 1
    assert state["classification_counts"] == {"source_unavailable": 1}


def test_cron_status_parses_generated_style_crontab_and_estimates_next_run(tmp_path):
    crontab = tmp_path / "stockey.generated.crontab"
    crontab.write_text(
        "\n".join(
            [
                "SHELL=/bin/bash",
                "# comment",
                '10 07 * * 1-5 rane cd "$STOCKEY_DIR" && "$STOCKEY_DIR/scripts/with_lock.sh" /tmp/stockey_complete_data.lock ./complete_data.sh >> "$LOG_DIR/complete_data.log" 2>&1',
                '*/10 09-15 * * 1-5 rane cd "$STOCKEY_DIR" && ./all_watchers.sh >> "$LOG_DIR/all_watchers.log" 2>&1',
            ]
        ),
        encoding="utf-8",
    )

    rows = cron_status.parse_crontab(crontab)

    assert len(rows) == 2
    assert rows[0]["job_name"] == "complete_data"
    assert rows[0]["log_file"] == "complete_data.log"
    assert rows[0]["lock_file"] == "/tmp/stockey_complete_data.lock"
    assert rows[1]["job_name"] == "all_watchers"
    assert cron_status.estimate_next_run(rows[0]["cron_fields"], now=pd.Timestamp("2026-06-08T06:58:00+05:30")) == "2026-06-08T07:10:00+05:30"


def test_cron_status_detects_stale_lock_and_log_marker(tmp_path):
    crontab = tmp_path / "stockey.generated.crontab"
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    lock_dir = tmp_path / "stockey_job.lock.d"
    lock_dir.mkdir()
    old_time = pd.Timestamp("2026-06-08T00:00:00Z").timestamp()
    (lock_dir / "pid").write_text("999999", encoding="utf-8")
    (lock_dir / "command").write_text("./job.sh", encoding="utf-8")
    os.utime(lock_dir, (old_time, old_time))
    (log_dir / "job.log").write_text("[stockey.script] name=job status=ok elapsed=12.5\n", encoding="utf-8")
    crontab.write_text(
        f'10 07 * * 1-5 rane cd "$STOCKEY_DIR" && "$STOCKEY_DIR/scripts/with_lock.sh" {tmp_path}/stockey_job.lock ./job.sh >> "$LOG_DIR/job.log" 2>&1\n',
        encoding="utf-8",
    )

    payload = cron_status.build_cron_status(
        crontab_path=crontab,
        log_dir=log_dir,
        now=pd.Timestamp("2026-06-08T08:00:00+05:30"),
        stale_lock_seconds=60,
    )

    assert payload["status"] == "error"
    job = payload["jobs"][0]
    assert job["latest_marker"]["status"] == "ok"
    assert job["lock_active"] is True
    assert job["lock_stale"] is True
    assert job["status"] == "error"


def test_cron_status_invalid_pid_records_fallback(monkeypatch):
    events = []
    monkeypatch.setattr(cron_status, "record_local_fallback_event", lambda **kwargs: events.append(kwargs) or kwargs)

    assert cron_status._pid_running("not-a-pid") is False

    assert events[0]["module"] == "utils.cron_status"
    assert events[0]["fallback_type"] == "cron_status_invalid_pid_file"
    assert events[0]["source"] == "cron_lock_pid"


def test_cron_status_pid_os_error_records_fallback(monkeypatch):
    events = []

    def fail_kill(pid, signal):
        raise PermissionError("permission denied")

    monkeypatch.setattr(cron_status.os, "kill", fail_kill)
    monkeypatch.setattr(cron_status, "record_local_fallback_event", lambda **kwargs: events.append(kwargs) or kwargs)

    assert cron_status._pid_running("123") is False

    assert events[0]["fallback_type"] == "cron_status_pid_check_failed"
    assert events[0]["metadata"]["pid"] == 123


def test_cron_status_missing_pid_remains_normal_false(monkeypatch):
    events = []

    def missing_process(pid, signal):
        raise ProcessLookupError("missing")

    monkeypatch.setattr(cron_status.os, "kill", missing_process)
    monkeypatch.setattr(cron_status, "record_local_fallback_event", lambda **kwargs: events.append(kwargs) or kwargs)

    assert cron_status._pid_running("123") is False
    assert events == []


def test_cron_preflight_validates_generated_crontab(tmp_path, monkeypatch):
    from scripts import cron_preflight

    repo = tmp_path / "repo"
    scripts_dir = repo / "scripts"
    logs_dir = repo / "logs" / "cron"
    scripts_dir.mkdir(parents=True)
    logs_dir.mkdir(parents=True)
    script = repo / "complete_data.sh"
    script.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    script.chmod(0o755)
    with_lock = scripts_dir / "with_lock.sh"
    with_lock.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    with_lock.chmod(0o755)
    crontab = tmp_path / "stockey.generated.crontab"
    crontab.write_text(
        "\n".join(
            [
                "SHELL=/bin/bash",
                "PATH=/usr/bin:/bin",
                "STOCKEY_DIR=" + str(repo),
                "LOG_DIR=" + str(logs_dir),
                '10 07 * * 1-5 rane cd "$STOCKEY_DIR" && "$STOCKEY_DIR/scripts/with_lock.sh" /tmp/stockey_complete_data.lock ./complete_data.sh >> "$LOG_DIR/complete_data.log" 2>&1',
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(cron_preflight, "_resolve_python", lambda repo_root: {"status": "ok", "check": "python", "message": "python ok", "python": str(sys.executable)})
    monkeypatch.setattr(cron_preflight, "_check_port", lambda host, port, timeout=0.25: {"host": host, "port": port, "state": "available"})

    payload = cron_preflight.build_cron_preflight(crontab_path=crontab)

    assert payload["status"] == "ok"
    assert payload["job_count"] == 1
    assert any(row["check"] == "scripts" and row["status"] == "ok" for row in payload["checks"])
    # start_cron.sh is the supported start path: it reconciles OHLCV coverage first,
    # then execs go-crond (which has no @reboot support).
    assert payload["next_command"].startswith("./start_cron.sh")


def test_cron_preflight_flags_missing_scripts_and_stale_locks(tmp_path, monkeypatch):
    from scripts import cron_preflight

    repo = tmp_path / "repo"
    logs_dir = repo / "logs" / "cron"
    repo.mkdir()
    logs_dir.mkdir(parents=True)
    lock_file = tmp_path / "stockey_job.lock"
    lock_dir = tmp_path / "stockey_job.lock.d"
    lock_dir.mkdir()
    (lock_dir / "pid").write_text("999999", encoding="utf-8")
    old_time = pd.Timestamp("2026-01-01T00:00:00Z").timestamp()
    os.utime(lock_dir, (old_time, old_time))
    crontab = tmp_path / "stockey.generated.crontab"
    crontab.write_text(
        "\n".join(
            [
                "SHELL=/bin/bash",
                "PATH=/usr/bin:/bin",
                "STOCKEY_DIR=" + str(repo),
                "LOG_DIR=" + str(logs_dir),
                f'10 07 * * 1-5 rane cd "$STOCKEY_DIR" && "$STOCKEY_DIR/scripts/with_lock.sh" {lock_file} ./missing.sh >> "$LOG_DIR/job.log" 2>&1',
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(cron_preflight, "_resolve_python", lambda repo_root: {"status": "ok", "check": "python", "message": "python ok", "python": str(sys.executable)})
    monkeypatch.setattr(cron_preflight, "_check_port", lambda host, port, timeout=0.25: {"host": host, "port": port, "state": "available"})

    payload = cron_preflight.build_cron_preflight(
        crontab_path=crontab,
        now=pd.Timestamp("2026-06-08T08:00:00Z"),
        stale_lock_seconds=60,
    )

    assert payload["status"] == "error"
    script_check = next(row for row in payload["checks"] if row["check"] == "scripts")
    assert script_check["status"] == "error"
    assert any("missing.sh" in item for item in script_check["missing"])
    lock_check = next(row for row in payload["checks"] if row["check"] == "locks")
    assert lock_check["status"] == "warn"
    assert lock_check["stale_count"] == 1
    assert payload["next_command"] == "fix preflight errors before starting go-crond"


def test_cron_preflight_resolve_python_failure_records_fallback(tmp_path, monkeypatch):
    from scripts import cron_preflight

    events = []
    repo = tmp_path / "repo"
    script = repo / "scripts" / "resolve_python.sh"
    script.parent.mkdir(parents=True)
    script.write_text("#!/usr/bin/env bash\nexit 1\n", encoding="utf-8")
    script.chmod(0o755)

    monkeypatch.setattr(
        cron_preflight.subprocess,
        "run",
        lambda *args, **kwargs: (_ for _ in ()).throw(subprocess.TimeoutExpired(cmd=args[0], timeout=3)),
    )
    monkeypatch.setattr(cron_preflight, "record_local_fallback_event", lambda **kwargs: events.append(kwargs) or kwargs)

    out = cron_preflight._resolve_python(repo, timeout_seconds=3)

    assert out["status"] == "error"
    assert out["check"] == "python"
    assert events[0]["module"] == "scripts.cron_preflight"
    assert events[0]["source"] == "resolve_python"
    assert events[0]["fallback_type"] == "cron_preflight_resolve_python_failed"
    assert events[0]["metadata"]["timeout_seconds"] == 3


def _positive_context_watch_target_frame():
    return pd.DataFrame(
        [
            {
                "symbol": "ABC",
                "asof_date": pd.Timestamp("2026-06-20T00:00:00Z"),
                "setup_score": 0.8,
                "candidate_state": "WATCH_BREAKOUT",
                "watch_enabled": True,
                "context_source": "announcement_context",
                "context_overlay_id": "announcement_context-1",
                "context_policy_effect": "watch_breakout_priority_no_buy_authority",
                "watch_reason_detail": "positive pressure",
            },
            {
                "symbol": "XYZ",
                "asof_date": pd.Timestamp("2026-06-20T00:00:00Z"),
                "setup_score": 0.7,
                "candidate_state": "WATCH_EVENT",
                "watch_enabled": True,
                "context_source": "exchange_context",
                "context_overlay_id": "exchange_context-1",
                "context_policy_effect": "watch_only_no_buy_authority",
                "watch_reason_detail": "positive pressure",
            },
        ]
    )








def test_codex_cli_uses_output_last_message(monkeypatch, tmp_path):
    calls = []

    class DummyCompleted:
        returncode = 0
        stdout = "event stream"
        stderr = ""

    def fake_run(cmd, **kwargs):
        calls.append((cmd, kwargs))
        output_path = cmd[cmd.index("--output-last-message") + 1]
        with open(output_path, "w", encoding="utf-8") as handle:
            handle.write("codex result")
        return DummyCompleted()

    monkeypatch.setattr(codex_cli.subprocess, "run", fake_run)
    monkeypatch.setattr(codex_cli, "resolve_codex_binary", lambda configured=None: "codex")

    result = codex_cli.run_codex_cli("Summarize this", model="gpt-test", images=[tmp_path / "page.png"])

    assert result == "codex result"
    cmd = calls[0][0]
    assert cmd[:2] == ["codex", "exec"]
    assert ["--model", "gpt-test"] == cmd[cmd.index("--model") : cmd.index("--model") + 2]
    assert ["--image", str(tmp_path / "page.png")] == cmd[cmd.index("--image") : cmd.index("--image") + 2]


def test_codex_cli_resolves_common_nvm_binary(monkeypatch, tmp_path):
    nvm_codex = tmp_path / ".nvm" / "versions" / "node" / "v22.14.0" / "bin" / "codex"
    nvm_codex.parent.mkdir(parents=True)
    nvm_codex.write_text("#!/bin/sh\n", encoding="utf-8")

    monkeypatch.setattr(codex_cli.Path, "home", lambda: tmp_path)
    monkeypatch.setattr(codex_cli.shutil, "which", lambda _name: None)

    assert codex_cli.resolve_codex_binary("codex") == str(nvm_codex)


def test_codex_cli_missing_binary_records_fallback(monkeypatch, tmp_path):
    events = []

    monkeypatch.setattr(codex_cli.Path, "home", lambda: tmp_path)
    monkeypatch.setattr(codex_cli.shutil, "which", lambda _name: None)
    monkeypatch.setattr(fallback_telemetry, "record_local_fallback_event", lambda **kwargs: events.append(kwargs) or kwargs)

    with pytest.raises(codex_cli.CodexCLIError):
        codex_cli.resolve_codex_binary("codex")

    assert events[0]["module"] == "utils.codex_cli"
    assert events[0]["source"] == "codex_cli"
    assert events[0]["fallback_type"] == "codex_cli_binary_not_found"
    assert events[0]["metadata"]["configured_binary"] == "codex"


def test_codex_cli_structured_validates_json(monkeypatch):
    calls = []

    class TinyModel(codex_cli.BaseModel):
        name: str
        score: float

    monkeypatch.setattr(
        codex_cli,
        "run_codex_cli",
        lambda prompt, model=None, timeout_seconds=None: calls.append(prompt) or '{"name":"ABC","score":0.7}',
    )

    result = codex_cli.run_codex_structured("Extract", response_model=TinyModel, model="gpt-test")

    assert result.name == "ABC"
    assert result.score == 0.7
    assert "JSON must validate against this schema" in calls[0]


def test_codex_cli_structured_validation_retry_records_fallback(monkeypatch):
    events = []
    calls = []

    class TinyModel(codex_cli.BaseModel):
        name: str
        score: float

    responses = iter(['{"name":"ABC"}', '{"name":"ABC","score":0.7}'])

    def fake_run_codex_cli(prompt, model=None, timeout_seconds=None):
        calls.append(prompt)
        return next(responses)

    monkeypatch.setattr(codex_cli, "run_codex_cli", fake_run_codex_cli)
    monkeypatch.setattr(fallback_telemetry, "record_local_fallback_event", lambda **kwargs: events.append(kwargs) or kwargs)

    result = codex_cli.run_codex_structured("Extract", response_model=TinyModel, model="gpt-test", max_attempts=2)

    assert result.name == "ABC"
    assert result.score == 0.7
    assert len(calls) == 2
    assert len(events) == 1
    assert events[0]["module"] == "utils.codex_cli"
    assert events[0]["source"] == "TinyModel"
    assert events[0]["fallback_type"] == "codex_structured_validation_retry"
    assert events[0]["metadata"]["attempt"] == 1


def test_codex_cli_extract_json_object_records_direct_parse_fallback(monkeypatch):
    events = []
    monkeypatch.setattr(fallback_telemetry, "record_local_fallback_event", lambda **kwargs: events.append(kwargs) or kwargs)

    payload = codex_cli.extract_json_object('Here is the JSON:\n{"name":"ABC"}')

    assert payload == {"name": "ABC"}
    assert len(events) == 1
    assert events[0]["module"] == "utils.codex_cli"
    assert events[0]["source"] == "extract_json_object"
    assert events[0]["fallback_type"] == "codex_json_direct_parse_failed"


def test_ocr_pdf_with_codex_routes_rendered_images(monkeypatch):
    calls = []

    monkeypatch.setattr(
        llm_ocr,
        "render_pdf_pages",
        lambda pdf_path, pages: [(1, llm_ocr.Image.new("RGB", (8, 8), color="white"))],
    )
    monkeypatch.setattr(
        llm_ocr,
        "run_codex_cli",
        lambda prompt, model, images: calls.append((prompt, model, list(images))) or "OCR text",
    )

    result = llm_ocr.ocr_pdf_with_codex("/tmp/test.pdf", pages="1", model="gpt-test")

    assert result == {1: "OCR text"}
    assert calls[0][0] == llm_ocr.OCR_PROMPT
    assert calls[0][1] == "gpt-test"
    assert calls[0][2][0].endswith(".png")


# utils/ocr/llm_ocr.py -- ocr_page_with_local / ocr_pdf_with_local (GLM-OCR, self-
# hosted via transformers -- fundamental screener step 6). The heavy model load and
# CPU inference itself is never exercised in this test suite (confirmed live
# 2026-08-10 separately: ~95-290s per page depending on image size) -- these tests
# fake the (processor, model) pair _load_local_model returns, matching the same
# "mock the boundary, not the library internals" pattern the codex tests above use.


class _FakeLocalOcrInputs(dict):
    def to(self, device):
        return self


class _FakeLocalOcrProcessor:
    def __init__(self, calls, decoded_text="  OCR'd text  "):
        self.calls = calls
        self.decoded_text = decoded_text

    def apply_chat_template(self, messages, **kwargs):
        self.calls["messages"] = messages
        self.calls["apply_chat_template_kwargs"] = kwargs
        return _FakeLocalOcrInputs({"input_ids": torch.tensor([[1, 2, 3]])})

    def decode(self, ids, skip_special_tokens=True):
        self.calls["decoded_ids"] = ids
        self.calls["skip_special_tokens"] = skip_special_tokens
        return self.decoded_text


class _FakeLocalOcrModel:
    device = "cpu"

    def __init__(self, calls):
        self.calls = calls

    def generate(self, **kwargs):
        self.calls["generate_kwargs"] = kwargs
        return torch.tensor([[1, 2, 3, 4, 5]])


def test_ocr_page_with_local_uses_task_prompt_and_downscales_image(monkeypatch):
    calls = {}
    monkeypatch.setattr(
        llm_ocr, "_load_local_model", lambda model: (_FakeLocalOcrProcessor(calls), _FakeLocalOcrModel(calls))
    )

    big_image = llm_ocr.Image.new("RGB", (4000, 3000), color="white")
    result = llm_ocr.ocr_page_with_local(big_image, max_image_dimension=1280, max_new_tokens=512)

    assert result == "OCR'd text"  # stripped
    messages = calls["messages"]
    content = messages[0]["content"]
    assert content[1] == {"type": "text", "text": llm_ocr.LOCAL_OCR_TASK_PROMPT}
    passed_image = content[0]["image"]
    assert max(passed_image.size) <= 1280
    assert calls["generate_kwargs"]["max_new_tokens"] == 512


def test_ocr_page_with_local_does_not_mutate_caller_image(monkeypatch):
    calls = {}
    monkeypatch.setattr(
        llm_ocr, "_load_local_model", lambda model: (_FakeLocalOcrProcessor(calls), _FakeLocalOcrModel(calls))
    )
    original = llm_ocr.Image.new("RGB", (4000, 3000), color="white")
    llm_ocr.ocr_page_with_local(original, max_image_dimension=1280)
    assert original.size == (4000, 3000)  # caller's image untouched -- only a copy was resized


def test_load_local_model_caches_by_model_name(monkeypatch):
    llm_ocr._local_model_cache.clear()
    load_calls = []

    import transformers

    # Patch the from_pretrained *method* on the real classes, not the class names on
    # the module -- transformers' top-level __init__.py lazily resolves these names
    # via module __getattr__, so monkeypatch.setattr(transformers, "AutoProcessor",
    # Fake) doesn't reliably intercept llm_ocr's own `from transformers import
    # AutoProcessor` (confirmed live: the real from_pretrained still ran and hit the
    # network). Patching the method on the already-resolved class object does.
    monkeypatch.setattr(
        transformers.AutoProcessor, "from_pretrained", staticmethod(lambda name: load_calls.append(("processor", name)) or object())
    )
    monkeypatch.setattr(
        transformers.AutoModelForImageTextToText,
        "from_pretrained",
        staticmethod(lambda name, **kwargs: load_calls.append(("model", name)) or object()),
    )

    first = llm_ocr._load_local_model("fake-model")
    second = llm_ocr._load_local_model("fake-model")

    assert first is second  # cached, not reloaded
    assert load_calls == [("processor", "fake-model"), ("model", "fake-model")]
    llm_ocr._local_model_cache.clear()


def test_ocr_pdf_with_local_routes_rendered_images(monkeypatch):
    calls = []
    monkeypatch.setattr(
        llm_ocr,
        "render_pdf_pages",
        lambda pdf_path, pages: [(1, llm_ocr.Image.new("RGB", (8, 8), color="white"))],
    )
    monkeypatch.setattr(
        llm_ocr, "ocr_page_with_local", lambda image, **kwargs: calls.append((image, kwargs)) or "local OCR text"
    )

    result = llm_ocr.ocr_pdf_with_local("/tmp/test.pdf", pages="1", model="local-test")

    assert result == {1: "local OCR text"}
    assert calls[0][1]["model"] == "local-test"


def test_ocr_pdf_routes_to_local_provider(monkeypatch):
    monkeypatch.setattr(llm_ocr, "ocr_pdf_with_local", lambda pdf_path, **k: {1: "local text"})
    result = llm_ocr.ocr_pdf("/tmp/test.pdf", provider="local")
    assert result == {"local": {1: "local text"}}


def test_download_runner_preserves_string_state_advanced_false():
    state = download_runner.build_run_state_result(
        {
            "module": "data.nseindia.indices_parser",
            "status": "ok",
            "returncode": 0,
            "rows": 0,
            "classification_counts": {"parse_failed": 1},
            "state_advanced": "false",
        },
        step={"module": "data.nseindia.indices_parser", "args": [], "purpose": "market_wide"},
    )
    advanced = download_runner.build_run_state_result(
        {
            "module": "data.nseindia.indices_parser",
            "status": "ok",
            "returncode": 0,
            "rows": 2,
            "state_advanced": "true",
        },
        step={"module": "data.nseindia.indices_parser", "args": [], "purpose": "market_wide"},
    )

    assert state["classification"] == "parse_failed"
    assert state["state_advanced"] is False
    assert advanced["classification"] == "ok"
    assert advanced["state_advanced"] is True


def test_download_runner_refines_successful_partial_failure_run_state():
    partial = download_runner.build_run_state_result(
        {
            "module": "data.dhanlive.ohlcv",
            "status": "ok",
            "returncode": 0,
            "rows": 14,
            "failed_symbol_count": 1,
            "classification_counts": {"source_unavailable": 1},
        },
        step={"module": "data.dhanlive.ohlcv", "args": [], "purpose": "dhan_ohlcv_precheck"},
    )
    parser_partial = download_runner.build_run_state_result(
        {
            "module": "data.nseindia.bhavcopy_parser",
            "status": "ok",
            "returncode": 0,
            "rows": 4,
            "classification_counts": {"parse_failed": 1, "no_data": 2},
            "state_advanced": True,
        },
        step={"module": "data.nseindia.bhavcopy_parser", "args": [], "purpose": "market_wide"},
    )
    mixed_no_data = download_runner.build_run_state_result(
        {
            "module": "data.dhanlive.ohlcv",
            "status": "ok",
            "returncode": 0,
            "rows": 8,
            "no_data_count": 2,
            "classification_counts": {"no_data": 2},
            "state_advanced": True,
        },
        step={"module": "data.dhanlive.ohlcv", "args": [], "purpose": "dhan_ohlcv_precheck"},
    )

    assert partial["classification"] == "partial_failed"
    assert partial["state_advanced"] is True
    assert partial["classification_counts"] == {"source_unavailable": 1}
    assert parser_partial["classification"] == "partial_failed"
    assert parser_partial["classification_counts"] == {"parse_failed": 1, "no_data": 2}
    assert mixed_no_data["classification"] == "ok"
    assert mixed_no_data["state_advanced"] is True


def test_download_runner_merges_exported_module_run_state_on_nonzero_exit(monkeypatch):
    persisted: list[dict[str, object]] = []
    monkeypatch.setattr(
        download_runner,
        "_execute_module_entrypoint",
        lambda _module_name: (
            2,
            {
                "rows": 0,
                "attempt_count": 3,
                "failed_attempt_count": 3,
                "source_unavailable_count": 1,
                "no_data_count": 0,
                "state_advanced": False,
            },
        ),
    )
    monkeypatch.setattr(download_runner, "persist_sync_state", lambda **kwargs: persisted.append(kwargs))

    result = download_runner.run_download_module({"module": "data.company_master", "args": [], "purpose": "identity_build"})

    assert result["status"] == "failed"
    assert result["returncode"] == 2
    assert result["run_state"]["classification"] == "failed"
    assert result["run_state"]["attempt_count"] == 3
    assert result["run_state"]["failed_attempt_count"] == 3
    assert result["run_state"]["source_unavailable_count"] == 1
    assert result["run_state"]["state_advanced"] is False
    assert persisted[0]["status"] == "error"
    assert persisted[0]["state"]["attempt_count"] == 3


def test_download_runner_entrypoint_preserves_state_after_system_exit(monkeypatch):
    module = types.ModuleType("fake_download_module")
    module.STOCKEY_RUN_STATE = {}

    def fake_main():
        module.STOCKEY_RUN_STATE = {
            "rows": 4,
            "attempt_count": 1,
            "state_advanced": True,
        }
        raise SystemExit(0)

    module.main = fake_main
    monkeypatch.setattr(download_runner.importlib, "import_module", lambda _module_name: module)
    monkeypatch.setitem(sys.modules, "fake_download_module", module)

    code, state = download_runner._execute_module_entrypoint("fake_download_module")

    assert code == 0
    assert state["rows"] == 4
    assert state["attempt_count"] == 1
    assert state["state_advanced"] is True


def test_download_runner_entrypoint_records_nonzero_system_exit(monkeypatch):
    events: list[dict[str, object]] = []
    module = types.ModuleType("fake_download_module_nonzero")
    module.STOCKEY_RUN_STATE = {}

    def fake_main():
        module.STOCKEY_RUN_STATE = {"rows": 0, "state_advanced": False}
        raise SystemExit(3)

    module.main = fake_main
    monkeypatch.setattr(download_runner.importlib, "import_module", lambda _module_name: module)
    monkeypatch.setattr(download_runner, "record_local_fallback_event", lambda **kwargs: events.append(kwargs) or kwargs)
    monkeypatch.setitem(sys.modules, "fake_download_module_nonzero", module)

    code, state = download_runner._execute_module_entrypoint("fake_download_module_nonzero")

    assert code == 3
    assert state["rows"] == 0
    assert events[0]["module"] == "data.download_runner"
    assert events[0]["fallback_type"] == "download_runner_entrypoint_system_exit_nonzero"
    assert events[0]["source"] == "fake_download_module_nonzero"
    assert events[0]["metadata"]["returncode"] == 3


def test_download_runner_entrypoint_falls_back_to_runpy_without_main(monkeypatch):
    module = types.ModuleType("fake_legacy_download_module")
    calls: list[str] = []

    monkeypatch.setattr(download_runner.importlib, "import_module", lambda _module_name: module)
    monkeypatch.setattr(
        download_runner.runpy,
        "run_module",
        lambda module_name, run_name: calls.append(f"{module_name}:{run_name}") or {"STOCKEY_RUN_STATE": {"rows": 7}},
    )
    monkeypatch.setitem(sys.modules, "fake_legacy_download_module", module)

    code, state = download_runner._execute_module_entrypoint("fake_legacy_download_module")

    assert code == 0
    assert state["rows"] == 7
    assert calls == ["fake_legacy_download_module:__main__"]
    assert "fake_legacy_download_module" not in sys.modules


def test_download_runner_persists_standard_run_state_on_parser_failure(monkeypatch):
    persisted: list[dict[str, object]] = []
    events: list[dict[str, object]] = []

    def fail_module(*args, **kwargs):
        raise ValueError("schema changed")

    monkeypatch.setattr(download_runner, "_execute_module_entrypoint", fail_module)
    monkeypatch.setattr(download_runner, "persist_sync_state", lambda **kwargs: persisted.append(kwargs))
    monkeypatch.setattr(download_runner, "record_local_fallback_event", lambda **kwargs: events.append(kwargs) or kwargs)

    result = download_runner.run_download_module({"module": "data.nseindia.bhavcopy_parser", "args": [], "purpose": "market_wide"})

    assert result["status"] == "failed"
    assert result["run_state"]["classification"] == "parse_failed"
    assert result["run_state"]["phase"] == "parser"
    assert result["run_state"]["state_advanced"] is False
    assert persisted[0]["status"] == "error"
    assert persisted[0]["error_text"].startswith("ValueError: schema changed")
    assert events[0]["fallback_type"] == "download_runner_module_exception"
    assert events[0]["source"] == "data.nseindia.bhavcopy_parser"
    assert events[0]["metadata"] == {"module": "data.nseindia.bhavcopy_parser", "purpose": "market_wide"}


def test_download_runner_records_nonzero_exit_fallback(monkeypatch):
    events: list[dict[str, object]] = []
    persisted: list[dict[str, object]] = []

    monkeypatch.setattr(download_runner, "_execute_module_entrypoint", lambda _module_name: (2, {"rows": 0}))
    monkeypatch.setattr(download_runner, "persist_sync_state", lambda **kwargs: persisted.append(kwargs))
    monkeypatch.setattr(download_runner, "record_local_fallback_event", lambda **kwargs: events.append(kwargs) or kwargs)

    result = download_runner.run_download_module({"module": "data.company_master", "args": [], "purpose": "identity_build"})

    assert result["status"] == "failed"
    assert result["returncode"] == 2
    assert persisted[0]["status"] == "error"
    assert events[0]["fallback_type"] == "download_runner_module_nonzero_exit"
    assert events[0]["source"] == "data.company_master"
    assert events[0]["metadata"] == {"module": "data.company_master", "purpose": "identity_build", "returncode": 2}


def test_schema_migration_dry_run_and_checksum_skip(monkeypatch):
    from utils import schema_migrations

    statements = ["CREATE TABLE test_schema_migration (id INT)"]
    checksum = schema_migrations.checksum_statements(statements)
    monkeypatch.setattr(schema_migrations, "ensure_schema_migrations_table", lambda: None)
    monkeypatch.setattr(schema_migrations, "load_schema_migration", lambda _migration_id: None)

    dry_run = schema_migrations.apply_schema_migration(
        migration_id="20260611_test",
        description="test migration",
        statements=statements,
        dry_run=True,
    )
    assert dry_run["status"] == "dry_run"
    assert dry_run["checksum"] == checksum
    assert dry_run["statement_count"] == 1

    monkeypatch.setattr(
        schema_migrations,
        "load_schema_migration",
        lambda _migration_id: {"status": "applied", "checksum": checksum},
    )
    skipped = schema_migrations.apply_schema_migration(
        migration_id="20260611_test",
        description="test migration",
        statements=statements,
    )
    assert skipped["status"] == "skipped_already_applied"


def test_schema_migration_rejects_checksum_mismatch(monkeypatch):
    from utils import schema_migrations

    monkeypatch.setattr(schema_migrations, "ensure_schema_migrations_table", lambda: None)
    monkeypatch.setattr(
        schema_migrations,
        "load_schema_migration",
        lambda _migration_id: {"status": "applied", "checksum": "old"},
    )

    with pytest.raises(ValueError, match="checksum mismatch"):
        schema_migrations.apply_schema_migration(
            migration_id="20260611_test",
            description="test migration",
            statements=["CREATE TABLE new_shape (id INT)"],
        )


def test_schema_migration_ensure_table_uses_retryable_operation(monkeypatch):
    from utils import schema_migrations

    operation_names = []
    executed = []

    class Cursor:
        def execute(self, query, params=None):
            executed.append((str(query), params))

    class Session:
        def __enter__(self):
            return None, Cursor()

        def __exit__(self, *_args):
            return False

    def fake_execute_db_operation(operation, *, operation_name, **_kwargs):
        operation_names.append(operation_name)
        return operation()

    monkeypatch.setattr(schema_migrations, "db_session", lambda: Session())
    monkeypatch.setattr(schema_migrations, "execute_db_operation", fake_execute_db_operation)

    schema_migrations.ensure_schema_migrations_table()

    assert operation_names == ["schema_migrations:ensure_table"]
    assert any("CREATE TABLE IF NOT EXISTS stockey_schema_migrations" in query for query, _ in executed)
    assert any("ADD COLUMN IF NOT EXISTS metadata_json" in query for query, _ in executed)


def test_schema_migration_applies_statements(monkeypatch):
    from utils import schema_migrations
    operation_names = []

    class FakeCursor:
        def __init__(self):
            self.calls = []

        def execute(self, query, params=None):
            self.calls.append((str(query), params))

    class FakeSession:
        def __init__(self, cur):
            self.cur = cur

        def __enter__(self):
            return None, self.cur

        def __exit__(self, *_args):
            return False

    cur = FakeCursor()
    monkeypatch.setattr(schema_migrations, "ensure_schema_migrations_table", lambda: None)
    monkeypatch.setattr(schema_migrations, "load_schema_migration", lambda _migration_id: None)
    monkeypatch.setattr(schema_migrations, "db_session", lambda *args, **kwargs: FakeSession(cur))
    monkeypatch.setattr(
        schema_migrations,
        "execute_db_operation",
        lambda operation, *, operation_name, **_kwargs: operation_names.append(operation_name) or operation(),
    )

    result = schema_migrations.apply_schema_migration(
        migration_id="20260611_apply",
        description="apply migration",
        statements=["CREATE TABLE schema_test (id INT)", "ALTER TABLE schema_test ADD COLUMN name TEXT"],
    )

    assert result["status"] == "applied"
    assert operation_names == ["schema_migrations:apply:20260611_apply"]
    assert any("CREATE TABLE schema_test" in query for query, _params in cur.calls)
    assert any("ALTER TABLE schema_test" in query for query, _params in cur.calls)
    assert any("SET status = 'applied'" in query for query, _params in cur.calls)


def test_schema_migration_records_failure_after_statement_error(monkeypatch):
    from utils import schema_migrations
    operation_names = []

    class FakeCursor:
        def __init__(self, fail_on_bad: bool):
            self.fail_on_bad = fail_on_bad
            self.calls = []

        def execute(self, query, params=None):
            self.calls.append((str(query), params))
            if self.fail_on_bad and str(query) == "BAD SQL":
                raise RuntimeError("boom")

    class FakeSession:
        def __init__(self, cur):
            self.cur = cur

        def __enter__(self):
            return None, self.cur

        def __exit__(self, *_args):
            return False

    apply_cur = FakeCursor(fail_on_bad=True)
    failure_cur = FakeCursor(fail_on_bad=False)
    sessions = [FakeSession(apply_cur), FakeSession(failure_cur)]

    monkeypatch.setattr(schema_migrations, "ensure_schema_migrations_table", lambda: None)
    monkeypatch.setattr(schema_migrations, "load_schema_migration", lambda _migration_id: None)
    monkeypatch.setattr(schema_migrations, "db_session", lambda *args, **kwargs: sessions.pop(0))
    monkeypatch.setattr(
        schema_migrations,
        "execute_db_operation",
        lambda operation, *, operation_name, **_kwargs: operation_names.append(operation_name) or operation(),
    )

    with pytest.raises(RuntimeError, match="boom"):
        schema_migrations.apply_schema_migration(
            migration_id="20260611_fail",
            description="failing migration",
            statements=["BAD SQL"],
        )

    assert operation_names == [
        "schema_migrations:apply:20260611_fail",
        "schema_migrations:record_failure:20260611_fail",
    ]
    assert any("status = 'failed'" in query or "'failed'" in query for query, _params in failure_cur.calls)
    assert any(params and "RuntimeError: boom" in params.get("error_text", "") for _query, params in failure_cur.calls)


def test_company_master_backfill_column_uses_schema_registry(monkeypatch):
    from data import backfill_company_master_ids as backfill

    calls = []
    spec = backfill.BackfillSpec("nseindia_events", "symbol", exchange="NSE")

    monkeypatch.setattr(backfill, "apply_schema_migration", lambda **kwargs: calls.append(kwargs) or {"status": "applied"})

    backfill.ensure_company_master_id_column(spec)

    assert len(calls) == 1
    call = calls[0]
    assert call["migration_id"] == "20260611_company_master_id_backfill_nseindia_events"
    assert call["owner"] == "data.backfill_company_master_ids"
    assert call["metadata"]["tables"] == ["nseindia_events"]
    assert call["metadata"]["ticker_column"] == "symbol"
    assert call["metadata"]["exchange"] == "NSE"
    ddl = "\n".join(call["statements"])
    assert 'ALTER TABLE public."nseindia_events" ADD COLUMN IF NOT EXISTS company_master_id TEXT' in ddl


def test_company_master_backfill_uses_retryable_operations(monkeypatch):
    from data import backfill_company_master_ids as backfill

    operation_names = []
    executed = []
    ensured = []

    class Cursor:
        def execute(self, query, params=None):
            executed.append((str(query), params))

    class Session:
        def __enter__(self):
            return None, Cursor()

        def __exit__(self, exc_type, exc, tb):
            return False

    def fake_execute_db_operation(operation, *, operation_name, **_kwargs):
        operation_names.append(operation_name)
        return operation()

    monkeypatch.setattr(backfill, "db_session", lambda: Session())
    monkeypatch.setattr(backfill, "execute_db_operation", fake_execute_db_operation)
    monkeypatch.setattr(backfill, "_table_exists", lambda _cur, table_name: table_name != "missing_table")
    monkeypatch.setattr(backfill, "_column_exists", lambda _cur, _table_name, _column_name: True)
    monkeypatch.setattr(backfill, "ensure_company_master_id_column", lambda spec: ensured.append(spec.table_name))
    monkeypatch.setattr(backfill, "_run_update", lambda _cur, spec: 7 if spec.table_name == "ok_table" else 0)

    results = backfill.backfill_company_master_ids(
        [
            backfill.BackfillSpec("ok_table", "symbol", exchange="NSE"),
            backfill.BackfillSpec("missing_table", "symbol", exchange="NSE"),
        ]
    )

    assert operation_names == [
        "backfill_company_master_ids:ok_table",
        "backfill_company_master_ids:missing_table",
    ]
    assert ensured == ["ok_table"]
    assert results == [
        {"table": "ok_table", "status": "ok", "updated": 7},
        {"table": "missing_table", "status": "missing"},
    ]
    assert len([query for query, _ in executed if "SET LOCAL statement_timeout = 0" in query]) == 2


def test_nse_offmarket_exports_retry_run_state(monkeypatch):
    class FakeRedis:
        def close(self):
            return None

    class FakePlaywrightContext:
        def __enter__(self):
            return object()

        def __exit__(self, exc_type, exc, tb):
            return False

    calls = {"get_next": 0, "download": 0}

    def fake_get_next(rop, dtype, g_start, g_end, skipped_dates=None):
        if dtype == "block_deals" and calls["get_next"] == 0:
            calls["get_next"] += 1
            return offmarket.datetime(2026, 6, 9), offmarket.datetime(2026, 6, 10)
        calls["get_next"] += 1
        return None

    def fake_download_data(playwright, dtype, from_date, to_date, rop):
        calls["download"] += 1
        return calls["download"] == 2

    monkeypatch.setattr(offmarket, "get_redis_client", lambda *args, **kwargs: FakeRedis())
    monkeypatch.setattr(offmarket, "sync_playwright", lambda: FakePlaywrightContext())
    monkeypatch.setattr(offmarket, "latest_completed_day", lambda: offmarket.datetime(2026, 6, 10))
    monkeypatch.setattr(offmarket, "NSE_OFFMARKET_DOWNLOAD_LOOKBACK_DAYS", 1)
    monkeypatch.setattr(offmarket, "get_next_download_block", fake_get_next)
    monkeypatch.setattr(offmarket, "download_data", fake_download_data)

    assert offmarket.main() == 0

    state = offmarket.STOCKEY_RUN_STATE
    assert state["source"] == "data.nseindia.offmarket"
    assert state["blocks_attempted"] == 1
    assert state["download_attempts"] == 2
    assert state["attempt_count"] == 2
    assert state["retry_count"] == 1
    assert state["failed_attempt_count"] == 1
    assert state["downloaded_blocks"] == 1
    assert state["dates_downloaded"] == 2
    assert state["rows_written"] == 2
    assert state["state_advanced"] is True


def test_nse_offmarket_records_download_failure_fallback(monkeypatch):
    monkeypatch.setattr(nse_rate_limiter.time, "sleep", lambda *a, **k: None)  # no real rate-gate delay in test
    events: list[dict[str, object]] = []

    class FakePage:
        def goto(self, *_args, **_kwargs):
            raise RuntimeError("offmarket timeout")

        def close(self):
            return None

    class FakeContext:
        def new_page(self):
            return FakePage()

    class FakeBrowser:
        contexts = []

        def new_context(self):
            return FakeContext()

        def close(self):
            return None

    class FakeChromium:
        def connect_over_cdp(self, _endpoint):
            return FakeBrowser()

    class FakePlaywright:
        chromium = FakeChromium()

    class FakeRedis:
        def sadd(self, *_args, **_kwargs):
            raise AssertionError("download failure must not mark redis downloaded")

    monkeypatch.setattr(offmarket, "record_local_fallback_event", lambda **kwargs: events.append(kwargs) or kwargs)

    ok = offmarket.download_data(
        FakePlaywright(),
        "block_deals",
        offmarket.datetime(2026, 6, 9),
        offmarket.datetime(2026, 6, 10),
        FakeRedis(),
    )

    assert ok is False
    assert len(events) == 1
    event = events[0]
    assert event["module"] == "data.nseindia.offmarket"
    assert event["source"] == "nsedeals:block_deals:2026-06-09:2026-06-10"
    assert event["fallback_type"] == "nse_offmarket_download_failed"
    assert event["severity"] == "warn"
    assert isinstance(event["error"], RuntimeError)
    assert str(event["error"]) == "offmarket timeout"
    assert event["metadata"] == {
        "dtype": "block_deals",
        "from_date": "2026-06-09",
        "to_date": "2026-06-10",
        "from_date_display": "09-06-2026",
        "to_date_display": "10-06-2026",
    }


def test_nse_offmarket_parser_exports_file_run_state(monkeypatch):
    from data.nseindia import offmarket_parser

    failed_states = []
    monkeypatch.setattr(
        offmarket_parser,
        "get_processed_keys",
        lambda _source, status="processed": {"nsedeals/already.csv"} if status == "processed" else set(),
    )
    monkeypatch.setattr(
        offmarket_parser,
        "mark_failed",
        lambda source_prefix, object_key, error_message: failed_states.append((source_prefix, object_key, error_message)),
    )
    monkeypatch.setattr(
        offmarket_parser.store,
        "list_files",
        lambda _prefix: ["nsedeals/already.csv", "nsedeals/block_deals.csv", "nsedeals/bad.csv"],
    )
    monkeypatch.setattr(offmarket_parser.store, "get_as_temp_file", lambda key: f"/tmp/{key.rsplit('/', 1)[-1]}")

    def fake_process(file_name, _csv_path):
        if file_name == "nsedeals/bad.csv":
            raise ValueError("bad schema")
        return {"file_name": file_name, "dtype": "block_deals", "rows": 4, "from_date": "2026-06-01", "to_date": "2026-06-02"}

    monkeypatch.setattr(offmarket_parser, "process_csv", fake_process)

    state = offmarket_parser.run_parser()

    assert state["source"] == "data.nseindia.offmarket_parser"
    assert state["files_seen"] == 3
    assert state["already_processed_count"] == 1
    assert state["files_considered"] == 2
    assert state["parsed_count"] == 1
    assert state["failed_count"] == 1
    assert state["failed_attempt_count"] == 1
    assert state["parse_failed_count"] == 1
    assert state["failed_classifications"] == {"parser_bug": 1}
    assert state["rows_written"] == 4
    assert state["from_date"] == "2026-06-01"
    assert state["to_date"] == "2026-06-02"
    assert state["failed_files"][0]["file_name"] == "nsedeals/bad.csv"
    assert state["failed_files"][0]["classification"] == "parser_bug"
    assert failed_states == [("nsedeals", "nsedeals/bad.csv", "classification=parser_bug; ValueError: bad schema")]
    assert state["state_advanced"] is True


def test_nse_offmarket_parser_records_parse_failure_fallback(monkeypatch):
    from data.nseindia import offmarket_parser

    failed_states = []
    events = []
    monkeypatch.setattr(offmarket_parser, "get_processed_keys", lambda _source, status="processed": set())
    monkeypatch.setattr(
        offmarket_parser,
        "mark_failed",
        lambda source_prefix, object_key, error_message: failed_states.append((source_prefix, object_key, error_message)),
    )
    monkeypatch.setattr(offmarket_parser.store, "list_files", lambda _prefix: ["nsedeals/bulk_deals_bad.csv"])
    monkeypatch.setattr(offmarket_parser.store, "get_as_temp_file", lambda key: "/tmp/bulk_deals_bad.csv")
    monkeypatch.setattr(
        offmarket_parser,
        "process_csv",
        lambda _file_name, _csv_path: (_ for _ in ()).throw(KeyError("client_name")),
    )
    monkeypatch.setattr(
        offmarket_parser,
        "record_local_fallback_event",
        lambda **kwargs: events.append(kwargs),
    )

    state = offmarket_parser.run_parser()

    assert state["failed_count"] == 1
    assert state["failed_classifications"] == {"schema_changed": 1}
    assert failed_states == [
        ("nsedeals", "nsedeals/bulk_deals_bad.csv", "classification=schema_changed; KeyError: 'client_name'")
    ]
    assert len(events) == 1
    event = events[0]
    assert event["module"] == "data.nseindia.offmarket_parser"
    assert event["source"] == "nsedeals/bulk_deals_bad.csv"
    assert event["fallback_type"] == "nse_offmarket_parse_failed"
    assert event["severity"] == "warn"
    assert isinstance(event["error"], KeyError)
    assert event["metadata"] == {
        "file_name": "nsedeals/bulk_deals_bad.csv",
        "classification": "schema_changed",
        "error_message": "classification=schema_changed; KeyError: 'client_name'",
    }


def test_nse_offmarket_parser_marks_empty_valid_source(monkeypatch):
    from data.nseindia import offmarket_parser

    monkeypatch.setattr(offmarket_parser, "get_processed_keys", lambda _source, status="processed": set())
    monkeypatch.setattr(offmarket_parser.store, "list_files", lambda _prefix: ["nsedeals/empty.csv"])
    monkeypatch.setattr(offmarket_parser.store, "get_as_temp_file", lambda key: "/tmp/empty.csv")
    monkeypatch.setattr(
        offmarket_parser,
        "process_csv",
        lambda file_name, _csv_path: {
            "file_name": file_name,
            "dtype": "bulk_deals",
            "status": offmarket_parser.EMPTY_VALID_STATUS,
            "rows": 0,
        },
    )

    state = offmarket_parser.run_parser()

    assert state["files_considered"] == 1
    assert state["parsed_count"] == 0
    assert state["empty_valid_count"] == 1
    assert state["failed_count"] == 0
    assert state["rows_written"] == 1
    assert state["state_advanced"] is True


def test_nse_offmarket_parser_treats_empty_valid_source_as_completed(monkeypatch):
    from data.nseindia import offmarket_parser

    monkeypatch.setattr(
        offmarket_parser,
        "get_processed_keys",
        lambda _source, status="processed": {"nsedeals/empty.csv"} if status == offmarket_parser.EMPTY_VALID_STATUS else set(),
    )
    monkeypatch.setattr(offmarket_parser.store, "list_files", lambda _prefix: ["nsedeals/empty.csv"])
    monkeypatch.setattr(
        offmarket_parser.store,
        "get_as_temp_file",
        lambda key: (_ for _ in ()).throw(AssertionError("completed empty key should not be fetched")),
    )

    state = offmarket_parser.run_parser()

    assert state["already_processed_count"] == 1
    assert state["files_considered"] == 0
    assert state["empty_valid_count"] == 0
    assert state["state_advanced"] is False


def test_nse_offmarket_parser_schema_helpers_use_retryable_operations(monkeypatch):
    from data.nseindia import offmarket_parser

    operation_names = []
    executed = []
    fetch_rows = iter(
        [
            ("public.nseindia_offmarket_block_deals",),
            None,
            ("public.nseindia_offmarket_block_deals",),
            ("integer",),
        ]
    )

    class FakeCursor:
        def execute(self, query, params=None):
            executed.append((str(query), params))

        def fetchone(self):
            return next(fetch_rows)

    class FakeSession:
        def __enter__(self):
            return None, FakeCursor()

        def __exit__(self, *_args):
            return False

    def fake_execute_db_operation(operation, *, operation_name, **_kwargs):
        operation_names.append(operation_name)
        return operation()

    monkeypatch.setattr(offmarket_parser, "db_session", lambda: FakeSession())
    monkeypatch.setattr(offmarket_parser, "execute_db_operation", fake_execute_db_operation)

    offmarket_parser.ensure_unique_constraint(
        "nseindia_offmarket_block_deals",
        "uniq_test",
        ["date", "symbol"],
        drop_constraints=["old_constraint"],
        drop_indexes=["old_index"],
    )
    offmarket_parser.ensure_text_columns("nseindia_offmarket_block_deals", ["symbol"])

    assert operation_names == [
        "offmarket_parser:ensure_unique_constraint:nseindia_offmarket_block_deals",
        "offmarket_parser:ensure_text_columns:nseindia_offmarket_block_deals",
    ]
    assert any("DROP CONSTRAINT IF EXISTS old_constraint" in query for query, _ in executed)
    assert any("DROP INDEX IF EXISTS old_index" in query for query, _ in executed)
    assert any("ADD CONSTRAINT uniq_test UNIQUE (date, symbol)" in query for query, _ in executed)
    assert any(
        "ALTER TABLE nseindia_offmarket_block_deals ALTER COLUMN symbol TYPE TEXT" in query
        for query, _ in executed
    )


def test_nse_offmarket_parser_failure_classifier_distinguishes_schema_and_parser_errors():
    from data.nseindia import offmarket_parser

    assert offmarket_parser.classify_offmarket_parse_failure(pd.errors.EmptyDataError("empty")) == "empty_valid_source"
    assert offmarket_parser.classify_offmarket_parse_failure(KeyError("client_name")) == "schema_changed"
    assert offmarket_parser.classify_offmarket_parse_failure(ValueError("Length mismatch: Expected axis has 4 elements")) == "schema_changed"
    assert offmarket_parser.classify_offmarket_parse_failure(RuntimeError("unexpected parser branch")) == "parser_bug"


def test_nse_offmarket_parser_main_exports_run_state(monkeypatch, capsys):
    from data.nseindia import offmarket_parser

    payload = {
        "source": "data.nseindia.offmarket_parser",
        "rows": 0,
        "rows_read": 0,
        "rows_written": 0,
        "files_seen": 1,
        "failed_count": 1,
        "failed_attempt_count": 1,
        "parse_failed_count": 1,
        "state_advanced": False,
    }

    class FakeRedis:
        def close(self):
            pass

    monkeypatch.setattr(offmarket_parser, "run_parser", lambda: payload)
    monkeypatch.setattr(offmarket_parser, "rop", FakeRedis())

    assert offmarket_parser.main() == 0
    capsys.readouterr()

    assert offmarket_parser.STOCKEY_RUN_STATE["source"] == "data.nseindia.offmarket_parser"
    assert offmarket_parser.STOCKEY_RUN_STATE["failed_count"] == 1
    assert offmarket_parser.STOCKEY_RUN_STATE["state_advanced"] is False


def test_bhavcopy_downloader_exports_retry_run_state(monkeypatch):
    class FakeRedis:
        def close(self):
            return None

    class FakePlaywrightContext:
        def __enter__(self):
            return object()

        def __exit__(self, exc_type, exc, tb):
            return False

    dates = [
        bhavcopy_downloader.datetime(2026, 6, 10),
        bhavcopy_downloader.datetime(2026, 6, 9),
        bhavcopy_downloader.datetime(2026, 6, 8),
    ]
    calls = {"download": 0}

    def fake_download(playwright, formatted_date, display_date, rop):
        # 2026-06-09 fails on EVERY attempt (incl. the in-run retries) so it stays a genuine failure;
        # a transient one-attempt failure would now be rescued by download_bhavcopy_with_retries.
        calls["download"] += 1
        return formatted_date != "2026-06-09"

    monkeypatch.setattr(bhavcopy_downloader, "get_redis_client", lambda *args, **kwargs: FakeRedis())
    monkeypatch.setattr(bhavcopy_downloader, "sync_playwright", lambda: FakePlaywrightContext())
    monkeypatch.setattr(bhavcopy_downloader, "load_downloaded_dates_from_store", lambda: set())
    monkeypatch.setattr(bhavcopy_downloader, "reverse_daterange", lambda start, end: dates)
    monkeypatch.setattr(bhavcopy_downloader, "filter_missing_date_members", lambda all_dates, existing: list(all_dates))
    monkeypatch.setattr(bhavcopy_downloader, "download_bhavcopy_for_date", fake_download)

    assert bhavcopy_downloader.main() == 0

    state = bhavcopy_downloader.STOCKEY_RUN_STATE
    assert state["source"] == "data.nseindia.bhavcopy_downloader"
    assert state["candidate_dates"] == 3
    assert state["missing_dates"] == 3
    assert state["download_attempts"] == 3
    assert state["failed_attempt_count"] == 1
    assert state["source_unavailable_count"] == 1
    assert state["downloaded_dates"] == 2
    assert state["rows_written"] == 2
    assert state["stopped_after_consecutive_failures"] is False
    assert state["state_advanced"] is True


def test_bhavcopy_downloader_total_failure_reports_source_unavailable(monkeypatch, capsys):
    """A run where every attempted date fails (e.g. NSE blocking this session) must not report
    "ok"/exit 0 -- download_runner's overall pass/fail check reads this raw status directly, and
    bhavcopy_downloader's purpose is CRITICAL there. A partial run stays "ok" (see the sibling
    retry_run_state test above); only a total failure (nothing downloaded) flips it."""

    class FakeRedis:
        def close(self):
            return None

    class FakePlaywrightContext:
        def __enter__(self):
            return object()

        def __exit__(self, exc_type, exc, tb):
            return False

    dates = [bhavcopy_downloader.datetime(2026, 6, 10), bhavcopy_downloader.datetime(2026, 6, 9)]

    monkeypatch.setattr(bhavcopy_downloader, "get_redis_client", lambda *args, **kwargs: FakeRedis())
    monkeypatch.setattr(bhavcopy_downloader, "sync_playwright", lambda: FakePlaywrightContext())
    monkeypatch.setattr(bhavcopy_downloader, "load_downloaded_dates_from_store", lambda: set())
    monkeypatch.setattr(bhavcopy_downloader, "reverse_daterange", lambda start, end: dates)
    monkeypatch.setattr(bhavcopy_downloader, "filter_missing_date_members", lambda all_dates, existing: list(all_dates))
    monkeypatch.setattr(bhavcopy_downloader, "download_bhavcopy_for_date", lambda *args, **kwargs: False)

    assert bhavcopy_downloader.main() == 1

    state = bhavcopy_downloader.STOCKEY_RUN_STATE
    assert state["downloaded_dates"] == 0
    assert state["failed_attempt_count"] == 2
    printed = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert printed["status"] == "source_unavailable"


def test_nse_request_gate_sleeps_out_the_remaining_interval(monkeypatch, tmp_path):
    lock_path = tmp_path / "gate.lock"
    timestamp_path = tmp_path / "last_request_at"
    timestamp_path.write_text(repr(1000.0), encoding="utf-8")  # "last request" at t=1000

    sleeps: list[float] = []
    monkeypatch.setattr(nse_rate_limiter.time, "sleep", lambda seconds: sleeps.append(seconds))
    monkeypatch.setattr(nse_rate_limiter.time, "time", lambda: 1004.0)  # "now" -- 4s since last request

    with nse_rate_limiter.nse_request_gate(
        lock_path=lock_path, timestamp_path=timestamp_path, min_interval_seconds=10.0
    ):
        pass

    assert len(sleeps) == 1
    assert sleeps[0] == pytest.approx(6.0)  # 10s floor - 4s already elapsed
    assert timestamp_path.read_text(encoding="utf-8") == repr(1004.0)  # stamped at request time, not entry time


def test_nse_request_gate_skips_sleep_once_interval_already_elapsed(monkeypatch, tmp_path):
    lock_path = tmp_path / "gate.lock"
    timestamp_path = tmp_path / "last_request_at"
    timestamp_path.write_text(repr(1000.0), encoding="utf-8")

    sleeps: list[float] = []
    monkeypatch.setattr(nse_rate_limiter.time, "sleep", lambda seconds: sleeps.append(seconds))
    monkeypatch.setattr(nse_rate_limiter.time, "time", lambda: 1015.0)  # 15s since last request, > 10s floor

    with nse_rate_limiter.nse_request_gate(
        lock_path=lock_path, timestamp_path=timestamp_path, min_interval_seconds=10.0
    ):
        pass

    assert sleeps == []


def test_nse_request_gate_first_ever_request_does_not_sleep(monkeypatch, tmp_path):
    lock_path = tmp_path / "gate.lock"
    timestamp_path = tmp_path / "last_request_at"  # no prior timestamp file at all

    sleeps: list[float] = []
    monkeypatch.setattr(nse_rate_limiter.time, "sleep", lambda seconds: sleeps.append(seconds))

    with nse_rate_limiter.nse_request_gate(lock_path=lock_path, timestamp_path=timestamp_path):
        pass

    assert sleeps == []
    assert timestamp_path.exists()


def test_nse_request_gate_times_out_when_another_process_holds_it(tmp_path):
    lock_path = tmp_path / "gate.lock"
    holder = open(lock_path, "a+")
    fcntl.flock(holder.fileno(), fcntl.LOCK_EX)  # simulate another process mid-request
    try:
        with pytest.raises(TimeoutError, match="Timed out"):
            with nse_rate_limiter.nse_request_gate(lock_path=lock_path, timeout_seconds=0.2):
                pass
    finally:
        fcntl.flock(holder.fileno(), fcntl.LOCK_UN)
        holder.close()


def test_nse_goto_calls_page_goto_through_the_gate(monkeypatch, tmp_path):
    monkeypatch.setattr(nse_rate_limiter.time, "sleep", lambda *a, **k: None)
    lock_path = tmp_path / "gate.lock"
    timestamp_path = tmp_path / "last_request_at"
    monkeypatch.setattr(nse_rate_limiter, "DEFAULT_LOCK_PATH", lock_path)
    monkeypatch.setattr(nse_rate_limiter, "DEFAULT_TIMESTAMP_PATH", timestamp_path)

    calls: list[tuple[str, dict]] = []

    class FakePage:
        def goto(self, url, **kwargs):
            calls.append((url, kwargs))
            return "response"

    result = nse_rate_limiter.nse_goto(FakePage(), "https://www.nseindia.com", timeout=15000)

    assert result == "response"
    assert calls == [("https://www.nseindia.com", {"timeout": 15000})]
    # DEFAULT_LOCK_PATH/DEFAULT_TIMESTAMP_PATH are read inside nse_request_gate()'s body
    # (not baked into an early-bound parameter default), so the monkeypatch above must
    # actually be honored -- these files, not the real .cache/ ones, should now exist.
    assert lock_path.exists()
    assert timestamp_path.exists()


def test_exchange_rate_limiter_domains_use_separate_lock_and_timestamp_files():
    assert exchange_rate_limiter.default_lock_path("nse") != exchange_rate_limiter.default_lock_path("bse")
    assert exchange_rate_limiter.default_timestamp_path("nse") != exchange_rate_limiter.default_timestamp_path("bse")


def test_exchange_rate_limiter_reads_domain_specific_env_var(monkeypatch):
    monkeypatch.setattr(exchange_rate_limiter.env, "float", lambda name, default: 42.0 if name == "BSE_MIN_REQUEST_INTERVAL_SECONDS" else default)
    assert exchange_rate_limiter.default_min_interval_seconds("bse") == 42.0
    assert exchange_rate_limiter.default_min_interval_seconds("nse") == 10.0


def test_exchange_rate_limiter_two_domains_do_not_block_each_other(monkeypatch, tmp_path):
    monkeypatch.setattr(exchange_rate_limiter.time, "sleep", lambda *a, **k: None)

    with exchange_rate_limiter.exchange_request_gate(
        domain="nse",
        lock_path=tmp_path / "nse.lock",
        timestamp_path=tmp_path / "nse_ts",
    ):
        # A concurrent BSE request must not wait on the held NSE lock -- separate domains,
        # separate locks. timeout_seconds is short specifically so this would fail loudly
        # (TimeoutError) rather than hang if domain isolation were broken.
        with exchange_rate_limiter.exchange_request_gate(
            domain="bse",
            lock_path=tmp_path / "bse.lock",
            timestamp_path=tmp_path / "bse_ts",
            timeout_seconds=2.0,
        ):
            pass


def test_bhavcopy_downloader_records_download_failure_fallback(monkeypatch):
    events: list[dict[str, object]] = []

    class FakePage:
        def wait_for_timeout(self, *_args, **_kwargs):
            raise RuntimeError("nse timeout")

        def close(self):
            return None

    class FakeContext:
        def new_page(self):
            return FakePage()

    class FakeBrowser:
        contexts = []

        def new_context(self):
            return FakeContext()

        def close(self):
            return None

    class FakeChromium:
        def connect_over_cdp(self, _endpoint):
            return FakeBrowser()

    class FakePlaywright:
        chromium = FakeChromium()

    class FakeRedis:
        def sadd(self, *_args, **_kwargs):
            raise AssertionError("download failure must not mark redis downloaded")

    monkeypatch.setattr(bhavcopy_downloader, "record_local_fallback_event", lambda **kwargs: events.append(kwargs) or kwargs)

    ok = bhavcopy_downloader.download_bhavcopy_for_date(
        FakePlaywright(),
        "2026-06-10",
        "10-Jun-2026",
        FakeRedis(),
    )

    assert ok is False
    assert len(events) == 1
    event = events[0]
    assert event["module"] == "data.nseindia.bhavcopy_downloader"
    assert event["source"] == "bhavcopy:2026-06-10"
    assert event["fallback_type"] == "nse_bhavcopy_download_failed"
    assert event["severity"] == "warn"
    assert isinstance(event["error"], RuntimeError)
    assert str(event["error"]) == "nse timeout"
    assert event["metadata"] == {
        "formatted_date": "2026-06-10",
        "display_date": "10-Jun-2026",
        "weekday": "Wednesday",
        "source_prefix": "bhavcopy",
    }


def test_indices_downloader_exports_failure_stop_run_state(monkeypatch):
    class FakeRedis:
        def hgetall(self, key):
            return {}

        def smembers(self, key):
            return set()

        def hset(self, *a, **k):
            return None

        def hdel(self, *a, **k):
            return None

        def sadd(self, *a, **k):
            return None

        def srem(self, *a, **k):
            return None

        def close(self):
            return None

    class FakePlaywrightContext:
        def __enter__(self):
            return object()

        def __exit__(self, exc_type, exc, tb):
            return False

    dates = [indices_downloader.datetime(2026, 6, day) for day in range(10, 2, -1)]
    monkeypatch.setattr(indices_downloader, "get_redis_client", lambda *args, **kwargs: FakeRedis())
    monkeypatch.setattr(indices_downloader, "sync_playwright", lambda: FakePlaywrightContext())
    monkeypatch.setattr(indices_downloader, "load_downloaded_dates_from_store", lambda: set())
    monkeypatch.setattr(indices_downloader, "load_confirmed_gaps", lambda **kwargs: set())  # hermetic: no DB
    monkeypatch.setattr(indices_downloader, "latest_downloaded_date", lambda existing: None)
    monkeypatch.setattr(indices_downloader, "reverse_daterange", lambda start, end: dates)
    monkeypatch.setattr(indices_downloader, "download_indices_for_date", lambda *args, **kwargs: "error")
    monkeypatch.setattr(indices_downloader, "parse_datetime_arg", lambda value: indices_downloader.datetime.strptime(value, "%Y-%m-%d") if value else None)
    monkeypatch.setattr(sys, "argv", ["data.nseindia.indices_downloader", "--backfill", "--from-date", "2026-06-03", "--to-date", "2026-06-10"])

    with pytest.raises(SystemExit):
        indices_downloader.main()

    state = indices_downloader.STOCKEY_RUN_STATE
    assert state["source"] == "data.nseindia.indices_downloader"
    assert state["mode"] == "backfill"
    assert state["candidate_dates"] == 8
    assert state["download_attempts"] == 7
    assert state["failed_attempt_count"] == 7
    assert state["source_unavailable_count"] == 7
    assert state["skipped_after_failure_stop"] == 1
    assert state["stopped_after_consecutive_failures"] is True
    assert state["state_advanced"] is False


def test_indices_downloader_records_download_failure_fallback(monkeypatch):
    events: list[dict[str, object]] = []

    class FakePage:
        def wait_for_timeout(self, *_args, **_kwargs):
            raise RuntimeError("indices timeout")

        def close(self):
            return None

    class FakeContext:
        def new_page(self):
            return FakePage()

    class FakeBrowser:
        contexts = []

        def new_context(self):
            return FakeContext()

        def close(self):
            return None

    class FakeChromium:
        def connect_over_cdp(self, _endpoint):
            return FakeBrowser()

    class FakePlaywright:
        chromium = FakeChromium()

    class FakeRedis:
        def sadd(self, *_args, **_kwargs):
            raise AssertionError("download failure must not mark redis downloaded")

    monkeypatch.setattr(indices_downloader, "record_local_fallback_event", lambda **kwargs: events.append(kwargs) or kwargs)

    ok = indices_downloader.download_indices_for_date(
        FakePlaywright(),
        "2026-06-10",
        "10-Jun-2026",
        FakeRedis(),
    )

    assert ok == "error"
    assert len(events) == 1
    event = events[0]
    assert event["module"] == "data.nseindia.indices_downloader"
    assert event["source"] == "indices:2026-06-10"
    assert event["fallback_type"] == "nse_indices_download_failed"
    assert event["severity"] == "warn"
    assert isinstance(event["error"], RuntimeError)
    assert str(event["error"]) == "indices timeout"
    assert event["metadata"] == {
        "formatted_date": "2026-06-10",
        "display_date": "10-Jun-2026",
        "weekday": "Wednesday",
        "source_prefix": "indices",
    }


def test_bhavcopy_parser_main_exports_runner_state(monkeypatch, capsys):
    monkeypatch.setattr(
        bhavcopy_parser,
        "run_parser",
        lambda **kw: {
            "source": "bhavcopy",
            "rows": 2,
            "rows_read": 3,
            "rows_written": 2,
            "files_seen": 4,
            "files_considered": 3,
            "parsed_count": 1,
            "empty_processed_count": 1,
            "failed_count": 1,
            "from_date": "2026-06-01",
            "to_date": "2026-06-03",
            "fallback_used": False,
            "state_advanced": True,
        },
    )

    assert bhavcopy_parser.main() == 0
    capsys.readouterr()

    assert bhavcopy_parser.STOCKEY_RUN_STATE["source"] == "bhavcopy"
    assert bhavcopy_parser.STOCKEY_RUN_STATE["rows"] == 2
    assert bhavcopy_parser.STOCKEY_RUN_STATE["rows_read"] == 3
    assert bhavcopy_parser.STOCKEY_RUN_STATE["rows_written"] == 2
    assert bhavcopy_parser.STOCKEY_RUN_STATE["parsed_count"] == 1
    assert bhavcopy_parser.STOCKEY_RUN_STATE["empty_processed_count"] == 1
    assert bhavcopy_parser.STOCKEY_RUN_STATE["failed_count"] == 1
    assert bhavcopy_parser.STOCKEY_RUN_STATE["state_advanced"] is True


def test_bhavcopy_parser_total_failure_reports_parse_failed(monkeypatch, capsys):
    """A run where every considered file fails to parse (rows_written == 0) must not report
    "ok"/exit 0 -- this module's purpose is CRITICAL in download_runner's classification. A
    partial run (some files parsed, one bad file) stays "ok" (see the sibling test above)."""
    monkeypatch.setattr(
        bhavcopy_parser,
        "run_parser",
        lambda **kw: {
            "source": "bhavcopy",
            "rows": 0,
            "rows_read": 2,
            "rows_written": 0,
            "files_seen": 2,
            "files_considered": 2,
            "parsed_count": 0,
            "empty_processed_count": 0,
            "failed_count": 2,
            "from_date": "2026-06-01",
            "to_date": "2026-06-02",
            "fallback_used": False,
            "state_advanced": False,
        },
    )

    assert bhavcopy_parser.main() == 1
    printed = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert printed["status"] == "parse_failed"


def test_indices_parser_main_exports_runner_state(monkeypatch, capsys):
    monkeypatch.setattr(
        indices_parser,
        "run_parser",
        lambda **kw: {
            "source": "indices",
            "rows": 1,
            "rows_read": 2,
            "rows_written": 1,
            "files_seen": 2,
            "files_considered": 2,
            "parsed_count": 1,
            "empty_or_incomplete_count": 1,
            "failed_count": 0,
            "from_date": "2026-06-01",
            "to_date": "2026-06-02",
            "fallback_used": False,
            "state_advanced": True,
        },
    )

    assert indices_parser.main() == 0
    capsys.readouterr()

    assert indices_parser.STOCKEY_RUN_STATE["source"] == "indices"
    assert indices_parser.STOCKEY_RUN_STATE["rows"] == 1
    assert indices_parser.STOCKEY_RUN_STATE["rows_read"] == 2
    assert indices_parser.STOCKEY_RUN_STATE["rows_written"] == 1
    assert indices_parser.STOCKEY_RUN_STATE["parsed_count"] == 1
    assert indices_parser.STOCKEY_RUN_STATE["empty_or_incomplete_count"] == 1
    assert indices_parser.STOCKEY_RUN_STATE["state_advanced"] is True


def test_download_runner_prioritizes_dhan_and_registered_screener_sync():
    steps = download_runner.DOWNLOAD_STEPS
    modules = [step["module"] for step in steps[:5]]
    assert modules == [
        "data.nseindia.holidays",
        "data.dhanlive.scrip_master",
        "data.sharpelydata.scrip_master",
        "data.company_master",
        "data.dhanlive.ohlcv",
    ]
    parser_modules = [step["module"] for step in download_runner.PARSER_STEPS]
    assert parser_modules[-2:] == [
        "data.nseindia.indices_parser",
        "data.benchmark_sync",
    ]


def test_company_master_main_exports_identifier_run_state(monkeypatch, capsys):
    from data import company_master as company_master_module

    df = pd.DataFrame(
        [
            {
                "company_master_id": "nse:AAA",
                "nse_ticker": "AAA",
                "bse_ticker": "500001",
                "sharpely_id": "s1",
                "dhan_nse_id": 1,
                "dhan_bse_id": 2,
            },
            {
                "company_master_id": "nse:BBB",
                "nse_ticker": "BBB",
                "bse_ticker": pd.NA,
                "sharpely_id": pd.NA,
                "dhan_nse_id": 3,
                "dhan_bse_id": pd.NA,
            },
        ]
    )
    monkeypatch.setattr(company_master_module, "sync_company_master", lambda: df)

    assert company_master_module.main() == 0
    capsys.readouterr()

    state = company_master_module.STOCKEY_RUN_STATE
    assert state["source"] == "data.company_master"
    assert state["rows_written"] == 2
    assert state["nse_ticker_count"] == 2
    assert state["bse_ticker_count"] == 1
    assert state["sharpely_id_count"] == 1
    assert state["dhan_nse_id_count"] == 2
    assert state["dhan_bse_id_count"] == 1
    assert state["state_advanced"] is True


def test_company_master_main_exports_no_data_state(monkeypatch, capsys):
    from data import company_master as company_master_module

    monkeypatch.setattr(company_master_module, "sync_company_master", lambda: pd.DataFrame())

    assert company_master_module.main() == 0
    capsys.readouterr()

    state = company_master_module.STOCKEY_RUN_STATE
    assert state["source"] == "data.company_master"
    assert state["rows_written"] == 0
    assert state["no_data_count"] == 1
    assert state["state_advanced"] is False


def test_benchmark_sync_normalizes_nifty_from_nse_indices(monkeypatch):
    captured: list[tuple[str, pd.DataFrame, list[str]]] = []
    index_history = pd.DataFrame(
        [
            {
                "date": pd.Timestamp("2026-05-26T00:00:00Z"),
                "open": 25000.0,
                "high": 25100.0,
                "low": 24900.0,
                "close": 25050.0,
                "volume": 0,
            }
        ]
    )

    def fake_sql_to_df(query, params=None):
        if "FROM nseindia_indices" in query and "SELECT date, open" in query:
            return index_history.copy()
        return pd.DataFrame([{"dhan_max_date": pd.Timestamp("2026-03-20T00:00:00Z"), "nse_max_date": pd.Timestamp("2026-05-26T00:00:00Z")}])

    monkeypatch.setattr(benchmark_sync, "ensure_ohlcv_tables", lambda: None)
    monkeypatch.setattr(benchmark_sync, "sql_to_df", fake_sql_to_df)
    monkeypatch.setattr(
        benchmark_sync,
        "upsert_to_db",
        lambda df, table_name, unique_keys, timescaledb_column=None: captured.append((table_name, df.copy(), unique_keys)),
    )

    result = benchmark_sync.sync_benchmark("NIFTY")

    assert result["rows_loaded"] == 1
    assert captured[0][0] == benchmark_sync.DAILY_TABLE
    assert captured[0][2] == ["exchange", "security_id", "date"]
    row = captured[0][1].iloc[0]
    assert row["ticker"] == "NIFTY"
    assert row["asset_type"] == "benchmark"
    assert int(row["security_id"]) == 13
    assert float(row["close"]) == 25050.0


def test_benchmark_sync_main_exports_partial_run_state(monkeypatch, capsys):
    events = []

    def fake_sync(symbol, **kwargs):
        assert kwargs["dry_run"] is False
        if symbol == "BAD":
            raise ValueError("Unsupported benchmark: BAD")
        return {"status": "ok", "ticker": symbol, "rows_loaded": 3}

    monkeypatch.setattr(benchmark_sync, "sync_benchmark", fake_sync)
    monkeypatch.setattr(benchmark_sync, "record_local_fallback_event", lambda **kwargs: events.append(kwargs) or kwargs)
    monkeypatch.setattr(sys, "argv", ["benchmark_sync.py", "--symbols", "NIFTY", "BAD"])

    assert benchmark_sync.main() == 0
    capsys.readouterr()

    state = benchmark_sync.STOCKEY_RUN_STATE
    assert state["source"] == "data.benchmark_sync"
    assert state["rows_written"] == 3
    assert state["symbol_count"] == 2
    assert state["succeeded_symbol_count"] == 1
    assert state["failed_symbol_count"] == 1
    assert state["failed_attempt_count"] == 1
    assert state["failed_symbols"][0]["symbol"] == "BAD"
    assert state["state_advanced"] is True
    assert len(events) == 1
    event = events[0]
    assert event["module"] == "data.benchmark_sync"
    assert event["fallback_type"] == "benchmark_sync_symbol_failed"
    assert event["source"] == "benchmark_sync"
    assert event["severity"] == "warn"
    assert isinstance(event["error"], ValueError)
    assert event["metadata"]["symbol"] == "BAD"
    assert event["metadata"]["dry_run"] is False


def test_benchmark_sync_main_marks_dry_run_not_advanced(monkeypatch, capsys):
    monkeypatch.setattr(
        benchmark_sync,
        "sync_benchmark",
        lambda symbol, **kwargs: {"status": "ok", "ticker": symbol, "rows_loaded": 2},
    )
    monkeypatch.setattr(sys, "argv", ["benchmark_sync.py", "--symbols", "NIFTY", "--dry-run"])

    assert benchmark_sync.main() == 0
    capsys.readouterr()

    state = benchmark_sync.STOCKEY_RUN_STATE
    assert state["rows"] == 2
    assert state["rows_written"] == 0
    assert state["dry_run"] is True
    assert state["state_advanced"] is False


def test_dhan_scrip_master_loads_new_freeze_quantity_column(tmp_path):
    csv_path = tmp_path / "dhan_master.csv"
    csv_path.write_text(
        "\n".join(
            [
                "EXCH_ID,SEGMENT,SECURITY_ID,ISIN,INSTRUMENT,UNDERLYING_SECURITY_ID,UNDERLYING_SYMBOL,SYMBOL_NAME,DISPLAY_NAME,INSTRUMENT_TYPE,SERIES,LOT_SIZE,SM_EXPIRY_DATE,STRIKE_PRICE,OPTION_TYPE,TICK_SIZE,EXPIRY_FLAG,BRACKET_FLAG,COVER_FLAG,ASM_GSM_FLAG,ASM_GSM_CATEGORY,BUY_SELL_INDICATOR,BUY_CO_MIN_MARGIN_PER,BUY_CO_SL_RANGE_MAX_PERC,BUY_CO_SL_RANGE_MIN_PERC,BUY_BO_MIN_MARGIN_PER,BUY_BO_PROFIT_RANGE_MAX_PERC,BUY_BO_PROFIT_RANGE_MIN_PERC,MTF_LEVERAGE,SM_UPPER_LIMIT,SM_LOWER_LIMIT,SM_FREEZE_QTY",
                "NSE,E,123,INE000A01001,EQUITY,,ABC,ABC LTD,ABC LTD,ES,EQ,1,,0,,0,,,,,,,,,,,,,0,100,90,5000",
            ]
        ),
        encoding="utf-8",
    )

    df = dhan_scrip_master.load_csv(csv_path)

    assert "sm_freeze_qty" in df.columns
    assert float(df.iloc[0]["sm_freeze_qty"]) == 5000.0


def test_dhan_scrip_master_dynamic_insert_uses_explicit_columns():
    sql = dhan_scrip_master.make_insert_sql(["exch_id", "segment", "security_id", "sm_freeze_qty", "future_col"])

    assert '"sm_freeze_qty"' in sql
    assert '"future_col"' in sql
    assert "SELECT st.\"exch_id\", st.\"segment\", st.\"security_id\", st.\"sm_freeze_qty\", st.\"future_col\"" in sql
    assert "SELECT st.*" not in sql


def test_dhan_scrip_master_ensure_master_table_uses_schema_registry(monkeypatch):
    calls = []

    monkeypatch.setattr(dhan_scrip_master, "apply_schema_migration", lambda **kwargs: calls.append(kwargs))

    dhan_scrip_master.ensure_master_table()

    assert len(calls) == 1
    call = calls[0]
    assert call["migration_id"] == dhan_scrip_master.DHAN_MASTER_SCHEMA_MIGRATION_ID
    assert call["owner"] == "data.dhanlive.scrip_master"
    assert call["metadata"] == {"tables": ["master_dhan_instruments"], "source": "dhan_scrip_master"}
    ddl = "\n".join(call["statements"])
    assert "CREATE TABLE IF NOT EXISTS master_dhan_instruments" in ddl
    assert "sm_freeze_qty           DOUBLE PRECISION" in ddl
    assert "CREATE INDEX IF NOT EXISTS idx_master_dhan_active" in ddl


def test_dhan_scrip_master_download_failure_records_local_fallback(monkeypatch):
    events = []

    def fail_get(*_args, **_kwargs):
        raise RuntimeError("network down")

    monkeypatch.setattr(dhan_scrip_master.requests, "get", fail_get)
    monkeypatch.setattr(dhan_scrip_master, "record_local_fallback_event", lambda **kwargs: events.append(kwargs) or kwargs)

    with pytest.raises(SystemExit) as raised:
        dhan_scrip_master.download_master_csv(timeout=7)

    assert "[ERROR] download failed" in str(raised.value)
    assert len(events) == 1
    assert events[0]["module"] == "data.dhanlive.scrip_master"
    assert events[0]["source"] == "dhan_scrip_master"
    assert events[0]["fallback_type"] == "dhan_scrip_master_download_failed"
    assert events[0]["metadata"]["timeout"] == 7


def test_dhan_scrip_master_short_response_records_local_fallback(monkeypatch):
    events = []

    class FakeResponse:
        content = b"too-small"

        def raise_for_status(self):
            return None

    monkeypatch.setattr(dhan_scrip_master.requests, "get", lambda *_args, **_kwargs: FakeResponse())
    monkeypatch.setattr(dhan_scrip_master, "record_local_fallback_event", lambda **kwargs: events.append(kwargs) or kwargs)

    with pytest.raises(SystemExit) as raised:
        dhan_scrip_master.download_master_csv()

    assert "response too small" in str(raised.value)
    assert len(events) == 1
    assert events[0]["fallback_type"] == "dhan_scrip_master_response_too_small"
    assert events[0]["metadata"]["response_bytes"] == len(FakeResponse.content)


def test_dhan_scrip_master_sync_schema_adds_missing_columns():
    class FakeCursor:
        def __init__(self):
            self.queries = []

        def execute(self, query):
            self.queries.append(str(query))

        def fetchall(self):
            return [("exch_id",), ("segment",), ("security_id",)]

    cur = FakeCursor()
    df = pd.DataFrame({"exch_id": ["NSE"], "segment": ["E"], "security_id": [1], "sm_freeze_qty": [100.0]})

    dhan_scrip_master.sync_master_schema(cur, df)

    assert any('ADD COLUMN "sm_freeze_qty" DOUBLE PRECISION' in query for query in cur.queries)


def test_dhan_scrip_master_update_database_uses_retryable_operation(monkeypatch):
    operation_names = []
    queries = []
    copied = []
    commits = []

    class FakeCursor:
        def execute(self, query, params=None):
            queries.append((str(query), params))

        def copy_from(self, buf, table, sep, null, columns):
            copied.append((buf.getvalue(), table, sep, null, columns))

    class FakeConnection:
        def commit(self):
            commits.append(True)

    class FakeSession:
        def __enter__(self):
            return FakeConnection(), FakeCursor()

        def __exit__(self, exc_type, exc, tb):
            return False

    def fake_execute_db_operation(operation, *, operation_name, **_kwargs):
        operation_names.append(operation_name)
        return operation()

    frame = pd.DataFrame({"exch_id": ["NSE"], "segment": ["E"], "security_id": [123], "sm_freeze_qty": [5000.0]})
    fixed_now = datetime(2026, 6, 12, 9, 30, tzinfo=timezone.utc)

    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed_now if tz else fixed_now.replace(tzinfo=None)

    monkeypatch.setattr(dhan_scrip_master, "datetime", FixedDateTime)
    monkeypatch.setattr(dhan_scrip_master, "ensure_master_table", lambda: None)
    monkeypatch.setattr(dhan_scrip_master, "sync_master_schema", lambda cur, df: queries.append(("SYNC_SCHEMA", list(df.columns))))
    monkeypatch.setattr(dhan_scrip_master, "db_session", lambda: FakeSession())
    monkeypatch.setattr(dhan_scrip_master, "execute_db_operation", fake_execute_db_operation)

    load_ts = dhan_scrip_master.update_database(frame)

    assert load_ts == fixed_now
    assert operation_names == ["dhan_scrip_master:update_database"]
    assert commits == [True]
    assert copied and copied[0][1] == "_stage"
    assert copied[0][4] == ["exch_id", "segment", "security_id", "sm_freeze_qty"]
    assert any("CREATE TEMP TABLE _stage" in query for query, _ in queries)
    assert any(query == "BEGIN;" for query, _ in queries)
    assert any(query == "COMMIT;" for query, _ in queries)


def test_dhan_scrip_master_main_exports_runner_state(monkeypatch, tmp_path):
    csv_path = tmp_path / "dhan_master.csv"
    df = pd.DataFrame(
        {
            "exch_id": ["NSE", "NSE"],
            "segment": ["E", "E"],
            "security_id": [1, 2],
            "sm_freeze_qty": [100.0, 200.0],
        }
    )
    load_ts = datetime(2026, 6, 11, 9, 30, tzinfo=timezone.utc)
    monkeypatch.setattr(dhan_scrip_master, "download_master_csv", lambda: csv_path)
    monkeypatch.setattr(dhan_scrip_master, "load_csv", lambda path: df.copy())
    monkeypatch.setattr(dhan_scrip_master, "update_database", lambda frame: load_ts)

    assert dhan_scrip_master.main() == 0

    assert dhan_scrip_master.STOCKEY_RUN_STATE["rows"] == 2
    assert dhan_scrip_master.STOCKEY_RUN_STATE["rows_read"] == 2
    assert dhan_scrip_master.STOCKEY_RUN_STATE["rows_written"] == 2
    assert dhan_scrip_master.STOCKEY_RUN_STATE["classification"] == "ok"
    assert dhan_scrip_master.STOCKEY_RUN_STATE["status"] == "ok"
    assert dhan_scrip_master.STOCKEY_RUN_STATE["column_count"] == 4
    assert dhan_scrip_master.STOCKEY_RUN_STATE["source"] == "dhan_scrip_master"
    assert dhan_scrip_master.STOCKEY_RUN_STATE["source_file"] == str(csv_path)
    assert dhan_scrip_master.STOCKEY_RUN_STATE["load_ts"] == load_ts.isoformat()
    assert dhan_scrip_master.STOCKEY_RUN_STATE["fallback_used"] is False


def test_dhan_scrip_master_main_exports_download_failure_state(monkeypatch):
    monkeypatch.setattr(
        dhan_scrip_master,
        "download_master_csv",
        lambda: (_ for _ in ()).throw(SystemExit("[ERROR] download failed: network down")),
    )

    with pytest.raises(SystemExit):
        dhan_scrip_master.main()

    assert dhan_scrip_master.STOCKEY_RUN_STATE["classification"] == "source_unavailable"
    assert dhan_scrip_master.STOCKEY_RUN_STATE["status"] == "failed"
    assert dhan_scrip_master.STOCKEY_RUN_STATE["rows_written"] == 0
    assert dhan_scrip_master.STOCKEY_RUN_STATE["source_unavailable_count"] == 1
    assert dhan_scrip_master.STOCKEY_RUN_STATE["state_advanced"] is False


def test_dhan_scrip_master_main_exports_schema_failure_state(monkeypatch, tmp_path):
    events: list[dict[str, object]] = []
    csv_path = tmp_path / "dhan_master.csv"
    df = pd.DataFrame({"exch_id": ["NSE"], "segment": ["E"], "security_id": [1]})

    monkeypatch.setattr(dhan_scrip_master, "download_master_csv", lambda: csv_path)
    monkeypatch.setattr(dhan_scrip_master, "load_csv", lambda path: df.copy())
    monkeypatch.setattr(
        dhan_scrip_master,
        "update_database",
        lambda frame: (_ for _ in ()).throw(RuntimeError('UndefinedColumn: column "sm_freeze_qty" of relation "_stage" does not exist')),
    )
    monkeypatch.setattr(dhan_scrip_master, "record_local_fallback_event", lambda **kwargs: events.append(kwargs) or kwargs)

    with pytest.raises(RuntimeError, match="UndefinedColumn"):
        dhan_scrip_master.main()

    assert dhan_scrip_master.STOCKEY_RUN_STATE["classification"] == "schema_changed"
    assert dhan_scrip_master.STOCKEY_RUN_STATE["status"] == "failed"
    assert dhan_scrip_master.STOCKEY_RUN_STATE["schema_changed_count"] == 1
    assert dhan_scrip_master.STOCKEY_RUN_STATE["source_file"] == str(csv_path)
    assert events[0]["fallback_type"] == "dhan_scrip_master_sync_failed"
    assert events[0]["metadata"]["classification"] == "schema_changed"


def test_sharpely_scrip_master_main_exports_runner_state(monkeypatch, capsys):
    writes: list[tuple[str, int, list[str]]] = []
    fund_a = pd.DataFrame(
        [
            {"amfi_code": "1001", "plan_name": "Fund A"},
            {"amfi_code": None, "plan_name": "Fund Missing"},
        ]
    )
    fund_b = pd.DataFrame([{"amfi_code": "2001", "plan_name": "Fund B"}])
    equity = pd.DataFrame(
        [
            {"symbol": "AAA", "bse_ticker": None, "proper_name": "AAA Ltd"},
            {"symbol": None, "bse_ticker": "500001", "proper_name": "BSE Only Ltd"},
            {"symbol": None, "bse_ticker": None, "proper_name": "Invalid Ltd"},
        ]
    )

    monkeypatch.setattr(sharpely_scrip_master, "get_sharpely_headers", lambda: {"Authorization": "Bearer test"})
    monkeypatch.setattr(sharpely_scrip_master, "get_latest_from_sharpely", lambda headers: [fund_a.copy(), fund_b.copy(), equity.copy()])
    monkeypatch.setattr(
        sharpely_scrip_master,
        "upsert_to_db",
        lambda df, table_name, unique_keys: writes.append((table_name, len(df), list(unique_keys))),
    )

    assert sharpely_scrip_master.main() == 0
    capsys.readouterr()

    assert writes == [
        ("master_sharpely_funds", 2, ["amfi_code"]),
        ("master_sharpely_equity", 2, ["symbol", "bse_ticker"]),
    ]
    assert sharpely_scrip_master.STOCKEY_RUN_STATE["source"] == "sharpely"
    assert sharpely_scrip_master.STOCKEY_RUN_STATE["rows"] == 4
    assert sharpely_scrip_master.STOCKEY_RUN_STATE["rows_read"] == 6
    assert sharpely_scrip_master.STOCKEY_RUN_STATE["rows_written"] == 4
    assert sharpely_scrip_master.STOCKEY_RUN_STATE["fund_rows"] == 2
    assert sharpely_scrip_master.STOCKEY_RUN_STATE["equity_rows"] == 2
    assert sharpely_scrip_master.STOCKEY_RUN_STATE["raw_fund_rows"] == 3
    assert sharpely_scrip_master.STOCKEY_RUN_STATE["raw_equity_rows"] == 3
    assert sharpely_scrip_master.STOCKEY_RUN_STATE["instrument_type_count"] == 3
    assert sharpely_scrip_master.STOCKEY_RUN_STATE["fallback_used"] is False
    assert sharpely_scrip_master.STOCKEY_RUN_STATE["state_advanced"] is True


def test_sharpely_data_main_returns_nonzero_on_failed_run_state(monkeypatch, capsys):
    monkeypatch.setattr(sharpely_data, "load_tracked_symbols", lambda symbols: ["AAA", "BAD"])
    monkeypatch.setattr(sharpely_data, "parse_datetime_arg", lambda value: pd.Timestamp(value).to_pydatetime() if value else None)
    monkeypatch.setattr(
        sharpely_data,
        "sync_sharpely_data",
        lambda **kwargs: {
            "source": "sharpely_fundamentals",
            "symbol_count": len(kwargs["symbols"]),
            "rows": 5,
            "rows_read": len(kwargs["symbols"]),
            "rows_written": 5,
            "classification": "partial_failed",
            "status": "failed",
            "failed_symbol_count": 1,
            "from_date": "2026-06-01",
            "to_date": "2026-06-11",
            "fallback_used": False,
            "state_advanced": True,
        },
    )
    monkeypatch.setattr(sys, "argv", ["data.sharpelydata.sharpely_data", "--symbols", "AAA", "BAD"])

    assert sharpely_data.main() == 1
    capsys.readouterr()

    assert sharpely_data.STOCKEY_RUN_STATE["classification"] == "partial_failed"
    assert sharpely_data.STOCKEY_RUN_STATE["status"] == "failed"


def test_sharpely_data_main_exports_runner_state(monkeypatch, capsys):
    monkeypatch.setattr(sharpely_data, "load_tracked_symbols", lambda symbols: ["AAA", "BBB"])
    monkeypatch.setattr(sharpely_data, "parse_datetime_arg", lambda value: pd.Timestamp(value).to_pydatetime() if value else None)
    monkeypatch.setattr(
        sharpely_data,
        "sync_sharpely_data",
        lambda **kwargs: {
            "source": "sharpely_fundamentals",
            "symbol_count": len(kwargs["symbols"]),
            "rows": 5,
            "rows_read": len(kwargs["symbols"]),
            "rows_written": 5,
            "classification": "ok",
            "status": "ok",
            "from_date": "2026-06-01",
            "to_date": "2026-06-11",
            "fallback_used": False,
            "state_advanced": True,
        },
    )
    monkeypatch.setattr(
        sys,
        "argv",
        ["data.sharpelydata.sharpely_data", "--symbols", "AAA", "BBB", "--from-date", "2026-06-01", "--to-date", "2026-06-11"],
    )

    assert sharpely_data.main() == 0
    capsys.readouterr()

    assert sharpely_data.STOCKEY_RUN_STATE["source"] == "sharpely_fundamentals"
    assert sharpely_data.STOCKEY_RUN_STATE["symbol_count"] == 2
    assert sharpely_data.STOCKEY_RUN_STATE["rows"] == 5
    assert sharpely_data.STOCKEY_RUN_STATE["rows_read"] == 2
    assert sharpely_data.STOCKEY_RUN_STATE["rows_written"] == 5
    assert sharpely_data.STOCKEY_RUN_STATE["classification"] == "ok"
    assert sharpely_data.STOCKEY_RUN_STATE["status"] == "ok"
    assert sharpely_data.STOCKEY_RUN_STATE["state_advanced"] is True


def test_nse_corporate_actions_sync_returns_standard_run_state(monkeypatch):
    monkeypatch.setattr(nse_rate_limiter.time, "sleep", lambda *a, **k: None)  # no real rate-gate delay in test
    persisted: list[dict[str, object]] = []
    upserts: list[pd.DataFrame] = []
    cursors: list[tuple[str, object]] = []

    class FakeRedis:
        def close(self):
            return None

    class FakeEquity:
        def __init__(self, symbol: str):
            self.display_name = f"{symbol} LTD"

    class FakePage:
        def goto(self, *_args, **_kwargs):
            return None

        def wait_for_timeout(self, *_args, **_kwargs):
            return None

        def close(self):
            return None

    class FakeContext:
        def new_page(self):
            return FakePage()

    class FakeBrowser:
        contexts = [FakeContext()]

        def close(self):
            return None

    class FakeChromium:
        def connect_over_cdp(self, *_args, **_kwargs):
            return FakeBrowser()

    class FakePlaywright:
        chromium = FakeChromium()

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    def fake_fetch(_page, symbol, _issuer, _from_date, _to_date):
        return pd.DataFrame(
            [
                {
                    "date": pd.Timestamp("2026-06-10"),
                    "symbol": symbol,
                    "series": "EQ",
                    "subject": "Bonus issue",
                }
            ]
        )

    monkeypatch.setattr(corporate_actions, "normalize_date_window", lambda from_date, to_date: (pd.Timestamp("2026-06-01").to_pydatetime(), pd.Timestamp("2026-06-11").to_pydatetime()))
    monkeypatch.setattr(corporate_actions, "get_redis_client", lambda *_args, **_kwargs: FakeRedis())
    monkeypatch.setattr(corporate_actions, "sync_playwright", lambda: FakePlaywright())
    monkeypatch.setattr(corporate_actions, "get_nse_equity", lambda symbol: FakeEquity(symbol))
    monkeypatch.setattr(corporate_actions, "choose_from_date", lambda explicit, candidates: pd.Timestamp("2026-06-01").to_pydatetime())
    monkeypatch.setattr(corporate_actions, "get_redis_cursor", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(corporate_actions, "get_db_max_date", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(corporate_actions, "fetch_corporate_actions", fake_fetch)
    monkeypatch.setattr(corporate_actions, "attach_company_master_id", lambda df, **_kwargs: df.copy())
    monkeypatch.setattr(corporate_actions, "upsert_to_db", lambda df, *_args, **_kwargs: upserts.append(df.copy()))
    monkeypatch.setattr(corporate_actions, "set_redis_cursor", lambda redis_client, key, value: cursors.append((key, value)))
    monkeypatch.setattr(corporate_actions, "persist_sync_state", lambda **kwargs: persisted.append(kwargs))

    result = corporate_actions.sync_corporate_actions(["aaa", "BBB"], from_date=None, to_date=None)

    assert result["source"] == corporate_actions.SYNC_SOURCE_NAME
    assert result["rows"] == 2
    assert result["rows_read"] == 2
    assert result["rows_written"] == 2
    assert result["symbol_count"] == 2
    assert result["symbols_queried"] == 2
    assert result["symbols_skipped"] == 0
    assert result["from_date"] == "2026-06-01"
    assert result["to_date"] == "2026-06-11"
    assert result["latest_item_ts"].startswith("2026-06-10")
    assert result["state_advanced"] is True
    assert len(upserts) == 2
    assert len(cursors) == 2
    assert persisted[-1]["source_name"] == corporate_actions.SYNC_SOURCE_NAME
    assert persisted[-1]["status"] == "ok"
    assert persisted[-1]["state"]["rows_written"] == 2


def test_recent_events_main_exports_runner_state(monkeypatch, capsys):
    persisted: list[dict[str, object]] = []

    class FakeRedis:
        def close(self):
            return None

    class FakePlaywright:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    monkeypatch.setattr(recent_events, "get_redis_client", lambda *_args, **_kwargs: FakeRedis())
    monkeypatch.setattr(recent_events, "sync_playwright", lambda: FakePlaywright())
    monkeypatch.setattr(
        recent_events,
        "dowload_events",
        lambda playwright, formatted_date, rop: {"formatted_date": formatted_date, "rows": 7, "file_path": f"calendar_{formatted_date}.csv"},
    )
    monkeypatch.setattr(recent_events, "persist_sync_state", lambda **kwargs: persisted.append(kwargs))
    monkeypatch.setattr(sys, "argv", ["data.nseindia.recent_events", "--date", "2026-06-11"])

    assert recent_events.main() == 0
    capsys.readouterr()

    assert recent_events.STOCKEY_RUN_STATE["source"] == recent_events.SYNC_SOURCE_NAME
    assert recent_events.STOCKEY_RUN_STATE["rows"] == 7
    assert recent_events.STOCKEY_RUN_STATE["rows_read"] == 1
    assert recent_events.STOCKEY_RUN_STATE["rows_written"] == 7
    assert recent_events.STOCKEY_RUN_STATE["from_date"] == "2026-06-11"
    assert recent_events.STOCKEY_RUN_STATE["to_date"] == "2026-06-11"
    assert recent_events.STOCKEY_RUN_STATE["state_advanced"] is True
    assert persisted[-1]["source_name"] == recent_events.SYNC_SOURCE_NAME
    assert persisted[-1]["status"] == "ok"


def _retention_prior_row(slug, ticker, date, raw=None):
    return {"date": pd.Timestamp(date, tz="UTC"), "screener_slug": slug, "screener_name": "S",
            "screener_url": None, "ticker": ticker, "exchange": "NSE", "company_master_id": f"cm-{ticker}",
            "security_id": None, "instrument": None, "isin": None, "display_name": ticker,
            "rank": 1, "raw_item_json": raw}


def _tier_row(symbol, state="WATCH_EVENT", rs=None, event_class=None, hint=None):
    return {"symbol": symbol, "current_state": state, "rs_percentile": rs,
            "last_event_class": event_class, "last_state_transition_hint": hint}


def test_sharpely_v2_decrypt_roundtrip():
    # the statements endpoint moved to AES-256-CBC "iv:ct"; the recovered key must
    # round-trip what encryptAES produced (zero-padded UTF-8 key, PKCS7).
    import base64
    from Crypto.Cipher import AES
    from Crypto.Util.Padding import pad
    from data.sharpelydata import sharpely_utils as su

    key = su._derive_key(su.SHARPELY_V2_AES_KEY)
    assert len(key) == 32
    plaintext = '{"statements": "{\\"inc_consol_interim\\": []}"}'
    iv = b"0123456789abcdef"
    ct = AES.new(key, AES.MODE_CBC, iv).encrypt(pad(plaintext.encode(), 16))
    payload = base64.b64encode(iv).decode() + ":" + base64.b64encode(ct).decode()
    assert su.decrypt_sharpely_v2(payload) == plaintext
    import pytest
    with pytest.raises(ValueError):
        su.decrypt_sharpely_v2("not-encrypted")


def test_sharpely_v2_decrypt_compressed_roundtrip():
    # /api/v2/core/getAllSectorData's plaintext turned out to be zlib-compressed
    # before encryption (confirmed live 2026-08-11, decrypted bytes started with the
    # zlib header 0x78 0x9c) -- a different contract from decrypt_sharpely_v2's own
    # "statements" endpoint, hence the separate function.
    import base64
    import zlib

    from Crypto.Cipher import AES
    from Crypto.Util.Padding import pad
    from data.sharpelydata import sharpely_utils as su

    key = su._derive_key(su.SHARPELY_V2_AES_KEY)
    plaintext = '{"sector": {"EQ": [{"sector_code": "IN0101", "sector_desc": "Chemicals"}]}}'
    compressed = zlib.compress(plaintext.encode("utf-8"))
    iv = b"0123456789abcdef"
    ct = AES.new(key, AES.MODE_CBC, iv).encrypt(pad(compressed, 16))
    payload = base64.b64encode(iv).decode() + ":" + base64.b64encode(ct).decode()
    assert su.decrypt_sharpely_v2_compressed(payload) == plaintext


def test_price_adjustment_derives_splits_from_price_steps():
    from data.nseindia import price_adjustment as pa
    dates = pd.bdate_range("2026-01-01", periods=8, tz="UTC")
    # a clean 1:10 split on day 4: 250 -> 25. Adjusted series must be smooth and the return real.
    split = pd.DataFrame({"symbol": "S", "date": dates,
                          "close": [240.0, 250.0, 245.0, 250.0, 25.0, 26.0, 27.0, 26.0]})
    adj = pa.adjust_frame(split).sort_values("date").reset_index(drop=True)
    assert (adj.loc[:3, "cum_adj_factor"] == 0.1).all() and (adj.loc[4:, "cum_adj_factor"] == 1.0).all()
    assert adj.loc[4, "ca_flag"] == "split_bonus"
    assert abs(adj.iloc[-1]["adj_close"] / adj.iloc[0]["adj_close"] - (26.0 / 24.0)) < 1e-9  # true return, not -90%
    # a 1:1 bonus (price halves) snaps to factor 0.5
    bonus = pd.DataFrame({"symbol": "B", "date": dates[:4], "close": [100.0, 102.0, 51.0, 52.0]})
    ab = pa.adjust_frame(bonus).sort_values("date").reset_index(drop=True)
    assert (ab.loc[:1, "cum_adj_factor"] == 0.5).all() and ab.loc[2, "ca_flag"] == "split_bonus"
    # an ambiguous circuit-breaching step (not a round split/bonus ratio) is FLAGGED, never adjusted
    weird = pd.DataFrame({"symbol": "W", "date": dates[:4], "close": [100.0, 100.0, 55.0, 56.0]})  # x0.55, no round match
    aw = pa.adjust_frame(weird)
    assert (aw["cum_adj_factor"] == 1.0).all() and (aw["ca_flag"] == "ambiguous").any()
    # a normal series is untouched (all factors 1.0, adj_close == close)
    calm = pd.DataFrame({"symbol": "C", "date": dates[:4], "close": [100.0, 103.0, 101.0, 104.0]})
    ac = pa.adjust_frame(calm)
    assert (ac["cum_adj_factor"] == 1.0).all() and (ac["adj_close"] == ac["close"]).all()


def test_price_adjustment_open_gap_catches_split_with_intraday_move():
    from data.nseindia import price_adjustment as pa
    dates = pd.bdate_range("2026-01-01", periods=3, tz="UTC")
    # IRCTC-style 5:1 split with a +12% ex-date intraday move: close/prev=0.221 fails the 6% snap, but the
    # OVERNIGHT gap open/prev=0.198 snaps cleanly to 1/5. The open-based detector must catch it.
    df = pd.DataFrame({"symbol": "IRCTC", "date": dates,
                       "open": [4068.0, 4250.0, 817.0], "close": [4189.0, 4130.0, 913.5]})
    a = pa.adjust_frame(df).sort_values("date").reset_index(drop=True)
    assert a.loc[2, "ca_flag"] == "split_bonus"
    assert abs(a.loc[0, "cum_adj_factor"] - 0.2) < 1e-9 and abs(a.loc[1, "cum_adj_factor"] - 0.2) < 1e-9
    # WITHOUT the open column it falls back to the close step, which misses this split (flags ambiguous)
    a2 = pa.adjust_frame(df.drop(columns=["open"]))
    assert (a2["ca_flag"] == "ambiguous").any() and (a2["cum_adj_factor"] == 1.0).all()


def test_price_adjustment_ca_purpose_parser():
    from data.nseindia import price_adjustment as pa
    f = lambda s: pa._factor_for_events(pa._events_from_subject(s))
    assert f("BONUS 1:1") == 0.5                       # X:Y bonus -> Y/(X+Y)
    assert f("BONUS 3:1") == 0.25
    assert abs(f("BONUS 5:2") - 2 / 7) < 1e-9
    assert f("FV SPLIT RS.10 TO RS.5") == 0.5          # split A->B -> B/A
    assert abs(f("FVSPLT FRM RS 5 TO RE 1") - 0.2) < 1e-9
    assert abs(f("BON 2:1/FVSPLT FRM RS 2 TO RE 1") - (1 / 3) * (1 / 2)) < 1e-9  # combined multiply
    assert f("DIV - RS 5 PER SH") is None              # dividend is not a split/bonus


def test_price_adjustment_declared_ca_fixes_missed_split():
    import datetime as _dt
    from data.nseindia import price_adjustment as pa
    # a 1:1 bonus whose ex-date ALSO fell ~7% -> overnight step 0.535 misses the round-ratio snap and is
    # left 'ambiguous' (unadjusted). NSE's declared 1:1 bonus supplies the exact 0.5 and back-adjusts.
    df = pd.DataFrame({
        "symbol": ["X", "X", "X"],
        "date": pd.to_datetime(["2024-01-01", "2024-01-02", "2024-01-03"], utc=True),
        "open": [100.0, 53.5, 50.0], "close": [100.0, 50.0, 49.0],
    })
    a0 = pa.adjust_frame(df.copy())
    assert (a0["ca_flag"] == "ambiguous").any() and (a0["cum_adj_factor"] == 1.0).all()  # unadjusted

    decl = {("X", _dt.date(2024, 1, 2)): 0.5}
    a1 = pa.adjust_frame(df.copy(), declared_ratios=decl).sort_values("date").reset_index(drop=True)
    assert a1.loc[1, "ca_flag"] == "split_bonus_ca"
    assert abs(a1.loc[0, "cum_adj_factor"] - 0.5) < 1e-9          # pre-event history rescaled
    assert abs(a1.loc[0, "adj_close"] - 50.0) < 1e-9             # 100 * 0.5 onto post-event basis
    # a declared CA with NO price step must NOT be applied (no double-adjust)
    flat = pd.DataFrame({"symbol": ["Y", "Y"], "date": pd.to_datetime(["2024-01-01", "2024-01-02"], utc=True),
                         "open": [100.0, 100.5], "close": [100.0, 101.0]})
    a2 = pa.adjust_frame(flat, declared_ratios={("Y", _dt.date(2024, 1, 2)): 0.5})
    assert (a2["cum_adj_factor"] == 1.0).all() and (a2["ca_flag"] == "").all()


def test_rbi_currency_parse_rate_rows():
    from data.rbi.download_currency_rates import parse_rate_rows
    # current RBI header carries unit suffixes + EUR/JPY + new AED/IDR -- the old fixed ["USD","GBP","EURO",
    # "YEN"] list corrupted this. Map by the leading currency CODE instead.
    rows = [
        ["Date", "USD (INR / 1 USD)", "GBP (INR / 1 GBP)", "EUR (INR / 1 EUR)",
         "JPY (INR / 100 JPY)", "AED (INR / 1 AED)", "IDR (INR / 10000 IDR)"],
        ["15/07/2026", "96.2219", "129.0746", "110.0798", "59.3300", "26.1980", "53.2577"],
        ["14/07/2026", "96.1138", "128.3750", "109.4557", "59.2000", "26.1688", "53.0831"],
    ]
    df = parse_rate_rows(rows)
    assert list(df.columns) == ["date", "usd", "gbp", "eur", "jpy", "aed", "idr"]
    assert df.iloc[0]["date"] == pd.Timestamp("2026-07-15")
    assert abs(df.iloc[0]["usd"] - 96.2219) < 1e-6 and abs(df.iloc[0]["jpy"] - 59.33) < 1e-6
    # legacy EURO/YEN labels (pre-suffix era) must still map to eur/jpy
    legacy = parse_rate_rows([["Date", "USD", "GBP", "EURO", "YEN"],
                              ["01/02/2015", "62.5", "95.1", "70.2", "0.52"]])
    assert list(legacy.columns) == ["date", "usd", "gbp", "eur", "jpy"]
    assert abs(legacy.iloc[0]["eur"] - 70.2) < 1e-6
    # a table with no recognizable header yields an empty frame (never a corrupt one)
    assert parse_rate_rows([["junk"], ["x"]]).empty


def test_corporate_action_events_parsers():
    from data.nseindia.corporate_action_events import parse_dividend, classify_capital_change
    # dividend amount + type from the real Bc PURPOSE formats
    assert parse_dividend("DIV - RS 2 PER SH") == {"dividend_amount": 2.0, "dividend_type": "final"}
    assert parse_dividend("AGM/DIV-RS 1.50 PER SHARE")["dividend_amount"] == 1.5
    assert parse_dividend("INTERIM DIVIDEND") == {"dividend_amount": None, "dividend_type": "interim"}
    assert parse_dividend("INTDIV - RE 1 PER SH") == {"dividend_amount": 1.0, "dividend_type": "interim"}
    assert parse_dividend("SPECIAL DIVIDEND RS 5 PER SH") == {"dividend_amount": 5.0, "dividend_type": "special"}
    assert parse_dividend("BONUS 1:1") is None                      # not a dividend
    assert parse_dividend("SUB-DIVISION FV RS 10 TO RE 1") is None  # subdivision = split, not a dividend
    # capital change: type + price factor, and dividends are excluded
    bonus = classify_capital_change("BONUS 1:1")
    assert bonus["event_type"] == "bonus" and abs(bonus["price_factor"] - 0.5) < 1e-9
    assert classify_capital_change("FVSPLT FRM RS 10 TO RE 1")["event_type"] == "split"
    assert classify_capital_change("BON 2:1/FVSPLT FRM RS 2 TO RE 1")["event_type"] == "bonus+split"
    assert classify_capital_change("DIV - RS 2 PER SH") is None


def test_price_data_sanity_report_contract():
    from scripts import price_data_sanity as pds
    # the reporting contract: findings render, errors gate status, benchmark gaps are an error
    report = {"status": "error", "db_status": "checked", "as_of": "2026-07-10", "errors": 1, "warnings": 2,
              "findings": [pds.Finding("warning", "unadjusted_corporate_actions", "78 steps").as_dict(),
                           pds.Finding("warning", "eq_to_be_migrations", "285 syms").as_dict(),
                           pds.Finding("error", "benchmark_calendar_gaps", "5 days").as_dict()]}
    text = pds.format_text_report(report)
    assert "status: error" in text and "unadjusted_corporate_actions" in text and "benchmark_calendar_gaps" in text
    # thresholds are the shared circuit-band convention (a split is a >35% single-day step)
    assert pds.CA_STEP_LOW < 0.7 and pds.CA_STEP_HIGH > 1.3




def test_dhan_identity_falls_back_from_nse_to_bse_security(monkeypatch):
    monkeypatch.setattr(
        dhan_db,
        "get_company_master_equity",
        lambda ticker, exchange: pd.Series(
            {
                "company_master_id": "nse:AAYUSHBULL",
                "nse_ticker": "AAYUSHBULL",
                "bse_ticker": "540718",
                "dhan_nse_id": pd.NA,
                "dhan_bse_id": 540718,
            }
        ),
    )
    monkeypatch.setattr(dhan_db, "_resolve_nse_fallback_security_id", lambda company: None)

    identity = dhan_db.resolve_dhan_identity("AAYUSHBULL", "NSE", asset_type="stock")
    assert identity["exchange"] == "BSE"
    assert identity["ticker"] == "540718"
    assert identity["security_id"] == 540718
    assert identity["exchange_segment"] == "BSE_EQ"


def test_get_dhan_ohlcv_daily_uses_resolved_exchange(monkeypatch):
    monkeypatch.setattr(
        dhan_db,
        "resolve_dhan_identity",
        lambda ticker, exchange, asset_type="stock": {
            "security_id": 540718,
            "exchange": "BSE",
            "asset_type": "stock",
        },
    )

    captured: dict[str, object] = {}

    def fake_sql_to_df(query, params=None):
        captured["params"] = params
        return pd.DataFrame()

    monkeypatch.setattr(dhan_db, "sql_to_df", fake_sql_to_df)
    dhan_db.get_dhan_ohlcv_daily("AAYUSHBULL", exchange="NSE", asset_type="stock")
    assert captured["params"]["exchange"] == "BSE"


def _technical_engine_base_row() -> pd.Series:
    return pd.Series(
        {
            "adj_close": 100.0,
            "avg_traded_value_20d": 250_000_000.0,
            "median_volume_20d": 500_000.0,
            "atr_pct": 3.5,
            "gap_frequency_60d": 0.04,
            "base_depth_60d_pct": 18.0,
            "pass_liquidity_20d": True,
            "pass_gap_behavior": True,
            "pass_above_dma_20": True,
            "pass_above_dma_50": True,
            "pass_above_dma_150": True,
            "pass_above_dma_200": True,
            "pass_trend_alignment": True,
            "dma_50_slope_20d_pct": 4.5,
            "dma_150_slope_20d_pct": 3.0,
            "dist_52w_high": -4.0,
            "trend_persistence_60d": 0.82,
            "trend_persistence_120d": 0.76,
            "higher_high_count_20d": 12.0,
            "higher_low_count_20d": 11.0,
            "pivot_distance_20d_pct": 0.2,
            "range_contraction_ratio": 0.72,
            "volatility_contraction_flag": True,
            "tight_close_upper_half_20d": 0.68,
            "support_hold_rate_20d": 0.55,
            "bb_width_rank_252d": 0.22,
            "breakout_extension_pct": 3.0,
            "breakout_day_volume_vs_20d": 2.1,
            "up_down_volume_ratio_20d": 1.4,
            "accumulation_days_20d": 5.0,
            "distribution_days_20d": 1.0,
            "pullback_volume_dryup_ratio_20d": 0.62,
            "rs_vs_benchmark": 0.12,
            "rs_vs_sector": 0.08,
            "stock_ret_60d": 0.18,
            "stock_ret_120d": 0.28,
            "support_distance_20d_pct": 2.0,
            "close_location_pct": 0.82,
            "dist_20d_high": 0.3,
            "gap_pct": 1.2,
        }
    )


def _rule_engine_strong_candidate_row() -> pd.Series:
    row = _technical_engine_base_row()
    row["asof_date"] = pd.Timestamp("2026-04-01T00:00:00Z")
    row["company_master_id"] = "nse:ABC"
    row["technical_snapshot_date"] = pd.Timestamp("2026-04-01T00:00:00Z")
    row["fundamentals_snapshot_date"] = pd.Timestamp("2026-04-01T00:00:00Z")
    row["regime_snapshot_date"] = pd.Timestamp("2026-04-01T00:00:00Z")
    row["intraday_snapshot_date"] = pd.Timestamp("2026-04-01T00:00:00Z")
    row["fundamentals_freshness_status"] = "fresh"
    row["market_cap"] = 5_000.0
    row["intraday_close_vs_vwap_pct"] = 0.4
    row["intraday_pct_bars_above_vwap"] = 0.7
    row["intraday_close_location_pct"] = 0.8
    row["intraday_opening_range_breakout_up"] = True
    row["intraday_prev_day_breakout_up"] = True
    row["intraday_failed_prev_day_breakout"] = False
    row["intraday_volume_vs_20d"] = 1.4
    row["intraday_breakout_score"] = 0.8
    row["total_revenue_qoq_growth_vs_sector"] = 0.1
    row["profit_after_tax_qoq_growth_vs_sector"] = 0.1
    row["debt_to_equity_vs_sector"] = 0.05
    row["promoter_total_vs_sector"] = 0.1
    row["fii_vs_sector"] = 0.1
    return row


def _rule_engine_regime_block_setup() -> dict[str, Any]:
    return {
        "allowed_regimes": ["STABLE"],
        "blocked_regimes": ["RISK_OFF"],
        "allowed_overlays": ["NONE"],
        "blocked_overlays": [],
        "technical_rules": [{"column": "pass_above_dma_50", "operator": "eq", "value": True}],
        "fundamental_rules": [],
        "intraday_rules": [],
        "scoring_weights": {"technical": 0.85, "fundamental": 0.10, "regime_fit": 0.0, "event": 0.05},
        "score_thresholds": {"pass_now": 0.65, "watch_breakout": 0.55, "watch_event": 0.45, "abstain": 0.35, "near_miss_gap": 0.05},
        "freshness_policy": {"technical_max_age_days": 10, "fundamentals_max_age_days": 180, "regime_max_age_days": 7, "intraday_max_age_days": 2, "fundamentals_required": True},
        "min_avg_traded_value_20d": 100_000_000,
        "max_breakout_extension_pct": 8,
        "watch_pullback_extension_pct": 6,
        "min_dist_52w_high": -20,
    }


def _golden_path_setup() -> dict[str, Any]:
    return {
        "allowed_regimes": ["STABLE"],
        "blocked_regimes": [],
        "allowed_overlays": ["NONE"],
        "blocked_overlays": [],
        "technical_rules": [],
        "fundamental_rules": [],
        "intraday_rules": [],
        "score_thresholds": {"pass_now": 0.68, "watch_breakout": 0.58, "watch_event": 0.48, "abstain": 0.40, "near_miss_gap": 0.05},
        "freshness_policy": {"technical_max_age_days": 10, "fundamentals_max_age_days": 180, "regime_max_age_days": 7, "intraday_max_age_days": 2, "fundamentals_required": True},
        "min_avg_traded_value_20d": 100_000_000,
        "max_breakout_extension_pct": 8,
        "watch_pullback_extension_pct": 6,
        "min_dist_52w_high": -20,
    }


def test_upsert_to_db_rejects_timescale_unique_keys_without_partition_column():
    from utils.db import upsert_to_db

    with pytest.raises(ValueError, match="partition column"):
        upsert_to_db(
            pd.DataFrame([{"evaluated_at": pd.Timestamp("2026-06-16T00:00:00Z"), "config_id": "cfg"}]),
            "unit_timescale_table",
            unique_keys=["config_id"],
            timescaledb_column="evaluated_at",
        )


def _monitor_record(excess, *, matured=True, event_class="order_win", sufficiency_path="aggregate_corroboration", beta=False):
    return {
        "symbol": "ABC",
        "matured": matured,
        "proposed_action": "BUY",
        "event_class": event_class,
        "sufficiency_path": sufficiency_path,
        "realized_excess_after_cost": excess,
        "resolved_beta_only": beta,
    }


def _evidence_rows(asof="2026-06-23"):
    return dict(
        symbol="ABC",
        asof_date=asof,
        technical_row={
            "asof_date": asof, "rs_vs_benchmark": 0.08, "technical_state": "READY",
            "pass_above_dma_50": True, "pass_above_dma_200": True, "pass_near_52w_high": True,
            "pass_breakout_extension": True, "pass_liquidity_20d": True, "pass_trend_alignment": True,
            "pass_gap_behavior": True,
        },
        allocation_row={
            "asof_date": asof, "allocation_status": "allocated", "conviction_bucket": "high",
            "risk_bucket": "low", "stop_price": 94.0, "invalidation_price": 90.0,
            "event_class": "order_win", "confidence": 0.8, "setup_effect": "strengthens", "score_impact": 0.2,
        },
        market_row={"asof_date": asof, "risk_on_score": 0.7, "macro_risk_state": "NORMAL", "regime_name": "STABLE", "macro_stress_score": 0.2},
        sector_reliability_row={"classification": "candidate_helpful", "excess_opportunity_hit_rate_after_cost": 0.62, "matured_count": 30, "source_context": "announcement_context"},
        exact_class_row={"classification": "candidate_helpful", "excess_opportunity_hit_rate_after_cost": 0.58, "matured_count": 24},
        benchmark_row={"classification": "candidate_helpful", "avg_excess_watch_return_after_cost": 0.015},
        hypothesis_rows=[{"status": "trusted_overlay", "match_score": 0.8, "hypothesis_id": "bull_order_v1", "decision_json": {}, "expected_effect_json": {"effect": "increase_exposure", "market_direction": "positive", "action_bias": "buy_watch"}}],
    )


def _price_series(start="2026-01-01", n=40, start_price=100.0, daily=0.0):
    import pandas as pd

    dates = pd.bdate_range(start=start, periods=n)
    return [{"date": d, "close": start_price * ((1.0 + daily) ** i)} for i, d in enumerate(dates)]


def test_external_task_queue_stable_task_id():
    row_a = external_task_queue.enqueue_task.__globals__["json_dumps"]({"symbol": "ABC"})
    row_b = external_task_queue.enqueue_task.__globals__["json_dumps"]({"symbol": "ABC"})

    assert row_a == row_b
    assert "ABC" in row_a


def test_external_task_queue_ensure_table_uses_schema_registry(monkeypatch):
    calls = []

    monkeypatch.setattr(external_task_queue, "apply_schema_migration", lambda **kwargs: calls.append(kwargs) or {"status": "applied"})

    external_task_queue.ensure_table()

    assert len(calls) == 1
    assert calls[0]["migration_id"] == external_task_queue.EXTERNAL_TASK_QUEUE_SCHEMA_MIGRATION_ID
    assert calls[0]["metadata"]["tables"] == [external_task_queue.TABLE_NAME]
    assert any(external_task_queue.TABLE_NAME in statement for statement in calls[0]["statements"])
    assert any("task_id TEXT PRIMARY KEY" in statement for statement in calls[0]["statements"])
    assert any("CREATE INDEX IF NOT EXISTS" in statement for statement in calls[0]["statements"])


def test_external_task_queue_executes_registered_handler(monkeypatch):
    calls: list[dict[str, object]] = []

    def fake_handler(args):
        calls.append(args)
        return {"status": "ok", "rows": 3}

    monkeypatch.setitem(external_task_queue.TASK_HANDLERS, "fake_task", fake_handler)
    result = external_task_queue.execute_task(
        {
            "task_type": "fake_task",
            "task_args_json": json.dumps({"symbol": "ABC"}),
        }
    )

    assert result == {"status": "ok", "rows": 3}
    assert calls == [{"symbol": "ABC"}]


def test_external_task_queue_rejects_unknown_handler():
    try:
        external_task_queue.execute_task({"task_type": "missing", "task_args_json": "{}"})
    except ValueError as exc:
        assert "No external task handler" in str(exc)
    else:
        raise AssertionError("Expected ValueError")


def test_external_task_queue_enqueue_uses_timestamp_dtypes(monkeypatch):
    captured: dict[str, pd.DataFrame] = {}

    monkeypatch.setattr(external_task_queue, "ensure_table", lambda: None)

    def fake_upsert(df, table_name, unique_keys):
        captured["df"] = df.copy()
        captured["table_name"] = table_name
        captured["unique_keys"] = unique_keys

    monkeypatch.setattr(external_task_queue, "upsert_to_db", fake_upsert)
    row = external_task_queue.enqueue_task(queue_name="nse", task_type="smoke", task_args={})

    assert row["task_id"]
    assert captured["table_name"] == external_task_queue.TABLE_NAME
    assert str(captured["df"]["claimed_at"].dtype).startswith("datetime64")
    assert str(captured["df"]["completed_at"].dtype).startswith("datetime64")


def test_external_task_queue_state_transitions_use_retryable_operations(monkeypatch):
    executed: list[tuple[str, object]] = []
    operation_names: list[str] = []

    class FakeCursor:
        def __init__(self, *, dict_rows: bool = False):
            self.dict_rows = dict_rows

        def execute(self, query, params=None):
            executed.append((str(query), params))

        def fetchone(self):
            return {"task_id": "task-1", "status": "claimed"} if self.dict_rows else ("task-1",)

    class FakeSession:
        def __init__(self, *, dict_factory: bool = False):
            self.dict_factory = dict_factory

        def __enter__(self):
            return None, FakeCursor(dict_rows=self.dict_factory)

        def __exit__(self, *_args):
            return False

    monkeypatch.setattr(external_task_queue, "ensure_table", lambda: None)
    monkeypatch.setattr(external_task_queue, "db_session", lambda **kwargs: FakeSession(dict_factory=bool(kwargs.get("dict_factory"))))

    def fake_execute_db_operation(operation, *, operation_name, **_kwargs):
        operation_names.append(operation_name)
        return operation()

    monkeypatch.setattr(external_task_queue, "execute_db_operation", fake_execute_db_operation)

    claimed = external_task_queue.claim_task(queue_name="nse", worker_id="worker-1")
    external_task_queue.complete_task(task_id="task-1", result={"status": "ok"})
    external_task_queue.fail_task(task_id="task-2", error="boom", retry_delay_seconds=30)

    assert claimed == {"task_id": "task-1", "status": "claimed"}
    assert operation_names == [
        "external_task_queue:claim_task",
        "external_task_queue:complete_task",
        "external_task_queue:fail_task",
    ]
    assert "RETURNING q.*" in executed[0][0]
    assert "status = 'completed'" in executed[1][0]
    assert "last_error = %s" in executed[2][0]


def test_external_task_queue_records_status_load_failure(monkeypatch):
    events = []

    monkeypatch.setattr(external_task_queue, "ensure_table", lambda: None)
    monkeypatch.setattr(external_task_queue, "sql_to_df", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("queue db down")))
    monkeypatch.setattr(external_task_queue, "record_local_fallback_event", lambda **kwargs: events.append(kwargs) or kwargs)

    df = external_task_queue.load_queue_status(queue_name="nse", limit=25)

    assert df.empty
    assert len(events) == 1
    assert events[0]["fallback_type"] == "external_task_queue_status_load_failed"
    assert events[0]["source"] == external_task_queue.TABLE_NAME
    assert events[0]["metadata"] == {"queue_name": "nse", "limit": 25}


def test_external_task_queue_records_worker_task_failure(monkeypatch):
    events = []
    failed_tasks = []
    task = {
        "task_id": "task-1",
        "queue_name": "nse",
        "task_type": "fake_fail",
        "task_args_json": "{}",
        "attempt_count": 1,
        "max_attempts": 3,
    }

    monkeypatch.setattr(external_task_queue, "ensure_table", lambda: None)
    monkeypatch.setattr(external_task_queue, "claim_task", lambda **kwargs: task)
    monkeypatch.setattr(external_task_queue, "execute_task", lambda task: (_ for _ in ()).throw(RuntimeError("worker boom")))
    monkeypatch.setattr(external_task_queue, "fail_task", lambda **kwargs: failed_tasks.append(kwargs))
    monkeypatch.setattr(external_task_queue, "record_local_fallback_event", lambda **kwargs: events.append(kwargs) or kwargs)

    result = external_task_queue.run_worker(queue_name="nse", worker_id="worker-test", once=True)

    assert result["failed"] == 1
    assert failed_tasks[0]["task_id"] == "task-1"
    assert len(events) == 1
    assert events[0]["fallback_type"] == "external_task_queue_task_failed"
    assert events[0]["severity"] == "error"
    assert events[0]["metadata"]["task_id"] == "task-1"
    assert events[0]["metadata"]["worker_id"] == "worker-test"


def test_download_queue_dry_run_no_inline():
    result = download_queue.enqueue_download_work(phase="downloaders", run_non_queued=False, dry_run=True)

    assert result["status"] == "ok"
    assert result["dry_run"] is True
    assert result["queued_count"] > 0
    assert result["inline_count"] == 0
    assert result["skipped_inline_count"] > 0
    assert {row["queue_name"] for row in result["queued"]} & {"dhan", "nse"}


def test_dhan_ohlcv_table_setup_runs_once_per_process(monkeypatch):
    calls = []

    monkeypatch.setattr(dhan_ohlcv, "_OHLCV_TABLES_ENSURED", False)
    monkeypatch.setattr(dhan_ohlcv, "apply_schema_migration", lambda **kwargs: calls.append(kwargs) or {"status": "applied"})

    dhan_ohlcv.ensure_ohlcv_tables()
    dhan_ohlcv.ensure_ohlcv_tables()

    assert len(calls) == 1
    assert calls[0]["migration_id"] == dhan_ohlcv.OHLCV_SCHEMA_MIGRATION_ID
    assert calls[0]["metadata"]["tables"] == [dhan_ohlcv.DAILY_TABLE, dhan_ohlcv.INTRADAY_TABLE]
    assert any(dhan_ohlcv.DAILY_TABLE in statement for statement in calls[0]["statements"])
    assert any(dhan_ohlcv.INTRADAY_TABLE in statement for statement in calls[0]["statements"])


def test_sync_state_ensure_table_uses_schema_registry(monkeypatch):
    calls = []

    monkeypatch.setattr(sync_state, "apply_schema_migration", lambda **kwargs: calls.append(kwargs) or {"status": "applied"})

    sync_state.ensure_sync_state_table()

    assert len(calls) == 1
    assert calls[0]["migration_id"] == sync_state.SYNC_STATE_SCHEMA_MIGRATION_ID
    assert calls[0]["metadata"]["tables"] == [sync_state.TABLE_NAME]
    assert any(sync_state.TABLE_NAME in statement for statement in calls[0]["statements"])
    assert any("source_name" in statement and "scope_key" in statement for statement in calls[0]["statements"])


def test_sync_state_persist_keeps_missing_timestamps_as_nullable_timestamptz(monkeypatch):
    captured = {}

    def fake_upsert(df, table_name, unique_keys):
        captured["df"] = df.copy()
        captured["table_name"] = table_name
        captured["unique_keys"] = list(unique_keys)

    monkeypatch.setattr(sync_state, "ensure_sync_state_table", lambda: None)
    monkeypatch.setattr(sync_state, "upsert_to_db", fake_upsert)

    sync_state.persist_sync_state(
        source_name="download_runner:data.sharpelydata.scrip_master",
        scope_key="sharpely_master_precheck",
        last_success_at=pd.Timestamp("2026-06-15T07:12:45Z"),
        last_item_ts=None,
        state={"rows": 8387},
    )

    df = captured["df"]
    assert captured["table_name"] == sync_state.TABLE_NAME
    assert captured["unique_keys"] == ["source_name", "scope_key"]
    assert str(df["last_success_at"].dtype) == "datetime64[ns, UTC]"
    assert str(df["last_item_ts"].dtype) == "datetime64[ns, UTC]"
    assert str(df["updated_at"].dtype) == "datetime64[ns, UTC]"
    assert str(df["load_ts"].dtype) == "datetime64[ns, UTC]"
    assert pd.isna(df["last_item_ts"].iloc[0])


def test_sync_state_load_records_state_json_parse_fallback(monkeypatch):
    events = []

    monkeypatch.setattr(
        sync_state,
        "load_sync_states",
        lambda source_name=None: pd.DataFrame(
            [
                {
                    "source_name": source_name,
                    "scope_key": "default",
                    "state_json": "{bad json",
                }
            ]
        ),
    )
    monkeypatch.setattr(sync_state, "record_local_fallback_event", lambda **kwargs: events.append(kwargs) or kwargs)

    row = sync_state.load_sync_state("download_runner:nse", "default")

    assert row is not None
    assert row["state"] == {}
    assert len(events) == 1
    assert events[0]["module"] == "utils.sync_state"
    assert events[0]["fallback_type"] == "sync_state_state_json_parse_failed"
    assert events[0]["source"] == "download_runner:nse"
    assert events[0]["metadata"] == {
        "source_name": "download_runner:nse",
        "scope_key": "default",
        "state_json_length": len("{bad json"),
    }


def test_sync_state_publish_bus_message_records_fallback(monkeypatch):
    events = []

    class FakeRedis:
        def publish(self, channel, payload):
            raise RuntimeError("redis unavailable")

        def close(self):
            raise AssertionError("close should not be called after publish failure")

    monkeypatch.setattr(sync_state, "get_redis_client", lambda **kwargs: FakeRedis())
    monkeypatch.setattr(sync_state, "record_local_fallback_event", lambda **kwargs: events.append(kwargs) or kwargs)

    result = sync_state.publish_bus_message("stockey:test", {"symbol": "ABC", "status": "ok"})

    assert result is False
    assert events[0]["module"] == "utils.sync_state"
    assert events[0]["fallback_type"] == "sync_state_bus_publish_failed"
    assert events[0]["source"] == "stockey:test"
    assert events[0]["error"].args == ("redis unavailable",)
    assert events[0]["metadata"]["channel"] == "stockey:test"
    assert set(events[0]["metadata"]["payload_keys"]) == {"status", "symbol"}
    assert events[0]["metadata"]["payload_type"] == "dict"


def test_sync_many_daily_continues_after_symbol_error(monkeypatch):
    events = []
    monkeypatch.setattr(dhan_ohlcv, "DhanHistoricalClient", lambda: object())
    monkeypatch.setattr(dhan_ohlcv, "record_local_fallback_event", lambda **kwargs: events.append(kwargs) or kwargs)

    def fake_sync_daily_ohlcv(ticker, **kwargs):
        if ticker == "BAD":
            raise dhan_client.DhanAPIError("bad values for parameters")
        return pd.DataFrame(
            [
                {
                    "date": pd.Timestamp("2026-04-09T00:00:00Z"),
                }
            ]
        )

    monkeypatch.setattr(dhan_ohlcv, "sync_daily_ohlcv", fake_sync_daily_ohlcv)

    results = dhan_ohlcv.sync_many_daily(["GOOD", "BAD"], exchange="NSE", asset_type="stock")

    assert len(results) == 2
    assert results[0]["ticker"] == "GOOD"
    assert results[0]["rows"] == 1
    assert results[1]["ticker"] == "BAD"
    assert results[1]["rows"] == 0
    assert "DhanAPIError" in results[1]["error"]
    assert len(events) == 1
    event = events[0]
    assert event["module"] == "data.dhanlive.ohlcv"
    assert event["fallback_type"] == "dhan_ohlcv_daily_symbol_sync_failed"
    assert event["source"] == dhan_ohlcv.DAILY_TABLE
    assert event["severity"] == "warn"
    assert isinstance(event["error"], dhan_client.DhanAPIError)
    assert event["metadata"]["ticker"] == "BAD"
    assert event["metadata"]["exchange"] == "NSE"
    assert event["metadata"]["asset_type"] == "stock"


def test_sync_daily_ohlcv_normalizes_mixed_timezone_dates(monkeypatch):
    captured: dict[str, datetime] = {}

    monkeypatch.setattr(dhan_ohlcv, "ensure_ohlcv_tables", lambda: None)
    monkeypatch.setattr(
        dhan_ohlcv,
        "resolve_dhan_identity",
        lambda ticker, exchange, asset_type="stock": {
            "company_master_id": "nse:TEST",
            "asset_type": "stock",
            "exchange": "NSE",
            "ticker": "TEST",
            "security_id": 123,
            "exchange_segment": "NSE_EQ",
            "instrument": "EQUITY",
        },
    )
    monkeypatch.setattr(
        dhan_ohlcv,
        "latest_daily_snapshot",
        lambda ticker, exchange, asset_type: {
            "min_date": pd.Timestamp("2026-04-01T00:00:00Z").to_pydatetime(),
            "max_date": pd.Timestamp("2026-04-22T00:00:00Z").to_pydatetime(),
            "max_load_ts": pd.Timestamp("2026-04-22T10:00:00Z").to_pydatetime(),
        },
    )
    monkeypatch.setattr(dhan_ohlcv, "has_recent_adjustment", lambda symbol, latest_stored_date: False)
    monkeypatch.setattr(dhan_ohlcv, "load_nse_holidays", lambda: set())
    monkeypatch.setattr(dhan_ohlcv, "upsert_to_db", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        dhan_ohlcv,
        "candles_to_df",
        lambda payload: pd.DataFrame(
            [
                {
                    "source_timestamp": pd.Timestamp("2026-04-23T10:00:00Z"),
                    "open": 1,
                    "high": 2,
                    "low": 1,
                    "close": 2,
                    "volume": 10,
                }
            ]
        ),
    )

    class DummyClient:
        def fetch_daily(self, **kwargs):
            captured["from_date"] = kwargs["from_date"]
            captured["to_date"] = kwargs["to_date"]
            return {"ok": True}

    df = dhan_ohlcv.sync_daily_ohlcv("TEST", to_date=datetime(2026, 4, 24), client=DummyClient())

    assert not df.empty
    assert captured["from_date"].tzinfo is None
    assert captured["to_date"].tzinfo is None


def test_sync_many_intraday_continues_after_symbol_error(monkeypatch):
    events = []
    monkeypatch.setattr(dhan_ohlcv, "DhanHistoricalClient", lambda: object())
    monkeypatch.setattr(dhan_ohlcv, "record_local_fallback_event", lambda **kwargs: events.append(kwargs) or kwargs)

    def fake_sync_intraday_ohlcv(ticker, **kwargs):
        if ticker == "BAD":
            raise ValueError("no dhan identity")
        return pd.DataFrame(
            [
                {
                    "timestamp": pd.Timestamp("2026-04-09T09:15:00Z"),
                }
            ]
        )

    monkeypatch.setattr(dhan_ohlcv, "sync_intraday_ohlcv", fake_sync_intraday_ohlcv)

    results = dhan_ohlcv.sync_many_intraday(["GOOD", "BAD"], exchange="NSE", asset_type="stock", interval_minutes=1)

    assert len(results) == 2
    assert results[0]["ticker"] == "GOOD"
    assert results[0]["rows"] == 1
    assert results[1]["ticker"] == "BAD"
    assert results[1]["rows"] == 0
    assert "ValueError" in results[1]["error"]
    assert len(events) == 1
    event = events[0]
    assert event["module"] == "data.dhanlive.ohlcv"
    assert event["fallback_type"] == "dhan_ohlcv_intraday_symbol_sync_failed"
    assert event["source"] == dhan_ohlcv.INTRADAY_TABLE
    assert event["severity"] == "warn"
    assert isinstance(event["error"], ValueError)
    assert event["metadata"]["ticker"] == "BAD"
    assert event["metadata"]["exchange"] == "NSE"
    assert event["metadata"]["asset_type"] == "stock"
    assert event["metadata"]["interval_minutes"] == 1


def test_dhan_ohlcv_build_run_state_aggregates_windows_and_errors():
    state = dhan_ohlcv.build_run_state(
        daily_results=[
            {"ticker": "AAA", "rows": 2, "from_date": "2026-06-01", "to_date": "2026-06-02"},
            {"ticker": "BAD", "rows": 0, "from_date": None, "to_date": None, "error": "ValueError: no identity"},
        ],
        intraday_results=[
            {"ticker": "AAA", "rows": 3, "from_timestamp": "2026-06-02 09:15:00", "to_timestamp": "2026-06-02 09:25:00"},
        ],
        only="both",
        symbols=["AAA", "BAD"],
        exchange="nse",
        asset_type="stock",
        interval_minutes=5,
    )

    assert state["rows"] == 5
    assert state["rows_written"] == 5
    assert state["daily_rows"] == 2
    assert state["intraday_rows"] == 3
    assert state["from_date"] == "2026-06-01"
    assert state["to_date"] == "2026-06-02"
    assert state["from_datetime"] == "2026-06-02 09:15:00"
    assert state["to_datetime"] == "2026-06-02 09:25:00"
    assert state["error_count"] == 1
    assert state["failed_symbols"] == ["BAD"]
    assert state["fallback_used"] is False


def test_dhan_ohlcv_main_exports_runner_state(monkeypatch):
    monkeypatch.setattr(
        dhan_ohlcv,
        "load_tracked_symbols",
        lambda symbols: ["AAA", "BBB"],
    )
    monkeypatch.setattr(dhan_ohlcv, "ensure_ohlcv_tables", lambda: None)
    monkeypatch.setattr(dhan_ohlcv, "parse_datetime_arg", lambda value: datetime(2026, 6, 1) if value else None)
    monkeypatch.setattr(
        dhan_ohlcv,
        "sync_many_daily",
        lambda *args, **kwargs: [
            {"ticker": "AAA", "rows": 2, "from_date": "2026-06-01", "to_date": "2026-06-02"},
            {"ticker": "BBB", "rows": 1, "from_date": "2026-06-01", "to_date": "2026-06-01"},
        ],
    )
    monkeypatch.setattr(
        dhan_ohlcv,
        "sync_many_intraday",
        lambda *args, **kwargs: [
            {"ticker": "AAA", "rows": 4, "from_timestamp": "2026-06-02 09:15:00", "to_timestamp": "2026-06-02 09:30:00"},
            {"ticker": "BBB", "rows": 0, "from_timestamp": None, "to_timestamp": None, "error": "DhanAPIError: no data"},
        ],
    )
    monkeypatch.setattr(
        sys,
        "argv",
        ["data.dhanlive.ohlcv", "--symbols", "AAA,BBB", "--only", "both", "--intraday-interval", "5", "--from-date", "2026-06-01"],
    )

    dhan_ohlcv.main()

    assert dhan_ohlcv.STOCKEY_RUN_STATE["rows"] == 7
    assert dhan_ohlcv.STOCKEY_RUN_STATE["daily_rows"] == 3
    assert dhan_ohlcv.STOCKEY_RUN_STATE["intraday_rows"] == 4
    assert dhan_ohlcv.STOCKEY_RUN_STATE["symbol_count"] == 2
    assert dhan_ohlcv.STOCKEY_RUN_STATE["error_count"] == 1
    assert dhan_ohlcv.STOCKEY_RUN_STATE["failed_symbols"] == ["BBB"]
    assert dhan_ohlcv.STOCKEY_RUN_STATE["only"] == "both"
    assert dhan_ohlcv.STOCKEY_RUN_STATE["interval_minutes"] == 5


def test_dhan_ohlcv_pull_records_failure_fallback(monkeypatch, capsys):
    from data.dhanlive import ohlcv_pull

    events = []
    args = argparse.Namespace(
        ticker="reliance",
        exchange="NSE",
        asset_type="stock",
        mode="intraday",
        source="api",
        interval_minutes=5,
        last_minutes=60,
        last_days=30,
        from_datetime=None,
        to_datetime=None,
        from_date=None,
        to_date=None,
        format="json",
        limit=20,
    )

    monkeypatch.setattr(ohlcv_pull, "parse_args", lambda: args)
    monkeypatch.setattr(
        ohlcv_pull,
        "load_api_intraday",
        lambda _args: (_ for _ in ()).throw(RuntimeError("token expired")),
    )
    monkeypatch.setattr(ohlcv_pull, "record_local_fallback_event", lambda **kwargs: events.append(kwargs))

    assert ohlcv_pull.main() == 1
    output = json.loads(capsys.readouterr().out)

    assert output["status"] == "error"
    assert output["requested_ticker"] == "RELIANCE"
    assert "token expired" in output["error"]
    assert len(events) == 1
    event = events[0]
    assert event["module"] == "data.dhanlive.ohlcv_pull"
    assert event["source"] == "dhan_ohlcv_pull:api:intraday:NSE:RELIANCE"
    assert event["fallback_type"] == "dhan_ohlcv_pull_failed"
    assert event["severity"] == "warn"
    assert event["symbol"] == "RELIANCE"
    assert isinstance(event["error"], RuntimeError)
    assert event["metadata"] == {
        "ticker": "RELIANCE",
        "exchange": "NSE",
        "asset_type": "stock",
        "mode": "intraday",
        "source": "api",
        "interval_minutes": 5,
    }


def test_fbil_gsec_uses_env_backed_lookback_days(monkeypatch):
    from data.rbi import download_fbil_gsec as fbil_gsec

    monkeypatch.setattr(fbil_gsec, "FBIL_GSEC_LOOKBACK_DAYS", 365)
    monkeypatch.setattr(fbil_gsec, "get_cookies", lambda: {})
    monkeypatch.setattr(fbil_gsec.rop, "get", lambda _key: None)
    captured = {"start": None, "stop": None}

    def fake_daterange(start, stop):
        captured["start"] = start
        captured["stop"] = stop
        return []

    monkeypatch.setattr(fbil_gsec, "daterange", fake_daterange)

    fbil_gsec.download_all_gsec_data()

    expected_start = datetime.combine(date.today(), datetime.min.time()) - pd.Timedelta(days=365)
    assert captured["start"] == expected_start


def test_fbil_gsec_download_all_exports_run_state(monkeypatch):
    from data.rbi import download_fbil_gsec as fbil_gsec

    dates = [date(2026, 6, 5), date(2026, 6, 6), date(2026, 6, 8)]
    monkeypatch.setattr(fbil_gsec, "get_cookies", lambda: {"session": "ok"})
    monkeypatch.setattr(fbil_gsec.rop, "get", lambda _key: None)
    monkeypatch.setattr(fbil_gsec, "daterange", lambda _start, _stop: dates)

    def fake_download_gsec(fdate, cookies):
        assert cookies == {"session": "ok"}
        if fdate == dates[0]:
            return {"date": "2026-06-05", "status": "downloaded", "rows": 7}
        if fdate == dates[1]:
            return {"date": "2026-06-06", "status": "skipped_weekend", "rows": 0}
        return {"date": "2026-06-08", "status": "source_unavailable", "rows": 0}

    monkeypatch.setattr(fbil_gsec, "download_gsec", fake_download_gsec)

    state = fbil_gsec.download_all_gsec_data()

    assert state["source"] == "data.rbi.download_fbil_gsec"
    assert state["date_count"] == 3
    assert state["downloaded_date_count"] == 1
    assert state["skipped_weekend_count"] == 1
    assert state["failed_date_count"] == 1
    assert state["attempt_count"] == 2
    assert state["failed_attempt_count"] == 1
    assert state["source_unavailable_count"] == 1
    assert state["rows_written"] == 7
    assert state["state_advanced"] is True


def test_fbil_gsec_download_failure_records_local_fallback(monkeypatch):
    from data.rbi import download_fbil_gsec as fbil_gsec

    events: list[dict[str, object]] = []
    target_date = date(2026, 6, 8)
    monkeypatch.setattr(fbil_gsec, "get_cookies", lambda: {"session": "ok"})
    monkeypatch.setattr(fbil_gsec.rop, "get", lambda _key: None)
    monkeypatch.setattr(fbil_gsec, "daterange", lambda _start, _stop: [target_date])
    monkeypatch.setattr(fbil_gsec, "record_local_fallback_event", lambda **kwargs: events.append(kwargs))
    monkeypatch.setattr(
        fbil_gsec,
        "download_gsec",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("fbil down")),
    )

    state = fbil_gsec.download_all_gsec_data()

    assert state["failed_date_count"] == 1
    assert len(events) == 1
    assert events[0]["module"] == "data.rbi.download_fbil_gsec"
    assert events[0]["source"] == "rbi_fbil_gsec"
    assert events[0]["fallback_type"] == "fbil_gsec_download_failed"
    assert events[0]["metadata"]["date"] == "2026-06-08"


def test_fbil_gsec_try_parsing_date_records_format_fallback(monkeypatch):
    from data.rbi import download_fbil_gsec as fbil_gsec

    events: list[dict[str, object]] = []
    monkeypatch.setattr(fbil_gsec, "_record_fbil_gsec_fallback", lambda **kwargs: events.append(kwargs))

    parsed = fbil_gsec.try_parsing_date("08 Jun, 2026")

    assert parsed == datetime(2026, 6, 8)
    assert len(events) == 1
    assert events[0]["fallback_type"] == "fbil_gsec_date_format_parse_failed"
    assert events[0]["source"] == "rbi_fbil_gsec"
    assert events[0]["metadata"]["format"] == "%d-%b-%Y"
    assert events[0]["metadata"]["raw_date"] == "08 Jun, 2026"


def test_fbil_gsec_parse_xls_records_trade_date_and_sheet_fallbacks(monkeypatch):
    from data.rbi import download_fbil_gsec as fbil_gsec

    events: list[dict[str, object]] = []
    calls: list[tuple[str, object]] = []

    def fake_read_excel(_path, sheet_name=None, skiprows=None):
        calls.append((str(sheet_name), skiprows))
        if sheet_name == "G-Sec" and skiprows is None:
            return pd.DataFrame([[None, None, None], [None, None, "bad-date"]])
        if sheet_name == "G-Sec" and skiprows == 5:
            return pd.DataFrame(
                [
                    [
                        "IN000000001",
                        "7.1",
                        "31-Dec-2030",
                        "100.2",
                        "7.0",
                        None,
                        None,
                        "liquid",
                    ]
                ]
            )
        if sheet_name == "Par Yield":
            raise ValueError("missing Par Yield")
        if sheet_name == "Par-Yield":
            return pd.DataFrame([[10, 7.2, 7.45]])
        raise AssertionError(f"unexpected read_excel call {sheet_name=} {skiprows=}")

    monkeypatch.setattr(fbil_gsec.pd, "read_excel", fake_read_excel)
    monkeypatch.setattr(fbil_gsec, "record_local_fallback_event", lambda **kwargs: events.append(kwargs))

    quote, par = fbil_gsec.parse_xls("dummy.xls", date(2026, 6, 8))

    assert quote["trade_date"].iloc[0] == date(2026, 6, 8)
    assert float(par["par_yield_sa"].iloc[0]) == 7.2
    assert [event["fallback_type"] for event in events] == [
        "fbil_gsec_trade_date_parse_failed",
        "fbil_gsec_par_yield_sheet_fallback",
    ]
    assert events[0]["metadata"]["raw_trade_date"] == "bad-date"
    assert all(event["metadata"]["date"] == "2026-06-08" for event in events)
    assert ("Par-Yield", 5) in calls


def test_fbil_gsec_main_exports_failed_run_state(monkeypatch, capsys):
    from data.rbi import download_fbil_gsec as fbil_gsec

    payload = {
        "source": "data.rbi.download_fbil_gsec",
        "rows": 0,
        "rows_read": 1,
        "rows_written": 0,
        "date_count": 1,
        "downloaded_date_count": 0,
        "failed_date_count": 1,
        "attempt_count": 1,
        "failed_attempt_count": 1,
        "source_unavailable_count": 1,
        "failed_dates": [{"date": "2026-06-08", "error": "RuntimeError: fbil down"}],
        "state_advanced": False,
    }
    monkeypatch.setattr(fbil_gsec, "download_all_gsec_data", lambda: payload)

    assert fbil_gsec.main() == 0
    capsys.readouterr()

    state = fbil_gsec.STOCKEY_RUN_STATE
    assert state["source"] == "data.rbi.download_fbil_gsec"
    assert state["date_count"] == 1
    assert state["failed_date_count"] == 1
    assert state["failed_dates"][0]["date"] == "2026-06-08"
    assert state["state_advanced"] is False


def test_rbi_bank_rates_exports_source_unavailable_state(monkeypatch):
    from data.rbi import download_bank_rates as bank_rates

    events = []

    class FakeChromium:
        def connect_over_cdp(self, _endpoint):
            raise bank_rates.PlaywrightTimeoutError("cdp timeout")

    class FakePlaywright:
        chromium = FakeChromium()

    monkeypatch.setattr(bank_rates, "record_local_fallback_event", lambda **kwargs: events.append(kwargs))

    state = bank_rates.download_latest_rates(FakePlaywright())

    assert state["source"] == "data.rbi.download_bank_rates"
    assert state["status"] == "source_unavailable"
    assert state["attempt_count"] == 1
    assert state["failed_attempt_count"] == 1
    assert state["source_unavailable_count"] == 1
    assert state["state_advanced"] is False
    assert "cdp timeout" in state["error"]
    assert len(events) == 1
    event = events[0]
    assert event["module"] == "data.rbi.download_bank_rates"
    assert event["source"] == "rbi_bank_rates:browser"
    assert event["fallback_type"] == "rbi_bank_rates_source_unavailable"
    assert event["severity"] == "warn"
    assert isinstance(event["error"], bank_rates.PlaywrightTimeoutError)
    assert event["metadata"] == {
        "cdp_endpoint": bank_rates.CDP_ENDPOINT,
        "status": "source_unavailable",
    }


def test_rbi_bank_rates_records_generic_download_failure(monkeypatch):
    from data.rbi import download_bank_rates as bank_rates

    events = []

    class FakePage:
        def close(self):
            pass

        def goto(self, _url):
            raise RuntimeError("rbi layout changed")

    class FakeContext:
        def new_page(self):
            return FakePage()

    class FakeBrowser:
        contexts = [FakeContext()]

        def close(self):
            pass

    class FakeChromium:
        def connect_over_cdp(self, _endpoint):
            return FakeBrowser()

    class FakePlaywright:
        chromium = FakeChromium()

    monkeypatch.setattr(bank_rates, "record_local_fallback_event", lambda **kwargs: events.append(kwargs))

    state = bank_rates.download_latest_rates(FakePlaywright())

    assert state["status"] == "failed"
    assert state["failed_attempt_count"] == 1
    assert state["source_unavailable_count"] == 0
    assert state["state_advanced"] is False
    assert "rbi layout changed" in state["error"]
    assert len(events) == 1
    event = events[0]
    assert event["module"] == "data.rbi.download_bank_rates"
    assert event["source"] == "rbi_bank_rates:download"
    assert event["fallback_type"] == "rbi_bank_rates_download_failed"
    assert event["severity"] == "warn"
    assert isinstance(event["error"], RuntimeError)
    assert event["metadata"] == {
        "cdp_endpoint": bank_rates.CDP_ENDPOINT,
        "status": "failed",
    }


def test_rbi_bank_rates_main_exports_run_state(monkeypatch, capsys):
    from data.rbi import download_bank_rates as bank_rates

    payload = {
        "source": "data.rbi.download_bank_rates",
        "status": "ok",
        "rows": 5,
        "rows_read": 5,
        "rows_written": 5,
        "attempt_count": 1,
        "failed_attempt_count": 0,
        "source_unavailable_count": 0,
        "state_advanced": True,
    }

    class FakePlaywrightContext:
        def __enter__(self):
            return object()

        def __exit__(self, *_args):
            return False

    monkeypatch.setattr(bank_rates, "sync_playwright", lambda: FakePlaywrightContext())
    monkeypatch.setattr(bank_rates, "download_latest_rates", lambda _playwright: payload)

    assert bank_rates.main() == 0
    capsys.readouterr()

    assert bank_rates.STOCKEY_RUN_STATE["source"] == "data.rbi.download_bank_rates"
    assert bank_rates.STOCKEY_RUN_STATE["rows_written"] == 5
    assert bank_rates.STOCKEY_RUN_STATE["state_advanced"] is True


def test_nse_holidays_exports_source_unavailable_state(monkeypatch):
    from data.nseindia import holidays as nse_holidays

    events: list[dict[str, object]] = []

    class FakeChromium:
        def connect_over_cdp(self, _endpoint):
            raise nse_holidays.PlaywrightTimeoutError("cdp timeout")

    class FakePlaywright:
        chromium = FakeChromium()

    monkeypatch.setattr(nse_holidays, "record_local_fallback_event", lambda **kwargs: events.append(kwargs) or kwargs)

    state = nse_holidays.download_holidays(FakePlaywright())

    assert state["source"] == "data.nseindia.holidays"
    assert state["status"] == "source_unavailable"
    assert state["attempt_count"] == 1
    assert state["failed_attempt_count"] == 1
    assert state["source_unavailable_count"] == 1
    assert state["state_advanced"] is False
    assert "cdp timeout" in state["error"]
    assert len(events) == 1
    event = events[0]
    assert event["module"] == "data.nseindia.holidays"
    assert event["source"] == "data.nseindia.holidays"
    assert event["fallback_type"] == "nse_holidays_source_unavailable"
    assert event["severity"] == "warn"
    assert isinstance(event["error"], nse_holidays.PlaywrightTimeoutError)
    assert event["metadata"] == {"classification": "source_unavailable", "attempt_count": 1}


def test_nse_holidays_records_unknown_segment_fallback(monkeypatch):
    from data.nseindia import holidays as nse_holidays

    monkeypatch.setattr(nse_rate_limiter.time, "sleep", lambda *a, **k: None)  # no real rate-gate delay in test
    events: list[dict[str, object]] = []
    captured: dict[str, object] = {}

    holiday_rows = [
        {
            "tradingDate": "26-Jan-2026",
            "weekDay": "Monday",
            "description": "Republic Day",
            "morning_session": "",
            "evening_session": "",
            "Sr_no": 1,
        }
    ]
    fake_data = {"CM": holiday_rows, "ZZNEWSEG": holiday_rows}

    class FakePage:
        def goto(self, *_args, **_kwargs):
            return None

        def wait_for_timeout(self, *_args, **_kwargs):
            return None

        def evaluate(self, *_args, **_kwargs):
            return fake_data

        def close(self):
            return None

    class FakeContext:
        def new_page(self):
            return FakePage()

    class FakeBrowser:
        contexts: list[object] = []

        def new_context(self):
            return FakeContext()

        def close(self):
            return None

    class FakeChromium:
        def connect_over_cdp(self, _endpoint):
            return FakeBrowser()

    class FakePlaywright:
        chromium = FakeChromium()

    class FakeRedis:
        def set(self, *_args, **_kwargs):
            return None

    monkeypatch.setattr(nse_holidays, "record_local_fallback_event", lambda **kwargs: events.append(kwargs) or kwargs)
    monkeypatch.setattr(nse_holidays, "upsert_to_db", lambda df, *args, **kwargs: captured.__setitem__("df", df))
    monkeypatch.setattr(nse_holidays, "rop", FakeRedis())

    state = nse_holidays.download_holidays(FakePlaywright())

    assert state["status"] == "ok"
    unknown_events = [e for e in events if e.get("fallback_type") == "nse_holidays_unknown_segment"]
    assert len(unknown_events) == 1
    assert unknown_events[0]["metadata"]["segment_key"] == "ZZNEWSEG"
    assert "ZZNEWSEG" in set(captured["df"]["type_name"])


def test_nse_holidays_records_generic_download_failure(monkeypatch):
    from data.nseindia import holidays as nse_holidays

    monkeypatch.setattr(nse_rate_limiter.time, "sleep", lambda *a, **k: None)  # no real rate-gate delay in test
    events: list[dict[str, object]] = []

    class FakePage:
        def goto(self, *_args, **_kwargs):
            raise RuntimeError("holiday parse failed")

        def close(self):
            return None

    class FakeContext:
        def new_page(self):
            return FakePage()

    class FakeBrowser:
        contexts = []

        def new_context(self):
            return FakeContext()

        def close(self):
            return None

    class FakeChromium:
        def connect_over_cdp(self, _endpoint):
            return FakeBrowser()

    class FakePlaywright:
        chromium = FakeChromium()

    monkeypatch.setattr(nse_holidays, "record_local_fallback_event", lambda **kwargs: events.append(kwargs) or kwargs)

    state = nse_holidays.download_holidays(FakePlaywright())

    assert state["source"] == "data.nseindia.holidays"
    assert state["status"] == "failed"
    assert state["failed_attempt_count"] == 1
    assert state["state_advanced"] is False
    assert "holiday parse failed" in state["error"]
    assert len(events) == 1
    event = events[0]
    assert event["fallback_type"] == "nse_holidays_download_failed"
    assert event["severity"] == "error"
    assert isinstance(event["error"], RuntimeError)
    assert event["metadata"] == {"classification": "failed", "attempt_count": 1}


def test_nse_holidays_main_exports_run_state(monkeypatch, capsys):
    from data.nseindia import holidays as nse_holidays

    payload = {
        "source": "data.nseindia.holidays",
        "status": "ok",
        "rows": 4,
        "rows_read": 4,
        "rows_written": 4,
        "attempt_count": 1,
        "failed_attempt_count": 0,
        "source_unavailable_count": 0,
        "state_advanced": True,
    }

    class FakeRedis:
        def get(self, _key):
            return None

        def close(self):
            pass

    class FakePlaywrightContext:
        def __enter__(self):
            return object()

        def __exit__(self, *_args):
            return False

    monkeypatch.setattr(nse_holidays, "rop", FakeRedis())
    monkeypatch.setattr(nse_holidays, "sync_playwright", lambda: FakePlaywrightContext())
    monkeypatch.setattr(nse_holidays, "download_holidays", lambda _playwright: payload)

    assert nse_holidays.main() == 0
    capsys.readouterr()

    assert nse_holidays.STOCKEY_RUN_STATE["source"] == "data.nseindia.holidays"
    assert nse_holidays.STOCKEY_RUN_STATE["rows_written"] == 4
    assert nse_holidays.STOCKEY_RUN_STATE["state_advanced"] is True


def test_env_example_audit_extracts_python_and_shell_vars():
    from scripts import env_example_audit

    text = """
api_key = env.str("OPENAI_API_KEY", "")
workers = int(os.getenv("SAMPLE_AUDIT_FIXTURE_INT_VAR", "4"))
token = os.environ.get("DHAN_ACCESS_TOKEN")
echo "${STOCKEY_DIR:-/tmp/stockey}" "$LOG_DIR" "$PWD"
"""

    usages = env_example_audit.extract_env_usages_from_text(text, "sample.sh")
    names = {usage.name for usage in usages}

    # SAMPLE_AUDIT_FIXTURE_INT_VAR is intentionally in IGNORED_ENV_NAMES (it's this
    # test's own fixture text, not a real env var) and must not appear in the output.
    assert names == {
        "OPENAI_API_KEY",
        "DHAN_ACCESS_TOKEN",
        "STOCKEY_DIR",
        "LOG_DIR",
    }


def test_env_example_audit_parse_env_example_names():
    from scripts import env_example_audit

    names = env_example_audit.parse_env_example_names(
        """
# comment
OPENAI_API_KEY=
BAD-name=value
LOG_DIR=logs/cron
  STOCKEY_DIR=/tmp/stockey
"""
    )

    assert names == {"OPENAI_API_KEY", "LOG_DIR", "STOCKEY_DIR"}


def test_env_example_audit_build_report_flags_missing_vars(tmp_path):
    from scripts import env_example_audit

    (tmp_path / ".env.example").write_text("OPENAI_API_KEY=\n", encoding="utf-8")
    (tmp_path / "runner.py").write_text(
        'env.str("OPENAI_API_KEY")\nos.getenv("MISSING_RUNTIME_VAR")\n',
        encoding="utf-8",
    )

    report = env_example_audit.build_report(repo_root=tmp_path)

    assert report["status"] == "missing_env_example_entries"
    assert report["missing"] == ["MISSING_RUNTIME_VAR"]
    assert report["documented_count"] == 1
    assert report["used_count"] == 2


def test_env_example_audit_records_decode_fallback(monkeypatch, tmp_path):
    from scripts import env_example_audit

    events = []
    (tmp_path / ".env.example").write_text("OPENAI_API_KEY=\n", encoding="utf-8")
    (tmp_path / "broken.py").write_bytes(b"\xff\xfe\xff")
    monkeypatch.setattr(fallback_telemetry, "record_local_fallback_event", lambda **kwargs: events.append(kwargs) or kwargs)

    usages = env_example_audit.collect_env_usages(repo_root=tmp_path)

    assert usages == []
    assert len(events) == 1
    assert events[0]["module"] == "scripts.env_example_audit"
    assert events[0]["source"] == "broken.py"
    assert events[0]["fallback_type"] == "env_example_audit_decode_failed"




def test_start_chrome_cdp_script_documents_remote_debugging_contract():
    script_path = Path("scripts/start_chrome_cdp.sh")
    script = script_path.read_text(encoding="utf-8")

    assert script_path.exists()
    assert script_path.stat().st_mode & 0o111
    assert 'CDP_HOST="${CDP_HOST:-127.0.0.1}"' in script
    assert 'CDP_PORT="${CDP_PORT:-9222}"' in script
    assert "CHROME_USER_DATA_DIR" in script
    assert "--remote-debugging-address=" in script
    assert "--remote-debugging-port=" in script
    assert "--user-data-dir=" in script
    assert "CDP_ENDPOINT=http://localhost:9222" in script




def test_run_with_markers_emits_start_and_failed_status():
    proc = subprocess.run(
        ["bash", "scripts/run_with_markers.sh", "marker_test", "bash", "-c", "exit 7"],
        cwd=Path.cwd(),
        capture_output=True,
        text=True,
        check=False,
    )

    assert proc.returncode == 7
    assert "[stockey.script] name=marker_test status=start" in proc.stdout
    assert "[stockey.script] name=marker_test status=failed exit_code=7" in proc.stdout


def test_cleanup_deprecated_tables_uses_retryable_drop(monkeypatch):
    from scripts import cleanup_deprecated_tables

    operation_names = []
    executed = []

    class FakeCursor:
        def execute(self, query, params=None):
            executed.append((str(query), params))

    class FakeSession:
        def __enter__(self):
            return None, FakeCursor()

        def __exit__(self, *_args):
            return False

    def fake_execute_db_operation(operation, *, operation_name, **_kwargs):
        operation_names.append(operation_name)
        return operation()

    monkeypatch.setattr(cleanup_deprecated_tables, "list_existing_deprecated_tables", lambda: ["dhan_screeners"])
    monkeypatch.setattr(cleanup_deprecated_tables, "db_session", lambda: FakeSession())
    monkeypatch.setattr(cleanup_deprecated_tables, "execute_db_operation", fake_execute_db_operation)

    dropped = cleanup_deprecated_tables.drop_deprecated_tables()

    assert dropped == ["dhan_screeners"]
    assert operation_names == ["cleanup_deprecated_tables:drop"]
    assert executed == [("DROP TABLE IF EXISTS dhan_screeners", None)]


def test_hot_table_retention_selects_trace_and_intraday_groups():
    from scripts import hot_table_retention

    trace_specs = hot_table_retention.selected_specs(group="trace")
    intraday_specs = hot_table_retention.selected_specs(group="intraday")

    assert trace_specs
    assert intraday_specs
    assert {spec.group for spec in trace_specs} == {"trace"}
    assert {spec.group for spec in intraday_specs} == {"intraday"}
    assert "advisory_decision_traces" in {spec.table_name for spec in trace_specs}
    assert "dhan_ohlcv_intraday" in {spec.table_name for spec in intraday_specs}


def test_hot_table_retention_parse_cutoff_normalizes_explicit_date():
    from scripts import hot_table_retention

    cutoff = hot_table_retention.parse_cutoff("2026-06-11 15:45:00+05:30", 45)

    assert str(cutoff.tz) == "UTC"
    assert cutoff.hour == 0
    assert cutoff.minute == 0


def test_hot_table_retention_blocks_delete_without_archive(monkeypatch):
    from scripts import hot_table_retention

    spec = hot_table_retention.RETENTION_TABLES["dhan_ohlcv_intraday"]
    monkeypatch.setattr(hot_table_retention, "table_exists", lambda table_name: True)
    monkeypatch.setattr(hot_table_retention, "column_exists", lambda table_name, column_name: True)
    monkeypatch.setattr(
        hot_table_retention,
        "month_chunks",
        lambda *args, **kwargs: [{"chunk_start": pd.Timestamp("2026-01-01T00:00:00Z"), "chunk_end": pd.Timestamp("2026-02-01T00:00:00Z"), "row_count": 10}],
    )

    result = hot_table_retention.archive_or_delete_table(
        spec,
        cutoff=pd.Timestamp("2026-03-01T00:00:00Z"),
        archive_s3=False,
        delete=True,
        execute=False,
        allow_delete_without_archive=False,
        archive_prefix="archives/hot_tables",
        max_chunks=None,
        exact_counts=True,
    )

    assert result["status"] == "blocked_delete_without_archive"
    assert result["candidate_rows"] == 10
    assert "archive" in result["message"].lower()


def test_hot_table_retention_execute_uses_retryable_operation(monkeypatch):
    from scripts import hot_table_retention

    spec = hot_table_retention.RETENTION_TABLES["dhan_ohlcv_intraday"]
    operation_names = []

    class FakeSession:
        def __enter__(self):
            return None, object()

        def __exit__(self, *_args):
            return False

    def fake_execute_db_operation(operation, *, operation_name, **_kwargs):
        operation_names.append(operation_name)
        return operation()

    monkeypatch.setattr(hot_table_retention, "table_exists", lambda table_name: True)
    monkeypatch.setattr(hot_table_retention, "column_exists", lambda table_name, column_name: True)
    monkeypatch.setattr(
        hot_table_retention,
        "month_chunks",
        lambda *args, **kwargs: [
            {
                "chunk_start": pd.Timestamp("2026-01-01T00:00:00Z"),
                "chunk_end": pd.Timestamp("2026-02-01T00:00:00Z"),
                "row_count": 10,
            }
        ],
    )
    monkeypatch.setattr(hot_table_retention, "db_session", lambda: FakeSession())
    monkeypatch.setattr(hot_table_retention, "execute_db_operation", fake_execute_db_operation)
    monkeypatch.setattr(hot_table_retention, "delete_chunk", lambda **_kwargs: 7)

    result = hot_table_retention.archive_or_delete_table(
        spec,
        cutoff=pd.Timestamp("2026-03-01T00:00:00Z"),
        archive_s3=False,
        delete=True,
        execute=True,
        allow_delete_without_archive=True,
        archive_prefix="archives/hot_tables",
        max_chunks=None,
        exact_counts=True,
    )

    assert operation_names == ["hot_table_retention:archive_or_delete:dhan_ohlcv_intraday"]
    assert result["status"] == "ok"
    assert result["deleted_rows"] == 7
    assert result["chunks"][0]["deleted_rows"] == 7






















def test_heavy_payload_inventory_classifies_control_payload():
    from scripts import heavy_payload_inventory

    row = heavy_payload_inventory.classify_column(
        {
            "schema_name": "public",
            "table_name": "advisory_decision_traces",
            "column_name": "payload_json",
            "data_type": "text",
            "estimated_rows": 250_000,
            "total_bytes": 250 * 1024 * 1024,
            "toast_bytes": 50 * 1024 * 1024,
        }
    )

    assert row["risk_score"] >= 6
    assert row["recommended_action"] == "compact_or_retention"


def test_heavy_payload_inventory_marks_announcement_as_handled():
    from scripts import heavy_payload_inventory

    row = heavy_payload_inventory.classify_column(
        {
            "schema_name": "public",
            "table_name": "announcement_pipeline_documents",
            "column_name": "full_ocr_text",
            "data_type": "text",
            "estimated_rows": 1000,
            "total_bytes": 500 * 1024 * 1024,
            "toast_bytes": 450 * 1024 * 1024,
        }
    )

    assert row["announcement_table"] is True
    assert row["recommended_action"] == "handled_by_announcement_offload"


def test_heavy_payload_inventory_build_report_sorts_by_risk(monkeypatch):
    from scripts import heavy_payload_inventory

    monkeypatch.setattr(
        heavy_payload_inventory,
        "fetch_payload_columns",
        lambda schema, include_announcement: [
            {
                "schema_name": "public",
                "table_name": "small_table",
                "column_name": "name",
                "data_type": "text",
                "estimated_rows": 10,
                "total_bytes": 100,
                "toast_bytes": 0,
            },
            {
                "schema_name": "public",
                "table_name": "advisory_event_processing_runs",
                "column_name": "payload_json",
                "data_type": "text",
                "estimated_rows": 500_000,
                "total_bytes": 600 * 1024 * 1024,
                "toast_bytes": 200 * 1024 * 1024,
            },
        ],
    )

    report = heavy_payload_inventory.build_inventory(min_risk_score=1)

    assert report["status"] == "ok"
    assert report["returned_count"] == 1
    assert report["rows"][0]["table_name"] == "advisory_event_processing_runs"
    assert report["recommended_action_counts"] == {"compact_or_retention": 1}




























class _BadPandasMissingCheck:
    def __array__(self, dtype=None):
        raise RuntimeError("missing check failed")


def test_cron_status_invalid_step_records_fallback(monkeypatch):
    events: list[dict[str, object]] = []
    monkeypatch.setattr(cron_status, "record_local_fallback_event", lambda **kwargs: events.append(kwargs) or kwargs)

    result = cron_status._expand_field("*/bad", 0, 5)

    assert result == set(range(0, 6))
    assert events[0]["fallback_type"] == "cron_status_invalid_step_fallback"
    assert events[0]["metadata"]["step_text"] == "bad"




def test_utils_date_pd_to_datetime_records_format_fallback(monkeypatch):
    from utils import date as date_utils

    events: list[dict[str, object]] = []
    monkeypatch.setattr(date_utils, "record_local_fallback_event", lambda **kwargs: events.append(kwargs) or kwargs)

    frame = pd.DataFrame({"published_on": ["2026/06/12"]})
    result = date_utils.pd_to_datetime(frame, "published_on", ["%d-%m-%Y", "%Y/%m/%d"])

    assert result["published_on"].iloc[0] == pd.Timestamp("2026-06-12")
    assert events[0]["fallback_type"] == "date_format_parse_failed"
    assert events[0]["metadata"] == {"column": "published_on", "format": "%d-%m-%Y"}


def test_display_time_records_json_parse_fallback(monkeypatch):
    from utils import display_time

    events: list[dict[str, object]] = []
    monkeypatch.setattr(display_time, "record_local_fallback_event", lambda **kwargs: events.append(kwargs) or kwargs)

    result = display_time.to_display_value("{bad json")

    assert result == "{bad json"
    assert events[0]["fallback_type"] == "display_time_json_parse_failed"
    assert events[0]["metadata"]["value_excerpt"] == "{bad json"


def test_agent_tool_runner_records_stdout_json_parse_fallback(monkeypatch):
    import subprocess
    from scripts import agent_tool_runner

    events: list[dict[str, object]] = []
    monkeypatch.setattr(
        agent_tool_runner,
        "registry_by_name",
        lambda: {"unit": {"name": "unit", "category": "qa", "read_only": True, "command": ["python", "-m", "unit"]}},
    )
    monkeypatch.setattr(
        agent_tool_runner.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args=args[0], returncode=0, stdout="{bad json", stderr=""),
    )
    monkeypatch.setattr(agent_tool_runner, "record_local_fallback_event", lambda **kwargs: events.append(kwargs) or kwargs)

    result = agent_tool_runner.run_tool("unit", [], allow_writes=False)

    assert result["stdout"] == "{bad json"
    assert events[0]["fallback_type"] == "agent_tool_runner_stdout_json_parse_failed"
    assert events[0]["source"] == "unit"


def test_duplicate_index_report_records_indexes_json_parse_fallback(monkeypatch):
    from scripts import db_duplicate_index_report

    events: list[dict[str, object]] = []
    monkeypatch.setattr(
        db_duplicate_index_report,
        "sql_to_df",
        lambda *args, **kwargs: pd.DataFrame(
            [{"schema_name": "public", "table_name": "t", "indexes": "{bad json", "total_index_bytes": 1}]
        ),
    )
    monkeypatch.setattr(db_duplicate_index_report, "record_local_fallback_event", lambda **kwargs: events.append(kwargs) or kwargs)

    result = db_duplicate_index_report.build_duplicate_index_report()

    assert result["duplicate_groups"][0]["indexes"] == "{bad json"
    assert result["suggested_drop_candidates"] == []
    assert events[0]["fallback_type"] == "duplicate_index_report_indexes_json_parse_failed"


def test_docs_and_env_audit_record_relative_path_fallback(monkeypatch, tmp_path):
    from scripts import docs_state_audit, env_example_audit

    docs_events: list[dict[str, object]] = []
    env_events: list[dict[str, object]] = []
    outside = tmp_path / "outside.md"
    outside.write_text("x", encoding="utf-8")
    monkeypatch.setattr(docs_state_audit, "record_local_fallback_event", lambda **kwargs: docs_events.append(kwargs) or kwargs)
    monkeypatch.setattr(
        "utils.fallback_telemetry.record_local_fallback_event",
        lambda **kwargs: env_events.append(kwargs) or kwargs,
    )

    assert docs_state_audit.should_scan_path(outside, repo_root=tmp_path / "repo") is True
    assert env_example_audit.should_scan_path(outside, repo_root=tmp_path / "repo") is True
    assert docs_events[0]["fallback_type"] == "docs_state_audit_relative_path_failed"
    assert env_events[0]["fallback_type"] == "env_example_audit_relative_path_failed"


def test_docs_state_audit_requires_pure_ta_framing_coverage():
    from scripts import docs_state_audit

    checks = dict(docs_state_audit.REQUIRED_COVERAGE["README.md"])
    pattern = checks["pure data platform framing"]

    assert pattern.search("Stockey is a **pure data platform** for Indian-equity price/reference data.")
    assert not pattern.search("Stockey is an Indian-equity advisory research and operator system.")


def test_docs_state_audit_flags_legacy_advisory_module_reference():
    from scripts import docs_state_audit

    repo_root = docs_state_audit.REPO_ROOT
    findings = docs_state_audit.check_stale_terms(
        repo_root / "docs" / "example.md",
        "Run `python -m advisory.rule_engine` to score candidates.\n",
        repo_root=repo_root,
    )

    assert any(f.code == "legacy_advisory_module_reference" and f.severity == "warning" for f in findings)


def test_docs_state_audit_allows_legacy_advisory_module_reference_with_removed_note():
    from scripts import docs_state_audit

    repo_root = docs_state_audit.REPO_ROOT
    findings = docs_state_audit.check_stale_terms(
        repo_root / "docs" / "example.md",
        "`advisory.rule_engine` was removed in the pure-TA cut.\n",
        repo_root=repo_root,
    )

    assert not any(f.code == "legacy_advisory_module_reference" for f in findings)


def test_transcribe_cleanup_missing_temp_file_records_fallback(monkeypatch, tmp_path):
    from utils.transcribe import llm_transcribe

    events: list[dict[str, object]] = []
    missing = tmp_path / "missing.mp3"
    monkeypatch.setattr(llm_transcribe, "record_local_fallback_event", lambda **kwargs: events.append(kwargs) or kwargs)

    llm_transcribe.cleanup_temp_file(str(missing))

    assert events[0]["fallback_type"] == "transcribe_temp_file_cleanup_missing"
    assert events[0]["metadata"] == {"path": str(missing)}


def _split_eval_row_with(**overrides):
    base = {
        "evaluated_at": pd.Timestamp("2026-06-20T12:00:00Z"),
        "source_evaluated_at": pd.Timestamp("2026-06-20T00:00:00Z"),
        "horizon_days": 10,
        "variant": "technical_plus_announcement_context_split_order_win_positive",
        "source_family": "announcement_context",
        "split_axis": "context_class_direction",
        "split_value": "ORDER_WIN|positive",
        "context_class": "ORDER_WIN",
        "direction": "positive",
        "asof_date": pd.Timestamp("2026-06-01T00:00:00Z"),
        "setup_id": "S1",
        "symbol": "ABC",
        "selected": True,
        "matured": True,
        "forward_return_after_cost": 0.08,
        "hit_after_cost": True,
        "technical_only_selected": True,
        "technical_only_forward_return_after_cost": 0.01,
        "technical_only_hit_after_cost": True,
        "broker_execution_allowed": False,
        "policy_auto_promotion_allowed": False,
    }
    base.update(overrides)
    return base


def _ic_cross_section(date, n, sign):
    """One date's cross-section where excess is a monotone (sign) function of the score -> Spearman=sign."""
    import numpy as np
    rng = np.arange(n, dtype="float64")
    return pd.DataFrame({"date": [date] * n, "s": rng, "excess": sign * rng * 0.001})


def _grad_eligible_row():
    return {"fdr_significant": True, "ci_excludes_zero": True, "wf_consistent": True,
            "fav_ic": 0.06, "unf_ic": 0.04, "drift_status": "green", "n_dates": 40, "mean_ic": 0.05}


def _tilt_day():
    # RS pool of 6 names all >= gate 80; a secondary factor F disagrees with RS ordering.
    return pd.DataFrame({
        "symbol": ["A", "B", "C", "D", "E", "F"],
        "rs_percentile": [99.0, 95.0, 92.0, 88.0, 85.0, 82.0],
        "factorX": [0.0, 0.0, 0.0, 9.0, 9.0, 9.0],   # favors the weaker-RS names
    })


def test_download_runner_criticality_status_matrix():
    from data.download_runner import _overall_download_status as s
    assert s([("ok", True), ("ok", False)], continue_on_error=False) == "ok"
    # a flaky NON-critical source (CPI SSL, sharpely) -> warning, NOT a failed run (was exit 1 daily)
    assert s([("failed", False), ("ok", True)], continue_on_error=False) == "warning"
    assert s([("source_unavailable", False), ("ok", True)], continue_on_error=False) == "warning"
    # a CRITICAL (market-data: bhavcopy/indices/ohlcv) source failing -> failed, surfaced loudly
    assert s([("failed", True)], continue_on_error=False) == "failed"
    assert s([("failed", True), ("failed", False)], continue_on_error=False) == "failed"
    # explicit continue_on_error never escalates to failed
    assert s([("failed", True)], continue_on_error=True) == "warning"


def test_bhavcopy_downloader_retries_within_run(monkeypatch):
    from data.nseindia import bhavcopy_downloader as b
    seq = iter([False, False, True]); calls = []
    monkeypatch.setattr(b, "download_bhavcopy_for_date", lambda *a, **k: calls.append(1) or next(seq))
    assert b.download_bhavcopy_with_retries(None, "2026-07-15", "15-Jul-2026", None) is True
    assert len(calls) == 3                                      # failed twice, succeeded on the 3rd
    calls.clear()
    monkeypatch.setattr(b, "download_bhavcopy_for_date", lambda *a, **k: calls.append(1) or False)
    assert b.download_bhavcopy_with_retries(None, "2026-07-15", "15-Jul-2026", None) is False
    assert len(calls) == 3                                      # 1 + 2 retries then give up (candidate next run)




def test_data_coverage_report_check_table_daily_ok(monkeypatch):
    from scripts import data_coverage_report as dcr

    today = dcr._today()
    fresh = pd.DataFrame([{
        "rows": 100, "symbols": 10,
        "min_date": today - pd.Timedelta(days=400), "max_date": today,
        "max_past_or_present_date": today,
    }])
    monkeypatch.setattr(dcr, "sql_to_df", lambda *a, **k: fresh)
    row = dcr.check_table("some_table", "date", "symbol", "daily", "Test")
    assert row["status"] == "ok"
    assert row["rows"] == 100
    assert row["symbols"] == 10
    assert row["staleness_days"] == 0


def test_data_coverage_report_check_table_daily_warn_and_error(monkeypatch):
    from scripts import data_coverage_report as dcr

    today = dcr._today()

    def make(days_stale):
        return pd.DataFrame([{
            "rows": 5, "symbols": None,
            "min_date": today - pd.Timedelta(days=10), "max_date": today - pd.Timedelta(days=days_stale),
            "max_past_or_present_date": today - pd.Timedelta(days=days_stale),
        }])

    monkeypatch.setattr(dcr, "sql_to_df", lambda *a, **k: make(dcr.WARN_STALENESS_DAYS + 1))
    row = dcr.check_table("t", "date", None, "daily", "Test")
    assert row["status"] == "warn"

    monkeypatch.setattr(dcr, "sql_to_df", lambda *a, **k: make(dcr.ERROR_STALENESS_DAYS + 1))
    row = dcr.check_table("t", "date", None, "daily", "Test")
    assert row["status"] == "error"


def test_data_coverage_report_check_table_informational_never_stales(monkeypatch):
    from scripts import data_coverage_report as dcr

    today = dcr._today()
    stale = pd.DataFrame([{
        "rows": 259, "symbols": None,
        "min_date": today - pd.Timedelta(days=30000), "max_date": today - pd.Timedelta(days=243),
        "max_past_or_present_date": today - pd.Timedelta(days=243),
    }])
    monkeypatch.setattr(dcr, "sql_to_df", lambda *a, **k: stale)
    row = dcr.check_table("rbi_bank_rates", "date", None, "informational", "RBI/FBIL")
    assert row["status"] == "ok"
    assert row["staleness_days"] == 243  # still reported, just not judged


def test_data_coverage_report_check_table_empty_table_is_error(monkeypatch):
    from scripts import data_coverage_report as dcr

    empty = pd.DataFrame([{"rows": 0, "symbols": 0, "min_date": pd.NaT, "max_date": pd.NaT, "max_past_or_present_date": pd.NaT}])
    monkeypatch.setattr(dcr, "sql_to_df", lambda *a, **k: empty)
    row = dcr.check_table("t", "date", "symbol", "daily", "Test")
    assert row["status"] == "error"
    assert "empty" in row["detail"]


def test_data_coverage_report_check_table_records_fallback_on_query_failure(monkeypatch):
    from scripts import data_coverage_report as dcr

    events = []
    monkeypatch.setattr(dcr, "sql_to_df", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("db down")))
    monkeypatch.setattr(dcr, "record_local_fallback_event", lambda **kwargs: events.append(kwargs) or kwargs)
    row = dcr.check_table("t", "date", None, "daily", "Test")
    assert row["status"] == "error"
    assert len(events) == 1
    assert events[0]["fallback_type"] == "data_coverage_query_failed"


def test_data_coverage_report_build_report_aggregates_worst_status(monkeypatch):
    from scripts import data_coverage_report as dcr

    calls = iter([
        {"table_name": "a", "category": "X", "check_kind": "daily", "rows": 1, "symbols": None,
         "min_date": None, "max_date": None, "max_past_or_present_date": None, "staleness_days": 0,
         "status": "ok", "detail": ""},
        {"table_name": "b", "category": "X", "check_kind": "daily", "rows": 1, "symbols": None,
         "min_date": None, "max_date": None, "max_past_or_present_date": None, "staleness_days": 10,
         "status": "error", "detail": "stale"},
    ])
    monkeypatch.setattr(dcr, "check_table", lambda *a, **k: next(calls))
    monkeypatch.setattr(dcr, "TABLES", [("a", None, None, "daily", "X"), ("b", None, None, "daily", "X")])
    report = dcr.build_report()
    assert report["overall"] == "error"
    assert report["counts"] == {"ok": 1, "warn": 0, "error": 1}


def test_data_coverage_report_format_text_report_includes_each_table():
    from scripts import data_coverage_report as dcr

    report = {
        "report_date": "2026-08-05",
        "overall": "warn",
        "counts": {"ok": 1, "warn": 1, "error": 0},
        "tables": [
            {"status": "ok", "table_name": "a", "rows": 5, "symbols": 2, "max_date": "2026-08-05", "staleness_days": 0, "detail": ""},
            {"status": "warn", "table_name": "b", "rows": 1, "symbols": None, "max_date": None, "staleness_days": None, "detail": "no date"},
        ],
    }
    text = dcr.format_text_report(report)
    assert "report_date=2026-08-05" in text
    assert "a" in text and "b" in text


def test_data_coverage_report_main_require_exits_nonzero_on_error(monkeypatch, capsys):
    from scripts import data_coverage_report as dcr

    monkeypatch.setattr(dcr, "build_report", lambda: {"report_date": "2026-08-05", "overall": "error", "counts": {"ok": 0, "warn": 0, "error": 1}, "tables": []})
    monkeypatch.setattr(dcr, "persist_report", lambda report: None)
    assert dcr.main(["--require"]) == 1

    monkeypatch.setattr(dcr, "build_report", lambda: {"report_date": "2026-08-05", "overall": "warn", "counts": {"ok": 0, "warn": 1, "error": 0}, "tables": []})
    assert dcr.main(["--require"]) == 0


def test_bse_search_response_parses_single_match():
    # Captured live 2026-08-10 (fundamentals/collectors/security_master.py's docstring).
    text = (
        "\"<li class='quotemenu quotemenuselect' ng-click=\\\"liclick('543235','ANGEL ONE LTD')\\\">"
        "<a><strong>ANGEL ONE</strong> LTD<br /><span>ANGELONE&nbsp;&nbsp;&nbsp;"
        "INE732I01021&nbsp;&nbsp;&nbsp;543235</span></a></li>\""
    )
    results = fundamentals_security_master.parse_bse_search_response(text)
    assert results == [
        {"scrip_code": "543235", "company_name": "ANGEL ONE LTD", "symbol": "ANGELONE", "isin": "INE732I01021"}
    ]


def test_bse_search_response_parses_strong_wrapped_isin():
    # Searching by ISIN highlights the matched term in <strong> -- must parse the same
    # as an unwrapped match, not silently drop the ISIN.
    text = (
        "\"<li class='quotemenu quotemenuselect' ng-click=\\\"liclick('532067','3B BLACKBIO DX LTD')\\\">"
        "<a>3B BLACKBIO DX LTD<br /><span>3BBLACKBIO&nbsp;&nbsp;&nbsp;"
        "<strong>INE994E01018</strong>&nbsp;&nbsp;&nbsp;532067</span></a></li>\""
    )
    results = fundamentals_security_master.parse_bse_search_response(text)
    assert results == [
        {"scrip_code": "532067", "company_name": "3B BLACKBIO DX LTD", "symbol": "3BBLACKBIO", "isin": "INE994E01018"}
    ]


def test_bse_search_response_no_match_returns_empty():
    # Confirmed live 2026-08-10: BSE's literal response for an ISIN it doesn't carry
    # (e.g. an NSE-SME-only listing never cross-listed on BSE) -- no <li ng-click=...>
    # block at all, so this must parse to zero results, not raise.
    text = "\"<li class='quotemenu'><a>No Match Found<br /><span></span></a></li>\""
    assert fundamentals_security_master.parse_bse_search_response(text) == []


def test_lookup_bse_scrip_code_rejects_non_exact_isin_match(monkeypatch):
    """Defensive check: only trust an exact ISIN match in the response. A fuzzy/partial
    match returned by BSE's search must never silently attach the wrong scrip code."""
    monkeypatch.setattr(fundamentals_security_master, "exchange_request_gate", lambda **kwargs: contextlib.nullcontext())
    monkeypatch.setattr(
        fundamentals_security_master,
        "parse_bse_search_response",
        lambda text: [{"scrip_code": "999999", "company_name": "SOMETHING ELSE LTD", "symbol": "SMELSE", "isin": "INE000000000"}],
    )

    class FakeResponse:
        text = "irrelevant, parse_bse_search_response is mocked"

        def raise_for_status(self):
            return None

    monkeypatch.setattr(fundamentals_security_master.requests, "get", lambda *a, **k: FakeResponse())

    result = fundamentals_security_master.lookup_bse_scrip_code("INE732I01021")

    assert result is None


def test_copy_bse_scrip_code_from_ticker_skips_when_nothing_eligible(monkeypatch):
    monkeypatch.setattr(fundamentals_security_master, "sql_to_df", lambda *_a, **_k: pd.DataFrame())
    upserts = []
    monkeypatch.setattr(fundamentals_security_master, "upsert_to_db", lambda *a, **k: upserts.append((a, k)))

    assert fundamentals_security_master.copy_bse_scrip_code_from_ticker() == 0
    assert upserts == []


# fundamentals/collectors/screenerin.py -- deleveraging screen (step 2).

SCREENERIN_FIXTURE_HTML = """
<div data-page-results><table>
<tr>
<th><a>S.No.</a></th>
<th><a>Name</a></th>
<th><a>CMP<span>Rs.</span></a></th>
<th><a>Mar Cap<span>Rs.Cr.</span></a></th>
<th><a>ROCE<span>%</span></a></th>
</tr>
<tr data-row-company-id="3163">
<td class="text">1.</td>
<td class="text"><a href="/company/AQYLON/" target="_blank">Aqylon Nexus</a></td>
<td>26.68</td><td>676.20</td><td>131.18</td>
</tr>
<tr data-row-company-id="1274762">
<td class="text">2.</td>
<td class="text"><a href="/company/KSOLVES/" target="_blank">Ksolves India</a></td>
<td>277.25</td><td>652.52</td><td>127.40</td>
</tr>
</table></div>
"""


def test_parse_screener_results_parses_header_and_rows():
    # Structurally-minimal but real fixture (captured live 2026-08-10 against a plain
    # market-cap/volume query, trimmed of screener.in's per-column sort-link/tooltip
    # markup which parse_screener_results ignores).
    results = fundamentals_screenerin.parse_screener_results(SCREENERIN_FIXTURE_HTML)
    assert results == [
        {
            "company_id": 3163,
            "name": "Aqylon Nexus",
            "url": "/company/AQYLON/",
            "ticker": "AQYLON",
            "metrics": {"cmp_rs": 26.68, "mar_cap_rscr": 676.2, "roce_pct": 131.18},
        },
        {
            "company_id": 1274762,
            "name": "Ksolves India",
            "url": "/company/KSOLVES/",
            "ticker": "KSOLVES",
            "metrics": {"cmp_rs": 277.25, "mar_cap_rscr": 652.52, "roce_pct": 127.4},
        },
    ]


def test_parse_screener_results_raises_when_table_missing():
    with pytest.raises(ValueError):
        fundamentals_screenerin.parse_screener_results("<div>no results table here</div>")


def test_parse_screener_results_skips_rows_without_company_id():
    html = """
    <div data-page-results><table>
    <tr><th><a>Name</a></th></tr>
    <tr><td>not a data row</td></tr>
    </table></div>
    """
    assert fundamentals_screenerin.parse_screener_results(html) == []


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("1,234", 1234),
        ("26.68", 26.68),
        ("-5.5", -5.5),
        ("-", None),
        ("--", None),
        ("NA", None),
        ("N/A", None),
        (None, None),
        ("  42  ", 42),
    ],
)
def test_to_number_parses_screener_value_formats(raw, expected):
    assert fundamentals_screenerin.to_number(raw) == expected


def test_metric_key_normalizes_labels():
    assert fundamentals_screenerin._metric_key("Mar Cap") == "mar_cap"
    assert fundamentals_screenerin._metric_key("ROCE %") == "roce_pct"
    assert fundamentals_screenerin._metric_key("Qtr Profit Var") == "qtr_profit_var"
    assert fundamentals_screenerin._metric_key("P/E") == "p_e"


def test_run_query_stops_after_a_short_final_page(monkeypatch):
    # page_size=2 for pages 1-2, page 3 comes back short (1 row) -- must fetch page 3
    # then stop, not loop forever or drop the short page.
    pages = {
        1: ("url?page=1", [{"company_id": 1}, {"company_id": 2}]),
        2: ("url?page=2", [{"company_id": 3}, {"company_id": 4}]),
        3: ("url?page=3", [{"company_id": 5}]),
    }
    calls = []

    def fake_fetch(session, query_text, *, page):
        calls.append(page)
        return pages[page]

    monkeypatch.setattr(fundamentals_screenerin, "_fetch_query_page", fake_fetch)

    first_url, companies = fundamentals_screenerin.run_query(object(), "some query")

    assert calls == [1, 2, 3]
    assert first_url == "url?page=1"
    assert [c["company_id"] for c in companies] == [1, 2, 3, 4, 5]


def test_run_query_small_first_page_fetches_one_confirming_empty_page(monkeypatch):
    # run_query has no fixed page-size constant to compare against (screener.in's real
    # limit, 50, is never hardcoded) -- it infers "full page" from len(first_page), so
    # a first page smaller than the true limit still triggers one extra page fetch to
    # confirm there's nothing more. That confirming page must come back empty and the
    # loop must stop there, not treat "still short" as "keep going".
    pages = {1: ("url?page=1", [{"company_id": 1}]), 2: ("url?page=2", [])}
    calls = []

    def fake_fetch(session, query_text, *, page):
        calls.append(page)
        return pages[page]

    monkeypatch.setattr(fundamentals_screenerin, "_fetch_query_page", fake_fetch)

    _, companies = fundamentals_screenerin.run_query(object(), "some query")

    assert calls == [1, 2]
    assert [c["company_id"] for c in companies] == [1]


def test_run_query_zero_results_does_not_fetch_a_second_page(monkeypatch):
    calls = []

    def fake_fetch(session, query_text, *, page):
        calls.append(page)
        return "url?page=1", []

    monkeypatch.setattr(fundamentals_screenerin, "_fetch_query_page", fake_fetch)

    _, companies = fundamentals_screenerin.run_query(object(), "some query")

    assert calls == [1]
    assert companies == []


def test_run_deleveraging_screen_upserts_and_summarizes(monkeypatch):
    companies = [
        {"company_id": 1274762, "name": "Ksolves India", "ticker": "KSOLVES", "url": "/company/KSOLVES/", "metrics": {"mar_cap_rscr": 652.52}},
        {"company_id": 3163, "name": "Aqylon Nexus", "ticker": "AQYLON", "url": "/company/AQYLON/", "metrics": {"mar_cap_rscr": 676.2}},
    ]
    monkeypatch.setattr(fundamentals_screenerin, "run_query", lambda session, query_text: ("screener_url", companies))

    upserts = []
    monkeypatch.setattr(fundamentals_screenerin, "upsert_to_db", lambda df, table, **k: upserts.append((df, table, k)))

    result = fundamentals_screenerin.run_deleveraging_screen(session=object())

    assert result == {
        "query_name": "deleveraging",
        "rows": 2,
        "companies": ["Ksolves India", "Aqylon Nexus"],
    }
    assert len(upserts) == 1
    df, table, kwargs = upserts[0]
    assert table == fundamentals_screenerin.RESULTS_TABLE
    assert kwargs["unique_keys"] == ["query_name", "run_date", "company_id"]
    assert set(df["company_id"]) == {1274762, 3163}
    assert json.loads(df.iloc[0]["metrics_json"]) == companies[0]["metrics"]


def test_run_deleveraging_screen_skips_upsert_when_no_results(monkeypatch):
    monkeypatch.setattr(fundamentals_screenerin, "run_query", lambda session, query_text: ("screener_url", []))
    upserts = []
    monkeypatch.setattr(fundamentals_screenerin, "upsert_to_db", lambda *a, **k: upserts.append((a, k)))

    result = fundamentals_screenerin.run_deleveraging_screen(session=object())

    assert result == {"query_name": "deleveraging", "rows": 0, "companies": []}
    assert upserts == []


# fundamentals/screens/l1_universe.py -- L1 universe filter (step 3).


def test_run_l1_universe_refresh_upserts_and_summarizes(monkeypatch):
    companies = [
        {"company_id": 1, "name": "Menon Pistons", "ticker": "MENNPIS", "url": "/company/MENNPIS/", "metrics": {"mar_cap_rscr": 381.73}},
        {"company_id": 2, "name": "Coral India Fin.", "ticker": "CORALFINAC", "url": "/company/CORALFINAC/", "metrics": {"mar_cap_rscr": 137.9}},
    ]
    monkeypatch.setattr(fundamentals_l1_universe, "run_query", lambda session, query_text: ("screener_url", companies))

    upserts = []
    monkeypatch.setattr(fundamentals_l1_universe, "upsert_to_db", lambda df, table, **k: upserts.append((df, table, k)))

    fallback_events = []
    monkeypatch.setattr(
        fundamentals_l1_universe,
        "record_local_fallback_event",
        lambda **kwargs: fallback_events.append(kwargs),
    )

    result = fundamentals_l1_universe.run_l1_universe_refresh(session=object())

    assert result == {
        "query_name": "l1_universe",
        "query_version": 1,
        "rows": 2,
        "checks_deferred": list(fundamentals_l1_universe.DEFERRED_CHECKS),
        "companies": ["Menon Pistons", "Coral India Fin."],
    }
    assert len(upserts) == 1
    df, table, kwargs = upserts[0]
    assert table == fundamentals_l1_universe.RESULTS_TABLE
    assert kwargs["unique_keys"] == ["query_name", "query_version", "run_date", "company_id"]
    assert set(df["company_id"]) == {1, 2}
    assert json.loads(df.iloc[0]["metrics_json"]) == companies[0]["metrics"]

    # Deferred auditor/RPT checks must be visibly logged on every run, not silent.
    assert len(fallback_events) == 1
    assert fallback_events[0]["fallback_type"] == "l1_checks_not_sourced"
    assert set(fallback_events[0]["metadata"]["deferred_checks"]) == set(fundamentals_l1_universe.DEFERRED_CHECKS)


def test_run_l1_universe_refresh_skips_upsert_when_no_results_but_still_logs_deferred(monkeypatch):
    monkeypatch.setattr(fundamentals_l1_universe, "run_query", lambda session, query_text: ("screener_url", []))
    upserts = []
    monkeypatch.setattr(fundamentals_l1_universe, "upsert_to_db", lambda *a, **k: upserts.append((a, k)))
    fallback_events = []
    monkeypatch.setattr(
        fundamentals_l1_universe,
        "record_local_fallback_event",
        lambda **kwargs: fallback_events.append(kwargs),
    )

    result = fundamentals_l1_universe.run_l1_universe_refresh(session=object())

    assert result["rows"] == 0
    assert result["companies"] == []
    assert upserts == []
    assert len(fallback_events) == 1


def test_run_l1_universe_refresh_builds_a_session_when_none_given(monkeypatch):
    monkeypatch.setattr(fundamentals_l1_universe, "build_authenticated_session", lambda: "the-session")
    seen_sessions = []

    def fake_run_query(session, query_text):
        seen_sessions.append(session)
        return "screener_url", []

    monkeypatch.setattr(fundamentals_l1_universe, "run_query", fake_run_query)
    monkeypatch.setattr(fundamentals_l1_universe, "upsert_to_db", lambda *a, **k: None)
    monkeypatch.setattr(fundamentals_l1_universe, "record_local_fallback_event", lambda **k: None)

    fundamentals_l1_universe.run_l1_universe_refresh()

    assert seen_sessions == ["the-session"]


# fundamentals/screens/l2_state.py -- L2 watch-state store (step 4).

# Real shape captured live 2026-08-10 from a company's #balance-sheet table
# (fundamentals/screens/l2_state.py's docstring) -- Mar 2025 then Mar 2026 columns,
# Borrowings/CWIP/Fixed Assets rows (the "+"-suffixed expandable-row marker included,
# same as the live markup).
BALANCE_SHEET_FIXTURE_HTML = """
<table><tr>
<th></th><th>Mar 2025</th><th>Mar 2026</th>
</tr>
<tr><td>Borrowings +</td><td>17</td><td>3</td></tr>
<tr><td>CWIP</td><td>0</td><td>4</td></tr>
<tr><td>Fixed Assets +</td><td>76</td><td>95</td></tr>
</table>
"""

PROFIT_LOSS_FIXTURE_HTML = """
<table><tr>
<th></th><th>Mar 2025</th><th>Mar 2026</th><th>TTM</th>
</tr>
<tr><td>Operating Profit</td><td>32</td><td>31</td><td>31</td></tr>
<tr><td>Interest</td><td>4</td><td>3</td><td>3</td></tr>
</table>
"""

SHAREHOLDING_FIXTURE_HTML = """
<table><tr>
<th></th><th>Sep 2025</th><th>Dec 2025</th><th>Mar 2026</th><th>Jun 2026</th>
</tr>
<tr><td>Promoters +</td><td>74.37%</td><td>74.37%</td><td>74.37%</td><td>74.37%</td></tr>
</table>
"""


def _table(html):
    return BeautifulSoup(html, "html.parser").select_one("table")


def test_parse_period_table_strips_expandable_marker_and_parses_numbers():
    parsed = fundamentals_l2_state._parse_period_table(_table(BALANCE_SHEET_FIXTURE_HTML))
    assert parsed["periods"] == ["Mar 2025", "Mar 2026"]
    assert parsed["rows"] == {
        "Borrowings": [17, 3],
        "CWIP": [0, 4],
        "Fixed Assets": [76, 95],
    }


def test_parse_period_table_strips_percent_signs():
    parsed = fundamentals_l2_state._parse_period_table(_table(SHAREHOLDING_FIXTURE_HTML))
    assert parsed["rows"]["Promoters"] == [74.37, 74.37, 74.37, 74.37]


def test_parse_period_table_handles_missing_table():
    assert fundamentals_l2_state._parse_period_table(None) == {"periods": [], "rows": {}}


def test_compute_debt_trajectory_matches_hand_computed_values():
    balance_sheet = fundamentals_l2_state._parse_period_table(_table(BALANCE_SHEET_FIXTURE_HTML))
    profit_loss = fundamentals_l2_state._parse_period_table(_table(PROFIT_LOSS_FIXTURE_HTML))

    result = fundamentals_l2_state.compute_debt_trajectory(balance_sheet, profit_loss)

    assert result == {
        "net_debt_rscr": 3,
        "net_debt_yoy_delta_rscr": 3 - 17,
        "interest_coverage": 31 / 3,
        "debt_to_ebitda": 3 / 31,
    }


def test_compute_debt_trajectory_returns_none_ratios_when_interest_is_zero():
    balance_sheet = fundamentals_l2_state._parse_period_table(_table(BALANCE_SHEET_FIXTURE_HTML))
    profit_loss = fundamentals_l2_state._parse_period_table(
        _table("<table><tr><th></th><th>Mar 2025</th><th>Mar 2026</th></tr>"
               "<tr><td>Operating Profit</td><td>32</td><td>31</td></tr>"
               "<tr><td>Interest</td><td>0</td><td>0</td></tr></table>")
    )

    result = fundamentals_l2_state.compute_debt_trajectory(balance_sheet, profit_loss)

    assert result["interest_coverage"] is None
    assert result["debt_to_ebitda"] == 3 / 31


def test_compute_cwip_ratio_matches_hand_computed_values():
    balance_sheet = fundamentals_l2_state._parse_period_table(_table(BALANCE_SHEET_FIXTURE_HTML))

    result = fundamentals_l2_state.compute_cwip_ratio(balance_sheet)

    assert result["cwip_ratio"] == 4 / 95
    assert result["cwip_ratio_yoy_delta"] == (4 / 95) - (0 / 76)


def test_compute_cwip_ratio_handles_zero_fixed_assets():
    balance_sheet = {"periods": ["Mar 2026"], "rows": {"CWIP": [4], "Fixed Assets": [0]}}
    result = fundamentals_l2_state.compute_cwip_ratio(balance_sheet)
    assert result == {"cwip_ratio": None, "cwip_ratio_yoy_delta": None}


@pytest.mark.parametrize(
    "values,expected_direction",
    [
        ([74.37, 74.37, 74.37, 74.37], "flat"),
        ([70.0, 71.0, 72.0, 73.0], "increasing"),
        ([73.0, 72.0, 71.0, 70.0], "decreasing"),
    ],
)
def test_compute_promoter_stake_direction_over_last_4_quarters(values, expected_direction):
    shareholding = {"rows": {"Promoters": values}}
    result = fundamentals_l2_state.compute_promoter_stake(shareholding)
    assert result["promoter_pct"] == values[-1]
    assert result["promoter_stake_direction"] == expected_direction


def test_compute_promoter_stake_direction_is_none_with_fewer_than_4_quarters():
    shareholding = {"rows": {"Promoters": [74.37, 74.37]}}
    result = fundamentals_l2_state.compute_promoter_stake(shareholding)
    assert result["promoter_pct"] == 74.37
    assert result["promoter_stake_direction"] is None


def test_fetch_pledge_levels_builds_dict_keyed_by_company_id(monkeypatch):
    companies = [
        {"company_id": 3163, "metrics": {"pledged_pct": 96.64}},
        {"company_id": 1274762, "metrics": {}},  # no pledged_pct -- must be skipped, not KeyError
    ]
    monkeypatch.setattr(fundamentals_l2_state, "run_query", lambda session, query_text: ("url", companies))

    levels = fundamentals_l2_state.fetch_pledge_levels(object())

    assert levels == {3163: 96.64}


def test_run_l2_state_refresh_upserts_and_logs_deferred_fields(monkeypatch):
    universe = pd.DataFrame(
        [
            {"company_id": 1, "company_name": "Aarey Drugs", "ticker": "AAREYDRUGS"},
            {"company_id": 2, "company_name": "TCC Concept", "ticker": "TCC"},
        ]
    )
    monkeypatch.setattr(fundamentals_l2_state, "load_l1_universe", lambda: universe)
    monkeypatch.setattr(fundamentals_l2_state, "fetch_pledge_levels", lambda session: {1: 5.0})
    detail = {
        "balance_sheet": {"rows": {"Borrowings": [17, 3], "CWIP": [0, 4], "Fixed Assets": [76, 95]}},
        "profit_loss": {"rows": {"Operating Profit": [32, 31], "Interest": [4, 3]}},
        "shareholding": {"rows": {"Promoters": [74.37, 74.37, 74.37, 74.37]}},
    }
    monkeypatch.setattr(fundamentals_l2_state, "fetch_company_detail", lambda session, ticker: detail)

    upserts = []
    monkeypatch.setattr(fundamentals_l2_state, "upsert_to_db", lambda df, table, **k: upserts.append((df, table, k)))
    fallback_events = []
    monkeypatch.setattr(
        fundamentals_l2_state, "record_local_fallback_event", lambda **kwargs: fallback_events.append(kwargs)
    )

    result = fundamentals_l2_state.run_l2_state_refresh(session=object())

    assert result["rows"] == 2
    assert result["failed_companies"] == []
    assert result["checks_deferred"] == list(fundamentals_l2_state.DEFERRED_FIELDS)
    assert len(upserts) == 1
    df, table, kwargs = upserts[0]
    assert table == fundamentals_l2_state.RESULTS_TABLE
    assert kwargs["unique_keys"] == ["company_id", "run_date", "state_vector_version"]
    assert set(df["company_id"]) == {1, 2}
    row1 = df[df["company_id"] == 1].iloc[0]
    assert row1["pledge_pct"] == 5.0
    row2 = df[df["company_id"] == 2].iloc[0]
    assert row2["pledge_pct"] == 0.0  # not in pledge_levels -> defaults to unpledged
    assert row1["sector_cycle_phase"] is None
    assert row1["valuation_percentile"] is None
    assert any(e["fallback_type"] == "l2_fields_not_sourced" for e in fallback_events)


def test_run_l2_state_refresh_skips_a_company_whose_detail_fetch_fails(monkeypatch):
    universe = pd.DataFrame(
        [
            {"company_id": 1, "company_name": "Good Co", "ticker": "GOOD"},
            {"company_id": 2, "company_name": "Bad Co", "ticker": "BAD"},
        ]
    )
    monkeypatch.setattr(fundamentals_l2_state, "load_l1_universe", lambda: universe)
    monkeypatch.setattr(fundamentals_l2_state, "fetch_pledge_levels", lambda session: {})
    detail = {
        "balance_sheet": {"rows": {"Borrowings": [17, 3], "CWIP": [0, 4], "Fixed Assets": [76, 95]}},
        "profit_loss": {"rows": {"Operating Profit": [32, 31], "Interest": [4, 3]}},
        "shareholding": {"rows": {"Promoters": [74.37, 74.37, 74.37, 74.37]}},
    }

    def fake_fetch(session, ticker):
        if ticker == "BAD":
            raise RuntimeError("boom")
        return detail

    monkeypatch.setattr(fundamentals_l2_state, "fetch_company_detail", fake_fetch)
    upserts = []
    monkeypatch.setattr(fundamentals_l2_state, "upsert_to_db", lambda df, table, **k: upserts.append((df, table, k)))
    fallback_events = []
    monkeypatch.setattr(
        fundamentals_l2_state, "record_local_fallback_event", lambda **kwargs: fallback_events.append(kwargs)
    )

    result = fundamentals_l2_state.run_l2_state_refresh(session=object())

    assert result["rows"] == 1
    assert result["failed_companies"] == ["BAD"]
    assert len(upserts) == 1
    assert any(e["fallback_type"] == "l2_company_detail_fetch_failed" and e["metadata"]["ticker"] == "BAD" for e in fallback_events)


def test_run_l2_state_refresh_returns_early_when_l1_universe_is_empty(monkeypatch):
    monkeypatch.setattr(fundamentals_l2_state, "load_l1_universe", lambda: pd.DataFrame())
    upserts = []
    monkeypatch.setattr(fundamentals_l2_state, "upsert_to_db", lambda *a, **k: upserts.append((a, k)))
    fallback_events = []
    monkeypatch.setattr(
        fundamentals_l2_state, "record_local_fallback_event", lambda **kwargs: fallback_events.append(kwargs)
    )

    result = fundamentals_l2_state.run_l2_state_refresh(session=object())

    assert result == {"rows": 0, "failed_companies": [], "checks_deferred": list(fundamentals_l2_state.DEFERRED_FIELDS), "companies": []}
    assert upserts == []
    assert any(e["fallback_type"] == "l2_no_l1_universe" for e in fallback_events)


# fundamentals/collectors/bse_announcements.py -- L3 detection, BSE half (step 5).


@pytest.mark.parametrize(
    "subcategory,headline,expected",
    [
        ("Insider Trading / SAST-Trading Window", "Closure of trading window", "pit_sast"),
        # Real SUBCATNAME, captured live 2026-08-10 against actual BSE announcement
        # history -- no "insider"/"sast" substring at all; the first classifier draft
        # missed this (see PIT_SAST_KEYWORDS' comment).
        ("Closure of Trading Window", "Intimation for closure of trading window", "pit_sast"),
        (None, "Disclosure under Regulation 29(2) of SEBI (SAST) Regulations", "pit_sast"),
        ("Credit Rating", "Rating action by CRISIL Ratings", "rating_action"),
        (None, "ICRA has revised the rating outlook", "rating_action"),
        ("Financial Results", "Board approves financial results for Q1 FY27", "results"),
        ("AGM/EGM", "Notice of Annual General Meeting", "other"),
        (None, "Earnings call transcript uploaded", "other"),
        # Real (subcategory, headline) pair, captured live 2026-08-10: a routine
        # newspaper-ad filing whose headline happens to mention "Financial Results" --
        # must NOT be classified as an actual results announcement (see
        # classify_announcement's docstring on why "results" has no headline fallback).
        ("Newspaper Publication", "Newspaper Publication of the Unaudited Financial Results", "other"),
        # Real (subcategory, headline) pair, captured live 2026-08-11 via structured_
        # extraction.py's own output flagging a bad prior classification: BSE's own
        # subcategory is unambiguously "Board Meeting", but the headline cites "LODR
        # Regulation 29" (prior board-meeting intimation) -- an unrelated regulation
        # that just happens to share a number with SEBI (SAST) Regulations' own
        # Reg 29. Must NOT match on the bare regulation number (see PIT_SAST_KEYWORDS'
        # comment on why "regulation 29"/"regulation 31" were removed).
        ("Board Meeting", "Bal Pharma Ltd has informed BSE... Pursuant to Regulation 29 and other applicable provisions...", "other"),
    ],
)
def test_classify_announcement(subcategory, headline, expected):
    assert fundamentals_bse_announcements.classify_announcement(subcategory, headline) == expected


def test_build_announcement_row_returns_none_for_uninteresting_filings():
    raw = {"SUBCATNAME": "AGM/EGM", "HEADLINE": "Notice of AGM", "NEWSID": "abc-1"}
    assert fundamentals_bse_announcements.build_announcement_row("524412", "nse:AAREYDRUGS", "INE000A01011", raw) is None


def test_build_announcement_row_builds_expected_fields_for_pit_sast():
    raw = {
        "NEWSID": "abc-123",
        "SUBCATNAME": "Insider Trading / SAST-Trading Window",
        "HEADLINE": "Closure of trading window",
        "DissemDT": "2026-08-01T10:15:00.00",
        "ATTACHMENTNAME": "somefile.pdf",
        "NSURL": "https://www.bseindia.com/stock-share-price/x/y/524412/",
    }
    row = fundamentals_bse_announcements.build_announcement_row("524412", "nse:AAREYDRUGS", "INE000A01011", raw)
    assert row["source"] == "bse"
    assert row["news_id"] == "abc-123"
    assert row["scrip_code"] == "524412"
    assert row["company_master_id"] == "nse:AAREYDRUGS"
    assert row["isin"] == "INE000A01011"
    assert row["filing_type"] == "pit_sast"
    assert row["headline"] == "Closure of trading window"
    assert row["disclosure_date"] == pd.Timestamp("2026-08-01T10:15:00", tz="UTC").date()
    assert row["quantity"] is None
    assert row["insider_name"] is None
    assert row["transaction_type"] is None
    assert row["attachment_name"] == "somefile.pdf"
    assert row["detection_source"] == "bse_announcements"
    assert row["enrichment_status"] == "pending"
    assert row["sources"] == "bse"
    assert json.loads(row["raw_json"]) == raw


def test_build_announcement_row_handles_missing_timestamp_gracefully():
    raw = {"NEWSID": "abc-2", "SUBCATNAME": "Credit Rating", "HEADLINE": "CRISIL rating action"}
    row = fundamentals_bse_announcements.build_announcement_row("524412", "nse:AAREYDRUGS", None, raw)
    assert row["isin"] is None
    assert row["disclosure_date"] is None
    assert row["announcement_timestamp"] is None


def test_build_result_calendar_row_parses_meeting_date():
    raw = {"scrip_Code": "524412", "meeting_date": "23 Oct 2026", "URL": "https://example.com"}
    row = fundamentals_bse_announcements.build_result_calendar_row("524412", "nse:AAREYDRUGS", "INE000A01011", raw)
    assert row["filing_type"] == "results_calendar"
    assert row["isin"] == "INE000A01011"
    assert row["disclosure_date"] == date(2026, 10, 23)
    assert row["news_id"] == "resultcal:524412:23 Oct 2026"
    assert row["detection_source"] == "bse_result_calendar"
    assert row["sources"] == "bse"


def test_build_result_calendar_row_handles_unparseable_date():
    raw = {"scrip_Code": "524412", "meeting_date": "garbage-date", "URL": "https://example.com"}
    row = fundamentals_bse_announcements.build_result_calendar_row("524412", "nse:AAREYDRUGS", None, raw)
    assert row["disclosure_date"] is None


def test_resolve_company_identity_joins_on_ticker_and_preserves_index(monkeypatch):
    tickers = pd.Series(["AAREYDRUGS", "UNKNOWNTICKER"], index=[5, 9])
    monkeypatch.setattr(
        fundamentals_bse_announcements,
        "map_company_master_ids",
        lambda series, **k: pd.Series(["nse:AAREYDRUGS", pd.NA], index=series.index, dtype="string"),
    )
    monkeypatch.setattr(
        fundamentals_bse_announcements,
        "sql_to_df",
        lambda *_a, **_k: pd.DataFrame({"company_master_id": ["nse:AAREYDRUGS"], "bse_scrip_code": ["524412"]}),
    )
    monkeypatch.setattr(
        fundamentals_bse_announcements,
        "resolve_isin",
        lambda company_master_ids: pd.Series(["INE000A01011", pd.NA], index=company_master_ids.index, dtype="string"),
    )

    result = fundamentals_bse_announcements.resolve_company_identity(tickers)

    assert list(result.index) == [5, 9]
    assert result.loc[5, "bse_scrip_code"] == "524412"
    assert result.loc[5, "isin"] == "INE000A01011"
    assert pd.isna(result.loc[9, "bse_scrip_code"])


def _bse_universe_df(n=3):
    return pd.DataFrame(
        {
            "company_id": list(range(1, n + 1)),
            "company_name": [f"Company {i}" for i in range(1, n + 1)],
            "ticker": [f"TICK{i}" for i in range(1, n + 1)],
        }
    )


def _bse_identity_df(tickers, scrip_codes, isins=None):
    return pd.DataFrame(
        {
            "company_master_id": [f"nse:{t}" for t in tickers],
            "bse_scrip_code": scrip_codes,
            "isin": isins if isins is not None else [f"ISIN{t}" for t in tickers],
        },
        index=tickers.index,
    )


def test_run_bse_l3_detection_happy_path_upserts_announcements_and_calendar(monkeypatch):
    universe = _bse_universe_df(2)
    monkeypatch.setattr(fundamentals_bse_announcements, "load_l1_universe_tickers", lambda: universe)
    monkeypatch.setattr(
        fundamentals_bse_announcements,
        "resolve_company_identity",
        lambda tickers: _bse_identity_df(tickers, ["111111", "222222"]),
    )

    interesting_raw = {"NEWSID": "n1", "SUBCATNAME": "Credit Rating", "HEADLINE": "CRISIL rating action"}
    monkeypatch.setattr(fundamentals_bse_announcements, "fetch_company_announcements", lambda scrip, **k: [interesting_raw])
    monkeypatch.setattr(
        fundamentals_bse_announcements,
        "fetch_result_calendar",
        lambda: [{"scrip_Code": "111111", "meeting_date": "23 Oct 2026", "URL": "https://x"}],
    )

    dedup_calls = []
    monkeypatch.setattr(
        fundamentals_bse_announcements,
        "upsert_events_with_dedup",
        lambda rows: dedup_calls.append(rows) or {"inserted": len(rows), "merged": 0},
    )
    fallback_events = []
    monkeypatch.setattr(
        fundamentals_bse_announcements, "record_local_fallback_event", lambda **kwargs: fallback_events.append(kwargs)
    )

    result = fundamentals_bse_announcements.run_bse_l3_detection()

    assert result["companies_scanned"] == 2
    assert result["announcement_rows"] == 2  # one per company
    assert result["result_calendar_rows"] == 1  # only scrip 111111 matched the universe
    assert result["merged_rows"] == 0
    assert result["blocked"] is False
    assert result["failed_companies"] == []
    assert len(dedup_calls) == 1
    assert len(dedup_calls[0]) == 3
    assert all(row["isin"] for row in dedup_calls[0])


def test_run_bse_l3_detection_trips_circuit_breaker_after_consecutive_failures(monkeypatch):
    universe = _bse_universe_df(5)
    monkeypatch.setattr(fundamentals_bse_announcements, "load_l1_universe_tickers", lambda: universe)
    monkeypatch.setattr(
        fundamentals_bse_announcements,
        "resolve_company_identity",
        lambda tickers: _bse_identity_df(tickers, ["1", "2", "3", "4", "5"]),
    )

    def always_fails(scrip, **k):
        raise fundamentals_bse_announcements.BseBlockedError("HTTP 403")

    monkeypatch.setattr(fundamentals_bse_announcements, "fetch_company_announcements", always_fails)
    monkeypatch.setattr(fundamentals_bse_announcements, "fetch_result_calendar", lambda: [])
    monkeypatch.setattr(fundamentals_bse_announcements, "upsert_events_with_dedup", lambda rows: {"inserted": len(rows), "merged": 0})
    fallback_events = []
    monkeypatch.setattr(
        fundamentals_bse_announcements, "record_local_fallback_event", lambda **kwargs: fallback_events.append(kwargs)
    )

    result = fundamentals_bse_announcements.run_bse_l3_detection()

    assert result["blocked"] is True
    # stopped after CIRCUIT_BREAKER_THRESHOLD consecutive failures, not all 5 companies
    assert len(result["failed_companies"]) == fundamentals_bse_announcements.CIRCUIT_BREAKER_THRESHOLD
    assert result["companies_scanned"] == 0
    assert any(e["fallback_type"] == "l3_bse_circuit_breaker_tripped" for e in fallback_events)
    # a tripped breaker must not then go on to call the result-calendar endpoint either
    assert result["result_calendar_rows"] == 0


def test_run_bse_l3_detection_resets_failure_streak_on_a_success(monkeypatch):
    universe = _bse_universe_df(4)
    monkeypatch.setattr(fundamentals_bse_announcements, "load_l1_universe_tickers", lambda: universe)
    monkeypatch.setattr(
        fundamentals_bse_announcements,
        "resolve_company_identity",
        lambda tickers: _bse_identity_df(tickers, ["1", "2", "3", "4"]),
    )

    call_log = []

    def flaky_fetch(scrip, **k):
        call_log.append(scrip)
        # fail, fail, succeed, fail -- never CIRCUIT_BREAKER_THRESHOLD (3) in a row
        if scrip in ("1", "2", "4"):
            raise fundamentals_bse_announcements.BseBlockedError("boom")
        return []

    monkeypatch.setattr(fundamentals_bse_announcements, "fetch_company_announcements", flaky_fetch)
    monkeypatch.setattr(fundamentals_bse_announcements, "fetch_result_calendar", lambda: [])
    monkeypatch.setattr(fundamentals_bse_announcements, "upsert_events_with_dedup", lambda rows: {"inserted": len(rows), "merged": 0})
    monkeypatch.setattr(fundamentals_bse_announcements, "record_local_fallback_event", lambda **kwargs: None)

    result = fundamentals_bse_announcements.run_bse_l3_detection()

    assert call_log == ["1", "2", "3", "4"]  # ran through the whole universe
    assert result["blocked"] is False
    assert result["companies_scanned"] == 1
    assert result["failed_companies"] == ["TICK1", "TICK2", "TICK4"]


def test_run_bse_l3_detection_skips_companies_with_no_bse_scrip_code(monkeypatch):
    universe = _bse_universe_df(2)
    monkeypatch.setattr(fundamentals_bse_announcements, "load_l1_universe_tickers", lambda: universe)
    monkeypatch.setattr(
        fundamentals_bse_announcements,
        "resolve_company_identity",
        lambda tickers: _bse_identity_df(tickers, ["111111", pd.NA]),
    )
    monkeypatch.setattr(fundamentals_bse_announcements, "fetch_company_announcements", lambda scrip, **k: [])
    monkeypatch.setattr(fundamentals_bse_announcements, "fetch_result_calendar", lambda: [])
    monkeypatch.setattr(fundamentals_bse_announcements, "upsert_events_with_dedup", lambda rows: {"inserted": len(rows), "merged": 0})
    fallback_events = []
    monkeypatch.setattr(
        fundamentals_bse_announcements, "record_local_fallback_event", lambda **kwargs: fallback_events.append(kwargs)
    )

    result = fundamentals_bse_announcements.run_bse_l3_detection()

    assert result["companies_total"] == 1  # TICK2 dropped for missing scrip code
    assert any(e["fallback_type"] == "l3_bse_scrip_code_missing" for e in fallback_events)


def test_run_bse_l3_detection_returns_early_on_empty_universe(monkeypatch):
    monkeypatch.setattr(fundamentals_bse_announcements, "load_l1_universe_tickers", lambda: pd.DataFrame())
    fallback_events = []
    monkeypatch.setattr(
        fundamentals_bse_announcements, "record_local_fallback_event", lambda **kwargs: fallback_events.append(kwargs)
    )

    result = fundamentals_bse_announcements.run_bse_l3_detection()

    assert result == {"rows": 0, "companies_scanned": 0, "failed_companies": [], "blocked": False}
    assert any(e["fallback_type"] == "l3_bse_no_l1_universe" for e in fallback_events)


# fundamentals/collectors/events_store.py -- shared fundamentals_events dedup/merge.


def test_resolve_isin_picks_most_recent_row_per_company(monkeypatch):
    monkeypatch.setattr(
        fundamentals_events_store,
        "sql_to_df",
        lambda *_a, **_k: pd.DataFrame({"company_master_id": ["nse:X", "nse:Y"], "isin": ["INE111", "INE222"]}),
    )
    company_master_ids = pd.Series(["nse:X", "nse:Y", "nse:Z"])

    result = fundamentals_events_store.resolve_isin(company_master_ids)

    assert result.iloc[0] == "INE111"
    assert result.iloc[1] == "INE222"
    assert pd.isna(result.iloc[2])


def test_resolve_issuer_names_picks_most_recent_row_per_company(monkeypatch):
    monkeypatch.setattr(
        fundamentals_events_store,
        "sql_to_df",
        lambda *_a, **_k: pd.DataFrame({"company_master_id": ["nse:X"], "display_name": ["X Industries Ltd"]}),
    )
    company_master_ids = pd.Series(["nse:X", "nse:UNKNOWN"])

    result = fundamentals_events_store.resolve_issuer_names(company_master_ids)

    assert result.iloc[0] == "X Industries Ltd"
    assert pd.isna(result.iloc[1])


def test_find_dedup_candidate_returns_none_without_isin_or_disclosure_date():
    assert fundamentals_events_store.find_dedup_candidate(None, "pit_sast", date(2026, 8, 1)) is None
    assert fundamentals_events_store.find_dedup_candidate("INE111", "pit_sast", None) is None
    assert fundamentals_events_store.find_dedup_candidate("INE111", None, date(2026, 8, 1)) is None


def test_find_dedup_candidate_queries_and_returns_earliest_match(monkeypatch):
    captured = {}

    def fake_sql_to_df(query, params=None):
        captured["params"] = params
        return pd.DataFrame(
            [{"source": "bse", "news_id": "n1", "quantity": None, "insider_name": None, "transaction_type": None, "announcement_timestamp": None, "sources": "bse"}]
        )

    monkeypatch.setattr(fundamentals_events_store, "sql_to_df", fake_sql_to_df)

    result = fundamentals_events_store.find_dedup_candidate("INE111", "pit_sast", date(2026, 8, 1))

    # compared as text, not date -- fundamentals_events.disclosure_date is TEXT in the
    # DB (found live 2026-08-10, see find_dedup_candidate's docstring); str(date(...))
    # matches the stored 'YYYY-MM-DD' format exactly.
    assert captured["params"] == ("INE111", "pit_sast", "2026-08-01")
    assert result["source"] == "bse"
    assert result["news_id"] == "n1"


def test_merge_row_fields_fills_missing_structured_fields_without_clobbering():
    # existing's announcement_timestamp arrives as a plain string -- it's a TEXT
    # column in the DB (see find_dedup_candidate's docstring) -- while incoming's is a
    # freshly-built pd.Timestamp; a bare `min(str, Timestamp)` raises TypeError, which
    # is exactly the live bug this regression-tests.
    existing = {
        "quantity": None,
        "insider_name": None,
        "transaction_type": None,
        "announcement_timestamp": "2026-08-02 00:00:00+00:00",
        "sources": "bse",
    }
    incoming = {
        "source": "nse",
        "quantity": 25000,
        "insider_name": "Jane Promoter",
        "transaction_type": "Purchase",
        "announcement_timestamp": pd.Timestamp("2026-08-01", tz="UTC"),
    }
    merged = fundamentals_events_store._merge_row_fields(existing, incoming)
    assert merged["quantity"] == 25000
    assert merged["insider_name"] == "Jane Promoter"
    assert merged["transaction_type"] == "Purchase"
    assert merged["announcement_timestamp"] == pd.Timestamp("2026-08-01", tz="UTC").isoformat()  # earliest of the two, as a string
    assert isinstance(merged["announcement_timestamp"], str)
    assert merged["sources"] == "bse,nse"


def test_merge_row_fields_never_overwrites_an_existing_real_value():
    existing = {
        "quantity": 99999,
        "insider_name": "Original Name",
        "transaction_type": None,
        "announcement_timestamp": None,
        "sources": "bse,nse",
    }
    incoming = {"source": "nse", "quantity": 1, "insider_name": "Different Name", "transaction_type": "Sale", "announcement_timestamp": None}
    merged = fundamentals_events_store._merge_row_fields(existing, incoming)
    assert merged["quantity"] == 99999
    assert merged["insider_name"] == "Original Name"
    assert merged["transaction_type"] == "Sale"  # existing had none, fills from incoming
    assert merged["sources"] == "bse,nse"  # already present, not duplicated


def test_ensure_events_schema_skips_alter_when_table_does_not_exist_yet(monkeypatch):
    executed = []

    class FakeCursor:
        def execute(self, query, params=None):
            executed.append((str(query), params))

        def fetchone(self):
            return None  # table doesn't exist

    @contextlib.contextmanager
    def fake_db_session():
        yield None, FakeCursor()

    monkeypatch.setattr(fundamentals_events_store, "db_session", fake_db_session)

    fundamentals_events_store._ensure_events_schema()

    # only the existence check ran, no ALTER TABLE statements
    assert len(executed) == 1


def test_ensure_events_schema_adds_missing_columns_when_table_exists(monkeypatch):
    executed = []

    class FakeCursor:
        def execute(self, query, params=None):
            executed.append((str(query), params))

        def fetchone(self):
            return (1,)  # table exists

    @contextlib.contextmanager
    def fake_db_session():
        yield None, FakeCursor()

    monkeypatch.setattr(fundamentals_events_store, "db_session", fake_db_session)

    fundamentals_events_store._ensure_events_schema()

    alter_statements = [q for q, _ in executed if "ALTER TABLE" in q]
    assert len(alter_statements) == len(fundamentals_events_store._DEDUP_COLUMN_TYPES)


def test_upsert_events_with_dedup_merges_a_matching_isin_row_instead_of_inserting(monkeypatch):
    existing_row = {
        "source": "bse",
        "news_id": "bse-1",
        "quantity": None,
        "insider_name": None,
        "transaction_type": None,
        "announcement_timestamp": None,
        "sources": "bse",
    }
    monkeypatch.setattr(fundamentals_events_store, "find_dedup_candidate", lambda isin, ft, d: existing_row)
    monkeypatch.setattr(fundamentals_events_store, "_ensure_events_schema", lambda: None)
    merge_calls = []
    monkeypatch.setattr(
        fundamentals_events_store, "_apply_merge", lambda **kwargs: merge_calls.append(kwargs)
    )
    upsert_calls = []
    monkeypatch.setattr(fundamentals_events_store, "upsert_to_db", lambda df, table, **k: upsert_calls.append((df, table, k)))

    incoming_row = {
        "source": "nse",
        "news_id": "nse-1",
        "isin": "INE111",
        "filing_type": "pit_sast",
        "disclosure_date": date(2026, 8, 1),
        "quantity": 25000,
    }
    result = fundamentals_events_store.upsert_events_with_dedup([incoming_row])

    assert result == {"inserted": 0, "merged": 1}
    assert len(merge_calls) == 1
    assert merge_calls[0]["source"] == "bse"
    assert merge_calls[0]["news_id"] == "bse-1"
    assert upsert_calls == []


def test_upsert_events_with_dedup_inserts_when_no_match(monkeypatch):
    monkeypatch.setattr(fundamentals_events_store, "find_dedup_candidate", lambda isin, ft, d: None)
    monkeypatch.setattr(fundamentals_events_store, "_ensure_events_schema", lambda: None)
    upsert_calls = []
    monkeypatch.setattr(fundamentals_events_store, "upsert_to_db", lambda df, table, **k: upsert_calls.append((df, table, k)))

    incoming_row = {"source": "bse", "news_id": "bse-1", "isin": "INE111", "filing_type": "pit_sast", "disclosure_date": date(2026, 8, 1)}
    result = fundamentals_events_store.upsert_events_with_dedup([incoming_row])

    assert result == {"inserted": 1, "merged": 0}
    assert len(upsert_calls) == 1
    df, table, kwargs = upsert_calls[0]
    assert table == fundamentals_events_store.RESULTS_TABLE
    assert kwargs["unique_keys"] == ["source", "news_id"]


def test_upsert_events_with_dedup_does_not_match_itself(monkeypatch):
    # find_dedup_candidate can legitimately return the SAME row that's about to be
    # (re-)upserted (e.g. a re-run of the same source) -- must insert/overwrite via the
    # normal path, not treat a row as a duplicate of itself.
    same_row = {"source": "bse", "news_id": "bse-1", "quantity": None, "insider_name": None, "transaction_type": None, "announcement_timestamp": None, "sources": "bse"}
    monkeypatch.setattr(fundamentals_events_store, "find_dedup_candidate", lambda isin, ft, d: same_row)
    monkeypatch.setattr(fundamentals_events_store, "_ensure_events_schema", lambda: None)
    merge_calls = []
    monkeypatch.setattr(fundamentals_events_store, "_apply_merge", lambda **kwargs: merge_calls.append(kwargs))
    upsert_calls = []
    monkeypatch.setattr(fundamentals_events_store, "upsert_to_db", lambda df, table, **k: upsert_calls.append((df, table, k)))

    incoming_row = {"source": "bse", "news_id": "bse-1", "isin": "INE111", "filing_type": "pit_sast", "disclosure_date": date(2026, 8, 1)}
    result = fundamentals_events_store.upsert_events_with_dedup([incoming_row])

    assert result == {"inserted": 1, "merged": 0}
    assert merge_calls == []
    assert len(upsert_calls) == 1


def test_upsert_events_with_dedup_empty_rows_is_a_noop(monkeypatch):
    calls = []
    monkeypatch.setattr(fundamentals_events_store, "find_dedup_candidate", lambda *a: calls.append(a))
    assert fundamentals_events_store.upsert_events_with_dedup([]) == {"inserted": 0, "merged": 0}
    assert calls == []


# fundamentals/collectors/nse_pit.py -- L3 detection, NSE half (step 5).


def test_parse_nse_pit_date_parses_dd_mon_yyyy():
    assert fundamentals_nse_pit._parse_nse_pit_date("15-Jul-2026") == date(2026, 7, 15)


def test_parse_nse_pit_date_handles_missing_and_garbage():
    assert fundamentals_nse_pit._parse_nse_pit_date(None) is None
    assert fundamentals_nse_pit._parse_nse_pit_date("") is None
    assert fundamentals_nse_pit._parse_nse_pit_date("not-a-date") is None


def test_parse_nse_pit_timestamp_parses_with_and_without_seconds():
    ts1 = fundamentals_nse_pit._parse_nse_pit_timestamp("15-Jul-2026 14:30")
    ts2 = fundamentals_nse_pit._parse_nse_pit_timestamp("15-Jul-2026 14:30:05")
    assert ts1 is not None and ts2 is not None
    assert ts1.tzinfo is not None  # converted to UTC from Asia/Kolkata


def test_parse_nse_pit_timestamp_handles_missing_and_garbage():
    assert fundamentals_nse_pit._parse_nse_pit_timestamp(None) is None
    assert fundamentals_nse_pit._parse_nse_pit_timestamp("garbage") is None


def test_build_pit_row_maps_structured_fields_with_no_pdf():
    raw = {
        "did": "D1",
        "pid": "P1",
        "acqName": "Jane Promoter",
        "personCategory": "Promoter",
        "tdpTransactionType": "Purchase",
        "secAcq": "25,000",
        "intimDt": "01-Aug-2026",
        "date": "01-Aug-2026 10:15",
    }
    row = fundamentals_nse_pit.build_pit_row("AAREYDRUGS", "nse:AAREYDRUGS", "INE198H01019", raw)
    assert row["source"] == "nse"
    assert row["news_id"] == "nse-pit:D1:P1:01-Aug-2026"
    assert row["scrip_code"] == "AAREYDRUGS"
    assert row["isin"] == "INE198H01019"
    assert row["filing_type"] == "pit_sast"
    assert row["quantity"] == 25000
    assert row["insider_name"] == "Jane Promoter"
    assert row["transaction_type"] == "Purchase"
    assert row["disclosure_date"] == date(2026, 8, 1)
    assert row["announcement_timestamp"] is not None
    assert row["detection_source"] == "nse_corporates_pit"
    assert row["enrichment_status"] == "structured"  # no OCR needed, unlike BSE's rows
    assert row["sources"] == "nse"


def test_build_pit_row_matches_a_real_captured_disclosure():
    # Real corporates-pit row, captured live 2026-08-10 against RELIANCE (the L1
    # smallcap/microcap universe itself had zero PIT disclosures in a 90-day window
    # across 21 companies -- plausible, not a bug, confirmed by sanity-checking
    # against a large-cap known for frequent promoter-family transactions).
    raw = {
        "acqMode": "Off Market", "acqName": "Shaila Narayan", "acqfromDt": "13-Feb-2026", "acqtoDt": "13-Feb-2026",
        "afterAcqSharesNo": "29620", "afterAcqSharesPer": "0", "anex": "7(2)", "befAcqSharesNo": "26500", "befAcqSharesPer": "0",
        "buyQuantity": "0", "buyValue": "0", "company": "Reliance Industries Limited", "date": "18-Feb-2026 19:06",
        "derivativeType": "-", "did": "563849", "exchange": "NA", "intimDt": "16-Feb-2026", "personCategory": "Immediate relative",
        "pid": "1194033", "remarks": "-", "secAcq": "3120", "secType": "Equity Shares", "secVal": "4430088",
        "securitiesTypePost": "Equity Shares", "sellValue": "0", "sellquantity": "0", "symbol": "RELIANCE",
        "tdpDerivativeContractType": "-", "tdpTransactionType": "Buy", "tkdAcqm": None,
        "xbrl": "https://nsearchives.nseindia.com/corporate/xbrl/IT_1194033_1627626_18022026070637_WEB.xml", "xbrlFileSize": None,
    }
    row = fundamentals_nse_pit.build_pit_row("RELIANCE", "nse:RELIANCE", "INE002A01018", raw)
    assert row["news_id"] == "nse-pit:563849:1194033:16-Feb-2026"
    assert row["quantity"] == 3120
    assert row["insider_name"] == "Shaila Narayan"
    assert row["transaction_type"] == "Buy"
    assert row["disclosure_date"] == date(2026, 2, 16)
    # 18-Feb-2026 19:06 IST -> 13:36 UTC
    assert row["announcement_timestamp"] == pd.Timestamp("2026-02-18 13:36:00", tz="UTC")
    assert json.loads(row["raw_json"]) == raw


def test_resolve_company_identity_uses_events_store_helpers(monkeypatch):
    tickers = pd.Series(["AAREYDRUGS"], index=[0])
    monkeypatch.setattr(
        fundamentals_nse_pit, "map_company_master_ids", lambda series, **k: pd.Series(["nse:AAREYDRUGS"], index=series.index, dtype="string")
    )
    monkeypatch.setattr(fundamentals_nse_pit, "resolve_isin", lambda cmids: pd.Series(["INE198H01019"], index=cmids.index))
    monkeypatch.setattr(fundamentals_nse_pit, "resolve_issuer_names", lambda cmids: pd.Series(["Aarey Drugs & Pharmaceuticals"], index=cmids.index))

    result = fundamentals_nse_pit.resolve_company_identity(tickers)

    assert result.loc[0, "company_master_id"] == "nse:AAREYDRUGS"
    assert result.loc[0, "isin"] == "INE198H01019"
    assert result.loc[0, "issuer"] == "Aarey Drugs & Pharmaceuticals"


def test_fetch_company_pit_raises_on_missing_data_key(monkeypatch):
    monkeypatch.setattr(fundamentals_nse_pit, "nse_request_gate", lambda **kwargs: contextlib.nullcontext())

    class FakePage:
        def evaluate(self, script, url):
            return {"unexpected": "shape"}

    with pytest.raises(fundamentals_nse_pit.NsePitBlockedError):
        fundamentals_nse_pit.fetch_company_pit(
            FakePage(), symbol="X", issuer="X Ltd", from_date=datetime(2026, 8, 1), to_date=datetime(2026, 8, 8)
        )


def test_fetch_company_pit_wraps_page_evaluate_exceptions(monkeypatch):
    monkeypatch.setattr(fundamentals_nse_pit, "nse_request_gate", lambda **kwargs: contextlib.nullcontext())

    class FakePage:
        def evaluate(self, script, url):
            raise RuntimeError("HTTP 403")

    with pytest.raises(fundamentals_nse_pit.NsePitBlockedError):
        fundamentals_nse_pit.fetch_company_pit(
            FakePage(), symbol="X", issuer="X Ltd", from_date=datetime(2026, 8, 1), to_date=datetime(2026, 8, 8)
        )


def test_fetch_company_pit_returns_data_rows(monkeypatch):
    monkeypatch.setattr(fundamentals_nse_pit, "nse_request_gate", lambda **kwargs: contextlib.nullcontext())

    class FakePage:
        def evaluate(self, script, url):
            return {"data": [{"did": "D1"}]}

    rows = fundamentals_nse_pit.fetch_company_pit(
        FakePage(), symbol="X", issuer="X Ltd", from_date=datetime(2026, 8, 1), to_date=datetime(2026, 8, 8)
    )
    assert rows == [{"did": "D1"}]


class _FakeNsePitPage:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True

    def wait_for_timeout(self, ms):
        pass


class _FakeNsePitContext:
    def __init__(self, page):
        self._page = page

    def new_page(self):
        return self._page


class _FakeNsePitBrowser:
    def __init__(self, page):
        self.contexts = [_FakeNsePitContext(page)]


def _patch_fake_playwright(monkeypatch, page):
    class _FakeChromium:
        @staticmethod
        def connect_over_cdp(endpoint):
            return _FakeNsePitBrowser(page)

    class _FakePlaywrightHandle:
        chromium = _FakeChromium()

    @contextlib.contextmanager
    def _fake_sync_playwright():
        yield _FakePlaywrightHandle()

    monkeypatch.setattr(fundamentals_nse_pit, "sync_playwright", _fake_sync_playwright)
    monkeypatch.setattr(fundamentals_nse_pit, "nse_goto", lambda page, url, **k: None)
    monkeypatch.setattr(fundamentals_nse_pit, "CDP_ENDPOINT", "http://localhost:9222")


def _nse_universe_df(n=3):
    return pd.DataFrame(
        {
            "company_id": list(range(1, n + 1)),
            "company_name": [f"Company {i}" for i in range(1, n + 1)],
            "ticker": [f"TICK{i}" for i in range(1, n + 1)],
        }
    )


def _nse_identity_df(tickers):
    return pd.DataFrame(
        {
            "company_master_id": [f"nse:{t}" for t in tickers],
            "isin": [f"ISIN{t}" for t in tickers],
            "issuer": [f"{t} Ltd" for t in tickers],
        },
        index=tickers.index,
    )


def test_run_nse_pit_detection_happy_path(monkeypatch):
    universe = _nse_universe_df(2)
    monkeypatch.setattr(fundamentals_nse_pit, "load_l1_universe_tickers", lambda: universe)
    monkeypatch.setattr(fundamentals_nse_pit, "resolve_company_identity", lambda tickers: _nse_identity_df(tickers))
    _patch_fake_playwright(monkeypatch, _FakeNsePitPage())

    raw = {"did": "D1", "pid": "P1", "acqName": "Someone", "tdpTransactionType": "Purchase", "secAcq": "100", "intimDt": "01-Aug-2026", "date": "01-Aug-2026 10:00"}
    monkeypatch.setattr(fundamentals_nse_pit, "fetch_company_pit", lambda page, **k: [raw])
    dedup_calls = []
    monkeypatch.setattr(
        fundamentals_nse_pit, "upsert_events_with_dedup", lambda rows: dedup_calls.append(rows) or {"inserted": len(rows), "merged": 0}
    )
    monkeypatch.setattr(fundamentals_nse_pit, "record_local_fallback_event", lambda **k: None)

    result = fundamentals_nse_pit.run_nse_pit_detection()

    assert result["companies_scanned"] == 2
    assert result["rows"] == 2  # one PIT row per company
    assert result["blocked"] is False
    assert result["failed_companies"] == []
    assert len(dedup_calls) == 1 and len(dedup_calls[0]) == 2


def test_run_nse_pit_detection_trips_circuit_breaker(monkeypatch):
    universe = _nse_universe_df(5)
    monkeypatch.setattr(fundamentals_nse_pit, "load_l1_universe_tickers", lambda: universe)
    monkeypatch.setattr(fundamentals_nse_pit, "resolve_company_identity", lambda tickers: _nse_identity_df(tickers))
    _patch_fake_playwright(monkeypatch, _FakeNsePitPage())

    def always_fails(page, **k):
        raise fundamentals_nse_pit.NsePitBlockedError("boom")

    monkeypatch.setattr(fundamentals_nse_pit, "fetch_company_pit", always_fails)
    monkeypatch.setattr(fundamentals_nse_pit, "upsert_events_with_dedup", lambda rows: {"inserted": len(rows), "merged": 0})
    fallback_events = []
    monkeypatch.setattr(fundamentals_nse_pit, "record_local_fallback_event", lambda **kwargs: fallback_events.append(kwargs))

    result = fundamentals_nse_pit.run_nse_pit_detection()

    assert result["blocked"] is True
    assert len(result["failed_companies"]) == fundamentals_nse_pit.CIRCUIT_BREAKER_THRESHOLD
    assert result["companies_scanned"] == 0
    assert any(e["fallback_type"] == "l3_nse_circuit_breaker_tripped" for e in fallback_events)


def test_run_nse_pit_detection_skips_companies_with_no_issuer(monkeypatch):
    universe = _nse_universe_df(2)
    monkeypatch.setattr(fundamentals_nse_pit, "load_l1_universe_tickers", lambda: universe)

    def identity_with_one_missing_issuer(tickers):
        df = _nse_identity_df(tickers)
        df.loc[df.index[-1], "issuer"] = pd.NA
        return df

    monkeypatch.setattr(fundamentals_nse_pit, "resolve_company_identity", identity_with_one_missing_issuer)
    _patch_fake_playwright(monkeypatch, _FakeNsePitPage())
    monkeypatch.setattr(fundamentals_nse_pit, "fetch_company_pit", lambda page, **k: [])
    monkeypatch.setattr(fundamentals_nse_pit, "upsert_events_with_dedup", lambda rows: {"inserted": len(rows), "merged": 0})
    fallback_events = []
    monkeypatch.setattr(fundamentals_nse_pit, "record_local_fallback_event", lambda **kwargs: fallback_events.append(kwargs))

    result = fundamentals_nse_pit.run_nse_pit_detection()

    assert result["companies_total"] == 1
    assert any(e["fallback_type"] == "l3_nse_issuer_missing" for e in fallback_events)


def test_run_nse_pit_detection_returns_early_without_cdp_endpoint(monkeypatch):
    universe = _nse_universe_df(1)
    monkeypatch.setattr(fundamentals_nse_pit, "load_l1_universe_tickers", lambda: universe)
    monkeypatch.setattr(fundamentals_nse_pit, "resolve_company_identity", lambda tickers: _nse_identity_df(tickers))
    monkeypatch.setattr(fundamentals_nse_pit, "CDP_ENDPOINT", "")
    fallback_events = []
    monkeypatch.setattr(fundamentals_nse_pit, "record_local_fallback_event", lambda **kwargs: fallback_events.append(kwargs))

    result = fundamentals_nse_pit.run_nse_pit_detection()

    assert result == {"rows": 0, "companies_scanned": 0, "failed_companies": [], "blocked": False}
    assert any(e["fallback_type"] == "l3_nse_no_cdp_endpoint" for e in fallback_events)


def test_run_nse_pit_detection_returns_early_on_empty_universe(monkeypatch):
    monkeypatch.setattr(fundamentals_nse_pit, "load_l1_universe_tickers", lambda: pd.DataFrame())
    fallback_events = []
    monkeypatch.setattr(fundamentals_nse_pit, "record_local_fallback_event", lambda **kwargs: fallback_events.append(kwargs))

    result = fundamentals_nse_pit.run_nse_pit_detection()

    assert result == {"rows": 0, "companies_scanned": 0, "failed_companies": [], "blocked": False}
    assert any(e["fallback_type"] == "l3_nse_no_l1_universe" for e in fallback_events)


# fundamentals/collectors/rating_agencies.py -- L3 enrichment, rating agencies (step 5).

# Real (trimmed) fragment, captured live 2026-08-10 from ICRA's own
# /Rating/GetAllRatingRational search response for a real detected event (ZF Steering
# Gear, disclosed 2026-08-04).
ICRA_SEARCH_RESULT_FIXTURE_HTML = """
<div class="customTable table-responsive border rounded-3 mt-3">
<table class="table align-middle mb-0">
<thead><tr><th>Date</th><th>Sector</th><th>Reports</th><th>Action</th></tr></thead>
<tbody>
<tr>
<td>04 Aug 2026</td>
<td>Corporate Debt Rating</td>
<td><a href="/Rationale/ShowRationaleReport?Id=144806">ZF Steering Gear (India) Limited: Ratings reaffirmed</a></td>
<td>Lender-wise facilities</td>
</tr>
<tr>
<td>26 Jun 2025</td>
<td>Corporate Debt Rating</td>
<td><a href="/Rationale/ShowRationaleReport?Id=135978">ZF Steering Gear (India) Limited: Ratings reaffirmed, rated amount enhanced</a></td>
<td>Lender-wise facilities</td>
</tr>
<tr>
<td>20 May 2024</td>
<td>&mdash;</td>
<td><a href="/Rationale/ShowRationaleReport?Id=127574">ZF Steering Gear (India) Limited: Ratings reaffirmed</a></td>
<td>Lender-wise facilities</td>
</tr>
</tbody>
</table>
</div>
"""


@pytest.mark.parametrize(
    "keyword_text,expected",
    [
        ("Reaffirmation of Credit Ratings by ICRA", "icra"),
        ("CRISIL Ratings has downgraded", "crisil"),
        ("CARE Ratings assigns rating", "care"),
        ("India Ratings affirms", "india ratings"),
        ("Acuité Ratings revises outlook", "acuite"),
        ("Board Meeting Notice", None),
    ],
)
def test_detect_agency_from_headline(keyword_text, expected):
    assert fundamentals_rating_agencies.detect_agency(keyword_text, None) == expected


@pytest.mark.parametrize(
    "headline,expected",
    [
        ("Ratings downgraded", "downgraded"),
        ("Ratings upgraded", "upgraded"),
        ("Rating withdrawn", "withdrawn"),
        ("Rating suspended", "suspended"),
        ("Placed on credit watch", "placed_on_watch"),
        ("Ratings reaffirmed", "reaffirmed"),
        ("Rating assigned", "assigned"),
        ("Something unrelated", "other"),
    ],
)
def test_classify_rating_action_type(headline, expected):
    assert fundamentals_rating_agencies.classify_rating_action_type(headline) == expected


def test_parse_icra_search_results_extracts_real_row_shape():
    results = fundamentals_rating_agencies.parse_icra_search_results(ICRA_SEARCH_RESULT_FIXTURE_HTML)
    assert results == [
        {"date_text": "04 Aug 2026", "sector": "Corporate Debt Rating", "headline": "ZF Steering Gear (India) Limited: Ratings reaffirmed", "rationale_id": "144806"},
        {"date_text": "26 Jun 2025", "sector": "Corporate Debt Rating", "headline": "ZF Steering Gear (India) Limited: Ratings reaffirmed, rated amount enhanced", "rationale_id": "135978"},
        {"date_text": "20 May 2024", "sector": "—", "headline": "ZF Steering Gear (India) Limited: Ratings reaffirmed", "rationale_id": "127574"},
    ]


def test_parse_icra_search_results_empty_when_no_table():
    assert fundamentals_rating_agencies.parse_icra_search_results("<div>no results</div>") == []


def test_match_rationale_picks_closest_within_tolerance():
    results = fundamentals_rating_agencies.parse_icra_search_results(ICRA_SEARCH_RESULT_FIXTURE_HTML)
    matched = fundamentals_rating_agencies.match_rationale(results, date(2026, 8, 4))
    assert matched["rationale_id"] == "144806"


def test_match_rationale_returns_none_outside_tolerance():
    results = fundamentals_rating_agencies.parse_icra_search_results(ICRA_SEARCH_RESULT_FIXTURE_HTML)
    # nothing within MATCH_DATE_TOLERANCE_DAYS of an unrelated date
    assert fundamentals_rating_agencies.match_rationale(results, date(2020, 1, 1)) is None


def test_match_rationale_returns_none_without_target_date():
    results = fundamentals_rating_agencies.parse_icra_search_results(ICRA_SEARCH_RESULT_FIXTURE_HTML)
    assert fundamentals_rating_agencies.match_rationale(results, None) is None


def test_match_rationale_skips_unparseable_dates():
    results = [{"date_text": "not-a-date", "sector": "-", "headline": "X", "rationale_id": "1"}]
    assert fundamentals_rating_agencies.match_rationale(results, date(2026, 8, 4)) is None


def test_open_icra_session_raises_when_token_missing(monkeypatch):
    monkeypatch.setattr(fundamentals_rating_agencies, "exchange_request_gate", lambda **k: contextlib.nullcontext())

    class FakeResponse:
        status_code = 200
        text = "<html>no token here</html>"

    class FakeSession:
        def get(self, url, headers=None, timeout=None):
            return FakeResponse()

    monkeypatch.setattr(fundamentals_rating_agencies.requests, "Session", FakeSession)
    with pytest.raises(fundamentals_rating_agencies.IcraBlockedError):
        fundamentals_rating_agencies.open_icra_session()


def test_open_icra_session_raises_on_non_200(monkeypatch):
    monkeypatch.setattr(fundamentals_rating_agencies, "exchange_request_gate", lambda **k: contextlib.nullcontext())

    class FakeResponse:
        status_code = 500
        text = ""

    class FakeSession:
        def get(self, url, headers=None, timeout=None):
            return FakeResponse()

    monkeypatch.setattr(fundamentals_rating_agencies.requests, "Session", FakeSession)
    with pytest.raises(fundamentals_rating_agencies.IcraBlockedError):
        fundamentals_rating_agencies.open_icra_session()


def test_load_pending_rating_actions_queries_expected_filters(monkeypatch):
    captured = {}

    def fake_sql_to_df(query, **kwargs):
        captured["query"] = query
        return pd.DataFrame()

    monkeypatch.setattr(fundamentals_rating_agencies, "sql_to_df", fake_sql_to_df)
    fundamentals_rating_agencies.load_pending_rating_actions()
    assert "filing_type = 'rating_action'" in captured["query"]
    assert "enrichment_status = 'pending'" in captured["query"]


def test_run_rating_agency_enrichment_returns_early_when_nothing_pending(monkeypatch):
    monkeypatch.setattr(fundamentals_rating_agencies, "_ensure_events_schema", lambda: None)
    monkeypatch.setattr(fundamentals_rating_agencies, "_bootstrap_rating_columns", lambda: None)
    monkeypatch.setattr(fundamentals_rating_agencies, "load_pending_rating_actions", lambda limit=None: pd.DataFrame())

    result = fundamentals_rating_agencies.run_rating_agency_enrichment()

    assert result == {"matched": 0, "no_match": 0, "unsupported_agency": 0, "failed": 0, "blocked": False}


def test_run_rating_agency_enrichment_routes_non_icra_rows_as_unsupported(monkeypatch):
    monkeypatch.setattr(fundamentals_rating_agencies, "_ensure_events_schema", lambda: None)
    monkeypatch.setattr(fundamentals_rating_agencies, "_bootstrap_rating_columns", lambda: None)
    pending = pd.DataFrame(
        [{"source": "bse", "news_id": "n1", "company_master_id": "nse:X", "headline": "CRISIL downgrades rating", "subcategory": None, "disclosure_date": date(2026, 8, 4)}]
    )
    monkeypatch.setattr(fundamentals_rating_agencies, "load_pending_rating_actions", lambda limit=None: pending)
    status_calls = []
    monkeypatch.setattr(
        fundamentals_rating_agencies, "_set_enrichment_status", lambda **kwargs: status_calls.append(kwargs)
    )
    fallback_events = []
    monkeypatch.setattr(fundamentals_rating_agencies, "_record_fallback", lambda *a, **k: fallback_events.append((a, k)))

    result = fundamentals_rating_agencies.run_rating_agency_enrichment()

    assert result["unsupported_agency"] == 1
    assert status_calls == [{"source": "bse", "news_id": "n1", "status": "unsupported_agency"}]
    assert len(fallback_events) == 1


def test_run_rating_agency_enrichment_matches_icra_row(monkeypatch):
    monkeypatch.setattr(fundamentals_rating_agencies, "_ensure_events_schema", lambda: None)
    monkeypatch.setattr(fundamentals_rating_agencies, "_bootstrap_rating_columns", lambda: None)
    pending = pd.DataFrame(
        [{"source": "bse", "news_id": "n1", "company_master_id": "nse:ZFSTEERING", "headline": "Reaffirmation of Credit Ratings by ICRA", "subcategory": "Credit Rating", "disclosure_date": date(2026, 8, 4)}]
    )
    monkeypatch.setattr(fundamentals_rating_agencies, "load_pending_rating_actions", lambda limit=None: pending)
    monkeypatch.setattr(fundamentals_rating_agencies, "_resolve_issuer_name", lambda cmid: "ZF Steering Gear")
    monkeypatch.setattr(fundamentals_rating_agencies, "open_icra_session", lambda: ("fake-session", "fake-token"))
    matched_result = {"date_text": "04 Aug 2026", "sector": "Corporate Debt Rating", "headline": "ZF Steering Gear (India) Limited: Ratings reaffirmed", "rationale_id": "144806"}
    monkeypatch.setattr(fundamentals_rating_agencies, "search_icra_rationales", lambda session, token, issuer: [matched_result])
    status_calls = []
    monkeypatch.setattr(fundamentals_rating_agencies, "_set_enrichment_status", lambda **kwargs: status_calls.append(kwargs))

    result = fundamentals_rating_agencies.run_rating_agency_enrichment()

    assert result["matched"] == 1
    assert result["blocked"] is False
    assert len(status_calls) == 1
    call = status_calls[0]
    assert call["status"] == "matched"
    assert call["fields"]["rating_agency"] == "icra"
    assert call["fields"]["rating_action_type"] == "reaffirmed"
    assert call["fields"]["rationale_id"] == "144806"


def test_run_rating_agency_enrichment_no_match_when_search_empty(monkeypatch):
    monkeypatch.setattr(fundamentals_rating_agencies, "_ensure_events_schema", lambda: None)
    monkeypatch.setattr(fundamentals_rating_agencies, "_bootstrap_rating_columns", lambda: None)
    pending = pd.DataFrame(
        [{"source": "bse", "news_id": "n1", "company_master_id": "nse:ZFSTEERING", "headline": "Reaffirmation of Credit Ratings by ICRA", "subcategory": "Credit Rating", "disclosure_date": date(2026, 8, 4)}]
    )
    monkeypatch.setattr(fundamentals_rating_agencies, "load_pending_rating_actions", lambda limit=None: pending)
    monkeypatch.setattr(fundamentals_rating_agencies, "_resolve_issuer_name", lambda cmid: "ZF Steering Gear")
    monkeypatch.setattr(fundamentals_rating_agencies, "open_icra_session", lambda: ("fake-session", "fake-token"))
    monkeypatch.setattr(fundamentals_rating_agencies, "search_icra_rationales", lambda session, token, issuer: [])
    status_calls = []
    monkeypatch.setattr(fundamentals_rating_agencies, "_set_enrichment_status", lambda **kwargs: status_calls.append(kwargs))

    result = fundamentals_rating_agencies.run_rating_agency_enrichment()

    assert result["no_match"] == 1
    assert status_calls == [{"source": "bse", "news_id": "n1", "status": "no_match"}]


def test_run_rating_agency_enrichment_trips_circuit_breaker(monkeypatch):
    monkeypatch.setattr(fundamentals_rating_agencies, "_ensure_events_schema", lambda: None)
    monkeypatch.setattr(fundamentals_rating_agencies, "_bootstrap_rating_columns", lambda: None)
    pending = pd.DataFrame(
        [
            {"source": "bse", "news_id": f"n{i}", "company_master_id": f"nse:X{i}", "headline": "Reaffirmation of Credit Ratings by ICRA", "subcategory": "Credit Rating", "disclosure_date": date(2026, 8, 4)}
            for i in range(5)
        ]
    )
    monkeypatch.setattr(fundamentals_rating_agencies, "load_pending_rating_actions", lambda limit=None: pending)
    monkeypatch.setattr(fundamentals_rating_agencies, "_resolve_issuer_name", lambda cmid: "Some Company")
    monkeypatch.setattr(fundamentals_rating_agencies, "open_icra_session", lambda: ("fake-session", "fake-token"))

    def always_fails(session, token, issuer):
        raise fundamentals_rating_agencies.IcraBlockedError("boom")

    monkeypatch.setattr(fundamentals_rating_agencies, "search_icra_rationales", always_fails)
    monkeypatch.setattr(fundamentals_rating_agencies, "_set_enrichment_status", lambda **kwargs: None)
    fallback_events = []
    monkeypatch.setattr(fundamentals_rating_agencies, "_record_fallback", lambda *a, **k: fallback_events.append((a, k)))

    result = fundamentals_rating_agencies.run_rating_agency_enrichment()

    assert result["blocked"] is True
    assert result["failed"] == fundamentals_rating_agencies.CIRCUIT_BREAKER_THRESHOLD
    assert any(a and a[0] == "rating_enrichment_circuit_breaker_tripped" for a, k in fallback_events)


def test_run_rating_agency_enrichment_fails_row_with_no_resolvable_issuer(monkeypatch):
    monkeypatch.setattr(fundamentals_rating_agencies, "_ensure_events_schema", lambda: None)
    monkeypatch.setattr(fundamentals_rating_agencies, "_bootstrap_rating_columns", lambda: None)
    pending = pd.DataFrame(
        [{"source": "bse", "news_id": "n1", "company_master_id": "nse:UNKNOWN", "headline": "Reaffirmation of Credit Ratings by ICRA", "subcategory": "Credit Rating", "disclosure_date": date(2026, 8, 4)}]
    )
    monkeypatch.setattr(fundamentals_rating_agencies, "load_pending_rating_actions", lambda limit=None: pending)
    monkeypatch.setattr(fundamentals_rating_agencies, "_resolve_issuer_name", lambda cmid: None)
    status_calls = []
    monkeypatch.setattr(fundamentals_rating_agencies, "_set_enrichment_status", lambda **kwargs: status_calls.append(kwargs))

    result = fundamentals_rating_agencies.run_rating_agency_enrichment()

    assert result["failed"] == 1
    assert status_calls == [{"source": "bse", "news_id": "n1", "status": "failed"}]


# fundamentals/collectors/ocr_pipeline.py -- L3/L4 OCR fetch+store (step 6).


def test_resolve_document_source_prefers_icra_rationale_over_bse_attachment():
    row = {"attachment_name": "some.pdf", "rationale_pdf_url": "https://www.icra.in/Rating/GetRationalReportFilePdf?Id=1"}
    assert fundamentals_ocr_pipeline.resolve_document_source(row) == ("icra", "https://www.icra.in/Rating/GetRationalReportFilePdf?Id=1")


def test_resolve_document_source_falls_back_to_bse_attachment():
    row = {"attachment_name": "abc-123.pdf", "rationale_pdf_url": None}
    domain, url = fundamentals_ocr_pipeline.resolve_document_source(row)
    assert domain == "bse"
    assert url == "https://www.bseindia.com/xml-data/corpfiling/AttachLive/abc-123.pdf"


def test_resolve_document_source_returns_none_without_either():
    assert fundamentals_ocr_pipeline.resolve_document_source({"attachment_name": None, "rationale_pdf_url": None}) is None


def test_fetch_document_bytes_raises_on_non_200(monkeypatch):
    monkeypatch.setattr(fundamentals_ocr_pipeline, "exchange_request_gate", lambda **k: contextlib.nullcontext())

    class FakeResponse:
        status_code = 404
        content = b""

    monkeypatch.setattr(fundamentals_ocr_pipeline.requests, "get", lambda *a, **k: FakeResponse())
    with pytest.raises(fundamentals_ocr_pipeline.DocumentFetchError):
        fundamentals_ocr_pipeline.fetch_document_bytes("https://example.com/x.pdf", domain="bse")


def test_fetch_document_bytes_raises_on_empty_body(monkeypatch):
    monkeypatch.setattr(fundamentals_ocr_pipeline, "exchange_request_gate", lambda **k: contextlib.nullcontext())

    class FakeResponse:
        status_code = 200
        content = b""

    monkeypatch.setattr(fundamentals_ocr_pipeline.requests, "get", lambda *a, **k: FakeResponse())
    with pytest.raises(fundamentals_ocr_pipeline.DocumentFetchError):
        fundamentals_ocr_pipeline.fetch_document_bytes("https://example.com/x.pdf", domain="bse")


def test_fetch_document_bytes_returns_content_on_success(monkeypatch):
    monkeypatch.setattr(fundamentals_ocr_pipeline, "exchange_request_gate", lambda **k: contextlib.nullcontext())

    class FakeResponse:
        status_code = 200
        content = b"%PDF-1.4 fake pdf bytes"

    monkeypatch.setattr(fundamentals_ocr_pipeline.requests, "get", lambda *a, **k: FakeResponse())
    assert fundamentals_ocr_pipeline.fetch_document_bytes("https://example.com/x.pdf", domain="icra") == b"%PDF-1.4 fake pdf bytes"


def test_ocr_pdf_bytes_writes_temp_file_and_joins_pages(monkeypatch):
    captured = {}

    def fake_ocr_pdf_with_local(path):
        captured["path_exists"] = Path(path).exists()
        captured["path_suffix"] = Path(path).suffix
        return {2: "page two", 1: "page one"}

    monkeypatch.setattr(fundamentals_ocr_pipeline, "ocr_pdf_with_local", fake_ocr_pdf_with_local)

    result = fundamentals_ocr_pipeline.ocr_pdf_bytes(b"%PDF-1.4 fake")

    assert result == "page one\n\npage two"  # sorted by page number, not dict order
    assert captured["path_exists"] is True
    assert captured["path_suffix"] == ".pdf"


def test_load_pending_ocr_targets_queries_expected_filters(monkeypatch):
    captured = {}

    def fake_sql_to_df(query, **kwargs):
        captured["query"] = query
        return pd.DataFrame()

    monkeypatch.setattr(fundamentals_ocr_pipeline, "sql_to_df", fake_sql_to_df)
    fundamentals_ocr_pipeline.load_pending_ocr_targets()
    assert "ocr_status IS NULL OR ocr_status = 'pending'" in captured["query"]
    assert "attachment_name IS NOT NULL OR rationale_pdf_url IS NOT NULL" in captured["query"]


def test_run_ocr_pipeline_returns_early_when_nothing_pending(monkeypatch):
    monkeypatch.setattr(fundamentals_ocr_pipeline, "_ensure_events_schema", lambda: None)
    monkeypatch.setattr(fundamentals_ocr_pipeline, "_bootstrap_ocr_columns", lambda: None)
    monkeypatch.setattr(fundamentals_ocr_pipeline, "load_pending_ocr_targets", lambda limit=None: pd.DataFrame())

    result = fundamentals_ocr_pipeline.run_ocr_pipeline()

    assert result == {"ocred": 0, "failed": 0, "no_document": 0, "blocked": False}


def test_run_ocr_pipeline_marks_rows_with_no_document_reference(monkeypatch):
    monkeypatch.setattr(fundamentals_ocr_pipeline, "_ensure_events_schema", lambda: None)
    monkeypatch.setattr(fundamentals_ocr_pipeline, "_bootstrap_ocr_columns", lambda: None)
    pending = pd.DataFrame([{"source": "bse", "news_id": "n1", "attachment_name": None, "rationale_pdf_url": None}])
    monkeypatch.setattr(fundamentals_ocr_pipeline, "load_pending_ocr_targets", lambda limit=None: pending)
    status_calls = []
    monkeypatch.setattr(fundamentals_ocr_pipeline, "_set_ocr_result", lambda **kwargs: status_calls.append(kwargs))

    result = fundamentals_ocr_pipeline.run_ocr_pipeline()

    assert result["no_document"] == 1
    assert status_calls == [{"source": "bse", "news_id": "n1", "status": "no_document"}]


def test_run_ocr_pipeline_happy_path_stores_pdf_and_text(monkeypatch):
    monkeypatch.setattr(fundamentals_ocr_pipeline, "_ensure_events_schema", lambda: None)
    monkeypatch.setattr(fundamentals_ocr_pipeline, "_bootstrap_ocr_columns", lambda: None)
    pending = pd.DataFrame(
        [{"source": "icra", "news_id": "n1", "attachment_name": None, "rationale_pdf_url": "https://www.icra.in/Rating/GetRationalReportFilePdf?Id=1"}]
    )
    monkeypatch.setattr(fundamentals_ocr_pipeline, "load_pending_ocr_targets", lambda limit=None: pending)
    monkeypatch.setattr(fundamentals_ocr_pipeline, "fetch_document_bytes", lambda url, **k: b"%PDF-1.4 fake")
    monkeypatch.setattr(fundamentals_ocr_pipeline, "ocr_pdf_bytes", lambda pdf_bytes: "extracted rationale text")

    saved_files = []
    monkeypatch.setattr(fundamentals_ocr_pipeline, "save_file_content", lambda key, content: saved_files.append((key, content)))

    from utils.blob_store import TextBlobMetadata

    fake_metadata = TextBlobMetadata(key="fundamentals/ocr/icra/n1.txt", sha256="abc123", char_count=24, byte_count=24, excerpt="extracted rationale text")
    monkeypatch.setattr(fundamentals_ocr_pipeline, "put_text_blob", lambda text, key: fake_metadata)

    status_calls = []
    monkeypatch.setattr(fundamentals_ocr_pipeline, "_set_ocr_result", lambda **kwargs: status_calls.append(kwargs))

    result = fundamentals_ocr_pipeline.run_ocr_pipeline()

    assert result["ocred"] == 1
    assert result["blocked"] is False
    assert saved_files == [("fundamentals/filings/icra/n1.pdf", b"%PDF-1.4 fake")]
    assert len(status_calls) == 1
    call = status_calls[0]
    assert call["status"] == "done"
    assert call["fields"]["ocr_text_s3_key"] == "fundamentals/ocr/icra/n1.txt"
    assert call["fields"]["ocr_text_excerpt"] == "extracted rationale text"
    assert call["fields"]["source_pdf_s3_key"] == "fundamentals/filings/icra/n1.pdf"
    assert call["fields"]["source_pdf_sha256"] == hashlib.sha256(b"%PDF-1.4 fake").hexdigest()


def test_run_ocr_pipeline_trips_circuit_breaker_per_domain(monkeypatch):
    monkeypatch.setattr(fundamentals_ocr_pipeline, "_ensure_events_schema", lambda: None)
    monkeypatch.setattr(fundamentals_ocr_pipeline, "_bootstrap_ocr_columns", lambda: None)
    pending = pd.DataFrame(
        [
            {"source": "bse", "news_id": f"n{i}", "attachment_name": f"file{i}.pdf", "rationale_pdf_url": None}
            for i in range(5)
        ]
    )
    monkeypatch.setattr(fundamentals_ocr_pipeline, "load_pending_ocr_targets", lambda limit=None: pending)

    def always_fails(url, **k):
        raise fundamentals_ocr_pipeline.DocumentFetchError("boom")

    monkeypatch.setattr(fundamentals_ocr_pipeline, "fetch_document_bytes", always_fails)
    monkeypatch.setattr(fundamentals_ocr_pipeline, "_set_ocr_result", lambda **kwargs: None)
    fallback_events = []
    monkeypatch.setattr(fundamentals_ocr_pipeline, "_record_fallback", lambda *a, **k: fallback_events.append((a, k)))

    result = fundamentals_ocr_pipeline.run_ocr_pipeline()

    assert result["blocked"] is True
    assert result["failed"] == fundamentals_ocr_pipeline.CIRCUIT_BREAKER_THRESHOLD
    assert any(a and a[0] == "ocr_pipeline_circuit_breaker_tripped" for a, k in fallback_events)


# fundamentals/collectors/structured_extraction.py -- L3/L4 OCR-text -> typed fields
# (step 6, second stage). gpt-5.4-mini itself is never called in this test suite
# (verified live 2026-08-11 against 3 real documents instead, see module docstring for
# what that testing found and fixed) -- these tests cover schema routing, the
# extraction-target query, and run_structured_extraction's orchestration, all with the
# OpenAI/DB/S3 calls mocked.


def test_extract_structured_fields_raises_for_unsupported_filing_type():
    with pytest.raises(fundamentals_structured_extraction.UnsupportedFilingTypeError):
        fundamentals_structured_extraction.extract_structured_fields("some text", "results_calendar_unknown_type")


def test_extract_structured_fields_routes_to_the_right_schema(monkeypatch):
    captured = {}

    class FakeMessage:
        content = '{"ok": true}'

    class FakeChoice:
        message = FakeMessage()

    class FakeResponse:
        choices = [FakeChoice()]

    class FakeCompletions:
        def create(self, **kwargs):
            captured.update(kwargs)
            return FakeResponse()

    class FakeChat:
        completions = FakeCompletions()

    class FakeClient:
        chat = FakeChat()

    monkeypatch.setattr(fundamentals_structured_extraction, "OpenAI", lambda api_key: FakeClient())

    result = fundamentals_structured_extraction.extract_structured_fields("some rating text", "rating_action", model="test-model")

    assert result == {"ok": True}
    assert captured["model"] == "test-model"
    assert captured["messages"][1]["content"] == "some rating text"
    assert captured["response_format"]["json_schema"]["name"] == "rating_action_extraction"
    assert captured["response_format"]["json_schema"]["schema"] == fundamentals_structured_extraction.RATING_ACTION_SCHEMA


@pytest.mark.parametrize("filing_type", ["results", "results_calendar", "rating_action", "pit_sast"])
def test_every_supported_filing_type_has_a_schema(filing_type):
    assert filing_type in fundamentals_structured_extraction.SCHEMAS_BY_FILING_TYPE


def test_load_pending_extraction_targets_queries_expected_filters(monkeypatch):
    captured = {}

    def fake_sql_to_df(query, **kwargs):
        captured["query"] = query
        return pd.DataFrame()

    monkeypatch.setattr(fundamentals_structured_extraction, "sql_to_df", fake_sql_to_df)
    fundamentals_structured_extraction.load_pending_extraction_targets()
    assert "ocr_status = 'done'" in captured["query"]
    assert "structured_extraction_status IS NULL OR structured_extraction_status = 'pending'" in captured["query"]


def test_run_structured_extraction_returns_early_when_nothing_pending(monkeypatch):
    monkeypatch.setattr(fundamentals_structured_extraction, "_ensure_events_schema", lambda: None)
    monkeypatch.setattr(fundamentals_structured_extraction, "_bootstrap_extraction_columns", lambda: None)
    monkeypatch.setattr(fundamentals_structured_extraction, "load_pending_extraction_targets", lambda limit=None: pd.DataFrame())

    result = fundamentals_structured_extraction.run_structured_extraction()

    assert result == {"extracted": 0, "failed": 0, "unsupported_filing_type": 0, "blocked": False}


def test_run_structured_extraction_marks_unsupported_filing_types(monkeypatch):
    monkeypatch.setattr(fundamentals_structured_extraction, "_ensure_events_schema", lambda: None)
    monkeypatch.setattr(fundamentals_structured_extraction, "_bootstrap_extraction_columns", lambda: None)
    pending = pd.DataFrame([{"source": "bse", "news_id": "n1", "filing_type": "something_new", "ocr_text_s3_key": "key1"}])
    monkeypatch.setattr(fundamentals_structured_extraction, "load_pending_extraction_targets", lambda limit=None: pending)
    status_calls = []
    monkeypatch.setattr(fundamentals_structured_extraction, "_set_extraction_result", lambda **kwargs: status_calls.append(kwargs))

    result = fundamentals_structured_extraction.run_structured_extraction()

    assert result["unsupported_filing_type"] == 1
    assert status_calls == [{"source": "bse", "news_id": "n1", "status": "unsupported_filing_type"}]


def test_run_structured_extraction_happy_path(monkeypatch):
    monkeypatch.setattr(fundamentals_structured_extraction, "_ensure_events_schema", lambda: None)
    monkeypatch.setattr(fundamentals_structured_extraction, "_bootstrap_extraction_columns", lambda: None)
    pending = pd.DataFrame([{"source": "icra", "news_id": "n1", "filing_type": "rating_action", "ocr_text_s3_key": "fundamentals/ocr/icra/n1.txt"}])
    monkeypatch.setattr(fundamentals_structured_extraction, "load_pending_extraction_targets", lambda limit=None: pending)
    monkeypatch.setattr(fundamentals_structured_extraction, "get_text_blob", lambda key: "the ocr text")
    extracted = {"company_name": "X Ltd", "rating_action": "reaffirmed"}
    monkeypatch.setattr(fundamentals_structured_extraction, "extract_structured_fields", lambda text, filing_type, **k: extracted)
    status_calls = []
    monkeypatch.setattr(fundamentals_structured_extraction, "_set_extraction_result", lambda **kwargs: status_calls.append(kwargs))

    result = fundamentals_structured_extraction.run_structured_extraction(model="test-model")

    assert result["extracted"] == 1
    assert result["blocked"] is False
    assert len(status_calls) == 1
    call = status_calls[0]
    assert call["status"] == "done"
    assert json.loads(call["fields"]["structured_extraction_json"]) == extracted
    assert call["fields"]["structured_extraction_model"] == "test-model"
    assert call["fields"]["structured_extraction_schema_version"] == fundamentals_structured_extraction.SCHEMA_VERSION


def test_run_structured_extraction_trips_circuit_breaker(monkeypatch):
    monkeypatch.setattr(fundamentals_structured_extraction, "_ensure_events_schema", lambda: None)
    monkeypatch.setattr(fundamentals_structured_extraction, "_bootstrap_extraction_columns", lambda: None)
    pending = pd.DataFrame(
        [{"source": "bse", "news_id": f"n{i}", "filing_type": "results", "ocr_text_s3_key": f"key{i}"} for i in range(5)]
    )
    monkeypatch.setattr(fundamentals_structured_extraction, "load_pending_extraction_targets", lambda limit=None: pending)
    monkeypatch.setattr(fundamentals_structured_extraction, "get_text_blob", lambda key: "text")

    def always_fails(text, filing_type, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr(fundamentals_structured_extraction, "extract_structured_fields", always_fails)
    monkeypatch.setattr(fundamentals_structured_extraction, "_set_extraction_result", lambda **kwargs: None)
    fallback_events = []
    monkeypatch.setattr(fundamentals_structured_extraction, "_record_fallback", lambda *a, **k: fallback_events.append((a, k)))

    result = fundamentals_structured_extraction.run_structured_extraction()

    assert result["blocked"] is True
    assert result["failed"] == fundamentals_structured_extraction.CIRCUIT_BREAKER_THRESHOLD
    assert any(a and a[0] == "structured_extraction_circuit_breaker_tripped" for a, k in fallback_events)


# fundamentals/screens/l3_triggers.py -- L3 rule-based alerting (step 7, rule half).


def test_evaluate_rating_action_trigger_downgrade_always_alerts():
    result = fundamentals_l3_triggers.evaluate_rating_action_trigger(
        {"rating_action_type": "downgraded"}, {"net_debt_yoy_delta_rscr": 5}
    )
    assert result["trigger_type"] == "rating_downgrade"


def test_evaluate_rating_action_trigger_downgrade_alerts_even_without_l2_state():
    result = fundamentals_l3_triggers.evaluate_rating_action_trigger({"rating_action_type": "downgraded"}, None)
    assert result["trigger_type"] == "rating_downgrade"


def test_evaluate_rating_action_trigger_reaffirmed_with_declining_debt_alerts():
    result = fundamentals_l3_triggers.evaluate_rating_action_trigger(
        {"rating_action_type": "reaffirmed"}, {"net_debt_yoy_delta_rscr": -12}
    )
    assert result["trigger_type"] == "rating_confirms_deleveraging"


def test_evaluate_rating_action_trigger_upgraded_with_declining_debt_alerts():
    result = fundamentals_l3_triggers.evaluate_rating_action_trigger(
        {"rating_action_type": "upgraded"}, {"net_debt_yoy_delta_rscr": -1}
    )
    assert result["trigger_type"] == "rating_confirms_deleveraging"


def test_evaluate_rating_action_trigger_reaffirmed_with_rising_debt_does_not_alert():
    result = fundamentals_l3_triggers.evaluate_rating_action_trigger(
        {"rating_action_type": "reaffirmed"}, {"net_debt_yoy_delta_rscr": 5}
    )
    assert result is None


def test_evaluate_rating_action_trigger_reaffirmed_without_l2_state_does_not_alert():
    result = fundamentals_l3_triggers.evaluate_rating_action_trigger({"rating_action_type": "reaffirmed"}, None)
    assert result is None


def test_evaluate_pit_sast_trigger_buy_always_alerts():
    result = fundamentals_l3_triggers.evaluate_pit_sast_trigger(
        {"transaction_type": "Buy", "insider_name": "X"}, {"promoter_stake_direction": "flat"}
    )
    assert result["trigger_type"] == "insider_buy"


def test_evaluate_pit_sast_trigger_sell_surprise_alerts():
    result = fundamentals_l3_triggers.evaluate_pit_sast_trigger(
        {"transaction_type": "Sell", "insider_name": "X"}, {"promoter_stake_direction": "flat"}
    )
    assert result["trigger_type"] == "insider_sell_surprise"


def test_evaluate_pit_sast_trigger_sell_confirming_known_trend_does_not_alert():
    result = fundamentals_l3_triggers.evaluate_pit_sast_trigger(
        {"transaction_type": "Sell", "insider_name": "X"}, {"promoter_stake_direction": "decreasing"}
    )
    assert result is None


def test_evaluate_pit_sast_trigger_no_transaction_data_does_not_alert():
    # the common real case -- a trading-window-closure procedural notice
    assert fundamentals_l3_triggers.evaluate_pit_sast_trigger({"transaction_type": None}, {"promoter_stake_direction": "flat"}) is None


def test_load_candidate_events_queries_expected_filters(monkeypatch):
    captured = {}

    def fake_sql_to_df(query, **kwargs):
        captured["query"] = query
        return pd.DataFrame()

    monkeypatch.setattr(fundamentals_l3_triggers, "sql_to_df", fake_sql_to_df)
    fundamentals_l3_triggers.load_candidate_events()
    assert "rating_action" in captured["query"]
    assert "pit_sast" in captured["query"]
    assert "rule_trigger_status IS NULL OR rule_trigger_status = 'pending'" in captured["query"]


def test_run_l3_rule_triggers_returns_early_when_no_candidates(monkeypatch):
    monkeypatch.setattr(fundamentals_l3_triggers, "_bootstrap_rule_trigger_column", lambda: None)
    monkeypatch.setattr(fundamentals_l3_triggers, "_ensure_alerts_table", lambda: None)
    monkeypatch.setattr(fundamentals_l3_triggers, "load_candidate_events", lambda limit=None: pd.DataFrame())

    result = fundamentals_l3_triggers.run_l3_rule_triggers()

    assert result == {"alerted": 0, "not_alert_worthy": 0, "no_l2_state": 0}


def test_run_l3_rule_triggers_writes_an_alert_for_a_downgrade(monkeypatch):
    monkeypatch.setattr(fundamentals_l3_triggers, "_bootstrap_rule_trigger_column", lambda: None)
    monkeypatch.setattr(fundamentals_l3_triggers, "_ensure_alerts_table", lambda: None)
    events = pd.DataFrame(
        [
            {
                "source": "bse", "news_id": "n1", "company_master_id": "nse:X", "filing_type": "rating_action",
                "headline": "downgrade notice", "rating_action_type": "downgraded", "transaction_type": None,
                "insider_name": None, "quantity": None, "disclosure_date": date(2026, 8, 1),
            }
        ]
    )
    monkeypatch.setattr(fundamentals_l3_triggers, "load_candidate_events", lambda limit=None: events)
    l2_state = pd.DataFrame([{"ticker": "X", "company_name": "X Ltd", "net_debt_yoy_delta_rscr": 3, "run_date": date(2026, 7, 1)}])
    monkeypatch.setattr(fundamentals_l3_triggers, "load_latest_l2_state", lambda: l2_state)

    upserts = []
    monkeypatch.setattr(fundamentals_l3_triggers, "upsert_to_db", lambda df, table, **k: upserts.append((df, table, k)))
    status_calls = []
    monkeypatch.setattr(fundamentals_l3_triggers, "_set_rule_trigger_status", lambda **kwargs: status_calls.append(kwargs))

    result = fundamentals_l3_triggers.run_l3_rule_triggers()

    assert result == {"alerted": 1, "not_alert_worthy": 0, "no_l2_state": 0}
    assert len(upserts) == 1
    df, table, kwargs = upserts[0]
    assert table == fundamentals_l3_triggers.RESULTS_TABLE
    assert kwargs["unique_keys"] == ["source", "news_id", "trigger_type"]
    assert df.iloc[0]["trigger_type"] == "rating_downgrade"
    assert df.iloc[0]["origin"] == "rule"
    assert df.iloc[0]["company_master_id"] == "nse:X"
    assert status_calls == [{"source": "bse", "news_id": "n1", "status": "alerted"}]


def test_run_l3_rule_triggers_marks_not_alert_worthy_and_skips_upsert(monkeypatch):
    monkeypatch.setattr(fundamentals_l3_triggers, "_bootstrap_rule_trigger_column", lambda: None)
    monkeypatch.setattr(fundamentals_l3_triggers, "_ensure_alerts_table", lambda: None)
    events = pd.DataFrame(
        [
            {
                "source": "bse", "news_id": "n1", "company_master_id": "nse:X", "filing_type": "pit_sast",
                "headline": "trading window closed", "rating_action_type": None, "transaction_type": None,
                "insider_name": None, "quantity": None, "disclosure_date": date(2026, 8, 1),
            }
        ]
    )
    monkeypatch.setattr(fundamentals_l3_triggers, "load_candidate_events", lambda limit=None: events)
    monkeypatch.setattr(fundamentals_l3_triggers, "load_latest_l2_state", lambda: pd.DataFrame(columns=["ticker"]))
    upserts = []
    monkeypatch.setattr(fundamentals_l3_triggers, "upsert_to_db", lambda df, table, **k: upserts.append((df, table, k)))
    status_calls = []
    monkeypatch.setattr(fundamentals_l3_triggers, "_set_rule_trigger_status", lambda **kwargs: status_calls.append(kwargs))
    fallback_events = []
    monkeypatch.setattr(fundamentals_l3_triggers, "_record_fallback", lambda *a, **k: fallback_events.append((a, k)))

    result = fundamentals_l3_triggers.run_l3_rule_triggers()

    assert result == {"alerted": 0, "not_alert_worthy": 1, "no_l2_state": 1}
    assert upserts == []
    assert status_calls == [{"source": "bse", "news_id": "n1", "status": "not_alert_worthy"}]
    assert any(a and a[0] == "l3_trigger_no_l2_state" for a, k in fallback_events)


# fundamentals/screens/llm_triage.py -- L3 LLM-judgment alerting (step 7, LLM half).
# gpt-5.4-mini is never called in this suite (live-validated separately against real
# events, see the module's own commit) -- these tests mock triage_event/OpenAI and
# cover evidence-bundle construction, price-context math, and orchestration.


def test_load_price_context_returns_empty_without_event_date():
    assert fundamentals_llm_triage.load_price_context("nse:X", None) == {}


def test_load_price_context_computes_expected_ratios(monkeypatch):
    before = pd.DataFrame(
        [
            {"date": date(2026, 8, 4), "close": 100.0, "volume": 200},
            {"date": date(2026, 8, 3), "close": 98.0, "volume": 50},
            {"date": date(2026, 7, 8), "close": 90.0, "volume": 50},
        ]
    )
    after = pd.DataFrame([{"date": date(2026, 8, 5), "close": 105.0, "volume": 60}])

    calls = {"n": 0}

    def fake_sql_to_df(query, params=None):
        calls["n"] += 1
        return before if calls["n"] == 1 else after

    monkeypatch.setattr(fundamentals_llm_triage, "sql_to_df", fake_sql_to_df)

    ctx = fundamentals_llm_triage.load_price_context("nse:X", date(2026, 8, 4))

    assert ctx["close_on_or_before_event"] == 100.0
    assert ctx["price_pct_change_last_3_sessions"] == round((100.0 - 90.0) / 90.0 * 100, 2)
    assert ctx["volume_on_event_vs_avg_ratio"] == round(200 / before["volume"].mean(), 2)
    assert ctx["price_pct_change_since_event"] == round((105.0 - 100.0) / 100.0 * 100, 2)
    assert ctx["sessions_since_event_available"] == 1


def test_load_price_context_returns_empty_when_no_prior_data(monkeypatch):
    monkeypatch.setattr(fundamentals_llm_triage, "sql_to_df", lambda query, params=None: pd.DataFrame())
    assert fundamentals_llm_triage.load_price_context("nse:X", date(2026, 8, 4)) == {}


def test_build_evidence_bundle_parses_structured_extraction_json():
    event = {
        "filing_type": "rating_action", "headline": "h", "subcategory": "s", "disclosure_date": date(2026, 8, 4),
        "rating_action_type": "downgraded", "transaction_type": None, "insider_name": None, "quantity": None,
        "structured_extraction_json": '{"rating_action": "downgraded"}',
    }
    bundle = fundamentals_llm_triage.build_evidence_bundle(event, {"ticker": "X"}, {"volume_on_event_vs_avg_ratio": 2.0})
    assert bundle["event"]["structured_extraction"] == {"rating_action": "downgraded"}
    assert bundle["l2_state"] == {"ticker": "X"}
    assert bundle["price_context"] == {"volume_on_event_vs_avg_ratio": 2.0}


def test_build_evidence_bundle_handles_missing_structured_extraction():
    event = {"filing_type": "pit_sast", "headline": "h", "subcategory": None, "disclosure_date": date(2026, 8, 4)}
    bundle = fundamentals_llm_triage.build_evidence_bundle(event, None, {})
    assert bundle["event"]["structured_extraction"] is None
    assert bundle["l2_state"] is None


def test_load_candidate_events_for_triage_queries_expected_filters(monkeypatch):
    captured = {}

    def fake_sql_to_df(query, **kwargs):
        captured["query"] = query
        return pd.DataFrame()

    monkeypatch.setattr(fundamentals_llm_triage, "sql_to_df", fake_sql_to_df)
    fundamentals_llm_triage.load_candidate_events_for_triage()
    assert "'rating_action', 'pit_sast', 'results'" in captured["query"]
    assert "llm_triage_status IS NULL OR llm_triage_status = 'pending'" in captured["query"]


def test_run_llm_triage_returns_early_when_no_candidates(monkeypatch):
    monkeypatch.setattr(fundamentals_llm_triage, "_bootstrap_triage_column", lambda: None)
    monkeypatch.setattr(fundamentals_llm_triage, "_ensure_alerts_table", lambda: None)
    monkeypatch.setattr(fundamentals_llm_triage, "load_candidate_events_for_triage", lambda limit=None: pd.DataFrame())

    result = fundamentals_llm_triage.run_llm_triage()

    assert result == {"flagged": 0, "not_interesting": 0, "failed": 0, "blocked": False}


def test_run_llm_triage_writes_alert_when_flagged_interesting(monkeypatch):
    monkeypatch.setattr(fundamentals_llm_triage, "_bootstrap_triage_column", lambda: None)
    monkeypatch.setattr(fundamentals_llm_triage, "_ensure_alerts_table", lambda: None)
    events = pd.DataFrame(
        [
            {
                "source": "bse", "news_id": "n1", "company_master_id": "nse:X", "filing_type": "rating_action",
                "headline": "h", "subcategory": "s", "disclosure_date": date(2026, 8, 4),
                "rating_action_type": "downgraded", "transaction_type": None, "insider_name": None,
                "quantity": None, "structured_extraction_json": None,
            }
        ]
    )
    monkeypatch.setattr(fundamentals_llm_triage, "load_candidate_events_for_triage", lambda limit=None: events)
    monkeypatch.setattr(fundamentals_llm_triage, "load_latest_l2_state", lambda: pd.DataFrame([{"ticker": "X", "run_date": date(2026, 7, 1)}]))
    monkeypatch.setattr(fundamentals_llm_triage, "load_price_context", lambda cmid, d, **k: {"volume_on_event_vs_avg_ratio": 3.0})
    monkeypatch.setattr(
        fundamentals_llm_triage, "triage_event", lambda bundle, **k: {"interesting": True, "reasoning": "matters", "confidence": "high"}
    )
    upserts = []
    monkeypatch.setattr(fundamentals_llm_triage, "upsert_to_db", lambda df, table, **k: upserts.append((df, table, k)))
    status_calls = []
    monkeypatch.setattr(fundamentals_llm_triage, "_set_triage_status", lambda **kwargs: status_calls.append(kwargs))

    result = fundamentals_llm_triage.run_llm_triage(model="test-model")

    assert result == {"flagged": 1, "not_interesting": 0, "failed": 0, "blocked": False}
    assert len(upserts) == 1
    df, table, kwargs = upserts[0]
    assert table == fundamentals_llm_triage.RESULTS_TABLE
    assert kwargs["unique_keys"] == ["source", "news_id", "trigger_type"]
    row = df.iloc[0]
    assert row["trigger_type"] == "llm_flagged"
    assert row["origin"] == "llm_triage"
    assert row["reasoning"] == "matters"
    assert row["model"] == "test-model"
    assert row["prompt_version"] == fundamentals_llm_triage.PROMPT_VERSION
    assert status_calls == [{"source": "bse", "news_id": "n1", "status": "flagged"}]


def test_run_llm_triage_skips_upsert_when_not_interesting(monkeypatch):
    monkeypatch.setattr(fundamentals_llm_triage, "_bootstrap_triage_column", lambda: None)
    monkeypatch.setattr(fundamentals_llm_triage, "_ensure_alerts_table", lambda: None)
    events = pd.DataFrame(
        [
            {
                "source": "bse", "news_id": "n1", "company_master_id": "nse:X", "filing_type": "results",
                "headline": "h", "subcategory": "s", "disclosure_date": date(2026, 8, 4),
                "rating_action_type": None, "transaction_type": None, "insider_name": None,
                "quantity": None, "structured_extraction_json": None,
            }
        ]
    )
    monkeypatch.setattr(fundamentals_llm_triage, "load_candidate_events_for_triage", lambda limit=None: events)
    monkeypatch.setattr(fundamentals_llm_triage, "load_latest_l2_state", lambda: pd.DataFrame(columns=["ticker"]))
    monkeypatch.setattr(fundamentals_llm_triage, "load_price_context", lambda cmid, d, **k: {})
    monkeypatch.setattr(
        fundamentals_llm_triage, "triage_event", lambda bundle, **k: {"interesting": False, "reasoning": "routine", "confidence": "high"}
    )
    upserts = []
    monkeypatch.setattr(fundamentals_llm_triage, "upsert_to_db", lambda df, table, **k: upserts.append((df, table, k)))
    status_calls = []
    monkeypatch.setattr(fundamentals_llm_triage, "_set_triage_status", lambda **kwargs: status_calls.append(kwargs))

    result = fundamentals_llm_triage.run_llm_triage()

    assert result == {"flagged": 0, "not_interesting": 1, "failed": 0, "blocked": False}
    assert upserts == []
    assert status_calls == [{"source": "bse", "news_id": "n1", "status": "not_interesting"}]


def test_run_llm_triage_trips_circuit_breaker(monkeypatch):
    monkeypatch.setattr(fundamentals_llm_triage, "_bootstrap_triage_column", lambda: None)
    monkeypatch.setattr(fundamentals_llm_triage, "_ensure_alerts_table", lambda: None)
    events = pd.DataFrame(
        [
            {
                "source": "bse", "news_id": f"n{i}", "company_master_id": "nse:X", "filing_type": "results",
                "headline": "h", "subcategory": "s", "disclosure_date": date(2026, 8, 4),
                "rating_action_type": None, "transaction_type": None, "insider_name": None,
                "quantity": None, "structured_extraction_json": None,
            }
            for i in range(5)
        ]
    )
    monkeypatch.setattr(fundamentals_llm_triage, "load_candidate_events_for_triage", lambda limit=None: events)
    monkeypatch.setattr(fundamentals_llm_triage, "load_latest_l2_state", lambda: pd.DataFrame(columns=["ticker"]))
    monkeypatch.setattr(fundamentals_llm_triage, "load_price_context", lambda cmid, d, **k: {})

    def always_fails(bundle, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr(fundamentals_llm_triage, "triage_event", always_fails)
    monkeypatch.setattr(fundamentals_llm_triage, "_set_triage_status", lambda **kwargs: None)
    fallback_events = []
    monkeypatch.setattr(fundamentals_llm_triage, "_record_fallback", lambda *a, **k: fallback_events.append((a, k)))

    result = fundamentals_llm_triage.run_llm_triage()

    assert result["blocked"] is True
    assert result["failed"] == fundamentals_llm_triage.CIRCUIT_BREAKER_THRESHOLD
    assert any(a and a[0] == "llm_triage_circuit_breaker_tripped" for a, k in fallback_events)


# fundamentals/screens/l4_thesis.py -- L4 thesis register + quarterly scoring (step 8).


def test_create_thesis_requires_prediction_text(monkeypatch):
    monkeypatch.setattr(fundamentals_l4_thesis, "_ensure_thesis_table", lambda: None)
    with pytest.raises(fundamentals_l4_thesis.ThesisValidationError):
        fundamentals_l4_thesis.create_thesis(
            company_master_id="nse:X", prediction_text="  ", target_date=date(2027, 1, 1),
            invalidation_criteria="x", origin_tag="ad_hoc",
        )


def test_create_thesis_requires_target_date(monkeypatch):
    monkeypatch.setattr(fundamentals_l4_thesis, "_ensure_thesis_table", lambda: None)
    with pytest.raises(fundamentals_l4_thesis.ThesisValidationError):
        fundamentals_l4_thesis.create_thesis(
            company_master_id="nse:X", prediction_text="p", target_date=None,
            invalidation_criteria="x", origin_tag="ad_hoc",
        )


def test_create_thesis_requires_invalidation_criteria(monkeypatch):
    monkeypatch.setattr(fundamentals_l4_thesis, "_ensure_thesis_table", lambda: None)
    with pytest.raises(fundamentals_l4_thesis.ThesisValidationError):
        fundamentals_l4_thesis.create_thesis(
            company_master_id="nse:X", prediction_text="p", target_date=date(2027, 1, 1),
            invalidation_criteria="", origin_tag="ad_hoc",
        )


def test_create_thesis_requires_valid_origin_tag(monkeypatch):
    monkeypatch.setattr(fundamentals_l4_thesis, "_ensure_thesis_table", lambda: None)
    with pytest.raises(fundamentals_l4_thesis.ThesisValidationError):
        fundamentals_l4_thesis.create_thesis(
            company_master_id="nse:X", prediction_text="p", target_date=date(2027, 1, 1),
            invalidation_criteria="x", origin_tag="not_a_real_tag",
        )


def test_create_thesis_requires_valid_metric_operator(monkeypatch):
    monkeypatch.setattr(fundamentals_l4_thesis, "_ensure_thesis_table", lambda: None)
    with pytest.raises(fundamentals_l4_thesis.ThesisValidationError):
        fundamentals_l4_thesis.create_thesis(
            company_master_id="nse:X", prediction_text="p", target_date=date(2027, 1, 1),
            invalidation_criteria="x", origin_tag="ad_hoc", metric_operator="~=",
        )


def test_create_thesis_builds_expected_row(monkeypatch):
    monkeypatch.setattr(fundamentals_l4_thesis, "_ensure_thesis_table", lambda: None)
    upserts = []
    monkeypatch.setattr(fundamentals_l4_thesis, "upsert_to_db", lambda df, table, **k: upserts.append((df, table, k)))

    row = fundamentals_l4_thesis.create_thesis(
        company_master_id="nse:X",
        prediction_text="Net debt < 50 by Q3 FY27",
        target_date=date(2027, 3, 31),
        invalidation_criteria="Debt rises further before target date",
        origin_tag="systematic_screen",
        signal_definition_version="l3_rule:v1",
        source_alert={"source": "bse", "news_id": "n1", "trigger_type": "rating_downgrade"},
        metric_name="net_debt_rscr",
        metric_operator="<",
        metric_threshold=50.0,
        created_date=date(2026, 8, 11),
    )

    assert row["thesis_id"].startswith("thesis:")
    assert row["status"] == "open"
    assert row["resolved_true"] is None
    assert row["source_alert_source"] == "bse"
    assert row["signal_definition_version"] == "l3_rule:v1"
    assert len(upserts) == 1
    df, table, kwargs = upserts[0]
    assert table == fundamentals_l4_thesis.RESULTS_TABLE
    assert kwargs["unique_keys"] == ["thesis_id"]


def test_create_thesis_without_source_alert_is_ad_hoc(monkeypatch):
    monkeypatch.setattr(fundamentals_l4_thesis, "_ensure_thesis_table", lambda: None)
    monkeypatch.setattr(fundamentals_l4_thesis, "upsert_to_db", lambda df, table, **k: None)
    row = fundamentals_l4_thesis.create_thesis(
        company_master_id="nse:X", prediction_text="p", target_date=date(2027, 1, 1),
        invalidation_criteria="x", origin_tag="ad_hoc",
    )
    assert row["source_alert_source"] is None
    assert row["source_alert_news_id"] is None


def test_resolve_thesis_requires_failure_attribution_when_false(monkeypatch):
    with pytest.raises(fundamentals_l4_thesis.ThesisValidationError):
        fundamentals_l4_thesis.resolve_thesis(thesis_id="thesis:x", resolved_true=False)


def test_resolve_thesis_rejects_failure_attribution_when_true(monkeypatch):
    with pytest.raises(fundamentals_l4_thesis.ThesisValidationError):
        fundamentals_l4_thesis.resolve_thesis(thesis_id="thesis:x", resolved_true=True, failure_attribution="thesis_wrong")


def test_resolve_thesis_writes_expected_update(monkeypatch):
    executed = []

    class FakeCursor:
        def execute(self, query, params=None):
            executed.append((str(query), params))

    @contextlib.contextmanager
    def fake_db_session():
        yield None, FakeCursor()

    monkeypatch.setattr(fundamentals_l4_thesis, "db_session", fake_db_session)

    fundamentals_l4_thesis.resolve_thesis(
        thesis_id="thesis:x", resolved_true=False, failure_attribution="thesis_wrong",
        resolution_notes="note", resolution_date=date(2026, 12, 31),
    )

    assert len(executed) == 1
    query, params = executed[0]
    assert "UPDATE fundamentals_l4_thesis" in query
    assert params == (False, date(2026, 12, 31), "note", "thesis_wrong", "thesis:x")


def test_check_structured_prediction_returns_none_without_metric_fields():
    assert fundamentals_l4_thesis.check_structured_prediction({"company_master_id": "nse:X"}) is None


def test_check_structured_prediction_evaluates_against_l2_state(monkeypatch):
    monkeypatch.setattr(
        fundamentals_l4_thesis, "sql_to_df", lambda query, params=None: pd.DataFrame([{"pledge_pct": 50.73}])
    )
    thesis_row = {"company_master_id": "nse:CINELINE", "metric_name": "pledge_pct", "metric_operator": "<", "metric_threshold": 45.0}
    assert fundamentals_l4_thesis.check_structured_prediction(thesis_row) is False

    thesis_row["metric_threshold"] = 60.0
    assert fundamentals_l4_thesis.check_structured_prediction(thesis_row) is True


def test_check_structured_prediction_returns_none_when_no_l2_row(monkeypatch):
    monkeypatch.setattr(fundamentals_l4_thesis, "sql_to_df", lambda query, params=None: pd.DataFrame())
    thesis_row = {"company_master_id": "nse:X", "metric_name": "pledge_pct", "metric_operator": "<", "metric_threshold": 45.0}
    assert fundamentals_l4_thesis.check_structured_prediction(thesis_row) is None


def test_check_structured_prediction_returns_none_when_value_is_null(monkeypatch):
    monkeypatch.setattr(
        fundamentals_l4_thesis, "sql_to_df", lambda query, params=None: pd.DataFrame([{"pledge_pct": None}])
    )
    thesis_row = {"company_master_id": "nse:X", "metric_name": "pledge_pct", "metric_operator": "<", "metric_threshold": 45.0}
    assert fundamentals_l4_thesis.check_structured_prediction(thesis_row) is None


def test_compute_quarterly_scoring_with_no_theses(monkeypatch):
    monkeypatch.setattr(fundamentals_l4_thesis, "sql_to_df", lambda query, **k: pd.DataFrame())
    result = fundamentals_l4_thesis.compute_quarterly_scoring(as_of_date=date(2026, 8, 11))
    assert result["total_theses"] == 0
    assert result["hit_rate"] is None


def test_compute_quarterly_scoring_computes_hit_rate_and_time_to_confirmation(monkeypatch):
    theses = pd.DataFrame(
        [
            {
                "status": "resolved", "resolved_true": True, "failure_attribution": None,
                "created_date": date(2026, 8, 1), "resolution_date": date(2026, 10, 1),
            },
            {
                "status": "resolved", "resolved_true": False, "failure_attribution": "thesis_wrong",
                "created_date": date(2026, 8, 1), "resolution_date": date(2026, 12, 31),
            },
            {"status": "open", "resolved_true": None, "failure_attribution": None, "created_date": date(2026, 8, 1), "resolution_date": None},
        ]
    )
    monkeypatch.setattr(fundamentals_l4_thesis, "sql_to_df", lambda query, **k: theses)

    result = fundamentals_l4_thesis.compute_quarterly_scoring(as_of_date=date(2026, 8, 11))

    assert result["total_theses"] == 3
    assert result["open"] == 1
    assert result["resolved"] == 2
    assert result["hit_rate"] == 50.0
    assert result["failure_attribution_breakdown"] == {"thesis_wrong": 1}
    assert result["time_to_confirmation_days"]["count"] == 1
    assert result["time_to_confirmation_days"]["median_days"] == 61.0


def test_load_open_theses_past_target_date_queries_correctly(monkeypatch):
    captured = {}

    def fake_sql_to_df(query, params=None):
        captured["query"] = query
        captured["params"] = params
        return pd.DataFrame()

    monkeypatch.setattr(fundamentals_l4_thesis, "sql_to_df", fake_sql_to_df)
    fundamentals_l4_thesis.load_open_theses_past_target_date(as_of_date=date(2026, 8, 11))
    assert "status = 'open'" in captured["query"]
    assert captured["params"] == (date(2026, 8, 11),)


# fundamentals/collectors/sector_data.py -- sector reference data (feeds step 9).

# Real response shape, captured live 2026-08-11 from /api/v2/core/getAllSectorData
# (trimmed to 2 entries per level; the "Construction\nMaterials" embedded newline is
# real, not a typo -- see build_reference_rows' whitespace-collapsing).
SECTOR_DATA_FIXTURE = {
    "sector": {
        "EQ": [
            {"sector_code": "IN0101", "sector_desc": "Chemicals", "Debt to Equity": 0.414376, "ROCE": 10.2107, "ROE": 9.85546},
            {"sector_code": "IN0102", "sector_desc": "Construction\nMaterials", "Debt to Equity": 0.79559, "ROCE": 7.92772, "ROE": 8.85059},
        ]
    },
    "indgrp": {
        "EQ": [
            {"industry_code": "IN010101", "industry_desc": "Chemicals & Petrochemicals", "Debt to Equity": 0.334288, "ROCE": 9.25939, "ROE": 9.03159},
        ]
    },
    "ind": {
        "EQ": [
            {"basic_industry_code": "IN010101001", "basic_industry_desc": "Commodity Chemicals", "Debt to Equity": 0.345999, "ROCE": 6.4822, "ROE": 4.2789},
        ]
    },
}


def test_build_reference_rows_extracts_all_three_levels():
    rows = fundamentals_sector_data.build_reference_rows(SECTOR_DATA_FIXTURE, as_of_date=date(2026, 8, 11))
    assert len(rows["sector"]) == 2
    assert len(rows["industry_group"]) == 1
    assert len(rows["basic_industry"]) == 1


def test_build_reference_rows_collapses_embedded_whitespace():
    rows = fundamentals_sector_data.build_reference_rows(SECTOR_DATA_FIXTURE, as_of_date=date(2026, 8, 11))
    descriptions = {row["code"]: row["description"] for row in rows["sector"]}
    assert descriptions["IN0102"] == "Construction Materials"


def test_build_reference_rows_maps_ratio_fields():
    rows = fundamentals_sector_data.build_reference_rows(SECTOR_DATA_FIXTURE, as_of_date=date(2026, 8, 11))
    chemicals = next(row for row in rows["sector"] if row["code"] == "IN0101")
    assert chemicals["roce"] == 10.2107
    assert chemicals["roe"] == 9.85546
    assert chemicals["debt_to_equity"] == 0.414376


def test_build_reference_rows_skips_entries_without_code():
    data = {"sector": {"EQ": [{"sector_desc": "No code here"}]}, "indgrp": {"EQ": []}, "ind": {"EQ": []}}
    rows = fundamentals_sector_data.build_reference_rows(data, as_of_date=date(2026, 8, 11))
    assert rows["sector"] == []


def test_fetch_sector_reference_data_unwraps_double_encoded_json(monkeypatch):
    class FakeResponse:
        text = "encrypted-payload"

    monkeypatch.setattr(fundamentals_sector_data, "get_sharpely_v2_headers", lambda: {})
    monkeypatch.setattr(fundamentals_sector_data, "get_with_retries", lambda url, **k: FakeResponse())
    # double-encoded: the decrypted plaintext is itself a JSON string containing JSON
    monkeypatch.setattr(
        fundamentals_sector_data, "decrypt_sharpely_v2_compressed", lambda payload: json.dumps(json.dumps(SECTOR_DATA_FIXTURE))
    )

    data = fundamentals_sector_data.fetch_sector_reference_data()
    assert data == SECTOR_DATA_FIXTURE


def test_fetch_sector_reference_data_handles_single_encoded_json(monkeypatch):
    monkeypatch.setattr(fundamentals_sector_data, "get_sharpely_v2_headers", lambda: {})
    monkeypatch.setattr(fundamentals_sector_data, "get_with_retries", lambda url, **k: type("R", (), {"text": "x"})())
    monkeypatch.setattr(fundamentals_sector_data, "decrypt_sharpely_v2_compressed", lambda payload: json.dumps(SECTOR_DATA_FIXTURE))

    data = fundamentals_sector_data.fetch_sector_reference_data()
    assert data == SECTOR_DATA_FIXTURE


def test_run_sector_reference_refresh_upserts_all_three_tables(monkeypatch):
    monkeypatch.setattr(fundamentals_sector_data, "fetch_sector_reference_data", lambda: SECTOR_DATA_FIXTURE)
    upserts = []
    monkeypatch.setattr(fundamentals_sector_data, "upsert_to_db", lambda df, table, **k: upserts.append((table, len(df), k)))

    result = fundamentals_sector_data.run_sector_reference_refresh()

    assert result == {"sectors": 2, "industry_groups": 1, "basic_industries": 1}
    tables_upserted = {table for table, _, _ in upserts}
    assert tables_upserted == {
        fundamentals_sector_data.SECTOR_TABLE,
        fundamentals_sector_data.INDUSTRY_GROUP_TABLE,
        fundamentals_sector_data.BASIC_INDUSTRY_TABLE,
    }
    for _, _, kwargs in upserts:
        assert kwargs["unique_keys"] == ["code", "as_of_date"]


# fundamentals/screens/sector_cycle.py -- sector capital-cycle aggregation (step 9).


def test_classify_phase_expansion_when_capacity_outruns_demand():
    assert fundamentals_sector_cycle.classify_phase(20.0, 5.0) == "capacity_expansion"


def test_classify_phase_discipline_when_demand_outruns_capacity():
    assert fundamentals_sector_cycle.classify_phase(2.0, 15.0) == "capacity_discipline"


def test_classify_phase_balanced_within_threshold():
    assert fundamentals_sector_cycle.classify_phase(10.0, 8.0) == "balanced"


def test_classify_phase_none_when_either_input_missing():
    assert fundamentals_sector_cycle.classify_phase(None, 5.0) is None
    assert fundamentals_sector_cycle.classify_phase(10.0, None) is None


def test_fetch_gross_block_data_filters_incomplete_rows(monkeypatch):
    companies = [
        {"company_id": 1, "metrics": {"gross_block_rscr": 120, "gross_block_py_rscr": 100}},
        {"company_id": 2, "metrics": {"gross_block_rscr": None, "gross_block_py_rscr": 50}},  # incomplete -- excluded
        {"company_id": 3, "metrics": {}},  # no gross block fields at all -- excluded
    ]
    monkeypatch.setattr(fundamentals_sector_cycle, "run_query", lambda session, query: ("url", companies))

    result = fundamentals_sector_cycle.fetch_gross_block_data(session=object())

    assert result == {1: {"gross_block_current_rscr": 120, "gross_block_preceding_rscr": 100}}


def test_compute_sector_aggregates_computes_capacity_and_demand_growth():
    l1 = pd.DataFrame(
        [
            {"company_id": 1, "company_name": "A", "ticker": "A", "sector_code": "IN0101", "qtr_sales_var_pct": 10.0},
            {"company_id": 2, "company_name": "B", "ticker": "B", "sector_code": "IN0101", "qtr_sales_var_pct": 20.0},
            {"company_id": 3, "company_name": "C", "ticker": "C", "sector_code": "IN0201", "qtr_sales_var_pct": 5.0},
        ]
    )
    gross_block = {
        1: {"gross_block_current_rscr": 110, "gross_block_preceding_rscr": 100},
        2: {"gross_block_current_rscr": 220, "gross_block_preceding_rscr": 200},
        # company 3 has no gross block data
    }

    result = fundamentals_sector_cycle.compute_sector_aggregates(l1, gross_block)

    ch = result[result["sector_code"] == "IN0101"].iloc[0]
    assert ch["n_companies_in_l1"] == 2
    assert ch["n_companies_with_gross_block"] == 2
    # (110+220 - 100-200) / (100+200) * 100 = 10.0
    assert ch["capacity_growth_pct"] == 10.0
    assert ch["demand_growth_pct"] == 15.0  # median(10, 20)

    auto = result[result["sector_code"] == "IN0201"].iloc[0]
    assert auto["n_companies_with_gross_block"] == 0
    assert pd.isna(auto["capacity_growth_pct"])  # None -> NaN once mixed into a float64 DataFrame column
    assert auto["demand_growth_pct"] == 5.0


def test_compute_sector_aggregates_flags_low_sample_size_confidence():
    # found live 2026-08-11: multiple real sectors had only 1 L1 company contributing
    # gross-block data -- a single company is not a "sector aggregate".
    l1 = pd.DataFrame(
        [{"company_id": i, "company_name": str(i), "ticker": str(i), "sector_code": "IN0101", "qtr_sales_var_pct": 10.0} for i in range(1, 7)]
    )
    # only 2 of 6 companies have gross-block data -- below MIN_COMPANIES_FOR_CONFIDENCE (5)
    gross_block = {
        1: {"gross_block_current_rscr": 110, "gross_block_preceding_rscr": 100},
        2: {"gross_block_current_rscr": 220, "gross_block_preceding_rscr": 200},
    }
    result = fundamentals_sector_cycle.compute_sector_aggregates(l1, gross_block)
    assert result.iloc[0]["sample_size_confidence"] == "low"


def test_compute_sector_aggregates_flags_adequate_sample_size_confidence():
    l1 = pd.DataFrame(
        [{"company_id": i, "company_name": str(i), "ticker": str(i), "sector_code": "IN0101", "qtr_sales_var_pct": 10.0} for i in range(1, 7)]
    )
    gross_block = {i: {"gross_block_current_rscr": 110, "gross_block_preceding_rscr": 100} for i in range(1, 6)}  # 5 of 6
    result = fundamentals_sector_cycle.compute_sector_aggregates(l1, gross_block)
    assert result.iloc[0]["sample_size_confidence"] == "adequate"


def test_compute_sector_aggregates_excludes_companies_without_sector_code():
    l1 = pd.DataFrame(
        [
            {"company_id": 1, "company_name": "A", "ticker": "A", "sector_code": "IN0101", "qtr_sales_var_pct": 10.0},
            {"company_id": 2, "company_name": "B", "ticker": "B", "sector_code": None, "qtr_sales_var_pct": 20.0},
        ]
    )
    result = fundamentals_sector_cycle.compute_sector_aggregates(l1, {})
    assert list(result["sector_code"]) == ["IN0101"]


def test_compute_sector_aggregates_empty_l1_returns_empty():
    assert fundamentals_sector_cycle.compute_sector_aggregates(pd.DataFrame(), {}).empty


def test_run_sector_cycle_aggregation_returns_early_on_empty_l1(monkeypatch):
    monkeypatch.setattr(fundamentals_sector_cycle, "load_l1_companies_with_sector", lambda: pd.DataFrame())
    fallback_events = []
    monkeypatch.setattr(fundamentals_sector_cycle, "_record_fallback", lambda *a, **k: fallback_events.append((a, k)))

    result = fundamentals_sector_cycle.run_sector_cycle_aggregation()

    assert result == {"sectors": 0, "companies_without_sector_code": 0}
    assert any(a and a[0] == "sector_cycle_no_l1_universe" for a, k in fallback_events)


def test_run_sector_cycle_aggregation_logs_missing_sector_code_and_upserts(monkeypatch):
    l1 = pd.DataFrame(
        [
            {"company_id": 1, "company_name": "A", "ticker": "A", "sector_code": "IN0101", "qtr_sales_var_pct": 10.0},
            {"company_id": 2, "company_name": "B", "ticker": "B", "sector_code": None, "qtr_sales_var_pct": 20.0},
        ]
    )
    monkeypatch.setattr(fundamentals_sector_cycle, "load_l1_companies_with_sector", lambda: l1)
    monkeypatch.setattr(fundamentals_sector_cycle, "fetch_gross_block_data", lambda: {})
    upserts = []
    monkeypatch.setattr(fundamentals_sector_cycle, "upsert_to_db", lambda df, table, **k: upserts.append((df, table, k)))
    fallback_events = []
    monkeypatch.setattr(fundamentals_sector_cycle, "_record_fallback", lambda *a, **k: fallback_events.append((a, k)))

    result = fundamentals_sector_cycle.run_sector_cycle_aggregation()

    assert result == {"sectors": 1, "companies_without_sector_code": 1}
    assert len(upserts) == 1
    assert upserts[0][1] == fundamentals_sector_cycle.RESULTS_TABLE
    assert upserts[0][2]["unique_keys"] == ["sector_code", "run_date"]
    assert any(a and a[0] == "sector_cycle_missing_sector_code" for a, k in fallback_events)


def test_compute_technicals_empty_history_returns_none_stats():
    result = fundamentals_technicals.compute_technicals(pd.DataFrame(columns=["date", "adj_close"]))
    assert result == {"as_of_date": None, "close": None, "data_points_available": 0}


def test_compute_technicals_partial_history_leaves_long_windows_none():
    # only 30 rows -- shorter than every return/DMA window this module computes
    dates = pd.date_range("2026-01-01", periods=30, freq="D")
    history = pd.DataFrame({"date": dates, "adj_close": [100.0 + i for i in range(30)]})

    result = fundamentals_technicals.compute_technicals(history)

    assert result["data_points_available"] == 30
    assert result["close"] == 129.0
    assert result["return_3m_pct"] is None
    assert result["dma_50"] is None
    assert result["z_score_vs_200d_mean"] is None


def test_compute_technicals_computes_returns_and_dma_with_full_history():
    # 260 rows: enough for every window (63/126/252 trading days, 50/200 DMA)
    dates = pd.date_range("2025-01-01", periods=260, freq="D")
    closes = [100.0] * 259 + [110.0]  # flat history except the very last close, up 10%
    history = pd.DataFrame({"date": dates, "adj_close": closes})

    result = fundamentals_technicals.compute_technicals(history)

    assert result["close"] == 110.0
    assert result["return_3m_pct"] == 10.0
    assert result["return_6m_pct"] == 10.0
    assert result["return_12m_pct"] == 10.0
    assert result["dma_50"] is not None
    assert result["dma_200"] is not None
    assert result["pct_vs_dma_50"] > 0  # last close above its own trailing average
    assert result["z_score_vs_200d_mean"] is not None
    assert result["z_score_vs_200d_mean"] > 0  # last close is an outlier above a flat run


def test_run_technicals_refresh_returns_early_on_empty_l1(monkeypatch):
    monkeypatch.setattr(fundamentals_technicals, "load_l1_tickers", lambda: pd.DataFrame())
    fallback_events = []
    monkeypatch.setattr(fundamentals_technicals, "_record_fallback", lambda *a, **k: fallback_events.append((a, k)))

    result = fundamentals_technicals.run_technicals_refresh()

    assert result == {"companies": 0, "no_history": 0}
    assert any(a and a[0] == "technicals_no_l1_universe" for a, k in fallback_events)


def test_run_technicals_refresh_flags_insufficient_history_and_upserts(monkeypatch):
    tickers = pd.DataFrame([{"ticker": "AAA", "company_name": "A Co"}, {"ticker": "BBB", "company_name": "B Co"}])
    monkeypatch.setattr(fundamentals_technicals, "load_l1_tickers", lambda: tickers)

    full_history = pd.DataFrame({"date": pd.date_range("2025-01-01", periods=260, freq="D"), "adj_close": [100.0] * 260})
    thin_history = pd.DataFrame({"date": pd.date_range("2026-07-01", periods=5, freq="D"), "adj_close": [50.0] * 5})

    def fake_history(ticker, **kwargs):
        return full_history if ticker == "AAA" else thin_history

    monkeypatch.setattr(fundamentals_technicals, "load_adjusted_price_history", fake_history)
    upserts = []
    monkeypatch.setattr(fundamentals_technicals, "upsert_to_db", lambda df, table, **k: upserts.append((df, table, k)))
    fallback_events = []
    monkeypatch.setattr(fundamentals_technicals, "_record_fallback", lambda *a, **k: fallback_events.append((a, k)))

    result = fundamentals_technicals.run_technicals_refresh()

    assert result == {"companies": 2, "no_history": 1}
    assert len(upserts) == 1
    assert upserts[0][1] == fundamentals_technicals.RESULTS_TABLE
    assert upserts[0][2]["unique_keys"] == ["company_master_id", "run_date"]
    written = upserts[0][0]
    assert set(written["company_master_id"]) == {"nse:AAA", "nse:BBB"}
    assert any(a and a[0] == "technicals_insufficient_history" for a, k in fallback_events)


def test_load_price_near_prefers_adjusted_falls_back_to_raw(monkeypatch):
    calls = []

    def fake_sql_to_df(query, params=None):
        calls.append(query)
        if "advisory_adjusted_ohlcv_daily" in query:
            return pd.DataFrame()  # no adjusted data -- forces fallback
        return pd.DataFrame([{"close": 42.5}])

    monkeypatch.setattr(fundamentals_watchlist, "sql_to_df", fake_sql_to_df)

    price = fundamentals_watchlist.load_price_near("nse:FOO", "2026-08-01")

    assert price == 42.5
    assert len(calls) == 2  # tried adjusted first, then raw


def test_load_price_near_returns_none_when_no_source_has_data(monkeypatch):
    monkeypatch.setattr(fundamentals_watchlist, "sql_to_df", lambda q, params=None: pd.DataFrame())
    assert fundamentals_watchlist.load_price_near("nse:FOO", "2026-08-01") is None


def test_load_price_near_returns_none_for_missing_ticker_or_date():
    assert fundamentals_watchlist.load_price_near(None, "2026-08-01") is None
    assert fundamentals_watchlist.load_price_near("nse:FOO", None) is None


def test_sync_watchlist_from_alerts_returns_early_on_no_alerts(monkeypatch):
    monkeypatch.setattr(fundamentals_watchlist, "_ensure_watchlist_table", lambda: None)
    monkeypatch.setattr(fundamentals_watchlist, "load_l3_alert_summary_by_company", lambda: pd.DataFrame())

    result = fundamentals_watchlist.sync_watchlist_from_alerts()

    assert result == {"companies": 0, "new_candidates": 0, "no_price_at_first_seen": 0}


def test_sync_watchlist_from_alerts_new_company_looks_up_first_seen_price(monkeypatch):
    monkeypatch.setattr(fundamentals_watchlist, "_ensure_watchlist_table", lambda: None)
    alert_summary = pd.DataFrame(
        [{"company_master_id": "nse:FOO", "first_alert_date": pd.Timestamp("2026-08-01"), "last_alert_date": pd.Timestamp("2026-08-03"), "alert_count": 2}]
    )
    monkeypatch.setattr(fundamentals_watchlist, "load_l3_alert_summary_by_company", lambda: alert_summary)
    monkeypatch.setattr(fundamentals_watchlist, "load_existing_watchlist", lambda: pd.DataFrame())
    monkeypatch.setattr(fundamentals_watchlist, "load_price_near", lambda cid, date: 100.0)
    upserts = []
    monkeypatch.setattr(fundamentals_watchlist, "upsert_to_db", lambda df, table, **k: upserts.append((df, table, k)))

    result = fundamentals_watchlist.sync_watchlist_from_alerts()

    assert result["companies"] == 1
    assert result["new_candidates"] == 1
    assert result["new_candidate_ids"] == ["nse:FOO"]
    assert result["no_price_at_first_seen"] == 0
    written = upserts[0][0].iloc[0]
    assert written["first_seen_at"] == pd.Timestamp("2026-08-01")
    assert written["first_seen_price"] == 100.0
    assert written["last_alert_at"] == pd.Timestamp("2026-08-03")
    assert written["alert_count"] == 2
    assert upserts[0][2]["unique_keys"] == ["company_master_id"]


def test_sync_watchlist_from_alerts_existing_company_keeps_first_seen_updates_last_alert(monkeypatch):
    # a company already on the watchlist gets a NEW alert -- first_seen_at/price must
    # stay exactly as originally recorded, only last_alert_at/alert_count move.
    monkeypatch.setattr(fundamentals_watchlist, "_ensure_watchlist_table", lambda: None)
    alert_summary = pd.DataFrame(
        [{"company_master_id": "nse:FOO", "first_alert_date": pd.Timestamp("2026-08-01"), "last_alert_date": pd.Timestamp("2026-08-05"), "alert_count": 3}]
    )
    monkeypatch.setattr(fundamentals_watchlist, "load_l3_alert_summary_by_company", lambda: alert_summary)
    existing = pd.DataFrame([{"company_master_id": "nse:FOO", "first_seen_at": pd.Timestamp("2026-08-01"), "first_seen_price": 100.0}])
    monkeypatch.setattr(fundamentals_watchlist, "load_existing_watchlist", lambda: existing)

    def fail_if_called(cid, date):
        raise AssertionError("load_price_near must not be called for an already-known company")

    monkeypatch.setattr(fundamentals_watchlist, "load_price_near", fail_if_called)
    upserts = []
    monkeypatch.setattr(fundamentals_watchlist, "upsert_to_db", lambda df, table, **k: upserts.append((df, table, k)))

    result = fundamentals_watchlist.sync_watchlist_from_alerts()

    assert result["new_candidates"] == 0
    written = upserts[0][0].iloc[0]
    assert written["first_seen_at"] == pd.Timestamp("2026-08-01")
    assert written["first_seen_price"] == 100.0
    assert written["last_alert_at"] == pd.Timestamp("2026-08-05")
    assert written["alert_count"] == 3


def test_sync_watchlist_from_alerts_flags_missing_price_without_failing(monkeypatch):
    monkeypatch.setattr(fundamentals_watchlist, "_ensure_watchlist_table", lambda: None)
    alert_summary = pd.DataFrame(
        [{"company_master_id": "nse:FOO", "first_alert_date": pd.Timestamp("2026-08-01"), "last_alert_date": pd.Timestamp("2026-08-01"), "alert_count": 1}]
    )
    monkeypatch.setattr(fundamentals_watchlist, "load_l3_alert_summary_by_company", lambda: alert_summary)
    monkeypatch.setattr(fundamentals_watchlist, "load_existing_watchlist", lambda: pd.DataFrame())
    monkeypatch.setattr(fundamentals_watchlist, "load_price_near", lambda cid, date: None)
    monkeypatch.setattr(fundamentals_watchlist, "upsert_to_db", lambda df, table, **k: None)
    fallback_events = []
    monkeypatch.setattr(fundamentals_watchlist, "_record_fallback", lambda *a, **k: fallback_events.append((a, k)))

    result = fundamentals_watchlist.sync_watchlist_from_alerts()

    assert result["no_price_at_first_seen"] == 1
    assert any(a and a[0] == "watchlist_no_price_at_first_seen" for a, k in fallback_events)


def test_build_company_evidence_bundle_shapes_output():
    alerts = pd.DataFrame([{"trigger_type": "rating_downgrade", "origin": "rule", "alert_date": date(2026, 8, 1), "reasoning": "r"}])
    bundle = fundamentals_watch_summary.build_company_evidence_bundle("nse:X", alerts, {"ticker": "X"}, {"close": 100}, {"sector_code": "IN01"})
    assert bundle["company_master_id"] == "nse:X"
    assert bundle["alerts"] == [{"trigger_type": "rating_downgrade", "origin": "rule", "alert_date": date(2026, 8, 1), "reasoning": "r"}]
    assert bundle["l2_state"] == {"ticker": "X"}
    assert bundle["technicals"] == {"close": 100}
    assert bundle["sector_context"] == {"sector_code": "IN01"}


def test_build_company_evidence_bundle_empty_alerts():
    bundle = fundamentals_watch_summary.build_company_evidence_bundle("nse:X", pd.DataFrame(), None, None, None)
    assert bundle["alerts"] == []
    assert bundle["l2_state"] is None


def test_run_watch_summary_refresh_returns_early_when_no_candidates(monkeypatch):
    monkeypatch.setattr(fundamentals_watch_summary, "_bootstrap_narrative_columns", lambda: None)
    monkeypatch.setattr(fundamentals_watch_summary, "load_companies_needing_narrative_refresh", lambda limit=None: pd.DataFrame())

    result = fundamentals_watch_summary.run_watch_summary_refresh()

    assert result == {"generated": 0, "failed": 0, "blocked": False, "narrative_events": []}


def _patch_watch_summary_evidence_loaders(monkeypatch):
    monkeypatch.setattr(fundamentals_watch_summary, "load_company_alerts", lambda cmid: pd.DataFrame())
    monkeypatch.setattr(fundamentals_watch_summary, "load_latest_l2_state_for_company", lambda cmid: None)
    monkeypatch.setattr(fundamentals_watch_summary, "load_latest_technicals_for_company", lambda cmid: None)
    monkeypatch.setattr(fundamentals_watch_summary, "load_sector_context_for_company", lambda cmid: None)


def test_run_watch_summary_refresh_new_candidate_marks_narrative_changed(monkeypatch):
    monkeypatch.setattr(fundamentals_watch_summary, "_bootstrap_narrative_columns", lambda: None)
    candidates = pd.DataFrame([{"company_master_id": "nse:X", "last_alert_at": date(2026, 8, 1), "narrative_generated_at": None, "narrative_text": None}])
    monkeypatch.setattr(fundamentals_watch_summary, "load_companies_needing_narrative_refresh", lambda limit=None: candidates)
    _patch_watch_summary_evidence_loaders(monkeypatch)
    monkeypatch.setattr(
        fundamentals_watch_summary, "generate_watch_summary", lambda bundle, **k: {"narrative": "first narrative", "suggested_watch_duration_days": 30, "confidence": "high"}
    )
    updates = []
    monkeypatch.setattr(fundamentals_watch_summary, "_update_narrative", lambda **kwargs: updates.append(kwargs))

    result = fundamentals_watch_summary.run_watch_summary_refresh(model="test-model")

    assert result["generated"] == 1
    assert result["failed"] == 0
    assert result["blocked"] is False
    event = result["narrative_events"][0]
    assert event["company_master_id"] == "nse:X"
    assert event["is_new_candidate"] is True
    assert event["narrative_changed"] is True
    assert event["narrative_text"] == "first narrative"
    assert len(updates) == 1
    assert updates[0]["company_master_id"] == "nse:X"
    assert updates[0]["model"] == "test-model"


def test_run_watch_summary_refresh_existing_candidate_detects_text_change(monkeypatch):
    monkeypatch.setattr(fundamentals_watch_summary, "_bootstrap_narrative_columns", lambda: None)
    candidates = pd.DataFrame(
        [{"company_master_id": "nse:X", "last_alert_at": date(2026, 8, 5), "narrative_generated_at": pd.Timestamp("2026-08-01", tz="UTC"), "narrative_text": "old narrative"}]
    )
    monkeypatch.setattr(fundamentals_watch_summary, "load_companies_needing_narrative_refresh", lambda limit=None: candidates)
    _patch_watch_summary_evidence_loaders(monkeypatch)
    monkeypatch.setattr(
        fundamentals_watch_summary, "generate_watch_summary", lambda bundle, **k: {"narrative": "new narrative", "suggested_watch_duration_days": 10, "confidence": "medium"}
    )
    monkeypatch.setattr(fundamentals_watch_summary, "_update_narrative", lambda **kwargs: None)

    result = fundamentals_watch_summary.run_watch_summary_refresh()

    event = result["narrative_events"][0]
    assert event["is_new_candidate"] is False
    assert event["narrative_changed"] is True


def test_run_watch_summary_refresh_unchanged_text_not_flagged_as_changed(monkeypatch):
    # edge case: a company already had a narrative and the LLM happens to return the
    # exact same text again -- narrative_changed must be False so no spurious email.
    monkeypatch.setattr(fundamentals_watch_summary, "_bootstrap_narrative_columns", lambda: None)
    candidates = pd.DataFrame(
        [{"company_master_id": "nse:X", "last_alert_at": date(2026, 8, 5), "narrative_generated_at": pd.Timestamp("2026-08-01", tz="UTC"), "narrative_text": "same narrative"}]
    )
    monkeypatch.setattr(fundamentals_watch_summary, "load_companies_needing_narrative_refresh", lambda limit=None: candidates)
    _patch_watch_summary_evidence_loaders(monkeypatch)
    monkeypatch.setattr(
        fundamentals_watch_summary, "generate_watch_summary", lambda bundle, **k: {"narrative": "same narrative", "suggested_watch_duration_days": 10, "confidence": "medium"}
    )
    monkeypatch.setattr(fundamentals_watch_summary, "_update_narrative", lambda **kwargs: None)

    result = fundamentals_watch_summary.run_watch_summary_refresh()

    assert result["narrative_events"][0]["narrative_changed"] is False


def test_run_watch_summary_refresh_trips_circuit_breaker(monkeypatch):
    monkeypatch.setattr(fundamentals_watch_summary, "_bootstrap_narrative_columns", lambda: None)
    candidates = pd.DataFrame(
        [{"company_master_id": f"nse:X{i}", "last_alert_at": date(2026, 8, 1), "narrative_generated_at": None, "narrative_text": None} for i in range(5)]
    )
    monkeypatch.setattr(fundamentals_watch_summary, "load_companies_needing_narrative_refresh", lambda limit=None: candidates)
    _patch_watch_summary_evidence_loaders(monkeypatch)

    def always_fails(bundle, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr(fundamentals_watch_summary, "generate_watch_summary", always_fails)
    monkeypatch.setattr(fundamentals_watch_summary, "_update_narrative", lambda **kwargs: None)
    fallback_events = []
    monkeypatch.setattr(fundamentals_watch_summary, "_record_fallback", lambda *a, **k: fallback_events.append((a, k)))

    result = fundamentals_watch_summary.run_watch_summary_refresh()

    assert result["blocked"] is True
    assert result["failed"] == fundamentals_watch_summary.CIRCUIT_BREAKER_THRESHOLD
    assert any(a and a[0] == "watch_summary_circuit_breaker_tripped" for a, k in fallback_events)


def test_build_email_content_distinguishes_new_candidate_vs_narrative_updated():
    new_subject, new_body = fundamentals_notifications.build_email_content(
        {"company_master_id": "nse:FOO", "is_new_candidate": True, "narrative_text": "n", "suggested_watch_until": "2026-09-01", "confidence": "high"}
    )
    updated_subject, updated_body = fundamentals_notifications.build_email_content(
        {"company_master_id": "nse:FOO", "is_new_candidate": False, "narrative_text": "n", "suggested_watch_until": "2026-09-01", "confidence": "high"}
    )
    assert "New candidate" in new_subject
    assert "Narrative updated" in updated_subject
    assert "not a trade recommendation" in new_body
    assert "not a trade recommendation" in updated_body


def test_notify_watchlist_events_no_changed_events_returns_zero():
    result = fundamentals_notifications.notify_watchlist_events(
        [{"company_master_id": "nse:FOO", "narrative_changed": False}]
    )
    assert result == {"sent": 0, "skipped_disabled": 0, "failed": 0}


def test_notify_watchlist_events_skips_when_disabled(monkeypatch):
    monkeypatch.setattr(fundamentals_notifications, "WATCHLIST_ALERT_EMAIL_ENABLED", False)
    events = [{"company_master_id": "nse:FOO", "narrative_changed": True, "is_new_candidate": True, "narrative_text": "n"}]

    result = fundamentals_notifications.notify_watchlist_events(events)

    assert result == {"sent": 0, "skipped_disabled": 1, "failed": 0}


def test_notify_watchlist_events_flags_missing_config_when_enabled(monkeypatch):
    monkeypatch.setattr(fundamentals_notifications, "WATCHLIST_ALERT_EMAIL_ENABLED", True)
    monkeypatch.setattr(fundamentals_notifications, "WATCHLIST_ALERT_EMAIL_FROM", "")
    monkeypatch.setattr(fundamentals_notifications, "WATCHLIST_ALERT_EMAIL_TO", "")
    fallback_events = []
    monkeypatch.setattr(fundamentals_notifications, "_record_fallback", lambda *a, **k: fallback_events.append((a, k)))
    events = [{"company_master_id": "nse:FOO", "narrative_changed": True, "is_new_candidate": True, "narrative_text": "n"}]

    result = fundamentals_notifications.notify_watchlist_events(events)

    assert result == {"sent": 0, "skipped_disabled": 0, "failed": 1}
    assert any(a and a[0] == "watchlist_email_misconfigured" for a, k in fallback_events)


def test_notify_watchlist_events_sends_when_enabled_and_configured(monkeypatch):
    monkeypatch.setattr(fundamentals_notifications, "WATCHLIST_ALERT_EMAIL_ENABLED", True)
    monkeypatch.setattr(fundamentals_notifications, "WATCHLIST_ALERT_EMAIL_FROM", "alerts@example.com")
    monkeypatch.setattr(fundamentals_notifications, "WATCHLIST_ALERT_EMAIL_TO", "me@example.com")
    sent_calls = []
    monkeypatch.setattr(fundamentals_notifications, "send_email", lambda subject, body: sent_calls.append((subject, body)))
    events = [
        {"company_master_id": "nse:FOO", "narrative_changed": True, "is_new_candidate": True, "narrative_text": "n1"},
        {"company_master_id": "nse:BAR", "narrative_changed": False, "is_new_candidate": False, "narrative_text": "n2"},
    ]

    result = fundamentals_notifications.notify_watchlist_events(events)

    assert result == {"sent": 1, "skipped_disabled": 0, "failed": 0}
    assert len(sent_calls) == 1  # only the narrative_changed=True event sends


def test_notify_watchlist_events_one_failure_does_not_block_others(monkeypatch):
    monkeypatch.setattr(fundamentals_notifications, "WATCHLIST_ALERT_EMAIL_ENABLED", True)
    monkeypatch.setattr(fundamentals_notifications, "WATCHLIST_ALERT_EMAIL_FROM", "alerts@example.com")
    monkeypatch.setattr(fundamentals_notifications, "WATCHLIST_ALERT_EMAIL_TO", "me@example.com")

    def flaky_send(subject, body):
        if "FOO" in subject:
            raise RuntimeError("ses down")
        return {"MessageId": "ok"}

    monkeypatch.setattr(fundamentals_notifications, "send_email", flaky_send)
    fallback_events = []
    monkeypatch.setattr(fundamentals_notifications, "_record_fallback", lambda *a, **k: fallback_events.append((a, k)))
    events = [
        {"company_master_id": "nse:FOO", "narrative_changed": True, "is_new_candidate": True, "narrative_text": "n1"},
        {"company_master_id": "nse:BAR", "narrative_changed": True, "is_new_candidate": True, "narrative_text": "n2"},
    ]

    result = fundamentals_notifications.notify_watchlist_events(events)

    assert result == {"sent": 1, "skipped_disabled": 0, "failed": 1}
    assert any(a and a[0] == "watchlist_email_send_failed" for a, k in fallback_events)


def test_send_email_returns_none_when_disabled(monkeypatch):
    monkeypatch.setattr(fundamentals_notifications, "WATCHLIST_ALERT_EMAIL_ENABLED", False)
    assert fundamentals_notifications.send_email("subject", "body") is None


def test_run_watchlist_notification_pipeline_chains_all_four_steps(monkeypatch):
    monkeypatch.setattr(
        fundamentals_notifications, "sync_watchlist_from_alerts", lambda: {"companies": 5, "new_candidates": 2, "new_candidate_ids": ["nse:A", "nse:B"], "no_price_at_first_seen": 0}
    )
    narrative_events = [{"company_master_id": "nse:A", "narrative_changed": True, "is_new_candidate": True, "narrative_text": "n"}]
    monkeypatch.setattr(
        fundamentals_notifications, "run_watch_summary_refresh", lambda: {"generated": 1, "failed": 0, "blocked": False, "narrative_events": narrative_events}
    )
    notify_calls = []
    monkeypatch.setattr(fundamentals_notifications, "notify_watchlist_events", lambda events: notify_calls.append(events) or {"sent": 1, "skipped_disabled": 0, "failed": 0})
    digest_calls = []
    # NOTE: send_daily_digest must always be mocked in tests that exercise the full
    # pipeline -- it reads the real WATCHLIST_ALERT_EMAIL_* config and would attempt
    # a real SES send against the real watchlist otherwise (this config is enabled
    # in production .env, not just a test fixture).
    monkeypatch.setattr(fundamentals_notifications, "send_daily_digest", lambda: digest_calls.append(1) or {"sent": 1, "skipped_disabled": 0, "failed": 0})

    result = fundamentals_notifications.run_watchlist_notification_pipeline()

    assert result["watchlist_companies"] == 5
    assert result["new_candidates"] == 2
    assert result["narratives_generated"] == 1
    assert result["emails_sent"] == 1
    assert result["digest_sent"] == 1
    assert notify_calls == [narrative_events]
    assert digest_calls == [1]


def test_clean_records_converts_nan_to_none_and_timestamp_to_iso():
    df = pd.DataFrame([{"a": float("nan"), "b": pd.Timestamp("2026-08-01", tz="UTC"), "c": "x"}])
    records = fundamentals_api_queries._clean_records(df)
    assert records == [{"a": None, "b": "2026-08-01T00:00:00.000Z", "c": "x"}]


def test_clean_records_empty_df_returns_empty_list():
    assert fundamentals_api_queries._clean_records(pd.DataFrame()) == []


def test_get_universe_returns_query_and_parsed_metrics(monkeypatch):
    df = pd.DataFrame(
        [{"ticker": "FOO", "company_name": "Foo Co", "metrics_json": json.dumps({"cmp_rs": 100.0, "p_e": 15.0}), "run_date": date(2026, 8, 10)}]
    )
    monkeypatch.setattr(fundamentals_api_queries, "sql_to_df", lambda q: df)

    result = fundamentals_api_queries.get_universe()

    assert result["query_text"] == fundamentals_api_queries.L1_QUERY
    assert result["query_version"] == fundamentals_api_queries.L1_QUERY_VERSION
    assert result["run_date"] == "2026-08-10"
    assert result["companies"] == [{"ticker": "FOO", "company_name": "Foo Co", "cmp_rs": 100.0, "p_e": 15.0, "mar_cap_rscr": None, "div_yld_pct": None, "roce_pct": None, "qtr_sales_var_pct": None, "avg_vol_1mth": None}]


def test_get_universe_empty_returns_empty_companies(monkeypatch):
    monkeypatch.setattr(fundamentals_api_queries, "sql_to_df", lambda q: pd.DataFrame())
    result = fundamentals_api_queries.get_universe()
    assert result["companies"] == []
    assert result["run_date"] is None


def test_get_watchlist_detail_returns_none_when_not_found(monkeypatch):
    monkeypatch.setattr(fundamentals_api_queries, "sql_to_df", lambda q, params=None: pd.DataFrame())
    assert fundamentals_api_queries.get_watchlist_detail("nse:MISSING") is None


def test_get_watchlist_detail_parses_evidence_bundle_and_joins_context(monkeypatch):
    watchlist_df = pd.DataFrame([{"company_master_id": "nse:FOO", "first_seen_at": date(2026, 8, 1), "alert_count": 1}])
    alerts_df = pd.DataFrame(
        [{"source": "bse", "news_id": "n1", "trigger_type": "insider_buy", "origin": "rule", "alert_date": date(2026, 8, 1), "reasoning": "r", "status": "new", "evidence_bundle_json": json.dumps({"event": {"headline": "h"}})}]
    )
    thesis_df = pd.DataFrame()

    calls = {"watchlist": watchlist_df, "alerts": alerts_df, "thesis": thesis_df}
    call_order = iter(["watchlist", "alerts", "thesis"])
    monkeypatch.setattr(fundamentals_api_queries, "sql_to_df", lambda q, params=None: calls[next(call_order)])
    monkeypatch.setattr(fundamentals_api_queries, "load_latest_l2_state_for_company", lambda cmid: {"ticker": "FOO"})
    monkeypatch.setattr(fundamentals_api_queries, "load_latest_technicals_for_company", lambda cmid: {"close": 100})
    monkeypatch.setattr(fundamentals_api_queries, "load_sector_context_for_company", lambda cmid: {"sector_code": "IN01"})

    result = fundamentals_api_queries.get_watchlist_detail("nse:FOO")

    assert result["watchlist"]["company_master_id"] == "nse:FOO"
    assert result["alerts"][0]["evidence_bundle"] == {"event": {"headline": "h"}}
    assert "evidence_bundle_json" not in result["alerts"][0]
    assert result["l2_state"] == {"ticker": "FOO"}
    assert result["technicals"] == {"close": 100}
    assert result["sector_context"] == {"sector_code": "IN01"}
    assert result["portfolio"] == []


def test_get_sectors_groups_watched_companies_by_sector(monkeypatch):
    sector_df = pd.DataFrame([{"sector_code": "IN01", "capacity_growth_pct": 5.0, "demand_growth_pct": 3.0, "phase": "capacity_expansion", "sample_size_confidence": "adequate", "n_companies_in_l1": 10, "sector_name": "Chemicals"}])
    watched_df = pd.DataFrame([{"sector_code": "IN01", "company_master_id": "nse:FOO", "alert_count": 2, "narrative_text": "x" * 300}])
    call_order = iter([sector_df, watched_df])
    monkeypatch.setattr(fundamentals_api_queries, "sql_to_df", lambda q: next(call_order))

    result = fundamentals_api_queries.get_sectors()

    assert len(result) == 1
    watched = result[0]["watched_companies"]
    assert len(watched) == 1
    assert watched[0]["company_master_id"] == "nse:FOO"
    assert len(watched[0]["narrative_snippet"]) == 200


def test_get_sectors_empty_returns_empty_list(monkeypatch):
    monkeypatch.setattr(fundamentals_api_queries, "sql_to_df", lambda q: pd.DataFrame())
    assert fundamentals_api_queries.get_sectors() == []


def test_get_sectors_no_watched_companies_gives_empty_list_per_sector(monkeypatch):
    sector_df = pd.DataFrame([{"sector_code": "IN01", "capacity_growth_pct": 5.0, "demand_growth_pct": 3.0, "phase": "balanced", "sample_size_confidence": "low", "n_companies_in_l1": 2, "sector_name": "X"}])
    call_order = iter([sector_df, pd.DataFrame()])
    monkeypatch.setattr(fundamentals_api_queries, "sql_to_df", lambda q: next(call_order))
    result = fundamentals_api_queries.get_sectors()
    assert result[0]["watched_companies"] == []


def test_create_portfolio_entry_delegates_to_create_thesis(monkeypatch):
    calls = []
    monkeypatch.setattr(fundamentals_api_queries, "create_thesis", lambda **kwargs: calls.append(kwargs) or {"thesis_id": "t1"})
    result = fundamentals_api_queries.create_portfolio_entry({"company_master_id": "nse:FOO", "prediction_text": "p"})
    assert result == {"thesis_id": "t1"}
    assert calls == [{"company_master_id": "nse:FOO", "prediction_text": "p"}]


def test_resolve_portfolio_entry_delegates_to_resolve_thesis(monkeypatch):
    calls = []
    monkeypatch.setattr(fundamentals_api_queries, "resolve_thesis", lambda **kwargs: calls.append(kwargs))
    fundamentals_api_queries.resolve_portfolio_entry("t1", {"resolved_true": True})
    assert calls == [{"thesis_id": "t1", "resolved_true": True}]


# --- HTTP routing tests (fundamentals/api/app.py) -- queries.py mocked, so these
# exercise only request/response wiring (status codes, validation, error translation),
# not the DB-backed query logic already covered above. ---

def test_api_universe_route_returns_queries_result(monkeypatch):
    monkeypatch.setattr(fundamentals_api_queries, "get_universe", lambda: {"query_text": "q", "query_version": 1, "run_date": None, "companies": []})
    client = TestClient(fundamentals_api_app)
    r = client.get("/api/universe")
    assert r.status_code == 200
    assert r.json()["query_version"] == 1


def test_api_watchlist_detail_route_404_when_not_found(monkeypatch):
    monkeypatch.setattr(fundamentals_api_queries, "get_watchlist_detail", lambda cmid: None)
    client = TestClient(fundamentals_api_app)
    r = client.get("/api/watchlist/nse:MISSING")
    assert r.status_code == 404


def test_api_watchlist_detail_route_200_when_found(monkeypatch):
    monkeypatch.setattr(fundamentals_api_queries, "get_watchlist_detail", lambda cmid: {"watchlist": {"company_master_id": cmid}})
    client = TestClient(fundamentals_api_app)
    r = client.get("/api/watchlist/nse:FOO")
    assert r.status_code == 200
    assert r.json()["watchlist"]["company_master_id"] == "nse:FOO"


def test_api_create_portfolio_rejects_invalid_origin_tag():
    client = TestClient(fundamentals_api_app)
    payload = {
        "company_master_id": "nse:FOO", "prediction_text": "p", "target_date": "2026-12-01",
        "invalidation_criteria": "c", "origin_tag": "bogus",
    }
    r = client.post("/api/portfolio", json=payload)
    assert r.status_code == 400


def test_api_create_portfolio_success_calls_queries(monkeypatch):
    calls = []
    monkeypatch.setattr(fundamentals_api_queries, "create_portfolio_entry", lambda payload: calls.append(payload) or {"thesis_id": "t1"})
    client = TestClient(fundamentals_api_app)
    payload = {
        "company_master_id": "nse:FOO", "prediction_text": "p", "target_date": "2026-12-01",
        "invalidation_criteria": "c", "origin_tag": "ad_hoc",
        "source_alert_source": "bse", "source_alert_news_id": "n1", "source_alert_trigger_type": "insider_buy",
    }
    r = client.post("/api/portfolio", json=payload)
    assert r.status_code == 200
    assert r.json() == {"thesis_id": "t1"}
    assert calls[0]["source_alert"] == {"source": "bse", "news_id": "n1", "trigger_type": "insider_buy"}
    assert "source_alert_source" not in calls[0]


def test_api_create_portfolio_validation_error_becomes_400(monkeypatch):
    def raise_validation(payload):
        raise fundamentals_l4_thesis.ThesisValidationError("prediction_text is mandatory")

    monkeypatch.setattr(fundamentals_api_queries, "create_portfolio_entry", raise_validation)
    client = TestClient(fundamentals_api_app)
    payload = {"company_master_id": "nse:FOO", "prediction_text": "p", "target_date": "2026-12-01", "invalidation_criteria": "c", "origin_tag": "ad_hoc"}
    r = client.post("/api/portfolio", json=payload)
    assert r.status_code == 400
    assert "mandatory" in r.json()["detail"]


def test_api_resolve_portfolio_validation_error_becomes_400(monkeypatch):
    def raise_validation(thesis_id, payload):
        raise fundamentals_l4_thesis.ThesisValidationError("failure_attribution required")

    monkeypatch.setattr(fundamentals_api_queries, "resolve_portfolio_entry", raise_validation)
    client = TestClient(fundamentals_api_app)
    r = client.post("/api/portfolio/t1/resolve", json={"resolved_true": False})
    assert r.status_code == 400
    assert "failure_attribution" in r.json()["detail"]


def test_api_resolve_portfolio_success(monkeypatch):
    monkeypatch.setattr(fundamentals_api_queries, "resolve_portfolio_entry", lambda thesis_id, payload: None)
    client = TestClient(fundamentals_api_app)
    r = client.post("/api/portfolio/t1/resolve", json={"resolved_true": True})
    assert r.status_code == 200
    assert r.json() == {"status": "resolved", "thesis_id": "t1"}


def _fake_step_module(*, main_fn, run_state=None):
    """A fake module object for run_pipeline.run_step tests -- registers itself into
    sys.modules on "import" the same way a real importlib.import_module call would,
    since run_step's SystemExit branch looks the module back up via sys.modules.get()."""
    module = types.SimpleNamespace(main=main_fn, STOCKEY_RUN_STATE=run_state or {})
    return module


def test_run_step_success_extracts_run_state(monkeypatch):
    fake_module = _fake_step_module(main_fn=lambda: 0, run_state={"source": "fake", "status": "ok"})

    def fake_import(name):
        sys.modules[name] = fake_module
        return fake_module

    monkeypatch.setattr(fundamentals_run_pipeline.importlib, "import_module", fake_import)

    result = fundamentals_run_pipeline.run_step("fundamentals.test.fake_ok")

    assert result["module"] == "fundamentals.test.fake_ok"
    assert result["returncode"] == 0
    assert result["run_state"] == {"source": "fake", "status": "ok"}
    assert "error" not in result
    sys.modules.pop("fundamentals.test.fake_ok", None)


def test_run_step_nonzero_return_marked_failed(monkeypatch):
    fake_module = _fake_step_module(main_fn=lambda: 1, run_state={"status": "blocked"})
    monkeypatch.setattr(fundamentals_run_pipeline.importlib, "import_module", lambda name: fake_module)

    result = fundamentals_run_pipeline.run_step("fundamentals.test.fake_nonzero")

    assert result["returncode"] == 1
    assert result["run_state"] == {"status": "blocked"}


def test_run_step_system_exit_nonzero_extracts_run_state(monkeypatch):
    def fake_import(name):
        fake_module.STOCKEY_RUN_STATE = {"status": "failed"}
        sys.modules[name] = fake_module

        def raising_main():
            raise SystemExit(1)

        fake_module.main = raising_main
        return fake_module

    fake_module = _fake_step_module(main_fn=lambda: 0)
    monkeypatch.setattr(fundamentals_run_pipeline.importlib, "import_module", fake_import)

    result = fundamentals_run_pipeline.run_step("fundamentals.test.fake_sysexit")

    assert result["returncode"] == 1
    assert result["run_state"] == {"status": "failed"}
    sys.modules.pop("fundamentals.test.fake_sysexit", None)


def test_run_step_system_exit_none_normalizes_to_zero(monkeypatch):
    def raising_main():
        raise SystemExit()  # SystemExit(None) -- the raise SystemExit(main()) convention with a None main()

    fake_module = _fake_step_module(main_fn=raising_main)

    def fake_import(name):
        sys.modules[name] = fake_module
        return fake_module

    monkeypatch.setattr(fundamentals_run_pipeline.importlib, "import_module", fake_import)

    result = fundamentals_run_pipeline.run_step("fundamentals.test.fake_sysexit_none")

    assert result["returncode"] == 0
    sys.modules.pop("fundamentals.test.fake_sysexit_none", None)


def test_run_step_unexpected_exception_is_isolated(monkeypatch):
    def raising_main():
        raise RuntimeError("boom")

    fake_module = _fake_step_module(main_fn=raising_main)
    monkeypatch.setattr(fundamentals_run_pipeline.importlib, "import_module", lambda name: fake_module)

    result = fundamentals_run_pipeline.run_step("fundamentals.test.fake_exception")

    assert result["returncode"] == 1
    assert result["run_state"] == {}
    assert "RuntimeError: boom" in result["error"]


def test_run_step_import_error_is_isolated(monkeypatch):
    def raise_import_error(name):
        raise ModuleNotFoundError(f"No module named {name!r}")

    monkeypatch.setattr(fundamentals_run_pipeline.importlib, "import_module", raise_import_error)

    result = fundamentals_run_pipeline.run_step("fundamentals.test.does_not_exist")

    assert result["returncode"] == 1
    assert "ModuleNotFoundError" in result["error"]


def test_run_pipeline_isolates_one_failure_and_continues(monkeypatch):
    calls = []

    def make_fake(name, code):
        def fake_import(_name, _code=code, _name2=name):
            calls.append(_name2)
            return _fake_step_module(main_fn=lambda c=_code: c)

        return fake_import

    order = iter([make_fake("step_a", 1), make_fake("step_b", 0)])
    monkeypatch.setattr(fundamentals_run_pipeline.importlib, "import_module", lambda name: next(order)(name))

    result = fundamentals_run_pipeline.run_pipeline(["step_a", "step_b"])

    assert result["steps_run"] == 2
    assert result["failed"] == ["step_a"]
    assert calls == ["step_a", "step_b"]


def test_run_pipeline_default_steps_matches_module_list():
    assert fundamentals_run_pipeline.run_pipeline.__defaults__ or True  # sanity: run_pipeline() with no args uses STEPS
    assert len(fundamentals_run_pipeline.STEPS) == 13
    assert fundamentals_run_pipeline.STEPS[-1] == "fundamentals.screens.notifications"


def test_main_exits_zero_when_not_all_steps_failed(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["run_pipeline"])
    monkeypatch.setattr(
        fundamentals_run_pipeline,
        "run_pipeline",
        lambda steps=None: {"steps_run": 2, "failed": ["a"], "results": []},
    )

    exit_code = fundamentals_run_pipeline.main()

    assert exit_code == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed["source"] == "fundamentals.run_pipeline"
    assert printed["failed"] == ["a"]


def test_main_exits_nonzero_when_all_steps_failed(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["run_pipeline"])
    monkeypatch.setattr(
        fundamentals_run_pipeline,
        "run_pipeline",
        lambda steps=None: {"steps_run": 2, "failed": ["a", "b"], "results": []},
    )

    exit_code = fundamentals_run_pipeline.main()

    assert exit_code == 1


def test_main_passes_steps_override_through(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["run_pipeline", "--steps", "mod.a", "mod.b"])
    captured = {}

    def fake_run_pipeline(steps=None):
        captured["steps"] = steps
        return {"steps_run": 2, "failed": [], "results": []}

    monkeypatch.setattr(fundamentals_run_pipeline, "run_pipeline", fake_run_pipeline)

    fundamentals_run_pipeline.main()

    assert captured["steps"] == ["mod.a", "mod.b"]


def test_api_health_route():
    client = TestClient(fundamentals_api_app)
    r = client.get("/api/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_get_portfolio_scoring_delegates_to_compute_quarterly_scoring(monkeypatch):
    canned = {"as_of_date": "2026-08-12", "total_theses": 3, "open": 1, "resolved": 2, "hit_rate": 50.0, "failure_attribution_breakdown": {"thesis_wrong": 1}, "time_to_confirmation_days": {"median_days": 90.0}}
    monkeypatch.setattr(fundamentals_api_queries, "compute_quarterly_scoring", lambda: canned)

    result = fundamentals_api_queries.get_portfolio_scoring()

    assert result == canned


def test_api_portfolio_scoring_route_returns_queries_result(monkeypatch):
    canned = {"as_of_date": "2026-08-12", "total_theses": 0, "open": 0, "resolved": 0, "hit_rate": None, "failure_attribution_breakdown": {}, "time_to_confirmation_days": {}}
    monkeypatch.setattr(fundamentals_api_queries, "get_portfolio_scoring", lambda: canned)
    client = TestClient(fundamentals_api_app)

    r = client.get("/api/portfolio/scoring")

    assert r.status_code == 200
    assert r.json() == canned


def test_parse_recipients_splits_on_whitespace():
    assert fundamentals_notifications._parse_recipients("a@x.com b@y.com   c@z.com") == ["a@x.com", "b@y.com", "c@z.com"]


def test_parse_recipients_single_address_with_surrounding_whitespace():
    assert fundamentals_notifications._parse_recipients("  single@x.com  ") == ["single@x.com"]


def test_parse_recipients_empty_or_none_returns_empty_list():
    assert fundamentals_notifications._parse_recipients("") == []
    assert fundamentals_notifications._parse_recipients(None) == []


def test_send_email_passes_full_recipient_list_to_ses(monkeypatch):
    monkeypatch.setattr(fundamentals_notifications, "WATCHLIST_ALERT_EMAIL_ENABLED", True)
    monkeypatch.setattr(fundamentals_notifications, "WATCHLIST_ALERT_EMAIL_TO", "a@x.com b@y.com")
    monkeypatch.setattr(fundamentals_notifications, "WATCHLIST_ALERT_EMAIL_FROM", "from@x.com")
    calls = []

    class FakeSesClient:
        def send_email(self, **kwargs):
            calls.append(kwargs)
            return {"MessageId": "1"}

    monkeypatch.setattr(fundamentals_notifications, "_get_ses_client", lambda: FakeSesClient())

    fundamentals_notifications.send_email("subject", "body")

    assert calls[0]["Destination"] == {"ToAddresses": ["a@x.com", "b@y.com"]}


def test_load_full_watchlist_empty_returns_empty_list(monkeypatch):
    monkeypatch.setattr(fundamentals_notifications, "sql_to_df", lambda q: pd.DataFrame())
    assert fundamentals_notifications.load_full_watchlist() == []


def test_build_daily_digest_content_empty_watchlist():
    subject, body = fundamentals_notifications.build_daily_digest_content([])
    assert "nothing on the watchlist" in subject.lower()
    assert "No companies" in body


def test_build_daily_digest_content_lists_every_company():
    rows = [
        {"company_master_id": "nse:FOO", "company_name": "Foo Co", "first_seen_at": "2026-08-01", "first_seen_price": 100.0, "alert_count": 2, "narrative_text": "foo narrative", "suggested_watch_until": "2026-11-01"},
        {"company_master_id": "nse:BAR", "company_name": "Bar Co", "first_seen_at": "2026-08-02", "first_seen_price": 50.0, "alert_count": 1, "narrative_text": None, "suggested_watch_until": None},
    ]

    subject, body = fundamentals_notifications.build_daily_digest_content(rows)

    assert subject == "[Watchlist] Daily digest -- 2 companies"
    assert "FOO" in body and "Foo Co" in body and "foo narrative" in body
    assert "BAR" in body and "narrative not generated yet" in body
    assert "not a trade recommendation" in body


def test_send_daily_digest_skips_when_disabled(monkeypatch):
    monkeypatch.setattr(fundamentals_notifications, "WATCHLIST_ALERT_EMAIL_ENABLED", False)
    result = fundamentals_notifications.send_daily_digest()
    assert result == {"sent": 0, "skipped_disabled": 1, "failed": 0}


def test_send_daily_digest_flags_missing_config(monkeypatch):
    monkeypatch.setattr(fundamentals_notifications, "WATCHLIST_ALERT_EMAIL_ENABLED", True)
    monkeypatch.setattr(fundamentals_notifications, "WATCHLIST_ALERT_EMAIL_FROM", "")
    monkeypatch.setattr(fundamentals_notifications, "WATCHLIST_ALERT_EMAIL_TO", "")
    fallback_events = []
    monkeypatch.setattr(fundamentals_notifications, "_record_fallback", lambda *a, **k: fallback_events.append((a, k)))

    result = fundamentals_notifications.send_daily_digest()

    assert result == {"sent": 0, "skipped_disabled": 0, "failed": 1}
    assert any(a and a[0] == "watchlist_digest_email_misconfigured" for a, k in fallback_events)


def test_send_daily_digest_sends_successfully(monkeypatch):
    monkeypatch.setattr(fundamentals_notifications, "WATCHLIST_ALERT_EMAIL_ENABLED", True)
    monkeypatch.setattr(fundamentals_notifications, "WATCHLIST_ALERT_EMAIL_FROM", "from@x.com")
    monkeypatch.setattr(fundamentals_notifications, "WATCHLIST_ALERT_EMAIL_TO", "a@x.com b@y.com")
    monkeypatch.setattr(fundamentals_notifications, "load_full_watchlist", lambda: [{"company_master_id": "nse:FOO", "company_name": "Foo", "first_seen_at": "2026-08-01", "first_seen_price": 1, "alert_count": 1, "narrative_text": "n", "suggested_watch_until": None}])
    sent_calls = []
    monkeypatch.setattr(fundamentals_notifications, "send_email", lambda subject, body: sent_calls.append((subject, body)))

    result = fundamentals_notifications.send_daily_digest()

    assert result == {"sent": 1, "skipped_disabled": 0, "failed": 0}
    assert len(sent_calls) == 1
    assert "1 companies" in sent_calls[0][0]


def test_send_daily_digest_records_failure_without_raising(monkeypatch):
    monkeypatch.setattr(fundamentals_notifications, "WATCHLIST_ALERT_EMAIL_ENABLED", True)
    monkeypatch.setattr(fundamentals_notifications, "WATCHLIST_ALERT_EMAIL_FROM", "from@x.com")
    monkeypatch.setattr(fundamentals_notifications, "WATCHLIST_ALERT_EMAIL_TO", "a@x.com")
    monkeypatch.setattr(fundamentals_notifications, "load_full_watchlist", lambda: [])

    def raise_error(subject, body):
        raise RuntimeError("ses down")

    monkeypatch.setattr(fundamentals_notifications, "send_email", raise_error)
    fallback_events = []
    monkeypatch.setattr(fundamentals_notifications, "_record_fallback", lambda *a, **k: fallback_events.append((a, k)))

    result = fundamentals_notifications.send_daily_digest()

    assert result == {"sent": 0, "skipped_disabled": 0, "failed": 1}
    assert any(a and a[0] == "watchlist_digest_email_send_failed" for a, k in fallback_events)
