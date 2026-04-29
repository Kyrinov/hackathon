"""Pre-warm the Streamlit demo cache with one big query.

The original top_rings(20) fired 600+ per-ring director lookups, which was
the hot path bottleneck against the remote Render Postgres. This version:
  1. Pulls the top N CRA cycles in one query.
  2. Pulls the union of shared directors for all members in one query.
  3. Stitches them in Python and writes the JSON cache.

Run before every demo session:
    PYTHONPATH=. .venv/bin/python -m scripts.prewarm
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import polars as pl

from src.db import queries
from src.db.connection import get_conn

CACHE_PATH = Path("data/cache/top_rings.json")


def fetch_top_cycles(limit: int = 30) -> pl.DataFrame:
    """Top CRA cycles ranked by total flow (single query, ~1s)."""
    sql = """
        SELECT
            l.id::text AS cycle_id,
            ARRAY(
                SELECT e.id
                FROM unnest(l.path_bns) WITH ORDINALITY AS u(bn, ord)
                JOIN general.entity_golden_records e
                    ON e.bn_root = left(u.bn, 9)
                WHERE e.status = 'active'
                ORDER BY u.ord
            ) AS entity_ids,
            COALESCE(l.total_flow, l.bottleneck_amt, 0) AS total_amount,
            l.hops AS hop_count
        FROM cra.loops l
        WHERE l.hops BETWEEN 2 AND 6
        ORDER BY COALESCE(l.total_flow, l.bottleneck_amt, 0) DESC NULLS LAST
        LIMIT %(limit)s
    """
    conn = get_conn()
    with conn.cursor() as cur:
        cur.execute(sql, {"limit": int(limit)})
        rows = cur.fetchall()
    return pl.DataFrame(list(rows)) if rows else pl.DataFrame()


def fetch_shared_directors_bulk(entity_ids: list[int]) -> dict[int, list[str]]:
    """Map each entity_id to its normalized director names — one query."""
    if not entity_ids:
        return {}
    sql = f"""
        SELECT
            e.id AS entity_id,
            ARRAY_AGG(DISTINCT {queries._DIRECTOR_NAME_NORMALIZED_SQL}) AS directors
        FROM general.entity_golden_records e
        JOIN cra.cra_directors d ON left(d.bn, 9) = e.bn_root
        WHERE e.id = ANY(%(ids)s)
          AND COALESCE(trim(concat_ws(' ', d.first_name, d.initials, d.last_name)), '') <> ''
        GROUP BY e.id
    """
    conn = get_conn()
    with conn.cursor() as cur:
        cur.execute(sql, {"ids": [int(e) for e in entity_ids]})
        rows = cur.fetchall()
    out: dict[int, list[str]] = {}
    for row in rows:
        eid = int(row["entity_id"])
        names = [n for n in (row.get("directors") or []) if n and len(str(n)) > 4]
        out[eid] = sorted(set(names))
    return out


def main() -> int:
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)

    print("[prewarm] fetching top 80 CRA cycles ...")
    t = time.time()
    cycles_df = fetch_top_cycles(80)
    print(f"[prewarm] {len(cycles_df)} cycles in {time.time() - t:.1f}s")
    if cycles_df.is_empty():
        print("[prewarm] no cycles — DB unreachable or empty")
        return 1

    all_entity_ids = sorted(
        {int(eid) for row in cycles_df.iter_rows(named=True)
         for eid in (row.get("entity_ids") or []) if eid}
    )
    print(f"[prewarm] resolving {len(all_entity_ids)} entity names + directors ...")
    t = time.time()
    name_df = queries.fetch_entities_by_ids(all_entity_ids)
    name_map = {
        int(row["entity_id"]): row.get("canonical_name") or f"Entity {row['entity_id']}"
        for row in name_df.iter_rows(named=True)
    }
    print(f"[prewarm] names resolved in {time.time() - t:.1f}s")

    t = time.time()
    director_map = fetch_shared_directors_bulk(all_entity_ids)
    print(f"[prewarm] directors fetched in {time.time() - t:.1f}s")

    # Filter out big-name national institutions that produce noisy cycles.
    KNOWN_NATIONAL = {
        "salvation army", "red cross", "united way", "toronto foundation",
        "vancouver foundation", "chimp", "charitable impact foundation",
        "community foundation", "canada foundation", "community chest",
    }

    def _is_national(name: str) -> bool:
        n = (name or "").lower()
        return any(term in n for term in KNOWN_NATIONAL)

    rings = []
    for row in cycles_df.iter_rows(named=True):
        entity_ids = [int(e) for e in (row.get("entity_ids") or []) if e]
        if len(entity_ids) < 2:
            continue
        canonical_names = [name_map.get(e, f"Entity {e}") for e in entity_ids]
        if all(_is_national(name) for name in canonical_names):
            continue

        # Shared directors = anyone who appears on at least 2 of the ring's
        # entities. Stricter "full-intersection" is too restrictive on long
        # cycles — most legitimate director-network signals are pairwise
        # within a ring of 4-6 charities.
        director_counts: dict[str, int] = {}
        for e in entity_ids:
            for d in director_map.get(e, []):
                director_counts[d] = director_counts.get(d, 0) + 1
        shared = sorted(d for d, c in director_counts.items() if c >= 2)

        amount = float(row.get("total_amount") or 0.0)
        score = 0.4
        if amount >= 1_000_000:
            score += 0.3
        elif amount >= 500_000:
            score += 0.2
        elif amount >= 100_000:
            score += 0.1
        if shared:
            score += 0.3
        score = min(score, 1.0)

        rings.append({
            "ring_id": f"cra-cycle-{row['cycle_id']}",
            "ring_type": "round_trip",
            "entity_ids": [str(e) for e in entity_ids],
            "canonical_names": canonical_names,
            "shared_persons": shared,
            "funding_edges": [],
            "evidence": [{
                "source": "cra_gift",
                "source_row_id": row["cycle_id"],
                "mapping_method": "authoritative",
                "confidence_score": 1.0,
            }],
            "total_amount": amount,
            "total_score": score,
            "datasets_touched": ["cra"],
            "flags": (
                ["Round-trip funding (CRA-confirmed)", "Director controls multiple funded entities"]
                if shared else ["Round-trip funding (CRA-confirmed)"]
            ),
        })

    # Sort: shared-director rings on top, then by amount.
    rings.sort(key=lambda r: (bool(r.get("shared_persons")), r["total_amount"]), reverse=True)
    rings = rings[:30]

    by_type = {}
    with_shared = 0
    for r in rings:
        by_type[r["ring_type"]] = by_type.get(r["ring_type"], 0) + 1
        if r.get("shared_persons"):
            with_shared += 1

    print(f"[prewarm] kept {len(rings)} rings: {by_type}, {with_shared} with shared director")
    CACHE_PATH.write_text(json.dumps(rings, default=str, ensure_ascii=False))
    print(f"[prewarm] wrote {CACHE_PATH} ({CACHE_PATH.stat().st_size:,} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
