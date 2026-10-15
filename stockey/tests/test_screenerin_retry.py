import pytest
import requests

from fundamentals.collectors import screenerin


class R:
    def __init__(self, code):
        self.status_code, self.url, self.text = code, "https://www.screener.in/screen/raw/", "<table></table>"

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code}")


class S:
    def __init__(self, codes):
        self.codes = list(codes)

    def get(self, *a, **k):
        return R(self.codes.pop(0))


@pytest.fixture(autouse=True)
def _quiet(monkeypatch):
    monkeypatch.setattr(screenerin.time, "sleep", lambda s: None)
    monkeypatch.setattr(screenerin, "_record_fallback", lambda *a, **k: None)
    monkeypatch.setattr(screenerin, "parse_screener_results", lambda html: [])


def test_a_502_is_retried_and_the_page_still_loads():
    s = S([502, 200])
    screenerin._fetch_query_page(s, "Market Capitalization > 0", page=1)
    assert s.codes == []


def test_persistent_5xx_fails_after_the_retries():
    s = S([502, 503, 502, 502])
    with pytest.raises(requests.HTTPError):
        screenerin._fetch_query_page(s, "q", page=1)


def test_a_4xx_is_not_retried():
    s = S([403, 200])
    with pytest.raises(requests.HTTPError):
        screenerin._fetch_query_page(s, "q", page=1)
    assert s.codes == [200]
