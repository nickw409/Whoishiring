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
from concurrent.futures import ThreadPoolExecutor, as_completed

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

SEEN_FILE = Path(__file__).parent / "seen_posts.json"
RESULTS_DIR = Path(__file__).parent / "results"
PRUNE_DAYS = 180  # 6 months

HN_API = "https://hacker-news.firebaseio.com/v0"
HN_SEARCH_API = "https://hn.algolia.com/api/v1"
HN_USER = "whoishiring"
THREAD_TITLE_PREFIX = "Ask HN: Who is hiring?"

KEYWORD_CATEGORIES = {
    "AI tooling": {
        "weight": 3,
        "keywords": [
            "claude code", "copilot", "cursor", "ai-assisted", "ai tools",
            "ai coding", "agentic", "llm", "ai engineer",
        ],
    },
    "Systems": {
        "weight": 2,
        "keywords": [
            "rust", "cuda", "gpu", "simd", "high-performance", "hpc",
            "systems programming",
        ],
    },
    "General AI+SWE": {
        "weight": 1,
        "keywords": [
            "machine learning", "tensorflow", "pytorch", "deep learning",
            "computer vision", "ml engineer", "ai/ml",
        ],
    },
}

NEGATIVE_KEYWORDS = [
    "staff engineer", "principal engineer", "engineering manager",
    "director of", "vp of", "10+ years", "15+ years",
]

# Pre-compile regexes
_kw_patterns = {}
for _cat, _info in KEYWORD_CATEGORIES.items():
    for _kw in _info["keywords"]:
        # ai/ml needs special handling — escape the slash
        _escaped = re.escape(_kw)
        _kw_patterns[_kw] = re.compile(r"\b" + _escaped + r"\b", re.IGNORECASE)

_neg_patterns = {}
for _kw in NEGATIVE_KEYWORDS:
    _escaped = re.escape(_kw)
    _neg_patterns[_kw] = re.compile(r"\b" + _escaped + r"\b", re.IGNORECASE)

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
# Keyword matching & scoring
# ---------------------------------------------------------------------------

def match_keywords(text):
    """Return dict of {category: [matched keywords]} for a comment."""
    matches = {}
    for cat, info in KEYWORD_CATEGORIES.items():
        cat_matches = []
        for kw in info["keywords"]:
            if _kw_patterns[kw].search(text):
                cat_matches.append(kw)
        if cat_matches:
            matches[cat] = cat_matches
    return matches


def match_negative(text):
    """Return list of matched negative keywords."""
    return [kw for kw, pat in _neg_patterns.items() if pat.search(text)]


def score_matches(matches):
    """Score based on matched categories (per-category, not per-keyword)."""
    total = 0
    for cat in matches:
        total += KEYWORD_CATEGORIES[cat]["weight"]
    return total


# ---------------------------------------------------------------------------
# Comment parsing
# ---------------------------------------------------------------------------

def strip_html(text):
    """Strip HTML tags and decode entities from HN comment text."""
    # HN uses <p> for paragraph breaks
    text = re.sub(r"<p>", "\n\n", text)
    text = re.sub(r"<br\s*/?>", "\n", text)
    # pull out links before stripping
    text = re.sub(r"<[^>]+>", "", text)
    return html.unescape(text).strip()


def extract_urls(html_text):
    """Extract URLs from href attributes and plain text."""
    urls = set()
    # from href attributes (unescape HTML entities in URLs)
    for m in re.finditer(r'href="([^"]+)"', html_text):
        urls.add(html.unescape(m.group(1)))
    # from plain text (after stripping tags)
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
    location = ""
    remote = False
    _skip_patterns = re.compile(
        r"^(remote|onsite|on-site|hybrid|full.time|part.time|contract|intern"
        r"|visa|no visa|relocation|\$[\d,]+|http|www\.|engineer|developer"
        r"|manager|designer|scientist|analyst|lead|senior|junior|founding"
        r"|backend|frontend|fullstack|full stack|devops|sre|platform"
        r"|mobile|ios|android|data|ml |ai |research)",
        re.IGNORECASE,
    )
    _location_hint = re.compile(
        r"(remote|nyc|sf|san francisco|new york|london|berlin|austin|seattle"
        r"|boston|chicago|denver|toronto|vancouver|paris|amsterdam|singapore"
        r"|bay area|usa|eu\b|uk\b|canada|germany|india|japan"
        r"|,\s*[A-Z]{2}\b)",  # "City, ST" pattern
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
            if not location and _location_hint.search(part) and not part.startswith("$"):
                location = part
            elif not location and not _skip_patterns.match(lower):
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

    return {
        "id": comment["id"],
        "time": comment.get("time", 0),
        "company": company,
        "location": location,
        "remote": remote,
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
# Deduplication
# ---------------------------------------------------------------------------

def load_seen():
    """Load seen post IDs from disk."""
    if SEEN_FILE.exists():
        try:
            data = json.loads(SEEN_FILE.read_text())
            return data
        except (json.JSONDecodeError, KeyError):
            return {"posts": {}}
    return {"posts": {}}


def save_seen(seen):
    """Save seen post IDs to disk."""
    SEEN_FILE.write_text(json.dumps(seen, indent=2))


def prune_seen(seen):
    """Remove entries older than PRUNE_DAYS."""
    cutoff = time.time() - (PRUNE_DAYS * 86400)
    seen["posts"] = {
        pid: ts for pid, ts in seen["posts"].items()
        if ts > cutoff
    }
    return seen


def mark_seen(seen, post_ids):
    """Mark post IDs as seen."""
    now = time.time()
    for pid in post_ids:
        seen["posts"][str(pid)] = now
    return seen


def is_first_run(seen):
    """Check if this is the first run (no seen posts)."""
    return len(seen.get("posts", {})) == 0


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


def format_post_html(parsed, matches, neg_matches=None):
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

    blocked_html = ""
    if neg_matches:
        blocked_kws = ", ".join(f'<code>{html.escape(k)}</code>' for k in neg_matches)
        blocked_html = f'<div style="color:#c62828;margin-top:4px">Blocked by: {blocked_kws}</div>'

    return f"""
    <div style="border:1px solid #e0e0e0;border-radius:8px;padding:16px;margin-bottom:12px;background:#fff">
        <div style="margin-bottom:8px">
            <span style="background:#e3f2fd;padding:3px 8px;border-radius:4px;font-size:0.85em;font-weight:600">{html.escape(cat_label)}</span>
            <strong style="font-size:1.1em;margin-left:8px">{html.escape(parsed["company"])}</strong>
            {f' &mdash; {location}' if location else ""}
            {remote_tag}
        </div>
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
            posts_html += format_post_html(item["parsed"], item["matches"])
    else:
        posts_html = '<p style="color:#999;text-align:center">No matching posts found.</p>'

    filtered_html = ""
    if filtered_out:
        filtered_html = """
        <div style="margin-top:32px;border-top:2px solid #ffcdd2;padding-top:16px">
            <h2 style="color:#c62828;font-size:1.1em">Filtered Out (negative keyword match)</h2>
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

        print(f"\n[{cats}] (score: {item['score']})")
        print(f"  {parsed['company']}{loc}{remote}")
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
    args = parser.parse_args()

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
    seen = load_seen()
    first_run = is_first_run(seen)

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
    results = []
    filtered_out = []
    all_seen_ids = []

    for thread in threads:
        print(f"\nFetching comments from: {thread.get('title', 'Unknown')}...")
        comments = fetch_comments(thread)
        print(f"  {len(comments)} top-level comments")

        for comment in comments:
            cid = str(comment["id"])

            # Dedup
            if cid in seen.get("posts", {}):
                continue

            all_seen_ids.append(cid)
            raw_text = strip_html(comment.get("text", ""))

            # Match keywords
            matches = match_keywords(raw_text)
            if not matches:
                continue

            neg = match_negative(raw_text)
            score = score_matches(matches)
            parsed = parse_comment(comment)

            # Scrape job boards
            scrape_job_boards(parsed)

            item = {
                "parsed": parsed,
                "matches": matches,
                "score": score,
                "thread_title": thread.get("title", ""),
            }

            if neg:
                item["neg_matches"] = neg
                filtered_out.append(item)
            else:
                results.append(item)

    # Sort by score descending
    results.sort(key=lambda x: x["score"], reverse=True)
    filtered_out.sort(key=lambda x: x["score"], reverse=True)

    print(f"\n{len(results)} matching posts, {len(filtered_out)} filtered out")

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
                "location": r["parsed"]["location"],
                "remote": r["parsed"]["remote"],
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
    mark_seen(seen, all_seen_ids)
    prune_seen(seen)
    save_seen(seen)
    print(f"Marked {len(all_seen_ids)} posts as seen")


if __name__ == "__main__":
    main()
