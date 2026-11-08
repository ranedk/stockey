from fundamentals.trigger_study import orders


def test_the_prefilter_keeps_order_announcements_and_drops_routine_filings():
    assert orders.is_candidate("Award of Order / Receipt of Order", "XYZ has received an order worth Rs 450 crore")
    assert orders.is_candidate("General updates", "The company has bagged a contract from NHAI")
    assert orders.is_candidate("Updates", "Receipt of Letter of Award from Coal India")
    assert not orders.is_candidate("Trading Window", "closure of trading window from April 1")
    assert not orders.is_candidate("Dividend", "Record date for final dividend")


def test_exchange_boilerplate_and_orders_against_the_company_are_not_candidates():
    assert not orders.is_candidate("Spurt in Volume", "Significant increase in volume has been observed in X. The Exchange, in order to ensure that investors")
    assert not orders.is_candidate("Action(s) taken or orders passed", "X has informed the Exchange about Action(s) taken or orders passed")
    assert not orders.is_candidate("Updates", "Receipt of assessment order from the Income Tax department")
    assert orders.is_candidate("Clarification", "The price movement is due to the order worth Rs 300 crore received from BHEL")


def test_order_values_convert_to_crore_and_need_their_sentence():
    fx = {"usd": 83.0}
    assert orders.to_crore("1,250.5", "crore", "INR", fx) == 1250.5
    assert orders.to_crore("45", "million", "USD", fx) == 45 * 0.1 * 83.0
    assert orders.to_crore("350", "lakh", "INR", fx) == 3.5
    assert orders.to_crore("12", "furlongs", "INR", fx) is None
    text = "The company has received an order worth Rs 450 crore from NTPC for supply of transformers."
    ok = orders.validate_classified({"order_win": True, "value": "450", "unit": "crore", "currency": "INR",
                                     "quote": "received an order worth Rs 450 crore from NTPC"}, text, fx)
    assert ok["value_cr"] == 450.0
    bad = orders.validate_classified({"order_win": True, "value": "540", "unit": "crore", "currency": "INR",
                                      "quote": "received an order worth Rs 450 crore from NTPC"}, text, fx)
    assert bad["value_cr"] is None and "not found" in bad["value_note"]
    rep = orders.validate_classified({"order_win": True, "repeat": True, "value": "450"}, text, fx)
    assert rep["order_win"] is False


def test_trailing_sales_use_four_quarters_published_before_the_order():
    import pandas as pd
    from fundamentals.trigger_study import order_test as ot
    res = pd.DataFrame({"period_end": pd.to_datetime(["2021-06-30", "2021-09-30", "2021-12-31", "2022-03-31", "2022-06-30"]),
                        "public_at": pd.to_datetime(["2021-08-10", "2021-11-10", "2022-02-10", "2022-05-20", "2022-08-10"]),
                        "consolidated": [True] * 5, "sales": [100.0, 110.0, 120.0, 130.0, 140.0]})
    assert ot.ttm_sales(res, pd.Timestamp("2022-06-01")) == 460.0      # the June 2022 quarter is not out yet
    assert ot.ttm_sales(res, pd.Timestamp("2022-09-01")) == 500.0
    assert pd.isna(ot.ttm_sales(res, pd.Timestamp("2022-01-01")))       # only two quarters known


def test_stage2_reads_the_last_completed_week():
    import numpy as np
    import pandas as pd
    from fundamentals.trigger_study import order_test as ot
    days = pd.bdate_range("2020-01-01", periods=300)
    up = pd.DataFrame({"X": np.linspace(100, 200, 300), "Y": np.linspace(200, 100, 300)}, index=days)
    s2 = ot.stage2(up)
    assert bool(s2["X"].iloc[-1]) and not bool(s2["Y"].iloc[-1])
    assert not s2["X"].iloc[:140].any()                                 # 30 weeks of history first


def test_mean_p_charges_the_cost_and_clusters_by_month():
    import pandas as pd
    from fundamentals.trigger_study import order_test as ot
    d = pd.DataFrame({"entry": pd.to_datetime(["2020-01-10", "2020-01-20", "2020-02-10", "2020-03-10"] * 5),
                      "A60": [0.05, 0.07, 0.04, 0.06] * 5})
    gap, p, n = ot.mean_p(d, "A60")
    assert round(gap, 4) == round(0.055 - 0.005, 4) and p < 0.01 and n == 20


def test_a_range_takes_its_lower_bound_and_k_is_not_counted_twice():
    fx = {"usd": 80.0}
    assert orders.to_crore("125-150", "crore", "INR", fx) == 125.0
    assert orders.to_crore("100+", "crore", "INR", fx) == 100.0
    assert orders.to_crore("96K", "thousand", "USD", fx) == 96 * 1e-4 * 80.0


def test_the_l1_prefilter_keeps_lowest_bidder_news_and_drops_other_l1_uses():
    from fundamentals.trigger_study.l1_bids import is_l1_candidate
    assert is_l1_candidate(None, "DBL has emerged as L-1 bidder for a new HAM project")
    assert is_l1_candidate(None, "The company is the lowest bidder for the NHAI package")
    assert is_l1_candidate("Bagging/Receiving of orders/contracts", "declared L1 in the tender")
    assert not is_l1_candidate(None, "Level 1 certification received")
    assert not is_l1_candidate(None, "Regulation 30 of SEBI LODR: Q1 results")
    assert not is_l1_candidate(None, "Significant increase in volume has been observed in L1 bidder news")


def test_l1_validation_uses_the_order_value_checks():
    from fundamentals.trigger_study.l1_bids import validate
    text = "XYZ has been declared L1 bidder for a project worth Rs. 1,060 Crores."
    ok = validate({"l1": True, "value": "1,060", "unit": "crore", "currency": "INR",
                   "quote": "declared L1 bidder for a project worth Rs. 1,060 Crores"}, text, {})
    assert ok["l1"] and ok["value_cr"] == 1060.0
    bad = validate({"l1": True, "value": "2,000", "unit": "crore", "currency": "INR", "quote": "not in the text"}, text, {})
    assert bad["value_cr"] is None and "not found" in bad["value_note"]
    assert validate({"l1": False}, text, {})["l1"] is False


def test_l1_evaluate_applies_bh_and_the_year_rule():
    import numpy as np
    import pandas as pd
    from fundamentals.trigger_study import l1_test
    rng = np.random.default_rng(0)
    entry = pd.to_datetime([f"{y}-{m:02d}-10" for y in range(2018, 2023) for m in range(1, 13)])
    ev = pd.DataFrame({"entry": entry, **{f"L{h}": 0.05 + rng.normal(0, 0.01, len(entry)) for h in l1_test.HORIZONS}})
    out = l1_test.evaluate(ev)
    assert all(out["members"][f"L1-{h}"]["pass"] for h in l1_test.HORIZONS)
    ev2 = ev.assign(**{f"L{h}": 0.0 for h in l1_test.HORIZONS})
    assert not any(l1_test.evaluate(ev2)["members"][f"L1-{h}"]["pass"] for h in l1_test.HORIZONS)
