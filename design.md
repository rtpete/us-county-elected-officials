# U.S. County Elected Officials: Data Acquisition Design

## The Problem

The United States has approximately 3,143 counties (including county-equivalents such as Louisiana parishes, Alaska boroughs, Virginia independent cities, and consolidated city-county governments). A single unified, authoritative national source containing data on their elected officials does not exist. Office structures vary by state: some counties elect a CFO, others appoint one, others roll that function into a broader "County Administrator" role. County websites range from well-maintained civic portals to single-page PDFs last updated in 2018.

The goal is to create a database which is accurate, updatable, maintainable, and structured for both analyst queries and downstream system consumption.

---

## 1. Data Model

**Design philosophy:** There are four core entities for modeling this data — *where* (the county), *what* (the office/elected position), *who* (the person elected to that office), and *when* (the tenure). A fifth table captures raw source data for every ingest, enabling auditability. This gives us five tables total.

---

### Table Definitions

*Where*: **`counties`** — Contains all US counties and county-specific data

| Column | Type | Notes |
|---|---|---|
| `county_fips` | CHAR(5) | Primary key; standard FIPS code (e.g., `"06037"` for LA County) |
| `state_fips` | CHAR(2) | First two digits of `county_fips`; included for direct state-level filtering |
| `state_name` | TEXT | e.g., `"California"` |
| `state_abbreviation` | CHAR(2) | e.g., `"CA"` |
| `county_name` | TEXT | e.g., `"Los Angeles County"` |
| `county_type` | TEXT | `County`, `Parish`, `Borough`, `Independent City`, `Municipality` |
| `website_url` | TEXT | Primary county government website; nullable |
| `social_media_urls` | TEXT[] | Social media profile URLs for the county government (e.g., Facebook, X/Twitter, Instagram); nullable array |
| `population` | INTEGER | Latest Census estimate; used for prioritization |

---

*What*: **`offices`** — Contains all elected or appointed office positions within a specific county

| Column | Type | Notes |
|---|---|---|
| `id` | UUID | Primary key |
| `county_fips` | CHAR(5) | Foreign Key → `counties` |
| `office_type` | TEXT | Normalized cross-county name (e.g., `"Sheriff"`, `"Chief Financial Officer"`) |
| `local_title` | TEXT | What this county actually calls the role |
| `category` | TEXT | `executive`, `legislative`, `judicial`, `administrative`, etc. |
| `is_board` | BOOLEAN | True for multi-member bodies (County Commission, Board of Supervisors) |
| `seats` | SMALLINT | Number of seats; 1 for single-holder offices |
| `term_years` | SMALLINT | Typical term length; nullable if varies or unknown |
| `partisan` | BOOLEAN | Whether the race appears on a partisan ballot |

**Populating `office_type` from raw sources:** Different sources use different vocabulary for the same role. A historical spreadsheet might list "CFO", "Co. Treasurer/CFO", or "Finance Director" for what this model calls `"Chief Financial Officer"`. Mapping these to a standard office type requires a normalization step, described in [Section 3](#3-collection-architecture).

---

*Who*: **`officials`** — Contains all elected officials, de-duplicated across counties and time

| Column | Type | Notes |
|---|---|---|
| `id` | UUID | Primary key |
| `first_name` | TEXT | Elected official's first name |
| `last_name` | TEXT | Elected official's last name |
| `party` | TEXT | `Democrat`, `Republican`, `Independent`, `Nonpartisan`, etc. |
| `email` | TEXT | Best-known contact email; nullable |
| `phone` | TEXT | Best-known phone; nullable |
| `social_media_urls` | TEXT[] | Social media profile URLs for this official (personal and official accounts); nullable array |
| `dedupe_hash` | TEXT | Computed from `lower(last_name) & lower(first_name) & county_fips`; used for deduplication |

A person who has held offices in multiple counties will have multiple `terms` rows but one `officials` row.

`dedupe_hash` is a fingerprint computed from the official's name and home county. Before writing a new row to `officials`, the ingest pipeline computes this hash and checks whether it already exists in the table. If it does, the incoming record is linked to the existing official rather than creating a duplicate. This reliably catches the most common case — the same person arriving from two different sources for the same county — but it has known limits: it does not handle cross-county moves, legal name changes, or two different people with the same name in the same county. It is a starting heuristic, not a complete deduplication solution. External source IDs stored in `source_records` provide a stronger deduplication signal where available and should be used to resolve conflicts the hash cannot.

---

*When*: **`terms`** — Contains records for each term an elected official holds in a given elected office

| Column | Type | Notes |
|---|---|---|
| `id` | UUID | Primary key |
| `official_id` | UUID | Foreign Key → `officials` |
| `office_id` | UUID | Foreign Key → `offices` |
| `term_start` | DATE | Nullable if start date unknown; use `first_seen_at` as a floor when null |
| `term_end` | DATE | Null = "currently serving, end unknown" |
| `first_seen_at` | TIMESTAMPTZ | When this term was first ingested; always populated from the creating `source_records.fetched_at`; serves as a temporal floor when `term_start` is unknown |
| `last_verified_at` | TIMESTAMPTZ | When a source last confirmed this person is still in this role; updated on every successful refresh |
| `is_current` | BOOLEAN | True as of `last_verified_at`; set to false when a refresh no longer finds this person in the role |
| `appointment_type` | TEXT | `elected`, `appointed`, `interim` |
| `confidence_score` | NUMERIC(3,2) | 0.0–1.0; propagated from source; low scores trigger review queue |

`term_end = NULL` means "currently serving, end unknown" and is intentional — it is not the same as a known-future end date. Records where `term_start` is also NULL represent officials confirmed present by a source but with no date context at all; `first_seen_at` provides the temporal floor in those cases. `last_verified_at` documents the freshness of the `is_current` flag: an `is_current = true` row last verified six months ago carries much less confidence than one verified last week, and downstream consumers can filter on it accordingly.

---

**`source_records`** — raw source data for every data pull

| Column | Type | Notes |
|---|---|---|
| `id` | UUID | Primary key |
| `term_id` | UUID | Foreign Key → `terms`; nullable (record may exist before being matched to a term) |
| `source_name` | TEXT | Human-readable label: `"Ballotpedia"`, `"Google Civic API"`, `"2021 County Survey.xlsx"`, etc. |
| `source_type` | TEXT | `api`, `scrape`, `llm`, `spreadsheet`, `manual` |
| `source_url` | TEXT | Specific URL fetched or file path; nullable for manual entries |
| `reliability_tier` | SMALLINT | 1 (highest) to 3 (lowest); manual = 0 (unverified) |
| `raw_data` | JSONB | Full original payload — API JSON response, scraped HTML excerpt, LLM output, spreadsheet row |
| `confidence_score` | NUMERIC(3,2) | Source-level confidence; feeds `terms.confidence_score` |
| `llm_extracted` | BOOLEAN | True when LLM was used to parse the raw content |
| `fetched_at` | TIMESTAMPTZ | When this record was collected |

`source_records` is populated for every data pull which gives full auditability (what did we actually receive?), conflict detection (two sources disagree on who holds an office), and the ability to re-process raw data without re-fetching.

---

## 2. Source Strategy

### Tier 1 — Aggregators and Paid Data

To ensure the dataset is robust and trusted, first look at centralized aggregators to establish a baseline.

- **Structured APIs** (**Google Civic API**, **Ballotpedia API**, **Cicero**, **KnowWho**): Reliable, well-structured data but require either API keys or licensing fees. Best evaluated with a sample of hard-to-reach counties to assess coverage before committing to cost.

- **Scrapeable Public Databases** (**Ballotpedia** website, **VoteSmart**): Structured enough for deterministic parsing without API access. Useful for validation and biographical gap-fill (e.g., party affiliation, term dates) for officials already identified through other sources. Coverage thins at the county level.

### Tier 2 — State-Level Data

State-level sources offer high engineering return on investment because one scrape can provide a lot of data. Attempts should be made to determine if state-maintained webpages provide the county-level elected official data we are seeking.

- **State Secretary of State & Election Boards**: SOS websites can publish rosters for constitutional offices like Sheriff, Clerk, and Treasurer.
- **State Associations of Counties**: Maintain member directories with administrative contacts, often more current than official government sources.
- **State Open Data Portals** (e.g., Washington, Colorado): Publish machine-readable official directories where available — worth checking before building a scraper.

### Tier 3 — County-Level Data

**USA.gov** has a Local Governments page linking to some state-level pages which contain data on county officials. Most of these contain a "Government" section. This would be the primary path for data collection: Navigate from USA.gov down to the county level and use an LLM scraper to pull the data from each HTML page. County sites are the most authoritative, but least cooperative so they would be the main target for LLM extraction (see Section 3).

> **Note:** USA.gov's local government links are incomplete — many point to state-level pages rather than individual county sites, and rural county coverage is thin. It is a useful starting point but should not be relied on as a complete navigation path to all 3,143 counties. Where USA.gov links fall short, the state Association of Counties directory or the state Secretary of State website is the better entry point for navigating to individual county pages.

### Tier 4 — Legacy Data and Manual Research

If the automated solution fails for a given county, we can fall back to historical records and manual research.

- **Historical Spreadsheets**: There are reportedly spreadsheets containing some amount of this data which exist today. That data could be quite useful in populating the database as well as potentially providing additional sources.

- **Manual Research and Outreach**: Analog collection methods could be leveraged for any counties which cannot have their data populated through other means. Collection methods include searching local newspapers or reaching out to county offices via phone or email. If manual outreach is performed, attempts should be made to find out term limits and establish a timeframe for necessary manual data refresh in the future. Additionally, requests could be made to have that local government website updated.

### Source Evaluation Criteria

Applied in priority order when choosing which source to trust for a given local government office:

1. **Freshness** — when was the source last updated?
2. **Coverage** — does this source actually cover this county and office type?
3. **Machine-readability** — how much processing is required to extract a structured record?
4. **Cross-verifiability** — can a second source confirm this?
5. **Cost** — API fees, compute, engineering time

---

## 3. Collection Architecture

### LLM-First Design Rationale

Creating and maintaining web scrapers for 3,143 different county websites is a daunting task. Each HTML page differs enough from another that thousands of bespoke parsing solutions would be required for successful data retrieval. LLMs have massive leverage in this space in 2026.

LLMs are not used for Tier 1 sources — those are structured API calls that return JSON directly. **Firecrawl** is used as the unified scraper for Tier 2 and Tier 3 web sources: it converts any URL to clean markdown before passing to the LLM for extraction, handles JavaScript-rendered pages and PDFs, and requires no per-site parsing rules. This eliminates the per-state scraper maintenance that a traditional BeautifulSoup approach would require. **Crawl4AI** is an open-source self-hosted alternative if avoiding external API costs is a priority.

### Pipeline Overview

```
[Election Calendar + Scheduled Refresh]
          |
          v
[Collector Layer]
  |-- API Collectors        (Google Civic, Ballotpedia, VoteSmart)
  |-- Firecrawl Scraper     (Tier 2 state sites + Tier 3 county sites) --> [LLM Extraction]
  |-- Spreadsheet Ingestors --> [Office Normalization]
  |-- Manual Entry
          |
          v
[source_records write]   <-- all collectors write here first
          |
          v
[Validation & Entity Resolution]
  |-- County FIPS lookup    (name + state → FIPS for spreadsheets)
  |-- Office normalization  (alias → office_type)
  |-- Name normalization    (strip suffixes, lowercase, trim)
  |-- Deduplication         (dedupe_hash check against officials)
  |-- Cross-source conflict detection
  |-- Completeness check    (expected offices per county_type)
          |
          v
[Confidence Scoring]
  |-- High confidence (>= 0.7): write to officials / offices / terms
  |-- Low confidence  (<  0.7): route to review queue
          |
          v
[PostgreSQL]
          |
          v
[Change Detection]   (diff against prior snapshot, alert on unexpected changes)
```

### LLM Extraction Layer

Applied to Tier 2 state sites and Tier 3 county websites. Firecrawl fetches the target URL and converts it to clean markdown before the extraction prompt is sent, reducing token cost and handling JavaScript rendering automatically.

**Input:** Markdown converted from the target page + county name + state + list of expected office types for that county type.

**Prompt instructs the model to return:**
```json
{
  "officials": [
    {
      "name": "Jane Smith",
      "title": "Sheriff",
      "party": "Democrat",
      "email": "jsmith@countygov.example",
      "phone": "555-123-4567",
      "social_media_urls": ["https://x.com/jsmith_sheriff"],
      "term_start": "2023-01-01",
      "term_end": null
    }
  ],
  "confidence": 0.85,
  "notes": "Page lists 3 officials; party affiliation not shown for 2 of them"
}
```

**Output handling:**
- Full LLM response stored in `source_records.raw_data` with `llm_extracted = true`
- `confidence` from LLM response maps to `source_records.confidence_score`
- Records below 0.7 confidence route to human review before writing to `terms`
- The HTML excerpt is also stored so the extraction can be rerun with an improved prompt without re-fetching

**Cost management:** HTML is trimmed to the relevant body section before sending. For counties with no useful page content, skip LLM call and log the county as `needs_manual_review`.


### Pipeline Technology

**Dagster** is the recommended orchestrator. Its asset-based model maps naturally to the data model — counties, offices, officials, and terms are all assets with defined dependencies and lineage. It supports partitioned runs by state or county group, event-based triggering for election calendar integration, and has built-in observability for monitoring data quality across refreshes. Dagster integrates cleanly with both Firecrawl and PostgreSQL via its IO manager system.

### Refresh Cadence

County-level officeholders serve 2–4 year terms, so most records go months without changing. The election calendar trigger handles the post-election surge periods; the baseline cadence only needs to catch appointments, vacancies, and corrections between elections.

| Source type | Cadence | Trigger |
|---|---|---|
| Tier 1 APIs (Ballotpedia, Google Civic) | Monthly | Scheduled + post-election surge |
| Tier 2 state sites | Monthly | Scheduled + post-election certification |
| Tier 3 county sites | Monthly | Scheduled + election calendar |
| Tier 4 — Historical spreadsheets | One-time ingest | On discovery |
| Tier 4 — Manual entries | As needed | Human |

### Election Calendar Integration

A `county_election_dates` lookup (maintainable separately, sourced from Ballotpedia or state election boards) drives targeted re-collection. Two to four weeks after a certification date, the pipeline re-scrapes the relevant county and state sources, re-runs LLM extraction if needed, and compares to the prior snapshot. This keeps freshness high in the post-election window without burning API quota on counties in the middle of a four-year term.

---

## 4. Tradeoffs and Open Questions

### Assumptions Made

- **FIPS codes as the geographic spine**: Standard, stable, widely supported in civic data, used by Census and most government APIs.
- **Counties will require manual fallback**: Automated coverage of 100% is not realistic at launch. These counties should be flagged explicitly rather than silently absent from the dataset.
- **Firecrawl cost is an acceptable trade**: Using Firecrawl as the unified scraper for Tier 2 and Tier 3 eliminates per-site parsing maintenance. The API cost is justified by the reduction in engineering overhead across thousands of state and county sources. At batch pricing, running LLM extraction across Tier 2 and Tier 3 sources on a monthly cadence is not prohibitive relative to the alternative of maintaining bespoke scrapers.
- **Dagster is the right orchestrator**: Its asset-based model and partitioned run support align well with the data model and election calendar trigger requirements.

### Known Weaknesses

- **Entity resolution for common names** is challenging. `dedupe_hash` on `{last_name, first_name, county_fips}` catches re-ingestion of the same person from a second source. It does not catch a person who moves counties, a name change, or two different "John Smith"s in the same county. External source IDs stored in `source_records` provide a stronger signal where available but are not guaranteed to exist.
- **Firecrawl dependency**: The entire Tier 2 and Tier 3 scraping pipeline runs through a single external API. A Firecrawl outage, pricing change, or rate limit event halts collection. Crawl4AI (self-hosted) exists as a fallback but requires engineering time to deploy.
- **Interim appointments and mid-term vacancies** are chronically underreported. `is_current` flags can become stale between monthly refreshes. The election calendar trigger helps with *elections* but not with *appointments*.
- **LLM hallucination** is a risk on any low-content page across both Tier 2 and Tier 3. A state SOS page that lists offices without names, or a county site with only a phone number, gives the model little to work with. Confidence scoring and the review queue mitigate this but do not eliminate it.
- **Spreadsheet vintage unknown**: If an analyst spreadsheet has no collection date, we cannot assess freshness. Treat undated spreadsheet data as unverified until cross-confirmed.
- **No known single source covers all 3,143 counties for all offices**: Gaps will exist at launch. The gap report (counties flagged `needs_manual_review`) is a feature, not a failure — it tells future analysts exactly where to focus.


### Open Questions for Murmuration

- Is there an existing internal county reference table or FIPS registry to build from, or should this system be the canonical source?
- Are there existing vendor relationships — Ballotpedia API access, a Cicero license — that change the Tier 1 source strategy?
- **Firecrawl vs Crawl4AI**: Is the Firecrawl API budget acceptable at scale, or should Crawl4AI be evaluated as a self-hosted alternative to reduce ongoing cost?
- **Dagster hosting**: Self-host Dagster or use Dagster Cloud? The answer affects infrastructure cost and operational overhead.
- What downstream systems consume this data? The answer affects whether a REST API layer, a read replica, or direct Postgres access is the right interface.
- Do the historical analyst spreadsheets have known collection dates? This determines whether they are safe to use for `is_current` or only for bootstrapping the offices/officials tables.

---

## 5. Example Analyst Queries

These queries assume a downstream analyst has read access to the Postgres database. All use the five-table schema defined above.

---

**Get all current officials for a specific county**

```sql
SELECT
    o.first_name,
    o.last_name,
    o.party,
    of.office_type    AS office,
    of.local_title,
    o.email,
    o.phone,
    t.appointment_type,
    t.term_start,
    t.confidence_score
FROM terms t
JOIN officials o  ON o.id = t.official_id
JOIN offices  of ON of.id = t.office_id
JOIN counties c  ON c.county_fips = of.county_fips
WHERE c.county_fips = '06037'   -- Los Angeles County
  AND t.is_current = true
ORDER BY of.category, of.office_type;
```

---

**Build a contact list of all current sheriffs in a state**

Useful for targeted outreach to law enforcement leadership.

```sql
SELECT
    c.county_name,
    c.state_abbreviation,
    o.first_name,
    o.last_name,
    o.party,
    o.email,
    o.phone
FROM terms t
JOIN officials o  ON o.id = t.official_id
JOIN offices  of ON of.id = t.office_id
JOIN counties c  ON c.county_fips = of.county_fips
WHERE of.office_type = 'Sheriff'
  AND c.state_abbreviation = 'TX'
  AND t.is_current = true
ORDER BY c.county_name;
```

---


**Find officials whose terms expire in the next 12 months**

Useful for election-cycle planning and proactive outreach before transitions.

```sql
SELECT
    c.county_name,
    c.state_abbreviation,
    of.office_type    AS office,
    o.first_name,
    o.last_name,
    o.party,
    o.email,
    t.term_end
FROM terms t
JOIN officials o  ON o.id = t.official_id
JOIN offices  of ON of.id = t.office_id
JOIN counties c  ON c.county_fips = of.county_fips
WHERE t.is_current = true
  AND t.term_end BETWEEN CURRENT_DATE AND CURRENT_DATE + INTERVAL '12 months'
ORDER BY t.term_end, c.state_abbreviation, c.county_name;
```

---


## 6. AI Usage Note

I used Gemini to conduct research on local government data collection methods and sources. I verified all suggested sources by navigating to their webpages and attempting to uncover the desired data. This process helped me to understand the shape and availability of data, and ruled many of the initial suggestions out. It also helped me to understand that the real challenge of this project is acquiring the data.
I used Claude Code to create the document outline, generate a pipeline proposal and data models. I significantly altered the initial models suggested and reduced the table count from the initially proposed 8 to 5. Then, I took an iterative approach to asking questions, researching, and revising the document with Claude’s assistance. I requested numerous changes and additions over a period of 5 hours. I researched all technologies suggested by Claude by navigating to their product websites, Github repos, and online developer forums to gain understanding of pros and cons for each. I have written this section fully on my own and have edited every other section of this document to ensure it is in line with my standards of writing and design. 
