import pandas as pd

from fundamentals.screens import event_drift as ed


def test_every_recorded_trigger_has_a_frozen_direction():
    assert set(ed.DIRECTION.values()) == {1, -1}
    assert ed.DIRECTION["insider_buy"] == 1 and ed.DIRECTION["results_decline"] == -1


def test_record_new_labels_direction_and_leaves_entry_for_the_next_session(monkeypatch):
    alerts = pd.DataFrame([{"source": "bse", "news_id": "n1", "trigger_type": "insider_buy",
                            "company_master_id": "nse:ABC", "detected_at": pd.Timestamp("2026-10-05 18:00", tz="Asia/Kolkata")}])
    monkeypatch.setattr(ed, "new_alerts", lambda: alerts)
    written = []
    monkeypatch.setattr(ed, "upsert_to_db", lambda df, table, **k: written.append(df))
    assert ed.record_new() == 1
    row = written[0].iloc[0]
    assert (row["direction"], row["symbol"], row["entry_price"]) == (1, "ABC", None)


def test_report_measures_excess_over_the_universe_only_once_the_horizon_is_reached(monkeypatch):
    rows = pd.DataFrame([{"trigger_type": "bulk_deal_buy", "symbol": "A", "entry_date": "2026-10-06", "entry_price": 100.0},
                         {"trigger_type": "bulk_deal_buy", "symbol": "B", "entry_date": "2026-10-06", "entry_price": 50.0}])
    monkeypatch.setattr(ed, "sql_to_df", lambda *a, **k: rows)
    monkeypatch.setattr(ed, "_forward_return", lambda s, d, h: {"A": 0.10, "B": 0.00}[s] if h == 20 else None)
    monkeypatch.setattr(ed, "_universe_return", lambda d, h, cache: 0.02)
    out = ed.report()["by_trigger"]["bulk_deal_buy"]
    assert out["20d"] == {"n": 2, "mean_excess_pct": 3.0}   # (8% + -2%) / 2
    assert out["40d"] == {"n": 0, "mean_excess_pct": None}  # not reached yet
