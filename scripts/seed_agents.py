"""Pre-populate findings table for demo robustness.

Runs one full cycle (FED + CRA watchers → resolver → analyst) and persists findings.
Idempotent: if findings already exist, can be run again to add fresh ones.
"""

from __future__ import annotations

import argparse
import sys

from src.agents import scheduler, state
from src.agents.watchers.cra_donees import CraDoneesWatcher
from src.agents.watchers.cra_t3010 import CraT3010Watcher
from src.agents.watchers.fed_grants import FedGrantsWatcher


def main() -> int:
    parser = argparse.ArgumentParser(description="Seed the agent findings table.")
    parser.add_argument(
        "--no-brief",
        action="store_true",
        help="Skip Anthropic briefer call (use seed narratives only)",
    )
    parser.add_argument(
        "--sources",
        nargs="*",
        default=["fed", "cra_t3010", "cra_donees"],
        help="Which watchers to run (default: all)",
    )
    args = parser.parse_args()

    state.init_db()
    pre = state.count_findings()
    print(f"[seed] starting with {pre} findings in db")

    pool = {
        "fed": FedGrantsWatcher(),
        "cra_t3010": CraT3010Watcher(),
        "cra_donees": CraDoneesWatcher(),
    }
    selected = [pool[k] for k in args.sources if k in pool]
    if not selected:
        print(f"[seed] no valid sources in {args.sources}", file=sys.stderr)
        return 1

    n = scheduler.run_cycle(selected, persist=True, brief_findings=not args.no_brief)
    post = state.count_findings()
    print(f"[seed] cycle produced {n} findings; total now {post}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
