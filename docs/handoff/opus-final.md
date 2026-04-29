# Opus — Hackathon heavy lifts (continuation brief)

You are continuing work in `/home/charles/agency_hack_2026` on **Agency
2026 Challenge #6**. Demo ships in **~2 hours**. Codex and Kimi are
running tasks 1–12 in parallel (`docs/handoff/codex-final.md` and
`docs/handoff/kimi-final.md`). Your tasks are **A–E** below — each one
touches the SQL ↔ ring-builder ↔ scorer ↔ UI seam, where mistakes
silently empty the demo.

## Context recap (read this once)
- Live data lives in a remote Postgres on Render (read-only). Connection
  via `DATABASE_URL` in `.env`. Five schemas: `cra`, `fed`, `ab`,
  `general`, `public`.
- `general.entity_golden_records` (~851K rows) is the canonical entity
  layer, resolved by organizers via Splink + Sonnet 4.6. Do not touch.
- `cra.loops` (~67K rows) is the organizers' precomputed round-trip
  cycle table. Use it; don't recompute.
- `src/db/queries.py` has every SQL query. **Avoid editing existing
  functions** — add new ones below the existing ones.
- `app/main.py` has been simplified by Codex (Task 3) — `use_demo` and
  the depth slider are gone. Page title rewritten by Kimi (Task 10) to
  match the challenge prompt. Account for that when you read it.
- The challenge prompt (verbatim): *"Who controls the entities that
  receive public money, and do they also control each other?
  Cross-reference directors from CRA T3010 filings with corporate
  registries and contract data."* Three sub-asks: (a) shared directors
  on funding rings, (b) contractors who are charity directors,
  (c) former public servants — out of scope today.

## Read first
- `docs/internal/hack.txt` §3–§5 (query layer, graph layer, scoring).
- `AGENCY2026_CHALLENGE6.md` §5 (rules R1–R7) — your R3 in **Task C**
  maps to this doc's R3.

---

## Task A — Shared-director ring path (DEMO-CRITICAL)

The current `find_related_party_rings` in `src/graph/builder.py` only
uses CRA precomputed cycles. Re-introduce a second path that surfaces
*director networks* — the heart of the challenge.

**Steps**

1. **Pre-materialize the slow query.** Create
   `scripts/precompute_rings.py` that:
   - calls `queries.fetch_shared_director_funding_pairs(min_total_amount=100_000)`
   - writes the resulting Polars DataFrame to
     `data/cache/director_rings.parquet`
   - prints row count and elapsed time.
   Run it once. If it times out (>3 min), drop `min_total_amount` to
   `250_000` and retry.

2. **Add a loader** in `src/graph/builder.py`:
   ```python
   def _load_director_pairs() -> pl.DataFrame:
       path = Path("data/cache/director_rings.parquet")
       if not path.exists():
           return pl.DataFrame()
       return pl.read_parquet(path)
   ```

3. **Extend `find_related_party_rings`** to append director-pair rings
   *after* the CRA cycle rings. For each row:
   ```python
   {
       "ring_id": f"director-pair-{director_norm}-{a}-{b}",
       "entity_ids": [str(a), str(b)],
       "canonical_names": [name_a, name_b],
       "shared_persons": [director_norm],
       "funding_edges": [],  # will be enriched in scorer.top_rings
       "evidence": [{"source": "cra_director_funding",
                     "source_row_id": row["source_row_id"],
                     "mapping_method": "authoritative",
                     "confidence_score": 1.0}],
       "total_amount": float(row["total_amount"]),
       "datasets_touched": ["cra"],
       "flags": ["Director controls multiple funded entities"],
       "ring_type": "shared_director",
   }
   ```
   Add `"ring_type": "round_trip"` to the existing CRA-cycle rings for
   parity.

4. **Dedupe** by sorted `entity_ids` tuple — keep the higher
   `total_amount`. The `_KNOWN_NATIONAL` filter still applies.

**Acceptance**
- `data/cache/director_rings.parquet` exists with ≥20 rows.
- `find_related_party_rings()` returns rings with both
  `ring_type="round_trip"` and `ring_type="shared_director"`.
- The total returned rings is unchanged or larger; nothing previously
  shown is dropped.
- No new live SQL added to the request path — director rings load from
  parquet only.

---

## Task B — Three-section UI

Restructure `app/main.py` so a judge reading the challenge prompt sees a
1:1 answer on screen.

**Steps**

1. After the title/intro (already rewritten by Kimi) and the live-
   findings expander, add **`st.tabs(["Round-trip rings",
   "Shared-director networks", "Contractor crossover"])`**.
2. **Tab 1** — current behaviour: featured CRA cycles + case dossier +
   network graph. Filter `top_rings()` results to
   `ring_type == "round_trip"`.
3. **Tab 2** — new: filter to `ring_type == "shared_director"`. Render
   the same `_ring_graph` / dossier components. Replace the "CRA cycle
   flow" metric with "Director linking entities". Use the
   `shared_persons[0]` as the ring caption.
4. **Tab 3** — placeholder if Task C doesn't ship: show the text "Rule
   R3 (procurement crossover) is implemented in Task C — see flagged
   entities in the Top 10 table below where flags include
   `Contractor / charity-director crossover`." If C ships, render the
   query result as a table.
5. Keep the live-findings expander **above** the tabs — it already
   maps any tab's ring via `_graph_for_ring`.
6. Keep the bottom "Top 10 Flagged Entities" table; do not duplicate it
   per tab.

**Acceptance**
- Three tabs render with non-empty content (Tab 3 may be a placeholder).
- Toggling tabs does not refetch live data — `@st.cache_data` is
  preserved on `_load_live_rings`.
- The score-threshold slider still controls node highlighting in all
  three tabs.

---

## Task C — Rule R3: procurement crossover (only if A+B stable)

Drop this task if A+B are not solid by **T-90 min**. Caveat in the UI is
already in place for that case.

**Steps**

1. Add to `src/db/queries.py`:
   ```python
   def fetch_director_procurement_crossover(min_amount: float = 100_000) -> pl.DataFrame:
       """Entities that received a contract (FED contract, AB contract, or
       AB sole-source) AND share a director name with a charity that
       received a federal grant.
       """
   ```
   Strategy: build a CTE of `director_norm → entity_id` for charity
   golden records linked to `fed.grants_contributions` (recipients).
   Build a second CTE for entities linked to AB contracts / sole-source
   or FED contracts. Inner-join on `director_norm`. Aggregate by
   contractor entity, keep `total_amount >= min_amount`. Return columns:
   `contractor_entity_id, contractor_name, charity_entity_id,
   charity_name, director_norm, total_contract_amount, total_grant_amount`.

2. Pre-materialize to `data/cache/crossover.parquet` via the same
   `scripts/precompute_rings.py`.

3. Add a scoring rule in `src/score/rules.py`:
   ```python
   def rule_director_procurement_crossover(entity_id) -> tuple[float, str, list]:
       # +0.4 if entity_id appears as contractor_entity_id OR
       # charity_entity_id in the crossover parquet.
       # Flag: "Contractor / charity-director crossover"
   ```
   Wire into `score_entity` in `src/score/scorer.py`.

4. **Tab 3 in the UI** — render the parquet contents as a Polars table
   with click-through to entity detail.

**Acceptance**
- `data/cache/crossover.parquet` exists.
- At least 5 entities flagged with the new rule appear in the Top 10
  list.
- The new rule's evidence rows have `mapping_method="authoritative"`
  for the BN joins and `splink_*` for the resolution layer.

---

## Task D — Pre-warm + cache choreography

Make the demo's first interaction fast and reproducible.

**Steps**

1. Create `scripts/prewarm.py`:
   ```python
   from src.score.scorer import top_rings
   from src.graph.builder import find_related_party_rings
   import json, time, pathlib
   t = time.time()
   rings = top_rings(20)
   pathlib.Path("data/cache/top_rings.json").write_text(json.dumps(rings, default=str))
   print(f"prewarmed {len(rings)} rings in {time.time()-t:.1f}s")
   ```

2. In `app/main.py`'s `_load_live_rings()`, **read from cache first**:
   ```python
   cache = Path("data/cache/top_rings.json")
   if cache.exists():
       return json.loads(cache.read_text())
   return top_rings(20)
   ```
   Bump the Streamlit `@st.cache_data(ttl=...)` to 3600.

3. Run `scripts/prewarm.py`. Confirm ≥10 rings returned, mix of
   `round_trip` and `shared_director` types.

4. Run `scripts/precompute_rings.py` if not already done in Task A.

**Acceptance**
- `data/cache/top_rings.json` exists with ≥10 rings.
- Cold Streamlit launch (`streamlit run app/main.py`) renders the first
  ring graph in <3 s.
- A subsequent live SQL call against `cra.loops` is **not** in the
  request path of the home page.

---

## Task E — Agent fleet smoke run

Seed `data/agent_state.db` with realistic findings.

**Steps**

1. `PYTHONPATH=. .venv/bin/python -m scripts.run_agents --once` (single
   cycle, no loop).
2. Inspect:
   ```bash
   PYTHONPATH=. .venv/bin/python -c "from src.agents import state;
   print('findings:', state.count_findings());
   for f in state.list_findings(limit=5):
       print(f['source'], f['severity'], f.get('total_amount'),
             (f.get('narrative') or '')[:80])"
   ```
3. Confirm at least one finding per source (`fed_grants`, `cra_t3010`,
   `cra_donees`). If a source returned 0 rows, accept it — note in the
   handoff. If a source crashed, capture the error and stop.
4. Confirm that opening the Streamlit app renders the "Live findings"
   expander with non-empty content.

**Acceptance**
- `state.count_findings()` ≥ 5.
- At least one finding has `severity="urgent"` or its `total_amount` ≥
  $1M (this triggers the urgent badge in the UI).
- No tracebacks in the run-agents output.

---

## Sequencing (your own clock)

| T-min | Task |
|------:|------|
| 0     | Read this file, `AGENCY2026_CHALLENGE6.md` §5, `hack.txt` §3–§5 |
| 5     | **Task A** — kick `precompute_rings.py` in background; while it runs, edit `builder.py`, then verify against the parquet |
| 35    | **Task B** — restructure `app/main.py` into three tabs |
| 75    | **Task C** *(optional)* — only if A+B clean and >75 min remain |
| 100   | **Task D** — prewarm script + cache-first read |
| 115   | **Task E** — agent smoke run |
| 125   | Buffer / hand back to human for the deploy step (`docs/handoff/deploy.md`) |

## Hand-off rules
- **Do not commit.** Stage changes; the human commits.
- If a task is at risk, ship the partial: a working three-tab UI with
  empty Tab 3 beats a broken Tab 3.
- Update the **Coverage** sidebar (Kimi's Task 11) with any new totals
  if you change `find_related_party_rings`'s output.
- When you finish, write `docs/handoff/opus-status.md` with: tasks
  done, tasks skipped, parquet row counts, anything the deploy step
  needs to know.
