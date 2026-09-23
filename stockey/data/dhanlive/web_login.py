from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import pyotp
from environs import Env
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright
from utils.cdp import connect_over_cdp as connect_over_cdp_guarded

from utils.fallback_telemetry import record_local_fallback_event
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
# The TOTP boxes are the code-input boxes that are NOT masked. CODE_INPUT_SELECTOR matches the
# masked PIN boxes too, which is how a skipped TOTP screen got the TOTP typed in as the PIN
# (2026-09-14/15): Dhan stopped asking for the TOTP on a device it already trusts -- the CDP
# Chrome profile keeps its cookies -- and went straight to the PIN.
TOTP_INPUT_SELECTOR = "code-input span:not(.code-hidden) input[autocomplete='one-time-code'], code-input span:not(.code-hidden) input[type='tel']"
# The PIN screen's own submit button. It says "Continue", not "Proceed" (the mobile
# screen's word), so PROCEED_BUTTON_SELECTOR does not match it.
PIN_SUBMIT_SELECTOR = ("button[type='submit']:has-text('Continue'), button.btn.btn-primary:has-text('Continue'), "
                       "button[type='submit']:has-text('Proceed')")
SCREEN_POLL_MS = 250
FAILURE_EVIDENCE_DIR = Path(env.str("DHAN_LOGIN_EVIDENCE_DIR", "logs/dhan_login"))
TOKEN_URL_MARKER = "tokenId="
DEFAULT_STEP_TIMEOUT_MS = max(int(env.int("DHAN_AUTO_LOGIN_STEP_TIMEOUT_MS", default=30000)), 1000)


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
    browser = connect_over_cdp_guarded(playwright, CDP_ENDPOINT, caller="data.dhanlive.web_login")
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


def _wait_for_pin_inputs_or_submit_totp(page, *, timeout_ms: int = 30000) -> None:
    pin_inputs = page.locator(PIN_INPUT_SELECTOR)
    try:
        pin_inputs.first.wait_for(state="visible", timeout=min(int(timeout_ms), 5000))
        return
    except PlaywrightTimeoutError as exc:
        _record_dhan_web_login_fallback(
            fallback_type="dhan_web_login_pin_wait_before_totp_submit_timeout",
            reason="Dhan automated login did not show PIN inputs after TOTP fill; clicking the visible Proceed button.",
            error=exc,
            metadata={"timeout_ms": min(int(timeout_ms), 5000)},
        )
    _click_enabled_proceed(page, timeout_ms=timeout_ms)
    pin_inputs.first.wait_for(state="visible", timeout=timeout_ms)


def _visible(page, selector: str) -> bool:
    try:
        locator = page.locator(selector)
        return locator.count() > 0 and bool(locator.first.is_visible())
    except Exception:
        return False


def _detect_code_screen(page, *, timeout_ms: int) -> str:
    """After the mobile number Dhan shows either the TOTP boxes or -- for a device it trusts --
    goes straight to the masked PIN boxes. Returns "totp" or "pin". Types NOTHING and raises if
    neither appears, or both do: a TOTP typed into the PIN boxes is a wrong PIN, and wrong PINs
    lock the account."""
    polls = max(int(timeout_ms) // SCREEN_POLL_MS, 0)
    for attempt in range(polls + 1):
        pin, totp = _visible(page, PIN_INPUT_SELECTOR), _visible(page, TOTP_INPUT_SELECTOR)
        if pin and totp:
            evidence = _save_failure_evidence(page, "ambiguous_code_screen")
            _record_dhan_web_login_fallback(
                fallback_type="dhan_web_login_code_screen_ambiguous",
                reason="Dhan showed TOTP and PIN boxes at once; typed nothing rather than guess.",
                error="ambiguous_code_screen",
                severity="error",
                metadata={"current_url_host": _url_host(getattr(page, "url", None)), **evidence},
            )
            raise DhanAuthError("Dhan login showed both TOTP and PIN inputs; refusing to guess which to fill")
        if pin:
            return "pin"
        if totp:
            return "totp"
        if attempt < polls:
            page.wait_for_timeout(SCREEN_POLL_MS)
    evidence = _save_failure_evidence(page, "no_code_screen")
    _record_dhan_web_login_fallback(
        fallback_type="dhan_web_login_code_screen_missing",
        reason="Dhan showed neither TOTP nor PIN inputs after the mobile number.",
        error="no_code_screen",
        severity="error",
        metadata={"timeout_ms": int(timeout_ms), "current_url_host": _url_host(getattr(page, "url", None)), **evidence},
    )
    raise DhanAuthError("Dhan login showed neither TOTP nor PIN inputs after the mobile number")


def _save_failure_evidence(page, label: str) -> dict[str, str]:
    """A screenshot and the page's visible text, so the next failure says what Dhan asked for.
    Best effort: never raises. Written under logs/ (gitignored)."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    base = FAILURE_EVIDENCE_DIR / f"{stamp}_{label}"
    out: dict[str, str] = {}
    try:
        base.parent.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(base.with_suffix(".png")), full_page=True)
        out["screenshot"] = str(base.with_suffix(".png"))
    except Exception as exc:
        out["screenshot_error"] = str(exc)[:200]
    try:
        text = page.inner_text("body", timeout=5000)
        base.with_suffix(".txt").write_text(str(text)[:20000], encoding="utf-8")
        out["page_text"] = str(base.with_suffix(".txt"))
    except Exception as exc:
        out["page_text_error"] = str(exc)[:200]
    return out


def _submit_mobile(page, mobile: str, *, timeout_ms: int) -> None:
    """Type the mobile number and click Proceed."""
    field = page.locator(MOBILE_INPUT_SELECTOR).first
    field.fill(str(mobile), timeout=timeout_ms)
    field.dispatch_event("input")
    field.dispatch_event("change")
    _click_enabled_proceed(page, timeout_ms=timeout_ms)


def _submit_pin(page, *, timeout_ms: int) -> None:
    """Click the PIN screen's own submit button if it is still showing. The screen
    auto-submitted on the sixth digit on 2026-09-15, and did not on 2026-09-17: the login
    sat on "Enter PIN for your Account" until it timed out
    (logs/dhan_login/20260917T020542Z_token_missing.txt). Best effort -- when the form has
    already submitted the button is gone, and there is nothing to click."""
    try:
        button = page.locator(PIN_SUBMIT_SELECTOR).last
        button.wait_for(state="visible", timeout=min(int(timeout_ms), 5000))
        button.click(timeout=timeout_ms)
    except Exception as exc:
        _record_dhan_web_login_fallback(
            fallback_type="dhan_web_login_pin_submit_skipped",
            reason="No PIN submit button to click; assuming the PIN screen submitted itself.",
            error=exc,
            metadata={"current_url_host": _url_host(getattr(page, "url", None))},
        )


def _main_frame_navigation(page, request) -> bool:
    """A navigation of the page itself -- not an ad tracker's iframe, which is how a
    doubleclick.net frame was once recorded as where the login 'redirected'."""
    try:
        if not request.is_navigation_request():
            return False
    except Exception:
        return False
    try:
        return request.frame == page.main_frame
    except Exception:
        return True


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
    timeout_ms: int | None = None,
) -> str:
    effective_timeout_ms = int(timeout_ms or DEFAULT_STEP_TIMEOUT_MS)
    effective_mobile = mobile or DHAN_LOGIN_MOBILE
    effective_pin = pin or DHAN_LOGIN_PIN
    if not effective_mobile:
        raise DhanAuthError("DHAN_LOGIN_MOBILE is required for automated Dhan login")
    if not effective_pin:
        raise DhanAuthError("DHAN_LOGIN_PIN is required for automated Dhan login")
    if len("".join(ch for ch in str(effective_pin) if ch.isdigit())) != 6:
        raise DhanAuthError("DHAN_LOGIN_PIN must be 6 digits")

    page.goto(consent_url, wait_until="domcontentloaded", timeout=effective_timeout_ms)
    page.wait_for_timeout(3000)
    _submit_mobile(page, effective_mobile, timeout_ms=effective_timeout_ms)

    # 2026-09-22: the Proceed click did not take and the page sat on "Login via Dhan / Mobile
    # Number" until the code-screen wait gave up, costing that whole day's Dhan jobs. If the
    # mobile field is still there, the step simply did not land: type it once more rather than
    # lose a day. Anything else (a screen we cannot identify) still fails without typing.
    try:
        screen = _detect_code_screen(page, timeout_ms=effective_timeout_ms)
    except DhanAuthError:
        if not _visible(page, MOBILE_INPUT_SELECTOR):
            raise
        _record_dhan_web_login_fallback(
            fallback_type="dhan_web_login_mobile_step_retried",
            reason="Dhan stayed on the mobile-number screen; submitting it once more.",
            error="mobile_screen_persisted",
            metadata={"current_url_host": _url_host(getattr(page, "url", None))},
        )
        page.wait_for_timeout(2000)
        _submit_mobile(page, effective_mobile, timeout_ms=effective_timeout_ms)
        screen = _detect_code_screen(page, timeout_ms=effective_timeout_ms)

    # The TOTP screen may or may not come: Dhan skips it for a device it trusts. Keep the TOTP
    # path -- it may come back -- but only ever type into the screen actually showing.
    if screen == "totp":
        page.wait_for_timeout(1000)
        fill_digit_code(page, generate_totp(totp_secret), selector=TOTP_INPUT_SELECTOR, timeout_ms=effective_timeout_ms)
        page.wait_for_timeout(1000)
        _wait_for_pin_inputs_or_submit_totp(page, timeout_ms=effective_timeout_ms)
    else:
        _record_dhan_web_login_fallback(
            fallback_type="dhan_web_login_totp_skipped",
            reason="Dhan went straight to the PIN after the mobile number (trusted device); no TOTP entered.",
            error="totp_screen_absent",
            metadata={"current_url_host": _url_host(getattr(page, "url", None))},
        )

    # To capture redirection to a non-existent url is tricky
    nav_capture = {
        "requested_url": None,
        "failed_url": None,
        "failure": None,
        "response_url": None,
        "response_status": None,
    }

    def on_request(request):
        if _main_frame_navigation(page, request):
            nav_capture["requested_url"] = request.url

    def on_response(response):
        if _main_frame_navigation(page, response.request):
            nav_capture["response_url"] = response.url
            nav_capture["response_status"] = response.status

    def on_request_failed(request):
        if _main_frame_navigation(page, request):
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
        timeout_ms=effective_timeout_ms,
    )
    _submit_pin(page, timeout_ms=effective_timeout_ms)

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
            page.wait_for_function(f"() => window.location.href.includes('{TOKEN_URL_MARKER}')", timeout=effective_timeout_ms)
            token_id = extract_token_id(str(page.url))
        except PlaywrightTimeoutError as exc:
            _record_dhan_web_login_fallback(
                fallback_type="dhan_web_login_token_wait_timeout",
                reason="Dhan automated login did not observe tokenId before timeout.",
                error=exc,
                metadata={"timeout_ms": int(effective_timeout_ms), "current_url_host": _url_host(getattr(page, "url", None))},
            )
            token_id = None

    if not token_id:
        evidence = _save_failure_evidence(page, "token_missing")
        _record_dhan_web_login_fallback(
            fallback_type="dhan_web_login_token_missing",
            reason="Dhan automated login redirected without tokenId.",
            error="missing_token_id",
            severity="error",
            metadata={
                **evidence,
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
