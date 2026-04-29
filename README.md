# Agency 2026 — Challenge #6: Related-Party Governance Networks

> **Who controls the entities that receive public money — and do they
> control each other?**

A Streamlit decision-support tool for Canadian public-sector accountability
analysts. We cross-reference CRA T3010 director filings, federal Grants &
Contributions, and Alberta contract data to surface four governance
patterns:

| Tab | Pattern | Detection signal |
| --- | --- | --- |
| (a) | **Round-trip funding rings** | Charity A gifts to B which gifts back to A, via CRA's pre-computed `cra.loops` cycle table |
| (b) | **Shared-director networks** | A person sits on the boards of multiple publicly-funded entities that also fund each other |
| (c) | **Contractor / charity-director crossover** (Rule R3) | A CRA T3010 director of a federal-grant recipient is also a director of an Alberta contract or sole-source recipient |
| (d) | **Live agent findings** | An autonomous agent fleet polls open.canada.ca and triggers when a fresh disbursement resolves into a known ring |

Every finding traces to a public source row (CRA T3010, federal G&C, or
Alberta open data). The system flags structural patterns — never infers
intent — consistent with the Treasury Board *Directive on Automated
Decision-Making*.

## Quick start

```bash
uv sync                                   # or: pip install -e .
cp .env.example .env                      # fill in DATABASE_URL + ANTHROPIC_API_KEY

# Pre-warm the cache (~30 s, hits cra.loops + cra.cra_directors once)
PYTHONPATH=. .venv/bin/python -m scripts.prewarm

# Optional — re-run R3 contractor-crossover precompute (~2 min)
PYTHONPATH=. .venv/bin/python -m scripts.precompute_crossover

# Optional — refresh agent findings from open.canada.ca (~1 min)
PYTHONPATH=. .venv/bin/python -m scripts.run_agents --once

# Launch the demo
PYTHONPATH=. .venv/bin/streamlit run app/main.py
```

## Architecture

**Hackathon organizers provided** a PostgreSQL database on Render with
four schemas — `cra`, `fed`, `ab`, and `general` — including pre-computed
CRA funding loops (`cra.loops`, ~67k cycles), Splink+Sonnet-resolved
canonical entities (`general.entity_golden_records`, ~851k records), and
`general.entity_source_links` for full row-level provenance.

**We built the analysis and review layer on top:**

- **`src/db/queries.py`** — parameterised SQL → Polars DataFrames
- **`src/graph/builder.py`** — NetworkX `MultiDiGraph` construction with
  director person nodes, gift edges, and cross-source funding edges
- **`src/score/rules.py` + `scorer.py`** — five deterministic risk rules
  (round-trip cycle, shared director, sole-source pattern, federal HHI,
  related-entity funding) producing a 0–1 risk score per entity
- **`src/agents/`** — autonomous fleet (`watchers`, `scheduler`,
  `analyst`, `briefer`, `narrator`) polling open.canada.ca and surfacing
  new disbursements that resolve into known rings; SQLite-backed state
- **`scripts/prewarm.py` + `precompute_crossover.py`** — pre-materialised
  caches under `data/cache/` so the demo's first paint is instant
- **`app/main.py`** — Streamlit four-tab review UI with pyvis network
  visualizations, click-through evidence panels, and a live agent feed

## Datasets

| Schema | Tables of record | Approx rows |
| --- | --- | --- |
| `general` | `entity_golden_records`, `entity_source_links`, Splink artifacts | ~851k canonical entities |
| `cra` | `cra_directors`, `cra_qualified_donees`, `loops`, T3010 financials | ~67k cycles, millions of director rows |
| `fed` | `grants_contributions` (federal Open Government Portal) | ~1.3M agreements |
| `ab` | `ab_grants`, `ab_contracts`, `ab_sole_source` | ~9k+ AB awards |

## Decision-support framing

This is decision support, not decision making. Every flag traces to a
public-record source row. The system surfaces structural patterns and
**does not infer intent**, consistent with the Treasury Board *Directive
on Automated Decision-Making*. A human reviewer with investigative
authority decides what to do with each flag.

## Design decisions

- **Cache-first reads.** The remote Render Postgres has 50–200 ms
  per-query round-trip latency and the heavy director joins take 30–120 s.
  We pre-materialise rings (`data/cache/top_rings.json`) and the R3
  crossover (`data/cache/crossover.parquet`), then read them in-memory at
  request time. The demo's first paint is instant; live queries are a
  fallback path.
- **Two-step join for R3.** A single SQL self-joining all CRA directors
  on normalised name was the obvious approach and the wrong one — it ran
  for 10+ minutes against the live DB. We pivoted to (1) "contractors
  with their CRA directors" first (small set), then (2) "for *those*
  director names, find which also direct grant-receiving charities".
  Two-pass cuts wall time from minutes to ~2 minutes total.
- **Shared-director tab re-uses CRA-cycle rings.** Building a separate
  director-pair detection layer was scoped but the dedicated SQL didn't
  return in usable time. Instead we filter cached round-trip rings to
  those whose participants share a director, and re-draw them with the
  person node as a focal point (`view="director_network"` in
  `_graph_for_ring`). Tabs (a) and (b) intentionally overlap; that
  overlap is the strongest signal.
- **DADM framing in language and UI.** Every flag carries a "flagged for
  review" caption, never an accusation. Scoring rules add to a 0–1 risk
  indicator that is explicitly *not* a probability. The UI footer states
  this on every render.
- **Polars over pandas, NetworkX `MultiDiGraph`.** Polars handles JSONB
  columns and multi-million-row scans without index footguns; the demo
  also avoids any cross-dataframe surprises. `MultiDiGraph` is necessary
  because a person can direct multiple orgs and an org can receive
  multiple grants from the same department in different years.
- **Haiku for narratives, prompt caching for cost.** Briefings use
  `claude-haiku-4-5-20251001` with a cached system prompt — about
  $0.0002 per finding. Sonnet/Opus weren't justified: the narrator
  summarises structured JSON, no reasoning required.
- **OSIC vs TBS jurisdictional split is preserved in risk labels.** The
  Procurement Integrity Regime (OSIC) and the Directive on Transfer
  Payments are statutorily distinct in Canada. Tab (c) crossover findings
  cross both regimes — we surface them but never blur the legal
  boundary in the language of the flag.

## Limitations

- **Director name collisions.** Matching is `lower(trim(first || ' ' ||
  last))`. Two unrelated "John Smith" entries in different charities
  will look like a shared director. The same caveat appears in the UI
  footer; reviewers must verify identity before acting.
- **No BO/PSIC/CBCA cross-reference.** Corporations Canada has no public
  API or bulk download as of April 2026; Alberta's corporate registry is
  paid-only and has no beneficial-ownership transparency law; the Public
  Sector Integrity Commissioner's registry of former public servants is
  not loaded. Pattern (c) of the original challenge prompt — "former
  public servants connected to entities funded by their former
  departments" — is therefore **out of scope**.
- **R3 surfaces only T3010-registered contractors.** Rule R3 needs a
  CRA T3010 director record on *both* sides of the match, so a
  pure-commercial contractor that never filed a T3010 is invisible
  even if its principal also directs a federal-grant-receiving charity.
  This is a structural data gap, not a bug.
- **T3010 publication lag.** CRA charity filings appear 9–18 months
  after fiscal year end. A director change made in early 2026 may not
  show in `cra.cra_directors` until late 2027. Temporal reasoning
  requires year-aligned joins.
- **Scoring weights are judgment-based.** The five rules each contribute
  a fixed weight (0.25–0.40) capped at 1.0. The weights are not trained
  against a labelled fraud dataset because no public Canadian dataset of
  "confirmed related-party abuse" exists. The score is a triage signal,
  not a probability.
- **The $50k–$100k thresholds are arbitrary.** They were chosen to
  filter out parish-level noise. Lowering them surfaces more rings and
  more false positives.
- **Live agent fleet polls three sources.** `fed_grants`, `cra_t3010`,
  `cra_donees` are wired (`src/agents/watchers/`). CanadaBuys procurement
  feeds, lobbying registrations, and Alberta proactive disclosure are
  not yet polled — they would slot in as additional `BaseWatcher`
  subclasses.
- **No ground-truth validation.** The 200 R3 crossover pairs and 30
  pre-warmed rings were not validated against a known-positive
  list (SDTC, GC Strategies, Dalian/Coradix). Building that validation
  set is the natural next step.

## Demo

![screenshot](docs/screenshot.png)

A pre-recorded walkthrough lives in `docs/internal/Hackathon_pre_demo.mkv`
(MKV → MP4 conversion via `ffmpeg -c copy` if you need the embeddable
form for GitHub).

## License

Code: MIT. Data follows the original publishers' open-government licences
(see `docs/internal/agency-26-hackathon/ATTRIBUTIONS.md` from the
organizers' repo).

## Submission

Built for the **Agency 2026 hackathon, Ottawa, 29 April 2026**. Project
description, working notes, and handoff briefs are in `docs/handoff/`.
