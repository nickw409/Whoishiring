# HN "Who's Hiring" Job Scanner

## Project Overview
Python script that scans Hacker News "Who is hiring?" threads, filters posts by keyword categories, scrapes job board listings, optionally ranks results against a resume using Claude, and delivers results via HTML email or local file.

## Tech
- Python 3
- Dependencies: `requests`, `pyyaml`, `pymupdf`, `anthropic`
- Single script: `hn_jobs.py`
- Config: `config.yaml` (optional, see `config.yaml.example`)

## Architecture

### Pipeline
1. Find threads via Algolia search API
2. Fetch top-level comments (parallelized, 20 workers)
3. Keyword match + score
4. Negative keyword filter (senior/management roles → "Filtered Out" section)
5. Location filter (non-US, non-remote → "Filtered Out")
6. Job board scraping (Greenhouse, Lever, Ashby)
7. Claude ranking (optional, requires resume + ANTHROPIC_API_KEY)
8. Output (email, HTML file, or terminal)

### Keyword Categories & Scoring
Posts are scored by matched category (not per-keyword):
- **AI tooling (weight 3):** claude code, copilot, cursor, ai-assisted, ai tools, ai coding, agentic, llm, ai engineer
- **Systems (weight 2):** rust, cuda, gpu, simd, high-performance, hpc, systems programming
- **General AI+SWE (weight 1):** machine learning, tensorflow, pytorch, deep learning, computer vision, ml engineer, ai/ml

All matching uses word boundaries (`\b`), case-insensitive. Scoring is used as a relevance gate; when Claude ranking is enabled, it replaces score-based ordering entirely.

### Negative Filter
Keywords: staff engineer, principal engineer, engineering manager, director of, vp of, 10+ years, 15+ years

If a post matches positive keywords BUT also matches a negative keyword, it goes to a "Filtered Out" section (not silently dropped). Posts matching only negative keywords are discarded.

### Location Filter
Non-US jobs are filtered out unless they're marked remote. Uses regex matching against known US and non-US cities/countries. Jobs with no detected location are kept (benefit of the doubt).

### Claude Ranking
When a resume is provided (via `config.yaml` or `--resume` flag) and `ANTHROPIC_API_KEY` is set:
- All filtered results + full resume text + user preferences sent to Claude in one API call
- Claude ranks every job by fit and returns a reason per job
- Results are re-ordered by Claude's ranking
- HTML output shows rank badges (#1, #2, etc.) and per-job reasoning
- Falls back to score ordering on any error or missing config

Model constant: `CLAUDE_MODEL` at top of ranking section.

### Post Parsing (best effort)
- Company name, location, remote status from comment text (pipe-delimited header)
- Email addresses + surrounding application instructions
- URLs to job boards (Greenhouse, Lever, Ashby) or careers pages

### Job Board Scraping
Try in order: JSON-LD → Open Graph → meta tags → `<title>` tag fallback.
Target domains: Greenhouse, Lever, Ashby. 0.5s politeness delay between requests.

### Config
`config.yaml` (optional):
- `resume` — path to PDF resume for Claude ranking
- `preferences.remote` — remote preference (e.g., "preferred", "required")
- `preferences.notes` — free-form text sent to Claude for ranking context

Environment variables:
- `ANTHROPIC_API_KEY` — for Claude ranking
- `HN_JOBS_EMAIL_TO`, `HN_JOBS_EMAIL_FROM`, `HN_JOBS_EMAIL_PASSWORD` — for email delivery

### Deduplication
- Track seen HN comment IDs in `seen_posts.json`
- Skip already-seen posts on subsequent runs
- Auto-prune entries older than 6 months

### Run Modes
- `python3 hn_jobs.py` — scan and email
- `--no-email` — print to terminal only
- `--dry-run` — save HTML preview to `results/`
- `--resume PDF` — override config resume path
- `--no-rank` — skip Claude ranking

### First Run Backfill
On first run (empty `seen_posts.json`), scan current month + 2 prior months.

### Output Files
- `results/preview_<timestamp>.html` — HTML preview (--dry-run)
- `results/results_<timestamp>.json` — structured JSON (always)

## MCP Server (`mcp_server.py`)
Exposes the scan pipeline to Claude Desktop via the Model Context Protocol (stdio transport). Claude Desktop acts as the ranker, eliminating the need for an API key.

### Tools
- `scan_jobs(months=1, ignore_seen=False)` — runs scan pipeline, returns JSON results. `ignore_seen=True` bypasses dedup.
- `get_resume()` — extracts resume text from configured PDF
- `get_preferences()` — returns preferences from config.yaml
- `get_latest_results()` — returns most recent saved results JSON

### Key Details
- Server imports from `hn_jobs.py` — all pipeline logic lives there, MCP server is just the interface
- `ignore_seen` skips dedup AND does not update `seen_posts.json` (avoids polluting the seen list)
- Claude Desktop config uses `wsl` as command on Windows, direct Python path on Linux/Mac

## Code Style
- Keep it simple, single-file where possible
- No over-engineering or unnecessary abstractions
- Comments only where logic isn't self-evident

## Git Rules
- Never add Co-authored-by trailers
- Never mention Claude, AI, or any assistant in commits
- Commits written as sole author
