"""Pre-materialize director-network rings into data/cache/.

Director-funding-pairs query is expensive against the live DB; running it
once and caching the parquet keeps the demo fast and reproducible.

Usage:
    PYTHONPATH=. .venv/bin/python -m scripts.precompute_rings

Tunable knobs:
    --min-amount     Minimum total CRA gift flow per pair (default 100_000).
    --limit          Max director-pair rows to keep (default 200).
"""

from __future__ import annotations

import argparse
import time
from pathlib import Path

import polars as pl

from src.db import queries
from src.db.connection import get_conn


CACHE_DIR = Path("data/cache")


def fetch_director_funding_pairs_fast(min_amount: float, limit: int) -> pl.DataFrame:
    """Self-join + gift-pair join, rewritten with LEAST/GREATEST so the
    qualifying-donee join can use a hash plan instead of the OR fallback
    in queries.fetch_shared_director_funding_pairs."""
    sql = f"""
        WITH dir_entity AS (
            SELECT DISTINCT
                {queries._DIRECTOR_NAME_NORMALIZED_SQL} AS director_name_normalized,
                e.id AS entity_id,
                e.bn_root
            FROM cra.cra_directors d
            JOIN general.entity_golden_records e ON e.bn_root = left(d.bn, 9)
            WHERE COALESCE(trim(concat_ws(' ', d.first_name, d.initials, d.last_name)), '') <> ''
              AND e.status = 'active'
              AND e.id IN (
                  SELECT entity_id FROM general.entity_source_links
                  WHERE source_schema IN ('fed', 'ab')
              )
        ),
        gift_pairs AS (
            SELECT
                LEAST(src.id, dst.id) AS entity_id_a,
                GREATEST(src.id, dst.id) AS entity_id_b,
                q.total_gifts,
                concat_ws('|', q.bn, q.fpe::text, q.sequence_number::text) AS source_row_id
            FROM cra.cra_qualified_donees q
            JOIN general.entity_golden_records src ON left(q.bn, 9) = src.bn_root
            JOIN general.entity_golden_records dst ON left(q.donee_bn, 9) = dst.bn_root
            WHERE q.total_gifts > 0
              AND src.id IS NOT NULL AND dst.id IS NOT NULL
              AND src.id <> dst.id
        ),
        shared_pairs AS (
            SELECT
                a.director_name_normalized,
                a.entity_id AS entity_id_a,
                b.entity_id AS entity_id_b
            FROM dir_entity a
            JOIN dir_entity b
              ON a.director_name_normalized = b.director_name_normalized
             AND a.entity_id < b.entity_id
        )
        SELECT
            sp.director_name_normalized,
            sp.entity_id_a,
            sp.entity_id_b,
            SUM(gp.total_gifts)::float AS total_amount,
            MIN(gp.source_row_id) AS source_row_id,
            COUNT(*)::int AS gift_count
        FROM shared_pairs sp
        JOIN gift_pairs gp
          ON gp.entity_id_a = sp.entity_id_a
         AND gp.entity_id_b = sp.entity_id_b
        GROUP BY sp.director_name_normalized, sp.entity_id_a, sp.entity_id_b
        HAVING SUM(gp.total_gifts) >= %(min_amount)s
        ORDER BY total_amount DESC
        LIMIT %(limit)s
    """
    conn = get_conn()
    with conn.cursor() as cur:
        cur.execute(sql, {"min_amount": float(min_amount), "limit": int(limit)})
        rows = cur.fetchall()
    return pl.DataFrame(list(rows)) if rows else pl.DataFrame()


def enrich_with_names(df: pl.DataFrame) -> pl.DataFrame:
    if df.is_empty():
        return df
    ids = sorted({int(v) for col in ("entity_id_a", "entity_id_b") for v in df[col]})
    names = queries.fetch_entities_by_ids(ids)
    name_map = {int(r["entity_id"]): r.get("canonical_name") for r in names.iter_rows(named=True)}
    return df.with_columns(
        pl.col("entity_id_a").map_elements(lambda i: name_map.get(int(i)) or f"Entity {i}",
                                            return_dtype=pl.Utf8).alias("name_a"),
        pl.col("entity_id_b").map_elements(lambda i: name_map.get(int(i)) or f"Entity {i}",
                                            return_dtype=pl.Utf8).alias("name_b"),
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--min-amount", type=float, default=100_000.0)
    parser.add_argument("--limit", type=int, default=200)
    parser.add_argument(
        "--out",
        default=str(CACHE_DIR / "director_rings.parquet"),
    )
    args = parser.parse_args()

    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    print(f"[precompute] director-funding pairs >= ${args.min_amount:,.0f} (limit {args.limit})")
    t = time.time()
    df = fetch_director_funding_pairs_fast(args.min_amount, args.limit)
    elapsed = time.time() - t
    print(f"[precompute] director pairs: {len(df)} rows in {elapsed:.1f}s")

    if df.is_empty():
        print("[precompute] no rows — try lowering --min-amount")
        return 1

    enriched = enrich_with_names(df)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    enriched.write_parquet(out)
    print(f"[precompute] wrote {out} ({out.stat().st_size:,} bytes)")
    print(enriched.head(5))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
