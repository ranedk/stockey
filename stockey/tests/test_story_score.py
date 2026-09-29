import json

import numpy as np
import pandas as pd

from fundamentals.screens import story_score as ss


def test_standout_discounts_the_luck_of_many_readings():
    assert ss.standout(95, 1) == 95.0
    assert round(ss.standout(95, 16), 1) == 44.0      # best of 16 at the 95th pct is common
    assert round(ss.standout(99.9, 16), 1) == 98.4    # a true outlier still stands out
    assert ss.standout(100, 30) == 100.0


def _company(i, group="operating", **kw):
    base = {"company_master_id": f"nse:C{i}", "ticker": f"C{i}", "group": group,
            "sales_growth_5y_pct": 10.0, "profit_growth_5y_pct": 10.0, "sales_growth_q_pct": 10.0,
            "profit_growth_q_pct": 10.0, "roce_pct": 15.0, "roe_pct": 15.0, "roce_5y_avg_pct": 15.0,
            "roe_5y_avg_pct": 15.0, "debt_to_equity": 0.3, "interest_cover": 8.0, "fcf_3y_total": 30.0,
            "market_cap_cr": 1000.0, "earnings_yield_pct": 6.0, "ev_to_ebit": 15.0, "price_to_book": 3.0,
            "valuation_vs_own_history_ratio": 1.0, "event_net": np.nan, "ocf_3y_total": 50.0,
            "profit_y1": 10.0, "profit_y2": 10.0, "promoter_pledged_pct": 0.0, "sales_q": 500.0}
    base.update(kw)
    return base


def test_one_exceptional_dimension_makes_the_story_and_a_flaw_caps_it():
    rows = [_company(i) for i in range(20)]
    rows.append(_company(99, sales_growth_q_pct=80.0, profit_growth_q_pct=90.0))       # growth inflection
    rows.append(_company(98, sales_growth_q_pct=80.0, profit_growth_q_pct=90.0,
                         profit_y1=-5.0, profit_y2=-5.0, ocf_3y_total=-10.0))            # same, but burning cash
    out = ss.score(pd.DataFrame(rows)).set_index("symbol")
    assert out.at["C99", "primary_dimension"] == "growth" and out.at["C99", "primary_kind"] == "acceleration"
    assert out.at["C99", "story_score"] > out["story_score"].drop(["C99", "C98"]).max()
    assert out.at["C98", "story_score"] == ss.FLAW_CAP and "cash burn" in out.at["C98", "flaws"]


def test_allow_rule_companies_are_not_flawed_for_their_accepted_losses():
    rows = [_company(i) for i in range(10)]
    rows.append(_company(50, profit_y1=-5.0, profit_y2=-5.0, ocf_3y_total=-10.0, layer2_allowed_by="scaling_growth"))
    out = ss.score(pd.DataFrame(rows)).set_index("symbol")
    assert pd.isna(out.at["C50", "flaws"])


def test_implausible_returns_are_ignored_and_pb_only_counts_for_lenders():
    rows = [_company(i) for i in range(10)] + [_company(60, roce_pct=164.0, roce_5y_avg_pct=40.0)]
    out = ss.score(pd.DataFrame(rows)).set_index("symbol")
    readings = json.loads(out.at["C60", "readings_json"])
    assert "roce_improvement" not in readings.get("profitability", {})
    assert "low_price_to_book" not in readings.get("value", {})
    lenders = ss.score(pd.DataFrame([_company(i, group="lender") for i in range(10)])).iloc[0]
    assert "low_price_to_book" in json.loads(lenders["readings_json"]).get("value", {})


def test_one_off_return_voids_its_five_year_average_and_tiny_bases_do_not_accelerate():
    rows = [_company(i) for i in range(10)]
    rows.append(_company(70, roe_pct=168.0, roe_5y_avg_pct=42.0))
    rows.append(_company(71, sales_q=40.0, sales_growth_q_pct=314.0))
    rows = [{"sales_q": 500.0, **r} for r in rows]
    x = ss.derive(pd.DataFrame(rows)).set_index("ticker")
    assert pd.isna(x.at["C70", "roe_5y_avg_pct"]) and pd.isna(x.at["C70", "roe_pct"])
    assert pd.isna(x.at["C71", "sales_acceleration"]) and pd.notna(x.at["C1", "sales_acceleration"])
