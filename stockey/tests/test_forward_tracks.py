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
