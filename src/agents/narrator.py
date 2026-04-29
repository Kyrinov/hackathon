from __future__ import annotations

import json
import time
from typing import Any

import anthropic
from dotenv import load_dotenv

MODEL = "claude-haiku-4-5-20251001"

SYSTEM_PROMPT = """You write factual, minister-grade summaries for the
Agency 2026 Challenge #6 platform (related-party governance networks).
Inputs are JSON evidence drawn from CRA T3010, federal Grants &
Contributions, and Alberta open data, resolved to canonical entities
via Splink + Sonnet 4.6.

Rules:
- Use only the supplied evidence. Never infer wrongdoing or intent.
- Cautious public-sector language: "flagged for review", "the records
  show", "warrants closer review".
- Sentence 1: name the trigger source, the dollar amount, and the ring
  or director relationship.
- Sentence 2: name 2-3 of the linked entities and the dataset
  combination (e.g., "CRA T3010 directorship plus federal grants
  disclosure").
- Sentence 3 (optional): one specific check a reviewer should do next.
- 3 sentences max. No bullets. No headers."""


def _client(client: anthropic.Anthropic | None) -> anthropic.Anthropic:
    if client is not None:
        return client
    load_dotenv()
    return anthropic.Anthropic()


def _payload(ring: dict[str, Any]) -> str:
    return json.dumps(ring, ensure_ascii=True, sort_keys=True, default=str)


def _extract_text(message: Any) -> str:
    chunks = []
    for block in getattr(message, "content", []) or []:
        text = getattr(block, "text", None)
        if text:
            chunks.append(text)
    return "\n".join(chunks).strip()


def generate_narrative(ring: dict, client: anthropic.Anthropic | None = None) -> str:
    api_client = _client(client)
    prompt = (
        "Write the narrative for this related-party ring. "
        "Use only the JSON evidence and preserve the decision-support framing.\n\n"
        f"{_payload(ring)}"
    )

    for attempt in range(2):
        try:
            response = api_client.messages.create(
                model=MODEL,
                max_tokens=300,
                temperature=0.3,
                system=[
                    {
                        "type": "text",
                        "text": SYSTEM_PROMPT,
                        "cache_control": {"type": "ephemeral"},
                    }
                ],
                messages=[{"role": "user", "content": prompt}],
            )
            return _extract_text(response)
        except anthropic.RateLimitError:
            if attempt == 0:
                time.sleep(2)
                continue
            raise

    raise RuntimeError("unreachable narrator retry state")


def generate_batch(rings: list[dict], max_rings: int = 10) -> list[dict]:
    api_client = _client(None)
    narrated = []
    for ring in rings[:max_rings]:
        enriched = dict(ring)
        enriched["narrative"] = generate_narrative(enriched, api_client)
        narrated.append(enriched)
    return narrated
