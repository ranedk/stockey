import pandas as pd

from fundamentals.api import themes_queries as tq


def test_themes_empty_before_the_first_watchlist_run(monkeypatch):
    monkeypatch.setattr(tq, "sql_to_df", lambda q, *a, **k: pd.DataFrame())
    assert tq.get_themes() == {"as_of": None, "themes": [], "stocks": []}


def test_only_shown_themes_bring_their_stocks(monkeypatch):
    themes = pd.DataFrame([{"theme_id": "ems", "status": "shown", "as_of_date": "2026-10-30"},
                           {"theme_id": "cdmo", "status": "next", "as_of_date": "2026-10-30"}])
    stocks = pd.DataFrame([{"theme_id": "ems", "company_master_id": "nse:X", "position": 1}])
    seen = []
    calls = iter([pd.DataFrame([{"x": 1}]), themes, stocks])

    def fake(q, *a, **k):
        seen.append(k.get("params"))
        return next(calls)
    monkeypatch.setattr(tq, "sql_to_df", fake)
    out = tq.get_themes()
    assert [t["theme_id"] for t in out["themes"]] == ["ems", "cdmo"] and out["themes"][0]["name"]
    assert seen[-1] == {"ids": ["ems"]} and len(out["stocks"]) == 1
