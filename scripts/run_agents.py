from __future__ import annotations

import argparse

from src.agents import scheduler, state
from src.agents.watchers.cra_donees import CraDoneesWatcher
from src.agents.watchers.cra_t3010 import CraT3010Watcher
from src.agents.watchers.fed_grants import FedGrantsWatcher


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the agent fleet.")
    parser.add_argument("--interval", type=int, default=60, help="Seconds between cycles")
    parser.add_argument("--once", action="store_true", help="Run a single cycle and exit")
    args = parser.parse_args()

    state.init_db()
    watchers = [FedGrantsWatcher(), CraT3010Watcher(), CraDoneesWatcher()]

    if args.once:
        n = scheduler.run_cycle(watchers)
        print(f"cycle complete: {n} new findings")
    else:
        scheduler.run_loop(watchers, interval_seconds=args.interval)


if __name__ == "__main__":
    main()
