import pandas as pd

from fundamentals.screens import portfolio_replay as R


def _inp(scores=None, floor_ok=None, narratives=None):
    return R.Inputs(days=[], scores=scores if scores is not None else pd.DataFrame(), closes=pd.DataFrame(),
                    valuation={}, alerts=pd.DataFrame(columns=["company_master_id", "trigger_type", "load_ts", "alert_date"]),
                    sectors={}, narratives=narratives if narratives is not None else pd.DataFrame(),
                    reads=pd.DataFrame(), floor_ok=floor_ok or {})


def test_band_hysteresis_enters_at_80_and_stays_while_65_or_more():
    d = pd.to_datetime(["2026-10-05", "2026-10-06", "2026-10-07", "2026-10-08"])
    s = pd.DataFrame({"date": d, "company_master_id": "nse:X", "symbol": "X", "story_score": [79, 82, 66, 64], "has_flaw": False})
    assert R.band_states(_inp(scores=s))["in_band"].tolist() == [False, True, True, False]


def test_v4_makes_no_entries_while_the_index_is_below_its_average():
    day = pd.Timestamp("2026-10-09")
    book = R.Book("v4")
    n = R.run_entries(book, _inp(floor_ok={day: False}), day, [{"ticker": "X"}], {}, lambda c, d: {"decision": "accept"}, {})
    assert n == 0 and book.log[-1][2] == "floor"


def test_watchlist_membership_is_rebuilt_point_in_time():
    w = pd.DataFrame({"narrative_text": ["", ""], "first_seen_at": pd.to_datetime(["2026-09-01", "2026-10-10"]).date,
                      "status": ["faded", "active"],
                      "status_updated_at": [pd.Timestamp("2026-10-12 10:00", tz="UTC"), pd.NaT]},
                     index=["nse:FADED_LATER", "nse:ADDED_LATER"])
    inp = _inp(narratives=w)
    assert R.on_watchlist(inp, "nse:FADED_LATER", pd.Timestamp("2026-10-09"))       # faded only on 10-12
    assert not R.on_watchlist(inp, "nse:FADED_LATER", pd.Timestamp("2026-10-13"))
    assert not R.on_watchlist(inp, "nse:ADDED_LATER", pd.Timestamp("2026-10-09"))   # not yet added
