from __future__ import annotations

import hashlib

import polars as pl


def _hash(seed: str) -> str:
    return hashlib.sha256(seed.encode()).hexdigest()[:16]


def _ev(source: str, row_id: str, method: str = "authoritative", confidence: float = 1.0) -> dict:
    return {
        "source": source,
        "source_row_id": row_id,
        "mapping_method": method,
        "confidence_score": confidence,
    }


def generate_demo_graph() -> tuple[pl.DataFrame, pl.DataFrame, list[dict]]:
    entities = pl.DataFrame(
        [
            {
                "entity_id": "1001",
                "canonical_name": "Northern Pathways Community Society",
                "entity_type": "charity",
                "datasets": ["cra", "fed"],
                "aliases": ["Northern Pathways Society"],
                "total_score": 0.85,
                "flags": ["Round-trip funding (CRA-confirmed)", "Concentrated funding sources"],
            },
            {
                "entity_id": "1002",
                "canonical_name": "Boreal Futures Network",
                "entity_type": "charity",
                "datasets": ["cra"],
                "aliases": ["Boreal Futures Network Inc."],
                "total_score": 0.70,
                "flags": ["Round-trip funding (CRA-confirmed)"],
            },
            {
                "entity_id": "1003",
                "canonical_name": "Harwick Consulting Group",
                "entity_type": "business",
                "datasets": ["ab"],
                "aliases": ["Harwick Consulting Group Ltd."],
                "total_score": 0.65,
                "flags": ["Sole-source pattern"],
            },
            {
                "entity_id": "1004",
                "canonical_name": "Prairie Skills Initiative",
                "entity_type": "charity",
                "datasets": ["cra", "ab"],
                "aliases": [],
                "total_score": 0.55,
                "flags": ["Director controls multiple funded entities"],
            },
            {
                "entity_id": "1005",
                "canonical_name": "Prairie Skills Foundation",
                "entity_type": "charity",
                "datasets": ["cra"],
                "aliases": ["PS Foundation"],
                "total_score": 0.45,
                "flags": ["Funding flows to related entity"],
            },
            {
                "entity_id": "person:jennifer harwick",
                "canonical_name": "jennifer harwick",
                "entity_type": "person",
                "datasets": ["cra"],
                "aliases": [],
                "total_score": 0.0,
                "flags": [],
            },
            {
                "entity_id": "person:marc tremblay cote",
                "canonical_name": "marc tremblay cote",
                "entity_type": "person",
                "datasets": ["cra"],
                "aliases": [],
                "total_score": 0.0,
                "flags": [],
            },
            {
                "entity_id": "source:fed_grant:88001",
                "canonical_name": "Federal Grants Disclosure",
                "entity_type": "gov",
                "datasets": ["fed"],
                "aliases": [],
                "total_score": 0.0,
                "flags": [],
            },
        ]
    )

    edges = pl.DataFrame(
        [
            {
                "from_entity_id": "person:jennifer harwick",
                "to_entity_id": "1001",
                "source": "cra_director",
                "amount": 0.0,
                "date": "2023",
                "source_row_id": "123456789|2023-12-31|1",
                "mapping_method": "authoritative",
                "confidence_score": 1.0,
            },
            {
                "from_entity_id": "person:jennifer harwick",
                "to_entity_id": "1002",
                "source": "cra_director",
                "amount": 0.0,
                "date": "2023",
                "source_row_id": "234567890|2023-12-31|1",
                "mapping_method": "authoritative",
                "confidence_score": 1.0,
            },
            {
                "from_entity_id": "1001",
                "to_entity_id": "1002",
                "source": "cra_gift",
                "amount": 142000.0,
                "date": "2023-12-31",
                "source_row_id": "123456789|2023-12-31|4",
                "mapping_method": "authoritative",
                "confidence_score": 1.0,
            },
            {
                "from_entity_id": "1002",
                "to_entity_id": "1001",
                "source": "cra_gift",
                "amount": 37000.0,
                "date": "2023-12-31",
                "source_row_id": "234567890|2023-12-31|7",
                "mapping_method": "authoritative",
                "confidence_score": 1.0,
            },
            {
                "from_entity_id": "source:fed_grant:88001",
                "to_entity_id": "1001",
                "source": "fed_grant",
                "amount": 2100000.0,
                "date": "2023-04-01",
                "source_row_id": "88001",
                "mapping_method": "splink_sonnet_review",
                "confidence_score": 0.97,
            },
            {
                "from_entity_id": "person:marc tremblay cote",
                "to_entity_id": "1004",
                "source": "cra_director",
                "amount": 0.0,
                "date": "2022",
                "source_row_id": "345678901|2022-12-31|2",
                "mapping_method": "authoritative",
                "confidence_score": 1.0,
            },
            {
                "from_entity_id": "person:marc tremblay cote",
                "to_entity_id": "1005",
                "source": "cra_director",
                "amount": 0.0,
                "date": "2022",
                "source_row_id": "456789012|2022-12-31|3",
                "mapping_method": "authoritative",
                "confidence_score": 1.0,
            },
            {
                "from_entity_id": "1004",
                "to_entity_id": "1005",
                "source": "ab_grant",
                "amount": 79000.0,
                "date": "2022-06-01",
                "source_row_id": "5541",
                "mapping_method": "splink_sonnet_review",
                "confidence_score": 0.92,
            },
            {
                "from_entity_id": "1001",
                "to_entity_id": "1003",
                "source": "ab_sole_source",
                "amount": 387000.0,
                "date": "2023-05-15",
                "source_row_id": "3e7e0bb0-f1ef-4a19-b81a-baf6d7211001",
                "mapping_method": "splink_sonnet_review",
                "confidence_score": 0.91,
            },
        ]
    )

    rings = [
        {
            "ring_id": "demo-cra-cycle-1",
            "entity_ids": ["1001", "1002"],
            "canonical_names": [
                "Northern Pathways Community Society",
                "Boreal Futures Network",
            ],
            "shared_persons": ["jennifer harwick"],
            "funding_edges": [
                {
                    "from": "1001",
                    "to": "1002",
                    "amount": 142000.0,
                    "date": "2023-12-31",
                    "source": "cra_gift",
                    "source_row_id": "123456789|2023-12-31|4",
                },
                {
                    "from": "1002",
                    "to": "1001",
                    "amount": 37000.0,
                    "date": "2023-12-31",
                    "source": "cra_gift",
                    "source_row_id": "234567890|2023-12-31|7",
                },
            ],
            "evidence": [
                _ev("cra_gift", "123456789|2023-12-31|4"),
                _ev("cra_gift", "234567890|2023-12-31|7"),
                _ev("cra_director", "123456789|2023-12-31|1"),
                _ev("fed_grant", "88001", "splink_sonnet_review", 0.97),
            ],
            "total_amount": 179000.0,
            "total_score": 1.0,
            "flags": ["Round-trip funding (CRA-confirmed)", "Concentrated funding sources"],
            "datasets_touched": ["cra", "fed"],
        },
        {
            "ring_id": "demo-shared-director-1",
            "entity_ids": ["1004", "1005"],
            "canonical_names": ["Prairie Skills Initiative", "Prairie Skills Foundation"],
            "shared_persons": ["marc tremblay cote"],
            "funding_edges": [
                {
                    "from": "1004",
                    "to": "1005",
                    "amount": 79000.0,
                    "date": "2022-06-01",
                    "source": "ab_grant",
                    "source_row_id": "5541",
                }
            ],
            "evidence": [
                _ev("cra_director", "345678901|2022-12-31|2"),
                _ev("cra_director", "456789012|2022-12-31|3"),
                _ev("ab_grant", "5541", "splink_sonnet_review", 0.92),
            ],
            "total_amount": 79000.0,
            "total_score": 0.55,
            "flags": ["Director controls multiple funded entities"],
            "datasets_touched": ["cra", "ab"],
        },
    ]
    return entities, edges, rings
