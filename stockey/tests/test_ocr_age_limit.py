from fundamentals.collectors import ocr_pipeline as ocr
from fundamentals.screens import l3_triggers


def test_queue_and_backlog_count_both_stop_at_six_months(monkeypatch):
    seen = []
    monkeypatch.setattr(ocr, "sql_to_df", lambda q, *a, **k: seen.append(q) or __import__("pandas").DataFrame({"n": [0]}))
    ocr.load_pending_ocr_targets(10)
    ocr.count_pending_ocr_targets()
    assert ocr.OCR_MAX_AGE_DAYS == 183
    assert all(f"AT TIME ZONE 'Asia/Kolkata')::date - {ocr.OCR_MAX_AGE_DAYS}" in q and "disclosure_date ~" in q for q in seen)


def test_too_old_is_a_finished_status_for_the_rule_pass():
    import inspect

    assert ocr.SKIPPED_TOO_OLD in inspect.getsource(l3_triggers)


def test_structured_extraction_main_survives_an_empty_queue(monkeypatch):
    import pandas as pd
    from fundamentals.collectors import structured_extraction as se

    monkeypatch.setattr(se, "_bootstrap_extraction_columns", lambda: None, raising=False)
    monkeypatch.setattr(se, "load_pending_extraction_targets", lambda limit=None: pd.DataFrame())
    monkeypatch.setattr(se, "promote_pit_fields_to_columns", lambda: 0)
    for name in dir(se):
        if name.startswith("_ensure") or name.startswith("_bootstrap"):
            monkeypatch.setattr(se, name, lambda *a, **k: None)
    assert se.main() == 0
