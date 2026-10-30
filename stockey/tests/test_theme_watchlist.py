import pandas as pd

from fundamentals.themes import watchlist as w


def test_auditor_resignation_is_the_companys_own_not_a_subsidiarys_or_a_cag_rotation():
    assert w.auditor_resigned("Resignation of Statutory Auditor")
    assert w.auditor_resigned("M/s VJ Shah & Co. have resigned as the Statutory Auditor of the Company and its subsidiaries")
    assert not w.auditor_resigned("Resignation of statutory auditors of material subsidiaries of the Company.")
    assert not w.auditor_resigned("resignation of M/s BGJC, Joint Statutory Auditor of the Unlisted Material Subsidiaries")
    assert not w.auditor_resigned("tendered their resignation as Joint Statutory Auditors, reason being the firm has been allotted another Statutory Audit Assignment by C&AG.")
    assert not w.auditor_resigned("Appointment of statutory auditor")


def test_cut_to_default_only_on_a_d_rating_or_default():
    assert w.cut_to_default("CRISIL downgrades XYZ to 'CRISIL D'")
    assert not w.cut_to_default("Ratings downgraded to 'Crisil BBB / Stable / Crisil A3+ '")


def test_themes_shown_skip_late_and_the_buffer_keeps_yesterdays_while_in_the_top_8():
    s = pd.DataFrame({"theme_id": list("abcdefghij"), "strength": [0.9, 0.85, 0.8, 0.75, 0.7, 0.65, 0.6, 0.55, 0.5, None],
                      "strength_order": [1, 2, 3, 4, 5, 6, 7, 8, 9, None],
                      "late": [False, True, False, False, False, False, False, False, False, False]})
    got = w.choose_themes(s, previously_shown={"h", "i"}).set_index("theme_id")
    assert got.loc["b", "status"] == "late" and got.loc["j", "status"] == "unranked"
    # h (8th) is kept by the buffer, i (9th) is not; the rest fill by strength
    assert sorted(got.index[got["status"] == "shown"]) == ["a", "c", "d", "e", "h"]
    assert bool(got.loc["h", "kept_by_buffer"]) and got.loc["i", "status"] == "next"


def test_rank_theme_rates_by_fifths_and_marks_the_top_10():
    d = pd.DataFrame({"share_pct": range(20, 40), "trend_score": [x / 10 for x in range(20)],
                      "sales_yoy_pct": range(20), "sales_vs_trend_pp": range(20), "margin_change_pp": range(20),
                      "cwip_ratio": [None] * 20, "dep_growth_pct": range(20)})
    r = w.rank_theme(d)
    assert r["position"].tolist() == list(range(1, 21))
    assert r["rating"].tolist() == list("AAAABBBBCCCCDDDDEEEE")
    assert r["on_watchlist"].sum() == 10 and r.iloc[0]["share_pct"] == 39


def _q(net):
    ends = pd.date_range(end="2026-09-30", periods=len(net), freq="QE")
    return pd.DataFrame({"period_end": ends.date, "net_profit": net})


def test_losses_shrinking_needs_a_loss_two_better_quarters_and_a_better_year():
    assert w.losses_shrinking(_q([-30, -50, -40, -25, -10])).startswith("results improving after a loss: net result Rs -40, -25, -10")
    assert w.losses_shrinking(_q([-30, -50, -40, -25, 5]))           # turned to profit still counts
    assert w.losses_shrinking(_q([-30, -50, -20, -25, -10])) is None  # one quarter went the wrong way
    assert w.losses_shrinking(_q([-5, -50, -40, -25, -10])) is None   # worse than a year ago
    assert w.losses_shrinking(_q([10, 12, 14, 16, 18])) is None       # no losses: nothing to waive
