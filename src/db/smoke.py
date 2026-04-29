from __future__ import annotations

from .queries import _safe_count


def run_smoke() -> dict:
    return {
        "golden_records": _safe_count("SELECT COUNT(*) AS count FROM general.entity_golden_records"),
        "directors_with_3_plus_orgs": _safe_count(
            """
            SELECT COUNT(*) AS count
            FROM (
                SELECT lower(trim(concat_ws(' ', d.first_name, d.initials, d.last_name))) AS name
                FROM cra.cra_directors d
                JOIN general.entity_golden_records e ON e.bn_root = left(d.bn, 9)
                GROUP BY 1
                HAVING COUNT(DISTINCT e.id) >= 3
            ) x
            """
        ),
        "funding_edges": _safe_count(
            """
            SELECT
                (SELECT COUNT(*) FROM cra.cra_qualified_donees WHERE total_gifts > 0)
              + (SELECT COUNT(*) FROM fed.grants_contributions WHERE agreement_value > 0)
              + (SELECT COUNT(*) FROM ab.ab_grants WHERE amount > 0)
              + (SELECT COUNT(*) FROM ab.ab_contracts WHERE amount > 0)
              + (SELECT COUNT(*) FROM ab.ab_sole_source WHERE amount > 0) AS count
            """
        ),
        "precomputed_cycles": _safe_count("SELECT COUNT(*) AS count FROM cra.loops"),
    }


if __name__ == "__main__":
    print(run_smoke())
