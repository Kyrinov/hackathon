"""Deterministic composite risk scorer for related-party rings.

Signals (all 0–1, summed and clamped):
  - Dollar amount tier          (0–0.30)
  - Shared-director count       (0–0.30)
  - Cycle hop count (inverse)   (0–0.20)
  - Entity concentration        (0–0.10)
  - Multi-dataset crossover     (0–0.10)
"""
from __future__ import annotations

import math
from typing import Any


def composite_score(ring: dict[str, Any]) -> float:
    score = 0.0

    amount = float(ring.get("total_amount") or 0.0)
    if amount >= 5_000_000:
        score += 0.30
    elif amount >= 1_000_000:
        score += 0.25
    elif amount >= 500_000:
        score += 0.18
    elif amount >= 100_000:
        score += 0.10
    elif amount >= 10_000:
        score += 0.04

    n_directors = len(ring.get("shared_persons") or [])
    score += min(n_directors * 0.10, 0.30)

    hops = int(ring.get("hop_count") or 4)
    hop_scores = {2: 0.20, 3: 0.15, 4: 0.10, 5: 0.06}
    score += hop_scores.get(hops, 0.03)

    n_entities = len(ring.get("entity_ids") or [])
    if n_entities <= 2:
        score += 0.10
    elif n_entities <= 3:
        score += 0.06
    elif n_entities <= 4:
        score += 0.03

    datasets = set(ring.get("datasets_touched") or [])
    if len(datasets) > 1:
        score += 0.10

    return round(min(score, 1.0), 4)


def risk_level(score: float) -> str:
    if score >= 0.75:
        return "critical"
    if score >= 0.55:
        return "high"
    if score >= 0.35:
        return "medium"
    return "low"


RISK_BADGE = {
    "critical": "🔴 Critical",
    "high":     "🟠 High",
    "medium":   "🟡 Medium",
    "low":      "🟢 Low",
}
