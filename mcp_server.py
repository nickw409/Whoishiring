#!/usr/bin/env python3
"""MCP server for HN Who's Hiring job scanner."""

import json
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

# Add project dir to path so we can import from hn_jobs
sys.path.insert(0, str(Path(__file__).parent))

from mcp.server.fastmcp import FastMCP

import hn_jobs
import waas

mcp = FastMCP(name="HN Who's Hiring")


def _scan_hn(months: int, ignore_seen: bool) -> tuple[list[dict], list[dict], list[str]]:
    """Extract HN scanning logic for reuse in scan_jobs and scan_all.

    Returns:
        (results, filtered_out, thread_titles)
    """
    months = max(1, min(3, months))

    seen = hn_jobs.load_seen() if not ignore_seen else {"posts": {}}
    threads = hn_jobs.find_hiring_threads(max_threads=months)
    if not threads:
        return [], [], []

    thread_titles = [t.get("title", "Unknown") for t in threads]

    results = []
    filtered_out = []
    all_seen_ids = []

    for thread in threads:
        comments = hn_jobs.fetch_comments(thread)
        for comment in comments:
            cid = str(comment["id"])
            if cid in seen.get("posts", {}):
                continue

            all_seen_ids.append(cid)
            raw_text = hn_jobs.strip_html(comment.get("text", ""))

            matches = hn_jobs.match_keywords(raw_text)
            if not matches:
                continue

            neg = hn_jobs.match_negative(raw_text)
            score = hn_jobs.score_matches(matches)
            parsed = hn_jobs.parse_comment(comment)
            hn_jobs.scrape_job_boards(parsed)

            item = {
                "parsed": parsed,
                "matches": matches,
                "score": score,
                "thread_title": thread.get("title", ""),
            }

            if neg:
                item["neg_matches"] = neg
                filtered_out.append(item)
            elif hn_jobs.is_outside_us(parsed):
                item["neg_matches"] = ["non-US location"]
                filtered_out.append(item)
            else:
                results.append(item)

    results.sort(key=lambda x: x["score"], reverse=True)

    if not ignore_seen:
        hn_jobs.mark_seen(seen, all_seen_ids)
        hn_jobs.prune_seen(seen)
        hn_jobs.save_seen(seen)

    return results, filtered_out, thread_titles


def _format_hn_results(results: list[dict]) -> list[dict]:
    """Format HN results for JSON output."""
    output = []
    for item in results:
        p = item["parsed"]
        output.append({
            "company": p["company"],
            "location": p["location"],
            "remote": p["remote"],
            "score": item["score"],
            "matched_categories": list(item["matches"].keys()),
            "matched_keywords": [kw for kws in item["matches"].values() for kw in kws],
            "full_text": p["full_text"],
            "emails": p["emails"],
            "job_board_urls": p["job_board_urls"],
            "other_urls": p["other_urls"],
            "hn_link": f"https://news.ycombinator.com/item?id={p['id']}",
            "source": "hn",
        })
    return output


def _format_waas_results(results: list[dict]) -> list[dict]:
    """Format WAAS results for JSON output."""
    output = []
    for item in results:
        p = item["parsed"]
        output.append({
            "company": p["company"],
            "location": p["location"],
            "remote": p["remote"],
            "score": item["score"],
            "matched_categories": list(item["matches"].keys()),
            "matched_keywords": [kw for kws in item["matches"].values() for kw in kws],
            "full_text": p["full_text"],
            "job_url": p["job_board_urls"][0]["url"] if p["job_board_urls"] else "",
            "job_title": p["job_board_urls"][0]["title"] if p["job_board_urls"] else "",
            "salary_range": p.get("salary_range", ""),
            "company_yc_batch": p.get("company_yc_batch", ""),
            "company_size": p.get("company_size", ""),
            "source": "waas",
        })
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

    output = {
        "threads": thread_titles,
        "total_results": len(results),
        "total_filtered": len(filtered_out),
        "results": _format_hn_results(results),
    }

    return json.dumps(output, indent=2)


@mcp.tool()
def scan_waas(ignore_seen: bool = False) -> str:
    """Scan Work at a Startup (workatastartup.com) for matching engineering jobs.

    Authenticates with YC (requires WAAS_USERNAME/WAAS_PASSWORD env vars),
    then fetches all job listings via the WAAS API. Filters by the same
    keyword categories as HN scanning. Pre-filters at the API level using
    Algolia (defaults: role=eng, job_type=fulltime; configurable via
    config.yaml under the "waas" key).

    Available config.yaml filters under "waas":
      role: eng|sales|operations|marketing|product
      eng_type: fs|be|ml|fe|eng_mgmt|devops|embedded
      remote: yes|only|no
      job_type: fulltime|intern|contract|cofounder
      min_experience: 0|1|3|6|11
      us_visa_required: yes|none|possible
      has_salary: true|false
      company_waas_stage: seed|series_a|growth|scale

    Each result includes: company (with YC batch and team size), job title,
    location, remote status, salary range, keyword score, matched
    categories/keywords, full job description, skills, and a direct
    WAAS job link.

    Without credentials, returns 0 results. Set WAAS_USERNAME and
    WAAS_PASSWORD in the .env file.

    Args:
        ignore_seen: If true, return all matching jobs even if previously seen.
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

    return json.dumps({
        "source": "waas",
        "total_results": len(results),
        "total_filtered": len(filtered_out),
        "results": _format_waas_results(results),
    }, indent=2)


@mcp.tool()
def scan_all(ignore_seen: bool = False, months: int = 1) -> str:
    """Scan both HN Who's Hiring and Work at a Startup, then combine results.

    Runs both scans in parallel for speed. HN results come from monthly
    "Who is Hiring?" threads; WAAS results come from YC's job board API
    (thousands of listings with full descriptions, salary ranges, and skills).

    Cross-source deduplication: if a company appears on both HN and WAAS,
    only the HN listing is kept. Results from both sources are merged and
    sorted by keyword score descending.

    Response includes per-source counts (hn_results, waas_results) and
    any errors that occurred. If one source fails, the other still returns.

    Each result has a "source" field ("hn" or "waas") so you can tell
    where it came from. WAAS results include extra fields: job_title,
    salary_range, company_yc_batch, and company_size.

    Args:
        ignore_seen: If true, return all jobs even if previously seen.
        months: Number of HN monthly threads to scan (1-3).
    """
    errors = []

    # Pass 1: scrape both sources in parallel
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

    # Pass 2: dedup WAAS against HN companies, then filter
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

    # Combine and sort by score descending
    combined = _format_hn_results(hn_results) + _format_waas_results(waas_results)
    combined.sort(key=lambda x: x.get("score", 0), reverse=True)

    result = {
        "sources": ["hn", "waas"],
        "threads": thread_titles,
        "total_results": len(combined),
        "total_filtered": len(hn_filtered) + len(waas_filtered),
        "hn_results": len(hn_results),
        "waas_results": len(waas_results),
        "waas_raw_scraped": len(waas_raw),
        "results": combined,
    }
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

    Use this to adjust preferences, resume path, or WAAS search filters.
    Pass null/None to remove a WAAS filter (revert to default).

    Args:
        resume: Path to PDF resume file for ranking.
        remote_preference: Remote work preference (e.g. "preferred", "required", "flexible").
        preference_notes: Free-form notes about what you're looking for, sent to the ranker.
        waas_role: WAAS role filter. Values: eng, sales, operations, marketing, product.
        waas_eng_type: WAAS engineering type. Values: fs, be, ml, fe, eng_mgmt, devops, embedded.
        waas_remote: WAAS remote filter. Values: yes, only, no.
        waas_job_type: WAAS job type. Values: fulltime, intern, contract, cofounder.
        waas_min_experience: WAAS min experience. Values: 0, 1, 3, 6, 11.
        waas_us_visa_required: WAAS visa filter. Values: yes, none, possible.
        waas_has_salary: WAAS salary listed filter. Values: true, false.
        waas_company_waas_stage: WAAS company stage. Values: seed, series_a, growth, scale.
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
            if value == "none" and key != "us_visa_required":
                # "none" means remove the filter (except us_visa_required where it's a valid value)
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


@mcp.prompt()
def find_jobs() -> str:
    """Find and rank jobs from both HN and YC startups against my resume."""
    return (
        "Use get_resume and get_preferences to understand my background, "
        "then scan_all with ignore_seen=true to find matching positions from "
        "both HN Who's Hiring and Work at a Startup (YC's job board). "
        "Rank every job from best to worst fit for me. "
        "For each of your top 15, include: rank, company name, job title, "
        "location/remote, salary range (if available from WAAS), YC batch "
        "(if available), and a 1-2 sentence reason why it's a good fit. "
        "Group WAAS results separately if they have salary/title info that "
        "HN results lack."
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
        "YC startups on Work at a Startup. "
        "Rank the results by fit. For each of your top 15, include: "
        "company name, YC batch, job title, salary range, location/remote, "
        "team size, and a reason why it fits my background. "
        "WAAS results have rich data — use salary ranges, skills, and "
        "company descriptions to make better ranking decisions."
    )


if __name__ == "__main__":
    mcp.run()
