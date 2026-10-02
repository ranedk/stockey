import datetime as dt

import pandas as pd

from fundamentals.screens import watchlist as wl
from fundamentals.screens import watchlist_exit as wx

TODAY = dt.date(2026, 10, 5)


def _live(rows):
    return pd.DataFrame(rows, columns=["company_master_id", "story_score", "primary_dimension", "flaws", "in_band"])


def test_qualify_story_band_or_event_above_median_never_with_a_flaw():
    live = _live([
        ("nse:BAND", 85, "growth", None, True),
        ("nse:EVT", 60, "value", None, False),          # above the median (50) with a recent positive alert
        ("nse:LOWEVT", 20, "value", None, False),       # recent alert, but a weak story
        ("nse:FLAW", 90, "growth", "cash burn with losses", True),
        ("nse:MID", 50, "events", None, False),
        ("nse:LOW", 10, "events", None, False),
    ])
    q = wl.qualify(live, {"nse:EVT", "nse:LOWEVT", "nse:FLAW", "nse:BAND"}).set_index("company_master_id")["entry_basis"]
    assert q.to_dict() == {"nse:BAND": "story+event", "nse:EVT": "event", "nse:LOWEVT": "none", "nse:FLAW": "none",
                           "nse:MID": "none", "nse:LOW": "none"}


def _status(row, history=()):
    base = {"first_seen_price": 100.0, "current_price": 100.0, "price_data_stale": False,
            "technicals_as_of_date": TODAY, "suggested_watch_until": None, "latest_alert_load_ts": None,
            "narrative_generated_at": None}
    return wx.evaluate_exit_status({**base, **row}, list(history), today=TODAY)[0]


def test_flaw_and_fading_decide_first():
    assert _status({"entry_basis": "none", "flaws": "pledge increased in the last 6 months", "story_score": 88}) == "flawed"
    assert _status({"entry_basis": "none", "flaws": None, "story_score": 40}) == "faded"
    assert _status({"entry_basis": "story", "flaws": None, "story_score": 90}) == "active"


def test_alert_era_checks_apply_only_to_event_members():
    history = [{"trigger_type": "insider_buy", "alert_date": dt.date(2026, 9, 1)},
               {"trigger_type": "insider_sell_surprise", "alert_date": dt.date(2026, 9, 20)}]
    assert _status({"entry_basis": "story", "flaws": None, "story_score": 90}, history) == "active"
    assert _status({"entry_basis": "event", "flaws": None, "story_score": 60}, history) == "invalidated"
    negative_only = [{"trigger_type": "results_decline", "alert_date": dt.date(2026, 9, 1)}]
    assert _status({"entry_basis": "story", "flaws": None, "story_score": 90}, negative_only) == "active"
    assert _status({"entry_basis": None}, negative_only) == "no_thesis"     # legacy rows: old rules


def test_sync_falls_back_to_the_alert_rules_when_live_scores_are_unavailable(monkeypatch):
    monkeypatch.setattr(wl, "_ensure_watchlist_table", lambda: None)
    monkeypatch.setattr(wl, "_ensure_membership_columns", lambda: None)
    monkeypatch.setattr(wl, "load_story_qualification", lambda: None)
    monkeypatch.setattr(wl, "sync_watchlist_from_alerts", lambda: {"companies": 3, "new_candidates": 0})
    assert wl.sync_watchlist()["membership"].startswith("alerts")


def test_sync_adds_only_qualified_newcomers_and_marks_leavers(monkeypatch):
    monkeypatch.setattr(wl, "_ensure_watchlist_table", lambda: None)
    monkeypatch.setattr(wl, "_ensure_membership_columns", lambda: None)
    q = pd.DataFrame([
        {"company_master_id": "nse:NEW", "story_score": 88.0, "primary_dimension": "growth", "flaws": None, "entry_basis": "story"},
        {"company_master_id": "nse:NOPE", "story_score": 20.0, "primary_dimension": "value", "flaws": None, "entry_basis": "none"},
        {"company_master_id": "nse:OLD", "story_score": 30.0, "primary_dimension": "value", "flaws": None, "entry_basis": "none"},
    ])
    monkeypatch.setattr(wl, "load_story_qualification", lambda: q)
    monkeypatch.setattr(wl, "load_l3_alert_summary_by_company", lambda: pd.DataFrame(
        columns=["company_master_id", "positive_alerts", "first_detected_date", "last_detected_date", "alert_count"]))
    monkeypatch.setattr(wl, "load_existing_watchlist", lambda: pd.DataFrame(
        [{"company_master_id": "nse:OLD"}, {"company_master_id": "nse:GONE"}]))
    monkeypatch.setattr(wl, "load_price_near", lambda cid, d: 123.0)
    written = []
    monkeypatch.setattr(wl, "upsert_to_db", lambda df, table, **k: written.append(df))
    result = wl.sync_watchlist()
    assert result["new_candidate_ids"] == ["nse:NEW"]
    # no frame may carry first-seen columns for an EXISTING member (they would be overwritten)
    for df in written:
        if "first_seen_at" in df:
            assert set(df["company_master_id"]) == {"nse:NEW"}
    rows = pd.concat([d.set_index("company_master_id") for d in written])
    assert "nse:NOPE" not in rows.index
    assert rows.at["nse:NEW", "membership_version"] == wl.MEMBERSHIP_VERSION and rows.at["nse:NEW", "first_seen_price"] == 123.0
    assert rows.at["nse:OLD", "entry_basis"] == "none" and rows.at["nse:GONE", "entry_basis"] == "none"


def test_a_stale_company_score_leaves_its_row_alone(monkeypatch):
    monkeypatch.setattr(wl, "_ensure_watchlist_table", lambda: None)
    monkeypatch.setattr(wl, "_ensure_membership_columns", lambda: None)
    q = pd.DataFrame([
        {"company_master_id": "nse:OLD", "story_score": 10.0, "primary_dimension": "value", "flaws": None,
         "entry_basis": "none", "fresh": False},
        {"company_master_id": "nse:NEW", "story_score": 88.0, "primary_dimension": "growth", "flaws": None,
         "entry_basis": "story", "fresh": True},
    ])
    monkeypatch.setattr(wl, "load_story_qualification", lambda: q)
    monkeypatch.setattr(wl, "load_l3_alert_summary_by_company", lambda: pd.DataFrame(
        columns=["company_master_id", "positive_alerts", "first_detected_date", "last_detected_date", "alert_count"]))
    monkeypatch.setattr(wl, "load_existing_watchlist", lambda: pd.DataFrame([{"company_master_id": "nse:OLD"}]))
    monkeypatch.setattr(wl, "load_price_near", lambda cid, d: 1.0)
    written = []
    monkeypatch.setattr(wl, "upsert_to_db", lambda df, table, **k: written.append(df))
    wl.sync_watchlist()
    assert all("nse:OLD" not in set(df["company_master_id"]) for df in written)


def test_price_flag_only_for_event_members():
    big_move = {"first_seen_price": 100.0, "current_price": 170.0}
    assert _status({"entry_basis": "story", "flaws": None, "story_score": 90, **big_move}) == "active"
    assert _status({"entry_basis": "event", "flaws": None, "story_score": 60, **big_move}) == "price_flagged"
