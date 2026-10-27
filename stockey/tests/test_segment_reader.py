from fundamentals.themes import segment_reader as sr

TEXT = """=== page 168 ===
37. Segment Information
The Group has four principal operating and reporting segments; viz. Oil To Chemicals (O2C), Oil and Gas, Retail and Digital Services.
  Revenue from Operations  6,40,972  23,837  3,28,202  1,49,965  70,813  - 10,75,675"""


def _item(**kw):
    it = {"theme_id": "x", "basis": "segment", "segment_name": "Retail", "theme_revenues": ["3,28,202"],
          "total_revenue": "10,75,675", "unit": "Rs crore", "fiscal_year": "FY2025-26",
          "theme_quotes": ["Revenue from Operations  6,40,972  23,837  3,28,202"],
          "total_quote": "70,813  - 10,75,675", "segment_quote": "Retail and Digital Services"}
    it.update(kw)
    return it


def test_share_is_computed_from_two_printed_numbers():
    out, dropped = sr.validate_read(_item(), TEXT)
    assert dropped is None and out["status"] == "quantified" and out["exposure_pct"] == 30.5


def test_a_number_not_in_the_report_is_rejected():
    out, dropped = sr.validate_read(_item(theme_revenues=["3,30,000"]), TEXT)
    assert out["status"] == "invalid" and out["exposure_pct"] is None and "not in the report" in dropped


def test_a_number_missing_from_its_own_quote_is_rejected():
    out, dropped = sr.validate_read(_item(total_quote="Revenue from Operations  6,40,972"), TEXT)
    assert out["status"] == "invalid" and "total_revenue" in dropped


def test_theme_above_total_is_rejected():
    out, _ = sr.validate_read(_item(theme_revenues=["10,75,675"], total_revenue="3,28,202",
                                    theme_quotes=["- 10,75,675"], total_quote="3,28,202"), TEXT)
    assert out["status"] == "invalid"


def test_not_separate_keeps_no_numbers():
    out, dropped = sr.validate_read(_item(basis="not_separate", theme_revenues=[], total_revenue=None), TEXT)
    assert out["status"] == "not_separate" and out["theme_revenue"] is None and dropped is None


def test_parse_amount_takes_only_one_plain_number():
    assert sr.parse_amount("3,65,820") == 365820.0
    assert sr.parse_amount("Rs 2,850.5") == 2850.5
    assert sr.parse_amount("(1,234)") is None and sr.parse_amount("about 300") is None


def test_latest_report_is_the_first_bse_link():
    docs = {"Annual reports": [{"title": "FY26", "links": [{"label": "AR 2026", "url": "https://example.com/a.pdf"}]},
                               {"title": "FY25", "links": [{"label": "AR 2025", "url": "https://www.bseindia.com/x.pdf"}]}]}
    assert sr.latest_report(docs) == ("AR 2025", "https://www.bseindia.com/x.pdf")
    assert sr.latest_report({}) is None


def test_pick_pages_takes_the_segment_note_and_keyword_pages():
    pages = ["cover", "Segment information\nsegment revenue\nreportable segment", "our data centre business grew",
             "data centre data centre capacity", "board report"]
    assert sr.pick_pages(pages, ["data centre"]) == [1, 2, 3]


def test_several_segments_add_up():
    out, dropped = sr.validate_read(_item(theme_revenues=["3,28,202", "1,49,965"],
                                          theme_quotes=["23,837  3,28,202", "3,28,202  1,49,965"]), TEXT)
    assert dropped is None and out["exposure_pct"] == 44.5


def test_a_quote_wrapped_across_columns_still_counts_when_its_words_sit_by_the_number():
    text = "Railways and urban transit: we recorded revenue over   Order book rose\n` 1,550 Crore this year.  Total  23,505"
    item = _item(theme_revenues=["1,550"], theme_quotes=["We recorded revenue over ` 1,550 Crore."],
                 total_revenue="23,505", total_quote="Total  23,505", segment_quote="railways and urban transit")
    out, dropped = sr.validate_read(item, text)
    assert dropped is None and out["exposure_pct"] == 6.6


def test_words_far_from_the_number_do_not_count():
    text = "We recorded revenue over the year." + " filler" * 100 + " 1,550 units shipped.  Total  23,505"
    item = _item(theme_revenues=["1,550"], theme_quotes=["We recorded revenue over 1,550"],
                 total_revenue="23,505", total_quote="Total  23,505", segment_quote="units shipped")
    out, _ = sr.validate_read(item, text)
    assert out["status"] == "invalid"
