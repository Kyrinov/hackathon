# TASK: GIC Appointees + Lobbyist Registry Ingest
## Agency 2026 — Challenge #6 | `data/ingest/`

> **Read before starting:** `AGENCY2026_CHALLENGE6.md` (graph schema, BN resolution
> strategy, node/edge types). This task builds two new ingest modules that produce
> Person nodes and edges joinable to the existing T3010 and proactive-disclosure layers.

---

## Deliverables

| File | Description |
|---|---|
| `data/ingest/gic_appointments.py` | Scrape PCO appointee directory → Person nodes + GIC_APPOINTEE edges |
| `data/ingest/lobbyist_registry.py` | Download + parse OCL bulk CSVs → Person/Org nodes + LOBBIED_FOR / FORMER_POH edges |
| `data/ingest/oic_scraper.py` | Scrape OIC database for appointment OICs 1990–present → supplement GIC layer with historical records |
| `data/resolve/gic_t3010_crosswalk.py` | Fuzzy-match GIC appointee names against T1235 director names |
| `data/resolve/lobbyist_t3010_crosswalk.py` | Match lobbyist/responsible-officer names against T1235 directors + proactive-disclosure recipients |
| `tests/test_gic_lobbyist.py` | Unit tests for parsers and crosswalks |

All output goes to **Parquet** files in `data/processed/` using the schema below.
All scrapers must be **resumable** (checkpoint on last PC number / page).

---

## Source 1 — PCO GIC Appointments Directory

### Access pattern

No API, no bulk download. Two structured HTML endpoints:

```
# Current incumbent directory
ORG_LIST  = "https://appointments.gc.ca/prsnt.asp?menu=2&page=gicList&lang=eng"
ORG_TABLE = "https://appointments.gc.ca/prsnt.asp?menu=2&page=gicByOrg&OrgID={org_id}&lang=eng"
```

Rate-limit: **1 request per 2 seconds**. `User-Agent: Mozilla/5.0` is sufficient.
The OIC search database is at https://orders-in-council.canada.ca/ — separate scraper.

### HTML structure (appointments.gc.ca)

Org list page contains a `<select>` or `<table>` of org names + numeric OrgIDs.
Per-org page contains a `<table>` with these columns (parse by column index, not header
text — headers are bilingual and position shifts):

| Col index | Field | Notes |
|---|---|---|
| 0 | `position_title` | Official title (EN) |
| 1 | `appointee_name` | "Last, First" or "First Last" — normalize both |
| 2 | `city_province` | City, Province at time of appointment |
| 3 | `tenure_type` | "During pleasure" \| "During good behaviour" |
| 4 | `fulltime_parttime` | "Full-time" \| "Part-time" |
| 5 | `appointment_date` | First appointment date (YYYY-MM-DD after parse) |
| 6 | `term_expiry` | Current term expiry — blank if indeterminate |

### Output schema — `gic_appointments.parquet`

```python
{
  "source": "pco_directory",
  "org_id": str,           # PCO numeric org ID
  "org_name": str,         # Organization name (EN)
  "org_type": str,         # "Crown Corporation" | "Agency" | "Tribunal" | "Commission" | "Other"
  "position_title": str,
  "appointee_name_raw": str,
  "appointee_name_normalized": str,   # Title-case, "First Last" order
  "city": str,
  "province_code": str,               # 2-char: ON, QC, BC ...
  "tenure_type": str,
  "is_fulltime": bool,
  "appointment_date": date,
  "term_expiry": date | None,
  "is_current": bool,                 # term_expiry >= today OR expiry is None
  "scraped_at": datetime,
}
```

### `gic_appointments.py` — implementation notes

```python
import requests, time
from bs4 import BeautifulSoup
import pandas as pd
from pathlib import Path
from datetime import datetime, date

CHECKPOINT = Path("data/processed/.gic_checkpoint.json")
OUT_PATH   = Path("data/processed/gic_appointments.parquet")

def scrape_org_list() -> list[dict]:
    """Return list of {org_id, org_name, org_type} from the PCO directory."""
    ...

def scrape_org_appointees(org_id: str, org_name: str) -> list[dict]:
    """Scrape the per-org appointee table. Return list of row dicts."""
    ...

def normalize_name(raw: str) -> str:
    """
    Handles 'Last, First', 'First Last', honorifics (Hon., Dr., Mr., Ms., The).
    Returns 'First Last' title-case. Strips trailing Jr./Sr./II/III.
    """
    ...

def parse_date(s: str) -> date | None:
    """Parse '%B %d, %Y', '%Y-%m-%d', '%d/%m/%Y'. Return None if blank/unparseable."""
    ...

def run():
    orgs = scrape_org_list()
    rows = []
    for org in orgs:
        rows.extend(scrape_org_appointees(org["org_id"], org["org_name"]))
        time.sleep(2)
    df = pd.DataFrame(rows)
    df.to_parquet(OUT_PATH, index=False)
    print(f"Wrote {len(df)} GIC appointee records")

if __name__ == "__main__":
    run()
```

---

## Source 2 — PCO Orders in Council Database (historical appointments)

Augments the directory with **terminated/historical** appointees needed for the
revolving-door and post-employment detection rules.

### Access pattern

```
SEARCH_URL = "https://orders-in-council.canada.ca/results.php"

# Use these param sets to capture appointment OICs:
params = {
    "keywords": "is appointed",   # Standard OIC appointment language
    "fromdate": "YYYY-MM-DD",
    "todate":   "YYYY-MM-DD",
    # Optionally filter by department to reduce volume
}
# Returns HTML table: PC_number | date | title (EN) | department
# Follow each record to: https://orders-in-council.canada.ca/doc.php?id={pc_number}
# Full OIC text is in <div id="oic-content"> or equivalent
```

Rate-limit: **1 req/3s**. Checkpoint on last processed PC number.
Batch by **quarterly date ranges** (fromdate/todate) to keep result sets manageable.

### Name extraction regex (standard OIC appointment language)

OICs follow one of these patterns — apply in order:

```python
import re

PATTERNS = [
    # "John Smith, of Ottawa, Ontario, is appointed..."
    r"^([A-Z][a-zéèêëàâîïôùûü'\-]+(?: [A-Z][a-zéèêëàâîïôùûü'\-]+)+),\s+of\s+([^,]+),\s+([A-Z][a-z]+(?:[ \-][A-Z][a-z]+)*),\s+is appointed\s+([\w\s,\-]+?)\s+of\s+([\w\s\-,()]+?)[,.]",
    # "..., to appoint John Smith, of City, Province, as..."
    r"to appoint\s+([A-Z][a-zéèêëàâîïôùûü'\-]+(?: [A-Z][a-zéèêëàâîïôùûü'\-]+)+),\s+of\s+([^,]+),\s+([A-Z][a-z]+)",
]
# Groups: (full_name, city, province, position_title, org_name)
```

### Output schema — `oic_appointments.parquet`

```python
{
  "pc_number": str,          # "YYYY-NNNN" e.g. "2024-0123"
  "oic_date": date,
  "appointee_name_raw": str,
  "appointee_name_normalized": str,
  "city": str,
  "province_code": str,
  "position_title": str,
  "org_name": str,
  "department": str,         # PCO-listed sponsoring department
  "action": str,             # "appointment" | "reappointment" | "termination" | "resignation"
  "oic_text_snippet": str,   # First 500 chars of OIC body for audit trail
  "scraped_at": datetime,
}
```

---

## Source 3 — OCL Lobbyist Registry Bulk Data

### Download URLs (confirmed live as of 2026-04-29)

```python
REGISTRATIONS_ZIP  = "https://lobbycanada.gc.ca/media/zwcjycef/registrations_enregistrements_ocl_cal.zip"
REGISTRATIONS_DICT = "https://lobbycanada.gc.ca/media/hcvmsu4e/dictionary_registrations_dictionnaire_enregistrements.xlsx"

COMMUNICATIONS_ZIP  = "https://lobbycanada.gc.ca/media/mqbbmaqk/communications_ocl_cal.zip"
COMMUNICATIONS_DICT = "https://lobbycanada.gc.ca/media/ilifarxv/communications_dictionary_dictionnaire_communication.xlsx"

# Open Government Portal dataset IDs (for metadata / update-date checks):
REGISTRATIONS_DATASET  = "70ef2117-1095-4d77-80eb-b87f2bada2a4"
COMMUNICATIONS_DATASET = "a34eb330-7136-4f5e-9f5f-3ba41df58b06"
```

**Coverage:** Registrations Jan 31 1996 – present (Lobbyists Registration Act + Lobbying Act).
Communications (DPOH contact reports) Jul 2, 2008 – present.
Updated: Registrations ~monthly; Communications ~monthly (last update 2026-04-27).

### ZIP contents structure

The registrations ZIP contains **primary + secondary CSV files** (one-to-many):

```
registrations_enregistrements_ocl_cal.zip
├── reg_main.csv              # One row per registration
├── reg_lobbyists.csv         # One row per lobbyist named in registration
├── reg_poh.csv               # One row per public office held by each lobbyist
├── reg_funding.csv           # One row per government funding entry
├── reg_subjects.csv          # One row per lobbying subject matter category
├── reg_beneficiaries.csv     # One row per beneficiary (consultant regs only)
└── reg_targets.csv           # One row per target government institution
```

### Key fields by file

#### `reg_main.csv` — one row per registration

| Field | Type | Graph use |
|---|---|---|
| `REGISTRATION_NUMBER` | str | Primary key |
| `REGISTRATION_TYPE` | str | `"Consultant"` \| `"In-house Organization"` \| `"In-house Corporation"` |
| `STATUS` | str | `"Active"` \| `"Inactive"` — filter Active for current risk |
| `CLIENT_ORG_CORPORATION_NAME` | str | Organization being lobbied for → Org node name |
| `CLIENT_BN` | str | **Business Number if available** — join to T3010 BN, proactive disclosure |
| `RESPONSIBLE_OFFICER_FIRST_NAME` | str | In-house reg: most senior paid officer |
| `RESPONSIBLE_OFFICER_LAST_NAME` | str | Join to T1235 director names |
| `EFFECTIVE_DATE` | date | Registration start |
| `END_DATE` | date | Registration end (blank if active) |
| `GOVT_FUNDING_RECEIVED` | bool | `"Y"/"N"` — **primary filter for related-party detection** |
| `GOVT_FUNDING_AMOUNT_TOTAL` | float | Total funding received (sum across reg_funding.csv) |

#### `reg_lobbyists.csv` — one row per lobbyist

| Field | Type | Graph use |
|---|---|---|
| `REGISTRATION_NUMBER` | str | FK to reg_main |
| `LOBBYIST_FIRST_NAME` | str | Person node |
| `LOBBYIST_LAST_NAME` | str | Person node |
| `FORMER_PUBLIC_OFFICE_HOLDER` | bool | `"Y"/"N"` — revolving-door flag |

#### `reg_poh.csv` — one row per former public office held

| Field | Type | Graph use |
|---|---|---|
| `REGISTRATION_NUMBER` | str | FK |
| `LOBBYIST_FIRST_NAME` | str | |
| `LOBBYIST_LAST_NAME` | str | |
| `POH_POSITION_TITLE` | str | Former position (e.g. "Director General", "Deputy Minister") |
| `POH_DEPARTMENT_NAME` | str | Former department |
| `POH_START_DATE` | date | |
| `POH_END_DATE` | date | |

#### `reg_funding.csv` — one row per government funding entry

| Field | Type | Graph use |
|---|---|---|
| `REGISTRATION_NUMBER` | str | FK |
| `FUNDING_INSTITUTION` | str | Granting department (federal) |
| `FUNDING_AMOUNT` | float | |
| `FUNDING_TYPE` | str | `"Received"` \| `"Expected"` |
| `FUNDING_FISCAL_YEAR` | str | |

> **Critical join:** `reg_funding.FUNDING_INSTITUTION` + `reg_main.CLIENT_BN`
> joined to `proactive_disclosure.department` + `proactive_disclosure.recipient_bn`
> gives you the lobbying-then-grant pattern: org lobbied department X → received
> grant from department X.

#### `communications_ocl_cal.zip` — DPOH contact reports

```
communications_ocl_cal.zip
├── comm_main.csv         # One row per communication report
├── comm_dpohs.csv        # One row per DPOH in each communication
└── comm_subjects.csv     # One row per subject matter
```

Key fields:
- `comm_main.REGISTRATION_NUMBER` — FK to registrations
- `comm_main.COMMUNICATION_DATE` — date of the "oral and arranged" communication
- `comm_dpohs.DPOH_FIRST_NAME` / `DPOH_LAST_NAME` — the public office holder contacted
- `comm_dpohs.DPOH_TITLE` / `DPOH_DEPARTMENT` — their role and department
- `comm_dpohs.DPOH_TYPE` — `"Minister"` \| `"Parliamentary Secretary"` \| `"Senior Official"` \| etc.

> DPOHs in communication reports are a **second GIC cross-reference layer**: an org
> that lobbied a minister and also received a sole-source contract from that minister's
> department is a Rule R3/R7 candidate.

### Output schemas

#### `lobbyist_registrations.parquet`

```python
{
  "registration_number": str,
  "registration_type": str,           # Consultant | In-house Org | In-house Corp
  "status": str,                      # Active | Inactive
  "client_org_name": str,
  "client_org_name_normalized": str,
  "client_bn": str | None,            # 9-digit BN root (strip suffix if present)
  "responsible_officer_name": str,    # normalized "First Last"
  "effective_date": date,
  "end_date": date | None,
  "govt_funding_flag": bool,          # True if GOVT_FUNDING_RECEIVED == "Y"
  "govt_funding_total": float,
  "scraped_at": datetime,
}
```

#### `lobbyist_persons.parquet`

```python
{
  "registration_number": str,
  "person_name_normalized": str,      # "First Last"
  "person_role": str,                 # "lobbyist" | "responsible_officer"
  "former_poh_flag": bool,
  "poh_positions": list[dict],        # [{title, department, start, end}]
}
```

#### `lobbyist_funding.parquet`

```python
{
  "registration_number": str,
  "client_bn": str | None,
  "client_org_name_normalized": str,
  "funding_institution": str,
  "funding_institution_normalized": str,  # Normalize dept names to match proactive_disclosure
  "funding_amount": float,
  "funding_type": str,                # Received | Expected
  "funding_fiscal_year": str,
}
```

#### `lobbyist_dpoh_contacts.parquet`

```python
{
  "registration_number": str,
  "communication_date": date,
  "dpoh_name_normalized": str,
  "dpoh_title": str,
  "dpoh_department": str,
  "dpoh_type": str,
}
```

### `lobbyist_registry.py` — implementation notes

```python
import io, zipfile, requests
import pandas as pd
from pathlib import Path

REGISTRATIONS_ZIP  = "https://lobbycanada.gc.ca/media/zwcjycef/registrations_enregistrements_ocl_cal.zip"
COMMUNICATIONS_ZIP = "https://lobbycanada.gc.ca/media/mqbbmaqk/communications_ocl_cal.zip"
OUT = Path("data/processed")

def download_zip(url: str) -> zipfile.ZipFile:
    r = requests.get(url, timeout=120)
    r.raise_for_status()
    return zipfile.ZipFile(io.BytesIO(r.content))

def normalize_bn(raw: str | None) -> str | None:
    """Strip program account suffix — keep 9-digit root only."""
    if not raw:
        return None
    digits = "".join(c for c in str(raw) if c.isdigit())
    return digits[:9] if len(digits) >= 9 else None

def normalize_dept_name(raw: str) -> str:
    """
    Map OCL department name variants to canonical proactive-disclosure names.
    E.g. "ESDC" == "Employment and Social Development Canada" == "Human Resources..."
    Maintain a lookup table; fall back to title-case strip.
    """
    ...

def run():
    reg_zip  = download_zip(REGISTRATIONS_ZIP)
    comm_zip = download_zip(COMMUNICATIONS_ZIP)

    reg_main    = pd.read_csv(reg_zip.open("reg_main.csv"),      encoding="utf-8-sig")
    reg_persons = pd.read_csv(reg_zip.open("reg_lobbyists.csv"), encoding="utf-8-sig")
    reg_poh     = pd.read_csv(reg_zip.open("reg_poh.csv"),       encoding="utf-8-sig")
    reg_funding = pd.read_csv(reg_zip.open("reg_funding.csv"),   encoding="utf-8-sig")
    comm_main   = pd.read_csv(comm_zip.open("comm_main.csv"),    encoding="utf-8-sig")
    comm_dpohs  = pd.read_csv(comm_zip.open("comm_dpohs.csv"),   encoding="utf-8-sig")

    # Build normalized outputs and write to parquet
    ...

if __name__ == "__main__":
    run()
```

> **Encoding note:** OCL CSVs use UTF-8 with BOM (`utf-8-sig`). Use that encoding
> or pandas will misread the first column header.

---

## Source 4 — Cross-Reference / Entity Resolution

### `data/resolve/gic_t3010_crosswalk.py`

Match GIC appointee names against T1235 director names to find persons sitting on
both a GIC board **and** a charity receiving federal grants.

```python
# Input:  gic_appointments.parquet  (appointee_name_normalized, org_name)
#         t1235_directors.parquet   (director_name_normalized, charity_bn, charity_name)
# Output: gic_t3010_matches.parquet

# Match strategy (apply in order, stop at first hit):
# 1. Exact normalized name match
# 2. Jaro-Winkler >= 0.92 on normalized name
# 3. Last name exact + first initial exact + same province_code

# For each match, produce:
{
  "gic_appointee_name": str,
  "gic_org_name": str,
  "gic_position_title": str,
  "gic_appointment_date": date,
  "gic_term_expiry": date | None,
  "charity_bn": str,
  "charity_name": str,
  "t1235_director_name": str,
  "match_method": str,       # "exact" | "jaro_winkler" | "last_initial_province"
  "match_score": float,
  "risk_flag": str,          # "GIC_DIRECTOR_CHARITY_OVERLAP"
}
```

### `data/resolve/lobbyist_t3010_crosswalk.py`

Find persons who lobbied a department AND appear as a director of a charity that
received grants from that same department.

```python
# Input:  lobbyist_persons.parquet      (person_name_normalized, registration_number)
#         lobbyist_funding.parquet      (registration_number, funding_institution_normalized, client_bn)
#         t1235_directors.parquet       (director_name_normalized, charity_bn)
#         proactive_disclosure.parquet  (recipient_bn, department_normalized)

# Key join:
# Person → lobbied for client → client received grant from dept X
# Person also director of charity → charity received grant from dept X
# => same person, same department, both directions

# Output: lobbyist_t3010_matches.parquet
{
  "person_name": str,
  "registration_number": str,
  "client_org": str,
  "client_bn": str | None,
  "dept_lobbied": str,
  "charity_bn": str,
  "charity_name": str,
  "grant_amount": float,
  "grant_fiscal_year": str,
  "former_poh_flag": bool,
  "risk_flag": str,   # "LOBBYIST_DIRECTOR_SAME_DEPT_GRANT"
}
```

---

## Graph Edges Produced by This Module

Add these to the Neo4j schema in `data/graph/schema.cypher`:

```cypher
// New edge types
(:Person)-[:GIC_APPOINTEE_OF {
  position_title,
  appointment_date,
  term_expiry,
  is_current,
  tenure_type,
  pc_number       // OIC reference
}]->(:Organization)

(:Person)-[:LOBBIED_FOR {
  registration_number,
  registration_type,
  effective_date,
  end_date,
  govt_funding_flag,
  govt_funding_total
}]->(:Organization)

(:Person)-[:FORMER_POH_AT {
  position_title,
  department,
  start_date,
  end_date
}]->(:Organization)   // Org node = government department

(:Person)-[:CONTACTED_DPOH {
  communication_date,
  dpoh_name,
  dpoh_department
}]->(:Person)         // DPOH is also a Person node

(:Organization)-[:LOBBIED_DEPT {
  registration_number,
  funding_institution,
  funding_amount,
  funding_fiscal_year
}]->(:Organization)   // Org (client) → lobbied → Dept (gov org)
```

---

## Detection Rules Enabled by This Module

These augment the R1–R7 rules in `AGENCY2026_CHALLENGE6.md`:

```python
# Rule R8 — GIC appointee / charity director overlap (COI Act s.6, s.21)
# Person P is GIC_APPOINTEE_OF Crown corp/agency C
# AND P is DIRECTOR_OF charity CH
# AND CH RECEIVED_GRANT from department D
# AND C is funded by / reports to department D
# => P in position to influence grant to own charity

# Rule R9 — Lobbyist-director-grant triangle (Lobbying Act; TBS Directive App. F item 11)
# Person P LOBBIED_FOR org O targeting dept D
# AND O RECEIVED_GRANT from dept D (proactive disclosure)
# AND P is DIRECTOR_OF O (T1235)
# => lobbyist = director = grant recipient (single-person triangle)

# Rule R10 — Former POH revolving door (COI Act ss.33–37 post-employment rules)
# Person P has FORMER_POH_AT dept D (end_date within last 5 years)
# AND P LOBBIED_FOR org O targeting dept D after end_date
# AND O RECEIVED_GRANT from dept D
# => potential COI Act post-employment violation (5-year cooling-off for DMs/ADMs;
#    1-year for other GIC appointees under s.35)

# Rule R11 — DPOH contact → sole-source contract (ArriveCAN / requirements-rigging)
# Person P (lobbyist) CONTACTED_DPOH minister/DG at dept D on date T1
# AND org O (P's client) received AWARDED_CONTRACT from dept D on date T2 > T1
# AND contract.sole_source_reason != NULL
# => lobbying preceded sole-source award to lobbyist's client

# Rule R12 — GIC board + lobbyist dual role
# Person P is GIC_APPOINTEE_OF Crown corp C (current)
# AND P LOBBIED_FOR org O (any registration, active or not)
# => Crown corp board member who is or was a registered lobbyist
# (Lobbying Act s.10.11 prohibits GIC appointees from lobbying during tenure)
```

---

## Known Limitations

| Limitation | Impact | Mitigation |
|---|---|---|
| OIC database has no bulk export | Historical appointments require scraping ~35 years of OICs | Batch by quarter; prioritize last 10 years first |
| GIC appointee directory shows **current only** | Terminated appointees missing | Supplement with OIC scraper (Layer A) |
| OCL CSVs contain **no BN** for ~40% of organizations | Cannot join to T3010/proactive disclosure by BN alone | Fall back to name fuzzy-match + postal code |
| Lobbying Act POH records pre-2005 incomplete | Pre-2005 revolving-door links missing | Document gap; flag as low-confidence for records before 2005 |
| OCL data is **self-reported** — amounts unverified | Funding amounts may be estimates | Cross-validate against `lobbyist_funding.funding_amount` vs `proactive_disclosure.amount` |
| `reg_main.CLIENT_BN` field is **often blank** | BN join rate is low (~60%) | Use name + address normalization as fallback |
| Lobbying Act s.10.11 prohibition on GIC lobbying | Rule R12 flags may include permissible historical lobbying before appointment | Always check `lobbied_date < appointment_date` vs `lobbied_date >= appointment_date` |

---

## Quick-Start Sequence for Claude Code

```bash
# 1. Install deps (if not already in requirements.txt)
pip install requests beautifulsoup4 pandas pyarrow rapidfuzz --break-system-packages

# 2. Run lobbyist registry first (fastest — pure download, no scraping)
python data/ingest/lobbyist_registry.py

# 3. Run GIC appointments directory scraper
python data/ingest/gic_appointments.py

# 4. Run OIC historical scraper (slow — batch quarterly, run overnight)
python data/ingest/oic_scraper.py --from 2015-01-01 --to 2026-04-29

# 5. Run crosswalks against T3010 (requires t1235_directors.parquet to exist)
python data/resolve/gic_t3010_crosswalk.py
python data/resolve/lobbyist_t3010_crosswalk.py

# 6. Load new edges into Neo4j
python data/graph/loader.py --source gic_appointments
python data/graph/loader.py --source lobbyist_registry

# 7. Run tests
python -m pytest tests/test_gic_lobbyist.py -v
```

---

## Test Cases

Seed `tests/test_gic_lobbyist.py` with these known-good assertions:

```python
# SDTC ground truth — Verschuren should appear in GIC appointee data
def test_verschuren_gic_record():
    df = pd.read_parquet("data/processed/gic_appointments.parquet")
    match = df[df["appointee_name_normalized"].str.contains("Verschuren", case=False)]
    assert len(match) > 0, "Verschuren not found in GIC records"
    assert "SDTC" in match["org_name"].values[0] or "Sustainable" in match["org_name"].values[0]

# OCL BN normalization
def test_bn_normalization():
    from data.ingest.lobbyist_registry import normalize_bn
    assert normalize_bn("123456789RT0001") == "123456789"
    assert normalize_bn("123456789")       == "123456789"
    assert normalize_bn(None)              is None
    assert normalize_bn("12345")           is None  # too short

# Name normalization handles "Last, First" format
def test_name_normalization():
    from data.ingest.gic_appointments import normalize_name
    assert normalize_name("Smith, John A.")  == "John Smith"
    assert normalize_name("Dr. Jane Doe")    == "Jane Doe"
    assert normalize_name("O'Brien, Patrick") == "Patrick O'Brien"

# Jaro-Winkler threshold
def test_jaro_winkler_crosswalk():
    from data.resolve.gic_t3010_crosswalk import match_names
    assert match_names("Annette Verschuren", "A. Verschuren") >= 0.88
    assert match_names("John Smith", "Jane Smith") < 0.92  # should not match
```

---

*Generated: 2026-04-29 | Agency 2026 Hackathon, Ottawa | Challenge #6*
