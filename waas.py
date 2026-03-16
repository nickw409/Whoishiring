#!/usr/bin/env python3
"""Work at a Startup (workatastartup.com) job scraper and filter."""

import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

WAAS_JOBS_URL = "https://www.workatastartup.com/jobs?role=eng"
WAAS_BASE_URL = "https://www.workatastartup.com"
WAAS_AUTH_URL = "https://account.ycombinator.com/authenticate?continue=https%3A%2F%2Fwww.workatastartup.com%2Fjobs%3Frole%3Deng"
WAAS_SEEN_FILE = Path(__file__).parent / "seen_waas.json"
WAAS_PRUNE_DAYS = 180
WAAS_UNAUTH_LIMIT = 30  # max jobs visible without login

# ---------------------------------------------------------------------------
# DOM selectors (discovered from live page inspection)
#
# Job listing page structure:
#   div.bg-beige-lighter          — individual job card
#   div.company-details           — company info container
#     span.font-bold              — company name + YC batch e.g. "Mason (W16)"
#     span.text-gray-600          — company one-liner description
#   a[target="company"]           — link to /companies/<slug>
#   div.job-name > a              — job title link, href=/jobs/<id>, data-jobid=<id>
#   p.job-details > span          — detail chips: fulltime, location, role type
# ---------------------------------------------------------------------------

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
# Browser helpers
# ---------------------------------------------------------------------------

def _create_browser():
    """Create a Playwright browser instance. Caller must close both."""
    from playwright.sync_api import sync_playwright
    playwright = sync_playwright().start()
    browser = playwright.chromium.launch(headless=True)
    return playwright, browser


def _waas_login(page) -> bool:
    """Attempt to log in to WAAS via YC auth. Returns True if successful.

    Requires WAAS_USERNAME and WAAS_PASSWORD env vars. If not set, skips
    login and returns False (unauthenticated scraping, limited to ~30 jobs).
    """
    username = os.environ.get("WAAS_USERNAME", "")
    password = os.environ.get("WAAS_PASSWORD", "")

    if not username or not password:
        return False

    logger.info("Attempting WAAS login...")
    page.goto(WAAS_AUTH_URL, timeout=30000)
    page.wait_for_selector('input[name="username"]', timeout=10000)

    page.fill('input[name="username"]', username)
    page.fill('input[name="password"]', password)
    page.click('button:has-text("Log in")')

    # Wait for navigation — either redirect to jobs page or stay on auth
    page.wait_for_timeout(3000)
    current_url = page.url

    if "account.ycombinator.com" in current_url:
        logger.warning("WAAS login failed — still on auth page. Check WAAS_USERNAME/WAAS_PASSWORD.")
        return False

    logger.info("WAAS login successful")
    return True


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

    Args:
        ignore_seen: If True, return all jobs without filtering by seen_waas.json
                     and do NOT update seen_waas.json.

    Returns list of dicts with keys: company_name, company_url, company_description,
    company_size, company_yc_batch, waas_company_url, job_title, job_url,
    job_salary_range, job_location, job_tags, job_details, job_description, remote.
    """
    # Playwright sync API can't run inside asyncio (e.g. MCP server).
    # Shell out to ourselves as a subprocess in that case.
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
    """Scrape directly using Playwright sync API (not inside asyncio)."""
    seen = load_waas_seen()
    prune_waas_seen(seen)

    playwright = None
    browser = None
    jobs = []

    try:
        playwright, browser = _create_browser()
        page = browser.new_page()

        # Authenticate if credentials are available
        authenticated = _waas_login(page)

        # Navigate to jobs page (login may have already redirected there)
        if WAAS_JOBS_URL not in page.url:
            page.goto(WAAS_JOBS_URL, timeout=30000)
        page.wait_for_selector("div.job-name", timeout=15000)

        # Scroll to load all results
        for _ in range(50):
            prev_height = page.evaluate("document.body.scrollHeight")
            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            page.wait_for_timeout(1500)
            new_height = page.evaluate("document.body.scrollHeight")
            if new_height == prev_height:
                break

        html = page.content()

        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html, "html.parser")

        cards = soup.select("div.bg-beige-lighter")
        for card in cards:
            try:
                # Company info
                company_span = card.find("span", class_="font-bold")
                company_name_raw = company_span.text.strip() if company_span else ""

                # Extract YC batch from name like "Mason (W16)"
                batch_match = re.search(r"\(([A-Z]\d{2})\)$", company_name_raw)
                company_yc_batch = batch_match.group(1) if batch_match else ""
                company_name = re.sub(r"\s*\([A-Z]\d{2}\)$", "", company_name_raw).strip()

                company_desc_span = card.find("span", class_="text-gray-600")
                company_description = company_desc_span.text.strip() if company_desc_span else ""

                company_link = card.find("a", attrs={"target": "company"})
                waas_company_url = ""
                if company_link and company_link.get("href"):
                    href = company_link["href"]
                    waas_company_url = href if href.startswith("http") else WAAS_BASE_URL + href

                # Job info
                job_div = card.find("div", class_="job-name")
                if not job_div:
                    continue
                job_link = job_div.find("a")
                if not job_link:
                    continue

                job_title = job_link.text.strip()
                job_href = job_link.get("href", "")
                job_url = job_href if job_href.startswith("http") else WAAS_BASE_URL + job_href

                # Job details from spans
                details_p = card.find("p", class_="job-details")
                detail_spans = details_p.find_all("span") if details_p else []
                detail_texts = [s.text.strip() for s in detail_spans if s.text.strip()]

                job_details = " | ".join(detail_texts)
                job_tags = detail_texts

                # Location: typically second span in details (first is fulltime/parttime)
                job_location = ""
                if len(detail_texts) >= 2:
                    job_location = detail_texts[1]

                # Remote detection
                remote = False
                for text in [job_details, job_location, job_title]:
                    if re.search(r"\bremote\b", text, re.IGNORECASE):
                        remote = True
                        break

                jobs.append({
                    "company_name": company_name,
                    "company_url": "",
                    "company_description": company_description,
                    "company_size": "",
                    "company_yc_batch": company_yc_batch,
                    "waas_company_url": waas_company_url,
                    "job_title": job_title,
                    "job_url": job_url,
                    "job_salary_range": "",
                    "job_location": job_location,
                    "job_tags": job_tags,
                    "job_details": job_details,
                    "job_description": company_description,
                    "remote": remote,
                })
            except Exception:
                logger.warning("Failed to parse a job card, skipping", exc_info=True)
                continue

        if not authenticated and len(jobs) >= WAAS_UNAUTH_LIMIT:
            logger.warning(
                "Only %d jobs found (unauthenticated limit). "
                "Set WAAS_USERNAME and WAAS_PASSWORD env vars for full results.",
                len(jobs),
            )

        # Fetch full descriptions from individual job pages
        logger.info("Fetching descriptions for %d jobs...", len(jobs))
        for job in jobs:
            try:
                page.goto(job["job_url"], timeout=15000)
                page.wait_for_selector("div.prose", timeout=5000)
                job_html = page.content()
                job_soup = BeautifulSoup(job_html, "html.parser")

                # Prose divs: 0=company desc, 1=job desc+skills, 2=tech stack
                prose_divs = job_soup.select("div.prose")
                if len(prose_divs) >= 2:
                    job["job_description"] = prose_divs[1].get_text(strip=True)
                elif prose_divs:
                    job["job_description"] = prose_divs[0].get_text(strip=True)

                # Pick up richer location/details from job page
                detail_div = job_soup.select_one("div.my-2.flex.flex-wrap")
                if detail_div:
                    spans = detail_div.find_all("span")
                    detail_texts = [s.get_text(strip=True) for s in spans if s.get_text(strip=True)]
                    if detail_texts:
                        job["job_tags"] = detail_texts
                        job["job_details"] = " | ".join(detail_texts)

                page.wait_for_timeout(500)  # politeness delay
            except Exception:
                logger.debug("Failed to fetch description for %s", job["job_url"])
                continue

    except Exception as e:
        logger.warning("WAAS scraping failed: %s", e, exc_info=True)
        return []
    finally:
        if browser is not None:
            browser.close()
        if playwright is not None:
            playwright.stop()

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

        # Build text for matching
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

        # Scoring
        try:
            score = hn_jobs.score_matches(matches)
        except Exception:
            logger.warning("score_matches failed for %s", job.get("job_url", "?"), exc_info=True)
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
