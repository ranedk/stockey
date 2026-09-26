from fundamentals.screens import universe_layer2 as l2


def test_parse_npa_takes_latest_filled_quarter_from_first_table():
    html = """
    <table><tr class=""> <td class="text"> Gross NPA % </td> <td class="highlight-cell"> </td>
      <td class=""> 2.53% </td> <td class=""> 2.13% </td> <td class=""> </td> </tr>
    <tr class="stripe"> <td class="text"> Net NPA % </td> <td class=""> 0.60% </td> <td class=""> 0.52% </td> </tr></table>
    <table><tr> <td class="text"> Gross NPA % </td> <td> 9.99% </td> </tr></table>
    """
    assert l2.parse_npa(html) == {"gross_npa_pct": 2.13, "net_npa_pct": 0.52}


def test_parse_npa_missing_rows_is_none():
    assert l2.parse_npa("<table><tr><td class=\"text\"> Sales </td><td> 1 </td></tr></table>") == {
        "gross_npa_pct": None, "net_npa_pct": None}


def _r(group, **kw):
    base = {"group": group, "basic_industry": None, "net_worth": 1000.0, "debt_to_equity": 0.5, "interest_cover": 5.0,
            "profit_y1": 100.0, "profit_y2": 100.0, "promoter_pledged_pct": 0.0,
            "ocf_y1": 50.0, "ocf_y2": 50.0, "ocf_3y_total": 150.0, "return_on_assets_pct": 1.5, "net_npa_pct": 1.0}
    base.update(kw)
    return l2.layer2_reasons(base)


def test_clean_company_passes_in_every_group():
    for g in ["lender", "other_financial", "realty_holding", "operating"]:
        assert _r(g) == []


def test_contingent_liabilities_no_longer_checked():
    assert _r("operating", contingent_liabilities=50_000.0) == []


def test_operating_cash_burn_excludes_only_with_a_loss_last_year():
    burning = {"ocf_y1": -10.0, "ocf_y2": -10.0, "ocf_3y_total": 30.0}  # third year derived as +50
    assert _r("operating", **burning) == []  # order-book grower, profitable
    assert _r("operating", profit_y1=-5.0, **burning) == [
        "operating cash flow negative in 2 of the last 3 years and a loss last year"]


def test_operating_debt_needs_both_high_leverage_and_thin_cover():
    assert _r("operating", debt_to_equity=3.0, interest_cover=2.0) == []
    assert _r("operating", debt_to_equity=3.0, interest_cover=1.2) == ["debt/equity >= 2 with interest cover < 1.5"]


def test_realty_debt_check_applies_to_developers_not_holding_companies():
    assert _r("realty_holding", basic_industry="Investment Company", debt_to_equity=4.0) == []
    assert _r("realty_holding", basic_industry="Residential, Commercial Projects", debt_to_equity=1.6) == [
        "developer debt/equity >= 1.5"]
    assert _r("realty_holding", basic_industry="Holding Company", net_worth=-5.0) == ["negative net worth"]


def test_lender_checks_and_missing_data_never_excludes():
    assert _r("lender", net_npa_pct=6.0) == ["net NPA >= 6%"]
    assert _r("lender", return_on_assets_pct=-0.1) == ["return on assets <= 0"]
    assert _r("lender", net_npa_pct=None, return_on_assets_pct=None) == []
    assert _r("other_financial", profit_y1=-1.0, profit_y2=-1.0) == ["loss-making both of the last 2 years"]
    assert _r("operating", promoter_pledged_pct=50.0) == ["promoter pledge >= 50%"]


_GROWER = {"profit_y1": -50.0, "profit_y2": -80.0, "sales_growth_pct": 45.0, "sales_growth_q_pct": 40.0,
           "profit_q": -10.0, "profit_q_year_ago": -30.0, "opm_q": -5.0, "opm_q_year_ago": -15.0,
           "debt_to_equity": 0.1, "market_cap_rs": 50_000e7, "median_value_rs": 100e7, "price_to_sales": 5.0}


def test_scaling_growth_lifts_loss_checks_only():
    assert _r("operating", profit_y1=-50.0, profit_y2=-80.0) == ["loss-making both of the last 2 years"]
    assert _r("operating", **_GROWER) == []
    # never lifts a pledge or debt finding
    assert _r("operating", **{**_GROWER, "promoter_pledged_pct": 60.0}) == ["promoter pledge >= 50%"]


def test_scaling_growth_needs_every_leg():
    for broken in [{"sales_growth_q_pct": 10.0}, {"profit_q": -40.0}, {"opm_q": -20.0}, {"debt_to_equity": 0.8},
                   {"market_cap_rs": 1000e7}, {"median_value_rs": 1e7}, {"price_to_sales": 1.5}]:
        assert _r("operating", **{**_GROWER, **broken}) == ["loss-making both of the last 2 years"], broken


def test_turnaround_needs_ttm_and_two_profitable_quarters():
    base = {"profit_y1": -50.0, "profit_y2": -80.0, "profit_ttm": 90.0, "profit_q": 30.0, "profit_prev_q": 20.0}
    assert _r("operating", **base) == []
    assert _r("other_financial", **base) == []
    assert _r("operating", **{**base, "profit_prev_q": -5.0}) == ["loss-making both of the last 2 years"]


def test_apply_layer2_records_which_rule_rescued():
    import pandas as pd
    rows = [{"symbol": "GROW", "group": "operating", "net_worth": 1000.0, "interest_cover": 5.0, "ocf_y1": 1.0,
             "ocf_y2": 1.0, "ocf_3y_total": 3.0, "promoter_pledged_pct": 0.0, **_GROWER},
            {"symbol": "CLEAN", "group": "operating", "net_worth": 1000.0, "profit_y1": 5.0, "profit_y2": 5.0}]
    out = l2.apply_layer2(pd.DataFrame(rows)).set_index("symbol")
    assert out.at["GROW", "layer2_allowed_by"] == "scaling_growth" and bool(out.at["GROW", "layer2_pass"])
    assert out.at["CLEAN", "layer2_allowed_by"] is None
