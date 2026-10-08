from datetime import datetime, timezone

from fundamentals.trigger_study.llm_runner import call_claude, in_night_window, parse_reset
from fundamentals.trigger_study.queue import keyword_class


def test_keyword_class_drops_routine_but_never_a_mis_tagged_material_filing():
    assert keyword_class("Trading Window", "X Ltd has informed the Exchange regarding the Trading Window closure") == "routine"
    assert keyword_class("Allotment of ESOP/ESPS", "allotment of 158816 Equity Shares under ESOP") == "routine"
    # a company that files an order win under a routine category must still be read
    assert keyword_class("Trading Window", "X Ltd bags order worth Rs 450 crore from NHAI") == "tag"
    assert keyword_class("Press Release", 'titled "BHEL bags EPC order for 30 MW Solar"') == "tag"


def test_keyword_class_results_and_calls_are_not_sent_to_the_tagger():
    assert keyword_class("Financial Result Updates", "Unaudited financial results for the quarter") == "results_filed"
    assert keyword_class("Press Release", 'titled "Press release on unaudited financial results"') == "results_filed"
    assert keyword_class("Investor Presentation", "X Ltd has informed the Exchange about Investor Presentation") == "presentation_or_call"
    assert keyword_class("Updates", "regarding 'Proposed Initial Public Offering of a subsidiary'") == "tag"


NOW = datetime(2026, 10, 8, 10, 0, tzinfo=timezone.utc)   # 15:30 IST


def test_parse_reset_epoch_form():
    assert parse_reset("Claude AI usage limit reached|1760000000", NOW) == datetime.fromtimestamp(1760000000, tz=timezone.utc)


def test_parse_reset_clock_time_rolls_to_the_next_occurrence_in_the_named_zone():
    at = parse_reset("You've hit your limit · resets 3am (Asia/Calcutta)", NOW)
    assert at == datetime(2026, 10, 8, 21, 30, tzinfo=timezone.utc)      # 03:00 IST tomorrow
    at = parse_reset("5-hour limit reached ∙ resets 4:30pm (Asia/Calcutta)", NOW)
    assert at == datetime(2026, 10, 8, 11, 0, tzinfo=timezone.utc)       # 16:30 IST today


def test_parse_reset_dated_weekly_form_and_unknown_text():
    at = parse_reset("Weekly limit reached · resets Oct 12, 3pm (Asia/Calcutta)", NOW)
    assert at == datetime(2026, 10, 12, 9, 30, tzinfo=timezone.utc)
    assert parse_reset("usage limit reached", NOW) is None


def test_night_window_is_ist():
    assert in_night_window(datetime(2026, 10, 8, 17, 0, tzinfo=timezone.utc))      # 22:30 IST
    assert not in_night_window(datetime(2026, 10, 8, 10, 0, tzinfo=timezone.utc))  # 15:30 IST


def test_call_claude_classifies_a_limit_reply(monkeypatch):
    class P:
        returncode = 1
        stdout = '{"type":"result","subtype":"success","is_error":true,"result":"You\'ve hit your limit · resets 3am"}'
        stderr = ""
    monkeypatch.setattr("subprocess.run", lambda *a, **k: P())
    res = call_claude("tag", [{"id": "1", "text": "x"}], "claude-haiku-4-5")
    assert res["ok"] is False and res["kind"] == "limit"


def test_parse_xbrl_reads_this_quarter_in_crore():
    from fundamentals.trigger_study.nse_results import parse_xbrl
    xml = ('<in-bse-fin:RevenueFromOperations contextRef="OneD" unitRef="INR" decimals="-5">1652355000.00</in-bse-fin:RevenueFromOperations>'
           '<in-bse-fin:RevenueFromOperations contextRef="FourD" unitRef="INR">4000000000.00</in-bse-fin:RevenueFromOperations>'
           '<in-bse-fin:ProfitLossForPeriod contextRef="OneD" unitRef="INR">91494000.00</in-bse-fin:ProfitLossForPeriod>')
    f = parse_xbrl(xml)
    assert round(f["sales"], 2) == 165.24 and round(f["net_profit"], 2) == 9.15


def test_choose_rows_keeps_one_basis_and_the_latest_refiling():
    from fundamentals.trigger_study.nse_results import choose_rows
    rows = []
    for q in ("31-Dec-2019", "31-Mar-2020", "30-Jun-2020"):
        for cons in ("Consolidated", "Non-Consolidated"):
            rows.append({"toDate": q, "consolidated": cons, "broadCastDate": "05-Feb-2020 14:34:29", "xbrl": f"{q}{cons}.xml"})
    rows.append({"toDate": "30-Jun-2020", "consolidated": "Consolidated", "broadCastDate": "06-Aug-2020 10:00:00", "xbrl": "refiled.xml"})
    rows.append({"toDate": "31-Mar-2023", "consolidated": "Consolidated", "broadCastDate": "10-May-2023 10:00:00", "xbrl": "check_years.xml"})
    df = choose_rows(rows)
    assert df["consolidated"].all() and len(df) == 3
    assert df.loc[df["period_end"] == "2020-06-30", "xbrl"].item() == "refiled.xml"
