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
            "profit_y1": 100.0, "profit_y2": 100.0, "contingent_liabilities": 10.0, "promoter_pledged_pct": 0.0,
            "ocf_y1": 50.0, "ocf_y2": 50.0, "ocf_3y_total": 150.0, "return_on_assets_pct": 1.5, "net_npa_pct": 1.0}
    base.update(kw)
    return l2.layer2_reasons(base)


def test_clean_company_passes_in_every_group():
    for g in ["lender", "other_financial", "realty_holding", "operating"]:
        assert _r(g) == []


def test_contingent_liabilities_not_applied_to_lenders_and_needs_full_net_worth():
    assert _r("lender", contingent_liabilities=50_000.0) == []
    assert _r("operating", contingent_liabilities=999.0) == []
    assert _r("operating", contingent_liabilities=1000.0) == ["contingent liabilities >= net worth"]


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
