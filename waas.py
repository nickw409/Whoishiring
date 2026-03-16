#!/usr/bin/env python3
"""Work at a Startup (workatastartup.com) job scraper and filter."""

import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Any

import requests as http_requests

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

WAAS_BASE_URL = "https://www.workatastartup.com"
WAAS_AUTH_URL = "https://account.ycombinator.com/authenticate?continue=https%3A%2F%2Fwww.workatastartup.com%2Fcompanies"
WAAS_COMPANIES_URL = "https://www.workatastartup.com/companies?demographic=any&hasEquity=any&hasSalary=any&industry=any&interviewProcess=any&jobType=any&layout=list-compact&sortBy=keyword&tab=any&usVisaNotRequired=any"
WAAS_FETCH_URL = "https://www.workatastartup.com/companies/fetch"
WAAS_ALGOLIA_URL = "https://45bwzj1sgc-dsn.algolia.net/1/indexes/*/queries"
WAAS_ALGOLIA_INDEX = "WaaSPublicCompanyJob_production"
WAAS_SEEN_FILE = Path(__file__).parent / "seen_waas.json"
WAAS_PRUNE_DAYS = 180
WAAS_FETCH_BATCH_SIZE = 10

# Algolia filter defaults. Set to None to disable a filter.
# Override via config.yaml under "waas" key.
WAAS_DEFAULT_FILTERS = {
    "role": "eng",
    "eng_type": None,        # fs, be, ml, fe, eng_mgmt, devops, embedded, etc.
    "remote": None,           # yes, only, no
    "job_type": "fulltime",
    "min_experience": None,   # 0, 1, 3, 6, 11
    "us_visa_required": None, # yes, none, possible
    "has_salary": None,       # true, false
    "company_waas_stage": None,  # seed, series_a, growth, scale
}


def _load_waas_filters() -> dict:
    """Load WAAS filters from config.yaml, falling back to defaults."""
    config_file = Path(__file__).parent / "config.yaml"
    filters = dict(WAAS_DEFAULT_FILTERS)

    if config_file.exists():
        try:
            import yaml
            with open(config_file) as f:
                config = yaml.safe_load(f) or {}
            waas_config = config.get("waas", {})
            if isinstance(waas_config, dict):
                for key in WAAS_DEFAULT_FILTERS:
                    if key in waas_config:
                        val = waas_config[key]
                        if val is None or str(val).lower() == "any" or str(val).strip() == "":
                            filters[key] = None
                        else:
                            filters[key] = str(val)
        except Exception:
            logger.debug("Could not load waas filters from config.yaml")

    return filters


def _build_algolia_filter_string(filters: dict) -> str:
    """Build an Algolia filter string from the filters dict."""
    parts = []
    for key, value in filters.items():
        if not value or value == "any":
            continue
        parts.append(f"({key}:{value})")

    return " AND ".join(parts)

# ---------------------------------------------------------------------------
# Deduplication
# ---------------------------------------------------------------------------

def load_waas_seen() -> dict[str, dict[str, float]]:
    """Load seen WAAS job URLs from disk."""
    if WAAS_SEEN_FILE.exists():
        try:
            data = json.loads(WAAS_SEEN_FILE.read_text())
            if "jobs" not in data:
                data = {"jobs": {}}
            return data
        except (json.JSONDecodeError, KeyError):
            logger.warning("Corrupt seen_waas.json, starting fresh")
            return {"jobs": {}}
    return {"jobs": {}}


def save_waas_seen(seen: dict[str, dict[str, float]]) -> None:
    """Save seen WAAS job URLs to disk."""
    WAAS_SEEN_FILE.write_text(json.dumps(seen, indent=2))


def mark_waas_seen(seen: dict[str, dict[str, float]], job_urls: list[str]) -> dict[str, dict[str, float]]:
    """Mark job URLs as seen with current timestamp."""
    now = time.time()
    for url in job_urls:
        seen["jobs"][url] = now
    return seen


def prune_waas_seen(seen: dict[str, dict[str, float]]) -> dict[str, dict[str, float]]:
    """Remove entries older than WAAS_PRUNE_DAYS."""
    cutoff = time.time() - (WAAS_PRUNE_DAYS * 86400)
    seen["jobs"] = {
        url: ts for url, ts in seen["jobs"].items()
        if isinstance(ts, (int, float)) and ts > cutoff
    }
    return seen


# ---------------------------------------------------------------------------
# Auth + API helpers
# ---------------------------------------------------------------------------

def _scrape_via_api() -> tuple[list[dict], str]:
    """Log in, fetch all company IDs via Algolia, fetch company data via page.evaluate.

    Returns (companies_data, algolia_key). Companies_data is a list of company dicts.
    Uses Playwright for both auth and API calls to maintain session integrity.
    """
    username = os.environ.get("WAAS_USERNAME", "")
    password = os.environ.get("WAAS_PASSWORD", "")

    if not username or not password:
        logger.warning(
            "WAAS_USERNAME/WAAS_PASSWORD not set. "
            "Set them in .env for full job listings."
        )
        return [], ""

    from playwright.sync_api import sync_playwright

    playwright = None
    browser = None
    try:
        playwright = sync_playwright().start()
        browser = playwright.chromium.launch(headless=True)
        context = browser.new_context(viewport={"width": 1920, "height": 1080})
        page = context.new_page()

        # Step 1: Login
        logger.info("Attempting WAAS login...")
        page.goto(WAAS_AUTH_URL, timeout=30000)
        page.wait_for_selector('input[name="username"]', timeout=10000)
        page.fill('input[name="username"]', username)
        page.fill('input[name="password"]', password)
        page.click('button:has-text("Log in")')
        page.wait_for_timeout(5000)

        if "account.ycombinator.com" in page.url:
            logger.warning("WAAS login failed — still on auth page. Check credentials.")
            return [], ""

        logger.info("WAAS login successful")

        # Step 2: Navigate to companies page to get Algolia key + CSRF
        page.goto(WAAS_COMPANIES_URL, timeout=30000)
        page.wait_for_timeout(3000)

        # Extract Algolia key from page
        algolia_key = page.evaluate(
            "() => window.AlgoliaOpts ? window.AlgoliaOpts.key : ''"
        )
        if not algolia_key:
            logger.warning("Could not extract Algolia key from page")
            return [], ""

        # Step 3: Build filters and fetch all company IDs from Algolia
        filters = _load_waas_filters()
        filter_str = _build_algolia_filter_string(filters)
        logger.info("Fetching company IDs from Algolia (filters: %s)...", filter_str or "none")

        company_ids = page.evaluate("""
            async (args) => {
                const [algoliaKey, filterStr] = args;
                const ids = new Set();
                let currentPage = 0;
                let nbPages = 1;

                while (currentPage < nbPages) {
                    const params = `query=&page=${currentPage}&filters=${encodeURIComponent(filterStr)}&attributesToRetrieve=%5B%22company_id%22%5D&attributesToHighlight=%5B%5D&attributesToSnippet=%5B%5D&hitsPerPage=100`;
                    const resp = await fetch('https://45bwzj1sgc-dsn.algolia.net/1/indexes/*/queries', {
                        method: 'POST',
                        headers: {
                            'X-Algolia-Application-Id': '45BWZJ1SGC',
                            'X-Algolia-API-Key': algoliaKey,
                            'Content-Type': 'application/json',
                        },
                        body: JSON.stringify({
                            requests: [{
                                indexName: 'WaaSPublicCompanyJob_production',
                                params: params
                            }]
                        })
                    });
                    const data = await resp.json();
                    const result = data.results[0];
                    nbPages = result.nbPages;
                    for (const hit of result.hits) {
                        if (hit.company_id) ids.add(hit.company_id);
                    }
                    currentPage++;
                }
                return Array.from(ids);
            }
        """, [algolia_key, filter_str])

        logger.info("Found %d unique companies in Algolia", len(company_ids))

        # Step 4: Fetch company data in batches via browser fetch (preserves session/CSRF)
        all_companies = []
        batch_size = WAAS_FETCH_BATCH_SIZE
        for i in range(0, len(company_ids), batch_size):
            batch = company_ids[i : i + batch_size]
            try:
                result = page.evaluate("""
                    async (ids) => {
                        const csrfMeta = document.querySelector('meta[name="csrf-token"]');
                        const csrf = csrfMeta ? csrfMeta.content : '';
                        const resp = await fetch('/companies/fetch', {
                            method: 'POST',
                            headers: {
                                'Content-Type': 'application/json',
                                'Accept': 'application/json',
                                'X-Requested-With': 'XMLHttpRequest',
                                'X-CSRF-Token': csrf,
                            },
                            body: JSON.stringify({ids: ids}),
                        });
                        if (!resp.ok) return {error: resp.status};
                        return await resp.json();
                    }
                """, batch)

                if isinstance(result, dict) and "error" in result:
                    logger.warning("companies/fetch returned %s for batch at %d", result["error"], i)
                    continue

                companies = result.get("companies", result) if isinstance(result, dict) else result
                if isinstance(companies, list):
                    all_companies.extend(companies)

                time.sleep(0.3)
            except Exception:
                logger.warning("Failed to fetch company batch at offset %d", i, exc_info=True)
                continue

        logger.info("Fetched data for %d companies", len(all_companies))
        return all_companies, algolia_key

    except Exception as e:
        logger.warning("WAAS API scrape failed: %s", e, exc_info=True)
        return [], ""
    finally:
        if browser:
            browser.close()
        if playwright:
            playwright.stop()


def _company_to_jobs(company: dict) -> list[dict[str, Any]]:
    """Convert a company API response to a list of flat job dicts."""
    jobs = []
    company_name = company.get("name", "")
    company_url = company.get("website", company.get("website_url", ""))
    company_description = company.get("description", company.get("one_liner", ""))
    team_size = company.get("team_size")
    company_size = f"{team_size} people" if team_size else ""
    company_yc_batch = company.get("batch", "")
    slug = company.get("slug", "")
    waas_company_url = f"{WAAS_BASE_URL}/companies/{slug}" if slug else ""

    for job in company.get("jobs", []):
        if job.get("state") != "visible":
            continue

        job_url = job.get("show_path", "")
        if job_url and not job_url.startswith("http"):
            job_url = WAAS_BASE_URL + job_url

        remote_val = job.get("remote", "")
        is_remote = remote_val in ("only", "yes", True) or bool(
            re.search(r"\bremote\b", job.get("pretty_location_or_remote", ""), re.IGNORECASE)
        )

        location = job.get("pretty_location_or_remote", "")
        salary = job.get("pretty_salary_range", "")
        skills = [s.get("name", "") for s in job.get("skills", []) if s.get("name")]

        job_desc = job.get("description", "")
        job_type = job.get("pretty_job_type", "")
        eng_type = job.get("pretty_eng_type", "")
        experience = job.get("pretty_min_experience", "")
        visa = job.get("pretty_sponsors_visa", "")

        detail_parts = [p for p in [job_type, location, eng_type, experience, visa] if p]

        jobs.append({
            "company_name": company_name,
            "company_url": company_url,
            "company_description": company_description,
            "company_size": company_size,
            "company_yc_batch": company_yc_batch,
            "waas_company_url": waas_company_url,
            "job_title": job.get("title", ""),
            "job_url": job_url,
            "job_salary_range": salary,
            "job_location": location,
            "job_tags": skills,
            "job_details": " | ".join(detail_parts),
            "job_description": job_desc,
            "remote": is_remote,
        })

    return jobs


# ---------------------------------------------------------------------------
# Scraper
# ---------------------------------------------------------------------------

def _is_in_asyncio_loop() -> bool:
    """Check if we're inside a running asyncio event loop."""
    import asyncio
    try:
        asyncio.get_running_loop()
        return True
    except RuntimeError:
        return False


def scrape_waas_jobs(ignore_seen: bool = False) -> list[dict[str, Any]]:
    """Scrape engineering jobs from workatastartup.com.

    Uses the WAAS Algolia API + /companies/fetch endpoint for full results.
    Requires WAAS_USERNAME and WAAS_PASSWORD env vars for authentication.

    Args:
        ignore_seen: If True, return all jobs without filtering by seen_waas.json
                     and do NOT update seen_waas.json.

    Returns list of dicts with keys: company_name, company_url, company_description,
    company_size, company_yc_batch, waas_company_url, job_title, job_url,
    job_salary_range, job_location, job_tags, job_details, job_description, remote.
    """
    if _is_in_asyncio_loop():
        return _scrape_via_subprocess(ignore_seen)

    return _scrape_direct(ignore_seen)


def _scrape_via_subprocess(ignore_seen: bool) -> list[dict[str, Any]]:
    """Run the scraper in a subprocess to avoid asyncio conflicts."""
    import subprocess
    import sys

    args = [sys.executable, __file__]
    if ignore_seen:
        args.append("--ignore-seen")

    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=300)
    except subprocess.TimeoutExpired:
        logger.warning("WAAS subprocess timed out")
        return []

    if result.returncode != 0:
        logger.warning("WAAS subprocess failed: %s", result.stderr)
        return []

    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        logger.warning("WAAS subprocess returned invalid JSON: %s", result.stdout[:200])
        return []


def _scrape_direct(ignore_seen: bool) -> list[dict[str, Any]]:
    """Scrape via API: login -> Algolia company IDs -> /companies/fetch."""
    seen = load_waas_seen()
    prune_waas_seen(seen)

    companies, _ = _scrape_via_api()
    if not companies:
        return []

    # Flatten to job dicts
    jobs = []
    for company in companies:
        try:
            jobs.extend(_company_to_jobs(company))
        except Exception:
            logger.warning("Failed to parse company %s", company.get("name", "?"), exc_info=True)
            continue

    logger.info("Total jobs extracted: %d", len(jobs))

    # Dedup
    if not ignore_seen:
        jobs = [j for j in jobs if j["job_url"] not in seen["jobs"]]
        new_urls = [j["job_url"] for j in jobs]
        seen = mark_waas_seen(seen, new_urls)
        save_waas_seen(seen)

    return jobs


# ---------------------------------------------------------------------------
# Filtering (reuses hn_jobs.py logic)
# ---------------------------------------------------------------------------

import hn_jobs


def _waas_to_parsed(job: dict) -> dict:
    """Convert a raw WAAS job dict to the parsed format used by HN results.

    Required fields: company_name, job_title, job_description, job_url, job_location
    """
    desc = job["job_description"]
    return {
        "id": job["job_url"],
        "time": 0,
        "company": job["company_name"],
        "location": job["job_location"],
        "remote": job.get("remote", False),
        "snippet": desc[:300] + ("..." if len(desc) > 300 else ""),
        "full_text": desc,
        "emails": [],
        "email_instructions": [],
        "job_board_urls": [{"url": job["job_url"], "type": "waas", "title": job["job_title"]}],
        "other_urls": [job["company_url"]] if job.get("company_url") else [],
        "source": "waas",
        "company_yc_batch": job.get("company_yc_batch", ""),
        "company_size": job.get("company_size", ""),
        "salary_range": job.get("job_salary_range", ""),
    }


# Section markers for weighting keyword matches
_REQUIREMENTS_RE = re.compile(
    r"(requirements?|must.have|qualifications?|what you.ll bring|who we.re looking for|what you bring|you have|you.ll need)",
    re.IGNORECASE,
)
_NICE_TO_HAVE_RE = re.compile(
    r"(nice.to.have|bonus|preferred|ideally|plus|not required)",
    re.IGNORECASE,
)

# Weight multipliers for where a keyword appears
_LOCATION_WEIGHTS = {
    "title": 3.0,          # keyword in job title = strong signal
    "requirements": 2.0,   # keyword in requirements section
    "description": 1.0,    # keyword in general description body
    "nice_to_have": 0.5,   # keyword in nice-to-have section
    "tags": 0.3,           # keyword in auto-generated skills tags
}


def _find_section(text: str) -> list[tuple[str, str]]:
    """Split description into labeled sections for weighting."""
    sections = []
    lines = text.split("\n")
    current_label = "description"
    current_lines = []

    for line in lines:
        if _REQUIREMENTS_RE.search(line) and len(line) < 100:
            if current_lines:
                sections.append((current_label, "\n".join(current_lines)))
            current_label = "requirements"
            current_lines = [line]
        elif _NICE_TO_HAVE_RE.search(line) and len(line) < 100:
            if current_lines:
                sections.append((current_label, "\n".join(current_lines)))
            current_label = "nice_to_have"
            current_lines = [line]
        else:
            current_lines.append(line)

    if current_lines:
        sections.append((current_label, "\n".join(current_lines)))

    return sections


def _weighted_score(job: dict, matches: dict) -> float:
    """Score a WAAS job based on where keywords appear.

    Returns a float score. Higher = more relevant. The base category
    weight is multiplied by a location weight depending on where the
    keyword was found (title > requirements > description > nice-to-have > tags).
    """
    title = job.get("job_title", "")
    description = job.get("job_description", "")
    tags = job.get("job_tags") or []
    tags_text = " ".join(tags)

    sections = _find_section(description)

    total = 0.0
    for cat, matched_kws in matches.items():
        cat_weight = hn_jobs.KEYWORD_CATEGORIES[cat]["weight"]
        for kw in matched_kws:
            pat = hn_jobs._kw_patterns[kw]
            best_location_weight = 0.0

            # Check title
            if pat.search(title):
                best_location_weight = max(best_location_weight, _LOCATION_WEIGHTS["title"])

            # Check description sections
            for label, text in sections:
                if pat.search(text):
                    w = _LOCATION_WEIGHTS.get(label, _LOCATION_WEIGHTS["description"])
                    best_location_weight = max(best_location_weight, w)

            # Check tags (only if not found elsewhere with higher weight)
            if pat.search(tags_text) and best_location_weight < _LOCATION_WEIGHTS["tags"]:
                best_location_weight = _LOCATION_WEIGHTS["tags"]

            # If keyword wasn't found in any specific location, fall back
            if best_location_weight == 0:
                best_location_weight = _LOCATION_WEIGHTS["description"]

            total += cat_weight * best_location_weight

    return round(total, 1)


def filter_waas_jobs(raw_jobs: list[dict], hn_company_names: set[str] | None = None) -> tuple[list[dict], list[dict]]:
    """Filter WAAS jobs using HN pipeline logic.

    Args:
        raw_jobs: List of raw job dicts from scrape_waas_jobs().
        hn_company_names: Set of company names from HN results (lowercased, stripped)
                         for cross-source dedup. None means no dedup.

    Returns:
        (results, filtered_out) — each is a list of dicts with parsed/matches/score/source keys.
    """
    results = []
    filtered_out = []

    for job in raw_jobs:
        # Cross-source dedup
        if hn_company_names is not None:
            normalized = job.get("company_name", "").lower().strip()
            if normalized in hn_company_names:
                continue

        # Convert to parsed format
        try:
            parsed = _waas_to_parsed(job)
        except KeyError:
            logger.warning("Skipping WAAS job with missing required fields: %s", job.get("job_url", "?"))
            continue

        # Build text for matching (all sources combined for keyword detection)
        job_tags = job.get("job_tags") or []
        text_to_match = job["job_title"] + " " + job["job_description"] + " " + " ".join(job_tags)

        # Keyword matching
        try:
            matches = hn_jobs.match_keywords(text_to_match)
        except Exception:
            logger.warning("match_keywords failed for %s", job.get("job_url", "?"), exc_info=True)
            continue

        if not matches:
            continue

        # Negative matching
        try:
            neg_matches = hn_jobs.match_negative(text_to_match)
        except Exception:
            logger.warning("match_negative failed for %s", job.get("job_url", "?"), exc_info=True)
            continue

        # Weighted scoring (title > requirements > description > nice-to-have > tags)
        try:
            score = _weighted_score(job, matches)
        except Exception:
            logger.warning("_weighted_score failed for %s", job.get("job_url", "?"), exc_info=True)
            continue

        # Location filtering
        try:
            outside_us = hn_jobs.is_outside_us(parsed)
        except Exception:
            logger.warning("is_outside_us failed for %s", job.get("job_url", "?"), exc_info=True)
            continue

        item = {"parsed": parsed, "matches": matches, "score": score, "source": "waas"}

        if neg_matches:
            item["filter_reason"] = [f"negative keyword: {kw}" for kw in neg_matches]
            filtered_out.append(item)
        elif outside_us:
            item["filter_reason"] = [f"non-US location: {parsed['location']}"]
            filtered_out.append(item)
        else:
            results.append(item)

    return results, filtered_out


def scan_and_filter_waas(ignore_seen: bool = False, hn_company_names: set[str] | None = None) -> tuple[list[dict], list[dict]]:
    """Scrape and filter WAAS jobs in one call.

    Returns:
        (results, filtered_out) in the same format as filter_waas_jobs()
    """
    raw_jobs = scrape_waas_jobs(ignore_seen=ignore_seen)
    return filter_waas_jobs(raw_jobs, hn_company_names=hn_company_names)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys

    ignore_seen = "--ignore-seen" in sys.argv

    try:
        jobs = _scrape_direct(ignore_seen)
        print(json.dumps(jobs))
        print(f"{len(jobs)} jobs scraped", file=sys.stderr)
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
