import pandas as pd

from fundamentals.screens import event_drift as ed


def test_every_recorded_trigger_has_a_frozen_direction():
    assert set(ed.DIRECTION.values()) == {1, -1}
    assert ed.DIRECTION["insider_buy"] == 1 and ed.DIRECTION["results_decline"] == -1


def test_record_new_labels_direction_and_leaves_entry_for_the_next_session(monkeypatch):
    alerts = pd.DataFrame([{"source": "bse", "news_id": "n1", "trigger_type": "insider_buy",
                            "company_master_id": "nse:ABC", "detected_at": pd.Timestamp("2026-10-05 18:00", tz="Asia/Kolkata")}])
    monkeypatch.setattr(ed, "new_alerts", lambda: alerts)
    monkeypatch.setattr(ed, "new_story_reads", lambda: pd.DataFrame())
    import fundamentals.screens.technicals as tech
    monkeypatch.setattr(tech, "load_market_state", lambda syms: {"ABC": {"delivery_change_pp": 6.5,
                                                                         "upper_band_hits_20d": 2, "lower_band_hits_20d": 0}})
    written = []
    monkeypatch.setattr(ed, "upsert_to_db", lambda df, table, **k: written.append(df))
    assert ed.record_new() == 1
    row = written[0].iloc[0]
    assert (row["direction"], row["symbol"], row["entry_price"]) == (1, "ABC", None)
    assert (row["delivery_change_pp"], row["upper_band_hits_20d"]) == (6.5, 2)


def test_report_measures_excess_over_the_universe_only_once_the_horizon_is_reached(monkeypatch):
    rows = pd.DataFrame([{"trigger_type": "bulk_deal_buy", "symbol": "A", "entry_date": "2026-10-06", "entry_price": 100.0},
                         {"trigger_type": "bulk_deal_buy", "symbol": "B", "entry_date": "2026-10-06", "entry_price": 50.0}])
    monkeypatch.setattr(ed, "sql_to_df", lambda *a, **k: rows)
    monkeypatch.setattr(ed, "_forward_return", lambda s, d, h: {"A": 0.10, "B": 0.00}[s] if h == 20 else None)
    monkeypatch.setattr(ed, "_universe_return", lambda d, h, cache: 0.02)
    out = ed.report()["by_trigger"]["bulk_deal_buy"]
    assert out["20d"] == {"n": 2, "mean_excess_pct": 3.0}   # (8% + -2%) / 2
    assert out["40d"] == {"n": 0, "mean_excess_pct": None}  # not reached yet


def test_market_state_blanks_band_hits_for_futures_stocks(monkeypatch):
    import fundamentals.screens.technicals as tech

    delivery = pd.DataFrame([{"symbol": "SMALL", "delivery_pct_20d": 60.0, "delivery_pct_1y_median": 50.0},
                             {"symbol": "BIGFO", "delivery_pct_20d": 40.0, "delivery_pct_1y_median": 45.0}])
    bands = pd.DataFrame([{"symbol": "BIGFO", "upper_band_hits_20d": 3, "lower_band_hits_20d": 1,
                           "upper_band_hits_60d": 4, "lower_band_hits_60d": 2}])
    results = iter([delivery, bands])
    monkeypatch.setattr(tech, "sql_to_df", lambda *a, **k: next(results))
    monkeypatch.setattr(tech, "futures_underlyings", lambda: {"BIGFO"})
    out = tech.load_market_state(["SMALL", "BIGFO"])
    assert out["SMALL"]["delivery_change_pp"] == 10.0 and out["SMALL"]["upper_band_hits_60d"] == 0
    assert out["BIGFO"]["upper_band_hits_20d"] is None and out["BIGFO"]["delivery_change_pp"] == -5.0


def test_story_reads_are_recorded_as_their_own_family(monkeypatch):
    reads = pd.DataFrame([{"source": "story_read", "news_id": "nse:ABC:t", "trigger_type": "story_read_negative",
                           "company_master_id": "nse:ABC", "detected_at": pd.Timestamp("2026-10-05 12:00", tz="UTC")}])
    monkeypatch.setattr(ed, "new_alerts", lambda: pd.DataFrame())
    monkeypatch.setattr(ed, "new_story_reads", lambda: reads)
    import fundamentals.screens.technicals as tech
    monkeypatch.setattr(tech, "load_market_state", lambda syms: {})
    written = []
    monkeypatch.setattr(ed, "upsert_to_db", lambda df, table, **k: written.append(df))
    assert ed.record_new() == 1
    row = written[0].iloc[0]
    assert (row["family"], row["direction"]) == ("story_read", -1)
    assert set(ed.STORY_READ_DIRECTION).isdisjoint(ed.DIRECTION)  # row 48's nine stay frozen


def test_story_reads_count_once_per_company_and_direction():
    t = pd.Timestamp("2026-10-05 12:00", tz="UTC")
    df = pd.DataFrame([
        {"company_master_id": "nse:A", "trigger_type": "story_read_negative", "detected_at": t},
        {"company_master_id": "nse:A", "trigger_type": "story_read_negative", "detected_at": t + pd.Timedelta(hours=5)},
        {"company_master_id": "nse:A", "trigger_type": "story_read_positive", "detected_at": t + pd.Timedelta(days=1)},
        {"company_master_id": "nse:A", "trigger_type": "story_read_negative", "detected_at": t + pd.Timedelta(days=120)},
    ])
    out = ed.dedupe_story_reads(df)
    assert len(out) == 3
