from datetime import date

from data.dhanlive import option_rolling as orl


def test_windows_cover_the_range_inclusively_within_the_span_limit():
    w = orl.windows(date(2026, 1, 1), date(2026, 3, 15))
    assert w[0][0] == date(2026, 1, 1) and w[-1][1] == date(2026, 3, 15)
    for (s, e), (s2, _) in zip(w, w[1:]):
        assert (e - s).days <= orl.MAX_SPAN_DAYS
        assert (s2 - e).days == 1  # no gap, no overlap
    assert orl.windows(date(2026, 1, 1), date(2026, 1, 1)) == [(date(2026, 1, 1), date(2026, 1, 1))]


def test_strike_labels():
    assert orl.strike_label(0) == "ATM"
    assert orl.strike_label(3) == "ATM+3"
    assert orl.strike_label(-10) == "ATM-10"


def test_frame_from_payload_reads_one_side_and_tolerates_missing_arrays():
    payload = {"data": {"ce": {"timestamp": [1756698300, 1756698360], "close": [43.1, 40.5],
                               "strike": [24050.0, 24050.0], "iv": []}, "pe": None}}
    ce = orl.frame_from_payload(payload, "CE")
    assert list(ce["close"]) == [43.1, 40.5]
    assert ce["iv"].isna().all()  # an empty array is missing, not misaligned
    assert str(ce["ts"].dt.tz) == "UTC"
    assert orl.frame_from_payload(payload, "PE").empty
    assert orl.frame_from_payload({"data": {}}, "CE").empty
