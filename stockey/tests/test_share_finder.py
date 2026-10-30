from fundamentals.themes import share_finder as sf

TEXT = ("Voltamp Transformers Limited is engaged solely in the manufacture of power and distribution "
        "transformers. Revenue from transformers was Rs 1,950 crore out of total revenue from operations of Rs 2,000 crore.")


def test_sole_business_needs_its_sentence_in_the_text():
    ok = sf.validate_sole({"verdict": "sole", "quote": "engaged solely in the manufacture of power and distribution transformers"}, TEXT)
    assert ok["status"] == "quantified" and ok["exposure_pct"] == 100.0
    made_up = sf.validate_sole({"verdict": "sole", "quote": "The company only makes transformers for utilities"}, TEXT)
    assert made_up["status"] == "unclear"
    other = sf.validate_sole({"verdict": "other", "quote": "engaged solely in the manufacture", "other_business": "none"}, TEXT)
    assert other["status"] == "other_business" and "exposure_pct" not in other


def test_a_share_from_other_sources_is_computed_in_code_from_grounded_numbers():
    got = sf.validate_source({"result": "found", "theme_revenue": "1,950", "total_revenue": "2,000",
                              "theme_quote": "Revenue from transformers was Rs 1,950 crore",
                              "total_quote": "total revenue from operations of Rs 2,000 crore",
                              "document": "results filing", "period": "FY26"}, TEXT)
    assert got["status"] == "quantified" and got["exposure_pct"] == 97.5
    wrong = sf.validate_source({"result": "found", "theme_revenue": "1,500", "total_revenue": "2,000",
                                "theme_quote": "Revenue from transformers was Rs 1,500 crore",
                                "total_quote": "total revenue from operations of Rs 2,000 crore"}, TEXT)
    assert wrong["status"] == "invalid"
    pct = sf.validate_source({"result": "found", "pct": 62, "pct_quote": "Cables contributed 62% of revenue"},
                             "In FY26, Cables contributed 62% of revenue, wires the rest.")
    assert pct["status"] == "quantified" and pct["exposure_pct"] == 62.0
    assert sf.validate_source({"result": "none"}, TEXT)["status"] == "not_found"


def test_a_total_or_continuing_operations_line_is_not_a_theme_line():
    text = "Continuing operations 12,498.90 6,714.92\n(a) Revenue from operations 18,335.08 10,887.31\n| | Total | 320.96 |"
    for tq, raw in (("Continuing operations 12,498.90 6,714.92", "12,498.90"), ("| | Total | 320.96 |", "320.96")):
        got = sf.validate_source({"result": "found", "theme_revenue": raw, "total_revenue": "18,335.08",
                                  "theme_quote": tq, "total_quote": "(a) Revenue from operations 18,335.08 10,887.31"}, text)
        assert got["status"] == "invalid", tq
