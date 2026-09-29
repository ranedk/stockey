import json

import pandas as pd

from fundamentals.screens import story_read as sr


def test_only_material_reads_become_alerts_and_a_deal_breaker_blocks_the_positive_one():
    assert sr.alert_type({"materiality": 3, "direction": "improving", "deal_breaker": None}) is None
    assert sr.alert_type({"materiality": 4, "direction": "improving", "deal_breaker": None}) == "story_read_positive"
    assert sr.alert_type({"materiality": 5, "direction": "improving", "deal_breaker": "pledge"}) is None
    assert sr.alert_type({"materiality": 4, "direction": "deteriorating", "deal_breaker": "x"}) == "story_read_negative"
    assert sr.alert_type({"materiality": 5, "direction": "unchanged", "deal_breaker": None}) is None


def test_picks_results_filings_and_band_entries_first_within_the_cap_and_universe():
    now = pd.Timestamp("2026-10-05 12:00", tz="UTC")
    t = pd.DataFrame([
        {"company_master_id": "nse:A", "ref": "change:story_fading:1", "kind": "story_fading", "at": now},
        {"company_master_id": "nse:B", "ref": "filing:bse:1", "kind": "filing:results", "at": now - pd.Timedelta(hours=5)},
        {"company_master_id": "nse:C", "ref": "change:band_enter:1", "kind": "band_enter", "at": now},
        {"company_master_id": "nse:C", "ref": "filing:bse:2", "kind": "filing:rating_action", "at": now},
        {"company_master_id": "nse:OUT", "ref": "filing:bse:3", "kind": "filing:results", "at": now},
    ])
    picks = sr.pick_companies(t, {"nse:A", "nse:B", "nse:C"}, cap=2)
    assert [p[0] for p in picks] == ["nse:B", "nse:C"]
    assert sorted(picks[1][1]) == ["change:band_enter:1", "filing:bse:2"]   # all of C's evidence is read together


def test_top_readings_are_the_strongest_across_dimensions():
    readings = {"growth": {"sales_yoy": {"pct": 91.0, "kind": "change"}},
                "value": {"earnings_yield": {"pct": 99.0, "kind": "level"}, "low_ev_ebit": {"pct": 12.0, "kind": "level"}}}
    top = sr.top_readings(json.dumps(readings), n=2)
    assert [r["reading"] for r in top] == ["earnings_yield", "sales_yoy"]
    assert sr.top_readings(None) == []


def test_story_read_alerts_do_not_feed_the_story_score():
    from fundamentals.screens import story_score as ss

    for t in ("story_read_positive", "story_read_negative"):
        assert t not in ss.POSITIVE_TRIGGERS and t not in ss.NEGATIVE_TRIGGERS


def test_negative_read_counts_against_a_watchlist_name():
    from fundamentals.screens.watchlist import NEGATIVE_TRIGGER_TYPES

    assert "story_read_negative" in NEGATIVE_TRIGGER_TYPES and "story_read_positive" not in NEGATIVE_TRIGGER_TYPES
