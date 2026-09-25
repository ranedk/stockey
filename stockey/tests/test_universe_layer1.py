import pandas as pd

from fundamentals.screens import universe


def _row(symbol, *, mcap=500e7, value=80e5, value_sessions=63, listed=400, category="Listed"):
    return {
        "symbol": symbol, "isin": f"INE{symbol}", "company_master_id": f"nse:{symbol}",
        "session_date": pd.Timestamp("2026-09-24", tz="UTC"), "category": category,
        "market_cap_rs": mcap, "mcap_date": None, "median_value_rs": value,
        "value_sessions": value_sessions, "listed_sessions": listed,
    }


def test_layer1_rules_pass_and_name_every_failed_rule():
    inputs = pd.DataFrame([
        _row("GOOD"),
        _row("ATFLOORS", mcap=300e7, value=50e5, value_sessions=40, listed=252),
        _row("SMALL", mcap=299e7),
        _row("ETF", mcap=None, category=None),
        _row("THIN", value=49e5),
        _row("GAPPY", value=90e5, value_sessions=39),
        _row("NEW", listed=251),
        _row("ALLBAD", mcap=100e7, value=1e5, listed=10),
    ])

    df = universe.apply_layer1_rules(inputs).set_index("symbol")

    assert df["layer1_pass"].to_dict() == {
        "GOOD": True, "ATFLOORS": True, "SMALL": False, "ETF": False,
        "THIN": False, "GAPPY": False, "NEW": False, "ALLBAD": False,
    }
    assert df.at["SMALL", "fail_reasons"] == ["market cap < Rs 300 cr"]
    assert df.at["ETF", "fail_reasons"] == ["no market cap"]
    assert df.at["THIN", "fail_reasons"] == ["traded value < Rs 50 L"]
    assert df.at["GAPPY", "fail_reasons"] == ["traded on < 40 of 63 sessions"]
    assert df.at["NEW", "fail_reasons"] == ["listed < 252 sessions"]
    assert len(df.at["ALLBAD", "fail_reasons"]) == 3


def test_layer1_report_counts():
    df = universe.apply_layer1_rules(pd.DataFrame([
        _row("A"), _row("B", category="Permitted"), _row("C", listed=5), _row("D", mcap=1e7, listed=5),
    ]))

    report = universe.layer1_report(df)

    assert report["session_date"] == "2026-09-24"
    assert report["pass_rules_1_3_4_5"] == 2
    assert report["pass_by_category"] == {"Listed": 1, "Permitted": 1}
    assert report["fails"] == {"rule3_mcap": 1, "rule4_traded_value": 0, "rule5_listed": 2}
    assert report["fails_only_this_rule"] == {"rule3_mcap": 0, "rule4_traded_value": 0, "rule5_listed": 1}


def test_layer1_query_bounds_every_ohlcv_read():
    # Hypertable gotcha: every nseindia_ohlcv / nseindia_mcap read must carry a date bound.
    sql = universe.LAYER1_INPUTS_QUERY
    reads = sql.count("FROM nseindia_ohlcv") + sql.count("FROM nseindia_mcap")
    assert reads == 5
    assert sql.count("date >= now() - interval") == reads
