import pandas as pd

from fundamentals.api import queries


def test_story_scores_empty_before_first_run(monkeypatch):
    monkeypatch.setattr(queries, "sql_to_df", lambda q, *a, **k: pd.DataFrame())
    assert queries.get_story_scores() == []


def test_story_scores_keyed_by_symbol(monkeypatch):
    calls = iter([pd.DataFrame({"x": [1]}), pd.DataFrame([{"symbol": "TCS", "company_master_id": "nse:TCS", "story_score": 91.0,
                                                          "primary_dimension": "profitability", "flaws": None, "in_band": True,
                                                          "watchlist_status": "active"}])])
    monkeypatch.setattr(queries, "sql_to_df", lambda q, *a, **k: next(calls))
    rows = queries.get_story_scores()
    assert rows[0]["symbol"] == "TCS" and rows[0]["story_score"] == 91.0
