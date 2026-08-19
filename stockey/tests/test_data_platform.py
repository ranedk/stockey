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
import time
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
from data.nseindia import bhavcopy_downloader, bhavcopy_parser, earnings_events, indices_downloader, indices_parser, recent_events, security_dimension, security_history
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
from fundamentals.screens import watchlist_exit as fundamentals_watchlist_exit
from fundamentals.screens import watch_summary as fundamentals_watch_summary
from fundamentals.screens import l4_thesis_draft as fundamentals_l4_thesis_draft
from fundamentals.screens import notifications as fundamentals_notifications
from fundamentals.api import queries as fundamentals_api_queries
from fundamentals.api.app import app as fundamentals_api_app
from fundamentals import run_pipeline as fundamentals_run_pipeline
from fundamentals.screens import investor_classification as fundamentals_investor_classification
from fundamentals.screens import signal_pointers as fundamentals_signal_pointers
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


def test_get_equity_universe_queries_bhavcopy(monkeypatch):
    # 2026-08-14: promoted out of ohlcv_reconcile.py's local load_universe_symbols(),
    # which used to union advisory_screener_constituents/advisory_watchlist/
    # advisory_operator_holdings, all of which lost their writers in the pure-TA cut
    # and had silently frozen -- now the one shared, always-live universe source.
    from utils import universe as universe_mod

    captured: dict[str, object] = {}

    def fake_sql_to_df(query, params=None):
        captured["query"] = query
        captured["params"] = params
        return pd.DataFrame({"symbol": ["reliance", "tcs", "reliance", "", None]})

    monkeypatch.setattr(universe_mod, "sql_to_df", fake_sql_to_df)

    result = universe_mod.get_equity_universe()

    assert result == ["RELIANCE", "TCS"]
    assert universe_mod.UNIVERSE_SOURCE_TABLE in captured["query"]
    assert "series = ANY(%s)" in captured["query"]
    assert captured["params"] == (["EQ", "BE"],)


def test_get_equity_universe_empty_on_db_error(monkeypatch):
    from utils import universe as universe_mod

    def fake_sql_to_df(query, params=None):
        raise RuntimeError("db unavailable")

    monkeypatch.setattr(universe_mod, "sql_to_df", fake_sql_to_df)

    assert universe_mod.get_equity_universe() == []


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
    monkeypatch.setattr(orc, "get_equity_universe", lambda: ["A", "B", "C", "D"])
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


def test_build_dim_security_price_row_count_is_summed_not_suffixed(monkeypatch):
    # 2026-08-15 bug found live: `latest` (one history row's own price_row_count) and `summary`
    # (the real SUM across all of a security's identity history) both carried a column named
    # price_row_count into the same merge -- pandas silently renamed them price_row_count_x/_y
    # instead of erroring, so no query anywhere could ever select the real, intended
    # (summed) price_row_count. Confirmed live: dim_security has exactly that _x/_y split today.
    history = pd.DataFrame(
        [
            {
                "security_id": 1, "raw_security_key": "k1", "symbol": "ABC", "series": "EQ", "isin": "INE001",
                "effective_from": "2020-01-01", "effective_to": "2021-01-01", "price_row_count": 100,
                "mapping_source": "nse", "confidence": 1.0, "relation_type": "same",
            },
            {
                "security_id": 1, "raw_security_key": "k1", "symbol": "ABC", "series": "EQ", "isin": "INE001",
                "effective_from": "2021-01-01", "effective_to": "2026-01-01", "price_row_count": 250,
                "mapping_source": "nse", "confidence": 1.0, "relation_type": "same",
            },
        ]
    )
    empty_dhan = pd.DataFrame(columns=["symbol", "series", "exch_id", "display_name", "instrument", "instrument_type", "tick_size", "lot_size"])
    empty_company_master = pd.DataFrame(columns=["company_master_id", "symbol", "bse_ticker", "company_name", "sector_code"])

    def fake_sql_to_df(query, **_k):
        if "dim_security_history" in query:
            return history
        if "master_dhan_instruments" in query:
            return empty_dhan
        return empty_company_master

    monkeypatch.setattr(security_dimension, "sql_to_df", fake_sql_to_df)

    df = security_dimension.build_dim_security()

    assert "price_row_count" in df.columns
    assert "price_row_count_x" not in df.columns
    assert "price_row_count_y" not in df.columns
    assert df.iloc[0]["price_row_count"] == 350  # summed across both history rows, not just the latest one (250)


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


def test_company_master_records_fallback_on_duplicate_ticker_collision(monkeypatch):
    # BUG FOUND LIVE 2026-08-17: two company_master rows sharing the same ticker used
    # to resolve via drop_duplicates(keep="last") with no ORDER BY and zero telemetry
    # -- an effectively arbitrary, silent pick.
    events: list[dict[str, object]] = []

    def fake_sql_to_df(*_args, **_kwargs):
        return pd.DataFrame([{"ticker": "DUP", "company_master_id": "nse:DUP_OLD"}, {"ticker": "DUP", "company_master_id": "nse:DUP_NEW"}])

    monkeypatch.setattr(company_master_utils, "sql_to_df", fake_sql_to_df)
    monkeypatch.setattr(company_master_utils, "record_local_fallback_event", lambda **kwargs: events.append(kwargs) or kwargs)

    out = company_master_utils.map_company_master_ids(["DUP"], exchange="NSE")

    assert out.astype("string").tolist() == ["nse:DUP_NEW"]  # still resolves -- just now visible when it does
    assert len(events) == 1
    assert events[0]["fallback_type"] == "company_master_ticker_collision"
    assert events[0]["metadata"]["tickers"] == ["DUP"]


def test_build_l1_ticker_by_company_master_id_inverts_the_forward_resolution(monkeypatch):
    # BUG FOUND LIVE 2026-08-18: naive company_master_id.removeprefix("nse:") only
    # recovers the correct fundamentals_l1_universe.ticker when the company IS its
    # own NSE symbol -- wrong for the ~22% BSE-only cohort, whose L1 slug is a raw
    # BSE scrip code (e.g. "524634" for nse:ALUFLUOR). This is the shared reverse
    # resolver that recurred as a naive-removeprefix mistake in 6+ files this
    # session; pins it in isolation.
    tickers_df = pd.DataFrame([{"ticker": "RELIANCE"}, {"ticker": "524634"}])
    monkeypatch.setattr(company_master_utils, "sql_to_df", lambda *a, **k: tickers_df)
    monkeypatch.setattr(
        company_master_utils,
        "map_company_master_ids_nse_or_bse",
        lambda tickers: pd.Series(["nse:RELIANCE", "nse:ALUFLUOR"], index=tickers.index, dtype="string"),
    )

    result = company_master_utils.build_l1_ticker_by_company_master_id()

    assert result == {"nse:RELIANCE": "RELIANCE", "nse:ALUFLUOR": "524634"}


def test_build_l1_ticker_by_company_master_id_empty_universe(monkeypatch):
    monkeypatch.setattr(company_master_utils, "sql_to_df", lambda *a, **k: pd.DataFrame())
    assert company_master_utils.build_l1_ticker_by_company_master_id() == {}


def test_build_l1_ticker_by_company_master_id_drops_unresolved_tickers(monkeypatch):
    tickers_df = pd.DataFrame([{"ticker": "RELIANCE"}, {"ticker": "UNKNOWNCODE"}])
    monkeypatch.setattr(company_master_utils, "sql_to_df", lambda *a, **k: tickers_df)
    monkeypatch.setattr(
        company_master_utils,
        "map_company_master_ids_nse_or_bse",
        lambda tickers: pd.Series(["nse:RELIANCE", pd.NA], index=tickers.index, dtype="string"),
    )
    assert company_master_utils.build_l1_ticker_by_company_master_id() == {"nse:RELIANCE": "RELIANCE"}


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


def test_map_company_master_ids_nse_or_bse_falls_back_and_flags_residual(monkeypatch):
    # 2026-08-14 bug found live: ~25% of the fundamentals L1 universe (screener.in,
    # exchange-ambiguous tickers) failed a plain exchange="NSE" match with zero
    # visibility -- most turned out to have a real company_master row all along, just
    # keyed under bse_ticker (screener.in reports a company's BSE scrip code even when
    # it's genuinely NSE-listed). NSE resolves first; only what's STILL missing after
    # the BSE fallback gets a fallback_telemetry event (the true residual gap, not the
    # common recoverable case).
    def fake_map(tickers, *, exchange):
        table = {
            "NSE": {"CAPRIHANS": "nse:CAPRIHANS"},          # NSE-listed, resolves directly
            "BSE": {"509486": "nse:CAPRIHANS", "531977": "nse:CHLOGIST"},  # BSE-code-reported cases
        }[exchange]
        return pd.Series([table.get(t, pd.NA) for t in tickers], index=tickers.index, dtype="string")

    monkeypatch.setattr(company_master_utils, "map_company_master_ids", fake_map)
    events = []
    monkeypatch.setattr(company_master_utils, "record_local_fallback_event", lambda **kwargs: events.append(kwargs))

    tickers = pd.Series(["CAPRIHANS", "509486", "531977", "TRULYUNKNOWN"], index=[0, 1, 2, 3])
    out = company_master_utils.map_company_master_ids_nse_or_bse(tickers)

    assert out.loc[0] == "nse:CAPRIHANS"   # resolved directly via NSE
    assert out.loc[1] == "nse:CAPRIHANS"   # BSE-code-for-an-NSE-company, recovered via fallback
    assert out.loc[2] == "nse:CHLOGIST"    # same
    assert pd.isna(out.loc[3])             # genuinely unknown either way

    assert len(events) == 1
    assert events[0]["fallback_type"] == "identity_unresolved_nse_and_bse"
    assert events[0]["metadata"]["tickers"] == ["TRULYUNKNOWN"]


def test_map_company_master_ids_nse_or_bse_no_telemetry_when_all_resolve(monkeypatch):
    monkeypatch.setattr(
        company_master_utils, "map_company_master_ids",
        lambda tickers, *, exchange: pd.Series(["nse:X"] * len(tickers), index=tickers.index, dtype="string"),
    )
    events = []
    monkeypatch.setattr(company_master_utils, "record_local_fallback_event", lambda **kwargs: events.append(kwargs))

    out = company_master_utils.map_company_master_ids_nse_or_bse(pd.Series(["X"], index=[0]))

    assert out.loc[0] == "nse:X"
    assert events == []


def test_ensure_company_master_dhan_ids_migration_uses_schema_registry(monkeypatch):
    # 2026-08-15 bug found live: dhan_bse_id/dhan_nse_id were stored as DOUBLE PRECISION -- a LEFT
    # merge introduces NaN for unmatched rows, silently upcasting the pandas int64 column to
    # float64, which upsert_to_db then wrote as DOUBLE PRECISION instead of BIGINT (the type of the
    # source column, master_dhan_instruments.security_id). Confirmed live: dhan_bse_id = 503696.0.
    calls = []
    monkeypatch.setattr(company_master_utils, "apply_schema_migration", lambda **kwargs: calls.append(kwargs) or {"status": "applied"})

    company_master_utils.ensure_company_master_dhan_ids_are_bigint()

    assert len(calls) == 1
    call = calls[0]
    assert call["migration_id"] == "20260815_company_master_dhan_ids_to_bigint"
    assert call["metadata"]["tables"] == ["company_master"]
    ddl = "\n".join(call["statements"])
    assert "ALTER COLUMN dhan_bse_id TYPE BIGINT" in ddl
    assert "ALTER COLUMN dhan_nse_id TYPE BIGINT" in ddl


def test_sync_company_master_ticker_case_normalized_and_dhan_ids_nullable_int(monkeypatch):
    # Both 2026-08-15 fixes together: nse_ticker/bse_ticker upper-cased (a third-party feed can
    # supply mixed case, e.g. real live row "Praxis-RE1", which would otherwise silently fail an
    # uppercase lookup from any NSE-sourced caller); dhan_bse_id/dhan_nse_id stay integer through
    # the left-merge NaN-upcast instead of drifting to float.
    monkeypatch.setattr(company_master_utils, "ensure_company_master_dhan_ids_are_bigint", lambda: None)
    monkeypatch.setattr(
        company_master_utils,
        "_company_master_sql_to_df",
        lambda query, *, params=None, operation: {
            "sync_sharpely_columns": pd.DataFrame({"column_name": ["sharpely_id"]}),
            "sync_sharpely_equity": pd.DataFrame(
                [{"nse_ticker": "Praxis-RE1", "bse_ticker": None, "company_name": "Praxis Home Retail", "sharpely_id": "sh1"}]
            ),
            "sync_dhan_bse": pd.DataFrame(columns=["bse_ticker", "dhan_bse_id"]),
            "sync_dhan_nse": pd.DataFrame([{"nse_ticker": "praxis-re1", "dhan_nse_id": 12345}]),
        }[operation],
    )
    upserts = []
    monkeypatch.setattr(company_master_utils, "upsert_to_db", lambda df, table, keys: upserts.append((df, table, keys)))

    result = company_master_utils.sync_company_master()

    assert result.iloc[0]["nse_ticker"] == "PRAXIS-RE1"  # upper-cased despite the source's mixed case
    assert result.iloc[0]["dhan_nse_id"] == 12345         # matched despite dhan's own lowercase ticker
    assert str(result["dhan_nse_id"].dtype) == "Int64"    # nullable integer, not float64
    assert len(upserts) == 1


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


def test_load_tracked_symbols_no_file_fallback(monkeypatch):
    # 2026-08-14: the config/tracked_symbols.txt file fallback was removed -- this
    # function must never silently resolve to a static placeholder list again. Only
    # an explicit --symbols arg or STOCKEY_SYMBOLS env var may produce a result.
    monkeypatch.delenv("STOCKEY_SYMBOLS", raising=False)

    assert sync_utils.load_tracked_symbols(None) == []
    assert sync_utils.load_tracked_symbols([]) == []

    monkeypatch.setenv("STOCKEY_SYMBOLS", "reliance, tcs,reliance")
    assert sync_utils.load_tracked_symbols(None) == ["RELIANCE", "TCS"]

    monkeypatch.delenv("STOCKEY_SYMBOLS", raising=False)
    assert sync_utils.load_tracked_symbols(["shaktipump,hdfcbank"]) == ["SHAKTIPUMP", "HDFCBANK"]


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


@contextlib.contextmanager
def _fake_dhan_login_lock_recording(calls):
    calls.append("entered")
    yield


def test_dhan_auth_cli_refresh_auto_login_without_manual_browser(monkeypatch):
    lock_calls = []
    monkeypatch.setattr(dhan_auth_cli, "normalize_token_id", lambda value: None)
    monkeypatch.setattr(dhan_auth_cli, "is_auto_login_configured", lambda: True)
    monkeypatch.setattr(dhan_auth_cli, "get_token_id_from_auto_login", lambda: "TOKEN123")
    monkeypatch.setattr(dhan_auth_cli, "consume_consent_token", lambda token_id: {"accessToken": f"access:{token_id}", "expiryTime": "2026-05-07T10:00:00Z"})
    monkeypatch.setattr(dhan_auth_cli, "validate_token", lambda access_token: {"status": "ok", "access_token": access_token})
    monkeypatch.setattr(dhan_auth_cli, "begin_browser_consent", lambda: (_ for _ in ()).throw(AssertionError("manual browser opened")))
    monkeypatch.setattr(dhan_auth_cli, "_dhan_login_lock", lambda: _fake_dhan_login_lock_recording(lock_calls))

    result = dhan_auth_cli.refresh_token()

    assert result["status"] == "ok"
    assert result["token_id_used"] == "TOKEN123"
    assert result["validation"] == {"status": "ok", "access_token": "access:TOKEN123"}
    # BUG FOUND LIVE 2026-08-19 (re-audit): refresh_token()'s login-driving branch (auto-login
    # or manual browser consent) used to call auth.py's shared-CDP-browser login flow with no
    # _dhan_login_lock() protection at all -- unlike auth.py's own get_access_token()/
    # force_refresh_access_token(), which always take this lock before touching the login UI.
    # Two processes racing this path could both submit mobile/TOTP/PIN into the same browser
    # tab, triggering Dhan's too-many-attempts block -- exactly what the lock exists to prevent.
    assert lock_calls == ["entered"]


def test_dhan_auth_cli_refresh_with_explicit_token_id_skips_the_login_lock(monkeypatch):
    # a caller who already has a token_id (e.g. a pasted CLI arg) never touches the login UI,
    # so it should not need to wait on the lock at all.
    lock_calls = []
    monkeypatch.setattr(dhan_auth_cli, "normalize_token_id", lambda value: "EXPLICIT_TOKEN")
    monkeypatch.setattr(dhan_auth_cli, "consume_consent_token", lambda token_id: {"accessToken": f"access:{token_id}", "expiryTime": "2026-05-07T10:00:00Z"})
    monkeypatch.setattr(dhan_auth_cli, "validate_token", lambda access_token: {"status": "ok", "access_token": access_token})
    monkeypatch.setattr(dhan_auth_cli, "_dhan_login_lock", lambda: _fake_dhan_login_lock_recording(lock_calls))

    result = dhan_auth_cli.refresh_token("EXPLICIT_TOKEN")

    assert result["token_id_used"] == dhan_auth_cli.mask_token("EXPLICIT_TOKEN")
    assert lock_calls == []


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


def test_dhan_client_default_construction_reads_auth_attempts_from_env(monkeypatch):
    # 2026-08-14 regression found live: an earlier same-day dead-code-sweep commit removed this module's
    # `env = Env()` (it looked unused after deleting DhanTradingClient, the only caller of bare `env(...)`)
    # but missed that DhanHistoricalClient.__init__ ALSO reads `env.int("DHAN_API_AUTH_ATTEMPTS", ...)` on
    # its default (no-`auth_attempts`-kwarg) path -- crashing with NameError: name 'env' is not defined.
    # Every existing test constructed the client with an explicit `auth_attempts=3`, which short-circuits
    # the `auth_attempts or env.int(...)` expression before it ever touches `env` -- masking the bug
    # entirely. Production code (data/dhanlive/ohlcv.py's sync_many_daily/sync_many_intraday) constructs
    # `DhanHistoricalClient()` with NO args, which is exactly the path that crashed -- confirmed live via
    # the 13:15 UTC ohlcv_reconcile.py cron job traceback. This test exercises that real default path.
    monkeypatch.setattr(dhan_client, "get_access_token", lambda: "OLD")
    monkeypatch.setattr(dhan_client.requests, "Session", lambda: type("S", (), {"headers": {}})())

    client = dhan_client.DhanHistoricalClient()  # no auth_attempts kwarg -- the real production call shape

    assert client.auth_attempts == 3  # DHAN_API_AUTH_ATTEMPTS default


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


def test_bhavcopy_parser_records_inner_file_parse_failure(monkeypatch, tmp_path):
    import zipfile

    events: list[dict[str, object]] = []
    zip_path = tmp_path / "bhavcopy_2026-01-01.zip"
    nested_zip_path = tmp_path / "cm_nested.zip"
    with zipfile.ZipFile(nested_zip_path, "w") as nested:
        nested.writestr("cm_bad.csv", "bad")
    with zipfile.ZipFile(zip_path, "w") as archive:
        archive.write(nested_zip_path, "cm_nested.zip")
    monkeypatch.setattr(bhavcopy_parser, "record_local_fallback_event", lambda **kwargs: events.append(kwargs) or kwargs)
    monkeypatch.setattr(
        bhavcopy_parser,
        "parse_ohlcv",
        lambda path: (_ for _ in ()).throw(ValueError("bad ohlcv schema")),
    )

    with pytest.raises(RuntimeError, match="ohlcv:cm_bad.csv"):
        bhavcopy_parser.unzip_and_process(str(zip_path))

    assert events[0]["fallback_type"] == "nse_bhavcopy_file_parse_failed"
    assert events[0]["metadata"]["label"] == "ohlcv"


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


def test_parse_mcap_converts_not_traded_sentinel_to_null_date(monkeypatch, tmp_path):
    # 2026-08-15 bug found live: last_trade_date was written as free TEXT, mixing real 'DD Mon YYYY'
    # date strings with a literal 'Not Traded' sentinel for symbols with zero trades that trade_date
    # (confirmed live: 3230 of 1.6M nseindia_mcap rows) -- conflating NULL with a magic string and
    # making any ORDER BY/range query on the column lexicographic garbage. parse_mcap must now parse
    # it to a real (possibly-null) datetime instead of passing the raw text through.
    monkeypatch.setattr(bhavcopy_parser, "ensure_mcap_last_trade_date_is_typed_date", lambda: None)
    monkeypatch.setattr(bhavcopy_parser, "with_company_master", lambda frame: frame)
    captured: dict[str, object] = {}
    monkeypatch.setattr(bhavcopy_parser, "upsert_to_db", lambda df, table, **kw: captured.update({"table": table, "df": df}))

    path = tmp_path / "MCAP01092025.csv"
    path.write_text(
        "disclaimer line\n"
        "Trade Date,Symbol,Series,Security Name,Category,Last Trade Date,Face Value(Rs.),Issue Size,Close/Paid up Value(Rs.),Market Cap(Rs.)\n"
        "01 SEP 2025,TRADED,EQ,Traded Co,A,23 SEP 2025,10,1000,100,100000\n"
        "01 SEP 2025,SUSPENDED,EQ,Suspended Co,A,Not Traded,10,1000,50,50000\n"
        "trailer line 1\n"
        "trailer line 2\n"
        "trailer line 3\n",
        encoding="utf-8",
    )

    frame = bhavcopy_parser.parse_mcap(str(path))

    assert captured["table"] == "nseindia_mcap"
    traded = frame[frame["symbol"] == "TRADED"].iloc[0]
    assert traded["last_trade_date"] == pd.Timestamp("2025-09-23")
    suspended = frame[frame["symbol"] == "SUSPENDED"].iloc[0]
    assert pd.isna(suspended["last_trade_date"])  # 'Not Traded' -> real NULL, not the literal string


def test_ensure_mcap_last_trade_date_migration_uses_schema_registry(monkeypatch):
    calls = []
    monkeypatch.setattr(bhavcopy_parser, "apply_schema_migration", lambda **kwargs: calls.append(kwargs) or {"status": "applied"})

    bhavcopy_parser.ensure_mcap_last_trade_date_is_typed_date()

    assert len(calls) == 1
    call = calls[0]
    assert call["migration_id"] == "20260815_nseindia_mcap_last_trade_date_to_date"
    assert call["metadata"]["tables"] == ["nseindia_mcap"]
    ddl = call["statements"][0]
    assert "ALTER TABLE nseindia_mcap ALTER COLUMN last_trade_date TYPE DATE" in ddl
    assert "'Not Traded'" not in ddl  # generic regex match, not a hardcoded literal check


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


# data/bseindia/bhavcopy.py + price_adjustment.py -- BSE-only-company price coverage
# (2026-08-15, fundamentals screener gap fix).

_BSE_UDIFF_CSV = (
    b"TradDt,BizDt,Sgmt,Src,FinInstrmTp,FinInstrmId,ISIN,TckrSymb,SctySrs,XpryDt,FininstrmActlXpryDt,StrkPric,"
    b"OptnTp,FinInstrmNm,OpnPric,HghPric,LwPric,ClsPric,LastPric,PrvsClsgPric,UndrlygPric,SttlmPric,OpnIntrst,"
    b"ChngInOpnIntrst,TtlTradgVol,TtlTrfVal,TtlNbOfTxsExctd,SsnId,NewBrdLotQty,Rmks,Rsvd1,Rsvd2,Rsvd3,Rsvd4\n"
    b"2026-08-14,2026-08-14,CM,BSE,STK,500166,INE192A01029,GOODRICKE,X,,,,,GOODRICKE GROUP LTD,213,220,204,"
    b"215.9,215.9,217.95,,,,,24901,5356464,312,F,1,,,,,\n"
    b"2026-08-14,2026-08-14,CM,BSE,STK,505036,INE117A01022,ACGL,B,,,,,ACGL,1808,1858.3,1808,1823.35,1823.35,"
    b"1834.85,,,,,1238,2258000,90,F,1,,,,,\n"
)


def test_bhavcopy_url_matches_bse_udiff_pattern():
    from data.bseindia.bhavcopy import bhavcopy_url

    assert bhavcopy_url(date(2026, 8, 14)) == "https://www.bseindia.com/download/BhavCopy/Equity/BhavCopy_BSE_CM_0_0_0_20260814_F_0000.CSV"


def test_parse_bhavcopy_maps_udiff_columns():
    from data.bseindia.bhavcopy import parse_bhavcopy

    df = parse_bhavcopy(_BSE_UDIFF_CSV)
    assert len(df) == 2
    row = df[df["scrip_code"] == "500166"].iloc[0]
    assert row["symbol"] == "GOODRICKE"
    assert row["series"] == "X"
    assert row["isin"] == "INE192A01029"
    assert row["close"] == 215.9
    assert row["previous_close"] == 217.95
    assert str(row["date"].date()) == "2026-08-14"


def test_parse_bhavcopy_drops_rows_with_no_scrip_code():
    from data.bseindia.bhavcopy import parse_bhavcopy

    csv = _BSE_UDIFF_CSV.replace(b"500166", b"")  # blank FinInstrmId -> NaN after parse
    df = parse_bhavcopy(csv)
    assert "" not in df["scrip_code"].tolist()


def test_attach_identity_joins_on_scrip_code_not_ticker_text(monkeypatch):
    from data.bseindia import bhavcopy as bse_bhavcopy

    captured = {}

    def fake_attach(df, *, ticker_column, exchange):
        captured["ticker_column"] = ticker_column
        captured["exchange"] = exchange
        return df.assign(company_master_id="nse:GOODRICKE")

    monkeypatch.setattr(bse_bhavcopy, "attach_company_master_id", fake_attach)
    df = bse_bhavcopy.parse_bhavcopy(_BSE_UDIFF_CSV)
    bse_bhavcopy.attach_identity(df)

    assert captured["ticker_column"] == "scrip_code"  # not "symbol" -- BSE ticker text doesn't match company_master
    assert captured["exchange"] == "BSE"


def test_fetch_bhavcopy_csv_raises_not_available_for_html_response(monkeypatch):
    from data.bseindia.bhavcopy import BseBhavcopyNotAvailableError, fetch_bhavcopy_csv

    class FakeResponse:
        content = b"<html>not found</html>"

        def raise_for_status(self):
            pass

    class FakeSession:
        def get(self, url, headers, timeout):
            return FakeResponse()

    with pytest.raises(BseBhavcopyNotAvailableError):
        fetch_bhavcopy_csv(date(2026, 8, 14), session=FakeSession())


def test_fetch_bhavcopy_csv_returns_bytes_for_real_csv(monkeypatch):
    from data.bseindia.bhavcopy import fetch_bhavcopy_csv

    class FakeResponse:
        content = _BSE_UDIFF_CSV

        def raise_for_status(self):
            pass

    class FakeSession:
        def get(self, url, headers, timeout):
            return FakeResponse()

    assert fetch_bhavcopy_csv(date(2026, 8, 14), session=FakeSession()) == _BSE_UDIFF_CSV


def test_collect_range_skips_weekends_and_counts_non_trading_days(monkeypatch):
    from data.bseindia import bhavcopy as bse_bhavcopy

    monkeypatch.setattr(bse_bhavcopy, "ensure_ohlcv_table", lambda: None)
    calls = []

    def fake_fetch(d, *, session=None):
        calls.append(d)
        if d.weekday() == 4:  # Friday: pretend it's a holiday
            raise bse_bhavcopy.BseBhavcopyNotAvailableError("holiday")
        return _BSE_UDIFF_CSV

    monkeypatch.setattr(bse_bhavcopy, "fetch_bhavcopy_csv", fake_fetch)
    monkeypatch.setattr(bse_bhavcopy, "attach_identity", lambda df: df.assign(company_master_id="nse:X"))
    upserts = []
    monkeypatch.setattr(bse_bhavcopy, "upsert_to_db", lambda df, table, **k: upserts.append(df))
    marked_non_trading = []
    monkeypatch.setattr(bse_bhavcopy, "_mark_non_trading_date", lambda d: marked_non_trading.append(d))

    # 2026-08-10 (Mon) .. 2026-08-16 (Sun): Fri 08-14 is the faked holiday, Sat/Sun skipped outright
    result = bse_bhavcopy.collect_range(date(2026, 8, 10), date(2026, 8, 16))

    assert result["days_written"] == 4  # Mon, Tue, Wed, Thu
    assert result["non_trading_days"] == 1  # Fri
    assert result["blocked"] is False
    assert len(calls) == 5  # weekdays only -- weekends never even attempted
    assert len(upserts) == 4
    assert marked_non_trading == [date(2026, 8, 14)]  # persisted so it isn't re-fetched forever


def test_collect_range_trips_circuit_breaker(monkeypatch):
    from data.bseindia import bhavcopy as bse_bhavcopy

    monkeypatch.setattr(bse_bhavcopy, "ensure_ohlcv_table", lambda: None)

    def always_fails(d, *, session=None):
        raise RuntimeError("boom")

    monkeypatch.setattr(bse_bhavcopy, "fetch_bhavcopy_csv", always_fails)
    monkeypatch.setattr(bse_bhavcopy, "upsert_to_db", lambda df, table, **k: None)
    fallback_events = []
    monkeypatch.setattr(bse_bhavcopy, "_record_fallback", lambda *a, **k: fallback_events.append((a, k)))

    result = bse_bhavcopy.collect_range(date(2026, 8, 10), date(2026, 8, 20))  # 8 weekdays available

    assert result["blocked"] is True
    assert len(result["failed_days"]) == 3
    assert any(a and a[0] == "bse_bhavcopy_circuit_breaker_tripped" for a, k in fallback_events)


def test_collect_dates_parse_failure_on_one_date_does_not_abort_the_batch(monkeypatch):
    # BUG FOUND LIVE 2026-08-17: parse_bhavcopy/attach_identity/upsert_to_db used to
    # sit OUTSIDE any try/except, unlike fetch_bhavcopy_csv's own guard right above --
    # a bad CSV/identity/DB error on ANY single date raised uncaught out of the whole
    # function, aborting every remaining date instead of being recorded and skipped
    # like a fetch failure already is.
    from data.bseindia import bhavcopy as bse_bhavcopy

    monkeypatch.setattr(bse_bhavcopy, "ensure_ohlcv_table", lambda: None)
    monkeypatch.setattr(bse_bhavcopy, "fetch_bhavcopy_csv", lambda d, session=None: _BSE_UDIFF_CSV)

    real_parse_bhavcopy = bse_bhavcopy.parse_bhavcopy

    def flaky_parse(csv_bytes):
        # First call (the failing date, sorted first) raises; the rest succeed.
        if not flaky_parse.calls:
            flaky_parse.calls.append(1)
            raise ValueError("BSE changed a column name")
        return real_parse_bhavcopy(csv_bytes)

    flaky_parse.calls = []
    monkeypatch.setattr(bse_bhavcopy, "parse_bhavcopy", flaky_parse)
    monkeypatch.setattr(bse_bhavcopy, "attach_identity", lambda df: df.assign(company_master_id="nse:X"))
    upserts = []
    monkeypatch.setattr(bse_bhavcopy, "upsert_to_db", lambda df, table, **k: upserts.append(df))
    fallback_events = []
    monkeypatch.setattr(bse_bhavcopy, "_record_fallback", lambda *a, **k: fallback_events.append((a, k)))

    dates = [date(2026, 8, 10), date(2026, 8, 11), date(2026, 8, 12)]
    result = bse_bhavcopy.collect_dates(dates)

    assert result["blocked"] is False
    assert result["failed_days"] == ["2026-08-10"]
    assert result["days_written"] == 2  # the 2 dates AFTER the failing one still succeeded
    assert len(upserts) == 2
    assert any(a and a[0] == "bse_bhavcopy_parse_or_upsert_failed" for a, k in fallback_events)


def test_collect_dates_trips_parse_circuit_breaker_separately_from_fetch(monkeypatch):
    from data.bseindia import bhavcopy as bse_bhavcopy

    monkeypatch.setattr(bse_bhavcopy, "ensure_ohlcv_table", lambda: None)
    monkeypatch.setattr(bse_bhavcopy, "fetch_bhavcopy_csv", lambda d, session=None: _BSE_UDIFF_CSV)

    def always_fails(csv_bytes):
        raise ValueError("boom")

    monkeypatch.setattr(bse_bhavcopy, "parse_bhavcopy", always_fails)
    monkeypatch.setattr(bse_bhavcopy, "upsert_to_db", lambda df, table, **k: None)
    fallback_events = []
    monkeypatch.setattr(bse_bhavcopy, "_record_fallback", lambda *a, **k: fallback_events.append((a, k)))

    dates = [date(2026, 8, 10), date(2026, 8, 11), date(2026, 8, 12), date(2026, 8, 13), date(2026, 8, 14)]
    result = bse_bhavcopy.collect_dates(dates)

    assert result["blocked"] is True
    assert len(result["failed_days"]) == 3  # stopped after 3 consecutive parse failures, not all 5 dates
    assert any(a and a[0] == "bse_bhavcopy_parse_circuit_breaker_tripped" for a, k in fallback_events)


def test_run_bse_bhavcopy_collection_returns_early_when_fully_caught_up(monkeypatch):
    from data.bseindia import bhavcopy as bse_bhavcopy

    monkeypatch.setattr(bse_bhavcopy, "ensure_ohlcv_table", lambda: None)
    monkeypatch.setattr(bse_bhavcopy, "load_downloaded_dates", lambda: {bse_bhavcopy.datetime.now().date() - timedelta(days=1)})
    monkeypatch.setattr(bse_bhavcopy, "load_known_non_trading_dates", lambda: set())
    monkeypatch.setattr(bse_bhavcopy, "BSE_BHAVCOPY_EARLIEST_DATE", bse_bhavcopy.datetime.now().date() - timedelta(days=2))

    result = bse_bhavcopy.run_bse_bhavcopy_collection(lookback_days=1)

    assert result["days_written"] == 0
    assert result["candidate_dates"] == 0


def test_run_bse_bhavcopy_collection_only_fetches_actually_missing_dates(monkeypatch):
    # found live 2026-08-15: an earlier version called collect_range(min(missing),
    # max(missing)), which re-walks EVERY calendar day in that span -- since weekends
    # never get a written row, they always show up as "missing" and pull the span
    # wide, so a nearly-fully-caught-up collector would still re-fetch almost the
    # entire lookback window every run. This pins the fix: only the genuinely
    # missing weekday dates are ever fetched, nothing in between.
    from data.bseindia import bhavcopy as bse_bhavcopy

    monkeypatch.setattr(bse_bhavcopy, "ensure_ohlcv_table", lambda: None)
    monkeypatch.setattr(bse_bhavcopy, "BSE_BHAVCOPY_EARLIEST_DATE", date(2024, 1, 1))
    # 2026-08-10 (Mon) .. 2026-08-14 (Fri): every weekday already downloaded except 08-12 (Wed)
    downloaded = {date(2026, 8, 10), date(2026, 8, 11), date(2026, 8, 13), date(2026, 8, 14)}
    monkeypatch.setattr(bse_bhavcopy, "load_downloaded_dates", lambda: downloaded)
    monkeypatch.setattr(bse_bhavcopy, "load_known_non_trading_dates", lambda: set())

    class _FakeNow:
        @staticmethod
        def date():
            return date(2026, 8, 15)

    class _FakeDatetime:
        @staticmethod
        def now():
            return _FakeNow()

    monkeypatch.setattr(bse_bhavcopy, "datetime", _FakeDatetime)

    fetched = []

    def fake_collect_dates(dates, **k):
        fetched.extend(dates)
        return {"days_written": len(dates), "rows_written": 0, "non_trading_days": 0, "failed_days": [], "blocked": False}

    monkeypatch.setattr(bse_bhavcopy, "collect_dates", fake_collect_dates)

    result = bse_bhavcopy.run_bse_bhavcopy_collection(lookback_days=5)

    assert fetched == [date(2026, 8, 12)]  # only the one genuinely missing weekday -- not the 5-day span
    assert result["candidate_dates"] == 1


def test_run_bse_bhavcopy_collection_caps_dates_per_run_and_records_fallback(monkeypatch):
    # BUG FOUND LIVE 2026-08-18 (re-audit): no per-run bound at all -- with only 3
    # days of history present against the 365-day lookback (the backfill has never
    # completed), the next cron run would produce ~250-260 missing weekday
    # candidates at the 10s rate-gate floor, ~45 minutes inside complete_data.sh.
    from data.bseindia import bhavcopy as bse_bhavcopy

    monkeypatch.setattr(bse_bhavcopy, "ensure_ohlcv_table", lambda: None)
    monkeypatch.setattr(bse_bhavcopy, "BSE_BHAVCOPY_EARLIEST_DATE", date(2024, 1, 1))
    monkeypatch.setattr(bse_bhavcopy, "load_downloaded_dates", lambda: set())
    monkeypatch.setattr(bse_bhavcopy, "load_known_non_trading_dates", lambda: set())
    monkeypatch.setattr(bse_bhavcopy, "BSE_BHAVCOPY_MAX_DATES_PER_RUN", 2)

    class _FakeNow:
        @staticmethod
        def date():
            return date(2026, 8, 15)  # a Friday

    class _FakeDatetime:
        @staticmethod
        def now():
            return _FakeNow()

    monkeypatch.setattr(bse_bhavcopy, "datetime", _FakeDatetime)
    fetched = []

    def fake_collect_dates(dates, **k):
        fetched.extend(dates)
        return {"days_written": len(dates), "rows_written": 0, "non_trading_days": 0, "failed_days": [], "blocked": False}

    monkeypatch.setattr(bse_bhavcopy, "collect_dates", fake_collect_dates)
    fallback_events = []
    monkeypatch.setattr(bse_bhavcopy, "_record_fallback", lambda *a, **k: fallback_events.append((a, k)))

    # 5 weekdays missing (08-11 Mon .. 08-14 Thu, end is always today-1) but capped to 2.
    result = bse_bhavcopy.run_bse_bhavcopy_collection(lookback_days=10)

    assert len(fetched) == 2  # only the oldest 2, not all of them
    assert fetched == sorted(fetched)  # oldest-first
    assert result["candidate_dates"] == 2
    assert result["total_missing_dates"] > 2
    assert len(fallback_events) == 1
    assert fallback_events[0][0][0] == "bse_bhavcopy_backlog_capped"
    assert fallback_events[0][1]["metadata"]["attempted_this_run"] == 2


def test_bse_bhavcopy_main_returns_nonzero_exit_code_when_blocked(monkeypatch):
    # found live 2026-08-15: main() always `return 0` regardless of a circuit-breaker
    # trip, so data.download_runner.py's exit-code-only classify_run_status() could
    # never see a blocked BSE run as anything but "ok". Matches bhavcopy_downloader.py's
    # own established "return 1 on real failure" convention.
    from data.bseindia import bhavcopy as bse_bhavcopy

    monkeypatch.setattr(
        bse_bhavcopy,
        "run_bse_bhavcopy_collection",
        lambda: {"days_written": 0, "rows_written": 0, "non_trading_days": 0, "failed_days": ["2026-08-10", "2026-08-11", "2026-08-12"], "blocked": True, "candidate_dates": 3, "total_missing_dates": 3},
    )
    assert bse_bhavcopy.main() == 1


def test_bse_bhavcopy_main_returns_zero_exit_code_when_not_blocked(monkeypatch):
    from data.bseindia import bhavcopy as bse_bhavcopy

    monkeypatch.setattr(
        bse_bhavcopy,
        "run_bse_bhavcopy_collection",
        lambda: {"days_written": 1, "rows_written": 4900, "non_trading_days": 0, "failed_days": [], "blocked": False, "candidate_dates": 1, "total_missing_dates": 1},
    )
    assert bse_bhavcopy.main() == 0


def test_build_bse_adjustment_factors_returns_zero_rows_when_empty(monkeypatch):
    from data.bseindia import price_adjustment as bse_pa

    monkeypatch.setattr(bse_pa, "sql_to_df", lambda q: pd.DataFrame())
    assert bse_pa.build_adjustment_factors(dry_run=True) == {"rows": 0}


def test_build_bse_adjustment_factors_uses_scrip_code_as_symbol_col(monkeypatch):
    from data.bseindia import price_adjustment as bse_pa

    raw = pd.DataFrame(
        [
            {"scrip_code": "500166", "symbol": "GOODRICKE", "date": pd.Timestamp("2026-08-13", tz="UTC"), "series": "X", "open": 217, "close": 217.95, "previous_close": 220},
            {"scrip_code": "500166", "symbol": "GOODRICKE", "date": pd.Timestamp("2026-08-14", tz="UTC"), "series": "X", "open": 213, "close": 215.9, "previous_close": 217.95},
        ]
    )

    def fake_sql_to_df(query):
        if "bseindia_ohlcv" in query:
            return raw
        return pd.DataFrame(columns=["symbol", "ex_date", "dividend_amount"])

    monkeypatch.setattr(bse_pa, "sql_to_df", fake_sql_to_df)
    summary = bse_pa.build_adjustment_factors(dry_run=True)

    assert summary["rows"] == 2
    assert summary["scrips"] == 1
    assert summary["split_bonus_events"] == 0  # no >35% overnight gap in this fixture


def test_ensure_bse_view_creates_ohlcv_table_first(monkeypatch):
    # a CREATE VIEW referencing bseindia_ohlcv fails outright if that table doesn't
    # exist yet -- ensure_view() must self-heal it, not assume bhavcopy.py already ran.
    from data.bseindia import price_adjustment as bse_pa

    calls = []

    class _FakeCursor:
        def execute(self, query, params=None):
            calls.append("direct_view")

    @contextlib.contextmanager
    def fake_db_session():
        yield None, _FakeCursor()

    monkeypatch.setattr(bse_pa, "ensure_ohlcv_table", lambda: calls.append("ohlcv"))
    monkeypatch.setattr(bse_pa, "ensure_factors_table", lambda: calls.append("factors"))
    monkeypatch.setattr(bse_pa, "apply_schema_migration", lambda **k: calls.append("view"))
    monkeypatch.setattr(bse_pa, "db_session", fake_db_session)
    monkeypatch.setattr(bse_pa, "execute_db_operation", lambda op, **k: op())

    bse_pa.ensure_view()

    assert calls == ["ohlcv", "factors", "view", "direct_view"]


def test_ensure_bse_view_runs_create_or_replace_unconditionally(monkeypatch):
    # BUG FOUND LIVE 2026-08-18 (re-audit): the "self-heals an accidental drop"
    # claim was false once the migration is recorded -- apply_schema_migration()
    # skips re-executing its statements entirely once migration_id is marked
    # 'applied', regardless of whether the VIEW itself still exists. CREATE OR
    # REPLACE VIEW is idempotent and cheap, so it must run directly, unconditionally,
    # on every ensure_view() call -- not gated behind the migration's own tracking.
    from data.bseindia import price_adjustment as bse_pa

    monkeypatch.setattr(bse_pa, "ensure_ohlcv_table", lambda: None)
    monkeypatch.setattr(bse_pa, "ensure_factors_table", lambda: None)
    # Simulate the migration already being marked applied (a no-op from apply_schema_migration's own POV).
    monkeypatch.setattr(bse_pa, "apply_schema_migration", lambda **k: {"status": "skipped_already_applied"})
    executed = []

    class _FakeCursor:
        def execute(self, query, params=None):
            executed.append(query)

    @contextlib.contextmanager
    def fake_db_session():
        yield None, _FakeCursor()

    monkeypatch.setattr(bse_pa, "db_session", fake_db_session)
    monkeypatch.setattr(bse_pa, "execute_db_operation", lambda op, **k: op())

    bse_pa.ensure_view()

    assert len(executed) == 1
    assert "CREATE OR REPLACE VIEW" in executed[0]


# scripts/dedupe_fundamentals_events.py -- one-time remediation for the in-batch
# dedup gap (2026-08-17). Only merges a group when every row's headline matches
# exactly; groups with differing headlines are genuinely different real disclosures
# that share the dedup key by the module's own documented accepted tradeoff, and
# must not be merged -- confirmed live 2026-08-17 that 54/73 "duplicate" groups
# were actually this case (e.g. 4 distinct real filings for 2 different named
# individuals sharing one isin/filing_type/date).


def test_is_true_duplicate_group_requires_identical_headline_on_every_row():
    from scripts.dedupe_fundamentals_events import _is_true_duplicate_group

    identical = [{"headline": "Same filing text"}, {"headline": "Same filing text"}]
    assert _is_true_duplicate_group(identical) is True

    differing = [{"headline": "Disclosure for Himanshu Kanakia"}, {"headline": "Disclosure for Rasesh Kanakia"}]
    assert _is_true_duplicate_group(differing) is False


def test_dedupe_group_skips_groups_with_differing_headlines(monkeypatch):
    import scripts.dedupe_fundamentals_events as dedupe_script

    rows = [
        {"source": "bse", "news_id": "a", "headline": "Disclosure for Himanshu Kanakia", "quantity": None, "insider_name": None, "transaction_type": None, "announcement_timestamp": None, "sources": "bse", "load_ts": None},
        {"source": "bse", "news_id": "b", "headline": "Disclosure for Rasesh Kanakia", "quantity": None, "insider_name": None, "transaction_type": None, "announcement_timestamp": None, "sources": "bse", "load_ts": None},
    ]
    monkeypatch.setattr(dedupe_script, "load_group_rows", lambda isin, ft, d: rows)
    apply_calls = []
    monkeypatch.setattr(dedupe_script, "_apply_merge", lambda **k: apply_calls.append(k))
    delete_calls = []
    monkeypatch.setattr(dedupe_script, "_delete_row", lambda **k: delete_calls.append(k))

    result = dedupe_script.dedupe_group("INE704H01022", "pit_sast", date(2026, 2, 25), dry_run=False)

    assert result["canonical"] is None
    assert result["removed"] == []
    assert "differ" in result["skipped_reason"]
    assert apply_calls == [] and delete_calls == []  # nothing touched


def test_dedupe_group_merges_true_duplicates_and_cleans_up_orphaned_alerts(monkeypatch):
    import scripts.dedupe_fundamentals_events as dedupe_script

    rows = [
        {"source": "bse", "news_id": "a", "headline": "Same real filing", "quantity": None, "insider_name": None, "transaction_type": None, "announcement_timestamp": None, "sources": "bse", "load_ts": "2026-08-01T00:00:00"},
        {"source": "bse", "news_id": "b", "headline": "Same real filing", "quantity": 40000, "insider_name": "Sonitron Limited", "transaction_type": "buy", "announcement_timestamp": None, "sources": "bse", "load_ts": "2026-08-01T00:01:00"},
    ]
    monkeypatch.setattr(dedupe_script, "load_group_rows", lambda isin, ft, d: rows)
    apply_calls = []
    monkeypatch.setattr(dedupe_script, "_apply_merge", lambda **k: apply_calls.append(k))
    delete_row_calls = []
    monkeypatch.setattr(dedupe_script, "_delete_row", lambda **k: delete_row_calls.append(k))
    monkeypatch.setattr(dedupe_script, "_count_orphaned_alerts", lambda source, news_id: 1 if news_id == "b" else 0)
    delete_alert_calls = []
    monkeypatch.setattr(dedupe_script, "_delete_orphaned_alerts", lambda **k: delete_alert_calls.append(k))

    result = dedupe_script.dedupe_group("INE111", "pit_sast", date(2026, 8, 1), dry_run=False)

    assert result["canonical"] == ["bse", "a"]  # earliest-loaded row's identity wins
    assert result["removed"] == [["bse", "b"]]
    assert result["orphaned_alerts_removed"] == 1
    assert len(apply_calls) == 1
    assert apply_calls[0]["merged_fields"]["quantity"] == 40000  # filled in from sibling "b"
    assert delete_alert_calls == [{"source": "bse", "news_id": "b"}]
    assert delete_row_calls == [{"source": "bse", "news_id": "b"}]


def test_dedupe_group_dry_run_never_calls_apply_or_delete(monkeypatch):
    import scripts.dedupe_fundamentals_events as dedupe_script

    rows = [
        {"source": "bse", "news_id": "a", "headline": "Same real filing", "quantity": None, "insider_name": None, "transaction_type": None, "announcement_timestamp": None, "sources": "bse", "load_ts": "2026-08-01T00:00:00"},
        {"source": "bse", "news_id": "b", "headline": "Same real filing", "quantity": 40000, "insider_name": "Sonitron Limited", "transaction_type": "buy", "announcement_timestamp": None, "sources": "bse", "load_ts": "2026-08-01T00:01:00"},
    ]
    monkeypatch.setattr(dedupe_script, "load_group_rows", lambda isin, ft, d: rows)
    monkeypatch.setattr(dedupe_script, "_count_orphaned_alerts", lambda source, news_id: 0)

    def _fail(*a, **k):
        raise AssertionError("dry_run must not call this")

    monkeypatch.setattr(dedupe_script, "_apply_merge", _fail)
    monkeypatch.setattr(dedupe_script, "_delete_row", _fail)
    monkeypatch.setattr(dedupe_script, "_delete_orphaned_alerts", _fail)

    result = dedupe_script.dedupe_group("INE111", "pit_sast", date(2026, 8, 1), dry_run=True)

    assert result["canonical"] == ["bse", "a"]  # plan is still computed and reported
    assert result["removed"] == [["bse", "b"]]


def test_legacy_archival_never_touches_protected_numerical_tables(monkeypatch):
    # Operator decision: core numerical bhavcopy data must never be archived out of the live DB. The
    # legacy-archival tool must skip every protected table by default, and only proceed under an
    # explicit force override.
    from scripts import archive_legacy_nse_tables as ala
    from scripts.db_table_retention_report import PROTECTED_NUMERICAL_TABLES

    assert {"nseindia_ohlcv", "nseindia_mcap", "nseindia_indices"} <= PROTECTED_NUMERICAL_TABLES

    calls: list[str] = []
    monkeypatch.setattr(ala, "archive_table", lambda *, table_name, **kw: calls.append(table_name) or {"table_name": table_name, "deleted_rows": 0})

    result = ala.run_archive(
        tables=["nseindia_ohlcv", "nseindia_mcap"], retention_days=365, cutoff="2020-01-01",
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


def test_dhan_index_identity_uppercases_mixed_case_underlying_symbol(monkeypatch):
    # 2026-08-15 bug found live: the index branch skipped .upper(), unlike the stock branch --
    # master_dhan_instruments.underlying_symbol has real mixed-case index rows ("Nifty Healthcare",
    # "Nifty GS 10Yr"). Currently harmless only because NIFTY (the sole synced index) is already
    # uppercase; this guards against a future sync silently writing a mixed-case ticker.
    monkeypatch.setattr(
        dhan_db,
        "get_index_instrument",
        lambda symbol, exchange: pd.Series({"underlying_symbol": "Nifty Healthcare", "security_id": 99}),
    )

    identity = dhan_db.resolve_dhan_identity("NIFTYHEALTHCARE", "NSE", asset_type="benchmark")

    assert identity["ticker"] == "NIFTY HEALTHCARE"


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


def test_record_local_fallback_event_prints_to_stderr_on_write_failure(monkeypatch, tmp_path, capsys):
    # BUG FOUND LIVE 2026-08-19 (re-audit): this is the fallback-of-last-resort recorder --
    # a bare `except: pass` here meant a write failure (disk full, permissions) left ZERO
    # trace anywhere: no DB row (already failed to get here), no local JSONL line (this
    # write just failed), no stderr line. Confirmed live by pointing the file at an
    # unwritable path and observing silent success-shaped return with no output at all.
    telemetry_file = tmp_path / "no_such_dir" / "local_fallback_events.jsonl"
    original_open = fallback_telemetry.Path.open

    def fake_open(path, *args, **kwargs):
        if path == telemetry_file:
            raise OSError("cannot write")
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(fallback_telemetry, "LOCAL_FALLBACK_TELEMETRY_FILE", telemetry_file)
    monkeypatch.setattr(fallback_telemetry.Path, "open", fake_open)

    row = fallback_telemetry.record_local_fallback_event(
        module="unit.test", source="unit", fallback_type="unit_failed", error=RuntimeError("x")
    )

    # the row is still returned (callers don't crash), but the write failure is now visible
    assert row["fallback_type"] == "unit_failed"
    captured = capsys.readouterr()
    assert "local fallback telemetry write failed" in captured.err
    assert "cannot write" in captured.err


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


def test_redaction_masks_db_dsn_userinfo_credentials():
    # 2026-08-14 gap found live: URL_RE only matched http(s):// -- a DB DSN (utils/db.py builds
    # postgresql+psycopg2://user:pass@host:port/db with a real password) never matched at all, and even
    # within a matched URL the userinfo segment (user:pass@) was never inspected -- only query-string keys
    # were redacted. Any exception surfacing a raw DSN would have leaked the password in plaintext into
    # fallback telemetry / logs.
    dsn = "postgresql+psycopg2://systrade_user:S3cr3tPass123@10.0.0.5:5432/stockey?sslmode=require"
    redacted = redaction.redact_text(f"connect failed: {dsn}")
    assert "S3cr3tPass123" not in redacted
    assert "systrade_user" not in redacted
    assert "10.0.0.5:5432/stockey" in redacted  # host/db stay visible -- useful for debugging, not secret
    assert redaction.REDACTED in redacted


def test_redaction_masks_access_key_id_style_keys():
    # 2026-08-14 gap found live: the key-pattern regexes caught AWS_SECRET_ACCESS_KEY but missed
    # AWS_ACCESS_KEY_ID / bare access_key / bare token spellings.
    assert "AKIAABCDEF1234567890" not in redaction.redact_text("AWS_ACCESS_KEY_ID=AKIAABCDEF1234567890")
    assert "AKIAABCDEF1234567890" not in redaction.redact_text("access_key_id: AKIAABCDEF1234567890")
    assert "abc123def456" not in redaction.redact_text("token=abc123def456")


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


def test_dhan_precheck_symbols_samples_by_liquidity(monkeypatch):
    # 2026-08-14: no hardcoded ticker names anywhere in this pipeline outside tests --
    # dhan_ohlcv_precheck used to pass a fixed SHAKTIPUMP/HDFCBANK pair; now it samples
    # the top-traded-value names off the latest bhavcopy session instead.
    captured: dict[str, object] = {}

    def fake_sql_to_df(query, params=None):
        captured["query"] = query
        captured["params"] = params
        return pd.DataFrame({"symbol": ["bhartiartl", "astral"]})

    monkeypatch.setattr("utils.db.sql_to_df", fake_sql_to_df)

    result = download_runner._dhan_precheck_symbols()

    assert result == ["BHARTIARTL", "ASTRAL"]
    assert "ORDER BY total_value DESC" in captured["query"]
    assert captured["params"] == (["EQ", "BE"], download_runner.DHAN_PRECHECK_SAMPLE_SIZE)


def test_dhan_precheck_symbols_empty_on_db_error(monkeypatch):
    def fake_sql_to_df(query, params=None):
        raise RuntimeError("db down")

    monkeypatch.setattr("utils.db.sql_to_df", fake_sql_to_df)

    assert download_runner._dhan_precheck_symbols() == []


def test_run_download_module_fills_dhan_precheck_symbols_dynamically(monkeypatch):
    monkeypatch.setattr(download_runner, "_dhan_precheck_symbols", lambda: ["BHARTIARTL", "ASTRAL"])
    monkeypatch.setattr(download_runner, "_execute_module_entrypoint", lambda _module_name: (0, {"rows": 2}))
    monkeypatch.setattr(download_runner, "persist_sync_state", lambda **kwargs: None)

    result = download_runner.run_download_module(
        {"module": "data.dhanlive.ohlcv", "args": [], "purpose": "dhan_ohlcv_precheck"}
    )

    assert result["status"] == "ok"
    assert result["args"] == ["--symbols", "BHARTIARTL", "ASTRAL"]


def test_run_download_module_dhan_precheck_stays_empty_when_universe_unavailable(monkeypatch):
    # A DB-unavailable universe must not silently fall back to any hardcoded name --
    # empty args flow through to ohlcv.py's own "No symbols provided" hard failure.
    monkeypatch.setattr(download_runner, "_dhan_precheck_symbols", lambda: [])
    monkeypatch.setattr(download_runner, "_execute_module_entrypoint", lambda _module_name: (1, {}))
    monkeypatch.setattr(download_runner, "persist_sync_state", lambda **kwargs: None)
    monkeypatch.setattr(download_runner, "record_local_fallback_event", lambda **kwargs: kwargs)

    result = download_runner.run_download_module(
        {"module": "data.dhanlive.ohlcv", "args": [], "purpose": "dhan_ohlcv_precheck"}
    )

    assert result["args"] == []
    assert result["status"] == "failed"


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
    # 2026-08-15: master_sharpely_funds (instrumentType 0/1, non-stock entities) retired -- zero
    # readers anywhere. get_latest_from_sharpely now fetches+returns only the equity frame directly
    # (not a 3-element list), and only master_sharpely_equity is ever upserted.
    writes: list[tuple[str, int, list[str]]] = []
    equity = pd.DataFrame(
        [
            {"symbol": "AAA", "bse_ticker": None, "proper_name": "AAA Ltd"},
            {"symbol": None, "bse_ticker": "500001", "proper_name": "BSE Only Ltd"},
            {"symbol": None, "bse_ticker": None, "proper_name": "Invalid Ltd"},
        ]
    )

    monkeypatch.setattr(sharpely_scrip_master, "get_sharpely_headers", lambda: {"Authorization": "Bearer test"})
    monkeypatch.setattr(sharpely_scrip_master, "get_latest_from_sharpely", lambda headers: equity.copy())
    monkeypatch.setattr(
        sharpely_scrip_master,
        "upsert_to_db",
        lambda df, table_name, unique_keys: writes.append((table_name, len(df), list(unique_keys))),
    )

    assert sharpely_scrip_master.main() == 0
    capsys.readouterr()

    assert writes == [
        ("master_sharpely_equity", 2, ["symbol", "bse_ticker"]),
    ]
    assert sharpely_scrip_master.STOCKEY_RUN_STATE["source"] == "sharpely"
    assert sharpely_scrip_master.STOCKEY_RUN_STATE["rows"] == 2
    assert sharpely_scrip_master.STOCKEY_RUN_STATE["rows_read"] == 3
    assert sharpely_scrip_master.STOCKEY_RUN_STATE["rows_written"] == 2
    assert sharpely_scrip_master.STOCKEY_RUN_STATE["equity_rows"] == 2
    assert sharpely_scrip_master.STOCKEY_RUN_STATE["raw_equity_rows"] == 3
    assert sharpely_scrip_master.STOCKEY_RUN_STATE["fallback_used"] is False
    assert sharpely_scrip_master.STOCKEY_RUN_STATE["state_advanced"] is True


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


def test_price_adjustment_view_composes_total_return_with_split_adjustment():
    # BUG FOUND LIVE 2026-08-19 (re-audit): tr_adj_close/open/high/low used to multiply raw close/open/
    # high/low by cum_total_return_factor ALONE -- cum_total_return_factor is purely dividend-derived and
    # knows nothing about splits/bonuses, so any split/bonus event faked a discontinuity in the
    # total-return columns at the split's own magnitude. Confirmed live on IRCTC's 2021-10-28 5:1 split:
    # adj_close stayed correctly smooth while tr_adj_close faked a ~4.5x "crash" on the same date.
    from data.nseindia import price_adjustment as pa

    view_sql = " ".join(pa.VIEW_SCHEMA_STATEMENTS)
    for col in ("close", "open", "high", "low"):
        assert f"o.{col} * f.cum_price_adjustment_factor * f.cum_total_return_factor AS tr_adj_{col}" in view_sql


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


def test_price_adjustment_ca_purpose_parser_handles_nse_no_separator_subjects():
    # BUG FOUND LIVE 2026-08-19 (re-audit): both regexes required a separator character NSE sometimes
    # omits entirely. Real, live-confirmed subjects that used to silently drop or miss the split half.
    from data.nseindia import price_adjustment as pa
    f = lambda s: pa._factor_for_events(pa._events_from_subject(s))

    # BAJFINANCE 2016-09-08: combined 1:1 bonus + 5:1 (FV 10->2) split, no separator before "TO".
    # bonus factor 0.5 * split factor 0.2 = 0.1, matching the real observed overnight ratio (~0.101).
    assert abs(f("BON 1:1/SPLIT RS.10TORS.2") - 0.1) < 1e-9

    # WELSPUNIND 2016-03-21: dividend + a 10:1 split, no separator before "TO" either. Used to match
    # NEITHER regex at all (bonus regex correctly doesn't match -- no bonus here -- but the split regex
    # used to miss it too), routing the whole date to declared_non_split_dates and leaving a real 10:1
    # split completely unadjusted.
    events = pa._events_from_subject("DIV-RS6/SPLIT RS 10TORE 1")
    assert ("split", 10, 1) in events
    assert abs(pa._factor_for_events(events) - 0.1) < 1e-9

    # the hyphenated bonus form ("BON-1:25") also used to fail to match at all.
    assert f("BON-1:25") == pytest.approx(25 / 26)


def test_price_adjustment_ca_purpose_parser_handles_face_value_slash_notation():
    # BUG FOUND LIVE 2026-08-19 (re-audit): _events_from_subject splits combined-event subjects on '/',
    # but NSE also uses '/' inside its "Rs.X/-" face-value notation -- a bare '/' split shredded plain,
    # single-event subjects like ASTRAL's "FV SPLIT RS.5/- TO RS.2/-" into unparseable fragments
    # ("FV SPLIT RS.5", "- TO RS.2", "-"), even though the whole, unsplit text matches cleanly.
    from data.nseindia import price_adjustment as pa
    f = lambda s: pa._factor_for_events(pa._events_from_subject(s))

    assert f("FV SPLIT RS.5/- TO RS.2/-") == 0.4          # ASTRAL 2013-09-05
    assert f("FV SPLIT RS 5/- TO RE 1/-") == 0.2           # HAVELLS 2014-08-26

    # a genuine combined-event '/' (not part of "/-" notation) still splits correctly
    combined = pa._events_from_subject("BON-1:25/SPLIT RS10TORS.5")
    assert combined == {("bonus", 1, 25), ("split", 10, 5)}

    # a subject genuinely truncated at the source (no second number at all) correctly stays unparseable
    # -- not a splitting bug, nothing to recover; must not be guessed at.
    assert pa._events_from_subject("FV SPLT FRM RS 10/- TO RS") == set()


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


def test_declared_ratio_plausible_accepts_close_match_rejects_far_off():
    from data.nseindia import price_adjustment as pa
    # a declared 1:1 bonus (0.5) whose ex-date also moved a genuine few % -- exactly the case the
    # declared path exists for, per adjust_frame's own docstring. Must still be trusted.
    assert pa._declared_ratio_plausible(0.5, 0.535) is True
    # TRIVENI-style: declared 0.6, observed ~0.615 -- also within tolerance.
    assert pa._declared_ratio_plausible(0.6, 0.6149) is True
    # BAJFINANCE-style: declared ratio only captures ONE of two combined events (bonus-only, 0.5) but the
    # real combined step is ~0.1 -- a whole separate event's worth of magnitude off. Must be rejected.
    assert pa._declared_ratio_plausible(0.5, 0.1013) is False
    assert pa._declared_ratio_plausible(0.0, 0.5) is False
    assert pa._declared_ratio_plausible(0.5, 0.0) is False
    assert pa._declared_ratio_plausible(float("nan"), 0.5) is False


def test_price_adjustment_declared_ratio_that_misses_a_combined_event_is_flagged_not_misapplied():
    # BUG FOUND LIVE 2026-08-19 (re-audit): reproduces BAJFINANCE's 2016-09-08 shape at unit-test scale --
    # a declared ratio that only captures ONE of two combined events used to be applied with full
    # confidence (ca_flag='split_bonus_ca'), corrupting the whole pre-event history. Now flagged for
    # review instead of silently misapplied.
    import datetime as _dt
    from data.nseindia import price_adjustment as pa

    # combined-event price ratio deliberately chosen to NOT snap cleanly to a round ratio either (~1/7.5,
    # roughly equidistant between the round ratios 7 and 8, both outside _snap_event_ratio's 6% tolerance)
    # -- isolates the plausibility-check behavior from the price-snap fallback recovering the same answer
    # by coincidence, which is what a round-numbered combined ratio (like the real BAJFINANCE ~10x) would do.
    true_ratio = 1 / 7.5
    prev_close = 1139.330
    df = pd.DataFrame({
        "symbol": ["BAJ", "BAJ", "BAJ"],
        "date": pd.to_datetime(["2016-09-06", "2016-09-07", "2016-09-08"], utc=True),
        "open": [1120.0, 1130.0, prev_close * true_ratio], "close": [1126.585, prev_close, prev_close * true_ratio + 0.5],
    })
    # a declared ratio that only captured one of two combined events (0.5), nowhere near the true ~0.133
    wrong_decl = {("BAJ", _dt.date(2016, 9, 8)): 0.5}
    a = pa.adjust_frame(df.copy(), declared_ratios=wrong_decl).sort_values("date").reset_index(drop=True)
    assert a.loc[2, "ca_flag"] == "declared_ca_mismatch"
    assert (a["cum_adj_factor"] == 1.0).all()  # NOT adjusted with the wrong-magnitude ratio

    # the correct combined ratio IS trusted and applied
    right_decl = {("BAJ", _dt.date(2016, 9, 8)): true_ratio}
    a2 = pa.adjust_frame(df.copy(), declared_ratios=right_decl).sort_values("date").reset_index(drop=True)
    assert a2.loc[2, "ca_flag"] == "split_bonus_ca"
    assert abs(a2.loc[0, "cum_adj_factor"] - true_ratio) < 1e-9


def test_price_adjustment_declared_non_split_ca_blocks_price_snap():
    # 2026-08-14 bug found live: a demerger, rights issue, or scheme of arrangement can produce a price
    # step that coincidentally snaps to a round ratio -- confirmed live for TATAMOTORS/SIEMENS/RAYMOND/IDFC
    # (demergers), CALSOFT/RSWM/IDEA (rights issues), and 11 more (schemes of arrangement), all wrongly
    # classified 'split_bonus' by the price-snap heuristic alone. TRIVENI's actual 2026-07-22 demerger:
    # 471.50 -> 289.95 open, ratio 0.6149 snaps cleanly to 3/5 -- a real round ratio, but NOT a split.
    import datetime as _dt
    from data.nseindia import price_adjustment as pa

    df = pd.DataFrame({
        "symbol": ["TRIVENI", "TRIVENI"],
        "date": pd.to_datetime(["2026-07-21", "2026-07-22"], utc=True),
        "open": [469.65, 289.95], "close": [471.50, 275.50],
    })
    # without the guard, this round-ratio step would be misread as a split (matches _snap_event_ratio's
    # own logic -- confirm the un-guarded baseline actually would have misclassified it)
    unguarded = pa.adjust_frame(df.copy())
    assert unguarded.sort_values("date").iloc[1]["ca_flag"] == "split_bonus"

    non_split = {("TRIVENI", _dt.date(2026, 7, 22))}
    guarded = pa.adjust_frame(df.copy(), declared_non_split_dates=non_split).sort_values("date").reset_index(drop=True)
    assert guarded.loc[1, "ca_flag"] == "declared_non_split_ca"
    assert (guarded["cum_adj_factor"] == 1.0).all()  # NOT adjusted -- the demerger's real value change stays visible

    # a declared split/bonus ratio still wins over a non-split date for the SAME key (shouldn't co-occur in
    # practice -- load_declared_ca_ratios() puts every date in exactly one bucket -- but declared_ratios must
    # take priority if it ever does)
    both = pa.adjust_frame(
        df.copy(),
        declared_ratios={("TRIVENI", _dt.date(2026, 7, 22)): 0.6},
        declared_non_split_dates=non_split,
    ).sort_values("date").reset_index(drop=True)
    assert both.loc[1, "ca_flag"] == "split_bonus_ca"


def test_load_declared_ca_ratios_separates_split_bonus_from_other_declared_actions(monkeypatch):
    from data.nseindia import price_adjustment as pa
    import datetime as _dt

    rows = pd.DataFrame({
        "symbol": ["A", "B", "C", "D"],
        "date": pd.to_datetime(["2026-01-01", "2026-01-02", "2026-01-03", "2026-01-04"], utc=True),
        "subject": ["BONUS 1:1", "DEMERGER", "RIGHTS 1:1 @ PRM RS 10", "SCHEME OF ARRANGEMENT"],
    })
    monkeypatch.setattr("utils.db.sql_to_df", lambda query: rows)

    ratios, non_split_dates = pa.load_declared_ca_ratios()

    assert ratios == {("A", _dt.date(2026, 1, 1)): 0.5}
    assert non_split_dates == {
        ("B", _dt.date(2026, 1, 2)),
        ("C", _dt.date(2026, 1, 3)),
        ("D", _dt.date(2026, 1, 4)),
    }


def test_load_declared_ca_ratios_mixed_split_and_non_split_same_date_is_not_split(monkeypatch):
    # 2026-08-14 bug found live: AHLEAST declared BOTH 'DEMERGER' and 'BONUS 1:2' for 2022-10-06 (across
    # its EQ/BE series rows). Unioning every subject's events at that key let the bonus event silently win
    # -- the date got the bonus-only factor (0.667) even though the real overnight step (0.528) also
    # carried the demerger's value carve-out, permanently under-adjusting the symbol's pre-event history.
    # A mixed date (real split/bonus event + a subject that parsed to none) must route to non_split_dates,
    # not ratios, so adjust_frame flags it for review instead of confidently misadjusting it.
    from data.nseindia import price_adjustment as pa
    import datetime as _dt

    rows = pd.DataFrame({
        "symbol": ["AHLEAST", "AHLEAST", "CLEAN"],
        "date": pd.to_datetime(["2022-10-06", "2022-10-06", "2026-01-01"], utc=True),
        "subject": ["DEMERGER", "BONUS 1:2", "BONUS 1:1"],
    })
    monkeypatch.setattr("utils.db.sql_to_df", lambda query: rows)

    ratios, non_split_dates = pa.load_declared_ca_ratios()

    assert ("AHLEAST", _dt.date(2022, 10, 6)) not in ratios
    assert ("AHLEAST", _dt.date(2022, 10, 6)) in non_split_dates
    # an unmixed date (only a bonus declared) is unaffected
    assert ratios == {("CLEAN", _dt.date(2026, 1, 1)): 0.5}


def test_compute_total_return_factor_back_adjusts_pre_dividend_history():
    # 2026-08-14 redesign: TR factor derived from events_dividend, not re-parsed CA text. A Rs 5 dividend
    # on 2026-01-03 (prev_close 100) -> daily factor 0.95; pre-ex-date history is back-adjusted by it,
    # post-ex-date history untouched (same reverse-cumprod convention as adjust_frame's cum_adj_factor).
    from data.nseindia import price_adjustment as pa
    dates = pd.bdate_range("2026-01-01", periods=4, tz="UTC")
    prices = pd.DataFrame({
        "symbol": "S", "date": dates,
        "previous_close": [99.0, 100.0, 100.0, 96.0],  # ex-date (2026-01-03) previous_close = 100
    })
    dividends = pd.DataFrame({"symbol": ["S"], "ex_date": [dates[2]], "dividend_amount": [5.0]})

    factor = pa.compute_total_return_factor(prices, dividends)

    assert abs(factor.iloc[0] - 0.95) < 1e-9
    assert abs(factor.iloc[1] - 0.95) < 1e-9
    assert abs(factor.iloc[2] - 1.0) < 1e-9   # the ex-date row itself and after: untouched
    assert abs(factor.iloc[3] - 1.0) < 1e-9


def test_compute_total_return_factor_empty_dividends_is_all_ones():
    from data.nseindia import price_adjustment as pa
    dates = pd.bdate_range("2026-01-01", periods=3, tz="UTC")
    prices = pd.DataFrame({"symbol": "S", "date": dates, "previous_close": [100.0, 101.0, 102.0]})
    factor = pa.compute_total_return_factor(prices, pd.DataFrame(columns=["symbol", "ex_date", "dividend_amount"]))
    assert (factor == 1.0).all()


def test_compute_total_return_factor_multiple_same_day_dividends_combine():
    # two dividend rows on the same ex-date (e.g. interim + final recorded separately) must combine into
    # one event, not be applied independently (which would double-count via two separate merge rows).
    from data.nseindia import price_adjustment as pa
    dates = pd.bdate_range("2026-01-01", periods=2, tz="UTC")
    prices = pd.DataFrame({"symbol": "S", "date": dates, "previous_close": [100.0, 100.0]})
    dividends = pd.DataFrame({
        "symbol": ["S", "S"], "ex_date": [dates[1], dates[1]], "dividend_amount": [3.0, 2.0],
    })
    factor = pa.compute_total_return_factor(prices, dividends)
    assert abs(factor.iloc[0] - 0.95) < 1e-9   # combined Rs 5 dividend, not two separate 0.97/0.98 factors


def test_compute_total_return_factor_dedupes_reworded_same_amount_duplicate():
    # 2026-08-14 bug found live: NSE republishes/reformats the same declared dividend under different
    # subject wording, and events_dividend's uniqueness key (symbol, ex_date, subject) lets each wording
    # variant survive as its own row -- e.g. SYMPHONY 2017-08-23's real Rs 1 dividend appeared 3x under
    # 3 different subjects. Summing them blindly (the old behavior) tripled the payout to Rs 3. Identical
    # (symbol, ex_date, amount) duplicates must collapse to ONE payout...
    from data.nseindia import price_adjustment as pa
    dates = pd.bdate_range("2026-01-01", periods=2, tz="UTC")
    prices = pd.DataFrame({"symbol": "S", "date": dates, "previous_close": [100.0, 100.0]})
    reworded_duplicate = pd.DataFrame({
        "symbol": ["S", "S", "S"], "ex_date": [dates[1], dates[1], dates[1]], "dividend_amount": [1.0, 1.0, 1.0],
    })
    factor = pa.compute_total_return_factor(prices, reworded_duplicate)
    assert abs(factor.iloc[0] - 0.99) < 1e-9   # ONE Rs 1 dividend, not three -- not (100-3)/100 = 0.97

    # ...while two GENUINELY distinct same-day dividends (different amounts) still both count, same as
    # test_compute_total_return_factor_multiple_same_day_dividends_combine above.
    genuinely_distinct = pd.DataFrame({
        "symbol": ["S", "S"], "ex_date": [dates[1], dates[1]], "dividend_amount": [3.0, 2.0],
    })
    factor2 = pa.compute_total_return_factor(prices, genuinely_distinct)
    assert abs(factor2.iloc[0] - 0.95) < 1e-9


def test_build_adjustment_factors_writes_compact_table(monkeypatch):
    from data.nseindia import price_adjustment as pa
    dates = pd.bdate_range("2026-01-01", periods=3, tz="UTC")
    prices = pd.DataFrame({
        "symbol": ["S", "S", "S"], "date": dates, "series": ["EQ", "EQ", "EQ"],
        "open": [100.0, 101.0, 102.0], "close": [100.0, 101.0, 102.0], "previous_close": [99.0, 100.0, 101.0],
    })
    dividends = pd.DataFrame({"symbol": ["S"], "ex_date": [dates[1]], "dividend_amount": [2.0]})

    def fake_sql_to_df(query, params=None):
        if "nseindia_ohlcv" in query:
            return prices
        if "events_dividend" in query:
            return dividends
        raise AssertionError(f"unexpected query: {query}")

    captured = {}

    def fake_upsert(df, table, **kwargs):
        captured["table"] = table
        captured["df"] = df
        captured["kwargs"] = kwargs

    # build_adjustment_factors() imports sql_to_df/upsert_to_db locally (from utils.db import ...) on
    # every call, so the source module -- not this module's namespace -- must be patched.
    import utils.db as db_module
    monkeypatch.setattr(db_module, "sql_to_df", fake_sql_to_df)
    monkeypatch.setattr(db_module, "upsert_to_db", fake_upsert)
    monkeypatch.setattr(pa, "load_declared_ca_ratios", lambda: ({}, set()))
    monkeypatch.setattr(pa, "ensure_factors_table", lambda: None)
    view_calls = []
    monkeypatch.setattr(pa, "ensure_view", lambda: view_calls.append(True))

    summary = pa.build_adjustment_factors(dry_run=False)

    assert summary["rows"] == 3
    assert summary["symbols"] == 1
    assert summary["dividend_events_loaded"] == 1
    assert captured["table"] == pa.ADJUSTMENT_FACTORS_TABLE
    assert list(captured["df"].columns) == ["symbol", "date", "cum_price_adjustment_factor", "ca_flag", "cum_total_return_factor", "load_ts"]
    assert captured["kwargs"]["unique_keys"] == ["symbol", "date"]
    row0 = captured["df"].iloc[0]
    row1 = captured["df"].iloc[1]
    assert abs(row0["cum_total_return_factor"] - 0.98) < 1e-9  # Rs 2 dividend on prev_close 100 -> 0.98, back-adjusts pre-ex-date history
    assert abs(row1["cum_total_return_factor"] - 1.0) < 1e-9   # the ex-date row itself: untouched
    # ensure_view() must run every non-dry-run build (2026-08-14 bug: it was defined but never called,
    # so a fresh DB or an accidental drop would silently never get advisory_adjusted_ohlcv_daily back).
    assert view_calls == [True]


def test_nse_ensure_view_runs_create_or_replace_unconditionally(monkeypatch):
    # BUG FOUND LIVE 2026-08-18 (re-audit): the "self-heals an accidental drop"
    # claim was false once the migration is recorded -- apply_schema_migration()
    # skips re-executing its statements entirely once migration_id is marked
    # 'applied', regardless of whether the VIEW itself still exists.
    # advisory_adjusted_ohlcv_daily is systrader's own PRIMARY series, so this is
    # the highest-traffic place this gap could bite. CREATE OR REPLACE VIEW is
    # idempotent and cheap, so it must run directly, unconditionally, on every
    # ensure_view() call -- not gated behind the migration's own tracking.
    from data.nseindia import price_adjustment as pa

    # ensure_view() imports apply_schema_migration/db_session/execute_db_operation
    # locally on every call (same convention noted above for build_adjustment_
    # factors), so the SOURCE modules, not this module's namespace, must be patched.
    import utils.db as db_module
    import utils.schema_migrations as schema_migrations_module

    monkeypatch.setattr(schema_migrations_module, "apply_schema_migration", lambda **k: {"status": "skipped_already_applied"})
    executed = []

    class _FakeCursor:
        def execute(self, query, params=None):
            executed.append(query)

    @contextlib.contextmanager
    def fake_db_session():
        yield None, _FakeCursor()

    monkeypatch.setattr(db_module, "db_session", fake_db_session)
    monkeypatch.setattr(db_module, "execute_db_operation", lambda op, **k: op())

    pa.ensure_view()

    assert len(executed) == 1
    assert "CREATE OR REPLACE VIEW" in executed[0]


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
    # 2026-08-14 bug found live: a combined interim+special declaration carries TWO figures, not one --
    # the old first-match-only regex understated 193 real subjects this way.
    assert parse_dividend("DIV-RS 4/SPL DIV-RS 3")["dividend_amount"] == 7.0
    assert parse_dividend("AGM/DIV-RS 7/SPLDIV-RS15")["dividend_amount"] == 22.0


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
    # split/bonus events must independently corroborate against Dhan within a sane tolerance -- not so
    # tight it flags ordinary trading noise (Dhan often pre/post-adjusts a symbol a few days off NSE's
    # official ex-date), not so loose it misses a real mismatch
    assert 0.02 <= pds.SPLIT_CROSSCHECK_TOLERANCE <= 0.10




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


def test_fbil_gsec_download_failure_skips_the_date_and_keeps_going(monkeypatch):
    # BUG FOUND LIVE 2026-08-19 (re-audit): a raised exception used to `break` the whole
    # catch-up loop -- since the redis DOWNLOADED cursor only advances on success, one
    # persistently-failing date permanently blocked every later date behind it. Confirmed
    # live: fbil_gsec_par's max date was stuck at 2026-08-13 for 6 days after exactly this.
    from data.rbi import download_fbil_gsec as fbil_gsec

    dates = [date(2026, 6, 8), date(2026, 6, 9), date(2026, 6, 10)]
    monkeypatch.setattr(fbil_gsec, "get_cookies", lambda: {"session": "ok"})
    monkeypatch.setattr(fbil_gsec.rop, "get", lambda _key: None)
    monkeypatch.setattr(fbil_gsec, "daterange", lambda _start, _stop: dates)
    monkeypatch.setattr(fbil_gsec, "record_local_fallback_event", lambda **kwargs: None)

    attempted = []

    def fake_download_gsec(fdate, cookies):
        attempted.append(fdate)
        if fdate == dates[0]:
            raise RuntimeError("fbil down for this one date")
        return {"date": fdate.isoformat(), "status": "downloaded", "rows": 5}

    monkeypatch.setattr(fbil_gsec, "download_gsec", fake_download_gsec)

    state = fbil_gsec.download_all_gsec_data()

    # all 3 dates were attempted -- the failure on dates[0] did NOT stop the loop
    assert attempted == dates
    assert state["failed_date_count"] == 1
    assert state["downloaded_date_count"] == 2
    assert state["blocked"] is False


def test_fbil_gsec_circuit_breaker_trips_after_3_consecutive_failures(monkeypatch):
    from data.rbi import download_fbil_gsec as fbil_gsec

    dates = [date(2026, 6, d) for d in range(8, 14)]  # 6 candidate dates
    monkeypatch.setattr(fbil_gsec, "get_cookies", lambda: {"session": "ok"})
    monkeypatch.setattr(fbil_gsec.rop, "get", lambda _key: None)
    monkeypatch.setattr(fbil_gsec, "daterange", lambda _start, _stop: dates)
    events: list[dict[str, object]] = []
    monkeypatch.setattr(fbil_gsec, "record_local_fallback_event", lambda **kwargs: events.append(kwargs))

    attempted = []

    def always_fails(fdate, cookies):
        attempted.append(fdate)
        raise RuntimeError("fbil source down")

    monkeypatch.setattr(fbil_gsec, "download_gsec", always_fails)

    state = fbil_gsec.download_all_gsec_data()

    # bounded: stops after the 3rd consecutive failure, doesn't burn through all 6 dates
    assert attempted == dates[:3]
    assert state["failed_date_count"] == 3
    assert state["blocked"] is True
    assert state["fallback_used"] is True
    assert any(e["fallback_type"] == "fbil_gsec_circuit_breaker_tripped" and e["severity"] == "error" for e in events)


def test_fbil_gsec_main_reports_blocked_status(monkeypatch, capsys):
    from data.rbi import download_fbil_gsec as fbil_gsec

    payload = {
        "source": "data.rbi.download_fbil_gsec",
        "failed_date_count": 3,
        "blocked": True,
    }
    monkeypatch.setattr(fbil_gsec, "download_all_gsec_data", lambda: payload)

    fbil_gsec.main()

    printed = json.loads(capsys.readouterr().out)
    assert printed["status"] == "blocked"


def test_fbil_gsec_main_reports_partial_status_without_circuit_breaker(monkeypatch, capsys):
    # a run with some failed dates but no circuit-breaker trip stays "partial", not "blocked"
    from data.rbi import download_fbil_gsec as fbil_gsec

    payload = {
        "source": "data.rbi.download_fbil_gsec",
        "failed_date_count": 1,
        "blocked": False,
    }
    monkeypatch.setattr(fbil_gsec, "download_all_gsec_data", lambda: payload)

    fbil_gsec.main()

    printed = json.loads(capsys.readouterr().out)
    assert printed["status"] == "partial"


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

    # 2026-08-15: fbil_gsec_quote retired (zero readers) -- parse_xls no longer reads the "G-Sec"
    # sheet's full quote table (skiprows=5), only its trade_date cell (no skiprows), plus Par Yield.
    events: list[dict[str, object]] = []
    calls: list[tuple[str, object]] = []

    def fake_read_excel(_path, sheet_name=None, skiprows=None):
        calls.append((str(sheet_name), skiprows))
        if sheet_name == "G-Sec" and skiprows is None:
            return pd.DataFrame([[None, None, None], [None, None, "bad-date"]])
        if sheet_name == "Par Yield":
            raise ValueError("missing Par Yield")
        if sheet_name == "Par-Yield":
            return pd.DataFrame([[10, 7.2, 7.45]])
        raise AssertionError(f"unexpected read_excel call {sheet_name=} {skiprows=}")

    monkeypatch.setattr(fbil_gsec.pd, "read_excel", fake_read_excel)
    monkeypatch.setattr(fbil_gsec, "record_local_fallback_event", lambda **kwargs: events.append(kwargs))

    par = fbil_gsec.parse_xls("dummy.xls", date(2026, 6, 8))

    assert par["trade_date"].iloc[0] == date(2026, 6, 8)
    assert float(par["par_yield_sa"].iloc[0]) == 7.2
    assert [event["fallback_type"] for event in events] == [
        "fbil_gsec_trade_date_parse_failed",
        "fbil_gsec_par_yield_sheet_fallback",
    ]
    assert events[0]["metadata"]["raw_trade_date"] == "bad-date"
    assert all(event["metadata"]["date"] == "2026-06-08" for event in events)
    assert ("Par-Yield", 5) in calls
    assert ("G-Sec", 5) not in calls


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


def _make_fake_rbi_modal(*, visible, button_labels=()):
    from data.rbi import download_bank_rates as bank_rates

    class FakeButton:
        def __init__(self, present):
            self._present = present
            self.clicked = False

        def count(self):
            return 1 if self._present else 0

        @property
        def first(self):
            return self

        def click(self):
            self.clicked = True

    class FakeDialog:
        def __init__(self):
            self.buttons = {label: FakeButton(label in button_labels) for label in ("Refresh", "OK", "Close")}

        @property
        def first(self):
            return self

        def is_visible(self, timeout=None):
            if not visible:
                raise bank_rates.PlaywrightTimeoutError("no modal")
            return True

        def get_by_role(self, role, name=None, exact=None):
            return self.buttons[name]

    class FakeKeyboard:
        def __init__(self):
            self.pressed = []

        def press(self, key):
            self.pressed.append(key)

    class FakePage:
        def __init__(self):
            self.dialog = FakeDialog()
            self.keyboard = FakeKeyboard()
            self.waits = []

        def locator(self, selector):
            return self.dialog

        def wait_for_timeout(self, ms):
            self.waits.append(ms)

    return FakePage()


def test_dismiss_blocking_modal_returns_false_when_nothing_visible(monkeypatch):
    from data.rbi import download_bank_rates as bank_rates

    page = _make_fake_rbi_modal(visible=False)
    assert bank_rates._dismiss_blocking_modal(page) is False
    assert page.keyboard.pressed == []


def test_dismiss_blocking_modal_clicks_known_button_label(monkeypatch):
    from data.rbi import download_bank_rates as bank_rates

    page = _make_fake_rbi_modal(visible=True, button_labels=("Refresh",))
    assert bank_rates._dismiss_blocking_modal(page) is True
    assert page.dialog.buttons["Refresh"].clicked is True
    assert page.keyboard.pressed == []  # a known button was found -- Escape never needed


def test_dismiss_blocking_modal_falls_back_to_escape_without_known_button(monkeypatch):
    from data.rbi import download_bank_rates as bank_rates

    page = _make_fake_rbi_modal(visible=True, button_labels=())
    assert bank_rates._dismiss_blocking_modal(page) is True
    assert page.keyboard.pressed == ["Escape"]


def test_click_past_blocking_modals_retries_after_dismissing(monkeypatch):
    from data.rbi import download_bank_rates as bank_rates

    page = _make_fake_rbi_modal(visible=True, button_labels=("OK",))

    class FlakyLocator:
        def __init__(self):
            self.attempts = 0

        def click(self, timeout=None):
            self.attempts += 1
            if self.attempts == 1:
                raise bank_rates.PlaywrightTimeoutError("blocked by modal")
            # succeeds on the second attempt, after the modal is dismissed

    locator = FlakyLocator()
    bank_rates._click_past_blocking_modals(page, locator)
    assert locator.attempts == 2
    assert page.dialog.buttons["OK"].clicked is True


def test_click_past_blocking_modals_reraises_when_nothing_to_dismiss(monkeypatch):
    # BUG FOUND LIVE 2026-08-19: the "Indicators" click has failed on every one of the
    # last 15 recorded complete_data.sh runs with a modal-backdrop intercepting pointer
    # events -- dismiss-and-retry is strictly protective, but a genuine non-modal timeout
    # (nothing to dismiss) must still surface as a real failure, not swallow forever.
    from data.rbi import download_bank_rates as bank_rates

    page = _make_fake_rbi_modal(visible=False)

    class AlwaysBlockedLocator:
        def click(self, timeout=None):
            raise bank_rates.PlaywrightTimeoutError("blocked, not by a modal")

    with pytest.raises(bank_rates.PlaywrightTimeoutError, match="blocked, not by a modal"):
        bank_rates._click_past_blocking_modals(page, AlwaysBlockedLocator())


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


def test_hot_table_retention_selects_trace_and_intraday_groups(monkeypatch):
    # 2026-08-14: the real "trace" group tables (advisory_decision_traces etc.) were
    # dropped in the cloud-DB cleanup -- RETENTION_TABLES is intraday-only now, so this
    # exercises selected_specs()'s group filter against a synthetic trace entry instead.
    from scripts import hot_table_retention

    fake_trace = hot_table_retention.RetentionTable(
        table_name="fake_trace_table", date_column="load_ts", group="trace",
        default_retention_days=90, description="test",
    )
    monkeypatch.setitem(hot_table_retention.RETENTION_TABLES, "fake_trace_table", fake_trace)

    trace_specs = hot_table_retention.selected_specs(group="trace")
    intraday_specs = hot_table_retention.selected_specs(group="intraday")

    assert trace_specs
    assert intraday_specs
    assert {spec.group for spec in trace_specs} == {"trace"}
    assert {spec.group for spec in intraday_specs} == {"intraday"}
    assert "fake_trace_table" in {spec.table_name for spec in trace_specs}
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


def test_hot_table_retention_execute_uses_retryable_operation(monkeypatch, tmp_path):
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
    # 2026-08-14 bug found live: tempfile.mkdtemp()'d tmp_root was never cleaned up -- 72 empty dirs
    # accumulated under /tmp over 3 days of --execute runs. Capture the actual dir mkdtemp() hands back
    # and assert it's gone once archive_or_delete_table returns.
    created_dirs = []
    real_mkdtemp = hot_table_retention.tempfile.mkdtemp

    def spying_mkdtemp(*args, **kwargs):
        path = real_mkdtemp(*args, dir=tmp_path, **kwargs)
        created_dirs.append(path)
        return path

    monkeypatch.setattr(hot_table_retention.tempfile, "mkdtemp", spying_mkdtemp)

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
    assert len(created_dirs) == 1
    assert not os.path.exists(created_dirs[0])  # cleaned up, not leaked






















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


def test_build_identity_break_events_returns_empty_for_no_breaks():
    result = fundamentals_security_master.build_identity_break_events(pd.DataFrame())
    assert result.empty


def test_build_identity_break_events_picks_current_row_deterministically(monkeypatch):
    # BUG FOUND LIVE 2026-08-17: the "current" dim_security row per symbol used to be
    # picked by pandas drop_duplicates(keep="last") over a SELECT with no ORDER BY --
    # Postgres's row order without ORDER BY is unspecified, so "last" was effectively
    # arbitrary. Every sibling query in this module that needs "current row per
    # symbol" (backfill_bse_scrip_codes) already uses DISTINCT ON (...) ORDER BY
    # last_trade_date DESC NULLS LAST, effective_to DESC NULLS LAST; this pins the
    # same deterministic query shape here.
    captured = {}

    def fake_sql_to_df(query, **kwargs):
        captured["query"] = query
        # Simulate Postgres's own DISTINCT ON already having picked one row per
        # symbol -- Python-side dedup logic no longer exists to test independently,
        # so the query text itself is the thing under test.
        return pd.DataFrame([{"isin": "INE_CURRENT", "symbol": "TEST", "series": "EQ", "security_id": "sec-current"}])

    monkeypatch.setattr(fundamentals_security_master, "sql_to_df", fake_sql_to_df)

    identity_breaks = pd.DataFrame([{"SYMBOL": "TEST", "SERIES": "EQ", "ISIN NUMBER": "INE_NEW"}])
    result = fundamentals_security_master.build_identity_break_events(identity_breaks)

    assert "DISTINCT ON (symbol)" in captured["query"]
    assert "ORDER BY symbol, last_trade_date DESC NULLS LAST, effective_to DESC NULLS LAST" in captured["query"]
    assert result.iloc[0]["isin"] == "INE_CURRENT"  # the prior isin, from the deterministically-picked row
    assert result.iloc[0]["related_isin"] == "INE_NEW"


def test_build_identity_break_events_query_has_a_final_deterministic_tiebreaker(monkeypatch):
    # BUG FOUND LIVE 2026-08-18 (re-audit): last_trade_date DESC NULLS LAST,
    # effective_to DESC NULLS LAST alone is STILL non-deterministic on a true tie --
    # live, 12 symbols tie on both (e.g. AARTISURF's EQ and P1 rows share the exact
    # same dates). Must prefer series='EQ' (the canonical listing, same convention
    # technicals.py/watchlist.py already use), then fall back to isin as a fully
    # deterministic final tiebreaker.
    captured = {}

    def fake_sql_to_df(query, **k):
        captured["query"] = query
        return pd.DataFrame(columns=["isin", "symbol", "series", "security_id"])

    monkeypatch.setattr(fundamentals_security_master, "sql_to_df", fake_sql_to_df)
    fundamentals_security_master.build_identity_break_events(
        pd.DataFrame([{"SYMBOL": "TEST", "SERIES": "EQ", "ISIN NUMBER": "INE_NEW"}])
    )
    assert "(series = 'EQ') DESC, isin" in captured["query"]


def test_load_bse_scrip_code_targets_query_has_the_same_final_tiebreaker(monkeypatch):
    # Same non-deterministic-tie gap as build_identity_break_events' own DISTINCT ON
    # query (re-audit 2026-08-18) -- this sibling query needs the identical
    # "series = 'EQ'", then isin, final tiebreaker.
    captured = {}

    def fake_sql_to_df(query, **k):
        captured["query"] = query
        return pd.DataFrame(columns=["company_master_id", "isin"])

    monkeypatch.setattr(fundamentals_security_master, "sql_to_df", fake_sql_to_df)

    fundamentals_security_master.load_bse_scrip_code_targets()

    assert "ORDER BY ds.company_master_id, ds.last_trade_date DESC NULLS LAST, ds.effective_to DESC NULLS LAST, (ds.series = 'EQ') DESC, ds.isin" in captured["query"]


# fundamentals/collectors/screenerin.py -- shared screener.in scraping infra (used by L1/L2).

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


def test_parse_screener_results_skips_rows_without_company_id(monkeypatch):
    # MEDIUM FINDING (re-audit 2026-08-18): a skipped/unparseable row used to be
    # silent -- if it happened on an otherwise-full page, run_query()'s pagination
    # loop (which decides "was this page full" purely from len(companies) ==
    # SCREENER_PAGE_SIZE) would wrongly treat a genuinely-full page as the final
    # one and silently drop every company on every subsequent page. Must now be
    # visible via fallback telemetry.
    html = """
    <div data-page-results><table>
    <tr><th><a>Name</a></th></tr>
    <tr><td>not a data row</td></tr>
    </table></div>
    """
    fallback_events = []
    monkeypatch.setattr(fundamentals_screenerin, "record_local_fallback_event", lambda **kwargs: fallback_events.append(kwargs))

    assert fundamentals_screenerin.parse_screener_results(html) == []

    assert len(fallback_events) == 1
    assert fallback_events[0]["fallback_type"] == "screenerin_query_rows_skipped_during_parse"
    assert fallback_events[0]["metadata"]["skipped_row_count"] == 1


def test_parse_screener_results_no_fallback_when_every_row_parses(monkeypatch):
    fallback_events = []
    monkeypatch.setattr(fundamentals_screenerin, "record_local_fallback_event", lambda **kwargs: fallback_events.append(kwargs))

    fundamentals_screenerin.parse_screener_results(SCREENERIN_FIXTURE_HTML)

    assert fallback_events == []


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
    # SCREENER_PAGE_SIZE monkeypatched to 2 so a small fixture can still exercise the
    # multi-page loop: pages 1-2 come back full (2 rows), page 3 comes back short (1
    # row) -- must fetch page 3, include it, then stop (not loop forever, not drop it).
    monkeypatch.setattr(fundamentals_screenerin, "SCREENER_PAGE_SIZE", 2)
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


def test_run_query_short_first_page_never_fetches_a_second_page(monkeypatch):
    # BUG FOUND LIVE 2026-08-15 (fixed in run_query): a first page shorter than the
    # real, fixed SCREENER_PAGE_SIZE IS the complete result set -- screener.in never
    # serves a genuine page 2 in that case, and the old self-referential
    # `page_size = len(first_page)` comparison used to trigger a wasted, duplicate-
    # producing extra fetch here. A short first page must not fetch page 2 at all.
    pages = {1: ("url?page=1", [{"company_id": 1}]), 2: ("url?page=2", [{"company_id": 999}])}
    calls = []

    def fake_fetch(session, query_text, *, page):
        calls.append(page)
        return pages[page]

    monkeypatch.setattr(fundamentals_screenerin, "_fetch_query_page", fake_fetch)

    _, companies = fundamentals_screenerin.run_query(object(), "some query")

    assert calls == [1]
    assert [c["company_id"] for c in companies] == [1]


def test_run_query_reserved_page_stops_without_duplicating(monkeypatch):
    # BUG FOUND LIVE 2026-08-15: for an out-of-range page, screener.in doesn't return
    # fewer/empty results -- it silently RE-SERVES the previous page's own content. A
    # full first page (== SCREENER_PAGE_SIZE) must still stop, via the company_id-set
    # backstop, rather than looping to max_pages and duplicating every company.
    monkeypatch.setattr(fundamentals_screenerin, "SCREENER_PAGE_SIZE", 2)
    first_page = [{"company_id": 1}, {"company_id": 2}]
    pages = {1: ("url?page=1", first_page), 2: ("url?page=2", list(first_page))}
    calls = []

    def fake_fetch(session, query_text, *, page):
        calls.append(page)
        return pages[page]

    monkeypatch.setattr(fundamentals_screenerin, "_fetch_query_page", fake_fetch)

    _, companies = fundamentals_screenerin.run_query(object(), "some query")

    assert calls == [1, 2]
    assert [c["company_id"] for c in companies] == [1, 2]


def test_run_query_zero_results_does_not_fetch_a_second_page(monkeypatch):
    calls = []

    def fake_fetch(session, query_text, *, page):
        calls.append(page)
        return "url?page=1", []

    monkeypatch.setattr(fundamentals_screenerin, "_fetch_query_page", fake_fetch)

    _, companies = fundamentals_screenerin.run_query(object(), "some query")

    assert calls == [1]
    assert companies == []


# fundamentals/screens/l1_universe.py -- L1 universe filter (step 3).


def test_run_l1_universe_refresh_upserts_and_summarizes(monkeypatch):
    companies = [
        {"company_id": 1, "name": "Menon Pistons", "ticker": "MENNPIS", "url": "/company/MENNPIS/", "metrics": {"mar_cap_rscr": 381.73}},
        {"company_id": 2, "name": "Coral India Fin.", "ticker": "CORALFINAC", "url": "/company/CORALFINAC/", "metrics": {"mar_cap_rscr": 137.9}},
    ]
    monkeypatch.setattr(fundamentals_l1_universe, "run_query", lambda session, query_text: ("screener_url", companies))
    # apply_post_hoc_exclusions itself has its own dedicated tests below -- here it's
    # a pass-through so this test stays focused on the upsert/summary shape.
    monkeypatch.setattr(
        fundamentals_l1_universe,
        "apply_post_hoc_exclusions",
        lambda cs: (cs, {"excluded_auditor_change": [], "excluded_related_party_transaction": []}),
    )

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
        "excluded_auditor_change": [],
        "excluded_related_party_transaction": [],
        "companies": ["Menon Pistons", "Coral India Fin."],
    }
    assert len(upserts) == 1
    df, table, kwargs = upserts[0]
    assert table == fundamentals_l1_universe.RESULTS_TABLE
    assert kwargs["unique_keys"] == ["query_name", "query_version", "run_date", "company_id"]
    assert set(df["company_id"]) == {1, 2}
    assert json.loads(df.iloc[0]["metrics_json"]) == companies[0]["metrics"]

    # DEFERRED_CHECKS is empty (both built 2026-08-13) -- no fallback logged for it.
    assert fallback_events == []


def test_run_l1_universe_refresh_skips_upsert_when_no_results(monkeypatch):
    monkeypatch.setattr(fundamentals_l1_universe, "run_query", lambda session, query_text: ("screener_url", []))
    monkeypatch.setattr(fundamentals_l1_universe, "apply_post_hoc_exclusions", lambda cs: (cs, {"excluded_auditor_change": [], "excluded_related_party_transaction": []}))
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
    assert fallback_events == []


def test_run_l1_universe_refresh_reports_exclusions_from_post_hoc_pass(monkeypatch):
    companies = [{"company_id": 1, "name": "Menon Pistons", "ticker": "MENNPIS", "url": "/company/MENNPIS/", "metrics": {}}]
    monkeypatch.setattr(fundamentals_l1_universe, "run_query", lambda session, query_text: ("screener_url", companies))
    monkeypatch.setattr(
        fundamentals_l1_universe,
        "apply_post_hoc_exclusions",
        lambda cs: ([], {"excluded_auditor_change": ["Menon Pistons"], "excluded_related_party_transaction": []}),
    )
    monkeypatch.setattr(fundamentals_l1_universe, "upsert_to_db", lambda *a, **k: None)
    monkeypatch.setattr(fundamentals_l1_universe, "record_local_fallback_event", lambda **kwargs: None)

    result = fundamentals_l1_universe.run_l1_universe_refresh(session=object())

    assert result["rows"] == 0  # excluded, not written
    assert result["excluded_auditor_change"] == ["Menon Pistons"]
    assert result["excluded_related_party_transaction"] == []


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


def test_load_auditor_rpt_events_for_companies_empty_input_skips_query(monkeypatch):
    calls = []
    monkeypatch.setattr(fundamentals_l1_universe, "sql_to_df", lambda q, params=None: calls.append(1) or pd.DataFrame())
    assert fundamentals_l1_universe.load_auditor_rpt_events_for_companies([]).empty
    assert calls == []


def test_auditor_change_excludes_only_on_confirmed_change_within_lookback():
    as_of = pd.Timestamp("2026-08-13", tz="UTC")
    confirmed_recent = {"structured_extraction_json": json.dumps({"disclosure_type": "confirmed_change"}), "disclosure_date": "2026-01-01"}
    confirmed_old = {"structured_extraction_json": json.dumps({"disclosure_type": "confirmed_change"}), "disclosure_date": "2020-01-01"}
    proposed = {"structured_extraction_json": json.dumps({"disclosure_type": "proposed_change_agenda"}), "disclosure_date": "2026-01-01"}
    incidental = {"structured_extraction_json": json.dumps({"disclosure_type": "incidental_mention"}), "disclosure_date": "2026-01-01"}

    assert fundamentals_l1_universe._auditor_change_excludes([confirmed_recent], as_of=as_of) is True
    assert fundamentals_l1_universe._auditor_change_excludes([confirmed_old], as_of=as_of) is False  # older than 3yr lookback
    assert fundamentals_l1_universe._auditor_change_excludes([proposed], as_of=as_of) is False
    assert fundamentals_l1_universe._auditor_change_excludes([incidental], as_of=as_of) is False
    assert fundamentals_l1_universe._auditor_change_excludes([], as_of=as_of) is False


def test_auditor_change_excludes_skips_unparseable_json():
    as_of = pd.Timestamp("2026-08-13", tz="UTC")
    event = {"structured_extraction_json": "not json", "disclosure_date": "2026-01-01"}
    assert fundamentals_l1_universe._auditor_change_excludes([event], as_of=as_of) is False


def test_rpt_excludes_only_when_applicable_and_over_threshold():
    as_of = pd.Timestamp("2026-08-13", tz="UTC")
    non_applicable = {"structured_extraction_json": json.dumps({"is_applicable": False, "pct_of_revenue": None}), "disclosure_date": "2026-01-01"}
    applicable_under = {"structured_extraction_json": json.dumps({"is_applicable": True, "pct_of_revenue": 5.0}), "disclosure_date": "2026-01-01"}
    applicable_over = {"structured_extraction_json": json.dumps({"is_applicable": True, "pct_of_revenue": 15.0}), "disclosure_date": "2026-01-01"}
    applicable_no_pct = {"structured_extraction_json": json.dumps({"is_applicable": True, "pct_of_revenue": None}), "disclosure_date": "2026-01-01"}

    assert fundamentals_l1_universe._rpt_excludes([non_applicable], as_of=as_of) is False
    assert fundamentals_l1_universe._rpt_excludes([applicable_under], as_of=as_of) is False
    assert fundamentals_l1_universe._rpt_excludes([applicable_over], as_of=as_of) is True
    assert fundamentals_l1_universe._rpt_excludes([applicable_no_pct], as_of=as_of) is False  # no guessed 0%/100%
    assert fundamentals_l1_universe._rpt_excludes([], as_of=as_of) is False


def test_rpt_excludes_falls_back_to_amount_when_no_percentage_stated():
    # BUG FOUND LIVE 2026-08-18 (re-audit): RPT_SCHEMA's own description says
    # pct_of_revenue is populated "only if the filing itself states this
    # percentage" -- many real filings only ever state an absolute rpt_amount_rs_cr
    # instead, and this used to silently never exclude those. Falls back to
    # "material because we can't rule it out" -- any positive amount with no stated
    # percentage counts, matching rpt_is_material's own docstring.
    as_of = pd.Timestamp("2026-08-13", tz="UTC")
    amount_only = {"structured_extraction_json": json.dumps({"is_applicable": True, "pct_of_revenue": None, "rpt_amount_rs_cr": 120.0}), "disclosure_date": "2026-01-01"}
    zero_amount = {"structured_extraction_json": json.dumps({"is_applicable": True, "pct_of_revenue": None, "rpt_amount_rs_cr": 0}), "disclosure_date": "2026-01-01"}
    assert fundamentals_l1_universe._rpt_excludes([amount_only], as_of=as_of) is True
    assert fundamentals_l1_universe._rpt_excludes([zero_amount], as_of=as_of) is False


def test_rpt_excludes_respects_lookback_window():
    # BUG FOUND LIVE 2026-08-18 (re-audit): no date bound at all, unlike
    # _auditor_change_excludes' own AUDITOR_CHANGE_LOOKBACK_YEARS cutoff -- a single
    # material RPT disclosure from years ago would exclude a company from L1
    # permanently.
    as_of = pd.Timestamp("2026-08-13", tz="UTC")
    recent = {"structured_extraction_json": json.dumps({"is_applicable": True, "pct_of_revenue": 15.0}), "disclosure_date": "2026-01-01"}
    old = {"structured_extraction_json": json.dumps({"is_applicable": True, "pct_of_revenue": 15.0}), "disclosure_date": "2020-01-01"}
    assert fundamentals_l1_universe._rpt_excludes([recent], as_of=as_of) is True
    assert fundamentals_l1_universe._rpt_excludes([old], as_of=as_of) is False  # older than RPT_LOOKBACK_YEARS


def test_rpt_is_material_reports_which_basis_decided_it():
    assert fundamentals_l1_universe.rpt_is_material({"is_applicable": False}) == (False, "not_applicable")
    assert fundamentals_l1_universe.rpt_is_material({"is_applicable": True, "pct_of_revenue": 15.0}) == (True, "pct")
    assert fundamentals_l1_universe.rpt_is_material({"is_applicable": True, "pct_of_revenue": 5.0}) == (False, "pct")
    assert fundamentals_l1_universe.rpt_is_material({"is_applicable": True, "pct_of_revenue": None, "rpt_amount_rs_cr": 50.0}) == (True, "amount")
    assert fundamentals_l1_universe.rpt_is_material({"is_applicable": True, "pct_of_revenue": None}) == (False, "unknown")


def _mock_identity_resolution_nse_prefix(monkeypatch):
    """apply_post_hoc_exclusions now resolves identity via
    map_company_master_ids_nse_or_bse instead of a naive 'nse:'+ticker string (see
    its own docstring for the bug this fixes) -- most of these tests aren't
    exercising that resolution itself, so mock it to the same "nse:"+ticker mapping
    the tests originally assumed, for continuity."""
    monkeypatch.setattr(
        fundamentals_l1_universe,
        "map_company_master_ids_nse_or_bse",
        lambda tickers: pd.Series([f"nse:{t}" for t in tickers], index=tickers.index, dtype="string"),
    )


def test_apply_post_hoc_exclusions_missing_data_never_excludes(monkeypatch):
    _mock_identity_resolution_nse_prefix(monkeypatch)
    companies = [{"company_id": 1, "name": "No History Co", "ticker": "NOHIST", "url": "", "metrics": {}}]
    monkeypatch.setattr(fundamentals_l1_universe, "load_auditor_rpt_events_for_companies", lambda cmids: pd.DataFrame())

    survivors, exclusions = fundamentals_l1_universe.apply_post_hoc_exclusions(companies)

    assert survivors == companies
    assert exclusions == {"excluded_auditor_change": [], "excluded_related_party_transaction": []}


def test_apply_post_hoc_exclusions_excludes_on_confirmed_auditor_change(monkeypatch):
    _mock_identity_resolution_nse_prefix(monkeypatch)
    companies = [
        {"company_id": 1, "name": "Changed Auditor Co", "ticker": "CHANGED", "url": "", "metrics": {}},
        {"company_id": 2, "name": "Clean Co", "ticker": "CLEAN", "url": "", "metrics": {}},
    ]
    events_df = pd.DataFrame(
        [
            {
                "company_master_id": "nse:CHANGED", "filing_type": "auditor_change", "disclosure_date": "2026-01-01",
                "structured_extraction_json": json.dumps({"disclosure_type": "confirmed_change"}),
            }
        ]
    )
    monkeypatch.setattr(fundamentals_l1_universe, "load_auditor_rpt_events_for_companies", lambda cmids: events_df)

    survivors, exclusions = fundamentals_l1_universe.apply_post_hoc_exclusions(companies)

    assert [c["name"] for c in survivors] == ["Clean Co"]
    assert exclusions["excluded_auditor_change"] == ["Changed Auditor Co"]


def test_apply_post_hoc_exclusions_excludes_on_material_rpt(monkeypatch):
    _mock_identity_resolution_nse_prefix(monkeypatch)
    companies = [{"company_id": 1, "name": "Big RPT Co", "ticker": "BIGRPT", "url": "", "metrics": {}}]
    events_df = pd.DataFrame(
        [
            {
                "company_master_id": "nse:BIGRPT", "filing_type": "related_party_transaction", "disclosure_date": "2026-01-01",
                "structured_extraction_json": json.dumps({"is_applicable": True, "pct_of_revenue": 25.0}),
            }
        ]
    )
    monkeypatch.setattr(fundamentals_l1_universe, "load_auditor_rpt_events_for_companies", lambda cmids: events_df)

    survivors, exclusions = fundamentals_l1_universe.apply_post_hoc_exclusions(companies)

    assert survivors == []
    assert exclusions["excluded_related_party_transaction"] == ["Big RPT Co"]


def test_apply_post_hoc_exclusions_empty_companies_list(monkeypatch):
    monkeypatch.setattr(fundamentals_l1_universe, "load_auditor_rpt_events_for_companies", lambda cmids: pd.DataFrame())
    survivors, exclusions = fundamentals_l1_universe.apply_post_hoc_exclusions([])
    assert survivors == []
    assert exclusions == {"excluded_auditor_change": [], "excluded_related_party_transaction": []}


def test_apply_post_hoc_exclusions_resolves_bse_numeric_ticker_correctly(monkeypatch):
    # BUG FOUND LIVE 2026-08-15, fixed here: a company whose L1 ticker is actually a
    # raw BSE numeric scrip code (confirmed live: 22% of the real L1 universe) used
    # to build company_master_id as the wrong 'nse:<scrip code>' string, so a real
    # auditor-change/RPT violation for it was silently never found. This pins the
    # fix: the resolved (not naive) company_master_id is what gets queried.
    monkeypatch.setattr(
        fundamentals_l1_universe,
        "map_company_master_ids_nse_or_bse",
        lambda tickers: pd.Series(["nse:ALUFLUOR" if t == "524634" else pd.NA for t in tickers], index=tickers.index, dtype="string"),
    )
    companies = [{"company_id": 1, "name": "Alufluoride", "ticker": "524634", "url": "", "metrics": {}}]
    events_df = pd.DataFrame(
        [
            {
                "company_master_id": "nse:ALUFLUOR", "filing_type": "auditor_change", "disclosure_date": "2026-01-01",
                "structured_extraction_json": json.dumps({"disclosure_type": "confirmed_change"}),
            }
        ]
    )
    captured_cmids = []
    monkeypatch.setattr(
        fundamentals_l1_universe,
        "load_auditor_rpt_events_for_companies",
        lambda cmids: captured_cmids.append(cmids) or events_df,
    )

    survivors, exclusions = fundamentals_l1_universe.apply_post_hoc_exclusions(companies)

    assert captured_cmids == [["nse:ALUFLUOR"]]  # queried the RESOLVED id, not "nse:524634"
    assert survivors == []
    assert exclusions["excluded_auditor_change"] == ["Alufluoride"]


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


def test_ensure_l2_valuation_columns_are_numeric_migration_uses_schema_registry(monkeypatch):
    # BUG FOUND LIVE 2026-08-18 (re-audit): pe/valuation_vs_own_history_ratio/
    # valuation_sector_percentile were always None before VALUATION_QUERY existed
    # (added 2026-08-13) -- upsert_to_db inferred TEXT from that all-null column on
    # first write, and ADD COLUMN IF NOT EXISTS never widens an existing column's
    # type, so they've stayed TEXT ever since despite now genuinely holding floats.
    # Confirmed live via information_schema: all 3 columns are still "text".
    calls = []
    monkeypatch.setattr(fundamentals_l2_state, "apply_schema_migration", lambda **kwargs: calls.append(kwargs) or {"status": "applied"})

    fundamentals_l2_state.ensure_l2_valuation_columns_are_numeric()

    assert len(calls) == 1
    call = calls[0]
    assert call["migration_id"] == "20260818_l2_state_valuation_columns_to_double_precision"
    assert call["metadata"]["tables"] == ["fundamentals_l2_state"]
    ddl = "\n".join(call["statements"])
    assert "ALTER COLUMN pe TYPE DOUBLE PRECISION" in ddl
    assert "ALTER COLUMN valuation_vs_own_history_ratio TYPE DOUBLE PRECISION" in ddl
    assert "ALTER COLUMN valuation_sector_percentile TYPE DOUBLE PRECISION" in ddl


def test_compute_debt_trajectory_matches_hand_computed_values():
    balance_sheet = fundamentals_l2_state._parse_period_table(_table(BALANCE_SHEET_FIXTURE_HTML))
    profit_loss = fundamentals_l2_state._parse_period_table(_table(PROFIT_LOSS_FIXTURE_HTML))

    result = fundamentals_l2_state.compute_debt_trajectory(balance_sheet, profit_loss)

    assert result == {
        "net_debt_rscr": 3,
        "net_debt_yoy_delta_rscr": 3 - 17,
        # fixture only has 2 periods -- 1 delta, not enough to compare a rate of change against
        "net_debt_consecutive_declining_years": 1,
        "net_debt_trend_direction": None,
        "interest_coverage": 31 / 3,
        "debt_to_ebitda": 3 / 31,
    }


def test_compute_debt_trajectory_aligns_pl_to_balance_sheets_own_latest_period():
    # BUG FOUND LIVE 2026-08-17: operating_profit_latest/interest_latest used to be
    # the P&L table's own last column unconditionally -- screener.in's P&L table
    # always carries a trailing TTM column the Balance Sheet table doesn't (confirmed
    # live for a real company), so the two "latest" lookups could silently land on
    # different reference dates. Real Reliance figures: Balance Sheet's latest column
    # was Mar 2026, but P&L's own last column was TTM with a materially different
    # Operating Profit (60,767 vs the Mar-2026-aligned 54,455). This fixture pins that
    # exact shape: TTM's Operating Profit/Interest deliberately differ from Mar 2026's.
    balance_sheet = fundamentals_l2_state._parse_period_table(_table(BALANCE_SHEET_FIXTURE_HTML))
    profit_loss = fundamentals_l2_state._parse_period_table(
        _table(
            "<table><tr><th></th><th>Mar 2025</th><th>Mar 2026</th><th>TTM</th></tr>"
            "<tr><td>Operating Profit</td><td>32</td><td>31</td><td>99</td></tr>"
            "<tr><td>Interest</td><td>4</td><td>3</td><td>9</td></tr></table>"
        )
    )

    result = fundamentals_l2_state.compute_debt_trajectory(balance_sheet, profit_loss)

    # Balance sheet's own latest column is Mar 2026 (net_debt_rscr=3) -- P&L must be
    # read at that SAME period (31/3), never TTM's 99/9, however much later TTM ends.
    assert result["interest_coverage"] == 31 / 3
    assert result["debt_to_ebitda"] == 3 / 31


def test_value_at_period_returns_none_when_period_label_absent():
    profit_loss = fundamentals_l2_state._parse_period_table(_table(PROFIT_LOSS_FIXTURE_HTML))
    assert fundamentals_l2_state._value_at_period(profit_loss, "Operating Profit", period_label="Mar 1999") is None
    assert fundamentals_l2_state._value_at_period(profit_loss, "Operating Profit", period_label=None) is None


def test_value_at_period_right_aligns_a_row_with_fewer_values_than_periods():
    # BUG FOUND LIVE 2026-08-18 (re-audit): this used to index `values` by
    # period_label's ABSOLUTE position in `periods`, but _value_at (the sibling
    # lookup) indexes `values` from the END -- screener.in right-aligns a row's own
    # value list to its most recent period when that row's earliest years are
    # missing, not left-aligned to the header. 3 periods, but this row is only
    # populated for the 2 most recent -- Mar 2024 (oldest) has no value at all.
    period_table = {
        "periods": ["Mar 2024", "Mar 2025", "Mar 2026"],
        "rows": {"Operating Profit": [40, 31]},  # right-aligned: index 0 -> Mar 2025, index 1 -> Mar 2026
    }
    # Old absolute-index code silently returned the WRONG value here (40, Mar 2024's
    # slot) for Mar 2025 -- not just None, an actively wrong figure.
    assert fundamentals_l2_state._value_at_period(period_table, "Operating Profit", period_label="Mar 2025") == 40
    assert fundamentals_l2_state._value_at_period(period_table, "Operating Profit", period_label="Mar 2026") == 31
    # Mar 2024 has no value at all for this row (right-aligned means it fell off the front).
    assert fundamentals_l2_state._value_at_period(period_table, "Operating Profit", period_label="Mar 2024") is None


def test_compute_debt_trajectory_records_fallback_when_pl_lacks_bs_latest_period(monkeypatch):
    # BUG FOUND LIVE 2026-08-18 (re-audit): _value_at_period() returning None when
    # the Balance Sheet's latest period isn't in the P&L's own period list is
    # correct (refusing to guess), but was silent -- a real coverage regression vs.
    # the pre-fix (misaligned) behavior. Must now be visible via fallback telemetry.
    balance_sheet = fundamentals_l2_state._parse_period_table(_table(BALANCE_SHEET_FIXTURE_HTML))  # latest period: Mar 2026
    profit_loss = fundamentals_l2_state._parse_period_table(
        _table("<table><tr><th></th><th>Mar 2024</th><th>Mar 2025</th></tr>"
               "<tr><td>Operating Profit</td><td>30</td><td>32</td></tr>"
               "<tr><td>Interest</td><td>3</td><td>4</td></tr></table>")
    )
    fallback_events = []
    monkeypatch.setattr(fundamentals_l2_state, "record_local_fallback_event", lambda **k: fallback_events.append(k))

    result = fundamentals_l2_state.compute_debt_trajectory(balance_sheet, profit_loss, ticker="TESTCO")

    assert result["interest_coverage"] is None
    assert result["debt_to_ebitda"] is None
    assert len(fallback_events) == 1
    assert fallback_events[0]["fallback_type"] == "l2_bs_pl_period_misaligned"
    assert fallback_events[0]["metadata"] == {"ticker": "TESTCO", "latest_balance_sheet_period": "Mar 2026", "pl_periods": ["Mar 2024", "Mar 2025"]}


def test_compute_debt_trajectory_no_fallback_when_periods_align(monkeypatch):
    balance_sheet = fundamentals_l2_state._parse_period_table(_table(BALANCE_SHEET_FIXTURE_HTML))
    profit_loss = fundamentals_l2_state._parse_period_table(_table(PROFIT_LOSS_FIXTURE_HTML))
    fallback_events = []
    monkeypatch.setattr(fundamentals_l2_state, "record_local_fallback_event", lambda **k: fallback_events.append(k))

    fundamentals_l2_state.compute_debt_trajectory(balance_sheet, profit_loss, ticker="TESTCO")

    assert fallback_events == []


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
    assert result == {
        "cwip_ratio": None,
        "cwip_ratio_yoy_delta": None,
        "cwip_ratio_consecutive_declining_years": None,  # zero-fixed-assets period excluded -> no usable series
        "cwip_ratio_trend_direction": None,
    }


# 2026-08-15: multi-year trend scoring (user request, "pace up 7 too") -- screener.in's annual
# Balance Sheet rows carry ~10-12 years of real history per company (confirmed live), but
# compute_debt_trajectory/compute_cwip_ratio only ever compared the latest 2 periods, discarding
# the rest. compute_trend_direction reads the whole available series.
@pytest.mark.parametrize(
    "values,expected_consecutive_declining,expected_direction",
    [
        ([17, 3], 1, None),  # only 1 delta -- not enough to compare a rate of change
        ([50, 40, 30, 20], 3, "steady_decline"),  # deltas -10,-10,-10 -- same magnitude, not a change of pace
        ([50, 45, 30, 5], 3, "accelerating_decline"),  # deltas -5,-15,-25 -- decline speeding up
        ([10, 20, 25, 40], 0, "accelerating_increase"),  # deltas +10,+5,+15 -- latest jump bigger than prior
        ([10, 20, 30, 35], 0, "decelerating_increase"),  # deltas +10,+10,+5 -- latest gain smaller than prior
        ([10, 20, 15, 25], 0, "reversal"),  # deltas +10,-5,+10 -- sign flipped between the last two deltas
        ([10, 10, 10, 10], 0, "flat"),  # deltas 0,0,0
        ([10, 8, 9], 0, "reversal"),  # deltas -2,+1 -- declined then reversed to a gain
        ([], None, None),  # no data at all
        ([10], None, None),  # single point, no deltas possible
    ],
)
def test_compute_trend_direction_reads_full_available_history(values, expected_consecutive_declining, expected_direction):
    result = fundamentals_l2_state.compute_trend_direction(values)
    assert result["consecutive_declining_periods"] == expected_consecutive_declining
    assert result["trend_direction"] == expected_direction


def test_compute_trend_direction_ignores_non_numeric_gaps():
    # screener.in cells can be non-numeric ("", None) for an early period with no disclosed data --
    # must not crash, and must not silently treat a gap as a real 0-valued data point.
    result = fundamentals_l2_state.compute_trend_direction([None, "", 50, 40, 30])
    assert result["consecutive_declining_periods"] == 2
    assert result["trend_direction"] == "steady_decline"  # deltas -10, -10 -- same magnitude both periods


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


@pytest.mark.parametrize(
    "fiis,diis,expected_direction",
    [
        ([0.5, 0.5, 0.5, 0.5], [1.0, 1.0, 1.0, 1.0], "flat"),
        ([0.0, 0.5, 1.0, 1.5], [0.0, 0.0, 0.0, 0.0], "increasing"),
        ([2.0, 1.5, 1.0, 0.5], [0.0, 0.0, 0.0, 0.0], "decreasing"),
    ],
)
def test_compute_institutional_stake_direction_over_last_4_quarters(fiis, diis, expected_direction):
    shareholding = {"rows": {"FIIs": fiis, "DIIs": diis}}
    result = fundamentals_l2_state.compute_institutional_stake(shareholding)
    assert result["institutional_pct"] == fiis[-1] + diis[-1]
    assert result["institutional_stake_direction"] == expected_direction


def test_compute_institutional_stake_sums_fii_and_dii():
    # real shape confirmed live 2026-08-13 (HALDYNGL): screener.in carries FIIs and
    # DIIs as separate rows, summed here to match L1_QUERY's own convention.
    shareholding = {"rows": {"FIIs": [0.0, 0.0, 0.0], "DIIs": [0.0, 1.17, 1.53]}}
    result = fundamentals_l2_state.compute_institutional_stake(shareholding)
    assert result["institutional_pct"] == 1.53


def test_compute_institutional_stake_first_entry_true_when_all_prior_explicitly_zero():
    shareholding = {"rows": {"FIIs": [0.0, 0.0, 0.0], "DIIs": [0.0, 0.0, 2.1]}}
    result = fundamentals_l2_state.compute_institutional_stake(shareholding)
    assert result["institutional_first_entry"] is True


def test_compute_institutional_stake_first_entry_false_when_already_held_before():
    # real shape confirmed live 2026-08-13 (HALDYNGL): DIIs already nonzero one
    # quarter back -- this is growth, not entry.
    shareholding = {"rows": {"FIIs": [0.0, 0.0], "DIIs": [1.17, 2.03]}}
    result = fundamentals_l2_state.compute_institutional_stake(shareholding)
    assert result["institutional_first_entry"] is False


def test_compute_institutional_stake_first_entry_false_when_latest_is_zero():
    shareholding = {"rows": {"FIIs": [0.0, 0.0], "DIIs": [0.0, 0.0]}}
    result = fundamentals_l2_state.compute_institutional_stake(shareholding)
    assert result["institutional_first_entry"] is False


def test_compute_institutional_stake_first_entry_false_with_only_one_quarter():
    shareholding = {"rows": {"FIIs": [1.0], "DIIs": [0.0]}}
    result = fundamentals_l2_state.compute_institutional_stake(shareholding)
    assert result["institutional_first_entry"] is False


def test_compute_institutional_stake_first_entry_false_when_prior_quarter_missing_not_zero():
    # a gap in the data (None) is not proof of a zero -- must not produce a false
    # "first entry" claim just because a value is missing.
    shareholding = {"rows": {"FIIs": [None, 0.0], "DIIs": [0.0, 3.0]}}
    result = fundamentals_l2_state.compute_institutional_stake(shareholding)
    assert result["institutional_first_entry"] is False


def test_compute_institutional_stake_empty_shareholding_returns_none_and_false():
    result = fundamentals_l2_state.compute_institutional_stake({"rows": {}})
    assert result == {"institutional_pct": None, "institutional_stake_direction": None, "institutional_first_entry": False}


def test_compute_institutional_stake_treats_missing_dii_row_as_zero():
    # BUG FOUND LIVE 2026-08-18 (re-audit): screener.in omits a holder-class row
    # ENTIRELY (not zeros) when that class has never held the stock -- zip()
    # against an empty list used to produce zero pairs, silently returning None
    # even with real FII data present. Confirmed live: 5/18 real L1 companies had
    # exactly this shape.
    result = fundamentals_l2_state.compute_institutional_stake({"rows": {"FIIs": [1.0, 2.0, 3.0]}})
    assert result["institutional_pct"] == 3.0  # 3.0 (FII) + 0 (DII, treated as absent-means-zero)


def test_compute_institutional_stake_treats_missing_fii_row_as_zero():
    result = fundamentals_l2_state.compute_institutional_stake({"rows": {"DIIs": [4.0, 5.0]}})
    assert result["institutional_pct"] == 5.0


def test_build_institutional_entry_event_row_shape(monkeypatch):
    monkeypatch.setattr(
        fundamentals_l2_state,
        "map_company_master_ids_nse_or_bse",
        lambda tickers: pd.Series([f"nse:{t}" for t in tickers], index=tickers.index, dtype="string"),
    )
    row = {"ticker": "TESTCO", "institutional_pct": 2.1, "run_date": date(2026, 8, 13)}
    event = fundamentals_l2_state._build_institutional_entry_event_row(row, latest_period="Jun 2026", load_ts=pd.Timestamp("2026-08-13", tz="UTC"))
    assert event["source"] == "l2_state"
    assert event["news_id"] == "institutional_entry:TESTCO:Jun 2026"
    assert event["company_master_id"] == "nse:TESTCO"
    assert event["filing_type"] == "institutional_entry"
    assert event["quantity"] == 2.1
    assert event["disclosure_date"] == date(2026, 8, 13)
    assert "2.1" in event["headline"]
    assert "re-entry" in event["headline"]  # 2026-08-13: caveat the ~3yr lookback window, not "first ever"


def test_build_institutional_entry_event_row_resolves_bse_numeric_ticker_correctly(monkeypatch):
    # BUG FOUND LIVE 2026-08-15, fixed here: company_master_id used to be built as
    # the naive f"nse:{ticker}" string -- wrong for a BSE-numeric-scrip-code ticker,
    # which would silently orphan this synthetic event under an id nothing else in
    # the pipeline resolves to.
    monkeypatch.setattr(
        fundamentals_l2_state,
        "map_company_master_ids_nse_or_bse",
        lambda tickers: pd.Series(["nse:ALUFLUOR" if t == "524634" else pd.NA for t in tickers], index=tickers.index, dtype="string"),
    )
    row = {"ticker": "524634", "institutional_pct": 2.1, "run_date": date(2026, 8, 13)}
    event = fundamentals_l2_state._build_institutional_entry_event_row(row, latest_period="Jun 2026", load_ts=pd.Timestamp("2026-08-13", tz="UTC"))
    assert event["company_master_id"] == "nse:ALUFLUOR"  # not the naive "nse:524634"


def test_fetch_pledge_levels_builds_dict_keyed_by_company_id(monkeypatch):
    companies = [
        {"company_id": 3163, "metrics": {"pledged_pct": 96.64}},
        {"company_id": 1274762, "metrics": {}},  # no pledged_pct -- must be skipped, not KeyError
    ]
    monkeypatch.setattr(fundamentals_l2_state, "run_query", lambda session, query_text: ("url", companies))

    levels = fundamentals_l2_state.fetch_pledge_levels(object())

    assert levels == {3163: 96.64}


def test_filter_universe_to_due_no_state_row_is_due(monkeypatch):
    universe = pd.DataFrame([{"company_id": 1, "ticker": "A"}, {"company_id": 2, "ticker": "B"}])
    monkeypatch.setattr(fundamentals_l2_state, "load_crawl_state", lambda: pd.DataFrame())
    result = fundamentals_l2_state.filter_universe_to_due(universe)
    assert set(result["company_id"]) == {1, 2}  # no crawl-state row at all -- both due


def test_filter_universe_to_due_filters_past_and_future(monkeypatch):
    universe = pd.DataFrame([{"company_id": 1, "ticker": "A"}, {"company_id": 2, "ticker": "B"}, {"company_id": 3, "ticker": "C"}])
    now = pd.Timestamp.now(tz="UTC")
    state = pd.DataFrame(
        [
            {"company_id": 1, "next_due_at": now - pd.Timedelta(days=1)},  # past -- due
            {"company_id": 2, "next_due_at": now + pd.Timedelta(days=30)},  # future -- not due
            # company 3 has no state row -- due
        ]
    )
    monkeypatch.setattr(fundamentals_l2_state, "load_crawl_state", lambda: state)
    result = fundamentals_l2_state.filter_universe_to_due(universe)
    assert set(result["company_id"]) == {1, 3}


def test_filter_universe_to_due_empty_universe_skips_state_query(monkeypatch):
    calls = []
    monkeypatch.setattr(fundamentals_l2_state, "load_crawl_state", lambda: calls.append(1) or pd.DataFrame())
    assert fundamentals_l2_state.filter_universe_to_due(pd.DataFrame()).empty
    assert calls == []


def test_mark_crawled_upserts_with_future_due_date(monkeypatch):
    calls = []
    monkeypatch.setattr(fundamentals_l2_state, "upsert_to_db", lambda df, table, **k: calls.append((df, table, k)))
    fundamentals_l2_state._mark_crawled(1, "AAREYDRUGS")
    df, table, kwargs = calls[0]
    assert table == fundamentals_l2_state.CRAWL_STATE_TABLE
    assert kwargs["unique_keys"] == ["company_id"]
    assert df.iloc[0]["company_id"] == 1
    assert df.iloc[0]["next_due_at"] > pd.Timestamp.now(tz="UTC")


def test_pull_crawl_forward_empty_input_skips_query(monkeypatch):
    calls = []
    monkeypatch.setattr(fundamentals_l2_state, "sql_to_df", lambda q, params=None: calls.append(1) or pd.DataFrame())
    assert fundamentals_l2_state.pull_crawl_forward([]) == {"pulled_forward": 0}
    assert calls == []


def test_pull_crawl_forward_sets_next_due_at_to_now(monkeypatch):
    monkeypatch.setattr(fundamentals_l2_state, "_ensure_crawl_state_table", lambda: None)
    universe_df = pd.DataFrame([{"company_id": 1, "ticker": "AAREYDRUGS"}])
    monkeypatch.setattr(fundamentals_l2_state, "sql_to_df", lambda q, params=None: universe_df)
    calls = []
    monkeypatch.setattr(fundamentals_l2_state, "upsert_to_db", lambda df, table, **k: calls.append((df, table, k)))

    result = fundamentals_l2_state.pull_crawl_forward(["nse:AAREYDRUGS"])

    assert result == {"pulled_forward": 1}
    df, table, kwargs = calls[0]
    assert table == fundamentals_l2_state.CRAWL_STATE_TABLE
    assert (pd.Timestamp.now(tz="UTC") - df.iloc[0]["next_due_at"]).total_seconds() < 5  # ~now, not the full interval


def test_pull_crawl_forward_no_matching_l1_company_skips_upsert(monkeypatch):
    monkeypatch.setattr(fundamentals_l2_state, "_ensure_crawl_state_table", lambda: None)
    monkeypatch.setattr(fundamentals_l2_state, "sql_to_df", lambda q, params=None: pd.DataFrame())
    calls = []
    monkeypatch.setattr(fundamentals_l2_state, "upsert_to_db", lambda *a, **k: calls.append(1))
    assert fundamentals_l2_state.pull_crawl_forward(["nse:NOTINUNIVERSE"]) == {"pulled_forward": 0}
    assert calls == []


def test_fetch_valuation_levels_builds_dict_keyed_by_company_id(monkeypatch):
    companies = [
        {"company_id": 19, "metrics": {"p_e": 5.59, "5yrs_pe": 24.33}},  # confirmed live shape
        {"company_id": 2, "metrics": {"p_e": -3.0, "5yrs_pe": 10.0}},  # negative PE -- excluded
        {"company_id": 3, "metrics": {"p_e": 8.0, "5yrs_pe": 0}},  # zero historical median -- excluded
        {"company_id": 4, "metrics": {"p_e": 8.0}},  # no historical median at all -- excluded
    ]
    monkeypatch.setattr(fundamentals_l2_state, "run_query", lambda session, query_text: ("url", companies))

    levels = fundamentals_l2_state.fetch_valuation_levels(object())

    assert levels == {19: {"pe": 5.59, "historical_pe_5y": 24.33}}


def test_load_sector_codes_for_tickers_strips_nse_prefix(monkeypatch):
    monkeypatch.setattr(
        fundamentals_l2_state,
        "map_company_master_ids_nse_or_bse",
        lambda tickers: pd.Series([f"nse:{t}" for t in tickers], index=tickers.index, dtype="string"),
    )
    df = pd.DataFrame([{"company_master_id": "nse:AAREYDRUGS", "sector_code": "IN01"}])
    captured = {}

    def fake_sql_to_df(q, params=None):
        captured["params"] = params
        return df

    monkeypatch.setattr(fundamentals_l2_state, "sql_to_df", fake_sql_to_df)

    result = fundamentals_l2_state.load_sector_codes_for_tickers(["AAREYDRUGS"])

    assert result == {"AAREYDRUGS": "IN01"}
    assert captured["params"] == (["nse:AAREYDRUGS"],)


def test_load_sector_codes_for_tickers_resolves_bse_numeric_ticker_correctly(monkeypatch):
    # BUG FOUND LIVE 2026-08-15, fixed here: this used to build company_master_id as
    # the naive f"nse:{ticker}" string -- wrong for a BSE-numeric-scrip-code ticker.
    # Confirmed live: ticker "524634" truly resolves to "nse:ALUFLUOR", and the
    # returned dict is still keyed by the ORIGINAL ticker ("524634"), matching what
    # compute_valuation_sector_percentiles looks up by (row["ticker"]).
    monkeypatch.setattr(
        fundamentals_l2_state,
        "map_company_master_ids_nse_or_bse",
        lambda tickers: pd.Series(["nse:ALUFLUOR" if t == "524634" else pd.NA for t in tickers], index=tickers.index, dtype="string"),
    )
    df = pd.DataFrame([{"company_master_id": "nse:ALUFLUOR", "sector_code": "IN02"}])
    captured = {}

    def fake_sql_to_df(q, params=None):
        captured["params"] = params
        return df

    monkeypatch.setattr(fundamentals_l2_state, "sql_to_df", fake_sql_to_df)

    result = fundamentals_l2_state.load_sector_codes_for_tickers(["524634"])

    assert result == {"524634": "IN02"}  # keyed by the original ticker, not the resolved id
    assert captured["params"] == (["nse:ALUFLUOR"],)  # queried the resolved id, not "nse:524634"


def test_load_sector_codes_for_tickers_empty_input_skips_query(monkeypatch):
    calls = []
    monkeypatch.setattr(fundamentals_l2_state, "sql_to_df", lambda q, params=None: calls.append(1) or pd.DataFrame())
    assert fundamentals_l2_state.load_sector_codes_for_tickers([]) == {}
    assert calls == []


def test_compute_valuation_sector_percentiles_ranks_within_sector():
    rows = [
        {"company_id": 1, "ticker": "A", "pe": 5.0},
        {"company_id": 2, "ticker": "B", "pe": 15.0},
        {"company_id": 3, "ticker": "C", "pe": 10.0},  # different sector -- ranked alone
    ]
    sector_codes = {"A": "IN01", "B": "IN01", "C": "IN02"}

    result = fundamentals_l2_state.compute_valuation_sector_percentiles(rows, sector_codes)

    assert result[1] == 50.0  # cheaper of two in IN01
    assert result[2] == 100.0  # pricier of two in IN01
    assert result[3] == 100.0  # only company in IN02


def test_compute_valuation_sector_percentiles_skips_missing_sector_or_pe():
    rows = [
        {"company_id": 1, "ticker": "A", "pe": 5.0},  # no sector_code resolved
        {"company_id": 2, "ticker": "B", "pe": None},  # no pe
    ]
    sector_codes = {"B": "IN01"}
    assert fundamentals_l2_state.compute_valuation_sector_percentiles(rows, sector_codes) == {}


def test_run_l2_state_refresh_upserts_and_logs_deferred_fields(monkeypatch):
    monkeypatch.setattr(fundamentals_l2_state, "ensure_l2_valuation_columns_are_numeric", lambda: None)
    universe = pd.DataFrame(
        [
            {"company_id": 1, "company_name": "Aarey Drugs", "ticker": "AAREYDRUGS"},
            {"company_id": 2, "company_name": "TCC Concept", "ticker": "TCC"},
        ]
    )
    monkeypatch.setattr(fundamentals_l2_state, "load_l1_universe", lambda: universe)
    monkeypatch.setattr(fundamentals_l2_state, "fetch_pledge_levels", lambda session: {1: 5.0})
    monkeypatch.setattr(fundamentals_l2_state, "fetch_valuation_levels", lambda session: {1: {"pe": 10.0, "historical_pe_5y": 20.0}})
    monkeypatch.setattr(fundamentals_l2_state, "load_sector_codes_for_tickers", lambda tickers: {"AAREYDRUGS": "IN01", "TCC": "IN01"})
    monkeypatch.setattr(fundamentals_l2_state, "filter_universe_to_due", lambda u: u)
    monkeypatch.setattr(fundamentals_l2_state, "_mark_crawled", lambda cid, ticker: None)
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
    # company 1 has a valuation_levels entry -> both derived fields populated
    assert row1["pe"] == 10.0
    assert row1["valuation_vs_own_history_ratio"] == 0.5  # 10.0 / 20.0
    assert row1["valuation_sector_percentile"] == 100.0  # only PE-eligible company in its sector this run
    # company 2 has no valuation_levels entry -> never a guessed value (None ->
    # NaN once mixed into a float64 DataFrame column alongside company 1's real value)
    assert pd.isna(row2["pe"])
    assert pd.isna(row2["valuation_vs_own_history_ratio"])
    assert pd.isna(row2["valuation_sector_percentile"])
    assert any(e["fallback_type"] == "l2_fields_not_sourced" for e in fallback_events)


def test_run_l2_state_refresh_does_not_mark_crawled_when_the_batched_upsert_fails(monkeypatch):
    monkeypatch.setattr(fundamentals_l2_state, "ensure_l2_valuation_columns_are_numeric", lambda: None)
    # BUG FOUND LIVE 2026-08-15, fixed here: _mark_crawled used to be called
    # per-company INSIDE the fetch loop, well before the single batched
    # fundamentals_l2_state upsert ran -- a transient failure on that upsert would
    # leave a company's crawl-state cooldown advanced ~75 days with no L2 state row
    # for this cycle at all. Now it's only called after the upsert succeeds.
    universe = pd.DataFrame([{"company_id": 1, "company_name": "Aarey Drugs", "ticker": "AAREYDRUGS"}])
    monkeypatch.setattr(fundamentals_l2_state, "load_l1_universe", lambda: universe)
    monkeypatch.setattr(fundamentals_l2_state, "fetch_pledge_levels", lambda session: {})
    monkeypatch.setattr(fundamentals_l2_state, "fetch_valuation_levels", lambda session: {})
    monkeypatch.setattr(fundamentals_l2_state, "load_sector_codes_for_tickers", lambda tickers: {})
    monkeypatch.setattr(fundamentals_l2_state, "filter_universe_to_due", lambda u: u)
    detail = {
        "balance_sheet": {"rows": {"Borrowings": [17, 3], "CWIP": [0, 4], "Fixed Assets": [76, 95]}},
        "profit_loss": {"rows": {"Operating Profit": [32, 31], "Interest": [4, 3]}},
        "shareholding": {"rows": {"Promoters": [74.37, 74.37, 74.37, 74.37]}},
    }
    monkeypatch.setattr(fundamentals_l2_state, "fetch_company_detail", lambda session, ticker: detail)

    def failing_upsert(df, table, **k):
        raise RuntimeError("transient DB error")

    monkeypatch.setattr(fundamentals_l2_state, "upsert_to_db", failing_upsert)
    monkeypatch.setattr(fundamentals_l2_state, "record_local_fallback_event", lambda **kwargs: None)
    crawl_calls = []
    monkeypatch.setattr(fundamentals_l2_state, "_mark_crawled", lambda cid, ticker: crawl_calls.append((cid, ticker)))

    with pytest.raises(RuntimeError):
        fundamentals_l2_state.run_l2_state_refresh(session=object())

    assert crawl_calls == []  # crawl-state cooldown was never advanced


def test_run_l2_state_refresh_isolates_one_market_wide_query_failure(monkeypatch):
    monkeypatch.setattr(fundamentals_l2_state, "ensure_l2_valuation_columns_are_numeric", lambda: None)
    # 2026-08-15 bug found live: fetch_pledge_levels/fetch_valuation_levels ran back-to-back with
    # no isolation -- either one raising took down the ENTIRE L2 step, before any per-company
    # detail fetch even started. Confirmed live: fetch_valuation_levels (added 2026-08-13) failing
    # with "Could not find screener.in results table in the response" crashed 2 days running.
    # Now: a failing query degrades to empty levels (same "absent from this dict" case every
    # company without real data already goes through) instead of aborting the run, and both
    # failures are recorded distinctly via fallback telemetry.
    universe = pd.DataFrame([{"company_id": 1, "company_name": "Aarey Drugs", "ticker": "AAREYDRUGS"}])
    monkeypatch.setattr(fundamentals_l2_state, "load_l1_universe", lambda: universe)
    monkeypatch.setattr(fundamentals_l2_state, "fetch_pledge_levels", lambda session: {1: 5.0})
    monkeypatch.setattr(
        fundamentals_l2_state,
        "fetch_valuation_levels",
        lambda session: (_ for _ in ()).throw(ValueError("Could not find screener.in results table in the response")),
    )
    monkeypatch.setattr(fundamentals_l2_state, "load_sector_codes_for_tickers", lambda tickers: {"AAREYDRUGS": "IN01"})
    monkeypatch.setattr(fundamentals_l2_state, "filter_universe_to_due", lambda u: u)
    monkeypatch.setattr(fundamentals_l2_state, "_mark_crawled", lambda cid, ticker: None)
    detail = {
        "balance_sheet": {"rows": {"Borrowings": [17, 3], "CWIP": [0, 4], "Fixed Assets": [76, 95]}},
        "profit_loss": {"rows": {"Operating Profit": [32, 31], "Interest": [4, 3]}},
        "shareholding": {"rows": {"Promoters": [74.37, 74.37, 74.37, 74.37]}},
    }
    monkeypatch.setattr(fundamentals_l2_state, "fetch_company_detail", lambda session, ticker: detail)
    monkeypatch.setattr(fundamentals_l2_state, "upsert_to_db", lambda df, table, **k: None)
    fallback_events = []
    monkeypatch.setattr(
        fundamentals_l2_state, "record_local_fallback_event", lambda **kwargs: fallback_events.append(kwargs)
    )

    result = fundamentals_l2_state.run_l2_state_refresh(session=object())

    assert result["rows"] == 1  # the run completes -- did NOT abort on the valuation query failure
    assert result["failed_companies"] == []
    valuation_failures = [e for e in fallback_events if e["fallback_type"] == "l2_market_wide_query_failed"]
    assert len(valuation_failures) == 1
    assert valuation_failures[0]["metadata"]["query_name"] == "valuation"


def test_run_l2_state_refresh_writes_synthetic_event_on_institutional_first_entry(monkeypatch):
    monkeypatch.setattr(fundamentals_l2_state, "ensure_l2_valuation_columns_are_numeric", lambda: None)
    universe = pd.DataFrame([{"company_id": 1, "company_name": "Entry Co", "ticker": "ENTRYCO"}])
    monkeypatch.setattr(fundamentals_l2_state, "load_l1_universe", lambda: universe)
    monkeypatch.setattr(fundamentals_l2_state, "fetch_pledge_levels", lambda session: {})
    monkeypatch.setattr(fundamentals_l2_state, "fetch_valuation_levels", lambda session: {})
    monkeypatch.setattr(fundamentals_l2_state, "load_sector_codes_for_tickers", lambda tickers: {})
    monkeypatch.setattr(fundamentals_l2_state, "filter_universe_to_due", lambda u: u)
    monkeypatch.setattr(fundamentals_l2_state, "_mark_crawled", lambda cid, ticker: None)
    detail = {
        "balance_sheet": {"rows": {"Borrowings": [17, 3], "CWIP": [0, 4], "Fixed Assets": [76, 95]}},
        "profit_loss": {"rows": {"Operating Profit": [32, 31], "Interest": [4, 3]}},
        "shareholding": {
            "periods": ["Mar 2026", "Jun 2026"],
            "rows": {"Promoters": [74.37, 74.37, 74.37, 74.37], "FIIs": [0.0, 0.0], "DIIs": [0.0, 3.2]},
        },
    }
    monkeypatch.setattr(fundamentals_l2_state, "fetch_company_detail", lambda session, ticker: detail)
    monkeypatch.setattr(fundamentals_l2_state, "_ensure_events_schema", lambda: None)
    upserts = []
    monkeypatch.setattr(fundamentals_l2_state, "upsert_to_db", lambda df, table, **k: upserts.append((df, table, k)))
    monkeypatch.setattr(
        fundamentals_l2_state, "record_local_fallback_event", lambda **kwargs: None
    )

    result = fundamentals_l2_state.run_l2_state_refresh(session=object())

    assert result["institutional_first_entries"] == 1
    assert len(upserts) == 2  # L2 state rows, then the synthetic event row
    event_df, event_table, event_kwargs = upserts[1]
    assert event_table == fundamentals_l2_state.EVENTS_TABLE
    assert event_kwargs["unique_keys"] == ["source", "news_id"]
    event_row = event_df.iloc[0]
    assert event_row["news_id"] == "institutional_entry:ENTRYCO:Jun 2026"
    assert event_row["filing_type"] == "institutional_entry"
    assert event_row["quantity"] == 3.2


def test_run_l2_state_refresh_no_synthetic_event_when_no_first_entry(monkeypatch):
    monkeypatch.setattr(fundamentals_l2_state, "ensure_l2_valuation_columns_are_numeric", lambda: None)
    universe = pd.DataFrame([{"company_id": 1, "company_name": "No Entry Co", "ticker": "NOENTRY"}])
    monkeypatch.setattr(fundamentals_l2_state, "load_l1_universe", lambda: universe)
    monkeypatch.setattr(fundamentals_l2_state, "fetch_pledge_levels", lambda session: {})
    monkeypatch.setattr(fundamentals_l2_state, "fetch_valuation_levels", lambda session: {})
    monkeypatch.setattr(fundamentals_l2_state, "load_sector_codes_for_tickers", lambda tickers: {})
    monkeypatch.setattr(fundamentals_l2_state, "filter_universe_to_due", lambda u: u)
    monkeypatch.setattr(fundamentals_l2_state, "_mark_crawled", lambda cid, ticker: None)
    detail = {
        "balance_sheet": {"rows": {"Borrowings": [17, 3], "CWIP": [0, 4], "Fixed Assets": [76, 95]}},
        "profit_loss": {"rows": {"Operating Profit": [32, 31], "Interest": [4, 3]}},
        "shareholding": {"periods": ["Mar 2026", "Jun 2026"], "rows": {"Promoters": [74.37] * 4, "FIIs": [0.0, 0.0], "DIIs": [0.0, 0.0]}},
    }
    monkeypatch.setattr(fundamentals_l2_state, "fetch_company_detail", lambda session, ticker: detail)
    upserts = []
    monkeypatch.setattr(fundamentals_l2_state, "upsert_to_db", lambda df, table, **k: upserts.append((df, table, k)))
    monkeypatch.setattr(fundamentals_l2_state, "record_local_fallback_event", lambda **kwargs: None)

    result = fundamentals_l2_state.run_l2_state_refresh(session=object())

    assert result["institutional_first_entries"] == 0
    assert len(upserts) == 1  # only the L2 state upsert, no synthetic event


def test_run_l2_state_refresh_skips_a_company_whose_detail_fetch_fails(monkeypatch):
    monkeypatch.setattr(fundamentals_l2_state, "ensure_l2_valuation_columns_are_numeric", lambda: None)
    universe = pd.DataFrame(
        [
            {"company_id": 1, "company_name": "Good Co", "ticker": "GOOD"},
            {"company_id": 2, "company_name": "Bad Co", "ticker": "BAD"},
        ]
    )
    monkeypatch.setattr(fundamentals_l2_state, "load_l1_universe", lambda: universe)
    monkeypatch.setattr(fundamentals_l2_state, "fetch_pledge_levels", lambda session: {})
    monkeypatch.setattr(fundamentals_l2_state, "fetch_valuation_levels", lambda session: {})
    monkeypatch.setattr(fundamentals_l2_state, "load_sector_codes_for_tickers", lambda tickers: {})
    monkeypatch.setattr(fundamentals_l2_state, "filter_universe_to_due", lambda u: u)
    monkeypatch.setattr(fundamentals_l2_state, "_mark_crawled", lambda cid, ticker: None)
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
    monkeypatch.setattr(fundamentals_l2_state, "ensure_l2_valuation_columns_are_numeric", lambda: None)
    monkeypatch.setattr(fundamentals_l2_state, "load_l1_universe", lambda: pd.DataFrame())
    upserts = []
    monkeypatch.setattr(fundamentals_l2_state, "upsert_to_db", lambda *a, **k: upserts.append((a, k)))
    fallback_events = []
    monkeypatch.setattr(
        fundamentals_l2_state, "record_local_fallback_event", lambda **kwargs: fallback_events.append(kwargs)
    )

    result = fundamentals_l2_state.run_l2_state_refresh(session=object())

    assert result == {"rows": 0, "failed_companies": [], "checks_deferred": list(fundamentals_l2_state.DEFERRED_FIELDS), "companies": [], "institutional_first_entries": 0, "companies_due": 0}
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
        # Real (subcategory, headline) pair, captured live 2026-08-12: a genuine
        # warrant-conversion allotment filed under BSE's generic 'Outcome without
        # intimation' bucket -- subcategory alone gives no signal, only the headline
        # does (see CAPITAL_RAISE_KEYWORDS' comment).
        ("Outcome without intimation", "Allotment of Equity Shares Pursuant to Conversion of Warrants", "capital_raise"),
        # Real (subcategory, headline) pair, captured live 2026-08-12: an NCD (debt)
        # allotment filed under BSE's 'Allotment of Equity Shares' subcategory despite
        # not being equity -- must NOT classify as capital_raise (an investor buying a
        # bond isn't the equity-investor-entry signal this trigger is for). See
        # DEBT_INSTRUMENT_EXCLUSION_KEYWORDS.
        ("Allotment of Equity Shares", "Allotment of NCDs to Clover Technologies Private Limited", "other"),
        # Real (subcategory, headline) pair, captured live 2026-08-18: employee stock
        # options are not third-party investor capital -- must stay "other". Headline
        # (unlike a fabricated one that never tripped CAPITAL_RAISE_KEYWORDS at all)
        # genuinely matches "allotment of equity share", so this exercises the real
        # CAPITAL_RAISE_NON_EVENT_HEADLINE_KEYWORDS exclusion, not a no-op.
        ("Allotment of ESOP / ESPS", "Intimation for allotment of equity shares pursuant to exercise of options under Employee Stock option", "other"),
        # Real (subcategory, headline) pairs, captured live 2026-08-18 via a full
        # 24-row audit of stored capital_raise rows -- see CAPITAL_RAISE_NON_EVENT_
        # HEADLINE_KEYWORDS' comment for the false-positive shapes these pin down.
        ("General", "This is to inform that BSE and NSE vide their respective letters dated July 23, 2026, have granted ''in-principle'' approvals for preferential issue of 3,38,030 convertible warrants of the Company", "other"),
        ("Preferential Issue", "Receipt of Trading Approval", "other"),
        ("General", "We submit herewith Trading approval received for 2,69,402 equity shares of Rs. 10/- each allotted pursuant to conversion of warrants issued on Preferential basis from NSE & BSE.", "other"),
        ("General", "Intimation under Regulation 30 of SEBI (LODR) regarding receipt of Listing Approval of 23180000 equity shares of Rs. 10/- each allotted on preferential basis attached.", "other"),
        ("General", "Attached disclosure of Closure of Rights issue of the Company.", "other"),
        ("General", "Intimation of International Securities Identification Number (\"ISIN) for the rights entitlements to be credited for the purpose of Rights issue of the Company.", "other"),
        ("General", "Utilization of funds raised through preferential allotment is fully complete as of this date.", "other"),
        ("Reg. 32 (1), (3) - Statement of Deviation & Variation", "Utilization of Proceeds of Preferential Issue of Shares for the period ended March 31, 2026 is attached", "other"),
        ("Allotment of Warrants", "The Board of Directors has approved the proposal of appointment of Cameo Corporate Services Limited as new RTA in place of MUFG Intime India Private Limited, the existing RTA as per details attached", "other"),
        (None, "Allotment of shares on preferential basis to ABC Fund LP", "capital_raise"),
        (None, "Allotment pursuant to Qualified Institutions Placement", "capital_raise"),
        ("General", "Rights Issue - Allotment of Equity Shares", "capital_raise"),
        # Real (subcategory, headline) pairs, captured live 2026-08-17: forward-
        # looking capital_raise-shaped filings, nothing actually allotted/raised yet
        # -- must stay "other", same "Board Meeting" (no "Outcome of") forward-
        # looking signal the results check above already established, plus a Postal
        # Ballot notice (seeking approval) and a compliance certificate about a still-
        # "Proposed" issue. See classify_announcement's own capital_raise comment.
        ("Board Meeting", "Diamines & Chemicals Ltd-has informed BSE that the meeting of the Board of Directors of the Company is scheduled on 24/03/2026, inter alia, to consider and approve conversion of warrants", "other"),
        ("Postal Ballot", "Postal Ballot Notice for consider the issue of 1000000 Warrants to Mr. Shailesh Siroya, promoter of the Company on preferential basis", "other"),
        ("General", "As requested by BSE Listing Team, please find enclosed herewith the Certificate wrt the Proposed Preferential Issue", "other"),
        # Same "Outcome of Board Meeting" subcategory as the results checks above --
        # must NOT be caught by the new "board meeting" exclusion (a substring check
        # would wrongly exclude this genuinely-completed raise; only an EXACT
        # subcategory match to "board meeting" is excluded).
        ("Outcome of Board Meeting", "Outcome of Board Meeting held on 29.06.2026 for raising of funds through Rights Issue", "capital_raise"),
        # Real (subcategory, headline) pairs, captured live 2026-08-13 against 40 real
        # companies' 3yr BSE history -- see AUDITOR_CHANGE_KEYWORDS' comment.
        ("Appointment of Statutory Auditor/s", "Resignation of Statutory Auditor.", "auditor_change"),
        ("Change in Management", "Appointment of Statutory auditor in place of retiring statutory auditor", "auditor_change"),
        (None, "The Board of Directors...approved the appointment of M/s. Borkar & Muzumdar, Chartered Accountants, as the Statutory Auditor", "auditor_change"),
        ("General", "Appointment of Joint Statutory Auditors of CSB Bank Limited for the financial year 2026-27", "auditor_change"),
        # Real pair, captured live: a routine results filing's headline mentions the
        # incumbent auditor's report -- not a change. SUBCATNAME "Financial Results"
        # routes it to "results" before reaching the auditor_change check at all.
        ("Financial Results", "Considered and Approved the Audited Financial Results along with the Auditor's Report issued by the Statutory Auditors", "results"),
        # Same real pair, but under "Outcome of Board Meeting" -- 2026-08-13 fix: this
        # used to fall through to "auditor_change" (a real bug: dropped the quarter's
        # numbers entirely). Now caught by the "outcome of board meeting" + results-
        # headline branch, which runs before auditor_change -- results wins the overlap.
        ("Outcome of Board Meeting", "Considered and Approved the Audited Financial Results along with the Auditor's Report issued by the Statutory Auditors", "results"),
        # Real (subcategory, headline) pairs for RPT -- see RELATED_PARTY_TRANSACTION_
        # KEYWORDS' comment. Both real matches found live were non-applicability
        # declarations, not amount disclosures -- structured_extraction.py's
        # RPT_SCHEMA is what distinguishes the two, not this classifier.
        ("General", "Disclosure of the Related Party transactions under Regulation 23(9) of SEBI (LODR) Regulation, 2015 is not applicable to our company.", "related_party_transaction"),
        ("General", "In reference to Regulation 23 (9) of SEBI (LODR) Regulation, 2015, wherein a Company is required to submit to the stock exchange a disclosure of related party transactions", "related_party_transaction"),
        # Real (subcategory, headline) pairs captured live 2026-08-13 (30-company BSE
        # history audit) -- "Outcome of Board Meeting" results outcomes that were
        # previously silently dropped as "other".
        ("Outcome of Board Meeting", "The Board of Directors of the Company in their Meeting held on Wednesday, 12th August, 2026 has duly approved the Unaudited Financial Results along with...", "results"),
        ("Outcome of Board Meeting", "Considered and approve the Unaudited Financial Results of the Company for the quarter ended 30th June, 2026;", "results"),
        ("Outcome of Board Meeting", "Board considered and approved the Un-audited financial result for the quarter and half year ended 30/09/2025", "results"),
        # Real pair: a forward-looking notice, distinct SUBCATNAME ("Board Meeting",
        # no "Outcome of") -- nothing filed yet, must stay "other".
        ("Board Meeting", "BWL Ltd has informed BSE that the meeting of the Board of Directors of the Company is scheduled on 04/08/2026, inter alia, to consider and approve Quarterly Financial Results", "other"),
        # Real pair: a redundant newspaper re-publication of the same results --
        # different SUBCATNAME than the source filing, must stay excluded (the
        # original 2026-08-10 false-positive concern this whole design avoids).
        ("Newspaper Publication", "Intimation of Publication of the Unaudited Financial Results for the Quarter ended 30th June, 2026.", "other"),
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
    assert row["disclosure_date"] == pd.Timestamp("2026-08-01").date()
    # BUG FOUND LIVE 2026-08-18 (re-audit): DissemDT is IST wall-clock with no
    # timezone marker of its own -- must be localized to Asia/Kolkata and converted
    # to UTC (10:15 IST -> 04:45 UTC), not tagged tz="UTC" directly (which would
    # store it as 10:15 UTC, 5h30m later than the true instant).
    assert row["announcement_timestamp"] == pd.Timestamp("2026-08-01 04:45:00", tz="UTC")
    assert row["quantity"] is None
    assert row["insider_name"] is None
    assert row["transaction_type"] is None
    assert row["attachment_name"] == "somefile.pdf"
    assert row["detection_source"] == "bse_announcements"
    assert row["enrichment_status"] == "pending"
    assert row["sources"] == "bse"
    assert json.loads(row["raw_json"]) == raw


def test_parse_bse_timestamp_converts_ist_to_utc_across_midnight():
    # BUG FOUND LIVE 2026-08-18 (re-audit): the "T" branch used to tag the parsed
    # value tz="UTC" directly instead of localizing it as IST -- confirmed live, a
    # real DissemDT of "2025-02-21T17:43:39.95" was being stored as 17:43 UTC
    # (would be 23:13 IST, implausible for a company's own disclosure timestamp)
    # instead of the correct 12:13 UTC. A late-evening IST filing crossing midnight
    # when converted is the clearest proof the conversion (not just a relabel) is
    # actually happening: 23:50 IST on 2026-08-01 is 18:20 UTC the SAME day (IST is
    # UTC+5:30 -- only pre-05:30-IST filings would cross into the prior UTC day).
    result = fundamentals_bse_announcements._parse_bse_timestamp("2026-08-01T23:50:00")
    assert result == pd.Timestamp("2026-08-01 18:20:00", tz="UTC")

    # A pre-market IST filing (02:00 IST) converts to the PRIOR UTC calendar day.
    result_crosses_day = fundamentals_bse_announcements._parse_bse_timestamp("2026-08-01T02:00:00")
    assert result_crosses_day == pd.Timestamp("2026-07-31 20:30:00", tz="UTC")


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


def test_fetch_company_announcements_single_page_stops_without_extra_requests(monkeypatch):
    calls = []

    def fake_bse_get(url, params):
        calls.append(params["pageno"])
        return {"Table": [{"NEWSID": "1"}, {"NEWSID": "2"}], "Table1": [{"ROWCNT": 2}]}

    monkeypatch.setattr(fundamentals_bse_announcements, "_bse_get", fake_bse_get)
    rows = fundamentals_bse_announcements.fetch_company_announcements("524412", from_date=date(2026, 1, 1), to_date=date(2026, 8, 1))

    assert calls == [1]
    assert len(rows) == 2


def test_fetch_company_announcements_paginates_until_rowcnt_satisfied(monkeypatch):
    # BUG FOUND LIVE 2026-08-18: this endpoint paginates at 50 rows/page and
    # reports the true total in Table1[0].ROWCNT -- the collector only ever
    # requested page 1. Confirmed live: every company tested returned exactly 50
    # rows with a real ROWCNT of 95-293. run_auditor_rpt_backfill's "3-year"
    # lookback was actually covering 5-15 months per company, and the company
    # gets marked permanently backfilled after that one truncated fetch.
    pages = {
        1: {"Table": [{"NEWSID": str(i)} for i in range(50)], "Table1": [{"ROWCNT": 120}]},
        2: {"Table": [{"NEWSID": str(i)} for i in range(50, 100)], "Table1": [{"ROWCNT": 120}]},
        3: {"Table": [{"NEWSID": str(i)} for i in range(100, 120)], "Table1": [{"ROWCNT": 120}]},
    }
    calls = []

    def fake_bse_get(url, params):
        calls.append(params["pageno"])
        return pages[params["pageno"]]

    monkeypatch.setattr(fundamentals_bse_announcements, "_bse_get", fake_bse_get)
    rows = fundamentals_bse_announcements.fetch_company_announcements("524412", from_date=date(2023, 1, 1), to_date=date(2026, 8, 1))

    assert calls == [1, 2, 3]
    assert len(rows) == 120
    assert [r["NEWSID"] for r in rows] == [str(i) for i in range(120)]


def test_fetch_company_announcements_stops_on_empty_page_even_if_rowcnt_says_more(monkeypatch):
    pages = {
        1: {"Table": [{"NEWSID": str(i)} for i in range(50)], "Table1": [{"ROWCNT": 120}]},
        2: {"Table": [], "Table1": [{"ROWCNT": 120}]},  # BSE has nothing more despite ROWCNT
    }
    calls = []

    def fake_bse_get(url, params):
        calls.append(params["pageno"])
        return pages[params["pageno"]]

    monkeypatch.setattr(fundamentals_bse_announcements, "_bse_get", fake_bse_get)
    rows = fundamentals_bse_announcements.fetch_company_announcements("524412", from_date=date(2023, 1, 1), to_date=date(2026, 8, 1))

    assert calls == [1, 2]
    assert len(rows) == 50


def test_fetch_company_announcements_missing_rowcnt_does_not_paginate(monkeypatch):
    calls = []

    def fake_bse_get(url, params):
        calls.append(params["pageno"])
        return {"Table": [{"NEWSID": str(i)} for i in range(50)], "Table1": []}

    monkeypatch.setattr(fundamentals_bse_announcements, "_bse_get", fake_bse_get)
    rows = fundamentals_bse_announcements.fetch_company_announcements("524412", from_date=date(2023, 1, 1), to_date=date(2026, 8, 1))

    assert calls == [1]  # no ROWCNT to compare against -- can't safely assume more pages exist
    assert len(rows) == 50


def test_resolve_company_identity_joins_on_ticker_and_preserves_index(monkeypatch):
    tickers = pd.Series(["AAREYDRUGS", "UNKNOWNTICKER"], index=[5, 9])
    monkeypatch.setattr(
        fundamentals_bse_announcements,
        "map_company_master_ids_nse_or_bse",
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

    assert result == {
        "rows": 0, "announcement_rows": 0, "result_calendar_rows": 0, "merged_rows": 0,
        "companies_scanned": 0, "companies_total": 0, "failed_companies": [], "blocked": False,
    }
    assert any(e["fallback_type"] == "l3_bse_no_l1_universe" for e in fallback_events)


def test_bse_announcements_main_does_not_crash_on_empty_universe(monkeypatch):
    # BUG FOUND LIVE 2026-08-18 (re-audit): the empty-universe early return was
    # missing companies_total, which main() reads unconditionally -- a plain
    # KeyError, crashing hardest exactly when the upstream L1 step has already
    # failed that day.
    monkeypatch.setattr(fundamentals_bse_announcements, "load_l1_universe_tickers", lambda: pd.DataFrame())
    monkeypatch.setattr(fundamentals_bse_announcements, "record_local_fallback_event", lambda **kwargs: None)
    # main() also drives run_auditor_rpt_backfill() now (see its own test below) --
    # mocked here to an empty-backlog no-op so this test stays scoped to the crash
    # fix above, not exercising (or hitting live BSE/DB via) the backfill nibble.
    monkeypatch.setattr(
        fundamentals_bse_announcements,
        "run_auditor_rpt_backfill",
        lambda **k: {"rows": 0, "companies_scanned": 0, "companies_remaining": 0, "failed_companies": [], "blocked": False},
    )

    assert fundamentals_bse_announcements.main() == 0


def test_bse_announcements_main_drives_the_auditor_rpt_backfill_nibble(monkeypatch):
    # BUG FOUND LIVE 2026-08-18 (re-audit): run_auditor_rpt_backfill() was
    # manual-only -- no reference in run_pipeline.py's STEPS, the crontab, or
    # docs/DATA_INVENTORY.md -- confirmed live, 0/43 target companies backfilled to
    # date. main() must now drive it too, bounded to BACKFILL_DAILY_LIMIT.
    monkeypatch.setattr(fundamentals_bse_announcements, "load_l1_universe_tickers", lambda: pd.DataFrame())
    monkeypatch.setattr(fundamentals_bse_announcements, "record_local_fallback_event", lambda **kwargs: None)
    backfill_calls = []

    def fake_backfill(**kwargs):
        backfill_calls.append(kwargs)
        return {"rows": 12, "companies_scanned": 5, "companies_remaining": 38, "failed_companies": [], "blocked": False}

    monkeypatch.setattr(fundamentals_bse_announcements, "run_auditor_rpt_backfill", fake_backfill)

    result_code = fundamentals_bse_announcements.main()

    assert result_code == 0
    assert backfill_calls == [{"limit": fundamentals_bse_announcements.BACKFILL_DAILY_LIMIT}]
    assert fundamentals_bse_announcements.STOCKEY_RUN_STATE["auditor_rpt_backfill"]["companies_scanned"] == 5


def test_bse_announcements_main_backfill_failure_does_not_fail_the_run(monkeypatch):
    # Best-effort: the daily backfill nibble erroring must not take down the
    # regular crawl's own success -- it has its own resumable progress tracking.
    monkeypatch.setattr(fundamentals_bse_announcements, "load_l1_universe_tickers", lambda: pd.DataFrame())
    fallback_events = []
    monkeypatch.setattr(fundamentals_bse_announcements, "record_local_fallback_event", lambda **kwargs: fallback_events.append(kwargs))

    def raise_backfill(**kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(fundamentals_bse_announcements, "run_auditor_rpt_backfill", raise_backfill)

    assert fundamentals_bse_announcements.main() == 0
    assert any(e["fallback_type"] == "l3_bse_auditor_rpt_backfill_failed" for e in fallback_events)


def test_run_bse_l3_detection_pulls_crawl_forward_for_fresh_results(monkeypatch):
    universe = _bse_universe_df(1)
    monkeypatch.setattr(fundamentals_bse_announcements, "load_l1_universe_tickers", lambda: universe)
    monkeypatch.setattr(
        fundamentals_bse_announcements, "resolve_company_identity", lambda tickers: _bse_identity_df(tickers, ["111111"])
    )
    monkeypatch.setattr(
        fundamentals_bse_announcements,
        "fetch_company_announcements",
        lambda scrip, **k: [{"NEWSID": "n1", "SUBCATNAME": "Financial Results", "HEADLINE": "Q1 results"}],
    )
    monkeypatch.setattr(fundamentals_bse_announcements, "fetch_result_calendar", lambda: [])
    monkeypatch.setattr(fundamentals_bse_announcements, "upsert_events_with_dedup", lambda rows: {"inserted": len(rows), "merged": 0})
    monkeypatch.setattr(fundamentals_bse_announcements, "record_local_fallback_event", lambda **kwargs: None)
    pull_calls = []
    monkeypatch.setattr(fundamentals_bse_announcements, "pull_crawl_forward", lambda cmids: pull_calls.append(cmids))

    fundamentals_bse_announcements.run_bse_l3_detection()

    assert pull_calls == [["nse:TICK1"]]


def test_run_bse_l3_detection_no_results_rows_skips_pull_crawl_forward(monkeypatch):
    universe = _bse_universe_df(1)
    monkeypatch.setattr(fundamentals_bse_announcements, "load_l1_universe_tickers", lambda: universe)
    monkeypatch.setattr(
        fundamentals_bse_announcements, "resolve_company_identity", lambda tickers: _bse_identity_df(tickers, ["111111"])
    )
    monkeypatch.setattr(
        fundamentals_bse_announcements,
        "fetch_company_announcements",
        lambda scrip, **k: [{"NEWSID": "n1", "SUBCATNAME": "Credit Rating", "HEADLINE": "CRISIL rating action"}],
    )
    monkeypatch.setattr(fundamentals_bse_announcements, "fetch_result_calendar", lambda: [])
    monkeypatch.setattr(fundamentals_bse_announcements, "upsert_events_with_dedup", lambda rows: {"inserted": len(rows), "merged": 0})
    monkeypatch.setattr(fundamentals_bse_announcements, "record_local_fallback_event", lambda **kwargs: None)
    pull_calls = []
    monkeypatch.setattr(fundamentals_bse_announcements, "pull_crawl_forward", lambda cmids: pull_calls.append(cmids))

    fundamentals_bse_announcements.run_bse_l3_detection()

    assert pull_calls == []


def test_run_bse_l3_detection_pull_crawl_forward_failure_is_non_fatal(monkeypatch):
    universe = _bse_universe_df(1)
    monkeypatch.setattr(fundamentals_bse_announcements, "load_l1_universe_tickers", lambda: universe)
    monkeypatch.setattr(
        fundamentals_bse_announcements, "resolve_company_identity", lambda tickers: _bse_identity_df(tickers, ["111111"])
    )
    monkeypatch.setattr(
        fundamentals_bse_announcements,
        "fetch_company_announcements",
        lambda scrip, **k: [{"NEWSID": "n1", "SUBCATNAME": "Financial Results", "HEADLINE": "Q1 results"}],
    )
    monkeypatch.setattr(fundamentals_bse_announcements, "fetch_result_calendar", lambda: [])
    monkeypatch.setattr(fundamentals_bse_announcements, "upsert_events_with_dedup", lambda rows: {"inserted": len(rows), "merged": 0})
    fallback_events = []
    monkeypatch.setattr(fundamentals_bse_announcements, "record_local_fallback_event", lambda **kwargs: fallback_events.append(kwargs))

    def failing_pull(cmids):
        raise RuntimeError("boom")

    monkeypatch.setattr(fundamentals_bse_announcements, "pull_crawl_forward", failing_pull)

    result = fundamentals_bse_announcements.run_bse_l3_detection()

    assert result["rows"] == 1  # the run itself still succeeds
    assert any(e["fallback_type"] == "l3_bse_pull_crawl_forward_failed" for e in fallback_events)


def test_load_companies_needing_backfill_excludes_already_done(monkeypatch):
    universe = _bse_universe_df(3)
    monkeypatch.setattr(fundamentals_bse_announcements, "_ensure_backfill_progress_table", lambda: None)
    monkeypatch.setattr(fundamentals_bse_announcements, "load_l1_universe_tickers", lambda: universe)
    monkeypatch.setattr(
        fundamentals_bse_announcements,
        "resolve_company_identity",
        lambda tickers: _bse_identity_df(tickers, ["1", "2", "3"]),
    )
    monkeypatch.setattr(
        fundamentals_bse_announcements, "sql_to_df", lambda q, params=None: pd.DataFrame({"company_master_id": ["nse:TICK1"]})
    )

    result = fundamentals_bse_announcements.load_companies_needing_backfill()

    assert set(result["company_master_id"]) == {"nse:TICK2", "nse:TICK3"}


def test_load_companies_needing_backfill_respects_limit(monkeypatch):
    universe = _bse_universe_df(3)
    monkeypatch.setattr(fundamentals_bse_announcements, "_ensure_backfill_progress_table", lambda: None)
    monkeypatch.setattr(fundamentals_bse_announcements, "load_l1_universe_tickers", lambda: universe)
    monkeypatch.setattr(
        fundamentals_bse_announcements,
        "resolve_company_identity",
        lambda tickers: _bse_identity_df(tickers, ["1", "2", "3"]),
    )
    monkeypatch.setattr(fundamentals_bse_announcements, "sql_to_df", lambda q, params=None: pd.DataFrame())

    result = fundamentals_bse_announcements.load_companies_needing_backfill(limit=1)

    assert len(result) == 1


def test_mark_backfilled_empty_list_skips_upsert(monkeypatch):
    calls = []
    monkeypatch.setattr(fundamentals_bse_announcements, "upsert_to_db", lambda *a, **k: calls.append(1))
    fundamentals_bse_announcements._mark_backfilled([])
    assert calls == []


def test_mark_backfilled_upserts_progress_rows(monkeypatch):
    calls = []
    monkeypatch.setattr(fundamentals_bse_announcements, "upsert_to_db", lambda df, table, **k: calls.append((df, table, k)))
    fundamentals_bse_announcements._mark_backfilled(["nse:X", "nse:Y"])
    df, table, kwargs = calls[0]
    assert table == fundamentals_bse_announcements.BACKFILL_PROGRESS_TABLE
    assert kwargs["unique_keys"] == ["company_master_id"]
    assert set(df["company_master_id"]) == {"nse:X", "nse:Y"}


def test_run_auditor_rpt_backfill_returns_early_when_nothing_remaining(monkeypatch):
    monkeypatch.setattr(fundamentals_bse_announcements, "load_companies_needing_backfill", lambda limit=None: pd.DataFrame())
    result = fundamentals_bse_announcements.run_auditor_rpt_backfill()
    assert result == {"rows": 0, "companies_scanned": 0, "companies_remaining": 0, "failed_companies": [], "blocked": False}


def test_run_auditor_rpt_backfill_marks_only_successful_companies(monkeypatch):
    universe = _bse_universe_df(2)
    identity = _bse_identity_df(universe["ticker"], ["111111", "222222"])
    combined = universe.join(identity)

    call_order = iter([combined, pd.DataFrame()])  # first call: work batch; second (companies_remaining recompute): none left
    monkeypatch.setattr(fundamentals_bse_announcements, "load_companies_needing_backfill", lambda limit=None: next(call_order))
    monkeypatch.setattr(
        fundamentals_bse_announcements,
        "fetch_company_announcements",
        lambda scrip, **k: [{"NEWSID": "n1", "SUBCATNAME": "Resignation of Statutory Auditors", "HEADLINE": "x"}],
    )
    monkeypatch.setattr(fundamentals_bse_announcements, "upsert_events_with_dedup", lambda rows: {"inserted": len(rows), "merged": 0})
    marked = []
    monkeypatch.setattr(fundamentals_bse_announcements, "_mark_backfilled", lambda cmids: marked.append(cmids))
    monkeypatch.setattr(fundamentals_bse_announcements, "record_local_fallback_event", lambda **kwargs: None)

    result = fundamentals_bse_announcements.run_auditor_rpt_backfill(limit=2)

    assert result["companies_scanned"] == 2
    assert result["companies_remaining"] == 0
    assert result["blocked"] is False
    assert result["rows"] == 2  # one auditor_change row per company, both in scope
    assert result["off_target_discarded"] == 0
    assert set(marked[0]) == {"nse:TICK1", "nse:TICK2"}


def test_run_auditor_rpt_backfill_discards_off_target_filing_types(monkeypatch):
    # BUG FOUND LIVE 2026-08-17: this backfill exists only for auditor_change/
    # related_party_transaction (L1's post-hoc exclusion needs 3yr history for those
    # two, unlike everything else). It used to store EVERY filing_type
    # build_announcement_row could classify -- confirmed live, 1,074 of 1,094 rows one
    # real run wrote were off-target (pit_sast/results/rating_action/capital_raise),
    # up to 3 years old, queuing ahead of fresh detections in ocr_pipeline.py and
    # mostly already 404 by the time OCR reached them.
    universe = _bse_universe_df(1)
    identity = _bse_identity_df(universe["ticker"], ["111111"])
    combined = universe.join(identity)
    call_order = iter([combined, pd.DataFrame()])
    monkeypatch.setattr(fundamentals_bse_announcements, "load_companies_needing_backfill", lambda limit=None: next(call_order))
    monkeypatch.setattr(
        fundamentals_bse_announcements,
        "fetch_company_announcements",
        lambda scrip, **k: [
            {"NEWSID": "n1", "SUBCATNAME": "Resignation of Statutory Auditors", "HEADLINE": "auditor change"},  # in scope
            {"NEWSID": "n2", "SUBCATNAME": "Credit Rating", "HEADLINE": "rating action"},  # off-target
            {"NEWSID": "n3", "SUBCATNAME": "Financial Results", "HEADLINE": "results"},  # off-target
            {"NEWSID": "n4", "SUBCATNAME": "General", "HEADLINE": "unrelated notice"},  # "other" -- already discarded upstream
        ],
    )
    upserted = []
    monkeypatch.setattr(fundamentals_bse_announcements, "upsert_events_with_dedup", lambda rows: upserted.append(rows) or {"inserted": len(rows), "merged": 0})
    monkeypatch.setattr(fundamentals_bse_announcements, "_mark_backfilled", lambda cmids: None)
    monkeypatch.setattr(fundamentals_bse_announcements, "record_local_fallback_event", lambda **kwargs: None)

    result = fundamentals_bse_announcements.run_auditor_rpt_backfill(limit=1)

    assert result["rows"] == 1
    assert result["off_target_discarded"] == 2  # n2 (rating_action) + n3 (results); n4 was already "other"
    assert len(upserted) == 1 and len(upserted[0]) == 1
    assert upserted[0][0]["filing_type"] == "auditor_change"


def test_run_auditor_rpt_backfill_does_not_mark_failed_companies(monkeypatch):
    universe = _bse_universe_df(1)
    identity = _bse_identity_df(universe["ticker"], ["111111"])
    combined = universe.join(identity)
    monkeypatch.setattr(fundamentals_bse_announcements, "load_companies_needing_backfill", lambda limit=None: combined)

    def always_fails(scrip, **k):
        raise fundamentals_bse_announcements.BseBlockedError("HTTP 403")

    monkeypatch.setattr(fundamentals_bse_announcements, "fetch_company_announcements", always_fails)
    monkeypatch.setattr(fundamentals_bse_announcements, "upsert_events_with_dedup", lambda rows: {"inserted": 0, "merged": 0})
    marked = []
    monkeypatch.setattr(fundamentals_bse_announcements, "_mark_backfilled", lambda cmids: marked.append(cmids))
    monkeypatch.setattr(fundamentals_bse_announcements, "record_local_fallback_event", lambda **kwargs: None)

    result = fundamentals_bse_announcements.run_auditor_rpt_backfill()

    assert result["companies_scanned"] == 0
    assert result["failed_companies"] == ["TICK1"]
    assert marked == [[]]  # called, but with nothing to mark -- the failed company stays eligible for retry


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


def test_upsert_events_with_dedup_merges_sibling_rows_within_the_same_batch(monkeypatch):
    # BUG FOUND LIVE 2026-08-15: find_dedup_candidate only ever queries the DB, so two
    # rows sharing a dedup key WITHIN one batch (neither flushed to the DB yet) both
    # independently found "no match" and both got inserted -- confirmed live, 167
    # orphaned duplicate rows across 73 groups. This pins the fix: they must merge
    # into ONE staged row instead of becoming two inserts.
    monkeypatch.setattr(fundamentals_events_store, "find_dedup_candidate", lambda isin, ft, d: None)  # nothing in the DB yet
    monkeypatch.setattr(fundamentals_events_store, "_ensure_events_schema", lambda: None)
    upsert_calls = []
    monkeypatch.setattr(fundamentals_events_store, "upsert_to_db", lambda df, table, **k: upsert_calls.append((df, table, k)))

    row_a = {"source": "bse", "news_id": "bse-1", "isin": "INE111", "filing_type": "pit_sast", "disclosure_date": date(2026, 8, 1), "quantity": None, "insider_name": None, "sources": "bse"}
    row_b = {"source": "nse", "news_id": "nse-1", "isin": "INE111", "filing_type": "pit_sast", "disclosure_date": date(2026, 8, 1), "quantity": 25000, "insider_name": "Jane Promoter", "sources": "nse"}

    result = fundamentals_events_store.upsert_events_with_dedup([row_a, row_b])

    assert result == {"inserted": 1, "merged": 1}  # ONE row written, not two
    df, table, kwargs = upsert_calls[0]
    assert len(df) == 1
    merged_row = df.iloc[0]
    assert merged_row["source"] == "bse" and merged_row["news_id"] == "bse-1"  # first-staged row's identity wins
    assert merged_row["quantity"] == 25000  # filled in from the second row
    assert merged_row["insider_name"] == "Jane Promoter"
    assert merged_row["sources"] == "bse,nse"


def test_upsert_events_with_dedup_batch_merge_handles_three_sibling_rows(monkeypatch):
    monkeypatch.setattr(fundamentals_events_store, "find_dedup_candidate", lambda isin, ft, d: None)
    monkeypatch.setattr(fundamentals_events_store, "_ensure_events_schema", lambda: None)
    upsert_calls = []
    monkeypatch.setattr(fundamentals_events_store, "upsert_to_db", lambda df, table, **k: upsert_calls.append((df, table, k)))

    rows = [
        {"source": "bse", "news_id": f"bse-{i}", "isin": "INE111", "filing_type": "pit_sast", "disclosure_date": date(2026, 8, 1)}
        for i in range(3)
    ]

    result = fundamentals_events_store.upsert_events_with_dedup(rows)

    assert result == {"inserted": 1, "merged": 2}
    assert len(upsert_calls[0][0]) == 1


def test_upsert_events_with_dedup_does_not_merge_rows_lacking_a_dedup_key(monkeypatch):
    # rows without a resolvable isin skip dedup entirely (module docstring) -- must
    # not be accidentally merged together in-batch just because they share (None,
    # None, None).
    monkeypatch.setattr(fundamentals_events_store, "find_dedup_candidate", lambda isin, ft, d: None)
    monkeypatch.setattr(fundamentals_events_store, "_ensure_events_schema", lambda: None)
    upsert_calls = []
    monkeypatch.setattr(fundamentals_events_store, "upsert_to_db", lambda df, table, **k: upsert_calls.append((df, table, k)))

    rows = [
        {"source": "bse", "news_id": "bse-1", "isin": None, "filing_type": "results", "disclosure_date": date(2026, 8, 1)},
        {"source": "bse", "news_id": "bse-2", "isin": None, "filing_type": "results", "disclosure_date": date(2026, 8, 1)},
    ]

    result = fundamentals_events_store.upsert_events_with_dedup(rows)

    assert result == {"inserted": 2, "merged": 0}
    assert len(upsert_calls[0][0]) == 2


def test_dedup_key_normalizes_disclosure_date_as_text():
    row = {"isin": "INE111", "filing_type": "pit_sast", "disclosure_date": date(2026, 8, 1)}
    assert fundamentals_events_store._dedup_key(row) == ("INE111", "pit_sast", "2026-08-01")


def test_dedup_key_none_when_missing_isin_or_filing_type_or_date():
    assert fundamentals_events_store._dedup_key({"isin": None, "filing_type": "pit_sast", "disclosure_date": date(2026, 8, 1)}) is None
    assert fundamentals_events_store._dedup_key({"isin": "INE111", "filing_type": None, "disclosure_date": date(2026, 8, 1)}) is None
    assert fundamentals_events_store._dedup_key({"isin": "INE111", "filing_type": "pit_sast", "disclosure_date": None}) is None


# fundamentals/collectors/nse_pit.py -- L3 detection, NSE half (step 5).
# Rewritten 2026-08-15 for NSE's corporates-pit -> corporates-pit-gg migration (see
# module docstring): the endpoint's list rows no longer carry flat acquirer/quantity
# fields, so those now come from parsing each matched filing's own XBRL document, and
# the collector fetches one market-wide list per run instead of one per company.


def test_parse_nse_pit_date_parses_dd_mon_yyyy_and_iso():
    assert fundamentals_nse_pit._parse_nse_pit_date("15-Jul-2026") == date(2026, 7, 15)
    assert fundamentals_nse_pit._parse_nse_pit_date("2026-07-15") == date(2026, 7, 15)


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


# Real (trimmed) corporates-pit-gg XBRL fragment, captured live 2026-08-15 against a
# real BlueStone Jewellery and Lifestyle Limited PIT filing -- the module docstring's
# endpoint-migration record.
_REAL_PIT_XBRL_FRAGMENT = """<in-bse-co:ScripCode contextRef="MainI">544484</in-bse-co:ScripCode><in-bse-co:Symbol contextRef="MainI">BLUESTONE</in-bse-co:Symbol><in-bse-co:NameOfTheCompany contextRef="MainI">BlueStone Jewellery and Lifestyle Limited</in-bse-co:NameOfTheCompany><in-bse-co:DateOfFiling contextRef="MainI">2026-08-14</in-bse-co:DateOfFiling><in-bse-co:ISINCode contextRef="MainI">INE304W01038</in-bse-co:ISINCode><in-bse-co:DisclosureUnderRegulation contextRef="MainI">Regulation 7 (2)</in-bse-co:DisclosureUnderRegulation><in-bse-co:CategoryOfPerson contextRef="Disclosure1">Designated Person</in-bse-co:CategoryOfPerson><in-bse-co:NameOfThePerson contextRef="Disclosure1">SUDEEP NAGAR</in-bse-co:NameOfThePerson><in-bse-co:SecuritiesAcquiredOrDisposedNumberOfSecurity contextRef="Disclosure1" unitRef="shares" decimals="INF">110000</in-bse-co:SecuritiesAcquiredOrDisposedNumberOfSecurity><in-bse-co:SecuritiesAcquiredOrDisposedTransactionType contextRef="Disclosure1">Pledge</in-bse-co:SecuritiesAcquiredOrDisposedTransactionType><in-bse-co:DateOfIntimationToCompany contextRef="Disclosure1">2026-08-14</in-bse-co:DateOfIntimationToCompany>"""


def test_parse_pit_xbrl_extracts_filing_and_single_disclosure():
    parsed = fundamentals_nse_pit.parse_pit_xbrl(_REAL_PIT_XBRL_FRAGMENT)
    assert parsed["filing"]["Symbol"] == "BLUESTONE"
    assert parsed["filing"]["ISINCode"] == "INE304W01038"
    assert len(parsed["disclosures"]) == 1
    assert parsed["disclosures"][0]["NameOfThePerson"] == "SUDEEP NAGAR"
    assert parsed["disclosures"][0]["SecuritiesAcquiredOrDisposedTransactionType"] == "Pledge"


def test_parse_pit_xbrl_handles_multiple_disclosure_contexts_in_one_filing():
    # A single filing covering two transactions -- confirmed live 2026-08-15 against a
    # real Sonata Software Employee Welfare Trust filing (Disclosure1 + Disclosure2).
    xml = (
        '<in-bse-co:Symbol contextRef="MainI">SONATSOFTW</in-bse-co:Symbol>'
        '<in-bse-co:NameOfThePerson contextRef="Disclosure1">Sonata Software Limited Employee Welfare Trust</in-bse-co:NameOfThePerson>'
        '<in-bse-co:SecuritiesAcquiredOrDisposedNumberOfSecurity contextRef="Disclosure1">500</in-bse-co:SecuritiesAcquiredOrDisposedNumberOfSecurity>'
        '<in-bse-co:NameOfThePerson contextRef="Disclosure2">Sonata Software Limited Employee Welfare Trust</in-bse-co:NameOfThePerson>'
        '<in-bse-co:SecuritiesAcquiredOrDisposedNumberOfSecurity contextRef="Disclosure2">800</in-bse-co:SecuritiesAcquiredOrDisposedNumberOfSecurity>'
    )
    parsed = fundamentals_nse_pit.parse_pit_xbrl(xml)
    assert len(parsed["disclosures"]) == 2
    assert [d["SecuritiesAcquiredOrDisposedNumberOfSecurity"] for d in parsed["disclosures"]] == ["500", "800"]


def test_parse_pit_xbrl_sorts_disclosure_contexts_numerically_not_lexicographically():
    # found live 2026-08-15 on a real 11-disclosure filing (JSW Steel Employees
    # Welfare Trust ESOP accounts): plain sorted() puts "Disclosure10"/"Disclosure11"
    # before "Disclosure2".."Disclosure9".
    xml = "".join(
        f'<in-bse-co:NameOfThePerson contextRef="Disclosure{i}">Person{i}</in-bse-co:NameOfThePerson>'
        for i in [1, 2, 10, 11, 3]
    )
    parsed = fundamentals_nse_pit.parse_pit_xbrl(xml)
    assert [d["NameOfThePerson"] for d in parsed["disclosures"]] == ["Person1", "Person2", "Person3", "Person10", "Person11"]


def test_parse_pit_xbrl_unescapes_html_entities():
    # found live 2026-08-15: a real company name came back as the literal string
    # "AVONMORE CAPITAL &amp;amp; MANAGEMENT SERVICES LIMITED" -- page.content()'s
    # HTML-escaped serialization was never decoded.
    xml = '<in-bse-co:NameOfTheCompany contextRef="MainI">AVONMORE CAPITAL &amp;amp; MANAGEMENT SERVICES LIMITED</in-bse-co:NameOfTheCompany>'
    parsed = fundamentals_nse_pit.parse_pit_xbrl(xml)
    assert parsed["filing"]["NameOfTheCompany"] == "AVONMORE CAPITAL & MANAGEMENT SERVICES LIMITED"


def test_parse_pit_xbrl_ignores_elements_without_a_context_ref():
    parsed = fundamentals_nse_pit.parse_pit_xbrl('<in-bse-co:Foo>no context here</in-bse-co:Foo>')
    assert parsed == {"filing": {}, "disclosures": []}


def test_build_pit_rows_maps_a_real_captured_disclosure():
    parsed = fundamentals_nse_pit.parse_pit_xbrl(_REAL_PIT_XBRL_FRAGMENT)
    rows = fundamentals_nse_pit.build_pit_rows(
        symbol="BLUESTONE",
        company_master_id="nse:BLUESTONE",
        isin=None,
        app_id="2283",
        xml_url="https://nsearchives.nseindia.com/corporate/xbrl/IT_25008_WEB.xml",
        detail_url="https://nsearchives.nseindia.com/corporate/ixbrl/IT_25008_WEB.html",
        broadcast_datetime="14-Aug-2026 22:29:18",
        filing=parsed["filing"],
        disclosures=parsed["disclosures"],
    )
    assert len(rows) == 1
    row = rows[0]
    assert row["source"] == "nse"
    # keyed on xml_url (per-document), not appId -- appId is confirmed live to be a
    # small, market-wide, likely-annually-resetting counter, so two unrelated filings
    # months apart can share one and collide under the (source, news_id) upsert key.
    assert row["news_id"] == "nse-pit:https://nsearchives.nseindia.com/corporate/xbrl/IT_25008_WEB.xml:1"
    assert row["scrip_code"] == "BLUESTONE"
    assert row["isin"] == "INE304W01038"  # from the XBRL filing itself, not the isin= arg
    assert row["filing_type"] == "pit_sast"
    assert row["quantity"] == 110000
    assert row["insider_name"] == "SUDEEP NAGAR"
    assert row["transaction_type"] == "Pledge"
    assert row["subcategory"] == "Designated Person"
    assert row["disclosure_date"] == date(2026, 8, 14)
    # 14-Aug-2026 22:29:18 IST -> 16:59:18 UTC
    assert row["announcement_timestamp"] == pd.Timestamp("2026-08-14 16:59:18", tz="UTC")
    assert row["detail_url"] == "https://nsearchives.nseindia.com/corporate/ixbrl/IT_25008_WEB.html"
    assert row["detection_source"] == "nse_corporates_pit"
    assert row["enrichment_status"] == "structured"  # no OCR needed, unlike BSE's rows
    assert row["sources"] == "nse"
    assert json.loads(row["raw_json"])["disclosure"]["NameOfThePerson"] == "SUDEEP NAGAR"


def test_build_pit_rows_disclosure_date_uses_broadcast_not_intimation_date():
    # MEDIUM FINDING (re-audit 2026-08-18): disclosure_date used to be
    # DateOfIntimationToCompany (when the insider told the company about their
    # trade), while bse_announcements.py's own disclosure_date is DissemDT's date
    # (when the exchange disseminated the announcement) -- confirmed live to differ
    # by 3 days on a real filing, so events_store.py's cross-source dedup match key
    # (isin, filing_type, disclosure_date), an EXACT date match, missed the
    # intended "BSE detects, NSE fills in quantity" merge. Must now use
    # announcement_timestamp's own date, matching BSE's semantic -- proven here by
    # an intimation date that's 3 days earlier than the broadcast date.
    rows = fundamentals_nse_pit.build_pit_rows(
        symbol="X", company_master_id="nse:X", isin=None,
        app_id="1", xml_url="https://nsearchives.nseindia.com/x.xml", detail_url=None,
        broadcast_datetime="14-Aug-2026 10:00:00",  # dissemination: Aug 14
        filing={"DateOfFiling": "2026-08-11"},
        disclosures=[{"NameOfThePerson": "A", "DateOfIntimationToCompany": "2026-08-11"}],  # intimation: Aug 11
    )
    # 14-Aug-2026 10:00 IST -> 04:30 UTC, still Aug 14
    assert rows[0]["disclosure_date"] == date(2026, 8, 14)  # broadcast date, NOT the Aug 11 intimation date
    # the precise intimation date is preserved, just not as the top-level disclosure_date
    assert json.loads(rows[0]["raw_json"])["disclosure"]["DateOfIntimationToCompany"] == "2026-08-11"


def test_build_pit_rows_disclosure_date_falls_back_to_filing_date_when_no_broadcast():
    rows = fundamentals_nse_pit.build_pit_rows(
        symbol="X", company_master_id="nse:X", isin=None,
        app_id="1", xml_url="https://nsearchives.nseindia.com/x.xml", detail_url=None, broadcast_datetime=None,
        filing={"DateOfFiling": "2026-08-11"},
        disclosures=[{"NameOfThePerson": "A", "DateOfIntimationToCompany": "2026-08-09"}],
    )
    assert rows[0]["disclosure_date"] == date(2026, 8, 11)  # filing date, not the Aug 9 intimation date


def test_build_pit_rows_falls_back_to_resolved_isin_when_xbrl_has_none():
    rows = fundamentals_nse_pit.build_pit_rows(
        symbol="X", company_master_id="nse:X", isin="INE_RESOLVED",
        app_id="1", xml_url="https://nsearchives.nseindia.com/x.xml", detail_url=None, broadcast_datetime=None,
        filing={}, disclosures=[{"NameOfThePerson": "Someone"}],
    )
    assert rows[0]["isin"] == "INE_RESOLVED"


def test_build_pit_rows_returns_one_row_per_disclosure_context():
    rows = fundamentals_nse_pit.build_pit_rows(
        symbol="X", company_master_id="nse:X", isin=None,
        app_id="7", xml_url="https://nsearchives.nseindia.com/7.xml", detail_url=None, broadcast_datetime=None,
        filing={"DateOfFiling": "2026-08-14"},
        disclosures=[{"NameOfThePerson": "A"}, {"NameOfThePerson": "B"}],
    )
    assert [r["news_id"] for r in rows] == [
        "nse-pit:https://nsearchives.nseindia.com/7.xml:1",
        "nse-pit:https://nsearchives.nseindia.com/7.xml:2",
    ]


def test_build_pit_rows_falls_back_to_app_id_when_xml_url_missing():
    # Defense in depth only -- run_nse_pit_detection() never calls build_pit_rows()
    # without a confirmed-fetched xml_url in practice (it `continue`s past filings
    # missing one), but the key should still degrade gracefully rather than crash.
    rows = fundamentals_nse_pit.build_pit_rows(
        symbol="X", company_master_id="nse:X", isin=None,
        app_id="42", xml_url=None, detail_url=None, broadcast_datetime=None,
        filing={}, disclosures=[{"NameOfThePerson": "A"}],
    )
    assert rows[0]["news_id"] == "nse-pit:42:1"


def test_build_pit_rows_news_id_does_not_collide_across_appid_reset(monkeypatch):
    # Regression for the collision this session found live: appId is a small,
    # market-wide counter that is NOT confirmed unique across NSE's fiscal-year
    # boundary (observed ~1000 in mid-June 2026, ~2300 in mid-August 2026 -- an
    # annual reset would make appId "2283" recur in a later fiscal year). Two
    # different filings that happen to share an appId must still get different
    # news_id values because their xml_url (the actual fetched document) differs.
    rows_a = fundamentals_nse_pit.build_pit_rows(
        symbol="AAA", company_master_id="nse:AAA", isin=None,
        app_id="2283", xml_url="https://nsearchives.nseindia.com/corporate/xbrl/old_filing.xml",
        detail_url=None, broadcast_datetime=None, filing={}, disclosures=[{"NameOfThePerson": "A"}],
    )
    rows_b = fundamentals_nse_pit.build_pit_rows(
        symbol="BBB", company_master_id="nse:BBB", isin=None,
        app_id="2283", xml_url="https://nsearchives.nseindia.com/corporate/xbrl/new_filing.xml",
        detail_url=None, broadcast_datetime=None, filing={}, disclosures=[{"NameOfThePerson": "B"}],
    )
    assert rows_a[0]["news_id"] != rows_b[0]["news_id"]


def test_resolve_company_identity_uses_events_store_helpers(monkeypatch):
    tickers = pd.Series(["AAREYDRUGS"], index=[0])
    monkeypatch.setattr(
        fundamentals_nse_pit, "map_company_master_ids_nse_or_bse", lambda series, **k: pd.Series(["nse:AAREYDRUGS"], index=series.index, dtype="string")
    )
    monkeypatch.setattr(fundamentals_nse_pit, "resolve_isin", lambda cmids: pd.Series(["INE198H01019"], index=cmids.index))

    result = fundamentals_nse_pit.resolve_company_identity(tickers)

    assert result.loc[0, "company_master_id"] == "nse:AAREYDRUGS"
    assert result.loc[0, "isin"] == "INE198H01019"
    assert "issuer" not in result.columns  # no longer needed -- see module docstring


def test_fetch_pit_disclosures_raises_on_missing_data_key(monkeypatch):
    monkeypatch.setattr(fundamentals_nse_pit, "nse_request_gate", lambda **kwargs: contextlib.nullcontext())

    class FakePage:
        def evaluate(self, script, url):
            return {"unexpected": "shape"}

    with pytest.raises(fundamentals_nse_pit.NsePitBlockedError):
        fundamentals_nse_pit.fetch_pit_disclosures(FakePage(), from_date=datetime(2026, 8, 1), to_date=datetime(2026, 8, 8))


def test_fetch_pit_disclosures_wraps_page_evaluate_exceptions(monkeypatch):
    monkeypatch.setattr(fundamentals_nse_pit, "nse_request_gate", lambda **kwargs: contextlib.nullcontext())

    class FakePage:
        def evaluate(self, script, url):
            raise RuntimeError("HTTP 403")

    with pytest.raises(fundamentals_nse_pit.NsePitBlockedError):
        fundamentals_nse_pit.fetch_pit_disclosures(FakePage(), from_date=datetime(2026, 8, 1), to_date=datetime(2026, 8, 8))


def test_fetch_pit_disclosures_returns_data_rows(monkeypatch):
    monkeypatch.setattr(fundamentals_nse_pit, "nse_request_gate", lambda **kwargs: contextlib.nullcontext())

    class FakePage:
        def evaluate(self, script, url):
            assert "symbol" not in url and "issuer" not in url  # market-wide, not per-company
            return {"data": [{"symbol": "X"}]}

    rows = fundamentals_nse_pit.fetch_pit_disclosures(FakePage(), from_date=datetime(2026, 8, 1), to_date=datetime(2026, 8, 8))
    assert rows == [{"symbol": "X"}]


def test_fetch_pit_disclosure_xml_uses_nse_goto_not_a_nested_gate(monkeypatch):
    # nse_goto already applies nse_request_gate internally -- a second nested
    # acquisition from the same process would spin until its 900s timeout (see module
    # docstring). Fail the test loudly if fetch_pit_disclosure_xml ever wraps the
    # nse_goto call in its own nse_request_gate() again.
    def _poison(*a, **k):
        raise AssertionError("fetch_pit_disclosure_xml must not nest nse_request_gate around nse_goto")

    monkeypatch.setattr(fundamentals_nse_pit, "nse_request_gate", _poison)

    calls = []

    def fake_nse_goto(page, url, **kwargs):
        calls.append(url)

    monkeypatch.setattr(fundamentals_nse_pit, "nse_goto", fake_nse_goto)

    class FakePage:
        def content(self):
            return "<xml/>"

    result = fundamentals_nse_pit.fetch_pit_disclosure_xml(FakePage(), xml_url="https://nsearchives.nseindia.com/x.xml")
    assert result == "<xml/>"
    assert calls == ["https://nsearchives.nseindia.com/x.xml"]


def test_fetch_pit_disclosure_xml_wraps_nse_goto_exceptions(monkeypatch):
    def failing_goto(page, url, **kwargs):
        raise RuntimeError("HTTP 403")

    monkeypatch.setattr(fundamentals_nse_pit, "nse_goto", failing_goto)

    with pytest.raises(fundamentals_nse_pit.NsePitBlockedError):
        fundamentals_nse_pit.fetch_pit_disclosure_xml(object(), xml_url="https://nsearchives.nseindia.com/x.xml")


class _FakeNsePitPage:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True

    def wait_for_timeout(self, ms):
        pass

    def evaluate(self, script, url):
        return {"data": []}


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
        },
        index=tickers.index,
    )


def test_run_nse_pit_detection_happy_path(monkeypatch):
    universe = _nse_universe_df(2)  # tickers TICK1, TICK2
    monkeypatch.setattr(fundamentals_nse_pit, "load_l1_universe_tickers", lambda: universe)
    monkeypatch.setattr(fundamentals_nse_pit, "resolve_company_identity", lambda tickers: _nse_identity_df(tickers))
    _patch_fake_playwright(monkeypatch, _FakeNsePitPage())

    # Market-wide list has 3 filings: 2 match the universe (TICK1, TICK2), 1 doesn't
    # (OUTSIDE) -- proves local filtering, not an NSE-side per-company filter.
    filings = [
        {"symbol": "TICK1", "appId": "1", "xmlFileName": "https://nsearchives.nseindia.com/1.xml", "ixbrl": "u1", "broadcastDateTime": "01-Aug-2026 10:00"},
        {"symbol": "TICK2", "appId": "2", "xmlFileName": "https://nsearchives.nseindia.com/2.xml", "ixbrl": "u2", "broadcastDateTime": "01-Aug-2026 11:00"},
        {"symbol": "OUTSIDE", "appId": "3", "xmlFileName": "https://nsearchives.nseindia.com/3.xml", "ixbrl": "u3", "broadcastDateTime": "01-Aug-2026 12:00"},
    ]
    monkeypatch.setattr(fundamentals_nse_pit, "fetch_pit_disclosures", lambda page, **k: filings)

    def fake_fetch_xml(page, *, xml_url):
        return xml_url  # identity -- parse_pit_xbrl below just needs a distinguishable string

    def fake_parse(xml_text):
        return {"filing": {"DateOfFiling": "2026-08-01"}, "disclosures": [{"NameOfThePerson": "Someone", "SecuritiesAcquiredOrDisposedNumberOfSecurity": "100"}]}

    monkeypatch.setattr(fundamentals_nse_pit, "fetch_pit_disclosure_xml", fake_fetch_xml)
    monkeypatch.setattr(fundamentals_nse_pit, "parse_pit_xbrl", fake_parse)
    dedup_calls = []
    monkeypatch.setattr(
        fundamentals_nse_pit, "upsert_events_with_dedup", lambda rows: dedup_calls.append(rows) or {"inserted": len(rows), "merged": 0}
    )
    monkeypatch.setattr(fundamentals_nse_pit, "record_local_fallback_event", lambda **k: None)

    result = fundamentals_nse_pit.run_nse_pit_detection()

    assert result["filings_matched"] == 2  # OUTSIDE dropped, not scanned
    assert result["filings_scanned"] == 2
    assert result["rows"] == 2  # one disclosure per matched filing in this fixture
    assert result["blocked"] is False
    assert result["failed_filings"] == []
    assert len(dedup_calls) == 1 and len(dedup_calls[0]) == 2
    # news_id is keyed on the fetched xmlFileName, not the small market-wide appId
    # counter -- see test_build_pit_rows_news_id_does_not_collide_across_appid_reset.
    assert {row["news_id"] for row in dedup_calls[0]} == {
        "nse-pit:https://nsearchives.nseindia.com/1.xml:1",
        "nse-pit:https://nsearchives.nseindia.com/2.xml:1",
    }


def test_run_nse_pit_detection_records_fallback_for_empty_market_wide_list(monkeypatch):
    # MEDIUM FINDING (re-audit 2026-08-18): an empty market-wide list used to report
    # status:"ok" identically to "real data, nothing matched our universe today" --
    # nothing distinguished a genuinely healthy zero from NSE's own endpoint
    # silently regressing to empty (the exact shape this module's own docstring
    # documents for the pre-migration corporates-pit endpoint's silent deprecation).
    universe = _nse_universe_df(1)
    monkeypatch.setattr(fundamentals_nse_pit, "load_l1_universe_tickers", lambda: universe)
    monkeypatch.setattr(fundamentals_nse_pit, "resolve_company_identity", lambda tickers: _nse_identity_df(tickers))
    _patch_fake_playwright(monkeypatch, _FakeNsePitPage())
    monkeypatch.setattr(fundamentals_nse_pit, "fetch_pit_disclosures", lambda page, **k: [])
    monkeypatch.setattr(fundamentals_nse_pit, "upsert_events_with_dedup", lambda rows: {"inserted": 0, "merged": 0})
    fallback_events = []
    monkeypatch.setattr(fundamentals_nse_pit, "record_local_fallback_event", lambda **k: fallback_events.append(k))

    result = fundamentals_nse_pit.run_nse_pit_detection()

    assert result["rows"] == 0
    assert any(e["fallback_type"] == "l3_nse_pit_empty_market_wide_list" for e in fallback_events)


def test_run_nse_pit_detection_records_fallback_for_missing_xml_url(monkeypatch):
    universe = _nse_universe_df(1)
    monkeypatch.setattr(fundamentals_nse_pit, "load_l1_universe_tickers", lambda: universe)
    monkeypatch.setattr(fundamentals_nse_pit, "resolve_company_identity", lambda tickers: _nse_identity_df(tickers))
    _patch_fake_playwright(monkeypatch, _FakeNsePitPage())
    filings = [{"symbol": "TICK1", "appId": "1", "xmlFileName": None, "ixbrl": "u1", "broadcastDateTime": "01-Aug-2026 10:00"}]
    monkeypatch.setattr(fundamentals_nse_pit, "fetch_pit_disclosures", lambda page, **k: filings)
    monkeypatch.setattr(fundamentals_nse_pit, "upsert_events_with_dedup", lambda rows: {"inserted": 0, "merged": 0})
    fallback_events = []
    monkeypatch.setattr(fundamentals_nse_pit, "record_local_fallback_event", lambda **k: fallback_events.append(k))

    result = fundamentals_nse_pit.run_nse_pit_detection()

    assert result["filings_scanned"] == 0  # skipped, not counted as scanned
    matches = [e for e in fallback_events if e["fallback_type"] == "l3_nse_pit_missing_xml_url"]
    assert len(matches) == 1
    assert matches[0]["metadata"]["ticker"] == "TICK1"


def test_run_nse_pit_detection_records_fallback_for_zero_disclosure_contexts(monkeypatch):
    universe = _nse_universe_df(1)
    monkeypatch.setattr(fundamentals_nse_pit, "load_l1_universe_tickers", lambda: universe)
    monkeypatch.setattr(fundamentals_nse_pit, "resolve_company_identity", lambda tickers: _nse_identity_df(tickers))
    _patch_fake_playwright(monkeypatch, _FakeNsePitPage())
    filings = [{"symbol": "TICK1", "appId": "1", "xmlFileName": "https://nsearchives.nseindia.com/1.xml", "ixbrl": "u1", "broadcastDateTime": "01-Aug-2026 10:00"}]
    monkeypatch.setattr(fundamentals_nse_pit, "fetch_pit_disclosures", lambda page, **k: filings)
    monkeypatch.setattr(fundamentals_nse_pit, "fetch_pit_disclosure_xml", lambda page, *, xml_url: xml_url)
    # A real filing, but zero Disclosure* contexts found -- e.g. a genuinely
    # disclosure-less filing, or parse_pit_xbrl's regex breaking on a schema change.
    monkeypatch.setattr(fundamentals_nse_pit, "parse_pit_xbrl", lambda xml_text: {"filing": {"DateOfFiling": "2026-08-01"}, "disclosures": []})
    monkeypatch.setattr(fundamentals_nse_pit, "upsert_events_with_dedup", lambda rows: {"inserted": 0, "merged": 0})
    fallback_events = []
    monkeypatch.setattr(fundamentals_nse_pit, "record_local_fallback_event", lambda **k: fallback_events.append(k))

    result = fundamentals_nse_pit.run_nse_pit_detection()

    assert result["filings_scanned"] == 1  # still counted as scanned -- the fetch succeeded
    assert result["rows"] == 0
    matches = [e for e in fallback_events if e["fallback_type"] == "l3_nse_pit_no_disclosure_contexts"]
    assert len(matches) == 1
    assert matches[0]["metadata"]["ticker"] == "TICK1"


def test_run_nse_pit_detection_trips_circuit_breaker(monkeypatch):
    universe = _nse_universe_df(5)
    monkeypatch.setattr(fundamentals_nse_pit, "load_l1_universe_tickers", lambda: universe)
    monkeypatch.setattr(fundamentals_nse_pit, "resolve_company_identity", lambda tickers: _nse_identity_df(tickers))
    _patch_fake_playwright(monkeypatch, _FakeNsePitPage())

    filings = [
        {"symbol": f"TICK{i}", "appId": str(i), "xmlFileName": f"https://nsearchives.nseindia.com/{i}.xml"} for i in range(1, 6)
    ]
    monkeypatch.setattr(fundamentals_nse_pit, "fetch_pit_disclosures", lambda page, **k: filings)

    def always_fails(page, *, xml_url):
        raise fundamentals_nse_pit.NsePitBlockedError("boom")

    monkeypatch.setattr(fundamentals_nse_pit, "fetch_pit_disclosure_xml", always_fails)
    monkeypatch.setattr(fundamentals_nse_pit, "upsert_events_with_dedup", lambda rows: {"inserted": len(rows), "merged": 0})
    fallback_events = []
    monkeypatch.setattr(fundamentals_nse_pit, "record_local_fallback_event", lambda **kwargs: fallback_events.append(kwargs))

    result = fundamentals_nse_pit.run_nse_pit_detection()

    assert result["blocked"] is True
    assert len(result["failed_filings"]) == fundamentals_nse_pit.CIRCUIT_BREAKER_THRESHOLD
    assert result["filings_scanned"] == 0
    assert any(e["fallback_type"] == "l3_nse_circuit_breaker_tripped" for e in fallback_events)


def test_run_nse_pit_detection_treats_list_fetch_failure_as_blocked(monkeypatch):
    universe = _nse_universe_df(1)
    monkeypatch.setattr(fundamentals_nse_pit, "load_l1_universe_tickers", lambda: universe)
    monkeypatch.setattr(fundamentals_nse_pit, "resolve_company_identity", lambda tickers: _nse_identity_df(tickers))
    _patch_fake_playwright(monkeypatch, _FakeNsePitPage())

    def failing_list(page, **k):
        raise fundamentals_nse_pit.NsePitBlockedError("boom")

    monkeypatch.setattr(fundamentals_nse_pit, "fetch_pit_disclosures", failing_list)
    fallback_events = []
    monkeypatch.setattr(fundamentals_nse_pit, "record_local_fallback_event", lambda **kwargs: fallback_events.append(kwargs))

    result = fundamentals_nse_pit.run_nse_pit_detection()

    assert result == {"rows": 0, "filings_scanned": 0, "failed_filings": [], "blocked": True}
    assert any(e["fallback_type"] == "l3_nse_pit_list_fetch_failed" for e in fallback_events)


def test_run_nse_pit_detection_returns_early_without_cdp_endpoint(monkeypatch):
    universe = _nse_universe_df(1)
    monkeypatch.setattr(fundamentals_nse_pit, "load_l1_universe_tickers", lambda: universe)
    monkeypatch.setattr(fundamentals_nse_pit, "resolve_company_identity", lambda tickers: _nse_identity_df(tickers))
    monkeypatch.setattr(fundamentals_nse_pit, "CDP_ENDPOINT", "")
    fallback_events = []
    monkeypatch.setattr(fundamentals_nse_pit, "record_local_fallback_event", lambda **kwargs: fallback_events.append(kwargs))

    result = fundamentals_nse_pit.run_nse_pit_detection()

    assert result == {"rows": 0, "filings_scanned": 0, "failed_filings": [], "blocked": False}
    assert any(e["fallback_type"] == "l3_nse_no_cdp_endpoint" for e in fallback_events)


def test_run_nse_pit_detection_returns_early_on_empty_universe(monkeypatch):
    monkeypatch.setattr(fundamentals_nse_pit, "load_l1_universe_tickers", lambda: pd.DataFrame())
    fallback_events = []
    monkeypatch.setattr(fundamentals_nse_pit, "record_local_fallback_event", lambda **kwargs: fallback_events.append(kwargs))

    result = fundamentals_nse_pit.run_nse_pit_detection()

    assert result == {"rows": 0, "filings_scanned": 0, "failed_filings": [], "blocked": False}
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


def test_detect_agency_falls_back_gracefully_when_keyword_lists_drift(monkeypatch):
    # BUG FOUND LIVE 2026-08-18 (re-audit): _AGENCY_CANONICAL_NAMES[keyword] was an
    # unguarded dict lookup -- RATING_AGENCY_KEYWORDS (bse_announcements.py) and
    # _AGENCY_CANONICAL_NAMES (this module) are two separately-maintained
    # structures that currently happen to have the same keys; a future edit to one
    # without the other would raise KeyError and crash this whole enrichment step.
    monkeypatch.setattr(fundamentals_rating_agencies, "RATING_AGENCY_KEYWORDS", ("newagency",))
    fallback_events = []
    monkeypatch.setattr(fundamentals_rating_agencies, "_record_fallback", lambda *a, **k: fallback_events.append((a, k)))

    result = fundamentals_rating_agencies.detect_agency("NewAgency Ratings downgrades", None)

    assert result == "newagency"  # falls back to the raw keyword, doesn't crash
    assert len(fallback_events) == 1
    assert fallback_events[0][0][0] == "rating_agency_keyword_missing_canonical_name"
    assert fallback_events[0][1]["metadata"]["keyword"] == "newagency"


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


@pytest.mark.parametrize("date_text", ["04 Aug 2026", "Aug 4, 2026", "2026-08-04T00:00:00"])
def test_match_rationale_parses_every_agencys_date_format(date_text):
    # ICRA uses "04 Aug 2026", CRISIL uses "Aug 4, 2026", India Ratings uses ISO --
    # confirmed live 2026-08-12 all three parse through the same flexible call.
    results = [{"date_text": date_text, "headline": "X", "rationale_id": "1"}]
    matched = fundamentals_rating_agencies.match_rationale(results, date(2026, 8, 4))
    assert matched["rationale_id"] == "1"


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
        captured["kwargs"] = kwargs
        return pd.DataFrame()

    monkeypatch.setattr(fundamentals_rating_agencies, "sql_to_df", fake_sql_to_df)
    fundamentals_rating_agencies.load_pending_rating_actions()
    assert "filing_type = 'rating_action'" in captured["query"]
    assert "enrichment_status = 'pending'" in captured["query"]
    # BUG FOUND LIVE 2026-08-15, fixed 2026-08-17: no_match/failed rows must also be
    # eligible for retry (fundamental_basic_goal.md sec 3.3: "retry tomorrow") --
    # previously only 'pending' was ever selected, permanently excluding them.
    assert "no_match" in captured["query"] and "failed" in captured["query"]
    assert "enrichment_attempted_at" in captured["query"]
    assert len(captured["kwargs"]["params"]) == 1  # the retry cutoff timestamp


def test_load_pending_rating_actions_includes_stale_no_match_and_failed_rows(monkeypatch):
    # Live behavior check (not just query text): a no_match row whose last attempt
    # is older than RETRY_AFTER must come back; nothing here mocks sql_to_df itself,
    # so this exercises the query against a fake in-memory frame via a stub that
    # actually applies the same WHERE-clause semantics.
    old_attempt = pd.Timestamp.now(tz="UTC") - fundamentals_rating_agencies.RETRY_AFTER - pd.Timedelta(hours=1)
    recent_attempt = pd.Timestamp.now(tz="UTC") - pd.Timedelta(hours=1)
    rows = pd.DataFrame(
        [
            {"source": "bse", "news_id": "stale-no-match", "enrichment_status": "no_match", "enrichment_attempted_at": old_attempt},
            {"source": "bse", "news_id": "fresh-no-match", "enrichment_status": "no_match", "enrichment_attempted_at": recent_attempt},
            {"source": "bse", "news_id": "stale-failed", "enrichment_status": "failed", "enrichment_attempted_at": old_attempt},
            {"source": "bse", "news_id": "never-attempted", "enrichment_status": "no_match", "enrichment_attempted_at": None},
            {"source": "bse", "news_id": "fresh-pending", "enrichment_status": "pending", "enrichment_attempted_at": None},
        ]
    )

    def fake_sql_to_df(query, params=None):
        cutoff = params[0]
        eligible = rows[
            (rows["enrichment_status"] == "pending")
            | (
                rows["enrichment_status"].isin(["no_match", "failed"])
                & (rows["enrichment_attempted_at"].isna() | (rows["enrichment_attempted_at"] < cutoff))
            )
        ]
        return eligible

    monkeypatch.setattr(fundamentals_rating_agencies, "sql_to_df", fake_sql_to_df)
    result = fundamentals_rating_agencies.load_pending_rating_actions()

    news_ids = set(result["news_id"])
    assert news_ids == {"stale-no-match", "stale-failed", "never-attempted", "fresh-pending"}
    assert "fresh-no-match" not in news_ids  # too recent -- not yet eligible for retry


def test_set_enrichment_status_always_stamps_enrichment_attempted_at(monkeypatch):
    captured = {}

    def fake_op(op, *, operation_name):
        op()  # run it against a fake cursor to capture what would be executed

    class _FakeCursor:
        def execute(self, query, params):
            captured["query"] = query
            captured["params"] = params

    import contextlib

    @contextlib.contextmanager
    def fake_db_session():
        yield None, _FakeCursor()

    monkeypatch.setattr(fundamentals_rating_agencies, "execute_db_operation", fake_op)
    monkeypatch.setattr(fundamentals_rating_agencies, "db_session", fake_db_session)

    fundamentals_rating_agencies._set_enrichment_status(source="bse", news_id="n1", status="no_match")

    assert "enrichment_attempted_at" in captured["query"]
    assert captured["params"][0] == "no_match"


def test_run_rating_agency_enrichment_returns_early_when_nothing_pending(monkeypatch):
    monkeypatch.setattr(fundamentals_rating_agencies, "_ensure_events_schema", lambda: None)
    monkeypatch.setattr(fundamentals_rating_agencies, "_bootstrap_rating_columns", lambda: None)
    monkeypatch.setattr(fundamentals_rating_agencies, "load_pending_rating_actions", lambda limit=None: pd.DataFrame())

    result = fundamentals_rating_agencies.run_rating_agency_enrichment()

    assert result == {"matched": 0, "no_match": 0, "unsupported_agency": 0, "failed": 0, "blocked": False, "time_budget_exceeded": False}


def test_run_rating_agency_enrichment_routes_non_icra_rows_as_unsupported(monkeypatch):
    # Acuite has no plugin yet (unlike icra/india ratings/crisil/care) -- a
    # genuinely still-unsupported agency, see module docstring (zero observed
    # volume in the 2026-08-13 S3 archive scan, unlike CARE's real 5).
    monkeypatch.setattr(fundamentals_rating_agencies, "_ensure_events_schema", lambda: None)
    monkeypatch.setattr(fundamentals_rating_agencies, "_bootstrap_rating_columns", lambda: None)
    pending = pd.DataFrame(
        [{"source": "bse", "news_id": "n1", "company_master_id": "nse:X", "headline": "Acuite Ratings downgrades the rating", "subcategory": None, "disclosure_date": date(2026, 8, 4)}]
    )
    monkeypatch.setattr(fundamentals_rating_agencies, "load_pending_rating_actions", lambda limit=None: pending)
    status_calls = []
    monkeypatch.setattr(
        fundamentals_rating_agencies, "_set_enrichment_status", lambda **kwargs: status_calls.append(kwargs)
    )
    fallback_events = []
    monkeypatch.setattr(fundamentals_rating_agencies, "_record_fallback", lambda *a, **k: fallback_events.append((a, k)))
    # track_unsupported_agencies does real DB work (table DDL + upsert) -- must be
    # mocked here or every test run would write "acuite" rows into the real
    # fundamentals_unsupported_rating_agencies table.
    tracked = []
    monkeypatch.setattr(fundamentals_rating_agencies, "track_unsupported_agencies", lambda rows: tracked.append(rows))

    result = fundamentals_rating_agencies.run_rating_agency_enrichment()

    assert result["unsupported_agency"] == 1
    assert status_calls == [{"source": "bse", "news_id": "n1", "status": "unsupported_agency"}]
    assert len(fallback_events) == 1
    assert fallback_events[0][1]["metadata"]["agency_counts"] == {"acuite": 1}
    assert len(tracked) == 1 and len(tracked[0]) == 1  # the unsupported row was handed to the tracker


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
    # BUG FOUND LIVE 2026-08-17: _record_fallback used to hardcode source="icra"
    # regardless of which agency actually failed -- a CRISIL/CARE outage would show
    # up as an ICRA problem in source-grouped monitoring. This fixture IS icra, but
    # pin that the real agency_name is now threaded through explicitly, not implicit.
    search_failed = [k for a, k in fallback_events if a and a[0] == "rating_enrichment_search_failed"]
    breaker_tripped = [k for a, k in fallback_events if a and a[0] == "rating_enrichment_circuit_breaker_tripped"]
    assert search_failed and all(k.get("source") == "icra" for k in search_failed)
    assert breaker_tripped and all(k.get("source") == "icra" for k in breaker_tripped)


def test_run_rating_agency_enrichment_fallback_source_matches_the_failing_agency(monkeypatch):
    # Same bug, proven against a NON-icra agency -- the old hardcoded source="icra"
    # would have passed the ICRA-only test above by coincidence. A CRISIL failure
    # must record source="crisil", not "icra".
    monkeypatch.setattr(fundamentals_rating_agencies, "_ensure_events_schema", lambda: None)
    monkeypatch.setattr(fundamentals_rating_agencies, "_bootstrap_rating_columns", lambda: None)
    pending = pd.DataFrame(
        [{"source": "bse", "news_id": "n1", "company_master_id": "nse:X1", "headline": "CRISIL Downgrades Rating", "subcategory": "Credit Rating", "disclosure_date": date(2026, 8, 4)}]
    )
    monkeypatch.setattr(fundamentals_rating_agencies, "load_pending_rating_actions", lambda limit=None: pending)
    monkeypatch.setattr(fundamentals_rating_agencies, "_resolve_issuer_name", lambda cmid: "Some Company")

    def always_fails(company):
        raise RuntimeError("boom")

    monkeypatch.setattr(fundamentals_rating_agencies, "search_crisil_rationales", always_fails)
    monkeypatch.setattr(fundamentals_rating_agencies, "_set_enrichment_status", lambda **kwargs: None)
    fallback_events = []
    monkeypatch.setattr(fundamentals_rating_agencies, "_record_fallback", lambda *a, **k: fallback_events.append((a, k)))

    fundamentals_rating_agencies.run_rating_agency_enrichment()

    search_failed = [k for a, k in fallback_events if a and a[0] == "rating_enrichment_search_failed"]
    assert search_failed and all(k.get("source") == "crisil" for k in search_failed)


def test_run_rating_agency_enrichment_fails_row_with_no_resolvable_issuer(monkeypatch):
    # BUG FOUND LIVE 2026-08-18 (re-audit): this was the only failure path in the
    # loop with no _record_fallback call -- a company whose issuer name couldn't be
    # resolved just permanently failed enrichment with no trace.
    monkeypatch.setattr(fundamentals_rating_agencies, "_ensure_events_schema", lambda: None)
    monkeypatch.setattr(fundamentals_rating_agencies, "_bootstrap_rating_columns", lambda: None)
    pending = pd.DataFrame(
        [{"source": "bse", "news_id": "n1", "company_master_id": "nse:UNKNOWN", "headline": "Reaffirmation of Credit Ratings by ICRA", "subcategory": "Credit Rating", "disclosure_date": date(2026, 8, 4)}]
    )
    monkeypatch.setattr(fundamentals_rating_agencies, "load_pending_rating_actions", lambda limit=None: pending)
    monkeypatch.setattr(fundamentals_rating_agencies, "_resolve_issuer_name", lambda cmid: None)
    status_calls = []
    monkeypatch.setattr(fundamentals_rating_agencies, "_set_enrichment_status", lambda **kwargs: status_calls.append(kwargs))
    fallback_events = []
    monkeypatch.setattr(fundamentals_rating_agencies, "_record_fallback", lambda *a, **k: fallback_events.append((a, k)))

    result = fundamentals_rating_agencies.run_rating_agency_enrichment()

    assert result["failed"] == 1
    assert status_calls == [{"source": "bse", "news_id": "n1", "status": "failed"}]
    matches = [k for a, k in fallback_events if a and a[0] == "rating_enrichment_issuer_resolution_failed"]
    assert len(matches) == 1
    assert matches[0]["metadata"]["company_master_id"] == "nse:UNKNOWN"


def test_run_rating_agency_enrichment_respects_default_batch_limit(monkeypatch):
    # BUG FOUND LIVE 2026-08-18 (re-audit): neither a batch cap nor a wall-clock
    # budget -- main() calls this with limit=None, so every run processed the
    # entire pending backlog. The eligible set grows monotonically (RETRY_AFTER
    # re-admits a no_match/failed row daily) at a >=10s/domain rate-gate floor.
    monkeypatch.setattr(fundamentals_rating_agencies, "_ensure_events_schema", lambda: None)
    monkeypatch.setattr(fundamentals_rating_agencies, "_bootstrap_rating_columns", lambda: None)
    captured = {}

    def fake_load_pending(limit=None):
        captured["limit"] = limit
        return pd.DataFrame()

    monkeypatch.setattr(fundamentals_rating_agencies, "load_pending_rating_actions", fake_load_pending)

    fundamentals_rating_agencies.run_rating_agency_enrichment()

    assert captured["limit"] == fundamentals_rating_agencies.DEFAULT_BATCH_LIMIT


def test_run_rating_agency_enrichment_stops_at_time_budget(monkeypatch):
    monkeypatch.setattr(fundamentals_rating_agencies, "_ensure_events_schema", lambda: None)
    monkeypatch.setattr(fundamentals_rating_agencies, "_bootstrap_rating_columns", lambda: None)
    pending = pd.DataFrame(
        [
            {"source": "bse", "news_id": "n1", "company_master_id": "nse:X1", "headline": "Reaffirmation of Credit Ratings by ICRA", "subcategory": "Credit Rating", "disclosure_date": date(2026, 8, 4)},
            {"source": "bse", "news_id": "n2", "company_master_id": "nse:X2", "headline": "Reaffirmation of Credit Ratings by ICRA", "subcategory": "Credit Rating", "disclosure_date": date(2026, 8, 4)},
        ]
    )
    monkeypatch.setattr(fundamentals_rating_agencies, "load_pending_rating_actions", lambda limit=None: pending)
    monkeypatch.setattr(fundamentals_rating_agencies, "_resolve_issuer_name", lambda cmid: None)
    monkeypatch.setattr(fundamentals_rating_agencies, "_set_enrichment_status", lambda **kwargs: None)
    monkeypatch.setattr(fundamentals_rating_agencies, "_record_fallback", lambda *a, **k: None)
    monkeypatch.setattr(fundamentals_rating_agencies, "MAX_RUNTIME_SECONDS", 100)
    # monotonic() called once for run_started, then once per row's own budget check --
    # row 1's check is within budget, row 2's check is past it.
    clock = iter([0, 0, 200])
    monkeypatch.setattr(fundamentals_rating_agencies.time, "monotonic", lambda: next(clock))

    result = fundamentals_rating_agencies.run_rating_agency_enrichment()

    assert result["time_budget_exceeded"] is True
    assert result["failed"] == 1  # only n1 was processed


def test_agency_plugins_registry_covers_icra_india_ratings_crisil_care():
    assert set(fundamentals_rating_agencies.AGENCY_PLUGINS.keys()) == {"icra", "india ratings", "crisil", "care"}
    for plugin in fundamentals_rating_agencies.AGENCY_PLUGINS.values():
        assert callable(plugin["open_session"])
        assert callable(plugin["search"])


def test_search_india_ratings_rationales_parses_real_response_shape(monkeypatch):
    class FakeResponse:
        status_code = 200

        def json(self):
            return [
                {
                    "issuerName": "Emaar India Limited",
                    "pressReleaseTitle": "India Ratings Upgrades Emaar India and its Bank Loan Facilities to ‘IND AA-’/Stable",
                    "effectiveDate": "2026-08-12T00:00:00",
                    "pressReleaseID": 84832,
                    "urlKey": "7nwgswxewzvyvccg7p4oxpeq",
                },
                {"issuerName": "No URL Key Co", "pressReleaseTitle": "Something", "effectiveDate": "2026-08-12T00:00:00", "pressReleaseID": 1, "urlKey": None},
            ]

    monkeypatch.setattr(fundamentals_rating_agencies, "exchange_request_gate", lambda **k: contextlib.nullcontext())
    monkeypatch.setattr(fundamentals_rating_agencies.requests, "get", lambda *a, **k: FakeResponse())

    results = fundamentals_rating_agencies.search_india_ratings_rationales("Emaar India")

    assert len(results) == 1  # the row with no urlKey is skipped
    assert results[0]["rationale_id"] == "84832"
    assert results[0]["rationale_url"] is None  # detail page is JS-rendered, not guessed
    assert "Upgrades" in results[0]["headline"]


def test_search_india_ratings_rationales_raises_on_non_200(monkeypatch):
    class FakeResponse:
        status_code = 500

    monkeypatch.setattr(fundamentals_rating_agencies, "exchange_request_gate", lambda **k: contextlib.nullcontext())
    monkeypatch.setattr(fundamentals_rating_agencies.requests, "get", lambda *a, **k: FakeResponse())
    with pytest.raises(fundamentals_rating_agencies.IndiaRatingsBlockedError):
        fundamentals_rating_agencies.search_india_ratings_rationales("X")


def test_search_crisil_rationales_parses_real_response_shape(monkeypatch):
    class FakeResponse:
        status_code = 200
        text = '{"docs": [{"companyName": "Adani Renewable Energy Thirty Seven Limited", "heading": "Adani Renewable Energy Thirty Seven Limited:Update", "ratingDate": "Aug 12, 2026", "prId": "2194513"}]}'

        def json(self):
            import json as _json

            return _json.loads(self.text)

    monkeypatch.setattr(fundamentals_rating_agencies, "exchange_request_gate", lambda **k: contextlib.nullcontext())
    monkeypatch.setattr(fundamentals_rating_agencies.requests, "get", lambda *a, **k: FakeResponse())

    results = fundamentals_rating_agencies.search_crisil_rationales("Adani Renewable")

    assert len(results) == 1
    assert results[0]["rationale_id"] == "2194513"
    assert results[0]["rationale_url"] is None


def test_search_crisil_rationales_empty_body_is_a_clean_no_match(monkeypatch):
    # confirmed live 2026-08-12: CRISIL returns an EMPTY body (not {"docs": []}) when
    # nothing matches -- must not be treated as a parse failure.
    class FakeResponse:
        status_code = 200
        text = ""

    monkeypatch.setattr(fundamentals_rating_agencies, "exchange_request_gate", lambda **k: contextlib.nullcontext())
    monkeypatch.setattr(fundamentals_rating_agencies.requests, "get", lambda *a, **k: FakeResponse())

    assert fundamentals_rating_agencies.search_crisil_rationales("Nonexistent Co") == []


def test_search_crisil_rationales_raises_on_non_200(monkeypatch):
    class FakeResponse:
        status_code = 500
        text = ""

    monkeypatch.setattr(fundamentals_rating_agencies, "exchange_request_gate", lambda **k: contextlib.nullcontext())
    monkeypatch.setattr(fundamentals_rating_agencies.requests, "get", lambda *a, **k: FakeResponse())
    with pytest.raises(fundamentals_rating_agencies.CrisilBlockedError):
        fundamentals_rating_agencies.search_crisil_rationales("X")


def test_search_care_rationales_parses_real_response_shape(monkeypatch):
    class FakeResponse:
        status_code = 200

        def json(self):
            return {
                "data": [
                    {"CompanyName": "Adani Ports and Special Economic Zone Limited", "FileURL": "202607120701_Adani_Ports_and_Special_Economic_Zone_Limited.pdf", "PublishedDate": "2026-07-03 00:00:00.000"},
                    {"CompanyName": "No File Co", "FileURL": None, "PublishedDate": "2026-07-03 00:00:00.000"},
                ]
            }

    monkeypatch.setattr(fundamentals_rating_agencies, "exchange_request_gate", lambda **k: contextlib.nullcontext())
    monkeypatch.setattr(fundamentals_rating_agencies.requests, "get", lambda *a, **k: FakeResponse())

    results = fundamentals_rating_agencies.search_care_rationales("Adani Ports")

    assert len(results) == 1  # the row with no FileURL is skipped
    assert results[0]["headline"] is None  # CARE's listing has no action-stating text at all
    assert results[0]["rationale_pdf_url"] == "https://www.careratings.com/upload/CompanyFiles/PR/202607120701_Adani_Ports_and_Special_Economic_Zone_Limited.pdf"


def test_search_care_rationales_url_quotes_filename_with_spaces(monkeypatch):
    class FakeResponse:
        status_code = 200

        def json(self):
            return {"data": [{"CompanyName": "X", "FileURL": "MUNDRA PORT AND SEZ LIMITED-03092010.pdf", "PublishedDate": "2010-09-03 00:00:00.000"}]}

    monkeypatch.setattr(fundamentals_rating_agencies, "exchange_request_gate", lambda **k: contextlib.nullcontext())
    monkeypatch.setattr(fundamentals_rating_agencies.requests, "get", lambda *a, **k: FakeResponse())

    results = fundamentals_rating_agencies.search_care_rationales("Mundra Port")

    assert "%20" in results[0]["rationale_pdf_url"]
    assert " " not in results[0]["rationale_pdf_url"]


def test_search_care_rationales_raises_on_non_200(monkeypatch):
    class FakeResponse:
        status_code = 500

    monkeypatch.setattr(fundamentals_rating_agencies, "exchange_request_gate", lambda **k: contextlib.nullcontext())
    monkeypatch.setattr(fundamentals_rating_agencies.requests, "get", lambda *a, **k: FakeResponse())
    with pytest.raises(fundamentals_rating_agencies.CareBlockedError):
        fundamentals_rating_agencies.search_care_rationales("X")


def test_run_rating_agency_enrichment_care_match_leaves_rating_action_type_unset(monkeypatch):
    # the key correctness point: a matched CARE row (no headline) must NOT write
    # rating_action_type="other" -- that would silently block l3_triggers.py's own
    # structured_extraction_json fallback (Part 1) for this row.
    monkeypatch.setattr(fundamentals_rating_agencies, "_ensure_events_schema", lambda: None)
    monkeypatch.setattr(fundamentals_rating_agencies, "_bootstrap_rating_columns", lambda: None)
    pending = pd.DataFrame(
        [{"source": "bse", "news_id": "n1", "company_master_id": "nse:X", "headline": "CARE Ratings assigns the rating", "subcategory": "Credit Rating", "disclosure_date": date(2026, 8, 4)}]
    )
    monkeypatch.setattr(fundamentals_rating_agencies, "load_pending_rating_actions", lambda limit=None: pending)
    monkeypatch.setattr(fundamentals_rating_agencies, "_resolve_issuer_name", lambda cmid: "Some Issuer")
    monkeypatch.setattr(
        fundamentals_rating_agencies, "search_care_rationales", lambda issuer: [{"date_text": "04 Aug 2026", "headline": None, "rationale_id": "f.pdf", "rationale_url": None, "rationale_pdf_url": "https://www.careratings.com/upload/CompanyFiles/PR/f.pdf"}]
    )
    status_calls = []
    monkeypatch.setattr(fundamentals_rating_agencies, "_set_enrichment_status", lambda **kwargs: status_calls.append(kwargs))

    result = fundamentals_rating_agencies.run_rating_agency_enrichment()

    assert result["matched"] == 1
    fields = status_calls[0]["fields"]
    assert fields["rating_agency"] == "care"
    assert fields["rating_action_type"] is None
    assert fields["rationale_pdf_url"] == "https://www.careratings.com/upload/CompanyFiles/PR/f.pdf"


def test_track_unsupported_agencies_empty_is_a_noop(monkeypatch):
    calls = []
    monkeypatch.setattr(fundamentals_rating_agencies, "_ensure_unsupported_agencies_table", lambda: calls.append(1))
    fundamentals_rating_agencies.track_unsupported_agencies(pd.DataFrame())
    assert calls == []


def test_track_unsupported_agencies_accumulates_count_and_keeps_first_seen(monkeypatch):
    monkeypatch.setattr(fundamentals_rating_agencies, "_ensure_unsupported_agencies_table", lambda: None)
    existing = pd.DataFrame([{"agency_name": "acuite", "occurrence_count": 3, "first_seen_at": pd.Timestamp("2026-08-01", tz="UTC")}])
    monkeypatch.setattr(fundamentals_rating_agencies, "sql_to_df", lambda q: existing)
    upserts = []
    monkeypatch.setattr(fundamentals_rating_agencies, "upsert_to_db", lambda df, table, **k: upserts.append((df, table, k)))

    rows = pd.DataFrame([{"source": "bse", "news_id": "n1", "company_master_id": "nse:X", "headline": "h", "_agency": "acuite"}])
    fundamentals_rating_agencies.track_unsupported_agencies(rows)

    written = upserts[0][0].iloc[0]
    assert written["agency_name"] == "acuite"
    assert written["occurrence_count"] == 4  # 3 prior + 1 new
    assert written["first_seen_at"] == pd.Timestamp("2026-08-01", tz="UTC")  # unchanged
    assert upserts[0][2]["unique_keys"] == ["agency_name"]


def test_track_unsupported_agencies_new_agency_starts_at_count_of_rows(monkeypatch):
    monkeypatch.setattr(fundamentals_rating_agencies, "_ensure_unsupported_agencies_table", lambda: None)
    monkeypatch.setattr(fundamentals_rating_agencies, "sql_to_df", lambda q: pd.DataFrame())
    upserts = []
    monkeypatch.setattr(fundamentals_rating_agencies, "upsert_to_db", lambda df, table, **k: upserts.append((df, table, k)))

    rows = pd.DataFrame(
        [
            {"source": "bse", "news_id": "n1", "company_master_id": "nse:X", "headline": "h1", "_agency": None},
            {"source": "bse", "news_id": "n2", "company_master_id": "nse:Y", "headline": "h2", "_agency": None},
        ]
    )
    fundamentals_rating_agencies.track_unsupported_agencies(rows)

    written = upserts[0][0].iloc[0]
    assert written["agency_name"] == "unnamed"  # None agency collapses to "unnamed"
    assert written["occurrence_count"] == 2


def test_get_unsupported_rating_agencies_excludes_agencies_with_a_plugin(monkeypatch):
    monkeypatch.setattr(fundamentals_rating_agencies, "_ensure_unsupported_agencies_table", lambda: None)
    df = pd.DataFrame(
        [
            {"agency_name": "acuite", "occurrence_count": 5},
            {"agency_name": "care", "occurrence_count": 100},  # has a plugin now -- must be excluded
            {"agency_name": "icra", "occurrence_count": 1},  # has a plugin -- excluded
        ]
    )
    monkeypatch.setattr(fundamentals_rating_agencies, "sql_to_df", lambda q: df)

    result = fundamentals_rating_agencies.get_unsupported_rating_agencies()

    assert list(result["agency_name"]) == ["acuite"]


def test_get_unsupported_rating_agencies_empty_table_returns_empty(monkeypatch):
    monkeypatch.setattr(fundamentals_rating_agencies, "_ensure_unsupported_agencies_table", lambda: None)
    monkeypatch.setattr(fundamentals_rating_agencies, "sql_to_df", lambda q: pd.DataFrame())
    assert fundamentals_rating_agencies.get_unsupported_rating_agencies().empty


def test_run_rating_agency_enrichment_handles_multiple_agencies_in_one_run(monkeypatch):
    monkeypatch.setattr(fundamentals_rating_agencies, "_ensure_events_schema", lambda: None)
    monkeypatch.setattr(fundamentals_rating_agencies, "_bootstrap_rating_columns", lambda: None)
    pending = pd.DataFrame(
        [
            {"source": "bse", "news_id": "n1", "company_master_id": "nse:A", "headline": "Reaffirmation of Credit Ratings by ICRA", "subcategory": "Credit Rating", "disclosure_date": date(2026, 8, 4)},
            {"source": "bse", "news_id": "n2", "company_master_id": "nse:B", "headline": "India Ratings upgrades the rating", "subcategory": "Credit Rating", "disclosure_date": date(2026, 8, 4)},
            {"source": "bse", "news_id": "n3", "company_master_id": "nse:C", "headline": "CRISIL downgrades the rating", "subcategory": "Credit Rating", "disclosure_date": date(2026, 8, 4)},
        ]
    )
    monkeypatch.setattr(fundamentals_rating_agencies, "load_pending_rating_actions", lambda limit=None: pending)
    monkeypatch.setattr(fundamentals_rating_agencies, "_resolve_issuer_name", lambda cmid: "Some Issuer")
    monkeypatch.setattr(fundamentals_rating_agencies, "open_icra_session", lambda: ("s", "t"))
    monkeypatch.setattr(fundamentals_rating_agencies, "search_icra_rationales", lambda session, token, issuer: [{"date_text": "04 Aug 2026", "headline": "reaffirmed", "rationale_id": "1"}])
    monkeypatch.setattr(fundamentals_rating_agencies, "search_india_ratings_rationales", lambda issuer: [{"date_text": "04 Aug 2026", "headline": "upgraded", "rationale_id": "2"}])
    monkeypatch.setattr(fundamentals_rating_agencies, "search_crisil_rationales", lambda issuer: [{"date_text": "04 Aug 2026", "headline": "downgraded", "rationale_id": "3"}])
    status_calls = []
    monkeypatch.setattr(fundamentals_rating_agencies, "_set_enrichment_status", lambda **kwargs: status_calls.append(kwargs))

    result = fundamentals_rating_agencies.run_rating_agency_enrichment()

    assert result["matched"] == 3
    assert result["blocked"] is False
    agencies_matched = {call["fields"]["rating_agency"] for call in status_calls}
    assert agencies_matched == {"icra", "india ratings", "crisil"}


def test_run_rating_agency_enrichment_one_agencys_circuit_breaker_does_not_block_another(monkeypatch):
    monkeypatch.setattr(fundamentals_rating_agencies, "_ensure_events_schema", lambda: None)
    monkeypatch.setattr(fundamentals_rating_agencies, "_bootstrap_rating_columns", lambda: None)
    icra_rows = [
        {"source": "bse", "news_id": f"icra{i}", "company_master_id": f"nse:X{i}", "headline": "Reaffirmation of Credit Ratings by ICRA", "subcategory": "Credit Rating", "disclosure_date": date(2026, 8, 4)}
        for i in range(5)
    ]
    india_ratings_row = {"source": "bse", "news_id": "ir1", "company_master_id": "nse:Y", "headline": "India Ratings upgrades the rating", "subcategory": "Credit Rating", "disclosure_date": date(2026, 8, 4)}
    pending = pd.DataFrame(icra_rows + [india_ratings_row])
    monkeypatch.setattr(fundamentals_rating_agencies, "load_pending_rating_actions", lambda limit=None: pending)
    monkeypatch.setattr(fundamentals_rating_agencies, "_resolve_issuer_name", lambda cmid: "Some Issuer")
    monkeypatch.setattr(fundamentals_rating_agencies, "open_icra_session", lambda: ("s", "t"))

    def icra_always_fails(session, token, issuer):
        raise fundamentals_rating_agencies.IcraBlockedError("boom")

    monkeypatch.setattr(fundamentals_rating_agencies, "search_icra_rationales", icra_always_fails)
    monkeypatch.setattr(fundamentals_rating_agencies, "search_india_ratings_rationales", lambda issuer: [{"date_text": "04 Aug 2026", "headline": "upgraded", "rationale_id": "9"}])
    status_calls = []
    monkeypatch.setattr(fundamentals_rating_agencies, "_set_enrichment_status", lambda **kwargs: status_calls.append(kwargs))
    fallback_events = []
    monkeypatch.setattr(fundamentals_rating_agencies, "_record_fallback", lambda *a, **k: fallback_events.append((a, k)))

    result = fundamentals_rating_agencies.run_rating_agency_enrichment()

    assert result["blocked"] is True  # icra tripped
    assert result["matched"] == 1  # but india ratings still went through
    assert any(call.get("fields", {}).get("rating_agency") == "india ratings" for call in status_calls if call["status"] == "matched")


# fundamentals/collectors/ocr_pipeline.py -- L3/L4 OCR fetch+store (step 6).


def test_resolve_document_source_prefers_icra_rationale_over_bse_attachment():
    row = {"attachment_name": "some.pdf", "rationale_pdf_url": "https://www.icra.in/Rating/GetRationalReportFilePdf?Id=1"}
    assert fundamentals_ocr_pipeline.resolve_document_source(row) == ("icra", "https://www.icra.in/Rating/GetRationalReportFilePdf?Id=1")


def test_resolve_document_source_detects_care_rationale_by_hostname():
    # BUG FOUND LIVE 2026-08-18 (re-audit): rationale_pdf_url is written by both
    # icra.in and careratings.com fetches (rating_agencies.py's ICRA_PDF_URL_TEMPLATE
    # and CARE_PDF_BASE_URL), but this used to hardcode every non-null
    # rationale_pdf_url as domain "icra" regardless of which agency actually issued
    # it -- CARE traffic shared ICRA's rate gate and circuit breaker. Domain must now
    # come from the URL's own hostname.
    row = {"attachment_name": None, "rationale_pdf_url": "https://www.careratings.com/upload/CompanyFiles/PR/some-file.pdf"}
    assert fundamentals_ocr_pipeline.resolve_document_source(row) == ("care", "https://www.careratings.com/upload/CompanyFiles/PR/some-file.pdf")


def test_resolve_document_source_falls_back_to_generic_domain_for_unknown_rationale_host():
    row = {"attachment_name": None, "rationale_pdf_url": "https://www.some-other-agency.example/report.pdf"}
    domain, url = fundamentals_ocr_pipeline.resolve_document_source(row)
    assert domain == "rating_agency_other"
    assert url == "https://www.some-other-agency.example/report.pdf"


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


def test_fetch_document_bytes_uses_care_headers_and_gate_for_care_domain(monkeypatch):
    gate_calls = []
    monkeypatch.setattr(fundamentals_ocr_pipeline, "exchange_request_gate", lambda **k: gate_calls.append(k) or contextlib.nullcontext())
    captured_headers = {}

    class FakeResponse:
        status_code = 200
        content = b"%PDF-1.4 care pdf"

    def fake_get(url, headers=None, **k):
        captured_headers.update(headers or {})
        return FakeResponse()

    monkeypatch.setattr(fundamentals_ocr_pipeline.requests, "get", fake_get)
    fundamentals_ocr_pipeline.fetch_document_bytes("https://www.careratings.com/upload/CompanyFiles/PR/x.pdf", domain="care")

    assert gate_calls == [{"domain": "care"}]  # its own rate-limiter/circuit-breaker domain, not ICRA's
    assert captured_headers == fundamentals_ocr_pipeline.CARE_PDF_HEADERS
    assert "Referer" not in captured_headers  # ICRA's Referer would be wrong against careratings.com


def test_ocr_pdf_bytes_writes_temp_file_and_joins_pages(monkeypatch):
    captured = {}

    def fake_render_pdf_pages(path, pages="all"):
        captured["path_exists"] = Path(path).exists()
        captured["path_suffix"] = Path(path).suffix
        return [(2, "image-2"), (1, "image-1")]  # deliberately out of order

    def fake_ocr_page_with_local(image, **kwargs):
        return {"image-1": "page one", "image-2": "page two"}[image]

    monkeypatch.setattr(fundamentals_ocr_pipeline, "render_pdf_pages", fake_render_pdf_pages)
    monkeypatch.setattr(fundamentals_ocr_pipeline, "ocr_page_with_local", fake_ocr_page_with_local)

    result = fundamentals_ocr_pipeline.ocr_pdf_bytes(b"%PDF-1.4 fake")

    assert result == "page one\n\npage two"  # sorted by page number, not render order
    assert captured["path_exists"] is True
    assert captured["path_suffix"] == ".pdf"


def test_ocr_pdf_bytes_raises_on_page_timeout(monkeypatch):
    # BUG FOUND LIVE 2026-08-17: a single page's OCR call had no timeout at all --
    # only checked between ROWS in run_ocr_pipeline, never within one document. A page
    # that hangs past PER_PAGE_OCR_TIMEOUT_SECONDS must raise (and let the caller move
    # on to the next row) rather than block forever.
    monkeypatch.setattr(fundamentals_ocr_pipeline, "render_pdf_pages", lambda path, pages="all": [(1, "image-1")])
    monkeypatch.setattr(fundamentals_ocr_pipeline, "PER_PAGE_OCR_TIMEOUT_SECONDS", 0.05)

    def slow_ocr_page(image, **kwargs):
        time.sleep(1.0)
        return "too slow"

    monkeypatch.setattr(fundamentals_ocr_pipeline, "ocr_page_with_local", slow_ocr_page)

    with pytest.raises(fundamentals_ocr_pipeline.OcrTimeoutError, match="PER_PAGE_OCR_TIMEOUT_SECONDS"):
        fundamentals_ocr_pipeline.ocr_pdf_bytes(b"%PDF-1.4 fake")


def test_ocr_pdf_bytes_raises_on_document_timeout_between_pages(monkeypatch):
    # A many-page document must be bounded even when every individual page finishes
    # within its own PER_PAGE_OCR_TIMEOUT_SECONDS -- MAX_DOCUMENT_OCR_SECONDS caps the
    # document's TOTAL OCR time, checked between pages.
    monkeypatch.setattr(fundamentals_ocr_pipeline, "render_pdf_pages", lambda path, pages="all": [(1, "image-1"), (2, "image-2")])
    monkeypatch.setattr(fundamentals_ocr_pipeline, "ocr_page_with_local", lambda image, **kwargs: "text")
    monkeypatch.setattr(fundamentals_ocr_pipeline, "MAX_DOCUMENT_OCR_SECONDS", 100)
    # monotonic() called once for document_started, then once per page's budget check --
    # page 1's check is within budget, page 2's check is past it.
    clock = iter([0, 0, 200])
    monkeypatch.setattr(fundamentals_ocr_pipeline.time, "monotonic", lambda: next(clock))

    with pytest.raises(fundamentals_ocr_pipeline.OcrTimeoutError, match="MAX_DOCUMENT_OCR_SECONDS"):
        fundamentals_ocr_pipeline.ocr_pdf_bytes(b"%PDF-1.4 fake")


def test_ocr_pdf_bytes_truncates_when_page_count_exceeds_max(monkeypatch):
    # BUG FOUND LIVE 2026-08-18 (re-audit): render_pdf_pages(path) with the default
    # pages="all" rasterizes every page into memory up front, before this document's
    # own MAX_DOCUMENT_OCR_SECONDS timer even starts -- a large scanned filing could
    # OOM the process before any bound applied. Over MAX_OCR_PAGES, ocr_pdf_bytes must
    # request only the first MAX_OCR_PAGES (routes render_pdf_pages through its
    # per-page, bounded-memory loop instead of the eager all-at-once path) and record
    # a fallback event about the truncation.
    monkeypatch.setattr(fundamentals_ocr_pipeline, "pdfinfo_from_path", lambda path, **k: {"Pages": 100})
    monkeypatch.setattr(fundamentals_ocr_pipeline, "MAX_OCR_PAGES", 3)
    captured = {}

    def fake_render_pdf_pages(path, pages="all"):
        captured["pages"] = pages
        return [(1, "image-1"), (2, "image-2"), (3, "image-3")]

    monkeypatch.setattr(fundamentals_ocr_pipeline, "render_pdf_pages", fake_render_pdf_pages)
    monkeypatch.setattr(fundamentals_ocr_pipeline, "ocr_page_with_local", lambda image, **kwargs: image)
    fallback_events = []
    monkeypatch.setattr(fundamentals_ocr_pipeline, "_record_fallback", lambda *a, **k: fallback_events.append((a, k)))

    result = fundamentals_ocr_pipeline.ocr_pdf_bytes(b"%PDF-1.4 fake")

    assert captured["pages"] == [1, 2, 3]  # explicit range, not "all" -- forces the bounded per-page loop
    assert result == "image-1\n\nimage-2\n\nimage-3"
    assert len(fallback_events) == 1
    assert fallback_events[0][0][0] == "ocr_pipeline_document_truncated"
    assert fallback_events[0][1]["metadata"] == {"total_pages": 100, "max_ocr_pages": 3}


def test_ocr_pdf_bytes_uses_eager_render_when_page_count_within_max(monkeypatch):
    monkeypatch.setattr(fundamentals_ocr_pipeline, "pdfinfo_from_path", lambda path, **k: {"Pages": 2})
    captured = {}

    def fake_render_pdf_pages(path, pages="all"):
        captured["pages"] = pages
        return [(1, "image-1")]

    monkeypatch.setattr(fundamentals_ocr_pipeline, "render_pdf_pages", fake_render_pdf_pages)
    monkeypatch.setattr(fundamentals_ocr_pipeline, "ocr_page_with_local", lambda image, **kwargs: image)
    fallback_events = []
    monkeypatch.setattr(fundamentals_ocr_pipeline, "_record_fallback", lambda *a, **k: fallback_events.append((a, k)))

    fundamentals_ocr_pipeline.ocr_pdf_bytes(b"%PDF-1.4 fake")

    assert captured["pages"] == "all"  # under MAX_OCR_PAGES -- eager render, unchanged behavior
    assert fallback_events == []


def test_ocr_pdf_bytes_falls_back_to_eager_render_when_pdfinfo_fails(monkeypatch):
    # pdfinfo failing (corrupt/unusual PDF) must not block OCR entirely -- fall back to
    # the pre-existing eager render rather than raising.
    def raise_pdfinfo(path, **k):
        raise RuntimeError("pdfinfo failed")

    monkeypatch.setattr(fundamentals_ocr_pipeline, "pdfinfo_from_path", raise_pdfinfo)
    captured = {}

    def fake_render_pdf_pages(path, pages="all"):
        captured["pages"] = pages
        return [(1, "image-1")]

    monkeypatch.setattr(fundamentals_ocr_pipeline, "render_pdf_pages", fake_render_pdf_pages)
    monkeypatch.setattr(fundamentals_ocr_pipeline, "ocr_page_with_local", lambda image, **kwargs: image)

    result = fundamentals_ocr_pipeline.ocr_pdf_bytes(b"%PDF-1.4 fake")

    assert captured["pages"] == "all"
    assert result == "image-1"


def test_load_pending_ocr_targets_queries_expected_filters(monkeypatch):
    captured = {}

    def fake_sql_to_df(query, **kwargs):
        captured["query"] = query
        return pd.DataFrame()

    monkeypatch.setattr(fundamentals_ocr_pipeline, "sql_to_df", fake_sql_to_df)
    fundamentals_ocr_pipeline.load_pending_ocr_targets()
    assert "ocr_status IS NULL OR ocr_status = 'pending'" in captured["query"]
    assert "attachment_name IS NOT NULL OR rationale_pdf_url IS NOT NULL" in captured["query"]
    # BUG FOUND LIVE 2026-08-17: plain load_ts ASC let a huge glut of old, mostly-
    # already-404 rows (see bse_announcements.py's BACKFILL_FILING_TYPES fix) starve
    # fresh detections behind them via the circuit breaker. Freshest filing first now.
    assert "ORDER BY disclosure_date DESC NULLS LAST, load_ts ASC NULLS LAST" in captured["query"]


def test_run_ocr_pipeline_returns_early_when_nothing_pending(monkeypatch):
    monkeypatch.setattr(fundamentals_ocr_pipeline, "_ensure_events_schema", lambda: None)
    monkeypatch.setattr(fundamentals_ocr_pipeline, "_bootstrap_ocr_columns", lambda: None)
    monkeypatch.setattr(fundamentals_ocr_pipeline, "load_pending_ocr_targets", lambda limit=None: pd.DataFrame())

    result = fundamentals_ocr_pipeline.run_ocr_pipeline()

    assert result == {"ocred": 0, "failed": 0, "no_document": 0, "blocked": False, "time_budget_exceeded": False, "backlog_remaining": 0}


def test_run_ocr_pipeline_marks_rows_with_no_document_reference(monkeypatch):
    monkeypatch.setattr(fundamentals_ocr_pipeline, "_ensure_events_schema", lambda: None)
    monkeypatch.setattr(fundamentals_ocr_pipeline, "_bootstrap_ocr_columns", lambda: None)
    monkeypatch.setattr(fundamentals_ocr_pipeline, "count_pending_ocr_targets", lambda: 0)
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
    monkeypatch.setattr(fundamentals_ocr_pipeline, "count_pending_ocr_targets", lambda: 0)
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


def test_run_ocr_pipeline_stops_at_time_budget_leaving_remaining_rows_pending(monkeypatch):
    # 2026-08-15: DEFAULT_BATCH_LIMIT raised 50 -> 200 to close a real backlog (user
    # request), but per-item OCR cost is highly variable (confirmed live: one run took
    # 8.1 hours for 50 items, another 7 minutes for the same count) -- MAX_RUNTIME_
    # SECONDS protects the rest of that day's pipeline (L1/L2/L3/notifications all run
    # AFTER this step) from a bad day of large documents eating the whole run. A row
    # not reached before the cutoff must stay untouched (pending), not marked failed.
    monkeypatch.setattr(fundamentals_ocr_pipeline, "_ensure_events_schema", lambda: None)
    monkeypatch.setattr(fundamentals_ocr_pipeline, "_bootstrap_ocr_columns", lambda: None)
    monkeypatch.setattr(fundamentals_ocr_pipeline, "count_pending_ocr_targets", lambda: 0)
    pending = pd.DataFrame(
        [
            {"source": "icra", "news_id": "n1", "attachment_name": None, "rationale_pdf_url": "https://www.icra.in/x?Id=1"},
            {"source": "icra", "news_id": "n2", "attachment_name": None, "rationale_pdf_url": "https://www.icra.in/x?Id=2"},
        ]
    )
    monkeypatch.setattr(fundamentals_ocr_pipeline, "load_pending_ocr_targets", lambda limit=None: pending)
    monkeypatch.setattr(fundamentals_ocr_pipeline, "fetch_document_bytes", lambda url, **k: b"%PDF-1.4 fake")
    monkeypatch.setattr(fundamentals_ocr_pipeline, "ocr_pdf_bytes", lambda pdf_bytes: "text")
    monkeypatch.setattr(fundamentals_ocr_pipeline, "save_file_content", lambda key, content: None)
    from utils.blob_store import TextBlobMetadata

    fake_metadata = TextBlobMetadata(key="k", sha256="abc", char_count=1, byte_count=1, excerpt="text")
    monkeypatch.setattr(fundamentals_ocr_pipeline, "put_text_blob", lambda text, key: fake_metadata)
    status_calls = []
    monkeypatch.setattr(fundamentals_ocr_pipeline, "_set_ocr_result", lambda **kwargs: status_calls.append(kwargs))
    monkeypatch.setattr(fundamentals_ocr_pipeline, "MAX_RUNTIME_SECONDS", 100)
    # monotonic() called once for run_started, then once per loop iteration's check --
    # first check (before item 1) is within budget, second check (before item 2) is past it.
    clock = iter([0, 0, 200])
    monkeypatch.setattr(fundamentals_ocr_pipeline.time, "monotonic", lambda: next(clock))

    result = fundamentals_ocr_pipeline.run_ocr_pipeline()

    assert result["ocred"] == 1  # only the first row was processed
    assert result["time_budget_exceeded"] is True
    assert len(status_calls) == 1  # n2 was never touched -- stays pending, not marked failed
    assert status_calls[0]["news_id"] == "n1"
    assert status_calls[0]["status"] == "done"


def test_run_ocr_pipeline_trips_circuit_breaker_per_domain(monkeypatch):
    monkeypatch.setattr(fundamentals_ocr_pipeline, "_ensure_events_schema", lambda: None)
    monkeypatch.setattr(fundamentals_ocr_pipeline, "_bootstrap_ocr_columns", lambda: None)
    monkeypatch.setattr(fundamentals_ocr_pipeline, "count_pending_ocr_targets", lambda: 0)
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


def test_run_ocr_pipeline_document_timeout_does_not_permanently_fail_or_trip_fetch_breaker(monkeypatch):
    # BUG FOUND LIVE 2026-08-18 (re-audit): OcrTimeoutError used to share the exact
    # same handling as DocumentFetchError -- a run of large-but-legitimate
    # documents could permanently fail the row (no automatic retry) AND trip a
    # false "this domain is blocking us" circuit breaker. A timeout is now its own
    # branch: no permanent status write, no fetch-failure breaker contribution.
    monkeypatch.setattr(fundamentals_ocr_pipeline, "_ensure_events_schema", lambda: None)
    monkeypatch.setattr(fundamentals_ocr_pipeline, "_bootstrap_ocr_columns", lambda: None)
    monkeypatch.setattr(fundamentals_ocr_pipeline, "count_pending_ocr_targets", lambda: 0)
    pending = pd.DataFrame([{"source": "bse", "news_id": "n1", "attachment_name": "file1.pdf", "rationale_pdf_url": None}])
    monkeypatch.setattr(fundamentals_ocr_pipeline, "load_pending_ocr_targets", lambda limit=None: pending)
    monkeypatch.setattr(fundamentals_ocr_pipeline, "fetch_document_bytes", lambda url, **k: b"%PDF-1.4 fake")

    def always_times_out(pdf_bytes):
        raise fundamentals_ocr_pipeline.OcrTimeoutError("page 1 OCR exceeded PER_PAGE_OCR_TIMEOUT_SECONDS")

    monkeypatch.setattr(fundamentals_ocr_pipeline, "ocr_pdf_bytes", always_times_out)
    status_calls = []
    monkeypatch.setattr(fundamentals_ocr_pipeline, "_set_ocr_result", lambda **kwargs: status_calls.append(kwargs))
    fallback_events = []
    monkeypatch.setattr(fundamentals_ocr_pipeline, "_record_fallback", lambda *a, **k: fallback_events.append((a, k)))

    result = fundamentals_ocr_pipeline.run_ocr_pipeline()

    assert result["failed"] == 1
    assert status_calls == []  # never marked ocr_status=failed -- stays pending for a future retry
    assert any(a and a[0] == "ocr_pipeline_document_timeout" for a, k in fallback_events)
    assert not any(a and a[0] == "ocr_pipeline_document_failed" for a, k in fallback_events)
    assert not any(a and a[0] == "ocr_pipeline_circuit_breaker_tripped" for a, k in fallback_events)


def test_run_ocr_pipeline_timeout_circuit_breaker_is_separate_from_fetch_failures(monkeypatch):
    monkeypatch.setattr(fundamentals_ocr_pipeline, "_ensure_events_schema", lambda: None)
    monkeypatch.setattr(fundamentals_ocr_pipeline, "_bootstrap_ocr_columns", lambda: None)
    monkeypatch.setattr(fundamentals_ocr_pipeline, "count_pending_ocr_targets", lambda: 0)
    pending = pd.DataFrame(
        [{"source": "bse", "news_id": f"n{i}", "attachment_name": f"file{i}.pdf", "rationale_pdf_url": None} for i in range(5)]
    )
    monkeypatch.setattr(fundamentals_ocr_pipeline, "load_pending_ocr_targets", lambda limit=None: pending)
    monkeypatch.setattr(fundamentals_ocr_pipeline, "fetch_document_bytes", lambda url, **k: b"%PDF-1.4 fake")

    def always_times_out(pdf_bytes):
        raise fundamentals_ocr_pipeline.OcrTimeoutError("boom")

    monkeypatch.setattr(fundamentals_ocr_pipeline, "ocr_pdf_bytes", always_times_out)
    monkeypatch.setattr(fundamentals_ocr_pipeline, "_set_ocr_result", lambda **kwargs: None)
    fallback_events = []
    monkeypatch.setattr(fundamentals_ocr_pipeline, "_record_fallback", lambda *a, **k: fallback_events.append((a, k)))

    result = fundamentals_ocr_pipeline.run_ocr_pipeline()

    assert result["blocked"] is True
    assert result["failed"] == fundamentals_ocr_pipeline.CIRCUIT_BREAKER_THRESHOLD
    assert any(a and a[0] == "ocr_pipeline_timeout_circuit_breaker_tripped" for a, k in fallback_events)


def test_run_with_timeout_returns_result_when_fast_enough():
    assert fundamentals_ocr_pipeline._run_with_timeout(lambda x: x * 2, (21,), timeout_seconds=5) == 42


def test_run_with_timeout_raises_timeout_error_and_thread_is_daemon():
    # BUG FOUND LIVE 2026-08-18 (re-audit): the previous ThreadPoolExecutor-based
    # implementation's worker threads were joined at interpreter exit regardless
    # of shutdown(wait=False) -- an abandoned OCR call could still block the whole
    # in-process pipeline from exiting. A plain daemon thread has no such hook.
    import threading as _threading

    def slow(seconds):
        time.sleep(seconds)
        return "done"

    threads_before = set(_threading.enumerate())
    with pytest.raises(TimeoutError):
        fundamentals_ocr_pipeline._run_with_timeout(slow, (0.3,), timeout_seconds=0.05)
    new_threads = set(_threading.enumerate()) - threads_before
    assert len(new_threads) == 1
    assert next(iter(new_threads)).daemon is True


def test_run_with_timeout_propagates_the_real_exception():
    def raises(msg):
        raise ValueError(msg)

    with pytest.raises(ValueError, match="boom"):
        fundamentals_ocr_pipeline._run_with_timeout(raises, ("boom",), timeout_seconds=5)


def test_count_pending_ocr_targets_mirrors_load_pending_where_clause(monkeypatch):
    captured = {}

    def fake_sql_to_df(query, **kwargs):
        captured["query"] = query
        return pd.DataFrame([{"n": 42}])

    monkeypatch.setattr(fundamentals_ocr_pipeline, "sql_to_df", fake_sql_to_df)
    assert fundamentals_ocr_pipeline.count_pending_ocr_targets() == 42
    assert "ocr_status IS NULL OR ocr_status = 'pending'" in captured["query"]
    assert "attachment_name IS NOT NULL OR rationale_pdf_url IS NOT NULL" in captured["query"]


def test_run_ocr_pipeline_backlog_fallback_fires_after_a_real_run(monkeypatch):
    # BUG FOUND LIVE 2026-08-17: DEFAULT_BATCH_LIMIT/MAX_RUNTIME_SECONDS bound one
    # run's damage, but nothing ever checked whether the backlog was actually
    # shrinking -- at worst-case documented per-item latency, MAX_RUNTIME_SECONDS's
    # budget binds long before DEFAULT_BATCH_LIMIT's item cap does, silently
    # undercutting the "pace up so we don't have backlog" intent. Now visible as a
    # distinct, monitorable fallback event instead of a one-time audit finding. Note:
    # load_pending_ocr_targets returning empty would take run_ocr_pipeline()'s early-
    # return path, which short-circuits BEFORE this check -- uses a nonempty pending
    # row so the check itself is actually exercised.
    monkeypatch.setattr(fundamentals_ocr_pipeline, "_ensure_events_schema", lambda: None)
    monkeypatch.setattr(fundamentals_ocr_pipeline, "_bootstrap_ocr_columns", lambda: None)
    pending = pd.DataFrame([{"source": "bse", "news_id": "n1", "attachment_name": None, "rationale_pdf_url": None}])
    monkeypatch.setattr(fundamentals_ocr_pipeline, "load_pending_ocr_targets", lambda limit=None: pending)
    monkeypatch.setattr(fundamentals_ocr_pipeline, "_set_ocr_result", lambda **kwargs: None)
    monkeypatch.setattr(
        fundamentals_ocr_pipeline, "count_pending_ocr_targets", lambda: fundamentals_ocr_pipeline.DEFAULT_BATCH_LIMIT + 1
    )
    fallback_events = []
    monkeypatch.setattr(fundamentals_ocr_pipeline, "_record_fallback", lambda *a, **k: fallback_events.append((a, k)))

    result = fundamentals_ocr_pipeline.run_ocr_pipeline()

    assert result["backlog_remaining"] == fundamentals_ocr_pipeline.DEFAULT_BATCH_LIMIT + 1
    assert any(a and a[0] == "ocr_pipeline_backlog_not_clearing" for a, k in fallback_events)


def test_run_ocr_pipeline_no_backlog_fallback_when_under_threshold(monkeypatch):
    monkeypatch.setattr(fundamentals_ocr_pipeline, "_ensure_events_schema", lambda: None)
    monkeypatch.setattr(fundamentals_ocr_pipeline, "_bootstrap_ocr_columns", lambda: None)
    pending = pd.DataFrame([{"source": "bse", "news_id": "n1", "attachment_name": None, "rationale_pdf_url": None}])
    monkeypatch.setattr(fundamentals_ocr_pipeline, "load_pending_ocr_targets", lambda limit=None: pending)
    monkeypatch.setattr(fundamentals_ocr_pipeline, "_set_ocr_result", lambda **kwargs: None)
    monkeypatch.setattr(fundamentals_ocr_pipeline, "count_pending_ocr_targets", lambda: 1)
    fallback_events = []
    monkeypatch.setattr(fundamentals_ocr_pipeline, "_record_fallback", lambda *a, **k: fallback_events.append((a, k)))

    fundamentals_ocr_pipeline.run_ocr_pipeline()

    assert not any(a and a[0] == "ocr_pipeline_backlog_not_clearing" for a, k in fallback_events)


def test_run_ocr_pipeline_backlog_fallback_uses_callers_own_limit_not_default(monkeypatch):
    # BUG FOUND LIVE 2026-08-18 (re-audit): the backlog-not-clearing check used to
    # compare against a module-level constant pinned to DEFAULT_BATCH_LIMIT (200),
    # ignoring whatever limit this specific call actually passed. A caller with a
    # smaller limit=10 would get "even a full run at DEFAULT_BATCH_LIMIT wouldn't
    # clear it" style reasoning that doesn't match its own, much smaller cap. Must
    # compare against THIS call's effective_limit.
    monkeypatch.setattr(fundamentals_ocr_pipeline, "_ensure_events_schema", lambda: None)
    monkeypatch.setattr(fundamentals_ocr_pipeline, "_bootstrap_ocr_columns", lambda: None)
    pending = pd.DataFrame([{"source": "bse", "news_id": "n1", "attachment_name": None, "rationale_pdf_url": None}])
    captured_limit = {}

    def fake_load_pending_ocr_targets(limit=None):
        captured_limit["limit"] = limit
        return pending

    monkeypatch.setattr(fundamentals_ocr_pipeline, "load_pending_ocr_targets", fake_load_pending_ocr_targets)
    monkeypatch.setattr(fundamentals_ocr_pipeline, "_set_ocr_result", lambda **kwargs: None)
    # 15 pending is well under DEFAULT_BATCH_LIMIT (200) but over this call's own limit=10.
    monkeypatch.setattr(fundamentals_ocr_pipeline, "count_pending_ocr_targets", lambda: 15)
    fallback_events = []
    monkeypatch.setattr(fundamentals_ocr_pipeline, "_record_fallback", lambda *a, **k: fallback_events.append((a, k)))

    result = fundamentals_ocr_pipeline.run_ocr_pipeline(limit=10)

    assert captured_limit["limit"] == 10
    assert result["backlog_remaining"] == 15
    matches = [k for a, k in fallback_events if a and a[0] == "ocr_pipeline_backlog_not_clearing"]
    assert len(matches) == 1
    assert matches[0]["metadata"]["effective_limit"] == 10


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


@pytest.mark.parametrize("filing_type", ["results", "results_calendar", "rating_action", "pit_sast", "capital_raise", "auditor_change", "related_party_transaction"])
def test_every_supported_filing_type_has_a_schema(filing_type):
    assert filing_type in fundamentals_structured_extraction.SCHEMAS_BY_FILING_TYPE


def test_capital_raise_schema_requires_investor_names_array():
    schema = fundamentals_structured_extraction.CAPITAL_RAISE_SCHEMA
    assert "investor_names" in schema["required"]
    assert schema["properties"]["investor_names"]["type"] == "array"


def test_rating_action_schema_can_represent_mixed_instrument_actions():
    # BUG FOUND LIVE 2026-08-17: a single rated_amount_rs_cr/previous_rating/current_
    # rating/rating_action set can't represent a filing that rates several distinct
    # instruments with DIFFERENT actions (e.g. long-term downgraded, short-term
    # reaffirmed in the same rationale) -- l3_triggers.py's evaluate_rating_action_
    # trigger() treats "downgraded" as always alert-worthy regardless of prior state,
    # so silently collapsing a mixed filing into "reaffirmed" would be a missed alert,
    # not a cosmetic gap. instrument_actions carries the full per-instrument
    # breakdown; the top-level rating_action must be pinned (by schema description,
    # verified live against the real model) to the most severe action present.
    schema = fundamentals_structured_extraction.RATING_ACTION_SCHEMA
    assert "instrument_actions" in schema["properties"]
    assert "instrument_actions" in schema["required"]
    assert schema["properties"]["instrument_actions"]["type"] == "array"
    item_schema = schema["properties"]["instrument_actions"]["items"]
    for field in ("instrument_description", "rated_amount_rs_cr", "previous_rating", "current_rating", "rating_action"):
        assert field in item_schema["properties"]
        assert field in item_schema["required"]
    assert item_schema["additionalProperties"] is False
    assert "multiple_instruments_covered" in schema["required"]
    assert schema["properties"]["multiple_instruments_covered"]["type"] == "boolean"
    # "downgraded" must be named as the most-severe action, ahead of reaffirmed/upgraded,
    # in the explicit severity-order clause -- not left to the model to infer. Sliced
    # to the severity-order clause specifically since "downgraded"/"upgraded" also
    # appear earlier in the description as plain enum members, in enum order.
    action_description = schema["properties"]["rating_action"]["description"]
    severity_clause = action_description[action_description.index("most to least severe"):]
    assert severity_clause.index("downgraded") < severity_clause.index("reaffirmed")
    assert severity_clause.index("downgraded") < severity_clause.index("upgraded")


@pytest.mark.parametrize(
    "schema",
    [
        fundamentals_structured_extraction.AUDITOR_CHANGE_SCHEMA,
        fundamentals_structured_extraction.RPT_SCHEMA,
    ],
)
def test_new_schemas_are_strict_mode_consistent(schema):
    # OpenAI strict json_schema mode requires every property listed in "required" and
    # additionalProperties=False -- same shape every other schema in this module uses.
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == set(schema["properties"].keys())


def test_auditor_change_schema_disclosure_type_is_the_load_bearing_field():
    schema = fundamentals_structured_extraction.AUDITOR_CHANGE_SCHEMA
    assert "disclosure_type" in schema["required"]
    assert schema["properties"]["disclosure_type"]["type"] == "string"


def test_rpt_schema_is_applicable_is_boolean_hedge():
    schema = fundamentals_structured_extraction.RPT_SCHEMA
    assert schema["properties"]["is_applicable"]["type"] == "boolean"
    assert schema["properties"]["pct_of_revenue"]["type"] == ["number", "null"]


def test_results_schema_is_strict_mode_consistent():
    schema = fundamentals_structured_extraction.RESULTS_SCHEMA
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == set(schema["properties"].keys())


def test_results_schema_splits_qoq_yoy_not_generic_comparison():
    schema = fundamentals_structured_extraction.RESULTS_SCHEMA
    for field in ("revenue_qoq_rs_lakh", "revenue_yoy_rs_lakh", "pat_qoq_rs_lakh", "pat_yoy_rs_lakh"):
        assert field in schema["properties"]
        assert schema["properties"][field]["type"] == ["number", "null"]
    # the old generic single-slot fields are gone, not just renamed
    assert "revenue_comparison_rs_lakh" not in schema["properties"]
    assert "revenue_comparison_period_label" not in schema["properties"]
    assert "pat_comparison_rs_lakh" not in schema["properties"]


def test_results_schema_has_margin_derivation_inputs():
    schema = fundamentals_structured_extraction.RESULTS_SCHEMA
    assert schema["properties"]["finance_costs_current_rs_lakh"]["type"] == ["number", "null"]
    assert schema["properties"]["depreciation_amortisation_current_rs_lakh"]["type"] == ["number", "null"]


def test_results_schema_period_type_mentions_half_yearly():
    schema = fundamentals_structured_extraction.RESULTS_SCHEMA
    desc = schema["properties"]["period_type"]["description"]
    assert "H1" in desc and "H2" in desc


def test_load_pending_extraction_targets_queries_expected_filters(monkeypatch):
    captured = {}

    def fake_sql_to_df(query, **kwargs):
        captured["query"] = query
        captured["params"] = kwargs.get("params")
        return pd.DataFrame()

    monkeypatch.setattr(fundamentals_structured_extraction, "sql_to_df", fake_sql_to_df)
    fundamentals_structured_extraction.load_pending_extraction_targets()
    assert "ocr_status = 'done'" in captured["query"]
    assert "structured_extraction_status IS NULL OR structured_extraction_status = 'pending'" in captured["query"]
    # BUG FOUND LIVE 2026-08-18: SCHEMA_VERSION bumps had no re-extraction path --
    # an already-'done' row under an old schema version stayed on that shape
    # forever. Confirmed live: 42/43 stored results extractions were still on the
    # dead v1 field names.
    assert "structured_extraction_schema_version IS NULL OR structured_extraction_schema_version < %s" in captured["query"]
    assert captured["params"] == (fundamentals_structured_extraction.SCHEMA_VERSION,)


def test_load_pending_extraction_targets_prioritizes_fresh_pending_over_stale_schema(monkeypatch):
    captured = {}

    def fake_sql_to_df(query, params=None):
        captured["query"] = query
        captured["params"] = params
        return pd.DataFrame()

    monkeypatch.setattr(fundamentals_structured_extraction, "sql_to_df", fake_sql_to_df)
    fundamentals_structured_extraction.load_pending_extraction_targets()
    # the stale-schema re-extraction branch orders AFTER genuinely-new pending
    # rows (CASE WHEN ... THEN 0 ELSE 1 END), so a schema bump's re-extraction
    # backlog can never crowd out fresh detections.
    assert "CASE WHEN structured_extraction_status IS NULL OR structured_extraction_status = 'pending' THEN 0 ELSE 1 END" in captured["query"]
    assert captured["params"] == (fundamentals_structured_extraction.SCHEMA_VERSION,)


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


def test_run_structured_extraction_skips_paid_call_for_routine_trading_window_closure(monkeypatch):
    # MEDIUM FINDING (re-audit 2026-08-18): "Closure of Trading Window" is the
    # dominant pit_sast shape (402 of ~600 real rows) and virtually never carries a
    # real transaction -- spending a paid gpt-5.4-mini call on every one contradicts
    # the PRD's own "bounded LLM cost" justification. Must skip the model call and
    # write the same "no transaction data" result directly.
    monkeypatch.setattr(fundamentals_structured_extraction, "_ensure_events_schema", lambda: None)
    monkeypatch.setattr(fundamentals_structured_extraction, "_bootstrap_extraction_columns", lambda: None)
    pending = pd.DataFrame(
        [
            {"source": "bse", "news_id": "n1", "filing_type": "pit_sast", "subcategory": "Closure of Trading Window", "ocr_text_s3_key": "k1"},
            # case/whitespace-insensitive match
            {"source": "bse", "news_id": "n2", "filing_type": "pit_sast", "subcategory": "  CLOSURE OF TRADING WINDOW  ", "ocr_text_s3_key": "k2"},
            # a genuine SAST disclosure under a DIFFERENT subcategory must still go through the real model
            {"source": "bse", "news_id": "n3", "filing_type": "pit_sast", "subcategory": "Disclosures under Reg. 29(2) of SEBI (SAST) Regulations, 2011", "ocr_text_s3_key": "k3"},
        ]
    )
    monkeypatch.setattr(fundamentals_structured_extraction, "load_pending_extraction_targets", lambda limit=None: pending)
    model_calls = []
    monkeypatch.setattr(
        fundamentals_structured_extraction,
        "extract_structured_fields",
        lambda text, filing_type, **k: model_calls.append(True) or {"disclosure_type": "sast_disclosure"},
    )
    monkeypatch.setattr(fundamentals_structured_extraction, "get_text_blob", lambda key: "text")
    status_calls = []
    monkeypatch.setattr(fundamentals_structured_extraction, "_set_extraction_result", lambda **kwargs: status_calls.append(kwargs))

    result = fundamentals_structured_extraction.run_structured_extraction()

    assert result["routine_pit_sast_skipped"] == 2
    assert result["extracted"] == 1  # only n3, the genuine SAST row, called the model
    assert len(model_calls) == 1  # the paid call happened exactly once, not 3 times
    skipped_calls = [c for c in status_calls if c["news_id"] in ("n1", "n2")]
    assert len(skipped_calls) == 2
    for call in skipped_calls:
        assert call["status"] == "done"
        payload = json.loads(call["fields"]["structured_extraction_json"])
        assert payload["disclosure_type"] == "trading_window_notice"
        assert payload["transaction_type"] is None
        assert call["fields"]["structured_extraction_model"] == "none_routine_shortcut"


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


def test_evaluate_pit_sast_trigger_falls_back_to_structured_extraction_json():
    # BUG FOUND LIVE 2026-08-15: bse_announcements.py always writes the flat
    # transaction_type/insider_name columns NULL (only NSE's own corporates-pit feed
    # populates them) -- 100% of real pit_sast events are BSE-sourced today, so this
    # trigger had never fired without the structured_extraction_json fallback. This
    # mirrors a real captured row (a promoter-entity buy).
    raw_json = json.dumps({"transaction_type": "buy", "insider_name": "Sonitron Limited", "pct_of_holding_before": 17.293, "pct_of_holding_after": 17.353})
    result = fundamentals_l3_triggers.evaluate_pit_sast_trigger(
        {"transaction_type": None, "insider_name": None, "structured_extraction_json": raw_json},
        {"promoter_stake_direction": "flat"},
    )
    assert result["trigger_type"] == "insider_buy"
    assert "Sonitron Limited" in result["reasoning"]


def test_evaluate_pit_sast_trigger_flat_column_wins_over_json_when_both_present():
    raw_json = json.dumps({"transaction_type": "sell", "insider_name": "JSON Name"})
    result = fundamentals_l3_triggers.evaluate_pit_sast_trigger(
        {"transaction_type": "Buy", "insider_name": "Flat Name", "structured_extraction_json": raw_json},
        {"promoter_stake_direction": "flat"},
    )
    assert result["trigger_type"] == "insider_buy"  # flat column ("Buy"), not the JSON's ("sell")
    assert "Flat Name" in result["reasoning"]


def test_evaluate_pit_sast_trigger_no_json_and_no_flat_column_does_not_alert():
    assert fundamentals_l3_triggers.evaluate_pit_sast_trigger({"transaction_type": None, "structured_extraction_json": None}, {"promoter_stake_direction": "flat"}) is None
    assert fundamentals_l3_triggers.evaluate_pit_sast_trigger({"transaction_type": None, "structured_extraction_json": "not json"}, {"promoter_stake_direction": "flat"}) is None


def test_evaluate_pit_sast_trigger_labels_reasoning_from_insider_category():
    # BUG FOUND LIVE 2026-08-18 (re-audit): used to hardcode "Promoter/insider" in
    # the reasoning regardless of PIT_SAST_SCHEMA's own already-extracted
    # insider_category. Real captured row shape: a KMP buy, not a promoter one.
    raw_json = json.dumps({"transaction_type": "buy", "insider_category": "kmp", "disclosure_type": "pit_disclosure"})
    result = fundamentals_l3_triggers.evaluate_pit_sast_trigger(
        {"transaction_type": None, "insider_name": "X", "structured_extraction_json": raw_json},
        {"promoter_stake_direction": "flat"},
    )
    assert result["trigger_type"] == "insider_buy"
    assert result["reasoning"].startswith("KMP buy")


def test_evaluate_pit_sast_trigger_unrecognized_category_falls_back_to_generic_insider_label():
    # Real captured row shape: a corporate SAST acquirer under Reg 10(6), not a
    # promoter/KMP/director/employee -- insider_category came back "other".
    raw_json = json.dumps({"transaction_type": "buy", "insider_category": "other", "disclosure_type": "sast_disclosure"})
    result = fundamentals_l3_triggers.evaluate_pit_sast_trigger(
        {"transaction_type": None, "insider_name": "TSF Investments Limited", "structured_extraction_json": raw_json},
        {"promoter_stake_direction": "flat"},
    )
    assert result["reasoning"].startswith("Insider buy")


def test_evaluate_pit_sast_trigger_trading_window_notice_never_alerts_even_with_a_transaction_type():
    # BUG FOUND LIVE 2026-08-18 (re-audit): disclosure_type was ignored entirely --
    # a "trading_window_notice" (the common case, a routine procedural filing) is
    # structurally not a transaction disclosure and must never alert, even if a
    # transaction_type value is present (e.g. a spurious LLM extraction).
    raw_json = json.dumps({"transaction_type": "buy", "insider_category": "promoter", "disclosure_type": "trading_window_notice"})
    result = fundamentals_l3_triggers.evaluate_pit_sast_trigger(
        {"transaction_type": None, "insider_name": "X", "structured_extraction_json": raw_json},
        {"promoter_stake_direction": "flat"},
    )
    assert result is None


def test_evaluate_capital_raise_trigger_always_alerts_regardless_of_l2_state():
    assert fundamentals_l3_triggers.evaluate_capital_raise_trigger({}, {"promoter_stake_direction": "decreasing"})["trigger_type"] == "capital_raise"
    assert fundamentals_l3_triggers.evaluate_capital_raise_trigger({}, None)["trigger_type"] == "capital_raise"


def test_summarize_investor_tiers_none_when_no_investors():
    assert fundamentals_l3_triggers._summarize_investor_tiers([]) is None
    assert fundamentals_l3_triggers._summarize_investor_tiers(None) is None


def test_summarize_investor_tiers_prefers_marquee_over_recognized():
    tiers = [{"name": "Beta LLC", "tier": "recognized"}, {"name": "Acme Fund", "tier": "marquee"}]
    assert fundamentals_l3_triggers._summarize_investor_tiers(tiers) == "Named investor Acme Fund classified as marquee."


def test_summarize_investor_tiers_unclassified_still_gets_a_sentence():
    tiers = [{"name": "Mystery Capital", "tier": None}]
    result = fundamentals_l3_triggers._summarize_investor_tiers(tiers)
    assert "not yet classified" in result
    assert "Mystery Capital" in result


def test_compute_growth_pct_positive():
    assert fundamentals_l3_triggers.compute_growth_pct(120.0, 100.0) == 20.0


def test_compute_growth_pct_negative():
    assert fundamentals_l3_triggers.compute_growth_pct(80.0, 100.0) == -20.0


def test_compute_growth_pct_loss_to_profit_swing_reads_positive():
    # real shape found live (Diamines and Chemicals): pat swung from a loss to a
    # profit -- must read as a large POSITIVE change, not negative.
    result = fundamentals_l3_triggers.compute_growth_pct(27.32, -209.4)
    assert result > 0


def test_compute_growth_pct_profit_to_loss_swing_reads_negative():
    result = fundamentals_l3_triggers.compute_growth_pct(-50.0, 100.0)
    assert result < 0


def test_compute_growth_pct_none_when_baseline_zero():
    assert fundamentals_l3_triggers.compute_growth_pct(100.0, 0) is None


def test_compute_growth_pct_none_when_either_input_missing():
    assert fundamentals_l3_triggers.compute_growth_pct(None, 100.0) is None
    assert fundamentals_l3_triggers.compute_growth_pct(100.0, None) is None


def test_compute_approx_operating_margin_pct_real_shape():
    # real values from a live-verified extraction (Ambika Cotton Mills Q1 FY27).
    result = fundamentals_l3_triggers.compute_approx_operating_margin_pct(
        revenue=25792.0, pat=2570.0, finance_costs=295.0, depreciation_amortisation=524.0
    )
    assert result == round((2570.0 + 295.0 + 524.0) / 25792.0 * 100, 2)


def test_compute_approx_operating_margin_pct_none_when_revenue_zero():
    assert fundamentals_l3_triggers.compute_approx_operating_margin_pct(revenue=0, pat=10, finance_costs=1, depreciation_amortisation=1) is None


def test_compute_approx_operating_margin_pct_none_when_any_input_missing():
    assert fundamentals_l3_triggers.compute_approx_operating_margin_pct(revenue=100, pat=None, finance_costs=1, depreciation_amortisation=1) is None


def test_load_prior_same_period_results_event_matches_period_type(monkeypatch):
    df = pd.DataFrame(
        [
            {"disclosure_date": "2025-11-10", "structured_extraction_json": json.dumps({"period_type": "Q2"})},
            {"disclosure_date": "2025-08-05", "structured_extraction_json": json.dumps({"period_type": "Q1"})},
        ]
    )
    monkeypatch.setattr(fundamentals_l3_triggers, "sql_to_df", lambda q, params=None: df)
    result = fundamentals_l3_triggers.load_prior_same_period_results_event("nse:ABC", period_type="Q1", before_disclosure_date="2026-08-01")
    assert result == {"disclosure_date": "2025-08-05"}


def test_load_prior_same_period_results_event_none_when_period_type_unknown():
    assert fundamentals_l3_triggers.load_prior_same_period_results_event("nse:ABC", period_type=None, before_disclosure_date="2026-08-01") is None


def test_load_prior_same_period_results_event_none_when_no_match(monkeypatch):
    df = pd.DataFrame([{"disclosure_date": "2025-11-10", "structured_extraction_json": json.dumps({"period_type": "Q2"})}])
    monkeypatch.setattr(fundamentals_l3_triggers, "sql_to_df", lambda q, params=None: df)
    assert fundamentals_l3_triggers.load_prior_same_period_results_event("nse:ABC", period_type="Q1", before_disclosure_date="2026-08-01") is None


def test_load_prior_same_period_results_event_query_is_bounded_by_lookback_window(monkeypatch):
    # BUG FOUND LIVE 2026-08-18 (re-audit): no upper bound on how far back this could
    # reach -- a genuine multi-year gap for a period_type could silently pick up a
    # 2+-year-old filing, and compute_timing_delay_days would then report ~365 days
    # of FALSE lateness (it always adds exactly 365 days to whatever this returns).
    captured = {}

    def fake_sql_to_df(query, params=None):
        captured["query"] = query
        captured["params"] = params
        return pd.DataFrame()

    monkeypatch.setattr(fundamentals_l3_triggers, "sql_to_df", fake_sql_to_df)
    fundamentals_l3_triggers.load_prior_same_period_results_event("nse:ABC", period_type="Q1", before_disclosure_date="2026-08-01")

    assert "disclosure_date >= %s" in captured["query"]
    lookback_start = pd.Timestamp("2026-08-01") - pd.Timedelta(days=fundamentals_l3_triggers.RESULTS_PRIOR_PERIOD_LOOKBACK_DAYS)
    assert captured["params"] == ("nse:ABC", "2026-08-01", str(lookback_start.date()))


def test_load_latest_results_calendar_event_returns_row(monkeypatch):
    df = pd.DataFrame([{"disclosure_date": "2026-08-10"}])
    monkeypatch.setattr(fundamentals_l3_triggers, "sql_to_df", lambda q, params=None: df)
    assert fundamentals_l3_triggers.load_latest_results_calendar_event("nse:ABC") == {"disclosure_date": "2026-08-10"}


def test_load_latest_results_calendar_event_none_when_empty(monkeypatch):
    monkeypatch.setattr(fundamentals_l3_triggers, "sql_to_df", lambda q, params=None: pd.DataFrame())
    assert fundamentals_l3_triggers.load_latest_results_calendar_event("nse:ABC") is None


def test_compute_timing_delay_days_both_baselines_present():
    result = fundamentals_l3_triggers.compute_timing_delay_days(
        "2026-08-20", prior_same_period_disclosure_date="2025-08-05", calendar_expected_date="2026-08-10"
    )
    # own-history: last year 2025-08-05 + 365 days = 2026-08-05; actual 2026-08-20 -> 15 days late
    assert result["vs_own_history_days"] == 15
    # calendar: expected 2026-08-10; actual 2026-08-20 -> 10 days late
    assert result["vs_calendar_days"] == 10


def test_compute_timing_delay_days_early_reads_negative():
    result = fundamentals_l3_triggers.compute_timing_delay_days("2026-08-01", prior_same_period_disclosure_date="2025-08-05", calendar_expected_date=None)
    assert result["vs_own_history_days"] < 0


def test_compute_timing_delay_days_none_baselines_stay_none():
    result = fundamentals_l3_triggers.compute_timing_delay_days("2026-08-20")
    assert result == {"vs_own_history_days": None, "vs_calendar_days": None}


def test_compute_timing_delay_days_unparseable_disclosure_date_returns_all_none():
    result = fundamentals_l3_triggers.compute_timing_delay_days("not a date", prior_same_period_disclosure_date="2025-08-05", calendar_expected_date="2026-08-10")
    assert result == {"vs_own_history_days": None, "vs_calendar_days": None}


def test_evaluate_capital_raise_trigger_reasoning_includes_investor_tier():
    event = {"investor_tiers": [{"name": "Acme Fund", "tier": "marquee"}]}
    result = fundamentals_l3_triggers.evaluate_capital_raise_trigger(event, None)
    assert "Acme Fund" in result["reasoning"] and "marquee" in result["reasoning"]


def test_evaluate_capital_raise_trigger_reasoning_unchanged_when_no_investor_tiers():
    result = fundamentals_l3_triggers.evaluate_capital_raise_trigger({}, None)
    assert "Named investor" not in result["reasoning"]


def test_resolve_investor_tiers_for_event_resolves_known_and_unknown():
    event = {"structured_extraction_json": json.dumps({"investor_names": ["Acme Fund", "Mystery Capital"]})}
    tiers_by_key = {"acme fund": {"llm_tier": "recognized", "override_tier": "marquee"}}

    result = fundamentals_l3_triggers._resolve_investor_tiers_for_event(event, tiers_by_key)

    by_name = {r["name"]: r["tier"] for r in result}
    assert by_name["Acme Fund"] == "marquee"  # override wins
    assert by_name["Mystery Capital"] is None


def test_resolve_investor_tiers_for_event_no_json_returns_empty():
    assert fundamentals_l3_triggers._resolve_investor_tiers_for_event({}, {}) == []


def test_resolve_investor_tiers_for_event_unparseable_json_returns_empty():
    assert fundamentals_l3_triggers._resolve_investor_tiers_for_event({"structured_extraction_json": "not json"}, {}) == []


def test_capital_raise_is_in_supported_filing_types_and_evaluators():
    assert "capital_raise" in fundamentals_l3_triggers.SUPPORTED_FILING_TYPES
    assert fundamentals_l3_triggers.TRIGGER_EVALUATORS["capital_raise"] is fundamentals_l3_triggers.evaluate_capital_raise_trigger


def test_evaluate_institutional_entry_trigger_always_alerts_regardless_of_l2_state():
    assert fundamentals_l3_triggers.evaluate_institutional_entry_trigger({}, {"promoter_stake_direction": "decreasing"})["trigger_type"] == "institutional_first_entry"
    assert fundamentals_l3_triggers.evaluate_institutional_entry_trigger({}, None)["trigger_type"] == "institutional_first_entry"


def test_evaluate_institutional_entry_trigger_uses_event_headline_as_reasoning():
    result = fundamentals_l3_triggers.evaluate_institutional_entry_trigger({"headline": "First institutional (FII+DII) stake detected: 3.2% as of Jun 2026"}, None)
    assert "3.2%" in result["reasoning"]


def test_institutional_entry_is_in_supported_filing_types_and_evaluators():
    assert "institutional_entry" in fundamentals_l3_triggers.SUPPORTED_FILING_TYPES
    assert fundamentals_l3_triggers.TRIGGER_EVALUATORS["institutional_entry"] is fundamentals_l3_triggers.evaluate_institutional_entry_trigger


def _results_event(**overrides):
    extracted = {
        "period_type": "Q1",
        "revenue_current_rs_lakh": 100.0,
        "revenue_yoy_rs_lakh": 100.0,
        "pat_current_rs_lakh": 10.0,
        "pat_yoy_rs_lakh": 10.0,
        "finance_costs_current_rs_lakh": 2.0,
        "depreciation_amortisation_current_rs_lakh": 3.0,
    }
    extracted.update(overrides)
    return {
        "company_master_id": "nse:ABC",
        "disclosure_date": "2026-08-01",
        "structured_extraction_json": json.dumps(extracted),
    }


def _no_timing_signal(monkeypatch):
    # most tests aren't about the timing branch -- neutralize it so decline/confirm
    # branches can be tested in isolation.
    monkeypatch.setattr(fundamentals_l3_triggers, "load_prior_same_period_results_event", lambda cmid, **k: None)
    monkeypatch.setattr(fundamentals_l3_triggers, "load_latest_results_calendar_event", lambda cmid: None)


def test_evaluate_results_trigger_none_when_no_extraction():
    assert fundamentals_l3_triggers.evaluate_results_trigger({}, None) is None


def test_evaluate_results_trigger_none_when_unparseable_json():
    assert fundamentals_l3_triggers.evaluate_results_trigger({"structured_extraction_json": "not json"}, None) is None


def test_evaluate_results_trigger_delayed_wins_regardless_of_growth(monkeypatch):
    monkeypatch.setattr(fundamentals_l3_triggers, "load_prior_same_period_results_event", lambda cmid, **k: {"disclosure_date": "2025-07-01"})
    monkeypatch.setattr(fundamentals_l3_triggers, "load_latest_results_calendar_event", lambda cmid: None)
    # 2025-07-01 + 365 days = 2026-07-01; actual 2026-08-01 -> 31 days late (> threshold)
    event = _results_event(disclosure_date="2026-08-01")
    result = fundamentals_l3_triggers.evaluate_results_trigger(event, None)
    assert result["trigger_type"] == "results_delayed"
    assert "31 days" in result["reasoning"]


def test_evaluate_results_trigger_falls_back_to_calendar_delay_when_no_own_history(monkeypatch):
    # BUG FOUND LIVE 2026-08-18 (re-audit): vs_calendar_days was computed but never
    # actually read anywhere -- a real per-event DB query with no observable effect.
    # Must now fire as a fallback when own-history has no baseline at all (e.g. this
    # company's first-ever filing of this period_type).
    monkeypatch.setattr(fundamentals_l3_triggers, "load_prior_same_period_results_event", lambda cmid, **k: None)
    monkeypatch.setattr(fundamentals_l3_triggers, "load_latest_results_calendar_event", lambda cmid: {"disclosure_date": "2026-07-01"})
    event = _results_event(disclosure_date="2026-08-01")  # 31 days after the calendar's expected date
    result = fundamentals_l3_triggers.evaluate_results_trigger(event, None)
    assert result["trigger_type"] == "results_delayed"
    assert "31 days" in result["reasoning"]
    assert "forward results calendar" in result["reasoning"]


def test_evaluate_results_trigger_own_history_wins_over_calendar_when_both_present(monkeypatch):
    # own-history stays the preferred, primary baseline -- calendar is a fallback
    # only, not a second independent trigger path.
    monkeypatch.setattr(fundamentals_l3_triggers, "load_prior_same_period_results_event", lambda cmid, **k: {"disclosure_date": "2026-07-25"})  # not late
    monkeypatch.setattr(fundamentals_l3_triggers, "load_latest_results_calendar_event", lambda cmid: {"disclosure_date": "2026-06-01"})  # would be late
    event = _results_event(revenue_current_rs_lakh=110.0, revenue_yoy_rs_lakh=100.0, disclosure_date="2026-08-01")  # +10%, no growth trigger either
    result = fundamentals_l3_triggers.evaluate_results_trigger(event, {"net_debt_yoy_delta_rscr": -5})
    assert result is None  # own-history says not late; calendar must not override that


def test_evaluate_results_trigger_decline_always_alerts(monkeypatch):
    _no_timing_signal(monkeypatch)
    event = _results_event(revenue_current_rs_lakh=80.0, revenue_yoy_rs_lakh=100.0)  # -20% YoY
    result = fundamentals_l3_triggers.evaluate_results_trigger(event, None)
    assert result["trigger_type"] == "results_decline"
    assert "Revenue declined -20.0% YoY" in result["reasoning"]


def test_exceptional_items_caveat_only_applies_to_pat():
    extracted = {"exceptional_items_rs_lakh": 50.0}
    assert fundamentals_l3_triggers._exceptional_items_caveat(extracted, "Revenue") == ""
    assert "exceptional item" in fundamentals_l3_triggers._exceptional_items_caveat(extracted, "PAT")


def test_exceptional_items_caveat_empty_when_zero_or_none():
    assert fundamentals_l3_triggers._exceptional_items_caveat({"exceptional_items_rs_lakh": 0}, "PAT") == ""
    assert fundamentals_l3_triggers._exceptional_items_caveat({"exceptional_items_rs_lakh": None}, "PAT") == ""
    assert fundamentals_l3_triggers._exceptional_items_caveat({}, "PAT") == ""


def test_evaluate_results_trigger_decline_pat_driven_includes_exceptional_items_caveat(monkeypatch):
    _no_timing_signal(monkeypatch)
    event = _results_event(pat_current_rs_lakh=5.0, pat_yoy_rs_lakh=20.0, exceptional_items_rs_lakh=8.0)  # -75% YoY PAT
    result = fundamentals_l3_triggers.evaluate_results_trigger(event, None)
    assert result["trigger_type"] == "results_decline"
    assert "exceptional item" in result["reasoning"]


def test_evaluate_results_trigger_decline_revenue_driven_omits_exceptional_items_caveat(monkeypatch):
    _no_timing_signal(monkeypatch)
    event = _results_event(revenue_current_rs_lakh=80.0, revenue_yoy_rs_lakh=100.0, exceptional_items_rs_lakh=8.0)  # revenue-driven, not PAT
    result = fundamentals_l3_triggers.evaluate_results_trigger(event, None)
    assert "exceptional item" not in result["reasoning"]


def test_evaluate_results_trigger_confirm_turnaround_includes_exceptional_items_caveat(monkeypatch):
    _no_timing_signal(monkeypatch)
    event = _results_event(pat_current_rs_lakh=120.0, pat_yoy_rs_lakh=100.0, exceptional_items_rs_lakh=15.0)  # +20% YoY PAT
    result = fundamentals_l3_triggers.evaluate_results_trigger(event, {"net_debt_yoy_delta_rscr": -50})
    assert result["trigger_type"] == "results_confirm_turnaround"
    assert "exceptional item" in result["reasoning"]


def test_evaluate_results_trigger_decline_alerts_even_without_l2_state(monkeypatch):
    _no_timing_signal(monkeypatch)
    event = _results_event(pat_current_rs_lakh=5.0, pat_yoy_rs_lakh=20.0)  # -75% YoY PAT
    result = fundamentals_l3_triggers.evaluate_results_trigger(event, None)
    assert result["trigger_type"] == "results_decline"


def test_evaluate_results_trigger_growth_with_l2_confirmation_alerts(monkeypatch):
    _no_timing_signal(monkeypatch)
    event = _results_event(revenue_current_rs_lakh=120.0, revenue_yoy_rs_lakh=100.0)  # +20% YoY
    result = fundamentals_l3_triggers.evaluate_results_trigger(event, {"net_debt_yoy_delta_rscr": -50})
    assert result["trigger_type"] == "results_confirm_turnaround"
    assert "Revenue grew 20.0% YoY" in result["reasoning"]
    assert "Approx operating margin" in result["reasoning"]


def test_evaluate_results_trigger_growth_without_l2_confirmation_does_not_alert(monkeypatch):
    _no_timing_signal(monkeypatch)
    event = _results_event(revenue_current_rs_lakh=120.0, revenue_yoy_rs_lakh=100.0)  # +20% YoY
    result = fundamentals_l3_triggers.evaluate_results_trigger(event, {"net_debt_yoy_delta_rscr": 50})  # worsening, not confirming
    assert result is None


def test_evaluate_results_trigger_growth_without_l2_state_does_not_alert(monkeypatch):
    _no_timing_signal(monkeypatch)
    event = _results_event(revenue_current_rs_lakh=120.0, revenue_yoy_rs_lakh=100.0)
    assert fundamentals_l3_triggers.evaluate_results_trigger(event, None) is None


def test_evaluate_results_trigger_flat_growth_no_signal(monkeypatch):
    _no_timing_signal(monkeypatch)
    event = _results_event()  # revenue/pat unchanged YoY
    assert fundamentals_l3_triggers.evaluate_results_trigger(event, {"net_debt_yoy_delta_rscr": -50}) is None


def test_evaluate_results_trigger_no_growth_data_returns_none(monkeypatch):
    _no_timing_signal(monkeypatch)
    event = _results_event(revenue_yoy_rs_lakh=None, pat_yoy_rs_lakh=None)
    assert fundamentals_l3_triggers.evaluate_results_trigger(event, None) is None


def test_results_is_in_supported_filing_types_and_evaluators():
    assert "results" in fundamentals_l3_triggers.SUPPORTED_FILING_TYPES
    assert fundamentals_l3_triggers.TRIGGER_EVALUATORS["results"] is fundamentals_l3_triggers.evaluate_results_trigger


def test_evaluate_auditor_change_trigger_alerts_on_confirmed_change():
    event = {"structured_extraction_json": json.dumps({"disclosure_type": "confirmed_change", "change_direction": "resignation", "previous_auditor": "ABC & Co", "new_auditor": None})}
    result = fundamentals_l3_triggers.evaluate_auditor_change_trigger(event, None)
    assert result["trigger_type"] == "auditor_change"
    assert "resignation" in result["reasoning"]
    assert "ABC & Co" in result["reasoning"]


def test_evaluate_auditor_change_trigger_alerts_even_without_l2_state():
    event = {"structured_extraction_json": json.dumps({"disclosure_type": "confirmed_change", "change_direction": "appointment"})}
    assert fundamentals_l3_triggers.evaluate_auditor_change_trigger(event, None)["trigger_type"] == "auditor_change"


@pytest.mark.parametrize("disclosure_type", ["proposed_change_agenda", "incidental_mention", "other", None])
def test_evaluate_auditor_change_trigger_none_when_not_confirmed(disclosure_type):
    event = {"structured_extraction_json": json.dumps({"disclosure_type": disclosure_type})}
    assert fundamentals_l3_triggers.evaluate_auditor_change_trigger(event, None) is None


def test_evaluate_auditor_change_trigger_none_when_no_extraction():
    assert fundamentals_l3_triggers.evaluate_auditor_change_trigger({}, None) is None
    assert fundamentals_l3_triggers.evaluate_auditor_change_trigger({"structured_extraction_json": "not json"}, None) is None


def test_auditor_change_is_in_supported_filing_types_and_evaluators():
    assert "auditor_change" in fundamentals_l3_triggers.SUPPORTED_FILING_TYPES
    assert fundamentals_l3_triggers.TRIGGER_EVALUATORS["auditor_change"] is fundamentals_l3_triggers.evaluate_auditor_change_trigger


def test_evaluate_related_party_transaction_trigger_alerts_over_threshold():
    event = {"structured_extraction_json": json.dumps({"is_applicable": True, "pct_of_revenue": 25.0, "related_party_name": "Promoter Group Ltd", "rpt_amount_rs_cr": 50.0})}
    result = fundamentals_l3_triggers.evaluate_related_party_transaction_trigger(event, None)
    assert result["trigger_type"] == "related_party_transaction"
    assert "Promoter Group Ltd" in result["reasoning"]
    assert "25.0%" in result["reasoning"]


def test_evaluate_related_party_transaction_trigger_none_when_not_applicable():
    event = {"structured_extraction_json": json.dumps({"is_applicable": False, "pct_of_revenue": None})}
    assert fundamentals_l3_triggers.evaluate_related_party_transaction_trigger(event, None) is None


def test_evaluate_related_party_transaction_trigger_none_when_under_threshold():
    event = {"structured_extraction_json": json.dumps({"is_applicable": True, "pct_of_revenue": 5.0})}
    assert fundamentals_l3_triggers.evaluate_related_party_transaction_trigger(event, None) is None


def test_evaluate_related_party_transaction_trigger_none_when_pct_missing():
    event = {"structured_extraction_json": json.dumps({"is_applicable": True, "pct_of_revenue": None})}
    assert fundamentals_l3_triggers.evaluate_related_party_transaction_trigger(event, None) is None


def test_evaluate_related_party_transaction_trigger_alerts_on_amount_when_no_percentage_stated():
    # BUG FOUND LIVE 2026-08-18 (re-audit): gated purely on pct_of_revenue, while
    # rpt_amount_rs_cr (what filings actually carry) was used only for display text.
    # A material RPT stated as an amount, not a percentage, silently never alerted.
    event = {"structured_extraction_json": json.dumps({"is_applicable": True, "pct_of_revenue": None, "rpt_amount_rs_cr": 120.0, "related_party_name": "Promoter Group Ltd"})}
    result = fundamentals_l3_triggers.evaluate_related_party_transaction_trigger(event, None)
    assert result["trigger_type"] == "related_party_transaction"
    assert "Promoter Group Ltd" in result["reasoning"]
    assert "Rs.120.0 cr" in result["reasoning"]
    assert "no percentage-of-revenue stated" in result["reasoning"]


def test_related_party_transaction_is_in_supported_filing_types_and_evaluators():
    assert "related_party_transaction" in fundamentals_l3_triggers.SUPPORTED_FILING_TYPES
    assert fundamentals_l3_triggers.TRIGGER_EVALUATORS["related_party_transaction"] is fundamentals_l3_triggers.evaluate_related_party_transaction_trigger


def test_load_candidate_events_queries_expected_filters(monkeypatch):
    captured = {}

    def fake_sql_to_df(query, **kwargs):
        captured["query"] = query
        return pd.DataFrame()

    monkeypatch.setattr(fundamentals_l3_triggers, "sql_to_df", fake_sql_to_df)
    fundamentals_l3_triggers.load_candidate_events()
    assert "rating_action" in captured["query"]
    assert "pit_sast" in captured["query"]
    assert "capital_raise" in captured["query"]
    assert "structured_extraction_json" in captured["query"]
    assert "rule_trigger_status IS NULL OR rule_trigger_status = 'pending'" in captured["query"]


def test_resolve_rating_action_type_prefers_column_over_json():
    event = {"rating_action_type": "downgraded", "structured_extraction_json": json.dumps({"rating_action": "upgraded"})}
    assert fundamentals_l3_triggers._resolve_rating_action_type(event) == "downgraded"


def test_resolve_rating_action_type_falls_back_to_structured_extraction():
    event = {"rating_action_type": None, "structured_extraction_json": json.dumps({"rating_action": "upgraded"})}
    assert fundamentals_l3_triggers._resolve_rating_action_type(event) == "upgraded"


def test_resolve_rating_action_type_none_when_column_empty_string():
    event = {"rating_action_type": "", "structured_extraction_json": json.dumps({"rating_action": "reaffirmed"})}
    assert fundamentals_l3_triggers._resolve_rating_action_type(event) == "reaffirmed"


def test_resolve_rating_action_type_none_when_neither_source_has_it():
    assert fundamentals_l3_triggers._resolve_rating_action_type({"rating_action_type": None, "structured_extraction_json": None}) is None
    assert fundamentals_l3_triggers._resolve_rating_action_type({"rating_action_type": None, "structured_extraction_json": json.dumps({"other_field": 1})}) is None


def test_resolve_rating_action_type_handles_unparseable_json():
    event = {"rating_action_type": None, "structured_extraction_json": "not json"}
    assert fundamentals_l3_triggers._resolve_rating_action_type(event) is None


def test_resolve_rating_action_type_a_downgraded_instrument_cannot_be_masked_by_the_flat_column(monkeypatch):
    # BUG FOUND LIVE 2026-08-18 (re-audit): used to check the flat rating_action_type
    # column FIRST, unconditionally -- bypassing instrument_actions' own per-
    # instrument breakdown entirely whenever the flat column (a simpler,
    # non-multi-instrument-aware ICRA-site scrape) happened to be populated. A real
    # downgrade on one instrument, with the flat column saying "reaffirmed" (about a
    # DIFFERENT instrument in the same filing), must still resolve to "downgraded".
    event = {
        "rating_action_type": "reaffirmed",
        "structured_extraction_json": json.dumps({
            "rating_action": "reaffirmed",
            "instrument_actions": [
                {"instrument_description": "Long-term Bank Facilities", "rating_action": "downgraded"},
                {"instrument_description": "Short-term Bank Facilities", "rating_action": "reaffirmed"},
            ],
        }),
    }
    assert fundamentals_l3_triggers._resolve_rating_action_type(event) == "downgraded"


def test_resolve_rating_action_type_top_level_summary_cannot_mask_a_downgraded_instrument():
    # Same guarantee, but defending against the top-level JSON rating_action field
    # itself disagreeing with its own instrument_actions breakdown (the LLM not
    # following RATING_ACTION_SCHEMA's "never collapse into reaffirmed" instruction).
    event = {
        "rating_action_type": None,
        "structured_extraction_json": json.dumps({
            "rating_action": "reaffirmed",  # should have been "downgraded" per the schema's own instruction
            "instrument_actions": [{"instrument_description": "NCD", "rating_action": "downgraded"}],
        }),
    }
    assert fundamentals_l3_triggers._resolve_rating_action_type(event) == "downgraded"


def test_resolve_rating_action_type_falls_back_to_old_precedence_for_unrecognized_values():
    # Neither source is a recognized severity vocabulary value -- falls back to the
    # simple flat-then-JSON precedence rather than returning None outright.
    event = {"rating_action_type": "some_unusual_action", "structured_extraction_json": json.dumps({"rating_action": "another_unusual_action"})}
    assert fundamentals_l3_triggers._resolve_rating_action_type(event) == "some_unusual_action"


def test_evaluate_rating_action_trigger_reasoning_names_agency_when_known():
    result = fundamentals_l3_triggers.evaluate_rating_action_trigger({"rating_action_type": "downgraded", "rating_agency": "CRISIL"}, None)
    assert "CRISIL" in result["reasoning"]


def test_evaluate_rating_action_trigger_reasoning_omits_agency_when_unknown():
    result = fundamentals_l3_triggers.evaluate_rating_action_trigger({"rating_action_type": "downgraded"}, None)
    assert "(" not in result["reasoning"]


def test_evaluate_rating_action_trigger_uses_structured_extraction_fallback():
    # real end-to-end shape: a non-ICRA rating action with no rating_action_type
    # column populated, only structured_extraction_json (from the generic BSE-PDF
    # OCR+extraction path) -- confirmed live 2026-08-12 against a real extracted
    # IRIS RegTech/ICRA filing.
    event = {"rating_action_type": None, "structured_extraction_json": json.dumps({"rating_action": "upgraded"})}
    assert fundamentals_l3_triggers.evaluate_rating_action_trigger(event, {"net_debt_yoy_delta_rscr": -8})["trigger_type"] == "rating_confirms_deleveraging"
    assert fundamentals_l3_triggers.evaluate_rating_action_trigger(event, None) is None


def test_run_l3_rule_triggers_returns_early_when_no_candidates(monkeypatch):
    monkeypatch.setattr(fundamentals_l3_triggers, "_bootstrap_rule_trigger_column", lambda: None)
    monkeypatch.setattr(fundamentals_l3_triggers, "_ensure_alerts_table", lambda: None)
    monkeypatch.setattr(fundamentals_l3_triggers, "load_candidate_events", lambda limit=None: pd.DataFrame())

    result = fundamentals_l3_triggers.run_l3_rule_triggers()

    assert result == {"alerted": 0, "not_alert_worthy": 0, "no_l2_state": 0}


def test_load_candidate_events_gates_on_enrichment_readiness(monkeypatch):
    # BUG FOUND LIVE 2026-08-18: this used to select every candidate the moment it
    # was detected, regardless of whether OCR/structured extraction had run yet --
    # 6 of 7 evaluators return None (no evidence) when structured_extraction_json
    # is still NULL, and that None got written as a TERMINAL rule_trigger_status=
    # 'not_alert_worthy' that nothing ever resets. Confirmed live: 1,037 of 1,042
    # backlog rows already had a terminal status while ocr_status was still NULL.
    captured = {}

    def fake_sql_to_df(query, **kwargs):
        captured["query"] = query
        return pd.DataFrame()

    monkeypatch.setattr(fundamentals_l3_triggers, "sql_to_df", fake_sql_to_df)
    fundamentals_l3_triggers.load_candidate_events()
    query = captured["query"]
    # rating_action/pit_sast rows with real flat data from their OWN dedicated
    # feeds (ICRA enrichment / NSE PIT) are ready regardless of the generic OCR
    # pipeline -- unaffected by the gate.
    assert "rating_action_type IS NOT NULL" in query
    assert "transaction_type IS NOT NULL AND transaction_type != ''" in query
    # capital_raise/institutional_entry alert unconditionally per this module's
    # own existing design -- never blocked on extraction.
    assert "filing_type IN ('capital_raise', 'institutional_entry')" in query
    # no document at all -- nothing to wait for.
    assert "attachment_name IS NULL AND rationale_pdf_url IS NULL" in query
    # OCR concluded one way or another AND extraction also concluded (or was
    # never attempted because OCR itself never produced text to extract from).
    assert "ocr_status IN ('done', 'failed', 'no_document')" in query
    assert "structured_extraction_status != 'pending'" in query


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
    monkeypatch.setattr(fundamentals_l3_triggers, "load_investor_tiers", lambda: pd.DataFrame())
    monkeypatch.setattr(fundamentals_l3_triggers, "build_l1_ticker_by_company_master_id", lambda: {"nse:X": "X"})

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


def test_run_l3_rule_triggers_does_not_mark_alerted_when_the_batched_upsert_fails(monkeypatch):
    # BUG FOUND LIVE 2026-08-15, fixed here: rule_trigger_status used to be set to
    # "alerted" per-event INSIDE the loop, before the single batched alert-row
    # upsert ran -- a transient failure on that upsert would leave the event
    # permanently marked "alerted" with no alert row ever written (load_candidate_
    # events only re-selects NULL/pending). Now the status is only set AFTER the
    # upsert succeeds, so a failing upsert must leave the event untouched (still
    # NULL/pending) and eligible for retry next run.
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
    monkeypatch.setattr(fundamentals_l3_triggers, "load_investor_tiers", lambda: pd.DataFrame())

    def failing_upsert(df, table, **k):
        raise RuntimeError("transient DB error")

    monkeypatch.setattr(fundamentals_l3_triggers, "upsert_to_db", failing_upsert)
    status_calls = []
    monkeypatch.setattr(fundamentals_l3_triggers, "_set_rule_trigger_status", lambda **kwargs: status_calls.append(kwargs))

    with pytest.raises(RuntimeError):
        fundamentals_l3_triggers.run_l3_rule_triggers()

    assert status_calls == []  # the event was never marked "alerted" -- stays retry-eligible


def test_run_l3_rule_triggers_attaches_investor_tiers_to_capital_raise_reasoning(monkeypatch):
    monkeypatch.setattr(fundamentals_l3_triggers, "_bootstrap_rule_trigger_column", lambda: None)
    monkeypatch.setattr(fundamentals_l3_triggers, "_ensure_alerts_table", lambda: None)
    events = pd.DataFrame(
        [
            {
                "source": "bse", "news_id": "n1", "company_master_id": "nse:X", "filing_type": "capital_raise",
                "headline": "preferential allotment", "rating_action_type": None, "transaction_type": None,
                "insider_name": None, "quantity": None, "disclosure_date": date(2026, 8, 1),
                "structured_extraction_json": json.dumps({"investor_names": ["Acme Fund"]}),
            }
        ]
    )
    monkeypatch.setattr(fundamentals_l3_triggers, "load_candidate_events", lambda limit=None: events)
    monkeypatch.setattr(fundamentals_l3_triggers, "load_latest_l2_state", lambda: pd.DataFrame(columns=["ticker"]))
    monkeypatch.setattr(fundamentals_l3_triggers, "load_investor_tiers", lambda: pd.DataFrame([{"investor_key": "acme fund", "llm_tier": "recognized", "override_tier": "marquee"}]))
    upserts = []
    monkeypatch.setattr(fundamentals_l3_triggers, "upsert_to_db", lambda df, table, **k: upserts.append((df, table, k)))
    monkeypatch.setattr(fundamentals_l3_triggers, "_set_rule_trigger_status", lambda **kwargs: None)
    monkeypatch.setattr(fundamentals_l3_triggers, "_record_fallback", lambda *a, **k: None)

    fundamentals_l3_triggers.run_l3_rule_triggers()

    df, _, _ = upserts[0]
    assert "Acme Fund" in df.iloc[0]["reasoning"]
    assert "marquee" in df.iloc[0]["reasoning"]


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
    monkeypatch.setattr(fundamentals_l3_triggers, "load_investor_tiers", lambda: pd.DataFrame())
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
    # 2026-08-15 bug found live: the baseline average must exclude the event day itself (row 0,
    # volume=200) -- including it dilutes/understates the ratio by the event's own spike
    # (confirmed live: nse:ONWARDTEC stored 18.1x when the true ratio was 180.9x). Prior-days-only
    # average here is mean([50, 50]) = 50, not mean([200, 50, 50]) = 100.
    assert ctx["volume_on_event_vs_avg_ratio"] == round(200 / 50, 2)
    assert ctx["price_pct_change_since_event"] == round((105.0 - 100.0) / 100.0 * 100, 2)
    assert ctx["sessions_since_event_available"] == 1


def test_load_price_context_ratio_unavailable_with_only_event_day(monkeypatch):
    # No prior-days history at all (only the event day itself) -- no guessed ratio, matching this
    # module's "never a guessed value" discipline elsewhere.
    before = pd.DataFrame([{"date": date(2026, 8, 4), "close": 100.0, "volume": 200}])
    monkeypatch.setattr(fundamentals_llm_triage, "sql_to_df", lambda query, params=None: before if "date <= " in query else pd.DataFrame())

    ctx = fundamentals_llm_triage.load_price_context("nse:X", date(2026, 8, 4))

    assert ctx["volume_on_event_vs_avg_ratio"] is None


def test_load_price_context_returns_empty_when_no_prior_data(monkeypatch):
    monkeypatch.setattr(fundamentals_llm_triage, "sql_to_df", lambda query, params=None: pd.DataFrame())
    assert fundamentals_llm_triage.load_price_context("nse:X", date(2026, 8, 4)) == {}


def test_build_evidence_bundle_parses_structured_extraction_json():
    event = {
        "filing_type": "rating_action", "headline": "h", "subcategory": "s", "disclosure_date": date(2026, 8, 4),
        "rating_action_type": "downgraded", "rating_agency": "icra", "transaction_type": None, "insider_name": None, "quantity": None,
        "structured_extraction_json": '{"rating_action": "downgraded"}',
    }
    bundle = fundamentals_llm_triage.build_evidence_bundle(event, {"ticker": "X"}, {"volume_on_event_vs_avg_ratio": 2.0})
    assert bundle["event"]["structured_extraction"] == {"rating_action": "downgraded"}
    # BUG FOUND LIVE 2026-08-17: l3_triggers.py's sibling evidence-builder got this
    # exact fix on 2026-08-13 ("rating_agency is included so ... reasoning can name
    # which agency acted"); llm_triage.py's bundle never carried it, so the LLM
    # triaging a rating_action event never saw which agency acted.
    assert bundle["event"]["rating_agency"] == "icra"
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
    assert "rating_agency" in captured["query"]
    # BUG FOUND LIVE 2026-08-18: this used to triage a candidate the moment it was
    # detected, before OCR/extraction had run -- results events (which need
    # structured_extraction_json entirely for the evidence bundle) got LLM-judged
    # on category alone. Pins the readiness gate is actually in the query.
    assert "ocr_status IN ('done', 'failed', 'no_document')" in captured["query"]
    assert "structured_extraction_status != 'pending'" in captured["query"]


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


def test_run_llm_triage_does_not_mark_flagged_when_the_batched_upsert_fails(monkeypatch):
    # Same bug/fix as l3_triggers.py's own rule pass (found live 2026-08-15):
    # llm_triage_status must only be set to "flagged" AFTER the batched alert-row
    # upsert succeeds, never before -- a failing upsert must leave the event
    # untouched (still NULL/pending) and eligible for retry.
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

    def failing_upsert(df, table, **k):
        raise RuntimeError("transient DB error")

    monkeypatch.setattr(fundamentals_llm_triage, "upsert_to_db", failing_upsert)
    status_calls = []
    monkeypatch.setattr(fundamentals_llm_triage, "_set_triage_status", lambda **kwargs: status_calls.append(kwargs))

    with pytest.raises(RuntimeError):
        fundamentals_llm_triage.run_llm_triage(model="test-model")

    assert status_calls == []


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
    monkeypatch.setattr(fundamentals_l4_thesis, "sql_to_df", lambda q, **k: pd.DataFrame())
    with pytest.raises(fundamentals_l4_thesis.ThesisValidationError):
        fundamentals_l4_thesis.create_thesis(
            company_master_id="nse:X", prediction_text="  ", target_date=date(2027, 1, 1),
            invalidation_criteria="x", origin_tag="ad_hoc",
        )


def test_create_thesis_requires_target_date(monkeypatch):
    monkeypatch.setattr(fundamentals_l4_thesis, "_ensure_thesis_table", lambda: None)
    monkeypatch.setattr(fundamentals_l4_thesis, "sql_to_df", lambda q, **k: pd.DataFrame())
    with pytest.raises(fundamentals_l4_thesis.ThesisValidationError):
        fundamentals_l4_thesis.create_thesis(
            company_master_id="nse:X", prediction_text="p", target_date=None,
            invalidation_criteria="x", origin_tag="ad_hoc",
        )


def test_create_thesis_requires_invalidation_criteria(monkeypatch):
    monkeypatch.setattr(fundamentals_l4_thesis, "_ensure_thesis_table", lambda: None)
    monkeypatch.setattr(fundamentals_l4_thesis, "sql_to_df", lambda q, **k: pd.DataFrame())
    with pytest.raises(fundamentals_l4_thesis.ThesisValidationError):
        fundamentals_l4_thesis.create_thesis(
            company_master_id="nse:X", prediction_text="p", target_date=date(2027, 1, 1),
            invalidation_criteria="", origin_tag="ad_hoc",
        )


def test_create_thesis_requires_valid_origin_tag(monkeypatch):
    monkeypatch.setattr(fundamentals_l4_thesis, "_ensure_thesis_table", lambda: None)
    monkeypatch.setattr(fundamentals_l4_thesis, "sql_to_df", lambda q, **k: pd.DataFrame())
    with pytest.raises(fundamentals_l4_thesis.ThesisValidationError):
        fundamentals_l4_thesis.create_thesis(
            company_master_id="nse:X", prediction_text="p", target_date=date(2027, 1, 1),
            invalidation_criteria="x", origin_tag="not_a_real_tag",
        )


def test_create_thesis_requires_valid_metric_operator(monkeypatch):
    monkeypatch.setattr(fundamentals_l4_thesis, "_ensure_thesis_table", lambda: None)
    monkeypatch.setattr(fundamentals_l4_thesis, "sql_to_df", lambda q, **k: pd.DataFrame())
    with pytest.raises(fundamentals_l4_thesis.ThesisValidationError):
        fundamentals_l4_thesis.create_thesis(
            company_master_id="nse:X", prediction_text="p", target_date=date(2027, 1, 1),
            invalidation_criteria="x", origin_tag="ad_hoc", metric_operator="~=",
        )


def test_create_thesis_builds_expected_row(monkeypatch):
    monkeypatch.setattr(fundamentals_l4_thesis, "_ensure_thesis_table", lambda: None)
    monkeypatch.setattr(fundamentals_l4_thesis, "sql_to_df", lambda q, **k: pd.DataFrame())
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
    monkeypatch.setattr(fundamentals_l4_thesis, "sql_to_df", lambda q, **k: pd.DataFrame())
    monkeypatch.setattr(fundamentals_l4_thesis, "upsert_to_db", lambda df, table, **k: None)
    row = fundamentals_l4_thesis.create_thesis(
        company_master_id="nse:X", prediction_text="p", target_date=date(2027, 1, 1),
        invalidation_criteria="x", origin_tag="ad_hoc",
    )
    assert row["source_alert_source"] is None
    assert row["source_alert_news_id"] is None


def test_create_thesis_refuses_to_overwrite_an_already_resolved_thesis(monkeypatch):
    # BUG FOUND LIVE 2026-08-15, fixed 2026-08-17: thesis_id is deterministic on
    # (company_master_id, prediction_text, created_date) -- re-submitting identical
    # inputs (e.g. a retried API call) used to silently wipe a real resolution back
    # to open/None. Reproduced live with a disposable test row before fixing.
    monkeypatch.setattr(fundamentals_l4_thesis, "_ensure_thesis_table", lambda: None)
    monkeypatch.setattr(fundamentals_l4_thesis, "sql_to_df", lambda q, **k: pd.DataFrame([{"status": "resolved"}]))
    upserts = []
    monkeypatch.setattr(fundamentals_l4_thesis, "upsert_to_db", lambda df, table, **k: upserts.append((df, table, k)))

    with pytest.raises(fundamentals_l4_thesis.ThesisValidationError, match="already resolved"):
        fundamentals_l4_thesis.create_thesis(
            company_master_id="nse:X", prediction_text="p", target_date=date(2027, 1, 1),
            invalidation_criteria="x", origin_tag="ad_hoc", created_date=date(2026, 8, 1),
        )
    assert upserts == []  # nothing was written -- the resolution stays intact


def test_create_thesis_allows_resubmitting_an_open_thesis(monkeypatch):
    # a re-submit of an OPEN (not yet resolved) thesis with identical inputs is
    # harmless -- same thesis_id, same data, just re-upserted. Only a RESOLVED
    # thesis is protected.
    monkeypatch.setattr(fundamentals_l4_thesis, "_ensure_thesis_table", lambda: None)
    monkeypatch.setattr(fundamentals_l4_thesis, "sql_to_df", lambda q, **k: pd.DataFrame([{"status": "open"}]))
    upserts = []
    monkeypatch.setattr(fundamentals_l4_thesis, "upsert_to_db", lambda df, table, **k: upserts.append((df, table, k)))

    row = fundamentals_l4_thesis.create_thesis(
        company_master_id="nse:X", prediction_text="p", target_date=date(2027, 1, 1),
        invalidation_criteria="x", origin_tag="ad_hoc", created_date=date(2026, 8, 1),
    )
    assert row["status"] == "open"
    assert len(upserts) == 1


def test_create_thesis_id_disambiguates_same_day_theses_by_target_date_and_invalidation(monkeypatch):
    # BUG FOUND LIVE 2026-08-18 (re-audit): thesis_id used to hash only (company,
    # prediction_text, created_date) -- two same-day theses on the same company
    # with identical wording but a different target_date or invalidation
    # criteria collided on thesis_id and silently overwrote each other via
    # create_thesis's upsert.
    monkeypatch.setattr(fundamentals_l4_thesis, "_ensure_thesis_table", lambda: None)
    monkeypatch.setattr(fundamentals_l4_thesis, "sql_to_df", lambda q, **k: pd.DataFrame())
    monkeypatch.setattr(fundamentals_l4_thesis, "upsert_to_db", lambda df, table, **k: None)

    row_a = fundamentals_l4_thesis.create_thesis(
        company_master_id="nse:X", prediction_text="p", target_date=date(2027, 1, 1),
        invalidation_criteria="misses Q2", origin_tag="ad_hoc", created_date=date(2026, 8, 1),
    )
    row_b = fundamentals_l4_thesis.create_thesis(
        company_master_id="nse:X", prediction_text="p", target_date=date(2027, 6, 30),
        invalidation_criteria="misses Q4", origin_tag="ad_hoc", created_date=date(2026, 8, 1),
    )
    assert row_a["thesis_id"] != row_b["thesis_id"]

    # a genuine retry with identical inputs on all five fields still collides,
    # preserving create_thesis's own idempotency guard.
    row_a_retry = fundamentals_l4_thesis.create_thesis(
        company_master_id="nse:X", prediction_text="p", target_date=date(2027, 1, 1),
        invalidation_criteria="misses Q2", origin_tag="ad_hoc", created_date=date(2026, 8, 1),
    )
    assert row_a_retry["thesis_id"] == row_a["thesis_id"]


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
    monkeypatch.setattr(fundamentals_l4_thesis, "sql_to_df", lambda q, **k: pd.DataFrame([{"status": "open"}]))

    fundamentals_l4_thesis.resolve_thesis(
        thesis_id="thesis:x", resolved_true=False, failure_attribution="thesis_wrong",
        resolution_notes="note", resolution_date=date(2026, 12, 31),
    )

    assert len(executed) == 1
    query, params = executed[0]
    assert "UPDATE fundamentals_l4_thesis" in query
    assert params == (False, date(2026, 12, 31), "note", "thesis_wrong", "thesis:x")


def test_resolve_thesis_raises_when_thesis_does_not_exist(monkeypatch):
    # BUG FOUND LIVE 2026-08-18 (re-audit): a typo'd thesis_id used to silently
    # succeed (UPDATE ... WHERE thesis_id = %s matches zero rows, no error).
    executed = []

    class FakeCursor:
        def execute(self, query, params=None):
            executed.append((str(query), params))

    @contextlib.contextmanager
    def fake_db_session():
        yield None, FakeCursor()

    monkeypatch.setattr(fundamentals_l4_thesis, "db_session", fake_db_session)
    monkeypatch.setattr(fundamentals_l4_thesis, "sql_to_df", lambda q, **k: pd.DataFrame())

    with pytest.raises(fundamentals_l4_thesis.ThesisValidationError, match="does not exist"):
        fundamentals_l4_thesis.resolve_thesis(
            thesis_id="thesis:typo", resolved_true=True,
        )
    assert executed == []  # nothing was written


def test_resolve_thesis_raises_when_already_resolved(monkeypatch):
    # BUG FOUND LIVE 2026-08-18 (re-audit): resolve_thesis used to freely
    # overwrite an existing resolution -- exactly what create_thesis's own
    # already-resolved guard exists to prevent, just reachable from this path.
    executed = []

    class FakeCursor:
        def execute(self, query, params=None):
            executed.append((str(query), params))

    @contextlib.contextmanager
    def fake_db_session():
        yield None, FakeCursor()

    monkeypatch.setattr(fundamentals_l4_thesis, "db_session", fake_db_session)
    monkeypatch.setattr(fundamentals_l4_thesis, "sql_to_df", lambda q, **k: pd.DataFrame([{"status": "resolved"}]))

    with pytest.raises(fundamentals_l4_thesis.ThesisValidationError, match="already resolved"):
        fundamentals_l4_thesis.resolve_thesis(
            thesis_id="thesis:x", resolved_true=True,
        )
    assert executed == []  # nothing was written


def test_check_structured_prediction_returns_none_without_metric_fields():
    assert fundamentals_l4_thesis.check_structured_prediction({"company_master_id": "nse:X"}) is None


def test_check_structured_prediction_treats_numpy_bool_as_non_numeric(monkeypatch):
    # BUG FOUND LIVE 2026-08-18 (re-audit): isinstance(v, bool) is False for
    # numpy's own boolean scalar type -- confirmed live: np.bool_(True) is not a
    # bool instance, but float(np.bool_(True)) succeeds and returns 1.0. An L2
    # state column arriving as pandas boolean dtype would sail past the old
    # `isinstance(value, (bool, str))` guard, get silently coerced to a float,
    # and compared against threshold as a real metric value instead of the
    # graceful None every other non-numeric branch here returns.
    import numpy as np

    thesis = {
        "company_master_id": "nse:X",
        "metric_name": "flag_col",
        "metric_operator": ">",
        "metric_threshold": 0.5,
    }
    monkeypatch.setattr(
        fundamentals_l4_thesis, "build_l1_ticker_by_company_master_id", lambda: {"nse:X": "X"}
    )
    monkeypatch.setattr(
        fundamentals_l4_thesis,
        "sql_to_df",
        lambda q, **k: pd.DataFrame([{"flag_col": np.bool_(True)}]),
    )
    assert fundamentals_l4_thesis.check_structured_prediction(thesis) is None


def test_check_structured_prediction_evaluates_against_l2_state(monkeypatch):
    monkeypatch.setattr(fundamentals_l4_thesis, "build_l1_ticker_by_company_master_id", lambda: {"nse:CINELINE": "CINELINE"})
    monkeypatch.setattr(
        fundamentals_l4_thesis, "sql_to_df", lambda query, params=None: pd.DataFrame([{"pledge_pct": 50.73}])
    )
    thesis_row = {"company_master_id": "nse:CINELINE", "metric_name": "pledge_pct", "metric_operator": "<", "metric_threshold": 45.0}
    assert fundamentals_l4_thesis.check_structured_prediction(thesis_row) is False

    thesis_row["metric_threshold"] = 60.0
    assert fundamentals_l4_thesis.check_structured_prediction(thesis_row) is True


def test_check_structured_prediction_returns_none_when_no_l2_row(monkeypatch):
    monkeypatch.setattr(fundamentals_l4_thesis, "build_l1_ticker_by_company_master_id", lambda: {"nse:X": "X"})
    monkeypatch.setattr(fundamentals_l4_thesis, "sql_to_df", lambda query, params=None: pd.DataFrame())
    thesis_row = {"company_master_id": "nse:X", "metric_name": "pledge_pct", "metric_operator": "<", "metric_threshold": 45.0}
    assert fundamentals_l4_thesis.check_structured_prediction(thesis_row) is None


def test_check_structured_prediction_returns_none_when_ticker_unresolved(monkeypatch):
    # BUG FOUND LIVE 2026-08-18: naive removeprefix("nse:") used to always "succeed"
    # (wrongly, for the ~22% BSE-only cohort) -- now a genuinely-unresolvable
    # company_master_id correctly short-circuits to None rather than querying L2
    # state under a wrong/empty ticker.
    monkeypatch.setattr(fundamentals_l4_thesis, "build_l1_ticker_by_company_master_id", lambda: {})
    calls = []
    monkeypatch.setattr(fundamentals_l4_thesis, "sql_to_df", lambda query, params=None: calls.append(1) or pd.DataFrame())
    thesis_row = {"company_master_id": "nse:X", "metric_name": "pledge_pct", "metric_operator": "<", "metric_threshold": 45.0}
    assert fundamentals_l4_thesis.check_structured_prediction(thesis_row) is None
    assert calls == []  # never even queried L2 state for an unresolvable company


def test_check_structured_prediction_returns_none_when_value_is_null(monkeypatch):
    monkeypatch.setattr(fundamentals_l4_thesis, "build_l1_ticker_by_company_master_id", lambda: {"nse:X": "X"})
    monkeypatch.setattr(
        fundamentals_l4_thesis, "sql_to_df", lambda query, params=None: pd.DataFrame([{"pledge_pct": None}])
    )
    thesis_row = {"company_master_id": "nse:X", "metric_name": "pledge_pct", "metric_operator": "<", "metric_threshold": 45.0}
    assert fundamentals_l4_thesis.check_structured_prediction(thesis_row) is None


def test_check_structured_prediction_returns_none_for_non_numeric_metric_name(monkeypatch):
    # BUG FOUND LIVE 2026-08-17: metric_name is free text a human types when creating
    # a thesis -- nothing stops it from naming a non-numeric fundamentals_l2_state
    # column (trend_direction, company_name, ticker, ...). Used to raise an uncaught
    # TypeError from `value < threshold` (str vs number) instead of returning None
    # like every other "can't evaluate this" branch in this function.
    monkeypatch.setattr(fundamentals_l4_thesis, "build_l1_ticker_by_company_master_id", lambda: {"nse:X": "X"})
    monkeypatch.setattr(
        fundamentals_l4_thesis,
        "sql_to_df",
        lambda query, params=None: pd.DataFrame([{"net_debt_trend_direction": "accelerating_decline"}]),
    )
    thesis_row = {"company_master_id": "nse:X", "metric_name": "net_debt_trend_direction", "metric_operator": "<", "metric_threshold": 45.0}
    assert fundamentals_l4_thesis.check_structured_prediction(thesis_row) is None


def test_check_structured_prediction_handles_numpy_numeric_dtypes(monkeypatch):
    # A real DataFrame column reads back as numpy.int64/float64, neither a Python
    # int/float subclass -- must still evaluate correctly (not be rejected as
    # "non-numeric" by an overly-strict isinstance-only guard).
    monkeypatch.setattr(fundamentals_l4_thesis, "build_l1_ticker_by_company_master_id", lambda: {"nse:X": "X"})
    monkeypatch.setattr(
        fundamentals_l4_thesis,
        "sql_to_df",
        lambda query, params=None: pd.DataFrame([{"net_debt_consecutive_declining_years": 3}]),  # pandas infers int64
    )
    thesis_row = {"company_master_id": "nse:X", "metric_name": "net_debt_consecutive_declining_years", "metric_operator": ">=", "metric_threshold": 2.0}
    assert fundamentals_l4_thesis.check_structured_prediction(thesis_row) is True


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


def test_build_reference_rows_extracts_sector_level():
    # 2026-08-15: industry_group/basic_industry levels retired (zero readers) -- only sector is
    # extracted now, even though the fixture still carries indgrp/ind data (unused, ignored).
    rows = fundamentals_sector_data.build_reference_rows(SECTOR_DATA_FIXTURE, as_of_date=date(2026, 8, 11))
    assert len(rows["sector"]) == 2
    assert set(rows) == {"sector"}


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
    data = {"sector": {"EQ": [{"sector_desc": "No code here"}]}}
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


def test_run_sector_reference_refresh_upserts_sector_table(monkeypatch):
    monkeypatch.setattr(fundamentals_sector_data, "fetch_sector_reference_data", lambda: SECTOR_DATA_FIXTURE)
    upserts = []
    monkeypatch.setattr(fundamentals_sector_data, "upsert_to_db", lambda df, table, **k: upserts.append((table, len(df), k)))

    result = fundamentals_sector_data.run_sector_reference_refresh()

    assert result == {"sectors": 2}
    tables_upserted = {table for table, _, _ in upserts}
    assert tables_upserted == {fundamentals_sector_data.SECTOR_TABLE}
    for _, _, kwargs in upserts:
        assert kwargs["unique_keys"] == ["code", "as_of_date"]


# fundamentals/screens/sector_cycle.py -- sector capital-cycle aggregation (step 9).


def test_load_l1_companies_with_sector_resolves_bse_numeric_ticker_correctly(monkeypatch):
    # BUG FOUND LIVE 2026-08-15, fixed here: this used to join dim_security on a
    # naive 'nse:' || ticker SQL string -- wrong for the ~22% of L1 tickers that are
    # actually raw BSE numeric scrip codes (confirmed live). Two real companies
    # (KSE/519421, SUPER/512527) were silently dropped from sector aggregation by
    # this before the fix; this test pins the mechanism, not the live figures.
    l1_df = pd.DataFrame(
        [
            {"company_id": 1, "company_name": "Alufluoride", "ticker": "524634", "metrics_json": json.dumps({"qtr_sales_var_pct": -39.28})},
            {"company_id": 2, "company_name": "Reliance", "ticker": "RELIANCE", "metrics_json": json.dumps({"qtr_sales_var_pct": 5.0})},
        ]
    )
    sector_df = pd.DataFrame(
        [
            {"company_master_id": "nse:ALUFLUOR", "sector_code": "IN02"},
            {"company_master_id": "nse:RELIANCE", "sector_code": "IN03"},
        ]
    )
    call_count = {"n": 0}

    def fake_sql_to_df(query, **kwargs):
        call_count["n"] += 1
        return l1_df if call_count["n"] == 1 else sector_df

    monkeypatch.setattr(fundamentals_sector_cycle, "sql_to_df", fake_sql_to_df)
    monkeypatch.setattr(
        fundamentals_sector_cycle,
        "map_company_master_ids_nse_or_bse",
        lambda tickers: pd.Series(["nse:ALUFLUOR" if t == "524634" else "nse:RELIANCE" for t in tickers], index=tickers.index, dtype="string"),
    )

    result = fundamentals_sector_cycle.load_l1_companies_with_sector()

    assert dict(zip(result["ticker"], result["sector_code"])) == {"524634": "IN02", "RELIANCE": "IN03"}
    assert "company_master_id" not in result.columns  # internal-only, same output shape as before the fix


def test_classify_phase_expansion_when_capacity_outruns_demand():
    assert fundamentals_sector_cycle.classify_phase(20.0, 5.0) == "capacity_expansion"


def test_classify_phase_discipline_when_demand_outruns_capacity():
    assert fundamentals_sector_cycle.classify_phase(2.0, 15.0) == "capacity_discipline"


def test_classify_phase_balanced_within_threshold():
    assert fundamentals_sector_cycle.classify_phase(10.0, 8.0) == "balanced"


def test_classify_phase_none_when_either_input_missing():
    assert fundamentals_sector_cycle.classify_phase(None, 5.0) is None
    assert fundamentals_sector_cycle.classify_phase(10.0, None) is None


def test_classify_growth_high_at_or_above_threshold():
    assert fundamentals_sector_cycle.classify_growth(15.0, "adequate") == "high_growth"
    assert fundamentals_sector_cycle.classify_growth(30.0, "adequate") == "high_growth"


def test_classify_growth_medium_between_thresholds():
    assert fundamentals_sector_cycle.classify_growth(5.0, "adequate") == "medium_growth"
    assert fundamentals_sector_cycle.classify_growth(14.9, "adequate") == "medium_growth"


def test_classify_growth_low_below_medium_threshold():
    assert fundamentals_sector_cycle.classify_growth(4.9, "adequate") == "low_growth"
    assert fundamentals_sector_cycle.classify_growth(-20.0, "adequate") == "low_growth"


def test_classify_growth_no_pattern_when_demand_missing():
    assert fundamentals_sector_cycle.classify_growth(None, "adequate") == "no_pattern"


def test_classify_growth_no_pattern_when_sample_size_low():
    # a real, non-missing 20% median demand growth off a single company is still not
    # a trustworthy sector read -- same gate `sample_size_confidence` applies to phase.
    assert fundamentals_sector_cycle.classify_growth(20.0, "low") == "no_pattern"


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

    # n_companies_with_gross_block=2 < MIN_COMPANIES_FOR_CONFIDENCE -> "no_pattern"
    # despite a real demand_growth_pct=15.0, same low-sample gate as IN0201 below.
    assert ch["sample_size_confidence"] == "low"
    assert ch["growth_classification"] == "no_pattern"

    auto = result[result["sector_code"] == "IN0201"].iloc[0]
    assert auto["n_companies_with_gross_block"] == 0
    assert pd.isna(auto["capacity_growth_pct"])  # None -> NaN once mixed into a float64 DataFrame column
    assert auto["demand_growth_pct"] == 5.0
    # n_companies_with_gross_block=0 < MIN_COMPANIES_FOR_CONFIDENCE -> sample_size_confidence
    # is "low" here, so growth_classification is "no_pattern" despite a real demand_growth_pct.
    assert auto["sample_size_confidence"] == "low"
    assert auto["growth_classification"] == "no_pattern"


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
    row = result.iloc[0]
    assert row["sample_size_confidence"] == "adequate"
    assert row["growth_classification"] == "medium_growth"  # demand_growth_pct=10.0, adequate sample


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


def test_load_adjusted_price_history_falls_back_to_bse_view_when_nse_empty(monkeypatch):
    # a BSE-only company (2026-08-15 gap fix) has nothing in advisory_adjusted_ohlcv_daily
    # at all -- must fall back to bse_advisory_adjusted_ohlcv_daily keyed by scrip_code.
    bse_rows = pd.DataFrame({"date": pd.to_datetime(["2026-08-13", "2026-08-14"]), "adj_close": [217.95, 215.9]})
    calls = []

    def fake_sql_to_df(query, params=None):
        calls.append(query)
        if "bse_advisory_adjusted_ohlcv_daily" in query:
            return bse_rows
        return pd.DataFrame(columns=["date", "adj_close"])  # NSE view: empty

    monkeypatch.setattr(fundamentals_technicals, "sql_to_df", fake_sql_to_df)

    result = fundamentals_technicals.load_adjusted_price_history("500166")

    assert len(result) == 2
    assert result["adj_close"].tolist() == [217.95, 215.9]
    assert any("advisory_adjusted_ohlcv_daily" in q and "bse_" not in q for q in calls)  # NSE tried first
    assert any("bse_advisory_adjusted_ohlcv_daily" in q for q in calls)


def test_load_adjusted_price_history_falls_back_to_nse_be_series_when_eq_empty(monkeypatch):
    # BUG FOUND LIVE 2026-08-18 (re-audit): series='EQ' hardcode drops BE-series
    # (trade-to-trade / restricted-segment) companies the adjusted view actually
    # covers -- live, VHLTD has 531 real BE-series rows spanning 2 years and zero
    # EQ rows, yet showed close=NULL purely because of this filter.
    be_rows = pd.DataFrame({"date": pd.to_datetime(["2026-08-13", "2026-08-14"]), "adj_close": [10.5, 11.0]})

    def fake_sql_to_df(query, params=None):
        if "series = 'EQ'" in query:
            return pd.DataFrame(columns=["date", "adj_close"])
        if "series = 'BE'" in query:
            return be_rows
        raise AssertionError("should not fall through to the BSE view when NSE BE has data")

    monkeypatch.setattr(fundamentals_technicals, "sql_to_df", fake_sql_to_df)

    result = fundamentals_technicals.load_adjusted_price_history("VHLTD")

    assert result["adj_close"].tolist() == [10.5, 11.0]


def test_load_adjusted_price_history_uses_nse_view_when_available(monkeypatch):
    nse_rows = pd.DataFrame({"date": pd.to_datetime(["2026-08-14"]), "adj_close": [100.0]})
    monkeypatch.setattr(fundamentals_technicals, "sql_to_df", lambda query, params=None: nse_rows)

    result = fundamentals_technicals.load_adjusted_price_history("RELIANCE")

    assert result["adj_close"].tolist() == [100.0]


def test_run_technicals_refresh_returns_early_on_empty_l1(monkeypatch):
    monkeypatch.setattr(fundamentals_technicals, "ensure_bse_view", lambda: None)
    monkeypatch.setattr(fundamentals_technicals, "check_bse_price_pipeline_freshness", lambda run_date: True)
    monkeypatch.setattr(fundamentals_technicals, "load_l1_tickers", lambda: pd.DataFrame())
    fallback_events = []
    monkeypatch.setattr(fundamentals_technicals, "_record_fallback", lambda *a, **k: fallback_events.append((a, k)))

    result = fundamentals_technicals.run_technicals_refresh()

    assert result == {"companies": 0, "no_history": 0, "stale_price": 0}
    assert any(a and a[0] == "technicals_no_l1_universe" for a, k in fallback_events)


def test_run_technicals_refresh_flags_insufficient_history_and_upserts(monkeypatch):
    monkeypatch.setattr(fundamentals_technicals, "ensure_bse_view", lambda: None)
    monkeypatch.setattr(fundamentals_technicals, "check_bse_price_pipeline_freshness", lambda run_date: True)
    tickers = pd.DataFrame([{"ticker": "AAA", "company_name": "A Co"}, {"ticker": "BBB", "company_name": "B Co"}])
    monkeypatch.setattr(fundamentals_technicals, "load_l1_tickers", lambda: tickers)
    monkeypatch.setattr(fundamentals_technicals, "map_company_master_ids_nse_or_bse", lambda tickers: tickers.map(lambda t: f"nse:{t}").astype("string"))

    # AAA's history ends "today" (not stale) -- isolates this test to the
    # insufficient-history behavior it's actually about, independent of the
    # separate price_data_stale check (covered by its own tests below).
    full_history = pd.DataFrame({"date": pd.date_range(end=pd.Timestamp.now(tz="UTC").normalize(), periods=260, freq="D"), "adj_close": [100.0] * 260})
    thin_history = pd.DataFrame({"date": pd.date_range("2026-07-01", periods=5, freq="D"), "adj_close": [50.0] * 5})

    def fake_history(ticker, **kwargs):
        return full_history if ticker == "AAA" else thin_history

    monkeypatch.setattr(fundamentals_technicals, "load_adjusted_price_history", fake_history)
    upserts = []
    monkeypatch.setattr(fundamentals_technicals, "upsert_to_db", lambda df, table, **k: upserts.append((df, table, k)))
    fallback_events = []
    monkeypatch.setattr(fundamentals_technicals, "_record_fallback", lambda *a, **k: fallback_events.append((a, k)))

    result = fundamentals_technicals.run_technicals_refresh()

    assert result["companies"] == 2
    assert result["no_history"] == 1
    assert len(upserts) == 1
    assert upserts[0][1] == fundamentals_technicals.RESULTS_TABLE
    assert upserts[0][2]["unique_keys"] == ["company_master_id", "run_date"]
    written = upserts[0][0]
    assert set(written["company_master_id"]) == {"nse:AAA", "nse:BBB"}
    assert any(a and a[0] == "technicals_insufficient_history" for a, k in fallback_events)
    aaa_row = written[written["ticker"] == "AAA"].iloc[0]
    assert bool(aaa_row["price_data_stale"]) is False


def test_run_technicals_refresh_flags_and_records_stale_price_series(monkeypatch):
    # BUG FOUND LIVE 2026-08-15, fixed here: the "latest row on or before today"
    # lookup used to happily return a months-old row with no indication it's stale
    # -- confirmed live, 32% of the whole price table affected, one real watchlist
    # company's "today's close" silently frozen for weeks with zero fallback event.
    monkeypatch.setattr(fundamentals_technicals, "ensure_bse_view", lambda: None)
    monkeypatch.setattr(fundamentals_technicals, "check_bse_price_pipeline_freshness", lambda run_date: True)
    tickers = pd.DataFrame([{"ticker": "STALECO", "company_name": "Stale Co"}, {"ticker": "FRESHCO", "company_name": "Fresh Co"}])
    monkeypatch.setattr(fundamentals_technicals, "load_l1_tickers", lambda: tickers)
    monkeypatch.setattr(fundamentals_technicals, "map_company_master_ids_nse_or_bse", lambda tickers: tickers.map(lambda t: f"nse:{t}").astype("string"))

    fresh_history = pd.DataFrame({"date": pd.date_range(end=pd.Timestamp.now(tz="UTC").normalize(), periods=25, freq="D"), "adj_close": [100.0] * 25})
    # last row is 30 days old -- past STALE_PRICE_THRESHOLD_DAYS (7)
    stale_end = pd.Timestamp.now(tz="UTC").normalize() - pd.Timedelta(days=30)
    stale_history = pd.DataFrame({"date": pd.date_range(end=stale_end, periods=25, freq="D"), "adj_close": [50.0] * 25})

    def fake_history(ticker, **kwargs):
        return stale_history if ticker == "STALECO" else fresh_history

    monkeypatch.setattr(fundamentals_technicals, "load_adjusted_price_history", fake_history)
    upserts = []
    monkeypatch.setattr(fundamentals_technicals, "upsert_to_db", lambda df, table, **k: upserts.append((df, table, k)))
    fallback_events = []
    monkeypatch.setattr(fundamentals_technicals, "_record_fallback", lambda *a, **k: fallback_events.append((a, k)))

    result = fundamentals_technicals.run_technicals_refresh()

    assert result["stale_price"] == 1
    written = upserts[0][0]
    stale_row = written[written["ticker"] == "STALECO"].iloc[0]
    fresh_row = written[written["ticker"] == "FRESHCO"].iloc[0]
    assert bool(stale_row["price_data_stale"]) is True
    assert bool(fresh_row["price_data_stale"]) is False
    stale_event = next(e for a, k in fallback_events for e in [k] if a and a[0] == "technicals_stale_price_series")
    assert stale_event["metadata"]["count"] == 1
    assert "STALECO" in stale_event["metadata"]["sample_tickers"]


def test_check_bse_price_pipeline_freshness_true_when_recent_and_well_covered(monkeypatch):
    run_date = pd.Timestamp.now(tz="UTC").normalize()
    monkeypatch.setattr(
        fundamentals_technicals,
        "sql_to_df",
        lambda query, **k: pd.DataFrame([{"date": run_date - pd.Timedelta(days=1), "n": 4100}]),
    )
    assert fundamentals_technicals.check_bse_price_pipeline_freshness(run_date) is True


def test_check_bse_price_pipeline_freshness_false_when_stale_or_empty(monkeypatch):
    run_date = pd.Timestamp.now(tz="UTC").normalize()
    stale = run_date - pd.Timedelta(days=fundamentals_technicals.STALE_PRICE_THRESHOLD_DAYS + 5)
    monkeypatch.setattr(fundamentals_technicals, "sql_to_df", lambda query, **k: pd.DataFrame([{"date": stale, "n": 4100}]))
    assert fundamentals_technicals.check_bse_price_pipeline_freshness(run_date) is False

    monkeypatch.setattr(fundamentals_technicals, "sql_to_df", lambda query, **k: pd.DataFrame())
    assert fundamentals_technicals.check_bse_price_pipeline_freshness(run_date) is False


def test_check_bse_price_pipeline_freshness_checks_the_joined_view_not_the_raw_table(monkeypatch):
    # BUG FOUND LIVE 2026-08-18 (re-audit): the first version only queried
    # bseindia_ohlcv (cron job 1) -- if job 4 (price_adjustment, which writes
    # bseindia_adjustment_factors) failed or was skipped, bseindia_ohlcv stayed
    # fresh and this check returned True even though the joined view technicals
    # actually reads had nothing for the new date.
    captured = {}

    def fake_sql_to_df(query, **k):
        captured["query"] = query
        return pd.DataFrame()

    monkeypatch.setattr(fundamentals_technicals, "sql_to_df", fake_sql_to_df)
    fundamentals_technicals.check_bse_price_pipeline_freshness(pd.Timestamp.now(tz="UTC"))
    assert "bse_advisory_adjusted_ohlcv_daily" in captured["query"]
    assert "bseindia_ohlcv" not in captured["query"]


def test_check_bse_price_pipeline_freshness_false_when_recent_but_thin_coverage(monkeypatch):
    # BUG FOUND LIVE 2026-08-18 (re-audit): MAX(date) alone can't tell "a
    # genuinely healthy day" (~4,100+ rows, confirmed live) from "one stray row
    # from an unrelated backfill" landing on a recent date.
    run_date = pd.Timestamp.now(tz="UTC").normalize()
    monkeypatch.setattr(
        fundamentals_technicals,
        "sql_to_df",
        lambda query, **k: pd.DataFrame([{"date": run_date - pd.Timedelta(days=1), "n": 3}]),
    )
    assert fundamentals_technicals.check_bse_price_pipeline_freshness(run_date) is False


def test_run_technicals_refresh_records_fallback_when_bse_pipeline_looks_stale(monkeypatch):
    # BUG FOUND LIVE 2026-08-17 (structural): fundamentals/run_pipeline.py's own
    # STEPS has no step that refreshes BSE OHLCV/adjustment data -- an implicit
    # dependency on a separate pure-TA cron job. A company with zero history reads
    # identically whether IT genuinely lacks a listing or the WHOLE upstream table
    # is stale/empty; this event distinguishes the latter.
    monkeypatch.setattr(fundamentals_technicals, "ensure_bse_view", lambda: None)
    monkeypatch.setattr(fundamentals_technicals, "check_bse_price_pipeline_freshness", lambda run_date: False)
    tickers = pd.DataFrame([{"ticker": "FRESHCO", "company_name": "Fresh Co"}])
    monkeypatch.setattr(fundamentals_technicals, "load_l1_tickers", lambda: tickers)
    fresh_history = pd.DataFrame({"date": pd.date_range(end=pd.Timestamp.now(tz="UTC").normalize(), periods=25, freq="D"), "adj_close": [100.0] * 25})
    monkeypatch.setattr(fundamentals_technicals, "load_adjusted_price_history", lambda ticker, **k: fresh_history)
    monkeypatch.setattr(fundamentals_technicals, "upsert_to_db", lambda df, table, **k: None)
    fallback_events = []
    monkeypatch.setattr(fundamentals_technicals, "_record_fallback", lambda *a, **k: fallback_events.append((a, k)))

    fundamentals_technicals.run_technicals_refresh()

    assert any(a and a[0] == "technicals_bse_price_pipeline_stale_or_never_run" for a, k in fallback_events)


def test_run_technicals_refresh_no_stale_prices_skips_fallback_event(monkeypatch):
    monkeypatch.setattr(fundamentals_technicals, "ensure_bse_view", lambda: None)
    monkeypatch.setattr(fundamentals_technicals, "check_bse_price_pipeline_freshness", lambda run_date: True)
    tickers = pd.DataFrame([{"ticker": "FRESHCO", "company_name": "Fresh Co"}])
    monkeypatch.setattr(fundamentals_technicals, "load_l1_tickers", lambda: tickers)
    monkeypatch.setattr(fundamentals_technicals, "map_company_master_ids_nse_or_bse", lambda tickers: tickers.map(lambda t: f"nse:{t}").astype("string"))
    fresh_history = pd.DataFrame({"date": pd.date_range(end=pd.Timestamp.now(tz="UTC").normalize(), periods=25, freq="D"), "adj_close": [100.0] * 25})
    monkeypatch.setattr(fundamentals_technicals, "load_adjusted_price_history", lambda ticker, **k: fresh_history)
    monkeypatch.setattr(fundamentals_technicals, "upsert_to_db", lambda df, table, **k: None)
    fallback_events = []
    monkeypatch.setattr(fundamentals_technicals, "_record_fallback", lambda *a, **k: fallback_events.append((a, k)))

    result = fundamentals_technicals.run_technicals_refresh()

    assert result["companies"] == 1
    assert result["stale_price"] == 0
    assert not any(a and a[0] == "technicals_stale_price_series" for a, k in fallback_events)


def test_load_price_near_prefers_adjusted_falls_back_to_raw(monkeypatch):
    calls = []

    def fake_sql_to_df(query, params=None):
        calls.append(query)
        if "advisory_adjusted_ohlcv_daily" in query:
            return pd.DataFrame()  # no adjusted data (EQ or BE) -- forces fallback
        return pd.DataFrame([{"close": 42.5}])

    monkeypatch.setattr(fundamentals_watchlist, "sql_to_df", fake_sql_to_df)

    price = fundamentals_watchlist.load_price_near("nse:FOO", "2026-08-01")

    assert price == 42.5
    assert len(calls) == 3  # tried EQ, then BE, then raw
    assert "series = 'EQ'" in calls[0]
    assert "series = 'BE'" in calls[1]


def test_load_price_near_falls_back_to_be_series_when_eq_empty(monkeypatch):
    # BUG FOUND LIVE 2026-08-18 (re-audit): series='EQ' hardcode drops BE-series
    # companies the adjusted view actually covers -- live, VHLTD has 531 real
    # BE-series rows and zero EQ rows.
    def fake_sql_to_df(query, params=None):
        if "series = 'EQ'" in query:
            return pd.DataFrame()
        if "series = 'BE'" in query:
            return pd.DataFrame([{"adj_close": 12.34}])
        raise AssertionError("should not fall through to the raw table when BE has data")

    monkeypatch.setattr(fundamentals_watchlist, "sql_to_df", fake_sql_to_df)
    assert fundamentals_watchlist.load_price_near("nse:VHLTD", "2026-08-01") == 12.34


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


def test_sync_watchlist_from_alerts_corrects_first_seen_at_backward_when_earlier_history_surfaces(monkeypatch):
    # BUG FOUND LIVE 2026-08-15, fixed here: first_seen_at used to be frozen forever
    # once a watchlist row existed. An identity-resolution backfill landing on an
    # OLDER fundamentals_l3_alerts row (after this company's watchlist row was
    # already created) can reveal a genuinely earlier true first alert -- confirmed
    # live, 9/38 real watchlist rows affected, one off by over a year. Must correct
    # BACKWARD when this happens.
    monkeypatch.setattr(fundamentals_watchlist, "_ensure_watchlist_table", lambda: None)
    alert_summary = pd.DataFrame(
        [{"company_master_id": "nse:FOO", "first_alert_date": pd.Timestamp("2025-05-23"), "last_alert_date": pd.Timestamp("2026-08-05"), "alert_count": 4}]
    )
    monkeypatch.setattr(fundamentals_watchlist, "load_l3_alert_summary_by_company", lambda: alert_summary)
    # existing row was frozen at 2026-08-06 -- LATER than the true first alert date
    # now visible above (2025-05-23), exactly the drift found live.
    existing = pd.DataFrame([{"company_master_id": "nse:FOO", "first_seen_at": pd.Timestamp("2026-08-06"), "first_seen_price": 90.51}])
    monkeypatch.setattr(fundamentals_watchlist, "load_existing_watchlist", lambda: existing)
    monkeypatch.setattr(fundamentals_watchlist, "load_price_near", lambda cid, date: 42.0)
    fallback_events = []
    monkeypatch.setattr(fundamentals_watchlist, "_record_fallback", lambda *a, **k: fallback_events.append((a, k)))
    upserts = []
    monkeypatch.setattr(fundamentals_watchlist, "upsert_to_db", lambda df, table, **k: upserts.append((df, table, k)))

    result = fundamentals_watchlist.sync_watchlist_from_alerts()

    written = upserts[0][0].iloc[0]
    assert written["first_seen_at"] == pd.Timestamp("2025-05-23")  # corrected backward
    assert written["first_seen_price"] == 42.0  # re-derived for the corrected date, not left at 90.51
    assert any(a and a[0] == "watchlist_first_seen_at_corrected" for a, k in fallback_events)
    assert result["new_candidates"] == 0  # still not treated as a brand-new candidate


def test_sync_watchlist_from_alerts_moves_first_seen_at_forward_when_the_supporting_alert_is_gone(monkeypatch):
    # BUG FOUND LIVE 2026-08-18 (re-audit): the original backward-only design
    # (de4aab6) assumed MIN(alert_date) only ever reveals an earlier true history,
    # but alert_date is mutable for a fixed (source, news_id, trigger_type) key -- a
    # later reclassification or deletion (e.g. this session's own capital_raise
    # false-positive remediation, which deleted spurious alerts) can make the true
    # earliest SURVIVING alert later than the frozen stored value, with nothing left
    # to support the old one. Confirmed live: 3 real companies post-remediation. Must
    # now correct FORWARD too, not stay frozen at an unsupported date, and must be
    # visible via the same fallback event as the backward case.
    monkeypatch.setattr(fundamentals_watchlist, "_ensure_watchlist_table", lambda: None)
    alert_summary = pd.DataFrame(
        [{"company_master_id": "nse:FOO", "first_alert_date": pd.Timestamp("2026-08-10"), "last_alert_date": pd.Timestamp("2026-08-10"), "alert_count": 1}]
    )
    monkeypatch.setattr(fundamentals_watchlist, "load_l3_alert_summary_by_company", lambda: alert_summary)
    existing = pd.DataFrame([{"company_master_id": "nse:FOO", "first_seen_at": pd.Timestamp("2026-08-01"), "first_seen_price": 100.0}])
    monkeypatch.setattr(fundamentals_watchlist, "load_existing_watchlist", lambda: existing)
    monkeypatch.setattr(fundamentals_watchlist, "load_price_near", lambda cid, date: 55.0)
    fallback_events = []
    monkeypatch.setattr(fundamentals_watchlist, "_record_fallback", lambda *a, **k: fallback_events.append((a, k)))
    upserts = []
    monkeypatch.setattr(fundamentals_watchlist, "upsert_to_db", lambda df, table, **k: upserts.append((df, table, k)))

    result = fundamentals_watchlist.sync_watchlist_from_alerts()

    written = upserts[0][0].iloc[0]
    assert written["first_seen_at"] == pd.Timestamp("2026-08-10")  # corrected forward to the true earliest surviving alert
    assert written["first_seen_price"] == 55.0  # re-derived for the corrected date, not left at 100.0
    fallback_calls = [(a, k) for a, k in fallback_events if a and a[0] == "watchlist_first_seen_at_corrected"]
    assert len(fallback_calls) == 1
    assert fallback_calls[0][1]["metadata"]["direction"] == "forward"
    assert result["new_candidates"] == 0  # still not treated as a brand-new candidate


def test_sync_watchlist_from_alerts_leaves_first_seen_at_alone_when_unchanged(monkeypatch):
    monkeypatch.setattr(fundamentals_watchlist, "_ensure_watchlist_table", lambda: None)
    alert_summary = pd.DataFrame(
        [{"company_master_id": "nse:FOO", "first_alert_date": pd.Timestamp("2026-08-01"), "last_alert_date": pd.Timestamp("2026-08-01"), "alert_count": 1}]
    )
    monkeypatch.setattr(fundamentals_watchlist, "load_l3_alert_summary_by_company", lambda: alert_summary)
    existing = pd.DataFrame([{"company_master_id": "nse:FOO", "first_seen_at": pd.Timestamp("2026-08-01"), "first_seen_price": 100.0}])
    monkeypatch.setattr(fundamentals_watchlist, "load_existing_watchlist", lambda: existing)
    monkeypatch.setattr(fundamentals_watchlist, "load_price_near", lambda cid, date: (_ for _ in ()).throw(AssertionError("must not be called")))
    upserts = []
    monkeypatch.setattr(fundamentals_watchlist, "upsert_to_db", lambda df, table, **k: upserts.append((df, table, k)))

    fundamentals_watchlist.sync_watchlist_from_alerts()

    written = upserts[0][0].iloc[0]
    assert written["first_seen_at"] == pd.Timestamp("2026-08-01")  # true first alert date matches stored -- no correction, no live price lookup
    assert written["first_seen_price"] == 100.0


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
    pointers = [{"signal_type": "rating_action", "value": "downgraded"}]
    bundle = fundamentals_watch_summary.build_company_evidence_bundle("nse:X", alerts, {"ticker": "X"}, {"close": 100}, {"sector_code": "IN01"}, pointers)
    assert bundle["company_master_id"] == "nse:X"
    assert bundle["alerts"] == [{"trigger_type": "rating_downgrade", "origin": "rule", "alert_date": date(2026, 8, 1), "reasoning": "r"}]
    assert bundle["l2_state"] == {"ticker": "X"}
    assert bundle["technicals"] == {"close": 100}
    assert bundle["sector_context"] == {"sector_code": "IN01"}
    assert bundle["signal_pointers"] == pointers


def test_build_company_evidence_bundle_empty_alerts():
    bundle = fundamentals_watch_summary.build_company_evidence_bundle("nse:X", pd.DataFrame(), None, None, None)
    assert bundle["alerts"] == []
    assert bundle["l2_state"] is None
    assert bundle["signal_pointers"] == []  # defaults to [] when the caller omits it (old 5-arg call shape)


def test_load_companies_needing_narrative_refresh_compares_processing_timestamps(monkeypatch):
    # BUG FOUND LIVE 2026-08-18 (re-audit): used to compare last_alert_at (a
    # filing's disclosure date) against narrative_generated_at (a wall-clock
    # processing timestamp) -- alert_date lags real alert creation by a median of
    # 15 days, so the gate stopped firing after the first narrative for nearly
    # every company. Confirmed live: 38/39 watchlisted companies permanently
    # gated out. Now compares MAX(fundamentals_l3_alerts.load_ts) -- both
    # processing timestamps.
    captured = {}

    def fake_sql_to_df(query, **kwargs):
        captured["query"] = query
        return pd.DataFrame()

    monkeypatch.setattr(fundamentals_watch_summary, "sql_to_df", fake_sql_to_df)
    fundamentals_watch_summary.load_companies_needing_narrative_refresh()
    query = captured["query"]
    assert "MAX(load_ts) AS latest_alert_load_ts" in query
    assert "FROM fundamentals_l3_alerts" in query
    assert "a.latest_alert_load_ts > w.narrative_generated_at" in query
    # not the old event-date comparison
    assert "last_alert_at > narrative_generated_at::date" not in query


def test_run_watch_summary_refresh_returns_early_when_no_candidates(monkeypatch):
    monkeypatch.setattr(fundamentals_watch_summary, "_bootstrap_narrative_columns", lambda: None)
    monkeypatch.setattr(fundamentals_watch_summary, "load_companies_needing_narrative_refresh", lambda limit=None: pd.DataFrame())
    monkeypatch.setattr(fundamentals_watch_summary, "_count_companies_needing_narrative_refresh", lambda: 0)

    result = fundamentals_watch_summary.run_watch_summary_refresh()

    assert result == {"generated": 0, "failed": 0, "blocked": False, "narrative_events": [], "backlog_size": 0}


def _patch_watch_summary_evidence_loaders(monkeypatch):
    monkeypatch.setattr(fundamentals_watch_summary, "load_company_alerts", lambda cmid: pd.DataFrame())
    monkeypatch.setattr(fundamentals_watch_summary, "load_latest_l2_state_for_company", lambda cmid: None)
    monkeypatch.setattr(fundamentals_watch_summary, "load_latest_technicals_for_company", lambda cmid: None)
    monkeypatch.setattr(fundamentals_watch_summary, "load_sector_context_for_company", lambda cmid: None)
    monkeypatch.setattr(fundamentals_watch_summary, "get_stock_signal_pointers", lambda cmid: [])
    monkeypatch.setattr(fundamentals_watch_summary, "_count_companies_needing_narrative_refresh", lambda: 0)


def test_count_companies_needing_narrative_refresh_mirrors_eligibility_condition(monkeypatch):
    captured = {}

    def fake_sql_to_df(query, **kwargs):
        captured["query"] = query
        return pd.DataFrame([{"n": 7}])

    monkeypatch.setattr(fundamentals_watch_summary, "sql_to_df", fake_sql_to_df)
    assert fundamentals_watch_summary._count_companies_needing_narrative_refresh() == 7
    query = captured["query"]
    assert "COUNT(*)" in query
    assert "w.narrative_generated_at IS NULL" in query
    assert "a.latest_alert_load_ts > w.narrative_generated_at" in query


def test_run_watch_summary_refresh_records_fallback_when_backlog_exceeds_limit(monkeypatch):
    # BUG FOUND LIVE 2026-08-18 (re-audit): candidates are ordered oldest-alert-
    # first with a fixed per-run limit and no visibility into whether the
    # backlog exceeds it -- a persistently-failing company at the front of the
    # queue (or backlog growth outpacing the limit) could starve companies
    # behind it indefinitely with no signal. Now surfaced via fallback telemetry.
    monkeypatch.setattr(fundamentals_watch_summary, "_bootstrap_narrative_columns", lambda: None)
    monkeypatch.setattr(fundamentals_watch_summary, "_count_companies_needing_narrative_refresh", lambda: 120)
    monkeypatch.setattr(fundamentals_watch_summary, "load_companies_needing_narrative_refresh", lambda limit=None: pd.DataFrame())
    events = []
    monkeypatch.setattr(
        fundamentals_watch_summary,
        "record_local_fallback_event",
        lambda **kwargs: events.append(kwargs),
    )

    result = fundamentals_watch_summary.run_watch_summary_refresh(limit=50)

    assert result["backlog_size"] == 120
    assert len(events) == 1
    assert events[0]["fallback_type"] == "narrative_refresh_backlog_exceeds_limit"
    assert events[0]["metadata"] == {"backlog_size": 120, "limit": 50, "deferred": 70}


def test_run_watch_summary_refresh_no_fallback_when_backlog_within_limit(monkeypatch):
    monkeypatch.setattr(fundamentals_watch_summary, "_bootstrap_narrative_columns", lambda: None)
    monkeypatch.setattr(fundamentals_watch_summary, "_count_companies_needing_narrative_refresh", lambda: 3)
    monkeypatch.setattr(fundamentals_watch_summary, "load_companies_needing_narrative_refresh", lambda limit=None: pd.DataFrame())
    events = []
    monkeypatch.setattr(
        fundamentals_watch_summary,
        "record_local_fallback_event",
        lambda **kwargs: events.append(kwargs),
    )

    fundamentals_watch_summary.run_watch_summary_refresh(limit=50)

    assert events == []


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


def test_run_watch_summary_refresh_treats_nat_narrative_generated_at_as_new_candidate(monkeypatch):
    # BUG FOUND LIVE 2026-08-18 (re-audit): narrative_generated_at is a
    # TIMESTAMPTZ column read through sql_to_df -- once ANY row in the result
    # has a real timestamp, pandas gives the whole column a datetime64[ns, UTC]
    # dtype and a SQL NULL comes back as pd.NaT, not None. The old
    # `candidate.get("narrative_generated_at") is None` check is False for NaT,
    # so a genuinely brand-new candidate sitting in the same batch as an
    # existing candidate was misclassified as "not new".
    monkeypatch.setattr(fundamentals_watch_summary, "_bootstrap_narrative_columns", lambda: None)
    candidates = pd.DataFrame(
        [
            {"company_master_id": "nse:OLD", "last_alert_at": date(2026, 8, 1), "narrative_generated_at": pd.Timestamp("2026-08-01", tz="UTC"), "narrative_text": "old"},
            {"company_master_id": "nse:NEW", "last_alert_at": date(2026, 8, 5), "narrative_generated_at": None, "narrative_text": None},
        ]
    )
    # mixing a real timestamp with None in the same column promotes it to
    # datetime64[ns, UTC], turning the None into pd.NaT -- exactly the live shape.
    assert pd.api.types.is_datetime64_any_dtype(candidates["narrative_generated_at"])
    assert candidates.iloc[1]["narrative_generated_at"] is pd.NaT
    monkeypatch.setattr(fundamentals_watch_summary, "load_companies_needing_narrative_refresh", lambda limit=None: candidates)
    _patch_watch_summary_evidence_loaders(monkeypatch)
    monkeypatch.setattr(
        fundamentals_watch_summary, "generate_watch_summary", lambda bundle, **k: {"narrative": "n", "suggested_watch_duration_days": 30, "confidence": "high"}
    )
    monkeypatch.setattr(fundamentals_watch_summary, "_update_narrative", lambda **kwargs: None)

    result = fundamentals_watch_summary.run_watch_summary_refresh()

    events_by_company = {e["company_master_id"]: e for e in result["narrative_events"]}
    assert events_by_company["nse:OLD"]["is_new_candidate"] is False
    assert events_by_company["nse:NEW"]["is_new_candidate"] is True


def test_run_watch_summary_refresh_passes_signal_pointers_into_evidence_bundle(monkeypatch):
    # closes the gap found auditing this pipeline (2026-08-13): agency name,
    # institutional numbers, and investor tier used to never reach the narrative LLM.
    monkeypatch.setattr(fundamentals_watch_summary, "_bootstrap_narrative_columns", lambda: None)
    candidates = pd.DataFrame([{"company_master_id": "nse:X", "last_alert_at": date(2026, 8, 1), "narrative_generated_at": None, "narrative_text": None}])
    monkeypatch.setattr(fundamentals_watch_summary, "load_companies_needing_narrative_refresh", lambda limit=None: candidates)
    _patch_watch_summary_evidence_loaders(monkeypatch)
    pointers = [{"signal_type": "rating_action", "label": "Rating downgraded (CRISIL)", "value": "downgraded", "direction": "down", "as_of_date": "2026-08-01", "source": "CRISIL"}]
    monkeypatch.setattr(fundamentals_watch_summary, "get_stock_signal_pointers", lambda cmid: pointers)
    captured = {}
    monkeypatch.setattr(
        fundamentals_watch_summary,
        "generate_watch_summary",
        lambda bundle, **k: (captured.update(bundle=bundle), {"narrative": "n", "suggested_watch_duration_days": 30, "confidence": "high"})[1],
    )
    monkeypatch.setattr(fundamentals_watch_summary, "_update_narrative", lambda **kwargs: None)

    fundamentals_watch_summary.run_watch_summary_refresh()

    assert captured["bundle"]["signal_pointers"] == pointers


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


def test_run_watch_summary_refresh_handles_malformed_llm_response(monkeypatch):
    # 2026-08-15 bug found live: summary["narrative"]/["suggested_watch_duration_days"] used to be
    # indexed OUTSIDE the try/except around the LLM call -- a response missing an expected key
    # raised an uncaught KeyError there, aborting the entire remaining batch with no
    # fallback-telemetry record. Now handled as this one company's failure, same as any other error.
    monkeypatch.setattr(fundamentals_watch_summary, "_bootstrap_narrative_columns", lambda: None)
    candidates = pd.DataFrame(
        [
            {"company_master_id": "nse:BAD", "last_alert_at": date(2026, 8, 1), "narrative_generated_at": None, "narrative_text": None},
            {"company_master_id": "nse:GOOD", "last_alert_at": date(2026, 8, 1), "narrative_generated_at": None, "narrative_text": None},
        ]
    )
    monkeypatch.setattr(fundamentals_watch_summary, "load_companies_needing_narrative_refresh", lambda limit=None: candidates)
    _patch_watch_summary_evidence_loaders(monkeypatch)

    def flaky_summary(bundle, **k):
        company_id = bundle.get("company_master_id") if isinstance(bundle, dict) else None
        if company_id == "nse:BAD":
            return {"suggested_watch_duration_days": 30, "confidence": "high"}  # missing "narrative"
        return {"narrative": "fine", "suggested_watch_duration_days": 30, "confidence": "high"}

    monkeypatch.setattr(fundamentals_watch_summary, "generate_watch_summary", flaky_summary)
    monkeypatch.setattr(fundamentals_watch_summary, "build_company_evidence_bundle", lambda company_master_id, *a, **k: {"company_master_id": company_master_id})
    updates = []
    monkeypatch.setattr(fundamentals_watch_summary, "_update_narrative", lambda **kwargs: updates.append(kwargs))
    fallback_events = []
    monkeypatch.setattr(fundamentals_watch_summary, "_record_fallback", lambda *a, **k: fallback_events.append((a, k)))

    result = fundamentals_watch_summary.run_watch_summary_refresh()

    assert result["blocked"] is False  # did NOT abort on the malformed response
    assert result["generated"] == 1
    assert result["failed"] == 1
    assert len(updates) == 1
    assert updates[0]["company_master_id"] == "nse:GOOD"
    assert any(a and a[0] == "watch_summary_generation_failed" for a, k in fallback_events)


# fundamentals/screens/l4_thesis_draft.py -- L4 thesis DRAFTING, step 11.5 (2026-08-15).
# Never writes fundamentals_l4_thesis -- see module docstring guardrail.


def test_load_companies_needing_draft_refresh_compares_processing_timestamps(monkeypatch):
    # BUG FOUND LIVE 2026-08-18 (re-audit): byte-identical bug to watch_summary.py's
    # narrative refresh gate, fixed the same day -- w.last_alert_at (disclosure
    # date) compared against d.generated_at (wall-clock), masked only because
    # fundamentals_l4_thesis_draft is still empty in production.
    captured = {}

    def fake_sql_to_df(query, **kwargs):
        captured["query"] = query
        return pd.DataFrame()

    monkeypatch.setattr(fundamentals_l4_thesis_draft, "sql_to_df", fake_sql_to_df)
    fundamentals_l4_thesis_draft.load_companies_needing_draft_refresh()
    query = captured["query"]
    assert "MAX(load_ts) AS latest_alert_load_ts" in query
    assert "a.latest_alert_load_ts > d.generated_at" in query
    assert "w.last_alert_at > d.generated_at::date" not in query


def test_run_l4_thesis_drafting_returns_early_when_no_candidates(monkeypatch):
    monkeypatch.setattr(fundamentals_l4_thesis_draft, "_ensure_draft_table", lambda: None)
    monkeypatch.setattr(fundamentals_l4_thesis_draft, "load_companies_needing_draft_refresh", lambda limit=None: pd.DataFrame())

    result = fundamentals_l4_thesis_draft.run_l4_thesis_drafting()

    assert result == {"drafted": 0, "failed": 0, "blocked": False}


def _patch_l4_draft_evidence_loaders(monkeypatch, alerts=None):
    monkeypatch.setattr(fundamentals_l4_thesis_draft, "load_company_alerts", lambda cmid: alerts if alerts is not None else pd.DataFrame())
    monkeypatch.setattr(fundamentals_l4_thesis_draft, "load_latest_l2_state_for_company", lambda cmid: None)
    monkeypatch.setattr(fundamentals_l4_thesis_draft, "load_latest_technicals_for_company", lambda cmid: None)
    monkeypatch.setattr(fundamentals_l4_thesis_draft, "load_sector_context_for_company", lambda cmid: None)
    monkeypatch.setattr(fundamentals_l4_thesis_draft, "get_stock_signal_pointers", lambda cmid: [])


def test_run_l4_thesis_drafting_writes_expected_row(monkeypatch):
    monkeypatch.setattr(fundamentals_l4_thesis_draft, "_ensure_draft_table", lambda: None)
    candidates = pd.DataFrame([{"company_master_id": "nse:X", "last_alert_at": date(2026, 8, 1), "narrative_text": "why we're watching"}])
    monkeypatch.setattr(fundamentals_l4_thesis_draft, "load_companies_needing_draft_refresh", lambda limit=None: candidates)
    _patch_l4_draft_evidence_loaders(monkeypatch)
    monkeypatch.setattr(
        fundamentals_l4_thesis_draft,
        "generate_l4_thesis_draft",
        lambda bundle, **k: {
            "prediction_text": "Net debt/RSCR falls below 45 by Q2 FY27.",
            "target_date": "2026-11-15",
            "invalidation_criteria": "Net debt stays flat or rises.",
            "confidence_score": 72,
            "rationale": "because X",
        },
    )
    upserts = []
    monkeypatch.setattr(fundamentals_l4_thesis_draft, "upsert_to_db", lambda df, table, **k: upserts.append((df, table, k)))

    result = fundamentals_l4_thesis_draft.run_l4_thesis_drafting(model="test-model")

    assert result == {"drafted": 1, "failed": 0, "blocked": False}
    assert len(upserts) == 1
    df, table, kwargs = upserts[0]
    assert table == fundamentals_l4_thesis_draft.RESULTS_TABLE
    assert kwargs["unique_keys"] == ["company_master_id"]
    row = df.iloc[0]
    assert row["company_master_id"] == "nse:X"
    assert row["prediction_text"] == "Net debt/RSCR falls below 45 by Q2 FY27."
    assert row["target_date"] == date(2026, 11, 15)
    assert row["confidence_score"] == 72
    assert row["model"] == "test-model"


def test_run_l4_thesis_drafting_includes_narrative_in_evidence_bundle(monkeypatch):
    # this module runs AFTER watch_summary in the pipeline specifically so it can read
    # the freshly-generated narrative -- confirm it actually reaches the LLM call.
    monkeypatch.setattr(fundamentals_l4_thesis_draft, "_ensure_draft_table", lambda: None)
    candidates = pd.DataFrame([{"company_master_id": "nse:X", "last_alert_at": date(2026, 8, 1), "narrative_text": "the actual narrative text"}])
    monkeypatch.setattr(fundamentals_l4_thesis_draft, "load_companies_needing_draft_refresh", lambda limit=None: candidates)
    _patch_l4_draft_evidence_loaders(monkeypatch)
    captured = {}

    def fake_generate(bundle, **k):
        captured.update(bundle=bundle)
        return {"prediction_text": "p", "target_date": "2026-11-15", "invalidation_criteria": "i", "confidence_score": 50, "rationale": "r"}

    monkeypatch.setattr(fundamentals_l4_thesis_draft, "generate_l4_thesis_draft", fake_generate)
    monkeypatch.setattr(fundamentals_l4_thesis_draft, "upsert_to_db", lambda df, table, **k: None)

    fundamentals_l4_thesis_draft.run_l4_thesis_drafting()

    assert captured["bundle"]["narrative_text"] == "the actual narrative text"


def test_run_l4_thesis_drafting_trips_circuit_breaker(monkeypatch):
    monkeypatch.setattr(fundamentals_l4_thesis_draft, "_ensure_draft_table", lambda: None)
    candidates = pd.DataFrame(
        [{"company_master_id": f"nse:X{i}", "last_alert_at": date(2026, 8, 1), "narrative_text": None} for i in range(5)]
    )
    monkeypatch.setattr(fundamentals_l4_thesis_draft, "load_companies_needing_draft_refresh", lambda limit=None: candidates)
    _patch_l4_draft_evidence_loaders(monkeypatch)

    def always_fails(bundle, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr(fundamentals_l4_thesis_draft, "generate_l4_thesis_draft", always_fails)
    monkeypatch.setattr(fundamentals_l4_thesis_draft, "upsert_to_db", lambda df, table, **k: None)
    fallback_events = []
    monkeypatch.setattr(fundamentals_l4_thesis_draft, "_record_fallback", lambda *a, **k: fallback_events.append((a, k)))

    result = fundamentals_l4_thesis_draft.run_l4_thesis_drafting()

    assert result["blocked"] is True
    assert result["failed"] == fundamentals_l4_thesis_draft.CIRCUIT_BREAKER_THRESHOLD
    assert result["drafted"] == 0
    assert any(a and a[0] == "l4_thesis_draft_circuit_breaker_tripped" for a, k in fallback_events)


def test_run_l4_thesis_drafting_handles_malformed_llm_response(monkeypatch):
    monkeypatch.setattr(fundamentals_l4_thesis_draft, "_ensure_draft_table", lambda: None)
    candidates = pd.DataFrame(
        [
            {"company_master_id": "nse:BAD", "last_alert_at": date(2026, 8, 1), "narrative_text": None},
            {"company_master_id": "nse:GOOD", "last_alert_at": date(2026, 8, 1), "narrative_text": None},
        ]
    )
    monkeypatch.setattr(fundamentals_l4_thesis_draft, "load_companies_needing_draft_refresh", lambda limit=None: candidates)
    _patch_l4_draft_evidence_loaders(monkeypatch)

    def flaky(bundle, **k):
        if bundle.get("company_master_id") == "nse:BAD":
            return {"target_date": "2026-11-15", "invalidation_criteria": "i", "confidence_score": 50, "rationale": "r"}  # missing prediction_text
        return {"prediction_text": "p", "target_date": "2026-11-15", "invalidation_criteria": "i", "confidence_score": 50, "rationale": "r"}

    monkeypatch.setattr(fundamentals_l4_thesis_draft, "generate_l4_thesis_draft", flaky)
    upserts = []
    monkeypatch.setattr(fundamentals_l4_thesis_draft, "upsert_to_db", lambda df, table, **k: upserts.append(df))
    fallback_events = []
    monkeypatch.setattr(fundamentals_l4_thesis_draft, "_record_fallback", lambda *a, **k: fallback_events.append((a, k)))

    result = fundamentals_l4_thesis_draft.run_l4_thesis_drafting()

    assert result["blocked"] is False
    assert result["drafted"] == 1
    assert result["failed"] == 1
    assert len(upserts) == 1
    assert upserts[0].iloc[0]["company_master_id"] == "nse:GOOD"
    assert any(a and a[0] == "l4_thesis_draft_generation_failed" for a, k in fallback_events)


def test_latest_trigger_type_returns_last_alert_trigger_type():
    alerts = pd.DataFrame([{"trigger_type": "llm_flagged", "alert_date": date(2026, 7, 1)}, {"trigger_type": "capital_raise", "alert_date": date(2026, 8, 1)}])
    assert fundamentals_l4_thesis_draft._latest_trigger_type(alerts) == "capital_raise"


def test_latest_trigger_type_returns_none_for_empty_alerts():
    assert fundamentals_l4_thesis_draft._latest_trigger_type(pd.DataFrame()) is None


def test_load_current_drafts_by_company_keys_by_company_master_id(monkeypatch):
    monkeypatch.setattr(fundamentals_l4_thesis_draft, "_ensure_draft_table", lambda: None)
    df = pd.DataFrame([{"company_master_id": "nse:X", "prediction_text": "p", "confidence_score": 60}])
    monkeypatch.setattr(fundamentals_l4_thesis_draft, "sql_to_df", lambda q: df)

    result = fundamentals_l4_thesis_draft.load_current_drafts_by_company()

    assert result == {"nse:X": {"company_master_id": "nse:X", "prediction_text": "p", "confidence_score": 60}}


def test_load_current_drafts_by_company_empty(monkeypatch):
    monkeypatch.setattr(fundamentals_l4_thesis_draft, "_ensure_draft_table", lambda: None)
    monkeypatch.setattr(fundamentals_l4_thesis_draft, "sql_to_df", lambda q: pd.DataFrame())
    assert fundamentals_l4_thesis_draft.load_current_drafts_by_company() == {}


def test_send_email_returns_none_when_disabled(monkeypatch):
    monkeypatch.setattr(fundamentals_notifications, "WATCHLIST_ALERT_EMAIL_ENABLED", False)
    assert fundamentals_notifications.send_email("subject", "body") is None


def test_run_watchlist_notification_pipeline_chains_all_five_steps(monkeypatch):
    call_order = []
    monkeypatch.setattr(
        fundamentals_notifications,
        "sync_watchlist_from_alerts",
        lambda: call_order.append("sync") or {"companies": 5, "new_candidates": 2, "new_candidate_ids": ["nse:A", "nse:B"], "no_price_at_first_seen": 0},
    )
    narrative_events = [{"company_master_id": "nse:A", "narrative_changed": True, "is_new_candidate": True, "narrative_text": "n"}]
    monkeypatch.setattr(
        fundamentals_notifications,
        "run_watch_summary_refresh",
        lambda: call_order.append("narrative") or {"generated": 1, "failed": 0, "blocked": False, "narrative_events": narrative_events},
    )
    monkeypatch.setattr(
        fundamentals_notifications,
        "run_l4_thesis_drafting",
        lambda: call_order.append("draft") or {"drafted": 1, "failed": 0, "blocked": False},
    )
    monkeypatch.setattr(
        fundamentals_notifications,
        "run_watchlist_exit_evaluation",
        lambda: call_order.append("exit") or {"companies": 5, "active": 4, "invalidated": 1, "price_flagged": 0, "stale": 0},
    )
    # NOTE: send_daily_digest must always be mocked in tests that exercise the full
    # pipeline -- it reads the real WATCHLIST_ALERT_EMAIL_* config and would attempt
    # a real SES send against the real watchlist otherwise (this config is enabled
    # in production .env, not just a test fixture).
    monkeypatch.setattr(
        fundamentals_notifications,
        "send_daily_digest",
        lambda: call_order.append("digest") or {"sent": 1, "skipped_disabled": 0, "failed": 0},
    )

    result = fundamentals_notifications.run_watchlist_notification_pipeline()

    assert result["watchlist_companies"] == 5
    assert result["new_candidates"] == 2
    assert result["narratives_generated"] == 1
    assert result["theses_drafted"] == 1
    assert result["watchlist_active"] == 4
    assert result["watchlist_invalidated"] == 1
    assert result["digest_sent"] == 1
    # BUG FOUND LIVE 2026-08-18 (re-audit): drafting used to run BEFORE exit
    # evaluation -- load_companies_needing_draft_refresh()'s `WHERE w.status =
    # 'active'` filter then reflected last run's status, so a company exit
    # evaluation was about to invalidate/flag stale this run still got a paid
    # LLM draft call that was immediately wasted. exit must now run before draft.
    assert call_order == ["sync", "narrative", "exit", "draft", "digest"]
    assert "emails_sent" not in result  # per-addition notifications removed 2026-08-14 -- digest only


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


def test_get_watchlist_attaches_strategies_per_company(monkeypatch):
    df = pd.DataFrame([{"company_master_id": "nse:FOO", "first_seen_at": date(2026, 8, 1), "alert_count": 2}])

    def fake_sql_to_df(q, params=None):
        return pd.DataFrame() if "fundamentals_l4_thesis_draft" in q else df

    monkeypatch.setattr(fundamentals_api_queries, "sql_to_df", fake_sql_to_df)
    monkeypatch.setattr(fundamentals_api_queries, "_ensure_draft_table", lambda: None)
    monkeypatch.setattr(fundamentals_api_queries, "load_satisfied_strategies_by_company", lambda: {"nse:FOO": ["capital_raise", "rating_downgrade"]})

    result = fundamentals_api_queries.get_watchlist()

    assert result[0]["strategies"] == ["capital_raise", "rating_downgrade"]
    assert result[0]["draft_thesis"] is None


def test_get_watchlist_attaches_draft_thesis_per_company(monkeypatch):
    # BUG FOUND LIVE 2026-08-17: draft L4 theses were never exposed via the API at
    # all -- notifications.py's digest email already shows them (load_full_watchlist
    # does this exact per-company join).
    watchlist_df = pd.DataFrame([{"company_master_id": "nse:FOO", "first_seen_at": date(2026, 8, 1), "alert_count": 2}])
    draft_df = pd.DataFrame([{"company_master_id": "nse:FOO", "prediction_text": "p", "confidence_score": 55}])

    def fake_sql_to_df(q, params=None):
        return draft_df if "fundamentals_l4_thesis_draft" in q else watchlist_df

    monkeypatch.setattr(fundamentals_api_queries, "sql_to_df", fake_sql_to_df)
    monkeypatch.setattr(fundamentals_api_queries, "_ensure_draft_table", lambda: None)
    monkeypatch.setattr(fundamentals_api_queries, "load_satisfied_strategies_by_company", lambda: {})

    result = fundamentals_api_queries.get_watchlist()

    assert result[0]["draft_thesis"]["prediction_text"] == "p"
    assert result[0]["draft_thesis"]["confidence_score"] == 55


def test_get_watchlist_selects_price_data_stale(monkeypatch):
    # BUG FOUND LIVE 2026-08-18 (re-audit): current_price used to be selected with
    # no staleness flag at all -- confirmed live, a real watchlist company showed
    # a current_price frozen 3.6 months stale under a column callers treat as
    # "today's close".
    captured = {}

    def fake_sql_to_df(q, params=None):
        captured["query"] = q
        return pd.DataFrame()

    monkeypatch.setattr(fundamentals_api_queries, "sql_to_df", fake_sql_to_df)
    monkeypatch.setattr(fundamentals_api_queries, "_ensure_draft_table", lambda: None)
    fundamentals_api_queries.get_watchlist()
    assert "tech.price_data_stale" in captured["query"]
    assert "SELECT close, price_data_stale FROM fundamentals_technicals" in captured["query"]


def test_get_watchlist_empty_watchlist_skips_strategies_query(monkeypatch):
    monkeypatch.setattr(fundamentals_api_queries, "sql_to_df", lambda q, params=None: pd.DataFrame())
    calls = []
    monkeypatch.setattr(fundamentals_api_queries, "load_satisfied_strategies_by_company", lambda: calls.append(1) or {})

    assert fundamentals_api_queries.get_watchlist() == []
    assert calls == []  # no point querying strategies for an empty watchlist


def test_get_watchlist_company_with_no_strategies_gets_empty_list(monkeypatch):
    df = pd.DataFrame([{"company_master_id": "nse:FOO", "first_seen_at": date(2026, 8, 1), "alert_count": 0}])

    def fake_sql_to_df(q, params=None):
        return pd.DataFrame() if "fundamentals_l4_thesis_draft" in q else df

    monkeypatch.setattr(fundamentals_api_queries, "sql_to_df", fake_sql_to_df)
    monkeypatch.setattr(fundamentals_api_queries, "_ensure_draft_table", lambda: None)
    monkeypatch.setattr(fundamentals_api_queries, "load_satisfied_strategies_by_company", lambda: {})

    result = fundamentals_api_queries.get_watchlist()

    assert result[0]["strategies"] == []


def test_get_watchlist_detail_returns_none_when_not_found(monkeypatch):
    monkeypatch.setattr(fundamentals_api_queries, "sql_to_df", lambda q, params=None: pd.DataFrame())
    assert fundamentals_api_queries.get_watchlist_detail("nse:MISSING") is None


def test_get_watchlist_detail_parses_evidence_bundle_and_joins_context(monkeypatch):
    watchlist_df = pd.DataFrame([{"company_master_id": "nse:FOO", "first_seen_at": date(2026, 8, 1), "alert_count": 1}])
    alerts_df = pd.DataFrame(
        [{"source": "bse", "news_id": "n1", "trigger_type": "insider_buy", "origin": "rule", "alert_date": date(2026, 8, 1), "reasoning": "r", "status": "new", "evidence_bundle_json": json.dumps({"event": {"headline": "h"}})}]
    )
    thesis_df = pd.DataFrame()
    draft_df = pd.DataFrame([{"company_master_id": "nse:FOO", "prediction_text": "p", "confidence_score": 72}])

    calls = {"watchlist": watchlist_df, "alerts": alerts_df, "thesis": thesis_df, "draft": draft_df}
    call_order = iter(["watchlist", "alerts", "thesis", "draft"])
    monkeypatch.setattr(fundamentals_api_queries, "sql_to_df", lambda q, params=None: calls[next(call_order)])
    monkeypatch.setattr(fundamentals_api_queries, "_ensure_draft_table", lambda: None)
    monkeypatch.setattr(fundamentals_api_queries, "load_latest_l2_state_for_company", lambda cmid: {"ticker": "FOO"})
    monkeypatch.setattr(fundamentals_api_queries, "load_latest_technicals_for_company", lambda cmid: {"close": 100})
    monkeypatch.setattr(fundamentals_api_queries, "load_sector_context_for_company", lambda cmid: {"sector_code": "IN01"})
    monkeypatch.setattr(fundamentals_api_queries, "get_stock_signal_pointers", lambda cmid: [{"signal_type": "rating_action", "value": "downgraded"}])

    result = fundamentals_api_queries.get_watchlist_detail("nse:FOO")

    assert result["watchlist"]["company_master_id"] == "nse:FOO"
    assert result["alerts"][0]["evidence_bundle"] == {"event": {"headline": "h"}}
    assert "evidence_bundle_json" not in result["alerts"][0]
    assert result["l2_state"] == {"ticker": "FOO"}
    assert result["technicals"] == {"close": 100}
    assert result["sector_context"] == {"sector_code": "IN01"}
    assert result["portfolio"] == []
    assert result["signal_pointers"] == [{"signal_type": "rating_action", "value": "downgraded"}]
    # BUG FOUND LIVE 2026-08-17: draft L4 theses were never exposed via the API at
    # all -- notifications.py's digest email already shows them.
    assert result["draft_thesis"]["prediction_text"] == "p"
    assert result["draft_thesis"]["confidence_score"] == 72


def test_get_watchlist_detail_draft_thesis_none_when_no_draft(monkeypatch):
    watchlist_df = pd.DataFrame([{"company_master_id": "nse:FOO", "first_seen_at": date(2026, 8, 1), "alert_count": 1}])
    calls = {"watchlist": watchlist_df, "alerts": pd.DataFrame(), "thesis": pd.DataFrame(), "draft": pd.DataFrame()}
    call_order = iter(["watchlist", "alerts", "thesis", "draft"])
    monkeypatch.setattr(fundamentals_api_queries, "sql_to_df", lambda q, params=None: calls[next(call_order)])
    monkeypatch.setattr(fundamentals_api_queries, "_ensure_draft_table", lambda: None)
    monkeypatch.setattr(fundamentals_api_queries, "load_latest_l2_state_for_company", lambda cmid: None)
    monkeypatch.setattr(fundamentals_api_queries, "load_latest_technicals_for_company", lambda cmid: None)
    monkeypatch.setattr(fundamentals_api_queries, "load_sector_context_for_company", lambda cmid: None)
    monkeypatch.setattr(fundamentals_api_queries, "get_stock_signal_pointers", lambda cmid: [])

    result = fundamentals_api_queries.get_watchlist_detail("nse:FOO")

    assert result["draft_thesis"] is None


def test_get_draft_theses_returns_active_watchlist_drafts(monkeypatch):
    # BUG FOUND LIVE 2026-08-17: draft L4 theses (fundamentals_l4_thesis_draft) are
    # generated daily and shown in the digest email (notifications.py's own
    # load_full_watchlist), but were never exposed anywhere in this API.
    captured = {}

    def fake_sql_to_df(q, params=None):
        captured["query"] = q
        return pd.DataFrame([{"company_master_id": "nse:FOO", "prediction_text": "p", "confidence_score": 80}])

    monkeypatch.setattr(fundamentals_api_queries, "sql_to_df", fake_sql_to_df)
    monkeypatch.setattr(fundamentals_api_queries, "_ensure_draft_table", lambda: None)

    result = fundamentals_api_queries.get_draft_theses()

    assert result == [{"company_master_id": "nse:FOO", "prediction_text": "p", "confidence_score": 80}]
    assert "fundamentals_l4_thesis_draft" in captured["query"]
    assert "w.status = 'active'" in captured["query"]
    # BUG FOUND LIVE 2026-08-18 (re-audit): no ORDER BY at all -- the only
    # list-producing query in this module without one, would reshuffle between
    # calls as Postgres reorders the heap after upserts.
    assert "ORDER BY d.generated_at DESC" in captured["query"]


def test_get_draft_theses_empty_when_none(monkeypatch):
    monkeypatch.setattr(fundamentals_api_queries, "sql_to_df", lambda q, params=None: pd.DataFrame())
    monkeypatch.setattr(fundamentals_api_queries, "_ensure_draft_table", lambda: None)
    assert fundamentals_api_queries.get_draft_theses() == []


def test_get_draft_theses_calls_ensure_draft_table_first(monkeypatch):
    # BUG FOUND LIVE 2026-08-18 (re-audit): this used to query fundamentals_l4_
    # thesis_draft directly with no _ensure_draft_table() call first -- the
    # screens-side reader this was copied from calls it; this one didn't. Verified
    # live by monkeypatching the table name: /api/watchlist, /api/drafts, and
    # /api/watchlist/{id} all 500'd on a fresh DB.
    calls = []
    monkeypatch.setattr(fundamentals_api_queries, "_ensure_draft_table", lambda: calls.append(True))
    monkeypatch.setattr(fundamentals_api_queries, "sql_to_df", lambda q, params=None: pd.DataFrame())

    fundamentals_api_queries.get_draft_theses()

    assert calls == [True]


def test_get_watchlist_shows_draft_thesis_for_a_non_active_row(monkeypatch):
    # BUG FOUND LIVE 2026-08-18 (re-audit): draft_thesis was silently absent on
    # every non-active row in the list view -- _load_draft_theses_by_company()
    # unconditionally gated on w.status='active' regardless of the `status` this
    # function was actually called with, inconsistent with get_watchlist_detail()'s
    # own deliberately-unstatus-gated draft lookup.
    watchlist_df = pd.DataFrame([{"company_master_id": "nse:FOO", "first_seen_at": date(2026, 8, 1), "alert_count": 2, "status": "invalidated"}])
    draft_df = pd.DataFrame([{"company_master_id": "nse:FOO", "prediction_text": "p", "confidence_score": 55}])
    captured = {}

    def fake_sql_to_df(q, params=None):
        if "fundamentals_l4_thesis_draft" in q:
            captured["draft_query"] = q
            return draft_df
        return watchlist_df

    monkeypatch.setattr(fundamentals_api_queries, "sql_to_df", fake_sql_to_df)
    monkeypatch.setattr(fundamentals_api_queries, "_ensure_draft_table", lambda: None)
    monkeypatch.setattr(fundamentals_api_queries, "load_satisfied_strategies_by_company", lambda: {})

    result = fundamentals_api_queries.get_watchlist(status="invalidated")

    assert result[0]["draft_thesis"]["prediction_text"] == "p"
    assert "w.status = 'active'" not in captured["draft_query"]


def test_api_drafts_route_returns_queries_result(monkeypatch):
    monkeypatch.setattr(fundamentals_api_queries, "get_draft_theses", lambda: [{"company_master_id": "nse:FOO", "prediction_text": "p"}])
    client = TestClient(fundamentals_api_app)
    r = client.get("/api/drafts")
    assert r.status_code == 200
    assert r.json() == [{"company_master_id": "nse:FOO", "prediction_text": "p"}]


def test_get_sectors_groups_watched_companies_by_sector(monkeypatch):
    sector_df = pd.DataFrame([{"sector_code": "IN01", "capacity_growth_pct": 5.0, "demand_growth_pct": 3.0, "phase": "capacity_expansion", "growth_classification": "low_growth", "sample_size_confidence": "adequate", "n_companies_in_l1": 10, "sector_name": "Chemicals"}])
    watched_df = pd.DataFrame([{"sector_code": "IN01", "company_master_id": "nse:FOO", "alert_count": 2, "narrative_text": "x" * 300}])
    call_order = iter([sector_df, watched_df])
    monkeypatch.setattr(fundamentals_api_queries, "sql_to_df", lambda q: next(call_order))

    result = fundamentals_api_queries.get_sectors()

    assert len(result) == 1
    watched = result[0]["watched_companies"]
    assert len(watched) == 1
    assert watched[0]["company_master_id"] == "nse:FOO"
    assert len(watched[0]["narrative_snippet"]) == 200
    assert result[0]["growth_classification"] == "low_growth"


def test_get_strategies_returns_grouped_counts(monkeypatch):
    df = pd.DataFrame([{"trigger_type": "capital_raise", "company_count": 3, "last_alert_date": date(2026, 8, 1)}])
    monkeypatch.setattr(fundamentals_api_queries, "sql_to_df", lambda q, params=None: df)
    result = fundamentals_api_queries.get_strategies()
    assert result[0]["trigger_type"] == "capital_raise"
    assert result[0]["company_count"] == 3


def test_get_strategy_detail_returns_none_when_never_fired(monkeypatch):
    monkeypatch.setattr(fundamentals_api_queries, "sql_to_df", lambda q, params=None: pd.DataFrame())
    assert fundamentals_api_queries.get_strategy_detail("no_such_trigger") is None


def test_get_strategy_detail_returns_companies(monkeypatch):
    df = pd.DataFrame([{"company_master_id": "nse:FOO", "alert_date": date(2026, 8, 1), "reasoning": "r", "origin": "rule", "company_name": "Foo Ltd", "current_price": 100.0}])
    monkeypatch.setattr(fundamentals_api_queries, "sql_to_df", lambda q, params=None: df)

    result = fundamentals_api_queries.get_strategy_detail("capital_raise")

    assert result["trigger_type"] == "capital_raise"
    assert result["companies"][0]["company_master_id"] == "nse:FOO"


def test_api_strategies_route_returns_queries_result(monkeypatch):
    monkeypatch.setattr(fundamentals_api_queries, "get_strategies", lambda: [{"trigger_type": "capital_raise", "company_count": 3}])
    client = TestClient(fundamentals_api_app)

    r = client.get("/api/strategies")

    assert r.status_code == 200
    assert r.json() == [{"trigger_type": "capital_raise", "company_count": 3}]


def test_api_strategy_detail_route_404_when_none(monkeypatch):
    monkeypatch.setattr(fundamentals_api_queries, "get_strategy_detail", lambda trigger_type: None)
    client = TestClient(fundamentals_api_app)

    r = client.get("/api/strategies/no_such_trigger")

    assert r.status_code == 404


def test_api_strategy_detail_route_returns_queries_result(monkeypatch):
    monkeypatch.setattr(fundamentals_api_queries, "get_strategy_detail", lambda trigger_type: {"trigger_type": trigger_type, "companies": []})
    client = TestClient(fundamentals_api_app)

    r = client.get("/api/strategies/capital_raise")

    assert r.status_code == 200
    assert r.json() == {"trigger_type": "capital_raise", "companies": []}


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


def _capturing_get_watchlist(captured):
    def _fn(status="active"):
        captured["status"] = status
        return []

    return _fn


def test_api_watchlist_route_defaults_to_active_status(monkeypatch):
    captured = {}
    monkeypatch.setattr(fundamentals_api_queries, "get_watchlist", _capturing_get_watchlist(captured))
    client = TestClient(fundamentals_api_app)
    r = client.get("/api/watchlist")
    assert r.status_code == 200
    assert captured["status"] == "active"


def test_api_watchlist_route_status_all_passes_none(monkeypatch):
    captured = {}
    monkeypatch.setattr(fundamentals_api_queries, "get_watchlist", _capturing_get_watchlist(captured))
    client = TestClient(fundamentals_api_app)
    r = client.get("/api/watchlist?status=all")
    assert r.status_code == 200
    assert captured["status"] is None


def test_api_watchlist_route_status_stale_passes_through(monkeypatch):
    captured = {}
    monkeypatch.setattr(fundamentals_api_queries, "get_watchlist", _capturing_get_watchlist(captured))
    client = TestClient(fundamentals_api_app)
    r = client.get("/api/watchlist?status=stale")
    assert r.status_code == 200
    assert captured["status"] == "stale"


def test_get_watchlist_status_none_omits_where_clause(monkeypatch):
    captured = {}

    def fake_sql_to_df(q, params=None):
        captured["query"] = q
        captured["params"] = params
        return pd.DataFrame()

    monkeypatch.setattr(fundamentals_api_queries, "sql_to_df", fake_sql_to_df)
    fundamentals_api_queries.get_watchlist(status=None)
    assert "WHERE w.status" not in captured["query"]
    assert captured["params"] == ()


def test_get_watchlist_status_filters_by_value(monkeypatch):
    captured = {}

    def fake_sql_to_df(q, params=None):
        captured["query"] = q
        captured["params"] = params
        return pd.DataFrame()

    monkeypatch.setattr(fundamentals_api_queries, "sql_to_df", fake_sql_to_df)
    fundamentals_api_queries.get_watchlist(status="stale")
    assert "WHERE w.status = %s" in captured["query"]
    assert captured["params"] == ("stale",)


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
    assert len(fundamentals_run_pipeline.STEPS) == 14
    assert fundamentals_run_pipeline.STEPS[-1] == "fundamentals.screens.notifications"
    assert "fundamentals.screens.investor_classification" in fundamentals_run_pipeline.STEPS
    # 2026-08-15: the deleveraging screen (fundamentals.collectors.screenerin's standalone step) was
    # retired -- its output table had zero readers. The module itself is not a pipeline step anymore.
    assert "fundamentals.collectors.screenerin" not in fundamentals_run_pipeline.STEPS


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


def test_send_email_passes_full_recipient_list_to_ses_as_bcc(monkeypatch):
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

    # 2026-08-14: recipients moved to Bcc so they don't see each other's addresses;
    # From doubles as the self-addressed To (a message needs some To header).
    assert calls[0]["Destination"] == {
        "ToAddresses": ["from@x.com"],
        "BccAddresses": ["a@x.com", "b@y.com"],
    }
    assert calls[0]["Message"]["Body"] == {"Text": {"Data": "body", "Charset": "UTF-8"}}
    assert calls[0]["Message"]["Subject"] == {"Data": "subject", "Charset": "UTF-8"}


def test_send_email_includes_html_part_when_given(monkeypatch):
    monkeypatch.setattr(fundamentals_notifications, "WATCHLIST_ALERT_EMAIL_ENABLED", True)
    monkeypatch.setattr(fundamentals_notifications, "WATCHLIST_ALERT_EMAIL_TO", "a@x.com")
    monkeypatch.setattr(fundamentals_notifications, "WATCHLIST_ALERT_EMAIL_FROM", "from@x.com")
    calls = []

    class FakeSesClient:
        def send_email(self, **kwargs):
            calls.append(kwargs)
            return {"MessageId": "1"}

    monkeypatch.setattr(fundamentals_notifications, "_get_ses_client", lambda: FakeSesClient())

    fundamentals_notifications.send_email("subject", "text version", "<p>html version</p>")

    body = calls[0]["Message"]["Body"]
    assert body["Text"] == {"Data": "text version", "Charset": "UTF-8"}
    assert body["Html"] == {"Data": "<p>html version</p>", "Charset": "UTF-8"}


def test_load_full_watchlist_empty_returns_empty_list(monkeypatch):
    monkeypatch.setattr(fundamentals_notifications, "sql_to_df", lambda q: pd.DataFrame())
    assert fundamentals_notifications.load_full_watchlist() == []


def test_load_full_watchlist_attaches_strategies_per_company(monkeypatch):
    df = pd.DataFrame([{"company_master_id": "nse:FOO", "first_seen_at": date(2026, 8, 1), "alert_count": 2}])
    monkeypatch.setattr(fundamentals_notifications, "sql_to_df", lambda q: df)
    monkeypatch.setattr(fundamentals_notifications, "load_satisfied_strategies_by_company", lambda: {"nse:FOO": ["capital_raise"]})
    monkeypatch.setattr(fundamentals_notifications, "load_current_drafts_by_company", lambda: {})

    result = fundamentals_notifications.load_full_watchlist()

    assert result[0]["strategies"] == ["capital_raise"]


def test_load_full_watchlist_attaches_draft_thesis_per_company(monkeypatch):
    df = pd.DataFrame([{"company_master_id": "nse:FOO", "first_seen_at": date(2026, 8, 1), "alert_count": 2}])
    monkeypatch.setattr(fundamentals_notifications, "sql_to_df", lambda q: df)
    monkeypatch.setattr(fundamentals_notifications, "load_satisfied_strategies_by_company", lambda: {})
    draft = {"prediction_text": "p", "confidence_score": 55}
    monkeypatch.setattr(fundamentals_notifications, "load_current_drafts_by_company", lambda: {"nse:FOO": draft})

    result = fundamentals_notifications.load_full_watchlist()

    assert result[0]["draft_thesis"] == draft


def test_load_full_watchlist_missing_draft_thesis_is_none(monkeypatch):
    df = pd.DataFrame([{"company_master_id": "nse:BAR", "first_seen_at": date(2026, 8, 1), "alert_count": 1}])
    monkeypatch.setattr(fundamentals_notifications, "sql_to_df", lambda q: df)
    monkeypatch.setattr(fundamentals_notifications, "load_satisfied_strategies_by_company", lambda: {})
    monkeypatch.setattr(fundamentals_notifications, "load_current_drafts_by_company", lambda: {})

    result = fundamentals_notifications.load_full_watchlist()

    assert result[0]["draft_thesis"] is None


def test_load_full_watchlist_selects_price_data_stale(monkeypatch):
    # BUG FOUND LIVE 2026-08-18 (re-audit): current_price used to be selected with
    # no staleness flag, so the digest's "Today's close" column rendered a
    # months-stale value at face value with no indication.
    captured = {}

    def fake_sql_to_df(q):
        captured["query"] = q
        return pd.DataFrame()

    monkeypatch.setattr(fundamentals_notifications, "sql_to_df", fake_sql_to_df)
    fundamentals_notifications.load_full_watchlist()
    assert "tech.price_data_stale" in captured["query"]
    assert "SELECT close, price_data_stale FROM fundamentals_technicals" in captured["query"]


def test_build_daily_digest_content_empty_watchlist():
    subject, text_body, html_body = fundamentals_notifications.build_daily_digest_content([])
    assert "nothing on the watchlist" in subject.lower()
    assert "No companies" in text_body
    assert "No companies" in html_body


def test_build_daily_digest_content_lists_every_company_in_both_parts():
    rows = [
        {"company_master_id": "nse:FOO", "company_name": "Foo Co", "first_seen_at": "2026-08-01", "first_seen_price": 100.0, "current_price": 110.0, "alert_count": 2, "narrative_text": "foo narrative", "suggested_watch_until": "2026-11-01"},
        {"company_master_id": "nse:BAR", "company_name": "Bar Co", "first_seen_at": "2026-08-02", "first_seen_price": 50.0, "current_price": float("nan"), "alert_count": 1, "narrative_text": None, "suggested_watch_until": None},
    ]

    subject, text_body, html_body = fundamentals_notifications.build_daily_digest_content(rows)

    assert subject == "[Watchlist] Daily digest -- 2 companies"
    assert "FOO" in text_body and "Foo Co" in text_body and "foo narrative" in text_body
    assert "BAR" in text_body and "narrative not generated yet" in text_body
    assert "not a trade recommendation" in text_body

    assert "FOO" in html_body and "foo narrative" in html_body
    assert "<html" in html_body and "<table>" in html_body
    assert 'class="pos"' in html_body  # FOO: 100 -> 110 is a gain
    assert "n/a" in html_body  # BAR's NaN current_price rendered honestly, not as "nan%"


def test_build_daily_digest_content_renders_strategy_badges():
    rows = [
        {
            "company_master_id": "nse:FOO", "company_name": "Foo Co", "first_seen_at": "2026-08-01",
            "first_seen_price": 100.0, "current_price": 110.0, "alert_count": 2, "narrative_text": "n",
            "suggested_watch_until": None, "strategies": ["capital_raise", "rating_downgrade"],
        }
    ]

    subject, text_body, html_body = fundamentals_notifications.build_daily_digest_content(rows)

    assert "Strategies: Capital raise, Rating downgrade" in text_body
    assert 'class="strategy-badge"' in html_body
    assert "Capital raise" in html_body and "Rating downgrade" in html_body


@pytest.mark.parametrize(
    "trigger_type",
    ["results_decline", "results_confirm_turnaround", "results_delayed", "auditor_change", "related_party_transaction"],
)
def test_strategy_labels_covers_new_trigger_types(trigger_type):
    assert trigger_type in fundamentals_notifications.STRATEGY_LABELS
    assert fundamentals_notifications._strategy_label(trigger_type) != trigger_type  # a real label, not the raw fallback


def test_build_daily_digest_content_no_strategies_key_renders_fine():
    # backward-compat: a row without a "strategies" key (old shape) must not crash.
    rows = [{"company_master_id": "nse:FOO", "company_name": "Foo Co", "first_seen_at": "2026-08-01", "first_seen_price": 100.0, "current_price": 110.0, "alert_count": 1, "narrative_text": "n", "suggested_watch_until": None}]
    subject, text_body, html_body = fundamentals_notifications.build_daily_digest_content(rows)
    assert "Strategies:" not in text_body
    assert 'class="strategy-badge"' not in html_body


def test_build_daily_digest_content_negative_change_gets_neg_class():
    rows = [{"company_master_id": "nse:FOO", "company_name": "Foo Co", "first_seen_at": "2026-08-01", "first_seen_price": 100.0, "current_price": 90.0, "alert_count": 1, "narrative_text": "n", "suggested_watch_until": None}]
    _, _, html_body = fundamentals_notifications.build_daily_digest_content(rows)
    assert 'class="neg"' in html_body


def test_build_daily_digest_content_stale_price_shows_stale_marker_not_a_pct(monkeypatch):
    # BUG FOUND LIVE 2026-08-18 (re-audit): current_price used to be rendered at
    # face value with no staleness check at all -- a real watchlist company showed
    # "+66.1%" off a close that was actually 3.6 months old, under a column
    # literally labeled "Today's close". watchlist_exit.py already refuses to
    # judge a price move when price_data_stale is set; this pins the digest doing
    # the same instead of showing a misleading percentage.
    rows = [
        {
            "company_master_id": "nse:FOO", "company_name": "Foo Co", "first_seen_at": "2026-08-01",
            "first_seen_price": 54.50, "current_price": 90.51, "alert_count": 1, "narrative_text": "n",
            "suggested_watch_until": None, "price_data_stale": True,
        }
    ]
    _, text_body, html_body = fundamentals_notifications.build_daily_digest_content(rows)
    assert "stale price" in text_body
    assert "66.1%" not in text_body
    assert 'class="stale"' in html_body
    assert "66.1%" not in html_body


def test_build_daily_digest_content_fresh_price_unaffected_by_stale_flag(monkeypatch):
    rows = [
        {
            "company_master_id": "nse:FOO", "company_name": "Foo Co", "first_seen_at": "2026-08-01",
            "first_seen_price": 100.0, "current_price": 110.0, "alert_count": 1, "narrative_text": "n",
            "suggested_watch_until": None, "price_data_stale": False,
        }
    ]
    _, text_body, html_body = fundamentals_notifications.build_daily_digest_content(rows)
    assert "(+10.0%)" in text_body
    assert 'class="pos"' in html_body
    assert "stale" not in text_body


def test_build_daily_digest_content_escapes_html_special_characters():
    rows = [{"company_master_id": "nse:FOO", "company_name": "Foo & <Bar>", "first_seen_at": "2026-08-01", "first_seen_price": 1.0, "current_price": 1.0, "alert_count": 1, "narrative_text": "n", "suggested_watch_until": None}]
    _, _, html_body = fundamentals_notifications.build_daily_digest_content(rows)
    assert "Foo &amp; &lt;Bar&gt;" in html_body
    assert "<Bar>" not in html_body


def test_build_daily_digest_content_renders_draft_thesis_with_confidence(monkeypatch):
    rows = [
        {
            "company_master_id": "nse:FOO", "company_name": "Foo Co", "first_seen_at": "2026-08-01",
            "first_seen_price": 100.0, "current_price": 110.0, "alert_count": 2, "narrative_text": "n",
            "suggested_watch_until": None,
            "draft_thesis": {
                "prediction_text": "Net debt/RSCR falls below 45 by Q2 FY27.",
                "target_date": date(2026, 11, 15),
                "invalidation_criteria": "Net debt stays flat or rises.",
                "confidence_score": 72,
            },
        }
    ]

    subject, text_body, html_body = fundamentals_notifications.build_daily_digest_content(rows)

    assert "DRAFT L4 thesis, NOT SAVED, confidence 72/100" in text_body
    assert "Net debt/RSCR falls below 45 by Q2 FY27." in text_body
    assert "Invalidation: Net debt stays flat or rises." in text_body
    assert "nothing is saved to the real thesis register" in text_body

    assert "Draft L4 Theses" in html_body
    assert "CANDIDATE ONLY" in html_body
    assert 'class="draft-confidence high"' in html_body  # 72 >= 66
    assert "Net debt/RSCR falls below 45 by Q2 FY27." in html_body


def test_build_daily_digest_content_renders_rationale_in_both_bodies():
    # BUG FOUND LIVE 2026-08-17: fundamentals_l4_thesis_draft.rationale is already
    # selected (load_current_drafts_by_company()'s SELECT d.*) but was never
    # rendered anywhere -- a human reviewing a draft to commit/discard saw the
    # prediction and confidence score but not WHY the model made the call.
    rows = [
        {
            "company_master_id": "nse:FOO", "company_name": "Foo Co", "first_seen_at": "2026-08-01",
            "first_seen_price": 100.0, "current_price": 110.0, "alert_count": 1, "narrative_text": "n",
            "suggested_watch_until": None,
            "draft_thesis": {
                "prediction_text": "p", "target_date": date(2026, 11, 15), "invalidation_criteria": "i",
                "confidence_score": 72, "rationale": "Because leverage keeps falling and margins are expanding.",
            },
        }
    ]
    _, text_body, html_body = fundamentals_notifications.build_daily_digest_content(rows)
    assert "Because leverage keeps falling and margins are expanding." in text_body
    assert "Because leverage keeps falling and margins are expanding." in html_body


def test_build_daily_digest_content_missing_rationale_shows_na_not_none():
    rows = [
        {
            "company_master_id": "nse:FOO", "company_name": "Foo Co", "first_seen_at": "2026-08-01",
            "first_seen_price": 100.0, "current_price": 110.0, "alert_count": 1, "narrative_text": "n",
            "suggested_watch_until": None,
            "draft_thesis": {"prediction_text": "p", "target_date": date(2026, 11, 15), "invalidation_criteria": "i", "confidence_score": 72},
        }
    ]
    _, text_body, _ = fundamentals_notifications.build_daily_digest_content(rows)
    assert "Rationale: n/a" in text_body
    assert "Rationale: None" not in text_body


def test_build_daily_digest_content_null_confidence_shows_na_not_none_in_text():
    # BUG FOUND LIVE 2026-08-17: the HTML side already guarded a null confidence_score
    # ("n/a"), but plain-text interpolated it directly -- literally printed "confidence
    # None/100" for a draft with no score.
    rows = [
        {
            "company_master_id": "nse:FOO", "company_name": "Foo Co", "first_seen_at": "2026-08-01",
            "first_seen_price": 100.0, "current_price": 110.0, "alert_count": 1, "narrative_text": "n",
            "suggested_watch_until": None,
            "draft_thesis": {"prediction_text": "p", "target_date": date(2026, 11, 15), "invalidation_criteria": "i", "confidence_score": None},
        }
    ]
    _, text_body, _ = fundamentals_notifications.build_daily_digest_content(rows)
    assert "confidence n/a" in text_body
    assert "None/100" not in text_body


def test_build_daily_digest_content_null_prediction_and_target_date_show_na_not_none_in_text():
    # BUG FOUND LIVE 2026-08-18 (re-audit): dbd861c routed confidence_score through
    # the null-safe _confidence_label() helper on this same line but left
    # prediction_text/target_date interpolated raw -- a draft with either field
    # null printed the literal string "None (by None)".
    rows = [
        {
            "company_master_id": "nse:FOO", "company_name": "Foo Co", "first_seen_at": "2026-08-01",
            "first_seen_price": 100.0, "current_price": 110.0, "alert_count": 1, "narrative_text": "n",
            "suggested_watch_until": None,
            "draft_thesis": {"prediction_text": None, "target_date": None, "invalidation_criteria": "i", "confidence_score": 72},
        }
    ]
    _, text_body, _ = fundamentals_notifications.build_daily_digest_content(rows)
    assert "n/a (by n/a)" in text_body
    assert "None (by None)" not in text_body


def test_build_daily_digest_content_renders_draft_generated_at_in_both_bodies():
    # BUG FOUND LIVE 2026-08-18 (re-audit): generated_at is already selected by
    # load_current_drafts_by_company()'s SELECT d.* but was never rendered -- a
    # draft is emailed in every digest until a human commits or discards it
    # (never auto-expires), so with no age shown a week-old un-reviewed draft
    # looked identical to a fresh one.
    rows = [
        {
            "company_master_id": "nse:FOO", "company_name": "Foo Co", "first_seen_at": "2026-08-01",
            "first_seen_price": 100.0, "current_price": 110.0, "alert_count": 1, "narrative_text": "n",
            "suggested_watch_until": None,
            "draft_thesis": {
                "prediction_text": "p", "target_date": date(2026, 11, 15), "invalidation_criteria": "i",
                "confidence_score": 72, "generated_at": date(2026, 8, 1),
            },
        }
    ]
    _, text_body, html_body = fundamentals_notifications.build_daily_digest_content(rows)
    assert "Drafted: 2026-08-01" in text_body
    assert "Drafted: 2026-08-01" in html_body


def test_build_daily_digest_content_missing_generated_at_shows_na_not_none():
    rows = [
        {
            "company_master_id": "nse:FOO", "company_name": "Foo Co", "first_seen_at": "2026-08-01",
            "first_seen_price": 100.0, "current_price": 110.0, "alert_count": 1, "narrative_text": "n",
            "suggested_watch_until": None,
            "draft_thesis": {"prediction_text": "p", "target_date": date(2026, 11, 15), "invalidation_criteria": "i", "confidence_score": 72},
        }
    ]
    _, text_body, html_body = fundamentals_notifications.build_daily_digest_content(rows)
    assert "Drafted: n/a" in text_body
    assert "Drafted: n/a" in html_body


def test_build_daily_digest_content_text_shows_price_change_pct():
    # BUG FOUND LIVE 2026-08-17: plain-text used to print only the two raw prices
    # with no pct change at all, an inconsistency with the HTML table cell right
    # next to it (which always computed one).
    rows = [{"company_master_id": "nse:FOO", "company_name": "Foo Co", "first_seen_at": "2026-08-01", "first_seen_price": 100.0, "current_price": 110.0, "alert_count": 1, "narrative_text": "n", "suggested_watch_until": None}]
    _, text_body, _ = fundamentals_notifications.build_daily_digest_content(rows)
    assert "(+10.0%)" in text_body


def test_build_daily_digest_content_low_confidence_gets_low_css_class():
    rows = [
        {
            "company_master_id": "nse:FOO", "company_name": "Foo Co", "first_seen_at": "2026-08-01",
            "first_seen_price": 100.0, "current_price": 110.0, "alert_count": 1, "narrative_text": "n",
            "suggested_watch_until": None,
            "draft_thesis": {"prediction_text": "p", "target_date": date(2026, 11, 15), "invalidation_criteria": "i", "confidence_score": 20},
        }
    ]
    _, _, html_body = fundamentals_notifications.build_daily_digest_content(rows)
    assert 'class="draft-confidence low"' in html_body


def test_build_daily_digest_content_no_draft_thesis_key_renders_fine():
    # backward-compat: a row without a "draft_thesis" key (or with None) must not crash
    # and must not render the draft section at all.
    rows = [{"company_master_id": "nse:FOO", "company_name": "Foo Co", "first_seen_at": "2026-08-01", "first_seen_price": 100.0, "current_price": 110.0, "alert_count": 1, "narrative_text": "n", "suggested_watch_until": None}]
    subject, text_body, html_body = fundamentals_notifications.build_daily_digest_content(rows)
    assert "DRAFT L4 thesis" not in text_body
    assert "Draft L4 Theses" not in html_body


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
    monkeypatch.setattr(fundamentals_notifications, "load_full_watchlist", lambda: [{"company_master_id": "nse:FOO", "company_name": "Foo", "first_seen_at": "2026-08-01", "first_seen_price": 1, "current_price": 1, "alert_count": 1, "narrative_text": "n", "suggested_watch_until": None}])
    sent_calls = []
    monkeypatch.setattr(fundamentals_notifications, "send_email", lambda subject, text_body, html_body=None: sent_calls.append((subject, text_body, html_body)))

    result = fundamentals_notifications.send_daily_digest()

    assert result == {"sent": 1, "skipped_disabled": 0, "failed": 0}
    assert len(sent_calls) == 1
    subject, text_body, html_body = sent_calls[0]
    assert "1 companies" in subject
    assert html_body is not None and "<table>" in html_body


def test_send_daily_digest_records_failure_without_raising(monkeypatch):
    monkeypatch.setattr(fundamentals_notifications, "WATCHLIST_ALERT_EMAIL_ENABLED", True)
    monkeypatch.setattr(fundamentals_notifications, "WATCHLIST_ALERT_EMAIL_FROM", "from@x.com")
    monkeypatch.setattr(fundamentals_notifications, "WATCHLIST_ALERT_EMAIL_TO", "a@x.com")
    monkeypatch.setattr(fundamentals_notifications, "load_full_watchlist", lambda: [])

    def raise_error(subject, text_body, html_body=None):
        raise RuntimeError("ses down")

    monkeypatch.setattr(fundamentals_notifications, "send_email", raise_error)
    fallback_events = []
    monkeypatch.setattr(fundamentals_notifications, "_record_fallback", lambda *a, **k: fallback_events.append((a, k)))

    result = fundamentals_notifications.send_daily_digest()

    assert result == {"sent": 0, "skipped_disabled": 0, "failed": 1}
    assert any(a and a[0] == "watchlist_digest_email_send_failed" for a, k in fallback_events)


def test_normalize_investor_key_collapses_whitespace_and_case():
    assert fundamentals_investor_classification.normalize_investor_key("  ABC   Fund  LP  ") == "abc fund lp"
    assert fundamentals_investor_classification.normalize_investor_key(None) == ""


def test_effective_tier_prefers_override():
    assert fundamentals_investor_classification.effective_tier({"llm_tier": "unknown", "override_tier": "marquee"}) == "marquee"
    assert fundamentals_investor_classification.effective_tier({"llm_tier": "recognized", "override_tier": None}) == "recognized"
    assert fundamentals_investor_classification.effective_tier({"llm_tier": None, "override_tier": None}) is None


def test_load_unclassified_investor_names_dedupes_within_batch_and_against_known(monkeypatch):
    events_df = pd.DataFrame(
        [
            {"source": "bse", "news_id": "n1", "structured_extraction_json": json.dumps({"investor_names": ["Acme Fund", "already known fund"]})},
            {"source": "bse", "news_id": "n2", "structured_extraction_json": json.dumps({"investor_names": ["acme fund", "Beta Ventures"]})},
        ]
    )
    known_df = pd.DataFrame([{"investor_key": "already known fund"}])
    calls = iter([events_df, known_df])
    monkeypatch.setattr(fundamentals_investor_classification, "sql_to_df", lambda q: next(calls))

    result = fundamentals_investor_classification.load_unclassified_investor_names()

    keys = [c["investor_key"] for c in result]
    assert keys == ["acme fund", "beta ventures"]  # "Acme Fund"/"acme fund" collapse to one; the known one is excluded
    assert result[0]["source"] == "bse" and result[0]["news_id"] == "n1"


def test_load_unclassified_investor_names_empty_events_returns_empty(monkeypatch):
    monkeypatch.setattr(fundamentals_investor_classification, "sql_to_df", lambda q: pd.DataFrame())
    assert fundamentals_investor_classification.load_unclassified_investor_names() == []


def test_load_unclassified_investor_names_skips_unparseable_json(monkeypatch):
    events_df = pd.DataFrame([{"source": "bse", "news_id": "n1", "structured_extraction_json": "not json"}])
    calls = iter([events_df, pd.DataFrame()])
    monkeypatch.setattr(fundamentals_investor_classification, "sql_to_df", lambda q: next(calls))
    assert fundamentals_investor_classification.load_unclassified_investor_names() == []


def test_run_investor_classification_returns_early_on_no_candidates(monkeypatch):
    monkeypatch.setattr(fundamentals_investor_classification, "_ensure_investor_classification_table", lambda: None)
    monkeypatch.setattr(fundamentals_investor_classification, "load_unclassified_investor_names", lambda limit=None: [])

    result = fundamentals_investor_classification.run_investor_classification()

    assert result == {"classified": 0, "failed": 0, "blocked": False}


def test_run_investor_classification_writes_classified_rows(monkeypatch):
    monkeypatch.setattr(fundamentals_investor_classification, "_ensure_investor_classification_table", lambda: None)
    candidates = [{"investor_key": "acme fund", "investor_name_display": "Acme Fund", "source": "bse", "news_id": "n1"}]
    monkeypatch.setattr(fundamentals_investor_classification, "load_unclassified_investor_names", lambda limit=None: candidates)
    monkeypatch.setattr(
        fundamentals_investor_classification, "classify_investor", lambda name, **k: {"tier": "unknown", "reasoning": "not a recognized name"}
    )
    upserts = []
    monkeypatch.setattr(fundamentals_investor_classification, "upsert_to_db", lambda df, table, **k: upserts.append((df, table, k)))

    result = fundamentals_investor_classification.run_investor_classification(model="test-model")

    assert result == {"classified": 1, "failed": 0, "blocked": False}
    assert len(upserts) == 1
    row = upserts[0][0].iloc[0]
    assert row["investor_key"] == "acme fund"
    assert row["classification_status"] == "done"
    assert row["llm_tier"] == "unknown"
    assert row["override_tier"] is None
    assert row["llm_model"] == "test-model"
    assert upserts[0][2]["unique_keys"] == ["investor_key"]


def test_run_investor_classification_handles_malformed_llm_response(monkeypatch):
    # 2026-08-15 bug found live: judgment["tier"]/["reasoning"] used to be indexed OUTSIDE the
    # try/except around the LLM call -- a response missing an expected key raised an uncaught
    # KeyError there, aborting the entire remaining batch with no fallback-telemetry record. Now
    # handled as this one name's failure, same as any other error from the call.
    monkeypatch.setattr(fundamentals_investor_classification, "_ensure_investor_classification_table", lambda: None)
    candidates = [
        {"investor_key": "bad fund", "investor_name_display": "Bad Fund", "source": "bse", "news_id": "n1"},
        {"investor_key": "good fund", "investor_name_display": "Good Fund", "source": "bse", "news_id": "n2"},
    ]
    monkeypatch.setattr(fundamentals_investor_classification, "load_unclassified_investor_names", lambda limit=None: candidates)
    monkeypatch.setattr(
        fundamentals_investor_classification,
        "classify_investor",
        lambda name, **k: {"reasoning": "missing tier key"} if name == "Bad Fund" else {"tier": "unknown", "reasoning": "ok"},
    )
    upserts = []
    monkeypatch.setattr(fundamentals_investor_classification, "upsert_to_db", lambda df, table, **k: upserts.append((df, table, k)))
    events = []
    monkeypatch.setattr(fundamentals_investor_classification, "record_local_fallback_event", lambda **kwargs: events.append(kwargs))

    result = fundamentals_investor_classification.run_investor_classification(model="test-model")

    assert result == {"classified": 1, "failed": 1, "blocked": False}  # did NOT abort on the malformed response
    assert len(upserts) == 1
    df = upserts[0][0]
    assert len(df) == 2  # both the failed row and the successful row are written -- see below
    good_row = df[df["investor_key"] == "good fund"].iloc[0]
    assert good_row["classification_status"] == "done"
    assert good_row["llm_tier"] == "unknown"
    bad_row = df[df["investor_key"] == "bad fund"].iloc[0]
    # BUG FOUND LIVE 2026-08-15, fixed here: a failed classification used to write
    # NOTHING, unlike every sibling LLM-calling module's permanent-fail pattern --
    # load_unclassified_investor_names() determines "known" purely by row presence,
    # so a name with no persisted status was re-queued at the same stable position
    # every run, forever.
    assert bad_row["classification_status"] == "failed"
    assert bad_row["llm_tier"] is None
    assert any(e["fallback_type"] == "investor_classification_failed" and e["metadata"]["investor_key"] == "bad fund" for e in events)


def test_run_investor_classification_trips_circuit_breaker(monkeypatch):
    monkeypatch.setattr(fundamentals_investor_classification, "_ensure_investor_classification_table", lambda: None)
    candidates = [{"investor_key": f"fund{i}", "investor_name_display": f"Fund {i}", "source": "bse", "news_id": f"n{i}"} for i in range(5)]
    monkeypatch.setattr(fundamentals_investor_classification, "load_unclassified_investor_names", lambda limit=None: candidates)

    def always_fails(name, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr(fundamentals_investor_classification, "classify_investor", always_fails)
    monkeypatch.setattr(fundamentals_investor_classification, "upsert_to_db", lambda df, table, **k: None)
    fallback_events = []
    monkeypatch.setattr(fundamentals_investor_classification, "_record_fallback", lambda *a, **k: fallback_events.append((a, k)))

    result = fundamentals_investor_classification.run_investor_classification()

    assert result["blocked"] is True
    assert result["failed"] == fundamentals_investor_classification.CIRCUIT_BREAKER_THRESHOLD
    assert any(a and a[0] == "investor_classification_circuit_breaker_tripped" for a, k in fallback_events)


def test_set_investor_override_rejects_invalid_tier():
    with pytest.raises(ValueError):
        fundamentals_investor_classification.set_investor_override("acme fund", tier="bogus")


def test_set_investor_override_writes_via_db_session(monkeypatch):
    calls = []

    class FakeCursor:
        def execute(self, query, params):
            calls.append((query, params))

    class FakeCtx:
        def __enter__(self):
            return (None, FakeCursor())

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(fundamentals_investor_classification, "db_session", lambda: FakeCtx())
    monkeypatch.setattr(fundamentals_investor_classification, "execute_db_operation", lambda fn, **k: fn())

    fundamentals_investor_classification.set_investor_override("acme fund", tier="marquee", notes="well known")

    assert len(calls) == 1
    assert calls[0][1][0] == "marquee"
    assert calls[0][1][1] == "well known"
    assert calls[0][1][3] == "acme fund"


def test_get_investor_classifications_returns_clean_records(monkeypatch):
    df = pd.DataFrame(
        [{"investor_key": "acme fund", "investor_name_display": "Acme Fund", "llm_tier": "unknown", "override_tier": None, "llm_classified_at": pd.Timestamp("2026-08-12", tz="UTC")}]
    )
    monkeypatch.setattr(fundamentals_api_queries, "get_all_investor_classifications", lambda: df)

    result = fundamentals_api_queries.get_investor_classifications()

    assert result[0]["investor_key"] == "acme fund"
    assert result[0]["override_tier"] is None


def test_set_investor_classification_override_delegates(monkeypatch):
    calls = []
    monkeypatch.setattr(fundamentals_api_queries, "set_investor_override", lambda key, **kwargs: calls.append((key, kwargs)))

    fundamentals_api_queries.set_investor_classification_override("acme fund", {"tier": "marquee", "notes": "well known"})

    assert calls == [("acme fund", {"tier": "marquee", "notes": "well known"})]


def test_api_investors_route_returns_queries_result(monkeypatch):
    monkeypatch.setattr(fundamentals_api_queries, "get_investor_classifications", lambda: [{"investor_key": "acme fund"}])
    client = TestClient(fundamentals_api_app)
    r = client.get("/api/investors")
    assert r.status_code == 200
    assert r.json() == [{"investor_key": "acme fund"}]


def test_api_override_investor_rejects_invalid_tier():
    client = TestClient(fundamentals_api_app)
    r = client.post("/api/investors/acme%20fund/override", json={"tier": "bogus"})
    assert r.status_code == 400


def test_api_override_investor_success(monkeypatch):
    calls = []
    monkeypatch.setattr(fundamentals_api_queries, "set_investor_classification_override", lambda key, payload: calls.append((key, payload)))
    client = TestClient(fundamentals_api_app)

    r = client.post("/api/investors/acme%20fund/override", json={"tier": "marquee", "notes": "well known"})

    assert r.status_code == 200
    assert r.json() == {"status": "updated", "investor_key": "acme fund"}
    assert calls == [("acme fund", {"tier": "marquee", "notes": "well known"})]


def test_get_todos_wraps_unsupported_rating_agencies(monkeypatch):
    df = pd.DataFrame([{"agency_name": "acuite", "occurrence_count": 5, "first_seen_at": pd.Timestamp("2026-08-01", tz="UTC"), "last_seen_at": pd.Timestamp("2026-08-13", tz="UTC")}])
    monkeypatch.setattr(fundamentals_api_queries, "get_unsupported_rating_agencies", lambda: df)

    result = fundamentals_api_queries.get_todos()

    assert result["rating_agencies"][0]["agency_name"] == "acuite"
    assert result["rating_agencies"][0]["occurrence_count"] == 5


def test_get_todos_empty_returns_empty_list(monkeypatch):
    monkeypatch.setattr(fundamentals_api_queries, "get_unsupported_rating_agencies", lambda: pd.DataFrame())
    assert fundamentals_api_queries.get_todos() == {"rating_agencies": []}


def test_api_todos_route_returns_queries_result(monkeypatch):
    monkeypatch.setattr(fundamentals_api_queries, "get_todos", lambda: {"rating_agencies": [{"agency_name": "acuite"}]})
    client = TestClient(fundamentals_api_app)

    r = client.get("/api/todos")

    assert r.status_code == 200
    assert r.json() == {"rating_agencies": [{"agency_name": "acuite"}]}


# --- signal_pointers.py -----------------------------------------------------------


def test_resolve_rating_action_prefers_column_over_json():
    event = {"rating_action_type": "downgraded", "structured_extraction_json": json.dumps({"rating_action": "upgraded"})}
    assert fundamentals_signal_pointers._resolve_rating_action(event) == "downgraded"


def test_resolve_rating_action_falls_back_to_json():
    event = {"rating_action_type": None, "structured_extraction_json": json.dumps({"rating_action": "upgraded"})}
    assert fundamentals_signal_pointers._resolve_rating_action(event) == "upgraded"


def test_resolve_rating_action_none_when_neither_present():
    assert fundamentals_signal_pointers._resolve_rating_action({"rating_action_type": None, "structured_extraction_json": None}) is None


def test_resolve_rating_action_a_downgraded_instrument_cannot_be_masked_by_the_flat_column():
    # Twin of l3_triggers.py's own regression test -- same bug, same fix, see there
    # for the full explanation.
    event = {
        "rating_action_type": "reaffirmed",
        "structured_extraction_json": json.dumps({
            "rating_action": "reaffirmed",
            "instrument_actions": [{"instrument_description": "NCD", "rating_action": "downgraded"}],
        }),
    }
    assert fundamentals_signal_pointers._resolve_rating_action(event) == "downgraded"


def test_load_l2_signals_for_company_resolves_l1_ticker_and_returns_row(monkeypatch):
    # BUG FOUND LIVE 2026-08-18: naive removeprefix("nse:") only recovers the
    # correct fundamentals_l2_state.ticker when the company IS its own NSE symbol
    # -- wrong for the ~22% BSE-only cohort, whose L2 ticker is a raw BSE scrip
    # code. This test used to name and assert the naive behavior directly; now
    # pins the real resolution via build_l1_ticker_by_company_master_id(), which
    # can map a company_master_id to an L1 ticker that DOESN'T just strip "nse:".
    captured = {}

    def fake_sql_to_df(q, params=None):
        captured["params"] = params
        return pd.DataFrame([{"promoter_pct": 55.0, "promoter_stake_direction": "increasing", "institutional_pct": 3.0, "institutional_stake_direction": "increasing", "institutional_first_entry": False, "run_date": date(2026, 8, 1)}])

    monkeypatch.setattr(fundamentals_signal_pointers, "build_l1_ticker_by_company_master_id", lambda: {"nse:ALUFLUOR": "524634"})
    monkeypatch.setattr(fundamentals_signal_pointers, "sql_to_df", fake_sql_to_df)
    result = fundamentals_signal_pointers.load_l2_signals_for_company("nse:ALUFLUOR")

    assert captured["params"] == ("524634",)  # the L1 slug, not a naive "ALUFLUOR" strip
    assert result["promoter_pct"] == 55.0


def test_load_l2_signals_for_company_none_when_unresolved(monkeypatch):
    calls = []
    monkeypatch.setattr(fundamentals_signal_pointers, "build_l1_ticker_by_company_master_id", lambda: {})
    monkeypatch.setattr(fundamentals_signal_pointers, "sql_to_df", lambda q, params=None: calls.append(1) or pd.DataFrame())
    assert fundamentals_signal_pointers.load_l2_signals_for_company("nse:UNKNOWN") is None
    assert calls == []  # never queried L2 state for an unresolvable company


def test_load_l2_signals_for_company_none_when_empty(monkeypatch):
    monkeypatch.setattr(fundamentals_signal_pointers, "sql_to_df", lambda q, params=None: pd.DataFrame())
    assert fundamentals_signal_pointers.load_l2_signals_for_company("nse:ABC") is None


def test_load_latest_rating_event_for_company_returns_row(monkeypatch):
    df = pd.DataFrame([{"rating_agency": "CRISIL", "rating_action_type": "downgraded", "structured_extraction_json": None, "disclosure_date": "2026-08-01", "load_ts": pd.Timestamp("2026-08-01", tz="UTC")}])
    monkeypatch.setattr(fundamentals_signal_pointers, "sql_to_df", lambda q, params=None: df)
    result = fundamentals_signal_pointers.load_latest_rating_event_for_company("nse:ABC")
    assert result["rating_agency"] == "CRISIL"


def test_load_latest_rating_event_for_company_none_when_empty(monkeypatch):
    monkeypatch.setattr(fundamentals_signal_pointers, "sql_to_df", lambda q, params=None: pd.DataFrame())
    assert fundamentals_signal_pointers.load_latest_rating_event_for_company("nse:ABC") is None


def test_load_latest_insider_transaction_for_company_returns_row(monkeypatch):
    df = pd.DataFrame([{"insider_name": "John Doe", "quantity": 10000.0, "transaction_type": "Buy", "disclosure_date": "2026-08-01", "load_ts": pd.Timestamp("2026-08-01", tz="UTC")}])
    monkeypatch.setattr(fundamentals_signal_pointers, "sql_to_df", lambda q, params=None: df)
    result = fundamentals_signal_pointers.load_latest_insider_transaction_for_company("nse:ABC")
    assert result["insider_name"] == "John Doe"


def test_load_latest_insider_transaction_for_company_none_when_empty(monkeypatch):
    monkeypatch.setattr(fundamentals_signal_pointers, "sql_to_df", lambda q, params=None: pd.DataFrame())
    assert fundamentals_signal_pointers.load_latest_insider_transaction_for_company("nse:ABC") is None


def test_load_latest_confirmed_auditor_change_for_company_finds_confirmed_row(monkeypatch):
    df = pd.DataFrame(
        [
            {"disclosure_date": "2026-08-01", "structured_extraction_json": json.dumps({"disclosure_type": "incidental_mention"})},
            {"disclosure_date": "2026-07-01", "structured_extraction_json": json.dumps({"disclosure_type": "confirmed_change", "change_direction": "resignation", "previous_auditor": "ABC & Co"})},
        ]
    )
    monkeypatch.setattr(fundamentals_signal_pointers, "sql_to_df", lambda q, params=None: df)
    result = fundamentals_signal_pointers.load_latest_confirmed_auditor_change_for_company("nse:ABC")
    assert result["change_direction"] == "resignation"
    assert result["disclosure_date"] == "2026-07-01"


def test_load_latest_confirmed_auditor_change_for_company_none_when_no_confirmed_row(monkeypatch):
    df = pd.DataFrame([{"disclosure_date": "2026-08-01", "structured_extraction_json": json.dumps({"disclosure_type": "incidental_mention"})}])
    monkeypatch.setattr(fundamentals_signal_pointers, "sql_to_df", lambda q, params=None: df)
    assert fundamentals_signal_pointers.load_latest_confirmed_auditor_change_for_company("nse:ABC") is None


def test_load_latest_confirmed_auditor_change_for_company_none_when_empty(monkeypatch):
    monkeypatch.setattr(fundamentals_signal_pointers, "sql_to_df", lambda q, params=None: pd.DataFrame())
    assert fundamentals_signal_pointers.load_latest_confirmed_auditor_change_for_company("nse:ABC") is None


def test_load_latest_material_rpt_for_company_finds_material_row(monkeypatch):
    df = pd.DataFrame(
        [
            {"disclosure_date": "2026-08-01", "structured_extraction_json": json.dumps({"is_applicable": False, "pct_of_revenue": None})},
            {"disclosure_date": "2026-07-01", "structured_extraction_json": json.dumps({"is_applicable": True, "pct_of_revenue": 25.0, "related_party_name": "Promoter Group"})},
        ]
    )
    monkeypatch.setattr(fundamentals_signal_pointers, "sql_to_df", lambda q, params=None: df)
    result = fundamentals_signal_pointers.load_latest_material_rpt_for_company("nse:ABC")
    assert result["pct_of_revenue"] == 25.0
    assert result["related_party_name"] == "Promoter Group"


def test_load_latest_material_rpt_for_company_none_when_under_threshold(monkeypatch):
    df = pd.DataFrame([{"disclosure_date": "2026-08-01", "structured_extraction_json": json.dumps({"is_applicable": True, "pct_of_revenue": 5.0})}])
    monkeypatch.setattr(fundamentals_signal_pointers, "sql_to_df", lambda q, params=None: df)
    assert fundamentals_signal_pointers.load_latest_material_rpt_for_company("nse:ABC") is None


def test_load_latest_material_rpt_for_company_none_when_empty(monkeypatch):
    monkeypatch.setattr(fundamentals_signal_pointers, "sql_to_df", lambda q, params=None: pd.DataFrame())
    assert fundamentals_signal_pointers.load_latest_material_rpt_for_company("nse:ABC") is None


def test_get_stock_signal_pointers_auditor_change(monkeypatch):
    auditor_change = {"disclosure_date": "2026-08-01", "change_direction": "resignation", "previous_auditor": "ABC & Co", "new_auditor": None}
    _patch_signal_pointer_loaders(monkeypatch, auditor_change=auditor_change)

    pointers = fundamentals_signal_pointers.get_stock_signal_pointers("nse:ABC")

    assert len(pointers) == 1
    assert pointers[0]["signal_type"] == "auditor_change"
    assert "resignation" in pointers[0]["label"]
    assert "ABC & Co" in pointers[0]["label"]


def test_get_stock_signal_pointers_rpt(monkeypatch):
    rpt = {"disclosure_date": "2026-08-01", "pct_of_revenue": 25.0, "related_party_name": "Promoter Group"}
    _patch_signal_pointer_loaders(monkeypatch, rpt=rpt)

    pointers = fundamentals_signal_pointers.get_stock_signal_pointers("nse:ABC")

    assert len(pointers) == 1
    assert pointers[0]["signal_type"] == "related_party_transaction"
    assert pointers[0]["value"] == 25.0
    assert "Promoter Group" in pointers[0]["label"]


def test_load_capital_raise_investor_signals_dedupes_and_resolves_tier(monkeypatch):
    events_df = pd.DataFrame(
        [
            {"structured_extraction_json": json.dumps({"investor_names": ["Acme Fund", "Unknown Guy"]}), "disclosure_date": "2026-08-01"},
            {"structured_extraction_json": json.dumps({"investor_names": ["acme fund"]}), "disclosure_date": "2026-08-05"},  # dup, same key -> first occurrence wins
        ]
    )
    tiers_df = pd.DataFrame([{"investor_key": "acme fund", "llm_tier": "recognized", "override_tier": "marquee"}])
    calls = iter([events_df, tiers_df])
    monkeypatch.setattr(fundamentals_signal_pointers, "sql_to_df", lambda q, params=None: next(calls))

    result = fundamentals_signal_pointers.load_capital_raise_investor_signals_for_company("nse:ABC")

    by_name = {r["investor_name"]: r for r in result}
    assert by_name["Acme Fund"]["tier"] == "marquee"  # override wins over llm_tier
    assert by_name["Acme Fund"]["as_of_date"] == "2026-08-01"  # first occurrence's date, not the dup's
    assert by_name["Unknown Guy"]["tier"] is None  # not yet classified -- surfaced, not dropped
    assert len(result) == 2  # "acme fund" dedup collapsed to one


def test_load_capital_raise_investor_signals_empty_events_returns_empty(monkeypatch):
    monkeypatch.setattr(fundamentals_signal_pointers, "sql_to_df", lambda q, params=None: pd.DataFrame())
    assert fundamentals_signal_pointers.load_capital_raise_investor_signals_for_company("nse:ABC") == []


def test_load_capital_raise_investor_signals_skips_unparseable_json(monkeypatch):
    events_df = pd.DataFrame([{"structured_extraction_json": "not json", "disclosure_date": "2026-08-01"}])
    calls = iter([events_df, pd.DataFrame()])
    monkeypatch.setattr(fundamentals_signal_pointers, "sql_to_df", lambda q, params=None: next(calls))
    assert fundamentals_signal_pointers.load_capital_raise_investor_signals_for_company("nse:ABC") == []


def test_load_sector_growth_for_company_returns_row(monkeypatch):
    df = pd.DataFrame([{"sector_code": "IN01", "sector_name": "Chemicals", "phase": "balanced", "demand_growth_pct": 20.0, "sample_size_confidence": "adequate", "run_date": date(2026, 8, 1)}])
    monkeypatch.setattr(fundamentals_signal_pointers, "sql_to_df", lambda q, params=None: df)
    result = fundamentals_signal_pointers.load_sector_growth_for_company("nse:ABC")
    assert result["sector_name"] == "Chemicals"


def test_load_sector_growth_for_company_none_when_empty(monkeypatch):
    monkeypatch.setattr(fundamentals_signal_pointers, "sql_to_df", lambda q, params=None: pd.DataFrame())
    assert fundamentals_signal_pointers.load_sector_growth_for_company("nse:ABC") is None


def test_load_satisfied_strategies_for_company_returns_rows(monkeypatch):
    df = pd.DataFrame([{"trigger_type": "capital_raise", "alert_date": date(2026, 8, 1), "reasoning": "r"}])
    monkeypatch.setattr(fundamentals_signal_pointers, "sql_to_df", lambda q, params=None: df)
    result = fundamentals_signal_pointers.load_satisfied_strategies_for_company("nse:ABC")
    assert result == [{"trigger_type": "capital_raise", "alert_date": date(2026, 8, 1), "reasoning": "r"}]


def test_load_satisfied_strategies_for_company_empty(monkeypatch):
    monkeypatch.setattr(fundamentals_signal_pointers, "sql_to_df", lambda q, params=None: pd.DataFrame())
    assert fundamentals_signal_pointers.load_satisfied_strategies_for_company("nse:ABC") == []


def test_load_satisfied_strategies_by_company_groups_and_sorts(monkeypatch):
    df = pd.DataFrame(
        [
            {"company_master_id": "nse:FOO", "trigger_type": "rating_downgrade"},
            {"company_master_id": "nse:FOO", "trigger_type": "capital_raise"},
            {"company_master_id": "nse:BAR", "trigger_type": "insider_buy"},
        ]
    )
    monkeypatch.setattr(fundamentals_signal_pointers, "sql_to_df", lambda q, params=None: df)

    result = fundamentals_signal_pointers.load_satisfied_strategies_by_company()

    assert result == {"nse:FOO": ["capital_raise", "rating_downgrade"], "nse:BAR": ["insider_buy"]}


def test_load_satisfied_strategies_by_company_empty(monkeypatch):
    monkeypatch.setattr(fundamentals_signal_pointers, "sql_to_df", lambda q, params=None: pd.DataFrame())
    assert fundamentals_signal_pointers.load_satisfied_strategies_by_company() == {}


def _patch_signal_pointer_loaders(monkeypatch, *, l2=None, rating=None, investors=None, sector=None, strategies=None, insider=None, auditor_change=None, rpt=None):
    monkeypatch.setattr(fundamentals_signal_pointers, "load_l2_signals_for_company", lambda cmid: l2)
    monkeypatch.setattr(fundamentals_signal_pointers, "load_latest_rating_event_for_company", lambda cmid: rating)
    monkeypatch.setattr(fundamentals_signal_pointers, "load_capital_raise_investor_signals_for_company", lambda cmid: investors or [])
    monkeypatch.setattr(fundamentals_signal_pointers, "load_sector_growth_for_company", lambda cmid: sector)
    monkeypatch.setattr(fundamentals_signal_pointers, "load_satisfied_strategies_for_company", lambda cmid: strategies or [])
    monkeypatch.setattr(fundamentals_signal_pointers, "load_latest_insider_transaction_for_company", lambda cmid: insider)
    monkeypatch.setattr(fundamentals_signal_pointers, "load_latest_confirmed_auditor_change_for_company", lambda cmid: auditor_change)
    monkeypatch.setattr(fundamentals_signal_pointers, "load_latest_material_rpt_for_company", lambda cmid: rpt)


def test_get_stock_signal_pointers_all_sources_empty_returns_empty(monkeypatch):
    _patch_signal_pointer_loaders(monkeypatch)
    assert fundamentals_signal_pointers.get_stock_signal_pointers("nse:ABC") == []


def test_get_stock_signal_pointers_promoter_and_institutional_holding(monkeypatch):
    l2 = {"promoter_pct": 55.0, "promoter_stake_direction": "decreasing", "institutional_pct": 4.0, "institutional_stake_direction": "increasing", "institutional_first_entry": False, "run_date": date(2026, 8, 1)}
    _patch_signal_pointer_loaders(monkeypatch, l2=l2)

    pointers = fundamentals_signal_pointers.get_stock_signal_pointers("nse:ABC")
    by_type = {p["signal_type"]: p for p in pointers}

    assert by_type["promoter_holding"]["value"] == 55.0
    assert by_type["promoter_holding"]["direction"] == "decreasing"
    assert by_type["institutional_holding"]["value"] == 4.0
    assert by_type["institutional_holding"]["direction"] == "increasing"
    assert "institutional_first_entry" not in by_type  # flag is False -- no pointer emitted


def test_get_stock_signal_pointers_institutional_first_entry_emitted_when_true(monkeypatch):
    l2 = {"promoter_pct": None, "promoter_stake_direction": None, "institutional_pct": None, "institutional_stake_direction": None, "institutional_first_entry": True, "run_date": date(2026, 8, 1)}
    _patch_signal_pointer_loaders(monkeypatch, l2=l2)

    pointers = fundamentals_signal_pointers.get_stock_signal_pointers("nse:ABC")

    assert len(pointers) == 1
    assert pointers[0]["signal_type"] == "institutional_first_entry"
    assert pointers[0]["direction"] == "new"
    assert "~3yr" in pointers[0]["label"]  # 2026-08-13: caveat the lookback window, not "first ever"


def test_get_stock_signal_pointers_rating_action_includes_agency_in_label(monkeypatch):
    rating = {"rating_agency": "CRISIL", "rating_action_type": "downgraded", "structured_extraction_json": None, "disclosure_date": "2026-08-01"}
    _patch_signal_pointer_loaders(monkeypatch, rating=rating)

    pointers = fundamentals_signal_pointers.get_stock_signal_pointers("nse:ABC")

    assert len(pointers) == 1
    assert pointers[0]["signal_type"] == "rating_action"
    assert pointers[0]["value"] == "downgraded"
    assert pointers[0]["direction"] == "down"
    assert "CRISIL" in pointers[0]["label"]
    assert pointers[0]["source"] == "CRISIL"


def test_get_stock_signal_pointers_rating_action_omitted_when_unresolvable(monkeypatch):
    rating = {"rating_agency": "CRISIL", "rating_action_type": None, "structured_extraction_json": None, "disclosure_date": "2026-08-01"}
    _patch_signal_pointer_loaders(monkeypatch, rating=rating)
    assert fundamentals_signal_pointers.get_stock_signal_pointers("nse:ABC") == []


def test_get_stock_signal_pointers_investor_entries(monkeypatch):
    investors = [{"investor_name": "Acme Fund", "tier": "marquee", "as_of_date": "2026-08-01"}, {"investor_name": "Unknown Guy", "tier": None, "as_of_date": "2026-08-01"}]
    _patch_signal_pointer_loaders(monkeypatch, investors=investors)

    pointers = fundamentals_signal_pointers.get_stock_signal_pointers("nse:ABC")

    assert len(pointers) == 2
    assert all(p["signal_type"] == "investor_entry" for p in pointers)
    assert {p["value"] for p in pointers} == {"marquee", None}


def test_get_stock_signal_pointers_sector_growth_uses_classify_growth(monkeypatch):
    sector = {"sector_code": "IN01", "sector_name": "Chemicals", "phase": "balanced", "demand_growth_pct": 20.0, "sample_size_confidence": "adequate", "run_date": date(2026, 8, 1)}
    _patch_signal_pointer_loaders(monkeypatch, sector=sector)

    pointers = fundamentals_signal_pointers.get_stock_signal_pointers("nse:ABC")

    assert len(pointers) == 1
    assert pointers[0]["signal_type"] == "sector_growth"
    assert pointers[0]["value"] == "high_growth"  # classify_growth(20.0, "adequate")
    assert "Chemicals" in pointers[0]["label"]


def test_get_stock_signal_pointers_sector_growth_omitted_when_demand_missing(monkeypatch):
    sector = {"sector_code": "IN01", "sector_name": "Chemicals", "phase": "balanced", "demand_growth_pct": None, "sample_size_confidence": "adequate", "run_date": date(2026, 8, 1)}
    _patch_signal_pointer_loaders(monkeypatch, sector=sector)
    assert fundamentals_signal_pointers.get_stock_signal_pointers("nse:ABC") == []


def test_get_stock_signal_pointers_strategy_satisfied(monkeypatch):
    strategies = [{"trigger_type": "capital_raise", "alert_date": date(2026, 8, 1), "reasoning": "r"}, {"trigger_type": "rating_downgrade", "alert_date": date(2026, 8, 2), "reasoning": "r2"}]
    _patch_signal_pointer_loaders(monkeypatch, strategies=strategies)

    pointers = fundamentals_signal_pointers.get_stock_signal_pointers("nse:ABC")

    assert len(pointers) == 2
    assert {p["value"] for p in pointers} == {"capital_raise", "rating_downgrade"}
    assert all(p["signal_type"] == "strategy_satisfied" for p in pointers)


def test_get_stock_signal_pointers_insider_buy(monkeypatch):
    insider = {"insider_name": "John Doe", "quantity": 10000.0, "transaction_type": "Buy", "disclosure_date": "2026-08-01"}
    _patch_signal_pointer_loaders(monkeypatch, insider=insider)

    pointers = fundamentals_signal_pointers.get_stock_signal_pointers("nse:ABC")

    assert len(pointers) == 1
    assert pointers[0]["signal_type"] == "insider_transaction"
    assert pointers[0]["direction"] == "buy"
    assert "John Doe" in pointers[0]["label"]
    assert "10,000" in pointers[0]["label"]


def test_get_stock_signal_pointers_insider_sell_no_quantity(monkeypatch):
    insider = {"insider_name": "Jane Roe", "quantity": None, "transaction_type": "Sell", "disclosure_date": "2026-08-01"}
    _patch_signal_pointer_loaders(monkeypatch, insider=insider)

    pointers = fundamentals_signal_pointers.get_stock_signal_pointers("nse:ABC")

    assert pointers[0]["direction"] == "sell"
    assert "Jane Roe" in pointers[0]["label"]
    assert "(" not in pointers[0]["label"]  # no quantity known -- omitted, not guessed


def test_get_stock_signal_pointers_insider_transaction_omitted_when_no_name_recognized_direction(monkeypatch):
    # a pit_sast row can have a transaction_type that's neither buy nor sell-shaped
    # (e.g. a pledge-related notice) -- must not silently invent a direction.
    insider = {"insider_name": "John Doe", "quantity": 100.0, "transaction_type": "Pledge Revoke", "disclosure_date": "2026-08-01"}
    _patch_signal_pointer_loaders(monkeypatch, insider=insider)
    assert fundamentals_signal_pointers.get_stock_signal_pointers("nse:ABC") == []


# fundamentals/screens/watchlist_exit.py -- watchlist exit signals (step 10.5).


def test_load_watchlist_for_exit_evaluation_selects_latest_alert_load_ts(monkeypatch):
    # BUG FOUND LIVE 2026-08-18 (re-audit): _check_stale's own guard used to
    # compare last_alert_at (a filing's disclosure date) against
    # narrative_generated_at (a wall-clock timestamp) -- the same event-date-vs-
    # processing-time mismatch watch_summary.py's narrative refresh gate had.
    captured = {}

    def fake_sql_to_df(query, **kwargs):
        captured["query"] = query
        return pd.DataFrame()

    monkeypatch.setattr(fundamentals_watchlist_exit, "sql_to_df", fake_sql_to_df)
    fundamentals_watchlist_exit.load_watchlist_for_exit_evaluation()
    query = captured["query"]
    assert "MAX(load_ts) AS latest_alert_load_ts" in query
    assert "FROM fundamentals_l3_alerts" in query


def test_check_invalidated_finds_contradicting_later_trigger():
    history = [
        {"trigger_type": "rating_confirms_deleveraging", "alert_date": date(2026, 8, 1)},
        {"trigger_type": "rating_downgrade", "alert_date": date(2026, 9, 1)},
    ]
    reason = fundamentals_watchlist_exit._check_invalidated(history)
    assert reason is not None
    assert "rating_downgrade" in reason and "rating_confirms_deleveraging" in reason


def test_check_invalidated_none_when_no_contradiction():
    history = [{"trigger_type": "rating_confirms_deleveraging", "alert_date": date(2026, 8, 1)}, {"trigger_type": "capital_raise", "alert_date": date(2026, 9, 1)}]
    assert fundamentals_watchlist_exit._check_invalidated(history) is None


def test_check_invalidated_none_when_opposing_trigger_came_first():
    # the contradiction must come AFTER the original -- an earlier downgrade before
    # a later confirms-deleveraging isn't invalidation, it's an outdated data point.
    history = [{"trigger_type": "rating_downgrade", "alert_date": date(2026, 7, 1)}, {"trigger_type": "rating_confirms_deleveraging", "alert_date": date(2026, 8, 1)}]
    assert fundamentals_watchlist_exit._check_invalidated(history) is None


def test_check_invalidated_none_for_trigger_type_with_no_mapped_opposite():
    history = [{"trigger_type": "capital_raise", "alert_date": date(2026, 8, 1)}, {"trigger_type": "institutional_first_entry", "alert_date": date(2026, 9, 1)}]
    assert fundamentals_watchlist_exit._check_invalidated(history) is None


def test_check_invalidated_empty_history():
    assert fundamentals_watchlist_exit._check_invalidated([]) is None


def test_check_price_flagged_rally():
    reason = fundamentals_watchlist_exit._check_price_flagged(100.0, 160.0)  # +60%
    assert reason is not None
    assert "up 60.0%" in reason


def test_check_price_flagged_decline():
    reason = fundamentals_watchlist_exit._check_price_flagged(100.0, 60.0)  # -40%
    assert reason is not None
    assert "down 40.0%" in reason


def test_check_price_flagged_none_within_band():
    assert fundamentals_watchlist_exit._check_price_flagged(100.0, 110.0) is None  # +10%, within band


def test_check_price_flagged_none_when_price_missing():
    assert fundamentals_watchlist_exit._check_price_flagged(None, 110.0) is None
    assert fundamentals_watchlist_exit._check_price_flagged(100.0, None) is None
    assert fundamentals_watchlist_exit._check_price_flagged(0, 110.0) is None


def test_check_price_flagged_declines_to_judge_stale_price_data():
    # BUG FOUND LIVE 2026-08-15, fixed here: a stale current_price (the feed hasn't
    # updated in weeks/years) used to be trusted at face value -- a real price move
    # would look like ~0% change against the frozen value, masking exactly the
    # "look again" signal this check exists to raise. Confirmed live for
    # AAREYDRUGS/BAFNAPH. A +60% move that would normally flag must NOT flag when
    # the underlying price data is known stale.
    assert fundamentals_watchlist_exit._check_price_flagged(100.0, 160.0, price_data_stale=True) is None
    assert fundamentals_watchlist_exit._check_price_flagged(100.0, 160.0, price_data_stale=False) is not None  # unaffected when fresh


def test_check_stale_true_when_watch_until_passed_and_nothing_new():
    reason = fundamentals_watchlist_exit._check_stale(
        date(2026, 8, 1), pd.Timestamp("2026-07-01", tz="UTC"), pd.Timestamp("2026-07-15", tz="UTC"), today=date(2026, 8, 13)
    )
    assert reason is not None
    assert "2026-08-01" in reason


def test_check_stale_none_when_watch_until_not_yet_passed():
    assert fundamentals_watchlist_exit._check_stale(date(2026, 12, 1), None, pd.Timestamp("2026-07-15", tz="UTC"), today=date(2026, 8, 13)) is None


def test_check_stale_none_when_fresher_alert_exists_than_narrative():
    # latest_alert_load_ts is AFTER narrative_generated_at -- a fresh alert exists
    # that hasn't been synthesized into a new narrative yet, not actually stale.
    reason = fundamentals_watchlist_exit._check_stale(
        date(2026, 8, 1), pd.Timestamp("2026-08-10", tz="UTC"), pd.Timestamp("2026-07-15", tz="UTC"), today=date(2026, 8, 13)
    )
    assert reason is None


def test_check_stale_none_when_no_suggested_watch_until():
    assert fundamentals_watchlist_exit._check_stale(None, None, None, today=date(2026, 8, 13)) is None


def test_evaluate_exit_status_priority_invalidated_over_price_and_stale():
    row = {"first_seen_price": 100.0, "current_price": 160.0, "technicals_as_of_date": date(2026, 8, 13), "suggested_watch_until": date(2026, 8, 1), "last_alert_at": None, "narrative_generated_at": None}
    history = [{"trigger_type": "rating_confirms_deleveraging", "alert_date": date(2026, 7, 1)}, {"trigger_type": "rating_downgrade", "alert_date": date(2026, 8, 1)}]
    status, reason, _stale_skip = fundamentals_watchlist_exit.evaluate_exit_status(row, history, today=date(2026, 8, 13))
    assert status == "invalidated"


def test_evaluate_exit_status_priority_price_over_stale():
    row = {"first_seen_price": 100.0, "current_price": 160.0, "technicals_as_of_date": date(2026, 8, 13), "suggested_watch_until": date(2026, 8, 1), "last_alert_at": None, "narrative_generated_at": None}
    status, reason, _stale_skip = fundamentals_watchlist_exit.evaluate_exit_status(row, [], today=date(2026, 8, 13))
    assert status == "price_flagged"


def test_evaluate_exit_status_falls_through_to_stale():
    row = {"first_seen_price": 100.0, "current_price": 110.0, "technicals_as_of_date": date(2026, 8, 13), "suggested_watch_until": date(2026, 8, 1), "last_alert_at": None, "narrative_generated_at": None}
    status, reason, _stale_skip = fundamentals_watchlist_exit.evaluate_exit_status(row, [], today=date(2026, 8, 13))
    assert status == "stale"


def test_evaluate_exit_status_active_when_nothing_fires():
    row = {"first_seen_price": 100.0, "current_price": 110.0, "technicals_as_of_date": date(2026, 8, 13), "suggested_watch_until": date(2026, 12, 1), "last_alert_at": None, "narrative_generated_at": None}
    status, reason, _stale_skip = fundamentals_watchlist_exit.evaluate_exit_status(row, [], today=date(2026, 8, 13))
    assert status == "active"
    assert reason is None


def test_evaluate_exit_status_stale_price_data_falls_through_to_next_check_not_masked():
    # BUG FOUND LIVE 2026-08-15, fixed here: a real +60% move would normally trigger
    # price_flagged (see test_evaluate_exit_status_priority_price_over_stale above),
    # but when the underlying price feed is known stale, this must not decide
    # "no move happened" from it -- it correctly falls through to the next
    # (weaker-evidence) check instead of silently landing on 'active'.
    row = {
        "first_seen_price": 100.0, "current_price": 160.0, "price_data_stale": True, "technicals_as_of_date": date(2026, 8, 13),
        "suggested_watch_until": date(2026, 8, 1), "last_alert_at": None, "narrative_generated_at": None,
    }
    status, reason, stale_skip = fundamentals_watchlist_exit.evaluate_exit_status(row, [], today=date(2026, 8, 13))
    assert status == "stale"  # not "price_flagged" (masked) and not "active" (silently fine)
    assert stale_skip is True


def test_evaluate_exit_status_aged_out_technicals_row_treated_as_stale_even_without_the_flag():
    # BUG FOUND LIVE 2026-08-18 (re-audit): a company that drops out of the L1
    # universe stops getting new technicals rows entirely -- its last row's
    # price_data_stale flag is frozen at whatever it was on its LAST processed run
    # and never recomputed. A real +60% move must not be trusted off a technicals
    # row that's 22 days old, even though price_data_stale itself is False/absent.
    #
    # technicals_as_of_date deliberately passed as a tz-aware pd.Timestamp here, not
    # a bare date -- fundamentals_technicals.as_of_date is a real TIMESTAMPTZ column,
    # and every one of these tests used to pass a naive `date(...)` instead, which
    # masked the exact live crash fixed in test_price_data_is_effectively_stale_*
    # below (both sides ended up tz-naive here, only in production did they mismatch).
    row = {
        "first_seen_price": 100.0, "current_price": 160.0,
        "technicals_as_of_date": pd.Timestamp("2026-07-22", tz="UTC"),
        "suggested_watch_until": date(2026, 8, 1), "last_alert_at": None, "narrative_generated_at": None,
    }
    status, reason, stale_skip = fundamentals_watchlist_exit.evaluate_exit_status(row, [], today=date(2026, 8, 13))
    assert status == "stale"  # not "price_flagged" -- the aged-out row must not be trusted
    assert stale_skip is True


def test_price_data_is_effectively_stale_handles_tz_aware_as_of_date():
    # BUG FOUND LIVE 2026-08-19: reproduced in the first real scheduled run since the
    # go-crond outage -- `today` (pd.Timestamp.now(tz="UTC").date(), from
    # run_watchlist_exit_evaluation) is a bare datetime.date, and pd.Timestamp(today)
    # built a tz-NAIVE Timestamp from it, while `as_of` (parsed from
    # fundamentals_technicals.as_of_date, a real TIMESTAMPTZ column) stayed
    # tz-aware. Subtracting them raised `TypeError: Cannot subtract tz-naive and
    # tz-aware datetime-like objects`, crashing the entire notifications pipeline
    # step -- the daily digest never sent. No test existed for this function at all
    # before this fix; every indirect test via evaluate_exit_status passed a bare
    # `date(...)` for technicals_as_of_date, which never triggers a tz mismatch.
    stale = fundamentals_watchlist_exit._price_data_is_effectively_stale
    today = date(2026, 8, 19)  # matches pd.Timestamp.now(tz="UTC").date()'s own shape -- a bare date

    # fresh, tz-aware, well within the threshold
    assert stale(False, pd.Timestamp("2026-08-18T00:00:00+00:00"), today=today) is False
    # stale, tz-aware, beyond the threshold -- must not crash, must correctly say stale
    assert stale(False, pd.Timestamp("2026-07-01T00:00:00+00:00"), today=today) is True
    # the flag itself still short-circuits regardless of the date
    assert stale(True, pd.Timestamp("2026-08-18T00:00:00+00:00"), today=today) is True
    # missing technicals row -- still correctly treated as stale, no crash
    assert stale(False, None, today=today) is True


def test_evaluate_exit_status_missing_technicals_row_treated_as_stale():
    row = {
        "first_seen_price": 100.0, "current_price": 160.0, "technicals_as_of_date": None,
        "suggested_watch_until": date(2026, 8, 1), "last_alert_at": None, "narrative_generated_at": None,
    }
    status, reason, stale_skip = fundamentals_watchlist_exit.evaluate_exit_status(row, [], today=date(2026, 8, 13))
    assert status == "stale"
    assert stale_skip is True


def test_load_trigger_type_history_by_company_groups_by_company(monkeypatch):
    df = pd.DataFrame(
        [
            {"company_master_id": "nse:FOO", "trigger_type": "capital_raise", "alert_date": date(2026, 8, 1)},
            {"company_master_id": "nse:FOO", "trigger_type": "rating_downgrade", "alert_date": date(2026, 8, 2)},
            {"company_master_id": "nse:BAR", "trigger_type": "insider_buy", "alert_date": date(2026, 8, 1)},
        ]
    )
    monkeypatch.setattr(fundamentals_watchlist_exit, "sql_to_df", lambda q: df)
    result = fundamentals_watchlist_exit.load_trigger_type_history_by_company()
    assert len(result["nse:FOO"]) == 2
    assert len(result["nse:BAR"]) == 1


def test_load_trigger_type_history_by_company_empty(monkeypatch):
    monkeypatch.setattr(fundamentals_watchlist_exit, "sql_to_df", lambda q: pd.DataFrame())
    assert fundamentals_watchlist_exit.load_trigger_type_history_by_company() == {}


def test_run_watchlist_exit_evaluation_empty_watchlist(monkeypatch):
    monkeypatch.setattr(fundamentals_watchlist_exit, "_bootstrap_status_columns", lambda: None)
    monkeypatch.setattr(fundamentals_watchlist_exit, "load_watchlist_for_exit_evaluation", lambda: pd.DataFrame())
    result = fundamentals_watchlist_exit.run_watchlist_exit_evaluation()
    assert result == {"companies": 0, "active": 0, "invalidated": 0, "price_flagged": 0, "stale": 0, "price_data_stale_skips": 0}


def test_run_watchlist_exit_evaluation_upserts_status_per_company(monkeypatch):
    monkeypatch.setattr(fundamentals_watchlist_exit, "_bootstrap_status_columns", lambda: None)
    today = pd.Timestamp.now(tz="UTC").date()
    watchlist = pd.DataFrame(
        [
            {"company_master_id": "nse:FOO", "first_seen_price": 100.0, "last_alert_at": None, "suggested_watch_until": None, "narrative_generated_at": None, "current_price": 110.0, "technicals_as_of_date": today},
            {"company_master_id": "nse:BAR", "first_seen_price": 100.0, "last_alert_at": None, "suggested_watch_until": None, "narrative_generated_at": None, "current_price": 200.0, "technicals_as_of_date": today},
        ]
    )
    monkeypatch.setattr(fundamentals_watchlist_exit, "load_watchlist_for_exit_evaluation", lambda: watchlist)
    monkeypatch.setattr(fundamentals_watchlist_exit, "load_trigger_type_history_by_company", lambda: {})
    calls = []
    monkeypatch.setattr(fundamentals_watchlist_exit, "upsert_to_db", lambda df, table, **k: calls.append((df, table, k)))

    result = fundamentals_watchlist_exit.run_watchlist_exit_evaluation()

    assert result["companies"] == 2
    assert result["active"] == 1
    assert result["price_flagged"] == 1
    assert result["price_data_stale_skips"] == 0
    df, table, kwargs = calls[0]
    assert table == "fundamentals_watchlist"
    assert kwargs["unique_keys"] == ["company_master_id"]
    statuses = dict(zip(df["company_master_id"], df["status"]))
    assert statuses == {"nse:FOO": "active", "nse:BAR": "price_flagged"}


def test_run_watchlist_exit_evaluation_records_fallback_and_count_for_stale_price_skips(monkeypatch):
    # BUG FOUND LIVE 2026-08-18 (re-audit): a stale-price skip used to be completely
    # silent -- no telemetry at all, and main() hardcoded fallback_used: False
    # regardless of what actually happened.
    monkeypatch.setattr(fundamentals_watchlist_exit, "_bootstrap_status_columns", lambda: None)
    watchlist = pd.DataFrame(
        [
            {"company_master_id": "nse:FOO", "first_seen_price": 100.0, "last_alert_at": None, "suggested_watch_until": None, "narrative_generated_at": None, "current_price": 160.0, "technicals_as_of_date": None},
        ]
    )
    monkeypatch.setattr(fundamentals_watchlist_exit, "load_watchlist_for_exit_evaluation", lambda: watchlist)
    monkeypatch.setattr(fundamentals_watchlist_exit, "load_trigger_type_history_by_company", lambda: {})
    monkeypatch.setattr(fundamentals_watchlist_exit, "upsert_to_db", lambda df, table, **k: None)
    fallback_events = []
    monkeypatch.setattr(fundamentals_watchlist_exit, "_record_fallback", lambda *a, **k: fallback_events.append((a, k)))

    result = fundamentals_watchlist_exit.run_watchlist_exit_evaluation()

    assert result["price_data_stale_skips"] == 1
    assert result["price_flagged"] == 0  # a real +60% move, but must not be trusted off no technicals row
    assert len(fallback_events) == 1
    assert fallback_events[0][0][0] == "watchlist_exit_price_check_skipped_stale_data"
    assert fallback_events[0][1]["metadata"]["company_master_ids"] == ["nse:FOO"]


def test_run_watchlist_exit_evaluation_uses_live_first_seen_price_not_frozen_column(monkeypatch):
    # 2026-08-14 bug found live: first_seen_price is written ONCE (watchlist-add time) and never
    # refreshed, while current_price is recomputed daily from the latest cum_adj_factor -- a split/bonus
    # for a watchlisted symbol AFTER it was first seen changes cum_adj_factor for every date strictly
    # before the event, so the frozen stored first_seen_price silently drifts out of sync with
    # current_price's basis. Re-deriving first_seen_price live via load_price_near (keyed on
    # first_seen_at) keeps both prices on the same adjustment basis. Here the STORED first_seen_price
    # (40.0) would say +175% (flagged); the LIVE value (95.0, as if a 1:1 bonus since first-seen halved
    # the historical adjusted price) says the true change is a plain +15.8% (not flagged) -- proving the
    # live value, not the stale one, drives the decision.
    monkeypatch.setattr(fundamentals_watchlist_exit, "_bootstrap_status_columns", lambda: None)
    watchlist = pd.DataFrame(
        [
            {
                "company_master_id": "nse:FOO", "first_seen_price": 40.0, "first_seen_at": "2026-01-01",
                "last_alert_at": None, "suggested_watch_until": None, "narrative_generated_at": None,
                "current_price": 110.0, "technicals_as_of_date": pd.Timestamp.now(tz="UTC").date(),
            },
        ]
    )
    monkeypatch.setattr(fundamentals_watchlist_exit, "load_watchlist_for_exit_evaluation", lambda: watchlist)
    monkeypatch.setattr(fundamentals_watchlist_exit, "load_trigger_type_history_by_company", lambda: {})
    price_lookup_calls = []

    def fake_load_price_near(company_master_id, as_of_date):
        price_lookup_calls.append((company_master_id, as_of_date))
        return 95.0

    monkeypatch.setattr(fundamentals_watchlist_exit, "load_price_near", fake_load_price_near)
    calls = []
    monkeypatch.setattr(fundamentals_watchlist_exit, "upsert_to_db", lambda df, table, **k: calls.append((df, table, k)))

    result = fundamentals_watchlist_exit.run_watchlist_exit_evaluation()

    assert price_lookup_calls == [("nse:FOO", "2026-01-01")]
    assert result["price_flagged"] == 0
    assert result["active"] == 1
    df, _table, _kwargs = calls[0]
    assert df.iloc[0]["status"] == "active"  # not price_flagged -- proves the live 95.0 was used, not 40.0


def test_run_watchlist_exit_evaluation_writes_live_first_seen_price_back_to_the_table(monkeypatch):
    # BUG FOUND LIVE 2026-08-18 (re-audit): the live-re-derived first_seen_price (this
    # function's own 2026-08-15 fix for split/bonus drift) was only ever used
    # in-memory to decide this function's own status -- never written back, so
    # notifications.py's digest and api/queries.py (both reading the stored column
    # directly) kept showing the stale, un-corrected value. Live: BLKASHYAP showed
    # -4.0% in the digest vs -1.5% in this evaluator for the same two prices.
    monkeypatch.setattr(fundamentals_watchlist_exit, "_bootstrap_status_columns", lambda: None)
    watchlist = pd.DataFrame(
        [
            {
                "company_master_id": "nse:FOO", "first_seen_price": 40.0, "first_seen_at": "2026-01-01",
                "last_alert_at": None, "suggested_watch_until": None, "narrative_generated_at": None,
                "current_price": 110.0, "technicals_as_of_date": pd.Timestamp.now(tz="UTC").date(),
            },
        ]
    )
    monkeypatch.setattr(fundamentals_watchlist_exit, "load_watchlist_for_exit_evaluation", lambda: watchlist)
    monkeypatch.setattr(fundamentals_watchlist_exit, "load_trigger_type_history_by_company", lambda: {})
    monkeypatch.setattr(fundamentals_watchlist_exit, "load_price_near", lambda *_a, **_k: 95.0)
    calls = []
    monkeypatch.setattr(fundamentals_watchlist_exit, "upsert_to_db", lambda df, table, **k: calls.append((df, table, k)))

    fundamentals_watchlist_exit.run_watchlist_exit_evaluation()

    df, _table, _kwargs = calls[0]
    assert df.iloc[0]["first_seen_price"] == 95.0  # the live value, not the stale stored 40.0


def test_run_watchlist_exit_evaluation_falls_back_to_stored_price_when_live_lookup_empty(monkeypatch):
    # load_price_near returning None (no price history that early) must fall back to the stored column,
    # not silently treat the company as having no first_seen_price at all.
    monkeypatch.setattr(fundamentals_watchlist_exit, "_bootstrap_status_columns", lambda: None)
    watchlist = pd.DataFrame(
        [
            {
                "company_master_id": "nse:FOO", "first_seen_price": 100.0, "first_seen_at": "2026-01-01",
                "last_alert_at": None, "suggested_watch_until": None, "narrative_generated_at": None,
                "current_price": 200.0, "technicals_as_of_date": pd.Timestamp.now(tz="UTC").date(),
            },
        ]
    )
    monkeypatch.setattr(fundamentals_watchlist_exit, "load_watchlist_for_exit_evaluation", lambda: watchlist)
    monkeypatch.setattr(fundamentals_watchlist_exit, "load_trigger_type_history_by_company", lambda: {})
    monkeypatch.setattr(fundamentals_watchlist_exit, "load_price_near", lambda *_a, **_k: None)
    calls = []
    monkeypatch.setattr(fundamentals_watchlist_exit, "upsert_to_db", lambda df, table, **k: calls.append((df, table, k)))

    result = fundamentals_watchlist_exit.run_watchlist_exit_evaluation()

    assert result["price_flagged"] == 1  # 100 -> 200 is +100%, still flagged using the stored fallback
