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
| `raw_data` | JSONB | Full original payload — API JSON response, scraped markdown content, LLM output, spreadsheet row |
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
- **State Open Data Portals** (e.g., Washington, Colorado): Publish machine-readable official directories where available.

### Tier 3 — County-Level Data

- **Local County Websites**: USA.gov has a Local Governments page linking to state -> county-level pages which contain data on county officials. Most of these contain a "Government" section. This would be the primary path for data collection: Navigate from USA.gov down to the county level and use an LLM scraper to pull the data from each HTML page. County sites are the most authoritative, but least cooperative so they would be the main target for LLM extraction (see Section 3). Where USA.gov links fall short, the state Association of Counties directory or the state Secretary of State website is the better entry point for navigating to individual county pages.

### Tier 4 — Legacy Data and Manual Research

If the automated solution fails for a given county, we can fall back to historical records and manual research.

- **Historical Spreadsheets**: If there are legacy spreadsheets, files or databases containing some amount of this data today, that data could be quite useful in populating the database as well as potentially providing additional sources.

- **Manual Research and Outreach**: Analog collection methods could be leveraged for any counties which cannot have their data populated through other means. Collection methods include searching local newspapers or reaching out to county offices via phone or email. If manual outreach is performed, attempts should be made to find out term limits and establish a timeframe for necessary manual data refresh in the future. Additionally, requests could be made to have that local government website updated.

- **LLM Seed Query**: As a last resort when all other sources fail for a given county, an LLM can be queried directly for known officials. This is the least reliable option: LLMs have a training data cutoff, coverage of small rural counties is thin, and the model will produce a plausible-sounding answer even when it does not have reliable data. Records produced this way must be written to `source_records` with `source_type: "llm"`, `llm_extracted: true`, and `reliability_tier: 3`, and routed directly to the review queue before writing to `terms`. They should be treated as unverified seeds — useful for knowing where to look, not as confirmed data. The goal is to surface a record that a human reviewer or a future automated run can confirm or reject, rather than leaving the county silently absent from the dataset.

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

Creating and maintaining web scrapers for 3,143 different county websites is a daunting task. Each HTML page differs enough from another that thousands of bespoke parsing solutions would be required for successful data retrieval. The combination of a unified scraper and an LLM eliminates that problem entirely.

LLMs are not used for Tier 1 sources — those are structured API calls that return JSON directly. For Tier 2 and Tier 3 web sources, **Firecrawl** solves the *fetching* problem: it converts any URL to clean markdown, handling JavaScript-rendered pages, PDFs, and other document types without per-site configuration. County sites frequently link directly to PDF rosters; Firecrawl auto-detects and parses these from the URL transparently, requiring no special handling. The **LLM** then solves the *parsing variability* problem: it reads whatever markdown Firecrawl returns and extracts structured data regardless of how each site is laid out, with no bespoke parsing rules required. Together they replace what would otherwise be thousands of site-specific scrapers. For county-scale runs, Firecrawl's `batch_scrape` endpoint accepts a list of URLs and processes them in parallel, returning results asynchronously — considerably faster than sequential per-county fetches at production scale (3,143 counties). **Crawl4AI** is an open-source self-hosted alternative to Firecrawl if avoiding external API costs is a priority.

### Pipeline Overview

```
[Election Calendar + Scheduled Refresh]
          |
          v
[Collector Layer]
  |-- API Collectors        (Google Civic, Ballotpedia, VoteSmart)
  |-- Firecrawl Scraper     (Tier 2 state sites + Tier 3 county sites) --> [LLM Extraction] --> [Post-Extraction Validation]
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

Applied to Tier 2 state sites and Tier 3 county websites. **Claude** (via the Anthropic API) is the recommended model for extraction given its strong performance on structured output tasks. Firecrawl fetches the target URL and converts it to clean markdown before the extraction prompt is sent, reducing token cost and handling JavaScript rendering automatically.

**Input:** Firecrawl fetches the target URL and returns the page's main content as clean markdown — navigation, scripts, and boilerplate stripped; HTML tables converted to markdown tables. The `markdown` field from the Firecrawl response is passed to the LLM along with county name, state, and the list of expected office types.

**Prompt schema — what the model is instructed to return:**
```json
{
  "officials": [
    {
      "name": "Jane Smith",
      "title": "Sheriff",
      "party": "Democrat",
      "email": "jsmith@countygov.example",
      "phone": "555-123-4567",
      "extraction_type": "explicit",
      "social_media_urls": ["https://x.com/jsmith_sheriff"],
      "term_start": "2023-01-01",
      "term_end": null
    }
  ],
  "notes": "Page lists 3 officials; party affiliation not shown for 2 of them"
}
```

`extraction_type` is a per-record categorical field with four allowed values:
- `explicit` — the value appeared verbatim on the page
- `inferred` — the value was derived from context (e.g. party inferred from an endorsement mention)
- `ambiguous` — the page contained conflicting or unclear signals
- `not_found` — the expected field was absent from the page entirely

**End-to-end example**

*Firecrawl `markdown` output:*
```markdown
# Grays Harbor County — Elected Officials

**Sheriff:** Rick Scott — (360) 249-3711
**Prosecuting Attorney:** Katie Svoboda — katie.svoboda@co.grays-harbor.wa.us
Re-elected November 2022 on the Democratic ticket.

| District | Commissioner | Phone |
|---|---|---|
| 1 | Vickie Raines | (360) 249-4222 |
| 2 | Jill Warne | (360) 249-4222 |

*Auditor, Assessor, Treasurer, and Clerk are listed on the Departments page.*
```

*Prompt (user message — sent after a system message instructing the model to return only valid JSON):*
```
County: Grays Harbor County, WA
Expected offices: Sheriff, Auditor, Assessor, Clerk, Treasurer, Prosecuting Attorney, Commissioner

Extract each official. Per record, set extraction_type to:
  "explicit"   — value appeared verbatim
  "inferred"   — derived from context (explain in notes)
  "ambiguous"  — conflicting signals
  "not_found"  — field absent from page

Include a not_found entry for every expected office not present on the page.

[firecrawl markdown pasted here]
```

*Expected response:*
```json
{
  "officials": [
    {
      "name": "Rick Scott", "title": "Sheriff",
      "party": null, "email": null, "phone": "(360) 249-3711",
      "term_start": null, "term_end": null, "extraction_type": "explicit"
    },
    {
      "name": "Katie Svoboda", "title": "Prosecuting Attorney",
      "party": "Democrat", "email": "katie.svoboda@co.grays-harbor.wa.us", "phone": null,
      "term_start": null, "term_end": null, "extraction_type": "inferred"
    },
    {
      "name": "Vickie Raines", "title": "Commissioner",
      "party": null, "email": null, "phone": "(360) 249-4222",
      "term_start": null, "term_end": null, "extraction_type": "explicit"
    },
    {
      "name": "Jill Warne", "title": "Commissioner",
      "party": null, "email": null, "phone": "(360) 249-4222",
      "term_start": null, "term_end": null, "extraction_type": "explicit"
    },
    { "name": null, "title": "Auditor", "extraction_type": "not_found" },
    { "name": null, "title": "Assessor", "extraction_type": "not_found" },
    { "name": null, "title": "Treasurer", "extraction_type": "not_found" },
    { "name": null, "title": "Clerk", "extraction_type": "not_found" }
  ],
  "notes": "Svoboda party inferred from 'Re-elected November 2022 on the Democratic ticket.' Auditor, Assessor, Treasurer, Clerk not on this page — referenced as being on the Departments page."
}
```

The four `not_found` entries are required by the prompt; they tell the pipeline exactly which offices to look for on the Departments page. The `notes` field carries context that a numeric confidence score would have obscured.

**Output handling:**
- Full LLM response stored in `source_records.raw_data` with `llm_extracted = true`
- Per-record `extraction_type` replaces a global confidence score — the model reports what it did, not how it felt about it
- Post-extraction format validation runs programmatically before writing to `source_records`: phone pattern check, email format check, name plausibility
- `explicit` records with passing format checks write directly to `terms`
- `inferred` records write to `terms` but are flagged for audit
- `ambiguous` records trigger a second-pass extraction with a differently-worded prompt; agreement between passes clears the flag; disagreement routes to human review
- `not_found` records are written to `source_records` only and logged as coverage gaps
- The markdown content and full LLM response are stored in `raw_data` so the extraction can be rerun with an improved prompt without re-fetching

### Confidence and Verification

A self-reported confidence score from an LLM does not measure whether extracted data is factually correct. It measures how unambiguous the input appeared to the model — a high score reflects a clear, well-structured page, not a verified fact. Critically, LLMs can be overconfident precisely when hallucinating, because the model "felt" like it saw something clearly even when it did not.

This pipeline replaces self-reported scoring with five verifiable checks:

**1. Categorical extraction type instead of a numeric score.** `extraction_type` describes what the model did — found a value verbatim, derived it from context, encountered conflicting signals, or found nothing. This is a question the model can answer accurately about its own behavior. A numeric score is not.

**2. Programmatic format validation.** After the LLM returns, format checks run independently of the model: does the phone number contain a plausible digit pattern? Does the email contain `@` and a domain? Does the name parse into at least two tokens? These checks catch malformed output regardless of how confident the model was.

**3. Verbatim presence check.** For each field the model labels `explicit`, programmatically search the source markdown for the extracted value as a string. If `"Rick Scott"` does not appear in the markdown, the model's `explicit` label is wrong regardless of how confident it was. This closes a gap that `extraction_type` alone leaves open — the model can mislabel inferred or fabricated values as explicit. The deterministic scrapers in this pipeline (BeautifulSoup reading directly from table cells) are the functional equivalent of 100% verbatim verified: every extracted value is present in the source by construction.

**4. Multi-pass consistency as a confidence signal.** For `ambiguous` records, a second extraction runs with a differently-worded prompt. If both passes return the same value, that agreement is a meaningful confidence signal — far stronger than any self-report. If they disagree, the record routes to human review.

**5. Cross-source agreement.** When the same official is confirmed by two independent sources, that agreement is the strongest signal available short of human review. The pipeline tracks which sources contributed to each official and applies a small confidence boost (+0.05) when more than one source confirms the same record, emitting a `CROSS_SOURCE_CONFIRMED` flag for auditability. With the current two sources (WACO and WSAC), overlap is rare since they cover different office types. This check becomes meaningful in production when Tier 1 APIs, Tier 2 state sites, and Tier 3 county pages all contribute to the same officials.

**6. Audit sampling.** A random sample of `explicit` records are verified against their source pages each run cycle. This produces a measured accuracy rate for the extraction layer over time, which is the calibration data a self-reported score never provides.

**Cost management:** Markdown content is trimmed to the relevant body section before sending. For counties with no useful page content, skip LLM call and log the county as `needs_manual_review`.

**Design tradeoff — Firecrawl JSON mode vs. markdown + Claude:** Firecrawl offers a built-in JSON extraction mode that accepts a schema and prompt and returns structured data directly, removing the separate Claude API call. It is not used here for four reasons. First, it requires relinquishing control over the model and prompt, making `extraction_type` classification and multi-pass consistency checking impossible — the two mechanisms that replace self-reported confidence scores. Second, the raw markdown is not returned unless explicitly requested alongside JSON, which means losing the intermediate state stored in `source_records.raw_data` for re-processing without re-fetching. Third, Firecrawl's internal model is undocumented and subject to change, introducing silent data quality risk across monthly refresh runs. Fourth, JSON mode costs 5 credits per page (1 base + 4 additional) versus 1 credit for markdown plus Claude API token costs, which favors the two-step approach at scale. JSON mode is appropriate for lightweight, one-off extractions where auditability is not required.


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

**Firecrawl caching and `maxAge`:** Firecrawl caches scraped pages for 2 days by default and serves cached results at the same credit cost. Routine monthly refreshes should use the default cache window — identical pages return faster with no extra cost. Post-election targeted re-collection runs must set `maxAge=0` to force a fresh fetch, since a cached page from before the election would silently return stale data. The election calendar trigger is the only place in this pipeline where `maxAge=0` is the correct setting.

### Election Calendar Integration

A `county_election_dates` lookup (maintainable separately, sourced from Ballotpedia or state election boards) drives targeted re-collection. Two to four weeks after a certification date, the pipeline re-scrapes the relevant county and state sources with `maxAge=0`, re-runs LLM extraction if needed, and compares to the prior snapshot. This keeps freshness high in the post-election window without burning API quota on counties in the middle of a four-year term.

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


### With More Time

- **Query documentation and analyst onboarding**: The five-table schema requires multi-table joins for most useful queries — a basic "who are the current officials in this county?" question touches four tables. A reference library of common named queries covering the most frequent use cases (contact lists by office type, terms expiring in a date range, gap reports) would lower the barrier significantly and double as onboarding material for new analysts.
- **Presentation layer**: Build PostgreSQL views that pre-join `terms`, `officials`, `offices`, and `counties` into flat, analyst-friendly shapes. A `current_officials_flat` view would make the most common queries trivially simple (`SELECT * FROM current_officials_flat WHERE state = 'TX'`) and reduce the risk of join errors producing silent data quality issues. This sits in front of the normalized tables without replacing them.
- **Stronger entity resolution**: Replace or supplement the `dedupe_hash` heuristic with a more robust approach — probabilistic name matching, external ID cross-referencing across `source_records`, or a human-review workflow specifically for flagged potential duplicates.
- **Vacancy and appointment monitoring**: Build a targeted signal for mid-term changes beyond the election calendar. Local news feeds, county RSS feeds, or a lightweight alert on `is_current` age could surface interim appointments and resignations that monthly scrapes miss.
- **Firecrawl redundancy**: Deploy and validate Crawl4AI as a self-hosted fallback so a Firecrawl outage does not halt the entire Tier 2 and Tier 3 pipeline.
- **Automated data quality reporting**: A scheduled report surfacing records with low confidence scores, stale `last_verified_at` dates, and counties approaching the `needs_manual_review` threshold — so data quality problems are visible before a downstream consumer notices them.

### Open Questions for Murmuration

- What downstream systems consume this data? 
- Is there legacy data? If so, do we want to port that data over?
- Is there an existing internal county reference table or FIPS registry to build from?
- Are there existing vendor relationships such as Ballotpedia API access or a Cicero license?
- Is the Firecrawl API budget acceptable at scale, or should Crawl4AI be evaluated as a self-hosted alternative to reduce ongoing cost?
- Is there an existing Anthropic API relationship or credit budget that makes Claude the right choice for LLM extraction? Should alternative models be evaluated for cost or performance?


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
