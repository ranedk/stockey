from types import SimpleNamespace

import pandas as pd

from fundamentals.screens import portfolio_exit as px
from fundamentals.screens import portfolio_ruleset as pr


def _pos(opened="2026-10-01"):
    return SimpleNamespace(opened_at=pd.Timestamp(opened, tz="UTC"), ruleset_version=2)


def _run(**axes):
    base = {"fundamentals_trajectory": True, "event_corroboration": True, "sector_cycle": None,
            "ownership": None, "valuation": True}
    base.update(axes)
    return {"run_date": "x", "score_version": 4, "axes": base}


TODAY_EARLY = pd.Timestamp("2026-10-03")   # 2 sessions after entry
TODAY_LATE = pd.Timestamp("2026-11-05")    # > 20 sessions after entry


def test_hard_contradiction_after_entry_exits_immediately():
    ev = [{"trigger_type": "results_decline", "load_ts": pd.Timestamp("2026-10-02", tz="UTC"), "alert_date": "2026-10-02"}]
    reason = px.v2_thesis_exit(_pos(), hard_events=ev, recent=[], stage_now=2, today=TODAY_EARLY)
    assert reason and "results_decline" in reason


def test_hard_contradiction_from_before_entry_is_ignored():
    ev = [{"trigger_type": "pledge_increase", "load_ts": pd.Timestamp("2026-09-20", tz="UTC"), "alert_date": "2026-09-20"}]
    assert px.v2_thesis_exit(_pos(), hard_events=ev, recent=[], stage_now=2, today=TODAY_LATE) is None


def test_soft_contradiction_needs_three_runs_and_the_minimum_hold():
    three = [_run(ownership=False)] * 3
    assert px.v2_thesis_exit(_pos(), hard_events=[], recent=three, stage_now=2, today=TODAY_EARLY) is None  # too early
    reason = px.v2_thesis_exit(_pos(), hard_events=[], recent=three, stage_now=2, today=TODAY_LATE)
    assert reason and "ownership" in reason
    flicker = [_run(ownership=False), _run(), _run(ownership=False)]
    assert px.v2_thesis_exit(_pos(), hard_events=[], recent=flicker, stage_now=2, today=TODAY_LATE) is None


def test_valuation_is_never_an_exit_reason():
    cheap_then_dear = [_run(valuation=False)] * 3
    assert px.v2_thesis_exit(_pos(), hard_events=[], recent=cheap_then_dear, stage_now=2, today=TODAY_LATE) is None


def test_trend_exit_only_on_stage_four_after_the_minimum_hold():
    assert px.v2_thesis_exit(_pos(), hard_events=[], recent=[], stage_now=3, today=TODAY_LATE) is None
    assert px.v2_thesis_exit(_pos(), hard_events=[], recent=[], stage_now=4, today=TODAY_EARLY) is None
    assert "stage 4" in px.v2_thesis_exit(_pos(), hard_events=[], recent=[], stage_now=4, today=TODAY_LATE)


def test_v2_entry_bar_and_wider_stop(monkeypatch):
    assert pr.V2_RULESET_VERSION == 2 and pr.ENTRY_MIN_SUPPORTING == 2 and pr.RULESET_VERSION == 3
    monkeypatch.setattr(pr, "sql_to_df", lambda *a, **k: pd.DataFrame([{"symbol": "CALM", "daily_vol": 0.005},
                                                                         {"symbol": "WILD", "daily_vol": 0.06}]))
    stops = pr.compute_stop_pct(["CALM", "WILD", "NEW"])
    assert stops["CALM"]["stop_pct"] == 10.0     # 2.5 x 1.58% = 4% -> floor 10
    assert stops["WILD"]["stop_pct"] == 25.0     # 2.5 x 19% = 47% -> cap 25
    assert stops["NEW"]["stop_pct"] == 17.5      # no history -> band midpoint


def test_v2_entry_needs_two_supporting_axes(monkeypatch):
    rows = pd.DataFrame([
        {"company_master_id": f"nse:{t}", "narrative_text": "", "first_seen_price": 1.0, "first_seen_at": None,
         "confluence_count": n, "contradicting_count": 0, "evaluable_count": n,
         "axis_fundamentals_trajectory": True, "axis_event_corroboration": n >= 2, "axis_sector_cycle": None,
         "axis_ownership": None, "axis_valuation": None,
         "scored_on": pd.Timestamp.now(tz="UTC"), "score_version": 4}
        for t, n in (("ONE", 1), ("TWO", 2))
    ])
    monkeypatch.setattr(pr, "sql_to_df", lambda *a, **k: rows)
    monkeypatch.setattr("fundamentals.screens.confluence_score._ensure_confluence_score_table", lambda: None)
    monkeypatch.setattr(pr, "load_stage_reads", lambda: {"ONE": 2, "TWO": 2})
    monkeypatch.setattr(pr, "load_stage_keys", lambda ids: {i: [i.replace("nse:", "")] for i in ids})
    monkeypatch.setattr(pr, "compute_stop_pct", lambda syms: {s: {"stop_pct": 15.0, "basis": "t"} for s in syms})
    monkeypatch.setattr(pr, "numeric_l2_metrics", lambda: [])
    out = pr.evaluate_entry_candidates_v2()
    assert [c["ticker"] for c in out["candidates"]] == ["TWO"]
