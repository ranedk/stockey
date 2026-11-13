"""serving/financials.py: TTM sums, cash flow from halves, owner-profit plausibility, mis-scaled
balance sheets, and that per-row ratios stay on their own row after the internal re-sort."""
import pandas as pd
import pytest

from serving import financials as F


def _row(sym, item, kind, start, end, value, pub, basis="consolidated", family="company"):
    return {"symbol": sym, "basis": basis, "family": family, "item": item, "kind": kind,
            "period_start": pd.Timestamp(start) if start else pd.NaT, "period_end": pd.Timestamp(end),
            "value": value, "public_at": pd.Timestamp(pub)}


QUARTERS = [("2025-04-01", "2025-06-30"), ("2025-07-01", "2025-09-30"),
            ("2025-10-01", "2025-12-31"), ("2026-01-01", "2026-03-31")]


def _company(sym="AAA", sales=(100, 110, 120, 130), pat=(10, 11, 12, 13), owners=None, scale_bs=1.0):
    rows = []
    for i, (s, e) in enumerate(QUARTERS):
        pub = pd.Timestamp(e) + pd.Timedelta(days=40)
        rows += [_row(sym, "revenue", "q", s, e, sales[i], pub), _row(sym, "revenue_net", "q", s, e, sales[i], pub),
                 _row(sym, "pat", "q", s, e, pat[i], pub), _row(sym, "pbt", "q", s, e, pat[i] * 1.3, pub)]
        if owners is not None:
            rows.append(_row(sym, "pat_owners", "q", s, e, owners[i], pub))
    # balance sheets at Sep and Mar, cash flow h1 and fy
    rows += [_row(sym, "total_equity", "i", None, "2025-09-30", 1000 * scale_bs, "2025-11-09"),
             _row(sym, "total_assets", "i", None, "2025-09-30", 2000 * scale_bs, "2025-11-09"),
             _row(sym, "total_equity", "i", None, "2026-03-31", 1100, "2026-05-10"),
             _row(sym, "total_assets", "i", None, "2026-03-31", 2100, "2026-05-10"),
             _row(sym, "total_equity", "i", None, "2025-03-31", 950, "2025-05-10"),
             _row(sym, "total_assets", "i", None, "2025-03-31", 1950, "2025-05-10"),
             _row(sym, "cfo", "fy", "2025-04-01", "2026-03-31", 60, "2026-05-10"),
             _row(sym, "cfo", "h1", "2025-04-01", "2025-09-30", 25, "2025-11-09")]
    return rows


def _ttm(df, sym, end):
    r = df[(df.symbol == sym) & (df.period == "ttm") & (df.period_end == pd.Timestamp(end))]
    return r.iloc[0] if len(r) else None


def test_ttm_sums_four_consecutive_quarters_and_takes_latest_public_time():
    df = F.build(pd.DataFrame(_company()))
    t = _ttm(df, "AAA", "2026-03-31")
    assert t["sales"] == 460 and t["profit"] == 46
    assert t["public_at"] == pd.Timestamp("2026-03-31") + pd.Timedelta(days=40)
    assert _ttm(df, "AAA", "2025-12-31") is None          # only three quarters before it


def test_ttm_needs_consecutive_quarters():
    rows = [r for r in _company() if not (r["kind"] == "q" and r["period_end"] == pd.Timestamp("2025-09-30"))]
    rows += [dict(r, period_end=r["period_end"] - pd.DateOffset(years=1)) for r in _company()
             if r["kind"] == "q" and r["period_end"] == pd.Timestamp("2025-06-30")]
    df = F.build(pd.DataFrame(rows))
    assert _ttm(df, "AAA", "2026-03-31") is None          # a gap in the four quarters


def test_owner_profit_used_only_when_plausible():
    df = F.build(pd.DataFrame(_company(owners=(9, 0, 0.01, 12))))
    q = df[(df.period == "q")].set_index("period_end")["profit"]
    assert q[pd.Timestamp("2025-06-30")] == 9             # within half to one and a half of pat
    assert q[pd.Timestamp("2025-09-30")] == 11            # filed as 0: fall back to pat
    assert q[pd.Timestamp("2025-12-31")] == 12            # tiny mis-tagged value: fall back to pat


def test_misscaled_balance_sheet_is_blanked_not_rescaled():
    F.SCALE_SUSPECT.clear()
    df = F.build(pd.DataFrame(_company(scale_bs=0.01)))
    t = _ttm(df, "AAA", "2025-09-30")
    assert t is None or pd.isna(t["total_equity"])
    assert ("AAA", "consolidated", pd.Timestamp("2025-09-30")) in F.SCALE_SUSPECT


def test_cash_flow_ttm_at_year_end_is_the_year():
    df = F.build(pd.DataFrame(_company()))
    assert _ttm(df, "AAA", "2026-03-31")["cfo"] == 60


def test_ratios_stay_on_their_own_company_after_resort():
    # two companies interleaved so the internal sort reorders rows; ROE must use each one's equity
    rows = _company("ZZZ", pat=(100, 100, 100, 100)) + _company("AAA", pat=(1, 1, 1, 1))
    df = F.build(pd.DataFrame(rows))
    a, z = _ttm(df, "AAA", "2026-03-31"), _ttm(df, "ZZZ", "2026-03-31")
    assert a["roe"] == pytest.approx(4 / 1100)       # no TTM row a year earlier: current equity only
    assert z["roe"] == pytest.approx(400 / 1100)
