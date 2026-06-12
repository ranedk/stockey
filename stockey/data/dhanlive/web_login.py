from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from urllib.parse import urlparse

import pyotp
from environs import Env
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

from advisory.fallback_telemetry import record_local_fallback_event
from data.dhanlive.auth import DhanAuthError, extract_token_id, normalize_token_id


env = Env()
env.read_env()

CDP_ENDPOINT = env("CDP_ENDPOINT", "")
DHAN_LOGIN_MOBILE = env("DHAN_LOGIN_MOBILE", "")
DHAN_LOGIN_PIN = env("DHAN_LOGIN_PIN", "")
DHAN_TOTP_SECRET = env("DHAN_TOTP_SECRET", "")

MOBILE_INPUT_SELECTOR = "input[type='tel'][maxlength='10'], input[placeholder*='mobile' i]"
PROCEED_BUTTON_SELECTOR = "button[type='submit']:has-text('Proceed'), button.btn.btn-primary:has-text('Proceed')"
CODE_INPUT_SELECTOR = "code-input input[autocomplete='one-time-code'], code-input input[type='tel']"
PIN_INPUT_SELECTOR = "code-input span.code-hidden input[autocomplete='one-time-code'], code-input span.code-hidden input[type='tel']"
TOKEN_URL_MARKER = "tokenId="


def _url_host(value: str | None) -> str | None:
    if not value:
        return None
    return urlparse(str(value)).netloc or None


def _record_dhan_web_login_fallback(
    *,
    fallback_type: str,
    reason: str,
    error: Exception | str,
    severity: str = "warn",
    metadata: dict[str, object] | None = None,
) -> None:
    record_local_fallback_event(
        module="data.dhanlive.web_login",
        source="dhan_web_login",
        fallback_type=fallback_type,
        severity=severity,
        reason=reason,
        error=error,
        metadata=metadata or {},
    )


@dataclass
class DhanBrowserSession:
    playwright: object
    browser: object
    context: object
    page: object
    owns_context: bool = False

    def close(self) -> None:
        try:
            self.page.close()
        except Exception as exc:
            _record_dhan_web_login_fallback(
                fallback_type="dhan_web_login_page_close_failed",
                reason="Dhan automated login could not close the Playwright page.",
                error=exc,
                metadata={"owns_context": self.owns_context},
            )
            pass
        if self.owns_context:
            try:
                self.context.close()
            except Exception as exc:
                _record_dhan_web_login_fallback(
                    fallback_type="dhan_web_login_context_close_failed",
                    reason="Dhan automated login could not close the Playwright context it created.",
                    error=exc,
                    metadata={"owns_context": self.owns_context},
                )
                pass
        try:
            self.playwright.stop()
        except Exception as exc:
            _record_dhan_web_login_fallback(
                fallback_type="dhan_web_login_playwright_stop_failed",
                reason="Dhan automated login could not stop Playwright cleanly.",
                error=exc,
                metadata={"owns_context": self.owns_context},
            )
            pass


def generate_totp(secret: str | None = None) -> str:
    effective_secret = secret or DHAN_TOTP_SECRET
    if not effective_secret:
        raise DhanAuthError("DHAN_TOTP_SECRET is required for automated Dhan login")
    return pyotp.TOTP(effective_secret).now()


def open_dhan_browser_session() -> DhanBrowserSession:
    if not CDP_ENDPOINT:
        raise DhanAuthError("CDP_ENDPOINT is required for automated Dhan login")
    playwright = sync_playwright().start()
    browser = playwright.chromium.connect_over_cdp(CDP_ENDPOINT)
    owns_context = not bool(browser.contexts)
    context = browser.contexts[0] if browser.contexts else browser.new_context()
    page = context.new_page()
    return DhanBrowserSession(playwright=playwright, browser=browser, context=context, page=page, owns_context=owns_context)


def _click_enabled_proceed(page, *, timeout_ms: int = 30000) -> None:
    button = page.locator(PROCEED_BUTTON_SELECTOR).last
    try:
        button.wait_for(state="visible", timeout=timeout_ms)
        button.click(timeout=timeout_ms)
    except Exception as exc:
        _record_dhan_web_login_fallback(
            fallback_type="dhan_web_login_proceed_click_fallback",
            reason="Dhan automated login normal Proceed click failed; retrying with force click.",
            error=exc,
            metadata={"timeout_ms": int(timeout_ms)},
        )
        page.locator(PROCEED_BUTTON_SELECTOR).last.click(force=True, timeout=timeout_ms)


def fill_digit_code(page, code: str, *, selector: str = CODE_INPUT_SELECTOR, timeout_ms: int = 30000) -> None:
    digits = "".join(ch for ch in str(code) if ch.isdigit())
    if len(digits) != 6:
        raise DhanAuthError("Expected a 6 digit Dhan code")
    inputs = page.locator(selector)
    inputs.first.wait_for(state="visible", timeout=timeout_ms)
    count = inputs.count()
    if count < 6:
        raise DhanAuthError(f"Expected at least 6 Dhan code inputs, found {count}")
    for index, digit in enumerate(digits):
        field = inputs.nth(index)
        field.click(timeout=timeout_ms)
        field.fill(digit, timeout=timeout_ms)
        field.dispatch_event("input")
        field.dispatch_event("change")


def run_dhan_consent_login(
    page,
    *,
    consent_url: str,
    mobile: str | None = None,
    pin: str | None = None,
    totp_secret: str | None = None,
    timeout_ms: int = 5000,
) -> str:
    effective_mobile = mobile or DHAN_LOGIN_MOBILE
    effective_pin = pin or DHAN_LOGIN_PIN
    if not effective_mobile:
        raise DhanAuthError("DHAN_LOGIN_MOBILE is required for automated Dhan login")
    if not effective_pin:
        raise DhanAuthError("DHAN_LOGIN_PIN is required for automated Dhan login")
    if len("".join(ch for ch in str(effective_pin) if ch.isdigit())) != 6:
        raise DhanAuthError("DHAN_LOGIN_PIN must be 6 digits")

    page.goto(consent_url, wait_until="domcontentloaded", timeout=timeout_ms)
    page.wait_for_timeout(3000)
    page.locator(MOBILE_INPUT_SELECTOR).first.fill(str(effective_mobile), timeout=timeout_ms)
    page.locator(MOBILE_INPUT_SELECTOR).first.dispatch_event("input")
    page.locator(MOBILE_INPUT_SELECTOR).first.dispatch_event("change")
    _click_enabled_proceed(page, timeout_ms=timeout_ms)

    page.wait_for_timeout(1000)
    fill_digit_code(page, generate_totp(totp_secret), selector=CODE_INPUT_SELECTOR, timeout_ms=timeout_ms)
    page.wait_for_timeout(3000)

    # To capture redirection to a non-existent url is tricky
    nav_capture = {
        "requested_url": None,
        "failed_url": None,
        "failure": None,
        "response_url": None,
        "response_status": None,
    }

    def on_request(request):
        if request.is_navigation_request():
            nav_capture["requested_url"] = request.url

    def on_response(response):
        if response.request.is_navigation_request():
            nav_capture["response_url"] = response.url
            nav_capture["response_status"] = response.status

    def on_request_failed(request):
        if request.is_navigation_request():
            nav_capture["failed_url"] = request.url
            nav_capture["failure"] = request.failure

    if hasattr(page, "on"):
        page.on("request", on_request)
        page.on("response", on_response)
        page.on("requestfailed", on_request_failed)

    fill_digit_code(
        page,
        str(effective_pin),
        selector=PIN_INPUT_SELECTOR,
        timeout_ms=timeout_ms,
    )

    page.wait_for_timeout(5000)

    actual_url = (
        nav_capture["failed_url"]
        or nav_capture["response_url"]
        or nav_capture["requested_url"]
        or getattr(page, "url", None)
    )

    token_id = extract_token_id(str(actual_url))
    if not token_id:
        try:
            page.wait_for_function(f"() => window.location.href.includes('{TOKEN_URL_MARKER}')", timeout=timeout_ms)
            token_id = extract_token_id(str(page.url))
        except PlaywrightTimeoutError as exc:
            _record_dhan_web_login_fallback(
                fallback_type="dhan_web_login_token_wait_timeout",
                reason="Dhan automated login did not observe tokenId before timeout.",
                error=exc,
                metadata={"timeout_ms": int(timeout_ms), "current_url_host": _url_host(getattr(page, "url", None))},
            )
            token_id = None

    if not token_id:
        _record_dhan_web_login_fallback(
            fallback_type="dhan_web_login_token_missing",
            reason="Dhan automated login redirected without tokenId.",
            error="missing_token_id",
            severity="error",
            metadata={
                "current_url_host": _url_host(getattr(page, "url", None)),
                "failed_url_host": _url_host(nav_capture.get("failed_url")),
                "response_url_host": _url_host(nav_capture.get("response_url")),
                "requested_url_host": _url_host(nav_capture.get("requested_url")),
            },
        )
        raise DhanAuthError(f"Dhan login redirected without tokenId. Current page: {page.url}")
    return token_id


def get_token_id_via_automated_login(consent_url: str) -> str:
    browser_session = open_dhan_browser_session()
    try:
        return run_dhan_consent_login(browser_session.page, consent_url=consent_url)
    finally:
        browser_session.close()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Automate Dhan consent login through the running Chrome CDP session.")
    parser.add_argument("--consent-url", required=True, help="Dhan consent URL from generate-consent")
    parser.add_argument("--print-token-id", action="store_true", help="Print the raw token id instead of only a masked status")
    return parser.parse_args()


def _mask(value: str | None) -> str | None:
    if not value:
        return None
    return value if len(value) <= 10 else f"{value[:6]}...{value[-4:]}"


def main() -> int:
    args = parse_args()
    token_id = normalize_token_id(get_token_id_via_automated_login(args.consent_url))
    print(
        json.dumps(
            {
                "status": "ok",
                "token_id": token_id if args.print_token_id else _mask(token_id),
                "printed_raw_token": bool(args.print_token_id),
            },
            indent=2,
            ensure_ascii=False,
            default=str,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
