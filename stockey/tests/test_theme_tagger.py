from fundamentals.themes import tagger

TEXT = "Polycab makes cables.\nBusiness Segments a) Wires & Cables (87% in FY26 vs 84% in FY25): leading maker of HT cables."
IDS = {"power_grid_equipment", "solar_modules"}


def _tag(**kw):
    t = {"theme_id": "power_grid_equipment", "role": "makes HT cables", "evidence_quote": "leading maker of HT cables",
         "exposure_pct": 87, "exposure_quote": "Wires & Cables (87% in FY26 vs 84% in FY25)", "exposure_basis": "segment_share"}
    t.update(kw)
    return t


def test_a_share_copied_from_the_text_is_kept():
    out, dropped = tagger.validate_tag(_tag(), TEXT, IDS)
    assert out["exposure_pct"] == 87 and dropped is None


def test_an_invented_quote_kills_the_tag():
    out, dropped = tagger.validate_tag(_tag(evidence_quote="India's top transformer maker"), TEXT, IDS)
    assert out is None and "evidence" in dropped


def test_a_share_not_in_its_quote_leaves_the_tag_unquantified():
    out, dropped = tagger.validate_tag(_tag(exposure_pct=40), TEXT, IDS)
    assert out["exposure_pct"] is None and "number" in dropped


def test_a_share_without_a_quote_is_never_taken_from_the_model():
    out, dropped = tagger.validate_tag(_tag(exposure_quote=None, exposure_basis="none", exposure_pct=60), TEXT, IDS)
    assert out["exposure_pct"] is None and dropped == "exposure given without a quote"


def test_whole_business_needs_its_sentence_and_counts_as_100():
    out, _ = tagger.validate_tag(_tag(exposure_basis="whole_business", exposure_pct=None,
                                      exposure_quote="Polycab makes cables."), TEXT, IDS)
    assert out["exposure_pct"] == 100.0


def test_unknown_theme_is_dropped():
    out, dropped = tagger.validate_tag(_tag(theme_id="space_mining"), TEXT, IDS)
    assert out is None


def test_a_share_under_the_theme_bar_is_below_min():
    assert tagger.status_of({"exposure_pct": 2.24}, 20) == "below_min"
    assert tagger.status_of({"exposure_pct": 41}, 20) == "quantified"
    assert tagger.status_of({"exposure_pct": None}, 20) == "unquantified"


def test_prompt_rules_exclude_plans_and_side_lines():
    from fundamentals.themes import catalog
    p = tagger.tag_prompt(catalog.load(1))
    assert "Plans, announced capacity" in p and "main business or a named business segment" in p


def test_profile_text_adds_the_annual_report_pages():
    from types import SimpleNamespace
    r = SimpleNamespace(about="Makes wagons.", key_points=None, front="=== page 3 ===\nAt a glance",
                        mda=float("nan"), segment="Segment information")
    t = tagger.profile_text(r)
    assert t.startswith("Makes wagons.") and "From the latest annual report:" in t and "nan" not in t


def test_a_quote_split_by_columns_is_still_found():
    text = ('The CODM reviews the operations of the Company  Order book rose 40%\n'
            'as a single operating segment, namely  Capex Rs 500 crore\n"Electronics Manufacturing Services (EMS)".')
    q = 'The CODM reviews the operations of the Company as a single operating segment, namely "Electronics Manufacturing Services (EMS)".'
    assert tagger.quote_in(q, text)


def test_scattered_words_are_not_a_quote():
    text = ("The company makes cement. " + "filler words here. " * 40 +
            "We also build solar plants in Gujarat. " + "more filler. " * 40 + "Defence orders grew.")
    assert not tagger.quote_in("The company builds solar plants and defence systems in Gujarat", text)
