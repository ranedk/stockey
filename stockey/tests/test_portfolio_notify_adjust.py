from fundamentals.screens.portfolio_notify import build_action_email

BOOK = {"open_count": 17, "committed_rs": 6.8e6, "is_live": False}


def test_trims_and_rebalances_are_in_the_email():
    actions = {"day": "2026-10-20", "entered": [], "exited": [], "deferred": [], "adjusted": [
        {"ticker": "ABC", "kind": "trim", "price": 210.0, "value_before_rs": 600000.0, "value_after_rs": 400000.0,
         "target_value_rs": None, "reason": "valuation 2.3x its own history"},
        {"ticker": "XYZ", "kind": "rebalance_up", "price": 50.0, "value_before_rs": 150000.0, "value_after_rs": 400000.0,
         "target_value_rs": 400000.0, "reason": "drifted 50% from target weight"}]}
    subject, text, html_body = build_action_email(actions, BOOK)
    assert subject == "[Portfolio] 2 adjust -- 2026-10-20"
    assert "ABC: Trim a third (valuation) | Rs 600,000 -> Rs 400,000 (sell Rs 200,000 at 210.00)" in text
    assert "XYZ: Rebalance up to target" in text and "buy Rs 250,000" in text
    assert "<h1>Adjust</h1>" in html_body and "No action today" not in html_body


def test_a_day_with_nothing_still_says_so():
    subject, text, _ = build_action_email({"day": "2026-10-20", "entered": [], "exited": [], "deferred": []}, BOOK)
    assert subject == "[Portfolio] No action -- 2026-10-20" and "No entries, exits or adjustments today." in text
