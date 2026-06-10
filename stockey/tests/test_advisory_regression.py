from __future__ import annotations

import argparse
import json
import os
import sys
import types
from datetime import date
from datetime import datetime

import pandas as pd

from advisory import action_recommender, adversarial_review, announcement_watch, company_memory_review, config_change_assistant, continuous_watch, cron_status, dashboard, decision_trace, event_data_quality, event_evidence_store, event_meta_model, event_model_artifact_store, event_model_data_prep, event_model_promotion_check, event_policy, event_policy_evaluator, event_router, execution_engine, exchange_events, exchange_features, external_task_queue, fallback_telemetry, hypothesis_engine, intraday_features, live_dashboard, llm_event_evaluator, macro_features, market_context, master_pipeline, model_training_runner, news_overlay_engine, news_theme_engine, news_watch, operator_health, operator_smoke, performance_slowlog, pipeline, portfolio_engine, position_lifecycle, prompt_registry, regime_engine, research_ledger, risk_engine, rule_engine, setup_registry, setup_trace, signal_quality_evaluator, signal_quality_promotion, signal_refresh, symbol_trace, technical_engine, technical_features, technical_threshold_calibration, technical_threshold_promotion, training_universe, ts_forecast_evaluator, ts_forecast_features, ts_forecast_workflow, wait_signals, watchlist_builder
from advisory import manual_review_state
from advisory import identity_issues
from advisory import superseded_failures
from advisory.api import app as operator_api
from data.announcements import pipeline as announcement_pipeline
from data.announcements import managed_pipeline as announcement_managed_pipeline
from data.announcements import state as announcement_state
from data.announcements.models import Announcement, CompanyMasterTarget, ParsedReport
from data.eaindustry import wpi
from data.dhanlive import auth as dhan_auth
from data.dhanlive import auth_cli as dhan_auth_cli
from data.dhanlive import client as dhan_client
from data.dhanlive import web_login as dhan_web_login
from data.dhanlive import dhan_db, ohlcv as dhan_ohlcv, scrip_master as dhan_scrip_master
from data.nseindia import bhavcopy_downloader, bhavcopy_parser, indices_downloader, indices_parser, offmarket, recent_events
from data.screenerin import auth as screener_auth
from data.mospi import cpi
from data.nsdl import fpi
from data import benchmark_sync, download_runner, download_queue
from utils import codex_cli
from utils import db as db_utils
from utils.ocr import llm_ocr
from utils import poppler as poppler_utils
from utils import redis_utils


def test_portfolio_engine_overlap_cap(monkeypatch):
    allocations = pd.DataFrame(
        [
            {
                "published_on": pd.Timestamp("2026-03-22T09:00:00Z"),
                "asof_date": pd.Timestamp("2026-03-22T00:00:00Z"),
                "setup_id": "TEST",
                "setup_name": "Test",
                "symbol": "HDFCBANK",
                "company_master_id": "nse:HDFCBANK",
                "unique_id": "u1",
                "confidence": 0.90,
                "conviction_bucket": "high",
                "risk_bucket": "medium",
                "suggested_allocation_inr": 40000.0,
                "allocation_pct_of_adv20d": 0.0001,
                "stop_price": 100.0,
                "invalidation_price": 95.0,
                "invalidation_rule": "rule",
                "notes": None,
                "context_snapshot_json": "{}",
            },
            {
                "published_on": pd.Timestamp("2026-03-22T10:00:00Z"),
                "asof_date": pd.Timestamp("2026-03-22T00:00:00Z"),
                "setup_id": "TEST",
                "setup_name": "Test",
                "symbol": "ICICIBANK",
                "company_master_id": "nse:ICICIBANK",
                "unique_id": "u2",
                "confidence": 0.88,
                "conviction_bucket": "high",
                "risk_bucket": "medium",
                "suggested_allocation_inr": 35000.0,
                "allocation_pct_of_adv20d": 0.0001,
                "stop_price": 100.0,
                "invalidation_price": 95.0,
                "invalidation_rule": "rule",
                "notes": None,
                "context_snapshot_json": "{}",
            },
            {
                "published_on": pd.Timestamp("2026-03-22T11:00:00Z"),
                "asof_date": pd.Timestamp("2026-03-22T00:00:00Z"),
                "setup_id": "TEST",
                "setup_name": "Test",
                "symbol": "RELIANCE",
                "company_master_id": "nse:RELIANCE",
                "unique_id": "u3",
                "confidence": 0.80,
                "conviction_bucket": "medium",
                "risk_bucket": "medium_high",
                "suggested_allocation_inr": 30000.0,
                "allocation_pct_of_adv20d": 0.0001,
                "stop_price": 100.0,
                "invalidation_price": 95.0,
                "invalidation_rule": "rule",
                "notes": None,
                "context_snapshot_json": "{}",
            },
        ]
    )

    monkeypatch.setattr(portfolio_engine, "load_allocations", lambda **kwargs: allocations.copy())
    monkeypatch.setattr(
        portfolio_engine,
        "build_overlap_map",
        lambda symbols: {
            "HDFCBANK": ("peer_cluster:1", "peer_cluster"),
            "ICICIBANK": ("peer_cluster:1", "peer_cluster"),
            "RELIANCE": ("symbol:RELIANCE", "symbol_only"),
        },
    )

    df = portfolio_engine.build_portfolio_orders(
        config=portfolio_engine.PortfolioConfig(
            capital_inr=120000.0,
            max_positions=3,
            single_position_cap_pct=1.0,
            per_setup_cap_pct=1.0,
            max_positions_per_overlap_group=1,
        )
    )

    status_map = df.set_index("symbol")["portfolio_status"].to_dict()
    reason_map = df.set_index("symbol")["portfolio_reason"].to_dict()
    assert status_map["HDFCBANK"] == "approved"
    assert status_map["ICICIBANK"] == "deferred"
    assert reason_map["ICICIBANK"] == "overlap_cap"
    assert status_map["RELIANCE"] == "approved"


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

    assert db_utils.with_db_retries(operation, attempts=3, operation_name="deadlock_test") == "ok"
    assert calls == {"operation": 3, "dispose": 2}


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

    out = db_utils.sql_to_df("select 1", retries=2)
    assert out.to_dict(orient="records") == [{"ok": 1}]
    assert calls["count"] == 3


def test_resilient_redis_retries_then_returns_safe_default(monkeypatch):
    calls = {"attempts": 0}

    class FailingRedis:
        def __init__(self, *args, **kwargs):
            pass

        def get(self, key):
            calls["attempts"] += 1
            raise redis_utils.redis.ConnectionError("connection refused")

    monkeypatch.setattr(redis_utils, "_ORIGINAL_REDIS_CLASS", FailingRedis)
    monkeypatch.setattr(redis_utils, "REDIS_OPERATION_ATTEMPTS", 3)
    monkeypatch.setattr(redis_utils.time, "sleep", lambda *_args, **_kwargs: None)
    client = redis_utils.ResilientRedis(host="127.0.0.1", port=6379, decode_responses=True, fail_soft=True)

    assert client.get("missing") is None
    assert calls["attempts"] == 3


def test_resilient_redis_enters_cooldown_after_failure(monkeypatch):
    calls = {"attempts": 0}

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

    client = redis_utils.ResilientRedis(host="127.0.0.1", port=6379, decode_responses=True, fail_soft=True)

    assert client.sadd("test", "value") == 0
    assert calls["attempts"] == 3

    assert client.sadd("test", "value2") == 0
    assert calls["attempts"] == 3


def test_screener_auth_auto_login_fills_credentials_and_reaches_dash():
    class FakePage:
        def __init__(self):
            self.url = "https://www.screener.in/login/"
            self.actions = []

        def goto(self, url, wait_until=None):
            self.actions.append(("goto", url, wait_until))
            self.url = url

        def wait_for_timeout(self, value):
            self.actions.append(("wait", value))

        def fill(self, selector, value):
            self.actions.append(("fill", selector, value))

        def click(self, selector):
            self.actions.append(("click", selector))
            self.url = "https://www.screener.in/dash/"

        def wait_for_url(self, pattern, timeout=None):
            self.actions.append(("wait_for_url", pattern, timeout))

    page = FakePage()

    screener_auth.auto_login(page, username="user@example.com", password="secret", wait_ms=5000)

    assert ("fill", screener_auth.USERNAME_SELECTOR, "user@example.com") in page.actions
    assert ("fill", screener_auth.PASSWORD_SELECTOR, "secret") in page.actions
    assert ("click", screener_auth.SUBMIT_SELECTOR) in page.actions
    assert page.url == "https://www.screener.in/dash/"


def test_screener_auth_is_logged_in_uses_login_redirect_to_dash():
    class FakePage:
        def __init__(self):
            self.url = ""

        def goto(self, url, wait_until=None):
            self.url = "https://www.screener.in/dash/"

        def wait_for_timeout(self, value):
            pass

    assert screener_auth.is_logged_in(FakePage()) is True


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


def test_dhan_web_login_generates_totp(monkeypatch):
    class FakeTOTP:
        def __init__(self, secret):
            self.secret = secret

        def now(self):
            return "111222"

    monkeypatch.setattr(dhan_web_login.pyotp, "TOTP", FakeTOTP)

    assert dhan_web_login.generate_totp("abc") == "111222"


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

    monkeypatch.setattr(dhan_client, "get_access_token", lambda: "OLD")
    monkeypatch.setattr(dhan_client, "force_refresh_access_token", lambda: "NEW")
    monkeypatch.setattr(dhan_client.requests, "Session", FakeSession)

    client = dhan_client.DhanHistoricalClient(auth_attempts=3)
    payload = client.validate_access_token()

    assert payload == {"status": "ok"}
    assert len(calls) == 2
    assert calls[0][2]["access-token"] == "OLD"
    assert calls[1][2]["access-token"] == "NEW"


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

    def fake_refresh():
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


def test_bhavcopy_parser_skips_db_parsed_dates(monkeypatch):
    processed: list[str] = []

    monkeypatch.setattr(
        bhavcopy_parser.store,
        "list_files",
        lambda prefix: iter(["bhavcopy/bhavcopy_2015-01-16.zip", "bhavcopy/bhavcopy_2015-01-17.zip"]),
    )
    monkeypatch.setattr(bhavcopy_parser, "should_consider_key", lambda key: True)
    monkeypatch.setattr(bhavcopy_parser, "get_processed_keys", lambda *_args, **_kwargs: set())
    monkeypatch.setattr(bhavcopy_parser, "get_failed_entries", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(
        bhavcopy_parser,
        "sql_to_df",
        lambda *_args, **_kwargs: pd.DataFrame({"parsed_date": [pd.Timestamp("2015-01-16")]}),
    )
    monkeypatch.setattr(bhavcopy_parser.store, "get_as_temp_file", lambda key: f"/tmp/{key.split('/')[-1]}")
    monkeypatch.setattr(bhavcopy_parser, "unzip_and_process", lambda file_path: processed.append(file_path))
    monkeypatch.setattr(bhavcopy_parser, "mark_processed", lambda *_args, **_kwargs: None)

    class DummyRedis:
        def sadd(self, *_args, **_kwargs):
            return 1

        def close(self):
            return None

    monkeypatch.setattr(bhavcopy_parser, "rop", DummyRedis())

    bhavcopy_parser.run_parser()

    assert processed == ["/tmp/bhavcopy_2015-01-17.zip"]


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

    monkeypatch.setattr(
        bhavcopy_parser.store,
        "list_files",
        lambda prefix: iter(["bhavcopy/bhavcopy_2015-01-16.zip"]),
    )
    monkeypatch.setattr(bhavcopy_parser, "should_consider_key", lambda key: True)
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

    class DummyRedis:
        def sadd(self, *_args, **_kwargs):
            raise AssertionError("should not mark redis success for failed parse")

        def close(self):
            return None

    monkeypatch.setattr(bhavcopy_parser, "rop", DummyRedis())

    bhavcopy_parser.run_parser()

    assert failed == [("bhavcopy", "bhavcopy/bhavcopy_2015-01-16.zip", "RuntimeError: broken nested zip")]


def test_bhavcopy_parser_marks_empty_like_key_processed(monkeypatch):
    processed: list[tuple[str, str]] = []

    monkeypatch.setattr(
        bhavcopy_parser.store,
        "list_files",
        lambda prefix: iter(["bhavcopy/bhavcopy_2015-10-18.zip"]),
    )
    monkeypatch.setattr(bhavcopy_parser, "should_consider_key", lambda key: True)
    monkeypatch.setattr(bhavcopy_parser, "get_processed_keys", lambda *_args, **_kwargs: set())
    monkeypatch.setattr(bhavcopy_parser, "get_failed_entries", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(bhavcopy_parser, "load_existing_ohlcv_dates", lambda *_args, **_kwargs: set())
    monkeypatch.setattr(bhavcopy_parser.store, "get_as_temp_file", lambda key: "/tmp/empty_like.zip")
    monkeypatch.setattr(bhavcopy_parser, "unzip_and_process", lambda path: False)
    monkeypatch.setattr(
        bhavcopy_parser,
        "mark_processed",
        lambda source_prefix, object_key, **_kwargs: processed.append((source_prefix, object_key)),
    )

    class DummyRedis:
        def sadd(self, *_args, **_kwargs):
            raise AssertionError("should not mark redis success for non-ohclv processed key")

        def close(self):
            return None

    monkeypatch.setattr(bhavcopy_parser, "rop", DummyRedis())

    bhavcopy_parser.run_parser()

    assert processed == [("bhavcopy", "bhavcopy/bhavcopy_2015-10-18.zip")]


def test_bhavcopy_parser_raises_on_bad_zip(monkeypatch, tmp_path):
    bad_zip = tmp_path / "bad.zip"
    bad_zip.write_text("not a zip", encoding="utf-8")

    try:
        bhavcopy_parser.unzip_and_process(str(bad_zip))
    except RuntimeError as exc:
        assert "bad_bhavcopy_zip" in str(exc)
    else:
        raise AssertionError("expected bad zip to raise RuntimeError")


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


def test_indices_parser_only_considers_last_year_keys():
    today = datetime(2026, 4, 13)

    assert indices_parser.should_consider_key("indices/indices_2026-04-12.zip", today=today)
    assert indices_parser.should_consider_key("indices/indices_2025-04-13.zip", today=today)
    assert not indices_parser.should_consider_key("indices/indices_2025-04-12.zip", today=today)
    assert not indices_parser.should_consider_key("indices/invalid.zip", today=today)


def test_indices_parser_records_failed_key(monkeypatch):
    failed: list[tuple[str, str, str]] = []

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

    class DummyRedis:
        def sadd(self, *_args, **_kwargs):
            raise AssertionError("should not mark redis success for failed indices parse")

        def close(self):
            return None

    monkeypatch.setattr(indices_parser, "rop", DummyRedis())

    # mirror module __main__ flow
    parsed_files = indices_parser.get_processed_keys(indices_parser.SOURCE_PREFIX)
    for f in indices_parser.store.list_files("indices"):
        if f in parsed_files:
            continue
        file_path = indices_parser.store.get_as_temp_file(f)
        try:
            parsed = indices_parser.unzip_and_process(file_path)
        except Exception as exc:
            indices_parser.mark_failed(indices_parser.SOURCE_PREFIX, f, f"{exc.__class__.__name__}: {exc}")
            continue
        if parsed:
            raise AssertionError("unexpected parsed success")

    assert failed == [("indices", "indices/indices_2015-01-16.zip", "RuntimeError: bad indices zip contents")]


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
        lambda playwright, formatted_date, rop: calls.append(formatted_date),
    )

    recent_events.main()

    assert len(calls) == 1


def test_recent_events_date_parser_falls_back_to_abbreviated_month():
    dates = recent_events.parse_event_dates(pd.Series(["01-June-2026", "01-Jun-2026"]))

    assert dates.tolist() == [pd.Timestamp("2026-06-01"), pd.Timestamp("2026-06-01")]


def test_cpi_sync_uses_db_months_only(monkeypatch):
    downloaded: list[str] = []

    monkeypatch.setattr(cpi, "load_existing_cpi_months", lambda: {date(2026, 1, 1)})
    monkeypatch.setattr(cpi, "get_db_max_date", lambda *args, **kwargs: pd.Timestamp("2026-01-01"))
    monkeypatch.setattr(cpi, "download_cpi_month", lambda month_start: downloaded.append(month_start.isoformat()))

    cpi.sync_cpi_data(from_date=date(2026, 1, 1), to_date=date(2026, 3, 1), force=False)

    assert downloaded == ["2026-02-01", "2026-03-01"]


def test_fpi_latest_downloaded_date_uses_db_anchor_only(monkeypatch):
    monkeypatch.setattr(fpi.futils, "downloaded_for", lambda today: (False, date(2026, 4, 10)))

    assert fpi.latest_downloaded_date(date(2026, 4, 11)) == date(2026, 4, 10)


def test_exchange_events_normalize_corporate_actions_handles_missing_side():
    df = pd.DataFrame(
        [
            {
                "symbol": "RELIANCE",
                "company_master_id": "nse:RELIANCE",
                "date": pd.Timestamp("2026-04-11"),
                "record_date": pd.Timestamp("2026-04-15"),
                "subject": "Dividend",
            }
        ]
    )

    out = exchange_events.normalize_corporate_actions(df)

    assert len(out) == 1
    assert pd.isna(out.iloc[0]["side"]) or out.iloc[0]["side"] is None


def test_exchange_events_normalize_corporate_actions_uses_action_type():
    df = pd.DataFrame(
        [
            {
                "source": "bc",
                "symbol": "RELIANCE",
                "date": pd.Timestamp("2026-04-11"),
                "record_date": pd.Timestamp("2026-04-15"),
                "subject": "Bonus 1:1",
                "action_type": "bonus",
            }
        ]
    )

    out = exchange_events.normalize_corporate_actions(df)

    assert out.iloc[0]["event_source"] == "nse_corporate_action"
    assert out.iloc[0]["event_type"] == "BONUS"
    assert "source=bc" in out.iloc[0]["event_summary"]


def test_exchange_events_repair_missing_event_types_dry_run(monkeypatch):
    queries: list[str] = []

    monkeypatch.setattr(exchange_events, "require_table_exists", lambda table_name: table_name == exchange_events.TABLE_NAME)

    def fake_sql_to_df(query, *args, **kwargs):
        queries.append(str(query))
        return pd.DataFrame([{"matched_rows": 3}])

    monkeypatch.setattr(exchange_events, "sql_to_df", fake_sql_to_df)
    monkeypatch.setattr(exchange_events, "db_session", lambda: (_ for _ in ()).throw(AssertionError("dry-run should not write")))

    result = exchange_events.repair_missing_event_types(dry_run=True)

    assert result["status"] == "dry_run"
    assert result["deal_rows"] == 3
    assert result["short_selling_rows"] == 3
    assert result["unclassified_rows"] == 0
    assert result["matched_rows"] == 6
    assert result["updated_rows"] == 0
    assert "event_source IS NULL OR event_type IS NULL" in queries[0]


def test_exchange_events_repair_missing_event_types_apply(monkeypatch):
    executed: list[str] = []

    class FakeCursor:
        rowcount = 2

        def execute(self, query, params=None):
            executed.append(str(query))

    class FakeSession:
        def __enter__(self):
            return object(), FakeCursor()

        def __exit__(self, exc_type, exc, tb):
            return False

    monkeypatch.setattr(exchange_events, "require_table_exists", lambda table_name: table_name == exchange_events.TABLE_NAME)
    query_counts = iter([pd.DataFrame([{"matched_rows": 2}]), pd.DataFrame([{"matched_rows": 4}]), pd.DataFrame([{"matched_rows": 10}])])
    monkeypatch.setattr(exchange_events, "sql_to_df", lambda *args, **kwargs: next(query_counts))
    monkeypatch.setattr(exchange_events, "db_session", lambda: FakeSession())

    result = exchange_events.repair_missing_event_types(dry_run=False)

    assert result["status"] == "applied"
    assert result["deal_rows"] == 2
    assert result["short_selling_rows"] == 4
    assert result["unclassified_rows"] == 4
    assert result["updated_rows"] == 6
    assert "nse_legacy_deal" in executed[0]
    assert "LEGACY_DEAL" in executed[0]
    assert "nse_short_selling" in executed[1]
    assert "SHORT_SELLING" in executed[1]
    assert "nse_unclassified_event" in executed[2]
    assert "UNCLASSIFIED_EVENT" in executed[2]


def test_exchange_events_persist_uses_timescale_safe_composite_key(monkeypatch):
    calls: list[dict[str, object]] = []

    monkeypatch.setattr(
        exchange_events,
        "upsert_to_db",
        lambda df, table_name, unique_keys, timescaledb_column=None: calls.append(
            {
                "table_name": table_name,
                "unique_keys": list(unique_keys),
                "timescaledb_column": timescaledb_column,
                "rows": len(df),
            }
        ),
    )

    df = pd.DataFrame(
        [
            {
                "event_id": "abc",
                "event_source": "nse_event_calendar",
                "event_type": "NSE_EVENT",
                "symbol": "RELIANCE",
                "company_master_id": "nse:RELIANCE",
                "event_date": pd.Timestamp("2026-04-11", tz="UTC"),
                "known_on": pd.Timestamp("2026-04-11", tz="UTC"),
                "disclosure_date": pd.Timestamp("2026-04-11", tz="UTC"),
                "participant": pd.NA,
                "side": pd.NA,
                "quantity": pd.NA,
                "price": pd.NA,
                "value_inr": pd.NA,
                "holding_pct_before": pd.NA,
                "holding_pct_after": pd.NA,
                "event_summary": "calendar",
                "raw_json": "{}",
                "load_ts": pd.Timestamp("2026-04-11", tz="UTC"),
            }
        ]
    )

    exchange_events.persist_exchange_events(df)

    assert calls == [
        {
            "table_name": "advisory_exchange_events",
            "unique_keys": ["event_id", "known_on"],
            "timescaledb_column": "known_on",
            "rows": 1,
        }
    ]


def test_wpi_parse_catalog_items_handles_weird_html():
    html = b"""
    <html><body>
      <ul class="ul-choose-item">
        <li>
          <input name="cname" value="A1"/>
          <span>Food</span>
          <input name="commname" value="(A). FOOD ARTICLES"/>
        </li>
      </ul>
      <div class="odd-layout">
        <input name="cname" value="B1"/>
        <div><input name="commname" value="(B). NON-FOOD ARTICLES"/></div>
      </div>
      <div>
        <input name="cname" value="B1"/>
        <input name="commname" value="(B). NON-FOOD ARTICLES"/>
      </div>
      <div>
        <input name="cname" value="X1"/>
        <input name="commname" value="INVALID"/>
      </div>
    </body></html>
    """

    assert wpi.parse_catalog_items(html) == [
        ["A1", "(A). FOOD ARTICLES", "(A). FOOD ARTICLES"],
        ["B1", "(B). NON-FOOD ARTICLES", "(B). NON-FOOD ARTICLES"],
    ]


def test_wpi_sync_uses_db_completion(monkeypatch):
    monkeypatch.setattr(
        wpi,
        "fetch_wpi_catalog",
        lambda year: (
            "session",
            {},
            [
                ["A1", "(A). FOOD ARTICLES", "(A). FOOD ARTICLES"],
                ["B1", "(B). NON-FOOD ARTICLES", "(B). NON-FOOD ARTICLES"],
            ],
        ),
    )

    completed_sets = [
        {"A1"},
        {"A1", "B1"},
    ]

    def fake_load_completed_items(year, *, expected_months):
        return completed_sets[0] if len(completed_sets) == 1 else completed_sets.pop(0)

    downloaded: list[str] = []
    monkeypatch.setattr(wpi, "load_completed_items", fake_load_completed_items)
    monkeypatch.setattr(
        wpi,
        "download_wpi_item",
        lambda **kwargs: pd.DataFrame(
            [{"date": pd.Timestamp("2026-02-28"), "value": 1.0, "cname": kwargs["item"][0], "name": kwargs["item"][1]}]
        ),
    )
    monkeypatch.setattr(wpi, "persist_wpi_item", lambda df, **kwargs: downloaded.append(kwargs["cname"]))
    monkeypatch.setattr(wpi.time, "sleep", lambda *_args, **_kwargs: None)

    summary = wpi.sync_wpi_for_year(2026, today=date(2026, 3, 25))

    assert downloaded == ["B1"]
    assert summary["skipped_count"] == 1
    assert summary["downloaded_count"] == 1


def test_portfolio_engine_keeps_only_highest_priority_symbol_owner(monkeypatch):
    allocations = pd.DataFrame(
        [
            {
                "published_on": pd.Timestamp("2026-03-22T09:00:00Z"),
                "asof_date": pd.Timestamp("2026-03-22T00:00:00Z"),
                "setup_id": "LARGECAP_BREAKOUT_POSITION_V1",
                "setup_name": "Largecap",
                "symbol": "ABC",
                "company_master_id": "nse:ABC",
                "unique_id": "u1",
                "confidence": 0.90,
                "conviction_bucket": "high",
                "risk_bucket": "medium",
                "suggested_allocation_inr": 40000.0,
                "allocation_pct_of_adv20d": 0.0001,
                "stop_price": 100.0,
                "invalidation_price": 95.0,
                "invalidation_rule": "rule",
                "notes": None,
                "context_snapshot_json": "{}",
            },
            {
                "published_on": pd.Timestamp("2026-03-22T10:00:00Z"),
                "asof_date": pd.Timestamp("2026-03-22T00:00:00Z"),
                "setup_id": "MIDCAP_IMPROVER_SWING_V1",
                "setup_name": "Midcap",
                "symbol": "XYZ",
                "company_master_id": "nse:XYZ",
                "unique_id": "u2",
                "confidence": 0.88,
                "conviction_bucket": "high",
                "risk_bucket": "medium",
                "suggested_allocation_inr": 35000.0,
                "allocation_pct_of_adv20d": 0.0001,
                "stop_price": 100.0,
                "invalidation_price": 95.0,
                "invalidation_rule": "rule",
                "notes": None,
                "context_snapshot_json": "{}",
            },
            {
                "published_on": pd.Timestamp("2026-03-22T11:00:00Z"),
                "asof_date": pd.Timestamp("2026-03-22T00:00:00Z"),
                "setup_id": "INTRADAY_BREAKOUT_TACTICAL_V1",
                "setup_name": "Intraday",
                "symbol": "ABC",
                "company_master_id": "nse:ABC",
                "unique_id": "u3",
                "confidence": 0.80,
                "conviction_bucket": "medium",
                "risk_bucket": "medium_high",
                "suggested_allocation_inr": 10000.0,
                "allocation_pct_of_adv20d": 0.0001,
                "stop_price": 100.0,
                "invalidation_price": 95.0,
                "invalidation_rule": "rule",
                "notes": None,
                "context_snapshot_json": "{}",
            },
        ]
    )

    monkeypatch.setattr(portfolio_engine, "load_allocations", lambda **kwargs: allocations.copy())
    monkeypatch.setattr(
        portfolio_engine,
        "build_overlap_map",
        lambda symbols: {
            "ABC": ("sector:IN0501", "sector_code"),
            "XYZ": ("sector:IN0501", "sector_code"),
        },
    )

    df = portfolio_engine.build_portfolio_orders(
        config=portfolio_engine.PortfolioConfig(
            capital_inr=120000.0,
            max_positions=5,
            single_position_cap_pct=1.0,
            per_setup_cap_pct=1.0,
            max_positions_per_overlap_group=1,
        )
    )

    assert sorted(df["symbol"].tolist()) == ["ABC", "XYZ"]
    owner_row = df[df["symbol"] == "ABC"].iloc[0]
    assert owner_row["setup_id"] == "LARGECAP_BREAKOUT_POSITION_V1"
    assert owner_row["portfolio_status"] == "approved"


def test_position_lifecycle_load_open_orders_dedupes_same_symbol(monkeypatch):
    sample = pd.DataFrame(
        [
            {
                "published_on": pd.Timestamp("2026-04-17T09:00:00Z"),
                "asof_date": pd.Timestamp("2026-04-17T00:00:00Z"),
                "setup_id": "LOWER",
                "symbol": "ABC",
                "portfolio_status": "trimmed",
                "approved_allocation_inr": 10000.0,
                "priority_score": 1.0,
            },
            {
                "published_on": pd.Timestamp("2026-04-17T10:00:00Z"),
                "asof_date": pd.Timestamp("2026-04-17T00:00:00Z"),
                "setup_id": "HIGHER",
                "symbol": "ABC",
                "portfolio_status": "approved",
                "approved_allocation_inr": 25000.0,
                "priority_score": 3.0,
            },
        ]
    )
    monkeypatch.setattr(position_lifecycle, "table_exists", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(position_lifecycle, "sql_to_df", lambda *_args, **_kwargs: sample.copy())

    out = position_lifecycle.load_open_orders(asof_date=pd.Timestamp("2026-04-17T00:00:00Z"))

    assert out[["symbol", "setup_id"]].to_dict(orient="records") == [{"symbol": "ABC", "setup_id": "HIGHER"}]


def test_portfolio_engine_overlap_limit_for_reason():
    config = portfolio_engine.PortfolioConfig(
        capital_inr=100000.0,
        max_positions=5,
        single_position_cap_pct=1.0,
        per_setup_cap_pct=1.0,
        max_positions_per_overlap_group=1,
    )
    assert portfolio_engine.overlap_limit_for_reason("same_symbol", config) == 1
    assert portfolio_engine.overlap_limit_for_reason("peer_cluster", config) == 1
    assert portfolio_engine.overlap_limit_for_reason("sector_code", config) == 2
    assert portfolio_engine.overlap_limit_for_reason("symbol_only", config) == 1


def test_position_lifecycle_classification_paths():
    hold_row = pd.Series(
        {
            "entry_price": 100.0,
            "current_price": 106.0,
            "stop_price": 95.0,
            "invalidation_price": 90.0,
            "pnl_pct": 6.0,
            "days_held": 5,
        }
    )
    status, _, action, _ = position_lifecycle.classify_position(
        hold_row,
        review_stale_days=20,
        tighten_stop_gain_pct=10.0,
        trim_winner_gain_pct=15.0,
    )
    assert status == "open"
    assert action == "hold"

    exit_row = pd.Series(
        {
            "entry_price": 100.0,
            "current_price": 88.0,
            "stop_price": 95.0,
            "invalidation_price": 90.0,
            "pnl_pct": -12.0,
            "days_held": 21,
        }
    )
    status, _, action, _ = position_lifecycle.classify_position(
        exit_row,
        review_stale_days=20,
        tighten_stop_gain_pct=10.0,
        trim_winner_gain_pct=15.0,
    )
    assert status == "exit_review"
    assert action == "exit_invalidation"


def test_position_lifecycle_trims_when_target_reached():
    row = pd.Series(
        {
            "entry_price": 100.0,
            "current_price": 121.0,
            "stop_price": 95.0,
            "invalidation_price": 90.0,
            "pnl_pct": 21.0,
            "days_held": 12,
            "recommended_target_price": 120.0,
        }
    )

    status, reason, action, action_reason = position_lifecycle.classify_position(
        row,
        review_stale_days=20,
        tighten_stop_gain_pct=10.0,
        trim_winner_gain_pct=25.0,
    )

    assert status == "open"
    assert reason == "Target zone reached."
    assert action == "trim_winner"
    assert "120.00" in action_reason


def test_position_lifecycle_time_stop_exits_loser_after_horizon():
    row = pd.Series(
        {
            "entry_price": 100.0,
            "current_price": 98.0,
            "stop_price": 94.0,
            "invalidation_price": 90.0,
            "pnl_pct": -2.0,
            "days_held": 45,
            "thesis_bucket": "TIME_HORIZON",
            "current_date": pd.Timestamp("2026-04-29T00:00:00Z"),
            "horizon_end_date": pd.Timestamp("2026-04-28T00:00:00Z"),
        }
    )

    status, _, action, action_reason = position_lifecycle.classify_position(
        row,
        review_stale_days=20,
        tighten_stop_gain_pct=10.0,
        trim_winner_gain_pct=15.0,
    )

    assert status == "review"
    assert action == "exit_time_stop"
    assert "no positive follow-through" in action_reason


def test_position_lifecycle_does_not_backdate_paper_entry(monkeypatch):
    orders = pd.DataFrame(
        [
            {
                "published_on": pd.Timestamp("2026-04-17T09:00:00Z"),
                "asof_date": pd.Timestamp("2026-04-17T00:00:00Z"),
                "setup_id": "TEST",
                "symbol": "ABC",
                "unique_id": "uid-abc",
                "portfolio_status": "approved",
                "approved_allocation_inr": 25000.0,
                "priority_score": 3.0,
                "stop_price": 95.0,
                "invalidation_price": 90.0,
                "thesis_bucket": "DATA_DEPENDENT",
            }
        ]
    )
    prices = pd.DataFrame(
        [
            {"symbol": "ABC", "date": pd.Timestamp("2026-04-15T00:00:00Z"), "close": 98.0},
            {"symbol": "ABC", "date": pd.Timestamp("2026-04-18T00:00:00Z"), "close": 101.0},
            {"symbol": "ABC", "date": pd.Timestamp("2026-04-20T00:00:00Z"), "close": 105.0},
        ]
    )

    monkeypatch.setattr(position_lifecycle, "load_open_orders", lambda **_kwargs: orders.copy())
    monkeypatch.setattr(position_lifecycle, "load_price_points", lambda *_args, **_kwargs: prices.copy())
    monkeypatch.setattr(position_lifecycle, "load_latest_technical_context", lambda *_args, **_kwargs: pd.DataFrame())

    lifecycle_df, actions_df = position_lifecycle.build_lifecycle_outputs(asof_date=pd.Timestamp("2026-04-20T00:00:00Z"))

    assert actions_df.empty
    row = lifecycle_df.iloc[0]
    assert row["entry_date"] == pd.Timestamp("2026-04-18T00:00:00Z")
    assert row["entry_price"] == 101.0
    assert row["current_price"] == 105.0
    assert row["days_held"] == 2
    context = json.loads(row["context_snapshot_json"])
    assert context["entry_assumption"] == "Paper entry uses the first available close on or after portfolio published_on."
    assert context["price_status"] == "priced"


def test_position_lifecycle_missing_post_approval_price_creates_manual_review_context(monkeypatch):
    orders = pd.DataFrame(
        [
            {
                "published_on": pd.Timestamp("2026-04-17T09:00:00Z"),
                "asof_date": pd.Timestamp("2026-04-17T00:00:00Z"),
                "setup_id": "TEST",
                "symbol": "ABC",
                "unique_id": "uid-abc",
                "portfolio_status": "approved",
                "approved_allocation_inr": 25000.0,
                "priority_score": 3.0,
                "stop_price": 95.0,
                "invalidation_price": 90.0,
                "thesis_bucket": "DATA_DEPENDENT",
            }
        ]
    )
    prices = pd.DataFrame(
        [{"symbol": "ABC", "date": pd.Timestamp("2026-04-15T00:00:00Z"), "close": 98.0}]
    )

    monkeypatch.setattr(position_lifecycle, "load_open_orders", lambda **_kwargs: orders.copy())
    monkeypatch.setattr(position_lifecycle, "load_price_points", lambda *_args, **_kwargs: prices.copy())
    monkeypatch.setattr(position_lifecycle, "load_latest_technical_context", lambda *_args, **_kwargs: pd.DataFrame())

    lifecycle_df, actions_df = position_lifecycle.build_lifecycle_outputs(asof_date=pd.Timestamp("2026-04-20T00:00:00Z"))

    lifecycle_row = lifecycle_df.iloc[0]
    action_row = actions_df.iloc[0]
    assert lifecycle_row["position_status"] == "pending_entry"
    assert lifecycle_row["next_action"] == "review_manual"
    assert "Missing entry/current price" in lifecycle_row["next_action_reason"]
    assert action_row["suggested_action"] == "review_manual"
    assert action_row["execution_mode"] == "review_only"
    context = json.loads(lifecycle_row["context_snapshot_json"])
    assert context["entry_date"] is None
    assert context["current_date"] == "2026-04-15T00:00:00+00:00"
    assert context["price_status"] == "missing_entry_or_current_price"
    assert context["entry_assumption"] == "No paper entry assumed because no close exists on or after portfolio published_on."
    assert context["stop_status"] == "stop_available"
    assert context["target_status"] == "target_missing"
    assert context["operator_question"] == "Missing entry/current price prevents automated lifecycle management."


def test_execution_engine_builds_planned_orders(monkeypatch):
    monkeypatch.setattr(execution_engine, "DEFAULT_ALLOW_LEGACY_EXECUTION_FALLBACK", True)
    portfolio_orders = pd.DataFrame(
        [
            {
                "published_on": pd.Timestamp("2026-03-23T09:00:00Z"),
                "asof_date": pd.Timestamp("2026-03-23T00:00:00Z"),
                "setup_id": "TEST",
                "symbol": "HDFCBANK",
                "unique_id": "uid-1",
                "company_master_id": "nse:HDFCBANK",
                "approved_allocation_inr": 40000.0,
            }
        ]
    )
    latest_prices = pd.DataFrame(
        [{"symbol": "HDFCBANK", "price_asof": pd.Timestamp("2026-03-23T09:15:00Z"), "price": 800.0, "price_source": "intraday"}]
    )

    monkeypatch.setattr(execution_engine, "load_portfolio_orders", lambda **kwargs: portfolio_orders.copy())
    monkeypatch.setattr(execution_engine, "load_exit_actions", lambda **kwargs: pd.DataFrame())
    monkeypatch.setattr(execution_engine, "load_action_recommendations", lambda **kwargs: pd.DataFrame())
    monkeypatch.setattr(execution_engine, "build_action_recommendations", lambda **kwargs: pd.DataFrame())
    monkeypatch.setattr(execution_engine, "load_latest_execution_prices", lambda symbols, asof_date: latest_prices.copy())
    monkeypatch.setattr(
        execution_engine,
        "resolve_dhan_identity",
        lambda symbol, exchange, asset_type="stock": {
            "security_id": 1333,
            "exchange_segment": "NSE_EQ",
        },
    )

    df = execution_engine.build_execution_orders(asof_date=pd.Timestamp("2026-03-23T00:00:00Z"))
    row = df.iloc[0]
    assert row["execution_status"] == "planned"
    assert row["quantity"] == 50
    assert row["reference_price_source"] == "intraday"
    assert row["estimated_order_value_inr"] == 40000.0
    assert row["security_id"] == 1333
    assert row["exchange_segment"] == "NSE_EQ"
    assert len(row["correlation_id"]) <= 30


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


def test_execution_engine_blocks_action_rows_without_complete_reason_contract(monkeypatch):
    action_rows = pd.DataFrame(
        [
            {
                "published_on": pd.Timestamp("2026-05-01T09:00:00Z"),
                "asof_date": pd.Timestamp("2026-05-01T00:00:00Z"),
                "setup_id": "SETUP",
                "symbol": "HDFCBANK",
                "unique_id": "action-1",
                "action_code": "BUY",
                "transaction_type": "BUY",
                "execution_mode": "broker_order",
                "approved_allocation_inr": 40000.0,
                "reason_contract_status": "incomplete_downgraded",
            }
        ]
    )
    latest_prices = pd.DataFrame(
        [{"symbol": "HDFCBANK", "price_asof": pd.Timestamp("2026-05-01T09:15:00Z"), "price": 800.0, "price_source": "intraday"}]
    )
    identity_calls = []

    monkeypatch.setattr(execution_engine, "load_action_recommendations", lambda **kwargs: action_rows.copy())
    monkeypatch.setattr(execution_engine, "build_action_recommendations", lambda **kwargs: pd.DataFrame())
    monkeypatch.setattr(execution_engine, "load_portfolio_orders", lambda **kwargs: pd.DataFrame())
    monkeypatch.setattr(execution_engine, "load_exit_actions", lambda **kwargs: pd.DataFrame())
    monkeypatch.setattr(execution_engine, "load_latest_execution_prices", lambda symbols, asof_date: latest_prices.copy())
    monkeypatch.setattr(execution_engine, "resolve_dhan_identity", lambda *args, **kwargs: identity_calls.append(args) or {"security_id": 1333, "exchange_segment": "NSE_EQ"})

    df = execution_engine.build_execution_orders(asof_date=pd.Timestamp("2026-05-01T00:00:00Z"))
    row = df.iloc[0]

    assert row["execution_status"] == "submit_blocked"
    assert "Reason contract is not complete" in row["execution_reason"]
    assert row["quantity"] == 0
    assert identity_calls == []


def test_execution_engine_marks_action_order_preview_as_approval_gated(monkeypatch):
    action_rows = pd.DataFrame(
        [
            {
                "published_on": pd.Timestamp("2026-05-01T09:00:00Z"),
                "asof_date": pd.Timestamp("2026-05-01T00:00:00Z"),
                "setup_id": "SETUP",
                "symbol": "HDFCBANK",
                "unique_id": "action-1",
                "action_code": "BUY",
                "transaction_type": "BUY",
                "execution_mode": "broker_order",
                "approved_allocation_inr": 40000.0,
                "reason_contract_status": "complete",
                "action_status": "approved",
            }
        ]
    )
    latest_prices = pd.DataFrame(
        [{"symbol": "HDFCBANK", "price_asof": pd.Timestamp("2026-05-01T09:15:00Z"), "price": 800.0, "price_source": "intraday"}]
    )

    monkeypatch.setattr(execution_engine, "load_action_recommendations", lambda **kwargs: action_rows.copy())
    monkeypatch.setattr(execution_engine, "build_action_recommendations", lambda **kwargs: pd.DataFrame())
    monkeypatch.setattr(execution_engine, "load_portfolio_orders", lambda **kwargs: pd.DataFrame())
    monkeypatch.setattr(execution_engine, "load_exit_actions", lambda **kwargs: pd.DataFrame())
    monkeypatch.setattr(execution_engine, "load_latest_execution_prices", lambda symbols, asof_date: latest_prices.copy())
    monkeypatch.setattr(execution_engine, "resolve_dhan_identity", lambda *args, **kwargs: {"security_id": 1333, "exchange_segment": "NSE_EQ"})

    df = execution_engine.build_execution_orders(asof_date=pd.Timestamp("2026-05-01T00:00:00Z"))
    row = df.iloc[0]
    safety = json.loads(row["safety_checks_json"])
    raw = json.loads(row["raw_broker_json"])

    assert row["execution_status"] == "planned"
    assert row["quantity"] == 50
    assert safety["operator_approval_required"] is True
    assert safety["operator_approval_status"] == "missing"
    assert safety["broker_reconciliation_required"] is True
    assert safety["broker_reconciliation_status"] == "not_run"
    assert safety["live_submission_allowed"] is False
    assert raw["execution_safety_contract"] == safety


def test_execution_engine_blocks_closed_or_expired_action_rows(monkeypatch):
    action_rows = pd.DataFrame(
        [
            {
                "published_on": pd.Timestamp("2026-05-01T09:00:00Z"),
                "asof_date": pd.Timestamp("2026-05-01T00:00:00Z"),
                "setup_id": "SETUP",
                "symbol": "HDFCBANK",
                "unique_id": "closed-action",
                "action_code": "BUY",
                "transaction_type": "BUY",
                "execution_mode": "broker_order",
                "approved_allocation_inr": 40000.0,
                "reason_contract_status": "complete",
                "action_status": "manual_closed",
                "closed_at": pd.Timestamp("2026-05-01T10:00:00Z"),
                "valid_until": pd.Timestamp("2026-04-30T15:30:00Z"),
            }
        ]
    )
    latest_prices = pd.DataFrame(
        [{"symbol": "HDFCBANK", "price_asof": pd.Timestamp("2026-05-01T09:15:00Z"), "price": 800.0, "price_source": "intraday"}]
    )
    identity_calls = []

    monkeypatch.setattr(execution_engine, "load_action_recommendations", lambda **kwargs: action_rows.copy())
    monkeypatch.setattr(execution_engine, "build_action_recommendations", lambda **kwargs: pd.DataFrame())
    monkeypatch.setattr(execution_engine, "load_portfolio_orders", lambda **kwargs: pd.DataFrame())
    monkeypatch.setattr(execution_engine, "load_exit_actions", lambda **kwargs: pd.DataFrame())
    monkeypatch.setattr(execution_engine, "load_latest_execution_prices", lambda symbols, asof_date: latest_prices.copy())
    monkeypatch.setattr(execution_engine, "resolve_dhan_identity", lambda *args, **kwargs: identity_calls.append(args) or {"security_id": 1333, "exchange_segment": "NSE_EQ"})

    df = execution_engine.build_execution_orders(asof_date=pd.Timestamp("2026-05-01T00:00:00Z"))
    row = df.iloc[0]
    safety = json.loads(row["safety_checks_json"])

    assert row["execution_status"] == "submit_blocked"
    assert row["quantity"] == 0
    assert "manual_closed" in row["execution_reason"]
    assert "closed_at" in row["execution_reason"]
    assert "expired" in row["execution_reason"]
    assert safety["issues"]
    assert identity_calls == []


def test_submit_live_orders_requires_operator_approval_and_reconciliation(monkeypatch):
    monkeypatch.setenv("STOCKEY_LIVE_TRADING_ENABLED", "true")
    monkeypatch.setenv("STOCKEY_EXECUTION_MAX_LIVE_ORDERS_PER_RUN", "2")
    monkeypatch.setenv("STOCKEY_EXECUTION_MAX_ORDER_VALUE_INR", "50000")
    monkeypatch.setenv("STOCKEY_EXECUTION_REQUIRE_FRESH_INTRADAY_PRICE", "false")

    class DummyClient:
        def __init__(self):
            raise AssertionError("unapproved order should not create broker client")

    monkeypatch.setattr(execution_engine, "DhanTradingClient", DummyClient)
    df = pd.DataFrame(
        [
            {
                "execution_status": "planned",
                "execution_reason": None,
                "transaction_type": "BUY",
                "security_id": 1333,
                "quantity": 10,
                "reference_price": 100.0,
                "reference_price_source": "intraday",
                "reference_price_asof": pd.Timestamp.utcnow(),
                "estimated_order_value_inr": 1000.0,
                "live_mode": False,
                "safety_checks_json": json.dumps(
                    execution_engine.build_execution_plan_safety_contract(
                        source="action_recommendation",
                        approval_status="missing",
                        reconciliation_status="not_run",
                    )
                ),
            }
        ]
    )

    out = execution_engine.submit_live_orders(df)

    assert out.iloc[0]["execution_status"] == "submit_blocked"
    assert "Operator approval is required" in out.iloc[0]["execution_reason"]


def test_execution_engine_uses_broker_cash_cap_and_exit_holdings(monkeypatch):
    monkeypatch.setattr(execution_engine, "DEFAULT_ALLOW_LEGACY_EXECUTION_FALLBACK", True)
    portfolio_orders = pd.DataFrame(
        [
            {
                "published_on": pd.Timestamp("2026-03-23T09:00:00Z"),
                "asof_date": pd.Timestamp("2026-03-23T00:00:00Z"),
                "setup_id": "BUY_SETUP",
                "symbol": "HDFCBANK",
                "unique_id": "buy-1",
                "company_master_id": "nse:HDFCBANK",
                "approved_allocation_inr": 40000.0,
                "invest_score_pct": 88.0,
            }
        ]
    )
    exit_actions = pd.DataFrame(
        [
            {
                "published_on": pd.Timestamp("2026-03-23T11:00:00Z"),
                "asof_date": pd.Timestamp("2026-03-23T00:00:00Z"),
                "setup_id": "EXIT_SETUP",
                "symbol": "ICICIBANK",
                "unique_id": "exit-1",
                "suggested_action": "exit_stop",
            }
        ]
    )
    latest_prices = pd.DataFrame(
        [
            {"symbol": "HDFCBANK", "price_asof": pd.Timestamp("2026-03-23T09:15:00Z"), "price": 800.0, "price_source": "intraday"},
            {"symbol": "ICICIBANK", "price_asof": pd.Timestamp("2026-03-23T09:15:00Z"), "price": 1200.0, "price_source": "intraday"},
        ]
    )

    monkeypatch.setattr(execution_engine, "load_portfolio_orders", lambda **kwargs: portfolio_orders.copy())
    monkeypatch.setattr(execution_engine, "load_exit_actions", lambda **kwargs: exit_actions.copy())
    monkeypatch.setattr(execution_engine, "load_action_recommendations", lambda **kwargs: pd.DataFrame())
    monkeypatch.setattr(execution_engine, "build_action_recommendations", lambda **kwargs: pd.DataFrame())
    monkeypatch.setattr(execution_engine, "load_latest_execution_prices", lambda symbols, asof_date: latest_prices.copy())
    monkeypatch.setattr(
        execution_engine,
        "resolve_dhan_identity",
        lambda symbol, exchange, asset_type="stock": {
            "security_id": 1333 if symbol == "HDFCBANK" else 1444,
            "exchange_segment": "NSE_EQ",
        },
    )

    class DummyClient:
        pass

    monkeypatch.setattr(execution_engine, "DhanTradingClient", DummyClient)
    monkeypatch.setattr(
        execution_engine,
        "load_live_account_budget",
        lambda client, strict=False: (
            10000.0,
            pd.DataFrame([{"symbol": "ICICIBANK", "security_id": 1444, "available_quantity": 7}]),
        ),
    )

    df = execution_engine.build_execution_orders(
        asof_date=pd.Timestamp("2026-03-23T00:00:00Z"),
        use_broker_account=True,
    )
    buy_row = df[df["transaction_type"] == "BUY"].iloc[0]
    sell_row = df[df["transaction_type"] == "SELL"].iloc[0]
    assert buy_row["quantity"] == 12
    assert buy_row["execution_status"] == "planned"
    assert buy_row["invest_score_pct"] == 88.0
    assert sell_row["quantity"] == 7
    assert sell_row["execution_status"] == "planned"


def test_execution_engine_normalizes_broker_account_budget_from_nested_payloads():
    class DummyClient:
        def get_fund_limits(self):
            return {"data": {"availabelBalance": "12345.50"}}

        def get_holdings(self):
            return {
                "data": [
                    {"tradingSymbol": "HDFCBANK", "securityId": "1333", "availableQty": "3"},
                    {"tradingSymbol": "HDFCBANK", "securityId": "1333", "availableQty": "2"},
                ]
            }

        def get_positions(self):
            return {"positions": [{"symbol": "ICICIBANK", "security_id": "1444", "netQty": "7"}]}

    cash, inventory = execution_engine.load_live_account_budget(DummyClient(), strict=True)

    assert cash == 12345.50
    rows = inventory.set_index("symbol").to_dict(orient="index")
    assert rows["HDFCBANK"]["available_quantity"] == 5
    assert rows["HDFCBANK"]["security_id"] == 1333
    assert rows["ICICIBANK"]["available_quantity"] == 7
    assert rows["ICICIBANK"]["security_id"] == 1444


def test_reconcile_live_orders_maps_broker_status_and_fills(monkeypatch):
    targets = pd.DataFrame(
        [
            {
                "published_on": pd.Timestamp("2026-05-01T09:00:00Z"),
                "asof_date": pd.Timestamp("2026-05-01T00:00:00Z"),
                "setup_id": "SETUP",
                "symbol": "HDFCBANK",
                "unique_id": "order-1",
                "correlation_id": "BUY-HDFCBANK-order-1",
                "broker_order_id": None,
                "exchange_order_id": None,
                "broker_order_status": None,
                "execution_status": "submitted",
                "execution_reason": None,
                "filled_quantity": 0,
            }
        ]
    )

    class DummyClient:
        def get_order_by_correlation_id(self, correlation_id):
            assert correlation_id == "BUY-HDFCBANK-order-1"
            return {
                "orderId": "broker-1",
                "exchangeOrderId": "exchange-1",
                "orderStatus": "TRADED",
                "filledQuantity": 10,
            }

        def get_order_by_id(self, order_id):
            raise AssertionError(f"unexpected order id lookup: {order_id}")

        def get_trades_by_order_id(self, order_id):
            assert order_id == "broker-1"
            return [
                {
                    "exchangeTradeId": "trade-1",
                    "tradedQuantity": 6,
                    "tradedPrice": 801.5,
                    "exchangeTime": "2026-05-01T09:31:00Z",
                },
                {
                    "exchangeTradeId": "trade-2",
                    "tradedQuantity": 4,
                    "tradedPrice": 802.0,
                    "exchangeTime": "2026-05-01T09:32:00Z",
                },
            ]

    monkeypatch.setattr(execution_engine, "load_recon_targets", lambda **kwargs: targets.copy())
    monkeypatch.setattr(execution_engine, "DhanTradingClient", DummyClient)

    order_df, fills_df = execution_engine.reconcile_live_orders(asof_date=pd.Timestamp("2026-05-01T00:00:00Z"))

    order = order_df.iloc[0]
    assert order["execution_status"] == "filled"
    assert order["broker_order_id"] == "broker-1"
    assert order["exchange_order_id"] == "exchange-1"
    assert order["filled_quantity"] == 10
    assert len(fills_df) == 2
    assert fills_df["traded_quantity"].sum() == 10
    assert fills_df.iloc[0]["exchange_trade_id"] == "trade-1"


def test_persist_reconciliation_marks_safety_contract_reconciled(monkeypatch):
    written: dict[str, pd.DataFrame] = {}
    traced: list[tuple[str, int]] = []
    safety = execution_engine.build_execution_plan_safety_contract(
        source="action_recommendation",
        action_status="approved",
        approval_status="approved",
        reconciliation_status="not_run",
    )
    order_df = pd.DataFrame(
        [
            {
                "published_on": pd.Timestamp("2026-05-01T09:00:00Z"),
                "asof_date": pd.Timestamp("2026-05-01T00:00:00Z"),
                "setup_id": "SETUP",
                "symbol": "HDFCBANK",
                "unique_id": "order-1",
                "correlation_id": "BUY-HDFCBANK-order-1",
                "broker_order_id": "broker-1",
                "exchange_order_id": "exchange-1",
                "broker_order_status": "TRADED",
                "execution_status": "filled",
                "execution_reason": None,
                "filled_quantity": "10",
                "quantity": "10",
                "safety_checks_json": json.dumps(safety),
                "raw_broker_json": json.dumps({"orderId": "broker-1", "orderStatus": "TRADED"}),
                "broker_update_time": "2026-05-01T09:35:00Z",
            }
        ]
    )
    fills_df = pd.DataFrame(
        [
            {
                "published_on": pd.Timestamp("2026-05-01T09:00:00Z"),
                "asof_date": pd.Timestamp("2026-05-01T00:00:00Z"),
                "setup_id": "SETUP",
                "symbol": "HDFCBANK",
                "unique_id": "order-1",
                "correlation_id": "BUY-HDFCBANK-order-1",
                "broker_order_id": "broker-1",
                "exchange_trade_id": "trade-1",
                "traded_quantity": "10",
                "traded_price": "801.5",
                "exchange_time": "2026-05-01T09:31:00Z",
                "raw_trade_json": "{}",
                "load_ts": "2026-05-01T09:35:00Z",
            }
        ]
    )

    monkeypatch.setattr(execution_engine, "ensure_execution_tables", lambda: None)
    monkeypatch.setattr(execution_engine, "_trace_execution_rows", lambda df, *, stage, step_idx: traced.append((stage, step_idx)))
    monkeypatch.setattr(
        execution_engine,
        "upsert_to_db",
        lambda df, table, **kwargs: written.setdefault(table, df.copy()),
    )

    execution_engine.persist_reconciliation(order_df, fills_df)

    persisted_order = written[execution_engine.EXECUTION_TABLE].iloc[0]
    persisted_safety = json.loads(persisted_order["safety_checks_json"])
    persisted_raw = json.loads(persisted_order["raw_broker_json"])
    persisted_fill = written[execution_engine.FILLS_TABLE].iloc[0]

    assert persisted_safety["broker_reconciliation_status"] == "reconciled"
    assert persisted_safety["broker_reconciliation_source"] == "broker_order_state"
    assert persisted_safety["broker_reconciliation_broker_order_status"] == "TRADED"
    assert persisted_safety["broker_reconciliation_broker_order_id"] == "broker-1"
    assert persisted_safety["live_submission_allowed"] is False
    assert persisted_raw["execution_safety_contract"] == persisted_safety
    assert persisted_order["filled_quantity"] == 10
    assert persisted_fill["traded_quantity"] == 10
    assert persisted_fill["traded_price"] == 801.5
    assert traced == [("execution_reconciliation", 63)]


def test_submit_live_orders_is_fail_closed_without_env(monkeypatch):
    called = {"client": False}

    class DummyClient:
        def __init__(self):
            called["client"] = True

    monkeypatch.delenv("STOCKEY_LIVE_TRADING_ENABLED", raising=False)
    monkeypatch.setattr(execution_engine, "DhanTradingClient", DummyClient)
    df = pd.DataFrame(
        [
            {
                "execution_status": "planned",
                "execution_reason": None,
                "transaction_type": "BUY",
                "security_id": 1333,
                "quantity": 1,
                "reference_price": 100.0,
                "reference_price_source": "intraday",
                "reference_price_asof": pd.Timestamp.utcnow(),
                "estimated_order_value_inr": 100.0,
                "live_mode": False,
            }
        ]
    )

    out = execution_engine.submit_live_orders(df)

    assert out.iloc[0]["execution_status"] == "submit_blocked"
    assert "Live trading disabled" in out.iloc[0]["execution_reason"]
    assert called["client"] is False


def test_submit_live_orders_enforces_caps_and_fresh_price(monkeypatch):
    monkeypatch.setenv("STOCKEY_LIVE_TRADING_ENABLED", "true")
    monkeypatch.setenv("STOCKEY_EXECUTION_MAX_LIVE_ORDERS_PER_RUN", "2")
    monkeypatch.setenv("STOCKEY_EXECUTION_MAX_ORDER_VALUE_INR", "500")
    monkeypatch.setenv("STOCKEY_EXECUTION_REQUIRE_FRESH_INTRADAY_PRICE", "true")
    monkeypatch.setenv("STOCKEY_EXECUTION_MAX_INTRADAY_PRICE_AGE_MINUTES", "30")

    class DummyClient:
        def place_order(self, **kwargs):
            raise AssertionError("blocked order should not submit")

    monkeypatch.setattr(execution_engine, "DhanTradingClient", DummyClient)
    df = pd.DataFrame(
        [
            {
                "execution_status": "planned",
                "execution_reason": None,
                "transaction_type": "BUY",
                "security_id": 1333,
                "quantity": 10,
                "reference_price": 100.0,
                "reference_price_source": "daily_close",
                "reference_price_asof": pd.Timestamp("2026-03-23T00:00:00Z"),
                "estimated_order_value_inr": 1000.0,
                "live_mode": False,
            }
        ]
    )

    out = execution_engine.submit_live_orders(df)

    assert out.iloc[0]["execution_status"] == "submit_blocked"
    assert "Estimated order value exceeds live cap" in out.iloc[0]["execution_reason"]


def test_pipeline_portfolio_stage_outputs_json(monkeypatch, capsys):
    portfolio_df = pd.DataFrame(
        [
            {
                "published_on": pd.Timestamp("2026-03-23T09:00:00Z"),
                "symbol": "HDFCBANK",
                "approved_allocation_inr": 40000.0,
            }
        ]
    )

    monkeypatch.setattr(
        pipeline,
        "parse_args",
        lambda: argparse.Namespace(
            date=None,
            symbols=None,
            setup_ids=None,
            start_at="portfolio",
            stop_at="portfolio",
            rebuild=False,
            skip_peer_sync=False,
            include_watch=False,
            include_news=False,
            include_lifecycle=False,
            include_execution=False,
            live_execution=False,
            execution_reconcile=False,
            eval_include_evaluated=False,
            portfolio_capital_inr=300000.0,
            portfolio_max_positions=5,
            portfolio_single_position_cap_pcnt=0.35,
            portfolio_single_position_cap_pct=0.35,
            portfolio_per_setup_cap_pct=0.50,
            portfolio_max_positions_per_overlap_group=1,
            event_model=None,
            dry_run=True,
        ),
    )
    monkeypatch.setattr(pipeline, "build_portfolio_orders", lambda **kwargs: portfolio_df.copy())

    rc = pipeline.main()
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["status"] == "ok"
    assert "portfolio" in out["stages"]
    assert out["stages"]["portfolio"]["row_count"] == 1


def test_news_watch_matches_symbol_in_title(monkeypatch):
    watchlist = pd.DataFrame(
        [
            {
                "asof_date": pd.Timestamp("2026-03-24T00:00:00Z"),
                "setup_id": "TEST",
                "setup_name": "Test",
                "symbol": "IGL",
                "company_master_id": "nse:IGL",
                "watch_reasons_json": '["test"]',
            }
        ]
    )
    news_items = pd.DataFrame(
        [
            {
                "source_name": "Economic Times RSS",
                "feed_name": "stocks",
                "guid": "guid-1",
                "title": "Castrol India, IGL among 5 energy stocks that hit 52-week lows",
                "link": "https://example.test/igl",
                "description": "A short ET item.",
                "categories_json": "[]",
                "published_on": pd.Timestamp("2026-03-24T11:12:46Z"),
            }
        ]
    )

    monkeypatch.setattr(
        news_watch,
        "load_watch_company_meta",
        lambda company_master_ids: pd.DataFrame(
            [
                {
                    "company_master_id": "nse:IGL",
                    "nse_ticker": "IGL",
                    "bse_ticker": None,
                    "company_name": "Indraprastha Gas Ltd.",
                }
            ]
        ),
    )

    df = news_watch.build_news_events(watchlist=watchlist, news_items=news_items)
    assert len(df) == 1
    row = df.iloc[0]
    assert row["symbol"] == "IGL"
    assert row["event_source"] == "economic_times_rss"
    assert row["match_score"] >= 4.0


def test_news_watch_marks_non_material_top_context_news_observed(monkeypatch):
    watchlist = pd.DataFrame(
        [
            {
                "asof_date": pd.Timestamp("2026-05-29T00:00:00Z"),
                "setup_id": "MARKET_CONTEXT_TOP50",
                "setup_name": "Top 50% market context",
                "symbol": "ABC",
                "company_master_id": "nse:ABC",
                "watch_reasons_json": '{"source":"market_context_top50"}',
                "monitor_source": "market_context",
            }
        ]
    )
    news_items = pd.DataFrame(
        [
            {
                "source_name": "Economic Times RSS",
                "feed_name": "stocks",
                "guid": "guid-1",
                "title": "ABC shares trade flat in muted session",
                "link": "https://example.test/abc-flat",
                "description": "Routine market movement without a corporate update.",
                "categories_json": "[]",
                "published_on": pd.Timestamp("2026-05-29T11:12:46Z"),
            },
            {
                "source_name": "Economic Times RSS",
                "feed_name": "stocks",
                "guid": "guid-2",
                "title": "ABC wins large order from government client",
                "link": "https://example.test/abc-order",
                "description": "The company disclosed a large order win.",
                "categories_json": "[]",
                "published_on": pd.Timestamp("2026-05-29T11:20:46Z"),
            },
        ]
    )
    monkeypatch.setattr(
        news_watch,
        "load_watch_company_meta",
        lambda company_master_ids: pd.DataFrame(
            [{"company_master_id": "nse:ABC", "nse_ticker": "ABC", "bse_ticker": None, "company_name": "ABC Limited"}]
        ),
    )

    df = news_watch.build_news_events(watchlist=watchlist, news_items=news_items).sort_values("unique_id")

    assert df["event_status"].tolist() == ["context_observed", "triggered"]
    assert df["monitor_source"].tolist() == ["market_context", "market_context"]


def test_announcement_watch_merges_market_context_as_lower_priority():
    primary = pd.DataFrame(
        [
            {
                "asof_date": pd.Timestamp("2026-05-29T00:00:00Z"),
                "setup_id": "WATCH_A",
                "symbol": "ABC",
                "company_master_id": "nse:ABC",
                "rank": 20,
                "monitor_source": "watchlist",
            }
        ]
    )
    secondary = pd.DataFrame(
        [
            {
                "asof_date": pd.Timestamp("2026-05-29T00:00:00Z"),
                "setup_id": "MARKET_CONTEXT_TOP50",
                "symbol": "ABC",
                "company_master_id": "nse:ABC",
                "rank": 1,
                "monitor_source": "market_context",
            },
            {
                "asof_date": pd.Timestamp("2026-05-29T00:00:00Z"),
                "setup_id": "MARKET_CONTEXT_TOP50",
                "symbol": "XYZ",
                "company_master_id": "nse:XYZ",
                "rank": 2,
                "monitor_source": "market_context",
            },
        ]
    )

    out = announcement_watch.merge_watch_targets(primary, secondary)

    assert out[["symbol", "setup_id", "monitor_source"]].to_dict(orient="records") == [
        {"symbol": "ABC", "setup_id": "WATCH_A", "monitor_source": "watchlist"},
        {"symbol": "XYZ", "setup_id": "MARKET_CONTEXT_TOP50", "monitor_source": "market_context"},
    ]


def test_announcement_pipeline_retries_nse_timeout(monkeypatch, capsys):
    calls = {"count": 0, "resets": [], "sleeps": []}

    class DummyResponse:
        status_code = 200

        class Cookies:
            def get_dict(self):
                return {}

        cookies = Cookies()

        def raise_for_status(self):
            return None

        def json(self):
            return []

    def fake_get(*args, **kwargs):
        calls["count"] += 1
        if calls["count"] == 1:
            raise announcement_pipeline.requests.ReadTimeout("nse timeout")
        return DummyResponse()

    pipeline_obj = object.__new__(announcement_pipeline.AnnouncementPipeline)
    pipeline_obj.request_timeout = 60
    pipeline_obj._nse_headers = {"accept": "*/*"}
    pipeline_obj._nse_cookies = {"stale": "cookie"}
    monkeypatch.setattr(announcement_pipeline, "NSE_HTTP_MAX_ATTEMPTS", 2)
    monkeypatch.setattr(announcement_pipeline, "NSE_HTTP_RETRY_SLEEP_SECONDS", 0.01)
    monkeypatch.setattr(announcement_pipeline, "NSE_HTTP_RETRY_MAX_SLEEP_SECONDS", 0.01)
    monkeypatch.setattr(
        pipeline_obj,
        "_reset_nse_http_state",
        lambda *, reason: calls["resets"].append(reason),
    )
    monkeypatch.setattr(announcement_pipeline.requests, "get", fake_get)
    monkeypatch.setattr(announcement_pipeline.time, "sleep", lambda seconds: calls["sleeps"].append(seconds))

    response = pipeline_obj._nse_get_with_retry("https://www.nseindia.com/api/test")
    captured = capsys.readouterr()

    assert isinstance(response, DummyResponse)
    assert calls["count"] == 2
    assert calls["resets"] == ["ReadTimeout: nse timeout"]
    assert calls["sleeps"] == [0.01]
    assert "NSE request failed; retrying" in captured.err
    assert "sleep=0.0s" in captured.err


def test_announcement_pipeline_reset_clears_nse_cookies(monkeypatch, capsys):
    pipeline_obj = object.__new__(announcement_pipeline.AnnouncementPipeline)
    pipeline_obj.request_timeout = 60
    pipeline_obj._nse_headers = {"accept": "*/*", "user-agent": "stale"}
    pipeline_obj._nse_cookies = {"stale": "cookie"}

    def fake_get(*args, **kwargs):
        raise announcement_pipeline.requests.ReadTimeout("bootstrap timeout")

    monkeypatch.setattr(announcement_pipeline, "get_dynamic_headers", lambda: {"user-agent": "fresh"})
    monkeypatch.setattr(announcement_pipeline.requests, "get", fake_get)

    pipeline_obj._reset_nse_http_state(reason="test")
    captured = capsys.readouterr()

    assert pipeline_obj._nse_headers["user-agent"] == "fresh"
    assert pipeline_obj._nse_cookies == {}
    assert "Reset NSE HTTP session state; cleared cookies reason=test" in captured.err


def test_poppler_resolver_prefers_explicit_directory(monkeypatch, tmp_path):
    poppler_dir = tmp_path / "poppler"
    poppler_dir.mkdir()
    (poppler_dir / "pdfinfo").write_text("", encoding="utf-8")

    monkeypatch.setenv("POPPLER_PATH", str(poppler_dir))
    monkeypatch.setattr(poppler_utils.shutil, "which", lambda _: None)

    assert poppler_utils.resolve_poppler_path() == str(poppler_dir)


def test_operator_api_splits_dashboard_payload(monkeypatch):
    payload = {
        "generated_at": "2026-05-14 10:00:00 IST",
        "asof_date": "2026-05-14",
        "summary": {"action_count": 2, "alert_count": 1, "ts_eval_summary_count": 3},
        "top_action_recommendations": [{"symbol": "ABC", "action": "BUY"}],
        "action_recommendations": [
            {
                "symbol": "ABC",
                "action": "BUY",
                "reason": "positive_action_blocked_by_market_context",
                "reason_detail": "blocked_by_adversarial_review",
                "recommendation_reason": {
                    "status": "incomplete_downgraded",
                    "action_code": "MANUAL_REVIEW",
                    "original_action_code": "BUY",
                    "action_source": "portfolio",
                    "primary_reason": "Breakout remains strongest.",
                    "missing_fields": ["action_reason", "buy_risk_level"],
                    "evidence": {
                        "conflict_resolution": {
                            "same_symbol_candidate_count": 2,
                            "same_symbol_conflict_count": 1,
                            "winning_action_code": "BUY",
                            "winning_action_source": "portfolio",
                            "winning_action_priority": 50,
                            "source_precedence_reason": "Selected BUY from portfolio over 1 same-symbol candidate by deterministic action priority.",
                            "losing_candidates": [
                                {
                                    "action_code": "WATCH",
                                    "action_source": "watchlist",
                                    "setup_id": "WATCH",
                                    "action_reason": "Watchlist was lower priority.",
                                }
                            ],
                        },
                        "event": {
                            "event_class": "capital_allocation",
                            "verdict": "adverse",
                            "state_transition_hint": "block_positive_action",
                            "score_impact": -0.4,
                            "review_action": "veto",
                            "veto": True,
                            "review_reason": "Adversarial review found unresolved governance risk.",
                            "action_status": "blocked_by_adversarial_review",
                        },
                        "macro_regime": {
                            "market_context_adjustment": "positive_action_blocked_by_market_context",
                            "market_context_adjustment_reason": "Positive broker action was blocked because broad market context is weak or risk-off.",
                            "regime_name": "RISK_OFF",
                            "macro_risk_state": "HIGH",
                            "breadth_trend_alignment_pct": 32.0,
                            "risk_off_score": 0.72,
                            "top_context_rank_pct": 3.0,
                            "top_context_sector": "IT",
                        },
                    },
                },
                "manual_revision_summary": "ABC final action needs operator checks.",
                "manual_revision_pointers": {"manual_checks": ["Confirm latest price."]},
                "manual_revision_status": "disabled",
                "safety_checks_json": json.dumps(
                    {
                        "operator_approval_required": True,
                        "operator_approval_status": "missing",
                        "broker_reconciliation_required": True,
                        "broker_reconciliation_status": "not_run",
                        "live_submission_allowed": False,
                        "source": "action_recommendation",
                        "issues": ["Dry-run only."],
                    }
                ),
            },
            {"symbol": "XYZ", "action": "SELL"},
        ],
        "today_recommendations": [{"symbol": "ABC"}],
        "current_recommendations": [{"symbol": "DEF"}],
        "exited_recommendations": [{"symbol": "XYZ"}],
        "watch_recommendations": [{"symbol": "WATCH"}],
        "watch_events": [{"unique_id": "event-1"}, {"unique_id": "event-2"}],
        "alerts": [{"symbol": "ABC"}],
        "operator_feed": [{"message": "feed"}],
        "sync_state": [{"source": "ohlcv"}],
        "runtime_processes": [{"name": "cron"}],
        "cron_status": [{"job": "all_advisory"}],
        "_snapshot": {
            "source": "operator_snapshot",
            "freshness": "stale",
            "reason": "fresh_snapshot_missing",
            "generated_at": "2026-06-07T00:00:00Z",
            "max_age_seconds": 900,
        },
    }

    monkeypatch.setattr(operator_api, "load_operator_payload", lambda **kwargs: payload)
    monkeypatch.setattr(
        operator_api,
        "_load_latest_company_memory_reviews",
        lambda symbols: {
            "ABC": {
                "review_date": "2026-06-07T00:00:00+00:00",
                "recommended_signal": "WATCH",
                "confidence": 0.62,
                "conviction_score": 68.0,
                "summary": "ABC memory review suggests WATCH.",
                "thesis": "Technical setup is constructive, but market context blocks action.",
                "risk_flags": ["Risk-off context"],
                "evidence_used": ["latest consolidated action is MANUAL_REVIEW"],
                "wait_for": ["wait for breadth recovery"],
                "authority_scope": "review_input_only",
                "review_status": "completed",
            }
        },
    )

    health = operator_api.build_health_payload()
    assert health["operator_controlled"] is True
    assert health["read_only"] is False
    summary_payload = operator_api.build_summary_payload()
    assert summary_payload["summary"]["action_count"] == 2
    assert summary_payload["snapshot_warning"]["status"] == "warn"
    assert summary_payload["snapshot_warning"]["operator_action"] == "run_operator_snapshot_or_wait_for_advisory"
    actions_payload = operator_api.build_actions_payload()
    assert len(actions_payload["action_recommendations"]) == 2
    assert actions_payload["pagination"]["action_recommendations"]["total_count"] == 2
    assert actions_payload["pagination"]["action_recommendations"]["returned_count"] == 2
    assert actions_payload["pagination"]["action_recommendations"]["has_more"] is False
    assert actions_payload["snapshot_warning"]["reason"] == "fresh_snapshot_missing"
    compact_action = operator_api.build_actions_payload(compact=True)["action_recommendations"][0]
    assert compact_action["manual_revision_summary"] == "ABC final action needs operator checks."
    assert compact_action["manual_revision_pointers"]["manual_checks"] == ["Confirm latest price."]
    assert compact_action["manual_revision_status"] == "disabled"
    assert compact_action["company_memory_review"]["recommended_signal"] == "WATCH"
    assert compact_action["company_memory_review"]["authority_scope"] == "review_input_only"
    assert compact_action["recommendation_reason"]["evidence"]["company_memory"]["summary"] == "ABC memory review suggests WATCH."
    assert compact_action["execution_safety_contract"]["operator_approval_status"] == "missing"
    assert compact_action["execution_safety_contract"]["broker_reconciliation_status"] == "not_run"
    assert compact_action["execution_safety_contract"]["live_submission_allowed"] is False
    assert compact_action["execution_safety_contract"]["issues"] == ["Dry-run only."]
    assert compact_action["reason"] == "Positive action blocked by market context"
    assert compact_action["reason_detail"] == "Blocked by adversarial review"
    assert compact_action["recommendation_reason"]["status"] == "Incomplete reason contract; downgraded to manual review"
    assert compact_action["recommendation_reason"]["action_code"] == "MANUAL_REVIEW"
    assert compact_action["recommendation_reason"]["missing_fields"] == ["Action reason", "Buy risk level"]
    assert compact_action["recommendation_reason"]["original_action_code"] == "BUY"
    compact_conflict = compact_action["recommendation_reason"]["evidence"]["conflict_resolution"]
    assert compact_conflict["source_precedence_reason"].startswith("Selected BUY from portfolio")
    assert compact_conflict["losing_candidates"][0]["action_source"] == "watchlist"
    compact_event = compact_action["recommendation_reason"]["evidence"]["event"]
    assert compact_event["review_action"] == "veto"
    assert compact_event["veto"] is True
    assert compact_event["review_reason"] == "Adversarial review found unresolved governance risk."
    assert compact_event["action_status"] == "Blocked by adversarial review"
    compact_macro = compact_action["recommendation_reason"]["evidence"]["macro_regime"]
    assert compact_macro["market_context_adjustment"] == "Positive action blocked by market context"
    assert compact_macro["market_context_adjustment_reason"].startswith("Positive broker action was blocked")
    assert compact_macro["risk_off_score"] == 0.72
    assert compact_macro["top_context_rank_pct"] == 3.0
    assert operator_api.build_portfolio_payload()["today_recommendations"][0]["symbol"] == "ABC"
    portfolio_payload = operator_api.build_portfolio_payload()
    assert portfolio_payload["pagination"]["portfolio"]["total_count"] == 0
    assert portfolio_payload["pagination"]["today_recommendations"]["total_count"] == 1
    assert operator_api.build_portfolio_payload()["snapshot_warning"]["status"] == "warn"
    assert operator_api.build_watchlist_payload()["watch_recommendations"][0]["symbol"] == "WATCH"
    assert operator_api.build_watchlist_payload()["snapshot_warning"]["status"] == "warn"
    events_payload = operator_api.build_events_payload(limit=1)
    assert events_payload["events"] == [{"unique_id": "event-1"}]
    assert events_payload["pagination"]["events"]["total_count"] == 2
    assert events_payload["pagination"]["events"]["returned_count"] == 1
    assert events_payload["pagination"]["events"]["has_more"] is True
    assert events_payload["pagination"]["events"]["next_offset"] == 1
    assert events_payload["snapshot_warning"]["status"] == "warn"
    data_health_payload = operator_api.build_data_health_payload()
    assert data_health_payload["summary"]["alert_count"] == 1
    assert data_health_payload["snapshot_warning"]["status"] == "warn"


def test_operator_api_compact_signal_refresh_keeps_effect_fields():
    rows = operator_api._compact_list_rows(
        [
            {
                "symbol": "ABC",
                "signal_action": "WATCH",
                "signal_status": "watch_or_review",
                "signal_source": "wait_signal",
                "effect_type": "wait_match_created",
                "effect_summary": "Created a wait-signal match.",
                "action_payload_json": "large omitted payload",
            }
        ],
        compact=True,
    )

    assert rows == [
        {
            "symbol": "ABC",
            "signal_action": "WATCH",
            "signal_status": "watch_or_review",
            "signal_source": "wait_signal",
            "effect_type": "wait_match_created",
            "effect_summary": "Created a wait-signal match.",
        }
    ]


def test_operator_api_actions_events_compact_pagination_caps_large_payloads(monkeypatch):
    large_text = "x" * 10_000
    payload = {
        "generated_at": "2026-06-07T10:00:00+05:30",
        "asof_date": "2026-06-07",
        "top_action_recommendations": [{"symbol": "TOP", "action": "BUY", "raw_json": large_text}],
        "action_recommendations": [
            {"symbol": f"SYM{i:03d}", "action": "BUY", "setup_id": "SETUP", "raw_json": large_text}
            for i in range(60)
        ],
        "alerts": [{"symbol": f"ALT{i:03d}", "alert_type": "price", "raw_json": large_text} for i in range(3)],
        "watch_events": [
            {"unique_id": f"event-{i:03d}", "symbol": f"SYM{i:03d}", "subject": "Announcement", "raw_json": large_text}
            for i in range(75)
        ],
        "operator_feed": [{"unique_id": f"feed-{i:03d}", "message": "feed", "raw_json": large_text} for i in range(4)],
    }

    monkeypatch.setattr(operator_api, "load_operator_sections_payload", lambda section_names, **kwargs: None)
    monkeypatch.setattr(operator_api, "load_operator_payload", lambda **kwargs: payload)
    monkeypatch.setattr(operator_api, "_latest_ohlcv_prices", lambda symbols: {})

    actions = operator_api.build_actions_payload(limit=10, offset=20, compact=True)
    assert [row["symbol"] for row in actions["action_recommendations"][:2]] == ["SYM020", "SYM021"]
    assert len(actions["action_recommendations"]) == 10
    assert actions["pagination"]["action_recommendations"] == {
        "total_count": 60,
        "returned_count": 10,
        "limit": 10,
        "offset": 20,
        "has_more": True,
        "next_offset": 30,
    }
    assert "raw_json" not in actions["action_recommendations"][0]
    assert actions["pagination"]["top_action_recommendations"]["total_count"] == 1

    events = operator_api.build_events_payload(limit=15, offset=45, compact=True)
    assert [row["unique_id"] for row in events["events"][:2]] == ["event-045", "event-046"]
    assert len(events["events"]) == 15
    assert events["pagination"]["events"] == {
        "total_count": 75,
        "returned_count": 15,
        "limit": 15,
        "offset": 45,
        "has_more": True,
        "next_offset": 60,
    }
    assert "raw_json" not in events["events"][0]
    assert events["pagination"]["operator_feed"]["returned_count"] == 4


def test_operator_api_portfolio_compact_pagination_caps_large_payloads(monkeypatch):
    large_text = "x" * 10_000
    payload = {
        "generated_at": "2026-06-07T10:00:00+05:30",
        "asof_date": "2026-06-07",
        "today_recommendations": [{"symbol": "TODAY", "action": "BUY", "raw_json": large_text}],
        "current_recommendations": [{"symbol": "CURRENT", "action": "HOLD", "raw_json": large_text}],
        "exited_recommendations": [{"symbol": "EXITED", "action": "SELL", "raw_json": large_text}],
        "portfolio": [{"symbol": f"PF{i:03d}", "portfolio_status": "active", "raw_json": large_text} for i in range(55)],
        "lifecycle": [{"symbol": f"LC{i:03d}", "lifecycle_action": "HOLD", "raw_json": large_text} for i in range(8)],
    }

    monkeypatch.setattr(operator_api, "load_operator_payload", lambda **kwargs: payload)

    portfolio = operator_api.build_portfolio_payload(limit=12, offset=24, compact=True)

    assert [row["symbol"] for row in portfolio["portfolio"][:2]] == ["PF024", "PF025"]
    assert len(portfolio["portfolio"]) == 12
    assert portfolio["pagination"]["portfolio"] == {
        "total_count": 55,
        "returned_count": 12,
        "limit": 12,
        "offset": 24,
        "has_more": True,
        "next_offset": 36,
    }
    assert portfolio["pagination"]["today_recommendations"]["total_count"] == 1
    assert portfolio["pagination"]["lifecycle"]["returned_count"] == 8
    assert "raw_json" not in portfolio["portfolio"][0]


def test_operator_api_logs_and_research_payloads_expose_pagination(monkeypatch, tmp_path):
    log_dir = tmp_path / "cron"
    log_dir.mkdir()
    for idx in range(5):
        path = log_dir / f"job_{idx}.log"
        path.write_text(f"[stockey.script] name=job_{idx} status=ok timestamp=2026-06-07T00:00:0{idx}Z\nline {idx}\n", encoding="utf-8")
    monkeypatch.setattr(operator_api, "CRON_LOG_DIR", log_dir)

    cron_payload = operator_api.build_cron_logs_payload(limit=2, offset=2, lines=1)

    assert len(cron_payload["logs"]) == 2
    assert cron_payload["pagination"]["logs"] == {
        "total_count": 5,
        "returned_count": 2,
        "limit": 2,
        "offset": 2,
        "has_more": True,
        "next_offset": 4,
    }
    assert len(cron_payload["logs"][0]["tail"]) == 1

    prompt_payload = operator_api.build_prompt_registry_api_payload(limit=2, offset=1)

    assert len(prompt_payload["contracts"]) == 2
    assert prompt_payload["pagination"]["contracts"]["offset"] == 1
    assert prompt_payload["pagination"]["contracts"]["total_count"] == prompt_payload["summary"]["contract_count"]

    hypotheses_df = pd.DataFrame([{"hypothesis_id": f"H{i}", "title": f"Hypothesis {i}"} for i in range(6)])
    monkeypatch.setattr(operator_api, "load_hypotheses", lambda: hypotheses_df)
    monkeypatch.setattr(operator_api, "load_matches", lambda limit=100: pd.DataFrame([{"hypothesis_id": "H2"}]))
    monkeypatch.setattr(operator_api, "load_action_plans", lambda limit=100: pd.DataFrame())
    monkeypatch.setattr(operator_api, "load_wait_signals", lambda **kwargs: pd.DataFrame())
    monkeypatch.setattr(operator_api, "load_wait_signal_matches", lambda **kwargs: pd.DataFrame())
    monkeypatch.setattr(operator_api, "latest_promotion_audits", lambda hypothesis_ids: [{"hypothesis_id": hypothesis_id, "audit_status": "pending"} for hypothesis_id in hypothesis_ids])

    hypothesis_payload = operator_api.build_hypotheses_payload(limit=2, offset=2)

    assert [row["hypothesis_id"] for row in hypothesis_payload["hypotheses"]] == ["H2", "H3"]
    assert hypothesis_payload["pagination"]["hypotheses"] == {
        "total_count": 6,
        "returned_count": 2,
        "limit": 2,
        "offset": 2,
        "has_more": True,
        "next_offset": 4,
    }
    assert [row["hypothesis_id"] for row in hypothesis_payload["promotion_audits"]] == ["H2", "H3"]


def test_operator_api_hypotheses_empty_page_skips_related_loaders(monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(operator_api, "load_hypotheses", lambda: pd.DataFrame())
    monkeypatch.setattr(operator_api, "load_matches", lambda limit=100: calls.append("matches") or pd.DataFrame())
    monkeypatch.setattr(operator_api, "load_action_plans", lambda limit=100: calls.append("action_plans") or pd.DataFrame())
    monkeypatch.setattr(operator_api, "load_wait_signals", lambda **kwargs: calls.append("wait_signals") or pd.DataFrame())
    monkeypatch.setattr(operator_api, "load_wait_signal_matches", lambda **kwargs: calls.append("wait_signal_matches") or pd.DataFrame())
    monkeypatch.setattr(operator_api, "latest_promotion_audits", lambda hypothesis_ids: calls.append("promotion_audits") or [])

    payload = operator_api.build_hypotheses_payload(limit=25)

    assert payload["hypotheses"] == []
    assert payload["matches"] == []
    assert payload["pagination"]["hypotheses"]["total_count"] == 0
    assert calls == []


def test_operator_api_latest_prices_prefers_current_price_cache(monkeypatch):
    operator_api._PAYLOAD_CACHE.clear()
    sql_calls: list[dict[str, Any]] = []
    monkeypatch.setattr(
        operator_api,
        "load_current_prices",
        lambda symbols: {
            "AAA": {
                "price": 101.0,
                "price_asof": pd.Timestamp("2026-06-08T00:00:00Z"),
                "price_source": "advisory_current_prices",
            }
        },
    )
    monkeypatch.setattr(operator_api, "_table_exists", lambda table_name: table_name == "dhan_ohlcv_daily")

    def fake_sql(query, params=None, **kwargs):
        sql_calls.append({"query": query, "params": params})
        assert params == {"symbols": ["BBB"]}
        return pd.DataFrame(
            [
                {
                    "symbol": "BBB",
                    "price": 202.0,
                    "price_asof": pd.Timestamp("2026-06-08T00:00:00Z"),
                }
            ]
        )

    monkeypatch.setattr(operator_api, "sql_to_df", fake_sql)

    prices = operator_api._latest_ohlcv_prices(["AAA", "BBB"])

    assert prices["AAA"]["price"] == 101.0
    assert prices["AAA"]["price_source"] == "advisory_current_prices"
    assert prices["BBB"]["price"] == 202.0
    assert len(sql_calls) == 1


def test_operator_api_actions_payload_uses_section_snapshot(monkeypatch):
    full_payload_calls: list[str] = []
    section_payload = {
        "generated_at": "2026-06-09T00:00:00Z",
        "asof_date": "2026-06-09",
        "_snapshot": {"source": "db_section_snapshot"},
        "top_action_recommendations": [{"symbol": "AAA", "action": "BUY"}],
        "action_recommendations": [{"symbol": "BBB", "action": "BUY"}],
        "alerts": [],
    }
    monkeypatch.setattr(operator_api, "load_operator_sections_payload", lambda section_names, **kwargs: section_payload)
    monkeypatch.setattr(operator_api, "load_operator_payload", lambda **kwargs: full_payload_calls.append("full") or {})
    monkeypatch.setattr(operator_api, "_latest_ohlcv_prices", lambda symbols: {})
    monkeypatch.setattr(operator_api, "_load_latest_company_memory_reviews", lambda symbols: {})

    payload = operator_api.build_actions_payload(limit=25, compact=True)

    assert payload["snapshot"]["source"] == "db_section_snapshot"
    assert payload["action_recommendations"][0]["symbol"] == "BBB"
    assert full_payload_calls == []


def test_operator_api_event_model_artifacts_paginates_manifest_files(monkeypatch):
    files = [
        {"path": f"model_{idx}.json", "latest_key": f"models/latest/model_{idx}.json"}
        for idx in range(6)
    ]
    monkeypatch.setattr(
        operator_api,
        "build_artifact_manifest",
        lambda **kwargs: {
            "status": "ok",
            "model_version": "event_meta_model_h10",
            "latest_prefix": "models/latest",
            "files": files,
        },
    )

    class FakeS3:
        def head_object(self, *, Bucket, Key):
            return {"ContentLength": 123, "LastModified": pd.Timestamp("2026-06-07T00:00:00Z"), "ETag": "etag"}

    monkeypatch.setitem(sys.modules, "utils.store", types.SimpleNamespace(AWS_BUCKET_NAME="bucket", _get_client=lambda: FakeS3()))

    payload = operator_api.build_event_model_artifacts_payload(limit=2, offset=3)

    assert [row["path"] for row in payload["artifact"]["files"]] == ["model_3.json", "model_4.json"]
    assert [row["key"] for row in payload["latest_s3_heads"]] == ["models/latest/model_3.json", "models/latest/model_4.json"]
    assert payload["pagination"]["artifact_files"] == {
        "total_count": 6,
        "returned_count": 2,
        "limit": 2,
        "offset": 3,
        "has_more": True,
        "next_offset": 5,
    }


def test_operator_api_builds_signal_quality_payload(monkeypatch):
    latest = pd.Timestamp("2026-06-01T00:00:00Z")

    def fake_table_exists(table_name: str) -> bool:
        return table_name in {
            signal_quality_evaluator.SUMMARY_TABLE,
            signal_quality_evaluator.EVALUATIONS_TABLE,
        }

    def fake_sql_to_df(query: str, params=None, **kwargs):
        if "MAX(evaluated_at)" in query:
            return pd.DataFrame([{"latest_evaluated_at": latest}])
        if signal_quality_evaluator.SUMMARY_TABLE in query:
            return pd.DataFrame(
                [
                    {
                        "evaluated_at": latest,
                        "horizon_days": 5,
                        "variant": "technical_only",
                        "sample_count": 10,
                        "selected_count": 4,
                        "matured_count": 4,
                        "avg_forward_return_after_cost": 0.01,
                        "lift_vs_technical_only": 0.0,
                        "recommendation": "baseline",
                    },
                    {
                        "evaluated_at": latest,
                        "horizon_days": 5,
                        "variant": "technical_plus_all",
                        "sample_count": 10,
                        "selected_count": 3,
                        "matured_count": 3,
                        "avg_forward_return_after_cost": 0.04,
                        "lift_vs_technical_only": 0.03,
                        "recommendation": "candidate_overlay_improves",
                    },
                ]
            )
        if "ROW_NUMBER()" in query:
            return pd.DataFrame(
                [
                    {
                        "evaluated_at": latest,
                        "horizon_days": 5,
                        "asof_date": pd.Timestamp("2026-05-01T00:00:00Z"),
                        "variant": "technical_plus_all",
                        "symbol": "ABC",
                        "selected": True,
                        "matured": True,
                        "forward_return_after_cost": 0.04,
                    }
                ]
            )
        if "candidate_rows" in query:
            return pd.DataFrame(
                [
                    {
                        "horizon_days": 5,
                        "candidate_rows": 10,
                        "event_policy_rows": 6,
                        "bhavcopy_rows": 4,
                        "company_memory_rows": 2,
                    }
                ]
            )
        return pd.DataFrame()

    monkeypatch.setattr(operator_api, "_table_exists", fake_table_exists)
    monkeypatch.setattr(operator_api, "sql_to_df", fake_sql_to_df)

    payload = operator_api.build_signal_quality_payload(limit=2)

    assert payload["status"] == "ok"
    assert len(payload["summary"]) == 2
    assert payload["coverage"][0]["event_policy_rows"] == 6
    assert payload["examples"][0]["symbol"] == "ABC"
    assert payload["meta"]["best_overlay_by_horizon"]["5"]["variant"] == "technical_plus_all"
    assert payload["meta"]["production_policy_changed"] is False


def test_operator_api_compact_actions_keep_raw_broker_execution_safety_contract(monkeypatch):
    payload = {
        "generated_at": "2026-05-14 10:00:00 IST",
        "asof_date": "2026-05-14",
        "action_recommendations": [
            {
                "symbol": "ABC",
                "action": "BUY",
                "raw_broker_json": json.dumps(
                    {
                        "execution_safety_contract": {
                            "operator_approval_required": True,
                            "operator_approval_status": "approved",
                            "broker_reconciliation_required": True,
                            "broker_reconciliation_status": "reconciled",
                            "live_submission_allowed": False,
                            "source": "execution_reconciliation",
                            "issues": ["Live submission remains disabled."],
                        }
                    }
                ),
            }
        ],
        "top_action_recommendations": [],
        "alerts": [],
    }

    monkeypatch.setattr(operator_api, "load_operator_payload", lambda **kwargs: payload)
    monkeypatch.setattr(operator_api, "_latest_ohlcv_prices", lambda symbols: {})

    compact_action = operator_api.build_actions_payload(compact=True)["action_recommendations"][0]

    assert compact_action["execution_safety_contract"] == {
        "operator_approval_required": True,
        "operator_approval_status": "approved",
        "broker_reconciliation_required": True,
        "broker_reconciliation_status": "reconciled",
        "live_submission_allowed": False,
        "source": "execution_reconciliation",
        "issues": ["Live submission remains disabled."],
    }


def test_operator_api_runtime_payload_flags_stale_code(monkeypatch):
    monkeypatch.delenv("STOCKEY_LIVE_TRADING_ENABLED", raising=False)
    monkeypatch.setattr(operator_api, "PROCESS_STARTED_AT", 1_000.0)
    monkeypatch.setattr(operator_api, "OPERATOR_API_STALE_CODE_GRACE_SECONDS", 2.0)
    monkeypatch.setattr(operator_api, "_latest_source_mtime", lambda: (1_010.0, "advisory/api/app.py"))
    monkeypatch.setattr(
        operator_api,
        "_git_output",
        lambda args: {
            ("rev-parse", "--short=12", "HEAD"): "deadbeef1234",
            ("rev-parse", "--abbrev-ref", "HEAD"): "main",
            ("status", "--porcelain"): " M advisory/api/app.py",
        }.get(tuple(args)),
    )
    monkeypatch.setattr(operator_api.time, "time", lambda: 1_030.0)

    payload = operator_api.build_runtime_payload()

    assert payload["status"] == "ok"
    assert payload["read_only"] is True
    assert payload["git_rev"] == "deadbeef1234"
    assert payload["git_dirty"] is True
    assert payload["latest_source_path"] == "advisory/api/app.py"
    assert payload["stale_code"] is True
    assert payload["stale_reason"] == "source_newer_than_api_process"
    assert payload["operator_action"] == "restart_operator_api"
    assert payload["uptime_seconds"] == 30
    assert payload["live_trading_enabled"] is False
    assert payload["live_trading_disabled"] is True
    assert payload["live_trading_env_var"] == "STOCKEY_LIVE_TRADING_ENABLED"
    assert "disabled by default" in payload["live_trading_operator_note"]


def test_operator_api_runtime_payload_surfaces_live_trading_env(monkeypatch):
    monkeypatch.setenv("STOCKEY_LIVE_TRADING_ENABLED", "true")
    monkeypatch.setattr(operator_api, "PROCESS_STARTED_AT", 1_000.0)
    monkeypatch.setattr(operator_api, "_latest_source_mtime", lambda: (None, None))
    monkeypatch.setattr(operator_api, "_git_output", lambda args: None)
    monkeypatch.setattr(operator_api.time, "time", lambda: 1_005.0)

    payload = operator_api.build_runtime_payload()

    assert payload["live_trading_enabled"] is True
    assert payload["live_trading_disabled"] is False
    assert "requires approval, reconciliation, and safety gates" in payload["live_trading_operator_note"]


def test_operator_health_summarizes_worst_status(monkeypatch, tmp_path):
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    (log_dir / "all_advisory.log").write_text("ok\nTraceback: sample failure\n", encoding="utf-8")

    monkeypatch.setattr(operator_health, "check_database", lambda: {"status": "ok", "message": "db ok"})
    monkeypatch.setattr(operator_health, "check_operator_api", lambda: {"status": "ok", "message": "api ok"})
    monkeypatch.setattr(operator_health, "check_trace_summaries", lambda: {"status": "ok", "message": "trace cache ok", "row_count": 1})
    monkeypatch.setattr(operator_health, "check_operator_snapshot", lambda: {"status": "ok", "message": "snapshot ok"})
    monkeypatch.setattr(operator_health, "build_event_data_quality_report", lambda limit=20: {"status": "ok", "message": "event quality ok", "summary": {}})
    monkeypatch.setattr(operator_health, "check_identity_issues", lambda limit=10: {"status": "ok", "message": "identity ok", "open_count": 0, "rows": []})
    monkeypatch.setattr(operator_health, "check_signal_quality", lambda: {"status": "ok", "message": "signal quality ok", "usable": True})
    monkeypatch.setattr(operator_health, "summarize_fallback_events", lambda hours=24, limit=25: {"status": "ok", "message": "fallback ok", "active_count": 0, "rows": []})
    monkeypatch.setattr(operator_health, "check_sync_state_failures", lambda: [{"status": "ok", "message": "sync ok"}])
    monkeypatch.setattr(operator_health, "check_operator_api_errors", lambda: {"status": "ok", "message": "api errors ok", "rows": []})
    monkeypatch.setattr(operator_health, "check_announcement_document_failures", lambda limit=25: [])
    monkeypatch.setattr(operator_health, "summarize_slow_operations", lambda limit=20: {"status": "ok", "returned_count": 0, "issues": []})
    monkeypatch.setattr(operator_health, "check_table_freshness", lambda **_kwargs: [{"status": "warn", "message": "stale", "name": "actions"}])
    monkeypatch.setattr(operator_health, "check_redis", lambda: {"status": "ok", "message": "redis ok"})
    monkeypatch.setattr(operator_health, "check_dhan_token", lambda: {"status": "ok", "message": "dhan ok"})
    monkeypatch.setattr(operator_health, "check_dhan_cache", lambda: {"status": "ok", "message": "dhan cache ok"})
    monkeypatch.setattr(operator_health, "check_optional_dependencies", lambda: [{"status": "ok", "message": "deps ok"}])
    monkeypatch.setattr(operator_health, "check_frontend_dependencies", lambda: {"status": "ok", "message": "frontend ok"})

    payload = operator_health.build_operator_health(log_dir=log_dir, detail_level="full")

    assert payload["status"] == "error"
    assert payload["detail_level"] == "full"
    assert payload["sections"]["table_freshness"][0]["status"] == "warn"
    assert payload["sections"]["cron_logs"][0]["status"] == "error"
    assert any(hint["title"] == "actions data is stale or missing" for hint in payload["fix_hints"])
    assert any("tail -100" in " ".join(hint["commands"]) for hint in payload["fix_hints"])
    assert payload["current_blockers"]["status"] == "error"
    assert payload["current_blockers"]["error_count"] >= 1
    assert any(row["category"] == "pipeline" for row in payload["current_blockers"]["rows"])
    assert payload["sections"]["trust_gate"]["status"] == "warn"
    assert payload["sections"]["trust_gate"]["trust_level"] == "review_required"


def test_operator_health_current_blockers_prioritize_active_trust_issues():
    sections = {
        "database": {"status": "error", "message": "db down"},
        "table_freshness": [{"status": "warn", "name": "actions", "message": "stale"}],
        "cron_logs": [
            {
                "status": "warn",
                "message": "Latest success marker appears after earlier error markers.",
                "latest_run_status": "ok_after_historical_errors",
                "historical_error_count": 1,
            }
        ],
    }
    fix_hints = [
        {
            "status": "warn",
            "title": "Historical cron errors recovered in all_advisory.log",
            "reason": "old traceback",
            "commands": ["tail -100 logs/cron/all_advisory.log"],
        },
        {
            "status": "warn",
            "title": "actions data is stale or missing",
            "reason": "stale",
            "commands": ["./all_advisory.sh"],
            "details": {"table": "advisory_action_recommendations"},
        },
        {
            "status": "error",
            "title": "Postgres is not reachable",
            "reason": "db down",
            "commands": ["python -m advisory.operator_health --skip-dhan"],
            "details": {"section": "database"},
        },
    ]

    blockers = operator_health.build_current_blockers(sections, fix_hints)

    assert blockers["status"] == "error"
    assert blockers["rows"][0]["title"] == "Postgres is not reachable"
    assert blockers["rows"][0]["category"] == "runtime"
    assert any(row["category"] == "data_freshness" for row in blockers["rows"])
    assert all("Historical cron errors recovered" not in row["title"] for row in blockers["rows"])


def test_operator_health_trust_gate_blocks_on_runtime_and_warns_on_signal_quality():
    blocked = operator_health.build_trust_gate(
        {
            "database": {"status": "error", "message": "db down"},
            "operator_api": {"status": "ok", "message": "api ok"},
            "dhan": {"status": "ok", "message": "dhan ok"},
            "table_freshness": [],
            "event_data_quality": {"status": "ok", "message": "event ok"},
            "identity_issues": {"status": "ok", "open_count": 0},
            "signal_quality": {"status": "ok", "usable": True},
            "fallback_telemetry": {"status": "ok", "active_count": 0},
            "degradation_feed": {"status": "ok", "active_count": 0},
        }
    )
    review = operator_health.build_trust_gate(
        {
            "database": {"status": "ok", "message": "db ok"},
            "operator_api": {"status": "ok", "message": "api ok"},
            "dhan": {"status": "ok", "message": "dhan ok"},
            "table_freshness": [],
            "event_data_quality": {"status": "ok", "message": "event ok"},
            "identity_issues": {"status": "ok", "open_count": 0},
            "signal_quality": {
                "status": "warn",
                "message": "Latest signal-quality run is not strong enough for promotion decisions.",
                "reasons": ["insufficient_overlay_coverage"],
                "max_matured_rows": 100,
                "overlay_rows": 0,
            },
            "fallback_telemetry": {"status": "ok", "active_count": 0},
            "degradation_feed": {"status": "ok", "active_count": 0},
        }
    )

    assert blocked["status"] == "error"
    assert blocked["trust_level"] == "blocked"
    assert review["status"] == "warn"
    assert review["trust_level"] == "review_required"
    assert any(row["key"] == "signal_quality" for row in review["checks"])


def test_fallback_telemetry_records_and_summarizes_events(monkeypatch):
    writes = []
    monkeypatch.setattr(fallback_telemetry, "ensure_table", lambda: None)
    monkeypatch.setattr(fallback_telemetry, "upsert_to_db", lambda df, *args, **kwargs: writes.append(df.copy()))
    row = fallback_telemetry.record_fallback_event(
        module="advisory.test",
        source="unit",
        fallback_type="llm_deterministic_fallback",
        severity="warn",
        symbol="abc",
        reason="LLM unavailable in test.",
        deterministic_fallback=True,
        error=RuntimeError("boom"),
        metadata={"model": "codex"},
    )

    assert row["symbol"] == "ABC"
    assert row["error_type"] == "RuntimeError"
    assert writes[0].iloc[0]["fallback_type"] == "llm_deterministic_fallback"
    assert bool(writes[0].iloc[0]["deterministic_fallback"]) is True

    monkeypatch.setattr(fallback_telemetry, "table_exists", lambda table_name=fallback_telemetry.TABLE_NAME: True)

    def fake_sql(query, params=None, **kwargs):
        if "COUNT(*) AS active_count" in query:
            return pd.DataFrame([{"active_count": 2, "error_count": 1, "warn_count": 1}])
        if "GROUP BY fallback_type" in query:
            return pd.DataFrame([{"fallback_type": "llm_deterministic_fallback", "count": 2}])
        if "GROUP BY module" in query:
            return pd.DataFrame([{"module": "advisory.test", "count": 2}])
        return pd.DataFrame(
            [
                {
                    "event_id": "evt-1",
                    "observed_at": pd.Timestamp("2026-06-09T10:00:00Z"),
                    "module": "advisory.test",
                    "source": "unit",
                    "fallback_type": "llm_deterministic_fallback",
                    "severity": "error",
                    "status": "active",
                    "reason": "LLM failed.",
                }
            ]
        )

    monkeypatch.setattr(fallback_telemetry, "sql_to_df", fake_sql)

    summary = fallback_telemetry.summarize_fallback_events(hours=24)

    assert summary["status"] == "error"
    assert summary["active_count"] == 2
    assert summary["counts_by_type"]["llm_deterministic_fallback"] == 2
    assert summary["rows"][0]["event_id"] == "evt-1"


def test_operator_health_surfaces_fallback_telemetry_in_trust_and_fix_hints():
    fallback_section = {
        "status": "warn",
        "message": "Recent fallback/degraded-path events found.",
        "window_hours": 24,
        "active_count": 3,
        "error_count": 0,
        "warn_count": 3,
        "counts_by_type": {"redis_fail_soft": 2, "llm_deterministic_fallback": 1},
        "counts_by_module": {"utils.redis": 2, "advisory.event_policy": 1},
        "rows": [],
    }
    sections = {
        "database": {"status": "ok", "message": "db ok"},
        "operator_api": {"status": "ok", "message": "api ok"},
        "dhan": {"status": "ok", "message": "dhan ok"},
        "table_freshness": [],
        "event_data_quality": {"status": "ok", "message": "event ok"},
        "identity_issues": {"status": "ok", "open_count": 0},
        "signal_quality": {"status": "ok", "usable": True},
        "fallback_telemetry": fallback_section,
        "degradation_feed": {"status": "ok", "active_count": 0},
    }

    hints = operator_health.build_fix_hints(sections)
    trust = operator_health.build_trust_gate(sections)

    assert any(hint["title"] == "Recent fallback/degraded-path events were recorded" for hint in hints)
    assert trust["status"] == "warn"
    assert any(row["key"] == "fallback_telemetry" for row in trust["checks"])


def test_operator_health_signal_quality_flags_sparse_overlay_coverage(monkeypatch):
    latest = pd.Timestamp.utcnow() - pd.Timedelta(days=1)
    monkeypatch.setattr(operator_health, "table_exists", lambda table_name: True)

    def fake_sql(query, params=None, **kwargs):
        if "MAX(evaluated_at)" in query:
            return pd.DataFrame([{"latest_evaluated_at": latest}])
        if "advisory_signal_quality_eval_summary" in query:
            return pd.DataFrame(
                [
                    {
                        "evaluated_at": latest,
                        "horizon_days": 5,
                        "variant": "technical_only",
                        "matured_count": 80,
                        "selected_count": 10,
                    }
                ]
            )
        if "advisory_signal_quality_evaluations" in query:
            return pd.DataFrame(
                [
                    {
                        "horizon_days": 5,
                        "candidate_rows": 100,
                        "event_policy_rows": 2,
                        "bhavcopy_rows": 0,
                        "company_memory_rows": 0,
                    }
                ]
            )
        return pd.DataFrame()

    monkeypatch.setattr(operator_health, "sql_to_df", fake_sql)

    payload = operator_health.check_signal_quality()

    assert payload["status"] == "warn"
    assert payload["usable"] is False
    assert "insufficient_overlay_coverage" in payload["reasons"]


def test_operator_smoke_builds_compact_trust_contract(monkeypatch):
    monkeypatch.setattr(
        operator_smoke,
        "build_operator_health",
        lambda log_dir="logs/cron", include_dhan=False: {
            "generated_at": "2026-06-08T00:00:00Z",
            "status": "warn",
            "sections": {
                "database": {"status": "ok"},
                "operator_api": {"status": "ok"},
                "frontend": {"status": "ok"},
                "operator_snapshot": {"status": "warn"},
                "signal_quality": {"status": "warn"},
                "identity_issues": {"status": "ok"},
                "cron_logs": [{"status": "ok"}],
                "trust_gate": {
                    "status": "warn",
                    "trust_level": "review_required",
                    "recommendation": "Use as review-only.",
                    "count": 2,
                    "error_count": 0,
                    "warn_count": 2,
                },
            },
            "current_blockers": {"count": 2, "rows": [{"title": "Snapshot stale"}]},
            "fix_hints": [
                {"status": "warn", "title": "Snapshot stale", "commands": ["python -m advisory.operator_snapshot"]},
                {"status": "warn", "title": "Signal quality sparse", "commands": ["python -m advisory.signal_quality_evaluator --dry-run"]},
            ],
        },
    )

    payload = operator_smoke.build_operator_smoke(fix_hint_limit=1)

    assert payload["status"] == "warn"
    assert payload["trust_level"] == "review_required"
    assert payload["read_only"] is True
    assert payload["broker_execution_enabled"] is False
    assert payload["counts"]["current_blockers"] == 2
    assert payload["next_commands"] == ["python -m advisory.operator_snapshot"]


def test_operator_health_flags_snapshot_and_sync_failures(monkeypatch):
    monkeypatch.setattr(operator_health, "table_exists", lambda table_name: True)

    def fake_sql(query, params=None, **kwargs):
        if "advisory_operator_snapshots" in query:
            return pd.DataFrame(
                [
                    {
                        "snapshot_key": "latest",
                        "generated_at": pd.Timestamp.utcnow() - pd.Timedelta(days=3),
                        "payload_bytes": 123,
                        "section_counts_json": "{}",
                    }
                ]
            )
        if "advisory_sync_state" in query:
            return pd.DataFrame(
                [
                    {
                        "source_name": "continuous_watch:ohlcv",
                        "scope_key": "default",
                        "status": "error",
                        "error_text": "ValueError: sample",
                        "updated_at": pd.Timestamp.utcnow(),
                        "last_success_at": pd.Timestamp.utcnow() - pd.Timedelta(hours=1),
                        "state_json": "{}",
                    }
                ]
            )
        return pd.DataFrame()

    monkeypatch.setattr(operator_health, "sql_to_df", fake_sql)
    monkeypatch.setattr(operator_health, "OPERATOR_SNAPSHOT_MAX_AGE_SECONDS", 60)

    snapshot = operator_health.check_operator_snapshot()
    sync_rows = operator_health.check_sync_state_failures()

    assert snapshot["status"] == "warn"
    assert "stale" in snapshot["message"].lower()
    assert sync_rows[0]["status"] == "error"
    assert sync_rows[0]["source_name"] == "continuous_watch:ohlcv"


def test_operator_health_degradation_feed_extracts_dhan_master_miss(monkeypatch, tmp_path):
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    (log_dir / "all_advisory.log").write_text(
        "[stockey.script] name=all_advisory status=failed\nValueError: No Dhan security id mapped for NSE:HUIL\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(operator_health, "check_announcement_document_failures", lambda limit=25: [])

    cron_rows = operator_health.check_cron_logs(log_dir)
    feed = operator_health.build_degradation_feed({"cron_logs": cron_rows, "sync_state_failures": [], "slow_operations": {"issues": []}}, log_dir=log_dir)

    assert feed["status"] == "error"
    assert feed["active_count"] == 1
    row = feed["rows"][0]
    assert row["kind"] == "dhan_master_miss"
    assert row["symbol"] == "HUIL"
    assert "Dhan master" in row["title"]


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


def test_operator_health_degradation_feed_includes_announcement_failures(monkeypatch):
    monkeypatch.setattr(
        operator_health,
        "check_announcement_document_failures",
        lambda limit=25: [
            {
                "status": "warn",
                "severity": "warn",
                "kind": "announcement_document_failure",
                "title": "Announcement document OCR/parse issue",
                "message": "poppler missing",
                "source": "announcement_pipeline_documents",
                "symbol": "ABC",
                "unique_id": "ABC-1",
                "observed_at": "2026-05-30T10:00:00+00:00",
                "suggested_fix": "Install Poppler.",
                "recovered": False,
            }
        ],
    )

    feed = operator_health.build_degradation_feed({"cron_logs": [], "sync_state_failures": [], "slow_operations": {"issues": []}})

    assert feed["status"] == "warn"
    assert feed["rows"][0]["kind"] == "announcement_document_failure"
    assert feed["rows"][0]["symbol"] == "ABC"


def test_operator_health_degradation_feed_groups_active_recovered_superseded(monkeypatch):
    monkeypatch.setattr(operator_health, "check_announcement_document_failures", lambda limit=25: [])
    monkeypatch.setattr(
        operator_health,
        "cleanup_superseded_failures",
        lambda apply=False, limit=100: {
            "status": "dry_run",
            "event_processing": {"candidates": 2, "sample": [{"unique_id": "EVT-1"}]},
            "announcement_documents": {"candidates": 1, "sample": [{"unique_id": "DOC-1"}]},
        },
    )

    feed = operator_health.build_degradation_feed(
        {
            "cron_logs": [
                {
                    "log_file": "all_advisory.log",
                    "modified_at": "2026-05-30T10:00:00Z",
                    "recent_errors": ["ValueError: No Dhan security id mapped for NSE:HUIL"],
                    "historical_errors": ["Deterministic fallback after error"],
                }
            ],
            "sync_state_failures": [],
            "slow_operations": {"issues": []},
        }
    )

    lifecycle = feed["lifecycle"]
    assert lifecycle["counts"] == {"active": 1, "recovered": 1, "superseded": 3}
    assert [group["key"] for group in lifecycle["groups"]] == ["active", "recovered", "superseded"]
    assert lifecycle["superseded_preview"]["event_processing_candidates"] == 2
    assert lifecycle["superseded_preview"]["announcement_document_candidates"] == 1
    assert lifecycle["superseded_preview"]["event_processing_sample"][0]["unique_id"] == "EVT-1"
    assert lifecycle["superseded_preview"]["announcement_document_sample"][0]["unique_id"] == "DOC-1"
    assert lifecycle["superseded_preview"]["dry_run_command"] == "python -m advisory.superseded_failures --limit 500"
    assert lifecycle["superseded_preview"]["apply_command"] == "python -m advisory.superseded_failures --apply --limit 500"
    assert lifecycle["superseded_preview"]["apply_requires_operator_intent"] is True
    assert "explicit operator intent" in lifecycle["groups"][2]["next_action"]


def test_operator_health_suppresses_recovered_announcement_last_error(monkeypatch):
    monkeypatch.setattr(operator_health, "table_exists", lambda _table_name: True)
    monkeypatch.setattr(
        operator_health,
        "table_columns",
        lambda _table_name: {"unique_id", "ticker", "ocr_status", "parse_status", "last_error", "updated_at"},
    )
    monkeypatch.setattr(
        operator_health,
        "sql_to_df",
        lambda *args, **kwargs: pd.DataFrame(
            [
                {
                    "unique_id": "ABC-1",
                    "ticker": "ABC",
                    "ocr_status": "completed",
                    "parse_status": "completed",
                    "last_error": "old poppler missing",
                    "updated_at": pd.Timestamp("2026-05-30T10:00:00Z"),
                }
            ]
        ),
    )

    assert operator_health.check_announcement_document_failures() == []


def test_operator_health_downgrades_recovered_cron_log_error(tmp_path):
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    (log_dir / "all_watchers.log").write_text(
        "\n".join(
            [
                "Traceback (most recent call last):",
                "ValueError: old failure",
                '{',
                '  "status": "ok",',
                '  "dry_run": false',
                '}',
            ]
        ),
        encoding="utf-8",
    )

    rows = operator_health.check_cron_logs(log_dir)
    row = rows[0]

    assert row["status"] == "warn"
    assert row["latest_run_status"] == "ok_after_historical_errors"
    assert row["recent_error_count"] == 0
    assert row["historical_error_count"] == 1


def test_operator_health_recovers_manual_interrupt_when_outputs_are_newer(monkeypatch, tmp_path):
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    log_path = log_dir / "all_advisory.log"
    log_path.write_text("Traceback (most recent call last):\nKeyboardInterrupt\n", encoding="utf-8")
    old_ts = pd.Timestamp("2026-05-01T10:00:00Z").timestamp()
    os.utime(log_path, (old_ts, old_ts))

    monkeypatch.setattr(operator_health, "table_exists", lambda table_name: True)
    monkeypatch.setattr(operator_health, "table_columns", lambda table_name: {"load_ts", "asof_date"})
    monkeypatch.setattr(
        operator_health,
        "sql_to_df",
        lambda *args, **kwargs: pd.DataFrame([{"latest_at": pd.Timestamp("2026-05-01T11:00:00Z"), "row_count": 1}]),
    )

    row = operator_health.check_cron_logs(log_dir)[0]

    assert row["status"] == "warn"
    assert row["latest_run_status"] == "recovered_after_manual_interrupt"
    assert row["recent_error_count"] == 0
    assert row["recovery_outputs"][0]["is_newer_than_log"] is True


def test_operator_health_prefers_script_markers_over_older_errors():
    analysis = operator_health.analyze_cron_log_lines(
        [
            "[stockey.script] name=all_ml status=start timestamp=2026-05-01T10:00:00Z",
            "Traceback (most recent call last):",
            "[stockey.script] name=all_ml status=done exit_code=0 timestamp=2026-05-01T10:05:00Z",
        ]
    )

    assert analysis["status"] == "warn"
    assert analysis["latest_run_status"] == "ok_after_historical_errors"
    assert analysis["recent_error_count"] == 0
    assert analysis["latest_script_marker"]["status"] == "done"


def test_operator_health_marks_failed_script_marker_as_error():
    analysis = operator_health.analyze_cron_log_lines(
        [
            "[stockey.script] name=all_ml status=start timestamp=2026-05-01T10:00:00Z",
            "[stockey.script] name=all_ml status=failed exit_code=1 timestamp=2026-05-01T10:05:00Z",
        ]
    )

    assert analysis["status"] == "error"
    assert analysis["latest_run_status"] == "failed"
    assert analysis["latest_script_marker"]["exit_code"] == "1"


def test_operator_health_api_check_reports_latency(monkeypatch):
    class DummyResponse:
        ok = True
        status_code = 200
        text = '{"status":"ok"}'
        headers = {"content-type": "application/json"}

        def json(self):
            return {"status": "ok"}

    monkeypatch.setattr(operator_health.requests, "get", lambda *args, **kwargs: DummyResponse())

    payload = operator_health.check_operator_api()

    assert payload["status"] == "ok"
    assert payload["status_code"] == 200
    assert payload["latency_ms"] >= 0


def test_operator_health_dhan_cache_warns_when_expiring(monkeypatch, tmp_path):
    cache_path = tmp_path / "dhan.json"
    cache_path.write_text("{}", encoding="utf-8")
    expires_at = pd.Timestamp.utcnow() + pd.Timedelta(minutes=30)

    monkeypatch.delenv("DHAN_ACCESS_TOKEN", raising=False)

    import data.dhanlive.auth as dhan_auth_module
    import data.dhanlive.auth_cli as dhan_auth_cli_module

    monkeypatch.setattr(dhan_auth_module, "DEFAULT_TOKEN_CACHE", cache_path)
    monkeypatch.setattr(
        dhan_auth_module,
        "load_cached_access_token_payload",
        lambda: {"accessToken": "abcdef1234567890", "expiryTime": expires_at.isoformat()},
    )
    monkeypatch.setattr(dhan_auth_cli_module, "DEFAULT_TOKEN_CACHE", cache_path)

    payload = operator_health.check_dhan_cache()

    assert payload["status"] == "warn"
    assert payload["seconds_to_expiry"] > 0
    assert payload["has_cached_access_token"] is True


def test_operator_api_health_details_payload(monkeypatch):
    operator_api._PAYLOAD_CACHE.clear()
    monkeypatch.setattr(operator_api, "build_operator_health", lambda **_kwargs: {"status": "ok", "detail_level": _kwargs.get("detail_level"), "sections": {"database": {"status": "ok"}}})

    payload = operator_api.build_operator_health_payload()

    assert payload["status"] == "ok"
    assert payload["detail_level"] == "fast"
    assert payload["sections"]["database"]["status"] == "ok"


def test_operator_api_health_details_payload_uses_short_cache(monkeypatch):
    operator_api._PAYLOAD_CACHE.clear()
    calls = {"count": 0}

    def fake_health(**kwargs):
        calls["count"] += 1
        return {"status": "ok", "detail_level": kwargs.get("detail_level"), "sections": {"database": {"status": "ok"}, "count": calls["count"]}}

    monkeypatch.setattr(operator_api, "build_operator_health", fake_health)

    first = operator_api.build_operator_health_payload(mode="fast")
    second = operator_api.build_operator_health_payload(mode="fast")

    assert first["sections"]["count"] == 1
    assert second["sections"]["count"] == 1
    assert calls["count"] == 1


def test_operator_api_critical_payloads_include_schema_metadata(monkeypatch):
    operator_api._PAYLOAD_CACHE.clear()
    monkeypatch.setattr(operator_api, "build_operator_health", lambda **_kwargs: {"status": "ok", "sections": {"database": {"status": "ok"}}})
    monkeypatch.setattr(
        operator_api,
        "load_operator_payload",
        lambda **_kwargs: {
            "generated_at": "2026-06-07T00:00:00Z",
            "asof_date": "2026-06-07",
            "summary": {},
            "top_action_recommendations": [],
            "action_recommendations": [],
            "alerts": [],
        },
    )
    monkeypatch.setattr(operator_api, "_table_exists", lambda _table_name: False)
    monkeypatch.setattr(operator_api, "load_wait_signals", lambda **_kwargs: pd.DataFrame())
    monkeypatch.setattr(operator_api, "load_wait_signal_matches", lambda **_kwargs: pd.DataFrame())
    monkeypatch.setattr(operator_api, "load_latest_manual_review_decisions", lambda **_kwargs: {})
    monkeypatch.setattr(operator_api, "load_promotion_reviews", lambda **_kwargs: [])
    monkeypatch.setattr(operator_api, "load_open_identity_issues", lambda **_kwargs: pd.DataFrame())

    payloads = [
        operator_api.build_operator_health_payload(),
        operator_api.build_data_health_payload(),
        operator_api.build_actions_payload(),
        operator_api.build_manual_review_payload(limit=1),
        operator_api.build_identity_issues_payload(limit=1),
        operator_api.build_wait_signals_payload(limit=1),
        operator_api.build_action_conflict_rules_payload(),
    ]

    for payload in payloads:
        schema = payload["api_schema"]
        assert schema["version"] == operator_api.OPERATOR_API_SCHEMA_VERSION
        assert schema["endpoint"].startswith("/api/")
        assert schema["read_only"] is True
        assert schema["broker_execution_enabled"] is False


def test_operator_api_critical_routes_publish_typed_response_models():
    app = operator_api.create_app()
    openapi = app.openapi()

    expected = {
        ("/api/actions", "get"): "OperatorActionsResponse",
        ("/api/health/details", "get"): "OperatorHealthDetailsResponse",
        ("/api/manual-review", "get"): "ManualReviewResponse",
        ("/api/identity-issues", "get"): "IdentityIssuesResponse",
        ("/api/identity-issues/resolve-preview", "post"): "IdentityIssueResolutionResponse",
        ("/api/identity-issues/resolve-apply", "post"): "IdentityIssueResolutionResponse",
        ("/api/manual-review/decision", "post"): "ManualReviewDecisionResponse",
        ("/api/wait-signals", "get"): "WaitSignalsResponse",
        ("/api/wait-signals/match", "post"): "WaitSignalMatchResponse",
        ("/api/action-conflict-rules", "get"): "ActionConflictRulesResponse",
        ("/api/action-conflict-rules/promote", "post"): "ActionConflictRuleWriteResponse",
        ("/api/action-conflict-rules/{rule_id}", "post"): "ActionConflictRuleWriteResponse",
    }
    trace_expected = {
        ("/api/events/{unique_id}/detail", "get"): "EventDetailResponse",
        ("/api/events/{unique_id}/trace", "get"): "EventTraceResponse",
        ("/api/events/{unique_id}/trace/summary", "get"): "TraceSummaryResponse",
        ("/api/symbols/{symbol}/trace", "get"): "SymbolTraceResponse",
        ("/api/symbols/{symbol}/trace/summary", "get"): "TraceSummaryResponse",
    }
    operations_expected = {
        ("/api/operations/smoke", "get"): "OperationsSmokeResponse",
        ("/api/operations/cron-logs", "get"): "OperationsCronLogsResponse",
        ("/api/operations/cron-status", "get"): "OperationsCronStatusResponse",
        ("/api/operations/commands", "get"): "OperationsCommandsResponse",
        ("/api/operations/api-errors", "get"): "OperationsApiErrorsResponse",
    }
    research_expected = {
        ("/api/research/event-model-promotion-check", "get"): "EventModelPromotionCheckResponse",
        ("/api/research/event-model-artifacts", "get"): "EventModelArtifactsResponse",
        ("/api/research/prompt-registry", "get"): "PromptRegistryResponse",
    }
    calibration_expected = {
        ("/api/technical-calibration", "get"): "TechnicalCalibrationResponse",
        ("/api/technical-calibration/promotion-reviews", "get"): "TechnicalPromotionReviewsResponse",
        ("/api/signal-quality/promotion-reviews", "get"): "SignalQualityPromotionReviewsResponse",
        ("/api/config-change/previews", "get"): "ConfigChangePreviewsResponse",
    }
    event_policy_expected = {
        ("/api/event-policy", "get"): "EventPolicyResponse",
        ("/api/event-policy/evaluation", "get"): "EventPolicyEvaluationResponse",
    }
    hypothesis_expected = {
        ("/api/hypotheses", "get"): "HypothesesResponse",
    }
    operator_read_expected = {
        ("/api/health", "get"): "OperatorHealthResponse",
        ("/api/runtime", "get"): "OperatorRuntimeResponse",
        ("/api/summary", "get"): "OperatorSummaryResponse",
        ("/api/home", "get"): "OperatorHomeResponse",
        ("/api/portfolio", "get"): "OperatorPortfolioResponse",
        ("/api/portfolio/{symbol}/detail", "get"): "OperatorDetailResponse",
        ("/api/watchlist", "get"): "OperatorWatchlistResponse",
        ("/api/market-context", "get"): "OperatorMarketContextResponse",
        ("/api/events", "get"): "OperatorEventsResponse",
        ("/api/signal-refresh", "get"): "SignalRefreshResponse",
        ("/api/data-health", "get"): "DataHealthResponse",
        ("/api/actions/detail", "get"): "OperatorDetailResponse",
    }

    schemas = openapi["components"]["schemas"]
    assert "OperatorApiSchemaModel" in schemas
    assert schemas["OperatorApiSchemaModel"]["properties"]["broker_execution_enabled"]["type"] == "boolean"
    for (path, method), schema_name in expected.items():
        schema = openapi["paths"][path][method]["responses"]["200"]["content"]["application/json"]["schema"]
        assert schema == {"$ref": f"#/components/schemas/{schema_name}"}
    for (path, method), schema_name in trace_expected.items():
        schema = openapi["paths"][path][method]["responses"]["200"]["content"]["application/json"]["schema"]
        assert schema == {"$ref": f"#/components/schemas/{schema_name}"}
    for (path, method), schema_name in operations_expected.items():
        schema = openapi["paths"][path][method]["responses"]["200"]["content"]["application/json"]["schema"]
        assert schema == {"$ref": f"#/components/schemas/{schema_name}"}
    for (path, method), schema_name in research_expected.items():
        schema = openapi["paths"][path][method]["responses"]["200"]["content"]["application/json"]["schema"]
        assert schema == {"$ref": f"#/components/schemas/{schema_name}"}
    for (path, method), schema_name in calibration_expected.items():
        schema = openapi["paths"][path][method]["responses"]["200"]["content"]["application/json"]["schema"]
        assert schema == {"$ref": f"#/components/schemas/{schema_name}"}
    for (path, method), schema_name in event_policy_expected.items():
        schema = openapi["paths"][path][method]["responses"]["200"]["content"]["application/json"]["schema"]
        assert schema == {"$ref": f"#/components/schemas/{schema_name}"}
    for (path, method), schema_name in hypothesis_expected.items():
        schema = openapi["paths"][path][method]["responses"]["200"]["content"]["application/json"]["schema"]
        assert schema == {"$ref": f"#/components/schemas/{schema_name}"}
    for (path, method), schema_name in operator_read_expected.items():
        schema = openapi["paths"][path][method]["responses"]["200"]["content"]["application/json"]["schema"]
        assert schema == {"$ref": f"#/components/schemas/{schema_name}"}


def test_operator_api_health_read_route_smoke_with_typed_payload(monkeypatch):
    from fastapi.testclient import TestClient

    def schema(endpoint: str, name: str) -> dict[str, object]:
        return {
            "name": name,
            "version": operator_api.OPERATOR_API_SCHEMA_VERSION,
            "endpoint": endpoint,
            "read_only": True,
            "broker_execution_enabled": False,
        }

    monkeypatch.setattr(
        operator_api,
        "build_health_payload",
        lambda: {
            "generated_at": "2026-06-07T00:00:00Z",
            "api_schema": schema("/api/health", "operator_health"),
            "status": "ok",
            "service": "stockey-operator-api",
            "operator_controlled": True,
            "read_only": False,
            "write_scope": "operator_audit_and_research_controls",
        },
    )

    response = TestClient(operator_api.create_app()).get("/api/health")

    assert response.status_code == 200
    body = response.json()
    assert body["api_schema"]["version"] == operator_api.OPERATOR_API_SCHEMA_VERSION
    assert body["api_schema"]["read_only"] is True
    assert body["api_schema"]["broker_execution_enabled"] is False
    assert body["status"] == "ok"
    assert body["read_only"] is False


def test_operator_api_home_portfolio_events_read_routes_smoke_with_typed_payloads(monkeypatch):
    from fastapi.testclient import TestClient

    def schema(endpoint: str, name: str) -> dict[str, object]:
        return {
            "name": name,
            "version": operator_api.OPERATOR_API_SCHEMA_VERSION,
            "endpoint": endpoint,
            "read_only": True,
            "broker_execution_enabled": False,
        }

    monkeypatch.setattr(
        operator_api,
        "build_home_payload",
        lambda **_kwargs: {
            "generated_at": "2026-06-07T00:00:00Z",
            "api_schema": schema("/api/home", "operator_home"),
            "asof_date": "2026-06-07",
            "snapshot": {"source": "test"},
            "snapshot_warning": None,
            "summary": {"action_count": 1},
            "runtime_processes": [],
            "cron_status": [],
            "sync_state": [],
            "top_action_recommendations": [{"symbol": "ABC", "action_code": "WATCH"}],
            "today_recommendations": [],
        },
    )
    monkeypatch.setattr(
        operator_api,
        "build_portfolio_payload",
        lambda **_kwargs: {
            "generated_at": "2026-06-07T00:00:00Z",
            "api_schema": schema("/api/portfolio", "operator_portfolio"),
            "asof_date": "2026-06-07",
            "snapshot": {"source": "test"},
            "snapshot_warning": None,
            "today_recommendations": [],
            "current_recommendations": [{"symbol": "ABC", "action_code": "HOLD"}],
            "exited_recommendations": [],
            "portfolio": [{"symbol": "ABC", "portfolio_status": "active"}],
            "lifecycle": [],
            "meta": {"filters": {"compact": True}},
        },
    )
    monkeypatch.setattr(
        operator_api,
        "build_events_payload",
        lambda **_kwargs: {
            "generated_at": "2026-06-07T00:00:00Z",
            "api_schema": schema("/api/events", "operator_events"),
            "asof_date": "2026-06-07",
            "snapshot": {"source": "test"},
            "snapshot_warning": None,
            "events": [{"unique_id": "event-1", "symbol": "ABC", "event_status": "open"}],
            "operator_feed": [{"unique_id": "feed-1", "symbol": "ABC"}],
            "alerts": [],
            "meta": {"filters": {"compact": True}},
        },
    )

    client = TestClient(operator_api.create_app())
    responses = [
        client.get("/api/home"),
        client.get("/api/portfolio?limit=1&compact=true"),
        client.get("/api/events?limit=1&compact=true"),
    ]

    for response in responses:
        assert response.status_code == 200
        body = response.json()
        assert body["api_schema"]["version"] == operator_api.OPERATOR_API_SCHEMA_VERSION
        assert body["api_schema"]["read_only"] is True
        assert body["api_schema"]["broker_execution_enabled"] is False

    assert responses[0].json()["top_action_recommendations"][0]["symbol"] == "ABC"
    assert responses[1].json()["portfolio"][0]["portfolio_status"] == "active"
    assert responses[2].json()["events"][0]["unique_id"] == "event-1"


def test_operator_api_summary_watchlist_market_context_read_routes_smoke_with_typed_payloads(monkeypatch):
    from fastapi.testclient import TestClient

    def schema(endpoint: str, name: str) -> dict[str, object]:
        return {
            "name": name,
            "version": operator_api.OPERATOR_API_SCHEMA_VERSION,
            "endpoint": endpoint,
            "read_only": True,
            "broker_execution_enabled": False,
        }

    monkeypatch.setattr(
        operator_api,
        "build_summary_payload",
        lambda **_kwargs: {
            "generated_at": "2026-06-07T00:00:00Z",
            "api_schema": schema("/api/summary", "operator_summary"),
            "asof_date": "2026-06-07",
            "snapshot": {"source": "test"},
            "snapshot_warning": None,
            "summary": {"action_count": 1},
            "runtime_processes": [],
            "cron_status": [],
            "sync_state": [],
        },
    )
    monkeypatch.setattr(
        operator_api,
        "build_watchlist_payload",
        lambda **_kwargs: {
            "generated_at": "2026-06-07T00:00:00Z",
            "api_schema": schema("/api/watchlist", "operator_watchlist"),
            "asof_date": "2026-06-07",
            "snapshot": {"source": "test"},
            "snapshot_warning": None,
            "watch_recommendations": [{"symbol": "ABC", "action_code": "WATCH"}],
            "watchlist": [{"symbol": "ABC", "setup_id": "SETUP"}],
            "ts_watch_recommendations": [],
            "ts_forecast_watch": [],
            "ts_forecast_eval_summary": [],
        },
    )
    monkeypatch.setattr(
        operator_api,
        "build_market_context_payload",
        lambda **_kwargs: {
            "generated_at": "2026-06-07T00:00:00Z",
            "api_schema": schema("/api/market-context", "operator_market_context"),
            "asof_date": "2026-06-07",
            "summary": {"regime_name": "RISK_OFF"},
            "top_universe": [{"symbol": "ABC", "rank_pct": 5.0}],
        },
    )

    client = TestClient(operator_api.create_app())
    responses = [
        client.get("/api/summary"),
        client.get("/api/watchlist"),
        client.get("/api/market-context?limit=1"),
    ]

    for response in responses:
        assert response.status_code == 200
        body = response.json()
        assert body["api_schema"]["version"] == operator_api.OPERATOR_API_SCHEMA_VERSION
        assert body["api_schema"]["read_only"] is True
        assert body["api_schema"]["broker_execution_enabled"] is False

    assert responses[0].json()["summary"]["action_count"] == 1
    assert responses[1].json()["watchlist"][0]["setup_id"] == "SETUP"
    assert responses[2].json()["top_universe"][0]["symbol"] == "ABC"


def test_operator_api_signal_refresh_read_route_smoke_with_typed_payload(monkeypatch):
    from fastapi.testclient import TestClient

    def schema(endpoint: str, name: str) -> dict[str, object]:
        return {
            "name": name,
            "version": operator_api.OPERATOR_API_SCHEMA_VERSION,
            "endpoint": endpoint,
            "read_only": True,
            "broker_execution_enabled": False,
        }

    monkeypatch.setattr(
        operator_api,
        "build_signal_refresh_payload",
        lambda **_kwargs: {
            "generated_at": "2026-06-07T00:00:00Z",
            "api_schema": schema("/api/signal-refresh", "signal_refresh"),
            "status": "ok",
            "signals": [
                {
                    "refresh_id": "refresh-1",
                    "symbol": "ABC",
                    "signal_action": "MANUAL_REVIEW",
                    "effect_type": "evidence_only",
                }
            ],
            "meta": {"signals": {"total": 1, "returned": 1}, "filters": {"compact": True}},
        },
    )

    response = TestClient(operator_api.create_app()).get("/api/signal-refresh?limit=1&compact=true")

    assert response.status_code == 200
    body = response.json()
    assert body["api_schema"]["version"] == operator_api.OPERATOR_API_SCHEMA_VERSION
    assert body["api_schema"]["read_only"] is True
    assert body["api_schema"]["broker_execution_enabled"] is False
    assert body["signals"][0]["refresh_id"] == "refresh-1"
    assert body["signals"][0]["effect_type"] == "evidence_only"


def test_operator_api_runtime_read_route_smoke_with_typed_payload(monkeypatch):
    from fastapi.testclient import TestClient

    def schema(endpoint: str, name: str) -> dict[str, object]:
        return {
            "name": name,
            "version": operator_api.OPERATOR_API_SCHEMA_VERSION,
            "endpoint": endpoint,
            "read_only": True,
            "broker_execution_enabled": False,
        }

    monkeypatch.setattr(
        operator_api,
        "build_runtime_payload",
        lambda: {
            "generated_at": "2026-06-07T00:00:00Z",
            "api_schema": schema("/api/runtime", "operator_runtime"),
            "status": "ok",
            "service": "stockey-operator-api",
            "process_started_at": "2026-06-07T00:00:00Z",
            "uptime_seconds": 10,
            "git_rev": "abc123",
            "git_branch": "main",
            "git_dirty": False,
            "latest_source_mtime": "2026-06-07T00:00:00Z",
            "latest_source_path": "advisory/api/app.py",
            "stale_code": False,
            "stale_reason": None,
            "operator_action": None,
            "live_trading_enabled": False,
            "live_trading_disabled": True,
            "live_trading_env_var": "STOCKEY_LIVE_TRADING_ENABLED",
            "live_trading_operator_note": "Live broker submission is disabled by default.",
            "read_only": True,
        },
    )

    response = TestClient(operator_api.create_app()).get("/api/runtime")

    assert response.status_code == 200
    body = response.json()
    assert body["api_schema"]["version"] == operator_api.OPERATOR_API_SCHEMA_VERSION
    assert body["api_schema"]["read_only"] is True
    assert body["api_schema"]["broker_execution_enabled"] is False
    assert body["service"] == "stockey-operator-api"
    assert body["live_trading_disabled"] is True


def test_operator_api_data_health_read_route_smoke_with_typed_payload(monkeypatch):
    from fastapi.testclient import TestClient

    def schema(endpoint: str, name: str) -> dict[str, object]:
        return {
            "name": name,
            "version": operator_api.OPERATOR_API_SCHEMA_VERSION,
            "endpoint": endpoint,
            "read_only": True,
            "broker_execution_enabled": False,
        }

    monkeypatch.setattr(
        operator_api,
        "build_data_health_payload",
        lambda **_kwargs: {
            "generated_at": "2026-06-07T00:00:00Z",
            "api_schema": schema("/api/data-health", "data_health"),
            "asof_date": "2026-06-07",
            "snapshot": {"source": "test", "freshness": "fresh"},
            "snapshot_warning": None,
            "summary": {"alert_count": 0, "action_count": 2, "ts_eval_summary_count": 1},
            "sync_state": [{"source": "watchers", "status": "ok"}],
            "runtime_processes": [],
            "cron_status": [{"name": "all_watchers", "status": "ok"}],
        },
    )

    response = TestClient(operator_api.create_app()).get("/api/data-health?asof_date=2026-06-07")

    assert response.status_code == 200
    body = response.json()
    assert body["api_schema"]["version"] == operator_api.OPERATOR_API_SCHEMA_VERSION
    assert body["api_schema"]["read_only"] is True
    assert body["api_schema"]["broker_execution_enabled"] is False
    assert body["summary"]["action_count"] == 2
    assert body["sync_state"][0]["source"] == "watchers"


def test_operator_api_action_portfolio_detail_read_routes_smoke_with_typed_payloads(monkeypatch):
    from fastapi.testclient import TestClient

    def schema(endpoint: str, name: str) -> dict[str, object]:
        return {
            "name": name,
            "version": operator_api.OPERATOR_API_SCHEMA_VERSION,
            "endpoint": endpoint,
            "read_only": True,
            "broker_execution_enabled": False,
        }

    monkeypatch.setattr(
        operator_api,
        "build_action_detail_payload",
        lambda **_kwargs: {
            "generated_at": "2026-06-07T00:00:00Z",
            "api_schema": schema("/api/actions/detail", "operator_action_detail"),
            "status": "ok",
            "kind": "actions",
            "filters": {"symbol": "ABC"},
            "rows": [{"symbol": "ABC", "action_code": "WATCH", "_source_section": "action_recommendations"}],
            "row_count": 1,
        },
    )
    monkeypatch.setattr(
        operator_api,
        "build_portfolio_detail_payload",
        lambda **_kwargs: {
            "generated_at": "2026-06-07T00:00:00Z",
            "api_schema": schema("/api/portfolio/{symbol}/detail", "operator_portfolio_detail"),
            "status": "ok",
            "kind": "portfolio",
            "filters": {"symbol": "ABC"},
            "rows": [{"symbol": "ABC", "portfolio_status": "active", "_source_section": "portfolio"}],
            "row_count": 1,
        },
    )

    client = TestClient(operator_api.create_app())
    responses = [
        client.get("/api/actions/detail?symbol=ABC"),
        client.get("/api/portfolio/ABC/detail"),
    ]

    for response in responses:
        assert response.status_code == 200
        body = response.json()
        assert body["api_schema"]["version"] == operator_api.OPERATOR_API_SCHEMA_VERSION
        assert body["api_schema"]["read_only"] is True
        assert body["api_schema"]["broker_execution_enabled"] is False
        assert body["row_count"] == 1

    assert responses[0].json()["rows"][0]["action_code"] == "WATCH"
    assert responses[1].json()["rows"][0]["portfolio_status"] == "active"


def test_operator_api_critical_routes_smoke_with_typed_payloads(monkeypatch):
    from fastapi.testclient import TestClient

    def schema(endpoint: str, name: str) -> dict[str, object]:
        return {
            "name": name,
            "version": operator_api.OPERATOR_API_SCHEMA_VERSION,
            "endpoint": endpoint,
            "read_only": True,
            "broker_execution_enabled": False,
        }

    monkeypatch.setattr(
        operator_api,
        "build_actions_payload",
        lambda **_kwargs: {
            "generated_at": "2026-06-07T00:00:00Z",
            "api_schema": schema("/api/actions", "operator_actions"),
            "asof_date": "2026-06-07",
            "snapshot": {"source": "test"},
            "top_action_recommendations": [],
            "action_recommendations": [],
            "alerts": [],
            "meta": {"filters": {}},
        },
    )
    monkeypatch.setattr(
        operator_api,
        "build_operator_health_payload",
        lambda: {
            "generated_at": "2026-06-07T00:00:00Z",
            "api_schema": schema("/api/health/details", "operator_health_details"),
            "status": "ok",
            "sections": {"database": {"status": "ok"}},
            "fix_hints": [],
            "current_blockers": {"total": 0},
        },
    )
    monkeypatch.setattr(
        operator_api,
        "build_manual_review_payload",
        lambda **_kwargs: {
            "generated_at": "2026-06-07T00:00:00Z",
            "api_schema": schema("/api/manual-review", "manual_review_queue"),
            "status": "ok",
            "summary": {"total": 0},
            "items": [],
        },
    )
    monkeypatch.setattr(
        operator_api,
        "record_manual_review_decision_payload",
        lambda payload: {
            "status": "ok",
            "api_schema": schema("/api/manual-review/decision", "manual_review_decision_result"),
            "decided_at": "2026-06-07T00:00:00Z",
            "item_id": payload["item_id"],
            "decision": payload["decision"],
            "closing_decision": True,
            "next_state": "closed_ignored",
            "creates_wait_signal": False,
            "wait_signal": None,
            "note": "Decision recorded only. No config, strategy, broker, or trading behavior was changed.",
        },
    )
    monkeypatch.setattr(
        operator_api,
        "build_identity_issues_payload",
        lambda **_kwargs: {
            "generated_at": "2026-06-07T00:00:00Z",
            "api_schema": schema("/api/identity-issues", "identity_issues"),
            "status": "ok",
            "summary": {"total_open": 0, "read_only": True, "broker_execution_enabled": False},
            "issues": [],
            "skipped": [],
        },
    )
    monkeypatch.setattr(
        operator_api,
        "resolve_identity_issues_payload",
        lambda payload=None, apply=False: {
            "generated_at": "2026-06-07T00:00:00Z",
            "api_schema": {
                **schema(
                    "/api/identity-issues/resolve-apply" if apply else "/api/identity-issues/resolve-preview",
                    "identity_issue_resolution",
                ),
                "read_only": not apply,
                "broker_execution_enabled": False,
            },
            "status": "ok",
            "mode": "apply" if apply else "dry_run",
            "checked_rows": 1,
            "counts": {"resolved" if apply else "would_resolve": 1},
            "results": [{"issue_key": "issue:1", "status": "resolved" if apply else "would_resolve"}],
            "requested_issue_keys": payload.get("issue_keys", []) if isinstance(payload, dict) else [],
            "operator_boundary": {
                "mutates_identity_issue_status": apply,
                "mutates_identity_mapping": False,
                "mutates_broker_execution": False,
            },
            "note": "test",
        },
    )
    monkeypatch.setattr(
        operator_api,
        "build_wait_signals_payload",
        lambda **_kwargs: {
            "generated_at": "2026-06-07T00:00:00Z",
            "api_schema": schema("/api/wait-signals", "wait_signals"),
            "status": "ok",
            "summary": {"total_signals": 0},
            "sections": {"active": [], "matched": [], "closed": [], "expired": []},
            "signals": [],
            "matches": [],
            "match_result": None,
        },
    )
    monkeypatch.setattr(
        operator_api,
        "run_wait_signal_match_payload",
        lambda payload: {
            "generated_at": "2026-06-07T00:00:00Z",
            "api_schema": schema("/api/wait-signals/match", "wait_signal_match"),
            "status": "ok",
            "match_result": {"matched_rows": 0, "symbols": payload.get("symbols")},
        },
    )
    monkeypatch.setattr(
        operator_api,
        "build_action_conflict_rules_payload",
        lambda: {
            "generated_at": "2026-06-07T00:00:00Z",
            "api_schema": schema("/api/action-conflict-rules", "action_conflict_rules"),
            "rules": [],
            "unresolved_conflicts": [],
            "row_count": 0,
            "unresolved_count": 0,
        },
    )
    monkeypatch.setattr(
        operator_api,
        "promote_action_conflict_rule_payload",
        lambda payload: {
            "status": "promoted",
            "generated_at": "2026-06-07T00:00:00Z",
            "api_schema": schema("/api/action-conflict-rules/promote", "action_conflict_rule_promotion"),
            "rule": {"rule_id": payload["rule_id"], "enabled": False},
            "condition": {"condition_type": "action_pair_exact"},
            "note": "Promoted conflict rules are disabled by default unless enabled is explicitly true.",
        },
    )
    monkeypatch.setattr(
        operator_api,
        "update_action_conflict_rule_payload",
        lambda rule_id, payload: {
            "status": "updated",
            "generated_at": "2026-06-07T00:00:00Z",
            "api_schema": schema("/api/action-conflict-rules/{rule_id}", "action_conflict_rule_update"),
            "rule": {"rule_id": rule_id, **payload},
            "condition": payload.get("condition"),
            "note": "Historical action rows are not rewritten.",
        },
    )

    client = TestClient(operator_api.create_app())

    responses = [
        client.get("/api/actions?limit=1&compact=true"),
        client.get("/api/health/details"),
        client.get("/api/manual-review?limit=1"),
        client.get("/api/identity-issues?limit=1"),
        client.post("/api/identity-issues/resolve-preview", json={"limit": 1}),
        client.post("/api/identity-issues/resolve-apply", json={"issue_keys": ["issue:1"], "limit": 1}),
        client.post("/api/manual-review/decision", json={"item_id": "item:1", "decision": "ignore", "rationale": "test"}),
        client.get("/api/wait-signals?limit=1"),
        client.post("/api/wait-signals/match", json={"symbols": ["ABC"], "limit": 1}),
        client.get("/api/action-conflict-rules"),
        client.post("/api/action-conflict-rules/promote", json={"rule_id": "TEST_RULE"}),
        client.post("/api/action-conflict-rules/TEST_RULE", json={"enabled": True}),
    ]

    for response in responses:
        assert response.status_code == 200
        body = response.json()
        assert body["api_schema"]["version"] == operator_api.OPERATOR_API_SCHEMA_VERSION
        assert body["api_schema"]["broker_execution_enabled"] is False


def test_operator_api_operations_read_routes_smoke_with_typed_payloads(monkeypatch):
    from fastapi.testclient import TestClient

    def schema(endpoint: str, name: str) -> dict[str, object]:
        return {
            "name": name,
            "version": operator_api.OPERATOR_API_SCHEMA_VERSION,
            "endpoint": endpoint,
            "read_only": True,
            "broker_execution_enabled": False,
        }

    monkeypatch.setattr(
        operator_api,
        "build_operations_smoke_payload",
        lambda: {
            "generated_at": "2026-06-07T00:00:00Z",
            "api_schema": schema("/api/operations/smoke", "operations_smoke"),
            "status": "ok",
            "operator_health": {"status": "ok"},
            "fix_hints": [],
            "read_only": True,
            "note": "read-only smoke",
        },
    )
    monkeypatch.setattr(
        operator_api,
        "build_cron_logs_payload",
        lambda **_kwargs: {
            "generated_at": "2026-06-07T00:00:00Z",
            "api_schema": schema("/api/operations/cron-logs", "operations_cron_logs"),
            "status": "ok",
            "log_dir": "logs/cron",
            "logs": [{"name": "all_watchers.log", "status": "ok", "tail": []}],
        },
    )
    monkeypatch.setattr(
        operator_api,
        "build_cron_status_payload",
        lambda **_kwargs: {
            "generated_at": "2026-06-07T00:00:00Z",
            "api_schema": schema("/api/operations/cron-status", "operations_cron_status"),
            "status": "ok",
            "crontab_path": "config/stockey.generated.crontab",
            "log_dir": "logs/cron",
            "counts": {"ok": 1},
            "jobs": [{"job_name": "all_watchers", "status": "ok"}],
            "pagination": {"jobs": {"total_count": 1}},
        },
    )
    monkeypatch.setattr(
        operator_api,
        "build_operator_commands_payload",
        lambda **_kwargs: {
            "generated_at": "2026-06-07T00:00:00Z",
            "api_schema": schema("/api/operations/commands", "operations_commands"),
            "status": "ok",
            "commands": [{"key": "operator_health_skip_dhan", "risk": "safe_read_only", "dry_run": True}],
            "recent_runs": [],
        },
    )
    monkeypatch.setattr(
        operator_api,
        "build_operator_api_errors_payload",
        lambda **_kwargs: {
            "generated_at": "2026-06-07T00:00:00Z",
            "api_schema": schema("/api/operations/api-errors", "operations_api_errors"),
            "status": "ok",
            "errors": [],
            "summary": {"total": 0, "error": 0, "warn": 0},
        },
    )

    client = TestClient(operator_api.create_app())
    responses = [
        client.get("/api/operations/smoke"),
        client.get("/api/operations/cron-logs?limit=1&lines=5"),
        client.get("/api/operations/cron-status?limit=1&lines=5"),
        client.get("/api/operations/commands?limit=1"),
        client.get("/api/operations/api-errors?limit=1"),
    ]

    for response in responses:
        assert response.status_code == 200
        body = response.json()
        assert body["api_schema"]["version"] == operator_api.OPERATOR_API_SCHEMA_VERSION
        assert body["api_schema"]["read_only"] is True
        assert body["api_schema"]["broker_execution_enabled"] is False

    commands = responses[3].json()["commands"]
    assert commands[0]["key"] == "operator_health_skip_dhan"
    assert commands[0]["dry_run"] is True


def test_operator_api_technical_calibration_read_routes_smoke_with_typed_payloads(monkeypatch):
    from fastapi.testclient import TestClient

    def schema(endpoint: str, name: str) -> dict[str, object]:
        return {
            "name": name,
            "version": operator_api.OPERATOR_API_SCHEMA_VERSION,
            "endpoint": endpoint,
            "read_only": True,
            "broker_execution_enabled": False,
        }

    monkeypatch.setattr(
        operator_api,
        "build_technical_calibration_payload",
        lambda **_kwargs: {
            "generated_at": "2026-06-07T00:00:00Z",
            "api_schema": schema("/api/technical-calibration", "technical_calibration"),
            "status": "ok",
            "summary": [{"horizon_days": 5, "best_config_id": "cfg-1"}],
            "top_configs": [{"horizon_days": 5, "config_id": "cfg-1"}],
        },
    )
    monkeypatch.setattr(
        operator_api,
        "build_technical_threshold_reviews_payload",
        lambda **_kwargs: {
            "generated_at": "2026-06-07T00:00:00Z",
            "api_schema": schema("/api/technical-calibration/promotion-reviews", "technical_promotion_reviews"),
            "status": "ok",
            "reviews": [{"setup_id": "SWING", "config_id": "cfg-1", "review_status": "pending_operator_decision"}],
        },
    )
    monkeypatch.setattr(
        operator_api,
        "build_signal_quality_promotion_reviews_payload",
        lambda **_kwargs: {
            "generated_at": "2026-06-07T00:00:00Z",
            "api_schema": schema("/api/signal-quality/promotion-reviews", "signal_quality_promotion_reviews"),
            "status": "ok",
            "reviews": [{"variant": "technical_plus_all", "review_status": "ok"}],
        },
    )
    monkeypatch.setattr(
        operator_api,
        "build_config_change_previews_payload",
        lambda **_kwargs: {
            "generated_at": "2026-06-07T00:00:00Z",
            "api_schema": schema("/api/config-change/previews", "config_change_previews"),
            "status": "ok",
            "previews": [{"preview_id": "p1", "applied": False}],
        },
    )

    client = TestClient(operator_api.create_app())
    responses = [
        client.get("/api/technical-calibration?limit=1"),
        client.get("/api/technical-calibration/promotion-reviews?limit=1"),
        client.get("/api/signal-quality/promotion-reviews?limit=1"),
        client.get("/api/config-change/previews?limit=1"),
    ]

    for response in responses:
        assert response.status_code == 200
        body = response.json()
        assert body["api_schema"]["version"] == operator_api.OPERATOR_API_SCHEMA_VERSION
        assert body["api_schema"]["read_only"] is True
        assert body["api_schema"]["broker_execution_enabled"] is False

    assert responses[0].json()["summary"][0]["best_config_id"] == "cfg-1"
    assert responses[1].json()["reviews"][0]["review_status"] == "pending_operator_decision"
    assert responses[2].json()["reviews"][0]["variant"] == "technical_plus_all"
    assert responses[3].json()["previews"][0]["applied"] is False


def test_operator_api_event_model_research_read_routes_smoke_with_typed_payloads(monkeypatch):
    from fastapi.testclient import TestClient

    def schema(endpoint: str, name: str) -> dict[str, object]:
        return {
            "name": name,
            "version": operator_api.OPERATOR_API_SCHEMA_VERSION,
            "endpoint": endpoint,
            "read_only": True,
            "broker_execution_enabled": False,
        }

    monkeypatch.setattr(
        operator_api,
        "build_event_model_promotion_check_payload",
        lambda: {
            "generated_at": "2026-06-07T00:00:00Z",
            "api_schema": schema("/api/research/event-model-promotion-check", "event_model_promotion_check"),
            "status": "ok",
            "decision": "hold_research_only",
            "ready_for_operator_review": False,
            "promotion_mode": "manual_low_weight_review",
            "artifact": {"status": "ok", "model_version": "event_meta_model_h10"},
            "metadata": {"model_name": "xgboost_event_meta_model"},
            "coverage": {"train_rows": 100, "test_rows": 25},
            "weekly_runs": {"successful_runs": 4},
            "score_freshness": {"age_days": 1},
            "gates": [{"name": "precision", "passed": True}],
            "failed_gates": [],
            "notes": ["Research-only gate; no policy promotion is applied."],
        },
    )
    monkeypatch.setattr(
        operator_api,
        "build_event_model_artifacts_payload",
        lambda **_kwargs: {
            "generated_at": "2026-06-07T00:00:00Z",
            "api_schema": schema("/api/research/event-model-artifacts", "event_model_artifacts"),
            "status": "ok",
            "artifact": {"model_version": "event_meta_model_h10", "latest_prefix": "models/event/latest"},
            "latest_s3_heads": [{"key": "models/event/latest/event_meta_model.json", "status": "ok"}],
            "read_only": True,
        },
    )
    monkeypatch.setattr(
        operator_api,
        "build_prompt_registry_api_payload",
        lambda **_kwargs: {
            "generated_at": "2026-06-07T00:00:00Z",
            "api_schema": schema("/api/research/prompt-registry", "prompt_registry"),
            "status": "ok",
            "contracts": [{"prompt_id": "advisory_event_evaluation", "broker_execution_allowed": False}],
            "summary": {"contract_count": 1, "broker_execution_allowed_count": 0},
            "notes": ["Registry is audit metadata."],
        },
    )

    client = TestClient(operator_api.create_app())
    responses = [
        client.get("/api/research/event-model-promotion-check"),
        client.get("/api/research/event-model-artifacts"),
        client.get("/api/research/prompt-registry"),
    ]

    for response in responses:
        assert response.status_code == 200
        body = response.json()
        assert body["api_schema"]["version"] == operator_api.OPERATOR_API_SCHEMA_VERSION
        assert body["api_schema"]["read_only"] is True
        assert body["api_schema"]["broker_execution_enabled"] is False

    assert responses[0].json()["decision"] == "hold_research_only"
    assert responses[1].json()["read_only"] is True
    assert responses[2].json()["summary"]["broker_execution_allowed_count"] == 0


def test_operator_api_event_policy_read_routes_smoke_with_typed_payloads(monkeypatch):
    from fastapi.testclient import TestClient

    def schema(endpoint: str, name: str) -> dict[str, object]:
        return {
            "name": name,
            "version": operator_api.OPERATOR_API_SCHEMA_VERSION,
            "endpoint": endpoint,
            "read_only": True,
            "broker_execution_enabled": False,
        }

    monkeypatch.setattr(
        operator_api,
        "build_event_policy_payload",
        lambda **_kwargs: {
            "generated_at": "2026-06-07T00:00:00Z",
            "api_schema": schema("/api/event-policy", "event_policy"),
            "status": "ok",
            "asof_date": "2026-06-07",
            "summary": {"action_counts": {"MANUAL_REVIEW": 1}, "policy_class_counts": {"ORDER_WIN": 1}, "row_count": 1},
            "rows": [{"unique_id": "event-1", "action_type": "MANUAL_REVIEW", "policy_class": "ORDER_WIN"}],
        },
    )
    monkeypatch.setattr(
        operator_api,
        "build_event_policy_evaluation_payload",
        lambda **_kwargs: {
            "generated_at": "2026-06-07T00:00:00Z",
            "api_schema": schema("/api/event-policy/evaluation", "event_policy_evaluation"),
            "status": "ok",
            "summary": [{"group_value": "ORDER_WIN", "recommendation": "candidate_policy_strengthen"}],
        },
    )

    client = TestClient(operator_api.create_app())
    responses = [
        client.get("/api/event-policy?limit=1&action_type=ALL"),
        client.get("/api/event-policy/evaluation?limit=1"),
    ]

    for response in responses:
        assert response.status_code == 200
        body = response.json()
        assert body["api_schema"]["version"] == operator_api.OPERATOR_API_SCHEMA_VERSION
        assert body["api_schema"]["read_only"] is True
        assert body["api_schema"]["broker_execution_enabled"] is False

    assert responses[0].json()["rows"][0]["policy_class"] == "ORDER_WIN"
    assert responses[1].json()["summary"][0]["recommendation"] == "candidate_policy_strengthen"


def test_operator_api_hypotheses_read_route_smoke_with_typed_payload(monkeypatch):
    from fastapi.testclient import TestClient

    def schema(endpoint: str, name: str) -> dict[str, object]:
        return {
            "name": name,
            "version": operator_api.OPERATOR_API_SCHEMA_VERSION,
            "endpoint": endpoint,
            "read_only": True,
            "broker_execution_enabled": False,
        }

    monkeypatch.setattr(
        operator_api,
        "build_hypotheses_payload",
        lambda **_kwargs: {
            "generated_at": "2026-06-07T00:00:00Z",
            "api_schema": schema("/api/hypotheses", "hypotheses"),
            "status": "ok",
            "hypotheses": [{"hypothesis_id": "H1", "title": "Austerity risk", "status": "active_review"}],
            "matches": [{"hypothesis_id": "H1", "source_table": "news", "source_key": "n1"}],
            "action_plans": [{"hypothesis_id": "H1", "action_type": "MANUAL_REVIEW", "production_allowed": False}],
            "wait_signals": [{"hypothesis_id": "H1", "signal_id": "ws1", "status": "active"}],
            "wait_signal_matches": [],
            "promotion_audits": [{"hypothesis_id": "H1", "audit_status": "insufficient_history"}],
        },
    )

    response = TestClient(operator_api.create_app()).get("/api/hypotheses?limit=1")

    assert response.status_code == 200
    body = response.json()
    assert body["api_schema"]["version"] == operator_api.OPERATOR_API_SCHEMA_VERSION
    assert body["api_schema"]["read_only"] is True
    assert body["api_schema"]["broker_execution_enabled"] is False
    assert body["hypotheses"][0]["hypothesis_id"] == "H1"
    assert body["promotion_audits"][0]["audit_status"] == "insufficient_history"


def test_operator_api_symbol_trace_routes_smoke_and_error_paths(monkeypatch):
    from fastapi.testclient import TestClient

    errors = []
    trace_calls = []
    summary_calls = []

    def fake_trace(symbol, limit=100):
        trace_calls.append({"symbol": symbol, "limit": limit})
        if symbol == "BAD":
            raise ValueError("invalid symbol for trace")
        if symbol == "ERR":
            raise RuntimeError("trace backend failed")
        return {
            "symbol": symbol.upper(),
            "processing": [{"stage": "event_evaluation", "status": "ok"}],
            "traces": [{"trace_id": "trace-1", "final_action": "WATCH"}],
            "steps": [{"trace_id": "trace-1", "stage": "technical"}],
            "action_conflicts": [],
        }

    def fake_summary(symbol, limit=100):
        summary_calls.append({"symbol": symbol, "limit": limit})
        return {
            "symbol": symbol.upper(),
            "processing": [{"stage": "event_evaluation", "status": "ok"}],
            "decisions": [{"trace_id": "trace-1", "trigger_type": "action", "steps": []}],
            "action_conflicts": [],
            "raw_counts": {"processing": 1, "traces": 1, "steps": 1, "action_conflicts": 0},
            "_trace_summary_cache": {"source": "test"},
        }

    monkeypatch.setattr(operator_api, "build_symbol_trace_payload", fake_trace)
    monkeypatch.setattr(operator_api, "build_symbol_trace_summary_payload", fake_summary)
    monkeypatch.setattr(operator_api, "record_operator_api_error", lambda **kwargs: errors.append(kwargs))

    client = TestClient(operator_api.create_app())

    trace_response = client.get("/api/symbols/abc/trace?limit=12")
    assert trace_response.status_code == 200
    trace_body = trace_response.json()
    assert trace_body["symbol"] == "ABC"
    assert trace_body["api_schema"]["endpoint"] == "/api/symbols/{symbol}/trace"
    assert trace_body["api_schema"]["read_only"] is True
    assert trace_body["api_schema"]["broker_execution_enabled"] is False
    assert trace_calls[-1] == {"symbol": "abc", "limit": 12}

    summary_response = client.get("/api/symbols/abc/trace/summary?limit=7")
    assert summary_response.status_code == 200
    summary_body = summary_response.json()
    assert summary_body["raw_counts"]["processing"] == 1
    assert summary_body["api_schema"]["endpoint"] == "/api/symbols/{symbol}/trace/summary"
    assert summary_body["api_schema"]["read_only"] is True
    assert summary_body["api_schema"]["broker_execution_enabled"] is False
    assert summary_calls[-1] == {"symbol": "abc", "limit": 7}

    validation_response = client.get("/api/symbols/abc/trace?limit=0")
    assert validation_response.status_code == 422

    bad_response = client.get("/api/symbols/BAD/trace")
    assert bad_response.status_code == 400
    assert bad_response.json()["detail"]["route"] == "/api/symbols/{symbol}/trace"
    assert bad_response.json()["detail"]["operation"] == "fake_trace"
    assert errors[-1]["status_code"] == 400
    assert errors[-1]["route"] == "/api/symbols/{symbol}/trace"

    err_response = client.get("/api/symbols/ERR/trace")
    assert err_response.status_code == 500
    assert err_response.json()["detail"]["error_type"] == "RuntimeError"
    assert errors[-1]["status_code"] == 500


def test_operator_api_event_detail_trace_routes_smoke_and_error_paths(monkeypatch):
    from fastapi.testclient import TestClient

    errors = []
    detail_calls = []
    trace_calls = []
    summary_calls = []

    def fake_detail(unique_id, asof_date=None):
        detail_calls.append({"unique_id": unique_id, "asof_date": asof_date})
        if unique_id == "bad-event":
            raise ValueError("invalid event id")
        return {
            "generated_at": "2026-06-07T00:00:00Z",
            "status": "ok",
            "kind": "events",
            "filters": {"unique_id": unique_id, "asof_date": asof_date},
            "rows": [{"unique_id": unique_id, "event_class": "ORDER_WIN"}],
            "row_count": 1,
        }

    def fake_trace(unique_id):
        trace_calls.append(unique_id)
        if unique_id == "err-event":
            raise RuntimeError("event trace backend failed")
        return {
            "unique_id": unique_id,
            "processing": [{"stage": "event_evaluation", "status": "ok"}],
            "traces": [{"trace_id": "trace-1", "final_action": "MANUAL_REVIEW"}],
            "steps": [{"trace_id": "trace-1", "stage": "event_policy"}],
            "action_conflicts": [],
        }

    def fake_summary(unique_id):
        summary_calls.append(unique_id)
        return {
            "unique_id": unique_id,
            "processing": [{"stage": "event_evaluation", "status": "ok"}],
            "decisions": [{"trace_id": "trace-1", "trigger_type": "event_policy", "steps": []}],
            "action_conflicts": [],
            "raw_counts": {"processing": 1, "traces": 1, "steps": 1, "action_conflicts": 0},
            "_trace_summary_cache": {"source": "test"},
        }

    monkeypatch.setattr(operator_api, "build_event_detail_payload", fake_detail)
    monkeypatch.setattr(operator_api, "build_event_trace_payload", fake_trace)
    monkeypatch.setattr(operator_api, "build_event_trace_summary_payload", fake_summary)
    monkeypatch.setattr(operator_api, "record_operator_api_error", lambda **kwargs: errors.append(kwargs))

    client = TestClient(operator_api.create_app())

    detail_response = client.get("/api/events/event-1/detail?asof_date=2026-06-07")
    assert detail_response.status_code == 200
    detail_body = detail_response.json()
    assert detail_body["kind"] == "events"
    assert detail_body["api_schema"]["endpoint"] == "/api/events/{unique_id}/detail"
    assert detail_body["api_schema"]["read_only"] is True
    assert detail_body["api_schema"]["broker_execution_enabled"] is False
    assert detail_calls[-1] == {"unique_id": "event-1", "asof_date": "2026-06-07"}

    trace_response = client.get("/api/events/event-1/trace")
    assert trace_response.status_code == 200
    trace_body = trace_response.json()
    assert trace_body["unique_id"] == "event-1"
    assert trace_body["api_schema"]["endpoint"] == "/api/events/{unique_id}/trace"
    assert trace_body["api_schema"]["read_only"] is True
    assert trace_body["api_schema"]["broker_execution_enabled"] is False
    assert trace_calls[-1] == "event-1"

    summary_response = client.get("/api/events/event-1/trace/summary")
    assert summary_response.status_code == 200
    summary_body = summary_response.json()
    assert summary_body["raw_counts"]["traces"] == 1
    assert summary_body["api_schema"]["endpoint"] == "/api/events/{unique_id}/trace/summary"
    assert summary_body["api_schema"]["read_only"] is True
    assert summary_body["api_schema"]["broker_execution_enabled"] is False
    assert summary_calls[-1] == "event-1"

    bad_response = client.get("/api/events/bad-event/detail")
    assert bad_response.status_code == 400
    assert bad_response.json()["detail"]["route"] == "/api/events/{unique_id}/detail"
    assert bad_response.json()["detail"]["operation"] == "fake_detail"
    assert errors[-1]["status_code"] == 400
    assert errors[-1]["route"] == "/api/events/{unique_id}/detail"

    err_response = client.get("/api/events/err-event/trace")
    assert err_response.status_code == 500
    assert err_response.json()["detail"]["route"] == "/api/events/{unique_id}/trace"
    assert err_response.json()["detail"]["error_type"] == "RuntimeError"
    assert errors[-1]["status_code"] == 500


def test_operator_api_event_trace_payload(monkeypatch):
    monkeypatch.setattr(operator_api, "load_event_trace", lambda unique_id: {"unique_id": unique_id, "steps": [{"stage": "event_evaluation"}]})

    payload = operator_api.build_event_trace_payload("event-1")

    assert payload == {
        "unique_id": "event-1",
        "steps": [{"stage": "event_evaluation"}],
        "pagination": {
            "primary": "traces",
            "processing": {"returned_count": 0, "total_count": 0, "limit": 0, "offset": 0, "has_more": False, "next_offset": None},
            "traces": {"returned_count": 0, "total_count": 0, "limit": 0, "offset": 0, "has_more": False, "next_offset": None},
            "steps": {"returned_count": 1, "total_count": 1, "limit": 1, "offset": 0, "has_more": False, "next_offset": None},
            "action_conflicts": {"returned_count": 0, "total_count": 0, "limit": 0, "offset": 0, "has_more": False, "next_offset": None},
            "bounded": False,
        },
    }


def test_operator_api_symbol_trace_payload(monkeypatch):
    monkeypatch.setattr(operator_api, "load_symbol_trace", lambda symbol, limit=200: {"symbol": symbol.upper(), "traces": [{"final_action": "SELL"}], "limit": limit})

    payload = operator_api.build_symbol_trace_payload("abc", limit=10)

    assert payload == {
        "symbol": "ABC",
        "traces": [{"final_action": "SELL"}],
        "limit": 10,
        "pagination": {
            "primary": "traces",
            "processing": {"returned_count": 0, "total_count": 0, "limit": 0, "offset": 0, "has_more": False, "next_offset": None},
            "traces": {"returned_count": 1, "total_count": 1, "limit": 10, "offset": 0, "has_more": False, "next_offset": None},
            "steps": {"returned_count": 0, "total_count": 0, "limit": 0, "offset": 0, "has_more": False, "next_offset": None},
            "action_conflicts": {"returned_count": 0, "total_count": 0, "limit": 0, "offset": 0, "has_more": False, "next_offset": None},
            "bounded": True,
        },
    }


def test_decision_trace_loads_manual_review_wait_signal_links(monkeypatch):
    queries: list[str] = []

    def fake_sql_to_df(query, *args, **kwargs):
        query_text = str(query)
        queries.append(query_text)
        if "information_schema.tables" in query_text:
            table_name = kwargs.get("params", [""])[0]
            if table_name in {
                decision_trace.MANUAL_REVIEW_DECISIONS_TABLE,
                decision_trace.WAIT_SIGNALS_TABLE,
                decision_trace.WAIT_SIGNAL_MATCHES_TABLE,
            }:
                return pd.DataFrame([{"exists_flag": 1}])
            return pd.DataFrame()
        if decision_trace.MANUAL_REVIEW_DECISIONS_TABLE in query_text and decision_trace.WAIT_SIGNALS_TABLE in query_text:
            return pd.DataFrame(
                [
                    {
                        "decided_at": pd.Timestamp("2026-06-01T09:00:00Z"),
                        "manual_review_item_id": "action_manual_review:source:key",
                        "manual_review_item_type": "action_manual_review",
                        "manual_review_symbol": "ABC",
                        "decision": "watch_for_event",
                        "rationale": "Wait for clarification.",
                        "follow_up_event": "order cancellation clarification",
                        "wait_signal_created_at": pd.Timestamp("2026-06-01T09:01:00Z"),
                        "signal_id": "sig-manual",
                        "wait_signal_status": "matched",
                        "signal_type": "clarification_filing",
                        "expected_action": "MANUAL_REVIEW",
                        "wait_question": "order cancellation clarification",
                        "condition_json": json.dumps({"condition_type": "clarification_filing"}),
                        "matched_at": pd.Timestamp("2026-06-02T09:00:00Z"),
                        "match_status": "matched",
                        "match_source_table": "advisory_watch_events",
                        "match_source_key": "ANN-1",
                        "match_reason": "Matched clarification filing.",
                        "evidence_json": json.dumps({"wait_signal": {"manual_review_item_id": "action_manual_review:source:key"}}),
                    }
                ]
            )
        return pd.DataFrame()

    monkeypatch.setattr(decision_trace, "sql_to_df", fake_sql_to_df)

    links = decision_trace.load_manual_review_wait_signal_links(symbol="abc", limit=10)

    assert len(links) == 1
    assert links[0]["manual_review_item_id"] == "action_manual_review:source:key"
    assert links[0]["signal_id"] == "sig-manual"
    assert links[0]["match_source_key"] == "ANN-1"
    assert any("LEFT JOIN advisory_wait_signal_matches" in query for query in queries)


def test_operator_api_trace_summary_normalizes_manual_review_wait_signal_links():
    payload = operator_api.normalize_trace_payload(
        {
            "symbol": "ABC",
            "processing": [],
            "traces": [],
            "steps": [],
            "manual_review_wait_signal_links": [
                {
                    "decided_at": pd.Timestamp("2026-06-01T09:00:00Z"),
                    "manual_review_item_id": "action_manual_review:source:key",
                    "manual_review_symbol": "ABC",
                    "decision": "watch_for_event",
                    "rationale": "Wait for clarification.",
                    "wait_signal_created_at": pd.Timestamp("2026-06-01T09:01:00Z"),
                    "signal_id": "sig-manual",
                    "wait_signal_status": "matched",
                    "signal_type": "clarification_filing",
                    "expected_action": "MANUAL_REVIEW",
                    "condition_json": json.dumps({"condition_type": "clarification_filing"}),
                    "matched_at": pd.Timestamp("2026-06-02T09:00:00Z"),
                    "match_status": "matched",
                    "match_source_key": "ANN-1",
                    "evidence_json": json.dumps({"subject": "Clarification filed"}),
                }
            ],
        }
    )

    assert payload["raw_counts"]["manual_review_wait_signal_links"] == 1
    link = payload["manual_review_wait_signal_links"][0]
    assert link["manual_review_item_id"] == "action_manual_review:source:key"
    assert link["condition"]["condition_type"] == "clarification_filing"
    assert link["evidence"]["subject"] == "Clarification filed"


def test_operator_api_trace_summary_uses_materialized_cache(monkeypatch):
    cached = {"symbol": "ABC", "decisions": [], "_trace_summary_cache": {"source": "materialized"}}
    monkeypatch.setattr(operator_api, "load_materialized_trace_summary", lambda entity_type, entity_key, limit=100: cached)
    monkeypatch.setattr(operator_api, "load_symbol_trace", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("live trace should not be called")))

    payload = operator_api.build_symbol_trace_summary_payload("ABC", limit=100)

    assert payload["_trace_summary_cache"]["source"] == "materialized"
    assert payload["pagination"]["decisions"]["returned_count"] == 0
    assert payload["pagination"]["decisions"]["limit"] == 100


def test_operator_api_trace_summary_falls_back_and_marks(monkeypatch):
    markers = []
    monkeypatch.setattr(operator_api, "load_materialized_trace_summary", lambda entity_type, entity_key, limit=100: None)
    monkeypatch.setattr(operator_api, "record_operator_api_marker", lambda **kwargs: markers.append(kwargs))
    monkeypatch.setattr(operator_api, "load_event_trace", lambda unique_id: {"unique_id": unique_id, "processing": [], "traces": [], "steps": []})

    payload = operator_api.build_event_trace_summary_payload("event-1")

    assert payload["_trace_summary_cache"]["source"] == "live_fallback"
    assert payload["pagination"]["decisions"]["returned_count"] == 0
    assert markers[0]["message"] == "trace_summary_cache_miss:event"


def test_operator_api_hypothesis_payloads(monkeypatch):
    monkeypatch.setattr(operator_api, "load_hypotheses", lambda: pd.DataFrame([{"hypothesis_id": "H1", "title": "Austerity"}]))
    monkeypatch.setattr(operator_api, "load_matches", lambda limit=100: pd.DataFrame([{"hypothesis_id": "H1", "suggested_action": "REDUCE_EXPOSURE_REVIEW_TESTING"}]))
    monkeypatch.setattr(operator_api, "load_action_plans", lambda limit=100: pd.DataFrame())
    monkeypatch.setattr(operator_api, "load_wait_signals", lambda **kwargs: pd.DataFrame())
    monkeypatch.setattr(operator_api, "load_wait_signal_matches", lambda **kwargs: pd.DataFrame())
    monkeypatch.setattr(operator_api, "latest_promotion_audits", lambda hypothesis_ids: [])
    monkeypatch.setattr(operator_api, "create_hypothesis", lambda payload: {"hypothesis_id": "H1", **payload})
    monkeypatch.setattr(operator_api, "run_hypothesis_scan", lambda **kwargs: {"status": "ok", "match_count": 1, **kwargs})

    assert operator_api.build_hypotheses_payload()["hypotheses"][0]["hypothesis_id"] == "H1"
    assert operator_api.create_hypothesis_payload({"title": "Austerity"})["hypothesis"]["title"] == "Austerity"
    result = operator_api.run_hypothesis_payload({"hypothesis_id": "H1", "persist": False})
    assert result["match_count"] == 1
    assert result["persist"] is False


def test_decision_trace_builds_action_conflicts():
    asof_date = pd.Timestamp("2026-05-14T00:00:00Z")
    candidates = pd.DataFrame(
        [
            {
                "asof_date": asof_date,
                "symbol": "ABC",
                "action_code": "SELL",
                "action_priority": 100,
                "action_source": "rebalance",
                "setup_id": "A",
                "unique_id": "u1",
                "raw_context_json": json.dumps({"suggested_action": "exit_stop", "lifecycle_reason": "Stop hit."}),
            },
            {
                "asof_date": asof_date,
                "symbol": "ABC",
                "action_code": "BUY",
                "action_priority": 50,
                "action_source": "portfolio",
                "setup_id": "B",
                "unique_id": "u2",
                "raw_context_json": "{}",
            },
        ]
    )
    winners = candidates.iloc[[0]].copy()

    conflicts = decision_trace.build_action_conflicts(candidates, winners)

    assert len(conflicts) == 1
    row = conflicts.iloc[0]
    assert row["winning_action_code"] == "SELL"
    assert row["losing_action_code"] == "BUY"
    assert row["lost_reason"].startswith("Lost to SELL")


def test_decision_trace_promoted_conflict_rule_matches_exact_pair():
    row = {
        "winning_action_code": "HOLD",
        "losing_action_code": "BUY",
        "winning_source": "lifecycle",
        "losing_source": "portfolio",
        "lost_reason": "No deterministic rule matched.",
    }
    dynamic_rules = [
        {
            "rule_id": "MANUAL_EVENT_REVIEW_BEATS_PORTFOLIO_BUY",
            "resolution_action": "keep_winner",
            "resolution_reason": "Operator promoted event review over portfolio buy.",
            "condition_json": json.dumps(
                    {
                        "condition_type": "action_pair_exact",
                        "winning_action_code": "HOLD",
                        "losing_action_code": "BUY",
                        "winning_source": "lifecycle",
                        "losing_source": "portfolio",
                    }
                ),
        }
    ]

    resolution = decision_trace.classify_action_conflict(row, dynamic_rules=dynamic_rules)
    miss = decision_trace.classify_action_conflict({**row, "losing_source": "screener"}, dynamic_rules=dynamic_rules)

    assert resolution["resolution_status"] == "resolved"
    assert resolution["resolution_rule_id"] == "MANUAL_EVENT_REVIEW_BEATS_PORTFOLIO_BUY"
    assert resolution["requires_manual_resolution"] is False
    assert miss["resolution_status"] == "unresolved"


def test_action_recommender_enabled_conflict_rule_changes_candidate_ranking(monkeypatch):
    asof_date = pd.Timestamp("2026-05-14T00:00:00Z")
    candidates = pd.DataFrame(
        [
            {
                "asof_date": asof_date,
                "published_on": pd.Timestamp("2026-05-14T09:45:00Z"),
                "symbol": "ABC",
                "setup_id": "LIFE",
                "unique_id": "exit-1",
                "action_code": "SELL",
                "action_priority": 30,
                "action_source": "rebalance",
                "source_action": "exit_stop",
                "transaction_type": "SELL",
                "execution_mode": "broker_order",
                "action_reason": "Stop hit.",
                "raw_context_json": json.dumps({"suggested_action": "exit_stop", "lifecycle_reason": "Stop hit."}),
                "load_ts": asof_date,
            },
            {
                "asof_date": asof_date,
                "published_on": pd.Timestamp("2026-05-14T10:00:00Z"),
                "symbol": "ABC",
                "setup_id": "PORT",
                "unique_id": "buy-1",
                "action_code": "BUY",
                "action_priority": 50,
                "action_source": "portfolio",
                "source_action": "approved",
                "transaction_type": "BUY",
                "execution_mode": "broker_order",
                "action_reason": "Fresh buy candidate.",
                "raw_context_json": json.dumps({"technical_state": "BUY_TRIGGERED"}),
                "load_ts": asof_date,
            },
        ]
    )

    monkeypatch.setattr(action_recommender, "load_enabled_conflict_rule_ids", lambda: {"EXIT_BEATS_ENTRY_OR_WATCH"})
    enabled_winner = action_recommender.rank_action_candidates(candidates)
    enabled_context = json.loads(enabled_winner.iloc[0]["raw_context_json"])

    monkeypatch.setattr(action_recommender, "load_enabled_conflict_rule_ids", lambda: set())
    disabled_winner = action_recommender.rank_action_candidates(candidates)

    assert enabled_winner.iloc[0]["action_code"] == "SELL"
    assert enabled_context["conflict_precedence_rule_id"] == "EXIT_BEATS_ENTRY_OR_WATCH"
    assert disabled_winner.iloc[0]["action_code"] == "BUY"


def test_action_recommender_promoted_conflict_rule_changes_candidate_ranking(monkeypatch):
    asof_date = pd.Timestamp("2026-05-14T00:00:00Z")
    candidates = pd.DataFrame(
        [
            {
                "asof_date": asof_date,
                "published_on": pd.Timestamp("2026-05-14T09:45:00Z"),
                "symbol": "ABC",
                "setup_id": "LIFE",
                "unique_id": "hold-1",
                "action_code": "HOLD",
                "action_priority": 20,
                "action_source": "lifecycle",
                "source_action": "hold_existing",
                "transaction_type": None,
                "execution_mode": "review_only",
                "action_reason": "Lifecycle says existing position is still valid.",
                "raw_context_json": "{}",
                "load_ts": asof_date,
            },
            {
                "asof_date": asof_date,
                "published_on": pd.Timestamp("2026-05-14T10:00:00Z"),
                "symbol": "ABC",
                "setup_id": "PORT",
                "unique_id": "buy-1",
                "action_code": "BUY",
                "action_priority": 50,
                "action_source": "portfolio",
                "source_action": "approved",
                "transaction_type": "BUY",
                "execution_mode": "broker_order",
                "action_reason": "Fresh buy candidate.",
                "raw_context_json": "{}",
                "load_ts": asof_date,
            },
        ]
    )
    promoted_rule = {
        "rule_id": "MANUAL_HOLD_BEATS_PORTFOLIO_BUY",
        "resolution_action": "keep_winner",
        "resolution_reason": "Operator confirmed lifecycle hold should beat this portfolio buy.",
        "priority": 25,
        "condition_json": json.dumps(
            {
                "condition_type": "action_pair_exact",
                "winning_action_code": "HOLD",
                "losing_action_code": "BUY",
                "winning_source": "lifecycle",
                "losing_source": "portfolio",
            }
        ),
    }

    monkeypatch.setattr(action_recommender, "load_enabled_conflict_rule_ids", lambda: set())
    monkeypatch.setattr(action_recommender, "load_enabled_dynamic_conflict_rules_for_ranking", lambda: [promoted_rule])
    promoted_winner = action_recommender.rank_action_candidates(candidates)
    promoted_context = json.loads(promoted_winner.iloc[0]["raw_context_json"])
    promoted_contract_df = action_recommender.add_recommendation_reason_contracts(promoted_winner, candidates)
    promoted_contract = json.loads(promoted_contract_df.iloc[0]["recommendation_reason_json"])
    promoted_conflict = promoted_contract["evidence"]["conflict_resolution"]

    nonmatching_rule = {
        **promoted_rule,
        "condition_json": json.dumps(
            {
                "condition_type": "action_pair_exact",
                "winning_action_code": "HOLD",
                "losing_action_code": "BUY",
                "winning_source": "lifecycle",
                "losing_source": "screener",
            }
        ),
    }
    monkeypatch.setattr(action_recommender, "load_enabled_dynamic_conflict_rules_for_ranking", lambda: [nonmatching_rule])
    fallback_winner = action_recommender.rank_action_candidates(candidates)

    assert promoted_winner.iloc[0]["action_code"] == "HOLD"
    assert promoted_context["conflict_precedence_rule_id"] == "MANUAL_HOLD_BEATS_PORTFOLIO_BUY"
    assert "lifecycle hold" in promoted_context["conflict_precedence_reason"]
    assert promoted_contract_df.iloc[0]["reason_contract_status"] == "complete"
    assert promoted_conflict["conflict_precedence_rule_id"] == "MANUAL_HOLD_BEATS_PORTFOLIO_BUY"
    assert promoted_conflict["conflict_precedence_score"] == 225
    assert "lifecycle hold" in promoted_conflict["conflict_precedence_reason"]
    assert promoted_conflict["same_symbol_conflict_count"] == 1
    assert promoted_conflict["losing_candidates"][0]["action_code"] == "BUY"
    assert promoted_conflict["losing_candidates"][0]["action_source"] == "portfolio"
    assert "conflict_resolution" in promoted_contract["evidence_sections_present"]
    assert fallback_winner.iloc[0]["action_code"] == "BUY"


def test_operator_api_updates_action_conflict_rule_enablement(monkeypatch):
    calls = []

    class Cursor:
        description = [
            ("rule_id",),
            ("enabled",),
            ("resolution_reason",),
            ("updated_at",),
        ]

        def execute(self, query, params):
            calls.append((query, params))

        def fetchone(self):
            return ("EXIT_BEATS_ENTRY_OR_WATCH", False, "Exit wins.", pd.Timestamp("2026-05-14T10:00:00Z"))

    class Session:
        def __enter__(self):
            return (None, Cursor())

        def __exit__(self, exc_type, exc, tb):
            return False

    monkeypatch.setattr(operator_api, "ensure_trace_tables", lambda: None)
    monkeypatch.setattr(operator_api, "db_session", lambda: Session())

    payload = operator_api.update_action_conflict_rule_payload("EXIT_BEATS_ENTRY_OR_WATCH", {"enabled": False})

    assert payload["status"] == "updated"
    assert payload["rule"]["rule_id"] == "EXIT_BEATS_ENTRY_OR_WATCH"
    assert payload["rule"]["enabled"] is False
    assert payload["rule"]["resolution_reason"] == "Exit wins."
    assert "historical action rows are not rewritten" in payload["note"]
    assert calls[0][1][0] is False
    assert calls[0][1][2] == "EXIT_BEATS_ENTRY_OR_WATCH"


def test_operator_api_updates_action_conflict_rule_reason(monkeypatch):
    calls = []

    class Cursor:
        description = [
            ("rule_id",),
            ("enabled",),
            ("resolution_reason",),
            ("updated_at",),
        ]

        def execute(self, query, params):
            calls.append((query, params))

        def fetchone(self):
            return ("EXIT_BEATS_ENTRY_OR_WATCH", True, "Operator-edited explanation.", pd.Timestamp("2026-05-14T10:00:00Z"))

    class Session:
        def __enter__(self):
            return (None, Cursor())

        def __exit__(self, exc_type, exc, tb):
            return False

    monkeypatch.setattr(operator_api, "ensure_trace_tables", lambda: None)
    monkeypatch.setattr(operator_api, "db_session", lambda: Session())

    payload = operator_api.update_action_conflict_rule_payload(
        "EXIT_BEATS_ENTRY_OR_WATCH",
        {"resolution_reason": " Operator-edited explanation. "},
    )

    assert payload["status"] == "updated"
    assert payload["rule"]["resolution_reason"] == "Operator-edited explanation."
    assert "resolution_reason = %s" in calls[0][0]
    assert calls[0][1][0] == "Operator-edited explanation."
    assert calls[0][1][2] == "EXIT_BEATS_ENTRY_OR_WATCH"


def test_operator_api_updates_action_conflict_rule_condition(monkeypatch):
    calls = []

    class Cursor:
        description = [
            ("rule_id",),
            ("enabled",),
            ("condition_json",),
            ("updated_at",),
        ]

        def execute(self, query, params):
            calls.append((query, params))

        def fetchone(self):
            return (
                "MANUAL_EVENT_REVIEW_BEATS_PORTFOLIO_BUY",
                False,
                calls[0][1][0],
                pd.Timestamp("2026-05-14T10:00:00Z"),
            )

    class Session:
        def __enter__(self):
            return (None, Cursor())

        def __exit__(self, exc_type, exc, tb):
            return False

    monkeypatch.setattr(operator_api, "ensure_trace_tables", lambda: None)
    monkeypatch.setattr(operator_api, "db_session", lambda: Session())

    payload = operator_api.update_action_conflict_rule_payload(
        "MANUAL_EVENT_REVIEW_BEATS_PORTFOLIO_BUY",
        {
            "condition": {
                "condition_type": "action_pair_exact",
                "winning_action_code": " manual_review ",
                "losing_action_code": " buy ",
                "winning_source": " Event_Policy ",
                "losing_source": " Portfolio ",
            }
        },
    )

    condition = json.loads(payload["rule"]["condition_json"])
    assert payload["status"] == "updated"
    assert "condition_json = %s" in calls[0][0]
    assert condition == {
        "condition_type": "action_pair_exact",
        "winning_action_code": "MANUAL_REVIEW",
        "losing_action_code": "BUY",
        "winning_source": "event_policy",
        "losing_source": "portfolio",
    }
    assert calls[0][1][2] == "MANUAL_EVENT_REVIEW_BEATS_PORTFOLIO_BUY"

    try:
        operator_api.update_action_conflict_rule_payload(
            "MANUAL_EVENT_REVIEW_BEATS_PORTFOLIO_BUY",
            {"condition": {"condition_type": "unsupported"}},
        )
    except ValueError as exc:
        assert "only action_pair_exact" in str(exc)
    else:
        raise AssertionError("unsupported conflict-rule condition should fail validation")


def test_operator_api_promotes_action_conflict_rule_disabled_by_default(monkeypatch):
    calls = []

    class Cursor:
        description = [
            ("rule_id",),
            ("enabled",),
            ("resolution_reason",),
            ("condition_json",),
            ("promoted_from_conflict_key",),
        ]

        def execute(self, query, params):
            calls.append((query, params))

        def fetchone(self):
            condition_json = calls[0][1][7]
            return (
                calls[0][1][0],
                calls[0][1][5],
                calls[0][1][4],
                condition_json,
                calls[0][1][8],
            )

    class Session:
        def __enter__(self):
            return (None, Cursor())

        def __exit__(self, exc_type, exc, tb):
            return False

    monkeypatch.setattr(operator_api, "ensure_trace_tables", lambda: None)
    monkeypatch.setattr(operator_api, "db_session", lambda: Session())

    payload = operator_api.promote_action_conflict_rule_payload(
        {
            "conflict": {
                "asof_date": "2026-05-14T00:00:00Z",
                "symbol": "ABC",
                "winning_action_code": "MANUAL_REVIEW",
                "losing_action_code": "BUY",
                "winning_source": "event_policy",
                "losing_source": "portfolio",
                "lost_reason": "Event review should win.",
            },
            "resolution_reason": "Operator confirmed event review should win over portfolio buy.",
        }
    )

    condition = json.loads(payload["rule"]["condition_json"])
    assert payload["status"] == "promoted"
    assert payload["rule"]["enabled"] is False
    assert payload["rule"]["rule_id"].startswith("MANUAL_")
    assert condition["condition_type"] == "action_pair_exact"
    assert condition["winning_action_code"] == "MANUAL_REVIEW"
    assert condition["losing_source"] == "portfolio"
    assert "disabled by default" in payload["note"]


def test_decision_trace_append_trace_casts_action_changed_boolean(monkeypatch):
    captured = []

    monkeypatch.setattr(decision_trace, "ensure_trace_tables", lambda: None)
    monkeypatch.setattr(
        decision_trace,
        "upsert_to_db",
        lambda df, table_name, unique_keys: captured.append((table_name, df.copy(), unique_keys)),
    )

    trace_id = decision_trace.append_trace(
        asof_date=pd.Timestamp("2026-05-26T00:00:00Z"),
        symbol="ABC",
        trigger_type="lifecycle",
        previous_action="hold",
        new_action="exit_stop",
        final_action="exit_stop",
    )

    assert trace_id
    assert captured[0][0] == decision_trace.TRACES_TABLE
    out = captured[0][1]
    assert str(out["action_changed"].dtype) == "boolean"
    assert bool(out.iloc[0]["action_changed"]) is True


def test_adversarial_review_emits_trace(monkeypatch):
    calls = []

    def fake_trace(func, **kwargs):
        calls.append((getattr(func, "__name__", str(func)), kwargs))
        return None

    monkeypatch.setattr(adversarial_review, "safe_trace_call", fake_trace)
    events = pd.DataFrame(
        [
            {
                "asof_date": pd.Timestamp("2026-05-14T00:00:00Z"),
                "published_on": pd.Timestamp("2026-05-14T09:00:00Z"),
                "setup_id": "SETUP",
                "symbol": "ABC",
                "unique_id": "evt-1",
                "event_source": "nse",
                "confidence": 0.9,
                "score_impact": 0.1,
                "materiality": "medium",
                "source_reliability": "high",
            }
        ]
    )

    reviews, meta = adversarial_review.build_reviews(events)

    assert meta["reviewed_count"] == 1
    assert len(reviews) == 1
    assert [name for name, _ in calls] == ["record_event_processing", "append_trace_step"]
    assert calls[0][1]["stage"] == "adversarial_review"


def test_action_recommender_persist_traces_consolidation(monkeypatch):
    calls = []

    def fake_trace(func, **kwargs):
        calls.append((getattr(func, "__name__", str(func)), kwargs))
        if getattr(func, "__name__", "") == "append_trace":
            return "trace-1"
        return None

    monkeypatch.setattr(action_recommender, "ensure_actions_table", lambda: None)
    monkeypatch.setattr(action_recommender, "persist_action_conflicts", lambda df: None)
    monkeypatch.setattr(action_recommender, "safe_trace_call", fake_trace)
    monkeypatch.setattr(action_recommender, "upsert_to_db", lambda *args, **kwargs: None)
    monkeypatch.setattr(action_recommender, "enrich_action_candidate_context", lambda df, asof_date: df)

    class DummyCursor:
        def execute(self, *args, **kwargs):
            return None

    class DummySession:
        def __enter__(self):
            return object(), DummyCursor()

        def __exit__(self, exc_type, exc, tb):
            return False

    monkeypatch.setattr(action_recommender, "db_session", lambda: DummySession())

    asof_date = pd.Timestamp("2026-05-14T00:00:00Z")
    candidates = pd.DataFrame(
        [
            {
                "asof_date": asof_date,
                "published_on": asof_date,
                "symbol": "ABC",
                "setup_id": "SETUP",
                "unique_id": "evt-1",
                "action_code": "SELL",
                "action_priority": 100,
                "action_source": "rebalance",
                "action_reason": "Stop hit.",
                "action_detail": "exit_stop",
                "execution_mode": "broker_order",
                "raw_context_json": json.dumps({"suggested_action": "exit_stop", "lifecycle_reason": "Stop hit."}),
                "load_ts": asof_date,
            },
            {
                "asof_date": asof_date,
                "published_on": asof_date,
                "symbol": "ABC",
                "setup_id": "SETUP",
                "unique_id": "evt-2",
                "action_code": "BUY",
                "action_priority": 50,
                "action_source": "portfolio",
                "action_reason": "New buy.",
                "raw_context_json": "{}",
                "load_ts": asof_date,
            },
        ]
    )
    winners = action_recommender.rank_action_candidates(candidates)
    winners.attrs["all_action_candidates"] = candidates

    action_recommender.persist_action_recommendations(winners)

    assert [name for name, _ in calls] == ["append_trace", "append_trace_step"]
    assert calls[0][1]["final_action"] == "SELL"
    assert calls[1][1]["payload"]["conflict_count"] == 1
    assert calls[1][1]["payload"]["reason_contract_status"] == "complete"


def test_action_recommender_reason_contract_complete_for_risk_bounded_buy():
    winners = pd.DataFrame(
        [
            {
                "asof_date": pd.Timestamp("2026-05-01T00:00:00Z"),
                "published_on": pd.Timestamp("2026-05-01T09:30:00Z"),
                "symbol": "TCS",
                "setup_id": "SETUP",
                "action_code": "BUY",
                "action_priority": 50,
                "action_source": "portfolio",
                "transaction_type": "BUY",
                "execution_mode": "broker_order",
                "stop_price": 100.0,
                "action_reason": "Breakout confirmed.",
                "action_detail": "Strong technical setup.",
                "raw_context_json": json.dumps({"technical_state": "BUY_TRIGGERED", "source_screener_slug": "breakouts"}),
            }
        ]
    )

    out = action_recommender.add_recommendation_reason_contracts(winners, winners)
    contract = json.loads(out.iloc[0]["recommendation_reason_json"])

    assert out.iloc[0]["action_code"] == "BUY"
    assert out.iloc[0]["reason_contract_status"] == "complete"
    assert contract["status"] == "complete"
    assert set(contract["evidence_sections_present"]) >= {"screener", "technical", "risk"}


def test_action_recommender_reason_contract_explains_same_symbol_source_precedence(monkeypatch):
    monkeypatch.setattr(action_recommender, "load_enabled_conflict_rule_ids", lambda: set(action_recommender.DEFAULT_CONFLICT_RULE_IDS))
    asof_date = pd.Timestamp("2026-05-01T00:00:00Z")
    candidates = pd.DataFrame(
        [
            {
                "asof_date": asof_date,
                "published_on": pd.Timestamp("2026-05-01T10:00:00Z"),
                "symbol": "TCS",
                "setup_id": "LIFE",
                "unique_id": "pos-1",
                "action_code": "SELL",
                "action_priority": 100,
                "action_source": "rebalance",
                "source_action": "exit_stop",
                "transaction_type": "SELL",
                "execution_mode": "broker_order",
                "reference_price": 124.0,
                "stop_price": 125.0,
                "action_reason": "Stop hit after close below risk level.",
                "action_detail": "exit_stop",
                "raw_context_json": json.dumps({"suggested_action": "exit_stop", "lifecycle_reason": "Stop hit."}),
                "load_ts": asof_date,
            },
            {
                "asof_date": asof_date,
                "published_on": pd.Timestamp("2026-05-01T10:05:00Z"),
                "symbol": "TCS",
                "setup_id": "PORT",
                "unique_id": "buy-1",
                "action_code": "BUY",
                "action_priority": 50,
                "action_source": "portfolio",
                "source_action": "approved",
                "transaction_type": "BUY",
                "execution_mode": "broker_order",
                "stop_price": 118.0,
                "action_reason": "Portfolio model still likes the breakout.",
                "raw_context_json": json.dumps({"technical_state": "BUY_TRIGGERED"}),
                "load_ts": asof_date,
            },
        ]
    )

    winners = action_recommender.rank_action_candidates(candidates)
    with_contract = action_recommender.add_recommendation_reason_contracts(winners, candidates)
    contract = json.loads(with_contract.iloc[0]["recommendation_reason_json"])
    conflict = contract["evidence"]["conflict_resolution"]

    assert with_contract.iloc[0]["action_code"] == "SELL"
    assert with_contract.iloc[0]["reason_contract_status"] == "complete"
    assert "conflict_resolution" in contract["evidence_sections_present"]
    assert conflict["same_symbol_conflict_count"] == 1
    assert conflict["winning_action_source"] == "rebalance"
    assert conflict["losing_candidates"][0]["action_source"] == "portfolio"
    assert "deterministic action priority" in conflict["source_precedence_reason"]


def test_action_recommender_reason_contract_explains_duplicate_buy_screener_collapse(monkeypatch):
    monkeypatch.setattr(action_recommender, "load_enabled_conflict_rule_ids", lambda: set(action_recommender.DEFAULT_CONFLICT_RULE_IDS))
    asof_date = pd.Timestamp("2026-05-01T00:00:00Z")
    candidates = pd.DataFrame(
        [
            {
                "asof_date": asof_date,
                "published_on": pd.Timestamp("2026-05-01T10:05:00Z"),
                "symbol": "TCS",
                "setup_id": "BREAKOUT",
                "unique_id": "screener-new",
                "action_code": "BUY",
                "action_priority": 50,
                "action_source": "portfolio",
                "source_action": "approved",
                "transaction_type": "BUY",
                "execution_mode": "broker_order",
                "stop_price": 118.0,
                "action_reason": "Breakout screener confirmed a fresh entry.",
                "raw_context_json": json.dumps({"technical_state": "BUY_TRIGGERED", "source_screener_slug": "breakouts"}),
                "load_ts": asof_date,
            },
            {
                "asof_date": asof_date,
                "published_on": pd.Timestamp("2026-05-01T09:55:00Z"),
                "symbol": "TCS",
                "setup_id": "MOMENTUM",
                "unique_id": "screener-old",
                "action_code": "BUY",
                "action_priority": 50,
                "action_source": "portfolio",
                "source_action": "approved",
                "transaction_type": "BUY",
                "execution_mode": "broker_order",
                "stop_price": 116.0,
                "action_reason": "Momentum screener also selected the same symbol.",
                "raw_context_json": json.dumps({"technical_state": "READY", "source_screener_slug": "momentum"}),
                "load_ts": asof_date,
            },
        ]
    )

    winners = action_recommender.rank_action_candidates(candidates)
    with_contract = action_recommender.add_recommendation_reason_contracts(winners, candidates)
    contract = json.loads(with_contract.iloc[0]["recommendation_reason_json"])
    conflict = contract["evidence"]["conflict_resolution"]

    assert with_contract.iloc[0]["action_code"] == "BUY"
    assert with_contract.iloc[0]["setup_id"] == "BREAKOUT"
    assert with_contract.iloc[0]["reason_contract_status"] == "complete"
    assert conflict["same_symbol_candidate_count"] == 2
    assert conflict["same_symbol_conflict_count"] == 1
    assert conflict["losing_candidates"][0]["setup_id"] == "MOMENTUM"
    assert conflict["losing_candidates"][0]["action_code"] == "BUY"
    assert "conflict_resolution" in contract["evidence_sections_present"]


def test_action_recommender_reason_contract_explains_manual_review_beating_watch(monkeypatch):
    monkeypatch.setattr(action_recommender, "load_enabled_conflict_rule_ids", lambda: set(action_recommender.DEFAULT_CONFLICT_RULE_IDS))
    asof_date = pd.Timestamp("2026-05-01T00:00:00Z")
    candidates = pd.DataFrame(
        [
            {
                "asof_date": asof_date,
                "published_on": pd.Timestamp("2026-05-01T10:00:00Z"),
                "symbol": "TCS",
                "setup_id": "EVENT",
                "unique_id": "event-policy-1",
                "action_code": "MANUAL_REVIEW",
                "action_priority": 80,
                "action_source": "event_policy",
                "source_action": "BUY_WATCH",
                "transaction_type": None,
                "execution_mode": "review_only",
                "action_reason": "Material announcement needs operator review before entry.",
                "raw_context_json": json.dumps({"event_class": "order_win", "verdict": "manual_review"}),
                "load_ts": asof_date,
            },
            {
                "asof_date": asof_date,
                "published_on": pd.Timestamp("2026-05-01T10:10:00Z"),
                "symbol": "TCS",
                "setup_id": "WATCH",
                "unique_id": "watch-1",
                "action_code": "WATCH",
                "action_priority": 10,
                "action_source": "watchlist",
                "source_action": "READY",
                "transaction_type": None,
                "execution_mode": "review_only",
                "action_reason": "Watchlist is ready but not independently actionable.",
                "raw_context_json": json.dumps({"technical_state": "READY", "watch_reasons": ["near pivot"]}),
                "load_ts": asof_date,
            },
        ]
    )

    winners = action_recommender.rank_action_candidates(candidates)
    with_contract = action_recommender.add_recommendation_reason_contracts(winners, candidates)
    contract = json.loads(with_contract.iloc[0]["recommendation_reason_json"])
    conflict = contract["evidence"]["conflict_resolution"]

    assert with_contract.iloc[0]["action_code"] == "MANUAL_REVIEW"
    assert with_contract.iloc[0]["action_source"] == "event_policy"
    assert with_contract.iloc[0]["reason_contract_status"] == "complete"
    assert conflict["same_symbol_conflict_count"] == 1
    assert conflict["losing_candidates"][0]["action_code"] == "WATCH"
    assert conflict["losing_candidates"][0]["action_source"] == "watchlist"
    assert "conflict_resolution" in contract["evidence_sections_present"]


def test_action_recommender_reason_contract_explains_event_review_beating_portfolio_buy(monkeypatch):
    monkeypatch.setattr(action_recommender, "load_enabled_conflict_rule_ids", lambda: set(action_recommender.DEFAULT_CONFLICT_RULE_IDS))
    monkeypatch.setattr(action_recommender, "load_enabled_dynamic_conflict_rules_for_ranking", lambda: [])
    asof_date = pd.Timestamp("2026-05-01T00:00:00Z")
    candidates = pd.DataFrame(
        [
            {
                "asof_date": asof_date,
                "published_on": pd.Timestamp("2026-05-01T10:00:00Z"),
                "symbol": "TCS",
                "setup_id": "EVENT",
                "unique_id": "EVT-REVIEW",
                "action_code": "MANUAL_REVIEW",
                "action_priority": 80,
                "action_source": "event_policy",
                "source_action": "BUY_WATCH",
                "transaction_type": None,
                "execution_mode": "review_only",
                "action_reason": "Material order win needs confirmation before entry.",
                "action_detail": "Check order size, margin profile, and current price reaction.",
                "raw_context_json": json.dumps(
                    {
                        "event_class": "ORDER_WIN",
                        "verdict": "manual_review",
                        "state_transition_hint": "strengthens",
                        "score_impact": 0.24,
                        "review_action": "clear",
                    }
                ),
                "load_ts": asof_date,
            },
            {
                "asof_date": asof_date,
                "published_on": pd.Timestamp("2026-05-01T10:15:00Z"),
                "symbol": "TCS",
                "setup_id": "BUY",
                "unique_id": "BUY-1",
                "action_code": "BUY",
                "action_priority": 50,
                "action_source": "portfolio",
                "source_action": "approved",
                "transaction_type": "BUY",
                "execution_mode": "broker_order",
                "stop_price": 118.0,
                "action_reason": "Portfolio model approves the breakout entry.",
                "raw_context_json": json.dumps({"technical_state": "BUY_TRIGGERED", "source_screener_slug": "breakouts"}),
                "load_ts": asof_date,
            },
        ]
    )

    winners = action_recommender.rank_action_candidates(candidates)
    with_contract = action_recommender.add_recommendation_reason_contracts(winners, candidates)
    contract = json.loads(with_contract.iloc[0]["recommendation_reason_json"])
    event_evidence = contract["evidence"]["event"]
    manual_review = contract["evidence"]["manual_review"]
    conflict = contract["evidence"]["conflict_resolution"]

    assert with_contract.iloc[0]["action_code"] == "MANUAL_REVIEW"
    assert with_contract.iloc[0]["action_source"] == "event_policy"
    assert with_contract.iloc[0]["transaction_type"] is None
    assert with_contract.iloc[0]["execution_mode"] == "review_only"
    assert with_contract.iloc[0]["reason_contract_status"] == "complete"
    assert set(contract["evidence_sections_present"]) >= {"event", "manual_review", "conflict_resolution"}
    assert event_evidence["event_class"] == "ORDER_WIN"
    assert manual_review["boundary"] == "event_policy_review_required"
    assert manual_review["blocked_original_action"] == "MANUAL_REVIEW"
    assert manual_review["broker_execution_allowed"] is False
    assert conflict["same_symbol_conflict_count"] == 1
    assert conflict["winning_action_code"] == "MANUAL_REVIEW"
    assert conflict["winning_action_source"] == "event_policy"
    assert conflict["losing_candidates"][0]["action_code"] == "BUY"
    assert conflict["losing_candidates"][0]["action_source"] == "portfolio"
    assert conflict["losing_candidates"][0]["source_action"] == "approved"


def test_action_recommender_reason_contract_explains_lifecycle_hold_beating_watch(monkeypatch):
    monkeypatch.setattr(action_recommender, "load_enabled_conflict_rule_ids", lambda: set(action_recommender.DEFAULT_CONFLICT_RULE_IDS))
    monkeypatch.setattr(action_recommender, "load_enabled_dynamic_conflict_rules_for_ranking", lambda: [])
    asof_date = pd.Timestamp("2026-05-01T00:00:00Z")
    candidates = pd.DataFrame(
        [
            {
                "asof_date": asof_date,
                "published_on": pd.Timestamp("2026-05-01T09:55:00Z"),
                "symbol": "TCS",
                "setup_id": "LIFE",
                "unique_id": "POS-HOLD",
                "action_code": "HOLD",
                "action_priority": 20,
                "action_source": "lifecycle",
                "source_action": "hold",
                "transaction_type": None,
                "execution_mode": "review_only",
                "reference_price": 126.0,
                "stop_price": 112.0,
                "action_reason": "Open position remains valid above stop and below target.",
                "action_detail": "No lifecycle exit trigger.",
                "raw_context_json": json.dumps(
                    {
                        "position_status": "open",
                        "next_action": "hold",
                        "lifecycle_reason": "Open position remains valid above stop and below target.",
                        "next_action_reason": "No lifecycle exit trigger.",
                    }
                ),
                "load_ts": asof_date,
            },
            {
                "asof_date": asof_date,
                "published_on": pd.Timestamp("2026-05-01T10:30:00Z"),
                "symbol": "TCS",
                "setup_id": "WATCH",
                "unique_id": "WATCH-1",
                "action_code": "WATCH",
                "action_priority": 10,
                "action_source": "watchlist",
                "source_action": "READY",
                "transaction_type": None,
                "execution_mode": "review_only",
                "action_reason": "Watchlist setup is near pivot but not independently actionable.",
                "raw_context_json": json.dumps({"technical_state": "READY", "watch_reasons": ["near pivot"]}),
                "load_ts": asof_date,
            },
        ]
    )

    winners = action_recommender.rank_action_candidates(candidates)
    with_contract = action_recommender.add_recommendation_reason_contracts(winners, candidates)
    contract = json.loads(with_contract.iloc[0]["recommendation_reason_json"])
    conflict = contract["evidence"]["conflict_resolution"]

    assert with_contract.iloc[0]["action_code"] == "HOLD"
    assert with_contract.iloc[0]["action_source"] == "lifecycle"
    assert with_contract.iloc[0]["transaction_type"] is None
    assert with_contract.iloc[0]["execution_mode"] == "review_only"
    assert with_contract.iloc[0]["reason_contract_status"] == "complete"
    assert set(contract["evidence_sections_present"]) >= {"lifecycle", "risk", "conflict_resolution"}
    assert conflict["same_symbol_conflict_count"] == 1
    assert conflict["winning_action_code"] == "HOLD"
    assert conflict["winning_action_source"] == "lifecycle"
    assert conflict["losing_candidates"][0]["action_code"] == "WATCH"
    assert conflict["losing_candidates"][0]["action_source"] == "watchlist"
    assert conflict["losing_candidates"][0]["source_action"] == "READY"
    assert "deterministic action priority" in conflict["source_precedence_reason"]


def test_action_recommender_reason_contract_explains_portfolio_buy_beating_watch(monkeypatch):
    monkeypatch.setattr(action_recommender, "load_enabled_conflict_rule_ids", lambda: set(action_recommender.DEFAULT_CONFLICT_RULE_IDS))
    monkeypatch.setattr(action_recommender, "load_enabled_dynamic_conflict_rules_for_ranking", lambda: [])
    asof_date = pd.Timestamp("2026-05-01T00:00:00Z")
    candidates = pd.DataFrame(
        [
            {
                "asof_date": asof_date,
                "published_on": pd.Timestamp("2026-05-01T10:00:00Z"),
                "symbol": "TCS",
                "setup_id": "PORT",
                "unique_id": "BUY-1",
                "action_code": "BUY",
                "action_priority": 50,
                "action_source": "portfolio",
                "source_action": "approved",
                "transaction_type": "BUY",
                "execution_mode": "broker_order",
                "stop_price": 118.0,
                "action_reason": "Portfolio model approves the breakout entry.",
                "raw_context_json": json.dumps({"technical_state": "BUY_TRIGGERED", "source_screener_slug": "breakouts"}),
                "load_ts": asof_date,
            },
            {
                "asof_date": asof_date,
                "published_on": pd.Timestamp("2026-05-01T10:20:00Z"),
                "symbol": "TCS",
                "setup_id": "WATCH",
                "unique_id": "WATCH-1",
                "action_code": "WATCH",
                "action_priority": 10,
                "action_source": "watchlist",
                "source_action": "READY",
                "transaction_type": None,
                "execution_mode": "review_only",
                "action_reason": "Watchlist candidate is near pivot but not independently actionable.",
                "raw_context_json": json.dumps({"technical_state": "READY", "watch_reasons": ["near pivot"]}),
                "load_ts": asof_date,
            },
        ]
    )

    winners = action_recommender.rank_action_candidates(candidates)
    with_contract = action_recommender.add_recommendation_reason_contracts(winners, candidates)
    contract = json.loads(with_contract.iloc[0]["recommendation_reason_json"])
    conflict = contract["evidence"]["conflict_resolution"]

    assert with_contract.iloc[0]["action_code"] == "BUY"
    assert with_contract.iloc[0]["action_source"] == "portfolio"
    assert with_contract.iloc[0]["execution_mode"] == "broker_order"
    assert with_contract.iloc[0]["reason_contract_status"] == "complete"
    assert set(contract["evidence_sections_present"]) >= {"screener", "technical", "risk", "conflict_resolution"}
    assert conflict["same_symbol_conflict_count"] == 1
    assert conflict["winning_action_code"] == "BUY"
    assert conflict["winning_action_source"] == "portfolio"
    assert conflict["losing_candidates"][0]["action_code"] == "WATCH"
    assert conflict["losing_candidates"][0]["action_source"] == "watchlist"
    assert "conflict-rule precedence" in conflict["source_precedence_reason"]


def test_action_recommender_reason_contract_explains_buy_more_beating_portfolio_buy(monkeypatch):
    monkeypatch.setattr(action_recommender, "load_enabled_conflict_rule_ids", lambda: set(action_recommender.DEFAULT_CONFLICT_RULE_IDS))
    monkeypatch.setattr(action_recommender, "load_enabled_dynamic_conflict_rules_for_ranking", lambda: [])
    asof_date = pd.Timestamp("2026-05-01T00:00:00Z")
    candidates = pd.DataFrame(
        [
            {
                "asof_date": asof_date,
                "published_on": pd.Timestamp("2026-05-01T10:00:00Z"),
                "symbol": "TCS",
                "setup_id": "ADD_ON",
                "unique_id": "ADD-1",
                "action_code": "BUY_MORE",
                "action_priority": 60,
                "action_source": "portfolio",
                "source_action": "add_on_pullback",
                "transaction_type": "BUY",
                "execution_mode": "broker_order",
                "reference_price": 140.0,
                "stop_price": 126.0,
                "action_fraction": 0.2,
                "action_reason": "Add on a constructive pullback while the open position remains valid.",
                "action_detail": "add_on_pullback",
                "raw_context_json": json.dumps(
                    {
                        "technical_state": "ADD_ON_PULLBACK",
                        "source_screener_slug": "open-position-add-ons",
                        "position_status": "open",
                        "suggested_action": "add_on_pullback",
                        "lifecycle_reason": "Open position remains above stop and the pullback is constructive.",
                        "next_action_reason": "Add 20 percent on pullback support.",
                    }
                ),
                "load_ts": asof_date,
            },
            {
                "asof_date": asof_date,
                "published_on": pd.Timestamp("2026-05-01T10:20:00Z"),
                "symbol": "TCS",
                "setup_id": "BUY",
                "unique_id": "BUY-1",
                "action_code": "BUY",
                "action_priority": 50,
                "action_source": "portfolio",
                "source_action": "approved",
                "transaction_type": "BUY",
                "execution_mode": "broker_order",
                "stop_price": 118.0,
                "action_reason": "Portfolio model also approves a fresh entry setup.",
                "raw_context_json": json.dumps({"technical_state": "BUY_TRIGGERED", "source_screener_slug": "breakouts"}),
                "load_ts": asof_date,
            },
        ]
    )

    winners = action_recommender.rank_action_candidates(candidates)
    with_contract = action_recommender.add_recommendation_reason_contracts(winners, candidates)
    contract = json.loads(with_contract.iloc[0]["recommendation_reason_json"])
    conflict = contract["evidence"]["conflict_resolution"]

    assert with_contract.iloc[0]["action_code"] == "BUY_MORE"
    assert with_contract.iloc[0]["action_source"] == "portfolio"
    assert with_contract.iloc[0]["transaction_type"] == "BUY"
    assert with_contract.iloc[0]["execution_mode"] == "broker_order"
    assert with_contract.iloc[0]["reason_contract_status"] == "complete"
    assert set(contract["evidence_sections_present"]) >= {"screener", "technical", "risk", "lifecycle", "conflict_resolution"}
    assert conflict["same_symbol_conflict_count"] == 1
    assert conflict["winning_action_code"] == "BUY_MORE"
    assert conflict["winning_action_source"] == "portfolio"
    assert conflict["losing_candidates"][0]["action_code"] == "BUY"
    assert conflict["losing_candidates"][0]["action_source"] == "portfolio"
    assert conflict["losing_candidates"][0]["source_action"] == "approved"


def test_action_recommender_reason_contract_explains_buy_more_beating_watch(monkeypatch):
    monkeypatch.setattr(action_recommender, "load_enabled_conflict_rule_ids", lambda: set(action_recommender.DEFAULT_CONFLICT_RULE_IDS))
    monkeypatch.setattr(action_recommender, "load_enabled_dynamic_conflict_rules_for_ranking", lambda: [])
    asof_date = pd.Timestamp("2026-05-01T00:00:00Z")
    candidates = pd.DataFrame(
        [
            {
                "asof_date": asof_date,
                "published_on": pd.Timestamp("2026-05-01T10:00:00Z"),
                "symbol": "TCS",
                "setup_id": "ADD_ON",
                "unique_id": "ADD-1",
                "action_code": "BUY_MORE",
                "action_priority": 60,
                "action_source": "portfolio",
                "source_action": "add_on_pullback",
                "transaction_type": "BUY",
                "execution_mode": "broker_order",
                "reference_price": 140.0,
                "stop_price": 126.0,
                "action_fraction": 0.2,
                "action_reason": "Add on a constructive pullback while the open position remains valid.",
                "action_detail": "add_on_pullback",
                "raw_context_json": json.dumps(
                    {
                        "technical_state": "ADD_ON_PULLBACK",
                        "source_screener_slug": "open-position-add-ons",
                        "position_status": "open",
                        "suggested_action": "add_on_pullback",
                        "lifecycle_reason": "Open position remains above stop and the pullback is constructive.",
                        "next_action_reason": "Add 20 percent on pullback support.",
                    }
                ),
                "load_ts": asof_date,
            },
            {
                "asof_date": asof_date,
                "published_on": pd.Timestamp("2026-05-01T10:20:00Z"),
                "symbol": "TCS",
                "setup_id": "WATCH",
                "unique_id": "WATCH-1",
                "action_code": "WATCH",
                "action_priority": 10,
                "action_source": "watchlist",
                "source_action": "READY",
                "transaction_type": None,
                "execution_mode": "review_only",
                "action_reason": "Watchlist candidate is near pivot but not independently actionable.",
                "raw_context_json": json.dumps({"technical_state": "READY", "watch_reasons": ["near pivot"]}),
                "load_ts": asof_date,
            },
        ]
    )

    winners = action_recommender.rank_action_candidates(candidates)
    with_contract = action_recommender.add_recommendation_reason_contracts(winners, candidates)
    contract = json.loads(with_contract.iloc[0]["recommendation_reason_json"])
    conflict = contract["evidence"]["conflict_resolution"]

    assert with_contract.iloc[0]["action_code"] == "BUY_MORE"
    assert with_contract.iloc[0]["action_source"] == "portfolio"
    assert with_contract.iloc[0]["transaction_type"] == "BUY"
    assert with_contract.iloc[0]["execution_mode"] == "broker_order"
    assert with_contract.iloc[0]["reason_contract_status"] == "complete"
    assert set(contract["evidence_sections_present"]) >= {"screener", "technical", "risk", "lifecycle", "conflict_resolution"}
    assert conflict["same_symbol_conflict_count"] == 1
    assert conflict["winning_action_code"] == "BUY_MORE"
    assert conflict["winning_action_source"] == "portfolio"
    assert conflict["losing_candidates"][0]["action_code"] == "WATCH"
    assert conflict["losing_candidates"][0]["action_source"] == "watchlist"
    assert conflict["losing_candidates"][0]["source_action"] == "READY"
    assert "conflict-rule precedence" in conflict["source_precedence_reason"]


def test_action_recommender_reason_contract_explains_portfolio_sell_beating_portfolio_buy(monkeypatch):
    monkeypatch.setattr(action_recommender, "load_enabled_conflict_rule_ids", lambda: set(action_recommender.DEFAULT_CONFLICT_RULE_IDS))
    monkeypatch.setattr(action_recommender, "load_enabled_dynamic_conflict_rules_for_ranking", lambda: [])
    asof_date = pd.Timestamp("2026-05-01T00:00:00Z")
    candidates = pd.DataFrame(
        [
            {
                "asof_date": asof_date,
                "published_on": pd.Timestamp("2026-05-01T10:00:00Z"),
                "symbol": "TCS",
                "setup_id": "EXIT",
                "unique_id": "SELL-1",
                "action_code": "SELL",
                "action_priority": 85,
                "action_source": "portfolio",
                "source_action": "stop_loss_exit",
                "transaction_type": "SELL",
                "execution_mode": "broker_order",
                "reference_price": 132.0,
                "stop_price": 134.0,
                "action_fraction": 1.0,
                "action_reason": "Exit the portfolio position after a confirmed stop-loss breach.",
                "action_detail": "stop_loss_exit",
                "raw_context_json": json.dumps(
                    {
                        "position_status": "open",
                        "technical_state": "FULL_EXIT",
                        "suggested_action": "stop_loss_exit",
                        "lifecycle_reason": "Close moved below the approved stop after entry.",
                        "next_action_reason": "Exit the remaining position to enforce the risk plan.",
                    }
                ),
                "load_ts": asof_date,
            },
            {
                "asof_date": asof_date,
                "published_on": pd.Timestamp("2026-05-01T10:20:00Z"),
                "symbol": "TCS",
                "setup_id": "BUY",
                "unique_id": "BUY-1",
                "action_code": "BUY",
                "action_priority": 50,
                "action_source": "portfolio",
                "source_action": "approved",
                "transaction_type": "BUY",
                "execution_mode": "broker_order",
                "stop_price": 118.0,
                "action_reason": "Portfolio model still approves a fresh entry setup.",
                "raw_context_json": json.dumps({"technical_state": "BUY_TRIGGERED", "source_screener_slug": "breakouts"}),
                "load_ts": asof_date,
            },
        ]
    )

    winners = action_recommender.rank_action_candidates(candidates)
    with_contract = action_recommender.add_recommendation_reason_contracts(winners, candidates)
    contract = json.loads(with_contract.iloc[0]["recommendation_reason_json"])
    conflict = contract["evidence"]["conflict_resolution"]

    assert with_contract.iloc[0]["action_code"] == "SELL"
    assert with_contract.iloc[0]["action_source"] == "portfolio"
    assert with_contract.iloc[0]["transaction_type"] == "SELL"
    assert with_contract.iloc[0]["execution_mode"] == "broker_order"
    assert with_contract.iloc[0]["reason_contract_status"] == "complete"
    assert set(contract["evidence_sections_present"]) >= {"technical", "lifecycle", "risk", "conflict_resolution"}
    assert conflict["conflict_precedence_rule_id"] == "EXIT_BEATS_ENTRY_OR_WATCH"
    assert conflict["winning_action_code"] == "SELL"
    assert conflict["winning_action_source"] == "portfolio"
    assert conflict["same_symbol_conflict_count"] == 1
    assert conflict["losing_candidates"][0]["action_code"] == "BUY"
    assert conflict["losing_candidates"][0]["action_source"] == "portfolio"
    assert conflict["losing_candidates"][0]["source_action"] == "approved"


def test_action_recommender_reason_contract_explains_sell_beating_watch(monkeypatch):
    monkeypatch.setattr(action_recommender, "load_enabled_conflict_rule_ids", lambda: set(action_recommender.DEFAULT_CONFLICT_RULE_IDS))
    monkeypatch.setattr(action_recommender, "load_enabled_dynamic_conflict_rules_for_ranking", lambda: [])
    asof_date = pd.Timestamp("2026-05-01T00:00:00Z")
    candidates = pd.DataFrame(
        [
            {
                "asof_date": asof_date,
                "published_on": pd.Timestamp("2026-05-01T09:45:00Z"),
                "symbol": "TCS",
                "setup_id": "EXIT",
                "unique_id": "SELL-1",
                "action_code": "SELL",
                "action_priority": 100,
                "action_source": "rebalance",
                "source_action": "exit_technical_failure",
                "transaction_type": "SELL",
                "execution_mode": "broker_order",
                "reference_price": 124.0,
                "stop_price": 125.0,
                "action_fraction": 1.0,
                "action_reason": "Exit after technical invalidation of the open position.",
                "action_detail": "exit_technical_failure",
                "raw_context_json": json.dumps(
                    {
                        "position_status": "open",
                        "technical_state": "FULL_EXIT",
                        "suggested_action": "exit_technical_failure",
                        "lifecycle_reason": "Technical failure invalidated the open position.",
                        "next_action_reason": "Exit the remaining position before monitoring new entries.",
                    }
                ),
                "load_ts": asof_date,
            },
            {
                "asof_date": asof_date,
                "published_on": pd.Timestamp("2026-05-01T10:30:00Z"),
                "symbol": "TCS",
                "setup_id": "WATCH",
                "unique_id": "WATCH-1",
                "action_code": "WATCH",
                "action_priority": 10,
                "action_source": "watchlist",
                "source_action": "READY",
                "transaction_type": None,
                "execution_mode": "review_only",
                "action_reason": "Watchlist setup is near pivot but not independently actionable.",
                "raw_context_json": json.dumps(
                    {
                        "technical_state": "READY",
                        "technical_total_score": 72.0,
                        "source_screener_slug": "breakout-watch",
                    }
                ),
                "load_ts": asof_date,
            },
        ]
    )

    winners = action_recommender.rank_action_candidates(candidates)
    with_contract = action_recommender.add_recommendation_reason_contracts(winners, candidates)
    contract = json.loads(with_contract.iloc[0]["recommendation_reason_json"])
    conflict = contract["evidence"]["conflict_resolution"]

    assert with_contract.iloc[0]["action_code"] == "SELL"
    assert with_contract.iloc[0]["action_source"] == "rebalance"
    assert with_contract.iloc[0]["transaction_type"] == "SELL"
    assert with_contract.iloc[0]["execution_mode"] == "broker_order"
    assert with_contract.iloc[0]["reason_contract_status"] == "complete"
    assert set(contract["evidence_sections_present"]) >= {"technical", "lifecycle", "risk", "conflict_resolution"}
    assert conflict["conflict_precedence_rule_id"] == "EXIT_BEATS_ENTRY_OR_WATCH"
    assert conflict["same_symbol_conflict_count"] == 1
    assert conflict["winning_action_code"] == "SELL"
    assert conflict["winning_action_source"] == "rebalance"
    assert conflict["losing_candidates"][0]["action_code"] == "WATCH"
    assert conflict["losing_candidates"][0]["action_source"] == "watchlist"
    assert conflict["losing_candidates"][0]["source_action"] == "READY"


def test_action_recommender_reason_contract_explains_partial_sell_beating_portfolio_buy(monkeypatch):
    monkeypatch.setattr(action_recommender, "load_enabled_conflict_rule_ids", lambda: set(action_recommender.DEFAULT_CONFLICT_RULE_IDS))
    monkeypatch.setattr(action_recommender, "load_enabled_dynamic_conflict_rules_for_ranking", lambda: [])
    asof_date = pd.Timestamp("2026-05-01T00:00:00Z")
    candidates = pd.DataFrame(
        [
            {
                "asof_date": asof_date,
                "published_on": pd.Timestamp("2026-05-01T10:00:00Z"),
                "symbol": "TCS",
                "setup_id": "TRIM",
                "unique_id": "POS-TRIM",
                "action_code": "PARTIAL_SELL",
                "action_priority": 90,
                "action_source": "rebalance",
                "source_action": "trim_winner",
                "transaction_type": "SELL",
                "execution_mode": "broker_order",
                "reference_price": 145.0,
                "stop_price": 128.0,
                "action_fraction": 0.25,
                "action_reason": "Trim winner after target extension and elevated exposure.",
                "action_detail": "trim_winner",
                "raw_context_json": json.dumps(
                    {
                        "position_status": "open",
                        "suggested_action": "trim_winner",
                        "lifecycle_reason": "Position exceeded target band; reduce exposure while keeping core holding.",
                        "next_action_reason": "Trim 25 percent after target extension.",
                    }
                ),
                "load_ts": asof_date,
            },
            {
                "asof_date": asof_date,
                "published_on": pd.Timestamp("2026-05-01T10:20:00Z"),
                "symbol": "TCS",
                "setup_id": "BUY",
                "unique_id": "BUY-1",
                "action_code": "BUY",
                "action_priority": 50,
                "action_source": "portfolio",
                "source_action": "approved",
                "transaction_type": "BUY",
                "execution_mode": "broker_order",
                "stop_price": 118.0,
                "action_reason": "Portfolio model still approves the entry.",
                "raw_context_json": json.dumps({"technical_state": "BUY_TRIGGERED", "source_screener_slug": "breakouts"}),
                "load_ts": asof_date,
            },
        ]
    )

    winners = action_recommender.rank_action_candidates(candidates)
    with_contract = action_recommender.add_recommendation_reason_contracts(winners, candidates)
    contract = json.loads(with_contract.iloc[0]["recommendation_reason_json"])
    conflict = contract["evidence"]["conflict_resolution"]

    assert with_contract.iloc[0]["action_code"] == "PARTIAL_SELL"
    assert with_contract.iloc[0]["action_source"] == "rebalance"
    assert with_contract.iloc[0]["transaction_type"] == "SELL"
    assert with_contract.iloc[0]["execution_mode"] == "broker_order"
    assert with_contract.iloc[0]["reason_contract_status"] == "complete"
    assert set(contract["evidence_sections_present"]) >= {"lifecycle", "risk", "conflict_resolution"}
    assert conflict["conflict_precedence_rule_id"] == "EXIT_BEATS_ENTRY_OR_WATCH"
    assert conflict["same_symbol_conflict_count"] == 1
    assert conflict["winning_action_code"] == "PARTIAL_SELL"
    assert conflict["losing_candidates"][0]["action_code"] == "BUY"
    assert conflict["losing_candidates"][0]["action_source"] == "portfolio"


def test_action_recommender_reason_contract_explains_partial_sell_beating_watch(monkeypatch):
    monkeypatch.setattr(action_recommender, "load_enabled_conflict_rule_ids", lambda: set(action_recommender.DEFAULT_CONFLICT_RULE_IDS))
    monkeypatch.setattr(action_recommender, "load_enabled_dynamic_conflict_rules_for_ranking", lambda: [])
    asof_date = pd.Timestamp("2026-05-01T00:00:00Z")
    candidates = pd.DataFrame(
        [
            {
                "asof_date": asof_date,
                "published_on": pd.Timestamp("2026-05-01T09:45:00Z"),
                "symbol": "TCS",
                "setup_id": "TRIM",
                "unique_id": "POS-TRIM",
                "action_code": "PARTIAL_SELL",
                "action_priority": 90,
                "action_source": "rebalance",
                "source_action": "trim_winner",
                "transaction_type": "SELL",
                "execution_mode": "broker_order",
                "reference_price": 145.0,
                "stop_price": 128.0,
                "action_fraction": 0.25,
                "action_reason": "Trim winner after target extension and elevated exposure.",
                "action_detail": "trim_winner",
                "raw_context_json": json.dumps(
                    {
                        "position_status": "open",
                        "technical_state": "PARTIAL_EXIT",
                        "suggested_action": "trim_winner",
                        "lifecycle_reason": "Position exceeded target band; reduce exposure while keeping core holding.",
                        "next_action_reason": "Trim 25 percent after target extension.",
                    }
                ),
                "load_ts": asof_date,
            },
            {
                "asof_date": asof_date,
                "published_on": pd.Timestamp("2026-05-01T10:30:00Z"),
                "symbol": "TCS",
                "setup_id": "WATCH",
                "unique_id": "WATCH-1",
                "action_code": "WATCH",
                "action_priority": 10,
                "action_source": "watchlist",
                "source_action": "READY",
                "transaction_type": None,
                "execution_mode": "review_only",
                "action_reason": "Watchlist setup is near pivot but not independently actionable.",
                "raw_context_json": json.dumps(
                    {
                        "technical_state": "READY",
                        "technical_total_score": 72.0,
                        "source_screener_slug": "breakout-watch",
                    }
                ),
                "load_ts": asof_date,
            },
        ]
    )

    winners = action_recommender.rank_action_candidates(candidates)
    with_contract = action_recommender.add_recommendation_reason_contracts(winners, candidates)
    contract = json.loads(with_contract.iloc[0]["recommendation_reason_json"])
    conflict = contract["evidence"]["conflict_resolution"]

    assert with_contract.iloc[0]["action_code"] == "PARTIAL_SELL"
    assert with_contract.iloc[0]["action_source"] == "rebalance"
    assert with_contract.iloc[0]["transaction_type"] == "SELL"
    assert with_contract.iloc[0]["execution_mode"] == "broker_order"
    assert with_contract.iloc[0]["reason_contract_status"] == "complete"
    assert set(contract["evidence_sections_present"]) >= {"technical", "lifecycle", "risk", "conflict_resolution"}
    assert conflict["conflict_precedence_rule_id"] == "EXIT_BEATS_ENTRY_OR_WATCH"
    assert conflict["same_symbol_conflict_count"] == 1
    assert conflict["winning_action_code"] == "PARTIAL_SELL"
    assert conflict["winning_action_source"] == "rebalance"
    assert conflict["losing_candidates"][0]["action_code"] == "WATCH"
    assert conflict["losing_candidates"][0]["action_source"] == "watchlist"
    assert conflict["losing_candidates"][0]["source_action"] == "READY"


def test_action_recommender_reason_contract_explains_tighten_stop_beating_portfolio_buy(monkeypatch):
    monkeypatch.setattr(action_recommender, "load_enabled_conflict_rule_ids", lambda: set(action_recommender.DEFAULT_CONFLICT_RULE_IDS))
    monkeypatch.setattr(action_recommender, "load_enabled_dynamic_conflict_rules_for_ranking", lambda: [])
    asof_date = pd.Timestamp("2026-05-01T00:00:00Z")
    candidates = pd.DataFrame(
        [
            {
                "asof_date": asof_date,
                "published_on": pd.Timestamp("2026-05-01T10:00:00Z"),
                "symbol": "TCS",
                "setup_id": "STOP",
                "unique_id": "POS-STOP",
                "action_code": "TIGHTEN_STOP",
                "action_priority": 70,
                "action_source": "rebalance",
                "source_action": "tighten_stop",
                "transaction_type": None,
                "execution_mode": "review_only",
                "reference_price": 145.0,
                "stop_price": 128.0,
                "recommended_stop_price": 136.0,
                "action_reason": "Tighten the stop after a large gain while preserving the position.",
                "action_detail": "tighten_stop",
                "raw_context_json": json.dumps(
                    {
                        "position_status": "open",
                        "suggested_action": "tighten_stop",
                        "lifecycle_reason": "Position is extended after a large gain.",
                        "next_action_reason": "Raise the protective stop to lock in gains.",
                    }
                ),
                "load_ts": asof_date,
            },
            {
                "asof_date": asof_date,
                "published_on": pd.Timestamp("2026-05-01T10:20:00Z"),
                "symbol": "TCS",
                "setup_id": "BUY",
                "unique_id": "BUY-1",
                "action_code": "BUY",
                "action_priority": 50,
                "action_source": "portfolio",
                "source_action": "approved",
                "transaction_type": "BUY",
                "execution_mode": "broker_order",
                "stop_price": 118.0,
                "action_reason": "Portfolio model still approves the entry.",
                "raw_context_json": json.dumps({"technical_state": "BUY_TRIGGERED", "source_screener_slug": "breakouts"}),
                "load_ts": asof_date,
            },
        ]
    )

    winners = action_recommender.rank_action_candidates(candidates)
    with_contract = action_recommender.add_recommendation_reason_contracts(winners, candidates)
    contract = json.loads(with_contract.iloc[0]["recommendation_reason_json"])
    conflict = contract["evidence"]["conflict_resolution"]

    assert with_contract.iloc[0]["action_code"] == "TIGHTEN_STOP"
    assert with_contract.iloc[0]["action_source"] == "rebalance"
    assert with_contract.iloc[0]["transaction_type"] is None
    assert with_contract.iloc[0]["execution_mode"] == "review_only"
    assert with_contract.iloc[0]["reason_contract_status"] == "complete"
    assert set(contract["evidence_sections_present"]) >= {"lifecycle", "risk", "conflict_resolution"}
    assert conflict["conflict_precedence_rule_id"] == "EXIT_BEATS_ENTRY_OR_WATCH"
    assert conflict["same_symbol_conflict_count"] == 1
    assert conflict["winning_action_code"] == "TIGHTEN_STOP"
    assert conflict["winning_action_source"] == "rebalance"
    assert conflict["losing_candidates"][0]["action_code"] == "BUY"
    assert conflict["losing_candidates"][0]["action_source"] == "portfolio"


def test_action_recommender_reason_contract_explains_tighten_stop_beating_watch(monkeypatch):
    monkeypatch.setattr(action_recommender, "load_enabled_conflict_rule_ids", lambda: set(action_recommender.DEFAULT_CONFLICT_RULE_IDS))
    monkeypatch.setattr(action_recommender, "load_enabled_dynamic_conflict_rules_for_ranking", lambda: [])
    asof_date = pd.Timestamp("2026-05-01T00:00:00Z")
    candidates = pd.DataFrame(
        [
            {
                "asof_date": asof_date,
                "published_on": pd.Timestamp("2026-05-01T09:45:00Z"),
                "symbol": "TCS",
                "setup_id": "STOP",
                "unique_id": "POS-STOP",
                "action_code": "TIGHTEN_STOP",
                "action_priority": 70,
                "action_source": "rebalance",
                "source_action": "tighten_stop",
                "transaction_type": None,
                "execution_mode": "review_only",
                "reference_price": 145.0,
                "stop_price": 128.0,
                "recommended_stop_price": 136.0,
                "action_reason": "Tighten the stop after a large gain while preserving the position.",
                "action_detail": "tighten_stop",
                "raw_context_json": json.dumps(
                    {
                        "position_status": "open",
                        "technical_state": "HOLD",
                        "suggested_action": "tighten_stop",
                        "lifecycle_reason": "Position is extended after a large gain.",
                        "next_action_reason": "Raise the protective stop to lock in gains.",
                    }
                ),
                "load_ts": asof_date,
            },
            {
                "asof_date": asof_date,
                "published_on": pd.Timestamp("2026-05-01T10:30:00Z"),
                "symbol": "TCS",
                "setup_id": "WATCH",
                "unique_id": "WATCH-1",
                "action_code": "WATCH",
                "action_priority": 10,
                "action_source": "watchlist",
                "source_action": "READY",
                "transaction_type": None,
                "execution_mode": "review_only",
                "action_reason": "Watchlist setup is near pivot but not independently actionable.",
                "raw_context_json": json.dumps(
                    {
                        "technical_state": "READY",
                        "technical_total_score": 72.0,
                        "source_screener_slug": "breakout-watch",
                    }
                ),
                "load_ts": asof_date,
            },
        ]
    )

    winners = action_recommender.rank_action_candidates(candidates)
    with_contract = action_recommender.add_recommendation_reason_contracts(winners, candidates)
    contract = json.loads(with_contract.iloc[0]["recommendation_reason_json"])
    conflict = contract["evidence"]["conflict_resolution"]

    assert with_contract.iloc[0]["action_code"] == "TIGHTEN_STOP"
    assert with_contract.iloc[0]["action_source"] == "rebalance"
    assert with_contract.iloc[0]["transaction_type"] is None
    assert with_contract.iloc[0]["execution_mode"] == "review_only"
    assert with_contract.iloc[0]["reason_contract_status"] == "complete"
    assert set(contract["evidence_sections_present"]) >= {"technical", "lifecycle", "risk", "conflict_resolution"}
    assert conflict["conflict_precedence_rule_id"] == "EXIT_BEATS_ENTRY_OR_WATCH"
    assert conflict["same_symbol_conflict_count"] == 1
    assert conflict["winning_action_code"] == "TIGHTEN_STOP"
    assert conflict["winning_action_source"] == "rebalance"
    assert conflict["losing_candidates"][0]["action_code"] == "WATCH"
    assert conflict["losing_candidates"][0]["action_source"] == "watchlist"
    assert conflict["losing_candidates"][0]["source_action"] == "READY"


def test_action_recommender_adversarial_veto_manual_review_beats_buy_and_watch(monkeypatch):
    monkeypatch.setattr(action_recommender, "load_enabled_conflict_rule_ids", lambda: set(action_recommender.DEFAULT_CONFLICT_RULE_IDS))
    asof_date = pd.Timestamp("2026-05-01T00:00:00Z")
    candidates = pd.DataFrame(
        [
            {
                "asof_date": asof_date,
                "published_on": pd.Timestamp("2026-05-01T10:00:00Z"),
                "symbol": "TCS",
                "setup_id": "EVENT",
                "unique_id": "EVT-VETO",
                "action_code": "MANUAL_REVIEW",
                "action_priority": 5,
                "action_source": "event_policy",
                "source_action": "BUY_WATCH",
                "transaction_type": None,
                "execution_mode": "review_only",
                "action_reason": "Adversarial review vetoed the otherwise positive event.",
                "action_detail": "Operator must resolve contradictory/stale evidence before any entry.",
                "raw_context_json": json.dumps(
                    {
                        "event_class": "ORDER_WIN",
                        "verdict": "continue",
                        "state_transition_hint": "strengthens",
                        "score_impact": -0.5,
                        "review_action": "veto",
                        "veto": True,
                        "review_reason": "Order details conflict with prior disclosure.",
                        "action_status": "blocked_by_adversarial_review",
                    }
                ),
                "load_ts": asof_date,
            },
            {
                "asof_date": asof_date,
                "published_on": pd.Timestamp("2026-05-01T10:05:00Z"),
                "symbol": "TCS",
                "setup_id": "PORT",
                "unique_id": "BUY-1",
                "action_code": "BUY",
                "action_priority": 50,
                "action_source": "portfolio",
                "source_action": "approved",
                "transaction_type": "BUY",
                "execution_mode": "broker_order",
                "stop_price": 118.0,
                "action_reason": "Portfolio model still sees a breakout.",
                "raw_context_json": json.dumps({"technical_state": "BUY_TRIGGERED"}),
                "load_ts": asof_date,
            },
            {
                "asof_date": asof_date,
                "published_on": pd.Timestamp("2026-05-01T10:10:00Z"),
                "symbol": "TCS",
                "setup_id": "WATCH",
                "unique_id": "WATCH-1",
                "action_code": "WATCH",
                "action_priority": 10,
                "action_source": "watchlist",
                "source_action": "READY",
                "transaction_type": None,
                "execution_mode": "review_only",
                "action_reason": "Watchlist candidate is near pivot.",
                "raw_context_json": json.dumps({"technical_state": "READY"}),
                "load_ts": asof_date,
            },
        ]
    )

    winners = action_recommender.rank_action_candidates(candidates)
    with_contract = action_recommender.add_recommendation_reason_contracts(winners, candidates)
    contract = json.loads(with_contract.iloc[0]["recommendation_reason_json"])
    conflict = contract["evidence"]["conflict_resolution"]

    assert with_contract.iloc[0]["action_code"] == "MANUAL_REVIEW"
    assert with_contract.iloc[0]["action_source"] == "event_policy"
    assert with_contract.iloc[0]["execution_mode"] == "review_only"
    assert with_contract.iloc[0]["transaction_type"] is None
    assert with_contract.iloc[0]["reason_contract_status"] == "complete"
    assert conflict["conflict_precedence_rule_id"] == "ADVERSARIAL_VETO_MANUAL_BEATS_POSITIVE_OR_WATCH"
    assert conflict["same_symbol_conflict_count"] == 2
    assert {item["action_code"] for item in conflict["losing_candidates"]} == {"BUY", "WATCH"}
    assert contract["evidence"]["event"]["review_action"] == "veto"
    assert contract["evidence"]["event"]["veto"] is True
    assert contract["evidence"]["event"]["review_reason"] == "Order details conflict with prior disclosure."


def test_action_recommender_reason_contract_matrix_covers_event_playbook_lifecycle(monkeypatch):
    monkeypatch.setattr(action_recommender, "load_enabled_conflict_rule_ids", lambda: set(action_recommender.DEFAULT_CONFLICT_RULE_IDS))
    asof_date = pd.Timestamp("2026-05-01T00:00:00Z")
    candidates = pd.DataFrame(
        [
            {
                "asof_date": asof_date,
                "published_on": pd.Timestamp("2026-05-01T10:20:00Z"),
                "symbol": "TCS",
                "setup_id": "PLAYBOOK_A",
                "unique_id": "NEWS-1",
                "action_code": "MANUAL_REVIEW",
                "action_priority": 80,
                "action_source": "playbook_symbol",
                "source_action": "REDUCE_EXPOSURE_REVIEW",
                "transaction_type": None,
                "execution_mode": "review_only",
                "action_reason": "Trusted playbook requires exposure review after management commentary.",
                "action_detail": "Review event impact before changing exposure.",
                "raw_context_json": json.dumps(
                    {
                        "playbook_id": "PLAYBOOK_A",
                        "playbook_source_key": "NEWS-1",
                        "action_type": "REDUCE_EXPOSURE_REVIEW",
                        "operator_summary": "Review event impact before changing exposure.",
                        "decision_reason": "Management commentary matched a risk playbook.",
                        "checks": [{"check_type": "contradiction", "blocking": True}],
                    }
                ),
                "load_ts": asof_date,
            },
            {
                "asof_date": asof_date,
                "published_on": pd.Timestamp("2026-05-01T10:10:00Z"),
                "symbol": "TCS",
                "setup_id": "EVENT",
                "unique_id": "EVT-1",
                "action_code": "MANUAL_REVIEW",
                "action_priority": 80,
                "action_source": "event_policy",
                "source_action": "BUY_WATCH",
                "transaction_type": None,
                "execution_mode": "review_only",
                "action_reason": "Order win is material but needs operator confirmation.",
                "action_detail": "Check order size, margins, and current price reaction.",
                "raw_context_json": json.dumps(
                    {
                        "event_class": "ORDER_WIN",
                        "verdict": "continue",
                        "state_transition_hint": "strengthens",
                        "score_impact": 0.22,
                        "review_action": "clear",
                    }
                ),
                "load_ts": asof_date,
            },
            {
                "asof_date": asof_date,
                "published_on": pd.Timestamp("2026-05-01T09:50:00Z"),
                "symbol": "TCS",
                "setup_id": "LIFE",
                "unique_id": "POS-1",
                "action_code": "HOLD",
                "action_priority": 20,
                "action_source": "lifecycle",
                "source_action": "hold",
                "transaction_type": None,
                "execution_mode": "review_only",
                "reference_price": 121.0,
                "stop_price": 112.0,
                "action_reason": "Open position remains above stop and below target.",
                "action_detail": "No lifecycle exit trigger.",
                "raw_context_json": json.dumps(
                    {
                        "position_status": "open",
                        "next_action": "hold",
                        "lifecycle_reason": "Open position remains above stop and below target.",
                        "next_action_reason": "No lifecycle exit trigger.",
                    }
                ),
                "load_ts": asof_date,
            },
        ]
    )

    winners = action_recommender.rank_action_candidates(candidates)
    with_contract = action_recommender.add_recommendation_reason_contracts(winners, candidates)
    contract = json.loads(with_contract.iloc[0]["recommendation_reason_json"])
    conflict = contract["evidence"]["conflict_resolution"]

    assert with_contract.iloc[0]["action_source"] == "playbook_symbol"
    assert with_contract.iloc[0]["reason_contract_status"] == "complete"
    assert set(contract["evidence_sections_present"]) >= {"playbook", "conflict_resolution"}
    assert conflict["same_symbol_conflict_count"] == 2
    assert {item["action_source"] for item in conflict["losing_candidates"]} == {"event_policy", "lifecycle"}
    assert contract["evidence"]["playbook"]["playbook_id"] == "PLAYBOOK_A"


def test_action_recommender_reason_contract_matrix_covers_remaining_event_playbook_lifecycle_permutations(monkeypatch):
    monkeypatch.setattr(action_recommender, "load_enabled_conflict_rule_ids", lambda: set(action_recommender.DEFAULT_CONFLICT_RULE_IDS))
    asof_date = pd.Timestamp("2026-05-01T00:00:00Z")

    def event_row(published_on: str = "2026-05-01T10:10:00Z") -> dict:
        return {
            "asof_date": asof_date,
            "published_on": pd.Timestamp(published_on),
            "symbol": "TCS",
            "setup_id": "EVENT",
            "unique_id": "EVT-1",
            "action_code": "MANUAL_REVIEW",
            "action_priority": 80,
            "action_source": "event_policy",
            "source_action": "BUY_WATCH",
            "transaction_type": None,
            "execution_mode": "review_only",
            "action_reason": "Order win is material but needs operator confirmation.",
            "action_detail": "Check order size, margins, and current price reaction.",
            "raw_context_json": json.dumps(
                {
                    "event_class": "ORDER_WIN",
                    "verdict": "continue",
                    "state_transition_hint": "strengthens",
                    "score_impact": 0.22,
                    "review_action": "clear",
                }
            ),
            "load_ts": asof_date,
        }

    def playbook_row(published_on: str = "2026-05-01T10:20:00Z") -> dict:
        return {
            "asof_date": asof_date,
            "published_on": pd.Timestamp(published_on),
            "symbol": "TCS",
            "setup_id": "PLAYBOOK_A",
            "unique_id": "NEWS-1",
            "action_code": "MANUAL_REVIEW",
            "action_priority": 80,
            "action_source": "playbook_symbol",
            "source_action": "REDUCE_EXPOSURE_REVIEW",
            "transaction_type": None,
            "execution_mode": "review_only",
            "action_reason": "Trusted playbook requires exposure review after management commentary.",
            "action_detail": "Review event impact before changing exposure.",
            "raw_context_json": json.dumps(
                {
                    "playbook_id": "PLAYBOOK_A",
                    "playbook_source_key": "NEWS-1",
                    "action_type": "REDUCE_EXPOSURE_REVIEW",
                    "operator_summary": "Review event impact before changing exposure.",
                    "decision_reason": "Management commentary matched a risk playbook.",
                    "checks": [{"check_type": "contradiction", "blocking": True}],
                }
            ),
            "load_ts": asof_date,
        }

    lifecycle_hold = {
        "asof_date": asof_date,
        "published_on": pd.Timestamp("2026-05-01T09:50:00Z"),
        "symbol": "TCS",
        "setup_id": "LIFE",
        "unique_id": "POS-1",
        "action_code": "HOLD",
        "action_priority": 20,
        "action_source": "lifecycle",
        "source_action": "hold",
        "transaction_type": None,
        "execution_mode": "review_only",
        "reference_price": 121.0,
        "stop_price": 112.0,
        "action_reason": "Open position remains above stop and below target.",
        "action_detail": "No lifecycle exit trigger.",
        "raw_context_json": json.dumps(
            {
                "position_status": "open",
                "next_action": "hold",
                "lifecycle_reason": "Open position remains above stop and below target.",
                "next_action_reason": "No lifecycle exit trigger.",
            }
        ),
        "load_ts": asof_date,
    }
    lifecycle_exit = {
        **lifecycle_hold,
        "unique_id": "POS-EXIT",
        "action_code": "SELL",
        "action_priority": 100,
        "action_source": "rebalance",
        "source_action": "exit_stop",
        "transaction_type": "SELL",
        "execution_mode": "broker_order",
        "action_reason": "Stop hit after close below risk level.",
        "action_detail": "exit_stop",
        "raw_context_json": json.dumps(
            {
                "position_status": "open",
                "suggested_action": "exit_stop",
                "lifecycle_reason": "Stop hit after close below risk level.",
                "next_action_reason": "Exit because risk level failed.",
                "conflict_precedence_rule_id": "EXIT_BEATS_ENTRY_OR_WATCH",
            }
        ),
    }

    cases = [
        {
            "name": "exit_beats_review_overlays",
            "rows": [event_row(), playbook_row(), lifecycle_exit],
            "winner_source": "rebalance",
            "winner_action": "SELL",
            "lost_sources": {"event_policy", "playbook_symbol"},
            "required_sections": {"lifecycle", "conflict_resolution"},
        },
        {
            "name": "event_review_beats_lifecycle_hold",
            "rows": [event_row(), lifecycle_hold],
            "winner_source": "event_policy",
            "winner_action": "MANUAL_REVIEW",
            "lost_sources": {"lifecycle"},
            "required_sections": {"event", "conflict_resolution"},
        },
        {
            "name": "event_review_wins_equal_priority_freshness_tie",
            "rows": [event_row("2026-05-01T10:30:00Z"), playbook_row("2026-05-01T10:20:00Z"), lifecycle_hold],
            "winner_source": "event_policy",
            "winner_action": "MANUAL_REVIEW",
            "lost_sources": {"playbook_symbol", "lifecycle"},
            "required_sections": {"event", "conflict_resolution"},
        },
    ]

    for case in cases:
        candidates = pd.DataFrame(case["rows"])
        winners = action_recommender.rank_action_candidates(candidates)
        with_contract = action_recommender.add_recommendation_reason_contracts(winners, candidates)
        contract = json.loads(with_contract.iloc[0]["recommendation_reason_json"])
        conflict = contract["evidence"]["conflict_resolution"]

        assert with_contract.iloc[0]["action_source"] == case["winner_source"], case["name"]
        assert with_contract.iloc[0]["action_code"] == case["winner_action"], case["name"]
        assert with_contract.iloc[0]["reason_contract_status"] == "complete", case["name"]
        assert set(contract["evidence_sections_present"]) >= case["required_sections"], case["name"]
        assert {item["action_source"] for item in conflict["losing_candidates"]} == case["lost_sources"], case["name"]
        assert conflict["same_symbol_conflict_count"] == len(case["lost_sources"]), case["name"]


def test_action_recommender_reason_contract_covers_buy_sell_manual_watch_collision(monkeypatch):
    monkeypatch.setattr(action_recommender, "load_enabled_conflict_rule_ids", lambda: set(action_recommender.DEFAULT_CONFLICT_RULE_IDS))
    monkeypatch.setattr(action_recommender, "load_enabled_dynamic_conflict_rules_for_ranking", lambda: [])
    asof_date = pd.Timestamp("2026-05-01T00:00:00Z")
    candidates = pd.DataFrame(
        [
            {
                "asof_date": asof_date,
                "published_on": pd.Timestamp("2026-05-01T10:00:00Z"),
                "symbol": "TCS",
                "setup_id": "BUY",
                "unique_id": "BUY-1",
                "action_code": "BUY",
                "action_priority": 50,
                "action_source": "portfolio",
                "source_action": "approved",
                "transaction_type": "BUY",
                "execution_mode": "broker_order",
                "stop_price": 118.0,
                "action_reason": "Portfolio model still approves the entry.",
                "raw_context_json": json.dumps({"technical_state": "BUY_TRIGGERED", "source_screener_slug": "breakouts"}),
                "load_ts": asof_date,
            },
            {
                "asof_date": asof_date,
                "published_on": pd.Timestamp("2026-05-01T09:45:00Z"),
                "symbol": "TCS",
                "setup_id": "SELL",
                "unique_id": "POS-EXIT",
                "action_code": "SELL",
                "action_priority": 100,
                "action_source": "rebalance",
                "source_action": "exit_stop",
                "transaction_type": "SELL",
                "execution_mode": "broker_order",
                "reference_price": 124.0,
                "stop_price": 125.0,
                "action_reason": "Stop hit after close below risk level.",
                "action_detail": "exit_stop",
                "raw_context_json": json.dumps(
                    {
                        "position_status": "open",
                        "suggested_action": "exit_stop",
                        "lifecycle_reason": "Stop hit after close below risk level.",
                        "next_action_reason": "Exit because risk level failed.",
                    }
                ),
                "load_ts": asof_date,
            },
            {
                "asof_date": asof_date,
                "published_on": pd.Timestamp("2026-05-01T10:15:00Z"),
                "symbol": "TCS",
                "setup_id": "EVENT",
                "unique_id": "EVT-1",
                "action_code": "MANUAL_REVIEW",
                "action_priority": 80,
                "action_source": "event_policy",
                "source_action": "BUY_WATCH",
                "transaction_type": None,
                "execution_mode": "review_only",
                "action_reason": "Material event needs operator review before changing exposure.",
                "raw_context_json": json.dumps({"event_class": "ORDER_WIN", "verdict": "manual_review", "review_action": "clear"}),
                "load_ts": asof_date,
            },
            {
                "asof_date": asof_date,
                "published_on": pd.Timestamp("2026-05-01T10:30:00Z"),
                "symbol": "TCS",
                "setup_id": "WATCH",
                "unique_id": "WATCH-1",
                "action_code": "WATCH",
                "action_priority": 10,
                "action_source": "watchlist",
                "source_action": "READY",
                "transaction_type": None,
                "execution_mode": "review_only",
                "action_reason": "Watchlist setup is near pivot but not independently actionable.",
                "raw_context_json": json.dumps({"technical_state": "READY"}),
                "load_ts": asof_date,
            },
        ]
    )

    winners = action_recommender.rank_action_candidates(candidates)
    with_contract = action_recommender.add_recommendation_reason_contracts(winners, candidates)
    contract = json.loads(with_contract.iloc[0]["recommendation_reason_json"])
    conflict = contract["evidence"]["conflict_resolution"]

    assert with_contract.iloc[0]["action_code"] == "SELL"
    assert with_contract.iloc[0]["action_source"] == "rebalance"
    assert with_contract.iloc[0]["reason_contract_status"] == "complete"
    assert conflict["conflict_precedence_rule_id"] == "EXIT_BEATS_ENTRY_OR_WATCH"
    assert conflict["same_symbol_conflict_count"] == 3
    assert {item["action_code"] for item in conflict["losing_candidates"]} == {"BUY", "MANUAL_REVIEW", "WATCH"}
    assert {item["action_source"] for item in conflict["losing_candidates"]} == {"portfolio", "event_policy", "watchlist"}
    assert set(contract["evidence_sections_present"]) >= {"lifecycle", "conflict_resolution"}


def test_action_recommender_enriches_candidate_and_regime_context(monkeypatch):
    asof_date = pd.Timestamp("2026-05-01T00:00:00Z")
    rows = pd.DataFrame(
        [
            {
                "asof_date": asof_date,
                "published_on": asof_date,
                "symbol": "TCS",
                "setup_id": "SETUP",
                "action_code": "BUY",
                "action_priority": 50,
                "action_source": "portfolio",
                "action_reason": "Breakout confirmed.",
                "execution_mode": "broker_order",
                "stop_price": 100.0,
                "raw_context_json": "{}",
            }
        ]
    )
    monkeypatch.setattr(
        action_recommender,
        "_load_latest_candidate_context",
        lambda asof_date, symbols: (
            {
                ("SETUP", "TCS"): {
                    "symbol": "TCS",
                    "setup_id": "SETUP",
                    "technical_state": "BUY_TRIGGERED",
                    "technical_trigger_type": "breakout",
                    "source_screener_slug": "breakouts",
                    "setup_score": 0.82,
                }
            },
            {},
        ),
    )
    monkeypatch.setattr(
        action_recommender,
        "_load_regime_context",
        lambda asof_date: {"regime_name": "RISK_ON", "macro_risk_state": "LOW", "macro_stress_score": 0.2},
    )
    monkeypatch.setattr(action_recommender, "_load_latest_event_context", lambda asof_date, symbols: ({}, {}))
    monkeypatch.setattr(action_recommender, "_load_latest_playbook_context", lambda asof_date, symbols: ({}, {}))

    enriched = action_recommender.enrich_action_candidate_context(rows, asof_date=asof_date)
    enriched = action_recommender.add_recommendation_reason_contracts(enriched, enriched)
    context = json.loads(enriched.iloc[0]["raw_context_json"])
    contract = json.loads(enriched.iloc[0]["recommendation_reason_json"])

    assert context["technical_state"] == "BUY_TRIGGERED"
    assert context["source_screener_slug"] == "breakouts"
    assert context["macro_risk_state"] == "LOW"
    assert enriched.iloc[0]["reason_contract_status"] == "complete"
    assert set(contract["evidence_sections_present"]) >= {"technical", "screener", "macro_regime", "risk"}


def test_action_recommender_blocks_positive_broker_action_in_risk_off_market(monkeypatch):
    asof_date = pd.Timestamp("2026-05-01T00:00:00Z")
    rows = pd.DataFrame(
        [
            {
                "asof_date": asof_date,
                "published_on": asof_date,
                "symbol": "TCS",
                "setup_id": "SETUP",
                "action_code": "BUY",
                "action_priority": 50,
                "action_source": "portfolio",
                "transaction_type": "BUY",
                "execution_mode": "broker_order",
                "approved_allocation_inr": 100000.0,
                "stop_price": 100.0,
                "action_reason": "Portfolio approved breakout entry.",
                "raw_context_json": "{}",
            }
        ]
    )
    monkeypatch.setattr(
        action_recommender,
        "load_latest_market_context",
        lambda *args, **kwargs: {
            "summary": {
                "regime_name": "RISK_OFF",
                "macro_risk_state": "HIGH",
                "breadth_trend_alignment_pct": 32.0,
                "risk_off_score": 0.72,
            },
            "top_universe": [{"symbol": "TCS", "rank_pct": 3.0, "sector_name": "IT"}],
        },
    )

    out = action_recommender.apply_market_context_adjustments(rows, asof_date=asof_date)
    context = json.loads(out.iloc[0]["raw_context_json"])

    assert out.iloc[0]["action_code"] == "MANUAL_REVIEW"
    assert out.iloc[0]["transaction_type"] is None
    assert out.iloc[0]["execution_mode"] == "review_only"
    assert out.iloc[0]["approved_allocation_inr"] == 0.0
    assert context["market_context_adjustment"] == "positive_action_blocked_by_market_context"
    assert context["market_context_adjustment_json"]["original_action_code"] == "BUY"

    with_contract = action_recommender.add_recommendation_reason_contracts(out, out)
    contract = json.loads(with_contract.iloc[0]["recommendation_reason_json"])
    assert contract["original_action_code"] == "BUY"
    assert set(contract["evidence_sections_present"]) >= {"macro_regime", "manual_review"}
    manual_review = contract["evidence"]["manual_review"]
    assert manual_review["manual_review_boundary"] == "market_context_positive_action_review_required"
    assert manual_review["manual_review_effect"] == "review_only_no_broker_execution"
    assert manual_review["blocked_original_action_code"] == "BUY"
    assert manual_review["manual_review_action_source"] == "portfolio"
    assert manual_review["broker_execution_allowed"] is False
    macro = contract["evidence"]["macro_regime"]
    assert macro["market_context_adjustment"] == "positive_action_blocked_by_market_context"
    assert macro["market_context_adjustment_reason"].startswith("Positive broker action was blocked")
    assert macro["regime_name"] == "RISK_OFF"
    assert macro["macro_risk_state"] == "HIGH"
    assert macro["breadth_trend_alignment_pct"] == 32.0
    assert macro["risk_off_score"] == 0.72
    assert macro["top_context_rank_pct"] == 3.0
    assert macro["top_context_sector"] == "IT"


def test_action_recommender_market_gated_manual_review_beats_watch_with_contract(monkeypatch):
    monkeypatch.setattr(action_recommender, "load_enabled_conflict_rule_ids", lambda: set(action_recommender.DEFAULT_CONFLICT_RULE_IDS))
    monkeypatch.setattr(action_recommender, "load_enabled_dynamic_conflict_rules_for_ranking", lambda: [])
    asof_date = pd.Timestamp("2026-05-01T00:00:00Z")
    rows = pd.DataFrame(
        [
            {
                "asof_date": asof_date,
                "published_on": pd.Timestamp("2026-05-01T10:00:00Z"),
                "symbol": "TCS",
                "setup_id": "PORT",
                "unique_id": "BUY-1",
                "action_code": "BUY",
                "action_priority": 50,
                "action_source": "portfolio",
                "source_action": "approved",
                "transaction_type": "BUY",
                "execution_mode": "broker_order",
                "approved_allocation_inr": 100000.0,
                "stop_price": 118.0,
                "action_reason": "Portfolio model approves the breakout entry.",
                "raw_context_json": json.dumps({"technical_state": "BUY_TRIGGERED", "source_screener_slug": "breakouts"}),
                "load_ts": asof_date,
            },
            {
                "asof_date": asof_date,
                "published_on": pd.Timestamp("2026-05-01T10:20:00Z"),
                "symbol": "TCS",
                "setup_id": "WATCH",
                "unique_id": "WATCH-1",
                "action_code": "WATCH",
                "action_priority": 10,
                "action_source": "watchlist",
                "source_action": "READY",
                "transaction_type": None,
                "execution_mode": "review_only",
                "action_reason": "Watchlist setup is near pivot but not independently actionable.",
                "raw_context_json": json.dumps({"technical_state": "READY", "watch_reasons": ["near pivot"]}),
                "load_ts": asof_date,
            },
        ]
    )
    monkeypatch.setattr(
        action_recommender,
        "load_latest_market_context",
        lambda *args, **kwargs: {
            "summary": {
                "regime_name": "RISK_OFF",
                "macro_risk_state": "HIGH",
                "breadth_trend_alignment_pct": 31.0,
                "risk_off_score": 0.74,
            },
            "top_universe": [{"symbol": "TCS", "rank_pct": 4.0, "sector_name": "IT"}],
        },
    )

    adjusted = action_recommender.apply_market_context_adjustments(rows, asof_date=asof_date)
    winners = action_recommender.rank_action_candidates(adjusted)
    with_contract = action_recommender.add_recommendation_reason_contracts(winners, adjusted)
    contract = json.loads(with_contract.iloc[0]["recommendation_reason_json"])
    manual_review = contract["evidence"]["manual_review"]
    macro = contract["evidence"]["macro_regime"]
    conflict = contract["evidence"]["conflict_resolution"]

    assert with_contract.iloc[0]["action_code"] == "MANUAL_REVIEW"
    assert with_contract.iloc[0]["action_source"] == "portfolio"
    assert with_contract.iloc[0]["source_action"] == "approved"
    assert with_contract.iloc[0]["transaction_type"] is None
    assert with_contract.iloc[0]["execution_mode"] == "review_only"
    assert with_contract.iloc[0]["reason_contract_status"] == "complete"
    assert contract["original_action_code"] == "BUY"
    assert set(contract["evidence_sections_present"]) >= {"manual_review", "macro_regime", "conflict_resolution"}
    assert manual_review["manual_review_boundary"] == "market_context_positive_action_review_required"
    assert manual_review["manual_review_effect"] == "review_only_no_broker_execution"
    assert manual_review["blocked_original_action_code"] == "BUY"
    assert manual_review["manual_review_action_source"] == "portfolio"
    assert manual_review["broker_execution_allowed"] is False
    assert macro["market_context_adjustment"] == "positive_action_blocked_by_market_context"
    assert macro["risk_off_score"] == 0.74
    assert conflict["same_symbol_conflict_count"] == 1
    assert conflict["winning_action_code"] == "MANUAL_REVIEW"
    assert conflict["winning_action_source"] == "portfolio"
    assert conflict["losing_candidates"][0]["action_code"] == "WATCH"
    assert conflict["losing_candidates"][0]["action_source"] == "watchlist"
    assert conflict["losing_candidates"][0]["source_action"] == "READY"


def test_action_recommender_market_gated_buy_more_beats_watch_with_contract(monkeypatch):
    monkeypatch.setattr(action_recommender, "load_enabled_conflict_rule_ids", lambda: set(action_recommender.DEFAULT_CONFLICT_RULE_IDS))
    monkeypatch.setattr(action_recommender, "load_enabled_dynamic_conflict_rules_for_ranking", lambda: [])
    asof_date = pd.Timestamp("2026-05-01T00:00:00Z")
    rows = pd.DataFrame(
        [
            {
                "asof_date": asof_date,
                "published_on": pd.Timestamp("2026-05-01T10:00:00Z"),
                "symbol": "TCS",
                "setup_id": "ADD_ON",
                "unique_id": "BUY-MORE-1",
                "action_code": "BUY_MORE",
                "action_priority": 60,
                "action_source": "portfolio",
                "source_action": "add_on_pullback",
                "transaction_type": "BUY",
                "execution_mode": "broker_order",
                "approved_allocation_inr": 50000.0,
                "stop_price": 118.0,
                "action_reason": "Portfolio model approves an add-on pullback entry.",
                "raw_context_json": json.dumps(
                    {
                        "technical_state": "ADD_ON_PULLBACK",
                        "source_screener_slug": "portfolio-add-ons",
                    }
                ),
                "load_ts": asof_date,
            },
            {
                "asof_date": asof_date,
                "published_on": pd.Timestamp("2026-05-01T10:20:00Z"),
                "symbol": "TCS",
                "setup_id": "WATCH",
                "unique_id": "WATCH-1",
                "action_code": "WATCH",
                "action_priority": 10,
                "action_source": "watchlist",
                "source_action": "READY",
                "transaction_type": None,
                "execution_mode": "review_only",
                "action_reason": "Watchlist setup is near pivot but not independently actionable.",
                "raw_context_json": json.dumps({"technical_state": "READY", "watch_reasons": ["near pivot"]}),
                "load_ts": asof_date,
            },
        ]
    )
    monkeypatch.setattr(
        action_recommender,
        "load_latest_market_context",
        lambda *args, **kwargs: {
            "summary": {
                "regime_name": "RISK_OFF",
                "macro_risk_state": "HIGH",
                "breadth_trend_alignment_pct": 29.0,
                "risk_off_score": 0.78,
            },
            "top_universe": [{"symbol": "TCS", "rank_pct": 4.0, "sector_name": "IT"}],
        },
    )

    adjusted = action_recommender.apply_market_context_adjustments(rows, asof_date=asof_date)
    winners = action_recommender.rank_action_candidates(adjusted)
    with_contract = action_recommender.add_recommendation_reason_contracts(winners, adjusted)
    contract = json.loads(with_contract.iloc[0]["recommendation_reason_json"])
    manual_review = contract["evidence"]["manual_review"]
    macro = contract["evidence"]["macro_regime"]
    conflict = contract["evidence"]["conflict_resolution"]

    assert with_contract.iloc[0]["action_code"] == "MANUAL_REVIEW"
    assert with_contract.iloc[0]["action_source"] == "portfolio"
    assert with_contract.iloc[0]["source_action"] == "add_on_pullback"
    assert with_contract.iloc[0]["transaction_type"] is None
    assert with_contract.iloc[0]["execution_mode"] == "review_only"
    assert with_contract.iloc[0]["approved_allocation_inr"] == 0.0
    assert with_contract.iloc[0]["reason_contract_status"] == "complete"
    assert contract["original_action_code"] == "BUY_MORE"
    assert set(contract["evidence_sections_present"]) >= {"manual_review", "macro_regime", "conflict_resolution"}
    assert manual_review["manual_review_boundary"] == "market_context_positive_action_review_required"
    assert manual_review["manual_review_effect"] == "review_only_no_broker_execution"
    assert manual_review["blocked_original_action_code"] == "BUY_MORE"
    assert manual_review["manual_review_action_source"] == "portfolio"
    assert manual_review["broker_execution_allowed"] is False
    assert macro["market_context_adjustment"] == "positive_action_blocked_by_market_context"
    assert macro["risk_off_score"] == 0.78
    assert conflict["same_symbol_conflict_count"] == 1
    assert conflict["winning_action_code"] == "MANUAL_REVIEW"
    assert conflict["winning_action_source"] == "portfolio"
    assert conflict["losing_candidates"][0]["action_code"] == "WATCH"
    assert conflict["losing_candidates"][0]["action_source"] == "watchlist"
    assert conflict["losing_candidates"][0]["source_action"] == "READY"


def test_action_recommender_reduces_positive_action_size_in_cautious_market(monkeypatch):
    asof_date = pd.Timestamp("2026-05-01T00:00:00Z")
    rows = pd.DataFrame(
        [
            {
                "asof_date": asof_date,
                "published_on": asof_date,
                "symbol": "TCS",
                "setup_id": "SETUP",
                "action_code": "BUY",
                "action_priority": 50,
                "action_source": "portfolio",
                "transaction_type": "BUY",
                "execution_mode": "broker_order",
                "approved_allocation_inr": 100000.0,
                "action_fraction": 1.0,
                "invest_score_pct": 80.0,
                "stop_price": 100.0,
                "action_reason": "Portfolio approved breakout entry.",
                "raw_context_json": "{}",
            }
        ]
    )
    monkeypatch.setattr(
        action_recommender,
        "load_latest_market_context",
        lambda *args, **kwargs: {
            "summary": {
                "regime_name": "STABLE",
                "macro_risk_state": "NORMAL",
                "breadth_trend_alignment_pct": 46.0,
                "risk_off_score": 0.50,
                "macro_sizing_multiplier": 0.60,
            },
            "top_universe": [],
        },
    )

    out = action_recommender.apply_market_context_adjustments(rows, asof_date=asof_date)
    context = json.loads(out.iloc[0]["raw_context_json"])

    assert out.iloc[0]["action_code"] == "BUY"
    assert out.iloc[0]["transaction_type"] == "BUY"
    assert out.iloc[0]["approved_allocation_inr"] == 60000.0
    assert out.iloc[0]["action_fraction"] == 0.6
    assert out.iloc[0]["invest_score_pct"] == 48.0
    assert context["market_context_adjustment"] == "positive_action_size_reduced_by_market_context"


def test_action_recommender_cautious_buy_more_beats_watch_with_sizing_contract(monkeypatch):
    monkeypatch.setattr(action_recommender, "load_enabled_conflict_rule_ids", lambda: set(action_recommender.DEFAULT_CONFLICT_RULE_IDS))
    monkeypatch.setattr(action_recommender, "load_enabled_dynamic_conflict_rules_for_ranking", lambda: [])
    asof_date = pd.Timestamp("2026-05-01T00:00:00Z")
    rows = pd.DataFrame(
        [
            {
                "asof_date": asof_date,
                "published_on": pd.Timestamp("2026-05-01T10:00:00Z"),
                "symbol": "TCS",
                "setup_id": "ADD_ON",
                "unique_id": "BUY-MORE-1",
                "action_code": "BUY_MORE",
                "action_priority": 60,
                "action_source": "portfolio",
                "source_action": "add_on_pullback",
                "transaction_type": "BUY",
                "execution_mode": "broker_order",
                "approved_allocation_inr": 80000.0,
                "action_fraction": 0.4,
                "invest_score_pct": 75.0,
                "stop_price": 118.0,
                "action_reason": "Portfolio model approves an add-on pullback entry.",
                "raw_context_json": json.dumps(
                    {
                        "technical_state": "ADD_ON_PULLBACK",
                        "source_screener_slug": "portfolio-add-ons",
                        "position_status": "open",
                        "suggested_action": "add_on_pullback",
                        "lifecycle_reason": "Open position remains valid and pullback is constructive.",
                    }
                ),
                "load_ts": asof_date,
            },
            {
                "asof_date": asof_date,
                "published_on": pd.Timestamp("2026-05-01T10:20:00Z"),
                "symbol": "TCS",
                "setup_id": "WATCH",
                "unique_id": "WATCH-1",
                "action_code": "WATCH",
                "action_priority": 10,
                "action_source": "watchlist",
                "source_action": "READY",
                "transaction_type": None,
                "execution_mode": "review_only",
                "action_reason": "Watchlist setup is near pivot but not independently actionable.",
                "raw_context_json": json.dumps({"technical_state": "READY", "watch_reasons": ["near pivot"]}),
                "load_ts": asof_date,
            },
        ]
    )
    monkeypatch.setattr(
        action_recommender,
        "load_latest_market_context",
        lambda *args, **kwargs: {
            "summary": {
                "regime_name": "STABLE",
                "macro_risk_state": "NORMAL",
                "breadth_trend_alignment_pct": 46.0,
                "risk_off_score": 0.50,
                "macro_sizing_multiplier": 0.60,
            },
            "top_universe": [{"symbol": "TCS", "rank_pct": 6.0, "sector_name": "IT"}],
        },
    )

    adjusted = action_recommender.apply_market_context_adjustments(rows, asof_date=asof_date)
    winners = action_recommender.rank_action_candidates(adjusted)
    with_contract = action_recommender.add_recommendation_reason_contracts(winners, adjusted)
    contract = json.loads(with_contract.iloc[0]["recommendation_reason_json"])
    macro = contract["evidence"]["macro_regime"]
    conflict = contract["evidence"]["conflict_resolution"]

    assert with_contract.iloc[0]["action_code"] == "BUY_MORE"
    assert with_contract.iloc[0]["action_source"] == "portfolio"
    assert with_contract.iloc[0]["source_action"] == "add_on_pullback"
    assert with_contract.iloc[0]["transaction_type"] == "BUY"
    assert with_contract.iloc[0]["execution_mode"] == "broker_order"
    assert with_contract.iloc[0]["approved_allocation_inr"] == 48000.0
    assert with_contract.iloc[0]["action_fraction"] == 0.24
    assert with_contract.iloc[0]["invest_score_pct"] == 45.0
    assert with_contract.iloc[0]["reason_contract_status"] == "complete"
    assert contract["original_action_code"] == "BUY_MORE"
    assert set(contract["evidence_sections_present"]) >= {
        "screener",
        "technical",
        "risk",
        "lifecycle",
        "macro_regime",
        "conflict_resolution",
    }
    assert macro["market_context_adjustment"] == "positive_action_size_reduced_by_market_context"
    assert macro["market_context_adjustment_reason"].startswith("Positive broker action size was reduced")
    assert macro["size_multiplier"] == 0.60
    assert macro["macro_sizing_multiplier"] == 0.60
    assert macro["risk_off_score"] == 0.50
    assert macro["top_context_rank_pct"] == 6.0
    assert macro["top_context_sector"] == "IT"
    assert conflict["same_symbol_conflict_count"] == 1
    assert conflict["winning_action_code"] == "BUY_MORE"
    assert conflict["winning_action_source"] == "portfolio"
    assert conflict["losing_candidates"][0]["action_code"] == "WATCH"
    assert conflict["losing_candidates"][0]["action_source"] == "watchlist"
    assert conflict["losing_candidates"][0]["source_action"] == "READY"
    assert "conflict-rule precedence" in conflict["source_precedence_reason"]


def test_action_recommender_event_policy_manual_review_beats_market_adjusted_buy_more_watch(monkeypatch):
    monkeypatch.setattr(action_recommender, "load_enabled_conflict_rule_ids", lambda: set(action_recommender.DEFAULT_CONFLICT_RULE_IDS))
    monkeypatch.setattr(action_recommender, "load_enabled_dynamic_conflict_rules_for_ranking", lambda: [])
    asof_date = pd.Timestamp("2026-05-01T00:00:00Z")
    rows = pd.DataFrame(
        [
            {
                "asof_date": asof_date,
                "published_on": pd.Timestamp("2026-05-01T10:30:00Z"),
                "symbol": "TCS",
                "setup_id": "EVENT",
                "unique_id": "EVENT-REVIEW-1",
                "action_code": "MANUAL_REVIEW",
                "action_priority": 80,
                "action_source": "event_policy",
                "source_action": "BUY_WATCH",
                "transaction_type": None,
                "execution_mode": "review_only",
                "action_reason": "Large order win needs operator review before increasing exposure.",
                "action_detail": "Check order size, margin profile, execution period, and price reaction.",
                "raw_context_json": json.dumps(
                    {
                        "event_class": "ORDER_WIN",
                        "verdict": "manual_review",
                        "state_transition_hint": "strengthens",
                        "score_impact": 0.28,
                        "review_action": "clear",
                    }
                ),
                "load_ts": asof_date,
            },
            {
                "asof_date": asof_date,
                "published_on": pd.Timestamp("2026-05-01T10:00:00Z"),
                "symbol": "TCS",
                "setup_id": "ADD_ON",
                "unique_id": "BUY-MORE-1",
                "action_code": "BUY_MORE",
                "action_priority": 60,
                "action_source": "portfolio",
                "source_action": "add_on_pullback",
                "transaction_type": "BUY",
                "execution_mode": "broker_order",
                "approved_allocation_inr": 80000.0,
                "action_fraction": 0.4,
                "invest_score_pct": 75.0,
                "stop_price": 118.0,
                "action_reason": "Portfolio model approves an add-on pullback entry.",
                "raw_context_json": json.dumps(
                    {
                        "technical_state": "ADD_ON_PULLBACK",
                        "source_screener_slug": "portfolio-add-ons",
                        "position_status": "open",
                        "suggested_action": "add_on_pullback",
                        "lifecycle_reason": "Open position remains valid and pullback is constructive.",
                    }
                ),
                "load_ts": asof_date,
            },
            {
                "asof_date": asof_date,
                "published_on": pd.Timestamp("2026-05-01T10:20:00Z"),
                "symbol": "TCS",
                "setup_id": "WATCH",
                "unique_id": "WATCH-1",
                "action_code": "WATCH",
                "action_priority": 10,
                "action_source": "watchlist",
                "source_action": "READY",
                "transaction_type": None,
                "execution_mode": "review_only",
                "action_reason": "Watchlist setup is near pivot but not independently actionable.",
                "raw_context_json": json.dumps({"technical_state": "READY", "watch_reasons": ["near pivot"]}),
                "load_ts": asof_date,
            },
        ]
    )
    monkeypatch.setattr(
        action_recommender,
        "load_latest_market_context",
        lambda *args, **kwargs: {
            "summary": {
                "regime_name": "STABLE",
                "macro_risk_state": "NORMAL",
                "breadth_trend_alignment_pct": 46.0,
                "risk_off_score": 0.50,
                "macro_sizing_multiplier": 0.60,
            },
            "top_universe": [{"symbol": "TCS", "rank_pct": 6.0, "sector_name": "IT"}],
        },
    )

    adjusted = action_recommender.apply_market_context_adjustments(rows, asof_date=asof_date)
    winners = action_recommender.rank_action_candidates(adjusted)
    with_contract = action_recommender.add_recommendation_reason_contracts(winners, adjusted)
    contract = json.loads(with_contract.iloc[0]["recommendation_reason_json"])
    event_evidence = contract["evidence"]["event"]
    manual_review = contract["evidence"]["manual_review"]
    conflict = contract["evidence"]["conflict_resolution"]
    losing_by_source = {item["action_source"]: item for item in conflict["losing_candidates"]}

    assert with_contract.iloc[0]["action_code"] == "MANUAL_REVIEW"
    assert with_contract.iloc[0]["action_source"] == "event_policy"
    assert with_contract.iloc[0]["transaction_type"] is None
    assert with_contract.iloc[0]["execution_mode"] == "review_only"
    assert with_contract.iloc[0]["reason_contract_status"] == "complete"
    assert set(contract["evidence_sections_present"]) >= {"event", "manual_review", "conflict_resolution"}
    assert event_evidence["event_class"] == "ORDER_WIN"
    assert manual_review["manual_review_boundary"] == "event_policy_review_required"
    assert manual_review["manual_review_effect"] == "review_only_no_broker_execution"
    assert manual_review["broker_execution_allowed"] is False
    assert conflict["same_symbol_conflict_count"] == 2
    assert conflict["winning_action_code"] == "MANUAL_REVIEW"
    assert conflict["winning_action_source"] == "event_policy"
    assert losing_by_source["portfolio"]["action_code"] == "BUY_MORE"
    assert losing_by_source["portfolio"]["source_action"] == "add_on_pullback"
    assert losing_by_source["portfolio"]["market_context_adjustment"] == "positive_action_size_reduced_by_market_context"
    assert losing_by_source["portfolio"]["original_action_code"] == "BUY_MORE"
    assert losing_by_source["portfolio"]["size_multiplier"] == 0.60
    assert losing_by_source["watchlist"]["action_code"] == "WATCH"
    assert losing_by_source["watchlist"]["source_action"] == "READY"


def test_action_recommender_adversarial_veto_beats_market_gated_buy_more_watch(monkeypatch):
    monkeypatch.setattr(action_recommender, "load_enabled_conflict_rule_ids", lambda: set(action_recommender.DEFAULT_CONFLICT_RULE_IDS))
    monkeypatch.setattr(action_recommender, "load_enabled_dynamic_conflict_rules_for_ranking", lambda: [])
    asof_date = pd.Timestamp("2026-05-01T00:00:00Z")
    rows = pd.DataFrame(
        [
            {
                "asof_date": asof_date,
                "published_on": pd.Timestamp("2026-05-01T10:30:00Z"),
                "symbol": "TCS",
                "setup_id": "EVENT",
                "unique_id": "EVT-VETO",
                "action_code": "MANUAL_REVIEW",
                "action_priority": 5,
                "action_source": "event_policy",
                "source_action": "BUY_WATCH",
                "transaction_type": None,
                "execution_mode": "review_only",
                "action_reason": "Adversarial review vetoed the otherwise positive event.",
                "action_detail": "Operator must resolve contradictory evidence before any add-on.",
                "raw_context_json": json.dumps(
                    {
                        "event_class": "ORDER_WIN",
                        "verdict": "continue",
                        "state_transition_hint": "strengthens",
                        "score_impact": -0.5,
                        "review_action": "veto",
                        "veto": True,
                        "review_reason": "Order details conflict with prior disclosure.",
                        "action_status": "blocked_by_adversarial_review",
                    }
                ),
                "load_ts": asof_date,
            },
            {
                "asof_date": asof_date,
                "published_on": pd.Timestamp("2026-05-01T10:00:00Z"),
                "symbol": "TCS",
                "setup_id": "ADD_ON",
                "unique_id": "BUY-MORE-1",
                "action_code": "BUY_MORE",
                "action_priority": 60,
                "action_source": "portfolio",
                "source_action": "add_on_pullback",
                "transaction_type": "BUY",
                "execution_mode": "broker_order",
                "approved_allocation_inr": 80000.0,
                "action_fraction": 0.4,
                "invest_score_pct": 75.0,
                "stop_price": 118.0,
                "action_reason": "Portfolio model approves an add-on pullback entry.",
                "raw_context_json": json.dumps(
                    {
                        "technical_state": "ADD_ON_PULLBACK",
                        "source_screener_slug": "portfolio-add-ons",
                        "position_status": "open",
                        "suggested_action": "add_on_pullback",
                        "lifecycle_reason": "Open position remains valid and pullback is constructive.",
                    }
                ),
                "load_ts": asof_date,
            },
            {
                "asof_date": asof_date,
                "published_on": pd.Timestamp("2026-05-01T10:20:00Z"),
                "symbol": "TCS",
                "setup_id": "WATCH",
                "unique_id": "WATCH-1",
                "action_code": "WATCH",
                "action_priority": 10,
                "action_source": "watchlist",
                "source_action": "READY",
                "transaction_type": None,
                "execution_mode": "review_only",
                "action_reason": "Watchlist setup is near pivot but not independently actionable.",
                "raw_context_json": json.dumps({"technical_state": "READY", "watch_reasons": ["near pivot"]}),
                "load_ts": asof_date,
            },
        ]
    )
    monkeypatch.setattr(
        action_recommender,
        "load_latest_market_context",
        lambda *args, **kwargs: {
            "summary": {
                "regime_name": "RISK_OFF",
                "macro_risk_state": "HIGH",
                "breadth_trend_alignment_pct": 29.0,
                "risk_off_score": 0.78,
            },
            "top_universe": [{"symbol": "TCS", "rank_pct": 4.0, "sector_name": "IT"}],
        },
    )

    adjusted = action_recommender.apply_market_context_adjustments(rows, asof_date=asof_date)
    winners = action_recommender.rank_action_candidates(adjusted)
    with_contract = action_recommender.add_recommendation_reason_contracts(winners, adjusted)
    contract = json.loads(with_contract.iloc[0]["recommendation_reason_json"])
    event_evidence = contract["evidence"]["event"]
    manual_review = contract["evidence"]["manual_review"]
    conflict = contract["evidence"]["conflict_resolution"]
    losing_by_source = {item["action_source"]: item for item in conflict["losing_candidates"]}

    assert with_contract.iloc[0]["action_code"] == "MANUAL_REVIEW"
    assert with_contract.iloc[0]["action_source"] == "event_policy"
    assert with_contract.iloc[0]["transaction_type"] is None
    assert with_contract.iloc[0]["execution_mode"] == "review_only"
    assert with_contract.iloc[0]["reason_contract_status"] == "complete"
    assert set(contract["evidence_sections_present"]) >= {"event", "manual_review", "conflict_resolution"}
    assert event_evidence["review_action"] == "veto"
    assert event_evidence["veto"] is True
    assert event_evidence["action_status"] == "blocked_by_adversarial_review"
    assert manual_review["manual_review_boundary"] == "event_policy_review_required"
    assert manual_review["manual_review_effect"] == "review_only_no_broker_execution"
    assert manual_review["broker_execution_allowed"] is False
    assert conflict["conflict_precedence_rule_id"] == "ADVERSARIAL_VETO_MANUAL_BEATS_POSITIVE_OR_WATCH"
    assert conflict["same_symbol_conflict_count"] == 2
    assert conflict["winning_action_code"] == "MANUAL_REVIEW"
    assert conflict["winning_action_source"] == "event_policy"
    assert losing_by_source["portfolio"]["action_code"] == "MANUAL_REVIEW"
    assert losing_by_source["portfolio"]["source_action"] == "add_on_pullback"
    assert losing_by_source["portfolio"]["market_context_adjustment"] == "positive_action_blocked_by_market_context"
    assert losing_by_source["portfolio"]["original_action_code"] == "BUY_MORE"
    assert losing_by_source["portfolio"]["size_multiplier"] == 0.0
    assert losing_by_source["watchlist"]["action_code"] == "WATCH"
    assert losing_by_source["watchlist"]["source_action"] == "READY"


def test_action_recommender_rebalance_sell_beats_market_gated_buy_more_watch(monkeypatch):
    monkeypatch.setattr(action_recommender, "load_enabled_conflict_rule_ids", lambda: set(action_recommender.DEFAULT_CONFLICT_RULE_IDS))
    monkeypatch.setattr(action_recommender, "load_enabled_dynamic_conflict_rules_for_ranking", lambda: [])
    asof_date = pd.Timestamp("2026-05-01T00:00:00Z")
    rows = pd.DataFrame(
        [
            {
                "asof_date": asof_date,
                "published_on": pd.Timestamp("2026-05-01T10:10:00Z"),
                "symbol": "TCS",
                "setup_id": "REBALANCE",
                "unique_id": "SELL-1",
                "action_code": "SELL",
                "action_priority": 100,
                "action_source": "rebalance",
                "source_action": "full_exit",
                "transaction_type": "SELL",
                "execution_mode": "broker_order",
                "reference_price": 124.0,
                "stop_price": 118.0,
                "action_reason": "Lifecycle engine requires a full exit after stop invalidation.",
                "action_detail": "Exit trigger: close below invalidation and sector risk has deteriorated.",
                "raw_context_json": json.dumps(
                    {
                        "technical_state": "FULL_EXIT",
                        "position_status": "open",
                        "suggested_action": "full_exit",
                        "lifecycle_reason": "Close below invalidation triggered full exit.",
                        "next_action_reason": "Sell-side risk action takes precedence over add-on entries.",
                    }
                ),
                "load_ts": asof_date,
            },
            {
                "asof_date": asof_date,
                "published_on": pd.Timestamp("2026-05-01T10:30:00Z"),
                "symbol": "TCS",
                "setup_id": "ADD_ON",
                "unique_id": "BUY-MORE-1",
                "action_code": "BUY_MORE",
                "action_priority": 60,
                "action_source": "portfolio",
                "source_action": "add_on_pullback",
                "transaction_type": "BUY",
                "execution_mode": "broker_order",
                "approved_allocation_inr": 80000.0,
                "action_fraction": 0.4,
                "invest_score_pct": 75.0,
                "stop_price": 118.0,
                "action_reason": "Portfolio model approves an add-on pullback entry.",
                "raw_context_json": json.dumps(
                    {
                        "technical_state": "ADD_ON_PULLBACK",
                        "source_screener_slug": "portfolio-add-ons",
                        "position_status": "open",
                        "suggested_action": "add_on_pullback",
                        "lifecycle_reason": "Open position remains valid and pullback is constructive.",
                    }
                ),
                "load_ts": asof_date,
            },
            {
                "asof_date": asof_date,
                "published_on": pd.Timestamp("2026-05-01T10:20:00Z"),
                "symbol": "TCS",
                "setup_id": "WATCH",
                "unique_id": "WATCH-1",
                "action_code": "WATCH",
                "action_priority": 10,
                "action_source": "watchlist",
                "source_action": "READY",
                "transaction_type": None,
                "execution_mode": "review_only",
                "action_reason": "Watchlist setup is near pivot but not independently actionable.",
                "raw_context_json": json.dumps({"technical_state": "READY", "watch_reasons": ["near pivot"]}),
                "load_ts": asof_date,
            },
        ]
    )
    monkeypatch.setattr(
        action_recommender,
        "load_latest_market_context",
        lambda *args, **kwargs: {
            "summary": {
                "regime_name": "RISK_OFF",
                "macro_risk_state": "HIGH",
                "breadth_trend_alignment_pct": 29.0,
                "risk_off_score": 0.78,
            },
            "top_universe": [{"symbol": "TCS", "rank_pct": 4.0, "sector_name": "IT"}],
        },
    )

    adjusted = action_recommender.apply_market_context_adjustments(rows, asof_date=asof_date)
    winners = action_recommender.rank_action_candidates(adjusted)
    with_contract = action_recommender.add_recommendation_reason_contracts(winners, adjusted)
    contract = json.loads(with_contract.iloc[0]["recommendation_reason_json"])
    conflict = contract["evidence"]["conflict_resolution"]
    losing_by_source = {item["action_source"]: item for item in conflict["losing_candidates"]}

    assert with_contract.iloc[0]["action_code"] == "SELL"
    assert with_contract.iloc[0]["action_source"] == "rebalance"
    assert with_contract.iloc[0]["source_action"] == "full_exit"
    assert with_contract.iloc[0]["transaction_type"] == "SELL"
    assert with_contract.iloc[0]["execution_mode"] == "broker_order"
    assert with_contract.iloc[0]["reason_contract_status"] == "complete"
    assert set(contract["evidence_sections_present"]) >= {"technical", "risk", "lifecycle", "conflict_resolution"}
    assert conflict["conflict_precedence_rule_id"] == "EXIT_BEATS_ENTRY_OR_WATCH"
    assert conflict["winning_action_code"] == "SELL"
    assert conflict["winning_action_source"] == "rebalance"
    assert conflict["same_symbol_conflict_count"] == 2
    assert losing_by_source["portfolio"]["action_code"] == "MANUAL_REVIEW"
    assert losing_by_source["portfolio"]["source_action"] == "add_on_pullback"
    assert losing_by_source["portfolio"]["market_context_adjustment"] == "positive_action_blocked_by_market_context"
    assert losing_by_source["portfolio"]["original_action_code"] == "BUY_MORE"
    assert losing_by_source["portfolio"]["size_multiplier"] == 0.0
    assert losing_by_source["watchlist"]["action_code"] == "WATCH"
    assert losing_by_source["watchlist"]["source_action"] == "READY"


def test_action_recommender_does_not_upgrade_action_in_risk_on_market(monkeypatch):
    asof_date = pd.Timestamp("2026-05-01T00:00:00Z")
    rows = pd.DataFrame(
        [
            {
                "asof_date": asof_date,
                "published_on": asof_date,
                "symbol": "TCS",
                "setup_id": "SETUP",
                "action_code": "WATCH",
                "action_priority": 10,
                "action_source": "watchlist",
                "transaction_type": None,
                "execution_mode": "review_only",
                "action_reason": "Setup is near pivot but not triggered.",
                "raw_context_json": "{}",
            }
        ]
    )
    monkeypatch.setattr(
        action_recommender,
        "load_latest_market_context",
        lambda *args, **kwargs: {
            "summary": {
                "regime_name": "RISK_ON",
                "macro_risk_state": "LOW",
                "breadth_trend_alignment_pct": 72.0,
                "risk_off_score": 0.15,
            },
            "top_universe": [],
        },
    )

    out = action_recommender.apply_market_context_adjustments(rows, asof_date=asof_date)

    assert out.iloc[0]["action_code"] == "WATCH"
    assert json.loads(out.iloc[0]["raw_context_json"]) == {}


def test_action_recommender_enriches_event_and_playbook_context(monkeypatch):
    asof_date = pd.Timestamp("2026-05-01T00:00:00Z")
    rows = pd.DataFrame(
        [
            {
                "asof_date": asof_date,
                "published_on": asof_date,
                "symbol": "TCS",
                "setup_id": "PLAYBOOK_A",
                "unique_id": "EVT-1",
                "action_code": "MANUAL_REVIEW",
                "action_priority": 80,
                "action_source": "playbook_symbol",
                "source_action": "REDUCE_EXPOSURE_REVIEW",
                "execution_mode": "review_only",
                "action_reason": "Playbook matched event.",
                "raw_context_json": "{}",
            }
        ]
    )
    monkeypatch.setattr(action_recommender, "_load_latest_candidate_context", lambda asof_date, symbols: ({}, {}))
    monkeypatch.setattr(action_recommender, "_load_regime_context", lambda asof_date: {})
    monkeypatch.setattr(
        action_recommender,
        "_load_latest_event_context",
        lambda asof_date, symbols: (
            {
                ("PLAYBOOK_A", "TCS"): {
                    "event_unique_id": "EVT-1",
                    "event_class": "ORDER_WIN",
                    "verdict": "continue",
                    "what_happened": "Company won a large order.",
                    "review_action": "clear",
                    "veto": False,
                }
            },
            {},
        ),
    )
    monkeypatch.setattr(
        action_recommender,
        "_load_latest_playbook_context",
        lambda asof_date, symbols: (
            {
                "TCS": {
                    "playbook_id": "PLAYBOOK_A",
                    "playbook_source_key": "EVT-1",
                    "action_type": "REDUCE_EXPOSURE_REVIEW",
                    "checks_json": '[{"check_type":"contradiction"}]',
                    "operator_summary": "Review order-win playbook.",
                }
            },
            {"EVT-1": {"playbook_id": "PLAYBOOK_A", "playbook_source_key": "EVT-1"}},
        ),
    )

    enriched = action_recommender.enrich_action_candidate_context(rows, asof_date=asof_date)
    enriched = action_recommender.add_recommendation_reason_contracts(enriched, enriched)
    context = json.loads(enriched.iloc[0]["raw_context_json"])
    contract = json.loads(enriched.iloc[0]["recommendation_reason_json"])

    assert context["event_class"] == "ORDER_WIN"
    assert context["review_action"] == "clear"
    assert context["playbook_id"] == "PLAYBOOK_A"
    assert enriched.iloc[0]["reason_contract_status"] == "complete"
    assert set(contract["evidence_sections_present"]) >= {"event", "playbook"}


def test_action_recommender_reason_contract_downgrades_missing_buy_reason():
    winners = pd.DataFrame(
        [
            {
                "asof_date": pd.Timestamp("2026-05-01T00:00:00Z"),
                "published_on": pd.Timestamp("2026-05-01T09:30:00Z"),
                "symbol": "TCS",
                "setup_id": "SETUP",
                "action_code": "BUY",
                "action_priority": 50,
                "action_source": "portfolio",
                "transaction_type": "BUY",
                "execution_mode": "broker_order",
                "raw_context_json": "{}",
            }
        ]
    )

    out = action_recommender.add_recommendation_reason_contracts(winners, winners)
    contract = json.loads(out.iloc[0]["recommendation_reason_json"])

    assert out.iloc[0]["action_code"] == "MANUAL_REVIEW"
    assert out.iloc[0]["transaction_type"] is None
    assert out.iloc[0]["execution_mode"] == "review_only"
    assert out.iloc[0]["reason_contract_status"] == "incomplete_downgraded"
    assert "action_reason" in str(contract["missing_fields"])


def test_action_recommender_incomplete_contract_manual_review_boundary_is_explicit():
    winners = pd.DataFrame(
        [
            {
                "asof_date": pd.Timestamp("2026-05-01T00:00:00Z"),
                "published_on": pd.Timestamp("2026-05-01T09:30:00Z"),
                "symbol": "TCS",
                "setup_id": "SETUP",
                "action_code": "BUY",
                "action_priority": 50,
                "action_source": "portfolio",
                "transaction_type": "BUY",
                "execution_mode": "broker_order",
                "raw_context_json": "{}",
            }
        ]
    )

    out = action_recommender.add_recommendation_reason_contracts(winners, winners)
    context = json.loads(out.iloc[0]["raw_context_json"])
    contract = json.loads(out.iloc[0]["recommendation_reason_json"])
    manual_review = contract["evidence"]["manual_review"]

    assert out.iloc[0]["action_code"] == "MANUAL_REVIEW"
    assert out.iloc[0]["execution_mode"] == "review_only"
    assert out.iloc[0]["transaction_type"] is None
    assert out.iloc[0]["reason_contract_status"] == "incomplete_downgraded"
    assert contract["original_action_code"] == "BUY"
    assert "manual_review" in contract["evidence_sections_present"]
    assert manual_review["manual_review_boundary"] == "incomplete_reason_contract"
    assert manual_review["manual_review_effect"] == "review_only_no_broker_execution"
    assert manual_review["blocked_original_action_code"] == "BUY"
    assert manual_review["broker_execution_allowed"] is False
    assert "action_reason" in manual_review["blocked_reason_contract_missing_fields"]
    assert "buy_risk_level" in manual_review["blocked_reason_contract_missing_fields"]
    assert context["broker_execution_allowed"] is False


def test_action_recommender_rebalance_manual_review_contract_is_explicit():
    asof_date = pd.Timestamp("2026-05-01T00:00:00Z")
    cases = [
        (
            "review_manual",
            "lifecycle_rebalance_manual_review_required",
            "Missing entry/current price prevents automated lifecycle management.",
        ),
        (
            "review_stale",
            "lifecycle_rebalance_stale_review_required",
            "Holding period exceeded stale threshold without gains.",
        ),
        (
            "review_horizon",
            "lifecycle_rebalance_horizon_review_required",
            "Time horizon reached and needs operator review.",
        ),
        (
            "review_target",
            "lifecycle_rebalance_target_review_required",
            "Target is near and needs operator review before changing exposure.",
        ),
    ]
    winners = pd.DataFrame(
        [
            {
                "asof_date": asof_date,
                "published_on": pd.Timestamp(f"2026-05-01T09:3{idx}:00Z"),
                "symbol": f"TCS{idx}",
                "setup_id": "LIFE",
                "unique_id": f"POS-{idx}",
                "action_code": "MANUAL_REVIEW",
                "action_priority": 80,
                "action_source": "rebalance",
                "source_action": source_action,
                "transaction_type": None,
                "execution_mode": "review_only",
                "reference_price": 124.0,
                "stop_price": 112.0,
                "recommended_target_price": 130.0,
                "action_reason": reason,
                "action_detail": "Review whether to hold, trim, or raise target.",
                "raw_context_json": json.dumps(
                    {
                        "position_status": "open",
                        "suggested_action": source_action,
                        "lifecycle_reason": reason,
                        "next_action_reason": "Target review required before any order intent.",
                    }
                ),
            }
            for idx, (source_action, _boundary, reason) in enumerate(cases)
        ]
    )

    out = action_recommender.add_recommendation_reason_contracts(winners, winners)
    for idx, (source_action, expected_boundary, reason) in enumerate(cases):
        row = out.iloc[idx]
        contract = json.loads(row["recommendation_reason_json"])
        manual_review = contract["evidence"]["manual_review"]

        assert row["action_code"] == "MANUAL_REVIEW"
        assert row["execution_mode"] == "review_only"
        assert row["transaction_type"] is None
        assert row["reason_contract_status"] == "complete"
        assert set(contract["evidence_sections_present"]) >= {"manual_review", "lifecycle", "risk"}
        assert manual_review["manual_review_boundary"] == expected_boundary
        assert manual_review["manual_review_effect"] == "review_only_no_broker_execution"
        assert manual_review["manual_review_action_source"] == "rebalance"
        assert manual_review["manual_review_source_action"] == source_action
        assert manual_review["broker_execution_allowed"] is False
        assert manual_review["review_reason"] == reason


def test_action_recommender_event_and_playbook_manual_review_contract_boundaries_are_explicit():
    asof_date = pd.Timestamp("2026-05-01T00:00:00Z")
    winners = pd.DataFrame(
        [
            {
                "asof_date": asof_date,
                "published_on": pd.Timestamp("2026-05-01T09:30:00Z"),
                "symbol": "TCS",
                "setup_id": "EVENT",
                "unique_id": "EVT-1",
                "action_code": "MANUAL_REVIEW",
                "action_priority": 80,
                "action_source": "event_policy",
                "source_action": "BUY_WATCH",
                "transaction_type": None,
                "execution_mode": "review_only",
                "action_reason": "Material event needs operator confirmation before changing exposure.",
                "action_detail": "Check order size, margin impact, and price reaction.",
                "raw_context_json": json.dumps(
                    {
                        "event_class": "ORDER_WIN",
                        "verdict": "manual_review",
                        "state_transition_hint": "strengthens",
                        "score_impact": 0.22,
                        "review_action": "clear",
                    }
                ),
            },
            {
                "asof_date": asof_date,
                "published_on": pd.Timestamp("2026-05-01T09:45:00Z"),
                "symbol": "INFY",
                "setup_id": "PLAYBOOK_A",
                "unique_id": "NEWS-1",
                "action_code": "MANUAL_REVIEW",
                "action_priority": 80,
                "action_source": "playbook_symbol",
                "source_action": "REDUCE_EXPOSURE_REVIEW",
                "transaction_type": None,
                "execution_mode": "review_only",
                "action_reason": "Trusted playbook requires exposure review after management commentary.",
                "action_detail": "Review event impact before changing exposure.",
                "raw_context_json": json.dumps(
                    {
                        "playbook_id": "PLAYBOOK_A",
                        "playbook_source_key": "NEWS-1",
                        "action_type": "REDUCE_EXPOSURE_REVIEW",
                        "operator_summary": "Review event impact before changing exposure.",
                        "decision_reason": "Management commentary matched a risk playbook.",
                        "checks": [{"check_type": "contradiction", "blocking": True}],
                    }
                ),
            },
        ]
    )

    out = action_recommender.add_recommendation_reason_contracts(winners, winners)
    contracts = {
        row["symbol"]: json.loads(row["recommendation_reason_json"])
        for _, row in out.iterrows()
    }

    assert set(out["reason_contract_status"]) == {"complete"}
    event_manual = contracts["TCS"]["evidence"]["manual_review"]
    playbook_manual = contracts["INFY"]["evidence"]["manual_review"]
    assert event_manual["manual_review_boundary"] == "event_policy_review_required"
    assert event_manual["manual_review_effect"] == "review_only_no_broker_execution"
    assert event_manual["manual_review_action_source"] == "event_policy"
    assert event_manual["manual_review_source_action"] == "BUY_WATCH"
    assert event_manual["broker_execution_allowed"] is False
    assert playbook_manual["manual_review_boundary"] == "playbook_review_required"
    assert playbook_manual["manual_review_effect"] == "review_only_no_broker_execution"
    assert playbook_manual["manual_review_action_source"] == "playbook_symbol"
    assert playbook_manual["manual_review_source_action"] == "REDUCE_EXPOSURE_REVIEW"
    assert playbook_manual["broker_execution_allowed"] is False
    assert set(contracts["TCS"]["evidence_sections_present"]) >= {"event", "manual_review"}
    assert set(contracts["INFY"]["evidence_sections_present"]) >= {"playbook", "manual_review"}


def test_action_recommender_generic_manual_review_contract_beats_portfolio_buy(monkeypatch):
    monkeypatch.setattr(action_recommender, "load_enabled_conflict_rule_ids", lambda: set(action_recommender.DEFAULT_CONFLICT_RULE_IDS))
    monkeypatch.setattr(action_recommender, "load_enabled_dynamic_conflict_rules_for_ranking", lambda: [])
    asof_date = pd.Timestamp("2026-05-01T00:00:00Z")
    candidates = pd.DataFrame(
        [
            {
                "asof_date": asof_date,
                "published_on": pd.Timestamp("2026-05-01T10:00:00Z"),
                "symbol": "TCS",
                "setup_id": "RISK_REVIEW",
                "unique_id": "RISK-1",
                "action_code": "MANUAL_REVIEW",
                "action_priority": 80,
                "action_source": "risk_review",
                "source_action": "exposure_review",
                "transaction_type": None,
                "execution_mode": "review_only",
                "action_reason": "Risk overlay requires operator review before adding exposure.",
                "action_detail": "Confirm concentration and liquidity before changing the portfolio.",
                "raw_context_json": json.dumps(
                    {
                        "operator_question": "Should this exposure be allowed despite concentration and liquidity warnings?",
                        "review_reason": "Risk overlay found portfolio concentration and liquidity warnings.",
                        "risk_flags": ["concentration", "liquidity"],
                    }
                ),
                "load_ts": asof_date,
            },
            {
                "asof_date": asof_date,
                "published_on": pd.Timestamp("2026-05-01T10:10:00Z"),
                "symbol": "TCS",
                "setup_id": "PORT",
                "unique_id": "BUY-1",
                "action_code": "BUY",
                "action_priority": 50,
                "action_source": "portfolio",
                "source_action": "approved",
                "transaction_type": "BUY",
                "execution_mode": "broker_order",
                "stop_price": 118.0,
                "action_reason": "Portfolio model approves the breakout entry.",
                "raw_context_json": json.dumps({"technical_state": "BUY_TRIGGERED", "source_screener_slug": "breakouts"}),
                "load_ts": asof_date,
            },
        ]
    )

    winners = action_recommender.rank_action_candidates(candidates)
    with_contract = action_recommender.add_recommendation_reason_contracts(winners, candidates)
    contract = json.loads(with_contract.iloc[0]["recommendation_reason_json"])
    manual_review = contract["evidence"]["manual_review"]
    conflict = contract["evidence"]["conflict_resolution"]

    assert with_contract.iloc[0]["action_code"] == "MANUAL_REVIEW"
    assert with_contract.iloc[0]["action_source"] == "risk_review"
    assert with_contract.iloc[0]["execution_mode"] == "review_only"
    assert with_contract.iloc[0]["transaction_type"] is None
    assert with_contract.iloc[0]["reason_contract_status"] == "complete"
    assert set(contract["evidence_sections_present"]) >= {"manual_review", "conflict_resolution"}
    assert manual_review["manual_review_boundary"] == "action_consolidation_manual_review"
    assert manual_review["manual_review_effect"] == "review_only_no_broker_execution"
    assert manual_review["manual_review_action_source"] == "risk_review"
    assert manual_review["manual_review_source_action"] == "exposure_review"
    assert manual_review["broker_execution_allowed"] is False
    assert manual_review["operator_question"].startswith("Should this exposure be allowed")
    assert conflict["same_symbol_conflict_count"] == 1
    assert conflict["winning_action_code"] == "MANUAL_REVIEW"
    assert conflict["winning_action_source"] == "risk_review"
    assert conflict["losing_candidates"][0]["action_code"] == "BUY"
    assert conflict["losing_candidates"][0]["action_source"] == "portfolio"


def test_action_recommender_adds_manual_revision_pointers_without_llm():
    winners = pd.DataFrame(
        [
            {
                "asof_date": pd.Timestamp("2026-05-01T00:00:00Z"),
                "published_on": pd.Timestamp("2026-05-01T09:30:00Z"),
                "symbol": "TCS",
                "setup_id": "SETUP",
                "action_code": "BUY",
                "action_priority": 50,
                "action_source": "portfolio",
                "action_reason": "Breakout confirmed.",
                "action_detail": "Strong technical setup.",
            }
        ]
    )
    candidates = pd.DataFrame(
        [
            *winners.to_dict(orient="records"),
            {
                "asof_date": pd.Timestamp("2026-05-01T00:00:00Z"),
                "published_on": pd.Timestamp("2026-05-01T09:31:00Z"),
                "symbol": "TCS",
                "setup_id": "WATCH",
                "action_code": "WATCH",
                "action_priority": 10,
                "action_source": "watchlist",
                "action_reason": "Near pivot.",
            },
        ]
    )

    out = action_recommender.add_manual_revision_pointers(winners, candidates, use_llm=False)
    payload = json.loads(out.iloc[0]["manual_revision_pointers_json"])

    assert "manual_revision_summary" in out.columns
    assert out.iloc[0]["manual_revision_status"] == "disabled"
    assert "TCS" in out.iloc[0]["manual_revision_summary"]
    assert payload["manual_checks"]
    assert any("WATCH" in item for item in payload["risk_flags"])


def test_action_recommender_bridges_matched_manual_review_wait_signal(monkeypatch):
    matched_at = pd.Timestamp("2026-06-02T09:00:00Z")
    monkeypatch.setattr(
        action_recommender,
        "load_matched_manual_review_wait_signals",
        lambda **_kwargs: pd.DataFrame(
            [
                {
                    "matched_at": matched_at,
                    "signal_id": "sig-manual",
                    "hypothesis_id": "manual_review",
                    "match_symbol": "ABC",
                    "signal_type": "clarification_filing",
                    "expected_action": "MANUAL_REVIEW",
                    "match_status": "matched",
                    "match_score": 0.82,
                    "match_source_table": "advisory_watch_events",
                    "match_source_key": "ANN-1",
                    "observed_at": pd.Timestamp("2026-06-02T08:30:00Z"),
                    "match_reason": "Matched clarification filing required wait term: order cancellation.",
                    "evidence_json": json.dumps(
                        {
                            "wait_signal": {
                                "manual_review_item_id": "action_manual_review:source:key",
                                "manual_review_source_table": "advisory_manual_review_decisions",
                                "manual_review_source_key": "action_manual_review:source:key",
                                "wait_question": "Has management clarified the order cancellation?",
                            }
                        }
                    ),
                    "signal_source_table": "advisory_manual_review_decisions",
                    "signal_source_key": "action_manual_review:source:key",
                    "signal_symbol": "ABC",
                    "signal_expected_action": "MANUAL_REVIEW",
                    "operator_summary": "Wait for management clarification.",
                    "wait_question": "Has management clarified the order cancellation?",
                    "condition_json": json.dumps({"condition_type": "clarification_filing", "keywords": ["order cancellation"]}),
                    "generated_by": "manual_review_decision",
                }
            ]
        ),
    )

    rows = action_recommender.build_matched_wait_signal_action_candidates(
        asof_date=pd.Timestamp("2026-06-02T00:00:00Z"),
        symbols=["ABC"],
    )

    assert len(rows) == 1
    row = rows[0]
    assert row["action_code"] == "MANUAL_REVIEW"
    assert row["action_source"] == "manual_review_wait_signal"
    assert row["execution_mode"] == "review_only"
    assert row["transaction_type"] is None
    assert row["setup_id"] == action_recommender.MANUAL_REVIEW_WAIT_SIGNAL_SETUP_ID
    assert row["published_on"] == matched_at
    assert row["invest_score_pct"] == 82.0
    context = json.loads(row["raw_context_json"])
    followup = context["wait_signal_followup"]
    assert followup["manual_review_item_id"] == "action_manual_review:source:key"
    assert followup["match_source_key"] == "ANN-1"
    assert "do not create broker-executable trades" in followup["bridge_note"]

    with_contract = action_recommender.add_recommendation_reason_contracts(pd.DataFrame(rows), pd.DataFrame(rows))
    contract = json.loads(with_contract.iloc[0]["recommendation_reason_json"])
    assert with_contract.iloc[0]["action_code"] == "MANUAL_REVIEW"
    assert with_contract.iloc[0]["execution_mode"] == "review_only"
    assert with_contract.iloc[0]["reason_contract_status"] == "complete"
    assert "wait_signal" in contract["evidence_sections_present"]
    assert contract["evidence"]["wait_signal"]["wait_signal_followup"]["manual_review_item_id"] == "action_manual_review:source:key"


def test_action_recommender_manual_revision_pointers_use_codex(monkeypatch):
    class DummyPointers:
        def model_dump(self):
            return {
                "revision_summary": "Review TCS buy before execution.",
                "key_reasons": ["Breakout confirmed"],
                "manual_checks": ["Check latest price"],
                "risk_flags": ["Could be stale"],
                "missing_data": [],
                "operator_questions": ["Is liquidity still fine?"],
            }

    monkeypatch.setattr(action_recommender, "run_codex_structured", lambda *args, **kwargs: DummyPointers())
    row = pd.Series(
        {
            "asof_date": pd.Timestamp("2026-05-01T00:00:00Z"),
            "published_on": pd.Timestamp("2026-05-01T09:30:00Z"),
            "symbol": "TCS",
            "action_code": "BUY",
            "action_source": "portfolio",
            "action_reason": "Breakout confirmed.",
        }
    )

    pointers, model, status = action_recommender.build_manual_revision_pointers(row, pd.DataFrame([row.to_dict()]), use_llm=True)

    assert status == "ok"
    assert model == action_recommender.ACTION_MANUAL_REVISION_POINTERS_MODEL
    assert pointers["revision_summary"] == "Review TCS buy before execution."


def test_action_recommender_manual_revision_pointers_persist_prompt_metadata(monkeypatch):
    monkeypatch.setattr(
        action_recommender,
        "build_manual_revision_pointers",
        lambda row, candidates, **kwargs: ({"revision_summary": "Review before action."}, "codex", "ok"),
    )
    rows = pd.DataFrame(
        [
            {
                "asof_date": pd.Timestamp("2026-05-01T00:00:00Z"),
                "published_on": pd.Timestamp("2026-05-01T09:30:00Z"),
                "symbol": "TCS",
                "action_code": "BUY",
                "action_source": "portfolio",
                "action_reason": "Breakout confirmed.",
                "action_priority": 50,
            }
        ]
    )

    out = action_recommender.add_manual_revision_pointers(rows, rows, use_llm=True)

    assert out.iloc[0]["manual_revision_prompt_id"] == "manual_revision_pointers"
    assert out.iloc[0]["manual_revision_prompt_version"] == action_recommender.MANUAL_REVISION_PROMPT_VERSION
    assert out.iloc[0]["manual_revision_prompt_schema_version"] == action_recommender.MANUAL_REVISION_PROMPT_SCHEMA_VERSION


def test_event_policy_maps_order_win_to_buy_watch_overlay():
    events = pd.DataFrame(
        [
            {
                "published_on": pd.Timestamp("2026-05-01T09:30:00Z"),
                "asof_date": pd.Timestamp("2026-05-01T00:00:00Z"),
                "setup_id": "EVENT_OPPORTUNITY_V1",
                "setup_name": "Event Opportunity",
                "symbol": "ABC",
                "company_master_id": "nse:ABC",
                "unique_id": "ABC-1",
                "event_source": "announcement",
                "subject": "Large export orders",
                "event_class": "ORDER_WIN",
                "verdict": "continue",
                "setup_effect": "strengthens",
                "materiality": "medium",
                "governance_risk": "none",
                "balance_sheet_risk": "none",
                "execution_risk": "medium",
                "score_impact": 0.22,
                "confidence": 0.75,
                "what_happened": "The company received export orders worth INR 1,076 crore.",
                "rationale": "Order improves revenue visibility.",
                "review_action": "clear",
                "veto": False,
            }
        ]
    )

    out, meta = event_policy.build_event_policy_actions(events)

    assert meta["action_counts"]["BUY_WATCH"] == 1
    assert out.iloc[0]["policy_class"] == "ORDER_WIN"
    assert out.iloc[0]["action_type"] == "BUY_WATCH"
    assert "technical" in out.iloc[0]["checks_json"]


def test_event_policy_maps_regulatory_notice_to_reduce_exposure_review():
    row = pd.Series(
        {
            "published_on": pd.Timestamp("2026-05-01T09:30:00Z"),
            "asof_date": pd.Timestamp("2026-05-01T00:00:00Z"),
            "setup_id": "EVENT_OPPORTUNITY_V1",
            "symbol": "ABC",
            "unique_id": "ABC-2",
            "event_class": "REGULATORY_NOTICE",
            "verdict": "review_manual",
            "setup_effect": "weakens",
            "materiality": "high",
            "governance_risk": "high",
            "balance_sheet_risk": "none",
            "execution_risk": "medium",
            "score_impact": -0.5,
            "confidence": 0.8,
            "what_happened": "Company received a regulatory show-cause notice.",
            "rationale": "Regulatory action may impair valuation.",
            "review_action": "review_manual",
            "veto": False,
        }
    )

    policy = event_policy.build_policy_for_event(row)

    assert policy["policy_class"] == "REGULATORY_NOTICE"
    assert policy["action_type"] == "REDUCE_EXPOSURE_REVIEW"
    assert policy["policy_score"] < 0


def test_event_policy_llm_downgrades_non_actionable_manual_review(monkeypatch):
    class DummyReview:
        def model_dump(self):
            return {
                "final_action_type": "NO_ACTION",
                "confidence": 0.8,
                "operator_summary": "Routine analyst meet is not actionable.",
                "possible_action": "Ignore unless management gives concrete guidance.",
                "wait_for_events": ["Guidance upgrade", "Material order win"],
                "operator_questions": ["Was new guidance disclosed?"],
                "downgrade_reason": "No actionable follow-up likely from this routine event.",
                "rationale": "The event is procedural and does not change thesis.",
            }

    monkeypatch.setattr(event_policy, "run_codex_structured", lambda *args, **kwargs: DummyReview())
    row = event_policy.build_policy_for_event(
        pd.Series(
            {
                "published_on": pd.Timestamp("2026-05-01T09:30:00Z"),
                "asof_date": pd.Timestamp("2026-05-01T00:00:00Z"),
                "setup_id": "EVENT_OPPORTUNITY_V1",
                "symbol": "ABC",
                "unique_id": "ABC-3",
                "event_class": "OTHER",
                "verdict": "review_manual",
                "setup_effect": "neutral",
                "materiality": "medium",
                "score_impact": -0.09,
                "confidence": 0.4,
                "what_happened": "Analyst meet update without new guidance.",
                "rationale": "Routine meeting notice.",
            }
        )
    )

    out = event_policy.apply_llm_manual_review(row, use_llm=True)
    notes = json.loads(out["operator_notes_json"])

    assert out["action_type"] == "NO_ACTION"
    assert out["action_status"] == "llm_downgraded_no_action"
    assert out["llm_review_status"] == "ok"
    assert out["llm_prompt_id"] == "event_policy_manual_review"
    assert out["llm_prompt_version"] == event_policy.PROMPT_VERSION
    assert out["llm_prompt_schema_version"] == event_policy.PROMPT_SCHEMA_VERSION
    assert notes["wait_for_events"] == ["Guidance upgrade", "Material order win"]


def test_event_policy_llm_adds_questions_to_manual_review(monkeypatch):
    class DummyReview:
        def model_dump(self):
            return {
                "final_action_type": "MANUAL_REVIEW",
                "confidence": 0.65,
                "operator_summary": "Dilution terms require review.",
                "possible_action": "Review use of proceeds before changing exposure.",
                "wait_for_events": ["Issue price disclosure", "Promoter participation"],
                "operator_questions": ["Is dilution above 5%?", "Is pricing at a discount?"],
                "downgrade_reason": None,
                "rationale": "Capital raise can be positive or negative depending on terms.",
            }

    monkeypatch.setattr(event_policy, "run_codex_structured", lambda *args, **kwargs: DummyReview())
    events = pd.DataFrame(
        [
            {
                "published_on": pd.Timestamp("2026-05-01T09:30:00Z"),
                "asof_date": pd.Timestamp("2026-05-01T00:00:00Z"),
                "setup_id": "EVENT_OPPORTUNITY_V1",
                "symbol": "ABC",
                "unique_id": "ABC-4",
                "event_class": "DILUTION",
                "verdict": "review_manual",
                "setup_effect": "neutral",
                "materiality": "high",
                "score_impact": -0.1,
                "confidence": 0.7,
                "what_happened": "Company announced preferential issue.",
                "rationale": "Terms are not yet clear.",
            }
        ]
    )

    out, meta = event_policy.build_event_policy_actions(events, use_llm=True, llm_max_rows=1)
    notes = json.loads(out.iloc[0]["operator_notes_json"])

    assert out.iloc[0]["action_type"] == "MANUAL_REVIEW"
    assert out.iloc[0]["llm_review_status"] == "ok"
    assert out.iloc[0]["llm_prompt_id"] == "event_policy_manual_review"
    assert "Issue price disclosure" in notes["wait_for_events"]
    assert meta["llm_manual_review_rows"] == 1


def test_event_policy_evaluator_builds_rows_and_summary():
    evaluated_at = pd.Timestamp("2026-05-30T00:00:00Z")
    dataset = pd.DataFrame(
        [
            {
                "published_on": pd.Timestamp("2026-05-01T09:00:00Z"),
                "asof_date": pd.Timestamp("2026-05-01T00:00:00Z"),
                "setup_id": "SETUP",
                "symbol": "ABC",
                "unique_id": "E1",
                "event_source": "announcement",
                "event_class": "ORDER_WIN",
                "policy_class": "ORDER_WIN",
                "action_type": "BUY_WATCH",
                "action_status": "watch",
                "policy_score": 0.80,
                "confidence": 0.90,
                "score_bucket": "score_high",
                "confidence_bucket": "confidence_high",
                "entry_date_h5": pd.Timestamp("2026-05-02T00:00:00Z"),
                "exit_date_h5": pd.Timestamp("2026-05-08T00:00:00Z"),
                "entry_close_h5": 100.0,
                "exit_close_h5": 106.0,
                "forward_return_h5": 0.06,
                "raw_context_json": "{}",
            },
            {
                "published_on": pd.Timestamp("2026-05-01T09:00:00Z"),
                "asof_date": pd.Timestamp("2026-05-01T00:00:00Z"),
                "setup_id": "SETUP",
                "symbol": "XYZ",
                "unique_id": "E2",
                "event_source": "announcement",
                "event_class": "ORDER_WIN",
                "policy_class": "ORDER_WIN",
                "action_type": "BUY_WATCH",
                "action_status": "watch",
                "policy_score": 0.70,
                "confidence": 0.75,
                "score_bucket": "score_medium",
                "confidence_bucket": "confidence_medium",
                "entry_date_h5": pd.Timestamp("2026-05-02T00:00:00Z"),
                "exit_date_h5": pd.Timestamp("2026-05-08T00:00:00Z"),
                "entry_close_h5": 100.0,
                "exit_close_h5": 98.0,
                "forward_return_h5": -0.02,
                "raw_context_json": "{}",
            },
        ]
    )

    rows = event_policy_evaluator.build_evaluation_rows(
        dataset,
        horizons=[5],
        cost_bps=25,
        return_threshold=0.03,
        evaluated_at=evaluated_at,
    )
    summary = event_policy_evaluator.summarize_evaluations(rows, evaluated_at=evaluated_at, min_matured_rows=1)

    assert len(rows) == 2
    assert round(float(rows.iloc[0]["forward_return_after_cost"]), 4) == 0.0575
    assert bool(rows.iloc[0]["hit_after_cost"]) is True
    policy_summary = summary[(summary["group_type"] == "policy_class") & (summary["group_value"] == "ORDER_WIN")].iloc[0]
    assert policy_summary["matured_count"] == 2
    assert round(float(policy_summary["avg_forward_return_after_cost"]), 4) == 0.0175
    assert float(policy_summary["hit_rate_after_cost"]) == 0.5
    assert policy_summary["recommendation"] == "candidate_policy_strengthen"


def test_event_policy_evaluator_normalizes_persist_dtypes():
    frame = pd.DataFrame(
        [
            {
                "evaluated_at": "2026-05-30T00:00:00Z",
                "horizon_days": "5",
                "published_on": "2026-05-01T09:00:00Z",
                "asof_date": "2026-05-01",
                "entry_date": "2026-05-02",
                "exit_date": "2026-05-08",
                "symbol": "ABC",
                "entry_close": "100.5",
                "exit_close": "105.0",
                "forward_return": "0.044776",
                "forward_return_after_cost": "0.042276",
                "hit_after_cost": "true",
                "matured": "true",
                "policy_score": "0.8",
                "confidence": "0.9",
            }
        ]
    )

    out = event_policy_evaluator.normalize_evaluation_frame(frame)

    assert pd.api.types.is_float_dtype(out["entry_close"])
    assert pd.api.types.is_float_dtype(out["forward_return_after_cost"])
    assert pd.api.types.is_integer_dtype(out["horizon_days"])
    assert str(out["hit_after_cost"].dtype) == "boolean"
    assert str(out["entry_date"].dtype) == "datetime64[ns, UTC]"


def test_event_policy_evaluator_loads_policy_rows_and_attaches_returns(monkeypatch):
    policies = pd.DataFrame(
        [
            {
                "published_on": pd.Timestamp("2026-05-01T09:00:00Z"),
                "asof_date": pd.Timestamp("2026-05-01T00:00:00Z"),
                "setup_id": "SETUP",
                "symbol": "ABC",
                "unique_id": "E1",
                "event_source": "announcement",
                "event_class": "ORDER_WIN",
                "policy_class": "ORDER_WIN",
                "action_type": "BUY_WATCH",
                "action_status": "watch",
                "policy_score": 0.80,
                "confidence": 0.90,
                "score_bucket": "score_high",
                "confidence_bucket": "confidence_high",
                "raw_context_json": "{}",
            }
        ]
    )
    prices = pd.DataFrame(
        [
            {"symbol": "ABC", "date": pd.Timestamp("2026-05-02T00:00:00Z"), "close": 100.0},
            {"symbol": "ABC", "date": pd.Timestamp("2026-05-04T00:00:00Z"), "close": 103.0},
        ]
    )
    monkeypatch.setattr(event_policy_evaluator, "load_event_policy_rows", lambda **kwargs: policies.copy())
    monkeypatch.setattr(event_policy_evaluator, "load_price_history_for_returns", lambda **kwargs: prices.copy())

    evaluations, summary, meta = event_policy_evaluator.evaluate_event_policies(horizons=[2], cost_bps=0, min_matured_rows=1)

    assert len(evaluations) == 1
    assert round(float(evaluations.iloc[0]["forward_return"]), 4) == 0.03
    assert meta["matured_rows_by_horizon"]["2"] == 1
    action_summary = summary[(summary["group_type"] == "action_type") & (summary["group_value"] == "BUY_WATCH")].iloc[0]
    assert action_summary["matured_count"] == 1


def test_action_recommender_bridges_event_policy_actions(monkeypatch):
    asof_date = pd.Timestamp("2026-05-01T00:00:00Z")
    policies = pd.DataFrame(
        [
            {
                "published_on": pd.Timestamp("2026-05-01T09:30:00Z"),
                "asof_date": asof_date,
                "setup_id": "EVENT_OPPORTUNITY_V1",
                "symbol": "ABC",
                "unique_id": "ABC-1",
                "action_type": "BUY_WATCH",
                "action_reason": "ORDER_WIN is material and strengthens setup.",
                "action_detail": "Received a large order.",
                "confidence": 0.7,
                "checks_json": "[]",
                "raw_context_json": "{}",
            }
        ]
    )
    monkeypatch.setattr(action_recommender, "load_portfolio_actions", lambda **kwargs: pd.DataFrame())
    monkeypatch.setattr(action_recommender, "load_lifecycle_actions", lambda **kwargs: pd.DataFrame())
    monkeypatch.setattr(action_recommender, "load_rebalance_actions", lambda **kwargs: pd.DataFrame())
    monkeypatch.setattr(action_recommender, "load_watch_actions", lambda **kwargs: pd.DataFrame())
    monkeypatch.setattr(action_recommender, "load_event_policy_actions", lambda **kwargs: policies)
    monkeypatch.setattr(action_recommender, "load_playbook_action_plans", lambda **kwargs: pd.DataFrame())
    monkeypatch.setattr(action_recommender, "enrich_action_candidate_context", lambda df, asof_date: df)
    monkeypatch.setattr(action_recommender, "add_manual_revision_pointers", lambda df, all_candidates, **kwargs: df)

    out = action_recommender.build_action_recommendations(asof_date=asof_date)

    assert out.iloc[0]["symbol"] == "ABC"
    assert out.iloc[0]["action_source"] == "event_policy"
    assert out.iloc[0]["action_code"] == "MANUAL_REVIEW"


def test_hypothesis_engine_builds_matches_and_decision():
    hypotheses = pd.DataFrame(
        [
            {
                "hypothesis_id": "H1",
                "title": "Austerity risk",
                "status": "draft",
                "trigger_patterns_json": json.dumps({"keywords": ["austerity", "spending cuts", "prime minister"]}),
                "expected_effect_json": json.dumps({"effect": "go cash"}),
                "decision_policy_json": json.dumps({"min_terms": 2}),
            }
        ]
    )
    events = pd.DataFrame(
        [
            {
                "source_type": "news",
                "source_table": "advisory_news_events",
                "source_key": "N1",
                "published_on": pd.Timestamp("2026-05-14T10:00:00Z"),
                "symbol": "NIFTY",
                "subject": "Prime Minister calls for austerity",
                "concise_summary_text": "The speech mentioned spending cuts and fiscal discipline.",
                "source_url": "https://example.test/news",
            }
        ]
    )

    matches = hypothesis_engine.build_matches(hypotheses, events)

    assert len(matches) == 1
    row = matches.iloc[0]
    assert row["suggested_action"] == "REDUCE_EXPOSURE_REVIEW_TESTING"
    assert row["match_score"] > 0
    assert "austerity" in row["matched_terms_json"]


def test_hypothesis_engine_action_plans_persist_prompt_metadata(monkeypatch):
    monkeypatch.setattr(hypothesis_engine, "load_playbook_market_context", lambda match: {})
    matches = pd.DataFrame(
        [
            {
                "hypothesis_id": "H1",
                "hypothesis_title": "Austerity risk",
                "status": "testing",
                "source_type": "news",
                "source_table": "advisory_news_events",
                "source_key": "N1",
                "published_on": pd.Timestamp("2026-05-14T10:00:00Z"),
                "symbol": "NIFTY",
                "subject": "Prime Minister calls for austerity",
                "evidence_text": "Austerity and spending cuts were discussed.",
                "matched_terms_json": json.dumps(["austerity", "spending cuts"]),
                "match_score": 0.75,
                "expected_effect_json": json.dumps({"effect": "risk off"}),
                "suggested_action": "REDUCE_EXPOSURE_REVIEW_TESTING",
                "action_reason": "Risk-off hypothesis matched.",
            }
        ]
    )

    plans = hypothesis_engine.build_action_plans(matches, use_llm=False)

    assert plans.iloc[0]["prompt_id"] == "playbook_action_plan"
    assert plans.iloc[0]["prompt_version"] == hypothesis_engine.PLAYBOOK_ACTION_PROMPT_VERSION
    assert plans.iloc[0]["prompt_schema_version"] == hypothesis_engine.PLAYBOOK_ACTION_PROMPT_SCHEMA_VERSION


def test_hypothesis_engine_create_hypothesis_normalizes_payload(monkeypatch):
    captured = []

    monkeypatch.setattr(hypothesis_engine, "ensure_tables", lambda: None)
    monkeypatch.setattr(
        hypothesis_engine,
        "upsert_to_db",
        lambda df, table_name, unique_keys, timescaledb_column=None: captured.append((table_name, df.copy(), unique_keys)),
    )

    row = hypothesis_engine.create_hypothesis(
        {
            "title": "Liquidity shock",
            "trigger_patterns": "liquidity stress, redemption pressure",
            "expected_effect": {"effect": "reduce_exposure"},
        }
    )

    assert row["title"] == "Liquidity shock"
    assert captured[0][0] == hypothesis_engine.HYPOTHESES_TABLE
    stored = json.loads(captured[0][1].iloc[0]["trigger_patterns_json"])
    assert stored["keywords"] == ["liquidity stress", "redemption pressure"]

    captured.clear()
    hypothesis_engine.create_hypothesis(
        {
            "title": "Regulatory risk",
            "trigger_patterns": {
                "keywords": {
                    "include": ["show cause notice"],
                    "exclude": ["routine clarification"],
                }
            },
            "expected_effect": {"effect": "manual_review"},
        }
    )
    nested = json.loads(captured[0][1].iloc[0]["trigger_patterns_json"])
    assert nested["keywords"] == ["show cause notice"]
    assert nested["exclude_keywords"] == ["routine clarification"]


def test_hypothesis_engine_loads_yaml_config(tmp_path):
    config_path = tmp_path / "hypotheses.yaml"
    config_path.write_text(
        """
hypotheses:
  - hypothesis_id: MARKET_AUSTERITY_RISK_V1
    title: Top authority austerity risk
    description: Calls for austerity can reduce risk appetite.
    source: investor_interview
    status: validated
    trigger_scope: market
    trigger_patterns:
      keywords:
        include:
          - austerity
          - fiscal tightening
        exclude:
          - company cost cutting
      authority_roles:
        - finance minister
    expected_effect:
      effect: reduce_exposure
    holding_window:
      horizons_days: [1, 3, 20]
    decision_policy:
      min_terms: 1
    validation_protocol:
      method: point_in_time_forward_return
""",
        encoding="utf-8",
    )

    rows = hypothesis_engine.load_hypotheses_config(config_path)

    assert len(rows) == 1
    row = rows[0]
    assert row["hypothesis_id"] == "MARKET_AUSTERITY_RISK_V1"
    assert row["status"] == "active_review"
    assert row["holding_window_days"] == 20
    assert row["trigger_patterns"]["keywords"] == ["austerity", "fiscal tightening"]
    assert row["trigger_patterns"]["exclude_keywords"] == ["company cost cutting"]
    assert row["decision_policy"]["validation_protocol"]["method"] == "point_in_time_forward_return"


def test_hypothesis_engine_import_config_upserts(monkeypatch, tmp_path):
    config_path = tmp_path / "hypotheses.yaml"
    config_path.write_text(
        """
hypotheses:
  - hypothesis_id: PROMOTER_BUYING_CONFIDENCE_V1
    title: Promoter buying confidence
    trigger_patterns:
      keywords:
        include:
          - promoter bought
    expected_effect:
      effect: manual_review
""",
        encoding="utf-8",
    )
    captured = []

    monkeypatch.setattr(hypothesis_engine, "ensure_tables", lambda: None)
    monkeypatch.setattr(
        hypothesis_engine,
        "upsert_to_db",
        lambda df, table_name, unique_keys, timescaledb_column=None: captured.append((table_name, df.copy(), unique_keys)),
    )

    result = hypothesis_engine.import_hypotheses_config(config_path)

    assert result["imported_count"] == 1
    assert captured[0][0] == hypothesis_engine.HYPOTHESES_TABLE
    stored = captured[0][1].iloc[0]
    assert stored["hypothesis_id"] == "PROMOTER_BUYING_CONFIDENCE_V1"
    assert json.loads(stored["trigger_patterns_json"])["keywords"] == ["promoter bought"]


def test_hypothesis_engine_previews_payload_without_db_write():
    result = hypothesis_engine.preview_hypothesis_payload(
        {
            "title": "Regulatory shock",
            "status": "trusted_overlay",
            "trigger_patterns": {
                "keywords": {
                    "include": ["show cause notice"],
                    "exclude": ["routine clarification"],
                }
            },
            "expected_effect": {"effect": "manual_review"},
            "decision_policy": {"min_terms": 1},
        }
    )

    assert result["status"] == "ok"
    assert result["normalized_payload"]["trigger_patterns"]["keywords"] == ["show cause notice"]
    assert result["normalized_payload"]["trigger_patterns"]["exclude_keywords"] == ["routine clarification"]
    assert result["db_row_preview"]["status"] == "trusted_overlay"
    assert "review_only" in result["production_note"]


def test_hypothesis_engine_normalizes_paused_playbook_status():
    assert hypothesis_engine.normalize_playbook_status("paused") == "paused"
    assert hypothesis_engine.normalize_playbook_status("disabled") == "paused"
    assert hypothesis_engine.normalize_playbook_status("pause") == "paused"


def test_hypothesis_engine_update_preserves_id_and_created_at(monkeypatch):
    captured = []
    created_at = pd.Timestamp("2026-05-01T10:00:00Z")

    monkeypatch.setattr(hypothesis_engine, "ensure_tables", lambda: None)
    monkeypatch.setattr(
        hypothesis_engine,
        "sql_to_df",
        lambda *args, **kwargs: pd.DataFrame([{"created_at": created_at}]),
    )
    monkeypatch.setattr(
        hypothesis_engine,
        "upsert_to_db",
        lambda df, table_name, unique_keys, timescaledb_column=None: captured.append((table_name, df.copy(), unique_keys)),
    )

    row = hypothesis_engine.update_hypothesis(
        "REGULATORY_NOTICE_RISK_V1",
        {
            "title": "Regulatory notice risk",
            "status": "trusted_overlay",
            "trigger_patterns": {"keywords": {"include": ["show cause notice"]}},
            "expected_effect": {"effect": "manual_review"},
        },
    )

    assert row["hypothesis_id"] == "REGULATORY_NOTICE_RISK_V1"
    assert row["status"] == "trusted_overlay"
    assert pd.Timestamp(row["created_at"]) == created_at
    assert captured[-1][0] == hypothesis_engine.HYPOTHESES_TABLE
    assert captured[-1][1].iloc[0]["hypothesis_id"] == "REGULATORY_NOTICE_RISK_V1"


def test_hypothesis_engine_promotion_audit_blocks_without_evidence(monkeypatch):
    monkeypatch.setattr(hypothesis_engine, "ensure_tables", lambda: None)
    monkeypatch.setattr(hypothesis_engine, "sql_to_df", lambda *args, **kwargs: pd.DataFrame())

    row = hypothesis_engine.build_promotion_audit("MARKET_AUSTERITY_RISK_V1", min_matches=2)

    assert row["audit_status"] == "insufficient_history"
    assert row["match_count"] == 0
    assert "needs at least 2" in row["audit_reason"]


def test_hypothesis_engine_promotion_audit_approves_with_matches(monkeypatch):
    matches = pd.DataFrame(
        [
            {
                "hypothesis_id": "MARKET_AUSTERITY_RISK_V1",
                "source_type": "news",
                "published_on": pd.Timestamp("2026-05-01T10:00:00Z"),
                "symbol": "NIFTY",
                "source_key": "N1",
            },
            {
                "hypothesis_id": "MARKET_AUSTERITY_RISK_V1",
                "source_type": "announcement",
                "published_on": pd.Timestamp("2026-05-03T10:00:00Z"),
                "symbol": "RELIANCE",
                "source_key": "A1",
            },
        ]
    )
    prices = pd.DataFrame(
        [
            {"symbol": "NIFTY", "asset_type": "benchmark", "date": pd.Timestamp("2026-05-04T00:00:00Z"), "close": 100.0},
            {"symbol": "NIFTY", "asset_type": "benchmark", "date": pd.Timestamp("2026-05-05T00:00:00Z"), "close": 101.0},
            {"symbol": "NIFTY", "asset_type": "benchmark", "date": pd.Timestamp("2026-05-06T00:00:00Z"), "close": 102.0},
            {"symbol": "RELIANCE", "asset_type": "stock", "date": pd.Timestamp("2026-05-04T00:00:00Z"), "close": 200.0},
            {"symbol": "RELIANCE", "asset_type": "stock", "date": pd.Timestamp("2026-05-05T00:00:00Z"), "close": 210.0},
            {"symbol": "RELIANCE", "asset_type": "stock", "date": pd.Timestamp("2026-05-06T00:00:00Z"), "close": 220.0},
            {"symbol": "ONGC", "asset_type": "stock", "date": pd.Timestamp("2026-05-04T00:00:00Z"), "close": 100.0},
            {"symbol": "ONGC", "asset_type": "stock", "date": pd.Timestamp("2026-05-05T00:00:00Z"), "close": 102.0},
            {"symbol": "ONGC", "asset_type": "stock", "date": pd.Timestamp("2026-05-06T00:00:00Z"), "close": 103.0},
            {"symbol": "IOC", "asset_type": "stock", "date": pd.Timestamp("2026-05-04T00:00:00Z"), "close": 100.0},
            {"symbol": "IOC", "asset_type": "stock", "date": pd.Timestamp("2026-05-05T00:00:00Z"), "close": 101.0},
            {"symbol": "IOC", "asset_type": "stock", "date": pd.Timestamp("2026-05-06T00:00:00Z"), "close": 102.0},
        ]
    )
    sector_members = pd.DataFrame(
        [
            {"anchor_symbol": "RELIANCE", "member_symbol": "ONGC"},
            {"anchor_symbol": "RELIANCE", "member_symbol": "IOC"},
        ]
    )
    monkeypatch.setattr(hypothesis_engine, "ensure_tables", lambda: None)
    monkeypatch.setattr(hypothesis_engine, "_audit_horizons_for_hypothesis", lambda hypothesis_id: [1])

    def fake_sql_to_df(query, params=None):
        if "dhan_ohlcv_daily" in query:
            return prices
        if "master_sharpely_equity" in query:
            return sector_members
        return matches

    monkeypatch.setattr(hypothesis_engine, "sql_to_df", fake_sql_to_df)

    row = hypothesis_engine.build_promotion_audit("MARKET_AUSTERITY_RISK_V1", min_matches=2)
    evidence = json.loads(row["evidence_json"])

    assert row["audit_status"] == "sufficient_history"
    assert row["match_count"] == 2
    assert row["source_type_count"] == 2
    assert evidence["forward_return_status"] == "evaluated"
    assert evidence["forward_return_summary"][0]["evaluated_count"] == 2
    assert evidence["forward_return_summary"][0]["benchmark_evaluated_count"] == 2
    assert "mean_excess_return_vs_benchmark" in evidence["forward_return_summary"][0]
    assert "mean_excess_return_vs_sector_proxy" in evidence["forward_return_summary"][0]
    reliance_row = [item for item in evidence["forward_return_rows"] if item["symbol"] == "RELIANCE"][0]
    assert round(reliance_row["excess_return_vs_benchmark"], 6) == round(0.05 - 0.01, 6)
    assert round(reliance_row["excess_return_vs_sector_proxy"], 6) == round(0.05 - 0.015, 6)


def test_hypothesis_engine_trusted_overlay_does_not_require_statistical_audit(monkeypatch):
    monkeypatch.setattr(hypothesis_engine, "ensure_tables", lambda: None)
    monkeypatch.setattr(hypothesis_engine, "latest_promotion_audit", lambda hypothesis_id: None)
    captured = []
    monkeypatch.setattr(
        hypothesis_engine,
        "upsert_to_db",
        lambda df, table_name, unique_keys, timescaledb_column=None: captured.append((table_name, df.copy(), unique_keys)),
    )

    row = hypothesis_engine.create_hypothesis(
        {
            "hypothesis_id": "NO_AUDIT_V1",
            "title": "No audit",
            "status": "trusted_overlay",
        }
    )

    assert row["status"] == "trusted_overlay"
    assert captured[0][0] == hypothesis_engine.HYPOTHESES_TABLE


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

    result = codex_cli.run_codex_cli("Summarize this", model="gpt-test", images=[tmp_path / "page.png"])

    assert result == "codex result"
    cmd = calls[0][0]
    assert cmd[:2] == ["codex", "exec"]
    assert ["--model", "gpt-test"] == cmd[cmd.index("--model") : cmd.index("--model") + 2]
    assert ["--image", str(tmp_path / "page.png")] == cmd[cmd.index("--image") : cmd.index("--image") + 2]


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


def test_announcement_pipeline_summarizes_with_codex(monkeypatch):
    calls = []
    pipeline_obj = object.__new__(announcement_pipeline.AnnouncementPipeline)

    monkeypatch.setattr(
        announcement_pipeline,
        "run_codex_cli",
        lambda prompt, model: calls.append((prompt, model)) or "Concise summary",
    )
    monkeypatch.setattr(announcement_pipeline, "CODEX_CLI_SUMMARIZE_MODEL", "gpt-test")

    result = pipeline_obj._generate_text(prompt="Summarize", context="Document context", model="codex")

    assert result == "Concise summary"
    assert calls[0][1] == "gpt-test"
    assert "Document context" in calls[0][0]


def test_announcement_pipeline_structured_parse_uses_codex(monkeypatch):
    calls = []
    pipeline_obj = object.__new__(announcement_pipeline.AnnouncementPipeline)
    pipeline_obj.summarize_model = "codex:gpt-test"

    class DummyReport(announcement_pipeline.BaseModel):
        value: str

    ResponseModel = announcement_pipeline.create_model("DummyResponse", DummyReport=(DummyReport, ...))

    def fake_structured(prompt, *, response_model, model, system_prompt):
        calls.append((prompt, response_model, model, system_prompt))
        return response_model.model_validate({"DummyReport": {"value": "ok"}})

    monkeypatch.setattr(announcement_pipeline, "run_codex_structured", fake_structured)

    result = pipeline_obj._parse_structured_response(prompt="Document", response_model=ResponseModel)

    assert result.DummyReport.value == "ok"
    assert calls[0][2] == "gpt-test"
    assert "explicitly supported" in calls[0][3]


def test_managed_announcement_stage_serializes_dataclass_reports():
    report = ParsedReport(
        category="Financial Results",
        report_name="QuarterlyResult",
        model_name="codex",
        data={"revenue": 100},
    )

    payload = announcement_managed_pipeline.serialize_stage_payload([report])

    assert payload == [
        {
            "category": "Financial Results",
            "report_name": "QuarterlyResult",
            "model_name": "codex",
            "prompt_id": None,
            "prompt_version": None,
            "prompt_schema_version": None,
            "data": {"revenue": 100},
        }
    ]


def test_managed_announcement_stage_serializes_model_variants():
    class PydanticV1Style:
        def dict(self):
            return {"report_name": "V1Report", "value": 1}

    class PlainReport:
        def __init__(self):
            self.report_name = "PlainReport"
            self.value = {"nested": PydanticV1Style()}
            self._private = "ignore"

    payload = announcement_managed_pipeline.serialize_stage_payload([PydanticV1Style(), PlainReport()])

    assert payload == [
        {"report_name": "V1Report", "value": 1},
        {"report_name": "PlainReport", "value": {"nested": {"report_name": "V1Report", "value": 1}}},
    ]


def test_announcement_pipeline_ocr_uses_codex(monkeypatch):
    pipeline_obj = object.__new__(announcement_pipeline.AnnouncementPipeline)
    pipeline_obj.ocr_model = "codex:gpt-test"
    calls = []

    monkeypatch.setattr(
        announcement_pipeline,
        "ocr_pdf_with_codex",
        lambda path, pages, model: calls.append((path, pages, model)) or {1: "OCR text"},
    )

    result = pipeline_obj._ocr_pdf_bytes(b"%PDF-test", pages="1")

    assert result == {1: "OCR text"}
    assert calls[0][1] == "1"
    assert calls[0][2] == "gpt-test"


def test_ts_forecast_features_builds_experimental_horizon_rows():
    dates = pd.date_range("2026-01-01", periods=100, freq="D", tz="UTC")
    history = pd.DataFrame(
        {
            "symbol": ["ABC"] * len(dates),
            "date": dates,
            "open": [100 + idx for idx in range(len(dates))],
            "high": [101 + idx for idx in range(len(dates))],
            "low": [99 + idx for idx in range(len(dates))],
            "close": [100 + idx for idx in range(len(dates))],
            "volume": [100_000 + (idx * 100) for idx in range(len(dates))],
        }
    )

    df = ts_forecast_features.build_ts_forecasts(
        symbols=["ABC"],
        asof_date=pd.Timestamp("2026-04-10T00:00:00Z"),
        horizons=(5, 20),
        history=history,
    )

    assert len(df) == 2
    assert set(df["forecast_horizon_days"]) == {5, 20}
    assert set(df["action_hint"]).issubset(
        {
            "EXPERIMENTAL_POSITIVE",
            "EXPERIMENTAL_NEGATIVE",
            "EXPERIMENTAL_NEUTRAL",
            "EXPERIMENTAL_WEAK",
        }
    )
    assert (df["forecast_price"] > 0).all()
    assert df["feature_context_json"].str.contains("Experimental forecast feature only").all()


def test_ts_forecast_evaluator_scores_matured_rows_after_costs():
    forecasts = pd.DataFrame(
        [
            {
                "asof_date": pd.Timestamp("2026-01-01T00:00:00Z"),
                "symbol": "ABC",
                "model_name": "naive_momentum_v1",
                "forecast_horizon_days": 5,
                "action_hint": "EXPERIMENTAL_POSITIVE",
                "forecast_return": 0.05,
            },
            {
                "asof_date": pd.Timestamp("2026-01-08T00:00:00Z"),
                "symbol": "ABC",
                "model_name": "naive_momentum_v1",
                "forecast_horizon_days": 5,
                "action_hint": "EXPERIMENTAL_POSITIVE",
                "forecast_return": 0.02,
            },
        ]
    )
    prices = pd.DataFrame(
        {
            "symbol": ["ABC"] * 12,
            "date": pd.date_range("2026-01-01", periods=12, freq="D", tz="UTC"),
            "close": [100, 101, 102, 103, 104, 110, 111, 112, 113, 114, 115, 116],
        }
    )

    evaluations = ts_forecast_evaluator.build_forecast_evaluations(
        forecasts=forecasts,
        prices=prices,
        cost_bps=25,
    )
    summary = ts_forecast_evaluator.build_evaluation_summary(evaluations, cost_bps=25)

    evaluated = evaluations[evaluations["evaluation_status"].eq("evaluated")]
    not_matured = evaluations[evaluations["evaluation_status"].eq("not_matured")]
    assert len(evaluated) == 1
    assert len(not_matured) == 1
    row = evaluated.iloc[0]
    assert round(float(row["realized_return"]), 4) == 0.1
    assert round(float(row["cost_adjusted_return"]), 4) == 0.0975
    assert bool(row["direction_hit"]) is True
    assert len(summary) == 1
    assert int(summary.iloc[0]["row_count"]) == 1
    assert round(float(summary.iloc[0]["hit_rate"]), 4) == 1.0


def test_ts_forecast_evaluator_persist_casts_boolean_columns(monkeypatch):
    captured: list[tuple[str, pd.DataFrame]] = []
    evaluations = pd.DataFrame(
        [
            {
                "asof_date": pd.Timestamp("2026-01-01T00:00:00Z"),
                "symbol": "ABC",
                "model_name": "naive_momentum_v1",
                "forecast_horizon_days": 5,
                "action_hint": "EXPERIMENTAL_POSITIVE",
                "forecast_return": 0.05,
                "realized_return": 0.10,
                "cost_adjusted_return": 0.0975,
                "forecast_direction": 1,
                "realized_direction": 1,
                "direction_hit": True,
                "positive_realized": True,
                "absolute_error": 0.05,
                "squared_error": 0.0025,
                "future_date": pd.Timestamp("2026-01-06T00:00:00Z"),
                "entry_price": 100.0,
                "exit_price": 110.0,
                "evaluation_status": "evaluated",
                "evaluation_detail": "horizon=5; cost_bps=25.00",
                "load_ts": pd.Timestamp("2026-01-07T00:00:00Z"),
            },
            {
                "asof_date": pd.Timestamp("2026-01-08T00:00:00Z"),
                "symbol": "ABC",
                "model_name": "naive_momentum_v1",
                "forecast_horizon_days": 5,
                "action_hint": "EXPERIMENTAL_POSITIVE",
                "forecast_return": 0.02,
                "realized_return": None,
                "cost_adjusted_return": None,
                "forecast_direction": 1,
                "realized_direction": None,
                "direction_hit": None,
                "positive_realized": None,
                "absolute_error": None,
                "squared_error": None,
                "future_date": None,
                "entry_price": 111.0,
                "exit_price": None,
                "evaluation_status": "not_matured",
                "evaluation_detail": "horizon=5; cost_bps=25.00",
                "load_ts": pd.Timestamp("2026-01-08T00:00:00Z"),
            },
        ]
    )

    monkeypatch.setattr(ts_forecast_evaluator, "ensure_tables", lambda: None)
    monkeypatch.setattr(
        ts_forecast_evaluator,
        "upsert_to_db",
        lambda df, table_name, **kwargs: captured.append((table_name, df.copy())),
    )

    ts_forecast_evaluator.persist_evaluations(evaluations, pd.DataFrame())

    assert captured[0][0] == ts_forecast_evaluator.EVALUATIONS_TABLE
    persisted = captured[0][1]
    assert str(persisted["direction_hit"].dtype) == "boolean"
    assert str(persisted["positive_realized"].dtype) == "boolean"
    assert str(persisted["forecast_direction"].dtype) == "Int64"
    assert str(persisted["realized_direction"].dtype) == "Int64"


def test_ts_forecast_workflow_builds_watchlist_from_positive_forecasts():
    forecasts = pd.DataFrame(
        [
            {
                "asof_date": pd.Timestamp("2026-04-30T00:00:00Z"),
                "symbol": "ABC",
                "model_name": "timesfm_2p5_200m",
                "forecast_horizon_days": 10,
                "forecast_return": 0.08,
                "forecast_price": 108.0,
                "probability_positive": 0.70,
                "signal_quality": 0.55,
                "action_hint": "EXPERIMENTAL_POSITIVE",
            },
            {
                "asof_date": pd.Timestamp("2026-04-30T00:00:00Z"),
                "symbol": "XYZ",
                "model_name": "timesfm_2p5_200m",
                "forecast_horizon_days": 10,
                "forecast_return": 0.02,
                "forecast_price": 102.0,
                "probability_positive": 0.51,
                "signal_quality": 0.40,
                "action_hint": "EXPERIMENTAL_NEUTRAL",
            },
        ]
    )

    watchlist = ts_forecast_workflow.build_ts_watchlist(
        forecasts,
        asof_date=pd.Timestamp("2026-04-30T00:00:00Z"),
        source_name="unit_test",
        source_slug="unit-test",
    )

    assert len(watchlist) == 1
    assert watchlist.iloc[0]["symbol"] == "ABC"
    assert watchlist.iloc[0]["watch_status"] == "TS_WATCH"
    assert "Experimental TS forecast positive" in watchlist.iloc[0]["watch_reason"]


def test_ts_forecast_workflow_falls_back_when_screener_fails(monkeypatch):
    def fail_screener(**_kwargs):
        raise ValueError("Could not find Screener.in results table")

    forecasts = pd.DataFrame(
        [
            {
                "asof_date": pd.Timestamp("2026-04-30T00:00:00Z"),
                "symbol": "ABC",
                "model_name": "naive_momentum_v1",
                "forecast_horizon_days": 10,
                "forecast_return": 0.08,
                "forecast_price": 108.0,
                "probability_positive": 0.70,
                "signal_quality": 0.55,
                "action_hint": "EXPERIMENTAL_POSITIVE",
            }
        ]
    )

    monkeypatch.setattr(ts_forecast_workflow, "symbols_from_screener", fail_screener)
    monkeypatch.setattr(ts_forecast_workflow, "resolve_symbol_universe", lambda symbols: ["ABC", "XYZ"] if symbols is None else symbols)
    monkeypatch.setattr(ts_forecast_workflow, "build_ts_forecasts", lambda **_kwargs: forecasts.copy())

    result = ts_forecast_workflow.run_workflow(
        query_text="Market capitalization > 1000",
        query_name="Unit TS Screener",
        asof_date=pd.Timestamp("2026-04-30T00:00:00Z"),
        model_name="naive_momentum_v1",
        refresh_ohlcv=False,
        max_symbols=1,
        dry_run=True,
    )

    assert result["status"] == "ok"
    assert result["symbol_count"] == 1
    assert result["watch_rows"] == 1
    assert result["warnings"][0].startswith("screener_failed:ValueError")
    assert "query_name='Unit TS Screener'" in result["warnings"][0]
    assert "screener_url='https://www.screener.in/screen/raw/" in result["warnings"][0]
    assert result["screener"]["error"].startswith("screener_failed:ValueError")
    assert result["screener"]["screener_url"].startswith("https://www.screener.in/screen/raw/")


def test_live_dashboard_builds_experimental_ts_forecast_views():
    watch_df = pd.DataFrame(
        [
            {
                "asof_date": pd.Timestamp("2026-04-30T00:00:00Z"),
                "symbol": "ABC",
                "source_name": "TS Forecast Watch",
                "source_slug": "ts-watch",
                "model_name": "timesfm_2p5_200m",
                "forecast_horizon_days": 10,
                "forecast_return": 0.08,
                "probability_positive": 0.70,
                "signal_quality": 0.55,
                "action_hint": "EXPERIMENTAL_POSITIVE",
                "watch_status": "TS_WATCH",
                "watch_reason": "Experimental TS forecast positive.",
                "load_ts": pd.Timestamp("2026-04-30T10:00:00Z"),
            }
        ]
    )
    eval_df = pd.DataFrame(
        [
            {
                "evaluated_at": pd.Timestamp("2026-05-04T00:00:00Z"),
                "from_date": pd.Timestamp("2026-04-01T00:00:00Z"),
                "to_date": pd.Timestamp("2026-04-30T00:00:00Z"),
                "model_name": "timesfm_2p5_200m",
                "forecast_horizon_days": 10,
                "action_hint": "EXPERIMENTAL_POSITIVE",
                "row_count": 12,
                "hit_rate": 0.625,
                "positive_rate": 0.75,
                "avg_realized_return": 0.03,
                "avg_cost_adjusted_return": 0.0275,
                "median_cost_adjusted_return": 0.02,
                "sharpe_like": 1.2,
            }
        ]
    )

    views = live_dashboard.build_ts_forecast_views(
        watch_df=watch_df,
        forecast_df=pd.DataFrame(),
        eval_summary_df=eval_df,
    )

    assert len(views["watch"]) == 1
    assert views["watch"][0]["symbol"] == "ABC"
    assert views["watch"][0]["research_only"] is True
    assert views["watch"][0]["forecast_return_pct"] == 8.0
    assert views["watch"][0]["probability_positive_pct"] == 70.0
    assert len(views["recommendations"]) == 1
    assert views["recommendations"][0]["setup_id"] == "TS_WATCH"
    assert views["recommendations"][0]["status"] == "SWING_ONLY"
    assert "research recommendation" in views["recommendations"][0]["reason"].lower()
    assert len(views["evaluation_summary"]) == 1
    assert views["evaluation_summary"][0]["row_count"] == 12
    assert views["evaluation_summary"][0]["hit_rate_pct"] == 62.5


def test_live_dashboard_collapses_ts_horizons_into_windows():
    forecast_df = pd.DataFrame(
        [
            {
                "asof_date": pd.Timestamp("2026-05-04T00:00:00Z"),
                "symbol": "SHAKTIPUMP",
                "model_name": "naive_momentum_v1",
                "forecast_horizon_days": 10,
                "forecast_return": 0.04,
                "forecast_price": 584.0,
                "probability_positive": 0.66,
                "signal_quality": 0.76,
                "action_hint": "EXPERIMENTAL_POSITIVE",
            },
            {
                "asof_date": pd.Timestamp("2026-05-04T00:00:00Z"),
                "symbol": "SHAKTIPUMP",
                "model_name": "naive_momentum_v1",
                "forecast_horizon_days": 20,
                "forecast_return": 0.08,
                "forecast_price": 608.0,
                "probability_positive": 0.72,
                "signal_quality": 0.83,
                "action_hint": "EXPERIMENTAL_POSITIVE",
            },
            {
                "asof_date": pd.Timestamp("2026-05-03T00:00:00Z"),
                "symbol": "SHAKTIPUMP",
                "model_name": "naive_momentum_v1",
                "forecast_horizon_days": 10,
                "forecast_return": 0.02,
                "forecast_price": 570.0,
                "probability_positive": 0.61,
                "signal_quality": 0.70,
                "action_hint": "EXPERIMENTAL_POSITIVE",
            },
        ]
    )
    latest = forecast_df[forecast_df["asof_date"].eq(pd.Timestamp("2026-05-04T00:00:00Z"))].copy()

    views = live_dashboard.build_ts_forecast_views(
        watch_df=pd.DataFrame(),
        forecast_df=latest,
        eval_summary_df=pd.DataFrame(),
        history_df=forecast_df,
    )

    assert len(views["watch"]) == 1
    row = views["watch"][0]
    assert row["symbol"] == "SHAKTIPUMP"
    assert row["combined_state"] == "ALIGNED_POSITIVE"
    assert row["swing_window"]["state"] == "POSITIVE"
    assert row["position_window"]["state"] == "POSITIVE"
    assert "2026-05-04" in row["history_summary"]
    assert views["recommendations"][0]["status"] == "ALIGNED_POSITIVE"


def test_live_dashboard_parses_ps_elapsed_formats():
    assert live_dashboard._parse_ps_elapsed_seconds("125") == 125
    assert live_dashboard._parse_ps_elapsed_seconds("02:03") == 123
    assert live_dashboard._parse_ps_elapsed_seconds("01:02:03") == 3723
    assert live_dashboard._parse_ps_elapsed_seconds("2-01:02:03") == 176523


def test_live_dashboard_safe_loader_records_section_failures():
    live_dashboard.SECTION_FAILURES.clear()

    def broken_loader():
        raise ValueError("broken section")

    out = live_dashboard._safe_frame_loader(broken_loader)

    assert out.empty
    assert live_dashboard.SECTION_FAILURES[-1]["section"] == "broken_loader"
    assert "ValueError" in live_dashboard.SECTION_FAILURES[-1]["error"]


def test_announcement_watch_deduplicates_ingest_by_symbol(monkeypatch):
    watchlist = pd.DataFrame(
        [
            {
                "asof_date": pd.Timestamp("2026-03-24T00:00:00Z"),
                "setup_id": "SETUP_A",
                "setup_name": "A",
                "regime_name": "STABLE",
                "symbol": "ABC",
                "company_master_id": "nse:ABC",
                "watch_reasons_json": '["test"]',
                "watch_status": "active",
                "watch_enabled": True,
                "last_checked_at": pd.NaT,
                "last_document_published_on": pd.NaT,
            },
            {
                "asof_date": pd.Timestamp("2026-03-24T00:00:00Z"),
                "setup_id": "SETUP_B",
                "setup_name": "B",
                "regime_name": "STABLE",
                "symbol": "ABC",
                "company_master_id": "nse:ABC",
                "watch_reasons_json": '["test"]',
                "watch_status": "active",
                "watch_enabled": True,
                "last_checked_at": pd.NaT,
                "last_document_published_on": pd.NaT,
            },
        ]
    )
    calls: list[tuple[str, object, object]] = []

    class FakePipeline:
        def ingest_date_range(self, ticker, from_date, to_date, exchanges=None, parse_reports=None):
            calls.append((ticker, from_date, to_date))
            return type(
                "Summary",
                (),
                {
                    "requested": 1,
                    "discovered": 1,
                    "downloaded": 0,
                    "ocred": 0,
                    "categorized": 0,
                    "parsed": 0,
                    "skipped": 1,
                    "failed": 0,
                },
            )()

    monkeypatch.setattr(announcement_watch, "load_watchlist", lambda **kwargs: watchlist.copy())
    monkeypatch.setattr(announcement_watch, "ManagedAnnouncementPipeline", lambda: FakePipeline())
    monkeypatch.setattr(
        announcement_watch,
        "load_documents_for_company",
        lambda company_master_id, published_from, published_to: pd.DataFrame(
            [
                {
                    "unique_id": "doc-1",
                    "company_master_id": company_master_id,
                    "exchange": "NSE",
                    "subject": "Update",
                    "filed_under_category": "Announcements",
                    "published_on": pd.Timestamp("2026-03-24T10:00:00Z"),
                    "parse_status": "completed",
                    "concise_summary_text": "Done",
                    "categories_json": "[]",
                }
            ]
        ),
    )

    updates, events, meta = announcement_watch.run_announcement_watch(
        asof_date=pd.Timestamp("2026-03-24T00:00:00Z"),
        to_date=pd.Timestamp("2026-03-24T23:59:59Z"),
    )
    assert len(calls) == 1
    assert calls[0][0] == "ABC"
    assert len(updates) == 2
    assert len(events) == 2
    assert meta["watch_count"] == 2
    assert meta["unique_ingest_targets"] == 1


def test_announcement_watch_caps_initial_ingest_lookback(monkeypatch):
    watchlist = pd.DataFrame(
        [
            {
                "asof_date": pd.Timestamp("2026-03-20T00:00:00Z"),
                "setup_id": "SETUP_A",
                "setup_name": "A",
                "regime_name": "STABLE",
                "symbol": "ABC",
                "company_master_id": "nse:ABC",
                "watch_reasons_json": '["test"]',
                "watch_status": "active",
                "watch_enabled": True,
                "last_checked_at": pd.NaT,
                "last_document_published_on": pd.NaT,
            }
        ]
    )
    calls: list[tuple[object, object]] = []

    class FakePipeline:
        def ingest_date_range(self, ticker, from_date, to_date, exchanges=None, parse_reports=None):
            calls.append((from_date, to_date))
            return type(
                "Summary",
                (),
                {
                    "requested": 1,
                    "discovered": 0,
                    "downloaded": 0,
                    "ocred": 0,
                    "categorized": 0,
                    "parsed": 0,
                    "skipped": 0,
                    "failed": 0,
                },
            )()

    monkeypatch.setattr(announcement_watch, "load_watchlist", lambda **kwargs: watchlist.copy())
    monkeypatch.setattr(announcement_watch, "ManagedAnnouncementPipeline", lambda: FakePipeline())
    monkeypatch.setattr(
        announcement_watch,
        "load_documents_for_company",
        lambda company_master_id, published_from, published_to: pd.DataFrame(),
    )

    _, _, meta = announcement_watch.run_announcement_watch(
        asof_date=pd.Timestamp("2026-03-20T00:00:00Z"),
        to_date=pd.Timestamp("2026-04-01T00:00:00Z"),
    )
    assert calls[0][0] == pd.Timestamp("2026-03-29T00:00:00Z").date()
    assert meta["capped_watch_rows"] == 1
    assert meta["initial_lookback_days"] == 3


def test_event_normalization_produces_taxonomy_and_transition():
    event_row = pd.Series(
        {
            "subject": "Company wins major order from defence ministry",
            "filed_under_category": "Announcements",
            "concise_summary_text": "The company received a large order award.",
            "categories_json": '["orders"]',
        }
    )
    parsed = llm_event_evaluator.EventEvaluation(
        what_happened="The company won a major order.",
        sentiment="positive",
        materiality="high",
        setup_effect="strengthens",
        direction="positive",
        surprise=0.8,
        novelty=0.7,
        contradiction=0.0,
        expected_decay_days=120,
        source_reliability="high",
        affected_sectors=["defence"],
        affected_peers=["BEL"],
        governance_risk="none",
        balance_sheet_risk="none",
        execution_risk="low",
        investable_now=True,
        verdict="continue",
        event_class="ORDER_WIN",
        state_transition_hint="UPGRADE_TO_PASS_NOW",
        score_impact=0.4,
        confidence=0.9,
        rationale="Large positive order supports the setup.",
        source_trace=["subject"],
        key_risks=[],
    )

    event_class, transition, score_impact = llm_event_evaluator.normalize_event_evaluation(event_row, parsed)
    assert event_class == "ORDER_WIN"
    assert transition == "UPGRADE_TO_PASS_NOW"
    assert score_impact > 0


def test_event_evaluator_fallback_what_happened_is_valid_for_short_subject():
    row = pd.Series({"subject": "Dividend", "concise_summary_text": None})

    text = llm_event_evaluator.fallback_what_happened(row)
    parsed = llm_event_evaluator.EventEvaluation(
        what_happened=text,
        sentiment="neutral",
        materiality="medium",
        setup_effect="neutral",
        direction="neutral",
        surprise=0.0,
        novelty=0.0,
        contradiction=0.5,
        expected_decay_days=5,
        source_reliability="low",
        affected_sectors=[],
        affected_peers=[],
        governance_risk="none",
        balance_sheet_risk="none",
        execution_risk="none",
        investable_now=False,
        verdict="review_manual",
        event_class="OTHER",
        state_transition_hint="REVIEW_MANUAL",
        score_impact=0.0,
        confidence=0.0,
        rationale="Automatic LLM evaluation failed; manual review is required.",
        source_trace=["llm_error"],
        key_risks=[],
    )

    assert len(parsed.what_happened) >= 10
    assert "Dividend" in parsed.what_happened


def test_event_normalization_prefers_llm_event_class_over_keyword_fallback():
    event_row = pd.Series(
        {
            "subject": "Disclosure of material issue",
            "filed_under_category": "Disclosure of material issue",
            "concise_summary_text": "NCLT uploaded the first motion order in a scheme of amalgamation.",
            "categories_json": '["AMALGAMATION"]',
        }
    )
    parsed = llm_event_evaluator.EventEvaluation(
        what_happened="Procedural update in an amalgamation process.",
        sentiment="mixed",
        materiality="medium",
        setup_effect="neutral",
        direction="neutral",
        surprise=0.1,
        novelty=0.2,
        contradiction=0.1,
        expected_decay_days=10,
        source_reliability="high",
        affected_sectors=[],
        affected_peers=[],
        governance_risk="low",
        balance_sheet_risk="none",
        execution_risk="low",
        investable_now=False,
        verdict="review_manual",
        event_class="OTHER",
        state_transition_hint="REVIEW_MANUAL",
        score_impact=0.0,
        confidence=0.6,
        rationale="This is procedural rather than a clean taxonomy fit.",
        source_trace=["summary"],
        key_risks=[],
    )

    event_class, transition, score_impact = llm_event_evaluator.normalize_event_evaluation(event_row, parsed)
    assert event_class == "OTHER"
    assert transition == "REVIEW_MANUAL"
    assert score_impact < 0


def test_event_normalization_does_not_misclassify_director_update_as_order_win():
    event_row = pd.Series(
        {
            "subject": "Change in Director(s)",
            "filed_under_category": "Change in Director(s)",
            "concise_summary_text": "The company said its director will continue to hold charge under ministry order no. 123.",
            "categories_json": '["WORK_ORDER_CONTRACT"]',
        }
    )
    parsed = llm_event_evaluator.EventEvaluation(
        what_happened="Administrative continuation of director responsibilities.",
        sentiment="neutral",
        materiality="low",
        setup_effect="neutral",
        direction="neutral",
        surprise=0.0,
        novelty=0.1,
        contradiction=0.0,
        expected_decay_days=3,
        source_reliability="high",
        affected_sectors=[],
        affected_peers=[],
        governance_risk="low",
        balance_sheet_risk="none",
        execution_risk="none",
        investable_now=True,
        verdict="continue",
        event_class="OTHER",
        state_transition_hint="NO_CHANGE",
        score_impact=0.0,
        confidence=0.8,
        rationale="This is a routine management update.",
        source_trace=["subject"],
        key_risks=[],
    )

    event_class, transition, score_impact = llm_event_evaluator.normalize_event_evaluation(event_row, parsed)
    assert event_class == "OTHER"
    assert transition == "NO_CHANGE"
    assert score_impact == 0.0


def test_event_normalization_recovers_plural_export_orders_as_order_win():
    event_row = pd.Series(
        {
            "subject": "Outcome of Board Meeting",
            "filed_under_category": "General Updates",
            "concise_summary_text": "The company and its subsidiary received export orders worth INR 1,076 crore to be executed over three years.",
            "categories_json": "[]",
        }
    )
    parsed = llm_event_evaluator.EventEvaluation(
        what_happened="The company received large export orders from international customers.",
        sentiment="positive",
        materiality="medium",
        setup_effect="strengthens",
        direction="positive",
        surprise=0.7,
        novelty=0.7,
        contradiction=0.0,
        expected_decay_days=180,
        source_reliability="high",
        affected_sectors=["defence"],
        affected_peers=[],
        governance_risk="none",
        balance_sheet_risk="none",
        execution_risk="medium",
        investable_now=False,
        verdict="continue",
        event_class="OTHER",
        state_transition_hint="RAISE_SCORE_ONLY",
        score_impact=0.0,
        confidence=0.75,
        rationale="Large orders strengthen medium-term revenue visibility.",
        source_trace=["summary"],
        key_risks=[],
    )

    event_class, transition, score_impact = llm_event_evaluator.normalize_event_evaluation(event_row, parsed)
    assert event_class == "ORDER_WIN"
    assert transition in {"RAISE_SCORE_ONLY", "UPGRADE_TO_PASS_NOW"}
    assert score_impact > 0



def test_event_normalization_keeps_positive_non_investable_event_out_of_reject_state():
    event_row = pd.Series(
        {
            "subject": "Press Release",
            "filed_under_category": "Press Release",
            "concise_summary_text": "The company launched a major new therapy and expanded its product lineup.",
            "categories_json": '["product_launch"]',
        }
    )
    parsed = llm_event_evaluator.EventEvaluation(
        what_happened="A meaningful product launch strengthens the setup but is not enough for immediate entry.",
        sentiment="positive",
        materiality="medium",
        setup_effect="strengthens",
        direction="positive",
        surprise=0.5,
        novelty=0.6,
        contradiction=0.1,
        expected_decay_days=30,
        source_reliability="medium",
        affected_sectors=["pharma"],
        affected_peers=[],
        governance_risk="none",
        balance_sheet_risk="none",
        execution_risk="medium",
        investable_now=False,
        verdict="continue",
        event_class="OTHER",
        state_transition_hint="RAISE_SCORE_ONLY",
        score_impact=0.0,
        confidence=0.7,
        rationale="Constructive event, but not a standalone trigger.",
        source_trace=["summary"],
        key_risks=[],
    )

    event_class, transition, score_impact = llm_event_evaluator.normalize_event_evaluation(event_row, parsed)
    assert event_class == "OTHER"
    assert transition == "RAISE_SCORE_ONLY"
    assert score_impact > 0


def test_event_tensor_builds_richer_fields_into_evaluation_output(monkeypatch):
    events = pd.DataFrame(
        [
            {
                "asof_date": pd.Timestamp("2026-04-01T00:00:00Z"),
                "published_on": pd.Timestamp("2026-04-01T10:00:00Z"),
                "setup_id": "EVENT_OPPORTUNITY_V1",
                "setup_name": "Event Opportunity",
                "symbol": "LUPIN",
                "company_master_id": "lupin",
                "unique_id": "doc-1",
                "event_source": "announcement",
                "subject": "US approval for new product",
                "filed_under_category": "Press Release",
                "parse_status": "parsed",
                "concise_summary_text": "The company received an approval.",
                "categories_json": "[]",
                "watch_reasons_json": "[]",
            }
        ]
    )

    parsed = llm_event_evaluator.EventEvaluation(
        what_happened="The company received a product approval that improves the setup.",
        sentiment="positive",
        materiality="high",
        setup_effect="strengthens",
        direction="positive",
        surprise=0.75,
        novelty=0.65,
        contradiction=0.05,
        expected_decay_days=90,
        source_reliability="high",
        affected_sectors=["pharma"],
        affected_peers=["SUNPHARMA", "CIPLA"],
        governance_risk="none",
        balance_sheet_risk="none",
        execution_risk="low",
        investable_now=True,
        verdict="continue",
        event_class="GUIDANCE_UPGRADE",
        state_transition_hint="UPGRADE_TO_PASS_NOW",
        score_impact=0.3,
        confidence=0.88,
        rationale="Official approval supports a stronger setup with durable relevance.",
        source_trace=["subject", "summary"],
        key_risks=[],
    )

    monkeypatch.setattr(llm_event_evaluator, "load_announcement_evidence", lambda unique_ids: pd.DataFrame())
    monkeypatch.setattr(llm_event_evaluator, "load_documents", lambda unique_ids: pd.DataFrame())
    monkeypatch.setattr(llm_event_evaluator, "build_payload", lambda event_row, document_row: {"stub": True})
    monkeypatch.setattr(
        llm_event_evaluator.AdvisoryEventEvaluator,
        "evaluate_payload",
        lambda self, payload: parsed,
    )

    evaluations, risks, meta = llm_event_evaluator.build_outputs(events, model="test-model")
    assert risks.empty
    assert meta["evaluated_count"] == 1
    row = evaluations.iloc[0]
    assert row["direction"] == "positive"
    assert row["prompt_id"] == "advisory_event_evaluation"
    assert row["prompt_version"] == llm_event_evaluator.ADVISORY_EVENT_PROMPT_VERSION
    assert row["prompt_schema_version"] == llm_event_evaluator.PROMPT_SCHEMA_VERSION
    assert row["surprise"] == 0.75
    assert row["novelty"] == 0.65
    assert row["contradiction"] == 0.05
    assert row["expected_decay_days"] == 90
    assert row["source_reliability"] == "high"
    assert json.loads(row["affected_sectors_json"]) == ["pharma"]
    assert json.loads(row["affected_peers_json"]) == ["SUNPHARMA", "CIPLA"]
    tensor = json.loads(row["event_tensor_json"])
    assert tensor["event_type"] == "GUIDANCE_UPGRADE"
    assert tensor["affected_sectors"] == ["pharma"]
    assert tensor["affected_peers"] == ["SUNPHARMA", "CIPLA"]
    assert tensor["state_transition_hint"] == "UPGRADE_TO_PASS_NOW"


def test_llm_event_evaluator_prefers_compact_evidence_and_marks_raw_fallback(monkeypatch):
    events = pd.DataFrame(
        [
            {
                "asof_date": pd.Timestamp("2026-04-01T00:00:00Z"),
                "published_on": pd.Timestamp("2026-04-01T10:00:00Z"),
                "setup_id": "EVENT_OPPORTUNITY_V1",
                "setup_name": "Event Opportunity",
                "symbol": "LUPIN",
                "company_master_id": "lupin",
                "unique_id": "compact-1",
                "event_source": "announcement",
                "exchange": "NSE",
                "source_url": "https://example.test/compact",
                "subject": "US approval for new product",
                "filed_under_category": "Press Release",
                "parse_status": "parsed",
                "concise_summary_text": "Event-row summary should be secondary.",
                "categories_json": "[]",
                "watch_reasons_json": "[]",
            },
            {
                "asof_date": pd.Timestamp("2026-04-01T00:00:00Z"),
                "published_on": pd.Timestamp("2026-04-01T11:00:00Z"),
                "setup_id": "EVENT_OPPORTUNITY_V1",
                "setup_name": "Event Opportunity",
                "symbol": "SUNPHARMA",
                "company_master_id": "sunpharma",
                "unique_id": "raw-1",
                "event_source": "announcement",
                "exchange": "NSE",
                "source_url": "https://example.test/raw",
                "subject": "Board meeting update",
                "filed_under_category": "Board Meeting",
                "parse_status": "parsed",
                "concise_summary_text": "Raw event-row summary should be secondary.",
                "categories_json": "[]",
                "watch_reasons_json": "[]",
            },
        ]
    )
    compact_docs = pd.DataFrame(
        [
            {
                "evidence_id": "ev-compact-1",
                "unique_id": "compact-1",
                "company_name": "Lupin Ltd",
                "parse_status": "parsed",
                "ocr_status": "not_required",
                "source_reliability": "high",
                "has_text_evidence": True,
                "has_s3_evidence": False,
                "evidence_chars": 84,
                "evidence_summary": "Compact evidence says the company received a material approval.",
                "evidence_excerpt": "Compact excerpt with the important official filing text.",
                "event_class": "GUIDANCE_UPGRADE",
                "direction": "positive",
                "verdict": "continue",
                "confidence": 0.82,
                "event_tensor_json": json.dumps({"event_type": "GUIDANCE_UPGRADE"}),
                "prompt_version": "v-test",
            }
        ]
    )
    raw_docs = pd.DataFrame(
        [
            {
                "unique_id": "raw-1",
                "company_name": "Sun Pharma",
                "parse_status": "parsed",
                "concise_summary_text": "Raw document summary from announcement documents.",
                "text": "Raw document text used only when compact evidence is unavailable.",
                "categories_json": '["board_meeting"]',
                "parsed_reports_json": "[]",
            }
        ]
    )
    requested_raw_ids = []
    payloads = []
    parsed = llm_event_evaluator.EventEvaluation(
        what_happened="The official filing provides enough information for structured evaluation.",
        sentiment="positive",
        materiality="high",
        setup_effect="strengthens",
        direction="positive",
        surprise=0.7,
        novelty=0.6,
        contradiction=0.0,
        expected_decay_days=60,
        source_reliability="high",
        affected_sectors=[],
        affected_peers=[],
        governance_risk="none",
        balance_sheet_risk="none",
        execution_risk="low",
        investable_now=True,
        verdict="continue",
        event_class="GUIDANCE_UPGRADE",
        state_transition_hint="RAISE_SCORE_ONLY",
        score_impact=0.2,
        confidence=0.8,
        rationale="The evidence is compact, recent, and suitable for event scoring.",
        source_trace=["document_context"],
        key_risks=[],
    )

    monkeypatch.setattr(llm_event_evaluator, "load_announcement_evidence", lambda unique_ids: compact_docs)

    def fake_load_documents(unique_ids):
        requested_raw_ids.extend(unique_ids)
        return raw_docs

    def fake_evaluate(self, payload):
        payloads.append(payload)
        return parsed

    monkeypatch.setattr(llm_event_evaluator, "load_documents", fake_load_documents)
    monkeypatch.setattr(llm_event_evaluator, "load_point_in_time_context", lambda *args, **kwargs: {})
    monkeypatch.setattr(llm_event_evaluator, "load_exchange_context", lambda *args, **kwargs: {})
    monkeypatch.setattr(llm_event_evaluator, "load_broad_market_context", lambda *args, **kwargs: {})
    monkeypatch.setattr(llm_event_evaluator, "_safe_trace_call", lambda *args, **kwargs: None)
    monkeypatch.setattr(llm_event_evaluator.AdvisoryEventEvaluator, "evaluate_payload", fake_evaluate)

    evaluations, risks, meta = llm_event_evaluator.build_outputs(events, model="test-model")

    assert len(evaluations) == 2
    assert risks.empty
    assert meta["compact_context_count"] == 1
    assert meta["raw_fallback_context_count"] == 1
    assert requested_raw_ids == ["raw-1"]
    assert payloads[0]["document_context"]["context_source"] == "compact_announcement_evidence"
    assert payloads[0]["document_context"]["fallback_used"] is False
    assert payloads[0]["document_context"]["document_summary_text"] == compact_docs.iloc[0]["evidence_summary"]
    assert payloads[0]["document_context"]["document_text_excerpt"] == compact_docs.iloc[0]["evidence_excerpt"]
    assert payloads[1]["document_context"]["context_source"] == "raw_announcement_document_fallback"
    assert payloads[1]["document_context"]["fallback_used"] is True
    assert payloads[1]["document_context"]["document_summary_text"] == raw_docs.iloc[0]["concise_summary_text"]


def test_advisory_event_evaluator_uses_codex_structured(monkeypatch):
    calls = []

    def fake_structured(prompt, *, response_model, model, system_prompt):
        calls.append((prompt, response_model, model, system_prompt))
        return response_model.model_validate(
            {
                "what_happened": "Company announced a new order win with clear disclosed details.",
                "sentiment": "positive",
                "materiality": "high",
                "setup_effect": "strengthens",
                "direction": "positive",
                "surprise": 0.7,
                "novelty": 0.6,
                "contradiction": 0.1,
                "expected_decay_days": 60,
                "source_reliability": "high",
                "affected_sectors": [],
                "affected_peers": [],
                "governance_risk": "none",
                "balance_sheet_risk": "none",
                "execution_risk": "low",
                "investable_now": True,
                "verdict": "continue",
                "event_class": "ORDER_WIN",
                "state_transition_hint": "UPGRADE_TO_PASS_NOW",
                "score_impact": 0.4,
                "confidence": 0.82,
                "rationale": "Official exchange evidence supports the active setup.",
                "source_trace": ["subject"],
                "key_risks": [],
            }
        )

    monkeypatch.setattr(llm_event_evaluator, "run_codex_structured", fake_structured)
    monkeypatch.setattr(llm_event_evaluator, "CODEX_CLI_EVENT_MODEL", "gpt-test")

    result = llm_event_evaluator.AdvisoryEventEvaluator(model="codex").evaluate_payload({"stub": True})

    assert result.event_class == "ORDER_WIN"
    assert calls[0][2] == "gpt-test"
    assert calls[0][1] is llm_event_evaluator.EventEvaluation
    assert "stock exchange announcements" in calls[0][3]


def test_news_theme_engine_normalizes_richer_investment_theme_schema(tmp_path):
    config_path = tmp_path / "investment_themes.yaml"
    config_path.write_text(
        """
themes:
  - theme_id: DEFENSE_INDIGENISATION
    name: Defense Indigenisation
    status: active
    description: Defense manufacturing theme.
    classification:
      market_cap_fit: [large_cap, mid_cap]
      holding_profile: structural
      holding_period_days: {min: 90, max: 365}
      risk_level: medium
      exit_trigger_types: [thesis, technical]
    detection:
      keywords:
        include: [defense, order, procurement]
        exclude: [rumor]
      match:
        title_weight: 3
        description_weight: 1
        min_hit_score: 2
        max_titles_for_reason: 2
    routing:
      primary_pipeline: event_opportunity_pipeline
      downstream_agents: [news_theme_expert, screener_designer]
      output_mode: idea_candidates
      priority: 80
    screener_templates:
      - template_id: defense-indigenisation-v1
        provider: screenerin
        slug: defense-indigenisation-v1
        label: DEFENSE INDIGENISATION V1
        holding_horizon_note: 1 to 5 years
        entry_style: breakout_or_trend_continuation
        query: |
          Market Capitalization > 1000
    decay:
      model: medium
      half_life_days: 45
    portfolio_guidance:
      positive_sectors: [defense, aerospace]
      ideal_screener_logic: order wins and quality balance sheet
      invalidation_signals: [delayed orders]
""",
        encoding="utf-8",
    )

    themes = news_theme_engine.load_theme_config(config_path=str(config_path))
    assert len(themes) == 1
    theme = themes[0]
    assert theme["theme_id"] == "DEFENSE_INDIGENISATION"
    assert theme["market_cap_fit"] == ["large_cap", "mid_cap"]
    assert theme["ideal_screener_logic"] == "order wins and quality balance sheet"
    assert theme["negative_keywords"] == ["rumor"]
    assert theme["suggested_screeners"][0]["screener_query"] == "Market Capitalization > 1000"


def test_news_theme_engine_active_mapping_returns_theme_ids_and_slugs(monkeypatch):
    monkeypatch.setattr(
        news_theme_engine,
        "build_theme_recommendations",
        lambda **kwargs: {
            "asof_date": pd.Timestamp("2026-03-31T00:00:00Z"),
            "recommendations": [
                {"theme_id": "DEFENSE_INDIGENISATION"},
                {"theme_id": "POWER_GRID_STORAGE"},
            ],
            "error": None,
        },
    )
    monkeypatch.setattr(
        news_theme_engine,
        "list_theme_screeners",
        lambda theme_id=None: pd.DataFrame(
            [
                {"theme_id": "DEFENSE_INDIGENISATION", "screener_slug": "defense-indigenisation-v1", "is_active": True},
                {"theme_id": "POWER_GRID_STORAGE", "screener_slug": "power-grid-storage-v1", "is_active": True},
            ]
        ),
    )

    mapping = news_theme_engine.load_active_theme_screener_mapping(asof_date=pd.Timestamp("2026-03-31T00:00:00Z"))
    assert mapping["theme_ids"] == ["DEFENSE_INDIGENISATION", "POWER_GRID_STORAGE"]
    assert mapping["screener_slugs"] == ["defense-indigenisation-v1", "power-grid-storage-v1"]


def test_download_runner_stops_on_failure_when_continue_disabled(monkeypatch):
    monkeypatch.setattr(
        download_runner,
        "DOWNLOAD_STEPS",
        [
            {"module": "mod.ok", "args": [], "purpose": "test"},
            {"module": "mod.fail", "args": [], "purpose": "test"},
            {"module": "mod.skip", "args": [], "purpose": "test"},
        ],
    )

    def fake_run(step: dict[str, object]) -> dict[str, object]:
        if step["module"] == "mod.fail":
            return {"module": step["module"], "status": "failed", "returncode": 1}
        return {"module": step["module"], "status": "ok", "returncode": 0}

    monkeypatch.setattr(download_runner, "run_download_module", fake_run)

    payload = download_runner.run_all_downloads(continue_on_error=False, dry_run=False)
    assert payload["status"] == "failed"
    assert [row["module"] for row in payload["results"]] == ["mod.ok", "mod.fail"]


def test_download_runner_prioritizes_dhan_and_registered_screener_sync():
    steps = download_runner.DOWNLOAD_STEPS
    modules = [step["module"] for step in steps[:6]]
    assert modules == [
        "data.nseindia.holidays",
        "data.dhanlive.scrip_master",
        "data.sharpelydata.scrip_master",
        "data.company_master",
        "data.dhanlive.ohlcv",
        "data.screenerin.screener_parser",
    ]
    assert steps[5]["args"] == []
    assert steps[5]["purpose"] == "screener_sync_registered"
    parser_modules = [step["module"] for step in download_runner.PARSER_STEPS]
    assert parser_modules[-2:] == ["data.nseindia.indices_parser", "data.benchmark_sync"]


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


def test_technical_and_regime_benchmark_loaders_prefer_nse_indices(monkeypatch):
    nse = pd.DataFrame(
        [
            {"ticker": "Nifty 50", "date": pd.Timestamp("2026-01-01T00:00:00Z"), "close": 100.0},
            {"ticker": "Nifty 50", "date": pd.Timestamp("2026-01-02T00:00:00Z"), "close": 101.0},
        ]
    )
    dhan = pd.DataFrame(
        [
            {"ticker": "NIFTY", "date": pd.Timestamp("2025-01-01T00:00:00Z"), "close": 1.0},
            {"ticker": "NIFTY", "date": pd.Timestamp("2025-01-02T00:00:00Z"), "close": 1.0},
        ]
    )

    def fake_sql_to_df(query, params=None):
        if "FROM nseindia_indices" in query:
            return nse.copy()
        if "FROM dhan_ohlcv_daily" in query:
            return dhan.copy()
        return pd.DataFrame()

    monkeypatch.setattr(technical_features, "sql_to_df", fake_sql_to_df)
    monkeypatch.setattr(regime_engine, "sql_to_df", fake_sql_to_df)

    tech = technical_features.load_benchmark_series()
    regime = regime_engine.load_benchmark_history()

    assert tech["benchmark_close"].tolist() == [100.0, 101.0]
    assert regime["benchmark_close"].tolist() == [100.0, 101.0]


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


def test_research_ledger_start_and_finish(monkeypatch):
    writes: list[tuple[str, pd.DataFrame, list[str]]] = []

    monkeypatch.setattr(research_ledger, "ensure_tables", lambda: None)
    monkeypatch.setattr(research_ledger, "_try_git_rev", lambda: "deadbeef")

    def fake_upsert(df, table_name, unique_keys, **kwargs):
        writes.append((table_name, df.copy(), list(unique_keys)))

    monkeypatch.setattr(research_ledger, "upsert_to_db", fake_upsert)

    run_id = research_ledger.start_research_run(
        run_type="advisory_pipeline",
        entrypoint="advisory.pipeline",
        config={"alpha": 1, "beta": True},
        asof_date=pd.Timestamp("2026-04-04T00:00:00Z"),
        label="baseline-run",
        objective="test objective",
        validation_protocol={"split": "purged_walk_forward"},
    )
    assert run_id
    assert writes[0][0] == research_ledger.LEDGER_TABLE
    assert writes[0][1].iloc[0]["status"] == "running"
    assert writes[0][1].iloc[0]["git_rev"] == "deadbeef"
    assert writes[0][1].iloc[0]["validation_protocol_json"]

    research_ledger.finish_research_run(
        run_id,
        status="completed",
        data_snapshot={"asof_date": "2026-04-04"},
        result_metrics={"sharpe": 1.2},
    )
    assert writes[1][0] == research_ledger.LEDGER_TABLE
    assert writes[1][1].iloc[0]["research_run_id"] == run_id
    assert writes[1][1].iloc[0]["status"] == "completed"


def test_research_ledger_summary_helpers():
    summary = {
        "stages": {
            "rules": {"row_count": 10},
            "portfolio": {"row_count": 3},
        }
    }
    snapshot = research_ledger.build_data_snapshot(
        asof_date=pd.Timestamp("2026-04-04T00:00:00Z"),
        summary=summary,
    )
    metrics = research_ledger.build_result_metrics(status="completed", summary=summary)
    assert snapshot["stage_row_counts"]["rules"] == 10
    assert metrics["portfolio_row_count"] == 3


def test_master_pipeline_run_downloads_uses_python_runner(monkeypatch):
    monkeypatch.setattr(
        master_pipeline,
        "run_all_downloads",
        lambda **kwargs: {
            "status": "ok",
            "modules": ["data.nseindia.holidays"],
            "results": [{"module": "data.nseindia.holidays", "status": "ok", "returncode": 0}],
        },
    )

    payload = master_pipeline.run_downloads(
        dry_run=False,
        download_script=master_pipeline.DEFAULT_DOWNLOAD_SCRIPT,
        continue_on_error=False,
    )
    assert payload["status"] == "ok"
    assert payload["results"][0]["module"] == "data.nseindia.holidays"
    assert payload["download_script"].endswith("complete_data.sh")


def test_portfolio_priority_rewards_positive_event_transition():
    base = pd.Series(
        {
            "confidence": 0.8,
            "conviction_bucket": "medium",
            "risk_bucket": "medium",
            "suggested_allocation_inr": 50000.0,
            "allocation_pct_of_adv20d": 0.0001,
            "score_impact": 0.0,
            "state_transition_hint": "NO_CHANGE",
        }
    )
    upgraded = base.copy()
    upgraded["score_impact"] = 0.3
    upgraded["state_transition_hint"] = "UPGRADE_TO_PASS_NOW"

    assert portfolio_engine.compute_priority_score(upgraded) > portfolio_engine.compute_priority_score(base)


def test_watchlist_builder_applies_event_state_transition(monkeypatch):
    monkeypatch.setattr(
        watchlist_builder,
        "load_candidate_rows",
        lambda **kwargs: pd.DataFrame(
            [
                {
                    "asof_date": pd.Timestamp("2026-03-24T00:00:00Z"),
                    "setup_id": "TEST",
                    "setup_name": "Test",
                    "regime_name": "STABLE",
                    "symbol": "ABC",
                    "company_master_id": "nse:ABC",
                    "screener_slug": "demo",
                    "rank": 1,
                    "candidate_state": "WATCH_EVENT",
                    "setup_score": 0.55,
                    "watch_reason_detail": "waiting for event",
                    "entry_style": "RETEST_OF_PRIOR_BREAKOUT",
                    "attractive_price_low": 100.0,
                    "attractive_price_high": 105.0,
                    "invalidation_price": 95.0,
                    "entry_note": "wait",
                    "near_miss_flag": False,
                    "watch_enabled": True,
                    "watch_reasons": '["results"]',
                }
            ]
        ),
    )
    monkeypatch.setattr(
        watchlist_builder,
        "load_latest_event_transitions",
        lambda **kwargs: pd.DataFrame(
            [
                {
                    "setup_id": "TEST",
                    "symbol": "ABC",
                    "event_class": "RESULTS_POSITIVE",
                    "state_transition_hint": "UPGRADE_TO_PASS_NOW",
                    "score_impact": 0.3,
                }
            ]
        ),
    )
    monkeypatch.setattr(watchlist_builder, "sql_to_df", lambda *args, **kwargs: pd.DataFrame())

    df = watchlist_builder.build_watchlist()
    row = df.iloc[0]
    assert row["candidate_state"] == "WATCH_EVENT"
    assert row["current_state"] == "PASS_NOW"
    assert row["watch_status"] == "active"
    assert row["last_event_class"] == "RESULTS_POSITIVE"


def test_watchlist_builder_promotes_raise_score_only_when_adjusted_score_crosses_threshold(monkeypatch):
    monkeypatch.setattr(
        watchlist_builder,
        "load_candidate_rows",
        lambda **kwargs: pd.DataFrame(
            [
                {
                    "asof_date": pd.Timestamp("2026-03-24T00:00:00Z"),
                    "setup_id": "TEST",
                    "setup_name": "Test",
                    "regime_name": "STABLE",
                    "symbol": "ABC",
                    "company_master_id": "nse:ABC",
                    "screener_slug": "demo",
                    "rank": 1,
                    "candidate_state": "WATCH_EVENT",
                    "setup_score": 0.55,
                    "watch_reason_detail": "waiting for event",
                    "entry_style": "RETEST_OF_PRIOR_BREAKOUT",
                    "attractive_price_low": 100.0,
                    "attractive_price_high": 105.0,
                    "invalidation_price": 95.0,
                    "entry_note": "wait",
                    "near_miss_flag": False,
                    "watch_enabled": True,
                    "watch_reasons": '["results"]',
                }
            ]
        ),
    )
    monkeypatch.setattr(
        watchlist_builder,
        "load_latest_event_transitions",
        lambda **kwargs: pd.DataFrame(
            [
                {
                    "setup_id": "TEST",
                    "symbol": "ABC",
                    "event_class": "OTHER",
                    "state_transition_hint": "RAISE_SCORE_ONLY",
                    "score_impact": 0.2,
                }
            ]
        ),
    )
    monkeypatch.setattr(
        watchlist_builder,
        "load_setup_thresholds",
        lambda: {"TEST": {"pass_now": 0.68, "watch_breakout": 0.58, "watch_event": 0.48}},
    )
    monkeypatch.setattr(watchlist_builder, "sql_to_df", lambda *args, **kwargs: pd.DataFrame())

    df = watchlist_builder.build_watchlist()
    row = df.iloc[0]
    assert row["current_state"] == "PASS_NOW"
    assert row["watch_status"] == "active"
    assert row["last_state_transition_hint"] == "RAISE_SCORE_ONLY"


def test_setup_registry_normalizes_multi_screener_overlay_config(tmp_path):
    config_path = tmp_path / "advisory_setups.yaml"
    config_path.write_text(
        """
setups:
  - setup_id: TEST_SETUP
    setup_name: Test setup
    screeners: [screen-a, screen-b]
    screener_mode: intersection
    allowed_regimes: [STABLE]
    allowed_overlays: [NONE, TARIFF_PRESSURE]
    blocked_overlays: [GEOPOLITICAL_RISK]
    overlay_screeners:
      TARIFF_PRESSURE:
        add: [screen-c]
        remove: [screen-a]
""".strip(),
        encoding="utf-8",
    )

    setups = setup_registry.load_setup_registry(str(config_path))
    row = setups[0]
    assert row["screeners"] == ["screen-a", "screen-b"]
    assert row["screener_mode"] == "intersection"
    assert row["allowed_overlays"] == ["NONE", "TARIFF_PRESSURE"]
    assert row["blocked_overlays"] == ["GEOPOLITICAL_RISK"]
    assert row["overlay_screeners"]["TARIFF_PRESSURE"]["add"] == ["screen-c"]
    assert row["overlay_screeners"]["TARIFF_PRESSURE"]["remove"] == ["screen-a"]


def test_news_overlay_engine_classifies_tariff_overlay():
    regime_row = {"regime_name": "STABLE_BUT_TARIFF_RISING", "tariff_pressure_flag": True}
    news_rows = pd.DataFrame(
        [
            {"title": "Tariff pressure rises on imports", "description": "New import duty and tariff measures announced."},
            {"title": "Trade war concerns increase", "description": "Tariff commentary dominates market outlook."},
        ]
    )
    overlay_name, intensity, reason, source_count = news_overlay_engine.classify_overlay(regime_row, news_rows)
    assert overlay_name == "TARIFF_PRESSURE"
    assert intensity > 0
    assert source_count == 2
    assert "tariff" in reason.lower()


def test_rule_engine_resolves_overlay_screeners_and_blocks_disallowed_overlay():
    setup = {
        "setup_id": "TEST",
        "screeners": ["screen-a", "screen-b"],
        "screener_mode": "union",
        "allowed_regimes": ["STABLE"],
        "allowed_overlays": ["NONE"],
        "blocked_overlays": ["GEOPOLITICAL_RISK"],
        "overlay_screeners": {
            "TARIFF_PRESSURE": {"add": ["screen-c"], "remove": ["screen-a"]},
        },
        "score_thresholds": {"pass_now": 0.68, "watch_breakout": 0.58, "watch_event": 0.48, "near_miss_gap": 0.05},
    }
    active_screeners, mode, theme_ids = rule_engine.resolve_setup_screeners(setup, "TARIFF_PRESSURE")
    assert active_screeners == ["screen-b", "screen-c"]
    assert mode == "union"
    assert theme_ids == []

    row = pd.Series(
        {
            "company_master_id": "nse:ABC",
            "adj_close": 100.0,
            "fundamentals_freshness_status": "fresh",
            "market_cap": 100000.0,
            "avg_traded_value_20d": 1000000000.0,
            "breakout_extension_pct": 1.0,
            "pass_above_dma_20": True,
            "pass_above_dma_50": True,
            "pass_above_dma_200": True,
            "rs_vs_benchmark": 0.1,
            "rs_vs_sector": 0.1,
        }
    )
    candidate_state, _, rejections = rule_engine.evaluate_setup_row(
        row,
        regime_name="STABLE",
        overlay_name="GEOPOLITICAL_RISK",
        setup=setup,
    )
    assert candidate_state == "REJECT"
    assert any(item["reason_code"] == "overlay_not_allowed" for item in rejections)


def test_rule_engine_softens_non_preferred_overlay_into_score_penalty():
    setup = {
        "setup_id": "TEST",
        "screeners": ["screen-a"],
        "allowed_regimes": ["STABLE"],
        "allowed_overlays": ["NONE"],
        "blocked_overlays": [],
        "technical_rules": [],
        "fundamental_rules": [],
        "intraday_rules": [],
        "score_thresholds": {"pass_now": 0.68, "watch_breakout": 0.58, "watch_event": 0.48, "near_miss_gap": 0.05},
    }
    row = pd.Series(
        {
            "asof_date": pd.Timestamp("2026-04-01T00:00:00Z"),
            "company_master_id": "nse:ABC",
            "adj_close": 100.0,
            "technical_snapshot_date": pd.Timestamp("2026-04-01T00:00:00Z"),
            "fundamentals_snapshot_date": pd.Timestamp("2026-04-01T00:00:00Z"),
            "regime_snapshot_date": pd.Timestamp("2026-04-01T00:00:00Z"),
            "fundamentals_freshness_status": "fresh",
            "market_cap": 100000.0,
            "avg_traded_value_20d": 1000000000.0,
            "breakout_extension_pct": 1.0,
            "pass_above_dma_20": True,
            "pass_above_dma_50": True,
            "pass_above_dma_200": True,
            "rs_vs_benchmark": 0.1,
            "rs_vs_sector": 0.1,
        }
    )
    candidate_state, details, rejections = rule_engine.evaluate_setup_row(
        row,
        regime_name="STABLE",
        overlay_name="TARIFF_PRESSURE",
        setup=setup,
    )
    assert candidate_state != "REJECT"
    assert details["setup_score"] < 0.68
    assert not any(item["reason_code"] == "overlay_not_allowed" and item["severity"] == "hard" for item in rejections)


def test_pipeline_stage_alias_watch_spans_ingest_and_match():
    assert pipeline.stage_enabled("watch_ingest", start_at="watch", stop_at="watch")
    assert pipeline.stage_enabled("watch_match", start_at="watch", stop_at="watch")
    assert not pipeline.stage_enabled("news", start_at="watch", stop_at="watch")


def test_pipeline_stage_order_includes_macro_features_before_regime():
    assert pipeline.PIPELINE_STAGES.index("macro") < pipeline.PIPELINE_STAGES.index("macro_features")
    assert pipeline.PIPELINE_STAGES.index("macro_features") < pipeline.PIPELINE_STAGES.index("regime")
    assert pipeline.PIPELINE_STAGES.index("exchange_events") < pipeline.PIPELINE_STAGES.index("exchange_features")
    assert pipeline.PIPELINE_STAGES.index("exchange_features") < pipeline.PIPELINE_STAGES.index("evaluate")


def test_exchange_events_normalize_block_and_insider_rows():
    block = pd.DataFrame(
        [
            {
                "date": pd.Timestamp("2026-04-01T00:00:00Z"),
                "symbol": "hdfcbank",
                "client_name": "Long Fund",
                "buysell": "BUY",
                "quantity": "10,000",
                "price": "1500",
                "company_master_id": "nse:HDFCBANK",
            }
        ]
    )
    insider = pd.DataFrame(
        [
            {
                "date": pd.Timestamp("2026-04-02T00:00:00Z"),
                "trade_date_to": pd.Timestamp("2026-04-01T00:00:00Z"),
                "symbol": "hdfcbank",
                "insider_name": "Director A",
                "transaction_type": "Sell",
                "quantity": "5000",
                "value_inr": "7500000",
                "holding_pct_before": "1.2",
                "holding_pct_after": "1.1",
                "person_category": "Director",
                "company_master_id": "nse:HDFCBANK",
            }
        ]
    )
    block_out = exchange_events.normalize_block_or_bulk(block, source="nse_block_deal")
    insider_out = exchange_events.normalize_insider_deals(insider)
    assert block_out.iloc[0]["event_source"] == "nse_block_deal"
    assert block_out.iloc[0]["event_type"] == "BLOCK_DEAL"
    assert block_out.iloc[0]["symbol"] == "HDFCBANK"
    assert block_out.iloc[0]["value_inr"] == 15_000_000.0
    assert block_out.iloc[0]["side"] == "BUY"
    assert insider_out.iloc[0]["event_source"] == "nse_insider_deal"
    assert insider_out.iloc[0]["event_type"] == "INSIDER_DEAL"
    assert insider_out.iloc[0]["side"] == "SELL"
    assert insider_out.iloc[0]["known_on"] == pd.Timestamp("2026-04-02T00:00:00Z")


def test_exchange_events_normalize_recent_and_short_rows_keep_scalar_fields():
    recent = pd.DataFrame(
        [
            {
                "symbol": "JUSTDIAL",
                "company_master_id": "nse:JUSTDIAL",
                "date": pd.Timestamp("2026-04-13T00:00:00Z"),
                "purpose": "Financial Results",
                "details": "To consider and approve results",
            }
        ]
    )
    short = pd.DataFrame(
        [
            {
                "symbol": "BEL",
                "company_master_id": "nse:BEL",
                "date": pd.Timestamp("2026-04-09T00:00:00Z"),
                "quantity": 200,
            }
        ]
    )

    recent_out = exchange_events.normalize_recent_events(recent)
    short_out = exchange_events.normalize_short_selling(short)

    assert recent_out.iloc[0]["event_source"] == "nse_event_calendar"
    assert recent_out.iloc[0]["event_type"] == "NSE_EVENT"
    assert short_out.iloc[0]["event_source"] == "nse_short_selling"
    assert short_out.iloc[0]["event_type"] == "SHORT_SELLING"
    assert short_out.iloc[0]["side"] == "SELL"


def test_exchange_features_compute_distribution_and_upcoming_earnings():
    events = pd.DataFrame(
        [
            {
                "symbol": "HDFCBANK",
                "company_master_id": "nse:HDFCBANK",
                "known_on": pd.Timestamp("2026-04-01T00:00:00Z"),
                "event_date": pd.Timestamp("2026-04-01T00:00:00Z"),
                "event_source": "nse_bulk_deal",
                "event_type": "BULK_DEAL",
                "side": "SELL",
                "value_inr": 200_000_000.0,
                "quantity": 100000,
                "event_summary": "bulk sell",
            },
            {
                "symbol": "HDFCBANK",
                "company_master_id": "nse:HDFCBANK",
                "known_on": pd.Timestamp("2026-04-02T00:00:00Z"),
                "event_date": pd.Timestamp("2026-04-02T00:00:00Z"),
                "event_source": "nse_insider_deal",
                "event_type": "INSIDER_DEAL",
                "side": "SELL",
                "value_inr": 50_000_000.0,
                "quantity": 10000,
                "event_summary": "insider sell",
            },
            {
                "symbol": "HDFCBANK",
                "company_master_id": "nse:HDFCBANK",
                "known_on": pd.Timestamp("2026-04-03T00:00:00Z"),
                "event_date": pd.Timestamp("2026-04-10T00:00:00Z"),
                "event_source": "nse_earnings_event",
                "event_type": "EARNINGS_EVENT",
                "side": None,
                "value_inr": None,
                "quantity": None,
                "event_summary": "earnings",
            },
        ]
    )
    dates = pd.DataFrame({"asof_date": [pd.Timestamp("2026-04-04T00:00:00Z")]})
    out = exchange_features.compute_exchange_feature_columns(events, dates)
    latest = out.iloc[0]
    assert latest["bulk_deal_sell_value_20d"] == 200_000_000.0
    assert latest["insider_net_value_90d"] == -50_000_000.0
    assert bool(latest["upcoming_earnings_14d"]) is True
    assert latest["exchange_distribution_score"] > 0


def test_macro_features_are_point_in_time_and_null_tolerant():
    dates = pd.date_range("2026-01-01", periods=100, freq="D", tz="UTC")
    source = pd.DataFrame(
        {
            "asof_date": dates,
            "vix_close": [18.0] * 99 + [31.0],
            "broad_usd_index": list(range(100, 200)),
            "wti_crude_spot": list(range(70, 170)),
            "inr_usd_spot": list(pd.Series(range(8300, 8400)) / 100.0),
            "ust10y_yield": [4.0] * 80 + [4.4] * 20,
            "gsec_2y_yield": [6.5] * 100,
            "gsec_10y_yield": [7.0] * 80 + [7.4] * 20,
            "repo_rate": [6.5] * 100,
            "india_cfpi_combined": [150.0] * 40 + [153.0] * 60,
            "wpi_crude_petroleum_gas": [120.0] * 40 + [127.0] * 60,
            "macro_usa_freshness_status": ["FRESH"] * 100,
            "cpi_freshness_status": ["FRESH"] * 100,
        }
    )
    out = macro_features.compute_macro_feature_columns(source)
    latest = out.iloc[-1]
    assert latest["macro_stress_score"] >= 0.65
    assert latest["macro_risk_state"] == "STRESS"
    assert latest["macro_sizing_multiplier"] < 1.0
    assert "india_cpi_change_60d" in out.columns
    assert out["macro_missing_source_count"].eq(0).all()


def test_macro_features_handle_minimal_snapshot_without_crashing():
    source = pd.DataFrame({"asof_date": pd.date_range("2026-01-01", periods=3, freq="D", tz="UTC")})
    out = macro_features.compute_macro_feature_columns(source)
    assert len(out) == 3
    assert set(out["macro_risk_state"]) == {"NORMAL"}
    assert out["macro_sizing_multiplier"].eq(1.0).all()


def test_macro_feature_builder_uses_history_but_returns_target_window(monkeypatch):
    dates = pd.date_range("2026-01-01", periods=100, freq="D", tz="UTC")
    source = pd.DataFrame(
        {
            "asof_date": dates,
            "gsec_10y_yield": [7.0] * 80 + [7.4] * 20,
            "gsec_2y_yield": [6.5] * 100,
            "vix_close": [18.0] * 100,
        }
    )
    source.attrs["target_from"] = dates[-1].normalize()
    source.attrs["target_to"] = dates[-1].normalize()
    monkeypatch.setattr(macro_features, "load_macro_daily", lambda **kwargs: source)

    out = macro_features.build_macro_features(from_date=dates[-1], to_date=dates[-1])
    assert len(out) == 1
    assert out.iloc[0]["asof_date"] == dates[-1].normalize()
    assert round(float(out.iloc[0]["gsec_10y_change_20d_bps"]), 2) == 40.0


def test_event_model_dataset_joins_macro_features_by_anchor_date(monkeypatch):
    events = pd.DataFrame(
        [
            {
                "published_on": pd.Timestamp("2026-01-03T09:00:00Z"),
                "asof_date": pd.Timestamp("2026-01-03T00:00:00Z"),
                "setup_id": "TEST",
                "symbol": "HDFCBANK",
                "unique_id": "u1",
                "event_source": "announcement",
                "sentiment": "positive",
                "materiality": "high",
                "setup_effect": "strengthens",
                "direction": "positive",
                "surprise": 0.5,
                "novelty": 0.5,
                "contradiction": 0.0,
                "expected_decay_days": 10,
                "source_reliability": "high",
                "governance_risk": "none",
                "balance_sheet_risk": "none",
                "execution_risk": "none",
                "investable_now": True,
                "verdict": "continue",
                "event_class": "ORDER_WIN",
                "state_transition_hint": "UPGRADE_TO_PASS_NOW",
                "score_impact": 0.2,
                "confidence": 0.8,
            }
        ]
    )
    prices = pd.DataFrame(
        [
            {"symbol": "HDFCBANK", "date": pd.Timestamp("2026-01-04T00:00:00Z"), "close": 100.0},
            {"symbol": "HDFCBANK", "date": pd.Timestamp("2026-01-05T00:00:00Z"), "close": 104.0},
        ]
    )
    macro = pd.DataFrame(
        [
            {
                "macro_asof_date": pd.Timestamp("2026-01-04T00:00:00Z"),
                "macro_stress_score": 0.45,
                "macro_sizing_multiplier": 0.75,
            }
        ]
    )
    monkeypatch.setattr(event_meta_model, "load_event_rows", lambda **kwargs: events)
    monkeypatch.setattr(event_meta_model, "load_price_history", lambda *args, **kwargs: prices)
    monkeypatch.setattr(event_meta_model, "load_intraday_event_features", lambda *args, **kwargs: pd.DataFrame())
    monkeypatch.setattr(event_meta_model, "load_macro_event_features", lambda *args, **kwargs: macro)

    out = event_meta_model.build_labeled_event_dataset(horizon_days=1, return_threshold=0.02)
    assert len(out) == 1
    assert out.iloc[0]["macro_stress_score"] == 0.45
    features, feature_cols = event_meta_model._prepare_feature_frame(out)
    assert "macro_stress_score" in feature_cols
    assert features.iloc[0]["macro_sizing_multiplier"] == 0.75


def test_event_model_dataset_joins_exchange_features_by_anchor_date(monkeypatch):
    events = pd.DataFrame(
        [
            {
                "published_on": pd.Timestamp("2026-01-03T09:00:00Z"),
                "asof_date": pd.Timestamp("2026-01-03T00:00:00Z"),
                "setup_id": "TEST",
                "symbol": "HDFCBANK",
                "unique_id": "u1",
                "event_source": "announcement",
                "sentiment": "positive",
                "materiality": "high",
                "setup_effect": "strengthens",
                "direction": "positive",
                "surprise": 0.5,
                "novelty": 0.5,
                "contradiction": 0.0,
                "expected_decay_days": 10,
                "source_reliability": "high",
                "governance_risk": "none",
                "balance_sheet_risk": "none",
                "execution_risk": "none",
                "investable_now": True,
                "verdict": "continue",
                "event_class": "ORDER_WIN",
                "state_transition_hint": "UPGRADE_TO_PASS_NOW",
                "score_impact": 0.2,
                "confidence": 0.8,
            }
        ]
    )
    prices = pd.DataFrame(
        [
            {"symbol": "HDFCBANK", "date": pd.Timestamp("2026-01-04T00:00:00Z"), "close": 100.0},
            {"symbol": "HDFCBANK", "date": pd.Timestamp("2026-01-05T00:00:00Z"), "close": 104.0},
        ]
    )
    exchange = pd.DataFrame(
        [
            {
                "symbol": "HDFCBANK",
                "exchange_asof_date": pd.Timestamp("2026-01-04T00:00:00Z"),
                "exchange_distribution_score": 0.40,
                "exchange_event_score": -0.40,
                "short_selling_event_count_20d": 3,
            }
        ]
    )
    monkeypatch.setattr(event_meta_model, "load_event_rows", lambda **kwargs: events)
    monkeypatch.setattr(event_meta_model, "load_price_history", lambda *args, **kwargs: prices)
    monkeypatch.setattr(event_meta_model, "load_intraday_event_features", lambda *args, **kwargs: pd.DataFrame())
    monkeypatch.setattr(event_meta_model, "load_macro_event_features", lambda *args, **kwargs: pd.DataFrame())
    monkeypatch.setattr(event_meta_model, "load_exchange_event_features", lambda *args, **kwargs: exchange)

    out = event_meta_model.build_labeled_event_dataset(horizon_days=1, return_threshold=0.02)
    assert len(out) == 1
    assert out.iloc[0]["exchange_distribution_score"] == 0.40
    features, feature_cols = event_meta_model._prepare_feature_frame(out)
    assert "exchange_distribution_score" in feature_cols
    assert features.iloc[0]["short_selling_event_count_20d"] == 3


def test_event_model_live_dataset_uses_event_day_context_not_next_day_anchor(monkeypatch):
    events = pd.DataFrame(
        [
            {
                "published_on": pd.Timestamp("2026-01-03T09:00:00Z"),
                "asof_date": pd.Timestamp("2026-01-03T00:00:00Z"),
                "setup_id": "TEST",
                "symbol": "HDFCBANK",
                "unique_id": "u1",
                "event_source": "announcement",
                "sentiment": "positive",
                "materiality": "high",
                "setup_effect": "strengthens",
                "direction": "positive",
                "surprise": 0.5,
                "novelty": 0.5,
                "contradiction": 0.0,
                "expected_decay_days": 10,
                "source_reliability": "high",
                "governance_risk": "none",
                "balance_sheet_risk": "none",
                "execution_risk": "none",
                "investable_now": True,
                "verdict": "continue",
                "event_class": "ORDER_WIN",
                "state_transition_hint": "UPGRADE_TO_PASS_NOW",
                "score_impact": 0.2,
                "confidence": 0.8,
            }
        ]
    )
    intraday = pd.DataFrame(
        [
            {
                "symbol": "HDFCBANK",
                "asof_date": pd.Timestamp("2026-01-03T00:00:00Z"),
                "intraday_close_vs_vwap_pct": 0.4,
                "intraday_pct_bars_above_vwap": 0.6,
                "intraday_close_location_pct": 0.7,
                "intraday_opening_range_breakout_up": True,
                "intraday_prev_day_breakout_up": False,
                "intraday_failed_prev_day_breakout": False,
                "intraday_first_30m_return_pct": 0.2,
                "intraday_last_60m_return_pct": 0.1,
                "intraday_volume_vs_20d": 1.3,
                "intraday_breakout_score": 0.55,
                "intraday_pattern_label": "EVENT_DAY_STRENGTH",
            }
        ]
    )
    macro = pd.DataFrame(
        [
            {
                "macro_asof_date": pd.Timestamp("2026-01-03T00:00:00Z"),
                "macro_stress_score": 0.2,
                "macro_sizing_multiplier": 0.9,
            }
        ]
    )
    exchange = pd.DataFrame(
        [
            {
                "symbol": "HDFCBANK",
                "exchange_asof_date": pd.Timestamp("2026-01-03T00:00:00Z"),
                "deal_net_value_20d": 500000.0,
                "deal_cluster_count_20d": 1.0,
                "exchange_event_score": 0.25,
            }
        ]
    )
    monkeypatch.setattr(event_meta_model, "load_event_rows", lambda **kwargs: events)
    monkeypatch.setattr(event_meta_model, "load_intraday_event_features", lambda *args, **kwargs: intraday)
    monkeypatch.setattr(event_meta_model, "load_macro_event_features", lambda *args, **kwargs: macro)
    monkeypatch.setattr(event_meta_model, "load_exchange_event_features", lambda *args, **kwargs: exchange)

    out = event_meta_model.build_live_event_dataset()
    assert len(out) == 1
    assert out.iloc[0]["context_date"] == pd.Timestamp("2026-01-03T00:00:00Z")
    assert pd.isna(out.iloc[0]["anchor_date"])
    assert pd.isna(out.iloc[0]["target_label"])
    assert float(out.iloc[0]["intraday_breakout_score"]) == 0.55
    assert float(out.iloc[0]["macro_stress_score"]) == 0.2
    assert float(out.iloc[0]["deal_net_value_20d"]) == 500000.0


def test_llm_payload_includes_bounded_exchange_context(monkeypatch):
    event_row = pd.Series(
        {
            "published_on": pd.Timestamp("2026-04-01T09:00:00Z"),
            "asof_date": pd.Timestamp("2026-04-01T00:00:00Z"),
            "setup_id": "TEST",
            "setup_name": "Test",
            "unique_id": "u1",
            "event_source": "announcement",
            "company_master_id": "nse:HDFCBANK",
            "symbol": "HDFCBANK",
            "exchange": "NSE",
            "source_url": "",
            "subject": "Order win",
            "filed_under_category": "General",
            "parse_status": "parsed",
            "concise_summary_text": "Won order",
            "categories_json": "[]",
            "watch_reasons_json": "[]",
        }
    )
    monkeypatch.setattr(llm_event_evaluator, "load_point_in_time_context", lambda *args, **kwargs: {"regime_name": "STABLE"})
    monkeypatch.setattr(
        llm_event_evaluator,
        "load_exchange_context",
        lambda *args, **kwargs: {
            "features": {"exchange_distribution_score": 0.4},
            "recent_events": [{"event_source": "nse_bulk_deal", "side": "SELL"}],
        },
    )
    monkeypatch.setattr(
        llm_event_evaluator,
        "load_latest_market_context",
        lambda *args, **kwargs: {
            "summary": {"regime_name": "RISK_OFF", "breadth_trend_alignment_pct": 32.0},
            "top_universe": [{"symbol": "HDFCBANK", "rank_pct": 2.0}],
        },
    )
    payload = llm_event_evaluator.build_payload(event_row, None)
    assert payload["exchange_context"]["features"]["exchange_distribution_score"] == 0.4
    assert len(payload["exchange_context"]["recent_events"]) == 1
    assert payload["broad_market_context"]["summary"]["regime_name"] == "RISK_OFF"
    assert payload["broad_market_context"]["symbol_context"]["rank_pct"] == 2.0


def test_llm_exchange_context_reads_compact_bhavcopy_evidence(monkeypatch):
    calls = []

    def fake_table_exists(table_name):
        return table_name == llm_event_evaluator.BHAVCOPY_EVIDENCE_TABLE

    def fake_sql_to_df(sql, params=None):
        calls.append((sql, params))
        assert llm_event_evaluator.BHAVCOPY_EVIDENCE_TABLE in sql
        return pd.DataFrame(
            [
                {
                    "bhavcopy_asof_date": pd.Timestamp("2026-03-31T00:00:00Z"),
                    "close": 100.5,
                    "daily_return": 0.04,
                    "volume": 500000,
                    "avg_volume_20d": 250000,
                    "turnover_value_inr": 50000000,
                    "avg_turnover_value_20d": 22000000,
                    "number_of_trades": 12000,
                    "close_volatility_20d": 0.025,
                    "annualized_volatility": 0.39,
                    "applicable_margin": 12.5,
                    "deal_net_value_inr": 15000000,
                    "block_deal_count": 1,
                    "bulk_deal_count": 0,
                    "short_selling_quantity": 2000,
                    "short_selling_count": 1,
                    "circuit_hit_count": 0,
                    "circuit_hit_types": None,
                    "deal_pressure": "accumulation",
                    "evidence_score": 0.72,
                    "evidence_summary": "High turnover with net accumulation and no circuit stress.",
                }
            ]
        )

    monkeypatch.setattr(llm_event_evaluator, "_table_exists", fake_table_exists)
    monkeypatch.setattr(llm_event_evaluator, "sql_to_df", fake_sql_to_df)

    context = llm_event_evaluator.load_exchange_context("HDFCBANK", pd.Timestamp("2026-04-01T09:00:00Z"))

    assert len(calls) == 1
    assert context["recent_events"] == []
    assert context["bhavcopy_evidence"]["deal_pressure"] == "accumulation"
    assert context["bhavcopy_evidence"]["evidence_summary"] == "High turnover with net accumulation and no circuit stress."
    assert context["bhavcopy_evidence"]["bhavcopy_asof_date"] == "2026-03-31T00:00:00+00:00"


def test_risk_engine_macro_multiplier_reduces_allocated_size(monkeypatch):
    evaluations = pd.DataFrame(
        [
            {
                "published_on": pd.Timestamp("2026-04-01T09:00:00Z"),
                "asof_date": pd.Timestamp("2026-04-01T00:00:00Z"),
                "setup_id": "LARGECAP_BREAKOUT_V1",
                "setup_name": "Largecap",
                "symbol": "HDFCBANK",
                "company_master_id": "nse:HDFCBANK",
                "unique_id": "u1",
                "evaluation_status": "completed",
                "verdict": "continue",
                "investable_now": True,
                "materiality": "high",
                "setup_effect": "strengthens",
                "sentiment": "positive",
                "event_class": "ORDER_WIN",
                "state_transition_hint": "",
                "score_impact": 0.0,
                "confidence": 0.9,
                "governance_risk": "none",
                "balance_sheet_risk": "none",
                "execution_risk": "none",
                "review_action": "",
                "review_veto": False,
            }
        ]
    )
    monkeypatch.setattr(risk_engine, "load_event_evaluations", lambda **kwargs: evaluations)
    monkeypatch.setattr(risk_engine, "load_base_candidate_fallbacks", lambda **kwargs: pd.DataFrame())
    monkeypatch.setattr(risk_engine, "load_watch_states", lambda **kwargs: pd.DataFrame())
    monkeypatch.setattr(
        risk_engine,
        "load_point_in_time_context",
        lambda *args, **kwargs: {
            "adj_close": 100.0,
            "dma_20": 95.0,
            "dma_50": 90.0,
            "dma_200": 80.0,
            "atr_20": 3.0,
            "avg_traded_value_20d": 100_000_000.0,
            "macro_sizing_multiplier": 0.50,
        },
    )
    out = risk_engine.build_allocations(include_allocated=True)
    assert len(out) == 1
    assert out.iloc[0]["allocation_status"] == "allocated"
    assert out.iloc[0]["suggested_allocation_inr"] == 42000.0
    assert "Macro sizing multiplier applied" in out.iloc[0]["notes"]


def test_risk_engine_exchange_distribution_reduces_allocated_size(monkeypatch):
    evaluations = pd.DataFrame(
        [
            {
                "published_on": pd.Timestamp("2026-04-01T09:00:00Z"),
                "asof_date": pd.Timestamp("2026-04-01T00:00:00Z"),
                "setup_id": "LARGECAP_BREAKOUT_V1",
                "setup_name": "Largecap",
                "symbol": "HDFCBANK",
                "company_master_id": "nse:HDFCBANK",
                "unique_id": "u1",
                "evaluation_status": "completed",
                "verdict": "continue",
                "investable_now": True,
                "materiality": "high",
                "setup_effect": "strengthens",
                "sentiment": "positive",
                "event_class": "ORDER_WIN",
                "state_transition_hint": "",
                "score_impact": 0.0,
                "confidence": 0.9,
                "governance_risk": "none",
                "balance_sheet_risk": "none",
                "execution_risk": "none",
                "review_action": "",
                "review_veto": False,
            }
        ]
    )
    monkeypatch.setattr(risk_engine, "load_event_evaluations", lambda **kwargs: evaluations)
    monkeypatch.setattr(risk_engine, "load_base_candidate_fallbacks", lambda **kwargs: pd.DataFrame())
    monkeypatch.setattr(risk_engine, "load_watch_states", lambda **kwargs: pd.DataFrame())
    monkeypatch.setattr(
        risk_engine,
        "load_point_in_time_context",
        lambda *args, **kwargs: {
            "adj_close": 100.0,
            "dma_20": 95.0,
            "dma_50": 90.0,
            "dma_200": 80.0,
            "atr_20": 3.0,
            "avg_traded_value_20d": 100_000_000.0,
            "exchange_distribution_score": 0.40,
            "short_selling_event_count_20d": 3,
            "insider_net_value_90d": -10_000_000.0,
        },
    )
    out = risk_engine.build_allocations(include_allocated=True)
    assert len(out) == 1
    assert out.iloc[0]["allocation_status"] == "allocated"
    assert out.iloc[0]["suggested_allocation_inr"] == 63000.0
    assert "Exchange-event risk multiplier applied" in out.iloc[0]["notes"]


def test_risk_engine_point_in_time_context_excludes_same_day_daily_rows(monkeypatch):
    calls: list[dict[str, object]] = []

    def fake_sql_to_df(query, params=None, **kwargs):
        calls.append({"query": query, "params": params})
        if "advisory_technical_daily" in query:
            return pd.DataFrame(
                [
                    {
                        "technical_asof_date": pd.Timestamp("2026-04-14T00:00:00Z"),
                        "adj_close": 100.0,
                        "dma_20": 98.0,
                        "dma_50": 95.0,
                        "dma_200": 90.0,
                        "atr_20": 2.0,
                        "avg_traded_value_20d": 1000000.0,
                        "rs_vs_benchmark": 0.1,
                        "rs_vs_sector": 0.2,
                        "breakout_extension_pct": 0.03,
                        "fundamentals_asof_date": pd.Timestamp("2026-04-13T00:00:00Z"),
                        "debt_to_equity": 0.4,
                        "debt_to_equity_vs_sector": -0.1,
                    }
                ]
            )
        return pd.DataFrame()

    monkeypatch.setattr(risk_engine, "sql_to_df", fake_sql_to_df)
    monkeypatch.setattr(risk_engine, "load_macro_context", lambda cutoff: {"macro_asof_date": cutoff.isoformat()})
    monkeypatch.setattr(risk_engine, "load_exchange_feature_context", lambda symbol, cutoff: {"exchange_asof_date": cutoff.isoformat()})

    published_on = pd.Timestamp("2026-04-15T10:30:00Z")
    out = risk_engine.load_point_in_time_context("HDFCBANK", published_on)

    assert out["technical_asof_date"] == "2026-04-14T00:00:00+00:00"
    assert calls[0]["params"]["daily_cutoff"] == pd.Timestamp("2026-04-15T00:00:00Z")


def test_adversarial_review_penalizes_exchange_distribution_against_positive_event():
    row = pd.Series(
        {
            "published_on": pd.Timestamp("2026-03-28T00:00:00Z"),
            "asof_date": pd.Timestamp("2026-04-01T00:00:00Z"),
            "event_class": "ORDER_WIN",
            "verdict": "continue",
            "setup_effect": "strengthens",
            "state_transition_hint": "RAISE_SCORE_ONLY",
            "investable_now": True,
            "materiality": "high",
            "source_reliability": "high",
            "expected_decay_days": 20,
            "contradiction": 0.1,
            "confidence": 0.8,
            "novelty": 0.7,
            "surprise": 0.7,
            "score_impact": 0.25,
            "exchange_distribution_score": 0.45,
            "insider_net_value_90d": -5_000_000.0,
            "short_selling_event_count_20d": 3,
        }
    )
    review = adversarial_review.review_event_row(row)
    flags = json.loads(review["review_flags_json"])
    assert review["review_action"] == "penalize"
    assert "exchange_distribution_contradicts_positive_event" in flags


def test_pipeline_review_stage_sits_between_evaluate_and_risk():
    assert pipeline.stage_enabled("review", start_at="evaluate", stop_at="review")
    assert not pipeline.stage_enabled("risk", start_at="evaluate", stop_at="review")


def test_pipeline_event_model_stage_sits_between_evaluate_and_review():
    assert pipeline.stage_enabled("event_model", start_at="evaluate", stop_at="event_model")
    assert not pipeline.stage_enabled("review", start_at="evaluate", stop_at="event_model")


def test_adversarial_review_flags_stale_contradictory_event_as_veto():
    row = pd.Series(
        {
            "published_on": pd.Timestamp("2026-03-20T00:00:00Z"),
            "asof_date": pd.Timestamp("2026-04-01T00:00:00Z"),
            "event_class": "ORDER_WIN",
            "verdict": "continue",
            "setup_effect": "strengthens",
            "state_transition_hint": "UPGRADE_TO_PASS_NOW",
            "investable_now": True,
            "materiality": "high",
            "source_reliability": "medium",
            "expected_decay_days": 5,
            "contradiction": 0.8,
            "confidence": 0.7,
            "novelty": 0.7,
            "surprise": 0.8,
            "score_impact": 0.3,
        }
    )
    review = adversarial_review.review_event_row(row)
    assert review["review_action"] == "veto"
    assert review["veto"] is True
    assert "high_contradiction" in json.loads(review["review_flags_json"])


def test_adversarial_review_penalizes_low_model_edge():
    row = pd.Series(
        {
            "published_on": pd.Timestamp("2026-03-28T00:00:00Z"),
            "asof_date": pd.Timestamp("2026-04-01T00:00:00Z"),
            "event_class": "ORDER_WIN",
            "verdict": "continue",
            "setup_effect": "strengthens",
            "state_transition_hint": "RAISE_SCORE_ONLY",
            "investable_now": True,
            "materiality": "medium",
            "source_reliability": "high",
            "expected_decay_days": 30,
            "contradiction": 0.1,
            "confidence": 0.8,
            "novelty": 0.7,
            "surprise": 0.7,
            "score_impact": 0.25,
            "event_meta_score": 0.18,
        }
    )
    review = adversarial_review.review_event_row(row)
    assert review["review_action"] in {"penalize", "review_manual", "veto"}
    assert "low_model_edge" in json.loads(review["review_flags_json"])


def test_event_meta_model_builds_directional_label_from_future_returns(monkeypatch):
    monkeypatch.setattr(
        event_meta_model,
        "load_event_rows",
        lambda **kwargs: pd.DataFrame(
            [
                {
                    "published_on": pd.Timestamp("2026-03-20T10:00:00Z"),
                    "asof_date": pd.Timestamp("2026-03-20T00:00:00Z"),
                    "setup_id": "EVENT_OPPORTUNITY_V1",
                    "symbol": "ABC",
                    "unique_id": "u1",
                    "event_source": "announcement",
                    "sentiment": "positive",
                    "materiality": "high",
                    "setup_effect": "strengthens",
                    "direction": "positive",
                    "surprise": 0.8,
                    "novelty": 0.7,
                    "contradiction": 0.1,
                    "expected_decay_days": 20,
                    "source_reliability": "high",
                    "governance_risk": "none",
                    "balance_sheet_risk": "none",
                    "execution_risk": "low",
                    "investable_now": True,
                    "verdict": "continue",
                    "event_class": "ORDER_WIN",
                    "state_transition_hint": "RAISE_SCORE_ONLY",
                    "score_impact": 0.2,
                    "confidence": 0.8,
                }
            ]
        ),
    )
    monkeypatch.setattr(
        event_meta_model,
        "load_price_history",
        lambda symbols, start_date, end_date: pd.DataFrame(
            [
                {"symbol": "ABC", "date": pd.Timestamp("2026-03-21T00:00:00Z"), "close": 100.0},
                {"symbol": "ABC", "date": pd.Timestamp("2026-03-24T00:00:00Z"), "close": 101.0},
                {"symbol": "ABC", "date": pd.Timestamp("2026-03-25T00:00:00Z"), "close": 102.0},
                {"symbol": "ABC", "date": pd.Timestamp("2026-03-26T00:00:00Z"), "close": 103.0},
                {"symbol": "ABC", "date": pd.Timestamp("2026-03-27T00:00:00Z"), "close": 104.0},
                {"symbol": "ABC", "date": pd.Timestamp("2026-03-28T00:00:00Z"), "close": 105.0},
                {"symbol": "ABC", "date": pd.Timestamp("2026-03-31T00:00:00Z"), "close": 106.0},
                {"symbol": "ABC", "date": pd.Timestamp("2026-04-01T00:00:00Z"), "close": 107.0},
                {"symbol": "ABC", "date": pd.Timestamp("2026-04-02T00:00:00Z"), "close": 108.0},
                {"symbol": "ABC", "date": pd.Timestamp("2026-04-03T00:00:00Z"), "close": 109.0},
                {"symbol": "ABC", "date": pd.Timestamp("2026-04-04T00:00:00Z"), "close": 110.0},
                {"symbol": "ABC", "date": pd.Timestamp("2026-04-07T00:00:00Z"), "close": 111.0},
            ]
        ),
    )
    dataset = event_meta_model.build_labeled_event_dataset(horizon_days=10, return_threshold=0.02)
    row = dataset.iloc[0]
    assert row["direction_sign"] == 1
    assert row["anchor_date"] == pd.Timestamp("2026-03-21T00:00:00Z")
    assert row["directional_return_10d"] > 0.02
    assert int(row["target_label"]) == 1


def test_event_model_data_prep_summarizes_label_coverage(monkeypatch):
    def fake_build_labeled_event_dataset(*, horizon_days, **kwargs):
        if horizon_days == 1:
            return pd.DataFrame(
                [
                    {"target_label": 1},
                    {"target_label": 0},
                    {"target_label": pd.NA},
                ]
            )
        if horizon_days == 3:
            return pd.DataFrame(
                [
                    {"target_label": pd.NA},
                    {"target_label": pd.NA},
                ]
            )
        return pd.DataFrame()

    monkeypatch.setattr(
        event_model_data_prep,
        "build_labeled_event_dataset",
        fake_build_labeled_event_dataset,
    )

    summary = event_model_data_prep.summarize_label_coverage(horizons=[1, 3, 5], min_labeled_rows=2)
    coverage = {item["horizon_days"]: item for item in summary["coverage"]}

    assert coverage[1]["dataset_rows"] == 3
    assert coverage[1]["labeled_rows"] == 2
    assert coverage[1]["train_ready"] is True
    assert coverage[3]["dataset_rows"] == 2
    assert coverage[3]["labeled_rows"] == 0
    assert coverage[3]["train_ready"] is False
    assert coverage[5]["dataset_rows"] == 0
    assert coverage[5]["labeled_rows"] == 0
    assert summary["any_train_ready"] is True


def test_event_meta_model_joins_anchor_day_intraday_features(monkeypatch):
    monkeypatch.setattr(
        event_meta_model,
        "load_event_rows",
        lambda **kwargs: pd.DataFrame(
            [
                {
                    "published_on": pd.Timestamp("2026-03-20T10:00:00Z"),
                    "asof_date": pd.Timestamp("2026-03-20T00:00:00Z"),
                    "setup_id": "EVENT_OPPORTUNITY_V1",
                    "symbol": "ABC",
                    "unique_id": "u1",
                    "event_source": "announcement",
                    "sentiment": "positive",
                    "materiality": "high",
                    "setup_effect": "strengthens",
                    "direction": "positive",
                    "surprise": 0.8,
                    "novelty": 0.7,
                    "contradiction": 0.1,
                    "expected_decay_days": 20,
                    "source_reliability": "high",
                    "governance_risk": "none",
                    "balance_sheet_risk": "none",
                    "execution_risk": "low",
                    "investable_now": True,
                    "verdict": "continue",
                    "event_class": "ORDER_WIN",
                    "state_transition_hint": "RAISE_SCORE_ONLY",
                    "score_impact": 0.2,
                    "confidence": 0.8,
                }
            ]
        ),
    )
    monkeypatch.setattr(
        event_meta_model,
        "load_price_history",
        lambda symbols, start_date, end_date: pd.DataFrame(
            [
                {"symbol": "ABC", "date": pd.Timestamp("2026-03-21T00:00:00Z"), "close": 100.0},
                {"symbol": "ABC", "date": pd.Timestamp("2026-03-24T00:00:00Z"), "close": 103.0},
            ]
        ),
    )
    monkeypatch.setattr(
        event_meta_model,
        "load_intraday_event_features",
        lambda symbols, start_date, end_date: pd.DataFrame(
            [
                {
                    "symbol": "ABC",
                    "asof_date": pd.Timestamp("2026-03-21T00:00:00Z"),
                    "intraday_close_vs_vwap_pct": 0.6,
                    "intraday_pct_bars_above_vwap": 0.72,
                    "intraday_close_location_pct": 0.81,
                    "intraday_opening_range_breakout_up": True,
                    "intraday_prev_day_breakout_up": True,
                    "intraday_failed_prev_day_breakout": False,
                    "intraday_first_30m_return_pct": 0.4,
                    "intraday_last_60m_return_pct": 0.5,
                    "intraday_volume_vs_20d": 1.4,
                    "intraday_breakout_score": 0.88,
                    "intraday_pattern_label": "BREAKOUT_CONFIRMATION",
                }
            ]
        ),
    )

    dataset = event_meta_model.build_labeled_event_dataset(horizon_days=1, return_threshold=0.02)
    row = dataset.iloc[0]
    assert row["anchor_date"] == pd.Timestamp("2026-03-21T00:00:00Z")
    assert float(row["intraday_close_vs_vwap_pct"]) == 0.6
    assert float(row["intraday_breakout_score"]) == 0.88
    assert row["intraday_pattern_label"] == "BREAKOUT_CONFIRMATION"


def test_training_universe_normalizes_ad_hoc_payload(monkeypatch):
    monkeypatch.setattr(
        training_universe,
        "load_training_universe_config",
        lambda config_path=None: [
            {
                "screener_slug": "train-largecap-liquid",
                "screener_name": "TRAIN_LARGECAP_LIQUID",
                "query_name": "Train Largecap Liquid",
                "query_text": "Market capitalization > 20000",
            }
        ],
    )
    monkeypatch.setattr(
        training_universe,
        "fetch_ad_hoc_payload",
        lambda **kwargs: {
            "screener_url": "https://www.screener.in/screen/raw/?query=x",
            "companies": [
                {
                    "s_no": 1,
                    "name": "ABC Ltd",
                    "ticker": "ABC",
                    "page_slug": "ABC",
                    "metrics": {"cmp_rs": 123.4, "mar_cap_rscr": 25000.0, "p_e": 18.2},
                }
            ],
        },
    )
    monkeypatch.setattr(
        training_universe,
        "resolve_company_master",
        lambda df: df.assign(company_master_id="nse:ABC", ticker="ABC", exchange="NSE"),
    )

    summary = training_universe.sync_training_universes(
        asof_date=pd.Timestamp("2026-04-07T00:00:00Z"),
        dry_run=True,
    )

    assert summary["screener_count"] == 1
    assert summary["row_count"] == 0
    assert summary["results"][0]["screener_slug"] == "train-largecap-liquid"
    assert summary["results"][0]["status"] == "planned"


def test_rule_engine_excludes_research_only_setups_by_default(monkeypatch):
    monkeypatch.setattr(
        rule_engine,
        "get_effective_dates",
        lambda asof_date=None: {"screener_date": pd.Timestamp("2026-04-07T00:00:00Z")},
    )
    monkeypatch.setattr(
        rule_engine,
        "load_regime",
        lambda asof_date: {"regime_name": "STABLE", "asof_date": pd.Timestamp("2026-04-07T00:00:00Z")},
    )
    monkeypatch.setattr(
        rule_engine,
        "load_setup_registry",
        lambda config_path=None: [
            {
                "setup_id": "EVENT_MODEL_TRAINING_V1",
                "setup_name": "Training",
                "screeners": ["train-largecap-liquid"],
                "screener_mode": "union",
                "overlay_screeners": {},
                "research_only": True,
            }
        ],
    )
    monkeypatch.setattr(rule_engine, "load_technical", lambda asof_date: pd.DataFrame())
    monkeypatch.setattr(rule_engine, "load_intraday", lambda asof_date: pd.DataFrame())
    monkeypatch.setattr(rule_engine, "load_fundamentals", lambda asof_date: pd.DataFrame())
    monkeypatch.setattr(rule_engine, "load_overlay", lambda *args, **kwargs: {"overlay_name": "NONE", "asof_date": pd.Timestamp("2026-04-07T00:00:00Z")})
    monkeypatch.setattr(rule_engine, "load_active_theme_screener_mapping", lambda **kwargs: {"theme_ids": [], "screener_slugs": []})
    monkeypatch.setattr(rule_engine, "load_screener_universe", lambda *args, **kwargs: pd.DataFrame())

    candidates, rejections, meta = rule_engine.run_rule_engine(asof_date=pd.Timestamp("2026-04-07T00:00:00Z"))
    assert candidates.empty
    assert rejections.empty

    _, rejections_selected, _ = rule_engine.run_rule_engine(
        asof_date=pd.Timestamp("2026-04-07T00:00:00Z"),
        setup_ids=["EVENT_MODEL_TRAINING_V1"],
    )
    assert not rejections_selected.empty
    assert "missing_screener_universe" in set(rejections_selected["reason_code"].astype(str))


def test_model_training_runner_horizon_ready():
    prep_summary = {
        "label_coverage": {
            "coverage": [
                {"horizon_days": 1, "train_ready": True},
                {"horizon_days": 3, "train_ready": False},
            ]
        }
    }
    assert model_training_runner._horizon_ready(prep_summary, 1) is True
    assert model_training_runner._horizon_ready(prep_summary, 3) is False
    assert model_training_runner._horizon_ready(prep_summary, 5) is False


def test_event_model_promotion_check_passes_for_strong_evidence(monkeypatch):
    metadata = {
        "model_name": "xgboost_event_meta_model",
        "model_version": "event_meta_model_h1",
        "horizon_days": 1,
        "return_threshold": 0.02,
        "metrics": {
            "train_rows": 120,
            "test_rows": 35,
            "precision": 0.72,
            "positive_rate_test": 0.45,
            "roc_auc": 0.64,
        },
    }
    monkeypatch.setattr(
        event_model_promotion_check,
        "load_artifact_metadata",
        lambda artifact_dir, model_basename: (metadata, {"model_exists": True, "meta_exists": True}),
    )
    monkeypatch.setattr(
        event_model_promotion_check,
        "summarize_label_coverage",
        lambda **kwargs: {
            "horizon_days": 1,
            "dataset_rows": 180,
            "labeled_rows": 150,
            "date_count": 30,
            "symbol_count": 45,
            "event_class_count": 8,
            "positive_rate": 0.48,
        },
    )
    monkeypatch.setattr(
        event_model_promotion_check,
        "summarize_successful_runs",
        lambda *args, **kwargs: {
            "successful_runs": 4,
            "since_days": 35,
            "latest_status": "done",
            "latest_timestamp": "2026-06-01T00:00:00+00:00",
        },
    )
    monkeypatch.setattr(
        event_model_promotion_check,
        "summarize_score_freshness",
        lambda **kwargs: {"score_rows": 40, "latest_scored_at": "2026-06-01T00:00:00+00:00", "fresh": True},
    )

    payload = event_model_promotion_check.build_promotion_check(
        argparse.Namespace(
            artifact_dir=".cache/advisory_event_meta_model",
            model_basename="event_meta_model",
            horizon_days=1,
            return_threshold=0.02,
            log_path="logs/cron/all_ml.log",
            run_window_days=35,
            max_score_age_days=14,
            min_successful_runs=3,
            min_train_rows=80,
            min_test_rows=20,
            min_labeled_rows=100,
            min_dates=20,
            min_symbols=25,
            min_event_classes=4,
            min_score_rows=10,
            min_precision=0.55,
            min_precision_lift=0.10,
            min_roc_auc=0.55,
        )
    )

    assert payload["decision"] == "review_candidate"
    assert payload["ready_for_operator_review"] is True
    assert payload["failed_gates"] == []


def test_event_model_promotion_check_holds_when_gates_fail(monkeypatch):
    metadata = {
        "model_name": "xgboost_event_meta_model",
        "model_version": "event_meta_model_h1",
        "horizon_days": 1,
        "return_threshold": 0.02,
        "metrics": {
            "train_rows": 50,
            "test_rows": 8,
            "precision": 0.50,
            "positive_rate_test": 0.48,
            "roc_auc": 0.51,
        },
    }
    monkeypatch.setattr(
        event_model_promotion_check,
        "load_artifact_metadata",
        lambda artifact_dir, model_basename: (metadata, {"model_exists": True, "meta_exists": True}),
    )
    monkeypatch.setattr(
        event_model_promotion_check,
        "summarize_label_coverage",
        lambda **kwargs: {
            "horizon_days": 1,
            "dataset_rows": 60,
            "labeled_rows": 58,
            "date_count": 8,
            "symbol_count": 12,
            "event_class_count": 2,
            "positive_rate": 0.48,
        },
    )
    monkeypatch.setattr(
        event_model_promotion_check,
        "summarize_successful_runs",
        lambda *args, **kwargs: {"successful_runs": 1, "since_days": 35, "latest_status": "failed"},
    )
    monkeypatch.setattr(
        event_model_promotion_check,
        "summarize_score_freshness",
        lambda **kwargs: {"score_rows": 0, "latest_scored_at": None, "fresh": False},
    )

    payload = event_model_promotion_check.build_promotion_check(
        argparse.Namespace(
            artifact_dir=".cache/advisory_event_meta_model",
            model_basename="event_meta_model",
            horizon_days=1,
            return_threshold=0.02,
            log_path="logs/cron/all_ml.log",
            run_window_days=35,
            max_score_age_days=14,
            min_successful_runs=3,
            min_train_rows=80,
            min_test_rows=20,
            min_labeled_rows=100,
            min_dates=20,
            min_symbols=25,
            min_event_classes=4,
            min_score_rows=10,
            min_precision=0.55,
            min_precision_lift=0.10,
            min_roc_auc=0.55,
        )
    )

    assert payload["decision"] == "hold_research_only"
    assert payload["ready_for_operator_review"] is False
    assert "precision_lift_vs_positive_rate_test" in payload["failed_gates"]
    assert "latest_ml_run_not_failed" in payload["failed_gates"]


def test_event_model_artifact_store_builds_manifest(tmp_path):
    artifact_dir = tmp_path / "model"
    artifact_dir.mkdir()
    (artifact_dir / "event_meta_model.json").write_text("model-bytes", encoding="utf-8")
    (artifact_dir / "event_meta_model.meta.json").write_text(
        json.dumps(
            {
                "model_name": "xgboost_event_meta_model",
                "model_version": "event_meta_model_h1",
                "horizon_days": 1,
                "return_threshold": 0.02,
                "metrics": {"precision": 0.7},
            }
        ),
        encoding="utf-8",
    )

    manifest = event_model_artifact_store.build_artifact_manifest(
        artifact_dir=artifact_dir,
        model_basename="event_meta_model",
        s3_prefix="models/test",
    )

    assert manifest["model_version"] == "event_meta_model_h1"
    assert manifest["version_prefix"].startswith("models/test/event_meta_model/event_meta_model_h1/")
    assert manifest["latest_prefix"] == "models/test/event_meta_model/latest"
    assert {item["role"] for item in manifest["files"]} == {"model", "metadata"}
    assert all(item["sha256"] for item in manifest["files"])


def test_event_model_artifact_store_uploads_versioned_and_latest(tmp_path, monkeypatch):
    artifact_dir = tmp_path / "model"
    artifact_dir.mkdir()
    model_path = artifact_dir / "event_meta_model.json"
    meta_path = artifact_dir / "event_meta_model.meta.json"
    model_path.write_text("model-bytes", encoding="utf-8")
    meta_path.write_text(
        json.dumps(
            {
                "model_name": "xgboost_event_meta_model",
                "model_version": "event_meta_model_h1",
                "horizon_days": 1,
                "return_threshold": 0.02,
                "metrics": {"precision": 0.7},
            }
        ),
        encoding="utf-8",
    )
    uploaded_files = []
    uploaded_content = []
    fake_store = types.SimpleNamespace(
        save_file=lambda path, key: uploaded_files.append((str(path), key)),
        save_file_content=lambda key, content: uploaded_content.append((key, content)),
    )
    monkeypatch.setitem(sys.modules, "utils.store", fake_store)

    result = event_model_artifact_store.publish_event_model_artifacts(
        artifact_dir=artifact_dir,
        model_basename="event_meta_model",
        s3_prefix="models/test",
    )

    assert result["status"] == "uploaded"
    assert len(uploaded_files) == 4
    assert len(uploaded_content) == 2
    uploaded_keys = [key for _, key in uploaded_files] + [key for key, _ in uploaded_content]
    assert "models/test/event_meta_model/latest/event_meta_model.json" in uploaded_keys
    assert "models/test/event_meta_model/latest/event_meta_model.meta.json" in uploaded_keys
    assert "models/test/event_meta_model/latest/manifest.json" in uploaded_keys


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


def test_risk_engine_persist_allocations_normalizes_review_veto(monkeypatch):
    captured: dict[str, pd.DataFrame] = {}
    monkeypatch.setattr(risk_engine, "ensure_allocations_table", lambda: None)

    class DummyCursor:
        def execute(self, *args, **kwargs):
            return None

    class DummySession:
        def __enter__(self):
            return (None, DummyCursor())

        def __exit__(self, exc_type, exc, tb):
            return False

    monkeypatch.setattr(risk_engine, "db_session", lambda *args, **kwargs: DummySession())

    def fake_upsert(df, table_name, unique_keys=None, timescaledb_column=None):
        captured["df"] = df.copy()

    monkeypatch.setattr(risk_engine, "upsert_to_db", fake_upsert)
    risk_engine.persist_allocations(
        pd.DataFrame(
            [
                {
                    "published_on": pd.Timestamp("2026-04-07T09:00:00Z"),
                    "asof_date": pd.Timestamp("2026-04-07T00:00:00Z"),
                    "setup_id": "TEST",
                    "symbol": "ABC",
                    "unique_id": "u1",
                    "review_veto": "False",
                    "review_score": "-0.25",
                    "investable_now": "True",
                }
            ]
        )
    )
    out = captured["df"]
    assert out["review_veto"].dtype == bool
    assert out["investable_now"].dtype == bool
    assert pd.api.types.is_float_dtype(out["review_score"])


def test_position_lifecycle_uses_published_day_for_entry_price(monkeypatch):
    monkeypatch.setattr(
        position_lifecycle,
        "load_price_points",
        lambda symbols, monitor_date: pd.DataFrame(
            [
                {"symbol": "ABC", "date": pd.Timestamp("2026-04-07T00:00:00Z"), "close": 101.0},
                {"symbol": "ABC", "date": pd.Timestamp("2026-04-08T00:00:00Z"), "close": 103.0},
            ]
        ),
    )
    orders = pd.DataFrame(
        [
            {
                "symbol": "ABC",
                "published_on": pd.Timestamp("2026-04-07T15:30:00Z"),
            }
        ]
    )
    derived = position_lifecycle.derive_entry_and_current_prices(
        orders,
        pd.Timestamp("2026-04-08T00:00:00Z"),
    )
    row = derived.iloc[0]
    assert row["entry_date"] == pd.Timestamp("2026-04-07T00:00:00Z")
    assert float(row["entry_price"]) == 101.0
    assert float(row["current_price"]) == 103.0


def test_position_lifecycle_load_price_points_uses_resolved_bse_identity(monkeypatch):
    monkeypatch.setattr(
        position_lifecycle,
        "resolve_price_identities",
        lambda symbols: pd.DataFrame(
            [
                {
                    "symbol": "AAYUSHBULL",
                    "exchange": "BSE",
                    "security_id": 540718,
                    "resolved_ticker": "540718",
                }
            ]
        ),
    )
    monkeypatch.setattr(
        position_lifecycle,
        "sql_to_df",
        lambda query, params=None: pd.DataFrame(
            [
                {
                    "exchange": "BSE",
                    "security_id": 540718,
                    "ticker": "540718",
                    "date": pd.Timestamp("2026-04-07T00:00:00Z"),
                    "close": 12.5,
                }
            ]
        ),
    )
    prices = position_lifecycle.load_price_points(
        ["AAYUSHBULL"],
        pd.Timestamp("2026-04-07T00:00:00Z"),
    )
    assert len(prices) == 1
    row = prices.iloc[0]
    assert row["symbol"] == "AAYUSHBULL"
    assert row["exchange"] == "BSE"
    assert row["ticker"] == "540718"


def test_wpi_expected_month_count_for_current_year():
    assert wpi.expected_month_count_for_year(2026, today=date(2026, 4, 7)) == 3
    assert wpi.expected_month_count_for_year(2025, today=date(2026, 4, 7)) == 12
    assert wpi.expected_month_count_for_year(2027, today=date(2026, 4, 7)) == 0


def test_wpi_load_completed_items_uses_expected_month_count(monkeypatch):
    monkeypatch.setattr(
        wpi,
        "sql_to_df",
        lambda query, params=None: pd.DataFrame([{"cname": "A"}, {"cname": "B"}]),
    )
    completed = wpi.load_completed_items(2026, expected_months=3)
    assert completed == {"A", "B"}


def test_risk_engine_keeps_investable_review_manual_as_allocated(monkeypatch):
    monkeypatch.setattr(risk_engine, "load_base_candidate_fallbacks", lambda **kwargs: pd.DataFrame())
    monkeypatch.setattr(
        risk_engine,
        "load_event_evaluations",
        lambda **kwargs: pd.DataFrame(
            [
                {
                    "published_on": pd.Timestamp("2026-03-24T00:00:00Z"),
                    "asof_date": pd.Timestamp("2026-03-20T00:00:00Z"),
                    "setup_id": "DEFENSIVE_REGIME_POSITION_V1",
                    "setup_name": "Defensive regime position",
                    "symbol": "ABC",
                    "company_master_id": "nse:ABC",
                    "unique_id": "ABC-1",
                    "evaluation_status": "completed",
                    "verdict": "review_manual",
                    "investable_now": True,
                    "materiality": "low",
                    "setup_effect": "neutral",
                    "event_class": "OTHER",
                    "state_transition_hint": "REVIEW_MANUAL",
                    "score_impact": 0.0,
                    "confidence": 0.8,
                    "sentiment": "neutral",
                    "governance_risk": "none",
                }
            ]
        ),
    )
    monkeypatch.setattr(
        risk_engine,
        "load_point_in_time_context",
        lambda symbol, published_on: {
            "avg_traded_value_20d": 1_000_000_000.0,
            "adj_close": 100.0,
            "atr_20": 5.0,
            "dma_20": 98.0,
            "dma_50": 95.0,
            "dma_200": 90.0,
            "rs_vs_benchmark": 0.1,
            "rs_vs_sector": 0.1,
        },
    )
    monkeypatch.setattr(
        risk_engine,
        "load_watch_states",
        lambda **kwargs: pd.DataFrame(
            [
                {
                    "asof_date": pd.Timestamp("2026-03-20T00:00:00Z"),
                    "setup_id": "DEFENSIVE_REGIME_POSITION_V1",
                    "symbol": "ABC",
                    "candidate_state": "WATCH_EVENT",
                    "current_state": "WATCH_EVENT",
                    "watch_status": "active",
                }
            ]
        ),
    )

    df = risk_engine.build_allocations(asof_date=pd.Timestamp("2026-03-20T00:00:00Z"))
    row = df.iloc[0]
    assert row["allocation_status"] == "allocated"
    assert row["suggested_allocation_inr"] > 0
    assert "manual review" in str(row["notes"]).lower()


def test_risk_engine_rejects_allocation_when_adversarial_review_vetoes(monkeypatch):
    monkeypatch.setattr(risk_engine, "load_base_candidate_fallbacks", lambda **kwargs: pd.DataFrame())
    monkeypatch.setattr(
        risk_engine,
        "load_event_evaluations",
        lambda **kwargs: pd.DataFrame(
            [
                {
                    "published_on": pd.Timestamp("2026-03-24T00:00:00Z"),
                    "asof_date": pd.Timestamp("2026-03-20T00:00:00Z"),
                    "setup_id": "DEFENSIVE_REGIME_POSITION_V1",
                    "setup_name": "Defensive regime position",
                    "symbol": "ABC",
                    "company_master_id": "nse:ABC",
                    "unique_id": "ABC-1",
                    "evaluation_status": "completed",
                    "verdict": "continue",
                    "investable_now": True,
                    "materiality": "high",
                    "setup_effect": "strengthens",
                    "event_class": "ORDER_WIN",
                    "state_transition_hint": "UPGRADE_TO_PASS_NOW",
                    "score_impact": 0.3,
                    "confidence": 0.8,
                    "sentiment": "positive",
                    "governance_risk": "none",
                    "balance_sheet_risk": "none",
                    "execution_risk": "low",
                    "review_action": "veto",
                    "review_score": -0.6,
                    "review_veto": True,
                    "review_reason": "high_contradiction",
                }
            ]
        ),
    )
    monkeypatch.setattr(
        risk_engine,
        "load_point_in_time_context",
        lambda symbol, published_on: {
            "avg_traded_value_20d": 1_000_000_000.0,
            "adj_close": 100.0,
            "atr_20": 5.0,
            "dma_20": 98.0,
            "dma_50": 95.0,
            "dma_200": 90.0,
            "rs_vs_benchmark": 0.1,
            "rs_vs_sector": 0.1,
        },
    )
    monkeypatch.setattr(
        risk_engine,
        "load_watch_states",
        lambda **kwargs: pd.DataFrame(
            [
                {
                    "asof_date": pd.Timestamp("2026-03-20T00:00:00Z"),
                    "setup_id": "DEFENSIVE_REGIME_POSITION_V1",
                    "symbol": "ABC",
                    "candidate_state": "PASS_NOW",
                    "current_state": "PASS_NOW",
                    "watch_status": "active",
                }
            ]
        ),
    )

    df = risk_engine.build_allocations(asof_date=pd.Timestamp("2026-03-20T00:00:00Z"))
    row = df.iloc[0]
    assert row["allocation_status"] == "rejected"
    assert row["suggested_allocation_inr"] == 0.0
    assert "vetoed" in str(row["notes"]).lower()


def test_risk_engine_does_not_treat_missing_review_veto_as_true(monkeypatch):
    monkeypatch.setattr(risk_engine, "load_base_candidate_fallbacks", lambda **kwargs: pd.DataFrame())
    monkeypatch.setattr(
        risk_engine,
        "load_event_evaluations",
        lambda **kwargs: pd.DataFrame(
            [
                {
                    "published_on": pd.Timestamp("2026-03-24T00:00:00Z"),
                    "asof_date": pd.Timestamp("2026-03-20T00:00:00Z"),
                    "setup_id": "DEFENSIVE_REGIME_POSITION_V1",
                    "setup_name": "Defensive regime position",
                    "symbol": "ABC",
                    "company_master_id": "nse:ABC",
                    "unique_id": "ABC-1",
                    "evaluation_status": "completed",
                    "verdict": "continue",
                    "investable_now": True,
                    "materiality": "high",
                    "setup_effect": "strengthens",
                    "event_class": "ORDER_WIN",
                    "state_transition_hint": "UPGRADE_TO_PASS_NOW",
                    "score_impact": 0.2,
                    "confidence": 0.8,
                    "sentiment": "positive",
                    "governance_risk": "none",
                    "balance_sheet_risk": "none",
                    "execution_risk": "low",
                    "review_action": pd.NA,
                    "review_score": pd.NA,
                    "review_veto": float("nan"),
                    "review_reason": pd.NA,
                }
            ]
        ),
    )
    monkeypatch.setattr(
        risk_engine,
        "load_point_in_time_context",
        lambda symbol, published_on: {
            "avg_traded_value_20d": 1_000_000_000.0,
            "adj_close": 100.0,
            "atr_20": 5.0,
            "dma_20": 98.0,
            "dma_50": 95.0,
            "dma_200": 90.0,
            "rs_vs_benchmark": 0.1,
            "rs_vs_sector": 0.1,
        },
    )
    monkeypatch.setattr(
        risk_engine,
        "load_watch_states",
        lambda **kwargs: pd.DataFrame(
            [
                {
                    "asof_date": pd.Timestamp("2026-03-20T00:00:00Z"),
                    "setup_id": "DEFENSIVE_REGIME_POSITION_V1",
                    "symbol": "ABC",
                    "candidate_state": "PASS_NOW",
                    "current_state": "PASS_NOW",
                    "watch_status": "active",
                }
            ]
        ),
    )

    df = risk_engine.build_allocations(asof_date=pd.Timestamp("2026-03-20T00:00:00Z"))
    row = df.iloc[0]
    assert row["allocation_status"] == "allocated"
    assert row["review_veto"] != row["review_veto"] or row["review_veto"] is pd.NA
    assert "vetoed" not in str(row["notes"]).lower()


def test_risk_engine_production_setup_ids_excludes_research_only(monkeypatch):
    monkeypatch.setattr(
        risk_engine,
        "load_setup_registry",
        lambda: [
            {"setup_id": "PROD_SETUP", "research_only": False},
            {"setup_id": "EVENT_MODEL_TRAINING_V1", "research_only": True},
        ],
    )
    assert risk_engine.production_setup_ids() == ["PROD_SETUP"]


def test_risk_engine_promoted_pass_now_overrides_conservative_event_investable_flag(monkeypatch):
    monkeypatch.setattr(risk_engine, "load_base_candidate_fallbacks", lambda **kwargs: pd.DataFrame())
    monkeypatch.setattr(
        risk_engine,
        "load_event_evaluations",
        lambda **kwargs: pd.DataFrame(
            [
                {
                    "published_on": pd.Timestamp("2026-03-24T00:00:00Z"),
                    "asof_date": pd.Timestamp("2026-03-20T00:00:00Z"),
                    "setup_id": "DEFENSIVE_REGIME_POSITION_V1",
                    "setup_name": "Defensive regime position",
                    "symbol": "ABC",
                    "company_master_id": "nse:ABC",
                    "unique_id": "ABC-2",
                    "evaluation_status": "completed",
                    "verdict": "review_manual",
                    "investable_now": False,
                    "materiality": "medium",
                    "setup_effect": "strengthens",
                    "event_class": "OTHER",
                    "state_transition_hint": "RAISE_SCORE_ONLY",
                    "score_impact": 0.2,
                    "confidence": 0.8,
                    "sentiment": "positive",
                    "governance_risk": "none",
                    "has_review_manual": True,
                }
            ]
        ),
    )
    monkeypatch.setattr(
        risk_engine,
        "load_watch_states",
        lambda **kwargs: pd.DataFrame(
            [
                {
                    "asof_date": pd.Timestamp("2026-03-20T00:00:00Z"),
                    "setup_id": "DEFENSIVE_REGIME_POSITION_V1",
                    "symbol": "ABC",
                    "candidate_state": "WATCH_EVENT",
                    "current_state": "PASS_NOW",
                    "watch_status": "active",
                }
            ]
        ),
    )
    monkeypatch.setattr(
        risk_engine,
        "load_point_in_time_context",
        lambda symbol, published_on: {
            "avg_traded_value_20d": 1_000_000_000.0,
            "adj_close": 100.0,
            "atr_20": 5.0,
            "dma_20": 98.0,
            "dma_50": 95.0,
            "dma_200": 90.0,
            "rs_vs_benchmark": 0.1,
            "rs_vs_sector": 0.1,
        },
    )

    df = risk_engine.build_allocations(asof_date=pd.Timestamp("2026-03-20T00:00:00Z"))
    row = df.iloc[0]
    assert row["allocation_status"] == "allocated"
    assert row["suggested_allocation_inr"] > 0
    assert "pass_now overrode" in str(row["notes"]).lower()


def test_risk_engine_falls_back_to_base_candidates_without_event_rows(monkeypatch):
    monkeypatch.setattr(risk_engine, "load_event_evaluations", lambda **kwargs: pd.DataFrame())
    monkeypatch.setattr(
        risk_engine,
        "load_base_candidate_fallbacks",
        lambda **kwargs: pd.DataFrame(
            [
                {
                    "published_on": pd.Timestamp("2026-04-01T00:00:00Z"),
                    "asof_date": pd.Timestamp("2026-04-01T00:00:00Z"),
                    "setup_id": "LARGECAP_BREAKOUT_POSITION_V1",
                    "setup_name": "Large cap breakout",
                    "symbol": "ABC",
                    "company_master_id": "nse:ABC",
                    "unique_id": "candidate:2026-04-01:LARGECAP_BREAKOUT_POSITION_V1:ABC",
                    "evaluation_status": "completed",
                    "verdict": "continue",
                    "investable_now": True,
                    "materiality": "low",
                    "setup_effect": "neutral",
                    "event_class": "BASE_CANDIDATE",
                    "state_transition_hint": "NO_CHANGE",
                    "score_impact": 0.0,
                    "confidence": 0.6,
                    "sentiment": "neutral",
                    "governance_risk": "none",
                    "balance_sheet_risk": "none",
                    "execution_risk": "none",
                    "candidate_state": "PASS_NOW",
                    "current_state": "PASS_NOW",
                    "watch_status": "active",
                    "is_base_candidate_fallback": True,
                }
            ]
        ),
    )
    monkeypatch.setattr(risk_engine, "load_watch_states", lambda **kwargs: pd.DataFrame())
    monkeypatch.setattr(
        risk_engine,
        "load_point_in_time_context",
        lambda symbol, published_on: {
            "avg_traded_value_20d": 1_000_000_000.0,
            "adj_close": 100.0,
            "atr_20": 5.0,
            "dma_20": 98.0,
            "dma_50": 95.0,
            "dma_200": 90.0,
            "rs_vs_benchmark": 0.1,
            "rs_vs_sector": 0.1,
        },
    )

    df = risk_engine.build_allocations(asof_date=pd.Timestamp("2026-04-01T00:00:00Z"))
    row = df.iloc[0]
    assert row["allocation_status"] == "allocated"
    assert row["event_class"] == "BASE_CANDIDATE"
    assert "base rule-engine state" in str(row["notes"]).lower()


def test_symbol_trace_builds_stage_summary(monkeypatch):
    monkeypatch.setattr(
        symbol_trace,
        "load_latest_screener_rows",
        lambda symbol, setup_id=None: pd.DataFrame([{"ticker": symbol, "screener_slug": "demo"}]),
    )
    monkeypatch.setattr(
        symbol_trace,
        "latest_single_row",
        lambda table_name, **kwargs: {
            "advisory_technical_daily": {"symbol": "HDFCBANK", "asof_date": pd.Timestamp("2026-03-23T00:00:00Z")},
            "advisory_fundamentals_daily": {"symbol": "HDFCBANK", "asof_date": pd.Timestamp("2026-03-23T00:00:00Z")},
            "advisory_candidates": None,
            "advisory_watchlist": None,
            "advisory_event_evaluations": None,
            "advisory_allocations": None,
            "advisory_portfolio_orders": None,
            "advisory_position_lifecycle": None,
            "advisory_execution_orders": None,
        }.get(table_name),
    )
    monkeypatch.setattr(symbol_trace, "load_latest_rejections", lambda symbol, setup_id=None: pd.DataFrame([{"reason_code": "regime_not_allowed"}]))
    monkeypatch.setattr(
        symbol_trace,
        "latest_rows",
        lambda table_name, **kwargs: pd.DataFrame(),
    )
    monkeypatch.setattr(symbol_trace, "load_aggregated_event_decision", lambda symbol, setup_id=None: None)

    trace = symbol_trace.build_trace("HDFCBANK")
    assert trace["stage_summary"]["in_latest_screener"] is True
    assert trace["stage_summary"]["has_technical_snapshot"] is True
    assert trace["stage_summary"]["passed_rule_engine"] is False
    assert trace["decision_summary"]["latest_rejection_reasons"] == ["regime_not_allowed"]


def test_symbol_trace_surfaces_aggregated_event_decision(monkeypatch):
    monkeypatch.setattr(symbol_trace, "load_latest_screener_rows", lambda symbol, setup_id=None: pd.DataFrame())
    monkeypatch.setattr(
        symbol_trace,
        "latest_single_row",
        lambda table_name, **kwargs: {
            "advisory_technical_daily": None,
            "advisory_fundamentals_daily": None,
            "advisory_candidates": {"setup_id": "TEST", "candidate_state": "WATCH_EVENT"},
            "advisory_watchlist": {"setup_id": "TEST", "current_state": "PASS_NOW"},
            "advisory_event_evaluations": {
                "setup_id": "TEST",
                "verdict": "review_manual",
                "event_class": "OTHER",
                "state_transition_hint": "REVIEW_MANUAL",
                "event_source": "announcement",
            },
            "advisory_allocations": {"setup_id": "TEST", "allocation_status": "allocated"},
            "advisory_portfolio_orders": {"setup_id": "TEST", "portfolio_status": "approved"},
            "advisory_position_lifecycle": None,
            "advisory_execution_orders": None,
        }.get(table_name),
    )
    monkeypatch.setattr(symbol_trace, "load_latest_rejections", lambda symbol, setup_id=None: pd.DataFrame())
    monkeypatch.setattr(symbol_trace, "latest_rows", lambda table_name, **kwargs: pd.DataFrame())
    monkeypatch.setattr(
        symbol_trace,
        "load_aggregated_event_decision",
        lambda symbol, setup_id=None: {
            "effective_event_verdict": "continue",
            "effective_event_class": "OTHER",
            "effective_state_transition_hint": "RAISE_SCORE_ONLY",
            "effective_event_score_impact": 0.15,
            "effective_investable_now": True,
            "effective_event_source": "announcement",
            "raw_event_count": 2,
        },
    )

    trace = symbol_trace.build_trace("ABC", setup_id="TEST")
    assert trace["stage_summary"]["has_aggregated_event_decision"] is True
    assert trace["decision_summary"]["latest_event_verdict"] == "review_manual"
    assert trace["decision_summary"]["effective_event_verdict"] == "continue"
    assert trace["decision_summary"]["effective_state_transition_hint"] == "RAISE_SCORE_ONLY"
    assert trace["decision_summary"]["effective_raw_event_count"] == 2


def test_setup_trace_builds_funnel_summary(monkeypatch):
    monkeypatch.setattr(setup_trace, "resolve_asof_date", lambda setup_id, requested_date=None: pd.Timestamp("2026-03-24T00:00:00Z"))
    monkeypatch.setattr(setup_trace, "load_setup_regime", lambda asof_date: {"regime_name": "RISK_OFF"})
    monkeypatch.setattr(setup_trace, "load_market_overlay", lambda asof_date: {"overlay_name": "NONE", "overlay_reason": "no overlay"})
    monkeypatch.setattr(
        setup_trace,
        "load_latest_setup_screener",
        lambda setup_id, overlay_name=None: pd.DataFrame([{"ticker": "A"}, {"ticker": "B"}, {"ticker": "C"}]),
    )

    def fake_load_setup_rows(table_name, setup_id, asof_date, limit=50):
        mapping = {
            "advisory_candidates": pd.DataFrame(
                [
                    {
                        "symbol": "A",
                        "candidate_state": "PASS_NOW",
                        "setup_score": 0.72,
                        "technical_score": 0.80,
                        "fundamental_score": 0.70,
                        "near_miss_flag": False,
                    }
                ]
            ),
            "advisory_candidate_rejections": pd.DataFrame(
                [
                    {"reason_code": "regime_not_allowed", "is_near_miss": False},
                    {"reason_code": "liquidity_far_below_min", "is_near_miss": True},
                ]
            ),
            "advisory_watchlist": pd.DataFrame([{"symbol": "A", "candidate_state": "PASS_NOW"}]),
            "advisory_watch_events": pd.DataFrame(),
            "advisory_news_events": pd.DataFrame([{"symbol": "A"}]),
            "advisory_event_evaluations": pd.DataFrame([{"symbol": "A", "verdict": "review_manual", "event_source": "economic_times_rss"}]),
            "advisory_allocations": pd.DataFrame(),
            "advisory_portfolio_orders": pd.DataFrame(),
            "advisory_position_lifecycle": pd.DataFrame(),
            "advisory_execution_orders": pd.DataFrame(),
        }
        return mapping[table_name]

    monkeypatch.setattr(setup_trace, "load_setup_rows", fake_load_setup_rows)
    monkeypatch.setattr(setup_trace, "load_top_rejection_reasons", lambda setup_id, asof_date: {"regime_not_allowed": 1, "liquidity_far_below_min": 1})

    trace = setup_trace.build_trace("LARGECAP_BREAKOUT_POSITION_V1")
    assert trace["stage_summary"]["screener_universe_count"] == 3
    assert trace["stage_summary"]["candidate_count"] == 1
    assert trace["stage_summary"]["news_event_count"] == 1
    assert trace["stage_summary"]["watch_state_counts"] == {"PASS_NOW": 1}
    assert trace["stage_summary"]["avg_setup_score"] == 0.72
    assert trace["stage_summary"]["near_miss_count"] == 1
    assert trace["funnel_summary"]["screener_to_candidate"] == round(1 / 3, 4)
    assert trace["decision_summary"]["latest_event_source"] == "economic_times_rss"


def test_dashboard_aggregates_setup_traces(monkeypatch):
    monkeypatch.setattr(
        dashboard,
        "load_setup_registry",
        lambda: [
            {"setup_id": "A", "setup_name": "Setup A", "screener_slug": "screen-a"},
            {"setup_id": "B", "setup_name": "Setup B", "screener_slug": "screen-b"},
        ],
    )
    monkeypatch.setattr(dashboard, "_resolve_dashboard_date", lambda asof_date=None: pd.Timestamp("2026-03-24T00:00:00Z"))
    monkeypatch.setattr(dashboard, "load_setup_regime", lambda asof_date: {"regime_name": "RISK_OFF"})
    monkeypatch.setattr(dashboard, "load_market_overlay", lambda asof_date: {"overlay_name": "NONE", "overlay_reason": "no overlay"})
    monkeypatch.setattr(
        dashboard,
        "resolve_setup_screeners",
        lambda setup_id, overlay_name=None, asof_date=None: ([f"screen-{setup_id.lower()}"], []),
    )
    monkeypatch.setattr(
        dashboard,
        "_load_latest_screener_universe",
        lambda active: pd.DataFrame(
            [{"screener_slug": "screen-a", "ticker": f"A{i}"} for i in range(10)]
            + [{"screener_slug": "screen-b", "ticker": f"B{i}"} for i in range(20)]
        ),
    )

    def fake_load_asof_table(table_name, asof_date, columns=None):
        if table_name == "advisory_candidates":
            return pd.DataFrame(
                [
                    {
                        "setup_id": "A",
                        "symbol": "AAA",
                        "candidate_state": "PASS_NOW",
                        "setup_score": 0.7,
                        "technical_score": 0.75,
                        "fundamental_score": 0.65,
                        "near_miss_flag": True,
                        "source_screener_slug": "screen-a",
                    },
                    {
                        "setup_id": "B",
                        "symbol": "BBB",
                        "candidate_state": "PASS_NOW",
                        "setup_score": 0.7,
                        "technical_score": 0.75,
                        "fundamental_score": 0.65,
                        "near_miss_flag": False,
                        "source_screener_slug": "screen-b",
                    },
                ]
            )
        if table_name == "advisory_candidate_rejections":
            return pd.DataFrame(
                [
                    {"setup_id": "A", "symbol": "AA2", "reason_code": "regime_not_allowed", "is_near_miss": False},
                    {"setup_id": "A", "symbol": "AA3", "reason_code": "regime_not_allowed", "is_near_miss": False},
                ]
            )
        if table_name == "advisory_watchlist":
            return pd.DataFrame([{"setup_id": "A", "symbol": "AAA", "current_state": "PASS_NOW"}])
        return pd.DataFrame(columns=columns or [])

    monkeypatch.setattr(dashboard, "_load_asof_table", fake_load_asof_table)

    df = dashboard.build_dashboard()
    assert len(df) == 2
    assert set(df["setup_id"]) == {"A", "B"}
    assert df.loc[df["setup_id"] == "A", "screener_universe_count"].iloc[0] == 10


def test_setup_registry_loads_intraday_rules():
    setup = next(item for item in setup_registry.load_setup_registry() if item["setup_id"] == "EVENT_OPPORTUNITY_V1")
    assert setup["intraday_rules"]
    assert setup["intraday_rules"][0]["column"] == "intraday_close_vs_vwap_pct"


def test_intraday_feature_builder_detects_breakout_context():
    intraday = pd.DataFrame(
        [
            {
                "company_master_id": "nse:ABC",
                "symbol": "ABC",
                "interval_minutes": 1,
                "timestamp": pd.Timestamp("2026-03-31T03:45:00Z"),
                "asof_date": pd.Timestamp("2026-03-31T00:00:00Z"),
                "open": 100.0,
                "high": 101.0,
                "low": 99.8,
                "close": 100.5,
                "volume": 1000.0,
            },
            {
                "company_master_id": "nse:ABC",
                "symbol": "ABC",
                "interval_minutes": 1,
                "timestamp": pd.Timestamp("2026-03-31T03:46:00Z"),
                "asof_date": pd.Timestamp("2026-03-31T00:00:00Z"),
                "open": 100.5,
                "high": 102.2,
                "low": 100.4,
                "close": 101.8,
                "volume": 1200.0,
            },
            {
                "company_master_id": "nse:ABC",
                "symbol": "ABC",
                "interval_minutes": 1,
                "timestamp": pd.Timestamp("2026-03-31T09:25:00Z"),
                "asof_date": pd.Timestamp("2026-03-31T00:00:00Z"),
                "open": 101.8,
                "high": 103.0,
                "low": 101.6,
                "close": 102.7,
                "volume": 1500.0,
            },
        ]
    )
    daily_reference = pd.DataFrame(
        [
            {
                "symbol": "ABC",
                "date": pd.Timestamp("2026-03-31T00:00:00Z"),
                "prev_day_high": 101.5,
                "prev_day_low": 98.0,
                "avg_daily_volume_20d": 2500.0,
            }
        ]
    )

    out = intraday_features.compute_intraday_session_features(intraday, daily_reference=daily_reference)
    assert len(out) == 1
    row = out.iloc[0]
    assert bool(row["intraday_prev_day_breakout_up"]) is True
    assert bool(row["intraday_failed_prev_day_breakout"]) is False
    assert float(row["intraday_close_vs_vwap_pct"]) > 0
    assert float(row["intraday_volume_vs_20d"]) > 1


def test_intraday_features_dedupes_daily_reference_lookup():
    asof_date = pd.Timestamp("2026-03-31T00:00:00Z")
    intraday = pd.DataFrame(
        [
            {
                "company_master_id": "nse:ABC",
                "symbol": "ABC",
                "interval_minutes": 5,
                "timestamp": pd.Timestamp("2026-03-31T03:45:00Z"),
                "asof_date": asof_date,
                "open": 100.0,
                "high": 101.0,
                "low": 99.8,
                "close": 100.5,
                "volume": 1000.0,
            },
            {
                "company_master_id": "nse:ABC",
                "symbol": "ABC",
                "interval_minutes": 5,
                "timestamp": pd.Timestamp("2026-03-31T04:00:00Z"),
                "asof_date": asof_date,
                "open": 100.5,
                "high": 103.0,
                "low": 100.4,
                "close": 102.7,
                "volume": 2000.0,
            },
        ]
    )
    daily_reference = pd.DataFrame(
        [
            {
                "symbol": "ABC",
                "date": asof_date,
                "prev_day_high": 101.5,
                "prev_day_low": 98.0,
                "avg_daily_volume_20d": 2500.0,
            },
            {
                "symbol": "ABC",
                "date": asof_date,
                "prev_day_high": 102.0,
                "prev_day_low": 98.5,
                "avg_daily_volume_20d": 3000.0,
            },
        ]
    )

    out = intraday_features.compute_intraday_session_features(intraday, daily_reference=daily_reference)

    assert len(out) == 1
    assert out.iloc[0]["intraday_prev_day_high"] == 102.0
    assert out.iloc[0]["intraday_volume_vs_20d"] == 1.0


def test_intraday_features_builds_multiple_intervals(monkeypatch):
    asof_date = pd.Timestamp("2026-04-01T00:00:00Z")
    intraday_base = pd.DataFrame(
        [
            {
                "company_master_id": "nse:ABC",
                "symbol": "ABC",
                "interval_minutes": 1,
                "timestamp": pd.Timestamp("2026-04-01T03:45:00Z"),
                "asof_date": asof_date,
                "open": 100.0,
                "high": 101.0,
                "low": 99.8,
                "close": 100.8,
                "volume": 1000.0,
            },
            {
                "company_master_id": "nse:ABC",
                "symbol": "ABC",
                "interval_minutes": 1,
                "timestamp": pd.Timestamp("2026-04-01T04:15:00Z"),
                "asof_date": asof_date,
                "open": 100.8,
                "high": 102.0,
                "low": 100.7,
                "close": 101.9,
                "volume": 1500.0,
            },
            {
                "company_master_id": "nse:ABC",
                "symbol": "ABC",
                "interval_minutes": 5,
                "timestamp": pd.Timestamp("2026-04-01T03:45:00Z"),
                "asof_date": asof_date,
                "open": 100.0,
                "high": 101.2,
                "low": 99.9,
                "close": 101.0,
                "volume": 2000.0,
            },
            {
                "company_master_id": "nse:ABC",
                "symbol": "ABC",
                "interval_minutes": 5,
                "timestamp": pd.Timestamp("2026-04-01T04:15:00Z"),
                "asof_date": asof_date,
                "open": 101.0,
                "high": 102.1,
                "low": 100.9,
                "close": 101.8,
                "volume": 2500.0,
            },
        ]
    )
    daily_reference = pd.DataFrame(
        [
            {
                "symbol": "ABC",
                "date": asof_date,
                "high": 100.5,
                "low": 98.0,
                "close": 99.5,
                "volume": 1000.0,
                "prev_day_high": 99.0,
                "prev_day_low": 97.5,
                "avg_daily_volume_20d": 1200.0,
            }
        ]
    )

    monkeypatch.setattr(intraday_features, "ensure_intraday_features_table", lambda: None)
    monkeypatch.setattr(intraday_features, "resolve_symbol_universe", lambda symbols, asof_date=None: ["ABC"])
    read_windows: list[tuple[pd.Timestamp, pd.Timestamp, int]] = []

    def fake_load_intraday_history(symbols, start_timestamp, end_timestamp, interval_minutes):
        read_windows.append((start_timestamp, end_timestamp, interval_minutes))
        return intraday_base[intraday_base["interval_minutes"] == interval_minutes].copy()

    monkeypatch.setattr(intraday_features, "load_intraday_history", fake_load_intraday_history)
    monkeypatch.setattr(intraday_features, "load_daily_reference", lambda symbols, start_date, end_date: daily_reference.copy())

    df, meta = intraday_features.build_intraday_features(
        symbols=["ABC"],
        asof_date=asof_date,
        intervals=(1, 5),
        ensure_history=False,
    )

    assert len(df) == 2
    assert sorted(df["interval_minutes"].tolist()) == [1, 5]
    assert meta["intervals"] == [1, 5]
    assert {window[0] for window in read_windows} == {asof_date}


def test_intraday_history_records_dhan_mapping_issue(monkeypatch):
    monkeypatch.setattr(intraday_features, "load_intraday_coverage", lambda symbols, interval_minutes: pd.DataFrame())

    def fake_sync_intraday_ohlcv(*args, **kwargs):
        raise ValueError("No Dhan security id mapped for NSE:HUIL")

    monkeypatch.setattr(intraday_features, "sync_intraday_ohlcv", fake_sync_intraday_ohlcv)

    results = intraday_features.ensure_intraday_history(
        ["HUIL"],
        asof_date=pd.Timestamp("2026-05-25T00:00:00Z"),
        lookback_days=1,
        intervals=(1,),
    )

    assert results == [
        {
            "symbol": "HUIL",
            "interval_minutes": 1,
            "action": "issue",
            "reason": "dhan_intraday_sync_failed",
            "error_type": "ValueError",
            "error": "No Dhan security id mapped for NSE:HUIL",
        }
    ]


def test_daily_ohlcv_repair_records_dhan_mapping_issue(monkeypatch):
    from advisory import data_sync

    monkeypatch.setattr(data_sync, "get_db_max_date", lambda *args, **kwargs: None)

    def fake_sync_daily_ohlcv(*args, **kwargs):
        raise ValueError("No Dhan security id mapped for NSE:HUIL")

    monkeypatch.setattr(data_sync, "sync_daily_ohlcv", fake_sync_daily_ohlcv)

    results = data_sync.ensure_symbol_ohlcv(
        ["HUIL"],
        to_date=pd.Timestamp("2026-05-25T00:00:00Z"),
    )

    assert results == [
        {
            "symbol": "HUIL",
            "action": "issue",
            "reason": "dhan_daily_sync_failed",
            "error_type": "ValueError",
            "error": "No Dhan security id mapped for NSE:HUIL",
        }
    ]


def test_rule_engine_intraday_confirmation_lifts_default_technical_score():
    strong_row = pd.Series(
        {
            "pass_above_dma_20": True,
            "pass_above_dma_50": True,
            "pass_above_dma_200": True,
            "rs_vs_benchmark": 0.15,
            "rs_vs_sector": 0.10,
            "intraday_close_vs_vwap_pct": 0.8,
            "intraday_pct_bars_above_vwap": 0.9,
            "intraday_close_location_pct": 0.85,
            "intraday_opening_range_breakout_up": True,
            "intraday_prev_day_breakout_up": True,
            "intraday_failed_prev_day_breakout": False,
            "intraday_volume_vs_20d": 1.6,
            "intraday_breakout_score": 0.9,
            "total_revenue_qoq_growth_vs_sector": 0.0,
            "profit_after_tax_qoq_growth_vs_sector": 0.0,
            "debt_to_equity_vs_sector": 0.1,
            "promoter_total_vs_sector": 0.0,
            "fii_vs_sector": 0.0,
        }
    )
    weak_row = strong_row.copy()
    weak_row["intraday_close_vs_vwap_pct"] = -0.4
    weak_row["intraday_pct_bars_above_vwap"] = 0.2
    weak_row["intraday_close_location_pct"] = 0.3
    weak_row["intraday_opening_range_breakout_up"] = False
    weak_row["intraday_prev_day_breakout_up"] = False
    weak_row["intraday_failed_prev_day_breakout"] = True
    weak_row["intraday_volume_vs_20d"] = 0.3
    weak_row["intraday_breakout_score"] = 0.1

    setup = {"technical_rules": [], "intraday_rules": [], "fundamental_rules": []}
    strong_scores = rule_engine.compute_component_scores(strong_row, regime_name="STABLE", setup=setup)
    weak_scores = rule_engine.compute_component_scores(weak_row, regime_name="STABLE", setup=setup)

    assert strong_scores["technical_score"] > weak_scores["technical_score"]


def test_setup_registry_loads_freshness_and_intraday_usage():
    setup = next(item for item in setup_registry.load_setup_registry() if item["setup_id"] == "INTRADAY_BREAKOUT_TACTICAL_V1")
    assert setup["intraday_usage_mode"] == "tactical_primary"
    assert setup["freshness_policy"]["intraday_max_age_days"] == 1
    assert setup["risk_profile"]["max_allocation_inr"] == 20000
    assert setup["portfolio_cap_pct"] == 0.15


def test_rule_engine_timing_only_demotes_pass_now_to_watch_breakout():
    row = pd.Series(
        {
            "asof_date": pd.Timestamp("2026-04-01T00:00:00Z"),
            "company_master_id": "nse:ABC",
            "adj_close": 100.0,
            "technical_snapshot_date": pd.Timestamp("2026-04-01T00:00:00Z"),
            "fundamentals_snapshot_date": pd.Timestamp("2026-03-15T00:00:00Z"),
            "regime_snapshot_date": pd.Timestamp("2026-04-01T00:00:00Z"),
            "intraday_snapshot_date": pd.Timestamp("2026-04-01T00:00:00Z"),
            "fundamentals_freshness_status": "FRESH",
            "market_cap": 100000.0,
            "avg_traded_value_20d": 500000000.0,
            "breakout_extension_pct": 2.0,
            "dist_52w_high": -5.0,
            "pass_above_dma_20": True,
            "pass_above_dma_50": True,
            "pass_above_dma_200": True,
            "rs_vs_benchmark": 0.15,
            "rs_vs_sector": 0.10,
            "intraday_close_vs_vwap_pct": -0.2,
            "intraday_pct_bars_above_vwap": 0.3,
            "intraday_close_location_pct": 0.4,
            "intraday_opening_range_breakout_up": False,
            "intraday_prev_day_breakout_up": False,
            "intraday_failed_prev_day_breakout": False,
            "intraday_volume_vs_20d": 0.5,
            "intraday_breakout_score": 0.2,
            "total_revenue_qoq_growth_vs_sector": 0.1,
            "profit_after_tax_qoq_growth_vs_sector": 0.1,
            "debt_to_equity_vs_sector": 0.1,
            "promoter_total_vs_sector": 0.0,
            "fii_vs_sector": 0.0,
        }
    )
    setup = {
        "allowed_regimes": ["STABLE"],
        "blocked_regimes": [],
        "allowed_overlays": ["NONE"],
        "blocked_overlays": [],
        "technical_rules": [],
        "intraday_rules": [{"column": "intraday_close_vs_vwap_pct", "operator": "gte", "value": 0.0}],
        "fundamental_rules": [],
        "score_thresholds": {"pass_now": 0.60, "watch_breakout": 0.50, "watch_event": 0.40, "near_miss_gap": 0.05},
        "freshness_policy": {"technical_max_age_days": 10, "fundamentals_max_age_days": 180, "regime_max_age_days": 7, "intraday_max_age_days": 2, "fundamentals_required": True},
        "intraday_usage_mode": "timing_only",
        "min_avg_traded_value_20d": 100000000,
        "max_breakout_extension_pct": 10,
        "watch_pullback_extension_pct": 6,
        "min_dist_52w_high": -20,
    }
    state, evaluation, _ = rule_engine.evaluate_setup_row(row, regime_name="STABLE", overlay_name="NONE", setup=setup)
    assert state == "WATCH_BREAKOUT"
    assert "intraday timing" in str(evaluation.get("watch_reason_detail"))


def test_rule_engine_tactical_primary_can_run_without_fundamentals():
    row = pd.Series(
        {
            "asof_date": pd.Timestamp("2026-04-01T00:00:00Z"),
            "company_master_id": "nse:ABC",
            "adj_close": 100.0,
            "technical_snapshot_date": pd.Timestamp("2026-04-01T00:00:00Z"),
            "fundamentals_snapshot_date": pd.NaT,
            "regime_snapshot_date": pd.Timestamp("2026-04-01T00:00:00Z"),
            "intraday_snapshot_date": pd.Timestamp("2026-04-01T00:00:00Z"),
            "fundamentals_freshness_status": pd.NA,
            "market_cap": 10000.0,
            "avg_traded_value_20d": 300000000.0,
            "breakout_extension_pct": 2.0,
            "dist_52w_high": -4.0,
            "pass_above_dma_20": True,
            "pass_above_dma_50": True,
            "pass_above_dma_200": True,
            "rs_vs_benchmark": 0.12,
            "rs_vs_sector": 0.08,
            "intraday_close_vs_vwap_pct": 0.4,
            "intraday_pct_bars_above_vwap": 0.7,
            "intraday_close_location_pct": 0.8,
            "intraday_opening_range_breakout_up": True,
            "intraday_prev_day_breakout_up": True,
            "intraday_failed_prev_day_breakout": False,
            "intraday_volume_vs_20d": 1.2,
            "intraday_breakout_score": 0.8,
            "total_revenue_qoq_growth_vs_sector": pd.NA,
            "profit_after_tax_qoq_growth_vs_sector": pd.NA,
            "debt_to_equity_vs_sector": pd.NA,
            "promoter_total_vs_sector": pd.NA,
            "fii_vs_sector": pd.NA,
        }
    )
    setup = {
        "allowed_regimes": ["STABLE"],
        "blocked_regimes": [],
        "allowed_overlays": ["NONE"],
        "blocked_overlays": [],
        "technical_rules": [],
        "intraday_rules": [{"column": "intraday_breakout_score", "operator": "gte", "value": 0.55}],
        "fundamental_rules": [],
        "score_thresholds": {"pass_now": 0.55, "watch_breakout": 0.45, "watch_event": 0.35, "near_miss_gap": 0.05},
        "freshness_policy": {"technical_max_age_days": 10, "fundamentals_max_age_days": 180, "regime_max_age_days": 7, "intraday_max_age_days": 1, "fundamentals_required": False},
        "intraday_usage_mode": "tactical_primary",
        "min_avg_traded_value_20d": 100000000,
        "max_breakout_extension_pct": 8,
        "watch_pullback_extension_pct": 5,
        "min_dist_52w_high": -20,
    }
    state, _, rejections = rule_engine.evaluate_setup_row(row, regime_name="STABLE", overlay_name="NONE", setup=setup)
    assert state in {"PASS_NOW", "WATCH_BREAKOUT"}
    assert all(item["reason_code"] != "missing_fundamental_snapshot" for item in rejections)


def test_rule_engine_can_emit_explicit_abstain():
    row = pd.Series(
        {
            "asof_date": pd.Timestamp("2026-04-01T00:00:00Z"),
            "company_master_id": "nse:ABC",
            "adj_close": 100.0,
            "technical_snapshot_date": pd.Timestamp("2026-04-01T00:00:00Z"),
            "fundamentals_snapshot_date": pd.Timestamp("2026-04-01T00:00:00Z"),
            "regime_snapshot_date": pd.Timestamp("2026-04-01T00:00:00Z"),
            "fundamentals_freshness_status": "fresh",
            "market_cap": 100000.0,
            "avg_traded_value_20d": 1000000000.0,
            "breakout_extension_pct": 1.0,
            "dist_52w_high": -4.0,
            "edge_score": 0.4,
        }
    )
    setup = {
        "allowed_regimes": ["STABLE"],
        "blocked_regimes": [],
        "allowed_overlays": ["NONE"],
        "blocked_overlays": [],
        "technical_rules": [{"column": "edge_score", "operator": "gte", "value": 1.0}],
        "fundamental_rules": [],
        "intraday_rules": [],
        "scoring_weights": {"technical": 1.0, "fundamental": 0.0, "regime_fit": 0.0, "event": 0.0},
        "score_thresholds": {"pass_now": 0.70, "watch_breakout": 0.60, "watch_event": 0.48, "abstain": 0.40, "near_miss_gap": 0.05},
        "freshness_policy": {"technical_max_age_days": 10, "fundamentals_max_age_days": 180, "regime_max_age_days": 7, "intraday_max_age_days": 2, "fundamentals_required": True},
    }
    state, details, rejections = rule_engine.evaluate_setup_row(row, regime_name="STABLE", overlay_name="NONE", setup=setup)
    assert state == "ABSTAIN"
    assert "explicit abstain" in str(details.get("watch_reason_detail")).lower()
    assert any(item["reason_code"] == "abstain_low_edge" for item in rejections)


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


def test_technical_engine_emits_buy_triggered_for_clean_breakout():
    row = _technical_engine_base_row()
    out = technical_engine.evaluate_pre_entry_state(row)
    assert out["technical_state"] == "BUY_TRIGGERED"
    assert out["entry_trigger_type"] == "breakout"
    assert out["conviction_bucket"] in {"MEDIUM_CONVICTION", "HIGH_CONVICTION"}


def test_technical_engine_emits_near_pivot_for_constructive_setup_without_trigger():
    row = _technical_engine_base_row()
    row["breakout_day_volume_vs_20d"] = 0.9
    row["pivot_distance_20d_pct"] = 1.8
    row["support_distance_20d_pct"] = 8.0
    out = technical_engine.evaluate_pre_entry_state(row)
    assert out["technical_state"] == "NEAR_PIVOT"


def test_technical_engine_rejects_junk_chart_on_hard_filters():
    row = _technical_engine_base_row()
    row["avg_traded_value_20d"] = 1_000_000.0
    row["median_volume_20d"] = 5_000.0
    row["gap_frequency_60d"] = 0.35
    row["pass_liquidity_20d"] = False
    row["pass_gap_behavior"] = False
    out = technical_engine.evaluate_pre_entry_state(row)
    assert out["technical_state"] == "REJECT"
    assert "low_liquidity" in out["hard_filter_reasons"]


def test_technical_engine_emits_full_exit_on_failed_breakout():
    row = _technical_engine_base_row()
    row["pass_above_dma_50"] = False
    row["distribution_days_20d"] = 6.0
    row["rs_vs_sector"] = -0.08
    out = technical_engine.evaluate_post_entry_state(row)
    assert out["technical_state"] == "FULL_EXIT"
    assert "technical_thesis_failure" in out["technical_reasons"]


def test_technical_engine_emits_partial_exit_on_sharp_extension():
    row = _technical_engine_base_row()
    row["breakout_extension_pct"] = 14.0
    out = technical_engine.evaluate_post_entry_state(row)
    assert out["technical_state"] == "PARTIAL_EXIT"
    assert "extension_or_distribution" in out["technical_reasons"]


def test_technical_threshold_calibration_attaches_point_in_time_forward_returns():
    signals = pd.DataFrame(
        [
            {"asof_date": pd.Timestamp("2026-05-01T00:00:00Z"), "symbol": "ABC", "technical_total_score": 80.0},
            {"asof_date": pd.Timestamp("2026-05-02T00:00:00Z"), "symbol": "ABC", "technical_total_score": 80.0},
        ]
    )
    prices = pd.DataFrame(
        [
            {"symbol": "ABC", "date": pd.Timestamp("2026-05-01T00:00:00Z"), "close": 90.0},
            {"symbol": "ABC", "date": pd.Timestamp("2026-05-04T00:00:00Z"), "close": 100.0},
            {"symbol": "ABC", "date": pd.Timestamp("2026-05-05T00:00:00Z"), "close": 110.0},
            {"symbol": "ABC", "date": pd.Timestamp("2026-05-06T00:00:00Z"), "close": 121.0},
        ]
    )

    out = technical_threshold_calibration.attach_forward_returns(signals, prices, horizons=[2])

    assert out.iloc[0]["entry_date_h2"] == pd.Timestamp("2026-05-04T00:00:00Z")
    assert out.iloc[0]["exit_date_h2"] == pd.Timestamp("2026-05-05T00:00:00Z")
    assert round(float(out.iloc[0]["forward_return_h2"]), 4) == 0.1
    assert out.iloc[1]["entry_date_h2"] == pd.Timestamp("2026-05-04T00:00:00Z")


def test_technical_threshold_calibration_selects_profitable_threshold_config():
    dataset = pd.DataFrame(
        [
            {
                "asof_date": pd.Timestamp("2026-05-01T00:00:00Z"),
                "symbol": "WIN",
                "technical_state": "BUY_TRIGGERED",
                "technical_trigger_type": "breakout",
                "technical_trend_score": 18.0,
                "technical_structure_score": 22.0,
                "technical_participation_score": 13.0,
                "technical_relative_strength_score": 10.0,
                "technical_tradability_score": 7.0,
                "technical_total_score": 82.0,
                "forward_return_h5": 0.08,
            },
            {
                "asof_date": pd.Timestamp("2026-05-01T00:00:00Z"),
                "symbol": "LOW",
                "technical_state": "WATCHLIST",
                "technical_trigger_type": None,
                "technical_trend_score": 12.0,
                "technical_structure_score": 15.0,
                "technical_participation_score": 8.0,
                "technical_relative_strength_score": 6.0,
                "technical_tradability_score": 5.0,
                "technical_total_score": 65.0,
                "forward_return_h5": -0.04,
            },
        ]
    )
    config = {
        "trend_min": 15.0,
        "structure_min": 18.0,
        "participation_min": 10.0,
        "relative_strength_min": 8.0,
        "tradability_min": 6.0,
        "ready_total_min": 70.0,
        "buy_total_min": 78.0,
    }

    row = technical_threshold_calibration.evaluate_threshold_config(
        dataset,
        config,
        horizon_days=5,
        return_threshold=0.02,
        cost_bps=0.0,
        min_signals=1,
    )

    assert row["eligible_count"] == 1
    assert row["hit_rate_after_cost"] == 1.0
    assert row["avg_rejected_forward_return"] == -0.04
    assert row["objective_score"] > 0


def test_signal_quality_evaluator_compares_overlay_variants():
    dataset = pd.DataFrame(
        [
            {
                "asof_date": pd.Timestamp("2026-05-01T00:00:00Z"),
                "setup_id": "TECH_V1",
                "symbol": "WIN",
                "technical_total_score": 72.0,
                "technical_state": "READY",
                "candidate_state": "READY",
                "technical_trigger_type": "breakout",
                "event_action_type": "BUY_WATCH",
                "event_policy_class": "POSITIVE",
                "event_policy_score": 0.55,
                "event_confidence": 0.8,
                "bhavcopy_deal_pressure": "accumulation",
                "bhavcopy_evidence_score": 0.25,
                "company_memory_signal": "BUY",
                "company_memory_confidence": 0.75,
                "company_memory_conviction_score": 82.0,
                "entry_date_h5": pd.Timestamp("2026-05-04T00:00:00Z"),
                "exit_date_h5": pd.Timestamp("2026-05-08T00:00:00Z"),
                "entry_close_h5": 100.0,
                "exit_close_h5": 112.0,
                "forward_return_h5": 0.12,
            },
            {
                "asof_date": pd.Timestamp("2026-05-01T00:00:00Z"),
                "setup_id": "TECH_V1",
                "symbol": "RISK",
                "technical_total_score": 72.0,
                "technical_state": "READY",
                "candidate_state": "READY",
                "technical_trigger_type": "breakout",
                "event_action_type": "REDUCE_EXPOSURE_REVIEW",
                "event_policy_class": "RISK",
                "event_policy_score": -0.5,
                "event_confidence": 0.8,
                "bhavcopy_deal_pressure": "distribution_or_pressure",
                "bhavcopy_evidence_score": -0.25,
                "company_memory_signal": "SELL_PARTIAL",
                "company_memory_confidence": 0.75,
                "company_memory_conviction_score": 35.0,
                "entry_date_h5": pd.Timestamp("2026-05-04T00:00:00Z"),
                "exit_date_h5": pd.Timestamp("2026-05-08T00:00:00Z"),
                "entry_close_h5": 100.0,
                "exit_close_h5": 90.0,
                "forward_return_h5": -0.10,
            },
            {
                "asof_date": pd.Timestamp("2026-05-01T00:00:00Z"),
                "setup_id": "TECH_V1",
                "symbol": "NEAR",
                "technical_total_score": 66.0,
                "technical_state": "WATCHLIST",
                "candidate_state": "WATCHLIST",
                "technical_trigger_type": "",
                "event_action_type": "BUY_WATCH",
                "event_policy_class": "POSITIVE",
                "event_policy_score": 0.4,
                "event_confidence": 0.7,
                "bhavcopy_deal_pressure": "accumulation",
                "bhavcopy_evidence_score": 0.2,
                "company_memory_signal": "WATCH",
                "company_memory_confidence": 0.65,
                "company_memory_conviction_score": 70.0,
                "entry_date_h5": pd.Timestamp("2026-05-04T00:00:00Z"),
                "exit_date_h5": pd.Timestamp("2026-05-08T00:00:00Z"),
                "entry_close_h5": 100.0,
                "exit_close_h5": 108.0,
                "forward_return_h5": 0.08,
            },
        ]
    )

    evaluations = signal_quality_evaluator.build_evaluation_rows(
        dataset,
        horizons=[5],
        cost_bps=0.0,
        return_threshold=0.03,
        technical_min=70.0,
        near_technical_min=65.0,
        evaluated_at=pd.Timestamp("2026-06-01T00:00:00Z"),
    )

    selected = evaluations[evaluations["selected"].fillna(False).astype(bool)]
    technical_selected = selected[selected["variant"] == "technical_only"]["symbol"].tolist()
    all_selected = selected[selected["variant"] == "technical_plus_all"]["symbol"].tolist()

    assert technical_selected == ["WIN", "RISK"]
    assert all_selected == ["WIN", "NEAR"]
    risk_event = evaluations[(evaluations["symbol"] == "RISK") & (evaluations["variant"] == "technical_plus_event")].iloc[0]
    assert bool(risk_event["event_negative"]) is True
    assert bool(risk_event["selected"]) is False


def test_signal_quality_evaluator_summary_reports_lift_vs_technical_only():
    evaluations = signal_quality_evaluator.build_evaluation_rows(
        pd.DataFrame(
            [
                {
                    "asof_date": pd.Timestamp("2026-05-01T00:00:00Z"),
                    "setup_id": "TECH_V1",
                    "symbol": "WIN",
                    "technical_total_score": 72.0,
                    "technical_state": "READY",
                    "candidate_state": "READY",
                    "event_action_type": "BUY_WATCH",
                    "event_policy_class": "POSITIVE",
                    "event_policy_score": 0.5,
                    "bhavcopy_deal_pressure": "accumulation",
                    "bhavcopy_evidence_score": 0.2,
                    "company_memory_signal": "BUY",
                    "company_memory_confidence": 0.7,
                    "forward_return_h5": 0.10,
                },
                {
                    "asof_date": pd.Timestamp("2026-05-01T00:00:00Z"),
                    "setup_id": "TECH_V1",
                    "symbol": "RISK",
                    "technical_total_score": 72.0,
                    "technical_state": "READY",
                    "candidate_state": "READY",
                    "event_action_type": "REDUCE_EXPOSURE_REVIEW",
                    "event_policy_class": "RISK",
                    "event_policy_score": -0.5,
                    "bhavcopy_deal_pressure": "distribution_or_pressure",
                    "bhavcopy_evidence_score": -0.2,
                    "company_memory_signal": "SELL",
                    "company_memory_confidence": 0.7,
                    "forward_return_h5": -0.10,
                },
            ]
        ),
        horizons=[5],
        cost_bps=0.0,
        return_threshold=0.03,
        evaluated_at=pd.Timestamp("2026-06-01T00:00:00Z"),
    )

    summary = signal_quality_evaluator.summarize_evaluations(
        evaluations,
        evaluated_at=pd.Timestamp("2026-06-01T00:00:00Z"),
        min_matured_rows=1,
    )
    all_row = summary[summary["variant"] == "technical_plus_all"].iloc[0]

    assert round(float(all_row["baseline_avg_forward_return_after_cost"]), 4) == 0.0
    assert round(float(all_row["avg_forward_return_after_cost"]), 4) == 0.1
    assert round(float(all_row["lift_vs_technical_only"]), 4) == 0.1
    assert all_row["recommendation"] == "candidate_overlay_improves"


def test_operator_api_builds_technical_calibration_payload(monkeypatch):
    summary = pd.DataFrame(
        [
            {
                "evaluated_at": pd.Timestamp("2026-05-30T00:00:00Z"),
                "horizon_days": 5,
                "best_config_id": "cfg-1",
                "best_threshold_config_json": '{"buy_total_min": 78}',
                "best_objective_score": 0.7,
                "best_eligible_count": 100,
                "recommendation": "review_for_promotion",
            }
        ]
    )
    configs = pd.DataFrame(
        [
            {
                "horizon_days": 5,
                "config_id": "cfg-1",
                "threshold_config_json": '{"buy_total_min": 78}',
                "eligible_count": 100,
                "rn": 1,
            }
        ]
    )

    monkeypatch.setattr(operator_api, "_table_exists", lambda table_name: True)
    monkeypatch.setattr(
        operator_api,
        "sql_to_df",
        lambda query, *args, **kwargs: configs.copy() if "ROW_NUMBER" in query else summary.copy(),
    )

    payload = operator_api.build_technical_calibration_payload(limit=3)

    assert payload["status"] == "ok"
    assert payload["api_schema"]["endpoint"] == "/api/technical-calibration"
    assert payload["api_schema"]["read_only"] is True
    assert payload["api_schema"]["broker_execution_enabled"] is False
    assert payload["summary"][0]["best_config_id"] == "cfg-1"
    assert payload["top_configs"][0]["config_id"] == "cfg-1"


def test_operator_api_builds_event_policy_payload(monkeypatch):
    rows = pd.DataFrame(
        [
            {
                "asof_date": pd.Timestamp("2026-05-30T00:00:00Z"),
                "unique_id": "event-1",
                "symbol": "ABC",
                "action_type": "MANUAL_REVIEW",
                "policy_class": "ORDER_WIN",
                "event_class": "ORDER_WIN",
                "action_status": "needs_operator_review",
                "policy_score": 0.65,
                "action_reason": "order win needs valuation check",
                "checks_json": json.dumps([{"check": "materiality", "status": "review"}]),
                "operator_notes_json": json.dumps(
                    {
                        "operator_summary": "Check order size against trailing revenue.",
                        "wait_for_events": ["management commentary"],
                        "operator_questions": ["Is this already priced in?"],
                    }
                ),
                "llm_review_json": json.dumps({"recommended_action": "MANUAL_REVIEW"}),
                "raw_context_json": json.dumps({"source_type": "announcement"}),
                "published_on": pd.Timestamp("2026-05-30T09:00:00Z"),
            }
        ]
    )
    summary = pd.DataFrame(
        [
            {"action_type": "MANUAL_REVIEW", "policy_class": "ORDER_WIN", "row_count": 1},
            {"action_type": "NO_ACTION", "policy_class": "DIVIDEND", "row_count": 2},
        ]
    )

    monkeypatch.setattr(operator_api, "_table_exists", lambda table_name: table_name == operator_api.EVENT_POLICY_TABLE)

    def fake_sql_to_df(query, *args, **kwargs):
        if "GROUP BY action_type, policy_class" in query:
            return summary.copy()
        return rows.copy()

    monkeypatch.setattr(operator_api, "sql_to_df", fake_sql_to_df)

    payload = operator_api.build_event_policy_payload(limit=10, action_type="ALL")

    assert payload["status"] == "ok"
    assert payload["api_schema"]["endpoint"] == "/api/event-policy"
    assert payload["api_schema"]["read_only"] is True
    assert payload["api_schema"]["broker_execution_enabled"] is False
    assert payload["summary"]["action_counts"]["MANUAL_REVIEW"] == 1
    assert payload["summary"]["action_counts"]["NO_ACTION"] == 2
    assert payload["summary"]["policy_class_counts"]["ORDER_WIN"] == 1
    assert payload["rows"][0]["checks"][0]["check"] == "materiality"
    assert payload["rows"][0]["operator_notes"]["operator_questions"] == ["Is this already priced in?"]
    assert payload["rows"][0]["llm_review"]["recommended_action"] == "MANUAL_REVIEW"


def test_operator_api_builds_manual_review_payload(monkeypatch):
    action_rows = pd.DataFrame(
        [
            {
                "asof_date": pd.Timestamp("2026-05-30T00:00:00Z"),
                "updated_at": pd.Timestamp("2026-05-30T09:10:00Z"),
                "symbol": "ABC",
                "setup_id": "SETUP",
                "action_code": "MANUAL_REVIEW",
                "action_reason": "conflicting evidence",
                "reason_contract_status": "needs_review",
            }
        ]
    )
    policy_rows = pd.DataFrame(
        [
            {
                "asof_date": pd.Timestamp("2026-05-30T00:00:00Z"),
                "unique_id": "event-1",
                "symbol": "ABC",
                "action_type": "MANUAL_REVIEW",
                "action_status": "needs_operator_review",
                "action_reason": "order win needs valuation check",
                "published_on": pd.Timestamp("2026-05-30T09:00:00Z"),
            }
        ]
    )
    conflict_rows = pd.DataFrame(
        [
            {
                "asof_date": pd.Timestamp("2026-05-30T00:00:00Z"),
                "updated_at": pd.Timestamp("2026-05-30T09:30:00Z"),
                "symbol": "ABC",
                "winning_action_code": "BUY",
                "losing_action_code": "SELL",
                "lost_reason": "higher priority technical setup",
            }
        ]
    )
    execution_rows = pd.DataFrame(
        [
            {
                "asof_date": pd.Timestamp("2026-05-30T00:00:00Z"),
                "updated_at": pd.Timestamp("2026-05-30T10:00:00Z"),
                "symbol": "ABC",
                "execution_status": "submit_blocked",
                "execution_reason": "broker disabled",
                "execution_id": "exec-1",
            }
        ]
    )
    processing_rows = pd.DataFrame(
        [
            {
                "unique_id": "event-2",
                "symbol": "XYZ",
                "stage": "event_evaluation",
                "status": "failed",
                "error": "LLM schema mismatch",
                "completed_at": pd.Timestamp("2026-05-30T08:00:00Z"),
            }
        ]
    )
    document_rows = pd.DataFrame(
        [
            {
                "unique_id": "doc-1",
                "ticker": "XYZ",
                "ocr_status": "failed",
                "parse_status": "pending",
                "last_error": "poppler missing",
                "updated_at": pd.Timestamp("2026-05-30T07:00:00Z"),
            }
        ]
    )
    identity_rows = pd.DataFrame(
        [
            {
                "issue_key": "dhan_security_id_missing:stock:NSE:HUIL",
                "issue_type": "dhan_security_id_missing",
                "status": "open",
                "symbol": "HUIL",
                "requested_exchange": "NSE",
                "asset_type": "stock",
                "company_master_id": "nse:HUIL",
                "error_text": "No Dhan security id mapped for NSE:HUIL",
                "suggested_action": "Refresh Dhan scrip master and company master.",
                "last_seen_at": pd.Timestamp("2026-05-30T10:30:00Z"),
            }
        ]
    )

    monkeypatch.setattr(operator_api, "_table_exists", lambda _table_name: True)
    monkeypatch.setattr(operator_api, "load_latest_manual_review_decisions", lambda **_kwargs: {})
    monkeypatch.setattr(operator_api, "load_open_identity_issues", lambda **_kwargs: identity_rows.copy())
    monkeypatch.setattr(
        operator_api,
        "load_promotion_reviews",
        lambda limit=100: [
            {
                "reviewed_at": pd.Timestamp("2026-05-30T06:00:00Z"),
                "setup_id": "TECH",
                "config_id": "cfg-1",
                "review_status": "ready_for_operator_review",
                "manual_decision": None,
            }
        ],
    )

    def fake_sql_to_df(query, *args, **kwargs):
        if operator_api.EXECUTION_TABLE in query:
            return execution_rows.copy()
        if operator_api.ACTION_RECOMMENDATIONS_TABLE in query:
            return action_rows.copy()
        if operator_api.EVENT_POLICY_TABLE in query:
            return policy_rows.copy()
        if operator_api.ACTION_CONFLICTS_TABLE in query:
            return conflict_rows.copy()
        if "advisory_event_processing_runs" in query:
            return processing_rows.copy()
        if "announcement_pipeline_documents" in query:
            return document_rows.copy()
        return pd.DataFrame()

    monkeypatch.setattr(operator_api, "sql_to_df", fake_sql_to_df)

    payload = operator_api.build_manual_review_payload(limit=20)

    assert payload["status"] == "ok"
    assert payload["summary"]["total_items"] == 8
    assert payload["summary"]["by_type"]["action_manual_review"] == 1
    assert payload["summary"]["by_type"]["event_policy_manual_review"] == 1
    assert payload["summary"]["by_type"]["action_conflict"] == 1
    assert payload["summary"]["by_type"]["threshold_review"] == 1
    assert payload["summary"]["by_type"]["execution_blocker"] == 1
    assert payload["summary"]["by_type"]["event_processing_failure"] == 1
    assert payload["summary"]["by_type"]["announcement_failure"] == 1
    assert payload["summary"]["by_type"]["identity_issue"] == 1
    assert payload["summary"]["by_severity"]["error"] == 3
    assert any(row["reason"] == "source_rows_stale" for row in payload["source_warnings"])
    assert payload["items"][0]["item_type"] == "identity_issue"
    assert payload["items"][0]["review_lane"] == "technical_issue"
    assert payload["items"][0]["suggested_decision"] == "mark_fixed"
    assert "Dhan security id" in payload["items"][0]["reason"]
    execution_item = next(item for item in payload["items"] if item["item_type"] == "execution_blocker")
    assert execution_item["raw"]["updated_at"] == "2026-05-30T10:00:00+00:00"


def test_operator_api_builds_identity_issues_payload(monkeypatch):
    identity_rows = pd.DataFrame(
        [
            {
                "issue_key": "dhan_security_id_missing:stock:NSE:HUIL",
                "issue_type": "dhan_security_id_missing",
                "status": "open",
                "symbol": "HUIL",
                "requested_exchange": "NSE",
                "asset_type": "stock",
                "company_master_id": "nse:HUIL",
                "source": "resolve_dhan_identity",
                "error_text": "No Dhan security id mapped for NSE:HUIL",
                "exchanges_tried_json": '["NSE", "BSE"]',
                "fallback_tried_json": '[{"exchange": "BSE", "matched": false}]',
                "suggested_action": "Refresh Dhan scrip master and company master.",
                "context_json": '{"company_master": {"symbol": "HUIL"}}',
                "first_seen_at": pd.Timestamp("2026-05-30T09:00:00Z"),
                "last_seen_at": pd.Timestamp("2026-05-30T10:30:00Z"),
                "load_ts": pd.Timestamp("2026-05-30T10:31:00Z"),
            },
            {
                "issue_key": "dhan_security_id_missing:stock:NSE:MISS",
                "issue_type": "dhan_security_id_missing",
                "status": "open",
                "symbol": "MISS",
                "requested_exchange": "NSE",
                "asset_type": "stock",
                "source": "resolve_dhan_identity",
                "error_text": "No Dhan security id mapped for NSE:MISS",
                "exchanges_tried_json": '["NSE"]',
                "fallback_tried_json": "[]",
                "context_json": "{}",
            },
        ]
    )

    monkeypatch.setattr(operator_api, "_table_exists", lambda table_name: table_name == operator_api.IDENTITY_ISSUES_TABLE)
    monkeypatch.setattr(operator_api, "sql_to_df", lambda query, *args, **kwargs: identity_rows.copy() if operator_api.IDENTITY_ISSUES_TABLE in query else pd.DataFrame())

    payload = operator_api.build_identity_issues_payload(limit=20, symbol="HUIL")

    assert payload["status"] == "ok"
    assert payload["api_schema"]["endpoint"] == "/api/identity-issues"
    assert payload["api_schema"]["read_only"] is True
    assert payload["api_schema"]["broker_execution_enabled"] is False
    assert payload["summary"]["total_open"] == 1
    assert payload["summary"]["by_type"]["dhan_security_id_missing"] == 1
    assert payload["summary"]["broker_execution_enabled"] is False
    assert payload["source_warnings"][0]["reason"] == "source_rows_stale"
    assert payload["source_warnings"][0]["operator_action"] == "rerun_source_or_refresh_advisory"
    assert payload["skipped"] == []
    issue = payload["issues"][0]
    assert issue["symbol"] == "HUIL"
    assert issue["exchanges_tried"] == ["NSE", "BSE"]
    assert issue["fallback_tried"][0]["exchange"] == "BSE"
    assert issue["context"]["company_master"]["symbol"] == "HUIL"
    assert issue["manual_review_item_id"] == "identity_issue:advisory_identity_issues:dhan_security_id_missing:stock:NSE:HUIL"
    assert issue["operator_boundary"]["mutates_identity_mapping"] is False
    assert issue["operator_boundary"]["mutates_broker_execution"] is False


def test_operator_api_identity_resolution_preview_is_read_only(monkeypatch):
    calls: list[dict[str, object]] = []

    def fake_resolve(**kwargs):
        calls.append(kwargs)
        return {
            "status": "ok",
            "mode": "dry_run",
            "checked_rows": 1,
            "counts": {"would_resolve": 1},
            "results": [{"issue_key": "dhan_security_id_missing:stock:NSE:HUIL", "status": "would_resolve"}],
            "requested_issue_keys": [],
        }

    monkeypatch.setattr(operator_api, "resolve_open_identity_issues", fake_resolve)

    payload = operator_api.resolve_identity_issues_payload(payload={"limit": 20}, apply=False)

    assert payload["api_schema"]["endpoint"] == "/api/identity-issues/resolve-preview"
    assert payload["api_schema"]["read_only"] is True
    assert payload["api_schema"]["broker_execution_enabled"] is False
    assert payload["operator_boundary"]["mutates_identity_issue_status"] is False
    assert payload["counts"]["would_resolve"] == 1
    assert calls == [{"limit": 20, "apply": False, "issue_keys": None}]


def test_operator_api_identity_resolution_apply_requires_issue_keys(monkeypatch):
    calls: list[dict[str, object]] = []
    monkeypatch.setattr(operator_api, "resolve_open_identity_issues", lambda **kwargs: calls.append(kwargs) or {})

    payload = operator_api.resolve_identity_issues_payload(payload={"limit": 20}, apply=True)

    assert payload["status"] == "error"
    assert payload["api_schema"]["endpoint"] == "/api/identity-issues/resolve-apply"
    assert payload["api_schema"]["read_only"] is False
    assert payload["operator_boundary"]["mutates_identity_issue_status"] is True
    assert payload["operator_boundary"]["requires_preview_issue_keys"] is True
    assert calls == []


def test_manual_review_suppresses_superseded_processing_and_recovered_document_failures(monkeypatch):
    skipped: list[dict[str, str]] = []
    items: list[dict[str, object]] = []
    processing_rows = pd.DataFrame(
        [
            {
                "unique_id": "event-old",
                "symbol": "ABC",
                "stage": "event_evaluation",
                "status": "failed",
                "error": "LLM schema mismatch",
                "completed_at": pd.Timestamp("2026-05-30T08:00:00Z"),
                "superseded_at": pd.Timestamp("2026-05-30T09:00:00Z"),
                "superseded_by_status": "completed",
            },
            {
                "unique_id": "event-active",
                "symbol": "XYZ",
                "stage": "event_evaluation",
                "status": "failed",
                "error": "provider timeout",
                "completed_at": pd.Timestamp("2026-05-30T10:00:00Z"),
            },
        ]
    )
    document_rows = pd.DataFrame(
        [
            {
                "unique_id": "doc-old",
                "ticker": "ABC",
                "ocr_status": "completed",
                "parse_status": "completed",
                "last_error": "old poppler missing",
                "updated_at": pd.Timestamp("2026-05-30T09:00:00Z"),
            },
            {
                "unique_id": "doc-active",
                "ticker": "XYZ",
                "ocr_status": "completed",
                "parse_status": "failed",
                "last_error": "parser mismatch",
                "updated_at": pd.Timestamp("2026-05-30T10:00:00Z"),
            },
        ]
    )

    monkeypatch.setattr(operator_api, "_table_exists", lambda _table_name: True)

    def fake_safe_manual_query(source_name, query, *, params=None, skipped=None):
        if source_name == "advisory_event_processing_runs":
            return processing_rows.copy()
        if source_name == "announcement_pipeline_documents":
            return document_rows.copy()
        return pd.DataFrame()

    monkeypatch.setattr(operator_api, "_safe_manual_query", fake_safe_manual_query)

    operator_api._append_processing_failure_items(items, skipped, limit=10)
    operator_api._append_announcement_failure_items(items, skipped, limit=10)

    assert skipped == []
    assert [item["source_key"] for item in items] == ["event-active", "doc-active"]
    assert [item["item_type"] for item in items] == ["event_processing_failure", "announcement_failure"]


def test_superseded_failure_cleanup_dry_run_discovers_recovered_rows(monkeypatch):
    writes = {"called": False}

    def fake_db_session(*_args, **_kwargs):
        writes["called"] = True
        raise AssertionError("dry-run should not open a write session")

    def fake_sql_to_df(query, *args, **kwargs):
        query_text = str(query)
        params = kwargs.get("params")
        if "information_schema.tables" in query_text:
            return pd.DataFrame({"exists_flag": [1]})
        if "information_schema.columns" in query_text:
            table_name = params[0] if isinstance(params, tuple) else params
            if table_name == superseded_failures.EVENT_PROCESSING_TABLE:
                return pd.DataFrame({"column_name": ["unique_id", "stage", "status", "started_at", "completed_at", "load_ts", "error"]})
            return pd.DataFrame({"column_name": ["unique_id", "ticker", "ocr_status", "parse_status", "last_error", "updated_at", "published_on"]})
        if superseded_failures.EVENT_PROCESSING_TABLE in query_text:
            assert "failed.superseded_at IS NULL" not in query_text
            return pd.DataFrame(
                [
                    {
                        "unique_id": "event-old",
                        "symbol": "ABC",
                        "source_type": "announcement",
                        "stage": "event_evaluation",
                        "status": "failed",
                        "started_at": pd.Timestamp("2026-05-30T08:00:00Z"),
                        "completed_at": pd.Timestamp("2026-05-30T08:01:00Z"),
                        "error": "old schema mismatch",
                        "superseded_by_status": "completed",
                        "superseded_by_completed_at": pd.Timestamp("2026-05-30T09:00:00Z"),
                    }
                ]
            )
        if superseded_failures.ANNOUNCEMENT_DOCUMENTS_TABLE in query_text:
            assert "last_error_superseded_at IS NULL" not in query_text
            assert " symbol" not in query_text
            assert "load_ts" not in query_text
            assert "COALESCE(updated_at, published_on)" in query_text
            return pd.DataFrame(
                [
                    {
                        "unique_id": "doc-old",
                        "ticker": "ABC",
                        "ocr_status": "completed",
                        "parse_status": "completed",
                        "last_error": "old poppler error",
                        "updated_at": pd.Timestamp("2026-05-30T09:00:00Z"),
                    }
                ]
            )
        return pd.DataFrame()

    monkeypatch.setattr(superseded_failures, "sql_to_df", fake_sql_to_df)
    monkeypatch.setattr(superseded_failures, "db_session", fake_db_session)

    result = superseded_failures.cleanup_superseded_failures(apply=False, limit=10)

    assert result["status"] == "dry_run"
    assert result["event_processing"]["candidates"] == 1
    assert result["event_processing"]["updated"] == 0
    assert result["announcement_documents"]["candidates"] == 1
    assert result["announcement_documents"]["updated"] == 0
    assert writes["called"] is False


def test_superseded_failure_cleanup_apply_marks_candidates(monkeypatch):
    executed: list[tuple[str, tuple[object, ...]]] = []

    class FakeCursor:
        rowcount = 1

        def execute(self, query, params=None):
            executed.append((str(query), tuple(params or ())))

    class FakeSession:
        def __enter__(self):
            return object(), FakeCursor()

        def __exit__(self, exc_type, exc, tb):
            return False

    monkeypatch.setattr(superseded_failures, "ensure_superseded_columns", lambda: None)
    monkeypatch.setattr(
        superseded_failures,
        "load_superseded_event_processing_failures",
        lambda limit=500: [
            {
                "unique_id": "event-old",
                "stage": "event_evaluation",
                "started_at": pd.Timestamp("2026-05-30T08:00:00Z"),
                "superseded_by_status": "completed",
                "superseded_by_completed_at": pd.Timestamp("2026-05-30T09:00:00Z"),
            }
        ],
    )
    monkeypatch.setattr(
        superseded_failures,
        "load_recovered_announcement_document_errors",
        lambda limit=500: [{"unique_id": "doc-old", "ocr_status": "completed", "parse_status": "completed"}],
    )
    monkeypatch.setattr(superseded_failures, "db_session", lambda: FakeSession())

    result = superseded_failures.cleanup_superseded_failures(apply=True, limit=10)

    assert result["status"] == "applied"
    assert result["event_processing"]["updated"] == 1
    assert result["announcement_documents"]["updated"] == 1
    assert len(executed) == 2
    assert "SET superseded_at" in executed[0][0]
    assert executed[0][1][4:7] == ("event-old", "event_evaluation", pd.Timestamp("2026-05-30T08:00:00Z"))
    assert "SET last_error_superseded_at" in executed[1][0]
    assert executed[1][1][2] == "doc-old"


def test_event_data_quality_reports_schema_tolerant_issues(monkeypatch):
    table_columns = {
        "nseindia_ohlcv": {"date", "symbol"},
        "nseindia_indices": {"date", "symbol"},
        "nseindia_cmvolt": {"date", "symbol"},
        "nseindia_block_deals": {"date", "symbol"},
        "nseindia_bulk_deals": {"date", "symbol"},
        "nseindia_short_selling": {"date", "symbol"},
        "nseindia_circuit_hit": {"date", "symbol"},
        "nseindia_corporate_actions": {"date", "symbol"},
        "nseindia_corporate_actions_bc_raw": {"date", "symbol"},
        "nseindia_corporate_actions_normalized": {"date", "symbol"},
        "nseindia_earnings_events": {"event_date", "symbol"},
        "nseindia_insider_deals": {"date", "symbol"},
        event_data_quality.ANNOUNCEMENT_DOCUMENTS_TABLE: {"unique_id", "symbol", "published_on", "parse_status", "ocr_status", "concise_summary_s3_key"},
        event_data_quality.ANNOUNCEMENT_REPORTS_TABLE: {"unique_id", "symbol", "published_on"},
        event_data_quality.EXCHANGE_EVENTS_TABLE: {"event_id", "symbol", "known_on", "event_source", "event_type"},
        event_data_quality.EXCHANGE_FEATURES_TABLE: {"symbol", "asof_date", "exchange_event_score", "deal_cluster_count_20d", "corporate_action_count_30d"},
        event_data_quality.BHAVCOPY_EVIDENCE_TABLE: {"asof_date", "symbol"},
        event_data_quality.ANNOUNCEMENT_EVIDENCE_TABLE: {"published_on", "symbol"},
        "nseindia_var1": {"date", "symbol"},
    }

    def fake_sql_to_df(query, *args, **kwargs):
        query_text = str(query)
        params = kwargs.get("params")
        if "information_schema.tables" in query_text:
            table_name = params[0] if isinstance(params, tuple) else params
            return pd.DataFrame({"exists_flag": [1]}) if table_name in table_columns else pd.DataFrame()
        if "information_schema.columns" in query_text:
            table_name = params[0] if isinstance(params, tuple) else params
            return pd.DataFrame({"column_name": sorted(table_columns.get(table_name, set()))})
        if "announcement_pipeline_documents" in query_text and "SUM(CASE" in query_text:
            assert '"ticker"' not in query_text
            assert '"symbol"' in query_text
            return pd.DataFrame(
                [
                    {
                        "row_count": 10,
                        "min_published_on": pd.Timestamp("2026-05-01T00:00:00Z"),
                        "max_published_on": pd.Timestamp("2026-06-01T00:00:00Z"),
                        "distinct_entities": 5,
                        "parsed_rows": 9,
                        "parse_failed_rows": 1,
                        "ocr_failed_rows": 0,
                        "rows_with_text": 0,
                        "rows_with_s3_pointer": 8,
                    }
                ]
            )
        if "advisory_exchange_events" in query_text and "GROUP BY" not in query_text:
            return pd.DataFrame(
                [
                    {
                        "row_count": 3,
                        "distinct_symbols": 2,
                        "min_known_on": pd.Timestamp("2026-05-01T00:00:00Z"),
                        "max_known_on": pd.Timestamp("2026-06-01T00:00:00Z"),
                        "null_source_rows": 1,
                        "null_type_rows": 1,
                        "corporate_action_rows": 0,
                        "earnings_rows": 1,
                    }
                ]
            )
        if "advisory_exchange_events" in query_text and "GROUP BY" in query_text:
            return pd.DataFrame([{"event_source": "missing", "event_type": "missing", "rows": 1}])
        if "advisory_exchange_features_daily" in query_text:
            assert "short_selling_event_count_20d" not in query_text
            assert "NULL::bigint AS \"short_feature_rows\"" in query_text
            return pd.DataFrame(
                [
                    {
                        "row_count": 4,
                        "distinct_symbols": 2,
                        "min_asof_date": pd.Timestamp("2026-05-01T00:00:00Z"),
                        "max_asof_date": pd.Timestamp("2026-06-01T00:00:00Z"),
                        "nonzero_score_rows": 2,
                        "deal_feature_rows": 1,
                        "short_feature_rows": None,
                        "corporate_action_feature_rows": 0,
                        "earnings_feature_rows": None,
                    }
                ]
            )
        if "COUNT(*) AS row_count" in query_text and "MIN(" in query_text:
            return pd.DataFrame(
                [
                    {
                        "row_count": 10,
                        "min_at": pd.Timestamp("2026-05-01T00:00:00Z"),
                        "latest_at": pd.Timestamp("2026-06-01T00:00:00Z"),
                        "distinct_entities": 3,
                    }
                ]
            )
        if "SELECT COUNT(*) AS row_count FROM" in query_text:
            return pd.DataFrame({"row_count": [10]})
        return pd.DataFrame()

    monkeypatch.setattr(event_data_quality, "sql_to_df", fake_sql_to_df)

    payload = event_data_quality.build_event_data_quality_report(limit=50, now=pd.Timestamp("2026-06-08T00:00:00Z"))

    assert payload["status"] == "warn"
    issue_codes = {row["issue_code"] for row in payload["issues"]}
    assert f"announcement_readiness:{event_data_quality.ANNOUNCEMENT_DOCUMENTS_TABLE}" in issue_codes
    assert f"exchange_event_readiness:{event_data_quality.EXCHANGE_EVENTS_TABLE}" in issue_codes
    assert f"exchange_feature_readiness:{event_data_quality.EXCHANGE_FEATURES_TABLE}" in issue_codes
    feature_section = payload["sections"]["exchange_feature_readiness"]
    assert feature_section["missing_feature_columns"] == ["short_selling_event_count_20d", "upcoming_earnings_14d"]
    assert feature_section["corporate_action_feature_rows"] == 0


def test_event_evidence_store_orchestrates_compact_tables(monkeypatch):
    writes: list[tuple[str, pd.DataFrame, list[str], str | None]] = []
    bhavcopy = pd.DataFrame(
        [
            {
                "asof_date": pd.Timestamp("2026-06-01T00:00:00Z"),
                "symbol": "ABC",
                "deal_net_value_inr": 20_000_000.0,
                "short_selling_quantity": 0.0,
                "circuit_hit_count": 0,
            }
        ]
    )
    announcements = pd.DataFrame(
        [
            {
                "evidence_id": "ev1",
                "published_on": pd.Timestamp("2026-06-01T09:00:00Z"),
                "symbol": "ABC",
                "subject": "Order win",
            }
        ]
    )

    monkeypatch.setattr(event_evidence_store, "build_bhavcopy_evidence", lambda **_kwargs: bhavcopy.copy())
    monkeypatch.setattr(event_evidence_store, "build_announcement_evidence", lambda **_kwargs: announcements.copy())

    def fake_upsert_to_db(df, table, unique_keys, timescaledb_column=None):
        writes.append((table, df.copy(), list(unique_keys), timescaledb_column))

    monkeypatch.setattr(event_evidence_store, "upsert_to_db", fake_upsert_to_db)

    payload = event_evidence_store.build_event_evidence_store(from_date="2026-06-01", to_date="2026-06-01")

    assert payload["status"] == "ok"
    assert payload["bhavcopy"]["rows"] == 1
    assert payload["announcements"]["rows"] == 1
    assert writes[0][0] == event_evidence_store.BHAVCOPY_EVIDENCE_TABLE
    assert writes[0][2] == ["asof_date", "symbol"]
    assert writes[1][0] == event_evidence_store.ANNOUNCEMENT_EVIDENCE_TABLE
    assert writes[1][2] == ["published_on", "evidence_id"]


def test_event_evidence_store_dry_run_does_not_persist(monkeypatch):
    monkeypatch.setattr(
        event_evidence_store,
        "build_bhavcopy_evidence",
        lambda **_kwargs: pd.DataFrame([{"asof_date": pd.Timestamp("2026-06-01T00:00:00Z"), "symbol": "ABC"}]),
    )
    monkeypatch.setattr(
        event_evidence_store,
        "build_announcement_evidence",
        lambda **_kwargs: pd.DataFrame([{"published_on": pd.Timestamp("2026-06-01T09:00:00Z"), "symbol": "ABC"}]),
    )
    monkeypatch.setattr(event_evidence_store, "upsert_to_db", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("dry-run must not write")))

    payload = event_evidence_store.build_event_evidence_store(dry_run=True)

    assert payload["dry_run"] is True
    assert payload["bhavcopy"]["rows"] == 1
    assert payload["announcements"]["rows"] == 1


def test_company_memory_deterministic_review_is_review_input_only():
    context = {
        "symbol": "LUPIN",
        "technical": [{"rs_vs_benchmark": 0.08}],
        "candidates": [{"technical_score": 82.0, "setup_score": 78.0}],
        "announcement_evidence": [{"direction": "positive", "confidence": 0.8, "evidence_summary": "Material approval"}],
        "bhavcopy_evidence": [{"deal_pressure": "accumulation", "evidence_summary": "Net accumulation"}],
        "event_policy": [],
        "actions": [],
        "wait_signals": [],
    }

    review = company_memory_review.deterministic_review(context)

    assert review.recommended_signal == "BUY"
    assert review.confidence >= 0.65
    assert "Review input only" in review.deterministic_boundary
    assert any("technical score" in item for item in review.evidence_used)
    assert any("accumulation" in item for item in review.evidence_used)


def test_company_memory_deterministic_review_downgrades_buy_on_distribution_pressure():
    context = {
        "symbol": "ABC",
        "technical": [{"rs_vs_benchmark": 0.02}],
        "candidates": [{"technical_score": 84.0}],
        "announcement_evidence": [{"direction": "positive", "confidence": 0.9}],
        "bhavcopy_evidence": [{"deal_pressure": "distribution_or_pressure"}],
        "event_policy": [],
        "actions": [],
        "wait_signals": [],
    }

    review = company_memory_review.deterministic_review(context)

    assert review.recommended_signal == "WATCH"
    assert any("distribution" in item for item in review.risk_flags)


def test_company_memory_build_reviews_uses_bounded_symbols_and_persists_contract(monkeypatch):
    contexts = {
        "AAA": {
            "symbol": "AAA",
            "technical": [],
            "candidates": [{"technical_score": 70.0}],
            "announcement_evidence": [],
            "bhavcopy_evidence": [],
            "event_policy": [],
            "actions": [],
            "wait_signals": [],
        },
        "BBB": {
            "symbol": "BBB",
            "technical": [],
            "candidates": [{"technical_score": 90.0}],
            "announcement_evidence": [{"direction": "positive", "confidence": 0.9}],
            "bhavcopy_evidence": [],
            "event_policy": [],
            "actions": [],
            "wait_signals": [],
        },
    }
    writes = []

    monkeypatch.setattr(company_memory_review, "load_review_symbols", lambda **kwargs: ["AAA", "BBB"])
    monkeypatch.setattr(
        company_memory_review,
        "load_company_memory_context",
        lambda symbol, **kwargs: contexts[symbol],
    )
    monkeypatch.setattr(company_memory_review, "ensure_table", lambda: None)
    monkeypatch.setattr(company_memory_review, "upsert_to_db", lambda df, table, unique_keys: writes.append((df.copy(), table, unique_keys)))

    reviews, meta = company_memory_review.build_company_memory_reviews(
        asof_date=pd.Timestamp("2026-06-01T00:00:00Z"),
        limit=2,
        use_llm=False,
    )
    company_memory_review.persist_company_memory_reviews(reviews)

    assert meta["authority_scope"] == "review_input_only"
    assert len(reviews) == 2
    assert set(reviews["authority_scope"]) == {"review_input_only"}
    assert set(reviews["model_name"]) == {"deterministic_company_memory_v1"}
    assert set(reviews["prompt_id"]) == {"company_memory_review"}
    assert set(reviews["prompt_version"]) == {company_memory_review.PROMPT_VERSION}
    assert set(reviews["prompt_schema_version"]) == {company_memory_review.PROMPT_SCHEMA_VERSION}
    assert writes[0][1] == company_memory_review.TABLE_NAME
    assert writes[0][2] == ["review_date", "symbol"]
    assert str(writes[0][0]["fallback_used"].dtype) == "boolean"


def test_company_memory_stage_is_registered_before_risk():
    assert "company_memory" in pipeline.PIPELINE_STAGES
    assert pipeline.PIPELINE_STAGES.index("event_policy") < pipeline.PIPELINE_STAGES.index("company_memory")
    assert pipeline.PIPELINE_STAGES.index("company_memory") < pipeline.PIPELINE_STAGES.index("risk")


def test_operator_api_records_manual_review_decision(monkeypatch):
    writes: list[pd.DataFrame] = []
    wait_signal_writes: list[pd.DataFrame] = []

    monkeypatch.setattr(operator_api, "ensure_manual_review_decisions_table", lambda: None)
    monkeypatch.setattr(operator_api, "upsert_to_db", lambda df, *args, **kwargs: writes.append(df.copy()))
    monkeypatch.setattr(manual_review_state, "persist_wait_signals", lambda df: wait_signal_writes.append(df.copy()))

    payload = operator_api.record_manual_review_decision_payload(
        {
            "item_id": "action_manual_review:table:key",
            "item": {
                "item_id": "action_manual_review:table:key",
                "item_type": "action_manual_review",
                "source_table": "advisory_action_recommendations",
                "source_key": "key",
                "symbol": "ABC",
                "setup_id": "SETUP",
            },
            "decision": "watch_for_event",
            "rationale": "Need confirmation from next exchange filing.",
            "follow_up_event": "Management clarification",
            "operator_id": "rane",
        }
    )

    assert payload["status"] == "ok"
    assert payload["closing_decision"] is False
    assert payload["next_state"] == "waiting_for_event"
    assert payload["creates_wait_signal"] is True
    assert payload["mutates_portfolio"] is False
    assert payload["submits_order"] is False
    assert payload["wait_signal"]["table"] == manual_review_state.WAIT_SIGNALS_TABLE
    assert writes
    row = writes[0].iloc[0].to_dict()
    assert row["item_id"] == "action_manual_review:table:key"
    assert row["decision"] == "watch_for_event"
    assert row["symbol"] == "ABC"
    assert "Management clarification" in row["note_json"]
    assert wait_signal_writes
    signal = wait_signal_writes[0].iloc[0].to_dict()
    assert signal["symbol"] == "ABC"
    assert signal["signal_type"] == "clarification_filing"
    assert signal["generated_by"] == "manual_review_decision"
    assert "Management clarification" in signal["wait_question"]
    condition = json.loads(signal["condition_json"])
    assert condition["condition_type"] == "clarification_filing"
    assert condition["source_quality"]
    assert "published_on" in condition["required_evidence_fields"]


def test_operator_api_records_closing_manual_review_decision_without_side_effects(monkeypatch):
    writes: list[pd.DataFrame] = []
    wait_signal_writes: list[pd.DataFrame] = []

    monkeypatch.setattr(operator_api, "ensure_manual_review_decisions_table", lambda: None)
    monkeypatch.setattr(operator_api, "upsert_to_db", lambda df, *args, **kwargs: writes.append(df.copy()))
    monkeypatch.setattr(manual_review_state, "persist_wait_signals", lambda df: wait_signal_writes.append(df.copy()))

    payload = operator_api.record_manual_review_decision_payload(
        {
            "item_id": "action_manual_review:table:key",
            "item": {
                "item_id": "action_manual_review:table:key",
                "item_type": "action_manual_review",
                "source_table": "advisory_action_recommendations",
                "source_key": "key",
                "symbol": "ABC",
                "setup_id": "SETUP",
            },
            "decision": "downgrade_to_no_action",
            "rationale": "Evidence is immaterial and not actionable.",
            "operator_id": "rane",
        }
    )

    assert payload["status"] == "ok"
    assert payload["closing_decision"] is True
    assert payload["next_state"] == "closed_no_action"
    assert payload["creates_wait_signal"] is False
    assert payload["mutates_portfolio"] is False
    assert payload["mutates_action_recommendation"] is False
    assert payload["submits_order"] is False
    assert payload["wait_signal"] is None
    assert writes
    assert not wait_signal_writes


def test_operator_api_requires_follow_up_event_for_watch_signal(monkeypatch):
    writes: list[pd.DataFrame] = []
    wait_signal_writes: list[pd.DataFrame] = []

    monkeypatch.setattr(operator_api, "ensure_manual_review_decisions_table", lambda: None)
    monkeypatch.setattr(operator_api, "upsert_to_db", lambda df, *args, **kwargs: writes.append(df.copy()))
    monkeypatch.setattr(manual_review_state, "persist_wait_signals", lambda df: wait_signal_writes.append(df.copy()))

    try:
        operator_api.record_manual_review_decision_payload(
            {
                "item_id": "action_manual_review:table:key",
                "item": {
                    "item_id": "action_manual_review:table:key",
                    "item_type": "action_manual_review",
                    "source_table": "advisory_action_recommendations",
                    "source_key": "key",
                    "symbol": "ABC",
                },
                "decision": "watch_for_event",
                "rationale": "Need one specific future confirmation before acting.",
                "operator_id": "rane",
            }
        )
    except ValueError as exc:
        assert "follow_up_event is required" in str(exc)
    else:
        raise AssertionError("watch_for_event without follow_up_event should fail")

    assert not writes
    assert not wait_signal_writes


def test_operator_api_records_every_manual_review_decision_effect(monkeypatch):
    writes: list[pd.DataFrame] = []
    wait_signal_writes: list[pd.DataFrame] = []

    monkeypatch.setattr(operator_api, "ensure_manual_review_decisions_table", lambda: None)
    monkeypatch.setattr(operator_api, "upsert_to_db", lambda df, *args, **kwargs: writes.append(df.copy()))
    monkeypatch.setattr(manual_review_state, "persist_wait_signals", lambda df: wait_signal_writes.append(df.copy()))

    for decision in sorted(manual_review_state.ALLOWED_DECISIONS):
        payload = {
            "item_id": f"action_manual_review:table:key:{decision}",
            "item": {
                "item_id": f"action_manual_review:table:key:{decision}",
                "item_type": "action_manual_review",
                "source_table": "advisory_action_recommendations",
                "source_key": f"key:{decision}",
                "symbol": "ABC",
                "setup_id": "SETUP",
            },
            "decision": decision,
            "operator_id": "rane",
        }
        if decision != "add_operator_note":
            payload["rationale"] = f"Operator rationale for {decision}."
        if decision == "watch_for_event":
            payload["follow_up_event"] = "Management clarification filing."

        result = operator_api.record_manual_review_decision_payload(payload)
        effect = manual_review_state.decision_effect(decision)

        assert result["status"] == "ok"
        assert result["decision"] == decision
        assert result["next_state"] == effect.next_state
        assert result["closing_decision"] is effect.closes_item
        assert result["creates_wait_signal"] is effect.creates_wait_signal
        assert result["mutates_portfolio"] is False
        assert result["mutates_action_recommendation"] is False
        assert result["submits_order"] is False

    assert len(writes) == len(manual_review_state.ALLOWED_DECISIONS)
    assert len(wait_signal_writes) == 1
    signal = wait_signal_writes[0].iloc[0].to_dict()
    assert signal["signal_type"] == "clarification_filing"


def test_manual_review_state_declares_all_decision_effects():
    expected = {
        "needs_more_data": ("open_needs_more_data", False, False),
        "watch_for_event": ("waiting_for_event", False, True),
        "add_operator_note": ("annotated", False, False),
        "approve_for_manual_config": ("closed_approved_for_manual_config", True, False),
        "ignore": ("closed_ignored", True, False),
        "downgrade_to_no_action": ("closed_no_action", True, False),
        "mark_fixed": ("closed_fixed", True, False),
    }
    assert set(manual_review_state.ALLOWED_DECISIONS) == set(expected)
    for decision, (next_state, closes_item, creates_wait_signal) in expected.items():
        effect = manual_review_state.decision_effect(decision)
        assert effect.next_state == next_state
        assert effect.closes_item is closes_item
        assert effect.creates_wait_signal is creates_wait_signal
        assert effect.mutates_portfolio is False
        assert effect.mutates_action_recommendation is False
        assert effect.submits_order is False


def test_manual_review_state_reopens_waiting_item_after_wait_signal_match():
    waiting = manual_review_state.runtime_state_for_decision("watch_for_event")
    assert waiting.state == "waiting_for_event"
    assert waiting.active is True
    assert waiting.reopened_by_wait_signal is False

    reopened = manual_review_state.runtime_state_for_decision("watch_for_event", has_matched_wait_signal=True)
    assert reopened.state == "reopened_wait_signal_matched"
    assert reopened.active is False
    assert reopened.reopened_by_wait_signal is True
    assert reopened.suppression_reason == "reopened_as_wait_signal_followup"

    closed = manual_review_state.runtime_state_for_decision("ignore", has_matched_wait_signal=True)
    assert closed.state == "closed_ignored"
    assert closed.active is False
    assert closed.reopened_by_wait_signal is False
    assert closed.suppression_reason == "closed_by_operator"


def test_operator_api_builds_wait_signal_sections(monkeypatch):
    match_calls: list[dict[str, object]] = []
    load_match_calls: list[dict[str, object]] = []
    monkeypatch.setattr(
        operator_api,
        "load_wait_signals",
        lambda **kwargs: pd.DataFrame(
            [
                {
                    "signal_id": "sig-manual",
                    "created_at": pd.Timestamp("2026-06-01T09:00:00Z"),
                    "hypothesis_id": None,
                    "hypothesis_title": None,
                    "source_table": "advisory_manual_review_decisions",
                    "source_key": "review:1",
                    "symbol": "ABC",
                    "signal_type": "event_keywords",
                    "status": "active",
                    "priority": 80,
                    "expected_action": "MANUAL_REVIEW",
                    "operator_summary": "Wait for management clarification.",
                    "wait_question": "Has management clarified the order cancellation?",
                    "condition_json": json.dumps({"keywords": ["clarification", "order cancellation"]}),
                    "valid_from": pd.Timestamp("2026-06-01T09:00:00Z"),
                    "valid_until": pd.Timestamp("2026-06-15T09:00:00Z"),
                    "generated_by": "manual_review_decision",
                    "load_ts": pd.Timestamp("2026-06-01T09:00:00Z"),
                },
                {
                    "signal_id": "sig-playbook",
                    "created_at": pd.Timestamp("2026-06-01T09:00:00Z"),
                    "hypothesis_id": "H1",
                    "hypothesis_title": "Policy shock",
                    "source_table": "advisory_hypothesis_action_plans",
                    "source_key": "plan:1",
                    "symbol": "XYZ",
                    "signal_type": "price_close",
                    "status": "matched",
                    "priority": 90,
                    "expected_action": "REDUCE_EXPOSURE_REVIEW",
                    "operator_summary": "Wait for adverse price reaction.",
                    "wait_question": "Has XYZ closed below support?",
                    "condition_json": json.dumps({"operator": "close_below", "threshold": 100.0}),
                    "valid_from": pd.Timestamp("2026-06-01T09:00:00Z"),
                    "valid_until": pd.Timestamp("2026-06-15T09:00:00Z"),
                    "generated_by": "hypothesis_action_plan",
                    "load_ts": pd.Timestamp("2026-06-01T09:00:00Z"),
                },
            ]
        ),
    )
    monkeypatch.setattr(
        operator_api,
        "load_wait_signal_matches",
        lambda **kwargs: load_match_calls.append(kwargs) or pd.DataFrame(
            [
                {
                    "matched_at": pd.Timestamp("2026-06-02T09:00:00Z"),
                    "signal_id": "sig-playbook",
                    "hypothesis_id": "H1",
                    "symbol": "XYZ",
                    "signal_type": "price_close",
                    "expected_action": "REDUCE_EXPOSURE_REVIEW",
                    "match_status": "matched",
                    "match_score": 1.0,
                    "source_table": "dhan_ohlcv_daily",
                    "source_key": "XYZ:2026-06-02",
                    "observed_at": pd.Timestamp("2026-06-02T00:00:00Z"),
                    "observed_value": 98.0,
                    "threshold_value": 100.0,
                    "match_reason": "XYZ close below 100.0; latest close 98.0.",
                    "evidence_json": json.dumps({"close": 98.0}),
                    "load_ts": pd.Timestamp("2026-06-02T09:00:00Z"),
                }
            ]
        ),
    )
    monkeypatch.setattr(operator_api, "match_wait_signals", lambda **kwargs: match_calls.append(kwargs) or {"status": "ok", "matched_rows": 0})

    payload = operator_api.build_wait_signals_payload(limit=50)

    assert payload["status"] == "ok"
    assert payload["summary"]["total_signals"] == 2
    assert payload["summary"]["manual_review"] == 1
    assert payload["summary"]["playbook"] == 1
    assert any(row["reason"] == "source_rows_stale" for row in payload["source_warnings"])
    assert len(payload["sections"]["active"]) == 1
    assert len(payload["sections"]["matched"]) == 1
    manual_signal = payload["sections"]["active"][0]
    assert manual_signal["source_label"] == "Manual review"
    assert "clarification" in manual_signal["condition_summary"]
    matched_signal = payload["sections"]["matched"][0]
    assert matched_signal["latest_match"]["evidence_summary"] == "XYZ close below 100.0; latest close 98.0."
    assert matched_signal["condition_summary"] == "Wait for latest close to be below 100.0."
    assert match_calls == []
    assert set(load_match_calls[0]["signal_ids"]) == {"sig-manual", "sig-playbook"}

    match_payload = operator_api.run_wait_signal_match_payload({"symbol": "XYZ", "limit": 10})
    assert match_payload["status"] == "ok"
    assert match_payload["match_result"]["matched_rows"] == 0
    assert match_calls == [{"symbols": ["XYZ"], "limit": 10, "persist": True}]


def test_operator_api_filters_closed_manual_review_items(monkeypatch):
    monkeypatch.setattr(operator_api, "_table_exists", lambda table_name: table_name == operator_api.ACTION_RECOMMENDATIONS_TABLE)
    monkeypatch.setattr(
        operator_api,
        "sql_to_df",
        lambda *args, **kwargs: pd.DataFrame(
            [
                {
                    "asof_date": pd.Timestamp("2026-05-30T00:00:00Z"),
                    "updated_at": pd.Timestamp("2026-05-30T09:10:00Z"),
                    "symbol": "ABC",
                    "setup_id": "SETUP",
                    "action_code": "MANUAL_REVIEW",
                    "action_reason": "conflicting evidence",
                }
            ]
        ),
    )

    def fake_decisions(**_kwargs):
        item_id = "action_manual_review:advisory_action_recommendations:2026-05-30 00:00:00+00:00:ABC:SETUP"
        return {item_id: {"item_id": item_id, "decision": "ignore", "decided_at": pd.Timestamp("2026-05-30T10:00:00Z")}}

    monkeypatch.setattr(operator_api, "load_latest_manual_review_decisions", fake_decisions)
    monkeypatch.setattr(operator_api, "load_promotion_reviews", lambda **_kwargs: [])

    payload = operator_api.build_manual_review_payload(limit=20)

    assert payload["summary"]["closed_by_operator"] == 1
    assert payload["items"] == []


def test_operator_api_surfaces_matched_manual_review_wait_signal(monkeypatch):
    def fake_table_exists(table_name):
        return table_name in {operator_api.WAIT_SIGNALS_TABLE, operator_api.WAIT_SIGNAL_MATCHES_TABLE}

    def fake_sql_to_df(query, *args, **kwargs):
        if "advisory_wait_signal_matches" in query and "advisory_wait_signals" in query:
            return pd.DataFrame(
                [
                    {
                        "matched_at": pd.Timestamp("2026-06-02T09:00:00Z"),
                        "signal_id": "sig-manual",
                        "hypothesis_id": "manual_review",
                        "symbol": "ABC",
                        "signal_type": "clarification_filing",
                        "expected_action": "MANUAL_REVIEW",
                        "match_status": "matched",
                        "match_score": 1.0,
                        "match_source_table": "advisory_watch_events",
                        "match_source_key": "ANN-1",
                        "observed_at": pd.Timestamp("2026-06-02T08:30:00Z"),
                        "observed_value": None,
                        "threshold_value": None,
                        "match_reason": "Matched clarification filing required wait term: order cancellation.",
                        "evidence_json": json.dumps(
                            {
                                "subject": "Company clarifies order cancellation",
                                "concise_summary_text": "Management clarified that the cancelled order is immaterial.",
                                "wait_signal": {
                                    "signal_id": "sig-manual",
                                    "manual_review_item_id": "action_manual_review:source:key",
                                    "manual_review_source_table": "advisory_manual_review_decisions",
                                    "manual_review_source_key": "action_manual_review:source:key",
                                    "wait_question": "Has management clarified the order cancellation?",
                                    "generated_by": "manual_review_decision",
                                },
                            }
                        ),
                        "signal_created_at": pd.Timestamp("2026-06-01T09:00:00Z"),
                        "hypothesis_title": "Operator manual-review follow-up",
                        "signal_source_table": "advisory_manual_review_decisions",
                        "signal_source_key": "action_manual_review:source:key",
                        "signal_symbol": "ABC",
                        "signal_status": "matched",
                        "priority": 75,
                        "signal_expected_action": "MANUAL_REVIEW",
                        "operator_summary": "Wait for management clarification.",
                        "wait_question": "Has management clarified the order cancellation?",
                        "condition_json": json.dumps({"condition_type": "clarification_filing", "keywords": ["order cancellation"]}),
                        "valid_until": pd.Timestamp("2026-06-15T09:00:00Z"),
                        "generated_by": "manual_review_decision",
                    }
                ]
            )
        return pd.DataFrame()

    monkeypatch.setattr(operator_api, "_table_exists", fake_table_exists)
    monkeypatch.setattr(operator_api, "sql_to_df", fake_sql_to_df)
    monkeypatch.setattr(operator_api, "load_promotion_reviews", lambda **_kwargs: [])
    monkeypatch.setattr(operator_api, "load_latest_manual_review_decisions", lambda **_kwargs: {})

    payload = operator_api.build_manual_review_payload(limit=20)

    assert payload["summary"]["by_type"]["wait_signal_followup"] == 1
    item = payload["items"][0]
    assert item["item_type"] == "wait_signal_followup"
    assert item["symbol"] == "ABC"
    assert "previously recorded wait condition" in item["operator_summary"]
    followup = item["raw"]["wait_signal_followup"]
    assert followup["manual_review_item_id"] == "action_manual_review:source:key"
    assert followup["match_source_key"] == "ANN-1"
    assert "order cancellation" in followup["wait_question"]


def test_operator_api_replaces_waiting_original_with_matched_wait_followup(monkeypatch):
    manual_item_id = "action_manual_review:advisory_action_recommendations:2026-05-30 00:00:00+00:00:ABC:SETUP"

    def fake_table_exists(table_name):
        return table_name in {
            operator_api.ACTION_RECOMMENDATIONS_TABLE,
            operator_api.WAIT_SIGNALS_TABLE,
            operator_api.WAIT_SIGNAL_MATCHES_TABLE,
        }

    def fake_sql_to_df(query, *args, **kwargs):
        if "advisory_wait_signal_matches" in query and "advisory_wait_signals" in query:
            return pd.DataFrame(
                [
                    {
                        "matched_at": pd.Timestamp("2026-06-02T09:00:00Z"),
                        "signal_id": "sig-manual",
                        "hypothesis_id": "manual_review",
                        "symbol": "ABC",
                        "signal_type": "clarification_filing",
                        "expected_action": "MANUAL_REVIEW",
                        "match_status": "matched",
                        "match_score": 1.0,
                        "match_source_table": "advisory_watch_events",
                        "match_source_key": "ANN-1",
                        "observed_at": pd.Timestamp("2026-06-02T08:30:00Z"),
                        "observed_value": None,
                        "threshold_value": None,
                        "match_reason": "Matched clarification filing required wait term: order cancellation.",
                        "evidence_json": json.dumps(
                            {
                                "wait_signal": {
                                    "signal_id": "sig-manual",
                                    "manual_review_item_id": manual_item_id,
                                    "manual_review_source_table": "advisory_manual_review_decisions",
                                    "manual_review_source_key": manual_item_id,
                                    "wait_question": "Has management clarified the order cancellation?",
                                    "generated_by": "manual_review_decision",
                                }
                            }
                        ),
                        "signal_created_at": pd.Timestamp("2026-06-01T09:00:00Z"),
                        "hypothesis_title": "Operator manual-review follow-up",
                        "signal_source_table": "advisory_manual_review_decisions",
                        "signal_source_key": manual_item_id,
                        "signal_symbol": "ABC",
                        "signal_status": "matched",
                        "priority": 75,
                        "signal_expected_action": "MANUAL_REVIEW",
                        "operator_summary": "Wait for management clarification.",
                        "wait_question": "Has management clarified the order cancellation?",
                        "condition_json": json.dumps({"condition_type": "clarification_filing", "keywords": ["order cancellation"]}),
                        "valid_until": pd.Timestamp("2026-06-15T09:00:00Z"),
                        "generated_by": "manual_review_decision",
                    }
                ]
            )
        if operator_api.ACTION_RECOMMENDATIONS_TABLE in query:
            return pd.DataFrame(
                [
                    {
                        "asof_date": pd.Timestamp("2026-05-30T00:00:00Z"),
                        "updated_at": pd.Timestamp("2026-05-30T09:10:00Z"),
                        "symbol": "ABC",
                        "setup_id": "SETUP",
                        "action_code": "MANUAL_REVIEW",
                        "action_reason": "Needs clarification.",
                    }
                ]
            )
        return pd.DataFrame()

    monkeypatch.setattr(operator_api, "_table_exists", fake_table_exists)
    monkeypatch.setattr(operator_api, "sql_to_df", fake_sql_to_df)
    monkeypatch.setattr(operator_api, "load_promotion_reviews", lambda **_kwargs: [])
    monkeypatch.setattr(
        operator_api,
        "load_latest_manual_review_decisions",
        lambda **_kwargs: {
            manual_item_id: {
                "item_id": manual_item_id,
                "decision": "watch_for_event",
                "decided_at": pd.Timestamp("2026-06-01T09:00:00Z"),
            }
        },
    )

    payload = operator_api.build_manual_review_payload(limit=20)

    assert payload["summary"]["reopened_by_wait_signal"] == 1
    assert payload["summary"]["annotated_by_operator"] == 0
    assert payload["summary"]["by_type"] == {"wait_signal_followup": 1}
    assert len(payload["items"]) == 1
    assert payload["items"][0]["item_type"] == "wait_signal_followup"
    assert payload["items"][0]["raw"]["wait_signal_followup"]["manual_review_item_id"] == manual_item_id


def test_operator_api_suppresses_matched_wait_followup_after_original_closed(monkeypatch):
    manual_item_id = "action_manual_review:advisory_action_recommendations:2026-05-30 00:00:00+00:00:ABC:SETUP"

    def fake_table_exists(table_name):
        return table_name in {operator_api.WAIT_SIGNALS_TABLE, operator_api.WAIT_SIGNAL_MATCHES_TABLE}

    def fake_sql_to_df(query, *args, **kwargs):
        if "advisory_wait_signal_matches" in query and "advisory_wait_signals" in query:
            return pd.DataFrame(
                [
                    {
                        "matched_at": pd.Timestamp("2026-06-03T09:00:00Z"),
                        "signal_id": "sig-manual",
                        "hypothesis_id": "manual_review",
                        "symbol": "ABC",
                        "signal_type": "clarification_filing",
                        "expected_action": "MANUAL_REVIEW",
                        "match_status": "matched",
                        "match_score": 1.0,
                        "match_source_table": "advisory_watch_events",
                        "match_source_key": "ANN-2",
                        "observed_at": pd.Timestamp("2026-06-03T08:30:00Z"),
                        "observed_value": None,
                        "threshold_value": None,
                        "match_reason": "Matched clarification filing required wait term: order cancellation.",
                        "evidence_json": json.dumps(
                            {
                                "wait_signal": {
                                    "signal_id": "sig-manual",
                                    "manual_review_item_id": manual_item_id,
                                    "manual_review_source_table": "advisory_manual_review_decisions",
                                    "manual_review_source_key": manual_item_id,
                                    "wait_question": "Has management clarified the order cancellation?",
                                    "generated_by": "manual_review_decision",
                                }
                            }
                        ),
                        "signal_created_at": pd.Timestamp("2026-06-01T09:00:00Z"),
                        "hypothesis_title": "Operator manual-review follow-up",
                        "signal_source_table": "advisory_manual_review_decisions",
                        "signal_source_key": manual_item_id,
                        "signal_symbol": "ABC",
                        "signal_status": "matched",
                        "priority": 75,
                        "signal_expected_action": "MANUAL_REVIEW",
                        "operator_summary": "Wait for management clarification.",
                        "wait_question": "Has management clarified the order cancellation?",
                        "condition_json": json.dumps({"condition_type": "clarification_filing", "keywords": ["order cancellation"]}),
                        "valid_until": pd.Timestamp("2026-06-15T09:00:00Z"),
                        "generated_by": "manual_review_decision",
                    }
                ]
            )
        return pd.DataFrame()

    monkeypatch.setattr(operator_api, "_table_exists", fake_table_exists)
    monkeypatch.setattr(operator_api, "sql_to_df", fake_sql_to_df)
    monkeypatch.setattr(operator_api, "load_promotion_reviews", lambda **_kwargs: [])
    monkeypatch.setattr(
        operator_api,
        "load_latest_manual_review_decisions",
        lambda **_kwargs: {
            manual_item_id: {
                "item_id": manual_item_id,
                "decision": "ignore",
                "decided_at": pd.Timestamp("2026-06-03T10:00:00Z"),
            }
        },
    )

    payload = operator_api.build_manual_review_payload(limit=20)

    assert payload["summary"]["closed_by_operator"] == 1
    assert payload["summary"]["reopened_by_wait_signal"] == 0
    assert payload["summary"]["by_type"] == {}
    assert payload["items"] == []


def test_operator_journey_manual_review_wait_signal_match_reopens_review_only_candidate(monkeypatch):
    manual_item_id = "action_manual_review:advisory_action_recommendations:2026-05-30 00:00:00+00:00:ABC:SETUP"
    decision_writes: list[pd.DataFrame] = []
    wait_signal_writes: list[pd.DataFrame] = []

    monkeypatch.setattr(operator_api, "ensure_manual_review_decisions_table", lambda: None)
    monkeypatch.setattr(operator_api, "upsert_to_db", lambda df, *args, **kwargs: decision_writes.append(df.copy()))
    monkeypatch.setattr(manual_review_state, "persist_wait_signals", lambda df: wait_signal_writes.append(df.copy()))

    decision_payload = operator_api.record_manual_review_decision_payload(
        {
            "item_id": manual_item_id,
            "item": {
                "item_id": manual_item_id,
                "item_type": "action_manual_review",
                "source_table": "advisory_action_recommendations",
                "source_key": "2026-05-30 00:00:00+00:00:ABC:SETUP",
                "symbol": "ABC",
                "setup_id": "SETUP",
            },
            "decision": "watch_for_event",
            "rationale": "Wait for exchange clarification before changing the recommendation.",
            "follow_up_event": "order cancellation clarification filing",
            "operator_id": "rane",
        }
    )

    assert decision_payload["status"] == "ok"
    assert decision_payload["next_state"] == "waiting_for_event"
    assert decision_payload["creates_wait_signal"] is True
    assert decision_payload["mutates_portfolio"] is False
    assert decision_payload["submits_order"] is False
    assert decision_writes and wait_signal_writes

    wait_signal_row = wait_signal_writes[0].iloc[0].to_dict()
    monkeypatch.setattr(wait_signals, "ensure_tables", lambda: None)
    monkeypatch.setattr(wait_signals, "load_active_wait_signals", lambda **_kwargs: pd.DataFrame([wait_signal_row]))
    monkeypatch.setattr(
        wait_signals,
        "_load_source_events_for_match",
        lambda **_kwargs: pd.DataFrame(
            [
                {
                    "source_type": "announcement",
                    "source_table": "advisory_watch_events",
                    "source_key": "ANN-1",
                    "published_on": pd.Timestamp("2026-06-02T08:30:00Z"),
                    "symbol": "ABC",
                    "subject": "ABC files order cancellation clarification",
                    "concise_summary_text": "Management clarification says the cancelled order is immaterial.",
                    "source_url": "https://example.test/ann-1",
                }
            ]
        ),
    )

    match_result = wait_signals.match_wait_signals(symbols=["ABC"], persist=False)

    assert match_result["matched_rows"] == 1
    match_row = match_result["matches"][0]
    match_evidence = json.loads(match_row["evidence_json"])
    assert match_evidence["wait_signal"]["manual_review_item_id"] == manual_item_id
    assert match_evidence["wait_signal"]["generated_by"] == "manual_review_decision"

    joined_match_row = {
        **match_row,
        "match_source_table": match_row["source_table"],
        "match_source_key": match_row["source_key"],
        "signal_created_at": wait_signal_row["created_at"],
        "hypothesis_title": wait_signal_row["hypothesis_title"],
        "signal_source_table": wait_signal_row["source_table"],
        "signal_source_key": wait_signal_row["source_key"],
        "signal_symbol": wait_signal_row["symbol"],
        "signal_status": "matched",
        "priority": wait_signal_row["priority"],
        "signal_expected_action": wait_signal_row["expected_action"],
        "operator_summary": wait_signal_row["operator_summary"],
        "wait_question": wait_signal_row["wait_question"],
        "condition_json": wait_signal_row["condition_json"],
        "valid_until": wait_signal_row["valid_until"],
        "generated_by": wait_signal_row["generated_by"],
    }

    def fake_table_exists(table_name):
        return table_name in {
            operator_api.ACTION_RECOMMENDATIONS_TABLE,
            operator_api.WAIT_SIGNALS_TABLE,
            operator_api.WAIT_SIGNAL_MATCHES_TABLE,
        }

    def fake_operator_sql(query, *args, **kwargs):
        if "advisory_wait_signal_matches" in query and "advisory_wait_signals" in query:
            return pd.DataFrame([joined_match_row])
        if operator_api.ACTION_RECOMMENDATIONS_TABLE in query:
            return pd.DataFrame(
                [
                    {
                        "asof_date": pd.Timestamp("2026-05-30T00:00:00Z"),
                        "updated_at": pd.Timestamp("2026-05-30T09:10:00Z"),
                        "symbol": "ABC",
                        "setup_id": "SETUP",
                        "action_code": "MANUAL_REVIEW",
                        "action_reason": "Needs clarification before action.",
                    }
                ]
            )
        return pd.DataFrame()

    monkeypatch.setattr(operator_api, "_table_exists", fake_table_exists)
    monkeypatch.setattr(operator_api, "sql_to_df", fake_operator_sql)
    monkeypatch.setattr(operator_api, "load_promotion_reviews", lambda **_kwargs: [])
    monkeypatch.setattr(
        operator_api,
        "load_latest_manual_review_decisions",
        lambda **_kwargs: {
            manual_item_id: {
                "item_id": manual_item_id,
                "decision": "watch_for_event",
                "decided_at": pd.Timestamp(decision_payload["decided_at"]),
            }
        },
    )

    manual_payload = operator_api.build_manual_review_payload(limit=20)

    assert manual_payload["summary"]["reopened_by_wait_signal"] == 1
    assert manual_payload["summary"]["by_type"] == {"wait_signal_followup": 1}
    followup_item = manual_payload["items"][0]
    assert followup_item["item_type"] == "wait_signal_followup"
    assert followup_item["raw"]["wait_signal_followup"]["manual_review_item_id"] == manual_item_id
    assert followup_item["raw"]["wait_signal_followup"]["match_source_key"] == "ANN-1"

    monkeypatch.setattr(action_recommender, "table_exists", lambda table_name: True)
    monkeypatch.setattr(action_recommender, "sql_to_df", lambda *args, **kwargs: pd.DataFrame([joined_match_row]))

    candidates = action_recommender.build_matched_wait_signal_action_candidates(
        asof_date=pd.Timestamp("2026-06-03T00:00:00Z"),
        symbols=["ABC"],
        setup_ids=[action_recommender.MANUAL_REVIEW_WAIT_SIGNAL_SETUP_ID],
    )

    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate["action_code"] == "MANUAL_REVIEW"
    assert candidate["action_source"] == "manual_review_wait_signal"
    assert candidate["execution_mode"] == "review_only"
    candidate_context = json.loads(candidate["raw_context_json"])
    assert candidate_context["wait_signal_followup"]["manual_review_item_id"] == manual_item_id
    assert "do not create broker-executable trades" in candidate_context["wait_signal_followup"]["bridge_note"]


def test_operator_api_lists_operator_commands(monkeypatch):
    monkeypatch.setattr(operator_api, "ensure_operator_command_runs_table", lambda: None)
    monkeypatch.setattr(operator_api, "sql_to_df", lambda *args, **kwargs: pd.DataFrame([{"run_id": "run-1", "command_key": "operator_health_skip_dhan", "status": "ok", "command_args_json": '["python"]'}]))

    payload = operator_api.build_operator_commands_payload(limit=10)

    assert payload["status"] == "ok"
    assert payload["commands"][0]["key"] == "operator_smoke"
    assert payload["commands"][0]["risk"] == "safe_read_only"
    assert any(row["key"] == "operator_health_skip_dhan" for row in payload["commands"])
    superseded_command = next(row for row in payload["commands"] if row["key"] == "superseded_failure_cleanup_dry_run")
    assert superseded_command["dry_run"] is True
    assert superseded_command["risk"] == "safe_read_only"
    assert "--apply" not in superseded_command["args"]
    assert payload["recent_runs"][0]["command_args"] == ["python"]


def test_operator_api_runs_whitelisted_command_with_audit(monkeypatch):
    writes: list[pd.DataFrame] = []
    upsert_calls: list[dict[str, object]] = []

    class Completed:
        returncode = 0
        stdout = "health ok"
        stderr = ""

    monkeypatch.setattr(operator_api, "ensure_operator_command_runs_table", lambda: None)
    def fake_upsert(df, table, unique_keys, timescaledb_column=None):
        writes.append(df.copy())
        upsert_calls.append({"table": table, "unique_keys": unique_keys, "timescaledb_column": timescaledb_column})

    monkeypatch.setattr(operator_api, "upsert_to_db", fake_upsert)
    monkeypatch.setattr(operator_api.subprocess, "run", lambda *args, **kwargs: Completed())

    payload = operator_api.run_operator_command_payload(
        {
            "command_key": "operator_health_skip_dhan",
            "confirm": True,
            "operator_id": "rane",
            "requested_reason": "check health",
        }
    )

    assert payload["status"] == "ok"
    assert payload["run"]["stdout_tail"] == "health ok"
    assert len(writes) == 2
    assert writes[0].iloc[0]["status"] == "running"
    assert writes[1].iloc[0]["status"] == "ok"
    assert writes[1].iloc[0]["operator_id"] == "rane"
    assert {call["timescaledb_column"] for call in upsert_calls} == {None}
    assert {tuple(call["unique_keys"]) for call in upsert_calls} == {("run_id",)}


def test_operator_api_blocks_unknown_or_unconfirmed_operator_command():
    try:
        operator_api.run_operator_command_payload({"command_key": "operator_health_skip_dhan"})
    except ValueError as exc:
        assert "confirm=true" in str(exc)
    else:
        raise AssertionError("expected confirm error")

    try:
        operator_api.run_operator_command_payload({"command_key": "all_advisory", "confirm": True})
    except ValueError as exc:
        assert "Unknown command_key" in str(exc)
    else:
        raise AssertionError("expected unknown command error")


def test_operator_api_builds_event_policy_evaluation_payload(monkeypatch):
    summary = pd.DataFrame(
        [
            {
                "evaluated_at": pd.Timestamp("2026-05-30T00:00:00Z"),
                "horizon_days": 5,
                "group_type": "policy_class",
                "group_value": "ORDER_WIN",
                "matured_count": 12,
                "avg_forward_return_after_cost": 0.04,
                "hit_rate_after_cost": 0.58,
                "recommendation": "candidate_policy_strengthen",
            }
        ]
    )

    monkeypatch.setattr(operator_api, "_table_exists", lambda table_name: table_name == operator_api.EVENT_POLICY_EVAL_SUMMARY_TABLE)
    monkeypatch.setattr(operator_api, "sql_to_df", lambda query, *args, **kwargs: summary.copy())

    payload = operator_api.build_event_policy_evaluation_payload(limit=10)

    assert payload["status"] == "ok"
    assert payload["api_schema"]["endpoint"] == "/api/event-policy/evaluation"
    assert payload["api_schema"]["read_only"] is True
    assert payload["api_schema"]["broker_execution_enabled"] is False
    assert payload["summary"][0]["group_value"] == "ORDER_WIN"
    assert payload["summary"][0]["recommendation"] == "candidate_policy_strengthen"


def test_technical_threshold_promotion_review_is_manual_only(monkeypatch):
    persisted = []
    monkeypatch.setattr(
        technical_threshold_promotion,
        "load_setup",
        lambda setup_id: {
            "setup_id": "EVENT_OPPORTUNITY_V1",
            "setup_name": "Event Opportunity",
            "technical_thresholds": {"buy_total_min": 70},
        },
    )
    monkeypatch.setattr(
        technical_threshold_promotion,
        "load_calibration_config",
        lambda config_id: {
            "config_id": "cfg-1",
            "horizon_days": 5,
            "evaluated_at": pd.Timestamp("2026-05-01T00:00:00Z"),
            "threshold_config_json": '{"buy_total_min": 78}',
            "eligible_count": 20,
            "hit_rate_after_cost": 0.6,
            "avg_forward_return_after_cost": 0.04,
        },
    )
    monkeypatch.setattr(technical_threshold_promotion, "load_horizon_summary", lambda horizon_days, evaluated_at: {"best_config_id": "cfg-1"})
    monkeypatch.setattr(technical_threshold_promotion, "persist_review", lambda result: persisted.append(result))

    result = technical_threshold_promotion.generate_promotion_review(
        setup_id="EVENT_OPPORTUNITY_V1",
        config_id="cfg-1",
        use_llm=False,
        persist=True,
    )

    assert result["review_status"] == "disabled"
    assert result["pending_patch"]["mode"] == "manual_review_only"
    assert result["pending_patch"]["path"] == "config/advisory_setups.yaml"
    assert result["prompt_id"] == "technical_threshold_promotion_review"
    assert result["prompt_version"] == technical_threshold_promotion.PROMPT_VERSION
    assert result["prompt_schema_version"] == technical_threshold_promotion.PROMPT_SCHEMA_VERSION
    assert result["llm_review"]["recommendation"] == "needs_more_data"
    assert len(persisted) == 1


def test_operator_api_builds_technical_promotion_review_payload(monkeypatch):
    called = {}

    def fake_review(**kwargs):
        called.update(kwargs)
        return {
            "status": "ok",
            "setup_id": kwargs["setup_id"],
            "config_id": kwargs["config_id"],
            "pending_patch": {"mode": "manual_review_only"},
            "llm_review": {"recommendation": "promote_partially"},
        }

    monkeypatch.setattr(operator_api, "generate_promotion_review", fake_review)

    payload = operator_api.build_technical_threshold_promotion_review_payload(
        {"setup_id": "EVENT_OPPORTUNITY_V1", "config_id": "cfg-1", "use_llm": True}
    )

    assert payload["pending_patch"]["mode"] == "manual_review_only"
    assert called["setup_id"] == "EVENT_OPPORTUNITY_V1"
    assert called["config_id"] == "cfg-1"
    assert called["persist"] is True


def test_technical_threshold_promotion_manual_decision_does_not_apply_patch(monkeypatch):
    writes = []
    monkeypatch.setattr(technical_threshold_promotion, "ensure_tables", lambda: None)
    monkeypatch.setattr(
        technical_threshold_promotion,
        "find_review",
        lambda **kwargs: {
            "reviewed_at": pd.Timestamp("2026-05-30T00:00:00Z"),
            "setup_id": "EVENT_OPPORTUNITY_V1",
            "config_id": "cfg-1",
            "recommendation": "promote_partially",
            "confidence": 0.7,
            "llm_review": {"recommendation": "promote_partially"},
            "calibration_evidence": {"eligible_count": 100},
            "patch": {
                "path": "config/advisory_setups.yaml",
                "mode": "manual_review_only",
                "setup_id": "EVENT_OPPORTUNITY_V1",
                "technical_thresholds": {"buy_total_min": 78},
            },
        },
    )
    monkeypatch.setattr(technical_threshold_promotion, "upsert_to_db", lambda df, *args, **kwargs: writes.append(df.copy()))

    result = technical_threshold_promotion.record_manual_decision(
        reviewed_at="2026-05-30T00:00:00Z",
        setup_id="EVENT_OPPORTUNITY_V1",
        config_id="cfg-1",
        decision="approved",
        operator_id="tester",
        decision_reason="sample looks acceptable",
    )

    assert result["applied"] is False
    assert result["final_patch"]["mode"] == "manual_apply_required"
    assert "technical_thresholds:" in result["final_patch"]["manual_patch_text"]
    assert writes[0].iloc[0]["decision"] == "approved"


def test_operator_api_records_technical_promotion_manual_decision(monkeypatch):
    called = {}

    def fake_decision(**kwargs):
        called.update(kwargs)
        return {
            "status": "ok",
            "decision": kwargs["decision"],
            "setup_id": kwargs["setup_id"],
            "config_id": kwargs["config_id"],
            "final_patch": {"mode": "manual_apply_required"},
            "applied": False,
        }

    monkeypatch.setattr(operator_api, "record_manual_decision", fake_decision)

    payload = operator_api.build_technical_threshold_review_decision_payload(
        {
            "reviewed_at": "2026-05-30T00:00:00Z",
            "setup_id": "EVENT_OPPORTUNITY_V1",
            "config_id": "cfg-1",
            "decision": "approved",
            "operator_id": "tester",
        }
    )

    assert payload["applied"] is False
    assert called["decision"] == "approved"
    assert called["operator_id"] == "tester"


def test_signal_quality_promotion_review_is_manual_only(monkeypatch):
    persisted = []
    monkeypatch.setattr(
        signal_quality_promotion,
        "load_signal_quality_summary",
        lambda **_kwargs: {
            "evaluated_at": pd.Timestamp("2026-05-01T00:00:00Z"),
            "horizon_days": 5,
            "variant": "technical_plus_all",
            "selected_count": 35,
            "matured_count": 35,
            "avg_forward_return_after_cost": 0.04,
            "hit_rate_after_cost": 0.55,
            "lift_vs_technical_only": 0.025,
        },
    )
    monkeypatch.setattr(signal_quality_promotion, "persist_review", lambda result: persisted.append(result))

    result = signal_quality_promotion.generate_promotion_review(
        evaluated_at="2026-05-01T00:00:00Z",
        horizon_days=5,
        variant="technical_plus_all",
        persist=True,
    )

    assert result["review_model"] == "deterministic_signal_quality_v1"
    assert result["pending_patch"]["mode"] == "manual_review_only"
    assert result["pending_patch"]["rule_suggestion"]["broker_execution_allowed"] is False
    assert result["llm_review"]["recommendation"] == "promote_overlay_review"
    assert len(persisted) == 1


def test_signal_quality_promotion_manual_decision_does_not_apply_patch(monkeypatch):
    writes = []
    monkeypatch.setattr(signal_quality_promotion, "ensure_tables", lambda: None)
    monkeypatch.setattr(
        signal_quality_promotion,
        "find_review",
        lambda **_kwargs: {
            "reviewed_at": pd.Timestamp("2026-05-30T00:00:00Z"),
            "evaluated_at": pd.Timestamp("2026-05-01T00:00:00Z"),
            "horizon_days": 5,
            "variant": "technical_plus_all",
            "recommendation": "promote_overlay_review",
            "confidence": 0.55,
            "llm_review": {"recommendation": "promote_overlay_review"},
            "signal_quality_evidence": {"matured_count": 35},
            "patch": {
                "path": "config/advisory_setups.yaml",
                "mode": "manual_review_only",
                "variant": "technical_plus_all",
                "rule_suggestion": {"signal_quality_overlay": "technical_plus_all"},
            },
        },
    )
    monkeypatch.setattr(signal_quality_promotion, "upsert_to_db", lambda df, *args, **kwargs: writes.append(df.copy()))

    result = signal_quality_promotion.record_manual_decision(
        reviewed_at="2026-05-30T00:00:00Z",
        evaluated_at="2026-05-01T00:00:00Z",
        horizon_days=5,
        variant="technical_plus_all",
        decision="approved",
        operator_id="tester",
        decision_reason="stable lift across manual checks",
    )

    assert result["applied"] is False
    assert result["final_patch"]["mode"] == "manual_apply_required"
    assert "signal_quality_overlay_rules:" in result["final_patch"]["manual_patch_text"]
    assert writes[0].iloc[0]["decision"] == "approved"


def test_operator_api_builds_signal_quality_promotion_review_payload(monkeypatch):
    called = {}

    def fake_review(**kwargs):
        called.update(kwargs)
        return {
            "status": "ok",
            "evaluated_at": kwargs["evaluated_at"],
            "horizon_days": kwargs["horizon_days"],
            "variant": kwargs["variant"],
            "pending_patch": {"mode": "manual_review_only"},
            "llm_review": {"recommendation": "promote_overlay_review"},
        }

    monkeypatch.setattr(operator_api, "generate_signal_quality_promotion_review", fake_review)

    payload = operator_api.build_signal_quality_promotion_review_payload(
        {"evaluated_at": "2026-05-01T00:00:00Z", "horizon_days": 5, "variant": "technical_plus_all"}
    )

    assert payload["api_schema"]["endpoint"] == "/api/signal-quality/promotion-review"
    assert payload["pending_patch"]["mode"] == "manual_review_only"
    assert called["persist"] is True
    assert called["variant"] == "technical_plus_all"


def test_operator_api_records_signal_quality_promotion_manual_decision(monkeypatch):
    called = {}

    def fake_decision(**kwargs):
        called.update(kwargs)
        return {
            "status": "ok",
            "decision": kwargs["decision"],
            "evaluated_at": kwargs["evaluated_at"],
            "horizon_days": kwargs["horizon_days"],
            "variant": kwargs["variant"],
            "final_patch": {"mode": "manual_apply_required"},
            "applied": False,
        }

    monkeypatch.setattr(operator_api, "record_signal_quality_manual_decision", fake_decision)

    payload = operator_api.build_signal_quality_promotion_review_decision_payload(
        {
            "reviewed_at": "2026-05-30T00:00:00Z",
            "evaluated_at": "2026-05-01T00:00:00Z",
            "horizon_days": 5,
            "variant": "technical_plus_all",
            "decision": "approved",
            "operator_id": "tester",
        }
    )

    assert payload["applied"] is False
    assert payload["api_schema"]["endpoint"] == "/api/signal-quality/promotion-review/decision"
    assert called["decision"] == "approved"
    assert called["operator_id"] == "tester"


def test_config_change_assistant_renders_technical_threshold_diff(tmp_path):
    config_path = tmp_path / "advisory_setups.yaml"
    config_path.write_text(
        """setups:
  - setup_id: EVENT_OPPORTUNITY_V1
    setup_name: Event Opportunity
    score_thresholds:
      pass_now: 0.70
      watch_event: 0.50
    technical_rules:
      - column: pass_above_dma_50
        operator: eq
        value: true
""",
        encoding="utf-8",
    )

    result = config_change_assistant.build_preview_from_patch(
        source_type="technical_threshold",
        source_key="review-1",
        config_path=config_path,
        patch_payload={
            "setup_id": "EVENT_OPPORTUNITY_V1",
            "technical_thresholds": {"buy_total_min": 78, "structure_min": 18},
        },
        persist=False,
    )

    assert result["applied"] is False
    assert "technical_thresholds:" in result["unified_diff"]
    assert "+      buy_total_min: 78" in result["unified_diff"]
    assert "Preview only" in result["safety_checks"][0]


def test_config_change_assistant_renders_signal_quality_overlay_diff(tmp_path):
    config_path = tmp_path / "advisory_setups.yaml"
    config_path.write_text("setups:\n  - setup_id: TEST\n    setup_name: Test\n", encoding="utf-8")

    result = config_change_assistant.build_preview_from_patch(
        source_type="signal_quality_overlay",
        source_key="review-2",
        config_path=config_path,
        patch_payload={
            "rule_suggestion": {
                "signal_quality_overlay": "technical_plus_all",
                "minimum_horizon_days": 5,
                "minimum_matured_rows": 40,
                "minimum_lift_vs_technical_only": 0.02,
            }
        },
        persist=False,
    )

    assert result["applied"] is False
    assert "signal_quality_overlay_rules:" in result["unified_diff"]
    assert "+  - authority: review_input_only" in result["unified_diff"]
    assert "+    broker_execution_allowed: false" in result["unified_diff"]


def test_operator_api_builds_config_change_previews(monkeypatch):
    technical_called = {}
    signal_called = {}

    def fake_technical(**kwargs):
        technical_called.update(kwargs)
        return {
            "status": "ok",
            "source_type": "technical_threshold",
            "config_path": "config/advisory_setups.yaml",
            "unified_diff": "---\n+++",
            "applied": False,
        }

    def fake_signal(**kwargs):
        signal_called.update(kwargs)
        return {
            "status": "ok",
            "source_type": "signal_quality_overlay",
            "config_path": "config/advisory_setups.yaml",
            "unified_diff": "---\n+++",
            "applied": False,
        }

    monkeypatch.setattr(operator_api, "build_technical_threshold_preview", fake_technical)
    monkeypatch.setattr(operator_api, "build_signal_quality_overlay_preview", fake_signal)

    technical_payload = operator_api.build_technical_config_change_preview_payload(
        {"reviewed_at": "2026-05-30T00:00:00Z", "setup_id": "EVENT_OPPORTUNITY_V1", "config_id": "cfg-1"}
    )
    signal_payload = operator_api.build_signal_quality_config_change_preview_payload(
        {
            "reviewed_at": "2026-05-30T00:00:00Z",
            "evaluated_at": "2026-05-01T00:00:00Z",
            "horizon_days": 5,
            "variant": "technical_plus_all",
        }
    )

    assert technical_payload["applied"] is False
    assert technical_payload["api_schema"]["endpoint"] == "/api/config-change/technical-threshold-preview"
    assert technical_called["setup_id"] == "EVENT_OPPORTUNITY_V1"
    assert signal_payload["applied"] is False
    assert signal_payload["api_schema"]["endpoint"] == "/api/config-change/signal-quality-preview"
    assert signal_called["variant"] == "technical_plus_all"


def test_prompt_registry_lists_prompt_contracts_with_no_broker_authority():
    payload = prompt_registry.build_prompt_registry_payload()
    prompt_ids = {row["prompt_id"] for row in payload["contracts"]}

    assert payload["status"] == "ok"
    assert "advisory_event_evaluation" in prompt_ids
    assert "company_memory_review" in prompt_ids
    assert "manual_revision_pointers" in prompt_ids
    assert payload["summary"]["broker_execution_allowed_count"] == 0
    assert all(row["broker_execution_allowed"] is False for row in payload["contracts"])


def test_prompt_registry_filters_by_owner_and_scope():
    owner_payload = prompt_registry.build_prompt_registry_payload(owner_area="company_memory")
    scope_payload = prompt_registry.build_prompt_registry_payload(authority_scope="extraction_only")

    assert {row["owner_area"] for row in owner_payload["contracts"]} == {"company_memory"}
    assert scope_payload["contracts"]
    assert {row["authority_scope"] for row in scope_payload["contracts"]} == {"extraction_only"}


def test_operator_api_builds_prompt_registry_payload():
    payload = operator_api.build_prompt_registry_api_payload(authority_scope="review_input_only")

    assert payload["api_schema"]["endpoint"] == "/api/research/prompt-registry"
    assert payload["api_schema"]["read_only"] is True
    assert payload["summary"]["broker_execution_allowed_count"] == 0
    assert {row["authority_scope"] for row in payload["contracts"]} == {"review_input_only"}


def test_watchlist_builder_marks_abstain_as_not_watch_enabled(monkeypatch):
    monkeypatch.setattr(
        watchlist_builder,
        "load_candidate_rows",
        lambda **kwargs: pd.DataFrame(
            [
                {
                    "asof_date": pd.Timestamp("2026-04-01T00:00:00Z"),
                    "setup_id": "TEST",
                    "setup_name": "Test",
                    "regime_name": "STABLE",
                    "symbol": "ABC",
                    "company_master_id": "nse:ABC",
                    "screener_slug": "demo",
                    "rank": 1,
                    "candidate_state": "ABSTAIN",
                    "watch_reason_detail": "explicit abstain",
                        "watch_reasons": "[]",
                        "setup_score": 0.42,
                        "entry_style": pd.NA,
                        "attractive_price_low": pd.NA,
                        "attractive_price_high": pd.NA,
                        "invalidation_price": pd.NA,
                    "entry_note": "Do nothing",
                    "near_miss_flag": False,
                }
            ]
        ),
    )
    monkeypatch.setattr(watchlist_builder, "load_latest_event_transitions", lambda **kwargs: pd.DataFrame())

    df = watchlist_builder.build_watchlist()
    row = df.iloc[0]
    assert row["current_state"] == "ABSTAIN"
    assert bool(row["watch_enabled"]) is False
    assert row["watch_status"] == "abstained"


def test_risk_engine_emits_abstained_allocation_status(monkeypatch):
    monkeypatch.setattr(risk_engine, "load_base_candidate_fallbacks", lambda **kwargs: pd.DataFrame())
    monkeypatch.setattr(
        risk_engine,
        "load_event_evaluations",
        lambda **kwargs: pd.DataFrame(
            [
                {
                    "published_on": pd.Timestamp("2026-04-01T00:00:00Z"),
                    "asof_date": pd.Timestamp("2026-04-01T00:00:00Z"),
                    "setup_id": "MIDCAP_IMPROVER_SWING_V1",
                    "setup_name": "Midcap improver",
                    "symbol": "ABC",
                    "company_master_id": "nse:ABC",
                    "unique_id": "ABC-abstain",
                    "evaluation_status": "completed",
                    "verdict": "continue",
                    "investable_now": False,
                    "materiality": "low",
                    "setup_effect": "neutral",
                    "event_class": "BASE_CANDIDATE",
                    "state_transition_hint": "NO_CHANGE",
                    "score_impact": 0.0,
                    "confidence": 0.2,
                    "sentiment": "neutral",
                    "governance_risk": "none",
                    "has_review_manual": False,
                }
            ]
        ),
    )
    monkeypatch.setattr(
        risk_engine,
        "load_watch_states",
        lambda **kwargs: pd.DataFrame(
            [
                {
                    "asof_date": pd.Timestamp("2026-04-01T00:00:00Z"),
                    "setup_id": "MIDCAP_IMPROVER_SWING_V1",
                    "symbol": "ABC",
                    "candidate_state": "ABSTAIN",
                    "current_state": "ABSTAIN",
                    "watch_status": "abstained",
                }
            ]
        ),
    )
    monkeypatch.setattr(risk_engine, "load_point_in_time_context", lambda symbol, published_on: {})

    df = risk_engine.build_allocations(asof_date=pd.Timestamp("2026-04-01T00:00:00Z"))
    row = df.iloc[0]
    assert row["allocation_status"] == "abstained"
    assert float(row["suggested_allocation_inr"]) == 0.0


def test_portfolio_engine_uses_setup_cap_override(monkeypatch):
    allocations = pd.DataFrame(
        [
            {
                "published_on": pd.Timestamp("2026-04-01T09:00:00Z"),
                "asof_date": pd.Timestamp("2026-04-01T00:00:00Z"),
                "setup_id": "INTRADAY_BREAKOUT_TACTICAL_V1",
                "setup_name": "Intraday breakout tactical",
                "symbol": "ABC",
                "company_master_id": "nse:ABC",
                "unique_id": "a1",
                "confidence": 0.90,
                "conviction_bucket": "high",
                "risk_bucket": "high",
                "suggested_allocation_inr": 50000.0,
                "allocation_pct_of_adv20d": 0.0001,
                "stop_price": 100.0,
                "invalidation_price": 95.0,
                "invalidation_rule": "rule",
                "notes": None,
                "context_snapshot_json": "{}",
            }
        ]
    )
    monkeypatch.setattr(portfolio_engine, "load_allocations", lambda **kwargs: allocations.copy())
    monkeypatch.setattr(portfolio_engine, "build_overlap_map", lambda symbols: {"ABC": ("symbol:ABC", "symbol_only")})
    monkeypatch.setattr(
        portfolio_engine,
        "get_setup_cap_overrides",
        lambda: {"INTRADAY_BREAKOUT_TACTICAL_V1": 0.15},
    )
    monkeypatch.setattr(
        portfolio_engine,
        "get_single_position_cap_overrides",
        lambda: {},
    )
    df = portfolio_engine.build_portfolio_orders(
        config=portfolio_engine.PortfolioConfig(
            capital_inr=100000.0,
            max_positions=5,
            single_position_cap_pct=1.0,
            per_setup_cap_pct=0.50,
            max_positions_per_overlap_group=1,
        )
    )
    row = df.iloc[0]
    assert row["approved_allocation_inr"] == 15000.0


def test_announcement_upsert_reports_deduplicates_unique_id_and_report_name(monkeypatch):
    captured = {}

    def fake_upsert_to_db(df, table, unique_keys, timescaledb_column=None):
        captured["df"] = df.copy()
        captured["table"] = table
        captured["unique_keys"] = list(unique_keys)

    monkeypatch.setattr(announcement_state, "upsert_to_db", fake_upsert_to_db)

    announcement_state.upsert_reports(
        [
            {
                "unique_id": "u1",
                "report_name": "OrderWinReport",
                "report_json": '{"version": 1}',
            },
            {
                "unique_id": "u1",
                "report_name": "OrderWinReport",
                "report_json": '{"version": 2}',
            },
            {
                "unique_id": "u1",
                "report_name": "PromoterReport",
                "report_json": '{"version": 1}',
            },
        ]
    )

    df = captured["df"]
    assert captured["table"] == announcement_state.REPORT_TABLE
    assert captured["unique_keys"] == ["unique_id", "report_name"]
    assert len(df) == 2
    selected = df.set_index(["unique_id", "report_name"])["report_json"].to_dict()
    assert selected[("u1", "OrderWinReport")] == '{"version": 2}'
    assert selected[("u1", "PromoterReport")] == '{"version": 1}'


def test_announcement_document_row_defaults_to_s3_pointer_mode(monkeypatch):
    monkeypatch.setattr(announcement_state, "POSTGRES_TEXT_MODE", "pointer")
    monkeypatch.setattr(announcement_state, "POSTGRES_TEXT_EXCERPT_CHARS", 12)
    announcement = Announcement(
        company_master_id="cm1",
        exchange="NSE",
        ticker="ABC",
        company_name="ABC Ltd",
        unique_id="ABC-1",
        subject="Order win",
        text="Exchange text",
        filed_under_category="Updates",
        exchange_category_id="cat",
        raw={"id": 1},
        published_on=datetime(2026, 5, 1),
        exchange_published_on=datetime(2026, 5, 1),
        three_page_ocr_text="first page ocr text",
        full_ocr_text="full ocr text that is longer",
        audio_transcript_text="audio transcript text",
        concise_summary_text="Short useful summary.",
    )

    row = announcement_state.document_row_from_announcement(announcement)

    assert row["three_page_ocr_text"] is None
    assert row["full_ocr_text"] is None
    assert row["audio_transcript_text"] is None
    assert row["ocr_s3_key"].endswith("/ocr_first_3_pages.txt")
    assert row["full_ocr_s3_key"].endswith("/ocr_full_document.txt")
    assert row["audio_transcript_s3_key"].endswith("/audio_transcript.txt")
    assert row["ocr_excerpt"] == "first page o"
    assert row["full_ocr_chars"] == len("full ocr text that is longer")
    assert row["concise_summary_text"] == "Short useful summary."


def test_announcement_report_rows_store_report_pointer_by_default(monkeypatch):
    monkeypatch.setattr(announcement_state, "POSTGRES_TEXT_MODE", "pointer")
    monkeypatch.setattr(announcement_state, "POSTGRES_TEXT_EXCERPT_CHARS", 20)
    monkeypatch.setattr(
        announcement_state,
        "save_report_artifact",
        lambda announcement, report: f"reports/{announcement.unique_id}/{report.report_name}.json",
    )
    announcement = Announcement(
        company_master_id="cm1",
        exchange="NSE",
        ticker="ABC",
        company_name="ABC Ltd",
        unique_id="ABC-1",
        subject="Order win",
        text="Exchange text",
        filed_under_category="Updates",
        exchange_category_id="cat",
        raw={"id": 1},
        published_on=datetime(2026, 5, 1),
        exchange_published_on=datetime(2026, 5, 1),
        parsed_reports=[
            ParsedReport(
                category="ORDER_WIN",
                report_name="OrderWinReport",
                model_name="test-model",
                data={"amount": 100, "customer": "railways"},
            )
        ],
    )

    row = announcement_state.report_rows_from_announcement(announcement)[0]

    assert row["report_json"] is None
    assert row["report_s3_key"] == "reports/ABC-1/OrderWinReport.json"
    assert row["report_chars"] > 0
    assert row["report_excerpt"].startswith("{")


def test_performance_slowlog_deduplicates_state(tmp_path):
    log_file = tmp_path / "slow.jsonl"
    state_file = tmp_path / "state.json"

    first = performance_slowlog.record_slow_operation(
        kind="api",
        operation="GET /api/test",
        elapsed_ms=100.0,
        threshold_ms=10.0,
        details={"route": "/api/test", "query": "a=1"},
        log_file=log_file,
        state_file=state_file,
    )
    second = performance_slowlog.record_slow_operation(
        kind="api",
        operation="GET /api/test",
        elapsed_ms=150.0,
        threshold_ms=10.0,
        details={"route": "/api/test", "query": "a=2"},
        log_file=log_file,
        state_file=state_file,
    )

    assert first is not None
    assert second is not None
    assert first["fingerprint"] == second["fingerprint"]
    assert first["is_new"] is True
    assert second["is_new"] is False

    summary = performance_slowlog.summarize_slow_operations(state_file=state_file)
    issue = summary["issues"][0]
    assert issue["count"] == 2
    assert issue["max_elapsed_ms"] == 150.0
    assert issue["last_details"]["query"] == "a=2"
    assert len(log_file.read_text(encoding="utf-8").splitlines()) == 2


def test_operator_api_updates_slow_issue_status(monkeypatch):
    calls = []

    def fake_update(fingerprint, *, status, note=None):
        calls.append({"fingerprint": fingerprint, "status": status, "note": note})
        return {"fingerprint": fingerprint, "status": status, "status_note": note}

    monkeypatch.setattr(operator_api, "update_slow_issue_status", fake_update)

    result = operator_api.update_slow_issue_payload({"fingerprint": "abc", "status": "triaged", "operator_id": "qa"})

    assert result["status"] == "ok"
    assert calls[0]["status"] == "triaged"
    assert calls[0]["note"] == "operator=qa"


def test_operator_api_requires_note_for_fixed_slow_issue():
    try:
        operator_api.update_slow_issue_payload({"fingerprint": "abc", "status": "fixed"})
    except ValueError as exc:
        assert "note is required" in str(exc)
    else:
        raise AssertionError("Expected ValueError")


def test_rule_engine_refresh_missing_snapshots_can_skip_intraday(monkeypatch):
    called = {"intraday": False}

    monkeypatch.setattr(rule_engine, "ensure_advisory_symbol_inputs", lambda symbols, to_date: {"status": "ok"})
    monkeypatch.setattr(rule_engine, "sync_peer_data", lambda symbols, to_date: {"status": "ok"})
    monkeypatch.setattr(rule_engine, "build_technical_features", lambda **kwargs: pd.DataFrame([{"symbol": "ABC"}]))
    monkeypatch.setattr(rule_engine, "persist_technical_features", lambda *args, **kwargs: None)
    monkeypatch.setattr(rule_engine, "build_fundamental_snapshot", lambda **kwargs: pd.DataFrame([{"symbol": "ABC"}]))
    monkeypatch.setattr(rule_engine, "persist_fundamental_snapshot", lambda *args, **kwargs: None)

    def fake_build_intraday_features(**kwargs):
        called["intraday"] = True
        return pd.DataFrame(), {}

    monkeypatch.setattr(rule_engine, "build_intraday_features", fake_build_intraday_features)
    monkeypatch.setattr(rule_engine, "persist_intraday_features", lambda *args, **kwargs: None)

    result = rule_engine.refresh_missing_snapshots(
        ["ABC"],
        pd.Timestamp("2026-04-01T00:00:00Z"),
        include_intraday=False,
    )

    assert called["intraday"] is False
    assert result["intraday_rows"] == 0
    assert result["intraday_meta"]["reason"] == "intraday_refresh_disabled"


def test_continuous_watch_build_price_alerts_entry_and_invalidation():
    watchlist = pd.DataFrame(
        [
            {
                "asof_date": pd.Timestamp("2026-04-08T00:00:00Z"),
                "setup_id": "TEST",
                "symbol": "ABC",
                "current_state": "WATCH_BREAKOUT",
                "attractive_price_low": 95.0,
                "attractive_price_high": 105.0,
                "invalidation_price": 90.0,
            },
            {
                "asof_date": pd.Timestamp("2026-04-08T00:00:00Z"),
                "setup_id": "TEST",
                "symbol": "XYZ",
                "current_state": "PASS_NOW",
                "attractive_price_low": 50.0,
                "attractive_price_high": 60.0,
                "invalidation_price": 48.0,
            },
        ]
    )
    latest_prices = pd.DataFrame(
        [
            {"symbol": "ABC", "timestamp": pd.Timestamp("2026-04-08T09:20:00Z"), "close": 100.0},
            {"symbol": "XYZ", "timestamp": pd.Timestamp("2026-04-08T09:20:00Z"), "close": 47.5},
        ]
    )

    alerts = continuous_watch.build_price_alerts(
        watchlist,
        latest_prices,
        observed_at=pd.Timestamp("2026-04-08T09:21:00Z"),
    )

    assert set(alerts["alert_type"].tolist()) == {"ENTRY_ZONE_HIT", "INVALIDATION_HIT"}
    entry_row = alerts[alerts["symbol"] == "ABC"].iloc[0]
    invalidation_row = alerts[alerts["symbol"] == "XYZ"].iloc[0]
    assert entry_row["alert_type"] == "ENTRY_ZONE_HIT"
    assert invalidation_row["alert_type"] == "INVALIDATION_HIT"


def test_continuous_watch_build_price_alerts_breakout_above_range():
    watchlist = pd.DataFrame(
        [
            {
                "asof_date": pd.Timestamp("2026-04-08T00:00:00Z"),
                "setup_id": "TEST",
                "symbol": "ABC",
                "current_state": "WATCH_BREAKOUT",
                "attractive_price_low": 95.0,
                "attractive_price_high": 105.0,
                "invalidation_price": 90.0,
            }
        ]
    )
    latest_prices = pd.DataFrame(
        [
            {"symbol": "ABC", "timestamp": pd.Timestamp("2026-04-08T09:20:00Z"), "close": 110.0},
        ]
    )

    alerts = continuous_watch.build_price_alerts(
        watchlist,
        latest_prices,
        observed_at=pd.Timestamp("2026-04-08T09:21:00Z"),
    )

    assert len(alerts) == 1
    assert alerts.iloc[0]["alert_type"] == "BREAKOUT_ABOVE_RANGE"


def test_continuous_watch_build_price_alerts_for_open_positions_exit_points():
    watchlist = pd.DataFrame(
        [
            {
                "asof_date": pd.Timestamp("2026-04-08T00:00:00Z"),
                "setup_id": "TEST",
                "symbol": "ABC",
                "current_state": "OPEN_POSITION",
                "monitor_source": "position",
                "stop_price": 98.0,
                "invalidation_price": 95.0,
            },
            {
                "asof_date": pd.Timestamp("2026-04-08T00:00:00Z"),
                "setup_id": "TEST",
                "symbol": "XYZ",
                "current_state": "OPEN_POSITION",
                "monitor_source": "position",
                "stop_price": 48.0,
                "invalidation_price": 45.0,
            },
        ]
    )
    latest_prices = pd.DataFrame(
        [
            {"symbol": "ABC", "timestamp": pd.Timestamp("2026-04-08T09:20:00Z"), "close": 94.5},
            {"symbol": "XYZ", "timestamp": pd.Timestamp("2026-04-08T09:20:00Z"), "close": 47.5},
        ]
    )

    alerts = continuous_watch.build_price_alerts(
        watchlist,
        latest_prices,
        observed_at=pd.Timestamp("2026-04-08T09:21:00Z"),
    )

    assert set(alerts["alert_type"].tolist()) == {"POSITION_INVALIDATION_HIT", "STOP_HIT"}
    by_symbol = {row["symbol"]: row for row in alerts.to_dict(orient="records")}
    assert by_symbol["ABC"]["alert_type"] == "POSITION_INVALIDATION_HIT"
    assert by_symbol["XYZ"]["alert_type"] == "STOP_HIT"


def test_continuous_watch_load_monitored_universe_dedupes_same_symbol(monkeypatch):
    watchlist = pd.DataFrame(
        [
            {
                "asof_date": pd.Timestamp("2026-04-17T00:00:00Z"),
                "setup_id": "WATCH_A",
                "symbol": "ABC",
                "monitor_source": "watchlist",
                "rank": 3,
                "state_updated_at": pd.Timestamp("2026-04-17T08:00:00Z"),
            },
            {
                "asof_date": pd.Timestamp("2026-04-17T00:00:00Z"),
                "setup_id": "WATCH_B",
                "symbol": "XYZ",
                "monitor_source": "watchlist",
                "rank": 1,
                "state_updated_at": pd.Timestamp("2026-04-17T08:05:00Z"),
            },
        ]
    )
    positions = pd.DataFrame(
        [
            {
                "asof_date": pd.Timestamp("2026-04-17T00:00:00Z"),
                "setup_id": "POS_A",
                "symbol": "ABC",
                "monitor_source": "position",
            }
        ]
    )
    monkeypatch.setattr(continuous_watch, "load_active_watchlist", lambda **kwargs: watchlist.copy())
    monkeypatch.setattr(continuous_watch, "load_open_positions", lambda **kwargs: positions.copy())

    out = continuous_watch.load_monitored_universe(asof_date=pd.Timestamp("2026-04-17T00:00:00Z"))

    assert out[["symbol", "setup_id", "monitor_source"]].to_dict(orient="records") == [
        {"symbol": "ABC", "setup_id": "POS_A", "monitor_source": "position"},
        {"symbol": "XYZ", "setup_id": "WATCH_B", "monitor_source": "watchlist"},
    ]


def test_continuous_watch_ohlcv_cursor_does_not_advance_on_empty_pull():
    previous = pd.Timestamp("2026-04-08T09:20:00Z")
    requested_from = pd.Timestamp("2026-04-08T09:15:00Z")

    assert continuous_watch._next_cursor_after_pull(previous, requested_from, pd.NaT) == previous
    assert continuous_watch._next_cursor_after_pull(previous, requested_from, pd.Timestamp("2026-04-08T09:18:00Z")) == previous
    assert continuous_watch._next_cursor_after_pull(previous, requested_from, pd.Timestamp("2026-04-08T09:25:00Z")) == pd.Timestamp("2026-04-08T09:25:00Z")


def test_continuous_watch_ohlcv_cursor_repeats_initial_window_when_no_data():
    requested_from = pd.Timestamp("2026-04-08T09:15:00Z")

    assert continuous_watch._next_cursor_after_pull(pd.NaT, requested_from, pd.NaT) == requested_from


def test_continuous_watch_ohlcv_from_cursor_caps_stale_backfill():
    to_cursor = pd.Timestamp("2026-04-08T10:00:00Z")
    stale_from = pd.Timestamp("2026-04-07T10:00:00Z")
    capped, truncated = continuous_watch._bounded_intraday_from_cursor(stale_from, to_cursor, 240)

    assert truncated is True
    assert capped == pd.Timestamp("2026-04-08T06:00:00Z")


def test_continuous_watch_ohlcv_from_cursor_keeps_recent_backfill():
    to_cursor = pd.Timestamp("2026-04-08T10:00:00Z")
    recent_from = pd.Timestamp("2026-04-08T09:00:00Z")
    capped, truncated = continuous_watch._bounded_intraday_from_cursor(recent_from, to_cursor, 240)

    assert truncated is False
    assert capped == recent_from


def test_continuous_watch_event_cursor_caps_stale_backfill():
    to_cursor = pd.Timestamp("2026-04-08T10:00:00Z")
    stale_previous = pd.Timestamp("2026-04-07T09:00:00Z")
    capped, truncated = continuous_watch._bounded_event_from_cursor(
        stale_previous,
        to_cursor,
        initial_lookback_minutes=90,
        replay_minutes=15,
        max_lookback_minutes=240,
    )

    assert truncated is True
    assert capped == pd.Timestamp("2026-04-08T06:00:00Z")


def test_continuous_watch_event_cursor_uses_initial_window_without_state():
    to_cursor = pd.Timestamp("2026-04-08T10:00:00Z")
    from_cursor, truncated = continuous_watch._bounded_event_from_cursor(
        pd.NaT,
        to_cursor,
        initial_lookback_minutes=90,
        replay_minutes=15,
        max_lookback_minutes=240,
    )

    assert truncated is False
    assert from_cursor == pd.Timestamp("2026-04-08T08:30:00Z")


def test_event_router_build_routing_plan_merges_price_and_event_sources():
    alerts = pd.DataFrame(
        [
            {
                "symbol": "ABC",
                "setup_id": "SETUP_A",
                "alert_type": "ENTRY_ZONE_HIT",
                "asof_date": pd.Timestamp("2026-04-08T00:00:00Z"),
            }
        ]
    )
    announcement_events = pd.DataFrame(
        [
            {
                "symbol": "ABC",
                "setup_id": "SETUP_A",
                "load_ts": pd.Timestamp("2026-04-08T10:00:00Z"),
                "asof_date": pd.Timestamp("2026-04-08T00:00:00Z"),
            }
        ]
    )
    news_events = pd.DataFrame(
        [
            {
                "symbol": "XYZ",
                "setup_id": "SETUP_B",
                "load_ts": pd.Timestamp("2026-04-08T10:01:00Z"),
                "asof_date": pd.Timestamp("2026-04-08T00:00:00Z"),
            }
        ]
    )

    plan = event_router.build_routing_plan(
        alerts=alerts,
        announcement_events=announcement_events,
        news_events=news_events,
    )

    assert len(plan) == 2
    by_symbol = {row["symbol"]: row for row in plan}
    assert by_symbol["ABC"]["action_type"] == "refresh_symbol_full"
    assert by_symbol["ABC"]["start_at"] == "technicals"
    assert by_symbol["ABC"]["include_watch"] is True
    assert by_symbol["XYZ"]["action_type"] == "refresh_symbol_event"
    assert by_symbol["XYZ"]["start_at"] == "evaluate"


def test_event_router_limits_event_only_actions_and_prioritizes_price_alerts():
    alerts = pd.DataFrame(
        [
            {
                "symbol": "AAA",
                "setup_id": "SETUP_A",
                "alert_type": "INVALIDATION_HIT",
                "asof_date": pd.Timestamp("2026-04-08T00:00:00Z"),
            }
        ]
    )
    announcement_events = pd.DataFrame(
        [
            {"symbol": "BBB", "setup_id": "SETUP_B", "load_ts": pd.Timestamp("2026-04-08T10:00:00Z"), "asof_date": pd.Timestamp("2026-04-08T00:00:00Z")},
            {"symbol": "CCC", "setup_id": "SETUP_C", "load_ts": pd.Timestamp("2026-04-08T10:01:00Z"), "asof_date": pd.Timestamp("2026-04-08T00:00:00Z")},
            {"symbol": "DDD", "setup_id": "SETUP_D", "load_ts": pd.Timestamp("2026-04-08T10:02:00Z"), "asof_date": pd.Timestamp("2026-04-08T00:00:00Z")},
        ]
    )
    watch_priority = pd.DataFrame(
        [
            {"symbol": "AAA", "setup_id": "SETUP_A", "rank": 5},
            {"symbol": "BBB", "setup_id": "SETUP_B", "rank": 1},
            {"symbol": "CCC", "setup_id": "SETUP_C", "rank": 2},
            {"symbol": "DDD", "setup_id": "SETUP_D", "rank": 3},
        ]
    )

    plan = event_router.build_routing_plan(
        alerts=alerts,
        announcement_events=announcement_events,
        news_events=pd.DataFrame(),
        watchlist_priority=watch_priority,
        max_actions=3,
        max_event_only_actions=1,
    )

    assert len(plan) == 2
    assert plan[0]["symbol"] == "AAA"
    assert plan[0]["action_type"] == "refresh_symbol_price"
    event_only = [row for row in plan if row["action_type"] == "refresh_symbol_event"]
    assert len(event_only) == 1
    assert event_only[0]["symbol"] == "BBB"


def test_event_router_position_alerts_include_lifecycle():
    alerts = pd.DataFrame(
        [
            {
                "symbol": "ABC",
                "setup_id": "SETUP_A",
                "alert_type": "POSITION_INVALIDATION_HIT",
                "asof_date": pd.Timestamp("2026-04-08T00:00:00Z"),
            }
        ]
    )

    plan = event_router.build_routing_plan(
        alerts=alerts,
        announcement_events=pd.DataFrame(),
        news_events=pd.DataFrame(),
    )

    assert len(plan) == 1
    assert plan[0]["action_type"] == "refresh_symbol_price"
    assert plan[0]["stop_at"] == "lifecycle"
    assert plan[0]["include_lifecycle"] is True


def test_signal_refresh_prioritizes_exit_lifecycle_over_buy_action():
    signal = signal_refresh.choose_signal(
        symbol="ABC",
        reason="ohlcv",
        action={"action_code": "BUY", "invest_score_pct": 85, "action_reason": "Old buy thesis"},
        lifecycle={"next_action": "FULL_EXIT", "next_action_reason": "Pivot failed on high volume", "target_confidence": 0.7},
        rebalance=None,
        events=[],
    )

    assert signal["signal_action"] == "FULL_EXIT"
    assert signal["signal_status"] == "exit_or_reduce"
    assert signal["signal_source"] == "lifecycle"
    assert "Pivot failed" in signal["action_reason"]


def test_signal_refresh_maps_event_buy_watch_to_watch():
    signal = signal_refresh.choose_signal(
        symbol="ABC",
        reason="announcement",
        action=None,
        lifecycle=None,
        rebalance=None,
        events=[
            {
                "action_type": "BUY_WATCH",
                "confidence": 0.62,
                "action_reason": "Fresh order-win event needs technical trigger confirmation.",
            }
        ],
    )

    assert signal["signal_action"] == "WATCH"
    assert signal["signal_status"] == "watch_or_review"
    assert signal["signal_source"] == "event_policy"


def test_wait_signals_generate_price_and_event_waits(monkeypatch):
    monkeypatch.setattr(
        wait_signals,
        "load_recent_price",
        lambda symbol, asof=None: {
            "symbol": symbol,
            "date": pd.Timestamp("2026-04-08T00:00:00Z"),
            "close": 100.0,
        },
    )
    plans = pd.DataFrame(
        [
            {
                "planned_at": pd.Timestamp("2026-04-08T10:00:00Z"),
                "hypothesis_id": "HYP1",
                "source_table": "advisory_news_events",
                "source_key": "N1",
                "symbol": "ABC",
                "action_type": "BUY_WATCH",
                "operator_summary": "Wait for confirmation.",
                "action_plan_json": json.dumps({"follow_up_window_days": 5}),
            }
        ]
    )
    matches = pd.DataFrame(
        [
            {
                "hypothesis_id": "HYP1",
                "hypothesis_title": "Order win playbook",
                "source_table": "advisory_news_events",
                "source_key": "N1",
                "symbol": "ABC",
                "matched_terms_json": json.dumps(["order win", "large contract"]),
            }
        ]
    )

    out = wait_signals.build_wait_signals_from_action_plans(plans, matches)

    assert set(out["signal_type"]) == {"price_level", "event_keywords"}
    price_row = out[out["signal_type"] == "price_level"].iloc[0]
    condition = json.loads(price_row["condition_json"])
    assert condition["condition_type"] == "price_level"
    assert condition["operator"] == "gte"
    assert condition["threshold"] == 102.0


def test_wait_signal_condition_parser_supports_legacy_and_invalid_rows():
    legacy_price = wait_signals.normalize_wait_condition({"operator": "close_below", "threshold": "99.5"}, signal_type="price_close")
    assert legacy_price.valid is True
    assert legacy_price.condition_type == "price_level"
    assert legacy_price.operator == "lte"
    assert legacy_price.threshold == 99.5

    typed_event = wait_signals.normalize_wait_condition({"condition_type": "clarification_filing", "keywords": ["order cancelled"]})
    assert typed_event.valid is True
    assert typed_event.condition_type == "clarification_filing"
    assert "order cancelled" in typed_event.keywords
    assert typed_event.source_quality

    invalid = wait_signals.normalize_wait_condition({"condition_type": "mystery_condition"})
    assert invalid.valid is False
    assert "Unknown wait-signal condition_type" in str(invalid.issue_reason)


def test_wait_signal_matcher_routes_price_conditions(monkeypatch):
    monkeypatch.setattr(wait_signals, "ensure_tables", lambda: None)
    monkeypatch.setattr(
        wait_signals,
        "load_active_wait_signals",
        lambda **_kwargs: pd.DataFrame(
            [
                {
                    "signal_id": "sig-price",
                    "hypothesis_id": "manual_review",
                    "symbol": "ABC",
                    "signal_type": "price_level",
                    "expected_action": "MANUAL_REVIEW",
                    "source_table": "advisory_manual_review_decisions",
                    "source_key": "manual:item:1",
                    "generated_by": "manual_review_decision",
                    "wait_question": "Has ABC closed above 100?",
                    "condition_json": json.dumps(
                        wait_signals.build_price_level_condition(
                            operator="close_above",
                            threshold=100.0,
                            manual_review_item_id="manual:item:1",
                        )
                    ),
                }
            ]
        ),
    )
    monkeypatch.setattr(wait_signals, "load_recent_price", lambda symbol: {"date": pd.Timestamp("2026-04-10T00:00:00Z"), "close": 101.5})

    result = wait_signals.match_wait_signals(persist=False)

    assert result["matched_rows"] == 1
    assert result["issue_rows"] == 0
    match = result["matches"][0]
    assert match["signal_type"] == "price_level"
    assert match["observed_value"] == 101.5
    assert match["threshold_value"] == 100.0
    evidence = json.loads(match["evidence_json"])
    assert evidence["wait_signal"]["manual_review_item_id"] == "manual:item:1"
    assert evidence["wait_signal"]["manual_review_source_key"] == "manual:item:1"
    assert evidence["wait_signal"]["wait_question"] == "Has ABC closed above 100?"


def test_wait_signal_matcher_routes_typed_event_conditions(monkeypatch):
    monkeypatch.setattr(wait_signals, "ensure_tables", lambda: None)
    monkeypatch.setattr(
        wait_signals,
        "load_active_wait_signals",
        lambda **_kwargs: pd.DataFrame(
            [
                {
                    "signal_id": "sig-event",
                    "hypothesis_id": "manual_review",
                    "symbol": "ABC",
                    "signal_type": "clarification_filing",
                    "expected_action": "MANUAL_REVIEW",
                    "valid_from": pd.Timestamp("2026-04-01T00:00:00Z"),
                    "valid_until": pd.Timestamp("2026-04-30T00:00:00Z"),
                    "condition_json": json.dumps(
                        wait_signals.build_event_condition(
                            condition_type="clarification_filing",
                            keywords=["order cancellation"],
                            sources=["announcement"],
                        )
                    ),
                }
            ]
        ),
    )
    monkeypatch.setattr(
        wait_signals,
        "_load_source_events_for_match",
        lambda **_kwargs: pd.DataFrame(
            [
                {
                    "source_type": "announcement",
                    "source_table": "advisory_watch_events",
                    "source_key": "ANN1",
                    "published_on": pd.Timestamp("2026-04-15T09:00:00Z"),
                    "symbol": "ABC",
                    "subject": "Management clarification on order cancellation",
                    "concise_summary_text": "The company clarified the order cancellation impact.",
                }
            ]
        ),
    )

    result = wait_signals.match_wait_signals(persist=False)

    assert result["matched_rows"] == 1
    match = result["matches"][0]
    assert match["signal_type"] == "clarification_filing"
    assert "clarification filing" in match["match_reason"]
    evidence = json.loads(match["evidence_json"])
    assert evidence["condition_type"] == "clarification_filing"
    assert "source_quality" in evidence


def test_wait_signal_matcher_requires_operator_keywords_for_typed_events(monkeypatch):
    monkeypatch.setattr(wait_signals, "ensure_tables", lambda: None)
    monkeypatch.setattr(
        wait_signals,
        "load_active_wait_signals",
        lambda **_kwargs: pd.DataFrame(
            [
                {
                    "signal_id": "sig-event",
                    "hypothesis_id": "manual_review",
                    "symbol": "ABC",
                    "signal_type": "clarification_filing",
                    "expected_action": "MANUAL_REVIEW",
                    "valid_from": pd.Timestamp("2026-04-01T00:00:00Z"),
                    "valid_until": pd.Timestamp("2026-04-30T00:00:00Z"),
                    "condition_json": json.dumps(
                        wait_signals.build_event_condition(
                            condition_type="clarification_filing",
                            keywords=["order cancellation"],
                            sources=["announcement"],
                        )
                    ),
                }
            ]
        ),
    )
    monkeypatch.setattr(
        wait_signals,
        "_load_source_events_for_match",
        lambda **_kwargs: pd.DataFrame(
            [
                {
                    "source_type": "announcement",
                    "source_table": "advisory_watch_events",
                    "source_key": "ANN1",
                    "published_on": pd.Timestamp("2026-04-15T09:00:00Z"),
                    "symbol": "ABC",
                    "subject": "Management clarification",
                    "concise_summary_text": "The company issued a clarification unrelated to the operator's order condition.",
                }
            ]
        ),
    )

    result = wait_signals.match_wait_signals(persist=False)

    assert result["matched_rows"] == 0


def test_wait_signal_symbol_refresh_excludes_market_wide_waits(monkeypatch):
    captured: dict[str, object] = {}
    monkeypatch.setattr(wait_signals, "ensure_tables", lambda: None)

    def fake_sql_to_df(sql, params=None):
        captured["sql"] = sql
        captured["params"] = params
        return pd.DataFrame()

    monkeypatch.setattr(wait_signals, "sql_to_df", fake_sql_to_df)

    wait_signals.load_active_wait_signals(symbols=["ABC"], limit=25)

    assert "symbol IS NULL" not in str(captured["sql"])
    assert captured["params"][1] == ["ABC"]


def test_wait_signal_matcher_exposes_expiry_and_invalid_condition_issues(monkeypatch):
    monkeypatch.setattr(wait_signals, "ensure_tables", lambda: None)
    monkeypatch.setattr(
        wait_signals,
        "load_active_wait_signals",
        lambda **_kwargs: pd.DataFrame(
            [
                {
                    "signal_id": "sig-expiry",
                    "symbol": "ABC",
                    "signal_type": "expiry_only",
                    "condition_json": json.dumps({"condition_type": "expiry_only"}),
                },
                {
                    "signal_id": "sig-invalid",
                    "symbol": "XYZ",
                    "signal_type": "unknown",
                    "condition_json": json.dumps({"condition_type": "unknown"}),
                },
            ]
        ),
    )

    result = wait_signals.match_wait_signals(persist=False)

    assert result["matched_rows"] == 0
    assert result["issue_rows"] == 2
    by_signal = {row["signal_id"]: row for row in result["issues"]}
    assert by_signal["sig-expiry"]["condition_type"] == "expiry_only"
    assert "expiry_only waits" in by_signal["sig-expiry"]["issue_reason"]
    assert "Unknown wait-signal condition_type" in by_signal["sig-invalid"]["issue_reason"]


def test_external_task_queue_stable_task_id():
    row_a = external_task_queue.enqueue_task.__globals__["json_dumps"]({"symbol": "ABC"})
    row_b = external_task_queue.enqueue_task.__globals__["json_dumps"]({"symbol": "ABC"})

    assert row_a == row_b
    assert "ABC" in row_a


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


def test_download_queue_classifies_single_client_modules():
    dhan_daily = download_queue.classify_step({"module": "data.dhanlive.ohlcv", "purpose": "ohlcv", "args": ["--daily"]})
    dhan_master = download_queue.classify_step({"module": "data.dhanlive.scrip_master", "purpose": "master", "args": []})
    nse_module = download_queue.classify_step({"module": "data.nseindia.bhavcopy_downloader", "purpose": "bhavcopy", "args": []})
    screener_module = download_queue.classify_step({"module": "data.screenerin.screener_parser", "purpose": "screener", "args": []})
    safe_module = download_queue.classify_step({"module": "data.mospi.cpi", "purpose": "cpi", "args": []})

    assert dhan_daily["queue"] == "dhan"
    assert dhan_daily["task_type"] == "download_module"
    assert dhan_master["queue"] == "dhan"
    assert dhan_master["task_type"] == "dhan_scrip_master"
    assert nse_module["queue"] == "nse"
    assert nse_module["task_type"] == "nse_module"
    assert screener_module["queue"] == "screener"
    assert screener_module["task_type"] == "download_module"
    assert safe_module is None


def test_download_queue_dry_run_no_inline():
    result = download_queue.enqueue_download_work(phase="downloaders", run_non_queued=False, dry_run=True)

    assert result["status"] == "ok"
    assert result["dry_run"] is True
    assert result["queued_count"] > 0
    assert result["inline_count"] == 0
    assert result["skipped_inline_count"] > 0
    assert {row["queue_name"] for row in result["queued"]} & {"dhan", "nse"}


def test_dhan_ohlcv_table_setup_runs_once_per_process(monkeypatch):
    calls = {"execute": 0}

    class FakeCursor:
        def execute(self, *_args, **_kwargs):
            calls["execute"] += 1

    class FakeSession:
        def __enter__(self):
            return None, FakeCursor()

        def __exit__(self, *_args):
            return False

    monkeypatch.setattr(dhan_ohlcv, "_OHLCV_TABLES_ENSURED", False)
    monkeypatch.setattr(dhan_ohlcv, "db_session", lambda: FakeSession())

    dhan_ohlcv.ensure_ohlcv_tables()
    first_call_count = calls["execute"]
    dhan_ohlcv.ensure_ohlcv_tables()

    assert first_call_count > 0
    assert calls["execute"] == first_call_count


def test_announcement_pipeline_skips_malformed_nse_rows(monkeypatch):
    from data.announcements import pipeline as announcement_pipeline_module

    class FakeResponse:
        def json(self):
            return [
                "bad-row",
                {
                    "attchmntFile": "",
                    "exchdisstime": "08-Apr-2026 10:00:00",
                    "sort_date": "08-Apr-2026 10:00:00",
                    "desc": "Board Meeting",
                    "attchmntText": "Board meeting update",
                },
            ]

    pipe = object.__new__(announcement_pipeline_module.AnnouncementPipeline)
    monkeypatch.setattr(pipe, "_nse_get_with_retry", lambda *args, **kwargs: FakeResponse())
    company = CompanyMasterTarget(
        company_master_id="cm1",
        ticker="ABC",
        exchange="NSE",
        company_name="ABC Ltd",
    )

    rows = pipe._fetch_nse_announcements(company, pd.Timestamp("2026-04-08T00:00:00Z").to_pydatetime())

    assert len(rows) == 1
    assert rows[0].ticker == "ABC"
    assert rows[0].subject == "Board Meeting"


def test_signal_refresh_prioritizes_wait_signal_match():
    signal = signal_refresh.choose_signal(
        symbol="ABC",
        reason="news",
        action={"action_code": "WATCH", "action_reason": "Old watch"},
        lifecycle=None,
        rebalance=None,
        events=[],
        wait_matches=[
            {
                "expected_action": "REDUCE_EXPOSURE_REVIEW",
                "match_score": 1.0,
                "match_reason": "Price broke the wait-signal threshold.",
            }
        ],
    )

    assert signal["signal_action"] == "REDUCE_EXPOSURE_REVIEW"
    assert signal["signal_source"] == "wait_signal"
    assert signal["signal_status"] == "exit_or_reduce"
    assert "Matched wait signal" in signal["action_reason"]


def test_signal_refresh_does_not_escalate_positive_wait_signal_to_direct_buy():
    signal = signal_refresh.choose_signal(
        symbol="ABC",
        reason="announcement",
        action=None,
        lifecycle=None,
        rebalance=None,
        events=[],
        wait_matches=[
            {
                "expected_action": "BUY",
                "signal_type": "clarification_filing",
                "match_score": 0.8,
                "match_reason": "Matched clarification filing wait term.",
                "evidence_json": json.dumps(
                    {
                        "wait_signal": {
                            "condition_type": "clarification_filing",
                            "wait_question": "Has management clarified the order cancellation?",
                            "expected_action": "BUY",
                        }
                    }
                ),
            }
        ],
    )

    assert signal["signal_action"] == "WATCH"
    assert signal["signal_source"] == "wait_signal"
    assert signal["signal_status"] == "watch_or_review"
    assert signal["confidence"] == 0.8
    assert "Escalated safely from expected BUY to review-only WATCH" in signal["action_reason"]


def test_signal_refresh_keeps_manual_review_wait_signal_review_only():
    signal = signal_refresh.choose_signal(
        symbol="ABC",
        reason="announcement",
        action={"action_code": "BUY", "action_reason": "Older buy setup"},
        lifecycle=None,
        rebalance=None,
        events=[],
        wait_matches=[
            {
                "expected_action": "MANUAL_REVIEW",
                "signal_type": "clarification_filing",
                "match_score": 0.7,
                "match_reason": "Matched clarification filing wait term.",
                "evidence_json": json.dumps(
                    {
                        "wait_signal": {
                            "condition_type": "clarification_filing",
                            "manual_review_item_id": "action_manual_review:source:key",
                            "wait_question": "Has management clarified the order cancellation?",
                            "generated_by": "manual_review_decision",
                        }
                    }
                ),
            }
        ],
    )

    assert signal["signal_action"] == "MANUAL_REVIEW"
    assert signal["signal_source"] == "wait_signal"
    assert signal["signal_status"] == "watch_or_review"
    assert signal["confidence"] == 0.7
    assert "Expected follow-up action is MANUAL_REVIEW" in signal["action_reason"]
    assert "Original wait: Has management clarified the order cancellation?" in signal["action_reason"]


def test_signal_refresh_classifies_watcher_output_effects():
    wait_effect = signal_refresh.classify_signal_effect(
        signal={"signal_action": "MANUAL_REVIEW", "signal_source": "wait_signal"},
        action={"action_code": "WATCH"},
        wait_matches=[{"signal_type": "clarification_filing"}],
    )
    changed_effect = signal_refresh.classify_signal_effect(
        signal={"signal_action": "FULL_EXIT", "signal_source": "lifecycle"},
        action={"action_code": "BUY"},
        wait_matches=[],
    )
    evidence_effect = signal_refresh.classify_signal_effect(
        signal={"signal_action": "WATCH", "signal_source": "event_policy"},
        action={"action_code": "WATCH"},
        wait_matches=[],
    )

    assert wait_effect["effect_type"] == "wait_match_created"
    assert "wait-signal match" in str(wait_effect["effect_summary"])
    assert changed_effect["effect_type"] == "action_changed"
    assert "from BUY to FULL_EXIT" in str(changed_effect["effect_summary"])
    assert evidence_effect["effect_type"] == "evidence_only"


def test_event_router_execute_uses_signal_refresh(monkeypatch):
    calls: list[dict[str, object]] = []

    def fake_refresh_symbol(**kwargs):
        calls.append(kwargs)
        return {"symbol": kwargs["symbol"], "signal_action": "WATCH", "signal_status": "watch_or_review"}

    monkeypatch.setattr(signal_refresh, "refresh_symbol", fake_refresh_symbol)
    actions = event_router.execute_routing_plan(
        [
            {
                "symbol": "ABC",
                "setup_ids": ["SETUP_A"],
                "source_types": ["price_alert"],
                "action_type": "refresh_symbol_price",
                "asof_date": "2026-04-08T00:00:00Z",
                "reasons": ["ENTRY_ZONE_HIT"],
            }
        ]
    )

    assert calls[0]["symbol"] == "ABC"
    assert str(calls[0]["reason"]).startswith("router:price_alert")
    assert actions.iloc[0]["action_status"] == "ok"


def test_live_notifier_formats_operator_messages():
    from advisory import live_notifier

    assert "alerts count=2" in live_notifier.format_operator_message(
        "stockey:continuous_watch:alerts",
        {
            "published_at": "2026-04-08T10:00:00Z",
            "alert_count": 2,
            "alerts": [{"symbol": "ABC"}, {"symbol": "XYZ"}],
        },
    )
    assert "router planned=3 executed=2" in live_notifier.format_operator_message(
        "stockey:continuous_watch:router",
        {
            "published_at": "2026-04-08T10:00:00Z",
            "planned_actions": 3,
            "executed_actions": 2,
        },
    )


def test_live_notifier_appends_operator_feed(tmp_path):
    from advisory import live_notifier

    entry = live_notifier.append_operator_event(
        output_dir=tmp_path,
        channel="stockey:continuous_watch:alerts",
        payload={
            "published_at": "2026-04-08T10:00:00Z",
            "alert_count": 1,
            "alerts": [{"symbol": "ABC"}],
        },
        max_items=10,
    )

    assert entry["channel"] == "stockey:continuous_watch:alerts"
    feed = json.loads((tmp_path / "operator_feed.json").read_text(encoding="utf-8"))
    assert len(feed) == 1
    assert "alerts count=1" in feed[0]["message"]
    assert (tmp_path / "operator_feed.jsonl").exists()
    assert (tmp_path / "operator_feed.txt").exists()


def test_continuous_watch_records_failed_cycle_in_sync_state(monkeypatch):
    persisted = []
    published = []

    monkeypatch.setattr(continuous_watch, "ensure_sync_state_table", lambda: None)
    monkeypatch.setattr(continuous_watch, "ensure_alerts_table", lambda: None)
    monkeypatch.setattr(continuous_watch, "_is_due", lambda source_name, interval_seconds: source_name == "continuous_watch:ohlcv")
    monkeypatch.setattr(continuous_watch, "run_ohlcv_cycle", lambda **kwargs: (_ for _ in ()).throw(ValueError("bad ohlcv")))
    monkeypatch.setattr(continuous_watch, "route_live_updates", lambda: {"status": "ok"})
    monkeypatch.setattr(continuous_watch, "run_operator_frontend_cycle", lambda: {"status": "ok"})
    monkeypatch.setattr(continuous_watch, "persist_sync_state", lambda **kwargs: persisted.append(kwargs))
    monkeypatch.setattr(continuous_watch, "publish_bus_message", lambda channel, payload: published.append((channel, payload)) or True)

    summary = continuous_watch.run_once(
        ohlcv_interval_seconds=1,
        news_interval_seconds=1,
        announcement_interval_seconds=1,
        intraday_interval_minutes=1,
        ohlcv_max_lookback_minutes=240,
    )

    assert summary["status"] == "error"
    assert summary["cycles"]["ohlcv"]["status"] == "error"
    assert any(row["source_name"] == "continuous_watch:ohlcv" and row["status"] == "error" for row in persisted)
    assert any(channel == "stockey:continuous_watch:ohlcv" and payload["status"] == "error" for channel, payload in published)


def test_continuous_watch_news_cycle_persists_bounded_catchup_window(monkeypatch):
    persisted = []
    published = []
    captured = {}

    monkeypatch.setattr(
        continuous_watch,
        "load_sync_state",
        lambda source_name: {"last_item_ts": pd.Timestamp.utcnow() - pd.Timedelta(days=3)},
    )
    monkeypatch.setattr(continuous_watch, "persist_news_events", lambda events: None)
    monkeypatch.setattr(continuous_watch, "persist_sync_state", lambda **kwargs: persisted.append(kwargs))
    monkeypatch.setattr(continuous_watch, "publish_bus_message", lambda channel, payload: published.append((channel, payload)) or True)

    def fake_run_news_watch(**kwargs):
        captured.update(kwargs)
        return pd.DataFrame(), {"matched_event_count": 0, "watch_count": 1}

    monkeypatch.setattr(continuous_watch, "run_news_watch", fake_run_news_watch)

    result = continuous_watch.run_news_cycle(interval_seconds=1, max_lookback_minutes=60)

    requested_from = pd.to_datetime(captured["published_from"], utc=True)
    assert 0 <= (pd.Timestamp.utcnow() - requested_from).total_seconds() <= 65 * 60
    assert result["catchup_truncated"] is True
    assert persisted[0]["state"]["catchup_truncated"] is True
    assert persisted[0]["state"]["max_lookback_minutes"] == 60
    assert "requested_from" in persisted[0]["state"]
    assert published[0][0] == "stockey:continuous_watch:news"


def test_continuous_watch_announcement_cycle_passes_bounded_catchup_window(monkeypatch):
    persisted = []
    captured = {}

    monkeypatch.setattr(
        continuous_watch,
        "load_sync_state",
        lambda source_name: {"last_item_ts": pd.Timestamp.utcnow() - pd.Timedelta(days=2)},
    )
    monkeypatch.setattr(continuous_watch, "persist_watch_outputs", lambda watch_updates, events: None)
    monkeypatch.setattr(continuous_watch, "persist_sync_state", lambda **kwargs: persisted.append(kwargs))
    monkeypatch.setattr(continuous_watch, "publish_bus_message", lambda channel, payload: True)

    def fake_run_announcement_watch(**kwargs):
        captured.update(kwargs)
        return pd.DataFrame(), pd.DataFrame(), {"match_count": 0, "watch_count": 1}

    monkeypatch.setattr(continuous_watch, "run_announcement_watch", fake_run_announcement_watch)

    result = continuous_watch.run_announcement_cycle(interval_seconds=1, max_lookback_minutes=120)

    requested_from = pd.to_datetime(captured["market_context_last_checked_at"], utc=True)
    assert 0 <= (pd.Timestamp.utcnow() - requested_from).total_seconds() <= 125 * 60
    assert result["catchup_truncated"] is True
    assert persisted[0]["state"]["catchup_truncated"] is True
    assert persisted[0]["state"]["max_lookback_minutes"] == 120
    assert "requested_from" in persisted[0]["state"]


def test_continuous_watch_publishes_skipped_due_cycles(monkeypatch):
    published = []

    monkeypatch.setattr(continuous_watch, "ensure_sync_state_table", lambda: None)
    monkeypatch.setattr(continuous_watch, "ensure_alerts_table", lambda: None)
    monkeypatch.setattr(continuous_watch, "_is_due", lambda source_name, interval_seconds: False)
    monkeypatch.setattr(continuous_watch, "route_live_updates", lambda: {"status": "ok"})
    monkeypatch.setattr(continuous_watch, "run_operator_frontend_cycle", lambda: {"status": "ok"})
    monkeypatch.setattr(continuous_watch, "publish_bus_message", lambda channel, payload: published.append((channel, payload)) or True)

    summary = continuous_watch.run_once(
        ohlcv_interval_seconds=1,
        news_interval_seconds=1,
        announcement_interval_seconds=1,
        intraday_interval_minutes=1,
        ohlcv_max_lookback_minutes=240,
    )

    assert summary["status"] == "ok"
    assert summary["cycles"]["ohlcv"] == {"status": "skipped", "reason": "not_due"}
    assert summary["cycles"]["announcements"] == {"status": "skipped", "reason": "not_due"}
    assert summary["cycles"]["news"] == {"status": "skipped", "reason": "not_due"}
    skipped_by_channel = {
        channel: payload
        for channel, payload in published
        if channel in {
            "stockey:continuous_watch:ohlcv",
            "stockey:continuous_watch:announcements",
            "stockey:continuous_watch:news",
        }
    }
    assert set(skipped_by_channel) == {
        "stockey:continuous_watch:ohlcv",
        "stockey:continuous_watch:announcements",
        "stockey:continuous_watch:news",
    }
    assert all(payload["status"] == "skipped" and payload["reason"] == "not_due" for payload in skipped_by_channel.values())


def test_continuous_watch_publishes_lock_skipped_cycle_without_sync_state(monkeypatch):
    published = []
    persisted = []

    monkeypatch.setattr(continuous_watch, "publish_bus_message", lambda channel, payload: published.append((channel, payload)) or True)
    monkeypatch.setattr(continuous_watch, "persist_sync_state", lambda **kwargs: persisted.append(kwargs))

    summary = continuous_watch.publish_lock_skipped_cycle(lock_file="/tmp/stockey_watchers.lock", lock_pid="12345")

    assert persisted == []
    assert summary["status"] == "skipped"
    assert summary["reason"] == "lock_already_running"
    skipped_by_channel = {
        channel: payload
        for channel, payload in published
        if channel.startswith("stockey:continuous_watch:")
    }
    assert {
        "stockey:continuous_watch:ohlcv",
        "stockey:continuous_watch:announcements",
        "stockey:continuous_watch:news",
        "stockey:continuous_watch:router",
        "stockey:continuous_watch:operator_frontend",
        "stockey:continuous_watch:wait_signals",
        "stockey:continuous_watch:operator_snapshot",
        "stockey:continuous_watch:trace_summary_store",
        "stockey:continuous_watch:summary",
    }.issubset(set(skipped_by_channel))
    assert skipped_by_channel["stockey:continuous_watch:ohlcv"]["status"] == "skipped"
    assert skipped_by_channel["stockey:continuous_watch:ohlcv"]["reason"] == "lock_already_running"
    assert skipped_by_channel["stockey:continuous_watch:ohlcv"]["lock_file"] == "/tmp/stockey_watchers.lock"
    assert skipped_by_channel["stockey:continuous_watch:summary"]["cycles"]["wait_signals"]["status"] == "skipped"


def test_live_dashboard_loads_operator_feed(tmp_path):
    from advisory import live_dashboard

    operator_feed = [
        {
            "received_at": "2026-04-08T10:00:00Z",
            "channel": "stockey:continuous_watch:alerts",
            "message": "2026-04-08T10:00:00Z alerts count=1 symbols=ABC",
        }
    ]
    (tmp_path / "operator_feed.json").write_text(json.dumps(operator_feed), encoding="utf-8")

    loaded = live_dashboard.load_operator_feed(output_dir=tmp_path)

    assert len(loaded) == 1
    assert loaded[0]["channel"] == "stockey:continuous_watch:alerts"
    assert "alerts count=1" in loaded[0]["message"]


def test_sync_many_daily_continues_after_symbol_error(monkeypatch):
    monkeypatch.setattr(dhan_ohlcv, "DhanHistoricalClient", lambda: object())

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
    monkeypatch.setattr(dhan_ohlcv, "DhanHistoricalClient", lambda: object())

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


def test_wpi_sync_uses_env_backed_lookback_days(monkeypatch):
    monkeypatch.setattr(wpi, "WPI_LOOKBACK_DAYS", 365)
    monkeypatch.setattr(wpi, "sync_wpi_for_year", lambda year, today=None: {"year": year})
    monkeypatch.setattr(wpi, "expected_month_count_for_year", lambda year, today=None: 12)

    results = wpi.sync_wpi()

    expected_start_year = (date.today() - pd.Timedelta(days=365)).year
    assert results[0]["year"] == expected_start_year
    assert results[-1]["year"] == date.today().year


def test_cpi_sync_uses_env_backed_lookback_days(monkeypatch):
    monkeypatch.setattr(cpi, "CPI_LOOKBACK_DAYS", 365)
    monkeypatch.setattr(cpi, "load_existing_cpi_months", lambda: set())
    monkeypatch.setattr(cpi, "get_db_max_date", lambda *_args, **_kwargs: None)
    captured = {"months": []}

    def fake_download(month_start):
        captured["months"].append(month_start)

    monkeypatch.setattr(cpi, "download_cpi_month", fake_download)

    cpi.sync_cpi_data()

    assert captured["months"]
    assert captured["months"][0] == cpi.first_of_month(date.today() - cpi.relativedelta(days=365))


def test_fred_fallback_start_uses_env_backed_lookback_days(monkeypatch):
    from data.fred import us_macro

    monkeypatch.setattr(us_macro, "FRED_MACRO_LOOKBACK_DAYS", 365)

    def fake_table_has_date(*_args, **_kwargs):
        raise RuntimeError("db unavailable")

    captured = {"start": None}

    def fake_fetch(series_id, *, start, end):
        captured["start"] = start
        series = pd.Series([1.0], index=pd.to_datetime([start]), name=series_id)
        series.index.name = "date"
        return series

    monkeypatch.setattr(us_macro, "table_has_date", fake_table_has_date)
    monkeypatch.setattr(us_macro, "_fetch_single_fred_series", fake_fetch)
    monkeypatch.setattr(us_macro, "upsert_to_db", lambda *_args, **_kwargs: None)

    us_macro.fetch_fred_series(series={"DGS10": "ust10y_yield"}, resample=None)

    expected_latest = date.today() - pd.Timedelta(days=365)
    assert captured["start"] == expected_latest - pd.Timedelta(days=15)


def test_market_context_builds_top_fraction_and_summary(monkeypatch):
    asof_date = pd.Timestamp("2026-05-28T00:00:00Z")
    technical = pd.DataFrame(
        [
            {
                "asof_date": asof_date,
                "company_master_id": "nse:AAA",
                "symbol": "AAA",
                "series": "EQ",
                "sector_code": "BANK",
                "sector_name": "BANK",
                "adj_close": 100.0,
                "total_value": 1000.0,
                "avg_traded_value_20d": 1000.0,
                "avg_traded_value_60d": 900.0,
                "rs_vs_benchmark": 0.04,
                "rs_vs_sector": 0.01,
                "dist_52w_high": -2.0,
                "dma_50_slope_20d_pct": 3.0,
                "trend_persistence_60d": 0.8,
                "accumulation_days_20d": 3,
                "distribution_days_20d": 1,
                "pass_liquidity_20d": True,
                "pass_above_dma_50": True,
                "pass_trend_alignment": True,
                "pass_near_52w_high": True,
            },
            {
                "asof_date": asof_date,
                "company_master_id": "nse:BBB",
                "symbol": "BBB",
                "series": "EQ",
                "sector_code": "IT",
                "sector_name": "IT",
                "adj_close": 200.0,
                "total_value": 800.0,
                "avg_traded_value_20d": 800.0,
                "avg_traded_value_60d": 700.0,
                "rs_vs_benchmark": -0.01,
                "rs_vs_sector": 0.02,
                "dist_52w_high": -7.0,
                "dma_50_slope_20d_pct": 1.0,
                "trend_persistence_60d": 0.4,
                "accumulation_days_20d": 1,
                "distribution_days_20d": 2,
                "pass_liquidity_20d": True,
                "pass_above_dma_50": True,
                "pass_trend_alignment": False,
                "pass_near_52w_high": True,
            },
            {
                "asof_date": asof_date,
                "company_master_id": "nse:CCC",
                "symbol": "CCC",
                "series": "EQ",
                "sector_code": "METAL",
                "sector_name": "METAL",
                "adj_close": 80.0,
                "total_value": 600.0,
                "avg_traded_value_20d": 600.0,
                "avg_traded_value_60d": 550.0,
                "rs_vs_benchmark": 0.02,
                "rs_vs_sector": 0.01,
                "dist_52w_high": -12.0,
                "dma_50_slope_20d_pct": 1.0,
                "trend_persistence_60d": 0.3,
                "accumulation_days_20d": 1,
                "distribution_days_20d": 1,
                "pass_liquidity_20d": True,
                "pass_above_dma_50": True,
                "pass_trend_alignment": False,
                "pass_near_52w_high": True,
            },
            {
                "asof_date": asof_date,
                "company_master_id": "nse:DDD",
                "symbol": "DDD",
                "series": "EQ",
                "sector_code": "FMCG",
                "sector_name": "FMCG",
                "adj_close": 60.0,
                "total_value": 400.0,
                "avg_traded_value_20d": 400.0,
                "avg_traded_value_60d": 350.0,
                "rs_vs_benchmark": -0.03,
                "rs_vs_sector": -0.01,
                "dist_52w_high": -20.0,
                "dma_50_slope_20d_pct": -1.0,
                "trend_persistence_60d": 0.1,
                "accumulation_days_20d": 0,
                "distribution_days_20d": 3,
                "pass_liquidity_20d": True,
                "pass_above_dma_50": False,
                "pass_trend_alignment": False,
                "pass_near_52w_high": False,
            },
        ]
    )
    market_caps = pd.DataFrame(
        [
            {"symbol": "AAA", "market_cap": 1000.0, "market_cap_source_date": asof_date},
            {"symbol": "BBB", "market_cap": 900.0, "market_cap_source_date": asof_date},
            {"symbol": "CCC", "market_cap": 100.0, "market_cap_source_date": asof_date},
            {"symbol": "DDD", "market_cap": 50.0, "market_cap_source_date": asof_date},
        ]
    )
    events = pd.DataFrame(
        [
            {
                "symbol": "AAA",
                "announcement_event_count_7d": 1,
                "news_event_count_7d": 2,
                "evaluated_event_count_20d": 3,
                "positive_event_count_20d": 2,
                "negative_event_count_20d": 0,
            },
            {
                "symbol": "BBB",
                "announcement_event_count_7d": 0,
                "news_event_count_7d": 1,
                "evaluated_event_count_20d": 1,
                "positive_event_count_20d": 0,
                "negative_event_count_20d": 1,
            },
        ]
    )
    exchange = pd.DataFrame(
        [
            {"symbol": "AAA", "exchange_event_score": 0.2, "upcoming_earnings_14d": False},
            {"symbol": "BBB", "exchange_event_score": -0.1, "upcoming_earnings_14d": True},
        ]
    )

    monkeypatch.setattr(market_context, "load_latest_technical", lambda _asof: technical)
    monkeypatch.setattr(market_context, "load_latest_market_cap", lambda _asof: market_caps)
    monkeypatch.setattr(market_context, "load_event_counts", lambda _asof, symbols: events[events["symbol"].isin(symbols)])
    monkeypatch.setattr(market_context, "load_exchange_context", lambda _asof, symbols: exchange[exchange["symbol"].isin(symbols)])
    monkeypatch.setattr(
        market_context,
        "load_regime_context",
        lambda _asof: {"regime_name": "STABLE", "macro_risk_state": "LOW", "macro_stress_score": 0.1},
    )

    universe, summary = market_context.build_market_context(
        asof_date=asof_date,
        top_fraction=0.5,
        min_avg_traded_value_20d=1.0,
        min_price=1.0,
    )

    assert universe["symbol"].tolist() == ["AAA", "BBB"]
    assert universe["in_top_context"].all()
    assert summary.iloc[0]["universe_count"] == 4
    assert summary.iloc[0]["top_context_count"] == 2
    assert summary.iloc[0]["positive_event_count_20d"] == 2
    assert "Top-context universe has 2 symbols" in summary.iloc[0]["summary_text"]


def test_playbook_action_plan_downgrades_positive_action_in_weak_market(monkeypatch):
    monkeypatch.setattr(hypothesis_engine, "ensure_tables", lambda: None)
    monkeypatch.setattr(hypothesis_engine, "upsert_to_db", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        hypothesis_engine,
        "load_latest_market_context",
        lambda *args, **kwargs: {
            "summary": {
                "regime_name": "RISK_OFF",
                "macro_risk_state": "HIGH",
                "breadth_trend_alignment_pct": 30.0,
                "risk_off_score": 0.8,
            },
            "top_universe": [{"symbol": "ABC", "rank_pct": 12.5, "sector_name": "Capital Goods"}],
        },
    )
    matches = pd.DataFrame(
        [
            {
                "hypothesis_id": "PLAYBOOK_POSITIVE_V1",
                "hypothesis_title": "Positive event playbook",
                "status": "trusted_overlay",
                "source_table": "advisory_news_events",
                "source_key": "NEWS-1",
                "published_on": pd.Timestamp("2026-05-28T10:00:00Z"),
                "symbol": "ABC",
                "trigger_scope": "symbol",
                "suggested_action": "BUY",
                "action_reason": "Positive event matched a trusted playbook.",
                "expected_effect_json": "{}",
                "match_score": 0.8,
            }
        ]
    )

    out = hypothesis_engine.build_action_plans(matches, use_llm=False)

    assert out.iloc[0]["action_type"] == "BUY_WATCH"
    assert bool(out.iloc[0]["production_allowed"]) is False
    assert out.iloc[0]["market_context_adjustment"] == "positive_event_downgraded_by_market_context"
    checks = json.loads(out.iloc[0]["checks_json"])
    assert any(item["check_type"] == "market_context" and item["blocking"] for item in checks)


def test_pipeline_stage_order_includes_market_context_after_regime():
    assert pipeline.PIPELINE_STAGES.index("regime") < pipeline.PIPELINE_STAGES.index("market_context")
    assert pipeline.PIPELINE_STAGES.index("market_context") < pipeline.PIPELINE_STAGES.index("overlay")
    assert pipeline.PIPELINE_STAGES.index("review") < pipeline.PIPELINE_STAGES.index("event_policy")
    assert pipeline.PIPELINE_STAGES.index("event_policy") < pipeline.PIPELINE_STAGES.index("risk")


def test_fpi_update_uses_env_backed_lookback_days(monkeypatch):
    monkeypatch.setattr(fpi, "FPI_LOOKBACK_DAYS", 365)
    monkeypatch.setattr(fpi, "latest_downloaded_date", lambda today: None)
    captured = {"first_target_date": None}

    def fake_downloaded_for(target_date):
        if captured["first_target_date"] is None:
            captured["first_target_date"] = target_date
        return True, None

    monkeypatch.setattr(fpi.futils, "downloaded_for", fake_downloaded_for)
    calls = []
    monkeypatch.setattr(fpi, "get_fpi_data", lambda rdate: calls.append(rdate))

    fpi.update_fpi_data()

    assert calls == []
    expected_start = date.today() - pd.Timedelta(days=365)
    assert captured["first_target_date"] == min(
        date(expected_start.year, expected_start.month, fpi.futils.get_last_date(expected_start.year, expected_start.month).day),
        date.today(),
    )


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
