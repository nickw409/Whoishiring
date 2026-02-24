# HN "Who's Hiring" Job Scanner

## Project Overview
Python script that scans Hacker News "Who is hiring?" threads, filters posts by keyword categories, scrapes job board listings, and delivers results via HTML email.

## Tech
- Python 3, `requests` only. No heavy dependencies.
- Single script: `hn_jobs.py`

## Architecture

### Keyword Categories & Scoring
Posts are scored by matched category (not per-keyword):
- **AI tooling (weight 3):** claude code, copilot, cursor, ai-assisted, ai tools, ai coding, agentic, llm, ai engineer
- **Systems (weight 2):** rust, cuda, gpu, simd, high-performance, hpc, systems programming
- **General AI+SWE (weight 1):** machine learning, tensorflow, pytorch, deep learning, computer vision, ml engineer, ai/ml

All matching uses word boundaries (`\b`), case-insensitive.

### Negative Filter
Keywords: staff engineer, principal engineer, engineering manager, director of, vp of, 10+ years, 15+ years

If a post matches positive keywords BUT also matches a negative keyword, it goes to a "Filtered Out" section (not silently dropped). Posts matching only negative keywords are discarded.

### Post Parsing (best effort)
- Company name, location, remote status from comment text
- Email addresses + surrounding application instructions
- URLs to job boards (Greenhouse, Lever, Ashby) or careers pages

### Job Board Scraping
Try in order: JSON-LD → Open Graph → meta tags → `<title>` tag fallback.
Target domains: Greenhouse, Lever, Ashby.

### Email Layout
Sorted by score (highest first). Each entry shows:
- Company, location, remote status
- Comment snippet with matched keywords highlighted
- Apply method: email (with address + instructions), job board link (with scraped title), or careers URL
- "Filtered Out" section at bottom showing blocked posts with reason

### Deduplication
- Track seen HN comment IDs in `seen_posts.json`
- Skip already-seen posts on subsequent runs
- Auto-prune entries older than 6 months

### Run Modes
- `python hn_jobs.py` — scan and email
- `--no-email` — print to terminal only
- `--dry-run` — save HTML preview locally without sending

### First Run Backfill
On first run (empty `seen_posts.json`), scan current month + 2 prior months.

### Config (env vars)
- `HN_JOBS_EMAIL_TO`
- `HN_JOBS_EMAIL_FROM`
- `HN_JOBS_EMAIL_PASSWORD` (Gmail app password)

## Code Style
- Keep it simple, single-file where possible
- No over-engineering or unnecessary abstractions
- Comments only where logic isn't self-evident

## Git Rules
- Never add Co-authored-by trailers
- Never mention Claude, AI, or any assistant in commits
- Commits written as sole author
