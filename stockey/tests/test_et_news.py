from fundamentals.collectors import et_news

RSS = b"""<?xml version="1.0"?><rss><channel><title>Auto-Industry-Economic Times</title>
<item><title><![CDATA[Carmaker X raises prices]]></title><description><![CDATA[Up 2% from October.]]></description>
<link>https://economictimes.indiatimes.com/a/1.cms</link><pubDate>Tue, 29 Sep 2026 15:53:21 +0530</pubDate></item>
<item><title>No link item</title></item>
<item><title>Bad date</title><link>https://economictimes.indiatimes.com/a/2.cms</link><pubDate>not a date</pubDate></item>
</channel></rss>"""


def test_parse_feed_keeps_linked_items_and_tolerates_bad_dates():
    items = et_news.parse_feed(RSS, "auto", "IN0201")
    assert [i["link"] for i in items] == ["https://economictimes.indiatimes.com/a/1.cms",
                                          "https://economictimes.indiatimes.com/a/2.cms"]
    first = items[0]
    assert first["title"] == "Carmaker X raises prices" and first["sector_hint"] == "IN0201"
    assert first["published_at"].isoformat() == "2026-09-29T15:53:21+05:30"
    assert items[1]["published_at"] is None


def test_collect_survives_one_failing_feed_and_dedupes_links(monkeypatch):
    class Resp:
        def __init__(self, body, ok=True):
            self.content, self.ok = body, ok

        def raise_for_status(self):
            if not self.ok:
                raise RuntimeError("503")

    calls = {"n": 0}

    class Session:
        def get(self, url, **k):
            calls["n"] += 1
            return Resp(RSS, ok=calls["n"] != 2)

    monkeypatch.setattr(et_news.requests, "Session", Session)
    monkeypatch.setattr(et_news, "ensure_table", lambda: None)
    monkeypatch.setattr(et_news, "record_local_fallback_event", lambda **k: None)
    written = []
    monkeypatch.setattr(et_news, "upsert_to_db", lambda df, table, **k: written.append((df, k)))
    out = et_news.collect()
    assert len(out["failed_feeds"]) == 1
    df, kwargs = written[0]
    assert df["link"].is_unique and kwargs["on_conflict"] == "nothing"
    assert df.set_index("link").loc["https://economictimes.indiatimes.com/a/1.cms", "sector_hint"] is not None
