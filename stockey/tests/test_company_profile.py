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
