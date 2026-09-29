import datetime as dt

import pandas as pd

from fundamentals.collectors import bse_announcements as bse
from fundamentals.screens import news_tagging as nt
from fundamentals.screens import reeval
from fundamentals.screens import story_score as ss


# --- material changes -----------------------------------------------------------------------

def _live(score, primary="growth", flaws=None, in_band=None):
    return {"story_score": score, "primary_dimension": primary, "flaws": flaws,
            "in_band": score >= reeval.BAND_ENTER if in_band is None else in_band}


def test_first_sighting_is_a_baseline_not_a_change():
    assert reeval.changes_for(None, _live(95)) == []


def test_band_has_hysteresis():
    assert "band_enter" in reeval.changes_for(_live(70), _live(82))
    assert reeval.changes_for(_live(82), _live(70)) == []          # inside the band, above exit
    assert reeval.next_in_band(_live(82), 70) is True
    assert "band_exit" in reeval.changes_for(_live(82), _live(60))
    assert reeval.next_in_band(_live(70, in_band=False), 78) is False


def test_moves_story_changes_and_flaws():
    assert "story_strengthening" in reeval.changes_for(_live(40), _live(58))
    assert "story_fading" in reeval.changes_for(_live(58), _live(40))
    assert reeval.changes_for(_live(50), _live(60)) == []
    assert "story_changed" in reeval.changes_for(_live(70, in_band=True), _live(72, primary="margins"))
    assert "story_changed" not in reeval.changes_for(_live(30), _live(32, primary="margins"))
    kinds = reeval.changes_for(_live(50, flaws="a"), _live(40, flaws="b"))
    assert "flaw_appeared" in kinds and "flaw_cleared" in kinds


def test_sector_news_reaches_only_companies_whose_story_is_that_sector_or_dimension():
    live = pd.DataFrame({"company_master_id": ["a", "b", "c", "d"], "primary_dimension": ["sector", "growth", "margins", "sector"]})
    sectors = pd.DataFrame({"company_master_id": ["a", "b", "c", "d"], "sector_code": ["S1", "S1", "S1", "S2"]})
    assert reeval.sector_followers(live, sectors, "S1", None) == ["a"]
    assert sorted(reeval.sector_followers(live, sectors, "S1", "margins")) == ["a", "c"]


# --- news tagging ---------------------------------------------------------------------------

def _matchers(*names_tickers):
    return nt.build_matchers(pd.DataFrame([{"company_name": n, "ticker": t, "company_master_id": f"nse:{t}"}
                                           for n, t in names_tickers]))


def test_name_matching_avoids_the_known_false_friends():
    m = _matchers(("Bank of India", "BANKINDIA"), ("Central Bank", "CENTRALBK"), ("Dr Lal Pathlabs", "LALPATHLAB"),
                  ("Container Corpn.", "CONCOR"), ("Global Industries", "GLOBAL"))
    assert nt.candidates_for("Reserve Bank of India raises rates", "", m) == []
    assert nt.candidates_for("State Bank of India results", "", m) == []
    assert nt.candidates_for("European Central Bank holds", "", m) == []
    assert [c for c, _ in nt.candidates_for("Bank of India cuts MCLR", "", m)] == ["nse:BANKINDIA"]
    assert [c for c, _ in nt.candidates_for("Dr Lal PathLabs to acquire stake", "", m)] == ["nse:LALPATHLAB"]
    assert [c for c, _ in nt.candidates_for("Container Corporation of India wins bid", "", m)] == ["nse:CONCOR"]
    assert nt.candidates_for("global markets slide", "", m) == []


def test_model_cannot_add_companies_or_invent_sectors():
    item = {"link": "l", "first_seen_at": None, "candidate_companies": [{"id": "nse:A", "name": "A"}]}
    rows = nt.tag_rows(item, {"companies": ["nse:A", "nse:B"], "sector_code": "IN9999", "direction": "positive",
                              "dimension": "growth", "materiality": 4}, model="m")
    assert [(r["target_type"], r["target_id"]) for r in rows] == [("company", "nse:A")]
    rows = nt.tag_rows(item, {"companies": [], "sector_code": "IN0301", "direction": "negative",
                              "dimension": "margins", "materiality": 3}, model="m")
    assert [(r["target_type"], r["target_id"]) for r in rows] == [("sector", "IN0301")]


def test_news_features_weigh_by_materiality_and_decay():
    as_of = dt.date(2026, 9, 29)
    fresh = pd.Timestamp("2026-09-29 12:00", tz="Asia/Kolkata")
    news = pd.DataFrame([
        {"target_type": "company", "target_id": "nse:A", "direction": "positive", "materiality": 3, "first_seen_at": fresh},
        {"target_type": "company", "target_id": "nse:A", "direction": "negative", "materiality": 3,
         "first_seen_at": fresh - pd.Timedelta(days=30)},
        {"target_type": "sector", "target_id": "S1", "direction": "positive", "materiality": 5, "first_seen_at": fresh},
    ])
    comp, sect = ss.news_features(news, as_of)
    a = float(comp.set_index("company_master_id").at["nse:A", "news_net"])
    assert 0.4 < a < 0.6            # ~ +0.99 fresh minus ~0.5 a month old
    assert float(sect.set_index("sector_code").at["S1", "sector_news_net"]) > ss.SECTOR_NEWS_TAILWIND


# --- intraday BSE ---------------------------------------------------------------------------

def test_intraday_fetch_stops_paging_once_it_reaches_known_filings(monkeypatch):
    pages = {1: [{"DissemDT": "2026-09-29T19:00:00"}, {"DissemDT": "2026-09-29T18:50:00"}],
             2: [{"DissemDT": "2026-09-29T18:40:00"}], 3: [{"DissemDT": "2026-09-29T18:30:00"}]}
    calls = []

    def fake_get(url, params):
        calls.append(params["pageno"])
        return {"Table": pages[params["pageno"]], "Table1": [{"ROWCNT": 4}]}

    monkeypatch.setattr(bse, "_bse_get", fake_get)
    stop = bse._parse_bse_timestamp("2026-09-29T18:55:00")
    rows = bse.fetch_market_announcements(dt.date(2026, 9, 29), stop_before=stop)
    assert calls == [1] and len(rows) == 2
    calls.clear()
    assert len(bse.fetch_market_announcements(dt.date(2026, 9, 29))) == 4 and calls == [1, 2, 3]
