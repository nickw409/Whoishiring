#!/usr/bin/env python3
"""HN 'Who is Hiring?' Job Scanner — scans threads, filters by keywords, emails results."""

import argparse
import html
import json
import os
import re
import smtplib
import sys
import time
from datetime import datetime, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

import requests
import yaml
import fitz
from concurrent.futures import ThreadPoolExecutor, as_completed

from filters import (
    KEYWORD_CATEGORIES, NEGATIVE_KEYWORDS, SENIORITY_LEVELS, PRUNE_DAYS,
    _kw_patterns, _neg_patterns,
    match_keywords, match_negative, score_matches,
    estimate_seniority, seniority_exceeds,
    is_coding_job, is_outside_us,
    apply_filters, SeenTracker,
)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

CONFIG_FILE = Path(__file__).parent / "config.yaml"
SEEN_FILE = Path(__file__).parent / "seen_posts.json"
RESULTS_DIR = Path(__file__).parent / "results"

HN_API = "https://hacker-news.firebaseio.com/v0"
HN_SEARCH_API = "https://hn.algolia.com/api/v1"
HN_USER = "whoishiring"
THREAD_TITLE_PREFIX = "Ask HN: Who is hiring?"

# ---------------------------------------------------------------------------
# Config loading & resume extraction
# ---------------------------------------------------------------------------

def extract_resume_text(path):
    """Extract plain text from a PDF file using pymupdf."""
    p = Path(path)
    if not p.exists():
        print(f"ERROR: Resume file not found: {p}", file=sys.stderr)
        sys.exit(1)
    doc = fitz.open(str(p))
    text = ""
    for page in doc:
        text += page.get_text()
    doc.close()
    return text.strip()


def load_config(resume_override=None):
    """Load config from config.yaml. Returns dict with 'resume_text', 'preferences', and 'filters'."""
    config = {}
    if CONFIG_FILE.exists():
        with open(CONFIG_FILE) as f:
            config = yaml.safe_load(f) or {}

    resume_path = resume_override or config.get("resume")
    resume_text = None
    if resume_path:
        resume_text = extract_resume_text(resume_path)

    filters = config.get("filters", {})

    return {
        "resume_text": resume_text,
        "preferences": config.get("preferences", {}),
        "filters": {
            "max_seniority": filters.get("max_seniority"),
            "coding_only": filters.get("coding_only", False),
        },
    }


# ---------------------------------------------------------------------------
# HN API helpers
# ---------------------------------------------------------------------------

def hn_get(path, retries=3):
    """GET from HN API with retries."""
    url = f"{HN_API}/{path}.json"
    for attempt in range(retries):
        try:
            resp = requests.get(url, timeout=15)
            resp.raise_for_status()
            return resp.json()
        except (requests.RequestException, ValueError):
            if attempt < retries - 1:
                time.sleep(1 * (attempt + 1))
    return None


def find_hiring_threads(max_threads=3):
    """Find recent 'Who is hiring?' threads via Algolia search API."""
    try:
        resp = requests.get(
            f"{HN_SEARCH_API}/search_by_date",
            params={
                "query": '"Ask HN: Who is hiring?"',
                "tags": "story,author_whoishiring",
                "hitsPerPage": max_threads,
            },
            timeout=15,
        )
        resp.raise_for_status()
        hits = resp.json().get("hits", [])
    except (requests.RequestException, ValueError) as e:
        print(f"ERROR: Algolia search failed: {e}", file=sys.stderr)
        return []

    threads = []
    for hit in hits:
        title = hit.get("title", "")
        if not title.startswith(THREAD_TITLE_PREFIX):
            continue
        item = hn_get(f"item/{hit['objectID']}")
        if item:
            threads.append(item)

    return threads


def _fetch_one_comment(cid):
    """Fetch a single comment by ID (for use in thread pool)."""
    comment = hn_get(f"item/{cid}")
    if comment and comment.get("type") == "comment" and not comment.get("deleted") and not comment.get("dead"):
        return comment
    return None


def fetch_comments(thread):
    """Fetch all top-level comments for a thread (parallelized)."""
    kid_ids = thread.get("kids", [])
    comments = []
    with ThreadPoolExecutor(max_workers=20) as pool:
        futures = {pool.submit(_fetch_one_comment, cid): cid for cid in kid_ids}
        for future in as_completed(futures):
            result = future.result()
            if result:
                comments.append(result)
    return comments


# ---------------------------------------------------------------------------
# Comment parsing
# ---------------------------------------------------------------------------

def strip_html(text):
    """Strip HTML tags and decode entities from HN comment text."""
    text = re.sub(r"<p>", "\n\n", text)
    text = re.sub(r"<br\s*/?>", "\n", text)
    text = re.sub(r"<[^>]+>", "", text)
    return html.unescape(text).strip()


def extract_urls(html_text):
    """Extract URLs from href attributes and plain text."""
    urls = set()
    for m in re.finditer(r'href="([^"]+)"', html_text):
        urls.add(html.unescape(m.group(1)))
    plain = strip_html(html_text)
    for m in re.finditer(r'https?://[^\s<>"\')\]]+', plain):
        urls.add(m.group(0).rstrip(".,;:"))
    return list(urls)


def extract_emails(text):
    """Extract email addresses from text."""
    plain = strip_html(text)
    return list(set(re.findall(r'[\w.+-]+@[\w-]+\.[\w.-]+', plain)))


def extract_email_instructions(text):
    """Try to pull application instructions near an email address."""
    plain = strip_html(text)
    instructions = []
    for m in re.finditer(r'[\w.+-]+@[\w-]+\.[\w.-]+', plain):
        start = max(0, m.start() - 120)
        end = min(len(plain), m.end() + 120)
        context = plain[start:end].strip()
        instructions.append({"email": m.group(0), "context": context})
    return instructions


def classify_url(url):
    """Classify a URL as greenhouse, lever, ashby, or other."""
    lower = url.lower()
    if "greenhouse.io" in lower or "boards.greenhouse" in lower:
        return "greenhouse"
    if "lever.co" in lower or "jobs.lever" in lower:
        return "lever"
    if "ashbyhq.com" in lower:
        return "ashby"
    return "other"


def parse_comment(comment):
    """Parse a comment into structured data."""
    raw = comment.get("text", "")
    plain = strip_html(raw)
    lines = [l.strip() for l in plain.split("\n") if l.strip()]

    # First line is typically "Company | Role | Location | Remote/Onsite | Salary"
    company = ""
    role = ""
    location = ""
    remote = False
    _role_hint = re.compile(
        r"(engineer|developer|programmer|architect|sre|devops|swe"
        r"|designer|manager|scientist|analyst|lead|senior|junior|founding"
        r"|backend|frontend|fullstack|full.stack|platform|infrastructure"
        r"|product|marketing|sales|recruiter|operations"
        r"|mobile|ios|android|data|ml |ai |research|intern)",
        re.IGNORECASE,
    )
    _location_hint = re.compile(
        r"(remote|nyc|sf|san francisco|new york|london|berlin|austin|seattle"
        r"|boston|chicago|denver|toronto|vancouver|paris|amsterdam|singapore"
        r"|bay area|usa|eu\b|uk\b|canada|germany|india|japan"
        r"|,\s*[A-Z]{2}\b)",
        re.IGNORECASE,
    )
    _non_location = re.compile(
        r"^(remote|onsite|on-site|hybrid|full.time|part.time|contract"
        r"|visa|no visa|relocation|\$[\d,]+|http|www\.)",
        re.IGNORECASE,
    )
    if lines:
        header = lines[0]
        parts = [p.strip() for p in re.split(r"\s*[|]\s*", header)]
        if parts:
            company = parts[0]
        for part in parts[1:]:
            lower = part.lower()
            if "remote" in lower:
                remote = True
            if not role and _role_hint.search(part) and not part.startswith("$"):
                role = part
            elif not location and _location_hint.search(part) and not part.startswith("$"):
                location = part
            elif not location and not _non_location.match(lower) and not _role_hint.search(part):
                location = part

    urls = extract_urls(raw)
    emails = extract_emails(raw)
    email_instructions = extract_email_instructions(raw)

    job_board_urls = []
    other_urls = []
    for u in urls:
        kind = classify_url(u)
        if kind != "other":
            job_board_urls.append({"url": u, "type": kind, "title": None})
        else:
            other_urls.append(u)

    snippet = plain[:300] + ("..." if len(plain) > 300 else "")

    seniority = estimate_seniority(role, plain)
    coding = is_coding_job(role, plain)

    return {
        "id": comment["id"],
        "time": comment.get("time", 0),
        "company": company,
        "role": role,
        "location": location,
        "remote": remote,
        "seniority": seniority,
        "is_coding": coding,
        "snippet": snippet,
        "full_text": plain,
        "emails": emails,
        "email_instructions": email_instructions,
        "job_board_urls": job_board_urls,
        "other_urls": other_urls,
    }


# ---------------------------------------------------------------------------
# Job board scraping
# ---------------------------------------------------------------------------

def scrape_job_title(url):
    """Scrape a job title from a Greenhouse/Lever/Ashby page."""
    try:
        resp = requests.get(url, timeout=10, headers={
            "User-Agent": "Mozilla/5.0 (compatible; HNJobScanner/1.0)"
        })
        resp.raise_for_status()
        page = resp.text
    except requests.RequestException:
        return None

    # 1. JSON-LD
    for m in re.finditer(r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>', page, re.DOTALL):
        try:
            data = json.loads(m.group(1))
            if isinstance(data, list):
                data = data[0]
            if isinstance(data, dict) and data.get("title"):
                return data["title"]
            if isinstance(data, dict) and data.get("name"):
                return data["name"]
        except (json.JSONDecodeError, IndexError, KeyError):
            pass

    # 2. Open Graph
    og = re.search(r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']+)["\']', page, re.IGNORECASE)
    if not og:
        og = re.search(r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:title["\']', page, re.IGNORECASE)
    if og:
        title = html.unescape(og.group(1)).strip()
        if title and len(title) < 200:
            return title

    # 3. Meta title / description
    meta = re.search(r'<meta[^>]+name=["\']title["\'][^>]+content=["\']([^"\']+)["\']', page, re.IGNORECASE)
    if meta:
        title = html.unescape(meta.group(1)).strip()
        if title and len(title) < 200:
            return title

    # 4. <title> tag fallback
    t = re.search(r"<title[^>]*>([^<]+)</title>", page, re.IGNORECASE)
    if t:
        title = html.unescape(t.group(1)).strip()
        if title and len(title) < 200:
            return title

    return None


def scrape_job_boards(parsed):
    """Fill in job titles for job board URLs in a parsed comment."""
    for entry in parsed["job_board_urls"]:
        title = scrape_job_title(entry["url"])
        if title:
            entry["title"] = title
        time.sleep(0.5)  # politeness delay


# ---------------------------------------------------------------------------
# Process threads (shared between main() and MCP server)
# ---------------------------------------------------------------------------

def process_threads(threads, seen_tracker, config_filters, scrape=True):
    """Process HN threads through the filter pipeline.

    Args:
        threads: List of HN thread dicts.
        seen_tracker: SeenTracker instance, or None to skip dedup.
        config_filters: Dict with 'coding_only' and 'max_seniority' keys.
        scrape: Whether to scrape job board URLs.

    Returns:
        (results, filtered_out, all_seen_ids)
    """
    coding_only = config_filters.get("coding_only", False)
    max_seniority = config_filters.get("max_seniority")

    results = []
    filtered_out = []
    all_seen_ids = []

    for thread in threads:
        comments = fetch_comments(thread)

        for comment in comments:
            cid = str(comment["id"])

            if seen_tracker and seen_tracker.is_seen(cid):
                continue

            all_seen_ids.append(cid)
            raw_text = strip_html(comment.get("text", ""))

            matches = match_keywords(raw_text)
            if not matches:
                continue

            neg = match_negative(raw_text)
            score = score_matches(matches)
            parsed = parse_comment(comment)

            if scrape:
                scrape_job_boards(parsed)

            item = {
                "parsed": parsed,
                "matches": matches,
                "score": score,
                "thread_title": thread.get("title", ""),
            }

            reasons = apply_filters(
                parsed, neg,
                coding_only=coding_only,
                max_seniority=max_seniority,
            )
            if reasons:
                item["neg_matches"] = reasons
                filtered_out.append(item)
            else:
                results.append(item)

    results.sort(key=lambda x: x["score"], reverse=True)
    filtered_out.sort(key=lambda x: x["score"], reverse=True)

    return results, filtered_out, all_seen_ids


# ---------------------------------------------------------------------------
# Claude ranking
# ---------------------------------------------------------------------------

def build_ranking_prompt(results, resume_text, preferences):
    """Build the prompt for Claude job ranking."""
    jobs_payload = []
    for i, item in enumerate(results):
        p = item["parsed"]
        jobs_payload.append({
            "index": i,
            "id": p["id"],
            "company": p["company"],
            "location": p["location"],
            "remote": p["remote"],
            "full_text": p["full_text"],
            "matched_categories": list(item["matches"].keys()),
            "matched_keywords": [kw for kws in item["matches"].values() for kw in kws],
            "score": item["score"],
        })

    prefs_str = ""
    if preferences:
        if preferences.get("remote"):
            prefs_str += f"Remote preference: {preferences['remote']}\n"
        if preferences.get("notes"):
            prefs_str += f"{preferences['notes']}\n"

    return f"""You are a job matching assistant. Given a candidate's resume and preferences, rank these job postings from best to worst fit.

RESUME:
{resume_text}

PREFERENCES:
{prefs_str if prefs_str else "No specific preferences provided."}

JOB POSTINGS:
{json.dumps(jobs_payload, indent=2)}

Return a JSON array where each element has:
- "index": the original index from the input
- "reason": a brief explanation (1-2 sentences) of why this job is ranked here

Order the array from best fit (first) to worst fit (last). Include ALL jobs.
Return ONLY the JSON array, no other text."""


CLAUDE_MODEL = "claude-opus-4-6"


def rank_jobs_with_claude(results, resume_text, preferences):
    """Rank jobs using Claude. Returns re-ordered results list with claude_rank and claude_reason."""
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("WARNING: ANTHROPIC_API_KEY not set, skipping ranking", file=sys.stderr)
        return results

    import anthropic

    prompt = build_ranking_prompt(results, resume_text, preferences)

    try:
        client = anthropic.Anthropic(api_key=api_key)
        response = client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=16000,
            system="You are a job matching assistant. Analyze the candidate's resume and preferences, then rank every job by fit. Return ONLY valid JSON.",
            messages=[{"role": "user", "content": prompt}],
        )
        text = response.content[0].text.strip()
        if text.startswith("```"):
            text = re.sub(r"^```\w*\n?", "", text)
            text = re.sub(r"\n?```$", "", text)
        ranking = json.loads(text)
    except Exception as e:
        print(f"WARNING: Ranking failed: {e}", file=sys.stderr)
        return results

    # Reorder results by Claude's ranking
    ranked = []
    ranked_indices = set()
    for claude_rank, entry in enumerate(ranking):
        idx = entry.get("index")
        if idx is not None and 0 <= idx < len(results):
            item = results[idx].copy()
            item["claude_rank"] = claude_rank + 1
            item["claude_reason"] = entry.get("reason", "")
            ranked.append(item)
            ranked_indices.add(idx)

    # Append any jobs Claude missed, preserving score order
    for i, item in enumerate(results):
        if i not in ranked_indices:
            ranked.append(item)

    return ranked


# ---------------------------------------------------------------------------
# HTML email formatting
# ---------------------------------------------------------------------------

def highlight_keywords(text, matched_keywords):
    """Wrap matched keywords in the text with highlight spans."""
    result = text
    for kw in matched_keywords:
        pattern = re.compile(r"(\b" + re.escape(kw) + r"\b)", re.IGNORECASE)
        result = pattern.sub(r'<mark style="background:#ffd54f;padding:1px 3px;border-radius:3px">\1</mark>', result)
    return result


def format_apply_section(parsed):
    """Format the application method section."""
    parts = []

    for entry in parsed["job_board_urls"]:
        label = entry["title"] or entry["type"].capitalize() + " listing"
        parts.append(
            f'<a href="{html.escape(entry["url"])}" style="color:#1a73e8">'
            f'{html.escape(label)}</a> ({entry["type"].capitalize()})'
        )

    for info in parsed["email_instructions"]:
        email = html.escape(info["email"])
        context = html.escape(info["context"])
        parts.append(
            f'Email: <a href="mailto:{email}" style="color:#1a73e8">{email}</a>'
            f'<br><span style="color:#666;font-size:0.9em">{context}</span>'
        )

    if not parts and parsed["other_urls"]:
        for u in parsed["other_urls"][:3]:
            parts.append(f'<a href="{html.escape(u)}" style="color:#1a73e8">{html.escape(u)}</a>')

    if not parts:
        parts.append('<span style="color:#999">No application link found — check HN post</span>')

    return "<br>".join(parts)


def format_post_html(parsed, matches, neg_matches=None, claude_rank=None, claude_reason=None):
    """Format a single post as an HTML block."""
    all_kws = []
    cats = []
    for cat, kws in matches.items():
        cats.append(cat)
        all_kws.extend(kws)

    cat_label = " + ".join(cats)
    remote_tag = ' <span style="background:#c8e6c9;padding:2px 6px;border-radius:3px;font-size:0.85em">Remote</span>' if parsed["remote"] else ""
    location = html.escape(parsed["location"]) if parsed["location"] else ""

    snippet_highlighted = highlight_keywords(html.escape(parsed["snippet"]), all_kws)
    kw_list = ", ".join(f'<code>{html.escape(k)}</code>' for k in all_kws)
    apply_html = format_apply_section(parsed)

    hn_link = f'https://news.ycombinator.com/item?id={parsed["id"]}'

    rank_badge = ""
    if claude_rank is not None:
        rank_badge = f'<span style="background:#1a73e8;color:#fff;padding:2px 8px;border-radius:4px;font-size:0.85em;font-weight:700">#{claude_rank}</span> '

    blocked_html = ""
    if neg_matches:
        blocked_kws = ", ".join(f'<code>{html.escape(k)}</code>' for k in neg_matches)
        blocked_html = f'<div style="color:#c62828;margin-top:4px">Blocked by: {blocked_kws}</div>'

    reason_html = ""
    if claude_reason:
        reason_html = f'<div style="color:#555;font-style:italic;margin-bottom:6px">{html.escape(claude_reason)}</div>'

    return f"""
    <div style="border:1px solid #e0e0e0;border-radius:8px;padding:16px;margin-bottom:12px;background:#fff">
        <div style="margin-bottom:8px">
            {rank_badge}<span style="background:#e3f2fd;padding:3px 8px;border-radius:4px;font-size:0.85em;font-weight:600">{html.escape(cat_label)}</span>
            <strong style="font-size:1.1em;margin-left:8px">{html.escape(parsed["company"])}</strong>
            {f' &mdash; {location}' if location else ""}
            {remote_tag}
        </div>
        {reason_html}
        <div style="color:#444;font-size:0.95em;margin-bottom:8px;line-height:1.5">{snippet_highlighted}</div>
        <div style="margin-bottom:6px"><strong>Matched:</strong> {kw_list}</div>
        {blocked_html}
        <div style="margin-bottom:6px"><strong>Apply:</strong><br>{apply_html}</div>
        <div><a href="{hn_link}" style="color:#ff6600;font-size:0.85em">View on HN</a></div>
    </div>"""


def build_email_html(results, filtered_out, thread_titles):
    """Build the full HTML email."""
    thread_info = ", ".join(html.escape(t) for t in thread_titles)
    now = datetime.now().strftime("%B %d, %Y")

    posts_html = ""
    if results:
        for item in results:
            posts_html += format_post_html(
                item["parsed"], item["matches"],
                claude_rank=item.get("claude_rank"),
                claude_reason=item.get("claude_reason"),
            )
    else:
        posts_html = '<p style="color:#999;text-align:center">No matching posts found.</p>'

    filtered_html = ""
    if filtered_out:
        filtered_html = """
        <div style="margin-top:32px;border-top:2px solid #ffcdd2;padding-top:16px">
            <h2 style="color:#c62828;font-size:1.1em">Filtered Out</h2>
        """
        for item in filtered_out:
            filtered_html += format_post_html(item["parsed"], item["matches"], item["neg_matches"])
        filtered_html += "</div>"

    return f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8"></head>
<body style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;max-width:700px;margin:0 auto;padding:20px;background:#f5f5f5">
    <div style="background:#ff6600;color:#fff;padding:16px 20px;border-radius:8px 8px 0 0">
        <h1 style="margin:0;font-size:1.3em">HN Who's Hiring — Job Scan Results</h1>
        <div style="font-size:0.9em;margin-top:4px">{now} &bull; {thread_info}</div>
    </div>
    <div style="padding:16px 0">
        <div style="margin-bottom:8px;color:#666;font-size:0.9em">{len(results)} matching posts found</div>
        {posts_html}
    </div>
    {filtered_html}
</body>
</html>"""


# ---------------------------------------------------------------------------
# Email sending
# ---------------------------------------------------------------------------

def send_email(html_body, to_addr, from_addr, password):
    """Send HTML email via Gmail SMTP."""
    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"HN Who's Hiring — {datetime.now().strftime('%B %Y')}"
    msg["From"] = from_addr
    msg["To"] = to_addr
    msg.attach(MIMEText(html_body, "html"))

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(from_addr, password)
        server.sendmail(from_addr, to_addr, msg.as_string())

    print(f"Email sent to {to_addr}")


# ---------------------------------------------------------------------------
# Terminal output
# ---------------------------------------------------------------------------

def print_results(results, filtered_out):
    """Print results to terminal."""
    if not results:
        print("No matching posts found.")
        return

    for item in results:
        parsed = item["parsed"]
        matches = item["matches"]
        cats = " + ".join(matches.keys())
        kws = ", ".join(kw for kws in matches.values() for kw in kws)
        remote = " | Remote" if parsed["remote"] else ""
        loc = f" | {parsed['location']}" if parsed["location"] else ""

        rank_prefix = f"#{item['claude_rank']} " if item.get("claude_rank") else ""
        print(f"\n{rank_prefix}[{cats}] (score: {item['score']})")
        print(f"  {parsed['company']}{loc}{remote}")
        if item.get("claude_reason"):
            print(f"  {item['claude_reason']}")
        print(f"  Matched: {kws}")

        for entry in parsed["job_board_urls"]:
            label = entry["title"] or entry["type"]
            print(f"  Apply: {label} — {entry['url']}")
        for info in parsed["email_instructions"]:
            print(f"  Apply: {info['email']}")
            print(f"         {info['context']}")
        if not parsed["job_board_urls"] and not parsed["email_instructions"]:
            for u in parsed["other_urls"][:2]:
                print(f"  Link: {u}")

        print(f"  HN: https://news.ycombinator.com/item?id={parsed['id']}")

    if filtered_out:
        print(f"\n--- Filtered Out ({len(filtered_out)} posts) ---")
        for item in filtered_out:
            parsed = item["parsed"]
            cats = " + ".join(item["matches"].keys())
            neg = ", ".join(item["neg_matches"])
            print(f"\n  [{cats}] {parsed['company']}")
            print(f"    Blocked by: {neg}")
            print(f"    HN: https://news.ycombinator.com/item?id={parsed['id']}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Scan HN 'Who is Hiring?' threads")
    parser.add_argument("--no-email", action="store_true", help="Print to terminal only")
    parser.add_argument("--dry-run", action="store_true", help="Save HTML preview locally")
    parser.add_argument("--resume", metavar="PDF", help="Path to PDF resume for context")
    parser.add_argument("--no-rank", action="store_true", help="Skip ranking")
    args = parser.parse_args()

    config = load_config(resume_override=args.resume)
    if config["resume_text"]:
        print(f"Resume loaded ({len(config['resume_text'])} chars)")
    if config["preferences"]:
        print(f"Preferences loaded from config.yaml")

    need_email = not args.no_email and not args.dry_run
    if need_email:
        email_to = os.environ.get("HN_JOBS_EMAIL_TO")
        email_from = os.environ.get("HN_JOBS_EMAIL_FROM")
        email_pass = os.environ.get("HN_JOBS_EMAIL_PASSWORD")
        if not all([email_to, email_from, email_pass]):
            print("ERROR: Set HN_JOBS_EMAIL_TO, HN_JOBS_EMAIL_FROM, HN_JOBS_EMAIL_PASSWORD", file=sys.stderr)
            print("Or use --no-email / --dry-run", file=sys.stderr)
            sys.exit(1)

    # Load dedup state
    tracker = SeenTracker(SEEN_FILE, "posts")
    tracker.load()
    first_run = tracker.is_empty()

    # Find threads
    num_threads = 3 if first_run else 1
    print(f"Finding {'last 3 threads (first run)' if first_run else 'latest thread'}...")
    threads = find_hiring_threads(max_threads=num_threads)
    if not threads:
        print("No hiring threads found.", file=sys.stderr)
        sys.exit(1)

    thread_titles = [t.get("title", "Unknown") for t in threads]
    for t in thread_titles:
        print(f"  Found: {t}")

    # Process comments
    print(f"\nFetching comments...")
    results, filtered_out, all_seen_ids = process_threads(
        threads, tracker, config["filters"],
    )

    print(f"\n{len(results)} matching posts, {len(filtered_out)} filtered out")

    # Rank with Claude if resume is available
    if config["resume_text"] and not args.no_rank and results:
        print("Ranking jobs with Claude...")
        results = rank_jobs_with_claude(results, config["resume_text"], config["preferences"])
        print("Ranking complete")

    # Output
    if args.no_email:
        print_results(results, filtered_out)
    else:
        html_body = build_email_html(results, filtered_out, thread_titles)

        if args.dry_run:
            RESULTS_DIR.mkdir(exist_ok=True)
            out_path = RESULTS_DIR / f"preview_{datetime.now().strftime('%Y%m%d_%H%M%S')}.html"
            out_path.write_text(html_body)
            print(f"Preview saved to {out_path}")
        else:
            send_email(html_body, email_to, email_from, email_pass)

    # Save results JSON
    RESULTS_DIR.mkdir(exist_ok=True)
    json_path = RESULTS_DIR / f"results_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    json_data = {
        "scan_time": datetime.now().isoformat(),
        "threads": thread_titles,
        "results": [
            {
                "id": r["parsed"]["id"],
                "company": r["parsed"]["company"],
                "role": r["parsed"].get("role", ""),
                "location": r["parsed"]["location"],
                "remote": r["parsed"]["remote"],
                "seniority": r["parsed"].get("seniority", "unknown"),
                "is_coding": r["parsed"].get("is_coding", True),
                "score": r["score"],
                "matched_categories": list(r["matches"].keys()),
                "matched_keywords": [kw for kws in r["matches"].values() for kw in kws],
                "job_board_urls": r["parsed"]["job_board_urls"],
                "emails": r["parsed"]["emails"],
                "other_urls": r["parsed"]["other_urls"],
            }
            for r in results
        ],
        "filtered_out_count": len(filtered_out),
    }
    json_path.write_text(json.dumps(json_data, indent=2))
    print(f"Results saved to {json_path}")

    # Update dedup
    tracker.mark(all_seen_ids)
    tracker.prune()
    tracker.save()
    print(f"Marked {len(all_seen_ids)} posts as seen")


if __name__ == "__main__":
    main()
