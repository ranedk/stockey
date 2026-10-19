import math

import pandas as pd

from fundamentals.screens.story_score import MAX_HISTORICAL_PE, SCORE_VERSION, guard_own_history


def test_cheap_vs_own_history_is_ignored_when_the_history_is_near_zero_earnings():
    df = pd.DataFrame({"screener_company_id": [1, 2, 3],
                       "valuation_vs_own_history_ratio": [0.06, 0.4, None],   # FISCHER, a real one, none
                       "l2_pe": [97.87, 20.0, None]})
    out = guard_own_history(df)
    assert math.isnan(out.loc[0, "valuation_vs_own_history_ratio"])       # 97.87 / 0.06 ~ 1,631 > 100
    assert out.loc[1, "valuation_vs_own_history_ratio"] == 0.4             # 20 / 0.4 = 50, kept
    assert MAX_HISTORICAL_PE == 100 and SCORE_VERSION == 4
