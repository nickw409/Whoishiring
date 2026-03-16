#!/usr/bin/env python3
"""MCP server for HN Who's Hiring job scanner."""

import json
import sys
from pathlib import Path

# Add project dir to path so we can import from hn_jobs
sys.path.insert(0, str(Path(__file__).parent))

from mcp.server.fastmcp import FastMCP

import hn_jobs

mcp = FastMCP(name="HN Who's Hiring")


@mcp.tool()
def scan_jobs(months: int = 1, ignore_seen: bool = False) -> str:
    """Scan HN 'Who is Hiring?' threads for matching job posts.

    Fetches recent threads, filters by keyword categories (AI tooling,
    Systems, General AI+SWE), removes non-US non-remote jobs and
    senior/management roles, scrapes job board links, and returns
    structured results sorted by keyword score.

    Args:
        months: Number of monthly threads to scan (1-3). Use 1 for latest only, 3 for backfill.
        ignore_seen: If true, return all matching posts even if previously seen.
    """
    months = max(1, min(3, months))

    seen = hn_jobs.load_seen() if not ignore_seen else {"posts": {}}
    threads = hn_jobs.find_hiring_threads(max_threads=months)
    if not threads:
        return json.dumps({"error": "No hiring threads found"})

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

    # Update dedup (skip if ignore_seen to avoid polluting the seen list)
    if not ignore_seen:
        hn_jobs.mark_seen(seen, all_seen_ids)
        hn_jobs.prune_seen(seen)
        hn_jobs.save_seen(seen)

    # Format output for Claude
    output = {
        "threads": thread_titles,
        "total_results": len(results),
        "total_filtered": len(filtered_out),
        "results": [],
    }

    for item in results:
        p = item["parsed"]
        output["results"].append({
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
        })

    return json.dumps(output, indent=2)


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

    Returns remote preference and free-form notes about what
    the user is looking for in a role.
    """
    config = hn_jobs.load_config()
    prefs = config["preferences"]
    if not prefs:
        return "No preferences configured. Set 'preferences' in config.yaml."
    return json.dumps(prefs, indent=2)


@mcp.tool()
def get_latest_results() -> str:
    """Get the most recent scan results from disk.

    Returns the latest results JSON file without running a new scan.
    Useful for re-ranking or reviewing previous results.
    """
    results_dir = hn_jobs.RESULTS_DIR
    if not results_dir.exists():
        return json.dumps({"error": "No results directory found. Run scan_jobs first."})

    json_files = sorted(results_dir.glob("results_*.json"), reverse=True)
    if not json_files:
        return json.dumps({"error": "No results files found. Run scan_jobs first."})

    return json_files[0].read_text()


@mcp.prompt()
def find_jobs() -> str:
    """Scan this month's HN jobs and rank them against my resume."""
    return (
        "Use get_resume and get_preferences to understand my background, "
        "then scan_jobs with ignore_seen=true to find this month's matching positions. "
        "Rank every job from best to worst fit for me and give me your top 15 with a reason for each."
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
        "Scan for this month's jobs with scan_jobs using ignore_seen=true. "
        "Don't rank them — just summarize what's out there. "
        "Group them by category (AI tooling, Systems, General AI+SWE) and "
        "give me a count and highlights for each."
    )


@mcp.prompt()
def backfill() -> str:
    """Scan the last 3 months and find the best matches."""
    return (
        "Use get_resume and get_preferences to understand my background, "
        "then scan_jobs with months=3 and ignore_seen=true to get the last 3 months of jobs. "
        "Rank every job from best to worst fit and give me your top 20 with a reason for each."
    )


if __name__ == "__main__":
    mcp.run()
