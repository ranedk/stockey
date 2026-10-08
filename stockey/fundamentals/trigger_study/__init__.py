"""Trigger study -- what filings and results came before a momentum stock kept running?

Operator idea, 2026-10-08: take the stocks that rallied, find what was announced around each
rally, and learn per sector which triggers matter and how long they take to play out. Live use
would be: take high-momentum stocks, wait for a trigger, hold for its known timeline.

Design (agreed in the session that built it, docs: docs/TRIGGER_STUDY.md):
  * events  -- every point-in-time ENTRY into the top tenth by 26-week relative strength, with
    its outcome over the next 26 weeks against its own industry: "continued" or "failed".
    Comparing the two is what tells a trigger from noise; winners alone cannot.
  * filings -- each sampled stock's full NSE announcement list, one request per company.
  * queue   -- routine filings dropped by keyword (the company's own category is ignored, it
    is often wrong); the rest batched, shuffled across both groups, outcome hidden.
  * runner  -- tags each batch through the Claude CLI on the operator's plan (no API spend),
    paced, and parks itself until the usage window resets when the limit is hit.
  * report  -- trigger rates, continued vs failed, before vs after the entry.

Discovery uses entries 2016-07 -> 2022-12 only. Entries from 2023 are the check and must not
be tagged until the trigger list is frozen (systrader LEDGER row 56).
"""
