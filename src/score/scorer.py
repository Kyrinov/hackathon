from __future__ import annotations

from typing import Any

import polars as pl

from src.db import queries
from src.graph.builder import find_related_party_rings
from src.score.rules import (
    evidence_jsonable,
    rule_cycle_member,
    rule_funding_concentration,
    rule_related_entity_funding,
    rule_shared_director,
    rule_sole_source_growth,
)


def _cycle_dicts(cycles_df: pl.DataFrame) -> list[dict[str, Any]]:
    return cycles_df.to_dicts() if not cycles_df.is_empty() else []


def score_entity(entity_id, cycles) -> dict:
    score = 0.0
    flags = []
    evidence = []
    for contribution, label, rule_evidence in (
        rule_funding_concentration(entity_id),
        rule_cycle_member(entity_id, cycles),
        rule_shared_director(entity_id),
        rule_sole_source_growth(entity_id),
        rule_related_entity_funding(entity_id),
    ):
        if contribution > 0:
            score += contribution
            flags.append(label)
            evidence.extend(rule_evidence)
    return {
        "entity_id": str(entity_id),
        "total_score": min(score, 1.0),
        "flags": flags,
        "evidence": evidence_jsonable(evidence),
    }


def _candidate_entity_ids(cycles: list[dict[str, Any]]) -> list[int]:
    ids = set()
    for cycle in cycles:
        ids.update(int(entity_id) for entity_id in cycle.get("entity_ids", []) if entity_id)
    for row in queries.fetch_shared_director_candidates(3).iter_rows(named=True):
        ids.update(int(entity_id) for entity_id in row.get("entity_ids", []) if entity_id)
    for row in queries.fetch_ab_sole_source_flags().iter_rows(named=True):
        if row.get("entity_id") is not None:
            ids.add(int(row["entity_id"]))
    return sorted(ids)


def score_all_top_n(top_n: int = 200) -> pl.DataFrame:
    cycles = _cycle_dicts(queries.fetch_cra_precomputed_cycles(2, 6))
    entity_ids = _candidate_entity_ids(cycles)
    records = queries.fetch_entities_by_ids(entity_ids)
    names = {
        int(row["entity_id"]): row.get("canonical_name", str(row["entity_id"]))
        for row in records.iter_rows(named=True)
    }
    rows = []
    for entity_id in entity_ids:
        scored = score_entity(entity_id, cycles)
        if scored["total_score"] <= 0:
            continue
        rows.append(
            {
                "entity_id": scored["entity_id"],
                "canonical_name": names.get(entity_id, str(entity_id)),
                "total_score": scored["total_score"],
                "flags_csv": ", ".join(scored["flags"]),
                "evidence_count": len(scored["evidence"]),
            }
        )
    schema = {
        "entity_id": pl.Utf8,
        "canonical_name": pl.Utf8,
        "total_score": pl.Float64,
        "flags_csv": pl.Utf8,
        "evidence_count": pl.Int64,
    }
    return (
        pl.DataFrame(rows, schema=schema).sort("total_score", descending=True).head(top_n)
        if rows
        else pl.DataFrame(schema=schema)
    )


def _fast_score(ring: dict) -> float:
    """Deterministic score from ring metadata — zero extra DB queries."""
    amount = float(ring.get("total_amount") or 0.0)
    score = 0.4  # confirmed CRA cycle
    if amount >= 1_000_000:
        score += 0.3
    elif amount >= 500_000:
        score += 0.2
    elif amount >= 100_000:
        score += 0.1
    if ring.get("shared_persons"):
        score += 0.3
    return min(score, 1.0)


def top_rings(n: int = 20) -> list[dict]:
    rings = find_related_party_rings()

    # One batch query for all funding edges across all rings.
    all_ids = list({int(eid) for ring in rings for eid in ring.get("entity_ids", [])})
    edges_df = queries.fetch_ring_funding_edges(all_ids)
    edges_by_pair: dict[tuple, list[dict]] = {}
    for row in edges_df.iter_rows(named=True):
        key = (str(row["from_entity_id"]), str(row["to_entity_id"]))
        edges_by_pair.setdefault(key, []).append(row)

    enriched_rings = []
    for ring in rings:
        ring_set = set(ring.get("entity_ids", []))
        funding_edges = [
            edge for (frm, to), edge_list in edges_by_pair.items()
            if frm in ring_set and to in ring_set
            for edge in edge_list
        ]
        enriched = dict(ring)
        enriched["funding_edges"] = funding_edges
        enriched["evidence"] = [
            {"source": e.get("source"), "source_row_id": e.get("source_row_id"),
             "mapping_method": e.get("mapping_method"), "confidence_score": e.get("confidence_score")}
            for e in funding_edges
        ] or ring.get("evidence", [])
        enriched["total_score"] = _fast_score(ring)
        enriched["flags"] = list(ring.get("flags") or []) or ["Round-trip funding (CRA-confirmed)"]
        enriched_rings.append(enriched)

    enriched_rings.sort(key=lambda r: (r.get("total_score", 0), r.get("total_amount", 0)), reverse=True)
    return enriched_rings[:n]
