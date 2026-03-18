#!/usr/bin/env python3
"""MCP server for HN Who's Hiring job scanner."""

import json
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path

# Add project dir to path so we can import from hn_jobs
sys.path.insert(0, str(Path(__file__).parent))

from mcp.server.fastmcp import FastMCP

import hn_jobs
import waas
from filters import SeenTracker, estimate_seniority

mcp = FastMCP(name="HN Who's Hiring")


def _active_filters() -> dict:
    """Return the active result filters from config for inclusion in scan responses."""
    config = hn_jobs.load_config()
    f = config.get("filters", {})
    active = {}
    if f.get("max_seniority"):
        active["max_seniority"] = f["max_seniority"]
    if f.get("coding_only"):
        active["coding_only"] = True
    return active


_PROJECT_DIR = Path(__file__).parent
TRACKED_JOBS_FILE = _PROJECT_DIR / "tracked_jobs.json"
BACKLOG_JOBS_FILE = _PROJECT_DIR / "backlog_jobs.json"
APPLIED_JOBS_FILE = _PROJECT_DIR / "applied_jobs.json"
DISMISSED_JOBS_FILE = _PROJECT_DIR / "dismissed_jobs.json"

DEFAULT_MAX_TRACKED = 20


def _load_json(path: Path) -> dict:
    """Load a JSON dict file. Returns empty dict if missing or corrupt."""
    if path.exists():
        try:
            return json.loads(path.read_text())
        except (json.JSONDecodeError, KeyError):
            return {}
    return {}


def _save_json(path: Path, data: dict) -> None:
    """Write a JSON dict file."""
    path.write_text(json.dumps(data, indent=2))


def _load_tracked() -> dict:
    return _load_json(TRACKED_JOBS_FILE)


def _save_tracked(data: dict) -> None:
    _save_json(TRACKED_JOBS_FILE, data)


def _load_backlog() -> dict:
    return _load_json(BACKLOG_JOBS_FILE)


def _save_backlog(data: dict) -> None:
    _save_json(BACKLOG_JOBS_FILE, data)


def _load_applied() -> dict:
    return _load_json(APPLIED_JOBS_FILE)


def _save_applied(data: dict) -> None:
    _save_json(APPLIED_JOBS_FILE, data)


def _load_dismissed() -> dict:
    return _load_json(DISMISSED_JOBS_FILE)


def _save_dismissed(data: dict) -> None:
    _save_json(DISMISSED_JOBS_FILE, data)


def _max_tracked() -> int:
    """Read max_tracked from config.yaml tracking section, default 20."""
    config_file = _PROJECT_DIR / "config.yaml"
    if config_file.exists():
        try:
            import yaml
            with open(config_file) as f:
                config = yaml.safe_load(f) or {}
            return int(config.get("tracking", {}).get("max_tracked", DEFAULT_MAX_TRACKED))
        except Exception:
            pass
    return DEFAULT_MAX_TRACKED


def _make_entry(r: dict) -> dict:
    """Build a tracked/backlog entry from a formatted WAAS result."""
    return {
        "company": r.get("company", ""),
        "yc_batch": r.get("company_yc_batch", ""),
        "company_size": r.get("company_size", ""),
        "job_title": r.get("job_title", ""),
        "seniority": r.get("seniority", "unknown"),
        "salary_range": r.get("salary_range", ""),
        "location": r.get("location", ""),
        "remote": r.get("remote", False),
        "other_roles": r.get("other_roles", []),
        "score": r.get("score", 0),
        "status": "open",
        "date_added": date.today().isoformat(),
        "date_applied": None,
        "analysis": None,
    }


def _backfill_tracked(tracked: dict, backlog: dict, max_n: int) -> int:
    """Promote top backlog jobs into tracked until tracked reaches max_n.

    Modifies both dicts in place. Returns number promoted.
    """
    slots = max_n - len(tracked)
    if slots <= 0 or not backlog:
        return 0

    ranked = sorted(backlog.items(), key=lambda kv: kv[1].get("score", 0), reverse=True)
    promoted = 0
    for url, entry in ranked[:slots]:
        tracked[url] = entry
        del backlog[url]
        promoted += 1
    return promoted


def _track_waas_results(formatted: list[dict]) -> dict:
    """Process new WAAS results into the tracking pipeline.

    1. Filter out jobs already in tracked/backlog/applied/dismissed
    2. Build entries for truly new jobs
    3. Combine new + existing backlog, rank by score
    4. Fill tracked up to max_tracked
    5. Remainder stays in backlog

    Returns summary dict with counts.
    """
    tracked = _load_tracked()
    backlog = _load_backlog()
    applied = _load_applied()
    dismissed = _load_dismissed()
    max_n = _max_tracked()

    all_known = set(tracked) | set(backlog) | set(applied) | set(dismissed)

    # Build entries for truly new jobs
    new_entries = {}
    for r in formatted:
        job_url = r.get("job_url", "")
        if not job_url or job_url in all_known:
            continue
        new_entries[job_url] = _make_entry(r)

    # Add new entries to backlog pool
    backlog.update(new_entries)

    # Fill tracked from backlog up to max_n
    promoted = _backfill_tracked(tracked, backlog, max_n)

    _save_tracked(tracked)
    _save_backlog(backlog)

    return {
        "new_jobs_found": len(new_entries),
        "newly_tracked": promoted,
        "backlog_size": len(backlog),
        "tracked_size": len(tracked),
    }


def _scan_hn(months: int, ignore_seen: bool) -> tuple[list[dict], list[dict], list[str]]:
    """Extract HN scanning logic for reuse in scan_jobs and scan_all.

    Returns:
        (results, filtered_out, thread_titles)
    """
    months = max(1, min(3, months))
    config = hn_jobs.load_config()
    config_filters = config.get("filters", {})

    tracker = None
    if not ignore_seen:
        tracker = SeenTracker(hn_jobs.SEEN_FILE, "posts")
        tracker.load()

    threads = hn_jobs.find_hiring_threads(max_threads=months)
    if not threads:
        return [], [], []

    thread_titles = [t.get("title", "Unknown") for t in threads]

    results, filtered_out, all_seen_ids = hn_jobs.process_threads(
        threads, tracker, config_filters,
    )

    if tracker and not ignore_seen:
        tracker.mark(all_seen_ids)
        tracker.prune()
        tracker.save()

    return results, filtered_out, thread_titles


def _format_hn_results(results: list[dict]) -> list[dict]:
    """Format HN results for JSON output with keyword-focused snippets."""
    output = []
    for item in results:
        p = item["parsed"]
        keywords = [kw for kws in item["matches"].values() for kw in kws]
        snippet = _build_keyword_snippet(p["full_text"], keywords)
        output.append({
            "company": p["company"],
            "role": p.get("role", ""),
            "location": p["location"],
            "remote": p["remote"],
            "seniority": p.get("seniority", "unknown"),
            "is_coding": p.get("is_coding", True),
            "score": item["score"],
            "matched_categories": list(item["matches"].keys()),
            "matched_keywords": keywords,
            "full_text": snippet,
            "emails": p["emails"],
            "job_board_urls": p["job_board_urls"],
            "other_urls": p["other_urls"],
            "hn_link": f"https://news.ycombinator.com/item?id={p['id']}",
            "source": "hn",
        })
    return output


DESC_LIMIT = 500
CONTEXT_WINDOW = 100  # chars before/after each keyword match


def _build_keyword_snippet(text: str, keywords: list[str], limit: int = DESC_LIMIT) -> str:
    """Build a snippet showing text around matched keywords.

    Finds each keyword in the text, extracts surrounding context,
    merges overlapping regions, and joins with ' ... '. Falls back
    to the first `limit` chars if no keywords are found in the text.
    """
    if len(text) <= limit:
        return text

    if not keywords:
        return text[:limit] + "..."

    # Find all keyword positions
    regions = []
    for kw in keywords:
        for m in re.finditer(r"\b" + re.escape(kw) + r"\b", text, re.IGNORECASE):
            start = max(0, m.start() - CONTEXT_WINDOW)
            end = min(len(text), m.end() + CONTEXT_WINDOW)
            regions.append((start, end))

    if not regions:
        return text[:limit] + "..."

    # Merge overlapping regions
    regions.sort()
    merged = [regions[0]]
    for start, end in regions[1:]:
        if start <= merged[-1][1] + 20:  # merge if gap < 20 chars
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))

    # Build snippet from merged regions up to limit
    parts = []
    total = 0
    for start, end in merged:
        # Extend to word boundaries
        while start > 0 and text[start - 1] not in " \n\t":
            start -= 1
        while end < len(text) and text[end] not in " \n\t":
            end += 1

        chunk = text[start:end].strip()
        if total + len(chunk) > limit:
            remaining = limit - total
            if remaining > 50:
                parts.append(chunk[:remaining] + "...")
            break
        parts.append(chunk)
        total += len(chunk)

    return " ... ".join(parts)


def _dedup_by_company(results: list[dict]) -> list[dict]:
    """Keep only the highest-scoring role per company. Adds other_roles_count."""
    company_groups: dict[str, list[dict]] = {}
    for r in results:
        key = r["company"].lower().strip()
        company_groups.setdefault(key, []).append(r)

    deduped = []
    for group in company_groups.values():
        group.sort(key=lambda x: x.get("score", 0), reverse=True)
        best = group[0]
        if len(group) > 1:
            best["other_roles_count"] = len(group) - 1
            best["other_roles"] = [
                {"job_title": r.get("job_title", ""), "job_url": r.get("job_url", "")}
                for r in group[1:]
            ]
        deduped.append(best)

    deduped.sort(key=lambda x: x.get("score", 0), reverse=True)
    return deduped


# Full results cache for get_job_details
_full_results_cache: list[dict] = []


def _format_waas_results(results: list[dict]) -> list[dict]:
    """Format WAAS results for JSON output with keyword-focused snippets."""
    global _full_results_cache
    output = []
    full_cache = []
    for item in results:
        p = item["parsed"]
        keywords = [kw for kws in item["matches"].values() for kw in kws]
        snippet = _build_keyword_snippet(p["full_text"], keywords)
        job_url = p["job_board_urls"][0]["url"] if p["job_board_urls"] else ""
        job_title = p["job_board_urls"][0]["title"] if p["job_board_urls"] else ""

        formatted = {
            "company": p["company"],
            "location": p["location"],
            "remote": p["remote"],
            "score": item["score"],
            "matched_categories": list(item["matches"].keys()),
            "matched_keywords": keywords,
            "full_text": snippet,
            "job_url": job_url,
            "job_title": job_title,
            "salary_range": p.get("salary_range", ""),
            "company_yc_batch": p.get("company_yc_batch", ""),
            "company_size": p.get("company_size", ""),
            "seniority": p.get("seniority") or estimate_seniority(job_title, p["full_text"]),
            "is_coding": p.get("is_coding", True),
            "source": "waas",
        }
        output.append(formatted)

        # Cache with full text for get_job_details
        full_cache.append({**formatted, "full_text": p["full_text"]})

    _full_results_cache = full_cache
    return output


@mcp.tool()
def scan_jobs(months: int = 1, ignore_seen: bool = False) -> str:
    """Scan Hacker News "Who is Hiring?" threads for matching job posts.

    Searches recent monthly threads, filters by keyword categories
    (AI tooling, Systems, General AI+SWE), removes non-US non-remote
    jobs and senior/management roles, scrapes job board links (Greenhouse,
    Lever, Ashby), and returns structured results sorted by keyword score.

    Each result includes: company, location, remote status, keyword score,
    matched categories/keywords, full post text, application links/emails,
    and a direct HN link.

    Args:
        months: Number of monthly threads to scan (1-3). Use 1 for latest, 3 for backfill.
        ignore_seen: If true, return all matching posts even if previously seen.
                     If false, skip already-seen posts and mark new ones as seen.
    """
    results, filtered_out, thread_titles = _scan_hn(months, ignore_seen)

    if not thread_titles:
        return json.dumps({"error": "No hiring threads found"})

    active = _active_filters()
    output = {
        "threads": thread_titles,
        "total_results": len(results),
        "total_filtered": len(filtered_out),
        "results": _format_hn_results(results),
    }
    if active:
        output["active_filters"] = active

    return json.dumps(output, indent=2)


WAAS_MAX_RESULTS = 1000


@mcp.tool()
def scan_waas(ignore_seen: bool = False, group_by_company: bool = True) -> str:
    """Scan Work at a Startup (workatastartup.com) for matching engineering jobs.

    Returns up to 1000 results sorted by keyword score, with keyword-context
    snippets (500 chars around matched terms). Use get_job_details to fetch
    the full description for specific jobs.

    By default, results are grouped by company — only the highest-scoring
    role per company is returned. Each result includes other_roles_count
    and other_roles (title + URL) for that company. Set group_by_company=false
    to see all roles.

    Each result includes a seniority estimate (intern, junior, mid, senior,
    staff+, or unknown) derived from the job title and experience requirements.

    Authenticates with YC (requires WAAS_USERNAME/WAAS_PASSWORD env vars).
    Pre-filters at the API level using Algolia (defaults: role=eng,
    job_type=fulltime; configurable via config.yaml under "waas" key or
    via update_config tool).

    Available config.yaml filters under "waas" (use update_config to change).
    Use comma-separated values for OR logic (e.g. "0,1" for 0 OR 1 years):
      role: eng|sales|operations|marketing|product|any (default: eng)
      eng_type: fs|be|ml|fe|eng_mgmt|devops|embedded|any (default: any)
      remote: yes|only|no|any (default: any)
      job_type: fulltime|intern|contract|cofounder|any (default: fulltime)
      min_experience: 0|1|3|6|11|any — comma-separated for multiple (default: any)
      us_visa_required: yes|none|possible|any (default: any)
      has_salary: true|false|any (default: any)
      company_waas_stage: seed|series_a|growth|scale|any (default: any)
    Set a filter to "any" to disable it. Omit it to use the default.

    Args:
        ignore_seen: If true, return all matching jobs even if previously seen.
        group_by_company: If true (default), show only the best role per company.
    """
    try:
        results, filtered_out = waas.scan_and_filter_waas(ignore_seen=ignore_seen)
    except Exception as e:
        return json.dumps({
            "source": "waas",
            "total_results": 0,
            "total_filtered": 0,
            "results": [],
            "error": str(e),
        }, indent=2)

    formatted = _format_waas_results(results)
    total_all_roles = len(formatted)

    if group_by_company:
        formatted = _dedup_by_company(formatted)

    formatted = formatted[:WAAS_MAX_RESULTS]

    # Track new jobs
    tracking_summary = _track_waas_results(formatted)

    return json.dumps({
        "status": "ok",
        "total_scanned": total_all_roles,
        "total_filtered": len(filtered_out),
        "tracking": tracking_summary,
    }, indent=2)


@mcp.tool()
def get_job_details(job_url: str) -> str:
    """Get the full untruncated description for a specific WAAS job.

    Returns the complete job description and all metadata without any
    truncation. Use this after scan_waas or scan_all to read the full
    details for jobs you're considering.

    Args:
        job_url: The WAAS job URL (e.g. https://www.workatastartup.com/jobs/12345).
    """
    for r in _full_results_cache:
        if r.get("job_url") == job_url:
            return json.dumps(r, indent=2)
    return json.dumps({"error": f"Job not found in cache: {job_url}. Run scan_waas or scan_all first."})


ALL_MAX_RESULTS = 1000


@mcp.tool()
def scan_all(ignore_seen: bool = False, months: int = 1, group_by_company: bool = True) -> str:
    """Scan both HN Who's Hiring and Work at a Startup, then combine results.

    Returns up to 1000 results sorted by keyword score. Runs both scans
    in parallel. By default, groups by company (best role per company).

    Cross-source deduplication: if a company appears on both HN and WAAS,
    only the HN listing is kept. Each result has a "source" field
    ("hn" or "waas"). WAAS results include: job_title, salary_range,
    company_yc_batch, company_size, and seniority estimate.

    Use get_job_details to fetch full descriptions for specific WAAS jobs.

    Args:
        ignore_seen: If true, return all jobs even if previously seen.
        months: Number of HN monthly threads to scan (1-3).
        group_by_company: If true (default), show only the best role per company.
    """
    errors = []

    with ThreadPoolExecutor(max_workers=2) as pool:
        hn_future = pool.submit(_scan_hn, months, ignore_seen)
        waas_future = pool.submit(waas.scrape_waas_jobs, ignore_seen=ignore_seen)

        try:
            hn_results, hn_filtered, thread_titles = hn_future.result()
        except Exception as e:
            hn_results, hn_filtered, thread_titles = [], [], []
            errors.append(f"HN scan failed: {e}")

        try:
            waas_raw = waas_future.result()
        except Exception as e:
            waas_raw = []
            errors.append(f"WAAS scrape failed: {e}")

    hn_company_names = set()
    for item in hn_results:
        company = item["parsed"].get("company")
        if company:
            hn_company_names.add(company.lower().strip())

    try:
        waas_results, waas_filtered = waas.filter_waas_jobs(waas_raw, hn_company_names=hn_company_names)
    except Exception as e:
        waas_results, waas_filtered = [], []
        errors.append(f"WAAS filter failed: {e}")

    combined = _format_hn_results(hn_results) + _format_waas_results(waas_results)
    combined.sort(key=lambda x: x.get("score", 0), reverse=True)

    total_all_roles = len(combined)

    if group_by_company:
        combined = _dedup_by_company(combined)

    combined = combined[:ALL_MAX_RESULTS]

    active = _active_filters()
    result = {
        "sources": ["hn", "waas"],
        "threads": thread_titles,
        "total_results": len(combined),
        "total_all_roles": total_all_roles,
        "total_filtered": len(hn_filtered) + len(waas_filtered),
        "hn_results": len(hn_results),
        "waas_results": len(waas_results),
        "waas_raw_scraped": len(waas_raw),
        "grouped_by_company": group_by_company,
        "results": combined,
    }
    if active:
        result["active_filters"] = active
    if errors:
        result["errors"] = errors

    return json.dumps(result, indent=2)


@mcp.tool()
def get_resume() -> str:
    """Get the user's resume text extracted from their configured PDF.

    Reads the resume path from config.yaml and extracts the text content.
    Use this to understand the user's background before ranking jobs.
    """
    config = hn_jobs.load_config()
    if not config["resume_text"]:
        return "No resume configured. Set 'resume' path in config.yaml."
    return config["resume_text"]


@mcp.tool()
def get_preferences() -> str:
    """Get the user's job preferences from config.yaml.

    Returns remote preference and free-form notes about what the user
    is looking for. Use alongside get_resume to rank jobs effectively.
    """
    config = hn_jobs.load_config()
    prefs = config["preferences"]
    if not prefs:
        return "No preferences configured. Set 'preferences' in config.yaml."
    return json.dumps(prefs, indent=2)


@mcp.tool()
def get_latest_results() -> str:
    """Get the most recent scan results from disk without running a new scan.

    Returns the latest results JSON file. Useful for re-ranking or
    reviewing previous results without waiting for a new scan.
    """
    results_dir = hn_jobs.RESULTS_DIR
    if not results_dir.exists():
        return json.dumps({"error": "No results directory found. Run scan_jobs first."})

    json_files = sorted(results_dir.glob("results_*.json"), reverse=True)
    if not json_files:
        return json.dumps({"error": "No results files found. Run scan_jobs first."})

    return json_files[0].read_text()


@mcp.tool()
def get_config() -> str:
    """Get the current config.yaml contents.

    Returns the full config including resume path, preferences, and
    WAAS filters. Use this to see current settings before updating.
    """
    config_file = Path(__file__).parent / "config.yaml"
    if not config_file.exists():
        return json.dumps({"error": "No config.yaml found. Use update_config to create one."})
    return config_file.read_text()


@mcp.tool()
def update_config(
    resume: str | None = None,
    remote_preference: str | None = None,
    preference_notes: str | None = None,
    max_seniority: str | None = None,
    coding_only: bool | None = None,
    waas_role: str | None = None,
    waas_eng_type: str | None = None,
    waas_remote: str | None = None,
    waas_job_type: str | None = None,
    waas_min_experience: str | None = None,
    waas_us_visa_required: str | None = None,
    waas_has_salary: str | None = None,
    waas_company_waas_stage: str | None = None,
) -> str:
    """Update config.yaml settings. Only provided fields are changed; others are preserved.

    Use this to adjust preferences, resume path, WAAS search filters, or result filters.
    To disable/clear a WAAS filter, pass "any" as the value (NOT null).
    Omit a field entirely to leave it unchanged.
    Use comma-separated values for OR logic (e.g. "0,1" for min_experience 0 OR 1).

    Args:
        resume: Path to PDF resume file for ranking.
        remote_preference: Remote work preference (e.g. "preferred", "required", "flexible").
        preference_notes: Free-form notes about what you're looking for, sent to the ranker.
        max_seniority: Maximum seniority level to include. Values: intern, junior, mid, senior, staff+. Jobs above this level are filtered out. Pass "any" to disable.
        coding_only: If true, filter out non-coding roles (product, design, sales, management, etc.).
        waas_role: WAAS role filter. Values: eng, sales, operations, marketing, product, or "any" to disable/clear.
        waas_eng_type: WAAS engineering type. Values: fs, be, ml, fe, eng_mgmt, devops, embedded, or "any". Comma-separated for multiple.
        waas_remote: WAAS remote filter. Values: yes, only, no, or "any" to disable/clear. Comma-separated for multiple.
        waas_job_type: WAAS job type. Values: fulltime, intern, contract, cofounder, or "any". Comma-separated for multiple.
        waas_min_experience: WAAS min experience (years). Values: 0, 1, 3, 6, 11, or "any". Comma-separated for multiple (e.g. "0,1" for 0-1 years).
        waas_us_visa_required: WAAS visa filter. Values: yes, none, possible, or "any" to disable/clear.
        waas_has_salary: WAAS salary listed filter. Values: true, false, or "any" to disable/clear.
        waas_company_waas_stage: WAAS company stage. Values: seed, series_a, growth, scale, or "any". Comma-separated for multiple.
    """
    import yaml

    config_file = Path(__file__).parent / "config.yaml"
    config = {}
    if config_file.exists():
        try:
            with open(config_file) as f:
                config = yaml.safe_load(f) or {}
        except Exception:
            config = {}

    if resume is not None:
        config["resume"] = resume

    if remote_preference is not None or preference_notes is not None:
        prefs = config.get("preferences", {}) or {}
        if remote_preference is not None:
            prefs["remote"] = remote_preference
        if preference_notes is not None:
            prefs["notes"] = preference_notes
        config["preferences"] = prefs

    # Result filters (seniority, coding_only)
    if max_seniority is not None or coding_only is not None:
        filters = config.get("filters", {}) or {}
        if max_seniority is not None:
            if max_seniority.lower() == "any":
                filters.pop("max_seniority", None)
            else:
                filters["max_seniority"] = max_seniority
        if coding_only is not None:
            filters["coding_only"] = coding_only
        config["filters"] = filters

    # WAAS filters
    waas_args = {
        "role": waas_role,
        "eng_type": waas_eng_type,
        "remote": waas_remote,
        "job_type": waas_job_type,
        "min_experience": waas_min_experience,
        "us_visa_required": waas_us_visa_required,
        "has_salary": waas_has_salary,
        "company_waas_stage": waas_company_waas_stage,
    }

    # Only touch the waas section if any waas_ arg was explicitly passed
    waas_updates = {k: v for k, v in waas_args.items() if v is not None}
    if waas_updates:
        waas_config = config.get("waas", {}) or {}
        for key, value in waas_updates.items():
            if value.lower() == "any":
                # "any" means clear/disable the filter
                waas_config.pop(key, None)
            else:
                waas_config[key] = value
        config["waas"] = waas_config

    with open(config_file, "w") as f:
        yaml.dump(config, f, default_flow_style=False, sort_keys=False)

    return json.dumps({
        "status": "updated",
        "config": config,
    }, indent=2, default=str)


# ---------------------------------------------------------------------------
# Job tracking tools
# ---------------------------------------------------------------------------

@mcp.tool()
def get_tracked_jobs() -> str:
    """Return all tracked jobs (the active analysis pipeline).

    tracked_jobs.json holds up to max_tracked (default 20) open jobs.
    Each entry is keyed by WAAS job URL and contains: company, yc_batch,
    company_size, job_title, seniority, salary_range, location, remote,
    other_roles, score, status, date_added, date_applied, and analysis
    (null until populated via update_job_analysis).

    Use this to find jobs needing analysis (analysis is null).
    """
    return json.dumps(_load_tracked(), indent=2)


@mcp.tool()
def get_applied_jobs() -> str:
    """Return all jobs that have been marked as applied.

    These were moved out of tracked_jobs.json when mark_applied was called.
    Each entry preserves the full job data including analysis.
    """
    return json.dumps(_load_applied(), indent=2)


@mcp.tool()
def get_dismissed_jobs() -> str:
    """Return all jobs that have been marked as dismissed.

    These were moved out of tracked_jobs.json when mark_dismissed was called.
    Each entry preserves the full job data including analysis.
    """
    return json.dumps(_load_dismissed(), indent=2)


@mcp.tool()
def update_job_analysis(job_url: str, fit_explanation: str, odds: str,
                        odds_reasoning: str, salary_vs_col: str) -> str:
    """Write an analysis for a tracked job. Does not overwrite existing analysis.

    Args:
        job_url: The WAAS job URL to update.
        fit_explanation: How the user's resume connects to this role.
        odds: Estimated chance of getting the job — "low", "medium", or "high".
        odds_reasoning: Why the odds are what they are.
        salary_vs_col: Salary compared to cost of living for the job's location.
    """
    tracked = _load_tracked()
    if job_url not in tracked:
        return json.dumps({"error": f"Job not tracked: {job_url}"})

    if tracked[job_url]["analysis"] is not None:
        return json.dumps({"error": "Analysis already exists. Will not overwrite."})

    if odds not in ("low", "medium", "high"):
        return json.dumps({"error": f"Invalid odds value: {odds}. Must be low, medium, or high."})

    tracked[job_url]["analysis"] = {
        "fit_explanation": fit_explanation,
        "odds": odds,
        "odds_reasoning": odds_reasoning,
        "salary_vs_col": salary_vs_col,
    }
    _save_tracked(tracked)
    return json.dumps({"status": "updated", "job_url": job_url})


@mcp.tool()
def mark_applied(job_url: str) -> str:
    """Move a tracked job to applied_jobs.json with status "applied".

    Sets date_applied to today. Backfills the freed tracked slot from
    the backlog (highest score first).

    Args:
        job_url: The WAAS job URL to mark as applied.
    """
    tracked = _load_tracked()
    if job_url not in tracked:
        return json.dumps({"error": f"Job not tracked: {job_url}"})

    entry = tracked.pop(job_url)
    entry["status"] = "applied"
    entry["date_applied"] = date.today().isoformat()

    applied = _load_applied()
    applied[job_url] = entry
    _save_applied(applied)

    # Backfill from backlog
    backlog = _load_backlog()
    promoted = _backfill_tracked(tracked, backlog, _max_tracked())
    _save_tracked(tracked)
    if promoted:
        _save_backlog(backlog)

    return json.dumps({"status": "applied", "job_url": job_url, "backfilled": promoted})


@mcp.tool()
def mark_dismissed(job_url: str) -> str:
    """Move a tracked job to dismissed_jobs.json with status "dismissed".

    Backfills the freed tracked slot from the backlog (highest score first).

    Args:
        job_url: The WAAS job URL to dismiss.
    """
    tracked = _load_tracked()
    if job_url not in tracked:
        return json.dumps({"error": f"Job not tracked: {job_url}"})

    entry = tracked.pop(job_url)
    entry["status"] = "dismissed"

    dismissed = _load_dismissed()
    dismissed[job_url] = entry
    _save_dismissed(dismissed)

    # Backfill from backlog
    backlog = _load_backlog()
    promoted = _backfill_tracked(tracked, backlog, _max_tracked())
    _save_tracked(tracked)
    if promoted:
        _save_backlog(backlog)

    return json.dumps({"status": "dismissed", "job_url": job_url, "backfilled": promoted})


@mcp.tool()
def mark_open(job_url: str) -> str:
    """Move a job from applied or dismissed back to tracked.

    If tracked is at capacity, the lowest-score tracked job is demoted
    to the backlog to make room.

    Args:
        job_url: The WAAS job URL to revert to open.
    """
    # Find the job in applied or dismissed
    applied = _load_applied()
    dismissed = _load_dismissed()
    entry = None
    source = None

    if job_url in applied:
        entry = applied.pop(job_url)
        source = "applied"
    elif job_url in dismissed:
        entry = dismissed.pop(job_url)
        source = "dismissed"

    if entry is None:
        return json.dumps({"error": f"Job not found in applied or dismissed: {job_url}"})

    entry["status"] = "open"
    entry["date_applied"] = None

    tracked = _load_tracked()
    max_n = _max_tracked()

    # If at capacity, demote lowest-score tracked job to backlog
    if len(tracked) >= max_n:
        backlog = _load_backlog()
        lowest_url = min(tracked, key=lambda u: tracked[u].get("score", 0))
        backlog[lowest_url] = tracked.pop(lowest_url)
        _save_backlog(backlog)

    tracked[job_url] = entry
    _save_tracked(tracked)

    if source == "applied":
        _save_applied(applied)
    else:
        _save_dismissed(dismissed)

    return json.dumps({"status": "open", "job_url": job_url})


@mcp.tool()
def validate_tracked_jobs() -> str:
    """Check all tracked jobs and remove any whose listings are no longer live.

    Hits each job's URL to verify the listing still exists. Removes dead
    listings from tracked_jobs.json entirely, then backfills from the backlog.

    Returns a summary with validated count, removed count, and removed URLs.
    Run this before scan_waas to clean out stale listings.
    """
    import requests

    tracked = _load_tracked()
    urls_to_check = list(tracked.keys())

    removed = []
    for url in urls_to_check:
        try:
            resp = requests.head(url, timeout=10, allow_redirects=True, headers={
                "User-Agent": "Mozilla/5.0 (compatible; HNJobScanner/1.0)"
            })
            if resp.status_code >= 400:
                removed.append(url)
        except requests.RequestException:
            removed.append(url)

    for url in removed:
        del tracked[url]

    # Backfill from backlog
    backlog = _load_backlog()
    promoted = _backfill_tracked(tracked, backlog, _max_tracked())

    _save_tracked(tracked)
    if removed or promoted:
        _save_backlog(backlog)

    return json.dumps({
        "validated": len(urls_to_check) - len(removed),
        "removed": len(removed),
        "removed_jobs": removed,
        "backfilled": promoted,
    }, indent=2)


@mcp.prompt()
def find_jobs() -> str:
    """Find and rank jobs from both HN and YC startups against my resume."""
    return (
        "Use get_resume and get_preferences to understand my background, "
        "then scan_all with ignore_seen=true to find matching positions from "
        "both HN Who's Hiring and Work at a Startup (YC's job board). "
        "Results are grouped by company (best role per company). "
        "Each result includes a seniority estimate — use it to filter out "
        "roles that don't match my experience level. "
        "For promising matches, use get_job_details with the job_url to "
        "read the full description before finalizing your ranking. "
        "Rank your top 15 by fit. For each, include: company name, "
        "job title, seniority, location/remote, salary range, YC batch, "
        "job URL, and a reason connecting my resume to the role."
    )


@mcp.prompt()
def rerank_results() -> str:
    """Re-rank previous scan results against my resume."""
    return (
        "Use get_resume and get_preferences to understand my background, "
        "then use get_latest_results to load the previous scan. "
        "Rank the results from best to worst fit and give me your top 15 with a reason for each."
    )


@mcp.prompt()
def scan_overview() -> str:
    """Scan for jobs and give a high-level summary by category."""
    return (
        "Use scan_all with ignore_seen=true to get jobs from both sources. "
        "Don't rank them — just summarize what's out there. "
        "Group by category (AI tooling, Systems, General AI+SWE) and "
        "give a count and highlights for each. Note how many came from "
        "HN vs WAAS. Mention any interesting salary ranges or YC batch trends."
    )


@mcp.prompt()
def backfill() -> str:
    """Backfill last 3 months of HN and current WAAS jobs."""
    return (
        "Use scan_all with months=3 and ignore_seen=false to backfill the last 3 months of HN "
        "Who's Hiring threads plus current Work at a Startup jobs. Return a summary of how many "
        "new jobs were found from each source, broken down by category."
    )


@mcp.prompt()
def waas_only() -> str:
    """Scan only YC startups and rank against my resume."""
    return (
        "Use get_resume and get_preferences to understand my background, "
        "then scan_waas with ignore_seen=true to find engineering jobs from "
        "YC startups. Results are grouped by company with seniority estimates. "
        "Use get_job_details for promising matches to read full descriptions. "
        "Rank your top 15 by fit. For each, include: company name, YC batch, "
        "job title, seniority, salary range, location/remote, team size, "
        "job URL, and a reason connecting my resume to the role."
    )


if __name__ == "__main__":
    mcp.run()
