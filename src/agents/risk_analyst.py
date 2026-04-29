"""LLM-backed risk analyst for related-party rings.

Backend selection (automatic, no config needed):
  1. Ollama (local) — tried first; uses OLLAMA_MODEL (default: gemma4:31b)
  2. Kimi (cloud)   — used if Ollama is unreachable; requires KIMI_API_KEY

Set LLM_BACKEND=kimi to force Kimi regardless (e.g. for testing).

Returns a dict with keys: risk_level, risk_types, summary, key_concern.
Falls back gracefully to None on any error so prewarm never crashes.
"""
from __future__ import annotations

import json
import os
import re
import time
from typing import Any

import httpx

OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "gemma4:31b")

_SYSTEM = (
    "You are a Canadian public-sector financial compliance analyst. "
    "Given structured data about a charity funding ring, produce a JSON risk assessment. "
    "Use only the supplied evidence. Never assert intent or wrongdoing — use language like "
    "'warrants review', 'the records show', 'flagged for'. "
    "Reply ONLY with valid JSON, no markdown fences, no extra text."
)

_USER_TMPL = """Assess this related-party ring for related-party governance and ethical risk.

Ring data:
{ring_json}

Reply with this exact JSON schema:
{{
  "risk_level": "critical|high|medium|low",
  "risk_types": ["list", "of", "applicable", "risk", "categories"],
  "summary": "2-3 sentence plain-English summary for a senior reviewer.",
  "key_concern": "Single sentence naming the primary flag."
}}

Valid risk_types: "circular funding pattern", "shared directorship", "related-party overlap",
"sole-source concentration", "cross-sector directorship", "unverified connection",
"high-value cycle", "multi-dataset overlap"."""


def _ring_payload(ring: dict[str, Any]) -> str:
    slim = {
        "ring_id": ring.get("ring_id"),
        "entities": ring.get("canonical_names", []),
        "shared_directors": ring.get("shared_persons", []),
        "total_amount_cad": ring.get("total_amount", 0),
        "hop_count": ring.get("hop_count"),
        "datasets": ring.get("datasets_touched", []),
        "flags": ring.get("flags", []),
        "composite_score": ring.get("composite_score"),
    }
    return json.dumps(slim, ensure_ascii=False, default=str)


def _parse_response(text: str) -> dict | None:
    text = text.strip()
    # Strip markdown fences if model adds them anyway
    text = re.sub(r"^```[a-z]*\n?", "", text)
    text = re.sub(r"\n?```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # Try extracting first {...} block
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if m:
            try:
                return json.loads(m.group())
            except json.JSONDecodeError:
                pass
    return None


def _call_ollama(ring: dict[str, Any]) -> dict | None:
    prompt = f"{_SYSTEM}\n\n{_USER_TMPL.format(ring_json=_ring_payload(ring))}"
    try:
        r = httpx.post(
            f"{OLLAMA_URL}/api/generate",
            json={"model": OLLAMA_MODEL, "prompt": prompt, "stream": False, "format": "json"},
            timeout=120,
        )
        r.raise_for_status()
        return _parse_response(r.json().get("response", ""))
    except Exception as exc:
        print(f"[risk_analyst] ollama error: {exc}")
        return None


def _call_kimi(ring: dict[str, Any]) -> dict | None:
    """Moonshot AI Kimi — OpenAI-compatible endpoint."""
    api_key = os.getenv("KIMI_API_KEY", "")
    model = os.getenv("KIMI_MODEL", "moonshot-v1-8k")
    base_url = os.getenv("KIMI_URL", "https://api.moonshot.cn/v1")
    try:
        r = httpx.post(
            f"{base_url}/chat/completions",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={
                "model": model,
                "temperature": 0.3,
                "max_tokens": 400,
                "messages": [
                    {"role": "system", "content": _SYSTEM},
                    {"role": "user", "content": _USER_TMPL.format(ring_json=_ring_payload(ring))},
                ],
            },
            timeout=60,
        )
        r.raise_for_status()
        text = r.json()["choices"][0]["message"]["content"]
        return _parse_response(text)
    except Exception as exc:
        print(f"[risk_analyst] kimi error: {exc}")
        return None


def _ollama_available() -> bool:
    try:
        r = httpx.get(f"{OLLAMA_URL}/api/tags", timeout=3)
        return r.status_code == 200
    except Exception:
        return False


def assess(ring: dict[str, Any]) -> dict | None:
    if os.getenv("LLM_BACKEND", "").lower() == "kimi":
        return _call_kimi(ring)
    if _ollama_available():
        return _call_ollama(ring)
    print("[risk_analyst] Ollama not available — falling back to Kimi")
    return _call_kimi(ring)


def assess_batch(
    rings: list[dict[str, Any]],
    top_n: int = 100,
    delay: float = 0.5,
) -> list[dict[str, Any]]:
    """Score the top_n rings with LLM analysis; return enriched copies of all rings."""
    enriched = list(rings)
    for i, ring in enumerate(enriched[:top_n]):
        print(f"[risk_analyst] {i+1}/{min(top_n, len(rings))} — {ring.get('ring_id')}")
        assessment = assess(ring)
        if assessment:
            enriched[i] = {**ring, "risk_assessment": assessment}
        if delay and i < top_n - 1:
            time.sleep(delay)
    return enriched
