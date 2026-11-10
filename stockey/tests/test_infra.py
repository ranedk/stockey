from datetime import datetime, timezone

import pandas as pd

from infra import fleet, handlers, orchestrator, records, tasks


def test_history_pauses_in_the_main_servers_nse_bse_windows():
    assert orchestrator.quiet_now(datetime(2026, 11, 9, 2, 0, tzinfo=timezone.utc))        # 07:30 IST
    assert orchestrator.quiet_now(datetime(2026, 11, 9, 14, 0, tzinfo=timezone.utc))       # 19:30 IST
    assert not orchestrator.quiet_now(datetime(2026, 11, 9, 6, 0, tzinfo=timezone.utc))    # 11:30 IST


def test_pools_never_exceed_their_caps():
    assert fleet.want("hetzner", 0) == 0
    assert fleet.want("hetzner", 3) == 3
    assert fleet.want("hetzner", 500) == 8
    assert fleet.want("runpod", 1) == 1
    assert fleet.want("runpod", 41) == 3
    assert fleet.want("runpod", 10_000) == 3


def test_a_task_kind_knows_its_pool_and_site():
    t = tasks.new_task("fetch_attachment", {"url": "https://nsearchives.nseindia.com/corporate/x.pdf"}, dedup_key="k")
    assert (t["pool"], t["domain"]) == ("hetzner", "nse")
    assert tasks.new_task("fetch_attachment", {"url": "https://www.bseindia.com/xml-data/corpfiling/AttachLive/a.pdf"})["domain"] == "bse"
    assert tasks.new_task("ocr_pdf", {"url": "u"})["pool"] == "runpod"


def test_announcement_rows_and_bse_archive_fallback(monkeypatch):
    nse = tasks.nse_rows("KAYNES", [{"an_dt": "05-Nov-2026 18:01:22", "desc": "Press Release",
                                     "attchmntText": "Kaynes wins an order", "attchmntFile": "https://nsearchives.nseindia.com/a.pdf"}])
    assert len(nse) == 1 and nse.iloc[0]["announced_at"].year == 2026 and nse.iloc[0]["attachment_url"].endswith("a.pdf")
    bse = tasks.bse_rows("KAYNES", [{"NEWSID": "abc", "DissemDT": "2026-11-05T18:01:22", "HEADLINE": "Order",
                                     "ATTACHMENTNAME": "x.pdf", "SUBCATNAME": "Award of Order"}])
    assert bse.iloc[0]["attachment_url"] == tasks.BSE_LIVE + "x.pdf"
    stored, queued = [], []
    monkeypatch.setattr(tasks, "upsert_to_db", lambda df, *a, **k: stored.append(df))
    kids = tasks._ingest_announcements({"id": 7, "kind": "bse_announcements", "priority": 50, "run_tag": "t",
                                        "payload": {"symbol": "KAYNES"}},
                                       {"rows": [{"NEWSID": "abc", "ATTACHMENTNAME": "x.pdf"}]}, None)
    assert kids[0]["payload"]["alt"] == tasks.BSE_HIS + "x.pdf" and kids[0]["dedup_key"] == f"att:{tasks.BSE_LIVE}x.pdf"
    assert kids[0]["parent_id"] == 7 and stored[0].iloc[0]["task_id"] == 7


def test_a_scan_gets_an_ocr_task_and_a_text_pdf_does_not(monkeypatch):
    monkeypatch.setattr(tasks, "upsert_to_db", lambda *a, **k: None)
    import utils.store
    monkeypatch.setattr(utils.store, "save_file_content", lambda *a, **k: None)
    task = {"id": 1, "kind": "fetch_attachment", "priority": 50, "run_tag": None, "payload": {"url": "u", "ocr": True}}
    scan = tasks._ingest_attachment(task, {"status": "fetched", "pages": 3, "text_layer": "  "}, b"%PDF")
    text = tasks._ingest_attachment(task, {"status": "fetched", "pages": 1, "text_layer": "x" * 500}, b"%PDF")
    assert [k["kind"] for k in scan] == ["ocr_pdf"] and text == []


def test_hetzner_boot_script_runs_the_worker_with_its_own_token_only():
    s = fleet.hetzner_user_data("TOKEN123")
    assert "ORCH_TOKEN=TOKEN123" in s and "xvfb-run" in s and "systemctl enable --now worker" in s
    for secret in ("HETZNER_KEY", "RUNPOD_API_KEY", "DATABASE", "DHAN"):
        assert secret not in s


def test_worker_gap_holds_per_site(monkeypatch):
    t = {"now": 100.0}
    slept = []
    monkeypatch.setattr(handlers.time, "monotonic", lambda: t["now"])
    monkeypatch.setattr(handlers.time, "sleep", lambda s: slept.append(s))
    ctx = handlers.Context()
    ctx.gap("nse")
    t["now"] = 103.0
    ctx.gap("nse")
    ctx.gap("bse")
    assert slept == [7.0]


def test_answer_citations_must_be_filings_claude_was_given():
    from infra import research
    text, bad = research.cite_check("Orders rose [R2]. The CFO left [R9]. Margins fell [R2, R3].",
                                    {"R2", "R3"}, {"R2": "https://x/2.pdf", "R3": "https://x/3.pdf", "R9": "u9"})
    assert bad == ["R9"] and "R9" not in text.split("## Sources")[0]
    assert "- **R2**: https://x/2.pdf" in text and "- **R3**: https://x/3.pdf" in text


def _filing(source, fid, text_layer, when="2026-05-13 18:00", ocr=None, nbytes=1000, pages=1, sub="Outcome"):
    return {"source": source, "filing_id": fid, "symbol": "KAYNES", "announced_at": pd.Timestamp(when),
            "category": "Outcome of Board Meeting", "subject": sub, "text": None,
            "attachment_url": f"https://{source}/{fid}.pdf", "text_layer": text_layer, "ocr_text": ocr,
            "bytes": nbytes, "pages": pages}


def test_the_nse_and_bse_copies_of_one_document_become_one_record():
    body = "The Board approved the appointment of Mr A as Independent Director. " * 10
    df = pd.DataFrame([_filing("nse", "n1", body, "2026-05-13 18:05"), _filing("bse", "b1", body, "2026-05-13 18:01",
                                                                                 sub="Board appoints Mr A"),
                       _filing("bse", "b2", "x", nbytes=5555, sub="Scan")])
    recs = records.group(df)
    assert len(recs) == 2
    merged = next(r for r in recs if len(r["sources"]) == 2)
    assert merged["subject"] == "Board appoints Mr A" and str(merged["announced_at"]) == "2026-05-13 18:01:00"
    # OCR text arriving later changes the text (re-extract) but not the record's identity
    df.loc[2, "ocr_text"] = "Order received worth Rs 120 crore"
    again = records.group(df)
    assert {r["record_key"] for r in again} == {r["record_key"] for r in recs}
    scan = next(r for r in again if r["sources"][0]["filing_id"] == "b2")
    assert "120 crore" in scan["text"]


def test_a_fact_is_verified_only_when_its_quote_and_numbers_are_in_the_text():
    text = "The Company has received an order worth Rs 1,250.5 crore from NTPC. Allotted 10,00,000 shares at Rs 412.50 each."
    ok = records.validate({"type": "order_won", "summary": "s", "amount_value": "1,250.5", "amount_unit": "crore",
                           "currency": "INR", "quote": "received an order worth Rs 1,250.5 crore from NTPC"}, text, {})
    assert ok["verified"] and ok["amount_cr"] == 1250.5
    alloc = records.validate({"type": "preferential_issue", "summary": "s", "shares": 1000000, "price_inr": 412.5,
                              "quote": "Allotted 10,00,000 shares at Rs 412.50 each."}, text, {})
    assert alloc["verified"]
    wrong = records.validate({"type": "order_won", "summary": "s", "amount_value": "1,520", "amount_unit": "crore",
                              "quote": "received an order worth Rs 1,250.5 crore from NTPC"}, text, {})
    assert not wrong["verified"] and "amount not in the quote" in wrong["note"]
    made_up = records.validate({"type": "director_left", "summary": "s", "quote": "Mr B resigned as director."}, text, {})
    assert not made_up["verified"]


def test_routine_and_normal_records_go_in_separate_batches_by_size():
    items = [{"routine": False, "text": "x" * 25_000}, {"routine": True, "text": "r"}, {"routine": False, "text": "y" * 40_000},
             {"routine": True, "text": "r"}]
    out = records.batches(sorted(items, key=lambda x: x["routine"]))
    assert [[len(i["text"]) for i in b] for b in out] == [[25_000], [40_000], [1, 1]]


def test_passages_cover_the_whole_text():
    text = "\n".join(f"line {i} " + "w" * 300 for i in range(20)) + "\n" + "z" * 3000
    parts = records.chunks(text)
    assert all(len(p) <= records.CHUNK_CHARS for p in parts)
    assert "".join(parts).replace("\n", "") == text.replace("\n", "")


def test_runpod_low_balance_emails_once_until_topped_up(monkeypatch, tmp_path):
    from datetime import timedelta
    monkeypatch.setattr(fleet, "BALANCE_STATE", tmp_path / "b.json")
    sent, bal = [], {"v": 30.0}
    monkeypatch.setattr(fleet, "runpod_balance", lambda: bal["v"])
    t = datetime(2026, 11, 10, 10, 0, tzinfo=timezone.utc)
    send = lambda subj, body: sent.append(subj) or True
    for v in (30.0, 19.0, 15.0, 40.0, 18.0):
        bal["v"] = v
        t += timedelta(minutes=fleet.BALANCE_CHECK_MIN + 1)
        fleet.check_runpod_balance(t, send)
    assert len(sent) == 2 and "19.00" in sent[0] and "18.00" in sent[1]
    bal["v"] = 1.0
    assert fleet.check_runpod_balance(t + timedelta(minutes=1), send)["balance"] == 18.0   # not re-read within 30 min


class _Cur:
    def __init__(self):
        self.sql = []

    def execute(self, sql, args=None):
        self.sql.append((" ".join(sql.split()), args))
        self.rowcount = 1


def _fake_session(monkeypatch, cur):
    from contextlib import contextmanager

    @contextmanager
    def session(*a, **k):
        yield None, cur
    monkeypatch.setattr(records, "db_session", session)


def test_a_record_the_category_called_routine_is_reread_when_its_content_is_not(monkeypatch):
    cur = _Cur()
    _fake_session(monkeypatch, cur)
    rec = {"record_key": "k", "symbol": "KAYNES", "routine": True, "text": "Order worth Rs 50 crore received."}
    n, promoted = records._store(rec, {"kind": "order", "summary": "s", "facts": []}, {}, 2500, True)
    assert promoted and any("status" in q and "pending" in (a or ()) for q, a in cur.sql)
    n, promoted = records._store({**rec}, {"kind": "newspaper_copy", "summary": "s",
                                           "facts": [{"type": "other_material", "summary": "x", "quote": "Order"}]},
                                 {}, 2500, True)
    assert not promoted and n == 0          # routine kinds keep no facts


def test_a_partial_scan_gets_its_remaining_pages_only_when_claude_asks(monkeypatch):
    cur, queued = _Cur(), []
    _fake_session(monkeypatch, cur)
    monkeypatch.setattr(records.tasks, "enqueue", lambda ts, c=None: queued.extend(ts))
    rec = {"record_key": "k", "symbol": "KAYNES", "routine": False, "text": "t",
           "partial": {"url": "https://bse/x.pdf", "blob_key": "infra/docs/x.pdf", "pages": 12}}
    records._store(rec, {"kind": "investor_presentation", "summary": "s", "facts": [], "read_more": True}, {}, 1, False)
    assert queued[0]["payload"]["first_page"] == tasks.OCR_PROBE_PAGES + 1 and queued[0]["pool"] == "runpod"
    records._store(rec, {"kind": "board_outcome", "summary": "s", "facts": [], "read_more": False}, {}, 1, False)
    assert len(queued) == 1 and cur.sql[-1][1][0] == "rest_skipped"


def test_the_catalogue_reaches_the_prompt_and_the_doc():
    from infra import catalog
    prompt = records.PROMPT.format(kinds=catalog.prompt_kinds(), types=catalog.prompt_facts())
    doc = catalog.markdown()
    for k in list(catalog.KINDS) + list(catalog.FACT_TYPES):
        assert f"- {k}:" in prompt and f"`{k}`" in doc
    assert records.VERSION == catalog.VERSION and "allottee_category" in prompt
