#!/usr/bin/env python3
"""
collect.py — Washington State county elected officials collector.

Acquires data from two public sources:
  - WACO member directory  (sheriff, auditor, assessor, clerk, treasurer,
                            prosecuting attorney, coroner — all 39 counties)
  - WSAC member directory  (commissioners and council members)

Outputs:
  data/wa_officials.db        SQLite with the 5-table schema from design.md
  data/wa_officials.csv       Flat export of current officials
  data/validation_flags.csv   Flagged records needing review
"""

import csv
import hashlib
import json
import logging
import sqlite3
import time
import uuid
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import pandas as pd
import requests
from bs4 import BeautifulSoup, NavigableString

# ── Configuration ─────────────────────────────────────────────────────────────

WACO_URL = "https://countyofficials.org/Directory.aspx?DID=193"
WSAC_URL = "https://wsac.org/member-directory/"
FIPS_URL = "https://www2.census.gov/geo/docs/reference/codes2020/national_county2020.txt"

STATE_FIPS = "53"
STATE_NAME = "Washington"
STATE_ABBR = "WA"
TERM_YEARS = 4      # All WA county offices serve 4-year terms
PARTISAN   = True   # WA county offices appear on partisan ballots

DATA_DIR = Path("data")

# Set headers to get around basic bot protections to receive full HTML
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )
}

# Maps raw scraped titles to canonical office_type values.
# local_title (what the county calls the role) is preserved separately on offices rows.
# Interim/Acting qualifiers are stripped before this lookup — see parse_title().
OFFICE_TYPE_MAP: dict[str, str] = {
    "Sheriff":                               "Sheriff",
    "Auditor":                               "Auditor",
    "Coroner":                               "Coroner",
    "Medical Examiner":                      "Coroner",           # some counties have ME instead of elected coroner
    "Treasurer":                             "Treasurer",
    "Chief Treasury Officer":                "Treasurer",         # King County's title for the treasury function
    "Assessor":                              "Assessor",
    "Assessor-Treasurer":                    "Assessor-Treasurer", # Pierce County combines these two offices
    "Clerk":                                 "Clerk",
    "Prosecuting Attorney":                  "Prosecuting Attorney",
    "Commissioner":                          "Commissioner",
    "Council Member":                        "Commissioner",
    "Councilmember":                         "Commissioner",
    "Councilor":                             "Commissioner",      # Clark County's term
    "Councilmember / Executive Pro-Tempore": "Commissioner",      # Whatcom County
    "County Executive":                      "County Executive",  # elected in charter counties
    "Director of Elections":                 "Auditor",           # King County's elections function
}

OFFICE_CATEGORY: dict[str, str] = {
    "Sheriff":              "executive",
    "Auditor":              "administrative",
    "Coroner":              "administrative",
    "Treasurer":            "administrative",
    "Assessor":             "administrative",
    "Assessor-Treasurer":   "administrative",
    "Clerk":                "administrative",
    "Prosecuting Attorney": "judicial",
    "Commissioner":         "legislative",
    "County Executive":     "executive",
}

BOARD_OFFICES = {"Commissioner"}

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
log = logging.getLogger(__name__)


# ── Data structures ───────────────────────────────────────────────────────────

@dataclass
class RawOfficial:
    county_name: str
    local_title: str
    raw_name:    str
    phone:       str
    email:       str
    source_name: str
    source_url:  str
    raw_row:     dict   # original scraped row, stored verbatim in source_records.raw_data


@dataclass
class Flag:
    record_id: str
    flag_type: str
    detail:    str


# ── HTTP helper ───────────────────────────────────────────────────────────────

def fetch(url: str, retries: int = 2) -> str:
    """GET a URL with simple retry and a polite inter-request delay.

    Args:
        url: The URL to fetch.
        retries: Number of additional attempts after the first failure (default 2).

    Returns:
        The response body as a decoded string.

    Raises:
        requests.RequestException: If all attempts fail.
    """
    for attempt in range(retries + 1):
        try:
            r = requests.get(url, headers=HEADERS, timeout=15)
            r.raise_for_status()
            time.sleep(0.5)
            return r.text
        except requests.RequestException as exc:
            if attempt == retries:
                raise
            log.warning("Retry %d/%d for %s: %s", attempt + 1, retries, url, exc)
            time.sleep(2)


# ── FIPS reference ─────────────────────────────────────────────────────────────

def fetch_wa_fips() -> tuple[dict[str, str], dict[str, str]]:
    """Pull the Census county FIPS reference file and extract Washington entries.

    Returns:
        Tuple of (name_to_fips, fips_to_name) where:
          name_to_fips maps lowercase county name → 5-digit FIPS (e.g. "adams county" → "53001").
            Lowercase keys allow case-insensitive matching against scraped source data.
          fips_to_name maps 5-digit FIPS → proper-case county name (e.g. "53001" → "Adams County").
            Used to populate the counties table with display-ready names.
    """
    log.info("Fetching WA FIPS reference from Census")
    name_to_fips: dict[str, str] = {}
    fips_to_name: dict[str, str] = {}
    for line in fetch(FIPS_URL).strip().split("\n")[1:]:
        parts = line.split("|")
        if len(parts) < 5 or parts[0] != "WA":
            continue
        fips = parts[1] + parts[2].zfill(3)   # "53" + "001" → "53001"
        name = parts[4]                         # "Adams County"
        name_to_fips[name.lower()] = fips
        fips_to_name[fips] = name
    log.info("Loaded FIPS for %d WA counties", len(name_to_fips))
    return name_to_fips, fips_to_name


def resolve_fips(county_name: str, name_to_fips: dict[str, str]) -> Optional[str]:
    """Map a source county name to its 5-digit FIPS code (case-insensitive).

    Tries the name as-is (lowercased), then with ' county' appended if not already present.
    This handles sources that omit the word "County" (e.g. "Adams" vs "Adams County").

    Args:
        county_name: Raw county name from the scraped source (e.g. "Adams County" or "Adams").
        name_to_fips: Lowercase-keyed lookup dict from fetch_wa_fips().

    Returns:
        5-digit FIPS string (e.g. "53001"), or None if no match is found.
        A None return triggers an UNRESOLVED_COUNTY validation flag downstream.
    """
    n = county_name.lower().strip()
    if n in name_to_fips:
        return name_to_fips[n]
    if not n.endswith(" county"):
        candidate = n + " county"
        if candidate in name_to_fips:
            return name_to_fips[candidate]
    return None


# ── Scrapers ──────────────────────────────────────────────────────────────────

def scrape_waco() -> list[RawOfficial]:
    """Scrape the WACO member directory for non-commissioner county offices.

    Page structure: div#CityDirectoryLeftMargin contains alternating children:
      div.DirectoryCategoryText  — county name (e.g. "Adams County")
      div.pageStyles             — staff table with columns:
                                   Name | Title | Email[hidden] | Phone | ExtraPhone[hidden]

    The email column is present in the DOM but hidden via display:none and empty
    for all entries, so only phone data is available from this source.

    Returns:
        List of RawOfficial records, one per staff row across all 39 counties.
        Covers sheriff, auditor, assessor, clerk, treasurer, prosecuting attorney,
        and coroner/medical examiner. Does not include commissioners (see scrape_wsac).

    Raises:
        RuntimeError: If the expected page structure is not found, indicating the
            site layout has changed and the parser needs to be updated.
    """
    log.info("Scraping WACO: %s", WACO_URL)
    soup = BeautifulSoup(fetch(WACO_URL), "lxml")
    main_div = soup.find("div", id="CityDirectoryLeftMargin")
    if not main_div:
        raise RuntimeError("WACO: CityDirectoryLeftMargin div not found — page structure may have changed")

    records: list[RawOfficial] = []
    current_county: Optional[str] = None

    for child in main_div.children:
        if isinstance(child, NavigableString):
            continue
        classes = child.get("class") or []

        if "DirectoryCategoryText" in classes:
            text = child.get_text(strip=True)
            # Skip the top-level "WACO Membership" label; capture county-level headings
            if any(w in text for w in ("County", "Parish", "Borough", "Island")):
                current_county = text

        elif "pageStyles" in classes and current_county:
            table = child.find("table")
            if not table:
                continue
            for row in table.find_all("tr"):
                cells = [td.get_text(strip=True) for td in row.find_all(["td", "th"])]
                # Skip header rows (Name/Staff) and empty rows
                if not cells or cells[0] in ("Name", "Staff", ""):
                    continue
                raw_name    = cells[0]
                local_title = cells[1] if len(cells) > 1 else ""
                email       = cells[2] if len(cells) > 2 else ""   # hidden column, always empty
                phone       = cells[3] if len(cells) > 3 else ""
                records.append(RawOfficial(
                    county_name=current_county,
                    local_title=local_title,
                    raw_name=raw_name,
                    phone=phone.strip(),
                    email=email.strip(),
                    source_name="WACO Member Directory",
                    source_url=WACO_URL,
                    raw_row={
                        "county": current_county,
                        "name":   raw_name,
                        "title":  local_title,
                        "phone":  phone,
                        "email":  email,
                    },
                ))

    log.info("WACO: %d records across %d counties", len(records),
             len(set(r.county_name for r in records)))
    return records


def scrape_wsac() -> list[RawOfficial]:
    """Scrape the WSAC member directory for county commissioners and council members.

    Single HTML table with columns:
      County | District | Title | First Name | Last Name | Email

    The table has duplicate header rows at both the first and last positions (a quirk
    of the site's CMS), both of which are skipped. Email is present as a column but
    empty for all entries; no phone data is available from this source.

    Returns:
        List of RawOfficial records, one per commissioner/council member row.
        Names are recombined into "Last, First" format so parse_name() handles
        them consistently with WACO records.

    Raises:
        RuntimeError: If no table is found on the page.
    """
    log.info("Scraping WSAC: %s", WSAC_URL)
    soup = BeautifulSoup(fetch(WSAC_URL), "lxml")
    table = soup.find("table")
    if not table:
        raise RuntimeError("WSAC: no table found — page structure may have changed")

    records: list[RawOfficial] = []
    for row in table.find_all("tr")[1:-1]:   # skip first and last rows — both are header rows
        cells = [td.get_text(strip=True) for td in row.find_all(["td", "th"])]
        if len(cells) < 5:
            continue
        county_name = cells[0]
        district    = cells[1]
        title       = cells[2]
        first_name  = cells[3]
        last_name   = cells[4]
        email       = cells[5] if len(cells) > 5 else ""
        # Recombine into "Last, First" so parse_name() handles it uniformly
        raw_name = f"{last_name}, {first_name}" if last_name else first_name
        records.append(RawOfficial(
            county_name=county_name,
            local_title=title,
            raw_name=raw_name,
            phone="",
            email=email.strip(),
            source_name="WSAC Member Directory",
            source_url=WSAC_URL,
            raw_row={
                "county":     county_name,
                "district":   district,
                "title":      title,
                "first_name": first_name,
                "last_name":  last_name,
                "email":      email,
            },
        ))

    log.info("WSAC: %d commissioner/council records", len(records))
    return records


# ── Normalization helpers ─────────────────────────────────────────────────────

def parse_name(raw: str) -> tuple[str, str]:
    """Split a raw name string into (first_name, last_name).

    When commas are present, the first token is treated as the last name and the
    final token as the first name. Any middle tokens (credentials, suffixes) are
    discarded. When no comma is present, splits on the last space.

    Args:
        raw: Name string exactly as scraped from the source.

    Returns:
        Tuple of (first_name, last_name). Either may be an empty string if the
        input cannot be split (e.g. a single-token string with no spaces).

    Examples:
        "Thurman, Brad"         → ("Brad",  "Thurman")
        "Leach, D-ABMDI, Bill"  → ("Bill",  "Leach")   middle credential dropped
        "Fusaro, MD, Aldo"      → ("Aldo",  "Fusaro")
        "Van Pelt, Debra"       → ("Debra", "Van Pelt")
        "John Smith"            → ("John",  "Smith")
    """
    raw = raw.strip()
    if "," in raw:
        parts = [p.strip() for p in raw.split(",")]
        return parts[-1], parts[0]
    # "First Last" — split on last space so multi-word last names work
    parts = raw.rsplit(" ", 1)
    return (parts[0], parts[1]) if len(parts) == 2 else ("", raw)


def parse_title(title: str) -> tuple[Optional[str], str]:
    """Map a raw scraped title to a canonical (office_type, appointment_type) pair.

    Strips "Acting " prefix and "- Interim" suffix before the OFFICE_TYPE_MAP lookup,
    setting appointment_type to "interim" when either qualifier is present. This keeps
    interim officeholders in the same office row as their permanent counterparts while
    correctly flagging their appointment status on the term.

    Args:
        title: Raw title string exactly as scraped (e.g. "Acting Coroner", "Councilor").

    Returns:
        Tuple of (office_type, appointment_type) where:
          office_type is the canonical string from OFFICE_TYPE_MAP, or None if the
            cleaned title is not recognized (triggers UNKNOWN_OFFICE_TYPE flag).
          appointment_type is "interim" if an acting/interim qualifier was present,
            otherwise "elected".
    """
    t = title.strip()
    appointment_type = "elected"
    if t.startswith("Acting "):
        t = t[7:].strip()
        appointment_type = "interim"
    if "- Interim" in t:
        t = t.split("- Interim")[0].strip()
        appointment_type = "interim"
    return OFFICE_TYPE_MAP.get(t), appointment_type


def dedupe_hash(first: str, last: str, fips: str) -> str:
    """Compute a 20-character hex fingerprint used to deduplicate officials.

    Hash input is "{lowercase last}|{lowercase first}|{fips}". If the same person
    arrives from a second source for the same county, their hash will match an
    existing officials row and they will be linked rather than duplicated.

    Known limits: does not detect the same person across counties, after a legal
    name change, or when two different people share the same name in the same county.
    See design.md §1 for a full discussion of deduplication tradeoffs.

    Args:
        first: Parsed first name (from parse_name).
        last: Parsed last name (from parse_name).
        fips: 5-digit county FIPS code (from resolve_fips).

    Returns:
        20-character hex string (first 20 chars of SHA-256).
    """
    raw = f"{last.lower().strip()}|{first.lower().strip()}|{fips}"
    return hashlib.sha256(raw.encode()).hexdigest()[:20]


def score_confidence(first: str, last: str, phone: str, email: str) -> float:
    """Assign a confidence score based on data completeness only.

    Scores reflect whether the record has a parseable name and at least one contact
    field. They do NOT account for source freshness, membership lag, or cross-
    verification against a second source. The ceiling of 0.85 reflects the Tier 2
    reliability of association membership directories vs. a primary government source.

    Args:
        first: Parsed first name (empty string if missing).
        last: Parsed last name (empty string if missing).
        phone: Phone string from source (empty string if absent).
        email: Email string from source (empty string if absent).

    Returns:
        0.85 — full name present and at least one contact field present.
        0.72 — full name present but no contact info (all WSAC commissioner records).
        0.45 — name missing or unparseable; routes record to the review queue.
    """
    has_name    = bool(first.strip() and last.strip())
    has_contact = bool(phone.strip() or email.strip())
    if not has_name:
        return 0.45
    return 0.85 if has_contact else 0.72


# ── Validation ────────────────────────────────────────────────────────────────

def validate_record(
    rec: RawOfficial,
    rec_id: str,
    first: str,
    last: str,
    fips: Optional[str],
    office_type: Optional[str],
    score: float,
) -> list[Flag]:
    """Check a single record for data quality issues and return any flags.

    Args:
        rec: The original RawOfficial, used for context fields (county, title, contact).
        rec_id: UUID string to attach to any emitted flags for traceability.
        first: Parsed first name from parse_name().
        last: Parsed last name from parse_name().
        fips: Resolved FIPS code from resolve_fips(), or None if resolution failed.
        office_type: Normalized office type from parse_title(), or None if unrecognized.
        score: Confidence score from score_confidence().

    Returns:
        List of Flag objects, one per issue detected. An empty list means the record
        passed all checks. Possible flag types: MISSING_FIRST_NAME, MISSING_LAST_NAME,
        NO_CONTACT_INFO, UNRESOLVED_COUNTY, UNKNOWN_OFFICE_TYPE, LOW_CONFIDENCE.
    """
    flags: list[Flag] = []
    if not first.strip():
        flags.append(Flag(rec_id, "MISSING_FIRST_NAME", rec.raw_name))
    if not last.strip():
        flags.append(Flag(rec_id, "MISSING_LAST_NAME", rec.raw_name))
    if not rec.phone.strip() and not rec.email.strip():
        flags.append(Flag(rec_id, "NO_CONTACT_INFO",
                          f"{rec.raw_name} | {rec.local_title} | {rec.county_name}"))
    if fips is None:
        flags.append(Flag(rec_id, "UNRESOLVED_COUNTY", rec.county_name))
    if office_type is None:
        flags.append(Flag(rec_id, "UNKNOWN_OFFICE_TYPE",
                          f"{rec.local_title!r} from {rec.county_name}"))
    if score < 0.70:
        flags.append(Flag(rec_id, "LOW_CONFIDENCE",
                          f"score={score:.2f} | {rec.raw_name}"))
    return flags


# ── Build 5-table schema ──────────────────────────────────────────────────────

def build_tables(
    raw_records: list[RawOfficial],
    name_to_fips: dict[str, str],
    fips_to_name: dict[str, str],
    fetch_time: datetime,
) -> tuple[list, list, list, list, list, list[Flag]]:
    """Transform raw scraped records into the 5-table schema from design.md.

    Counties are seeded from the FIPS reference (not from scraped data) so all
    39 WA counties appear in the output even if a source omits one. A source_record
    is always written for every raw row — including those that fail validation —
    so the full raw payload is preserved for re-processing without re-fetching.

    Args:
        raw_records: Combined list of RawOfficial records from scrape_waco() and scrape_wsac().
        name_to_fips: Lowercase county name → FIPS lookup dict from fetch_wa_fips().
        fips_to_name: FIPS → proper county name lookup dict from fetch_wa_fips().
        fetch_time: UTC datetime recorded at run start; written to first_seen_at and
            last_verified_at on all new term records.

    Returns:
        Tuple of (counties, offices, officials, terms, source_records, flags) where
        each of the first five is a list of dicts ready for SQLite insertion, and
        flags is a list of Flag objects written to validation_flags.csv.
    """
    counties_rows = [
        {
            "county_fips":        fips,
            "state_fips":         STATE_FIPS,
            "state_name":         STATE_NAME,
            "state_abbreviation": STATE_ABBR,
            "county_name":        name,
            "county_type":        "County",
            "website_url":        None,
            "social_media_urls":  None,
            "population":         None,
        }
        for fips, name in sorted(fips_to_name.items())
    ]

    offices:   dict[tuple, dict] = {}   # (fips, office_type) → row
    officials: dict[str, dict]   = {}   # dedupe_hash → row
    terms:     list[dict]        = []
    srecs:     list[dict]        = []
    all_flags: list[Flag]        = []
    commissioner_counts: dict[str, int] = {}

    now_iso = fetch_time.isoformat()

    for raw in raw_records:
        rec_id      = str(uuid.uuid4())
        first, last = parse_name(raw.raw_name)
        fips        = resolve_fips(raw.county_name, name_to_fips)
        office_type, appointment_type = parse_title(raw.local_title)
        score       = score_confidence(first, last, raw.phone, raw.email)
        h           = dedupe_hash(first, last, fips or "") if (first or last) else ""

        # Vacant seats are not officials — flag and skip to source_record only
        if raw.raw_name.strip().lower() in ("vacant", "tbd", ""):
            all_flags.append(Flag(rec_id, "VACANT_SEAT",
                                  f"{raw.local_title} in {raw.county_name}"))
            srecs.append({
                "id": rec_id, "term_id": None,
                "source_name": raw.source_name, "source_type": "scrape",
                "source_url": raw.source_url, "reliability_tier": 2,
                "raw_data": json.dumps(raw.raw_row), "confidence_score": 0.0,
                "llm_extracted": 0, "fetched_at": now_iso,
            })
            continue

        all_flags.extend(validate_record(raw, rec_id, first, last, fips, office_type, score))

        term_id = None

        if fips and office_type and (first or last):
            # ── offices: one row per (county, office_type) pair ──
            key = (fips, office_type)
            if key not in offices:
                offices[key] = {
                    "id":          str(uuid.uuid4()),
                    "county_fips": fips,
                    "office_type": office_type,
                    "local_title": raw.local_title,
                    "category":    OFFICE_CATEGORY.get(office_type, "administrative"),
                    "is_board":    1 if office_type in BOARD_OFFICES else 0,
                    "seats":       1,   # updated for board offices after full pass
                    "term_years":  TERM_YEARS,
                    "partisan":    1 if PARTISAN else 0,
                }
            if office_type == "Commissioner":
                commissioner_counts[fips] = commissioner_counts.get(fips, 0) + 1

            # ── officials: deduplicated by hash ──
            if h and h not in officials:
                officials[h] = {
                    "id":               str(uuid.uuid4()),
                    "first_name":       first,
                    "last_name":        last,
                    "party":            None,
                    "email":            raw.email or None,
                    "phone":            raw.phone or None,
                    "social_media_urls": None,
                    "dedupe_hash":      h,
                }

            if h and h in officials:
                term_id = str(uuid.uuid4())
                terms.append({
                    "id":               term_id,
                    "official_id":      officials[h]["id"],
                    "office_id":        offices[key]["id"],
                    "term_start":       None,
                    "term_end":         None,
                    "first_seen_at":    now_iso,
                    "last_verified_at": now_iso,
                    "is_current":       1,
                    "appointment_type": appointment_type,
                    "confidence_score": score,
                })

        # Always write a source_record, even for rows we couldn't place.
        # This preserves the raw payload for re-processing without re-fetching.
        srecs.append({
            "id":               rec_id,
            "term_id":          term_id,
            "source_name":      raw.source_name,
            "source_type":      "scrape",
            "source_url":       raw.source_url,
            "reliability_tier": 2,
            "raw_data":         json.dumps(raw.raw_row),
            "confidence_score": score,
            "llm_extracted":    0,
            "fetched_at":       now_iso,
        })

    # Patch seats for board offices now that all records have been counted
    for fips, count in commissioner_counts.items():
        key = (fips, "Commissioner")
        if key in offices:
            offices[key]["seats"] = count

    # Second-pass duplicate hash detection
    seen: dict[str, str] = {}
    for h, official in officials.items():
        if h in seen:
            all_flags.append(Flag(
                official["id"],
                "DUPLICATE_DEDUPE_HASH",
                f"{official['first_name']} {official['last_name']} collides with official {seen[h]}",
            ))
        else:
            seen[h] = official["id"]

    return counties_rows, list(offices.values()), list(officials.values()), terms, srecs, all_flags


# ── SQLite ────────────────────────────────────────────────────────────────────

SCHEMA = """
CREATE TABLE IF NOT EXISTS counties (
    county_fips         TEXT PRIMARY KEY,
    state_fips          TEXT NOT NULL,
    state_name          TEXT NOT NULL,
    state_abbreviation  TEXT NOT NULL,
    county_name         TEXT NOT NULL,
    county_type         TEXT NOT NULL,
    website_url         TEXT,
    social_media_urls   TEXT,
    population          INTEGER
);

CREATE TABLE IF NOT EXISTS offices (
    id           TEXT PRIMARY KEY,
    county_fips  TEXT NOT NULL REFERENCES counties(county_fips),
    office_type  TEXT NOT NULL,
    local_title  TEXT NOT NULL,
    category     TEXT,
    is_board     INTEGER NOT NULL DEFAULT 0,
    seats        INTEGER NOT NULL DEFAULT 1,
    term_years   INTEGER,
    partisan     INTEGER
);

CREATE TABLE IF NOT EXISTS officials (
    id                TEXT PRIMARY KEY,
    first_name        TEXT,
    last_name         TEXT,
    party             TEXT,
    email             TEXT,
    phone             TEXT,
    social_media_urls TEXT,
    dedupe_hash       TEXT NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS terms (
    id                TEXT PRIMARY KEY,
    official_id       TEXT NOT NULL REFERENCES officials(id),
    office_id         TEXT NOT NULL REFERENCES offices(id),
    term_start        TEXT,
    term_end          TEXT,
    first_seen_at     TEXT NOT NULL,
    last_verified_at  TEXT NOT NULL,
    is_current        INTEGER NOT NULL DEFAULT 1,
    appointment_type  TEXT,
    confidence_score  REAL
);

CREATE TABLE IF NOT EXISTS source_records (
    id                TEXT PRIMARY KEY,
    term_id           TEXT REFERENCES terms(id),
    source_name       TEXT NOT NULL,
    source_type       TEXT NOT NULL,
    source_url        TEXT,
    reliability_tier  INTEGER,
    raw_data          TEXT,
    confidence_score  REAL,
    llm_extracted     INTEGER NOT NULL DEFAULT 0,
    fetched_at        TEXT NOT NULL
);
"""


def write_sqlite(
    db_path: Path,
    counties: list,
    offices:  list,
    officials: list,
    terms:    list,
    srecs:    list,
) -> None:
    """Create the SQLite database and insert all five tables.

    Deletes any existing file at db_path before writing so each run produces
    a clean snapshot rather than appending to prior data.

    Args:
        db_path: Destination path for the .db file. Parent directory is created
            if it does not exist.
        counties: List of county dicts from build_tables().
        offices: List of office dicts from build_tables().
        officials: List of official dicts from build_tables().
        terms: List of term dicts from build_tables().
        srecs: List of source_record dicts from build_tables().
    """
    log.info("Writing SQLite: %s", db_path)
    db_path.parent.mkdir(exist_ok=True)
    db_path.unlink(missing_ok=True)
    con = sqlite3.connect(db_path)
    con.executescript(SCHEMA)

    def insert(table: str, rows: list[dict]) -> None:
        if not rows:
            return
        cols = list(rows[0].keys())
        ph   = ",".join("?" * len(cols))
        con.executemany(
            f"INSERT OR IGNORE INTO {table} ({','.join(cols)}) VALUES ({ph})",
            [tuple(r[c] for c in cols) for r in rows],
        )

    insert("counties",       counties)
    insert("offices",        offices)
    insert("officials",      officials)
    insert("terms",          terms)
    insert("source_records", srecs)
    con.commit()
    con.close()
    log.info(
        "SQLite: %d counties | %d offices | %d officials | %d terms | %d source records",
        len(counties), len(offices), len(officials), len(terms), len(srecs),
    )


def write_flat_csv(db_path: Path, csv_path: Path) -> None:
    """Export a flat current-officials view to CSV.

    Queries the SQLite database and pre-joins all five tables into a single
    denormalized result. This lets analysts work directly with the CSV without
    needing to understand the normalized schema or write JOIN queries.
    Only includes records where is_current = 1.

    Args:
        db_path: Path to the SQLite database written by write_sqlite().
        csv_path: Destination path for the output CSV file.
    """
    con = sqlite3.connect(db_path)
    df = pd.read_sql_query(
        """
        SELECT
            c.county_fips,
            c.county_name,
            c.state_abbreviation,
            of_.office_type,
            of_.local_title,
            of_.category,
            of_.seats,
            of_.term_years,
            o.first_name,
            o.last_name,
            o.party,
            o.email,
            o.phone,
            t.appointment_type,
            t.term_start,
            t.term_end,
            t.first_seen_at,
            t.last_verified_at,
            t.confidence_score
        FROM terms t
        JOIN officials o   ON o.id = t.official_id
        JOIN offices  of_  ON of_.id = t.office_id
        JOIN counties c    ON c.county_fips = of_.county_fips
        WHERE t.is_current = 1
        ORDER BY c.county_name, of_.category, of_.office_type, o.last_name
        """,
        con,
    )
    con.close()
    df.to_csv(csv_path, index=False)
    log.info("Flat CSV: %d rows → %s", len(df), csv_path)


def write_flags_csv(flags: list[Flag], path: Path) -> None:
    """Write validation flags to a CSV file with columns: record_id, flag_type, detail.

    Separating flags into their own file means data quality issues are visible
    without querying the database, and a downstream reviewer can work through
    them independently of the main dataset.

    Args:
        flags: List of Flag objects from build_tables().
        path: Destination path for the output CSV. Parent directory is created
            if it does not exist.
    """
    path.parent.mkdir(exist_ok=True)
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["record_id", "flag_type", "detail"])
        w.writerows([[fl.record_id, fl.flag_type, fl.detail] for fl in flags])
    log.info("Validation flags: %d → %s", len(flags), path)


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    """Orchestrate the full collection pipeline and write all output files."""
    fetch_time = datetime.now(timezone.utc)

    # Step 1: Pull the Census FIPS reference to map county names → 5-digit FIPS codes.
    # These codes are the geographic primary key used throughout the schema.
    name_to_fips, fips_to_name = fetch_wa_fips()

    # Step 2: Scrape both source directories and combine into a single list of raw records.
    # WACO covers 7 non-commissioner offices; WSAC covers commissioners and council members.
    raw = scrape_waco() + scrape_wsac()

    # Step 3: Transform raw records into the 5-table schema — normalize titles, parse names,
    # resolve FIPS, deduplicate officials, and emit validation flags for suspicious records.
    counties, offices, officials, terms, srecs, flags = build_tables(
        raw, name_to_fips, fips_to_name, fetch_time
    )

    db_path    = DATA_DIR / "wa_officials.db"
    csv_path   = DATA_DIR / "wa_officials.csv"
    flags_path = DATA_DIR / "validation_flags.csv"

    # Step 4: Write all five tables to SQLite.
    write_sqlite(db_path, counties, offices, officials, terms, srecs)
    # Step 5: Export a flat analyst-friendly CSV (pre-joined view of all five tables).
    write_flat_csv(db_path, csv_path)
    # Step 6: Write validation flags so data quality issues are visible without querying the DB.
    write_flags_csv(flags, flags_path)

    flag_counts = Counter(f.flag_type for f in flags)

    print(f"\n{'─' * 52}")
    print(f"  Counties         {len(counties):>4}")
    print(f"  Offices          {len(offices):>4}")
    print(f"  Officials        {len(officials):>4}")
    print(f"  Terms            {len(terms):>4}")
    print(f"  Source records   {len(srecs):>4}")
    print(f"  Validation flags {len(flags):>4}")
    for ftype, count in flag_counts.most_common():
        print(f"    {ftype:<32} {count:>3}")
    print(f"{'─' * 52}")
    print(f"  Output:")
    print(f"    {db_path}")
    print(f"    {csv_path}")
    print(f"    {flags_path}")


if __name__ == "__main__":
    main()
