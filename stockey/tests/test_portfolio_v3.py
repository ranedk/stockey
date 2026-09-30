import datetime as dt
from types import SimpleNamespace

import pandas as pd
import pytest

from fundamentals.screens import portfolio_v3 as v3

TODAY = pd.Timestamp("2026-12-01")


def _b(cid, score, vol=2.0, sector="S1"):
    return {"company_master_id": cid, "story_score": score, "daily_vol_pct": vol, "sector_code": sector}


def test_weights_tilt_to_conviction_and_calm_within_caps():
    book = [_b(f"c{i}", 60 + i, vol=2.0, sector=f"S{i % 6}") for i in range(20)]
    book.append(_b("calm_strong", 95, vol=1.0, sector="S9"))
    book.append(_b("wild_weak", 50, vol=6.0, sector="S8"))
    w = v3.target_weights(book, 25)
    assert w["calm_strong"] == v3.WEIGHT_MAX and w["wild_weak"] == v3.WEIGHT_MIN
    assert all(v3.WEIGHT_MIN - 1e-9 <= x <= v3.WEIGHT_MAX + 1e-9 for x in w.values())
    assert sum(w.values()) <= 1.0 + 1e-9


def test_sector_cap_and_top_n():
    book = [_b(f"a{i}", 90, vol=1.0, sector="BANKS") for i in range(10)] + [_b("x", 70, sector="IT")]
    w = v3.target_weights(book, 25)
    assert abs(sum(w[f"a{i}"] for i in range(10)) - v3.SECTOR_MAX) < 1e-4
    assert set(v3.target_weights(book, 5)) == {f"a{i}" for i in range(5)}


def test_trailing_stop_ratchets_and_locks_breakeven_after_twice_the_stop():
    lv = v3.stop_levels(100.0, 15.0, None, 2.0)
    assert lv["effective"] == pytest.approx(85.0) and lv["binding"] == "stop_loss"
    lv = v3.stop_levels(100.0, 15.0, 120.0, 2.0)          # trail ~26.8%: 87.8 > initial 85
    assert lv["binding"] == "trailing_stop" and lv["effective"] == pytest.approx(120 * (1 - v3.trail_pct(2.0) / 100))
    lv = v3.stop_levels(100.0, 15.0, 131.0, 3.0)          # +31% >= 2 x 15%: never below entry
    assert lv["effective"] >= 100.0


def _pos(**kw):
    base = dict(entry_price=100.0, stop_pct=15.0, opened_at=pd.Timestamp("2026-06-01", tz="UTC"), daily_vol_pct=2.0,
                story_score_at_entry=85.0, trim_count=0, target_date=None)
    base.update(kw)
    return SimpleNamespace(**base)


def _act(p=None, **kw):
    args = dict(price=110.0, trail_high=112.0, live={"story_score": 85.0, "flaws": None}, hard_events=[],
                recent_scores=[85.0, 84.0, 86.0], valuation_ratio=1.2, today=TODAY)
    args.update(kw)
    return v3.v3_exit_action(p or _pos(), **args)


def test_exit_order_stop_then_thesis_then_fading():
    assert _act(price=80.0)["reason"] == "stop_loss"
    assert _act(live={"story_score": 85, "flaws": "cash burn with losses"})["reason"] == "thesis_broken"
    ev = [{"load_ts": pd.Timestamp("2026-07-01", tz="UTC"), "trigger_type": "rating_downgrade", "alert_date": "2026-07-01"}]
    assert _act(hard_events=ev)["reason"] == "thesis_broken"
    assert _act(recent_scores=[60.0, 50.0, 62.0])["reason"] == "story_fading"
    assert _act(recent_scores=[60.0, 70.0, 62.0]) is None
    assert _act() is None


def test_valuation_trims_once_and_only_when_the_story_is_not_strengthening():
    assert _act(valuation_ratio=2.3)["action"] == "trim"
    assert _act(valuation_ratio=2.3, live={"story_score": 92.0, "flaws": None}) is None
    assert _act(p=_pos(trim_count=1), valuation_ratio=2.3) is None


def test_tax_guard_holds_non_urgent_exits_near_the_anniversary_but_never_stops():
    near = _pos(opened_at=pd.Timestamp("2025-12-20", tz="UTC"))  # 346 days held on TODAY
    assert _act(p=near, recent_scores=[60.0, 50.0, 62.0])["action"] == "wait"
    assert _act(p=near, price=80.0)["action"] == "exit"
    assert _act(p=near, price=95.0, trail_high=100.0, recent_scores=[60.0, 50.0, 62.0])["action"] == "exit"  # at a loss
    assert v3.tax_guard_holds(pd.Timestamp("2025-06-01", tz="UTC"), 100, 150, TODAY) is False  # already long-term


def test_replacement_needs_a_clear_margin_and_a_seasoned_holding():
    book = pd.DataFrame([
        {"position_id": "p1", "company_master_id": "a", "ticker": "A", "opened_at": pd.Timestamp("2026-10-01", tz="UTC"),
         "entry_price": 100.0, "story_score": 70.0},
        {"position_id": "p2", "company_master_id": "b", "ticker": "B", "opened_at": pd.Timestamp("2026-11-25", tz="UTC"),
         "entry_price": 100.0, "story_score": 60.0},
    ])
    assert v3.weakest_replaceable(book, 80.0, TODAY) is None                   # 80 < 70 + 15; B too young
    assert v3.weakest_replaceable(book, 90.0, TODAY)["position_id"] == "p1"    # skips young B, replaces A


def test_runner_v3_sizes_by_weight_and_replaces_when_full(monkeypatch):
    import fundamentals.screens.portfolio_runner as runner
    import fundamentals.screens.l5_sizing as sizing

    cand = {"company_master_id": "nse:NEW", "ticker": "NEW", "stage": 2, "story_score": 95.0, "primary_dimension": "growth",
            "sector_code": "S2", "daily_vol_pct": 1.5, "story_read": None, "narrative_text": None, "score_version": 2,
            "confluence_count": None, "contradicting_count": None, "evaluable_count": None, "axes": None, "stop": {"stop_pct": 12.0}}
    held = pd.DataFrame([{"position_id": f"p{i}", "company_master_id": f"nse:H{i}", "ticker": f"H{i}",
                          "opened_at": pd.Timestamp("2026-06-01", tz="UTC"), "entry_price": 100.0, "position_size_rs": 4e5,
                          "target_weight": 0.04, "sector_code": "S1", "daily_vol_pct": 2.0, "story_score_at_entry": 85.0,
                          "story_score": 70.0 + i} for i in range(25)])
    monkeypatch.setattr(runner, "_ensure_tables", lambda: None)
    monkeypatch.setattr(runner, "_ensure_cohort_columns", lambda: None)
    monkeypatch.setattr(runner, "_catchup_signal_dates", lambda: {})
    monkeypatch.setattr(runner, "_recently_stopped_tickers", lambda: {})
    monkeypatch.setattr(runner, "_open_accepted_tickers", lambda: {f"H{i}" for i in range(25)})
    monkeypatch.setattr(runner, "_open_vetoed_tickers", lambda: {})
    monkeypatch.setattr(runner, "_latest_price", lambda t: (100.0, None))
    monkeypatch.setattr(runner, "evaluate_entry_candidates", lambda: {"ruleset_version": 3, "stage_api_available": True,
                                                                       "evaluated": 1, "candidates": [cand]})
    monkeypatch.setattr(runner, "adjudicate_entry", lambda c: {"decision": "accept", "reason": "ok", "model": "m"})
    monkeypatch.setattr(v3, "open_v3_book", lambda: held)
    monkeypatch.setattr(sizing, "load_adv_inputs", lambda cmid: None)
    out = runner.run_portfolio(live=False, dry_run=True)
    assert out["ruleset_version"] == 3 and out["entered_tickers"] == ["NEW"]
    assert out["replaced"][0]["ticker"] == "H0"                    # the weakest (70), 95 >= 70 + 15
    assert "nse:H0" not in out["target_weights"] and out["target_weights"]["nse:NEW"] > out["target_weights"]["nse:H1"]


def test_rebalance_signals_only_outside_the_band():
    book = pd.DataFrame([
        {"position_id": "p1", "company_master_id": "a", "ticker": "A", "position_size_rs": 400000.0, "entry_price": 100.0,
         "story_score": 80.0, "daily_vol_pct": 2.0, "sector_code": "S1", "trim_count": 0},
        {"position_id": "p2", "company_master_id": "b", "ticker": "B", "position_size_rs": 400000.0, "entry_price": 100.0,
         "story_score": 80.0, "daily_vol_pct": 2.0, "sector_code": "S2", "trim_count": 0},
    ])
    # both targets are 4% of 1 crore = 4 lakh; A doubled (8 lakh, >150%), B up 20% (inside the band)
    out = v3.rebalance_signals(book, {"A": 200.0, "B": 120.0}, 1e7, 25)
    assert [(s["ticker"], s["kind"]) for s in out] == [("A", "rebalance_down")]
