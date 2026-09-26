import pandas as pd

from fundamentals.screens import universe


def _row(symbol, *, mcap=500e7, value=80e5, value_sessions=63, listed=400, category="Listed",
         gsm=100, lt_asm=100, st_asm=100, encumbered=100, surveillance_date="2026-09-24"):
    return {
        "symbol": symbol, "isin": f"INE{symbol}", "company_master_id": f"nse:{symbol}",
        "session_date": pd.Timestamp("2026-09-24", tz="UTC"), "category": category,
        "market_cap_rs": mcap, "mcap_date": None, "median_value_rs": value,
        "value_sessions": value_sessions, "listed_sessions": listed,
        "surveillance_date": surveillance_date, "gsm": gsm, "lt_asm": lt_asm, "st_asm": st_asm,
        "esm": 100, "irp": 100, "encumbered_over_50": encumbered,
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
    assert report["pass_layer1"] == 2
    assert report["pass_by_category"] == {"Listed": 1, "Permitted": 1}
    assert report["fails"] == {"rule2_surveillance": 0, "rule3_mcap": 1, "rule4_traded_value": 0, "rule5_listed": 2}
    assert report["fails_only_this_rule"] == {"rule2_surveillance": 0, "rule3_mcap": 0, "rule4_traded_value": 0,
                                              "rule5_listed": 1}


def test_layer1_query_bounds_every_ohlcv_read():
    # Hypertable gotcha: every nseindia_ohlcv / nseindia_mcap read must carry a date bound.
    sql = universe.LAYER1_INPUTS_QUERY
    reads = (sql.count("FROM nseindia_ohlcv") + sql.count("FROM nseindia_mcap")
             + sql.count("FROM nseindia_surveillance_indicator"))
    assert reads == 6
    assert sql.count("date >= now() - interval") + sql.count("date >= (now() - interval") == reads


def test_rule2_excludes_gsm_any_stage_and_asm_stage_2_up_but_keeps_asm_stage_1():
    # ASM stage 1 is NSE's mechanical reaction to a price run; the operator keeps it in (2026-09-25).
    df = universe.apply_layer1_rules(pd.DataFrame([
        _row("ASM1", lt_asm=1, st_asm=1),
        _row("LTASM2", lt_asm=2),
        _row("STASM2", st_asm=2),
        _row("GSM0", gsm=0),
        _row("NOROW", gsm=None, lt_asm=None, st_asm=None, surveillance_date=None),
        _row("ENCUMBERED", encumbered=0),
    ])).set_index("symbol")

    assert df["layer1_pass"].to_dict() == {
        "ASM1": True, "LTASM2": False, "STASM2": False, "GSM0": False, "NOROW": True, "ENCUMBERED": True,
    }
    assert df.at["LTASM2", "fail_reasons"] == ["on surveillance: LT_ASM stage 2"]
    assert df.at["GSM0", "fail_reasons"] == ["on surveillance: GSM stage 0"]

    report = universe.layer1_report(df.reset_index())
    assert report["passing_but_asm_stage1"] == 1
    assert report["passing_but_encumbered_over_50"] == 1
    assert report["no_surveillance_row"] == 1


def test_assign_groups_uses_labels_and_rbi_register():
    df = pd.DataFrame([
        {"symbol": s, "isin": f"INE{s}", "company_master_id": f"nse:{s}"}
        for s in ["BANK", "NBFC", "HOLDCO", "CICNBFC", "AMC", "OTHERLENDER", "REALTY", "FACTORY", "NOLABEL"]
    ])
    fs = ("Financial Services", "Financial Services")
    labels = pd.DataFrame([
        {"isin": "INEBANK", "macro_sector": fs[0], "sector": fs[1], "industry": "Banks", "basic_industry": "Private Sector Bank"},
        {"isin": "INENBFC", "macro_sector": fs[0], "sector": fs[1], "industry": "Finance",
         "basic_industry": "Non Banking Financial Company (NBFC)"},
        {"isin": "INEHOLDCO", "macro_sector": fs[0], "sector": fs[1], "industry": "Finance", "basic_industry": "Holding Company"},
        {"isin": "INECICNBFC", "macro_sector": fs[0], "sector": fs[1], "industry": "Finance",
         "basic_industry": "Non Banking Financial Company (NBFC)"},
        {"isin": "INEAMC", "macro_sector": fs[0], "sector": fs[1], "industry": "Capital Markets",
         "basic_industry": "Asset Management Company"},
        {"isin": "INEOTHERLENDER", "macro_sector": fs[0], "sector": fs[1], "industry": "Finance",
         "basic_industry": "Other Financial Services"},
        {"isin": "INEREALTY", "macro_sector": "Realty", "sector": "Realty", "industry": "Realty",
         "basic_industry": "Residential, Commercial Projects"},
        {"isin": "INEFACTORY", "macro_sector": "Commodities", "sector": "Construction Materials", "industry": "Cement",
         "basic_industry": "Cement & Cement Products"},
    ])
    rbi = pd.Series({"nse:CICNBFC": "CIC", "nse:OTHERLENDER": "HFC", "nse:NBFC": "ICC"}, name="rbi_classification")

    out = universe.assign_groups(df, labels, rbi).set_index("symbol")["group"].to_dict()

    assert out == {
        "BANK": "lender", "NBFC": "lender", "HOLDCO": "realty_holding",
        "CICNBFC": "realty_holding",  # RBI says core investment company: a holding company whatever the label
        "AMC": "other_financial", "OTHERLENDER": "lender", "REALTY": "realty_holding",
        "FACTORY": "operating", "NOLABEL": "unlabelled",
    }


def test_normalise_company_name_matches_rbi_and_exchange_spellings():
    n = universe.normalise_company_name
    assert n("JIO Financial Services Ltd.") == n("Jio Financial Services Limited (Formerly: Reliance Strategic Investments Limited)")
    assert n("Cholamandalam Financial Holdings Ltd.") == n("Cholamandalam Financial Holdings Limited")
    assert n("Power Finance Corporation Ltd.") == n("Power Finance Corporation Limited")


def test_l1_loader_keeps_tracked_names_that_left_the_universe(monkeypatch):
    from fundamentals.screens import l1_universe

    latest = pd.DataFrame([{"company_id": 1, "company_name": "In", "ticker": "IN"}])
    tracked = pd.DataFrame([{"company_master_id": "bse:500001"}, {"company_master_id": "nse:IN"}])
    history = pd.DataFrame([{"company_id": 1, "company_name": "In", "ticker": "IN"},
                            {"company_id": 2, "company_name": "Held BSE-only", "ticker": "500001"},
                            {"company_id": 3, "company_name": "Gone, untracked", "ticker": "GONE"}])
    results = iter([latest, tracked, history])
    monkeypatch.setattr(l1_universe, "sql_to_df", lambda *a, **k: next(results).copy())
    monkeypatch.setattr(l1_universe, "map_company_master_ids_nse_or_bse",
                        lambda s: s.map({"500001": "bse:500001", "GONE": "nse:GONE"}))

    out = l1_universe.load_l1_universe_tickers()

    assert out["company_id"].tolist() == [1, 2]
