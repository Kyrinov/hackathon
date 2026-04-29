"""Weekly refresh: fetch new public data, boost freshly-confirmed rings, rebuild caches, push.

Wires open.canada.ca watchers → ring resolution → prewarm score boost → git push.
Tab (d) / agent_state.db are not used; findings feed directly into ring scoring.

Cron (every Monday 03:00 local):
    0 3 * * 1  cd /home/charles/agency_hack_2026 && PYTHONPATH=. .venv/bin/python -m scripts.weekly_refresh >> /var/log/agency_refresh.log 2>&1
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

FRESH_BOOSTS_PATH = Path("data/cache/fresh_boosts.json")


def _collect_recency_boosts() -> dict[str, float]:
    """Run one watcher cycle; return {ring_id: max_new_amount} for each ring hit."""
    from src.agents import analyst, resolver, state, validators
    from src.agents.watchers.cra_donees import CraDoneesWatcher
    from src.agents.watchers.cra_t3010 import CraT3010Watcher
    from src.agents.watchers.fed_grants import FedGrantsWatcher

    print("[refresh] running watcher cycle ...")
    state.init_db()
    watchers = [FedGrantsWatcher(), CraT3010Watcher(), CraDoneesWatcher()]
    boosts: dict[str, float] = {}

    for w in watchers:
        try:
            rows = w.fetch_new()
        except Exception as exc:
            print(f"[refresh] {w.name}: fetch failed: {exc}")
            continue
        if not rows:
            print(f"[refresh] {w.name}: 0 new rows")
            continue

        valid_rows, _ = validators.validate_rows(w.name, rows, validators.new_batch_id())
        if not valid_rows:
            print(f"[refresh] {w.name}: 0 valid rows")
            continue

        try:
            resolved_meta = resolver.resolve_batch_with_metadata(valid_rows, w.name)
            resolved = {ext_id: item["entity_ids"] for ext_id, item in resolved_meta.items()}
        except Exception as exc:
            print(f"[refresh] {w.name}: resolve failed: {exc}")
            continue

        rows_by_id = {str(r.get(w.external_id_field)): r for r in valid_rows}
        try:
            findings = analyst.analyze(resolved, w.name, rows_by_id)
        except Exception as exc:
            print(f"[refresh] {w.name}: analyze failed: {exc}")
            continue

        for f in findings:
            ring_id = f.get("ring_id") or ""
            amount = float(f.get("total_amount") or 0.0)
            if ring_id:
                boosts[ring_id] = max(boosts.get(ring_id, 0.0), amount)

        print(
            f"[refresh] {w.name}: {len(rows)} fetched / {len(valid_rows)} valid"
            f" / {len(findings)} ring hits"
        )

    print(f"[refresh] {len(boosts)} distinct rings with fresh activity")
    return boosts


def main() -> int:
    t0 = time.time()
    print(f"[refresh] starting weekly refresh at {time.strftime('%Y-%m-%d %H:%M:%S')}")

    # 1. Fetch new public data and find which rings have fresh activity
    boosts = _collect_recency_boosts()
    FRESH_BOOSTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    FRESH_BOOSTS_PATH.write_text(json.dumps(boosts, indent=2))

    # 2. Rebuild main ring cache with recency boosts applied
    print("[refresh] rebuilding ring cache ...")
    from scripts.prewarm import main as prewarm_main
    ret = prewarm_main(recency_boosts=boosts)
    if ret != 0:
        print(f"[refresh] prewarm failed (code {ret})")
        return ret

    # 3. Rebuild crossover cache
    try:
        print("[refresh] rebuilding crossover cache ...")
        from scripts.precompute_crossover import main as crossover_main
        crossover_main()
    except Exception as exc:
        print(f"[refresh] crossover rebuild failed (non-fatal): {exc}")

    elapsed = time.time() - t0
    print(f"[refresh] all caches rebuilt in {elapsed:.0f}s")

    # 4. Commit and push updated cache files
    cache_files = [
        "data/cache/top_rings.json",
        "data/cache/fresh_boosts.json",
        "data/cache/crossover.parquet",
    ]
    existing = [f for f in cache_files if Path(f).exists()]
    try:
        subprocess.run(["git", "add"] + existing, check=True)
        staged = subprocess.run(["git", "diff", "--cached", "--quiet"], capture_output=True)
        if staged.returncode != 0:
            stamp = time.strftime("%Y-%m-%d")
            msg = f"Weekly refresh {stamp}: {len(boosts)} rings with fresh activity"
            subprocess.run(["git", "commit", "-m", msg], check=True)
            subprocess.run(["git", "push"], check=True)
            print(f"[refresh] committed and pushed: {msg}")
        else:
            print("[refresh] no cache changes to commit")
    except subprocess.CalledProcessError as exc:
        print(f"[refresh] git step failed (non-fatal): {exc}")

    print(f"[refresh] done. total time: {time.time() - t0:.0f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
