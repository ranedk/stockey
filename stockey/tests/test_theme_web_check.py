from fundamentals.themes import web_check as wc


def test_a_check_needs_a_known_verdict_and_a_source():
    good = {"verdict": "watch", "in_theme": True, "share_estimate_pct": 85, "summary": "Makes transformers.",
            "recent_changes": "Bought a switchgear maker in March.", "red_flags": "",
            "sources": ["https://example.com/ar", "not a url"]}
    fields, err = wc.validate(good)
    assert err is None and fields["verdict"] == "watch" and fields["sources"] == '["https://example.com/ar"]'
    assert wc.validate({**good, "sources": []})[1] == "no source URL"
    assert wc.validate({**good, "verdict": "fine"})[1] == "verdict 'fine'"
    assert wc.validate({**good, "share_estimate_pct": 140})[0]["share_estimate_pct"] is None
