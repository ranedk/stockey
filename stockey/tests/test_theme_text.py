from fundamentals.themes import theme_text as tt


def test_numbers_in_the_text_must_come_from_the_evidence():
    ev = {"revenue_share_pct": 78.0, "sales_growth_yoy_pct": 23.4, "weeks_in_stage": 6,
          "capital_work_in_progress_pct_of_fixed_assets": 31.0, "notes": ["SEBI order in its matter (2023-08-28)"]}
    ok = "About 78% of revenue is defence radar work. Sales grew 23% last quarter; it has been rising for 6 weeks."
    assert tt.unsupported_numbers(ok, ev) == []
    assert tt.unsupported_numbers("Work in progress is 31% of fixed assets.", ev) == []
    assert tt.unsupported_numbers("A 2023 order named its shares.", ev) == []
    assert tt.unsupported_numbers("Sales grew 40% and margins rose 3.5 points.", ev) == ["40", "3.5"]


def test_fingerprint_ignores_small_moves_but_not_a_new_quarter_or_rating():
    base = {"rating": "A", "stage": "Stage 2 (rising)", "early_breakout": True, "revenue_share_pct": 78.0,
            "latest_quarter": "2026-09-30", "sales_growth_yoy_pct": 23.0, "operating_margin_change_pp": 1.0, "notes": []}
    fp = tt._fp(tt.stock_fingerprint(base))
    assert tt._fp(tt.stock_fingerprint({**base, "revenue_share_pct": 79.0, "sales_growth_yoy_pct": 24.0})) == fp
    assert tt._fp(tt.stock_fingerprint({**base, "latest_quarter": "2026-12-31"})) != fp
    assert tt._fp(tt.stock_fingerprint({**base, "rating": "B"})) != fp


def test_a_window_named_in_a_field_name_may_be_written():
    assert tt.unsupported_numbers("8% ahead of the market over 26 weeks.", {"theme_index_vs_market_26w_pct": 8.0}) == []
