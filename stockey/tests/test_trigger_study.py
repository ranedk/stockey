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
