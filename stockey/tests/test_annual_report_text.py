from fundamentals.collectors import annual_report_text as art

FILL = " revenue segment product capacity business" * 40


def _pages():
    return ["cover", "Contents  Management Discussion and Analysis  12", "At a glance: we make transformers." + FILL,
            "x", "Management Discussion and Analysis\nGlobal economy grew. Indian economy too. Outlook fine.",
            "Our segments: Power Systems" + FILL, "Segment information\nsegment revenue\nreportable segment", "notes"]


def test_mda_starts_at_the_section_not_the_contents_page():
    assert art.mda_pages(_pages())[0] == 4


def test_front_page_needs_text_and_skips_the_contents():
    assert art.front_pages(_pages()) == [2]


def test_extract_keeps_page_numbers_and_the_segment_note():
    out = art.extract(_pages())
    assert '"segment": [7' in out["pages"] and "Segment information" in out["segment"]
    assert out["front"].startswith("=== page 3 ===")


def test_latest_report_is_the_first_bse_link():
    docs = {"Annual reports": [{"title": "FY26", "links": [{"label": "AR 2026", "url": "https://example.com/a.pdf"}]},
                               {"title": "FY25", "links": [{"label": "AR 2025", "url": "https://www.bseindia.com/x.pdf"}]}]}
    assert art.latest_report(docs) == ("AR 2025", "https://www.bseindia.com/x.pdf")
    assert art.latest_report({}) is None
