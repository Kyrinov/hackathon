# Agency 2026 — Challenge #6: Related Parties and Governance Networks
## Claude Code Working Context

> **Purpose:** This document is the authoritative context file for implementing a Canadian public-sector AI accountability system that detects related-party grant abuse, director-network governance schemes, and round-trip funding cycles. It is intended as persistent context for Claude Code sessions.

---

## 1. System Overview

### What the system detects
1. **Round-trip funding cycles** — Charity A grants to Charity B, which grants back to Charity A (or A's principals), using CRA T3010 data (Form T1236 edges)
2. **Shared directors controlling multiple publicly-funded entities** — interlocking board membership across grant recipients and vendors (Form T1235 + Corporations Canada nodes)
3. **Sole-source procurement patterns** — non-competitive awards to director-linked vendors (CanadaBuys ACAN/sole-source codes + proactive disclosure contracts)
4. **Federal and Alberta grants flowing to related parties** — TBS proactive disclosure + Alberta Grant Payments Disclosure cross-referenced against director/officer networks

### Core detection approach
Multi-layer heterogeneous graph with four node types and five edge types:

```
Nodes:  Person | Organization | Grant | Contract
Edges:  [Person]--DIRECTOR_OF-->[Organization]
        [Organization]--GRANT_TO-->[Organization]      (T1236/T1441)
        [Organization]--RECEIVED_GRANT-->[Grant]       (proactive disclosure)
        [Organization]--AWARDED_CONTRACT-->[Contract]  (CanadaBuys)
        [Person]--OFFICER_OF-->[Organization]          (corporate registry)
```

---

## 2. Ground Truth Training Cases

These are the only 2023–2026 Canadian cases with audited dollar values, named individuals, and documented director-network and sole-source patterns. Anchor all labelled data here.

### Case A — SDTC (Positive: Director-network + round-trip grants)
- **OAG Report 6, June 4 2024** — Sustainable Development Technology Canada
  - URL: https://www.oag-bvg.gc.ca/internet/English/parl_oag_202406_06_e_44493.html
  - Audit period: March 2017 – December 2023
  - Total approved: $856M across 420 projects
  - **90 conflict-of-interest breaches** in board minutes → ~$76M
  - **10 of 58 sampled projects ineligible** → $59M; 8 projects ($51M) did not support new technology
  - ISED (oversight department) failed to monitor the contribution agreement
- **Ethics Commissioner — Verschuren Report, July 24 2024**
  - URL: https://ciec-ccie.parl.gc.ca/en/investigations-enquetes/Pages/VerschurenReport-RapportVerschuren.aspx
  - Finding: Chair Annette Verschuren contravened **ss. 6(1) and 21 of the Conflict of Interest Act**
  - Director overlap: SDTC Chair + CEO/majority shareholder of NRStor + director of MaRS Discovery District + director of Verschuren Centre
  - Approved $217,661 directly to NRStor without recusal; voted on 11 of 21 projects nominated by accelerator boards she sat on
- **Key graph features to encode:**
  - `Verschuren` → DIRECTOR_OF → `{SDTC, NRStor, MaRS, Verschuren Centre}`
  - `SDTC` → GRANT_TO → `NRStor` (labelled: related-party, no recusal)
  - `SDTC` → GRANT_TO → `[MaRS portfolio companies]` (labelled: accelerator conflict)
- **PACP follow-up:** Reports 42 and 43 of the 44th Parliament

### Case B — ArriveCAN / GC Strategies (Positive: Sole-source + requirements-rigging)
- **OAG Report 1, February 12 2024** — ArriveCAN
  - URL: https://www.oag-bvg.gc.ca/internet/English/parl_oag_202402_01_e_44428.html
  - Cost: ~$59.5M (precise total undeterminable due to missing records)
  - 18% of contractor invoices lacked supporting documentation
  - GC Strategies awarded initial **non-competitive contract June 2020**, then participated in writing requirements for the May 2022 TBIPS competitive contract ($25.3M) it subsequently won
- **OAG Report 4, June 10 2025** — Professional Services Contracts with GCStrategies Inc.
  - URL: https://www.oag-bvg.gc.ca/internet/English/parl_oag_202506_04_e_44645.html
  - Scope: 31 federal organizations, **106 contracts**, April 2015 – March 2024
  - Max value: $92.7M; paid out: ~$64.5M
  - 21% of audited contracts: no security clearance docs; 33%: no qualification evidence
  - **GC Strategies banned from federal contracting 7 years, June 2025**
- **Procurement Ombudsman Report, January 2024**
  - URL: https://opo-boa.gc.ca/praapp-prorev/2024/epa-ppr-01-2024-eng.html
  - Mandatory criteria CM5/CM7 were "extremely narrow" and "heavily favoured" GC Strategies

### Case C — Dalian/Coradix (Positive: Indigenous procurement front-company)
- **ISC Internal Audit, May 22 2025** (posted September 2025)
  - 122 contracts totalling $189.5M (2011–2024) to Dalian Enterprises and Coradix Technology Consulting
  - Suspended from federal contracting March 2024
  - No comprehensive checklist for joint ventures; no specialized fraud-detection training
- **OAG audit pending** — announced November 2024, not yet tabled as of April 29 2026
- This case will be the next major related-party dataset when published

### Case D — WE Charity / CSSG (Pre-2023 baseline only)
- No federal accountability action completed 2023–2026; no OAG audit tabled; no RCMP charges; no CRA revocation
- Use only as historical pattern reference; Ethics Commissioner reports May 2021 are the terminus

---

## 3. Data Sources

### 3.1 Primary — CRA T3010 (charity network backbone)

| Form / Field | Content | Graph use |
|---|---|---|
| **T1235** — Directors/Trustees Worksheet | Director name, position, postal code, arm's-length status with other directors (Y/N), term dates, employee status | Person → DIRECTOR_OF → Org edges |
| **T1236** — Qualified Donees Worksheet | Donee BN, name, city, total cash amount | Org → GRANT_TO → Org edges (primary round-trip source) |
| **T1441** — Qualifying Disbursements to Non-Qualified Donees | Recipient name/address/project description/amount (post-2022) | Edges to non-charity entities; reveals related-party advocacy/foreign vehicles |
| **T2081** — Excess Corporate Holdings | Foundation holdings of non-arm's-length corporations ≥2% | Direct related-party flag |
| **Section C8 / Line 3200** | "Did the charity compensate any directors/trustees or non-arm's-length persons?" | Boolean related-party flag |
| **Schedule 6 / Line 4510** | Donations received from other registered charities | Cross-charity edge (incoming) |
| **Schedule 6 / Line 4570** | Non-arm's-length transactions revenue | Revenue flag |
| **Schedule 6 / Line 4630** | Government revenue (federal/provincial/municipal split) | Grant-source attribution |
| **Schedule 6 / Line 4860** | Professional and consulting fees | Where related-party services hide |
| **Schedule 6 / Lines 5040/5045/5050** | Gifts to qualified donees / grants to non-qualified donees / total gifts | Outbound grant edges |

**Data quality issues to handle in pipeline:**
- Free-text director names with **no unique person identifier** (DOB in confidential Section F — not in bulk data)
- **9–18 month publication lag** behind fiscal year end
- No canonical CRA BN → CBCA/provincial corporate number crosswalk (biggest engineering problem)
- ~80,000–100,000 non-charitable nonprofits file no T3010
- Line-number drift across years — use year-aware schema
- T1236 captures aggregate amounts only, not grant purpose (T1441 captures purpose for non-qualified-donees only)
- Self-reported, no audit; weak schedule completeness enforcement

**Key research resources:**
- McMaster SEAL repository (cleaned longitudinal T3010 + T1235 + T1236): Borealis DOI `10.5683/SP2/QXWUAZ`
- Carleton T3010 Research Group (Brouard): https://carleton.ca/profbrouard/t3010researchgroup/
- IJ Foundation cleaned T3010 (1990–present, normalized line numbers): https://theijf.org/charities-databases-methodology
- CharityData.ca (Blumbergs) — free public search of ~600K T3010 directors
- CRA charity data bulk download: https://www.canada.ca/en/revenue-agency/services/charities-giving/charities-listings.html

### 3.2 Federal Proactive Disclosure (open.canada.ca)

| Dataset | ID | Threshold | Key fields for detection |
|---|---|---|---|
| Grants & Contributions consolidated | `432527ab-7aac-45b5-81d6-7597107a7013` | >$25,000 | recipient BN, recipient type, postal code, agreement number/value/dates, program/dept |
| Contracts | `53753f06-8b28-42d6-89d2-da34f9e9d12f` | >$10,000 | vendor BN, solicitation procedure, amendment history |
| CanadaBuys award notices | `a1acb126-9ce8-40a9-b889-5da2b1dd20cb` | daily refresh | `solicitationProcedure`, `limitedTenderingReason` → ACAN/sole-source flags |
| CanadaBuys contract history | `4fe645a1-ffcd-40c1-9385-2c771be956a4` | PSPC since Jan 2009 | contract amendments, option exercises |
| Public Accounts Vol III s.6 | `69bdc3eb-e919-4854-bc52-a435a3e19092` | $100,000/recipient/program | catches fragmented sub-$25K awards |
| Lobbying Registrations | `70ef2117-1095-4d7b-46e0-8a0e-40c2d520eac7` | all since 1996 | **government-funding flag**, prior public office holder → revolving-door detection |

> Note: `limitedTenderingReason` codes on CanadaBuys award notices identify ACAN, emergency, single-source justifications. These are the primary procurement sole-source flag.

### 3.3 Alberta-Specific

| Source | URL | Notes |
|---|---|---|
| Grant Payments Disclosure | https://www.alberta.ca/grant-payments-disclosure | FAA s.37, all departmental grant payments |
| Sole-Source Service Contracts | https://open.alberta.ca/opendata/sole-source-service-contracts | ≥$10,000 since Apr 2015; OData feed; includes permitted-situation code |
| Public Sector Body Compensation Disclosure | https://www.alberta.ca/public-sector-body-compensation-disclosure | Names of all agency/board/commission members and employees over threshold ($155,176 in 2024) → cross-match director names |
| Alberta Corporate Registry | https://www.alberta.ca/find-business-registry | **Paid only** — no free bulk download, no BO transparency law |
| Alberta Lobbyist Registry | https://albertalobbyistregistry.ca | No bulk download |

> **Alberta data gap:** Alberta has no beneficial-ownership transparency law and no free public corporate search. AHS has separate quarterly sole-source disclosure. This is a structural limitation for Alberta-scope detection.

### 3.4 Corporate / Beneficial Ownership Layers

| Source | Coverage | Openness |
|---|---|---|
| Corporations Canada (CBCA ISC register) | Federal corps since Jan 22 2024 | No API, no bulk download, no independent verification |
| Quebec REQ | QC corps — three principals, officers, affiliated establishments | Free, bulk dataset on Données Québec |
| BC Land Owner Transparency Registry | BC beneficial owners | Free since Apr 1 2024, 50 searches/day cap |
| OpenCorporates | All Canadian jurisdictions | Open but Alberta gaps; QC restricted since 2016 |
| OpenOwnership (BODS standard) | International BO, Canada committed | BODS schema is target schema for Canadian data |
| OpenSanctions | PEPs (~700K), sanctions, debarments, ICIJ Offshore Leaks | Free bulk download |
| OCCRP Aleph / Aleph Pro (Oct 2025) | 4.5B records, 50+ registries; automated risk scoring | Partially open |
| ICIJ Offshore Leaks | ~810K offshore entities | Downloadable as Neo4j/CSV — useful for related-party offshore structures |
| GLEIF (LEI) | Partial Canadian coverage | Open |

### 3.5 FINTRAC Signals

- **Operational Alert: Terrorist Activity Financing (Dec 15 2022)**: https://fintrac-canafe.canada.ca/intel/operation/taf-eng.pdf
  - Explicit indicator: "use of funds by an NPO is not consistent with the purpose for which it was established" → directly operationalizable T3010 rule
- **2025 National Risk Assessment** (September 2025): https://www.canada.ca/en/department-finance/programs/financial-sector-policy/nira-neri/2025/report.html
  - Paras. 124–125 discuss NPO abuse typologies

---

## 4. Regulatory and Policy Framework

### 4.1 Treasury Board — Binding Prohibitions on Grant Abuse

**Policy on Transfer Payments** (TBS 13525, effective 2022-04-01):
https://www.tbs-sct.canada.ca/pol/doc-eng.aspx?id=13525

**Directive on Transfer Payments** (TBS 14208, modified 2024-12-20):
https://www.tbs-sct.canada.ca/pol/doc-eng.aspx?id=14208

Key provisions to encode as graph predicates:

| Provision | Rule | Detection signal |
|---|---|---|
| Directive 6.1.1 + Appendix B item 9 | **Grants not appropriate where funding is to be further distributed** — explicit anti-conduit rule | T1236/T1441 flows where recipient immediately grants out ≥50% of received amount |
| Directive 6.5.5 | Coordinated audits where same recipient gets funding from multiple programs | Recipient appears across 3+ departments in proactive disclosure |
| Appendix F item 11 / Appendix G item 20 | No current/former public office holder under COIA may derive direct benefit | Director cross-match against COIA registry/lobbying/PSIC registry |
| Appendix G items 27–34 | Sub-recipient redistribution controls — audit rights, transparent decision-making, sub-agreement access | Flag any T1441 chain where no sub-agreement framework documented |
| Policy 5.4.15 | Transfer payments may not fund Crown corporation operating/capital | Flag Crown corp appearances in grant recipient graph |

### 4.2 Conflict of Interest Act (S.C. 2006, c. 9)

URL: https://laws-lois.justice.gc.ca/eng/acts/c-36.65/

Key sections:

| Section | Content | Hackathon use |
|---|---|---|
| s.4 | Definitions — "public office holder", "private interest" | Scope for person-node eligibility |
| s.6(1) | Prohibition on participating in decisions when private interest | SDTC Verschuren pattern |
| s.7 | Prohibition on preferential treatment | Sole-source-to-insider pattern |
| s.16(2)–(4) | Family-member contracts | T1235 family relationship flag |
| s.21 | Mandatory recusal | Board-minute non-recusal detection |
| s.51 | Public registry of recusals | Cross-check against known approvals |
| s.52 | Administrative monetary penalties | Outcome labelling for supervised learning |

### 4.3 CRA Charity Compliance Instruments

| Instrument | Key rule |
|---|---|
| **CPS-024** "Meeting the Public Benefit Test" s.3.2.4 | "Incidental private benefit" test — controls nearly every self-dealing revocation |
| **CG-032** "Registered charities making grants to non-qualified donees" (Dec 19 2023) | Post-Bill C-19 qualifying-disbursement regime driving T1441 |
| **ITA s.149.1(1)** | Definition of "ineligible individual" |
| **ITA s.149.1(4.1)(e)** | 5-year director ban |
| **ITA ss.188.1(4)–(5)** | Undue benefit penalties 105%/110% |

### 4.4 Procurement Integrity Regime (Vendor Side — Note Jurisdictional Split)

**OSIC Ineligibility and Suspension Policy** (effective May 31 2024):
https://www.tpsgc-pwgsc.gc.ca/ci-if/politique-policy-eng.html

> **CRITICAL:** The Integrity Regime covers procurement and real-property agreements **only — not transfer payments**. Grant-side and vendor-side detection regimes are **statutorily distinct** in Canada. Any risk score must flag this boundary.

Features:
- Discretionary determinations with 90-day provisional suspension
- Explicit **anti-avoidance framework for corporate succession** used to evade ineligibility → relevant to shell company detection
- GC Strategies banned under this regime June 2025

---

## 5. Algorithms and Methods

### 5.1 Validated Algorithm Stack

| Algorithm | Use case | Validated on | Source |
|---|---|---|---|
| **Louvain / Leiden community detection** | Identify director-linked clusters | AML transaction graphs | ReDiRect (arXiv 2604.01315) |
| **Betweenness + degree centralization** | Identify hub organizations in funding networks | Alberta active-living funding network | Spence et al. 2017, BMC Public Health |
| **Eigenvector centrality + clique detection** | Director-overlap clusters | Canadian inter-org networks | Gainforth et al. 2014, Int. J. Behavioral Medicine |
| **PANG induced-subgraph pattern mining** | Supervised fraud detection on procurement graphs | French FOPPA dataset | Potin et al. 2023 (arXiv 2306.10857) |
| **GNN collusion detection (zero-shot transfer)** | Cross-jurisdiction collusion | Japan/USA/Switzerland/Italy/Brazil procurement | Gomes et al. 2024 (arXiv 2410.07091) |
| **Heterogeneous Graph Transformer (HGT) / R-GCN** | Multi-type node graph classification | Innovation grants–firm graph (89.6% precision) | SME-HGT preprint |
| **Bipartite motif analysis** | Predict future grants from funder triangles | Philanthropic science network | PMC11043411 |
| **FaSTM∀N** | Multi-hop money flow (3+ hops) | Billion-scale transaction graphs | arXiv 2309.13662 |
| **IsolationForest** | Anomaly / false-positive rectification | Financial graph anomalies | ReDiRect (arXiv 2604.01315) |
| **Random Forest / Lasso / SVM / Super-learner** | Coalition-based bid screening | Procurement bid-rigging | Imhof & Wallimann 2021 (~90% accuracy) |

### 5.2 Detection Rules (Policy-Derived Predicates)

These translate directly from TBS Directive on Transfer Payments and CRA guidance into testable graph queries:

```python
# Rule R1 — Round-trip funding (anti-conduit, Directive 6.1.1 + App. B item 9)
# Org A receives grant from government → grants ≥50% to Org B → Org B grants back to Org A
# within same or adjacent fiscal year; director overlap between A and B

# Rule R2 — Director conflict (COI Act s.6(1), s.21; CRA C8/Line 3200)
# Director D sits on boards of both granting org G and receiving org R
# D participated in grant approval without recusal in board minutes

# Rule R3 — Sole-source to related party (Directive App. F item 11)
# Vendor V receives sole-source contract (CanadaBuys limitedTenderingReason ≠ competitive)
# Director(s) of funded NPO also appear as officer(s) of V in corporate registry

# Rule R4 — Pass-through layering (FATF R.8 INR Para. 3(ii); Directive App. G items 27–34)
# Chain: Gov → Org A → Org B → Org C where A and B share ≥1 director
# and the sum flowing to C approaches sum received by A (≥70%)

# Rule R5 — Multi-department concentration (Directive 6.5.5)
# Recipient R appears in proactive disclosure for 3+ distinct departments
# in same fiscal year with aggregate >$500K

# Rule R6 — Compensation of non-arm's-length persons (ITA s.188.1; CRA C8/3200)
# T1235 arm's-length flag = N for ≥2 directors + T3010 C8/3200 = Y
# → flag for potential undue benefit (ITA ss.188.1(4)–(5))

# Rule R7 — Requirements-writing (ArriveCAN pattern)
# Vendor V appears as contractor during requirements-development phase
# then appears as winner of resulting competitive procurement
# (requires contract amendment history + award notice timeline join)
```

### 5.3 Graph Schema (Implementation Target)

```cypher
// Neo4j / Cypher schema

// Nodes
(:Person {name, normalized_name, postal_code, source})
(:Organization {name, normalized_name, bn, type, province, status})
(:Grant {id, amount, fiscal_year, program, department, type})
(:Contract {id, amount, fiscal_year, department, procurement_type, sole_source_reason})

// Edges
(:Person)-[:DIRECTOR_OF {start_date, end_date, position, arms_length}]->(:Organization)
(:Person)-[:OFFICER_OF {role, start_date}]->(:Organization)
(:Organization)-[:GRANT_TO {amount, fiscal_year, form}]->(:Organization)
// form = 'T1236' | 'T1441'
(:Organization)-[:RECEIVED_GRANT {amount, fiscal_year}]->(:Grant)
(:Organization)-[:AWARDED_CONTRACT {amount, fiscal_year}]->(:Contract)
(:Person)-[:LOBBIED_FOR {year, government_funding_flag}]->(:Organization)
```

---

## 6. FATF and OECD Typology Reference

Use these when framing risk findings in the system's output layer.

### 6.1 FATF Recommendation 8 (revised October 2023)

URL: https://www.fatf-gafi.org/en/publications/Fatfrecommendations/protecting-non-profits-abuse-implementation-R8.html

INR.8 Para. 3 — Three TF abuse objectives mapping to hackathon targets:
1. **Para. 3(i)** — Terrorists posing as legitimate entities → **shell NPO structures**
2. **Para. 3(ii)** — Exploiting legitimate entities as conduits → **layering and pass-through**
3. **Para. 3(iii)** — Clandestine diversion of funds → **round-trip and grants to related parties**

### 6.2 FATF 2014 Typology — Five Named Categories

Source: *Risk of Terrorist Abuse in Non-Profit Organisations* (June 2014, 102 case studies)
URL: https://www.fatf-gafi.org/en/publications/Methodsandtrends/Risk-terrorist-abuse-non-profits.html

| # | Category | Maps to |
|---|---|---|
| 1 | Diversion of funds | Round-trip; grants to related parties |
| 2 | Affiliation with a terrorist entity | PEP/sanctions crossmatch |
| 3 | Abuse of programming | Purpose mismatch (T3010 vs T1441 project description) |
| 4 | Support to recruitment efforts | Out of scope |
| 5 | False representation | Shell NPO; fraudulent T3010 registration |
| 6 | Fundraising through social media | Egmont 2024 addition |

### 6.3 FATF 2023 Best Practices Paper on NPOs

URL: https://www.fatf-gafi.org/en/publications/Financialinclusionandnpoissues/Bpp-combating-abuse-npo.html

Chapter 2.2 — Internal controls checklist (conflict-of-interest, related-party transaction policies) → maps directly to CRA C8/Line 3200 and director-linked vendor detection.

### 6.4 OECD Authorities

| Instrument | Key provision | Hackathon use |
|---|---|---|
| Recommendation on Public Integrity (OECD/LEGAL/0435, Jan 26 2017) | Principles 11 (risk mgmt), 12 (oversight), 13 (transparency, COI, lobbying) | Frame risk scoring output |
| Recommendation on Public Procurement (OECD/LEGAL/0411, 2015) | Sole-source restricted to narrow exceptions with documented justification | Rule R3 legal basis |
| *Integrity in Public Procurement A–Z* (2007) | Box III.2 — Canadian procurement reforms | Historical baseline |

### 6.5 Canada FATF Status (Correction)

> **There is NO 2023 FATF Mutual Evaluation of Canada.** Do not cite one.

- **Last full evaluation:** September 15 2016
- **October 2021 follow-up:** Canada **downgraded to Partially Compliant on R.8**
  - URL: https://www.fatf-gafi.org/en/publications/Mutualevaluations/Fur-canada-2021.html
- **5th-round MER onsite:** November 2025; **Plenary discussion: June 2026** (pending)
- **Current authoritative baseline:** 2025 National Risk Assessment (September 2025)
  - Paras. 124–125 discuss NPO abuse typologies

---

## 7. Entity Resolution Strategy

This is the hardest engineering problem. Canada has no canonical person-identifier across CRA, Corporations Canada, provincial registries, and proactive disclosure.

### Resolution approach

```
Priority 1 — Business Number (BN):
  CRA T3010 BN = Proactive disclosure recipient BN = CBCA corporate number prefix
  Note: BN is the 9-digit root; program accounts add 4+5 suffix — strip to root

Priority 2 — Postal code + name fuzzy match:
  T1235 director postal code + normalized name
  → match to corporate registry officer address + normalized name
  Use: Jaro-Winkler ≥0.92 + postal code match as high-confidence link

Priority 3 — Address normalization:
  Registered address from T3010 vs registered address in corporate registry
  → exact match after normalization → high confidence org-to-corp link

Priority 4 — Known-bad patterns:
  Multiple charities sharing a single address (same postal code, same street number)
  → flag cluster for manual review regardless of director overlap
```

### Known resolution gaps
- **Alberta**: No free bulk corporate registry → Alberta-scoped director network is incomplete
- **T1235 DOB**: Confidential in bulk data → cannot distinguish common names definitively
- **Name variants**: "John Smith", "J. Smith", "J.A. Smith" — requires consistent normalization before graph load
- **CBCA ISC register**: No API, no bulk download as of April 2026 → manual lookup only

---

## 8. Output and Scoring

### Risk score components

```python
risk_score = weighted_sum([
    director_overlap_score,      # number of shared directors, weighted by centrality
    round_trip_flag,             # binary: T1236 cycle detected within 2 fiscal years
    sole_source_related_party,   # binary: sole-source vendor + director match
    pass_through_ratio,          # amount flowing out / amount received (0–1)
    multi_department_count,      # departments funding same recipient same FY
    arms_length_violation,       # T1235 arms_length = N + compensation = Y
    coi_act_exposure,            # director appears in COIA recusal registry
])
```

### Output schema per flagged cluster

```json
{
  "cluster_id": "uuid",
  "risk_score": 0.0,
  "risk_level": "HIGH|MEDIUM|LOW",
  "organizations": ["BN1", "BN2"],
  "persons": ["normalized_name_1"],
  "detected_patterns": ["ROUND_TRIP", "DIRECTOR_OVERLAP", "SOLE_SOURCE"],
  "policy_triggers": ["Directive 6.1.1", "COI Act s.6(1)", "ITA s.188.1(4)"],
  "fatf_typologies": ["Typology 1: Diversion", "INR.8 Para 3(ii)"],
  "evidence": {
    "t1236_edges": [],
    "proactive_disclosure_grants": [],
    "canada_buys_contracts": [],
    "director_overlap_persons": []
  },
  "jurisdictional_note": "Procurement-side findings subject to OSIC regime; grant-side findings subject to TBS Directive on Transfer Payments — these are separate legal tracks"
}
```

---

## 9. File and Module Structure (Suggested)

```
agency2026_challenge6/
├── AGENCY2026_CHALLENGE6.md        ← this file
├── data/
│   ├── ingest/
│   │   ├── t3010_loader.py         # CRA T3010 bulk data → normalized tables
│   │   ├── proactive_disclosure.py # TBS open.canada.ca datasets
│   │   ├── canadabuys.py           # CanadaBuys award notices + contract history
│   │   └── alberta_grants.py       # AB grant payments + sole-source contracts
│   ├── resolve/
│   │   ├── entity_resolution.py    # BN + fuzzy name + address matching
│   │   └── name_normalizer.py      # Jaro-Winkler + address normalization
│   └── graph/
│       ├── schema.cypher           # Neo4j schema definition
│       ├── loader.py               # Load resolved entities into graph DB
│       └── queries.py              # Cypher queries for each detection rule
├── detect/
│   ├── rules.py                    # Policy-derived predicates R1–R7
│   ├── community_detection.py      # Louvain/Leiden clustering
│   ├── centrality.py               # Betweenness, eigenvector, degree
│   ├── round_trip.py               # Multi-hop cycle detection (FaSTM∀N)
│   └── sole_source.py              # Procurement-side detection
├── score/
│   ├── risk_scorer.py              # Weighted composite score
│   └── output_formatter.py         # JSON output schema
├── ground_truth/
│   ├── sdtc_labels.json            # SDTC positive-label dataset
│   ├── gcstrategies_labels.json    # GC Strategies positive-label dataset
│   └── negative_samples.py        # Random sample of non-flagged recipients
└── tests/
    └── test_rules.py
```

---

## 10. Key Caveats for Any Output

1. **Jurisdictional split:** Procurement Integrity Regime (OSIC) and Transfer Payments Directive are separate legal tracks. Never conflate them in risk labels.
2. **Arm's-length commercial transactions:** Not all sole-source contracts are improper; not all shared directors create conflicts. Detection flags require human review.
3. **T3010 publication lag:** Director changes up to 18 months old may not yet appear. Temporal analysis requires year-aligned joining.
4. **Alberta structural gap:** No free bulk corporate registry → Alberta director network is materially incomplete.
5. **FATF R.8 framing:** The FATF typologies cover terrorist financing specifically, not domestic procurement fraud. Use OECD LEGAL/0411 and LEGAL/0435 for the procurement-fraud framing; use FATF for the NPO-abuse framing.
6. **No 2023 FATF MER of Canada:** Canada's 5th-round evaluation plenary is June 2026. Do not cite a 2023 report.
7. **CRA does not publish individual revocation reasons by category:** "Related-party" is not a published revocation category — ground-truth labels require manual reading of Tax Court and FCA decisions.

---

*Generated: 2026-04-29 | Agency 2026 Hackathon, Ottawa | Challenge #6: Related Parties and Governance Networks*
