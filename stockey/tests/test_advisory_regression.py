from __future__ import annotations

import argparse
import json
import os
import sys
import types
from datetime import date
from datetime import datetime

import pandas as pd

from advisory import action_recommender, adversarial_review, announcement_watch, continuous_watch, dashboard, decision_trace, event_meta_model, event_model_artifact_store, event_model_data_prep, event_model_promotion_check, event_policy, event_policy_evaluator, event_router, execution_engine, exchange_events, exchange_features, external_task_queue, hypothesis_engine, intraday_features, live_dashboard, llm_event_evaluator, macro_features, market_context, master_pipeline, model_training_runner, news_overlay_engine, news_theme_engine, news_watch, operator_health, performance_slowlog, pipeline, portfolio_engine, position_lifecycle, regime_engine, research_ledger, risk_engine, rule_engine, setup_registry, setup_trace, signal_refresh, symbol_trace, technical_engine, technical_features, technical_threshold_calibration, technical_threshold_promotion, training_universe, ts_forecast_evaluator, ts_forecast_features, ts_forecast_workflow, wait_signals, watchlist_builder
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
        "action_recommendations": [{"symbol": "ABC", "action": "BUY"}, {"symbol": "XYZ", "action": "SELL"}],
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
    }

    monkeypatch.setattr(operator_api, "load_operator_payload", lambda **kwargs: payload)

    health = operator_api.build_health_payload()
    assert health["operator_controlled"] is True
    assert health["read_only"] is False
    assert operator_api.build_summary_payload()["summary"]["action_count"] == 2
    assert len(operator_api.build_actions_payload()["action_recommendations"]) == 2
    assert operator_api.build_portfolio_payload()["today_recommendations"][0]["symbol"] == "ABC"
    assert operator_api.build_watchlist_payload()["watch_recommendations"][0]["symbol"] == "WATCH"
    assert operator_api.build_events_payload(limit=1)["events"] == [{"unique_id": "event-1"}]
    assert operator_api.build_data_health_payload()["summary"]["alert_count"] == 1


def test_operator_health_summarizes_worst_status(monkeypatch, tmp_path):
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    (log_dir / "all_advisory.log").write_text("ok\nTraceback: sample failure\n", encoding="utf-8")

    monkeypatch.setattr(operator_health, "check_database", lambda: {"status": "ok", "message": "db ok"})
    monkeypatch.setattr(operator_health, "check_operator_api", lambda: {"status": "ok", "message": "api ok"})
    monkeypatch.setattr(operator_health, "check_trace_summaries", lambda: {"status": "ok", "message": "trace cache ok", "row_count": 1})
    monkeypatch.setattr(operator_health, "check_operator_snapshot", lambda: {"status": "ok", "message": "snapshot ok"})
    monkeypatch.setattr(operator_health, "check_sync_state_failures", lambda: [{"status": "ok", "message": "sync ok"}])
    monkeypatch.setattr(operator_health, "summarize_slow_operations", lambda limit=20: {"status": "ok", "returned_count": 0, "issues": []})
    monkeypatch.setattr(operator_health, "check_table_freshness", lambda: [{"status": "warn", "message": "stale", "name": "actions"}])
    monkeypatch.setattr(operator_health, "check_redis", lambda: {"status": "ok", "message": "redis ok"})
    monkeypatch.setattr(operator_health, "check_dhan_token", lambda: {"status": "ok", "message": "dhan ok"})
    monkeypatch.setattr(operator_health, "check_dhan_cache", lambda: {"status": "ok", "message": "dhan cache ok"})
    monkeypatch.setattr(operator_health, "check_optional_dependencies", lambda: [{"status": "ok", "message": "deps ok"}])
    monkeypatch.setattr(operator_health, "check_frontend_dependencies", lambda: {"status": "ok", "message": "frontend ok"})

    payload = operator_health.build_operator_health(log_dir=log_dir)

    assert payload["status"] == "error"
    assert payload["sections"]["table_freshness"][0]["status"] == "warn"
    assert payload["sections"]["cron_logs"][0]["status"] == "error"
    assert any(hint["title"] == "actions data is stale or missing" for hint in payload["fix_hints"])
    assert any("tail -100" in " ".join(hint["commands"]) for hint in payload["fix_hints"])


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
    monkeypatch.setattr(operator_api, "build_operator_health", lambda: {"status": "ok", "sections": {"database": {"status": "ok"}}})

    payload = operator_api.build_operator_health_payload()

    assert payload["status"] == "ok"
    assert payload["sections"]["database"]["status"] == "ok"


def test_operator_api_event_trace_payload(monkeypatch):
    monkeypatch.setattr(operator_api, "load_event_trace", lambda unique_id: {"unique_id": unique_id, "steps": [{"stage": "event_evaluation"}]})

    payload = operator_api.build_event_trace_payload("event-1")

    assert payload == {"unique_id": "event-1", "steps": [{"stage": "event_evaluation"}]}


def test_operator_api_symbol_trace_payload(monkeypatch):
    monkeypatch.setattr(operator_api, "load_symbol_trace", lambda symbol, limit=200: {"symbol": symbol.upper(), "traces": [{"final_action": "SELL"}], "limit": limit})

    payload = operator_api.build_symbol_trace_payload("abc", limit=10)

    assert payload == {"symbol": "ABC", "traces": [{"final_action": "SELL"}], "limit": 10}


def test_operator_api_trace_summary_uses_materialized_cache(monkeypatch):
    cached = {"symbol": "ABC", "decisions": [], "_trace_summary_cache": {"source": "materialized"}}
    monkeypatch.setattr(operator_api, "load_materialized_trace_summary", lambda entity_type, entity_key, limit=100: cached)
    monkeypatch.setattr(operator_api, "load_symbol_trace", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("live trace should not be called")))

    payload = operator_api.build_symbol_trace_summary_payload("ABC", limit=100)

    assert payload["_trace_summary_cache"]["source"] == "materialized"


def test_operator_api_trace_summary_falls_back_and_marks(monkeypatch):
    markers = []
    monkeypatch.setattr(operator_api, "load_materialized_trace_summary", lambda entity_type, entity_key, limit=100: None)
    monkeypatch.setattr(operator_api, "record_operator_api_marker", lambda **kwargs: markers.append(kwargs))
    monkeypatch.setattr(operator_api, "load_event_trace", lambda unique_id: {"unique_id": unique_id, "processing": [], "traces": [], "steps": []})

    payload = operator_api.build_event_trace_summary_payload("event-1")

    assert payload["_trace_summary_cache"]["source"] == "live_fallback"
    assert markers[0]["message"] == "trace_summary_cache_miss:event"


def test_operator_api_hypothesis_payloads(monkeypatch):
    monkeypatch.setattr(operator_api, "load_hypotheses", lambda: pd.DataFrame([{"hypothesis_id": "H1", "title": "Austerity"}]))
    monkeypatch.setattr(operator_api, "load_matches", lambda limit=100: pd.DataFrame([{"hypothesis_id": "H1", "suggested_action": "REDUCE_EXPOSURE_REVIEW_TESTING"}]))
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
    assert "macro_regime" in contract["evidence_sections_present"]


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
    assert result["screener"]["error"].startswith("screener_failed:ValueError")


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

    monkeypatch.setattr(operator_api, "_table_exists", lambda _table_name: True)
    monkeypatch.setattr(operator_api, "load_latest_manual_review_decisions", lambda **_kwargs: {})
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
    assert payload["summary"]["total_items"] == 7
    assert payload["summary"]["by_type"]["action_manual_review"] == 1
    assert payload["summary"]["by_type"]["event_policy_manual_review"] == 1
    assert payload["summary"]["by_type"]["action_conflict"] == 1
    assert payload["summary"]["by_type"]["threshold_review"] == 1
    assert payload["summary"]["by_type"]["execution_blocker"] == 1
    assert payload["summary"]["by_type"]["event_processing_failure"] == 1
    assert payload["summary"]["by_type"]["announcement_failure"] == 1
    assert payload["summary"]["by_severity"]["error"] == 2
    assert payload["items"][0]["item_type"] == "execution_blocker"
    assert payload["items"][0]["raw"]["updated_at"] == "2026-05-30T10:00:00+00:00"


def test_operator_api_records_manual_review_decision(monkeypatch):
    writes: list[pd.DataFrame] = []

    monkeypatch.setattr(operator_api, "ensure_manual_review_decisions_table", lambda: None)
    monkeypatch.setattr(operator_api, "upsert_to_db", lambda df, *args, **kwargs: writes.append(df.copy()))

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
    assert writes
    row = writes[0].iloc[0].to_dict()
    assert row["item_id"] == "action_manual_review:table:key"
    assert row["decision"] == "watch_for_event"
    assert row["symbol"] == "ABC"
    assert "Management clarification" in row["note_json"]


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


def test_operator_api_lists_operator_commands(monkeypatch):
    monkeypatch.setattr(operator_api, "ensure_operator_command_runs_table", lambda: None)
    monkeypatch.setattr(operator_api, "sql_to_df", lambda *args, **kwargs: pd.DataFrame([{"run_id": "run-1", "command_key": "operator_health_skip_dhan", "status": "ok", "command_args_json": '["python"]'}]))

    payload = operator_api.build_operator_commands_payload(limit=10)

    assert payload["status"] == "ok"
    assert any(row["key"] == "operator_health_skip_dhan" for row in payload["commands"])
    assert payload["recent_runs"][0]["command_args"] == ["python"]


def test_operator_api_runs_whitelisted_command_with_audit(monkeypatch):
    writes: list[pd.DataFrame] = []

    class Completed:
        returncode = 0
        stdout = "health ok"
        stderr = ""

    monkeypatch.setattr(operator_api, "ensure_operator_command_runs_table", lambda: None)
    monkeypatch.setattr(operator_api, "upsert_to_db", lambda df, *args, **kwargs: writes.append(df.copy()))
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

    assert set(out["signal_type"]) == {"price_close", "event_keywords"}
    price_row = out[out["signal_type"] == "price_close"].iloc[0]
    condition = json.loads(price_row["condition_json"])
    assert condition["operator"] == "close_above"
    assert condition["threshold"] == 102.0


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
