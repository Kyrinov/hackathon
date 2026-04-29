# Kimi — Hackathon UI copy and prose tasks

You are working in `/home/charles/agency_hack_2026`. The demo ships in
**~2 hours**. Do **only** the tasks listed below. They are all
copy/prose changes — no logic, no SQL, no new files unless noted.

## Ground rules
- All tasks are **single-file edits** in `app/main.py`,
  `src/agents/narrator.py`, or `src/agents/briefer.py`.
- Do **not** change function signatures, imports, or control flow.
- Do **not** invent facts about the data — only rephrase what the
  code already says.
- After each task, run the import check noted under *Acceptance*.

---

## Task 9 — UI caveats footer

In `app/main.py`, at the very bottom of `main()` (after the "Top 10
Flagged Entities" `st.dataframe` call), add:

```python
st.markdown("---")
st.caption(
    "Decision support, not decision making. "
    "Every flag traces to a public-record source row "
    "(CRA T3010, federal Grants & Contributions, Alberta open data). "
    "Director matching uses normalized names only — common names may "
    "collide. Alberta corporate registry and former-public-servant "
    "cross-match are out of scope. "
    "This system flags patterns; it does not infer intent."
)
```

**Acceptance**
- `PYTHONPATH=. .venv/bin/python -c "import app.main"` succeeds.
- Streamlit renders the caption below the table (visual check).

---

## Task 10 — Page title and intro

In `app/main.py`, replace the existing `st.title(...)` and the
following `st.caption(...)` call with:

```python
st.title("Who controls the entities that receive public money — and do they control each other?")
st.caption(
    "Agency 2026 · Challenge #6 — Related-Party Governance Networks. "
    "Three detection patterns: round-trip funding rings, shared-director "
    "networks, and contractor / charity-director crossover. "
    "All findings traceable to CRA T3010, federal G&C, and Alberta open data."
)
```

Do not change `st.set_page_config` — keep `page_title="Agency 2026 -
Challenge #6"`.

**Acceptance**
- `grep -n "Who controls the entities" app/main.py` prints exactly one
  line.
- The old "Agency 2026 - Challenge #6: Related Party Networks" title
  string is gone.

---

## Task 11 — Sidebar coverage panel

In `app/main.py`, inside the `with st.sidebar:` block, **after** the
existing `threshold` slider and **before** the "Featured cases"
checkbox, add a `st.expander("Coverage", expanded=False)` that shows:

- **Datasets in use:** "CRA T3010 · federal G&C · Alberta open data"
- **Detection patterns:** "Round-trip · Shared director · Crossover"
- **Last live fetch:** the most recent `last_run_at` from `sources`
  (already loaded earlier in `main()` as `sources`); show `—` if none.
- **Live findings (24h):** `len(findings)` (already loaded as `findings`).

Use `st.write` or `st.metric` — do not add new DB queries. The values
must come from variables already defined in `main()`. If the variables
are not yet in scope at the sidebar block, lift them above the
`with st.sidebar:` block (one move, no new function).

**Acceptance**
- The expander renders with all four labels.
- No new `from src.db` imports.
- No DB calls inside the sidebar block.

---

## Task 12 — Tighten the narrator prompt

In `src/agents/narrator.py`, replace `SYSTEM_PROMPT` with:

```python
SYSTEM_PROMPT = """You write factual, minister-grade summaries for the
Agency 2026 Challenge #6 platform (related-party governance networks).
Inputs are JSON evidence drawn from CRA T3010, federal Grants &
Contributions, and Alberta open data, resolved to canonical entities
via Splink + Sonnet 4.6.

Rules:
- Use only the supplied evidence. Never infer wrongdoing or intent.
- Cautious public-sector language: "flagged for review", "the records
  show", "warrants closer review".
- Sentence 1: name the trigger source, the dollar amount, and the ring
  or director relationship.
- Sentence 2: name 2-3 of the linked entities and the dataset
  combination (e.g., "CRA T3010 directorship plus federal grants
  disclosure").
- Sentence 3 (optional): one specific check a reviewer should do next.
- 3 sentences max. No bullets. No headers."""
```

Keep `MODEL`, function signatures, and `briefer.py` untouched (the
briefer system prompt is fine as-is).

**Acceptance**
- `grep -c "minister-grade" src/agents/narrator.py` returns 1.
- `PYTHONPATH=. .venv/bin/python -c "from src.agents.narrator import
  SYSTEM_PROMPT; print(len(SYSTEM_PROMPT))"` prints a number > 400.

---

## When you finish

Print a single block listing:
- Each task touched and the file it modified.
- Any text you changed beyond the verbatim copy above (there should be
  none unless flagged).

Do **not** run `git commit`. Stop and hand back to the human.
