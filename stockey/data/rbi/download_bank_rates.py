# Download data from RBI website – sync version
import json
from datetime import datetime

import numpy as np
import pandas as pd
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright
from utils.cdp import connect_over_cdp as connect_over_cdp_guarded

from utils.fallback_telemetry import record_local_fallback_event
from utils.db import upsert_to_db

CDP_ENDPOINT = "http://localhost:9222"
SYNC_SOURCE_NAME = "data.rbi.download_bank_rates"
STOCKEY_RUN_STATE: dict[str, object] = {}


# BUG FOUND LIVE 2026-08-19 (re-audit): column_index_rename_map below maps output columns purely
# by POSITION with zero validation that the scraped header text actually says "Bank Rate"/"Repo
# Rate"/etc. -- if RBI ever inserts, reorders, or removes a column, values would be silently
# mislabeled (e.g. CRR's value stored under msf_rate) with no error. Not hard-blocking on a
# mismatch here -- I could not get a live download through the RBI site's own multi-step click
# flow to confirm the exact real header text for every column during this fix (the site was
# uncooperative on the attempts made), so a strict keyword check risks a false-positive block on
# a real, valid run. Recording fallback telemetry (visible, non-blocking) instead if the expected
# keywords aren't found where expected -- flags a real format change for review without stopping
# genuinely-fine runs on an unverified assumption.
_EXPECTED_HEADER_KEYWORDS = {
    2: "bank",
    3: "repo",
    7: "crr",
    8: "slr",
}


def parse_excel_file(file_path: str) -> pd.DataFrame:
    xls = pd.ExcelFile(file_path)
    sheet_names = xls.sheet_names

    preview_df = xls.parse(sheet_names[0], header=None, nrows=20)
    header_rows = preview_df.iloc[5:8].fillna("")
    combined_headers = (
        header_rows.astype(str).agg(" ".join).str.strip().replace("", np.nan)
    )

    mismatched = {}
    for index, keyword in _EXPECTED_HEADER_KEYWORDS.items():
        header_text = str(combined_headers.iloc[index]).lower() if index < len(combined_headers) else ""
        if keyword not in header_text:
            mismatched[index] = header_text
    if mismatched:
        record_local_fallback_event(
            module=SYNC_SOURCE_NAME,
            source="rbi_bank_rates:parse",
            fallback_type="rbi_bank_rates_header_mismatch",
            severity="warn",
            reason=(
                "RBI bank-rates Excel header text didn't contain the expected keyword at a "
                "column this parser maps by position -- the source format may have changed; "
                "values could be mislabeled. Proceeding with the existing positional mapping "
                "since this isn't confirmed as a real format change, only flagged for review."
            ),
            error=None,
            metadata={"mismatched_columns": mismatched},
        )

    data_df = xls.parse(sheet_names[0], header=None, skiprows=8)
    data_df.columns = combined_headers.values
    data_df.dropna(how="all", inplace=True)

    column_index_rename_map = {
        0: "",
        1: "effective_date",
        2: "bank_rate",
        3: "repo_rate",
        4: "reverse_repo_rate",
        5: "sdf_rate",
        6: "msf_rate",
        7: "crr",
        8: "slr",
    }

    data_df = data_df.iloc[:-1]
    data_df.columns = [
        column_index_rename_map.get(i, col) for i, col in enumerate(data_df.columns)
    ]
    data_df = data_df.drop(columns=data_df.columns[0])
    data_df.replace("-", np.nan, inplace=True)
    return data_df


def _empty_run_state(status: str = "running") -> dict[str, object]:
    now = datetime.now().isoformat()
    return {
        "source": SYNC_SOURCE_NAME,
        "status": status,
        "rows": 0,
        "rows_read": 0,
        "rows_written": 0,
        "attempt_count": 1,
        "download_attempts": 1,
        "failed_attempt_count": 0,
        "source_unavailable_count": 0,
        "fallback_used": False,
        "state_advanced": False,
        "started_at": now,
        "finished_at": now,
    }


def _wait_for_named_frame(page, *, name: str, timeout_ms: int = 20_000, poll_ms: int = 500):
    """page.frame(name=...) is an immediate, non-waiting lookup that returns None if the child
    frame hasn't attached yet -- a fixed pre-sleep before calling it (the previous approach) is
    a race against the RBI site's variable load time, and a None frame's .click() raised an
    opaque AttributeError instead of a clear timeout. Poll until it appears."""
    waited_ms = 0
    while waited_ms < timeout_ms:
        frame = page.frame(name=name)
        if frame is not None:
            return frame
        page.wait_for_timeout(poll_ms)
        waited_ms += poll_ms
    raise PlaywrightTimeoutError(f"Timed out after {timeout_ms}ms waiting for frame name={name!r}")


def _dismiss_blocking_modal(page) -> bool:
    """BUG FOUND LIVE 2026-08-19: the "Indicators" click below has failed on every one of the
    last 15 recorded runs (both complete_data.sh slots, several days running) with the same
    Playwright error -- a `.modal-backdrop.show` element "intercepts pointer events". Live
    reproduction on the real site/profile (2026-08-19) found a "Session has expired. Please
    refresh the page" dialog (plus a "Download App" and generic "OK" dialog) present in the
    DOM from this CDP profile's carried-over RBI session state, though its backdrop wasn't
    reliably reproducible as blocking in 3/3 isolated attempts -- the real cron process shares
    one Chrome instance with several other concurrent scrapers, so a slow fade-out under that
    contention is the more likely trigger than a permanently stuck dialog. Either way, dismissing
    any visible modal before the click is strictly protective (no-op when nothing is showing, as
    it was in all 3 successful isolated reproductions) and directly targets the one element type
    named in every historical failure. Returns True if a modal was found and dismissed."""
    dialog = page.locator(".modal.show, [role='dialog']:visible").first
    try:
        if not dialog.is_visible(timeout=1_000):
            return False
    except PlaywrightTimeoutError:
        return False
    for label in ("Refresh", "OK", "Close"):
        button = dialog.get_by_role("button", name=label, exact=False)
        if button.count() > 0:
            button.first.click()
            page.wait_for_timeout(1_000)
            return True
    page.keyboard.press("Escape")
    page.wait_for_timeout(1_000)
    return True


def _click_past_blocking_modals(page, locator, *, max_dismissals: int = 3) -> None:
    """Wraps a single .click() with up to max_dismissals modal-dismiss-and-retry attempts --
    see _dismiss_blocking_modal()'s docstring for why. Bounded so a modal that keeps
    reappearing (e.g. a reload loop) still surfaces as a real timeout, not an infinite retry."""
    for _ in range(max_dismissals):
        try:
            locator.click(timeout=10_000)
            return
        except PlaywrightTimeoutError:
            if not _dismiss_blocking_modal(page):
                raise
    locator.click()


def download_latest_rates(playwright) -> dict[str, object]:
    """
    Automate RBI website's download using the sync Playwright API.
    """
    state = _empty_run_state()
    browser = None
    page = None
    rates_page = None

    try:
        browser = connect_over_cdp_guarded(playwright, CDP_ENDPOINT, caller="data.rbi.download_bank_rates")
        context = browser.contexts[0] if browser.contexts else browser.new_context()
        page = context.new_page()

        page.goto("https://data.rbi.org.in/DBIE/#/dbie/home")
        page.wait_for_timeout(10_000)

        # BUG FOUND LIVE 2026-08-19 (re-audit): the modal-dismiss-and-retry wrapper (see
        # _dismiss_blocking_modal's own docstring for why it exists) was only applied to this
        # first click -- the root cause it's defensive against (a page-wide modal-backdrop
        # intercepting pointer events) isn't inherently specific to the Indicators link and could
        # equally block any later click in this same sequence. Applied to every click in the flow.
        _click_past_blocking_modals(page, page.get_by_role("link", name="Indicators", exact=True))
        page.wait_for_timeout(2_000)

        _click_past_blocking_modals(page, page.locator("a").filter(has_text="Financial Sector Indicators"))
        page.wait_for_timeout(2_000)

        with page.expect_popup() as popup_info:
            _click_past_blocking_modals(page, page.get_by_text("Key Rates"))

        rates_page = popup_info.value
        frame = _wait_for_named_frame(rates_page, name="openDocChildFrame")

        # #__button60 is an auto-generated ID from the underlying BI/reporting tool, unlike every
        # other click in this flow which uses a resilient role/text-based locator -- a real,
        # distinct brittleness point (BUG FOUND LIVE 2026-08-19, re-audit) with no equivalent
        # semantic locator available on the real page to replace it with; left as-is rather than
        # guessing at a "better" selector I can't verify against the live site, but still routed
        # through the same dismiss-and-retry wrapper for whatever resilience that buys it. Dismiss
        # target is rates_page (the popup these two clicks actually run in), not page (the
        # original tab) -- a modal on the popup can't be dismissed via a different window.
        _click_past_blocking_modals(rates_page, frame.locator("#__button60"))
        rates_page.wait_for_timeout(3_000)

        with rates_page.expect_download(timeout=15_000) as dl_info:
            _click_past_blocking_modals(rates_page, frame.get_by_role("button", name="Export", exact=True))

        download = dl_info.value
        file_path = download.path()
        df = parse_excel_file(file_path)
        df["date"] = pd.to_datetime(df["effective_date"], format="%d-%m-%Y")
        df = df.drop(columns=["effective_date"])
        for col in [
            "bank_rate",
            "repo_rate",
            "reverse_repo_rate",
            "sdf_rate",
            "msf_rate",
            "crr",
            "slr",
        ]:
            df[col] = pd.to_numeric(df[col], errors="coerce")
        upsert_to_db(df, "rbi_bank_rates", unique_keys=["date"], timescaledb_column="date")
        print(f"RBI bank rates updated: {len(df)} rows")
        state.update(
            {
                "status": "ok",
                "rows": int(len(df)),
                "rows_read": int(len(df)),
                "rows_written": int(len(df)),
                "from_date": df["date"].min().date().isoformat() if not df.empty else None,
                "to_date": df["date"].max().date().isoformat() if not df.empty else None,
                "state_advanced": bool(len(df) > 0),
            }
        )
        return state
    except (PlaywrightTimeoutError, PlaywrightError) as exc:
        record_local_fallback_event(
            module=SYNC_SOURCE_NAME,
            source="rbi_bank_rates:browser",
            fallback_type="rbi_bank_rates_source_unavailable",
            severity="warn",
            reason=(
                "RBI bank-rates browser/CDP download failed; policy-rate context may be stale until "
                "the source is reachable again."
            ),
            error=exc,
            metadata={"cdp_endpoint": CDP_ENDPOINT, "status": "source_unavailable"},
        )
        print(f"RBI bank rates download skipped: {exc}")
        state.update(
            {
                "status": "source_unavailable",
                "failed_attempt_count": 1,
                "source_unavailable_count": 1,
                # BUG FOUND LIVE 2026-08-19 (re-audit): fallback_used was set once in the initial
                # state dict (False) and never updated in either failure branch here, even though
                # record_local_fallback_event was just called a few lines above -- a consumer
                # keying off fallback_used specifically (rather than status/classification) would
                # misreport a real, recorded fallback as a clean run.
                "fallback_used": True,
                "error": f"{type(exc).__name__}: {exc}",
            }
        )
        return state
    except Exception as exc:
        record_local_fallback_event(
            module=SYNC_SOURCE_NAME,
            source="rbi_bank_rates:download",
            fallback_type="rbi_bank_rates_download_failed",
            severity="warn",
            reason=(
                "RBI bank-rates download or parse failed; policy-rate context may be stale until "
                "the failure is fixed and the job reruns."
            ),
            error=exc,
            metadata={"cdp_endpoint": CDP_ENDPOINT, "status": "failed"},
        )
        print(f"RBI bank rates download skipped: {exc}")
        state.update(
            {
                "status": "failed",
                "failed_attempt_count": 1,
                "fallback_used": True,
                "error": f"{type(exc).__name__}: {exc}",
            }
        )
        return state
    finally:
        state["finished_at"] = datetime.now().isoformat()
        if rates_page is not None:
            rates_page.close()
        if page is not None:
            page.close()
        if browser is not None:
            browser.close()


def main() -> int:
    global STOCKEY_RUN_STATE
    with sync_playwright() as p:
        STOCKEY_RUN_STATE = download_latest_rates(p)
    status = str(STOCKEY_RUN_STATE.get("status") or "ok")
    print(
        json.dumps({"status": status, **STOCKEY_RUN_STATE}, ensure_ascii=False, default=str),
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
