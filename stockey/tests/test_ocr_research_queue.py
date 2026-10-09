from fundamentals.collectors import ocr_pipeline


class FakeQueue:
    def __init__(self, n):
        self.items = [{"doc_id": f"d{i}", "pdf_path": __file__, "max_pages": 2} for i in range(n)]
        self.done = []

    def next_pending(self):
        return self.items[0] if self.items else None

    def mark(self, doc_id, text=None, pages_model=None, error=None):
        self.items = [x for x in self.items if x["doc_id"] != doc_id]
        self.done.append((doc_id, error))


def _patch(monkeypatch, queue, daily_pending):
    import fundamentals.collectors.ocr_research_queue as rq
    monkeypatch.setattr(rq, "next_pending", queue.next_pending)
    monkeypatch.setattr(rq, "mark", queue.mark)
    monkeypatch.setattr(ocr_pipeline, "count_pending_ocr_targets", daily_pending)
    monkeypatch.setattr(ocr_pipeline, "extract_pdf_text", lambda b, **k: ("text", {"pages_model": 1, "pages_total": 2}))


def test_research_tail_runs_only_while_the_daily_queue_is_empty(monkeypatch):
    q = FakeQueue(3)
    _patch(monkeypatch, q, lambda: 0)
    out = ocr_pipeline.run_research_tail(seconds=60)
    assert out["research_ocred"] == 3 and not out["research_stopped_for_daily"]


def test_research_tail_yields_to_a_daily_filing_that_arrives_mid_tail(monkeypatch):
    q = FakeQueue(5)
    calls = iter([0, 0, 1])          # a daily filing lands before the third research document
    _patch(monkeypatch, q, lambda: next(calls))
    out = ocr_pipeline.run_research_tail(seconds=60)
    assert out["research_ocred"] == 2 and out["research_stopped_for_daily"] == 1
    assert len(q.items) == 3


def test_main_skips_the_research_tail_while_daily_work_remains(monkeypatch):
    ran = []
    monkeypatch.setattr(ocr_pipeline, "ensure_ocr_timeout_column", lambda: None)
    monkeypatch.setattr(ocr_pipeline, "readmit_bse_attachlive_failures", lambda: None)
    monkeypatch.setattr(ocr_pipeline, "readmit_bse_sast_failures", lambda: None)
    monkeypatch.setattr(ocr_pipeline, "run_research_tail", lambda **k: ran.append(1) or {})
    monkeypatch.setattr(ocr_pipeline, "run_ocr_pipeline", lambda: {
        "ocred": 5, "failed": 0, "no_document": 0, "blocked": False, "time_budget_exceeded": False,
        "backlog_remaining": 12, "skipped_too_old": 0})
    ocr_pipeline.main()
    assert ran == []


def test_main_goes_back_to_daily_work_when_research_yields(monkeypatch):
    monkeypatch.setattr(ocr_pipeline, "ensure_ocr_timeout_column", lambda: None)
    monkeypatch.setattr(ocr_pipeline, "readmit_bse_attachlive_failures", lambda: None)
    monkeypatch.setattr(ocr_pipeline, "readmit_bse_sast_failures", lambda: None)
    daily = []
    def daily_run():
        daily.append(1)
        return {"ocred": 1, "failed": 0, "no_document": 0, "blocked": False, "time_budget_exceeded": False,
                "backlog_remaining": 0, "skipped_too_old": 0}
    tails = iter([{"research_ocred": 4, "research_failed": 0, "research_stopped_for_daily": 1},
                  {"research_ocred": 2, "research_failed": 0, "research_stopped_for_daily": 0}])
    monkeypatch.setattr(ocr_pipeline, "run_ocr_pipeline", daily_run)
    monkeypatch.setattr(ocr_pipeline, "run_research_tail", lambda **k: next(tails))
    ocr_pipeline.main()
    assert len(daily) == 2                      # daily, research (yields), daily again, research (done)
    assert ocr_pipeline.STOCKEY_RUN_STATE["research_ocred"] == 6
