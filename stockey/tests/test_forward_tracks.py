import pandas as pd

from fundamentals.screens import forward_tracks as ft


def _companies(n):
    return pd.DataFrame({
        "company_master_id": [f"nse:C{i}" for i in range(n)],
        "profit_y1": 10.0, "profit_y2": 10.0, "fcf_3y_total": 5.0,
        "roce_5y_avg_pct": range(n), "roe_5y_avg_pct": range(n), "debt_to_equity": [0.1] * n,
        "earnings_yield_pct": [float(n - i) for i in range(n)], "ev_to_ebit": [float(i + 1) for i in range(n)],
        "as_of_date": pd.Timestamp("2026-09-27").date(),
    })


def test_quality_excludes_loss_and_cash_burn_and_ranks_higher_returns_first():
    df = _companies(5)
    df.loc[0, "profit_y2"] = -1.0          # lost money two years ago
    df.loc[1, "fcf_3y_total"] = -3.0       # burned cash
    q = ft.quality_eligible(df).sort_values("score", ascending=False)
    assert set(q["company_master_id"]) == {"nse:C2", "nse:C3", "nse:C4"}
    assert q["company_master_id"].iloc[0] == "nse:C4"


def test_value_draws_only_from_the_better_quality_half_and_needs_positive_ev_ebit():
    df = _companies(10)
    v = ft.value_eligible(df)
    q = ft.quality_eligible(df)
    assert set(v["company_master_id"]) <= set(q[q["score"] >= q["score"].median()]["company_master_id"])
    df2 = df.copy(); df2.loc[9, "ev_to_ebit"] = -4.0
    assert "nse:C9" not in set(ft.value_eligible(df2)["company_master_id"])


def test_select_keeps_held_names_inside_the_buffer_and_fills_to_top_n():
    ranked = pd.DataFrame({"company_master_id": [f"nse:C{i}" for i in range(100)], "score": [100 - i for i in range(100)]})
    held = {"nse:C5", "nse:C45", "nse:C80"}   # ranks 6, 46, 81
    out = ft.select_holdings(ranked, held, top_n=30, keep_within=60)
    ids = set(out["company_master_id"])
    assert "nse:C45" in ids and "nse:C80" not in ids and "nse:C5" in ids
    assert len(out) == 30 and abs(out["weight"].sum() - 1) < 1e-9
    assert set(out.loc[out["action"] == "hold", "company_master_id"]) == {"nse:C5", "nse:C45"}


def test_rebalance_is_monthly():
    d = pd.Timestamp
    assert ft.is_due(None, d("2026-10-01").date())
    assert not ft.is_due(d("2026-10-01").date(), d("2026-10-20").date())
    assert ft.is_due(d("2026-10-01").date(), d("2026-11-02").date())


def test_a_frozen_spec_cannot_be_edited(monkeypatch):
    import json
    stored = pd.DataFrame([{"track": "quality", "version": 1, "spec_json": json.dumps({"changed": True})}])
    monkeypatch.setattr(ft, "sql_to_df", lambda *a, **k: stored)
    try:
        ft.freeze_specs()
    except RuntimeError as exc:
        assert "new version" in str(exc)
    else:
        raise AssertionError("an edited frozen spec must fail loudly")


# --- paradigm tracks (2026-10-23) ---------------------------------------------------------

def test_cadences_count_whole_months_since_the_last_rebalance():
    d = pd.Timestamp
    assert ft.is_due(None, d("2026-10-26").date(), "annual")
    assert not ft.is_due(d("2026-10-26").date(), d("2027-09-30").date(), "annual")
    assert ft.is_due(d("2026-10-26").date(), d("2027-10-01").date(), "annual")
    assert not ft.is_due(d("2026-10-26").date(), d("2026-12-31").date(), "quarterly")
    assert ft.is_due(d("2026-10-26").date(), d("2027-01-04").date(), "quarterly")


def test_every_paradigm_spec_states_its_paradigm_and_what_was_approximated():
    for t in ft.PARADIGM_TRACKS:
        s = ft.TRACKS[t]
        assert s["start"] == "2026-10-26" and s["version"] == 1 and s["rebalance"] in ft.CADENCE_MONTHS
        assert len(s["paradigm"]) > 80 and len(s["approximations"]) > 80
    assert "CRUDE STAND-IN" in ft.TRACKS["kedia"]["approximations"]
    assert "CRUDE STAND-IN" in ft.TRACKS["whiteoak"]["approximations"]


def _quarters(start, values):
    idx = [(pd.Timestamp(start) + pd.offsets.QuarterEnd(k)).date() for k in range(len(values))]
    return pd.Series(values, index=idx)


def test_fiscal_year_growth_needs_eight_consecutive_quarters_and_reads_march_year_ends():
    s = _quarters("2020-06-30", [100.0] * 4 + [110.0] * 4 + [121.0] * 4)   # Jun-2020 .. Mar-2023
    g = ft.fiscal_year_growth(s)
    assert set(g) == {pd.Timestamp("2022-03-31").date(), pd.Timestamp("2023-03-31").date()}
    assert abs(g[pd.Timestamp("2022-03-31").date()] - 0.10) < 1e-9
    gap = s.drop(pd.Timestamp("2021-09-30").date())
    assert pd.Timestamp("2022-03-31").date() not in ft.fiscal_year_growth(gap)


def test_consistent_growth_wants_five_straight_years_of_ten_percent_and_recent_data():
    years = {pd.Timestamp(f"{y}-03-31").date(): 0.15 for y in range(2022, 2027)}
    assert ft.consistent_growth(years, "2026-10-26")
    assert not ft.consistent_growth({**years, pd.Timestamp("2024-03-31").date(): 0.09}, "2026-10-26")
    assert not ft.consistent_growth({k: v for k, v in years.items() if k.year != 2023}, "2026-10-26")
    old = {pd.Timestamp(f"{y}-03-31").date(): 0.15 for y in range(2019, 2024)}
    assert not ft.consistent_growth(old, "2026-10-26")


def test_sageone_caps_an_industry_at_five_names():
    ranked = pd.DataFrame({"company_master_id": [f"nse:C{i}" for i in range(30)], "score": [30 - i for i in range(30)],
                           "industry": ["A"] * 10 + ["B"] * 20})
    out = ft.select_capped(ranked, set(), top_n=20, keep_within=40, max_per_group=5, group_col="industry")
    assert len(out) == 10 and (out["industry"] == "A").sum() == 5 and (out["industry"] == "B").sum() == 5


def test_sector_quotas_are_proportional_at_least_one_and_sum_to_the_book():
    q = ft.sector_quotas(pd.Series({"big": 300, "mid": 90, "tiny": 3, "one": 1}), 80)
    assert sum(q.values()) == 80 and q["tiny"] >= 1 and q["one"] == 1 and q["big"] > q["mid"] > q["tiny"]


def test_maheshwari_buys_leaders_first_keeps_within_the_wider_band_and_caps_each_industry():
    ranked = pd.DataFrame({
        "company_master_id": [f"nse:{c}" for c in ["A1", "A2", "A3", "A4", "A5", "B1", "B2", "C1", "Z1"]],
        "industry": ["A"] * 5 + ["B"] * 2 + ["C", "Z"], "ind_rank": [1] * 5 + [2, 2, 3, 7],
        "score": [500, 400, 300, 200, 100, 900, 50, 10, 5]})
    spec = {**ft.TRACKS["maheshwari"], "top_n": 6}
    out = ft.select_maheshwari(ranked, {"nse:Z1"}, spec)
    ids = list(out["company_master_id"])
    assert "nse:Z1" in ids                                   # held, industry still within the top 8
    assert {"nse:A1", "nse:B1", "nse:C1"} <= set(ids)        # each industry's leader comes first
    assert len(ids) == 6 and "nse:A5" not in ids


def test_kedia_keeps_a_holding_unless_it_lost_money_two_years_running():
    inputs = pd.DataFrame({"company_master_id": ["nse:H1", "nse:H2", "nse:N1", "nse:N2"],
                           "profit_y1": [-1.0, -1.0, 5.0, 5.0], "profit_y2": [3.0, -2.0, 5.0, 5.0],
                           "as_of_date": pd.Timestamp("2026-10-26").date()})
    ranked = inputs[inputs["company_master_id"].str.startswith("nse:N")].assign(score=[0.9, 0.8])
    out = ft.select_kedia(ranked, {"nse:H1", "nse:H2"}, {**ft.TRACKS["kedia"], "top_n": 2}, inputs)
    assert list(out["company_master_id"]) == ["nse:H1", "nse:N1"]
    assert list(out["action"]) == ["hold", "enter"]


def test_drifted_weights_grow_with_price_and_keep_cash_as_cash():
    prev = pd.DataFrame({"company_master_id": ["nse:A", "nse:B"], "symbol": ["A", "B"],
                         "weight": [0.45, 0.45], "ref_price": [100.0, 100.0]})   # 10% cash
    w = ft.drifted_weights(prev, {"A": (200.0, None), "B": (100.0, None)})
    assert abs(w["nse:A"] - 0.9 / 1.45) < 1e-9 and abs(w["nse:B"] - 0.45 / 1.45) < 1e-9


def test_period_return_is_the_equal_weight_mean_when_fully_invested_and_counts_cash_at_zero(monkeypatch):
    monkeypatch.setattr(ft, "_ref_prices", lambda syms, d: {"A": (110.0, d), "B": (90.0, d), "C": (130.0, d)})
    full = pd.DataFrame({"symbol": ["A", "B", "C"], "weight": [1 / 3] * 3, "ref_price": [100.0] * 3})
    r, n = ft._period_return(full, "2026-11-01")
    assert n == 3 and abs(r - 0.10) < 1e-9
    half = pd.DataFrame({"symbol": ["A"], "weight": [0.5], "ref_price": [100.0]})
    assert abs(ft._period_return(half, "2026-11-01")[0] - 0.05) < 1e-9
