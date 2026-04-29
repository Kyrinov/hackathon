"""Pre-materialize Rule R3 — director / contractor crossover.

R3 (Challenge #6): a person who is listed as a CRA T3010 director of a
charity that received a federal grant AND who is also listed as a CRA
T3010 director of an entity that received an Alberta contract or sole-
source award.

Note on coverage limits:
- Only the CHARITY side has director data in this DB (cra.cra_directors).
  If the contractor entity is itself a registered charity (or shares a
  bn_root with a registered charity), we surface the director match.
  Pure-commercial contractors that have never filed a T3010 cannot be
  cross-referenced from this dataset alone.
- AB grants are excluded — only AB contracts and AB sole-source count
  as the "contract" side, matching the OSIC procurement regime.

Output: data/cache/crossover.parquet

Usage:
    PYTHONPATH=. .venv/bin/python -m scripts.precompute_crossover
"""

from __future__ import annotations

import argparse
import time
from pathlib import Path

import polars as pl

from src.db import queries
from src.db.connection import get_conn

CACHE_PATH = Path("data/cache/crossover.parquet")


def _fetch_contractor_directors(min_contract: float) -> pl.DataFrame:
    """Step 1: contractors (AB contracts + sole-source) with their CRA directors.

    Done as one focused query so we can hand the result back to Python.
    Director name uses simple lower(trim(first || ' ' || last)) — much
    faster than the full token-sorted normalization, and good enough for
    the demo's matching purposes.
    """
    sql = """
        WITH contractor_entities AS (
            SELECT esl.entity_id, c.amount, 'ab_contract' AS contract_source
            FROM general.entity_source_links esl
            JOIN ab.ab_contracts c ON c.id = (esl.source_pk ->> 'id')::uuid
            WHERE esl.source_schema = 'ab' AND esl.source_table = 'ab_contracts'
              AND c.amount > 0
            UNION ALL
            SELECT esl.entity_id, ss.amount, 'ab_sole_source'
            FROM general.entity_source_links esl
            JOIN ab.ab_sole_source ss ON ss.id = (esl.source_pk ->> 'id')::uuid
            WHERE esl.source_schema = 'ab' AND esl.source_table = 'ab_sole_source'
              AND ss.amount > 0
        ),
        contractor_totals AS (
            SELECT entity_id,
                   SUM(amount)::float AS total_contract_amount,
                   MAX(contract_source) AS contract_source
            FROM contractor_entities
            GROUP BY entity_id
            HAVING SUM(amount) >= %(min_contract)s
        )
        SELECT DISTINCT
            ct.entity_id AS contractor_entity_id,
            ct.total_contract_amount,
            ct.contract_source,
            lower(trim(concat_ws(' ', d.first_name, d.last_name))) AS director_norm
        FROM contractor_totals ct
        JOIN general.entity_golden_records e ON e.id = ct.entity_id
        JOIN cra.cra_directors d ON left(d.bn, 9) = e.bn_root
        WHERE COALESCE(trim(concat_ws(' ', d.first_name, d.last_name)), '') <> ''
          AND length(trim(concat_ws(' ', d.first_name, d.last_name))) >= 5
    """
    conn = get_conn()
    with conn.cursor() as cur:
        cur.execute(sql, {"min_contract": float(min_contract)})
        rows = cur.fetchall()
    return pl.DataFrame(list(rows)) if rows else pl.DataFrame()


def _fetch_charities_for_directors(
    director_norms: list[str], min_grant: float
) -> pl.DataFrame:
    """Step 2: for the (small) set of contractor-director names we found
    in step 1, find which of those names also direct a charity that
    received federal grants. Filtered by name list, this query is fast.
    """
    if not director_norms:
        return pl.DataFrame()
    sql = """
        WITH grant_totals AS (
            SELECT esl.entity_id,
                   SUM(gc.agreement_value)::float AS total_grant_amount
            FROM general.entity_source_links esl
            JOIN fed.grants_contributions gc ON gc._id = (esl.source_pk ->> '_id')::int
            WHERE esl.source_schema = 'fed' AND esl.source_table = 'grants_contributions'
              AND gc.agreement_value > 0
            GROUP BY esl.entity_id
            HAVING SUM(gc.agreement_value) >= %(min_grant)s
        )
        SELECT DISTINCT
            gt.entity_id AS charity_entity_id,
            gt.total_grant_amount,
            lower(trim(concat_ws(' ', d.first_name, d.last_name))) AS director_norm
        FROM grant_totals gt
        JOIN general.entity_golden_records e ON e.id = gt.entity_id
        JOIN cra.cra_directors d ON left(d.bn, 9) = e.bn_root
        WHERE lower(trim(concat_ws(' ', d.first_name, d.last_name))) = ANY(%(names)s)
    """
    conn = get_conn()
    with conn.cursor() as cur:
        cur.execute(sql, {"min_grant": float(min_grant), "names": director_norms})
        rows = cur.fetchall()
    return pl.DataFrame(list(rows)) if rows else pl.DataFrame()


def fetch_crossover(min_grant: float, min_contract: float, limit: int) -> pl.DataFrame:
    """Two-step: contractor side first (small), then charity side filtered
    by names that appeared on the contractor side. Stitched in Python."""
    print("[crossover] step 1: contractors with CRA directors ...")
    t = time.time()
    contractors = _fetch_contractor_directors(min_contract)
    print(f"[crossover]   {len(contractors)} (contractor, director) rows in {time.time() - t:.1f}s")
    if contractors.is_empty():
        return pl.DataFrame()

    director_names = sorted({n for n in contractors["director_norm"] if n and len(n) >= 5})
    print(f"[crossover] step 2: matching {len(director_names)} director names against grant charities ...")
    t = time.time()
    charities = _fetch_charities_for_directors(director_names, min_grant)
    print(f"[crossover]   {len(charities)} (charity, director) rows in {time.time() - t:.1f}s")
    if charities.is_empty():
        return pl.DataFrame()

    joined = contractors.join(charities, on="director_norm", how="inner")
    joined = joined.filter(pl.col("contractor_entity_id") != pl.col("charity_entity_id"))
    if joined.is_empty():
        return pl.DataFrame()

    grouped = (
        joined.group_by(["charity_entity_id", "contractor_entity_id"])
        .agg(
            pl.col("director_norm").unique().alias("shared_directors"),
            pl.col("total_grant_amount").max(),
            pl.col("total_contract_amount").max(),
            pl.col("contract_source").max(),
        )
        .sort(["total_contract_amount", "total_grant_amount"], descending=True)
        .head(limit)
    )
    return grouped


def enrich_with_names(df: pl.DataFrame) -> pl.DataFrame:
    if df.is_empty():
        return df
    ids = sorted(
        {int(v)
         for col in ("charity_entity_id", "contractor_entity_id")
         for v in df[col]}
    )
    names = queries.fetch_entities_by_ids(ids)
    name_map = {
        int(r["entity_id"]): r.get("canonical_name")
        for r in names.iter_rows(named=True)
    }
    return df.with_columns(
        pl.col("charity_entity_id").map_elements(
            lambda i: name_map.get(int(i)) or f"Entity {i}",
            return_dtype=pl.Utf8,
        ).alias("charity_name"),
        pl.col("contractor_entity_id").map_elements(
            lambda i: name_map.get(int(i)) or f"Entity {i}",
            return_dtype=pl.Utf8,
        ).alias("contractor_name"),
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--min-grant", type=float, default=25_000.0,
                        help="Minimum total federal grants to charity (default 25k)")
    parser.add_argument("--min-contract", type=float, default=10_000.0,
                        help="Minimum total Alberta contracts to contractor (default 10k)")
    parser.add_argument("--limit", type=int, default=200)
    args = parser.parse_args()

    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    print(
        f"[crossover] grant >= ${args.min_grant:,.0f} ; "
        f"contract >= ${args.min_contract:,.0f} ; limit {args.limit}"
    )
    t = time.time()
    df = fetch_crossover(args.min_grant, args.min_contract, args.limit)
    elapsed = time.time() - t
    print(f"[crossover] {len(df)} rows in {elapsed:.1f}s")

    if df.is_empty():
        print("[crossover] no rows — try lowering thresholds")
        return 1

    enriched = enrich_with_names(df)
    enriched.write_parquet(CACHE_PATH)
    print(f"[crossover] wrote {CACHE_PATH} ({CACHE_PATH.stat().st_size:,} bytes)")
    print(enriched.select([
        "charity_name", "contractor_name", "shared_directors",
        "total_grant_amount", "total_contract_amount", "contract_source",
    ]).head(5))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
