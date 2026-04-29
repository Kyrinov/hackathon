from __future__ import annotations

import sys
from pathlib import Path

# Make the repo root importable so `from app...` and `from src...` resolve
# when Streamlit Cloud invokes this file directly (it sets cwd to the repo
# root but does not add it to sys.path). Locally `PYTHONPATH=.` does the
# same job. Idempotent — safe to run twice.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import json
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
    fetch_govt_funding_layer,
)
from src.agents.risk_scorer import RISK_BADGE
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


_TOP_RINGS_CACHE = Path("data/cache/top_rings.json")
_CROSSOVER_CACHE = Path("data/cache/crossover.parquet")


@st.cache_data(ttl=3600)
def _load_crossover() -> pl.DataFrame:
    """Read the R3 director / contractor crossover cache."""
    if not _CROSSOVER_CACHE.exists():
        return pl.DataFrame()
    try:
        return pl.read_parquet(_CROSSOVER_CACHE)
    except Exception:
        return pl.DataFrame()


def _join_list_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, pl.Series):
        values = value.to_list()
    elif isinstance(value, list | tuple):
        values = value
    else:
        return str(value)
    return ", ".join(str(item) for item in values if item is not None)


@st.cache_data(ttl=3600)
def _load_live_rings() -> list[dict]:
    """Cache-first ring loader.

    Prefers the JSON cache built by scripts/prewarm.py. Falls back to a
    live top_rings(20) call if the cache is missing or unreadable.
    """
    if _TOP_RINGS_CACHE.exists():
        try:
            cached = json.loads(_TOP_RINGS_CACHE.read_text())
            if isinstance(cached, list) and cached:
                return cached
        except Exception:
            pass
    return top_rings(10_000)


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
        st.markdown("**Review queue summary**")
        cols = st.columns(4)
        cols[0].metric("Candidates", len(candidates))
        cols[1].metric("Likely same", sum(1 for c in candidates if float(c["match_probability"]) >= 0.97))
        cols[2].metric("Needs review", sum(1 for c in candidates if c.get("status") in {"needs_review", "likely_same"}))
        cols[3].metric("Approved", sum(1 for c in candidates if c.get("status") == "same"))
        st.markdown("<div style='height:4px'></div>", unsafe_allow_html=True)

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
                body[0].markdown(
                    f"**{candidate.get('legal_name_l') or '—'}**  \n"
                    f"<span style='color:#94a3b8; font-size:0.85rem;'>"
                    f"dataset: `{candidate.get('source_dataset_l') or '—'}` · "
                    f"BN: `{candidate.get('bn_root_l') or '—'}` · "
                    f"{', '.join(p for p in [candidate.get('city_l'), candidate.get('province_l')] if p) or '—'}"
                    f"</span>",
                    unsafe_allow_html=True,
                )
                body[1].markdown(
                    f"**{candidate.get('legal_name_r') or '—'}**  \n"
                    f"<span style='color:#94a3b8; font-size:0.85rem;'>"
                    f"dataset: `{candidate.get('source_dataset_r') or '—'}` · "
                    f"BN: `{candidate.get('bn_root_r') or '—'}` · "
                    f"{', '.join(p for p in [candidate.get('city_r'), candidate.get('province_r')] if p) or '—'}"
                    f"</span>",
                    unsafe_allow_html=True,
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


def _crossover_graph(row: dict[str, Any]) -> nx.MultiDiGraph:
    """Build a small triangle-shaped graph for one R3 crossover pair.

    Nodes:
        - the charity (federal-grant recipient)
        - the contractor (AB contract / sole-source recipient)
        - one person node per shared director
        - synthetic 'gov:fed' and 'gov:ab' funder nodes
    Edges:
        - director → charity (DIRECTS)
        - director → contractor (DIRECTS)
        - fed_funder → charity (FED grant total)
        - ab_funder → contractor (AB contract total)
    """
    graph = nx.MultiDiGraph()

    charity_id = f"crossover-charity-{row.get('charity_entity_id')}"
    contractor_id = f"crossover-contractor-{row.get('contractor_entity_id')}"
    grant_amount = float(row.get("total_grant_amount") or 0.0)
    contract_amount = float(row.get("total_contract_amount") or 0.0)
    contract_source = str(row.get("contract_source") or "ab_contract")

    graph.add_node(
        charity_id,
        entity_id=charity_id,
        canonical_name=str(row.get("charity_name") or "Charity"),
        entity_type="charity",
        type="charity",
        datasets=["cra", "fed"],
        aliases=[],
        total_score=0.7,
        flags=["Federal-grant recipient"],
    )
    graph.add_node(
        contractor_id,
        entity_id=contractor_id,
        canonical_name=str(row.get("contractor_name") or "Contractor"),
        entity_type="business",
        type="business",
        datasets=["cra", "ab"],
        aliases=[],
        total_score=0.7,
        flags=["AB contract recipient"],
    )

    fed_id = "gov:fed_grants_contributions"
    graph.add_node(
        fed_id,
        entity_id=fed_id,
        canonical_name="Federal G&C",
        entity_type="gov",
        type="gov",
        datasets=["fed"],
        aliases=[],
        total_score=0.0,
        flags=[],
    )
    graph.add_edge(
        fed_id,
        charity_id,
        source="fed_grant",
        amount=grant_amount,
        date="",
        mapping_method="authoritative",
        confidence_score=1.0,
        source_row_id="",
    )

    ab_id = f"gov:{contract_source}"
    graph.add_node(
        ab_id,
        entity_id=ab_id,
        canonical_name=("Alberta sole-source"
                        if contract_source == "ab_sole_source"
                        else "Alberta contract"),
        entity_type="gov",
        type="gov",
        datasets=["ab"],
        aliases=[],
        total_score=0.0,
        flags=[],
    )
    graph.add_edge(
        ab_id,
        contractor_id,
        source=contract_source,
        amount=contract_amount,
        date="",
        mapping_method="authoritative",
        confidence_score=1.0,
        source_row_id="",
    )

    directors = list(row.get("shared_directors") or [])
    for director in directors:
        if not director:
            continue
        person_id = f"person:{director}"
        graph.add_node(
            person_id,
            entity_id=person_id,
            canonical_name=str(director),
            entity_type="person",
            type="person",
            datasets=["cra"],
            aliases=[],
            total_score=0.0,
            flags=["Shared director"],
        )
        graph.add_edge(
            person_id,
            charity_id,
            source="cra_director",
            amount=0.0,
            date="",
            mapping_method="authoritative",
            confidence_score=1.0,
            source_row_id="",
        )
        graph.add_edge(
            person_id,
            contractor_id,
            source="cra_director",
            amount=0.0,
            date="",
            mapping_method="authoritative",
            confidence_score=1.0,
            source_row_id="",
        )

    return graph


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


def _graph_for_ring(
    ring: dict,
    demo_mode: bool,
    view: str = "cycle",
) -> nx.MultiDiGraph:
    """Build a graph for a ring.

    view="cycle"           -> just the directed CRA gift cycle (charity→charity).
                              Best when the user is asking "how does the money
                              flow?" — keeps the picture clean.
    view="director_network" -> charities + director person nodes connecting
                              them. Best when the user is asking "who controls
                              these entities?" — surfaces the human relationship
                              that makes the round-trip possible.
    """
    if demo_mode:
        return _ring_graph(ring, True)

    if view == "director_network":
        # Cheap overlay: take the directed CRA gift cycle (one fast query)
        # and add the shared-director person nodes from the cached ring
        # metadata. No per-entity director or funding-edge queries — those
        # were timing out on the live DB and produced an empty graph.
        graph = nx.MultiDiGraph()
        ring_id = str(ring.get("ring_id") or "")
        if ring_id.startswith("cra-cycle-"):
            try:
                graph = build_cra_cycle_graph(ring_id.removeprefix("cra-cycle-"))
            except Exception:
                graph = nx.MultiDiGraph()
        if graph.number_of_edges() == 0:
            # Fallback: synthesize entity nodes from ring metadata.
            graph = _ring_graph(ring, False)

        # Overlay each shared director as a person node linked to every
        # ring entity. The ring carries `shared_persons` from the prewarm,
        # so this is purely in-memory.
        ring_entity_ids = {str(e) for e in ring.get("entity_ids", [])}
        for person in ring.get("shared_persons", []) or []:
            if not person:
                continue
            person_id = f"person:{person}"
            graph.add_node(
                person_id,
                entity_id=person_id,
                canonical_name=str(person),
                entity_type="person",
                type="person",
                datasets=["cra"],
                aliases=[],
                total_score=0.0,
                flags=["Shared director"],
            )
            for entity_id in ring_entity_ids:
                if entity_id in graph.nodes:
                    graph.add_edge(
                        person_id,
                        entity_id,
                        source="cra_director",
                        amount=0.0,
                        date="",
                        mapping_method="authoritative",
                        confidence_score=1.0,
                        source_row_id="",
                    )
        return graph

    # Default cycle view.
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

    st.markdown("#### Selected Case Dossier")
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
        "the tables below show the public-record edges and source links behind the case.",
        icon="ℹ️",
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
        st.markdown("**Directed funding edges**")
        st.dataframe(cycle_edges.select(edge_cols), width="stretch", hide_index=True)

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
    st.markdown("**Entities**")
    st.dataframe(pl.DataFrame(entity_table), width="stretch", hide_index=True)


def _network_html(graph: nx.MultiDiGraph, ring: dict, threshold: float) -> str:
    network = Network(height="620px", width="100%", bgcolor="#0f172a", font_color="#e2e8f0", directed=True)
    n_nodes = graph.number_of_nodes()
    # Scale damping and gravity by graph size so large graphs settle quickly
    # instead of flailing. Small graphs get looser physics for easier manual
    # arrangement; large graphs get heavy damping and fewer iterations.
    if n_nodes > 20:
        gravity, central_gravity, spring_len, spring_str, damping, iterations = (
            -400, 0.3, 180, 0.03, 0.5, 100
        )
    elif n_nodes > 8:
        gravity, central_gravity, spring_len, spring_str, damping, iterations = (
            -600, 0.2, 200, 0.025, 0.3, 150
        )
    else:
        gravity, central_gravity, spring_len, spring_str, damping, iterations = (
            -800, 0.15, 220, 0.02, 0.09, 200
        )
    network.barnes_hut(
        gravity=gravity,
        central_gravity=central_gravity,
        spring_length=spring_len,
        spring_strength=spring_str,
        damping=damping,
        overlap=1,
    )
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
        f"""
        const options = {{
          "nodes": {{"font": {{"size": 14, "face": "Inter"}}}},
          "edges": {{"font": {{"size": 11, "align": "middle"}}, "smooth": {{"type": "dynamic"}}}},
          "physics": {{
            "stabilization": {{"enabled": true, "iterations": {iterations}, "fit": true}},
            "barnesHut": {{"avoidOverlap": 1.0, "damping": {damping}, "centralGravity": {central_gravity}}},
            "minVelocity": 0.75
          }},
          "interaction": {{"hover": true, "navigationButtons": true, "zoomView": true, "dragNodes": true}}
        }}
        """
    )
    html_doc = network.generate_html(notebook=False)
    # Streamlit hosts the pyvis HTML in an iframe that may be created while
    # its tab is hidden — at that moment the canvas is 0x0, so any fit()
    # call against it does nothing. We brute-force the centering: once on
    # stabilization, again on a multi-step delay, and again whenever the
    # iframe element is actually resized (the moment it becomes visible).
    inject = (
        "network = new vis.Network(container, data, options); "
        "window.__net = network; "
        "function __fit() { "
        "  try { "
        "    var bb = network.getBoundingBox(); "
        "    var cx = (bb.left + bb.right) / 2; "
        "    var cy = (bb.top + bb.bottom) / 2; "
        "    network.moveTo({position: {x: cx, y: cy}, animation: false}); "
        "    network.fit({animation: false}); "
        "  } catch (e) {} "
        "} "
        "network.once('stabilizationIterationsDone', function() { "
        "  network.setOptions({physics: {enabled: false}}); "
        "  __fit(); setTimeout(__fit, 100); "
        "}); "
        "[200, 500, 900, 1500, 2500, 4000].forEach(function (t) { setTimeout(__fit, t); }); "
        "if (typeof ResizeObserver !== 'undefined') { "
        "  try { new ResizeObserver(function(){ setTimeout(__fit, 50); }).observe(container); } catch (e) {} "
        "} "
    )
    html_doc = html_doc.replace(
        "network = new vis.Network(container, data, options);",
        inject,
    )
    return html_doc


def _score_for_node(node: dict[str, Any], ring: dict) -> float:
    if node.get("total_score") is not None:
        return float(node["total_score"])
    if str(node.get("entity_id")) in {str(item) for item in ring.get("entity_ids", [])}:
        return float(ring.get("total_score") or 0.0)
    return 0.0


def _evidence_detail(item: dict[str, Any], demo_mode: bool) -> dict:
    """Return a JSON-serialisable evidence detail for a graph edge.

    On cloud (no DATABASE_URL) the live DB lookup raises; we fall back to
    the in-memory edge dict so the click-through panel still works.
    """
    if demo_mode:
        return {"table_name": item.get("source"), "row": dict(item)}
    try:
        result = fetch_evidence_for_edge(item.get("source"), item.get("source_row_id"))
        if result:
            return result
    except Exception as exc:
        return {
            "table_name": item.get("source"),
            "row": dict(item),
            "note": (
                "Live DB unavailable — showing cached edge data only. "
                f"({type(exc).__name__})"
            ),
        }
    return {"table_name": item.get("source"), "row": dict(item)}


def _ring_type(ring: dict[str, Any]) -> str:
    rt = str(ring.get("ring_type") or "").strip().lower()
    if rt in {"round_trip", "shared_director"}:
        return rt
    rid = str(ring.get("ring_id") or "")
    if rid.startswith("cra-cycle-"):
        return "round_trip"
    if rid.startswith("director-pair-"):
        return "shared_director"
    return "other"


def _apply_layers(
    base_graph: nx.MultiDiGraph,
    ring: dict[str, Any],
    layers: dict[str, bool],
    demo_mode: bool,
) -> nx.MultiDiGraph:
    """Return a copy of base_graph with optional relationship layers overlaid."""
    g = base_graph.copy()

    # Entity flows layer: remove CRA gift edges when unchecked
    if not layers.get("entity", True):
        remove = [
            (u, v, k)
            for u, v, k, d in g.edges(keys=True, data=True)
            if d.get("source") in {"cra_gift"}
        ]
        g.remove_edges_from(remove)

    # Director network layer: person nodes from cached shared_persons
    if layers.get("directors"):
        ring_entity_ids = {str(e) for e in ring.get("entity_ids", [])}
        for person in ring.get("shared_persons") or []:
            if not person:
                continue
            pid = f"person:{person}"
            if pid not in g.nodes:
                g.add_node(
                    pid,
                    entity_id=pid,
                    canonical_name=str(person),
                    entity_type="person",
                    type="person",
                    datasets=["cra"],
                    aliases=[],
                    total_score=0.0,
                    flags=["Shared director"],
                )
            for eid in ring_entity_ids:
                if eid in g.nodes:
                    g.add_edge(
                        pid, eid,
                        source="cra_director",
                        amount=0.0, date="",
                        mapping_method="authoritative",
                        confidence_score=1.0,
                        source_row_id="",
                    )

    # Government funding layer: federal department → entity edges
    if layers.get("govt") and not demo_mode:
        int_ids = [int(e) for e in ring.get("entity_ids", []) if str(e).isdigit()]
        try:
            govt_df = fetch_govt_funding_layer(int_ids) if int_ids else pl.DataFrame()
        except Exception:
            govt_df = pl.DataFrame()
        for row in govt_df.iter_rows(named=True):
            org = str(row.get("govt_org") or "").strip()
            if not org:
                continue
            node_id = f"fed:{org}"
            if node_id not in g.nodes:
                g.add_node(
                    node_id,
                    entity_id=node_id,
                    canonical_name=org,
                    entity_type="fed_govt",
                    type="fed_govt",
                    datasets=["fed"],
                    aliases=[],
                    total_score=0.0,
                    flags=["Federal funder"],
                )
            target = str(int(row["entity_id"]))
            if target in g.nodes:
                g.add_edge(
                    node_id, target,
                    source=str(row.get("source") or "fed_grant"),
                    amount=float(row.get("total_amount") or 0.0),
                    date="",
                    mapping_method="authoritative",
                    confidence_score=1.0,
                    source_row_id="",
                )

    return g


def _render_ring_panel(
    ring: dict[str, Any],
    graph: nx.MultiDiGraph,
    demo_mode: bool,
    threshold: float,
    key_prefix: str,
    finding: dict[str, Any] | None = None,
) -> None:
    """Dossier + metrics + graph + entity panel + per-case top entities."""
    _render_case_dossier(ring, finding, graph)

    # Shared director(s) banner — surfaces the human relationship that
    # links the ring's entities. Always visible when present so the
    # answer to "who is the shared director" is one glance away.
    shared_persons = [p for p in (ring.get("shared_persons") or []) if p]
    if shared_persons:
        director_chips = " · ".join(f"`{p}`" for p in shared_persons[:5])
        more = f" (+{len(shared_persons) - 5} more)" if len(shared_persons) > 5 else ""
        st.success(
            f"**Shared director(s) linking this ring:** {director_chips}{more}",
            icon="👤",
        )

    composite = float(ring.get("composite_score") or ring.get("total_score") or 0.0)
    rl = ring.get("risk_level") or ("critical" if composite >= 0.75 else "high" if composite >= 0.55 else "medium" if composite >= 0.35 else "low")
    badge = RISK_BADGE.get(rl, rl)
    metric_cols = st.columns(5)
    metric_cols[0].metric("Risk level", badge)
    metric_cols[1].metric("Composite score", f"{composite:.2f}")
    metric_cols[2].metric("Amount", f"${float(ring.get('total_amount') or 0.0):,.0f}")
    metric_cols[3].metric("Entities", len(ring.get("entity_ids", [])))
    metric_cols[4].metric("Shared directors", len(shared_persons))

    assessment = ring.get("risk_assessment")
    if assessment:
        with st.expander(f"AI risk assessment — {assessment.get('risk_level', rl).upper()}", expanded=True):
            types = assessment.get("risk_types") or []
            if types:
                st.markdown(" · ".join(f"`{t}`" for t in types))
            if assessment.get("key_concern"):
                st.warning(assessment["key_concern"], icon="⚠️")
            if assessment.get("summary"):
                st.markdown(assessment["summary"])

    # ---- Layer toggles ----
    st.markdown(
        "<div style='margin:10px 0 6px; color:#94a3b8; font-size:0.8rem; "
        "font-weight:600; text-transform:uppercase; letter-spacing:0.06em;'>"
        "Graph layers</div>",
        unsafe_allow_html=True,
    )
    toggle_cols = st.columns(3)
    layer_entity = toggle_cols[0].checkbox(
        "Entity flows",
        value=True,
        key=f"{key_prefix}-layer-entity",
        help="Directed CRA funding edges between ring entities (on by default)",
    )
    layer_govt = toggle_cols[1].checkbox(
        "Government funding",
        value=False,
        key=f"{key_prefix}-layer-govt",
        help="Add federal department nodes + grant edges from fed.grants_contributions",
    )
    layer_directors = toggle_cols[2].checkbox(
        "Director network",
        value=False,
        key=f"{key_prefix}-layer-directors",
        help="Add person nodes for shared directors linking entity nodes (blue)",
    )
    layers = {
        "entity": layer_entity,
        "govt": layer_govt,
        "directors": layer_directors,
    }
    display_graph = _apply_layers(graph, ring, layers, demo_mode)

    left, right = st.columns([0.6, 0.4], gap="large")
    with left:
        html(_network_html(display_graph, ring, threshold), height=620)

    entity_options = {
        attrs.get("canonical_name", node_id): node_id
        for node_id, attrs in display_graph.nodes(data=True)
        if attrs.get("entity_type") not in {"gov", "fed_govt"}
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
            attrs = display_graph.nodes[selected_entity]
            st.markdown(f"**{attrs.get('canonical_name', selected_entity)}**")

            cols = st.columns(3)
            cols[0].metric("Type", attrs.get("entity_type", "unknown"))
            cols[1].metric("Score", f"{_score_for_node(attrs, ring):.2f}")
            cols[2].metric("Datasets", ", ".join(attrs.get("datasets", [])) or "n/a")

            aliases = attrs.get("aliases") or []
            if aliases:
                st.markdown("**Aliases**")
                st.caption(", ".join(str(alias) for alias in aliases[:5]))

            flags = attrs.get("flags") or ring.get("flags") or []
            if flags:
                st.markdown("**Flags**")
                for flag in flags:
                    st.info(flag, icon="🚩")

            evidence = to_evidence_table(display_graph, selected_entity)
            if not evidence.is_empty():
                st.markdown("**Evidence**")
                st.dataframe(evidence, width="stretch", hide_index=True)
                for item in evidence.to_dicts()[:5]:
                    with st.expander(f"{item['source']} / {item['source_row_id']}"):
                        st.json(_evidence_detail(item, demo_mode))

    st.markdown("#### Top flagged entities in this case")
    rows = []
    for node_id, attrs in display_graph.nodes(data=True):
        if attrs.get("entity_type") in {"person", "fed_govt", "contractor"}:
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
        width="stretch",
        hide_index=True,
    )


_RISK_ICON = {"critical": "🔴", "high": "🟠", "medium": "🟡", "low": "🟢"}


def _select_ring_in_tab(rings: list[dict], key_prefix: str, empty_msg: str) -> dict | None:
    if not rings:
        st.info(empty_msg)
        return None
    labels = []
    for ring in rings:
        rl = ring.get("risk_level", "low")
        icon = _RISK_ICON.get(rl, "⚪")
        names = ", ".join(ring.get("canonical_names", [])[:2])
        amount = ring.get("total_amount", 0)
        labels.append(f"{icon} {names}  (${amount:,.0f})")
    label_to_ring = dict(zip(labels, rings))
    selected_label = st.selectbox(
        f"Select case ({len(rings)} total)",
        labels,
        key=f"{key_prefix}-select",
    )
    ring = label_to_ring[selected_label]

    # Show precis immediately below the dropdown — no need to open the panel
    assessment = ring.get("risk_assessment")
    if assessment:
        concern = assessment.get("key_concern", "")
        types = " · ".join(f"`{t}`" for t in (assessment.get("risk_types") or []))
        summary = assessment.get("summary", "")
        st.markdown(
            f"<div style='background:#1e293b;border-left:3px solid #f59e0b;"
            f"padding:10px 14px;border-radius:4px;margin:6px 0 12px'>"
            f"<span style='color:#fbbf24;font-weight:600'>⚠ {concern}</span><br>"
            f"<span style='color:#94a3b8;font-size:0.85rem'>{types}</span><br>"
            f"<span style='color:#cbd5e1;font-size:0.88rem'>{summary}</span>"
            f"</div>",
            unsafe_allow_html=True,
        )
    return ring


def main() -> None:
    st.set_page_config(
        page_title="Agency 2026 — Challenge #6",
        layout="wide",
        initial_sidebar_state="expanded",
    )
    st.markdown(
        """
        <style>
        /* ---- Base surface tokens ---- */
        .stApp {
            background: #0b1120;
            color: #e2e8f0;
        }
        section[data-testid="stSidebar"] {
            background: #0f172a;
            border-right: 1px solid #1e293b;
        }

        /* ---- Typography ---- */
        h1 {
            color: #f8fafc;
            font-weight: 700;
            letter-spacing: -0.015em;
            line-height: 1.25;
        }
        h2, h3 {
            color: #f1f5f9;
            font-weight: 600;
            letter-spacing: -0.01em;
            line-height: 1.3;
            margin-top: 1.25rem;
            margin-bottom: 0.5rem;
        }
        p, li, .stMarkdown {
            color: #cbd5e1;
            line-height: 1.6;
        }
        small, .stCaption {
            color: #94a3b8 !important;
        }

        /* ---- Metric cards ---- */
        div[data-testid="stMetric"] {
            background: #1e293b;
            border: 1px solid #334155;
            border-radius: 8px;
            padding: 14px 16px;
            box-shadow: 0 1px 2px rgba(0,0,0,0.25);
        }
        div[data-testid="stMetric"] label {
            color: #94a3b8 !important;
            font-size: 0.8rem;
            font-weight: 500;
            text-transform: uppercase;
            letter-spacing: 0.04em;
        }
        div[data-testid="stMetric"] div[data-testid="stMetricValue"] {
            color: #f8fafc !important;
            font-weight: 700;
        }

        /* ---- Containers & borders ---- */
        div[data-testid="stContainer"] {
            border-radius: 8px;
        }
        div[data-testid="stVerticalBlock"] > div[data-testid="stVerticalBlockBorderWrapper"] {
            border-color: #334155;
            border-radius: 8px;
        }

        /* ---- Tabs ---- */
        div[data-testid="stTabs"] {
            border-bottom: 2px solid #1e293b;
            margin-bottom: 1.2rem;
        }
        div[data-testid="stTabs"] button[role="tab"] {
            color: #94a3b8;
            font-size: 1rem;
            font-weight: 600;
            padding: 0.75rem 1.4rem;
            border-radius: 8px 8px 0 0;
            background: #0f172a;
            border: 1px solid #1e293b;
            border-bottom: none;
            margin-right: 4px;
            letter-spacing: 0.01em;
            transition: background 0.15s, color 0.15s;
        }
        div[data-testid="stTabs"] button[role="tab"]:hover {
            background: #1e293b;
            color: #e2e8f0;
        }
        div[data-testid="stTabs"] button[role="tab"][aria-selected="true"] {
            color: #f8fafc;
            background: #1e40af;
            border-color: #1e40af;
            font-weight: 700;
            font-size: 1.05rem;
        }

        /* ---- Dataframes ---- */
        div[data-testid="stDataFrame"] {
            border: 1px solid #1e293b;
            border-radius: 8px;
            overflow: hidden;
        }

        /* ---- Sidebar spacing ---- */
        .block-container {
            padding-top: 1.5rem;
            padding-left: 2rem;
            padding-right: 2rem;
        }
        section[data-testid="stSidebar"] .block-container {
            padding-left: 1.25rem;
            padding-right: 1.25rem;
        }

        /* ---- Selectbox / inputs ---- */
        div[data-testid="stSelectbox"] label,
        div[data-testid="stSlider"] label,
        div[data-testid="stCheckbox"] label {
            color: #cbd5e1 !important;
            font-weight: 500;
        }

        /* ---- Expander ---- */
        details summary {
            color: #e2e8f0;
            font-weight: 500;
        }
        details summary:hover {
            color: #60a5fa;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )

    # ---- Hero header with clear visual separation ----
    st.markdown(
        """
        <div style="
            background: linear-gradient(90deg, #0f172a 0%, #1e293b 100%);
            border: 1px solid #334155;
            border-radius: 10px;
            padding: 22px 26px 18px 26px;
            margin-bottom: 18px;
            box-shadow: 0 2px 6px rgba(0,0,0,0.3);
        ">
            <h1 style="margin:0 0 8px 0; font-size:1.55rem;">
                Who controls the entities that receive public money — and do they control each other?
            </h1>
            <p style="margin:0; color:#94a3b8; font-size:0.92rem;">
                Agency 2026 · Challenge #6 — Related-Party Governance Networks<br>
                <span style="color:#64748b;">
                Three detection patterns: round-trip funding rings, shared-director networks,
                and contractor / charity-director crossover. All findings traceable to CRA T3010,
                federal G&amp;C, and Alberta open data.
                </span>
            </p>
        </div>
        """,
        unsafe_allow_html=True,
    )

    _render_splink_review()

    with st.sidebar:
        st.markdown("##### Controls")
        threshold = st.slider(
            "Score threshold",
            0.0,
            1.0,
            0.5,
            0.05,
            help="Highlights nodes whose composite risk score is at or above this threshold.",
        )
        st.markdown("---")
        st.markdown("##### Coverage")
        st.markdown(
            "**Datasets:** CRA T3010 · federal G&C · Alberta open data",
            help="Public data sources ingested by the agent fleet",
        )
        st.markdown(
            "**Patterns:** Round-trip · Shared director · Crossover",
            help="Detection algorithms running against the ingested data",
        )
        st.markdown(
            "**Refresh:** Weekly — new open.canada.ca data scored and committed automatically.",
        )
        st.markdown(
            "<p style='font-size:0.78rem; color:#64748b; margin-top:1.2rem; "
            "line-height:1.4;'>"
            "Decision support, not decision making. Findings flag patterns; "
            "intent must be assessed by a human reviewer."
            "</p>",
            unsafe_allow_html=True,
        )

    _, rings, demo_mode = _load()
    if demo_mode:
        st.warning(
            "Demo mode active — connect event-day .env to load live data.",
            icon="⚠️",
        )

    rings_round = [r for r in rings if _ring_type(r) == "round_trip"]
    rings_shared = [r for r in rings if r.get("shared_persons")]

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
            "CRA's pre-computed loop table. Highest-confidence signal."
        )
        st.markdown("<div style='height:4px'></div>", unsafe_allow_html=True)
        ring = _select_ring_in_tab(
            rings_round, "rt", "No round-trip rings detected in cache."
        )
        if ring is not None:
            graph = _graph_for_ring(ring, demo_mode)
            _render_ring_panel(ring, graph, demo_mode, threshold, "rt")

    with tab_shared:
        st.caption(
            "**Who** connects these entities? Each ring contains at least "
            "one person who sits on multiple boards. The graph below is "
            "drawn around the **person**: blue person nodes connect the "
            "charity nodes they direct, and federal / Alberta funding "
            "sources are pulled in as additional context. The same rings "
            "appear in tab (a) as money-flow cycles — here we re-draw them "
            "to surface the human relationship."
        )
        st.markdown("<div style='height:4px'></div>", unsafe_allow_html=True)
        ring = _select_ring_in_tab(
            rings_shared,
            "sd",
            "No shared-director networks in cache. Run "
            "`PYTHONPATH=. .venv/bin/python -m scripts.prewarm` to refresh.",
        )
        if ring is not None:
            graph = _graph_for_ring(ring, demo_mode, view="director_network")
            _render_ring_panel(ring, graph, demo_mode, threshold, "sd")

    with tab_cross:
        st.caption(
            "Rule R3: a person listed as a CRA T3010 director of a charity "
            "that received federal grants AND of an entity that received "
            "Alberta contracts or sole-source awards. Both sides require "
            "T3010 director records — pure-commercial contractors with no "
            "T3010 history are not surfaced from this dataset alone. "
            "Closed-loop cases (charity also sent CRA gifts directly to the "
            "contractor) are ranked first and marked with a warning."
        )
        st.markdown("<div style='height:4px'></div>", unsafe_allow_html=True)
        crossover = _load_crossover()
        if crossover.is_empty():
            st.info(
                "No crossover cases in cache. Run "
                "`PYTHONPATH=. .venv/bin/python -m scripts.precompute_crossover` "
                "to materialize."
            )
        else:
            has_closed_loop_col = "closed_loop" in crossover.columns
            closed_count = int(crossover["closed_loop"].sum()) if has_closed_loop_col else 0

            cols = st.columns(5)
            cols[0].metric("Crossover pairs", len(crossover))
            cols[1].metric("Closed loop", closed_count,
                           help="Charity also sent CRA gifts directly to the contractor — strongest evidence")
            cols[2].metric(
                "Total federal grants",
                f"${float(crossover['total_grant_amount'].sum()):,.0f}",
            )
            cols[3].metric(
                "Total AB contracts",
                f"${float(crossover['total_contract_amount'].sum()):,.0f}",
            )
            distinct_directors = (
                crossover["shared_directors"].explode().drop_nulls().n_unique()
            )
            cols[4].metric("Distinct directors", distinct_directors)
            st.markdown("<div style='height:8px'></div>", unsafe_allow_html=True)

            # ---- Per-pair graph view ----
            crossover_rows = crossover.to_dicts()

            def _pair_label(r: dict) -> str:
                prefix = "CLOSED LOOP · " if r.get("closed_loop") else ""
                return (
                    f"{prefix}{r.get('charity_name', '?')} ↔ {r.get('contractor_name', '?')} · "
                    f"${float(r.get('total_contract_amount') or 0):,.0f} contract"
                )

            row_label = {_pair_label(r): r for r in crossover_rows[:50]}
            selected_label = st.selectbox(
                "Inspect a crossover pair",
                list(row_label),
                key="crossover-select",
                help=(
                    "Closed-loop pairs (charity → contractor gift confirmed) are listed first. "
                    "Each row links a charity that received federal grants to "
                    "a contractor that received AB awards via at least one "
                    "shared CRA director."
                ),
            )
            selected_row = row_label[selected_label]
            shared = ", ".join(str(d) for d in (selected_row.get("shared_directors") or []) if d)

            if selected_row.get("closed_loop"):
                gift_amt = float(selected_row.get("gift_to_contractor") or 0)
                st.error(
                    f"**Closed loop confirmed:** charity sent ${gift_amt:,.0f} in CRA gifts "
                    f"directly to this contractor. Shared director(s): `{shared}`",
                    icon="🔴",
                )
            elif shared:
                st.success(f"**Shared director(s):** `{shared}`")

            metric_cols = st.columns(5)
            metric_cols[0].metric(
                "Federal grants → charity",
                f"${float(selected_row.get('total_grant_amount') or 0):,.0f}",
            )
            metric_cols[1].metric(
                "AB awards → contractor",
                f"${float(selected_row.get('total_contract_amount') or 0):,.0f}",
            )
            metric_cols[2].metric(
                "CRA gifts → contractor",
                f"${float(selected_row.get('gift_to_contractor') or 0):,.0f}"
                if has_closed_loop_col else "—",
                help="Direct CRA T3010 gift from this charity to the contractor entity",
            )
            metric_cols[3].metric(
                "Contract type",
                str(selected_row.get("contract_source") or "—"),
            )
            metric_cols[4].metric(
                "Shared directors",
                len(selected_row.get("shared_directors") or []),
            )

            crossover_g = _crossover_graph(selected_row)
            stub_ring = {
                "entity_ids": [
                    f"crossover-charity-{selected_row.get('charity_entity_id')}",
                    f"crossover-contractor-{selected_row.get('contractor_entity_id')}",
                ],
                "total_score": 0.9 if selected_row.get("closed_loop") else 0.7,
                "flags": (
                    ["Closed loop: charity gifted directly to contractor", "Shared director crossover"]
                    if selected_row.get("closed_loop")
                    else ["Contractor / charity-director crossover"]
                ),
            }
            html(_network_html(crossover_g, stub_ring, threshold), height=520)

            st.markdown("<div style='height:8px'></div>", unsafe_allow_html=True)
            with st.expander("All crossover pairs", expanded=False):
                select_cols = [
                    pl.col("charity_name").alias("Charity (federal-grant recipient)"),
                    pl.col("contractor_name").alias("Contractor (AB award recipient)"),
                    pl.col("shared_directors").list.join(", ").alias("director(s)"),
                    pl.col("total_grant_amount").round(0).cast(pl.Int64).alias("federal grants ($)"),
                    pl.col("total_contract_amount").round(0).cast(pl.Int64).alias("AB contracts ($)"),
                    pl.col("contract_source").alias("Contract type"),
                ]
                if has_closed_loop_col:
                    select_cols.append(
                        pl.col("gift_to_contractor").round(0).cast(pl.Int64).alias("CRA gift to contractor ($)")
                    )
                    select_cols.append(pl.col("closed_loop").alias("Closed loop"))
                display = crossover.select(select_cols)
                st.dataframe(display, width="stretch", hide_index=True)

            st.caption(
                "All matches are flagged for review. A shared director is "
                "structural evidence; intent must be assessed by a human "
                "with investigative authority."
            )

    st.markdown("---")
    st.markdown(
        """
        <div style="
            border-top: 1px solid #1e293b;
            padding-top: 12px;
            margin-top: 8px;
            color: #64748b;
            font-size: 0.82rem;
            line-height: 1.5;
        ">
            <strong>Decision support, not decision making.</strong>
            Every flag traces to a public-record source row
            (CRA T3010, federal Grants &amp; Contributions, Alberta open data).
            Director matching uses normalized names only — common names may collide.
            Alberta corporate registry and former-public-servant cross-match are out of scope.
            This system flags patterns; it does not infer intent.
        </div>
        """,
        unsafe_allow_html=True,
    )


if __name__ == "__main__":
    main()
