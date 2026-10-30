import numpy as np
import pandas as pd

from fundamentals.themes import strength


def _quarters(sales, dep, opm, last="2026-09-30"):
    ends = pd.date_range(end=last, periods=len(sales), freq="QE")
    return pd.DataFrame({"company_id": 1, "ticker": "X", "period_end": ends.date, "sales": sales,
                         "depreciation": dep, "opm_pct": opm, "pbt": [10.0] * len(sales),
                         "other_income": [1.0] * len(sales)})


def test_a_member_whose_plant_grows_faster_than_its_sales_shows_the_gap_now_and_a_quarter_ago():
    # sales +10% a year, depreciation doubling: capacity running ahead
    sales = [100, 100, 100, 100, 110, 110, 110, 110, 121, 121]
    dep = [5, 5, 5, 5, 10, 10, 10, 10, 20, 20]
    q = _quarters(sales, dep, [20.0] * 10)
    n = strength.member_numbers(q, pd.Timestamp("2026-10-30").date())
    assert round(n["sales_ttm_yoy"]) == 10 and round(n["dep_ttm_yoy"]) == 100
    assert round(n["sales_ttm_yoy_prev"]) == 10 and n["dep_ttm_yoy_prev"] > 50


def test_weighted_median_follows_the_weight_and_skips_missing_values():
    assert strength.weighted_median([1, 2, 100], [0.1, 0.1, 1.0]) == 100
    assert strength.weighted_median([1, 2, np.nan], [1, 1, 5]) == 1
    assert np.isnan(strength.weighted_median([np.nan], [1]))


def test_series_growth_compares_like_with_like_a_year_apart():
    fytd = pd.DataFrame({"period": ["2025-08", "2025-09", "2026-08", "2026-09"], "value": [10, 20, 15, 30]})
    assert strength.series_yoy(fytd, pd.Timestamp("2026-10-30")) == 50.0     # Sep vs Sep, not vs Aug
    yearly = pd.DataFrame({"period": ["FY2024-25", "FY2025-26"], "value": [100, 80]})
    assert round(strength.series_yoy(yearly, pd.Timestamp("2026-10-30")), 6) == -20.0
    single = pd.DataFrame({"period": ["2026-06-30"], "value": [5]})
    assert np.isnan(strength.series_yoy(single, pd.Timestamp("2026-10-30")))
    stale = pd.DataFrame({"period": ["2022", "2023"], "value": [1, 2]})
    assert np.isnan(strength.series_yoy(stale, pd.Timestamp("2026-10-30")))


def test_policy_score_counts_support_size_and_freshness_and_subtracts_headwinds():
    items = pd.DataFrame([
        {"direction": "support", "status": "active", "outlay_cr": 20000.0, "announced": "2026-03"},   # 1 + 1 + 0.5
        {"direction": "support", "status": "proposed", "outlay_cr": None, "announced": "2025"},       # 0.5
        {"direction": "headwind", "status": "active", "outlay_cr": None, "announced": "2024-01-01"},  # -1
        {"direction": "support", "status": "ended", "outlay_cr": 5.0, "announced": "2020"},           # 0
        {"direction": "support", "status": "active", "outlay_cr": None, "announced": "2027-01"},      # not yet known
    ])
    assert strength.policy_score(items, pd.Timestamp("2026-10-30").date()) == 2.0
