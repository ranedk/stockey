from fundamentals.themes import share_web as sw

PAGE = ("<html><head><script>var x = '62%';</script></head><body><p>In FY26, Cables contributed 62% of "
        "revenue from operations, wires the rest.</p></body></html>").encode() + b" " * 200
FOUND = {"result": "found", "pct": 62, "pct_quote": "Cables contributed 62% of revenue from operations",
         "period": "FY26", "document": "investor presentation", "source_url": "https://www.example.com/ip.pdf"}


def test_the_share_is_checked_against_the_page_we_downloaded():
    text = sw.page_text(PAGE, "text/html")
    assert "var x" not in text
    got = sw.verify(FOUND, text, None)
    assert got["status"] == "quantified" and got["exposure_pct"] == 62.0
    assert got["source_label"] == "web: example.com FY26" and got["source_url"] == FOUND["source_url"]
    # the model's quote is not on the page it named
    other = sw.verify({**FOUND, "pct": 70, "pct_quote": "Cables contributed 70% of revenue"}, text, None)
    assert other["status"] == "invalid" and "source_label" not in other


def test_a_page_we_cannot_download_is_never_used():
    got = sw.verify(FOUND, None, "HTTP 403")
    assert got["status"] == "unverified" and "exposure_pct" not in got and got["note"] == "HTTP 403"
    assert sw.verify({"result": "none"}, None, None)["status"] == "not_found"
    assert sw.fetch_page("not a url") == (None, "no usable URL")


def test_exchange_pages_go_through_their_request_gates(monkeypatch):
    import utils.exchange_rate_limiter as erl
    seen = []
    monkeypatch.setattr(erl, "exchange_request_gate", lambda domain: seen.append(domain) or sw.nullcontext())
    for host in ("www.bseindia.com", "www.screener.in", "www.sebi.gov.in", "www.example.com"):
        sw._gate(host)
    assert seen == ["bse", "screenerin", "sebi"]


def test_the_reviewer_can_only_take_a_share_away(monkeypatch):
    from fundamentals.themes import share_finder
    T = type("T", (), {"name": "Jewellery", "definition": "sells jewellery", "exclude": ()})
    rows = [{"company_master_id": "a", "theme_id": "j", "status": "quantified", "exposure_pct": 75.0, "quote": "gold 75%"},
            {"company_master_id": "b", "theme_id": "j", "status": "quantified", "exposure_pct": 90.0, "quote": "jewellery 90%"},
            {"company_master_id": "c", "theme_id": "j", "status": "quantified", "exposure_pct": 50.0, "quote": "x 50%"},
            {"company_master_id": "d", "theme_id": "j", "status": "not_found"}]
    monkeypatch.setattr(share_finder, "call_claude", lambda *a: {"ok": True, "items": [
        {"id": "0", "verdict": "reject", "reason": "big miss: silver and diamond left out"},
        {"id": "1", "verdict": "confirm", "reason": ""}]})
    sw.review(rows, {}, {"j": T})
    assert [r["status"] for r in rows] == ["rejected", "quantified", "unverified", "not_found"]
    assert rows[0]["note"].startswith("review: big miss")
