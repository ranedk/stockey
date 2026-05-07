from __future__ import annotations

import argparse
import json
from datetime import date
from datetime import datetime

import pandas as pd

from advisory import adversarial_review, announcement_watch, continuous_watch, dashboard, event_meta_model, event_model_data_prep, event_router, execution_engine, exchange_events, exchange_features, intraday_features, live_dashboard, llm_event_evaluator, macro_features, master_pipeline, model_training_runner, news_overlay_engine, news_theme_engine, news_watch, pipeline, portfolio_engine, position_lifecycle, research_ledger, risk_engine, rule_engine, setup_registry, setup_trace, symbol_trace, technical_engine, training_universe, ts_forecast_evaluator, ts_forecast_features, ts_forecast_workflow, watchlist_builder
from data.announcements import pipeline as announcement_pipeline
from data.announcements import state as announcement_state
from data.eaindustry import wpi
from data.dhanlive import auth as dhan_auth
from data.dhanlive import auth_cli as dhan_auth_cli
from data.dhanlive import client as dhan_client
from data.dhanlive import web_login as dhan_web_login
from data.dhanlive import dhan_db, ohlcv as dhan_ohlcv
from data.nseindia import bhavcopy_downloader, bhavcopy_parser, indices_downloader, indices_parser, offmarket, recent_events
from data.screenerin import auth as screener_auth
from data.mospi import cpi
from data.nsdl import fpi
from data import download_runner
from utils import db as db_utils
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


def test_execution_engine_uses_broker_cash_cap_and_exit_holdings(monkeypatch):
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
    payload = llm_event_evaluator.build_payload(event_row, None)
    assert payload["exchange_context"]["features"]["exchange_distribution_score"] == 0.4
    assert len(payload["exchange_context"]["recent_events"]) == 1


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
    monkeypatch.setattr(
        intraday_features,
        "load_intraday_history",
        lambda symbols, start_timestamp, end_timestamp, interval_minutes: intraday_base[intraday_base["interval_minutes"] == interval_minutes].copy(),
    )
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
