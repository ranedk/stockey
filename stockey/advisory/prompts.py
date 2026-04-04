from __future__ import annotations

import json
import textwrap
from typing import Any


ADVISORY_EVENT_PROMPT_VERSION = "ADVISORY_EVENT_EVAL_V4"

SYSTEM_PROMPT = textwrap.dedent(
    """\
    You evaluate stock exchange announcements for an existing investment setup.
    Work only from the provided announcement and point-in-time stock context.
    Do not invent facts. Be conservative about materiality and investability.
    If evidence is mixed or incomplete, prefer review_manual over continue.
    Return one concise factual summary and pick exactly one event class from the provided taxonomy.
    Use the taxonomy semantically, not by keyword matching.
    Return structured numeric fields conservatively. Use 0.0 when the evidence for a numeric signal is weak.
    """
).strip()


def _stable_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str, indent=2)


def render_event_prompt(payload: dict[str, Any]) -> str:
    return textwrap.dedent(
        f"""\
        Evaluate this triggered company event for the current advisory setup.

        Return a structured investment-evaluation response that answers:
        - what happened
        - whether it is positive, negative, mixed, or neutral
        - whether it is materially important right now
        - whether it strengthens or weakens the active setup
        - whether it introduces governance, balance-sheet, or execution risk
        - whether the stock is investable now
        - whether the pipeline should continue, reject, or review manually
        - which event class best fits this event
        - the event direction
        - the event surprise from 0.0 to 1.0
        - the event novelty from 0.0 to 1.0
        - the contradiction risk from 0.0 to 1.0
        - expected decay in days
        - source reliability
        - affected sectors and peers if they are evident from the input
        - whether this should upgrade to pass now, downgrade to reject, or only change score
        - the score impact from -1.0 to 1.0

        Use short evidence-backed statements. Quote only facts present in the input.
        Numeric guidance:
        - surprise: how unexpected this event is relative to a normal flow of disclosures
        - novelty: how new the information is versus a routine or already-known update
        - contradiction: how much the event conflicts with the active setup thesis
        - expected_decay_days: how long this event should matter before its edge likely fades
        - source_reliability: high for official company or exchange disclosures with clear facts, medium for partially clear items, low for weak or incomplete evidence
        Event taxonomy:
        - RESULTS_POSITIVE
        - RESULTS_NEGATIVE
        - ORDER_WIN
        - CAPEX_EXPANSION
        - GUIDANCE_UPGRADE
        - GUIDANCE_DOWNGRADE
        - PLEDGE_UP
        - PLEDGE_DOWN
        - DILUTION
        - AUDITOR_GOVERNANCE
        - POLICY_SECTOR_POSITIVE
        - POLICY_SECTOR_NEGATIVE
        - OTHER

        Prefer OTHER when the document is procedural, administrative, court-process-related,
        board-process-related, or otherwise does not cleanly fit the taxonomy.

        If peers or sectors are not clearly inferable from the input, return empty lists.

        Input payload:
        {_stable_json(payload)}
        """
    ).strip()
