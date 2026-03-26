from __future__ import annotations

import json
import textwrap
from typing import Any


ADVISORY_EVENT_PROMPT_VERSION = "ADVISORY_EVENT_EVAL_V3"

SYSTEM_PROMPT = textwrap.dedent(
    """\
    You evaluate stock exchange announcements for an existing investment setup.
    Work only from the provided announcement and point-in-time stock context.
    Do not invent facts. Be conservative about materiality and investability.
    If evidence is mixed or incomplete, prefer review_manual over continue.
    Return one concise factual summary and pick exactly one event class from the provided taxonomy.
    Use the taxonomy semantically, not by keyword matching.
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
        - whether this should upgrade to pass now, downgrade to reject, or only change score
        - the score impact from -1.0 to 1.0

        Use short evidence-backed statements. Quote only facts present in the input.
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

        Input payload:
        {_stable_json(payload)}
        """
    ).strip()
