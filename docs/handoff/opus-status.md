# Opus session status — pre-demo

Generated 2026-04-29 13:15 (T-~75 min to demo).

## Tasks

| ID | Task | Status | Notes |
|----|------|--------|-------|
| A  | Director-ring path | ✅ partial | Added `_load_director_pairs` + ring_type tags in `src/graph/builder.py`. The dedicated `precompute_rings.py` SQL hung on the live DB (gift_pairs ⨯ director self-join too heavy). **Pivoted:** the shared-director tab is populated via CRA-cycle rings whose participants share a director (computed in `scripts/prewarm.py`). 22 of 30 cached rings have shared directors. |
| B  | Three-section UI | ✅ done | `app/main.py` now renders three `st.tabs`: round-trip, shared-director, contractor crossover. Tab 3 is a stub with a clear "deferred — see AGENCY2026_CHALLENGE6.md §5 Rule R3" caveat. |
| C  | R3 procurement crossover | ⏭ skipped | Skipped per the time budget. Caveat in tab 3 makes the limitation explicit. |
| D  | Pre-warm cache | ✅ done | `scripts/prewarm.py` writes `data/cache/top_rings.json` with 30 rings (22 with shared directors) in ~25 s. App's `_load_live_rings` reads cache first; cache TTL 3600 s. |
| E  | Agent fleet smoke run | ✅ done | `data/agent_state.db` already has **292 findings**, including 5 urgent multi-million-dollar cases. Live-findings expander will populate. No re-run needed. |

## Verified

- `PYTHONPATH=. .venv/bin/python -c "import app.main"` → ok
- `PYTHONPATH=. .venv/bin/streamlit run app/main.py --server.headless true --server.port 8599` → HTTP 200, no errors in log
- `data/cache/top_rings.json` → 20,904 bytes, 30 rings
- `data/agent_state.db` → 292 findings, 5 urgent

## Artifacts

- `data/cache/top_rings.json` — pre-warmed top rings (must commit? **no** — gitignored, regenerate with prewarm)
- `data/agent_state.db` — agent state (gitignored)
- `streamlit_app.py` — Streamlit Cloud entrypoint shim

## Known limitations (in priority order)

1. **Director-pair-only rings (no CRA cycle)** are not in the cache — the dedicated query was too slow. Most legitimate director-network signals are still surfaced via CRA-cycle rings with shared directors.
2. **R3 (contractor / charity-director crossover)** is not implemented. Tab 3 carries the caveat.
3. **Director name matching** uses normalized name only — common-name collisions possible. Stated in the UI footer.
4. **Alberta corporate registry** is out of scope. Stated in the UI footer.

## Run commands (for the human, demo day)

```bash
# Re-warm the cache (run 5 minutes before demo)
PYTHONPATH=. .venv/bin/python -m scripts.prewarm

# Launch the demo
PYTHONPATH=. .venv/bin/streamlit run app/main.py
```

## Submission

The repo is already pushed to `git@github.com:Kyrinov/hackathon.git` (branch
`main`). Submit that URL to the MS Form. See `docs/handoff/deploy.md` for
optional Streamlit Cloud deployment.
