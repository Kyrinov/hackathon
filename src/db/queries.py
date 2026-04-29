from __future__ import annotations

import functools
import json
from collections.abc import Sequence
from typing import Any

import polars as pl
from psycopg import sql
from psycopg.errors import UndefinedColumn, UndefinedTable

from .connection import get_conn


def _df(rows: Sequence[dict[str, Any]]) -> pl.DataFrame:
    return pl.DataFrame(list(rows)) if rows else pl.DataFrame()


def _query(query: str, params: dict[str, Any] | None = None) -> pl.DataFrame:
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(query, params or {})
            return _df(cur.fetchall())
    except (UndefinedTable, UndefinedColumn):
        return pl.DataFrame()


def _one(query: str, params: dict[str, Any] | None = None) -> dict[str, Any] | None:
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(query, params or {})
            return cur.fetchone()
    except (UndefinedTable, UndefinedColumn):
        return None


@functools.lru_cache(maxsize=1)
def _status_column_exists() -> bool:
    try:
        conn = get_conn()
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT 1 FROM information_schema.columns
                WHERE table_schema = 'general'
                  AND table_name = 'entity_golden_records'
                  AND column_name = 'status'
                LIMIT 1
                """
            )
            return cur.fetchone() is not None
    except Exception:
        return False


def _status_and(alias: str = "e") -> str:
    """SQL fragment to AND into a WHERE clause; empty if column missing."""
    return f"AND {alias}.status = 'active'" if _status_column_exists() else ""


def _status_where(alias: str = "e") -> str:
    """SQL fragment for use as a standalone WHERE; 'TRUE' if column missing."""
    return f"{alias}.status = 'active'" if _status_column_exists() else "TRUE"


@functools.lru_cache(maxsize=1)
def _fed_grants_relation() -> str:
    """Returns the FED grants relation that deduplicates amendments.

    Prefers fed.vw_agreement_current if present; otherwise an inline DISTINCT ON
    subquery on fed.grants_contributions to take the latest amendment per ref_number.
    """
    try:
        conn = get_conn()
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT 1 FROM information_schema.views
                WHERE table_schema = 'fed' AND table_name = 'vw_agreement_current'
                LIMIT 1
                """
            )
            if cur.fetchone() is not None:
                return "fed.vw_agreement_current"
    except Exception:
        pass
    return (
        "(SELECT DISTINCT ON (ref_number) * "
        "FROM fed.grants_contributions "
        "ORDER BY ref_number, amendment_number DESC NULLS LAST)"
    )


# Token-sorted, punctuation-stripped, initials-dropped director name expression.
# Handles "Last, First" reversals and middle initials. Used in any SQL that
# normalizes cra.cra_directors rows so SQL-side normalization stays consistent
# across fetch_directors_for_org and fetch_shared_director_candidates.
_DIRECTOR_NAME_NORMALIZED_SQL = """array_to_string(
            ARRAY(
                SELECT DISTINCT t
                FROM unnest(
                    string_to_array(
                        regexp_replace(
                            lower(coalesce(d.first_name,'') || ' ' || coalesce(d.last_name,'')),
                            '[^a-z ]', '', 'g'
                        ),
                        ' '
                    )
                ) AS t
                WHERE t <> '' AND length(t) > 1
                ORDER BY 1
            ),
            ' '
        )"""


def fetch_golden_records(limit=None, dataset_filter=None) -> pl.DataFrame:
    """Source: general.entity_golden_records joined to general.entity_source_links."""
    query = f"""
        SELECT
            e.id AS entity_id,
            e.canonical_name,
            COALESCE(e.entity_type, 'unknown') AS entity_type,
            e.bn_root,
            COALESCE(e.bn_variants, ARRAY[]::text[]) AS bn_variants,
            COALESCE(e.aliases, '[]'::jsonb) AS aliases,
            COALESCE(e.dataset_sources, ARRAY[]::text[]) AS datasets,
            COALESCE(e.source_link_count, COUNT(esl.id)::int, 0) AS source_link_count
        FROM general.entity_golden_records e
        LEFT JOIN general.entity_source_links esl ON esl.entity_id = e.id
        WHERE {_status_where('e')}
          AND (%(dataset_filter)s::text IS NULL OR %(dataset_filter)s = ANY(e.dataset_sources))
        GROUP BY e.id
        ORDER BY source_link_count DESC, e.id
        LIMIT COALESCE(%(limit)s, 1000000)
    """
    return _query(query, {"limit": limit, "dataset_filter": dataset_filter})


def fetch_directors_for_org(entity_id) -> pl.DataFrame:
    """Source: cra.cra_directors through general.entity_golden_records.bn_root."""
    query = f"""
        SELECT DISTINCT
            {_DIRECTOR_NAME_NORMALIZED_SQL} AS director_name_normalized,
            EXTRACT(year FROM d.fpe)::int AS t3010_year,
            concat_ws('|', d.bn, d.fpe::text, d.sequence_number::text) AS source_row_id
        FROM general.entity_golden_records e
        JOIN cra.cra_directors d ON left(d.bn, 9) = e.bn_root
        WHERE e.id = %(entity_id)s
          AND COALESCE(trim(concat_ws(' ', d.first_name, d.initials, d.last_name)), '') <> ''
        ORDER BY t3010_year DESC NULLS LAST, director_name_normalized
    """
    return _query(query, {"entity_id": int(entity_id)})


def fetch_funding_edges(entity_id, direction: str = "both") -> pl.DataFrame:
    """Source: cra.cra_qualified_donees, fed.grants_contributions, ab.ab_grants/contracts/sole_source."""
    entity_id = int(entity_id)
    rows = []
    if direction in {"both", "out"}:
        rows.append(
            _query(
                """
                SELECT
                    src.id AS from_entity_id,
                    dst.id AS to_entity_id,
                    q.total_gifts AS amount,
                    q.fpe::date AS date,
                    'cra_gift' AS source,
                    concat_ws('|', q.bn, q.fpe::text, q.sequence_number::text) AS source_row_id,
                    'authoritative' AS mapping_method,
                    1.0::float AS confidence_score
                FROM general.entity_golden_records src
                JOIN cra.cra_qualified_donees q ON left(q.bn, 9) = src.bn_root
                LEFT JOIN general.entity_golden_records dst ON left(q.donee_bn, 9) = dst.bn_root
                WHERE src.id = %(entity_id)s
                  AND q.donee_bn IS NOT NULL
                  AND q.total_gifts > 0
                """,
                {"entity_id": entity_id},
            )
        )
    if direction in {"both", "in"}:
        rows.append(
            _query(
                """
                SELECT
                    src.id AS from_entity_id,
                    dst.id AS to_entity_id,
                    q.total_gifts AS amount,
                    q.fpe::date AS date,
                    'cra_gift' AS source,
                    concat_ws('|', q.bn, q.fpe::text, q.sequence_number::text) AS source_row_id,
                    'authoritative' AS mapping_method,
                    1.0::float AS confidence_score
                FROM general.entity_golden_records dst
                JOIN cra.cra_qualified_donees q ON left(q.donee_bn, 9) = dst.bn_root
                LEFT JOIN general.entity_golden_records src ON left(q.bn, 9) = src.bn_root
                WHERE dst.id = %(entity_id)s
                  AND q.total_gifts > 0
                """,
                {"entity_id": entity_id},
            )
        )
    if direction in {"both", "in"}:
        rows.extend(
            [
                _query(
                    f"""
                    SELECT
                        NULL::int AS from_entity_id,
                        esl.entity_id AS to_entity_id,
                        gc.agreement_value AS amount,
                        gc.agreement_start_date AS date,
                        CASE WHEN gc.agreement_type = 'C' THEN 'fed_contribution' ELSE 'fed_grant' END AS source,
                        gc._id::text AS source_row_id,
                        esl.match_method AS mapping_method,
                        COALESCE(esl.match_confidence, 1.0)::float AS confidence_score
                    FROM general.entity_source_links esl
                    JOIN {_fed_grants_relation()} gc ON gc._id = (esl.source_pk ->> '_id')::int
                    WHERE esl.entity_id = %(entity_id)s
                      AND esl.source_schema = 'fed'
                      AND esl.source_table = 'grants_contributions'
                      AND gc.agreement_value > 0
                    """,
                    {"entity_id": entity_id},
                ),
                _query(
                    """
                    SELECT
                        NULL::int AS from_entity_id,
                        esl.entity_id AS to_entity_id,
                        g.amount,
                        g.payment_date::date AS date,
                        'ab_grant' AS source,
                        g.id::text AS source_row_id,
                        esl.match_method AS mapping_method,
                        COALESCE(esl.match_confidence, 1.0)::float AS confidence_score
                    FROM general.entity_source_links esl
                    JOIN ab.ab_grants g ON g.id = (esl.source_pk ->> 'id')::int
                    WHERE esl.entity_id = %(entity_id)s
                      AND esl.source_schema = 'ab'
                      AND esl.source_table = 'ab_grants'
                      AND g.amount > 0
                    """,
                    {"entity_id": entity_id},
                ),
                _query(
                    """
                    SELECT
                        NULL::int AS from_entity_id,
                        esl.entity_id AS to_entity_id,
                        c.amount,
                        NULL::date AS date,
                        'ab_contract' AS source,
                        c.id::text AS source_row_id,
                        esl.match_method AS mapping_method,
                        COALESCE(esl.match_confidence, 1.0)::float AS confidence_score
                    FROM general.entity_source_links esl
                    JOIN ab.ab_contracts c ON c.id = (esl.source_pk ->> 'id')::uuid
                    WHERE esl.entity_id = %(entity_id)s
                      AND esl.source_schema = 'ab'
                      AND esl.source_table = 'ab_contracts'
                      AND c.amount > 0
                    """,
                    {"entity_id": entity_id},
                ),
                _query(
                    """
                    SELECT
                        NULL::int AS from_entity_id,
                        esl.entity_id AS to_entity_id,
                        ss.amount,
                        ss.start_date AS date,
                        'ab_sole_source' AS source,
                        ss.id::text AS source_row_id,
                        esl.match_method AS mapping_method,
                        COALESCE(esl.match_confidence, 1.0)::float AS confidence_score
                    FROM general.entity_source_links esl
                    JOIN ab.ab_sole_source ss ON ss.id = (esl.source_pk ->> 'id')::uuid
                    WHERE esl.entity_id = %(entity_id)s
                      AND esl.source_schema = 'ab'
                      AND esl.source_table = 'ab_sole_source'
                      AND ss.amount > 0
                    """,
                    {"entity_id": entity_id},
                ),
            ]
        )

    frames = [frame for frame in rows if not frame.is_empty()]
    return pl.concat(frames, how="diagonal_relaxed") if frames else pl.DataFrame()


def fetch_cra_precomputed_cycles(min_hops: int = 2, max_hops: int = 6) -> pl.DataFrame:
    """Source: cra.loops with entity mapping through general.entity_golden_records.bn_root."""
    query = f"""
        SELECT
            l.id::text AS cycle_id,
            ARRAY(
                SELECT e.id
                FROM unnest(l.path_bns) WITH ORDINALITY AS u(bn, ord)
                JOIN general.entity_golden_records e ON e.bn_root = left(u.bn, 9)
                WHERE {_status_where('e')}
                ORDER BY u.ord
            ) AS entity_ids,
            COALESCE(l.total_flow, l.bottleneck_amt, 0) AS total_amount,
            l.hops AS hop_count,
            ARRAY[l.min_year, l.max_year] AS fiscal_years
        FROM cra.loops l
        WHERE l.hops BETWEEN %(min_hops)s AND %(max_hops)s
        ORDER BY COALESCE(l.total_flow, l.bottleneck_amt, 0) DESC NULLS LAST
        LIMIT 5000
    """
    return _query(query, {"min_hops": min_hops, "max_hops": max_hops})


def fetch_cra_cycle_edges(cycle_id: int | str) -> pl.DataFrame:
    """Source: cra.loops path_bns joined to cra.loop_edges for display."""
    query = f"""
        WITH loop AS (
            SELECT id, path_bns
            FROM cra.loops
            WHERE id = %(cycle_id)s
        ), pairs AS (
            SELECT
                loop.id,
                u.ord::int AS edge_order,
                left(u.bn, 9) AS src_bn,
                left(COALESCE(loop.path_bns[u.ord::int + 1], loop.path_bns[1]), 9) AS dst_bn
            FROM loop
            CROSS JOIN LATERAL unnest(loop.path_bns) WITH ORDINALITY AS u(bn, ord)
        )
        SELECT
            src.id AS from_entity_id,
            dst.id AS to_entity_id,
            pairs.id::text AS cycle_id,
            pairs.edge_order,
            pairs.src_bn,
            pairs.dst_bn,
            COALESCE(le.total_amt, 0) AS amount,
            COALESCE(le.max_year::text, le.min_year::text, '') AS date,
            'cra_gift' AS source,
            concat_ws('|', pairs.id::text, pairs.edge_order::text) AS source_row_id,
            'authoritative' AS mapping_method,
            1.0::float AS confidence_score
        FROM pairs
        JOIN general.entity_golden_records src ON src.bn_root = pairs.src_bn
        JOIN general.entity_golden_records dst ON dst.bn_root = pairs.dst_bn
        LEFT JOIN cra.loop_edges le
          ON left(le.src, 9) = pairs.src_bn
         AND left(le.dst, 9) = pairs.dst_bn
        WHERE {_status_where('src')}
          AND {_status_where('dst')}
        ORDER BY pairs.edge_order
    """
    return _query(query, {"cycle_id": int(cycle_id)})


def fetch_cra_cycle_summary(cycle_id: int | str) -> dict[str, Any] | None:
    """Source: cra.loops selected by id for case-level CRA cycle context."""
    query = """
        SELECT
            id::text AS cycle_id,
            COALESCE(total_flow, bottleneck_amt, 0) AS total_amount,
            hops AS hop_count,
            min_year,
            max_year,
            path_bns
        FROM cra.loops
        WHERE id = %(cycle_id)s
    """
    return _one(query, {"cycle_id": int(cycle_id)})


def fetch_entity_source_summary(entity_ids: Sequence[int | str]) -> pl.DataFrame:
    """Batch source-link counts by entity/schema/table for selected case entities."""
    ids = [int(entity_id) for entity_id in entity_ids if entity_id is not None]
    if not ids:
        return pl.DataFrame()
    query = """
        SELECT
            esl.entity_id,
            esl.source_schema,
            esl.source_table,
            COUNT(*)::int AS link_count
        FROM general.entity_source_links esl
        WHERE esl.entity_id = ANY(%(entity_ids)s)
        GROUP BY esl.entity_id, esl.source_schema, esl.source_table
        ORDER BY esl.entity_id, esl.source_schema, esl.source_table
    """
    return _query(query, {"entity_ids": ids})


def fetch_ab_sole_source_flags(entity_id=None) -> pl.DataFrame:
    """Source: ab.ab_sole_source via general.entity_source_links; repeat/splitting flags derived."""
    filter_sql = "AND esl.entity_id = %(entity_id)s" if entity_id is not None else ""
    query = f"""
        SELECT
            esl.entity_id,
            ss.vendor AS vendor_name,
            COUNT(*)::int AS contract_count,
            COALESCE(SUM(ss.amount), 0) AS total_amount,
            (COUNT(*) >= 2) AS repeat_vendor_flag,
            (COUNT(*) FILTER (WHERE ss.amount BETWEEN 9000 AND 100000) >= 2) AS splitting_flag
        FROM general.entity_source_links esl
        JOIN ab.ab_sole_source ss ON ss.id = (esl.source_pk ->> 'id')::uuid
        WHERE esl.source_schema = 'ab'
          AND esl.source_table = 'ab_sole_source'
          {filter_sql}
        GROUP BY esl.entity_id, ss.vendor
        HAVING COUNT(*) >= 1
        ORDER BY total_amount DESC
        LIMIT 5000
    """
    return _query(query, {"entity_id": int(entity_id)} if entity_id is not None else {})


def fetch_fed_concentration(entity_id) -> pl.DataFrame:
    """Source: fed.grants_contributions via general.entity_source_links; HHI derived by owner_org."""
    query = f"""
        WITH dept AS (
            SELECT gc.owner_org, SUM(gc.agreement_value)::numeric AS amount
            FROM general.entity_source_links esl
            JOIN {_fed_grants_relation()} gc ON gc._id = (esl.source_pk ->> '_id')::int
            WHERE esl.entity_id = %(entity_id)s
              AND esl.source_schema = 'fed'
              AND esl.source_table = 'grants_contributions'
              AND gc.agreement_value > 0
            GROUP BY gc.owner_org
        ), totals AS (
            SELECT SUM(amount)::numeric AS total_amount FROM dept
        )
        SELECT
            COALESCE(SUM(POWER(dept.amount / NULLIF(totals.total_amount, 0), 2)), 0)::float AS hhi_score,
            COUNT(*)::int AS dept_count,
            COALESCE(MAX(dept.amount / NULLIF(totals.total_amount, 0)), 0)::float AS top_dept_share
        FROM dept, totals
    """
    return _query(query, {"entity_id": int(entity_id)})


def fetch_related_entities(entity_id) -> pl.DataFrame:
    """Source: general.entity_golden_records.related_entities JSONB."""
    query = """
        SELECT
            COALESCE((rel.value ->> 'entity_id')::int, (rel.value ->> 'id')::int) AS related_entity_id,
            COALESCE(rel.value ->> 'relationship_type', rel.value ->> 'type', 'RELATED') AS relationship_type,
            COALESCE((rel.value ->> 'confidence_score')::float, (rel.value ->> 'confidence')::float, 1.0) AS confidence_score,
            rel.value AS evidence
        FROM general.entity_golden_records e
        CROSS JOIN LATERAL jsonb_array_elements(COALESCE(e.related_entities, '[]'::jsonb)) rel(value)
        WHERE e.id = %(entity_id)s
    """
    return _query(query, {"entity_id": int(entity_id)})


def fetch_entities_by_ids(entity_ids: Sequence[int | str]) -> pl.DataFrame:
    """Source: general.entity_golden_records filtered by id."""
    ids = [int(entity_id) for entity_id in entity_ids if entity_id is not None]
    if not ids:
        return pl.DataFrame()
    query = """
        SELECT
            id AS entity_id,
            canonical_name,
            COALESCE(entity_type, 'unknown') AS entity_type,
            bn_root,
            COALESCE(bn_variants, ARRAY[]::text[]) AS bn_variants,
            COALESCE(aliases, '[]'::jsonb) AS aliases,
            COALESCE(dataset_sources, ARRAY[]::text[]) AS datasets,
            source_link_count
        FROM general.entity_golden_records
        WHERE id = ANY(%(entity_ids)s)
    """
    return _query(query, {"entity_ids": ids})


def fetch_evidence_for_edge(source, source_row_id) -> dict:
    """Source: original cra/fed/ab source table selected by source and source_row_id."""
    source = str(source)
    source_row_id = str(source_row_id)
    if source in {"cra_gift", "cra_director"}:
        parts = source_row_id.split("|")
        if source == "cra_gift" and len(parts) == 2:
            return fetch_cra_cycle_edge_evidence(parts[0], parts[1])
        if len(parts) >= 3:
            table = "cra_directors" if source == "cra_director" else "cra_qualified_donees"
            row = _one(
                f"""
                SELECT %(table)s AS table_name, to_jsonb(t) AS row
                FROM cra.{table} t
                WHERE bn = %(bn)s
                  AND fpe = %(fpe)s::date
                  AND sequence_number = %(sequence_number)s
                """,
                {
                    "table": f"cra.{table}",
                    "bn": parts[0],
                    "fpe": parts[1],
                    "sequence_number": int(parts[2]),
                },
            )
            return row or {}
    source_map = {
        "fed_grant": ("fed", "grants_contributions", "_id", "int"),
        "fed_contribution": ("fed", "grants_contributions", "_id", "int"),
        "ab_grant": ("ab", "ab_grants", "id", "int"),
        "ab_contract": ("ab", "ab_contracts", "id", "uuid"),
        "ab_sole_source": ("ab", "ab_sole_source", "id", "uuid"),
    }
    if source not in source_map:
        return {}
    schema, table, pk, pk_type = source_map[source]
    cast = "::int" if pk_type == "int" else "::uuid"
    conn = get_conn()
    query = sql.SQL("SELECT {label} AS table_name, to_jsonb(t) AS row FROM {schema}.{table} t WHERE {pk} = %(id)s" + cast).format(
        label=sql.Literal(f"{schema}.{table}"),
        schema=sql.Identifier(schema),
        table=sql.Identifier(table),
        pk=sql.Identifier(pk),
    )
    with conn.cursor() as cur:
        cur.execute(query, {"id": source_row_id})
        return cur.fetchone() or {}


def fetch_cra_cycle_edge_evidence(cycle_id: int | str, edge_order: int | str) -> dict:
    """Source: cra.loops + cra.loop_edges for precomputed cycle edge provenance."""
    query = """
        WITH loop AS (
            SELECT id, path_bns
            FROM cra.loops
            WHERE id = %(cycle_id)s
        ), pair AS (
            SELECT
                loop.id,
                u.ord::int AS edge_order,
                left(u.bn, 9) AS src_bn,
                left(COALESCE(loop.path_bns[u.ord::int + 1], loop.path_bns[1]), 9) AS dst_bn
            FROM loop
            CROSS JOIN LATERAL unnest(loop.path_bns) WITH ORDINALITY AS u(bn, ord)
            WHERE u.ord::int = %(edge_order)s
        )
        SELECT
            'cra.loop_edges' AS table_name,
            jsonb_build_object(
                'cycle_id', pair.id,
                'edge_order', pair.edge_order,
                'source_bn', pair.src_bn,
                'destination_bn', pair.dst_bn,
                'amount', COALESCE(le.total_amt, 0),
                'min_year', le.min_year,
                'max_year', le.max_year,
                'provenance', 'cra.loop_edges'
            ) AS row
        FROM pair
        LEFT JOIN cra.loop_edges le
          ON left(le.src, 9) = pair.src_bn
         AND left(le.dst, 9) = pair.dst_bn
    """
    return _one(query, {"cycle_id": int(cycle_id), "edge_order": int(edge_order)}) or {}


def _safe_count(query: str) -> int:
    row = _one(query)
    return int(row["count"]) if row and row.get("count") is not None else 0


def fetch_shared_director_candidates(min_orgs: int = 3) -> pl.DataFrame:
    """Source: cra.cra_directors joined to general.entity_golden_records by bn_root.

    Filters to directors who control at least min_orgs publicly-funded entities
    (entities with at least one source link in fed or ab). Without this filter
    the top-N is dominated by parish boards with shared pastors.
    """
    query = f"""
        SELECT
            {_DIRECTOR_NAME_NORMALIZED_SQL} AS director_name_normalized,
            ARRAY_AGG(DISTINCT e.id) AS entity_ids,
            COUNT(DISTINCT e.id)::int AS org_count
        FROM cra.cra_directors d
        JOIN general.entity_golden_records e ON e.bn_root = left(d.bn, 9)
        WHERE COALESCE(trim(concat_ws(' ', d.first_name, d.initials, d.last_name)), '') <> ''
          AND {_status_where('e')}
          AND e.id IN (
              SELECT entity_id
              FROM general.entity_source_links
              WHERE source_schema IN ('fed', 'ab')
          )
        GROUP BY 1
        HAVING COUNT(DISTINCT e.id) >= GREATEST(%(min_orgs)s, 2)
        ORDER BY org_count DESC
        LIMIT 5000
    """
    return _query(query, {"min_orgs": min_orgs})


def fetch_high_overhead_candidates(entity_id=None) -> pl.DataFrame:
    """Source: cra.cra_compensation + cra_financial_details via bn_root.

    Returns entities where total compensation > 50% of total expenditures.
    Filters out obviously legitimate large employers (school boards, health
    authorities) by requiring the entity to be in a cycle OR have a shared
    director — we only care about high-overhead within already-suspicious rings.
    """
    filter_sql = "AND e.id = %(entity_id)s" if entity_id is not None else ""
    query = f"""
        SELECT
            e.id AS entity_id,
            ci.legal_name,
            fd.field_5100 AS total_expenditures,
            c.field_390 AS total_compensation,
            ROUND(c.field_390 / NULLIF(fd.field_5100, 0) * 100, 1)::float AS comp_pct
        FROM general.entity_golden_records e
        JOIN cra.cra_compensation c ON e.bn_root = left(c.bn, 9)
        JOIN cra.cra_financial_details fd ON c.bn = fd.bn AND c.fpe = fd.fpe
        JOIN cra.cra_identification ci ON c.bn = ci.bn AND ci.fiscal_year = EXTRACT(YEAR FROM c.fpe)::int
        WHERE c.fpe >= '2022-01-01'
          AND c.field_390 > 0
          AND fd.field_5100 > 0
          AND c.field_390 / NULLIF(fd.field_5100, 0) > 0.5
          AND fd.field_5100 < 100000000  -- exclude mega-institutions
          AND e.id IN (
              SELECT DISTINCT e2.id
              FROM general.entity_golden_records e2
              WHERE e2.id IN (SELECT entity_id FROM general.entity_source_links WHERE source_schema IN ('fed','ab'))
                 OR e2.bn_root IN (SELECT left(bn,9) FROM cra.loop_participants)
          )
          {filter_sql}
        ORDER BY comp_pct DESC
        LIMIT 2000
    """
    return _query(query, {"entity_id": int(entity_id)} if entity_id is not None else {})


def fetch_address_cluster_candidates(entity_id=None) -> pl.DataFrame:
    """Source: general.entity_golden_records.addresses JSONB.

    Returns entities that share an address with at least 2 other entities
    that have received federal or Alberta public funds.
    """
    filter_sql = "AND e.id = %(entity_id)s" if entity_id is not None else ""
    query = f"""
        WITH funded AS (
            SELECT DISTINCT entity_id
            FROM general.entity_source_links
            WHERE source_schema IN ('fed', 'ab')
        ),
        addr_entities AS (
            SELECT
                e.id AS entity_id,
                LOWER(TRIM(addr.value->>'postal_code')) AS postal,
                LOWER(TRIM(addr.value->>'city')) AS city
            FROM general.entity_golden_records e,
            LATERAL jsonb_array_elements(e.addresses) AS addr(value)
            WHERE e.addresses IS NOT NULL AND jsonb_array_length(e.addresses) > 0
        ),
        cluster_counts AS (
            SELECT postal, city, COUNT(*) AS cnt
            FROM addr_entities
            GROUP BY postal, city
            HAVING COUNT(*) >= 3
        )
        SELECT DISTINCT ae.entity_id, cc.cnt AS cluster_size
        FROM addr_entities ae
        JOIN cluster_counts cc ON ae.postal = cc.postal AND ae.city = cc.city
        WHERE ae.entity_id IN (SELECT entity_id FROM funded)
          {filter_sql}
        ORDER BY cc.cnt DESC
        LIMIT 5000
    """
    return _query(query, {"entity_id": int(entity_id)} if entity_id is not None else {})


def fetch_nonqualified_donee_flags(entity_id=None) -> pl.DataFrame:
    """Source: cra.cra_non_qualified_donees via general.entity_golden_records.bn_root.

    Returns entities that granted significant cash to non-qualified donees
    (organizations or individuals not registered as Canadian charities).
    """
    filter_sql = "AND e.id = %(entity_id)s" if entity_id is not None else ""
    query = f"""
        SELECT
            e.id AS entity_id,
            COUNT(*)::int AS grant_count,
            COALESCE(SUM(nqd.cash_amount), 0)::numeric AS total_cash,
            ARRAY_AGG(DISTINCT nqd.recipient_name) FILTER (WHERE nqd.recipient_name IS NOT NULL) AS recipient_names
        FROM general.entity_golden_records e
        JOIN cra.cra_non_qualified_donees nqd ON e.bn_root = left(nqd.bn, 9)
        WHERE nqd.fpe >= '2022-01-01'
          AND nqd.cash_amount > 0
          {filter_sql}
        GROUP BY e.id
        HAVING COUNT(*) >= 2
        ORDER BY total_cash DESC
        LIMIT 2000
    """
    return _query(query, {"entity_id": int(entity_id)} if entity_id is not None else {})


def fetch_ring_funding_edges(entity_ids: list[int]) -> pl.DataFrame:
    """Single query: all CRA gift flows between any pair of entities in the list."""
    if not entity_ids:
        return pl.DataFrame()
    query = """
        SELECT
            src.id AS from_entity_id,
            dst.id AS to_entity_id,
            q.total_gifts AS amount,
            q.fpe::date AS date,
            'cra_gift' AS source,
            concat_ws('|', q.bn, q.fpe::text, q.sequence_number::text) AS source_row_id,
            'authoritative' AS mapping_method,
            1.0::float AS confidence_score
        FROM cra.cra_qualified_donees q
        JOIN general.entity_golden_records src ON left(q.bn, 9) = src.bn_root
        JOIN general.entity_golden_records dst ON left(q.donee_bn, 9) = dst.bn_root
        WHERE src.id = ANY(%(ids)s)
          AND dst.id = ANY(%(ids)s)
          AND src.id <> dst.id
          AND q.total_gifts > 0
        ORDER BY q.total_gifts DESC
        LIMIT 2000
    """
    return _query(query, {"ids": [int(e) for e in entity_ids]})


def fetch_shared_director_funding_pairs(min_total_amount: float = 50_000) -> pl.DataFrame:
    """Bulk query: director-linked entity pairs with CRA gift flow >= min_total_amount."""
    query = f"""
        WITH dir_entity AS (
            SELECT DISTINCT
                {_DIRECTOR_NAME_NORMALIZED_SQL} AS director_name_normalized,
                e.id AS entity_id,
                e.bn_root
            FROM cra.cra_directors d
            JOIN general.entity_golden_records e ON e.bn_root = left(d.bn, 9)
            WHERE COALESCE(trim(concat_ws(' ', d.first_name, d.initials, d.last_name)), '') <> ''
              AND {_status_where('e')}
              AND e.id IN (
                  SELECT entity_id FROM general.entity_source_links
                  WHERE source_schema IN ('fed', 'ab')
              )
        ),
        pairs AS (
            SELECT a.director_name_normalized,
                   a.entity_id AS entity_id_a, a.bn_root AS bn_a,
                   b.entity_id AS entity_id_b, b.bn_root AS bn_b
            FROM dir_entity a
            JOIN dir_entity b ON a.director_name_normalized = b.director_name_normalized
                              AND a.entity_id < b.entity_id
        )
        SELECT
            p.director_name_normalized,
            p.entity_id_a,
            p.entity_id_b,
            SUM(q.total_gifts)::float AS total_amount,
            MIN(concat_ws('|', q.bn, q.fpe::text, q.sequence_number::text)) AS source_row_id
        FROM pairs p
        JOIN cra.cra_qualified_donees q
          ON (left(q.bn, 9) = p.bn_a AND left(q.donee_bn, 9) = p.bn_b)
          OR (left(q.bn, 9) = p.bn_b AND left(q.donee_bn, 9) = p.bn_a)
        WHERE q.total_gifts > 0
        GROUP BY p.director_name_normalized, p.entity_id_a, p.entity_id_b
        HAVING SUM(q.total_gifts) >= %(min_amount)s
        ORDER BY total_amount DESC
        LIMIT 500
    """
    return _query(query, {"min_amount": min_total_amount})


def row_to_jsonable(row: Any) -> Any:
    if isinstance(row, (dict, list, tuple, str, int, float, bool)) or row is None:
        return row
    return json.loads(json.dumps(row, default=str))
