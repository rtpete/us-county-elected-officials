# U.S. County Elected Officials — Data Acquisition

The design document for this project is [`design.md`](./design.md).

---

## Part 2: Washington State MVP

### Prerequisites

- **Python 3.10+** — check with `python3 --version`
- **pip** — included with Python 3.4+; check with `pip --version`
- An internet connection (the script fetches live data from WACO, WSAC, and Census.gov)
- No API keys or paid accounts required

### Running the collector

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python3 collect.py
```

This produces three files in `data/`:

| File | Contents |
|---|---|
| `wa_officials.db` | SQLite database with the full 5-table schema from `design.md` |
| `wa_officials.csv` | Flat export of all current officials (join of all 5 tables) |
| `validation_flags.csv` | Records that triggered a quality check |


---


#### Why Washington State

In order to complete this MVP in the 2-3 hour window, I searched for states which have a machine-readable statewide source covering multiple office types. Most states do not have this data consolidated and publicly available online. Colorado and Iowa were both assessed, but I ran into machine access & data availability issues. 

Washington, however, has the **WACO member directory** (Washington Association of County Officials) and the **WSAC member directory** (Washington State Association of Counties) which are both accessible and have decent data coverage. These two sources provide data on eight office types across all 39 WA counties from publicly accessible HTML pages with no login or API key required. 

#### Sources used and why

**WACO Member Directory** (`countyofficials.org/Directory.aspx?DID=193`)  
Lists sheriff, auditor, assessor, clerk, treasurer, prosecuting attorney, and coroner for all 39 counties. Each county has its own HTML table inside a single page, with Name, Title, and Phone columns. Email could not be retrieved. This is a live membership directory maintained by the association, so it should be current. Rated reliability tier 2 (state-level association, not a primary government source).

**WSAC Member Directory** (`wsac.org/member-directory/`)  
Lists county commissioners and council members for all 39 counties in a single HTML table. Includes district number and title. No phone or email. Rated reliability tier 2.

**Census FIPS Reference** (`census.gov`)  
Used to map county names to 5-digit FIPS codes, which serve as the geographic primary key throughout the schema (as designed in Part 1). This is tier 1 reference data.

These sources were chosen because they are the most comprehensive, current, publicly accessible statewide sources I could find after evaluating alternatives. Firecrawl and an LLM extraction layer (as designed in Part 1) were not used here because these HTML tables are structured well enough for deterministic parsing.

#### Data quality concerns if this went to production

**Membership lag is the primary risk.** Both WACO and WSAC are membership directories rather than certified government records. An official who leaves office may remain listed until their organization updates its roster. There is no publication date on either directory page to assess how stale a given entry might be. In production, these sources should be cross-verified against primary government sources (SOS filings, county government pages) before `is_current = true` is written.

**No contact information for commissioners.** WSAC lists 142 commissioner and council-member records with names only and no contact information. Thus, all 142 records triggered `NO_CONTACT_INFO` validation flags. If contact information for commissioners is required downstream, it must come from a separate source such as individual county websites or phone outreach.

**No party affiliation or term dates from either source.** The `party`, `term_start`, and `term_end` fields are all null in this dataset. Term dates would require cross-referencing election results through sources like Ballotpedia or state SOS election returns.

**The data is varied and not always clean.** Even within a single state, the scraper encountered charter counties with non-standard office structures (King County), a vacant seat (Grays Harbor District 3), interim appointments (Jefferson County Sheriff, Kittitas County Coroner), and at least one likely source data entry error ("Cori, Cori" for the Kittitas acting coroner). These edge cases are flagged in `validation_flags.csv` rather than silently dropped. At national scale, this kind of variability will be the rule, not the exception, and must be accounted for.

**Dataset completeness is not guaranteed.** Coverage depends on WACO and WSAC membership so officials who are not members may be absent. 3 of 39 counties have no coroner or medical examiner in the output; whether those positions are vacant, held by non-members, or handled through a different arrangement is unknown. The 39-county list itself is complete and authoritative (seeded from Census FIPS), but the officials within it are not exhaustive.

**Confidence scores measure data completeness, not accuracy.** A score of 0.85 means the record has a full name and at least one contact field — it does not mean the person is verified to still hold office. Scores do not account for membership lag, source recency, or cross-verification against a second source. The three thresholds are: 0.85 (name + contact), 0.72 (name only), 0.45 (no parseable name).


#### With more time

The main gaps in this dataset are null values and unverified freshness. Given more time I would:

- **Cross-verify against the WA Secretary of State** to confirm current officeholders and get certified term dates, which would fill `term_start`, `term_end`, and allow `is_current` to be set with higher confidence than a membership directory alone provides.
- **Supplement commissioner contact info** from individual county websites, since WSAC provides names only. All 142 commissioner records currently have no phone or email.
- **Pull party affiliation** from Ballotpedia or state election returns to populate the `party` field, which is null across the entire dataset.
- **Investigate the 3 counties with no coroner or medical examiner** to determine whether the position is vacant, contracted out, or held by someone not in the WACO directory.
- **Resolve the King County edge cases** — specifically whether the Director of Records and Licensing Services maps to Clerk or should be excluded as an appointed role.

#### How AI was used
I used Claude Code substantially throughout this implementation, researching ideal states, writing code, and creating documentation. I reviewed all of the code created, adding comments & expanding docstrings to increase readability and maintainability. I manually validated that the data pulled is accurate and complete by spot checking and assessing edge cases against the source data. I have reviewed, edited, and added my own contributions throughout this README. While Claude did create this file, this section was written entirely on my own.
