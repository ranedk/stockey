from __future__ import annotations

import json
import textwrap
from typing import Any


ADVISORY_EVENT_PROMPT_VERSION = "ADVISORY_EVENT_EVAL_V1"

SYSTEM_PROMPT = textwrap.dedent(
    """\
    You evaluate stock exchange announcements for an existing investment setup.
    Work only from the provided announcement and point-in-time stock context.
    Do not invent facts. Be conservative about materiality and investability.
    If evidence is mixed or incomplete, prefer review_manual over continue.
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

        Use short evidence-backed statements. Quote only facts present in the input.

        Input payload:
        {_stable_json(payload)}
        """
    ).strip()
