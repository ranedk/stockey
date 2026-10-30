import pytest

from fundamentals.collectors import company_profile as cp

PAGE = """
<div class="company-profile"><div class="about"><p>Makes cables and wires.</p></div></div>
<button data-url="/wiki/company/1274653/commentary/v2/">Read More</button>
<section id="peers"><a href="/market/IN07/">Industrials</a><a href="/market/IN07/IN0701/">Capital Goods</a>
<a href="/market/IN07/IN0701/IN070102/">Industrial Products</a><a href="/market/IN07/IN0701/IN070102/IN070102003/">Cables - Electricals</a></section>
<section id="documents">
 <div class="documents"><h3>Annual reports</h3><ul><li><a href="https://bse/ar2026.pdf">Annual Report 2026</a></li></ul></div>
 <div class="documents"><h3>Concalls</h3><ul><li>Oct 2026 <a href="https://bse/t.pdf">Transcript</a><a href="https://bse/p.pdf">PPT</a></li></ul></div>
</section>
"""

COMMENTARY = """<div><h3>Business
 Segments</h3><p>a) Wires &amp; <b>Cables</b> (87% in FY26 <br>\nvs 84% in FY25):<sup><a href="https://bse/ar.pdf#page=9">[1]</a></sup></p>
<p>Capex</p><p>Rs. 1,480 Cr</p><p>Last edited 3 months, 1 week ago</p><p>Request an update</p><p>© Protected by Copyright</p></div>"""


def test_company_page_gives_about_peers_documents_and_the_commentary_id():
    p = cp.parse_company_page(PAGE)
    assert p["screener_company_id"] == "1274653"
    assert p["about"] == "Makes cables and wires."
    assert p["peers_path"] == "Industrials > Capital Goods > Industrial Products > Cables - Electricals"
    assert p["documents"]["Annual reports"][0]["links"][0]["url"] == "https://bse/ar2026.pdf"
    assert [l["label"] for l in p["documents"]["Concalls"][0]["links"]] == ["Transcript", "PPT"]


def test_commentary_keeps_the_segment_shares_and_drops_footnotes_and_boilerplate():
    text, edited, sources = cp.parse_commentary(COMMENTARY)
    assert "Wires & Cables (87% in FY26 vs 84% in FY25):" in text
    assert "Business Segments" in text
    assert "[1]" not in text and "Copyright" not in text and "Request an update" not in text
    assert edited == "Last edited 3 months, 1 week ago"
    assert sources == ["https://bse/ar.pdf#page=9"]


def test_a_full_page_in_place_of_the_commentary_is_a_failure_not_text():
    with pytest.raises(ValueError):
        cp.parse_commentary(PAGE)


def test_slug_candidates_quote_the_symbol_and_fall_back_to_the_bse_code():
    from fundamentals.collectors.company_profile import slug_candidates
    assert slug_candidates("GET&D", "522275") == ["GET%26D", "522275"]
    assert slug_candidates("544467-BOM", "544467") == ["544467"]
    assert slug_candidates("TCS", None) == ["TCS"]


def test_name_key_ignores_ltd_and_punctuation():
    from fundamentals.collectors.company_profile import _name_key
    assert _name_key("Eternal Ltd.") == _name_key("Eternal Ltd") == "eternal"
    assert _name_key("GE Vernova T&D India Ltd.") == "gevernovatdindia"


def test_the_premium_notice_is_not_stored_as_key_points():
    import pytest
    from fundamentals.collectors.company_profile import KeyPointsLocked, parse_commentary
    html = "<div><p>Please upgrade to premium to read more key insights.</p><p>Free users can access 20 free key-insights</p></div>"
    with pytest.raises(KeyPointsLocked):
        parse_commentary(html)


def test_a_renamed_company_listed_twice_becomes_one_row_with_both_codes():
    import pandas as pd
    u = pd.DataFrame({
        "company_master_id": ["nse:HBLENGINE", "nse:HBLPOWER", "nse:544395-BOM", "nse:STLNETWORK", "nse:X"],
        "median_tv": [7e8, 7e7, 2e8, 1e8, 5e8],
        "company_name": ["HBL Engineering", "HBL Engineering Ltd.", "STL Networks Ltd.", "STL Networks", "X Ltd."],
        "bse_code": [None, "517271", "544395", None, "1"],
        "ticker": ["HBLENGINE", "HBLPOWER", "544395-BOM", "STLNETWORK", "X"],
    })
    out = cp.merge_renamed(u).set_index("ticker")
    assert list(out.index) == ["HBLENGINE", "X", "STLNETWORK"]
    assert out.loc["HBLENGINE", "bse_code"] == "517271" and out.loc["HBLENGINE", "median_tv"] == 7.7e8
    assert out.loc["HBLENGINE", "merged_ids"] == ["nse:HBLPOWER"]
    assert out.loc["STLNETWORK", "bse_code"] == "544395"
    assert out.loc["X", "merged_ids"] == []
