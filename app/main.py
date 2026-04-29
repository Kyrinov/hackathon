from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import networkx as nx
import polars as pl
import streamlit as st
from pyvis.network import Network
from streamlit.components.v1 import html

from app.demo_data import generate_demo_graph
from src.agents import state
from src.db.queries import (
    fetch_cra_cycle_edges,
    fetch_cra_cycle_summary,
    fetch_entities_by_ids,
    fetch_entity_source_summary,
    fetch_evidence_for_edge,
)
from src.graph.builder import build_cra_cycle_graph, build_full_ring_graph
from src.graph.exporter import EDGE_COLORS, NODE_COLORS, to_evidence_table
from src.score.scorer import top_rings


def _demo_graph() -> tuple[nx.MultiDiGraph, list[dict], dict[str, dict[str, Any]]]:
    entities, edges, rings = generate_demo_graph()
    graph = nx.MultiDiGraph()
    entity_map = {row["entity_id"]: row for row in entities.iter_rows(named=True)}
    for entity_id, row in entity_map.items():
        graph.add_node(
            entity_id,
            entity_id=entity_id,
            canonical_name=row["canonical_name"],
            entity_type=row["entity_type"],
            type=row["entity_type"],
            datasets=row["datasets"],
            aliases=row["aliases"],
            total_score=row["total_score"],
            flags=row["flags"],
        )
    for row in edges.iter_rows(named=True):
        graph.add_edge(
            row["from_entity_id"],
            row["to_entity_id"],
            source=row["source"],
            amount=float(row["amount"] or 0.0),
            date=str(row["date"] or ""),
            source_row_id=row["source_row_id"],
            mapping_method=row["mapping_method"],
            confidence_score=float(row["confidence_score"] or 1.0),
        )
    return graph, rings, entity_map


@st.cache_data(ttl=600)
def _load_live_rings() -> list[dict]:
    return top_rings(20)


def _load_agent_state() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    db = Path("data/agent_state.db")
    if not db.exists():
        return [], []
    with sqlite3.connect(str(db)) as conn:
        conn.row_factory = sqlite3.Row
        findings = [
            dict(row)
            for row in conn.execute(
                "SELECT id, created_at, source, finding_type, ring_id, "
                "trigger_external_id, narrative, total_amount, severity, entity_ids "
                "FROM findings ORDER BY created_at DESC LIMIT 25"
            )
        ]
        sources = [dict(row) for row in conn.execute("SELECT name, last_run_at, rows_fetched_total FROM sources")]
    return findings, sources


def _probability_band(probability: float) -> str:
    if probability >= 0.97:
        return "Likely same (>=0.97)"
    if probability >= 0.70:
        return "Review (0.70-0.97)"
    return "Audit (<0.70)"


def _render_splink_review() -> None:
    candidates = state.list_splink_candidates(status="all", limit=100)
    if not candidates:
        return
    with st.expander("Splink entity-resolution review", expanded=False):
        cols = st.columns(4)
        cols[0].metric("Candidates", len(candidates))
        cols[1].metric("Likely same", sum(1 for c in candidates if float(c["match_probability"]) >= 0.97))
        cols[2].metric("Needs review", sum(1 for c in candidates if c.get("status") in {"needs_review", "likely_same"}))
        cols[3].metric("Approved", sum(1 for c in candidates if c.get("status") == "same"))

        status_filter = st.selectbox(
            "Review queue",
            ["needs_review", "likely_same", "audit", "same", "different", "all"],
            index=0,
        )
        visible = [
            c for c in candidates if status_filter == "all" or c.get("status") == status_filter
        ][:25]
        for candidate in visible:
            probability = float(candidate.get("match_probability") or 0.0)
            with st.container(border=True):
                head = st.columns([0.18, 0.52, 0.15, 0.15])
                head[0].markdown(f"**{probability:.3f}**")
                head[1].markdown(
                    f"`{_probability_band(probability)}` · `{candidate.get('status')}`"
                )
                head[2].markdown(f"#{candidate.get('entity_id_l')}")
                head[3].markdown(f"#{candidate.get('entity_id_r')}")
                body = st.columns(2)
                body[0].write(
                    {
                        "name": candidate.get("legal_name_l"),
                        "dataset": candidate.get("source_dataset_l"),
                        "bn_root": candidate.get("bn_root_l"),
                        "place": ", ".join(
                            p for p in [candidate.get("city_l"), candidate.get("province_l")] if p
                        ),
                    }
                )
                body[1].write(
                    {
                        "name": candidate.get("legal_name_r"),
                        "dataset": candidate.get("source_dataset_r"),
                        "bn_root": candidate.get("bn_root_r"),
                        "place": ", ".join(
                            p for p in [candidate.get("city_r"), candidate.get("province_r")] if p
                        ),
                    }
                )
                actions = st.columns([0.12, 0.14, 0.18, 0.56])
                candidate_id = int(candidate["id"])
                if actions[0].button("Same", key=f"splink-same-{candidate_id}"):
                    state.approve_splink_candidate(candidate_id, reviewed_by="streamlit")
                    st.rerun()
                if actions[1].button("Different", key=f"splink-different-{candidate_id}"):
                    state.update_splink_candidate_status(
                        candidate_id,
                        "different",
                        reviewed_by="streamlit",
                    )
                    st.rerun()
                if actions[2].button("Needs review", key=f"splink-review-{candidate_id}"):
                    state.update_splink_candidate_status(
                        candidate_id,
                        "needs_review",
                        reviewed_by="streamlit",
                    )
                    st.rerun()


def _severity_rank(finding: dict[str, Any]) -> int:
    return {"urgent": 0, "review": 1, "info": 2}.get(str(finding.get("severity") or ""), 3)


def _default_finding_index(findings: list[dict[str, Any]]) -> int:
    if not findings:
        return 0
    best_rank = min(_severity_rank(finding) for finding in findings)
    candidates = [idx for idx, finding in enumerate(findings) if _severity_rank(finding) == best_rank]
    return max(candidates, key=lambda idx: str(findings[idx].get("created_at") or ""))


def _entity_ids_from_finding(finding: dict[str, Any]) -> list[str]:
    try:
        raw_ids = json.loads(finding.get("entity_ids") or "[]")
    except (TypeError, json.JSONDecodeError):
        raw_ids = []
    return [str(entity_id) for entity_id in raw_ids if entity_id not in (None, "")]


def _finding_label(finding: dict[str, Any]) -> str:
    amount = finding.get("total_amount")
    amount_s = f"${amount:,.0f}" if amount else "no amount"
    timestamp = (finding.get("created_at") or "")[:19].replace("T", " ")
    return (
        f"#{finding.get('id')} {finding.get('source') or 'source'} "
        f"{finding.get('finding_type') or 'finding'} · {amount_s} · {timestamp}"
    )


def _ring_from_finding(finding: dict[str, Any]) -> dict[str, Any]:
    entity_ids = _entity_ids_from_finding(finding)
    return {
        "ring_id": finding.get("ring_id") or f"finding-{finding.get('id')}",
        "entity_ids": entity_ids,
        "canonical_names": [f"Entity {entity_id}" for entity_id in entity_ids],
        "shared_persons": [],
        "funding_edges": [],
        "evidence": [
            {
                "source": finding.get("source"),
                "source_row_id": finding.get("trigger_external_id"),
                "mapping_method": "agent_resolved",
                "confidence_score": 1.0,
            }
        ],
        "total_amount": float(finding.get("total_amount") or 0.0),
        "datasets_touched": [finding.get("source") or "agent"],
        "flags": [finding.get("finding_type") or "agent_finding"],
        "narrative": finding.get("narrative") or "",
    }


def _load() -> tuple[nx.MultiDiGraph, list[dict], bool]:
    try:
        rings = _load_live_rings()
        if not rings:
            raise RuntimeError("No live rings returned")
        # Build lightweight graph from ring data — no extra DB queries.
        graph = _ring_graph(rings[0], False)
        return graph, rings, False
    except Exception:
        graph, rings, _ = _demo_graph()
        return graph, rings, True


def _ring_graph(ring: dict, demo_mode: bool) -> nx.MultiDiGraph:
    if demo_mode:
        graph, _, _ = _demo_graph()
        keep = set(ring["entity_ids"]) | {
            source
            for source, target in graph.edges()
            if source.startswith("person:") and target in set(ring["entity_ids"])
        }
        keep.update(
            target
            for source, target in graph.edges()
            if source in keep or target in keep or source in set(ring["entity_ids"])
        )
        return graph.subgraph(keep).copy()
    # Build a lightweight graph directly from ring data — no extra DB queries.
    graph = nx.MultiDiGraph()
    names = dict(zip(ring.get("entity_ids", []), ring.get("canonical_names", [])))
    for entity_id in ring.get("entity_ids", []):
        graph.add_node(str(entity_id), entity_id=str(entity_id),
                       canonical_name=names.get(entity_id, str(entity_id)),
                       entity_type="charity", type="charity",
                       datasets=ring.get("datasets_touched", ["cra"]),
                       aliases=[], total_score=ring.get("total_score", 0.0),
                       flags=ring.get("flags", []))
    for person in ring.get("shared_persons", []):
        person_id = f"person:{person}"
        graph.add_node(person_id, entity_id=person_id, canonical_name=person,
                       entity_type="person", type="person", datasets=["cra"],
                       aliases=[], total_score=0.0, flags=[])
        for entity_id in ring.get("entity_ids", []):
            graph.add_edge(person_id, str(entity_id), source="cra_director",
                           amount=0.0, date="", mapping_method="authoritative",
                           confidence_score=1.0, source_row_id="")
    for edge in ring.get("funding_edges", []):
        frm = str(edge.get("from") or edge.get("from_entity_id", ""))
        to = str(edge.get("to") or edge.get("to_entity_id", ""))
        if frm and to:
            graph.add_edge(frm, to, source=edge.get("source", "cra_gift"),
                           amount=float(edge.get("amount") or 0.0),
                           date=str(edge.get("date") or ""),
                           mapping_method=edge.get("mapping_method", "authoritative"),
                           confidence_score=float(edge.get("confidence_score") or 1.0),
                           source_row_id=str(edge.get("source_row_id") or ""))
    return graph


def _graph_for_ring(ring: dict, demo_mode: bool) -> nx.MultiDiGraph:
    if demo_mode:
        return _ring_graph(ring, True)
    ring_id = str(ring.get("ring_id") or "")
    if ring_id.startswith("cra-cycle-"):
        try:
            graph = build_cra_cycle_graph(ring_id.removeprefix("cra-cycle-"))
            if graph.number_of_edges():
                return graph
        except Exception:
            pass
    try:
        graph = build_full_ring_graph(ring.get("entity_ids", []))
        if graph.number_of_edges():
            return graph
    except Exception:
        pass
    return _ring_graph(ring, False)


def _cycle_id_from_ring(ring: dict[str, Any]) -> str | None:
    ring_id = str(ring.get("ring_id") or "")
    if ring_id.startswith("cra-cycle-"):
        return ring_id.removeprefix("cra-cycle-")
    return None


def _safe_dataframe(fetcher, *args) -> pl.DataFrame:
    try:
        return fetcher(*args)
    except Exception:
        return pl.DataFrame()


def _safe_cycle_summary(cycle_id: str | None) -> dict[str, Any] | None:
    if not cycle_id:
        return None
    try:
        return fetch_cra_cycle_summary(cycle_id)
    except Exception:
        return None


def _entity_source_badges(source_rows: list[dict[str, Any]], finding_source: str | None) -> list[str]:
    schemas = {str(row.get("source_schema") or "").lower() for row in source_rows if row.get("source_schema")}
    badges = []
    if "cra" in schemas:
        badges.append("CRA")
    if "fed" in schemas:
        badges.append("FED")
    if "ab" in schemas:
        badges.append("AB")
    if len(schemas) > 1:
        badges.append("multi-source")
    if finding_source and str(finding_source).split("_")[0].lower() in schemas:
        badges.append("live-triggered")
    badges.append("cycle-member")
    return badges


def _render_case_dossier(
    ring: dict[str, Any],
    finding: dict[str, Any] | None,
    graph: nx.MultiDiGraph,
) -> None:
    cycle_id = _cycle_id_from_ring(ring)
    summary = _safe_cycle_summary(cycle_id)
    cycle_edges = _safe_dataframe(fetch_cra_cycle_edges, cycle_id) if cycle_id else pl.DataFrame()
    entity_ids = [str(entity_id) for entity_id in ring.get("entity_ids", [])]
    entity_rows = _safe_dataframe(fetch_entities_by_ids, entity_ids)
    source_summary = _safe_dataframe(fetch_entity_source_summary, entity_ids)

    st.subheader("Selected Case Dossier")
    case_cols = st.columns(6)
    case_cols[0].metric("Ring ID", str(ring.get("ring_id") or "n/a"))
    case_cols[1].metric("Finding source", str((finding or {}).get("source") or "featured"))
    case_cols[2].metric("Severity", str((finding or {}).get("severity") or "review"))
    case_cols[3].metric("Trigger row", str((finding or {}).get("trigger_external_id") or "n/a"))
    case_cols[4].metric(
        "CRA cycle flow",
        f"${float((summary or {}).get('total_amount') or ring.get('total_amount') or 0):,.0f}",
    )
    case_cols[5].metric("Hops", str((summary or {}).get("hop_count") or graph.number_of_edges()))

    if summary and (summary.get("min_year") is not None or summary.get("max_year") is not None):
        st.caption(f"Fiscal years: {summary.get('min_year') or 'n/a'}-{summary.get('max_year') or 'n/a'}")

    st.info(
        "Why flagged: a live public row resolved to an entity already inside a "
        "CRA-confirmed circular funding path. The graph shows the directed CRA cycle; "
        "the tables below show the public-record edges and source links behind the case."
    )

    if not cycle_edges.is_empty():
        edge_cols = [
            col
            for col in [
                "edge_order",
                "from_entity_id",
                "to_entity_id",
                "src_bn",
                "dst_bn",
                "amount",
                "date",
                "source_row_id",
            ]
            if col in cycle_edges.columns
        ]
        st.write("Directed funding edges")
        st.dataframe(cycle_edges.select(edge_cols), use_container_width=True, hide_index=True)

    if entity_rows.is_empty():
        return

    source_rows_by_entity: dict[str, list[dict[str, Any]]] = {}
    if not source_summary.is_empty():
        for row in source_summary.iter_rows(named=True):
            source_rows_by_entity.setdefault(str(row.get("entity_id")), []).append(row)

    entity_table = []
    for row in entity_rows.iter_rows(named=True):
        entity_id = str(row.get("entity_id"))
        source_rows = source_rows_by_entity.get(entity_id, [])
        datasets = row.get("datasets") or []
        source_links = sum(int(item.get("link_count") or 0) for item in source_rows)
        entity_table.append(
            {
                "entity_id": entity_id,
                "canonical_name": row.get("canonical_name"),
                "entity_type": row.get("entity_type"),
                "datasets": ", ".join(str(item).upper() for item in datasets),
                "source_links": source_links or row.get("source_link_count") or 0,
                "badges": ", ".join(_entity_source_badges(source_rows, (finding or {}).get("source"))),
            }
        )
    st.write("Entities")
    st.dataframe(pl.DataFrame(entity_table), use_container_width=True, hide_index=True)


def _network_html(graph: nx.MultiDiGraph, ring: dict, threshold: float) -> str:
    network = Network(height="620px", width="100%", bgcolor="#111827", font_color="#f9fafb", directed=True)
    network.barnes_hut(gravity=-3000, central_gravity=0.2, spring_length=140, spring_strength=0.03)
    highlighted = {str(entity_id) for entity_id in ring.get("entity_ids", [])}

    for node_id, attrs in graph.nodes(data=True):
        node_type = attrs.get("entity_type") or attrs.get("type") or "unknown"
        score = float(attrs.get("total_score") or 0.0)
        network.add_node(
            node_id,
            label=attrs.get("canonical_name", node_id),
            title=f"{attrs.get('canonical_name', node_id)}<br>{node_type}<br>Score: {score:.2f}",
            color={
                "background": NODE_COLORS.get(node_type, NODE_COLORS["unknown"]),
                "border": "#ef4444" if node_id in highlighted or score >= threshold else "#e5e7eb",
            },
            borderWidth=4 if node_id in highlighted or score >= threshold else 1,
            size=18 if node_id in highlighted else 13,
        )

    for source, target, data in graph.edges(data=True):
        amount = float(data.get("amount") or 0.0)
        edge_source = data.get("source", "")
        network.add_edge(
            source,
            target,
            label=f"${amount:,.0f}" if amount else edge_source,
            title=(
                f"Source: {edge_source}<br>"
                f"Amount: ${amount:,.2f}<br>"
                f"Row: {data.get('source_row_id', '')}<br>"
                f"Method: {data.get('mapping_method', '')}"
            ),
            color=EDGE_COLORS.get(edge_source, "#94a3b8"),
            arrows="to",
        )

    network.set_options(
        """
        const options = {
          "interaction": {"hover": true, "navigationButtons": true},
          "nodes": {"font": {"size": 14, "face": "Inter"}},
          "edges": {"font": {"size": 11, "align": "middle"}, "smooth": {"type": "dynamic"}},
          "physics": {"stabilization": {"iterations": 120}}
        }
        """
    )
    return network.generate_html(notebook=False)


def _score_for_node(node: dict[str, Any], ring: dict) -> float:
    if node.get("total_score") is not None:
        return float(node["total_score"])
    if str(node.get("entity_id")) in {str(item) for item in ring.get("entity_ids", [])}:
        return float(ring.get("total_score") or 0.0)
    return 0.0


def _evidence_detail(item: dict[str, Any], demo_mode: bool) -> dict:
    if demo_mode:
        return {"table_name": item.get("source"), "row": dict(item)}
    return fetch_evidence_for_edge(item.get("source"), item.get("source_row_id"))


def _ring_type(ring: dict[str, Any]) -> str:
    """Classify a ring as round_trip / shared_director / other.

    Falls back to inspecting ring_id when ring_type is missing — keeps
    the demo robust against older cached payloads.
    """
    rt = str(ring.get("ring_type") or "").strip().lower()
    if rt in {"round_trip", "shared_director"}:
        return rt
    rid = str(ring.get("ring_id") or "")
    if rid.startswith("cra-cycle-"):
        return "round_trip"
    if rid.startswith("director-pair-"):
        return "shared_director"
    return "other"


def _render_ring_panel(
    ring: dict[str, Any],
    graph: nx.MultiDiGraph,
    demo_mode: bool,
    threshold: float,
    key_prefix: str,
    finding: dict[str, Any] | None = None,
) -> None:
    """Render the standard ring view: dossier + metrics + graph + entity panel."""
    _render_case_dossier(ring, finding, graph)

    metric_cols = st.columns(4)
    metric_cols[0].metric("Ring score", f"{float(ring.get('total_score') or 0.0):.2f}")
    metric_cols[1].metric("Amount", f"${float(ring.get('total_amount') or 0.0):,.0f}")
    metric_cols[2].metric("Entities", len(ring.get("entity_ids", [])))
    metric_cols[3].metric("Evidence rows", len(ring.get("evidence", [])))

    left, right = st.columns([0.6, 0.4], gap="large")
    with left:
        html(_network_html(graph, ring, threshold), height=620)

    entity_options = {
        attrs.get("canonical_name", node_id): node_id
        for node_id, attrs in graph.nodes(data=True)
        if attrs.get("entity_type") != "gov"
    }
    with right:
        if not entity_options:
            st.info("No non-government entities to inspect in this ring.")
        else:
            selected_entity_label = st.selectbox(
                "Entity detail",
                list(entity_options.keys()),
                key=f"{key_prefix}-entity",
            )
            selected_entity = entity_options[selected_entity_label]
            attrs = graph.nodes[selected_entity]
            st.subheader(attrs.get("canonical_name", selected_entity))

            cols = st.columns(3)
            cols[0].metric("Type", attrs.get("entity_type", "unknown"))
            cols[1].metric("Score", f"{_score_for_node(attrs, ring):.2f}")
            cols[2].metric("Datasets", ", ".join(attrs.get("datasets", [])) or "n/a")

            aliases = attrs.get("aliases") or []
            if aliases:
                st.write("Aliases")
                st.write(", ".join(str(alias) for alias in aliases[:5]))

            flags = attrs.get("flags") or ring.get("flags") or []
            if flags:
                st.write("Flags")
                for flag in flags:
                    st.info(flag)

            evidence = to_evidence_table(graph, selected_entity)
            if not evidence.is_empty():
                st.write("Evidence")
                st.dataframe(evidence, use_container_width=True, hide_index=True)
                for item in evidence.to_dicts()[:5]:
                    with st.expander(f"{item['source']} / {item['source_row_id']}"):
                        st.json(_evidence_detail(item, demo_mode))

    st.subheader("Top flagged entities in this case")
    rows = []
    for node_id, attrs in graph.nodes(data=True):
        if attrs.get("entity_type") == "person":
            continue
        rows.append(
            {
                "entity_id": node_id,
                "canonical_name": attrs.get("canonical_name", node_id),
                "entity_type": attrs.get("entity_type", "unknown"),
                "score": _score_for_node(attrs, ring),
                "flags": ", ".join(attrs.get("flags") or ring.get("flags") or []),
            }
        )
    st.dataframe(
        pl.DataFrame(rows).sort("score", descending=True).head(10) if rows else pl.DataFrame(),
        use_container_width=True,
        hide_index=True,
    )


def _select_ring_in_tab(rings: list[dict], key_prefix: str, empty_msg: str) -> dict | None:
    if not rings:
        st.info(empty_msg)
        return None
    featured = {
        f"{ring.get('ring_id', '')} — {', '.join(ring.get('canonical_names', [])[:2])}": ring
        for ring in rings[:10]
    }
    selected_label = st.radio(
        "Select case",
        list(featured.keys()),
        key=f"{key_prefix}-radio",
        horizontal=False,
    )
    return featured[selected_label]


def main() -> None:
    st.set_page_config(page_title="Agency 2026 - Challenge #6", layout="wide")
    st.markdown(
        """
        <style>
        .stApp { background: #0b1120; color: #e5e7eb; }
        section[data-testid="stSidebar"] { background: #111827; }
        h1, h2, h3 { color: #f9fafb; letter-spacing: 0; }
        div[data-testid="stMetric"] { background: #111827; border: 1px solid #263244; padding: 12px; border-radius: 6px; }
        .block-container { padding-top: 1.1rem; }
        </style>
        """,
        unsafe_allow_html=True,
    )

    st.title("Agency 2026 - Challenge #6: Related Party Networks")
    st.caption(
        "Flagged for ministerial review - all findings traceable to public records "
        "(CRA T3010, federal G&C, Alberta open data)"
    )

    findings, sources = _load_agent_state()
    selected_finding: dict[str, Any] | None = None
    if findings:
        with st.expander("🟢 Live findings — agent fleet", expanded=True):
            cols = st.columns(4)
            cols[0].metric("Findings (24h-ish)", len(findings))
            urgent = sum(1 for finding in findings if finding.get("severity") == "urgent")
            cols[1].metric("Urgent", urgent)
            active_sources = {source.get("name") for source in sources if source.get("last_run_at")}
            cols[2].metric("Active sources", len(active_sources))
            last_seen = max((source.get("last_run_at") or "" for source in sources), default="—")
            cols[3].metric("Last fetch", last_seen[-8:] if last_seen else "—")
            st.caption(
                "Agent fleet polls open.canada.ca (FED Grants & Contributions, CRA T3010 directors, "
                "CRA Qualified Donees) and surfaces new disbursements connected to existing CRA-detected "
                "funding rings. All evidence traces to public records."
            )
            finding_by_label = {_finding_label(finding): finding for finding in findings}
            selected_finding_label = st.selectbox(
                "Graph live finding",
                list(finding_by_label),
                index=_default_finding_index(findings),
                help="Select a flagged agent finding to render its funding relationships below.",
            )
            selected_finding = finding_by_label[selected_finding_label]
            for finding in findings[:8]:
                severity = finding.get("severity")
                badge = {"urgent": "🔴", "review": "🟡", "info": "🔵"}.get(severity, "⚪")
                timestamp = (finding.get("created_at") or "")[:19].replace("T", " ")
                total_amount = finding.get("total_amount")
                amount_s = f"${total_amount:,.0f}" if total_amount else "—"
                with st.container(border=True):
                    head_cols = st.columns([0.08, 0.50, 0.20, 0.22])
                    head_cols[0].markdown(f"**{badge}**")
                    head_cols[1].markdown(f"**{finding.get('source') or '—'}** → ring `{finding.get('ring_id') or '—'}`")
                    head_cols[2].markdown(f"`{finding.get('finding_type') or '—'}`")
                    head_cols[3].markdown(f"**{amount_s}**  ·  {timestamp or '—'}")
                    st.markdown(f"<small>{finding.get('narrative') or ''}</small>", unsafe_allow_html=True)
                    with st.expander("Evidence"):
                        st.write(
                            {
                                "trigger_external_id": finding.get("trigger_external_id"),
                                "entity_ids": _entity_ids_from_finding(finding),
                                "ring_id": finding.get("ring_id"),
                            }
                        )
    _render_splink_review()

    with st.sidebar:
        threshold = st.slider("Score threshold", 0.0, 1.0, 0.5, 0.05)
        show_featured = st.checkbox(
            "Featured cases",
            value=False,
            help="Runs the slower top-ring ranking path instead of the selected live finding.",
        )

    if selected_finding and not show_featured:
        ring = _ring_from_finding(selected_finding)
        graph = _graph_for_ring(ring, False)
        demo_mode = False
        st.subheader(f"Live finding: {ring['ring_id']}")
        if ring.get("narrative"):
            st.markdown(f"**{ring['narrative']}**")
        _render_ring_panel(ring, graph, demo_mode, threshold, "live", selected_finding)
    else:
        _, rings, demo_mode = _load()
        if demo_mode:
            st.warning("Connect event-day .env to load live data.")

        rings_round = [r for r in rings if _ring_type(r) == "round_trip"]
        rings_shared = [r for r in rings if _ring_type(r) == "shared_director"]

        tab_round, tab_shared, tab_cross = st.tabs(
            [
                f"(a) Round-trip rings ({len(rings_round)})",
                f"(b) Shared-director networks ({len(rings_shared)})",
                "(c) Contractor / charity-director crossover",
            ]
        )

        with tab_round:
            st.caption(
                "Charity A gifts to charity B which gifts back to A — confirmed via "
                "CRA's pre-computed loop table (cra.loops). Highest-confidence signal."
            )
            ring = _select_ring_in_tab(
                rings_round, "rt", "No round-trip rings detected in cache."
            )
            if ring is not None:
                graph = _graph_for_ring(ring, demo_mode)
                _render_ring_panel(ring, graph, demo_mode, threshold, "rt")

        with tab_shared:
            st.caption(
                "One director sits on the boards of two publicly-funded entities "
                "that also have a CRA gift flow between them. Director name match "
                "is normalized — review for common-name collisions."
            )
            ring = _select_ring_in_tab(
                rings_shared,
                "sd",
                "No shared-director networks in cache. Run "
                "`PYTHONPATH=. .venv/bin/python -m scripts.precompute_rings` to build.",
            )
            if ring is not None:
                graph = _graph_for_ring(ring, demo_mode)
                _render_ring_panel(ring, graph, demo_mode, threshold, "sd")

        with tab_cross:
            st.caption(
                "Principals of contract-receiving companies who are also directors "
                "of charities receiving federal grants. (Rule R3 — implementation "
                "deferred; the underlying datasets are loaded.)"
            )
            st.info(
                "Coverage stub: this pattern is structurally detectable from the "
                "CRA T1235 director list joined to fed.grants_contributions and "
                "AB contracts. Implementation pending — flagged entities will "
                "surface here in the next iteration. See AGENCY2026_CHALLENGE6.md "
                "§5 Rule R3 for the policy basis."
            )


if __name__ == "__main__":
    main()
