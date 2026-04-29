from __future__ import annotations

import re
import time
from typing import Any

from src.agents import state
from src.db.connection import get_conn
from src.db.queries import _status_where

# Per-source field map. extra_bn lets a CRA donees row resolve donor + donee BNs.
_FIELDS: dict[str, dict[str, Any]] = {
    "fed_grants": {
        "bn": "recipient_business_number",
        "name": "recipient_legal_name",
        "external_id": "_id",
        "extra_bn": [],
    },
    "cra_t3010": {
        # T3010 director rows are keyed by org BN; no separate org name on the row.
        "bn": "BN",
        "name": None,
        "external_id": "_id",
        "extra_bn": [],
    },
    "cra_donees": {
        "bn": "BN",
        "name": "Donee Name",
        "external_id": "_id",
        "extra_bn": ["Donee BN"],
    },
}

_BN_NON_ALNUM = re.compile(r"[^a-z0-9]", re.IGNORECASE)
_NAME_NON_KEEP = re.compile(r"[^a-z0-9 ]")
_WS = re.compile(r"\s+")


def _normalize_bn(value: Any) -> str | None:
    if value is None:
        return None
    cleaned = _BN_NON_ALNUM.sub("", str(value))
    digits = "".join(ch for ch in cleaned if ch.isdigit())
    if len(digits) < 9:
        return None
    return digits[:9]


def _normalize_name(value: Any) -> str | None:
    if value is None:
        return None
    s = _NAME_NON_KEEP.sub(" ", str(value).lower())
    s = _WS.sub(" ", s).strip()
    if not s or len(s) < 3:
        return None
    return s


def _row_bns(row: dict[str, Any], source: str) -> list[str]:
    spec = _FIELDS.get(source, {})
    fields = [spec.get("bn")] + list(spec.get("extra_bn", []) or [])
    out: list[str] = []
    for f in fields:
        if not f:
            continue
        bn = _normalize_bn(row.get(f))
        if bn:
            out.append(bn)
    return out


def _row_name(row: dict[str, Any], source: str) -> str | None:
    spec = _FIELDS.get(source, {})
    return _normalize_name(row.get(spec.get("name", "")))


def _row_external_id(row: dict[str, Any], source: str) -> str | None:
    spec = _FIELDS.get(source, {})
    raw = row.get(spec.get("external_id", "_id"))
    return None if raw is None else str(raw)


# ----- in-memory entity index (loaded once per process) -----

_BN_INDEX: dict[str, list[int]] | None = None
_NAME_INDEX: dict[str, list[int]] | None = None
_ALIAS_OVERRIDE_INDEX: dict[str, list[int]] | None = None
_SURVIVOR_OVERRIDES: dict[int, tuple[int, float]] | None = None
_ALIAS_OVERRIDE_CONFIDENCE: dict[tuple[str, int], float] | None = None
_INDEX_LOADED_AT: float | None = None


def _load_index(
    force: bool = False,
) -> tuple[dict[str, list[int]], dict[str, list[int]], dict[str, list[int]], dict[int, tuple[int, float]]]:
    """Pull canonical_name + aliases + bn_root for all active golden records into memory.

    ~851K rows × ~120 bytes = ~100 MB. Loads once per process. Subsequent matches are
    O(1) hash lookups, eliminating the per-batch SQL scan that times out remotely.
    """
    global _BN_INDEX, _NAME_INDEX, _ALIAS_OVERRIDE_INDEX, _SURVIVOR_OVERRIDES
    global _ALIAS_OVERRIDE_CONFIDENCE, _INDEX_LOADED_AT
    if (
        _BN_INDEX is not None
        and _NAME_INDEX is not None
        and _ALIAS_OVERRIDE_INDEX is not None
        and _SURVIVOR_OVERRIDES is not None
        and not force
    ):
        return _BN_INDEX, _NAME_INDEX, _ALIAS_OVERRIDE_INDEX, _SURVIVOR_OVERRIDES

    bn_idx: dict[str, list[int]] = {}
    name_idx: dict[str, list[int]] = {}
    alias_override_idx: dict[str, list[int]] = {}
    survivor_overrides: dict[int, tuple[int, float]] = {}
    alias_confidence: dict[tuple[str, int], float] = {}

    conn = get_conn()
    query = f"""
        SELECT e.id, e.bn_root, e.canonical_name, COALESCE(e.aliases, '[]'::jsonb) AS aliases
        FROM general.entity_golden_records e
        WHERE {_status_where('e')}
    """
    t = time.time()
    with conn.cursor() as cur:
        cur.execute(query)
        while True:
            batch = cur.fetchmany(50_000)
            if not batch:
                break
            for row in batch:
                eid = int(row["id"])
                bn = row.get("bn_root")
                if bn:
                    bn_idx.setdefault(str(bn)[:9], []).append(eid)
                canonical_norm = _normalize_name(row.get("canonical_name"))
                if canonical_norm:
                    name_idx.setdefault(canonical_norm, []).append(eid)
                aliases = row.get("aliases") or []
                if isinstance(aliases, list):
                    for alias in aliases:
                        alias_norm = _normalize_name(alias if isinstance(alias, str) else None)
                        if alias_norm:
                            name_idx.setdefault(alias_norm, []).append(eid)

    # Dedupe entity_id lists
    for key in bn_idx:
        bn_idx[key] = sorted(set(bn_idx[key]))
    for key in name_idx:
        name_idx[key] = sorted(set(name_idx[key]))

    try:
        overrides = state.load_resolution_overrides()
    except Exception as exc:
        print(f"[resolver] warning: could not load resolution overrides: {exc}")
        overrides = {"aliases": [], "survivors": []}
    for row in overrides.get("aliases", []):
        alias_norm = row.get("alias_norm")
        entity_id = row.get("entity_id")
        if not alias_norm or entity_id is None:
            continue
        eid = int(entity_id)
        alias_override_idx.setdefault(str(alias_norm), []).append(eid)
        name_idx.setdefault(str(alias_norm), []).append(eid)
        alias_confidence[(str(alias_norm), eid)] = float(row.get("confidence") or 1.0)
    for row in overrides.get("survivors", []):
        duplicate_id = row.get("duplicate_entity_id")
        survivor_id = row.get("survivor_entity_id")
        if duplicate_id is None or survivor_id is None:
            continue
        survivor_overrides[int(duplicate_id)] = (
            int(survivor_id),
            float(row.get("confidence") or 1.0),
        )
    for key in alias_override_idx:
        alias_override_idx[key] = sorted(set(alias_override_idx[key]))
    for key in name_idx:
        name_idx[key] = _collapse_entity_ids(name_idx[key], survivor_overrides)

    _BN_INDEX = bn_idx
    _NAME_INDEX = name_idx
    _ALIAS_OVERRIDE_INDEX = alias_override_idx
    _SURVIVOR_OVERRIDES = survivor_overrides
    _ALIAS_OVERRIDE_CONFIDENCE = alias_confidence
    _INDEX_LOADED_AT = time.time()
    elapsed = _INDEX_LOADED_AT - t
    print(
        "[resolver] loaded index: "
        f"{len(bn_idx)} BN keys, {len(name_idx)} name keys, "
        f"{len(alias_override_idx)} Splink alias keys, {len(survivor_overrides)} survivor overrides "
        f"in {elapsed:.1f}s"
    )
    return bn_idx, name_idx, alias_override_idx, survivor_overrides


def _collapse_entity_ids(
    entity_ids: list[int],
    survivor_overrides: dict[int, tuple[int, float]] | None = None,
) -> list[int]:
    overrides = survivor_overrides if survivor_overrides is not None else (_SURVIVOR_OVERRIDES or {})
    collapsed = [overrides.get(int(eid), (int(eid), 1.0))[0] for eid in entity_ids]
    return sorted(set(collapsed))


def _survivor_confidence(entity_ids: list[int]) -> float:
    if not _SURVIVOR_OVERRIDES:
        return 1.0
    confidences = [
        _SURVIVOR_OVERRIDES[int(eid)][1]
        for eid in entity_ids
        if int(eid) in _SURVIVOR_OVERRIDES
    ]
    return min(confidences) if confidences else 1.0


def _alias_override_confidence(name_norm: str, entity_ids: list[int]) -> float:
    if not _ALIAS_OVERRIDE_CONFIDENCE:
        return 1.0
    confidences = [
        _ALIAS_OVERRIDE_CONFIDENCE[(name_norm, int(eid))]
        for eid in entity_ids
        if (name_norm, int(eid)) in _ALIAS_OVERRIDE_CONFIDENCE
    ]
    return max(confidences) if confidences else 1.0


def index_stats() -> dict[str, Any]:
    bn_idx, name_idx, alias_override_idx, survivor_overrides = _load_index()
    return {
        "bn_keys": len(bn_idx),
        "name_keys": len(name_idx),
        "splink_alias_keys": len(alias_override_idx),
        "splink_survivor_overrides": len(survivor_overrides),
        "loaded_at": _INDEX_LOADED_AT,
    }


def resolve_batch_with_metadata(rows: list[dict[str, Any]], source: str) -> dict[str, dict[str, Any]]:
    """Map external rows to entity_ids and include provenance for the mapping.

    BN-root matches are deterministic. Exact normalized canonical-name/alias matches
    are weaker but still deterministic enough for the current scanner.
    """
    if not rows or source not in _FIELDS:
        return {}

    bn_idx, name_idx, alias_override_idx, survivor_overrides = _load_index()
    resolved: dict[str, dict[str, Any]] = {}

    for r in rows:
        ext = _row_external_id(r, source)
        if ext is None:
            continue
        eids: list[int] = []
        method = ""
        confidence = 0.0
        pre_collapse_eids: list[int] = []
        for bn in _row_bns(r, source):
            eids.extend(bn_idx.get(bn, []))
        if eids:
            method = "bn_root"
            confidence = 1.0
        if not eids:
            nm = _row_name(r, source)
            if nm:
                eids.extend(name_idx.get(nm, []))
            if eids:
                if nm and nm in alias_override_idx:
                    method = "splink_alias_override"
                    confidence = _alias_override_confidence(nm, eids)
                else:
                    method = "exact_name_or_alias"
                    confidence = 0.9
        if eids:
            pre_collapse_eids = sorted(set(int(eid) for eid in eids))
            collapsed = _collapse_entity_ids(pre_collapse_eids, survivor_overrides)
            if collapsed != pre_collapse_eids and method != "splink_alias_override":
                method = "splink_survivor_override"
                confidence = min(confidence or 1.0, _survivor_confidence(pre_collapse_eids))
            resolved[ext] = {
                "entity_ids": collapsed,
                "mapping_method": method,
                "confidence_score": confidence,
            }

    return resolved


def resolve_batch(rows: list[dict[str, Any]], source: str) -> dict[str, list[int]]:
    """Map external rows to entity_ids in bulk via in-memory index.

    Returns {external_id: [entity_id, ...]} for rows with at least one match.
    """
    return {
        external_id: item["entity_ids"]
        for external_id, item in resolve_batch_with_metadata(rows, source).items()
    }


def resolve_row(row: dict[str, Any], source: str) -> list[int]:
    result = resolve_batch([row], source)
    if not result:
        return []
    return next(iter(result.values()), [])


def coverage(rows: list[dict[str, Any]], source: str) -> tuple[int, int, dict[str, list[int]]]:
    resolved = resolve_batch(rows, source)
    return len(resolved), len(rows), resolved
