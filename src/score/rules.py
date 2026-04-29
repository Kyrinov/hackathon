from __future__ import annotations

from typing import Any

from src.db import queries


def rule_funding_concentration(entity_id) -> tuple[float, str, list]:
    """Source: fed.grants_contributions concentration derived by owner_org."""
    df = queries.fetch_fed_concentration(entity_id)
    if df.is_empty():
        return (0.0, "", [])
    row = df.to_dicts()[0]
    if float(row.get("hhi_score") or 0.0) > 0.5:
        return (
            0.3,
            "Concentrated funding sources",
            [{"source": "fed_concentration", "source_row_id": str(entity_id), **row}],
        )
    return (0.0, "", [])


def rule_cycle_member(entity_id, cycles) -> tuple[float, str, list]:
    """Source: cra.loops precomputed cycle membership."""
    entity_id = str(entity_id)
    evidence = []
    for cycle in cycles:
        if entity_id in {str(item) for item in cycle.get("entity_ids", [])}:
            evidence.append(
                {
                    "source": "cra_cycle",
                    "source_row_id": cycle.get("cycle_id"),
                    "mapping_method": "authoritative",
                    "confidence_score": 1.0,
                }
            )
    return (0.4, "Round-trip funding (CRA-confirmed)", evidence) if evidence else (0.0, "", [])


def rule_shared_director(entity_id, director_threshold: int = 3) -> tuple[float, str, list]:
    """Source: cra.cra_directors grouped through general.entity_golden_records.bn_root."""
    directors = queries.fetch_directors_for_org(entity_id)
    if directors.is_empty():
        return (0.0, "", [])
    candidates = queries.fetch_shared_director_candidates(director_threshold)
    candidate_names = {
        row["director_name_normalized"]: row
        for row in candidates.iter_rows(named=True)
    }
    evidence = []
    for row in directors.iter_rows(named=True):
        candidate = candidate_names.get(row["director_name_normalized"])
        if candidate:
            evidence.append(
                {
                    "source": "cra_director",
                    "source_row_id": row.get("source_row_id"),
                    "mapping_method": "authoritative",
                    "confidence_score": 1.0,
                    "director_name_normalized": row["director_name_normalized"],
                    "org_count": candidate.get("org_count"),
                }
            )
    return (0.3, "Shared directorship across multiple funded entities", evidence) if evidence else (0.0, "", [])


def rule_sole_source_growth(entity_id) -> tuple[float, str, list]:
    """Source: ab.ab_sole_source repeat/splitting flags derived by vendor."""
    df = queries.fetch_ab_sole_source_flags(entity_id)
    evidence = [
        {
            "source": "ab_sole_source",
            "source_row_id": str(entity_id),
            "mapping_method": "authoritative",
            "confidence_score": 1.0,
            **row,
        }
        for row in df.iter_rows(named=True)
        if row.get("repeat_vendor_flag") or row.get("splitting_flag")
    ]
    return (0.35, "Sole-source pattern", evidence) if evidence else (0.0, "", [])


def rule_related_entity_funding(entity_id) -> tuple[float, str, list]:
    """Source: general.entity_golden_records.related_entities plus funding edges."""
    related = queries.fetch_related_entities(entity_id)
    if related.is_empty():
        return (0.0, "", [])
    related_ids = {row["related_entity_id"] for row in related.iter_rows(named=True)}
    evidence = []
    for edge in queries.fetch_funding_edges(entity_id, "both").iter_rows(named=True):
        if edge.get("from_entity_id") in related_ids or edge.get("to_entity_id") in related_ids:
            evidence.append(
                {
                    "source": edge.get("source"),
                    "source_row_id": edge.get("source_row_id"),
                    "mapping_method": edge.get("mapping_method"),
                    "confidence_score": edge.get("confidence_score"),
                }
            )
    return (0.25, "Funding flows to related entity", evidence) if evidence else (0.0, "", [])


def evidence_jsonable(evidence: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [dict(item) for item in evidence]
