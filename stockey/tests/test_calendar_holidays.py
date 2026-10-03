import pandas as pd

from data.nseindia import calendar_creator as cc


def test_equity_holidays_leave_the_calendar_and_other_segments_do_not(monkeypatch):
    year = pd.Timestamp.today().year

    def fake_sql(q, params=None):
        if "FROM nseindia_ohlcv" in q:
            return pd.DataFrame({"date": []})
        if "ILIKE" in q:  # muhurat overrides
            return pd.DataFrame({"date": []})
        assert "type = 'CM'" in q, "only equity cash-market holidays may remove a session"
        # timestamptz from Postgres: tz-aware -- the exact shape that used to remove nothing
        return pd.DataFrame({"date": pd.to_datetime([f"{year}-10-02", f"{year}-12-25"], utc=True)})

    monkeypatch.setattr(cc, "sql_to_df", fake_sql)
    days = set(pd.to_datetime(cc.compute_trading_days()["date"]).dt.date.astype(str))
    for h in (f"{year}-10-02", f"{year}-12-25"):
        if pd.Timestamp(h).weekday() < 5:
            assert h not in days, f"{h} is an equity holiday"
    assert f"{year}-08-26" in days or pd.Timestamp(f"{year}-08-26").weekday() >= 5


def test_only_laxmi_pujan_is_added_back_as_a_muhurat_session(monkeypatch):
    seen = []
    monkeypatch.setattr(cc, "sql_to_df", lambda q, params=None: seen.append(q) or pd.DataFrame({"date": []}))
    cc.compute_trading_days()
    muhurat = [q for q in seen if "ILIKE" in q]
    assert muhurat and "laxmi" in muhurat[0].lower() and "%%diwali%%" not in muhurat[0]
