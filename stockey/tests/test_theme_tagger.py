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
