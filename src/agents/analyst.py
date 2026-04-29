from __future__ import annotations

from typing import Any

from src.agents import state
from src.graph.builder import analyze_neighborhood


def _amount_for(row: dict[str, Any], source: str) -> float:
    if source == "fed_grants":
        try:
            return float(row.get("agreement_value") or 0.0)
        except (TypeError, ValueError):
            return 0.0
    if source == "cra_donees":
        # CKAN field is "Total Gifts" (string); historical schema uses total_gifts
        raw = row.get("Total Gifts") or row.get("total_gifts") or 0
        try:
            return float(raw)
        except (TypeError, ValueError):
            return 0.0
    return 0.0


def _severity(amount: float, ring_count: int) -> str:
    if ring_count >= 1 and amount >= 1_000_000:
        return "urgent"
    if ring_count >= 1:
        return "review"
    return "info"


def _previously_seen_ring_ids() -> set[str]:
    """All ring_ids ever recorded as findings, used to label expanded vs new."""
    seen: set[str] = set()
    for f in state.list_findings(limit=2000):
        rid = f.get("ring_id")
        if rid:
            seen.add(rid)
    return seen


def analyze(
    resolved: dict[str, list[int]],
    source: str,
    rows_by_external_id: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    """Promotion pipeline: turn resolved external rows into findings.

    Strategy:
      1. Union all resolved entity_ids → single batch call to analyze_neighborhood.
         (One remote query for cycles, not one per resolved row.)
      2. For each ring touched, find the trigger row(s) whose entities seeded it.
      3. Emit one finding per (ring × trigger row) pair, capped to control noise.
    """
    if not resolved:
        return []

    # Build entity_id → list of (external_id, row, amount) reverse map for triggers
    entity_to_triggers: dict[int, list[tuple[str, dict[str, Any], float]]] = {}
    all_seeds: set[int] = set()
    for ext_id, entity_ids in resolved.items():
        row = rows_by_external_id.get(ext_id) or {}
        amount = _amount_for(row, source)
        if amount > 0 and amount < 1000:
            continue  # skip noise
        for eid in entity_ids:
            entity_to_triggers.setdefault(int(eid), []).append((ext_id, row, amount))
            all_seeds.add(int(eid))

    if not all_seeds:
        return []

    rings = analyze_neighborhood(sorted(all_seeds))
    if not rings:
        return []

    prior_ring_ids = _previously_seen_ring_ids()

    findings: list[dict[str, Any]] = []
    emitted: set[tuple[str, str]] = set()

    for ring in rings:
        ring_id = ring.get("ring_id") or ""
        ring_amount = float(ring.get("total_amount") or 0.0)
        ring_seeds = set(ring.get("seeds_touched") or [])
        ring_names = ring.get("canonical_names") or []
        finding_type = "expanded_ring" if ring_id in prior_ring_ids else "new_ring"

        # Pick best trigger row per seed in this ring (highest amount); cap one trigger per ring
        candidates: list[tuple[str, dict[str, Any], float, int]] = []
        for seed_eid in ring_seeds:
            for ext_id, row, amount in entity_to_triggers.get(int(seed_eid), []):
                candidates.append((ext_id, row, amount, int(seed_eid)))
        if not candidates:
            continue
        candidates.sort(key=lambda c: c[2], reverse=True)
        best_ext_id, best_row, best_amount, best_seed = candidates[0]

        key = (ring_id, best_ext_id)
        if key in emitted:
            continue
        emitted.add(key)

        severity = _severity(max(ring_amount, best_amount), 1)
        trigger_name = best_row.get("recipient_legal_name") or best_row.get("name") or ""
        names_preview = ", ".join(str(n) for n in ring_names[:3])
        if len(ring_names) > 3:
            names_preview += "..."
        narrative_seed = (
            f"{source} disbursement of ${best_amount:,.0f} to {trigger_name} "
            f"(external_id={best_ext_id}) connects to ring {ring_id} "
            f"(${ring_amount:,.0f} flow across {len(ring.get('entity_ids', []))} entities: {names_preview})."
        )

        findings.append({
            "source": source,
            "finding_type": finding_type,
            "entity_ids": [best_seed] + [int(e) for e in ring.get("entity_ids", []) if int(e) != best_seed],
            "ring_id": ring_id,
            "trigger_external_id": best_ext_id,
            "narrative": narrative_seed,
            "total_amount": ring_amount or best_amount,
            "severity": severity,
        })

    return findings
