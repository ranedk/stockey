from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass

import requests
from environs import Env
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

from utils.http import get_dynamic_headers


env = Env()
env.read_env()

CDP_ENDPOINT = env("CDP_ENDPOINT", "")
SCREENER_IN_LOGIN = env("SCREENER_IN_LOGIN", "")
SCREENER_IN_PASSWORD = env("SCREENER_IN_PASSWORD", "")

DASHBOARD_URL = "https://www.screener.in/dash/"
LOGIN_URL = "https://www.screener.in/login/"
USERNAME_SELECTOR = "input#id_username[name='username']"
PASSWORD_SELECTOR = "input#id_password[name='password']"
SUBMIT_SELECTOR = "button[type='submit'].button-primary"


@dataclass
class ScreenerBrowserSession:
    playwright: object
    browser: object
    context: object
    page: object
    owns_context: bool = False

    def close(self) -> None:
        try:
            self.page.close()
        except Exception:
            pass
        if self.owns_context:
            try:
                self.context.close()
            except Exception:
                pass
        try:
            self.playwright.stop()
        except Exception:
            pass


def open_screener_browser_session() -> ScreenerBrowserSession:
    if not CDP_ENDPOINT:
        raise RuntimeError("CDP_ENDPOINT is required for authenticated Screener.in flows")
    playwright = sync_playwright().start()
    browser = playwright.chromium.connect_over_cdp(CDP_ENDPOINT)
    owns_context = not bool(browser.contexts)
    context = browser.contexts[0] if browser.contexts else browser.new_context()
    page = context.new_page()
    return ScreenerBrowserSession(playwright=playwright, browser=browser, context=context, page=page, owns_context=owns_context)


def is_logged_in(page) -> bool:
    try:
        page.goto(LOGIN_URL, wait_until="domcontentloaded")
        page.wait_for_timeout(1500)
    except PlaywrightTimeoutError:
        pass
    return "login" not in str(page.url).lower() and "/dash/" in str(page.url)


def auto_login(page, *, username: str | None = None, password: str | None = None, wait_ms: int = 5000) -> None:
    effective_username = username if username is not None else SCREENER_IN_LOGIN
    effective_password = password if password is not None else SCREENER_IN_PASSWORD
    if not effective_username or not effective_password:
        raise RuntimeError("SCREENER_IN_LOGIN and SCREENER_IN_PASSWORD are required for automated Screener.in login")

    page.goto(LOGIN_URL, wait_until="domcontentloaded")
    page.wait_for_timeout(int(wait_ms))
    if "login" not in str(page.url).lower() and "/dash/" in str(page.url):
        return

    page.fill(USERNAME_SELECTOR, effective_username)
    page.fill(PASSWORD_SELECTOR, effective_password)
    page.click(SUBMIT_SELECTOR)
    try:
        page.wait_for_url("**/dash/**", timeout=30000)
    except PlaywrightTimeoutError:
        page.wait_for_timeout(3000)
    if "login" in str(page.url).lower() or "/dash/" not in str(page.url):
        raise RuntimeError(f"Screener.in automated login did not reach dashboard. Current page: {page.url}")


def ensure_screener_logged_in(context, page) -> None:
    if is_logged_in(page):
        return
    print("[screener.in] login required; attempting automated login with SCREENER_IN_LOGIN", file=sys.stderr, flush=True)
    auto_login(page)
    if not is_logged_in(page):
        raise RuntimeError(f"Screener.in login check failed after automated login. Current page: {page.url}")


def build_authenticated_requests_session(context) -> requests.Session:
    session = requests.Session()
    session.headers.update(get_dynamic_headers())
    for cookie in context.cookies():
        session.cookies.set(cookie["name"], cookie["value"], domain=cookie.get("domain"), path=cookie.get("path"))
    return session


def ensure_authenticated_requests_session() -> requests.Session:
    browser_session = open_screener_browser_session()
    try:
        ensure_screener_logged_in(browser_session.context, browser_session.page)
        return build_authenticated_requests_session(browser_session.context)
    finally:
        browser_session.close()


def check_login_status() -> dict[str, object]:
    browser_session = open_screener_browser_session()
    try:
        logged_in = is_logged_in(browser_session.page)
        return {"status": "ok", "logged_in": logged_in, "current_url": str(browser_session.page.url)}
    finally:
        browser_session.close()


def login_and_check() -> dict[str, object]:
    browser_session = open_screener_browser_session()
    try:
        ensure_screener_logged_in(browser_session.context, browser_session.page)
        return {"status": "ok", "logged_in": is_logged_in(browser_session.page), "current_url": str(browser_session.page.url)}
    finally:
        browser_session.close()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Check or establish Screener.in login using the configured Chrome CDP session.")
    parser.add_argument("--check", action="store_true", help="Only check whether the CDP browser is logged in")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    result = check_login_status() if args.check else login_and_check()
    print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
