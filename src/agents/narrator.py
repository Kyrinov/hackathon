from __future__ import annotations

import json
import time
from typing import Any

import anthropic
from dotenv import load_dotenv

MODEL = "claude-haiku-4-5-20251001"

SYSTEM_PROMPT = """You write factual decision-support narratives for the AI For Accountability hackathon.
The current submission is for Agency 2026 Challenge #6: Related Parties and Governance Networks.
The underlying entity layer is built from organizer-provided golden records: Splink probabilistic linkage plus Sonnet 4.6 review for borderline entity-resolution decisions.
Every finding must be grounded in the supplied evidence and source row IDs.
Use cautious public-sector language: say "flagged for review", do not accuse, infer intent, or imply wrongdoing.
Name the entities, the shared director or cycle relationship, and the dataset combination such as CRA T3010 directorship plus federal grants disclosure.
Keep the response to 3-5 concise sentences."""


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
