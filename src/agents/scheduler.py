from __future__ import annotations

import time
from typing import Iterable

from src.agents import analyst, briefer, resolver, state, validators


def _source_url(watcher) -> str | None:
    resource_id = getattr(watcher, "resource_id", None)
    base_url = getattr(getattr(watcher, "client", None), "base_url", None)
    if not resource_id or not base_url:
        return None
    return f"{base_url}?resource_id={resource_id}"


def run_cycle(watchers: Iterable, persist: bool = True, brief_findings: bool = True) -> int:
    """One pass: fetch_new from each watcher, resolve, analyze, brief, persist findings.

    Returns the number of findings produced this cycle.
    """
    state.init_db()
    total = 0
    for w in watchers:
        try:
            rows = w.fetch_new()
        except Exception as exc:  # noqa: BLE001
            print(f"[scheduler] {w.name}: fetch failed: {exc}")
            continue
        if not rows:
            print(f"[scheduler] {w.name}: 0 new rows")
            continue

        batch_id = validators.new_batch_id()
        fetched_at = validators.utcnow_iso()
        valid_rows, quarantined = validators.validate_rows(w.name, rows, batch_id)
        state.insert_quarantine(quarantined)
        state.insert_staged_batch(
            batch_id=batch_id,
            source=w.name,
            resource_id=getattr(w, "resource_id", None),
            source_url=_source_url(w),
            fetched_at=fetched_at,
            raw_row_count=len(rows),
            valid_row_count=len(valid_rows),
            quarantined_row_count=len(quarantined),
        )
        if not valid_rows:
            print(
                f"[scheduler] {w.name}: {len(rows)} fetched / 0 bronze-valid / "
                f"{len(quarantined)} quarantined"
            )
            continue

        try:
            resolved_meta = resolver.resolve_batch_with_metadata(valid_rows, w.name)
            resolved = {
                external_id: item["entity_ids"]
                for external_id, item in resolved_meta.items()
            }
        except Exception as exc:  # noqa: BLE001
            print(f"[scheduler] {w.name}: resolve failed: {exc}")
            continue

        rows_by_id = {str(r.get(w.external_id_field)): r for r in valid_rows}
        try:
            findings = analyst.analyze(resolved, w.name, rows_by_id)
        except Exception as exc:  # noqa: BLE001
            print(f"[scheduler] {w.name}: analyze failed: {exc}")
            continue

        if not findings:
            print(f"[scheduler] {w.name}: {len(rows)} fetched / {len(resolved)} resolved / 0 promoted")
            continue

        for f in findings:
            trigger_external_id = f.get("trigger_external_id")
            trigger_row = rows_by_id.get(str(trigger_external_id))
            mapping = resolved_meta.get(str(trigger_external_id), {})
            f.update(
                {
                    "batch_id": batch_id,
                    "resource_id": getattr(w, "resource_id", None),
                    "source_url": _source_url(w),
                    "fetched_at": fetched_at,
                    "trigger_row_hash": trigger_row.get("_row_hash") if trigger_row else None,
                    "mapping_method": mapping.get("mapping_method"),
                    "confidence_score": mapping.get("confidence_score"),
                    "review_status": "pending",
                }
            )
            if brief_findings:
                try:
                    polished = briefer.brief(f)
                    if polished:
                        f["narrative"] = polished
                except Exception:
                    pass
            if persist:
                state.insert_finding(**f)
            total += 1

        print(
            f"[scheduler] {w.name}: {len(rows)} fetched / {len(valid_rows)} bronze-valid / "
            f"{len(quarantined)} quarantined / {len(resolved)} resolved / {len(findings)} promoted"
        )

    return total


def run_loop(watchers: Iterable, interval_seconds: int = 60) -> None:
    """Continuous scheduler loop. Catches per-cycle exceptions and logs."""
    while True:
        try:
            n = run_cycle(list(watchers))
            print(f"[scheduler] cycle complete: {n} findings")
        except Exception as exc:  # noqa: BLE001
            print(f"[scheduler] cycle failed: {exc}")
        time.sleep(interval_seconds)
