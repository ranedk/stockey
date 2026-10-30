from fundamentals.themes import policy as pol

GOOD = {"theme_id": "cdmo", "name": "PLI pharma", "kind": "pli", "direction": "support", "authority": "DoP",
        "announced": "2021-02", "outlay_cr": 15000, "status": "active", "latest_figure": None, "latest_as_of": None,
        "source_url": "https://pib.gov.in/x", "source_title": "t", "source_date": "2026-10-01", "note": "n"}


def test_a_complete_sourced_item_passes():
    assert pol.validate_policy([GOOD], {"cdmo"}) == []


def test_missing_source_bad_enums_bad_dates_and_duplicates_are_refused():
    bad = {**GOOD, "source_url": None, "kind": "grant", "announced": "Feb 2021"}
    errs = pol.validate_policy([bad, GOOD, GOOD], {"cdmo"})
    assert any("no source URL" in e for e in errs)
    assert any("kind 'grant'" in e for e in errs)
    assert any("announced" in e for e in errs)
    assert any("duplicate" in e for e in errs)
    assert any("unknown theme" in e for e in pol.validate_policy([GOOD], {"ems"}))


def test_capacity_points_need_a_number_a_source_and_one_unit_per_series():
    p = {"theme_id": "solar_modules", "series": "almm_list1", "kind": "capacity", "period": "2026-08",
         "value": 217107, "unit": "MW", "cumulative": True, "source_url": "https://x", "source_date": None}
    assert pol.validate_capacity([p], {"solar_modules"}) == []
    errs = pol.validate_capacity([p, {**p, "period": "2026-09", "unit": "GW"}, {**p, "value": "217 GW"},
                                  {**p, "period": "FY2025-26", "source_url": ""}], {"solar_modules"})
    assert any("differs" in e for e in errs)
    assert any("not a number" in e for e in errs)
    assert any("no source URL" in e for e in errs)
