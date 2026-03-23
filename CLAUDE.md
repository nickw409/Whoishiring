# HN "Who's Hiring" Job Scanner

## Project Overview
Python script that scans Hacker News "Who is hiring?" threads, filters posts by keyword categories, scrapes job board listings, optionally ranks results against a resume using Claude, and delivers results via HTML email or local file.

## Tech
- Python 3
- Dependencies: `requests`, `pyyaml`, `pymupdf`, `anthropic`, `playwright`, `beautifulsoup4`
- Main scripts: `hn_jobs.py`, `waas.py`
- Shared filtering: `filters.py` (keyword matching, scoring, seniority, coding classification, location, dedup)
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

### Seniority Filter
Estimates seniority from job title (staff/senior/lead/junior/intern keywords) and description (experience year requirements). Configurable via `filters.max_seniority` in `config.yaml` — jobs above the specified level go to "Filtered Out". Levels: intern, junior, mid, senior, staff+. Unknown seniority is never filtered (benefit of the doubt).

### Job Type Filter
Classifies jobs as coding (engineer, developer, SRE, etc.) vs non-coding (product manager, designer, sales, recruiter, etc.). Engineering management is classified as non-coding. Configurable via `filters.coding_only: true` in `config.yaml`. Unknown job types are kept (benefit of the doubt).

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
- `filters.max_seniority` — max seniority level to include (intern/junior/mid/senior/staff+)
- `filters.coding_only` — if true, filter out non-coding roles

Environment variables:
- `ANTHROPIC_API_KEY` — for Claude ranking
- `HN_JOBS_EMAIL_TO`, `HN_JOBS_EMAIL_FROM`, `HN_JOBS_EMAIL_PASSWORD` — for email delivery
- `WAAS_USERNAME`, `WAAS_PASSWORD` — YC account credentials for full WAAS access (without these, limited to ~30 jobs)

### Deduplication
- Track seen HN comment IDs in `seen_posts.json`
- Track seen WAAS job URLs in `seen_waas.json`
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
- `scan_jobs(months=1, ignore_seen=False)` — runs HN scan pipeline, returns JSON results. `ignore_seen=True` bypasses dedup.
- `scan_waas(ignore_seen=False)` — scrapes Work at a Startup, filters and returns JSON results.
- `scan_all(months=1, ignore_seen=False)` — combines HN + WAAS, deduplicates by company name (HN priority), sorted by score.
- `get_resume()` — extracts resume text from configured PDF
- `get_preferences()` — returns preferences from config.yaml
- `get_latest_results()` — returns most recent saved results JSON
- `get_tracked_jobs()` — returns tracked jobs (active analysis pipeline, max N)
- `get_applied_jobs()` — returns jobs marked as applied
- `get_rejected_jobs()` — returns jobs rejected after applying
- `get_accepted_jobs()` — returns jobs where an offer was received
- `update_job_analysis(job_url, fit_explanation, odds, odds_reasoning, salary_vs_col)` — writes analysis for a tracked job (will not overwrite existing)
- `mark_applied(job_url)` — moves job from tracked → applied_jobs.json, backfills from backlog
- `mark_dismissed(job_url)` — moves job from tracked → dismissed_jobs.json, backfills from backlog
- `mark_rejected(job_url)` — moves job from applied → rejected_jobs.json
- `mark_accepted(job_url)` — moves job from applied → accepted_jobs.json
- `mark_open(job_url)` — moves job from applied/dismissed/longshot/rejected/accepted back to tracked (demotes lowest-score if at cap)
- `validate_tracked_jobs()` — checks tracked jobs are still live, removes dead, backfills from backlog

### Job Tracking
Six JSON files keyed by WAAS job URL:
- `tracked_jobs.json` — active pipeline, max N jobs (default 20, configurable via `tracking.max_tracked` in config.yaml). All entries are open status.
- `backlog_jobs.json` — overflow jobs that passed filters but didn't make the top N cut. Ranked by score for promotion.
- `applied_jobs.json` — jobs moved here by `mark_applied`
- `dismissed_jobs.json` — jobs moved here by `mark_dismissed`
- `rejected_jobs.json` — applied jobs where you were rejected, moved here by `mark_rejected`
- `accepted_jobs.json` — applied jobs where you got an offer, moved here by `mark_accepted`

Each entry has: company, yc_batch, company_size, job_title, seniority, salary_range, location, remote, other_roles, score, status, date_added, date_applied, analysis.

- `scan_waas` ranks new jobs by score, fills tracked up to N, rest go to backlog
- `mark_applied`/`mark_dismissed` free a tracked slot; top backlog job is promoted
- `mark_rejected`/`mark_accepted` move applied jobs to outcome tracking
- `validate_tracked_jobs` removes dead listings, backfills from backlog
- Daily workflow: validate → scan → analyze unanalyzed → display in React artifact

### Prompts
Server registers MCP prompts (`find_jobs`, `rerank_results`, `scan_overview`, `backfill`) but Claude Desktop does not surface them in its UI as of March 2026. They are returned via `prompts/list` but ignored by the client.

### WAAS Integration
The MCP server exposes Work at a Startup (YC's job board) alongside HN:

- `scan_waas` calls `waas.scan_and_filter_waas()`, returns JSON matching HN format. Browser failures return empty results with error field.
- `scan_all` combines HN + WAAS in one call. Deduplicates by company name (case-insensitive, stripped). HN takes priority. Results sorted by score descending across sources. Error resilient: if one source fails, returns the other.
- WAAS uses same keyword categories and scoring as HN (imported from hn_jobs.py)
- WAAS has separate dedup tracking in `seen_waas.json`

### Key Details
- Server imports from `hn_jobs.py` and `waas.py` — pipeline logic lives there, MCP server is just the interface
- `ignore_seen` skips dedup AND does not update seen files (avoids polluting the seen list)
- Claude Desktop config uses `wsl` as command on Windows, direct Python path on Linux/Mac

## Code Style
- Keep it simple, single-file where possible
- No over-engineering or unnecessary abstractions
- Comments only where logic isn't self-evident

## Git Rules
- Never add Co-authored-by trailers
- Never mention Claude, AI, or any assistant in commits
- Commits written as sole author
