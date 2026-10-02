import datetime as dt

import pandas as pd

from fundamentals.screens import results_reading as rr


def _quarters(n=13, last="2026-06-30", growth=0.10, last_growth=None, opm=20.0, last_opm=None,
              other_income=5.0, last_other_income=None):
    ends = pd.date_range(end=last, periods=n, freq="QE")
    rows = []
    for i, e in enumerate(ends):
        years = i / 4
        g = growth
        sales = 100.0 * (1 + g) ** years
        if i == n - 1 and last_growth is not None:
            sales = 100.0 * (1 + g) ** ((i - 4) / 4) * (1 + last_growth)
        m = last_opm if (i == n - 1 and last_opm is not None) else opm
        oi = last_other_income if (i == n - 1 and last_other_income is not None) else other_income
        op = sales * m / 100
        rows.append({"period_end": e.date(), "sales": sales, "opm_pct": m, "operating_profit": op,
                     "other_income": oi, "interest": 1.0, "depreciation": 2.0, "pbt": op + oi - 3.0})
    return pd.DataFrame(rows)


AS_OF = dt.date(2026, 8, 15)


def test_steady_grower_reads_steady_and_a_jump_reads_accelerating():
    steady = rr.read_company(_quarters(), AS_OF)
    assert abs(steady["sales_yoy_pct"] - 10) < 0.01 and steady["sales_direction"] == "steady"
    jump = rr.read_company(_quarters(last_growth=0.35), AS_OF)
    assert jump["sales_direction"] == "accelerating" and abs(jump["sales_vs_trend_pp"] - 25) < 0.01


def test_one_off_in_other_income_is_stripped_from_underlying_profit():
    base = rr.read_company(_quarters(), AS_OF)
    spiked = rr.read_company(_quarters(last_other_income=500.0), AS_OF)
    assert abs(spiked["underlying_pbt_cr"] - base["underlying_pbt_cr"]) < 1e-9
    assert abs(spiked["profit_yoy_pct"] - base["profit_yoy_pct"]) < 1e-9
    assert spiked["one_off_cr"] == 495.0 and spiked["one_off_share"] > 0.8


def test_margin_change_is_year_on_year_and_against_trend():
    r = rr.read_company(_quarters(last_opm=24.0), AS_OF)
    assert r["margin_change_pp"] == 4.0 and r["margin_vs_trend_pp"] == 4.0


def test_stale_latest_quarter_gives_no_reading():
    assert rr.read_company(_quarters(last="2025-12-31"), AS_OF) is None


def test_quarter_rows_map_the_lender_layout():
    table = {"periods": ["Mar 2026", "Jun 2026", "TTM"],
             "rows": {"Revenue": [10.0, 12.0, 40.0], "Financing Profit": [3.0, 4.0, 13.0],
                      "Financing Margin %": [30.0, 33.0, 32.0], "Other Income": [1.0, None, 3.0]}}
    rows = rr.quarter_rows(7, "BANK", table)
    assert [r["period_end"] for r in rows] == [dt.date(2026, 3, 31), dt.date(2026, 6, 30)]
    assert rows[1]["layout"] == "lender" and rows[1]["sales"] == 12.0 and rows[1]["opm_pct"] == 33.0
    assert "other_income" not in rows[1]


def test_profit_growth_needs_a_real_year_ago_base():
    q = _quarters()
    # year-ago quarter (index 8): a near-zero profit; latest a normal one
    q.loc[8, "pbt"] = q.loc[8, "other_income"] - 3.0 + 0.5   # underlying ~0.5 cr on ~sales 127
    r = rr.read_company(q, AS_OF)
    assert pd.isna(r["profit_yoy_pct"]), "growth off a 0.5 cr base must not be read"
    assert not pd.isna(rr.read_company(_quarters(), AS_OF)["sales_yoy_pct"])


def test_blank_period_labels_are_skipped_not_stored_as_nat():
    assert rr.period_end(None) is None and rr.period_end("") is None and rr.period_end("TTM") is None
    rows = rr.quarter_rows(1, "X", {"periods": ["", "Jun 2026"], "rows": {"Sales": [1.0, 2.0]}})
    assert [r["period_end"] for r in rows] == [dt.date(2026, 6, 30)]


def test_trend_window_is_by_date_when_a_quarter_is_missing():
    q = _quarters()
    q = q.drop(index=10).reset_index(drop=True)   # one quarter missing inside the last year
    r = rr.read_company(q, AS_OF)
    assert abs(r["sales_vs_trend_pp"]) < 1e-6   # steady 10% growth still reads steady
