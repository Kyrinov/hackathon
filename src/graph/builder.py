from __future__ import annotations

from pathlib import Path
from typing import Any

import networkx as nx
import polars as pl

from src.db import queries

_DIRECTOR_RINGS_CACHE = Path("data/cache/director_rings.parquet")


def _load_director_pairs() -> pl.DataFrame:
    """Load pre-materialized director-funding-pair rings from parquet cache.

    Returns an empty DataFrame if the cache file is missing — callers must
    handle this and fall back gracefully (the round-trip path stays valid).
    Run scripts/precompute_rings.py to (re)build the cache.
    """
    if not _DIRECTOR_RINGS_CACHE.exists():
        return pl.DataFrame()
    try:
        return pl.read_parquet(_DIRECTOR_RINGS_CACHE)
    except Exception:
        return pl.DataFrame()


def _entity_rows(entity_ids: list[int | str]) -> dict[int, dict[str, Any]]:
    df = queries.fetch_entities_by_ids(entity_ids)
    return {int(row["entity_id"]): row for row in df.iter_rows(named=True)}


def _add_entity_node(graph: nx.MultiDiGraph, entity_id: int | str, row: dict[str, Any] | None = None) -> None:
    entity_id = int(entity_id)
    row = row or {}
    graph.add_node(
        str(entity_id),
        entity_id=str(entity_id),
        canonical_name=row.get("canonical_name") or f"Entity {entity_id}",
        entity_type=row.get("entity_type") or "unknown",
        type=row.get("entity_type") or "unknown",
        datasets=row.get("datasets") or [],
        aliases=row.get("aliases") or [],
    )


def _add_directors(graph: nx.MultiDiGraph, entity_id: int | str) -> None:
    for row in queries.fetch_directors_for_org(entity_id).iter_rows(named=True):
        name = row["director_name_normalized"]
        if not name:
            continue
        person_id = f"person:{name}"
        graph.add_node(
            person_id,
            entity_id=person_id,
            canonical_name=name,
            entity_type="person",
            type="person",
            datasets=["cra"],
        )
        graph.add_edge(
            person_id,
            str(entity_id),
            source="cra_director",
            amount=0.0,
            date=str(row.get("t3010_year") or ""),
            mapping_method="authoritative",
            confidence_score=1.0,
            source_row_id=row.get("source_row_id"),
        )


def _add_funding_edge(graph: nx.MultiDiGraph, row: dict[str, Any], known_entities: dict[int, dict[str, Any]]) -> None:
    source_id = row.get("from_entity_id")
    target_id = row.get("to_entity_id")
    if source_id is None:
        source_id = f"source:{row.get('source')}:{row.get('source_row_id')}"
        graph.add_node(
            source_id,
            entity_id=source_id,
            canonical_name=str(row.get("source", "public funder")).replace("_", " ").title(),
            entity_type="gov",
            type="gov",
            datasets=[str(row.get("source", "")).split("_")[0]],
        )
    else:
        source_id = int(source_id)
        _add_entity_node(graph, source_id, known_entities.get(source_id))

    if target_id is None:
        return
    target_id = int(target_id)
    _add_entity_node(graph, target_id, known_entities.get(target_id))
    graph.add_edge(
        str(source_id),
        str(target_id),
        source=row.get("source"),
        amount=float(row.get("amount") or 0.0),
        date=str(row.get("date") or ""),
        mapping_method=row.get("mapping_method") or "authoritative",
        confidence_score=float(row.get("confidence_score") or 1.0),
        source_row_id=row.get("source_row_id"),
    )


def build_ego_graph(entity_id, radius: int = 2) -> nx.MultiDiGraph:
    graph = nx.MultiDiGraph()
    frontier = {int(entity_id)}
    visited: set[int] = set()

    for _ in range(max(radius, 1)):
        current = frontier - visited
        if not current:
            break
        known_entities = _entity_rows(list(current))
        for current_id in current:
            _add_entity_node(graph, current_id, known_entities.get(current_id))
            _add_directors(graph, current_id)
            edges = queries.fetch_funding_edges(current_id, "both")
            neighbor_ids = set()
            for row in edges.iter_rows(named=True):
                _add_funding_edge(graph, row, known_entities)
                for key in ("from_entity_id", "to_entity_id"):
                    if row.get(key) is not None:
                        neighbor_ids.add(int(row[key]))
            frontier.update(neighbor_ids)
        visited.update(current)

    return graph


def build_full_ring_graph(ring_entity_ids: list[str]) -> nx.MultiDiGraph:
    entity_ids = [int(entity_id) for entity_id in ring_entity_ids]
    graph = nx.MultiDiGraph()
    known_entities = _entity_rows(entity_ids)
    for entity_id in entity_ids:
        _add_entity_node(graph, entity_id, known_entities.get(entity_id))
        _add_directors(graph, entity_id)

    ring_set = set(entity_ids)
    for entity_id in entity_ids:
        for row in queries.fetch_funding_edges(entity_id, "both").iter_rows(named=True):
            endpoints = {row.get("from_entity_id"), row.get("to_entity_id")}
            if any(endpoint in ring_set for endpoint in endpoints):
                _add_funding_edge(graph, row, known_entities)
    return graph


def build_cra_cycle_graph(cycle_id: int | str) -> nx.MultiDiGraph:
    graph = nx.MultiDiGraph()
    edges = queries.fetch_cra_cycle_edges(cycle_id)
    entity_ids: set[int] = set()
    for row in edges.iter_rows(named=True):
        for key in ("from_entity_id", "to_entity_id"):
            if row.get(key) is not None:
                entity_ids.add(int(row[key]))

    known_entities = _entity_rows(list(entity_ids)) if entity_ids else {}
    for entity_id in entity_ids:
        _add_entity_node(graph, entity_id, known_entities.get(entity_id))
    for row in edges.iter_rows(named=True):
        _add_funding_edge(graph, dict(row), known_entities)
    return graph


def _shared_directors(entity_ids: list[int]) -> list[str]:
    director_sets = []
    for entity_id in entity_ids:
        directors = {
            row["director_name_normalized"]
            for row in queries.fetch_directors_for_org(entity_id).iter_rows(named=True)
            if row.get("director_name_normalized")
        }
        director_sets.append(directors)
    if not director_sets:
        return []
    return sorted(set.intersection(*director_sets))


def _ring_edges(entity_ids: list[int]) -> list[dict[str, Any]]:
    ring_set = set(entity_ids)
    edges = []
    seen = set()
    for entity_id in entity_ids:
        for row in queries.fetch_funding_edges(entity_id, "both").iter_rows(named=True):
            endpoints = {row.get("from_entity_id"), row.get("to_entity_id")}
            if not endpoints <= ring_set:
                continue
            edge_key = (row.get("source"), row.get("source_row_id"), row.get("from_entity_id"), row.get("to_entity_id"))
            if edge_key in seen:
                continue
            seen.add(edge_key)
            edges.append(dict(row))
    return edges


def find_related_party_rings(min_total_amount: float = 50_000) -> list[dict]:
    rings = []

    # Use pre-computed CRA cycles exclusively — 2 DB queries total.
    # The organiser pre-computed these; they are authoritative cycle evidence.
    cycles_df = queries.fetch_cra_precomputed_cycles(2, 6)
    top_cycles = [
        row for row in cycles_df.head(200).iter_rows(named=True)
        if len([e for e in (row.get("entity_ids") or []) if e]) >= 2
        and float(row.get("total_amount") or 0.0) >= min_total_amount
    ]

    all_ids: list[int] = []
    for row in top_cycles:
        all_ids.extend(int(e) for e in (row.get("entity_ids") or []) if e)
    entity_names: dict[int, str] = {}
    if all_ids:
        for r in queries.fetch_entities_by_ids(list(set(all_ids))).iter_rows(named=True):
            entity_names[int(r["entity_id"])] = r.get("canonical_name") or str(r["entity_id"])

    for row in top_cycles:
        entity_ids = [int(e) for e in (row.get("entity_ids") or []) if e]
        amount = float(row.get("total_amount") or 0.0)
        rings.append({
            "ring_id": f"cra-cycle-{row['cycle_id']}",
            "ring_type": "round_trip",
            "entity_ids": [str(e) for e in entity_ids],
            "canonical_names": [entity_names.get(e, str(e)) for e in entity_ids],
            "shared_persons": _shared_directors(entity_ids),
            "funding_edges": [],
            "evidence": [{"source": "cra_gift", "source_row_id": row["cycle_id"],
                          "mapping_method": "authoritative", "confidence_score": 1.0}],
            "total_amount": amount,
            "datasets_touched": ["cra"],
            "flags": ["Round-trip funding (CRA-confirmed)"],
        })

    # Path 2 — director-network rings (pre-materialized via
    # scripts/precompute_rings.py). Each row is a pair of golden-record
    # entities that share a normalized director name AND have a CRA
    # qualified-donee gift flow between them >= the cache threshold.
    director_pairs = _load_director_pairs()
    if not director_pairs.is_empty():
        for row in director_pairs.iter_rows(named=True):
            amount = float(row.get("total_amount") or 0.0)
            if amount < min_total_amount:
                continue
            entity_id_a = int(row["entity_id_a"])
            entity_id_b = int(row["entity_id_b"])
            director_norm = str(row.get("director_name_normalized") or "").strip()
            ring_id = (
                f"director-pair-{director_norm.replace(' ', '-')}-"
                f"{entity_id_a}-{entity_id_b}"
            )
            rings.append({
                "ring_id": ring_id,
                "ring_type": "shared_director",
                "entity_ids": [str(entity_id_a), str(entity_id_b)],
                "canonical_names": [
                    str(row.get("name_a") or f"Entity {entity_id_a}"),
                    str(row.get("name_b") or f"Entity {entity_id_b}"),
                ],
                "shared_persons": [director_norm] if director_norm else [],
                "funding_edges": [],
                "evidence": [{
                    "source": "cra_director_funding",
                    "source_row_id": row.get("source_row_id"),
                    "mapping_method": "authoritative",
                    "confidence_score": 1.0,
                }],
                "total_amount": amount,
                "datasets_touched": ["cra"],
                "flags": ["Shared directorship across multiple funded entities"],
            })

    # Deduplicate by sorted entity set, keep highest-amount version.
    unique = {}
    for ring in rings:
        key = tuple(sorted(ring["entity_ids"]))
        if key not in unique or ring["total_amount"] > unique[key]["total_amount"]:
            unique[key] = ring

    # Filter out large national institutions that generate legitimate cycles
    # (donor-advised funds, Red Cross, United Way, Salvation Army).
    _KNOWN_NATIONAL = {
        "salvation army", "red cross", "united way", "toronto foundation",
        "vancouver foundation", "chimp", "charitable impact foundation",
        "community foundation", "canada foundation", "community chest",
    }

    def _is_national(name: str) -> bool:
        n = name.lower()
        return any(term in n for term in _KNOWN_NATIONAL)

    filtered = [
        ring for ring in unique.values()
        if not all(_is_national(name) for name in ring.get("canonical_names", []))
    ]

    return sorted(filtered, key=lambda item: item["total_amount"], reverse=True)


def analyze_neighborhood(seed_entity_ids: list[int], min_total_amount: float = 0.0) -> list[dict]:
    """Find rings that touch any of the seed entity_ids.

    Used by the agent layer when a new external row resolves to one or more
    golden-record entities — we want to know which (if any) rings those entities
    sit in, without re-running the full 5,000-cycle pipeline.
    """
    if not seed_entity_ids:
        return []
    seeds = {int(e) for e in seed_entity_ids}

    cycles_df = queries.fetch_cra_precomputed_cycles(2, 6)
    if cycles_df.is_empty():
        return []

    matching = []
    for row in cycles_df.iter_rows(named=True):
        ring_ids = [int(e) for e in (row.get("entity_ids") or []) if e]
        if len(ring_ids) < 2:
            continue
        if not (set(ring_ids) & seeds):
            continue
        amount = float(row.get("total_amount") or 0.0)
        if amount < min_total_amount:
            continue
        matching.append((row, ring_ids, amount))

    if not matching:
        return []

    all_ids: set[int] = set()
    for _, ring_ids, _ in matching:
        all_ids.update(ring_ids)
    entity_names: dict[int, str] = {}
    if all_ids:
        for r in queries.fetch_entities_by_ids(list(all_ids)).iter_rows(named=True):
            entity_names[int(r["entity_id"])] = r.get("canonical_name") or str(r["entity_id"])

    rings = []
    for row, ring_ids, amount in matching:
        rings.append({
            "ring_id": f"cra-cycle-{row['cycle_id']}",
            "entity_ids": [str(e) for e in ring_ids],
            "canonical_names": [entity_names.get(e, str(e)) for e in ring_ids],
            "shared_persons": [],
            "funding_edges": [],
            "evidence": [{
                "source": "cra_gift",
                "source_row_id": row["cycle_id"],
                "mapping_method": "authoritative",
                "confidence_score": 1.0,
            }],
            "total_amount": amount,
            "datasets_touched": ["cra"],
            "seeds_touched": sorted(seeds & set(ring_ids)),
        })
    return sorted(rings, key=lambda item: item["total_amount"], reverse=True)
