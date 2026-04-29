from __future__ import annotations

import polars as pl

from src.graph.builder import build_cra_cycle_graph, build_full_ring_graph, find_related_party_rings
from src.score.scorer import score_all_top_n, top_rings


def _patch_queries(monkeypatch):
    from src.db import queries

    entities = {
        1001: {
            "entity_id": 1001,
            "canonical_name": "Northern Pathways Community Society",
            "entity_type": "charity",
            "datasets": ["cra", "fed"],
            "aliases": ["Northern Pathways"],
        },
        1002: {
            "entity_id": 1002,
            "canonical_name": "Boreal Futures Network",
            "entity_type": "charity",
            "datasets": ["cra"],
            "aliases": ["Boreal Futures Network Inc."],
        },
        1003: {
            "entity_id": 1003,
            "canonical_name": "Harwick Consulting Group",
            "entity_type": "business",
            "datasets": ["ab"],
            "aliases": [],
        },
    }

    director_rows = {
        1001: [
            {
                "director_name_normalized": "jennifer harwick",
                "t3010_year": 2023,
                "source_row_id": "123456789|2023-12-31|1",
            }
        ],
        1002: [
            {
                "director_name_normalized": "jennifer harwick",
                "t3010_year": 2023,
                "source_row_id": "234567890|2023-12-31|1",
            }
        ],
        1003: [
            {
                "director_name_normalized": "jennifer harwick",
                "t3010_year": 2023,
                "source_row_id": "345678901|2023-12-31|1",
            }
        ],
    }

    funding_rows = {
        1001: [
            {
                "from_entity_id": 1001,
                "to_entity_id": 1002,
                "amount": 142000.0,
                "date": "2023-12-31",
                "source": "cra_gift",
                "source_row_id": "123456789|2023-12-31|4",
                "mapping_method": "authoritative",
                "confidence_score": 1.0,
            },
            {
                "from_entity_id": None,
                "to_entity_id": 1001,
                "amount": 2100000.0,
                "date": "2023-04-01",
                "source": "fed_grant",
                "source_row_id": "88001",
                "mapping_method": "splink_sonnet_review",
                "confidence_score": 0.97,
            },
        ],
        1002: [
            {
                "from_entity_id": 1002,
                "to_entity_id": 1001,
                "amount": 37000.0,
                "date": "2023-12-31",
                "source": "cra_gift",
                "source_row_id": "234567890|2023-12-31|7",
                "mapping_method": "authoritative",
                "confidence_score": 1.0,
            }
        ],
        1003: [
            {
                "from_entity_id": 1001,
                "to_entity_id": 1003,
                "amount": 387000.0,
                "date": "2023-05-15",
                "source": "ab_sole_source",
                "source_row_id": "3e7e0bb0-f1ef-4a19-b81a-baf6d7211001",
                "mapping_method": "splink_sonnet_review",
                "confidence_score": 0.91,
            }
        ],
    }

    monkeypatch.setattr(
        queries,
        "fetch_entities_by_ids",
        lambda ids: pl.DataFrame([entities[int(entity_id)] for entity_id in ids if int(entity_id) in entities]),
    )
    monkeypatch.setattr(
        queries,
        "fetch_directors_for_org",
        lambda entity_id: pl.DataFrame(director_rows.get(int(entity_id), [])),
    )
    monkeypatch.setattr(
        queries,
        "fetch_funding_edges",
        lambda entity_id, direction="both": pl.DataFrame(funding_rows.get(int(entity_id), [])),
    )
    monkeypatch.setattr(
        queries,
        "fetch_ring_funding_edges",
        lambda entity_ids: pl.DataFrame(
            [
                row
                for entity_id in entity_ids
                for row in funding_rows.get(int(entity_id), [])
                if row.get("from_entity_id") in {int(item) for item in entity_ids}
                and row.get("to_entity_id") in {int(item) for item in entity_ids}
            ]
        ),
    )
    monkeypatch.setattr(
        queries,
        "fetch_cra_cycle_edges",
        lambda cycle_id: pl.DataFrame(
            [
                {
                    "from_entity_id": 1001,
                    "to_entity_id": 1002,
                    "cycle_id": str(cycle_id),
                    "edge_order": 1,
                    "src_bn": "123456789",
                    "dst_bn": "234567890",
                    "amount": 142000.0,
                    "date": "2023",
                    "source": "cra_gift",
                    "source_row_id": f"{cycle_id}|1",
                    "mapping_method": "authoritative",
                    "confidence_score": 1.0,
                },
                {
                    "from_entity_id": 1002,
                    "to_entity_id": 1001,
                    "cycle_id": str(cycle_id),
                    "edge_order": 2,
                    "src_bn": "234567890",
                    "dst_bn": "123456789",
                    "amount": 37000.0,
                    "date": "2023",
                    "source": "cra_gift",
                    "source_row_id": f"{cycle_id}|2",
                    "mapping_method": "authoritative",
                    "confidence_score": 1.0,
                },
            ]
        ),
    )
    monkeypatch.setattr(
        queries,
        "fetch_cra_precomputed_cycles",
        lambda min_hops=2, max_hops=6: pl.DataFrame(
            [
                {
                    "cycle_id": "1",
                    "entity_ids": [1001, 1002],
                    "total_amount": 179000.0,
                    "hop_count": 2,
                    "fiscal_years": [2023, 2023],
                }
            ]
        ),
    )
    monkeypatch.setattr(
        queries,
        "fetch_shared_director_candidates",
        lambda min_orgs=3: pl.DataFrame(
            [
                {
                    "director_name_normalized": "jennifer harwick",
                    "entity_ids": [1001, 1002, 1003],
                    "org_count": 3,
                }
            ]
        ),
    )
    monkeypatch.setattr(
        queries,
        "fetch_fed_concentration",
        lambda entity_id: pl.DataFrame([{"hhi_score": 0.7, "dept_count": 1, "top_dept_share": 1.0}])
        if int(entity_id) == 1001
        else pl.DataFrame([{"hhi_score": 0.0, "dept_count": 0, "top_dept_share": 0.0}]),
    )
    monkeypatch.setattr(
        queries,
        "fetch_ab_sole_source_flags",
        lambda entity_id=None: pl.DataFrame(
            [
                {
                    "entity_id": 1003,
                    "vendor_name": "Harwick Consulting Group",
                    "contract_count": 2,
                    "total_amount": 387000.0,
                    "repeat_vendor_flag": True,
                    "splitting_flag": False,
                }
            ]
        )
        if entity_id is None or int(entity_id) == 1003
        else pl.DataFrame(),
    )
    monkeypatch.setattr(queries, "fetch_related_entities", lambda entity_id: pl.DataFrame())


def test_related_party_rings_from_fixture(monkeypatch):
    _patch_queries(monkeypatch)

    rings = find_related_party_rings(min_total_amount=50_000)
    cycle_ring = next(ring for ring in rings if ring["ring_id"] == "cra-cycle-1")

    assert rings
    assert cycle_ring["shared_persons"] == ["jennifer harwick"]
    assert set(cycle_ring["entity_ids"]) == {"1001", "1002"}
    assert cycle_ring["total_amount"] == 179000.0
    assert any(item["source"] == "cra_gift" for item in cycle_ring["evidence"])


def test_graph_and_scoring_from_fixture(monkeypatch):
    _patch_queries(monkeypatch)

    graph = build_full_ring_graph(["1001", "1002"])
    cycle_graph = build_cra_cycle_graph("1")
    ranked = top_rings(5)
    scored = score_all_top_n(10)

    assert graph.number_of_nodes() >= 3
    assert graph.has_edge("1001", "1002")
    assert cycle_graph.has_edge("1001", "1002")
    assert cycle_graph.has_edge("1002", "1001")
    assert ranked[0]["total_score"] > 0
    assert "Round-trip funding (CRA-confirmed)" in ranked[0]["flags"]
    assert not scored.is_empty()
    assert scored["total_score"].max() > 0
