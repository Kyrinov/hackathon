from __future__ import annotations

from typing import Any

import networkx as nx
import polars as pl


NODE_COLORS = {
    "person": "#2563eb",
    "charity": "#f97316",
    "organization": "#f97316",
    "business": "#16a34a",
    "company": "#16a34a",
    "gov": "#dc2626",
    "unknown": "#64748b",
}

EDGE_COLORS = {
    "cra_gift": "#f59e0b",
    "fed_grant": "#22c55e",
    "fed_contribution": "#84cc16",
    "ab_grant": "#38bdf8",
    "ab_contract": "#a78bfa",
    "ab_sole_source": "#ef4444",
    "cra_director": "#94a3b8",
}


def to_pyvis_json(G: nx.MultiDiGraph, highlight_nodes=None) -> dict[str, list[dict[str, Any]]]:
    highlighted = {str(node) for node in (highlight_nodes or [])}
    nodes = []
    for node_id, attrs in G.nodes(data=True):
        node_type = attrs.get("entity_type") or attrs.get("type") or "unknown"
        nodes.append(
            {
                "id": node_id,
                "label": attrs.get("canonical_name", node_id),
                "title": attrs.get("canonical_name", node_id),
                "group": node_type,
                "color": {
                    "background": NODE_COLORS.get(node_type, NODE_COLORS["unknown"]),
                    "border": "#ef4444" if str(node_id) in highlighted else "#e5e7eb",
                },
                "borderWidth": 4 if str(node_id) in highlighted else 1,
            }
        )
    edges = []
    for source, target, key, data in G.edges(keys=True, data=True):
        edge_source = data.get("source", "unknown")
        amount = float(data.get("amount") or 0.0)
        edges.append(
            {
                "id": f"{source}:{target}:{key}",
                "from": source,
                "to": target,
                "label": f"${amount:,.0f}" if amount else edge_source,
                "title": (
                    f"Source: {edge_source}<br>"
                    f"Amount: ${amount:,.2f}<br>"
                    f"Date: {data.get('date', '')}<br>"
                    f"Row: {data.get('source_row_id', '')}"
                ),
                "color": EDGE_COLORS.get(edge_source, "#94a3b8"),
                "arrows": "to",
                "source": edge_source,
                "amount": amount,
                "date": data.get("date", ""),
                "mapping_method": data.get("mapping_method", ""),
                "confidence_score": float(data.get("confidence_score") or 1.0),
                "source_row_id": data.get("source_row_id", ""),
            }
        )
    return {"nodes": nodes, "edges": edges}


def to_evidence_table(G: nx.MultiDiGraph, entity_id) -> pl.DataFrame:
    rows = []
    node = str(entity_id)
    for source, target, data in list(G.in_edges(node, data=True)) + list(G.out_edges(node, data=True)):
        rows.append(
            {
                "source": data.get("source", ""),
                "table": str(data.get("source", "")).replace("_", "."),
                "source_row_id": data.get("source_row_id", ""),
                "mapping_method": data.get("mapping_method", ""),
                "confidence_score": float(data.get("confidence_score") or 1.0),
                "from": G.nodes[source].get("canonical_name", source),
                "to": G.nodes[target].get("canonical_name", target),
            }
        )
    schema = {
        "source": pl.Utf8,
        "table": pl.Utf8,
        "source_row_id": pl.Utf8,
        "mapping_method": pl.Utf8,
        "confidence_score": pl.Float64,
        "from": pl.Utf8,
        "to": pl.Utf8,
    }
    return pl.DataFrame(rows, schema=schema) if rows else pl.DataFrame(schema=schema)
