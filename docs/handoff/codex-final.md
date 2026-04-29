# Codex — Hackathon finalization tasks

You are working in `/home/charles/agency_hack_2026`, Python 3.13, Streamlit
demo for **Agency 2026 Challenge #6** (related-party governance networks).
The demo ships in **~2 hours**. Do **only** the tasks listed below. Do not
add features, refactor surrounding code, or touch SQL.

## Ground rules
- **Do not** edit `src/db/queries.py`, `src/score/scorer.py`, or
  `src/graph/builder.py`. Heavy lifts are owned by another model.
- **Do not** push to git. Stage changes; the human commits.
- After each task, run the test/typecheck command listed under
  *Acceptance* and stop on failure — flag it instead of working around it.
- Single-file edits where possible. Prefer the `Edit` tool over `Write`.
- No new dependencies.

---

## Task 1 — Repo hygiene

Move demo-day artifacts out of the project root and tighten `.gitignore`.

**Move (use `git mv`)** into `docs/internal/`:
- `Hackathon_pre_demo.mkv`
- `excalidraw.log`
- `hack.txt`
- `agency_hack_2026_context`
- `.codex`

**Append to `.gitignore`** (one block, with a comment header):
```
# Hackathon-day artifacts and tooling state
docs/internal/
agency-26-hackathon/
data/agent_state.db
.claude/
.claude-flow/
.swarm/
.planning/
.streamlit/credentials.toml
```

**Acceptance**
- `git status --porcelain | grep -E "(Hackathon_pre_demo|excalidraw|hack\.txt|agency_hack_2026_context|\.codex)"` returns nothing.
- `git check-ignore agency-26-hackathon/ data/agent_state.db .planning/` prints all three.
- `.env` is still gitignored.

---

## Task 2 — Delete empty medallion dirs

Remove the empty placeholder directories under `data/`. Keep `data/cache/`
(another agent will write parquet there).

**Delete:** `data/bronze/`, `data/silver/`, `data/gold/`, `data/raw/`,
`data/staged/`, `data/validation/`, `data/duckdb_tmp/`.

**Acceptance**
- `ls data/` shows only `cache/` and `agent_state.db` (and any `.gitkeep`
  you choose to add to `cache/`).
- `git status` shows the deletions staged.

---

## Task 3 — Strip dead UI controls

In `app/main.py`:
1. Remove the **"Use demo data"** checkbox (sidebar) and **all** code
   paths that consume `use_demo`. The demo data is only acceptable as a
   fallback when `DATABASE_URL` is unset — do not present it as a user
   option.
2. Remove the **"Max ring depth"** slider (it is not wired to anything).
3. In `_load(use_demo)`, change the signature to `_load()` and only
   fall back to demo data inside the `except Exception` branch.
4. Leave the score-threshold slider, the "Featured cases" checkbox,
   and everything else untouched.

**Acceptance**
- `PYTHONPATH=. .venv/bin/python -c "import app.main"` succeeds.
- `grep -n "use_demo\|Max ring depth\|Use demo data" app/main.py` returns
  nothing.
- App still launches: `PYTHONPATH=. .venv/bin/streamlit run app/main.py
  --server.headless true --server.port 8599 &` then `curl -s
  localhost:8599 | head -c 200` returns HTML; kill the process after.

---

## Task 4 — Connection error message

In `src/db/connection.py`, in `_new_conn()`, replace the bare
`os.environ["PGHOST"]` access with an explicit guard. If neither
`DATABASE_URL`/`DB_CONNECTION_STRING` nor a complete `PGHOST/PGUSER/
PGPASSWORD/PGDATABASE` set is present, raise:

```python
raise RuntimeError(
    "DATABASE_URL not set; load event-day .env (see README) "
    "or run with `use_demo=True` for the synthetic demo dataset."
)
```

Keep the existing happy paths identical.

**Acceptance**
- `unset DATABASE_URL DB_CONNECTION_STRING PGHOST PGUSER PGPASSWORD
  PGDATABASE && PYTHONPATH=. .venv/bin/python -c "from src.db.connection
  import get_conn; get_conn()"` exits non-zero with the new message, not
  with `KeyError`.
- With the existing `.env` loaded, the connection still opens.

---

## Task 5 — Mark offline scripts; delete duplicate

1. Add this comment block at the top of
   `scripts/run_splink_resolution.py` (above the existing imports):
   ```python
   """OFFLINE-ONLY: organizers ran Splink for this hackathon. Do not run
   on demo day — the entity resolution step takes ~45 min and writes to
   the shared DB. Kept for reproducibility only.
   """
   ```
2. Delete `scripts/seed_agents.py` — it is a thin wrapper around
   `scripts/run_agents.py --once` and adds nothing.

**Acceptance**
- `head -5 scripts/run_splink_resolution.py` shows the new docstring.
- `scripts/seed_agents.py` does not exist.
- `PYTHONPATH=. .venv/bin/python -m scripts.run_agents --help` still works.

---

## Task 6 — Rename `medallion.py` → `validators.py`

The file is a Pydantic bronze-validation layer. The "medallion"
naming oversells it.

1. `git mv src/agents/medallion.py src/agents/validators.py`
2. Update the one importer: `src/agents/scheduler.py` —
   `from src.agents import analyst, briefer, medallion, resolver, state`
   becomes `from src.agents import analyst, briefer, validators, resolver, state`,
   and every `medallion.X` call in that file becomes `validators.X`.
3. Update any test imports under `tests/` that reference
   `src.agents.medallion`.

**Acceptance**
- `grep -rn "medallion" src/ scripts/ tests/ app/` returns nothing.
- `PYTHONPATH=. .venv/bin/python -c "from src.agents import validators;
  print(validators.utcnow_iso())"` prints a timestamp.

---

## Task 7 — Test triage

Run `PYTHONPATH=. .venv/bin/pytest tests/ -x --timeout=60` (install
`pytest-timeout` if missing — `uv pip install pytest-timeout`).
For **each** failing test:

1. Read the failure once.
2. If it is a fast logic fix (≤3 lines, no behavioural change), fix it.
3. Otherwise add `@pytest.mark.skip(reason="hackathon: <one-line>")`
   above the function. Do not delete tests.

**Acceptance**
- `pytest tests/` exits 0.
- A short `tests/SKIPPED.md` file lists each skipped test name and reason.

---

## Task 8 — Repo-root README

Replace whatever is at the repo root with a single-page `README.md`
aimed at hackathon judges. Use the structure below verbatim. Fill in
content from `docs/internal/hack.txt` §1 and §2 — do not invent.

```markdown
# Agency 2026 — Challenge #6: Related-Party Governance Networks

> **Who controls the entities that receive public money — and do they
> control each other?**

We cross-reference directors from CRA T3010 filings with federal grant
recipients and Alberta contract awardees to surface three patterns:

1. **Round-trip funding rings** — charity A gifts to charity B, which
   gifts back to A, with a shared director.
2. **Shared-director networks** — one person sits on the boards of
   multiple publicly-funded entities that fund each other.
3. **Contractor / charity-director crossover** — a principal of a
   contract-receiving company is also a director of a charity receiving
   federal grants.

## Quick start

```bash
uv sync                                   # or: pip install -e .
cp .env.example .env                      # fill in DATABASE_URL + ANTHROPIC_API_KEY
PYTHONPATH=. .venv/bin/streamlit run app/main.py
```

## Architecture (one paragraph)

[paste from hack.txt §1 — "what we built / what organizers built"]

## Datasets

[CRA T3010, federal Grants & Contributions, Alberta open data — paste
from hack.txt §2; one row per schema with row counts]

## Decision-support framing

This is decision support, not decision making. Every flag is traceable
to a public-record source row. Director matching uses normalized name
only and may collide on common names. Alberta corporate registry is
out of scope.

## Demo

![screenshot](docs/screenshot.png)

## License

Code: MIT. Data follows original publishers' open-government licences.
```

Also create `.env.example` at the repo root containing only:
```
DATABASE_URL=
ANTHROPIC_API_KEY=
```

**Acceptance**
- README is ≤180 lines, no marketing.
- Renders in `glow README.md` without warnings (or `mdcat`).
- `.env.example` exists and contains no real values.
- A `docs/screenshot.png` placeholder is created (1×1 PNG is fine; the
  human will replace it).

---

## When you finish

Print a one-block summary of:
- Tasks completed (1–8).
- Tests skipped count.
- Any task you flagged instead of completing, with the exact error.

Do **not** run `git commit`. Stop and hand back to the human.
