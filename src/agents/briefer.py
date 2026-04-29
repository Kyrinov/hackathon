from __future__ import annotations

import os
from typing import Any

from src.agents import narrator

SYSTEM_PROMPT = """You write 2-3 sentence minister-grade briefings for autonomously-detected
funding-ring findings on the AI For Accountability platform (Challenge #6: Related Parties
and Governance Networks). Inputs: a finding describing a new external disbursement that the
agent fleet has linked to a known CRA-confirmed funding cycle.

Strict rules:
- Use only the supplied evidence. Do not infer wrongdoing.
- Use cautious language: "flagged for review", "the records show".
- Name the trigger source, the amount, and the ring connection in one sentence.
- Second sentence: name 2-3 of the ring entities.
- Third sentence (optional): one sentence on what a reviewer should check next.
- Total length 2-3 sentences, no bullets, no headers.
"""


def _has_api_key() -> bool:
    return bool(os.getenv("ANTHROPIC_API_KEY"))


def brief(finding: dict[str, Any]) -> str:
    """Generate a minister-grade briefing for a finding.

    Returns the analyst's seed narrative if no API key or on error. Never raises.
    """
    seed = finding.get("narrative") or ""
    if not _has_api_key():
        return seed
    try:
        client = narrator._client(None)
        prompt = (
            "Write the briefing for this autonomously-detected finding. Use only the "
            "JSON evidence and preserve the cautious, decision-support framing.\n\n"
            f"{narrator._payload(finding)}"
        )
        response = client.messages.create(
            model=narrator.MODEL,
            max_tokens=200,
            temperature=0.2,
            system=[
                {
                    "type": "text",
                    "text": SYSTEM_PROMPT,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            messages=[{"role": "user", "content": prompt}],
        )
        text = narrator._extract_text(response)
        return text or seed
    except Exception:
        return seed
