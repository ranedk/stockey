import pandas as pd

from fundamentals.collectors import fundamentals_snapshot as fs


def test_companies_to_frame_keeps_identity_and_defaults_only_when_asked():
    companies = [{"company_id": 1, "ticker": "ABC", "name": "Abc", "url": "/company/ABC/",
                  "metrics": {"cmp_rs": 10.0, "roe_pct": 12.5, "mar_cap_rscr": 900.0}}]
    plain = fs.companies_to_frame(companies, {"roe_pct": "roe_pct"})
    assert list(plain.columns) == ["screener_company_id", "screener_ticker", "roe_pct"]
    full = fs.companies_to_frame(companies, {"roe_pct": "roe_pct"}, keep_identity=True).iloc[0]
    assert (full["screener_name"], full["price"], full["market_cap_cr"], full["roe_pct"]) == ("Abc", 10.0, 900.0, 12.5)


def test_fetch_snapshot_fails_loudly_if_a_field_is_not_revealed(monkeypatch):
    import fundamentals.collectors.screenerin as scr

    monkeypatch.setattr(scr, "run_query", lambda session, q: ("u", [{"company_id": 1, "ticker": "A", "metrics": {}}]))
    try:
        fs.fetch_snapshot(session=object())
    except RuntimeError as exc:
        assert "did not reveal" in str(exc)
    else:
        raise AssertionError("expected a loud failure")


def test_missing_weekdays_names_every_hole(monkeypatch):
    have = pd.DataFrame({"as_of_date": [pd.Timestamp("2026-09-28").date(), pd.Timestamp("2026-09-30").date()]})
    monkeypatch.setattr(fs, "sql_to_df", lambda *a, **k: have)
    # Mon 28 and Wed 30 stored; Tue 29 and Thu 1 missing; Fri 2 is "today", not yet due
    assert fs.missing_weekdays(pd.Timestamp("2026-10-02").date()) == ["2026-09-29", "2026-10-01"]
