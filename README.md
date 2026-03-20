# HN "Who's Hiring" Job Scanner

Scans Hacker News monthly "Who is hiring?" threads and YC's Work at a Startup board, filters by keywords/seniority/location/job type, tracks top jobs in a persistent pipeline, and integrates with Claude Desktop for analysis and ranking.

## Sources

### HN Who's Hiring
- Monthly threads from Hacker News "Ask HN: Who is hiring?"
- Fetched via Algolia search API
- Scans current month + configurable history (1-3 months)

### Work at a Startup (WAAS)
- YC's job board at workatastartup.com
- Scraped using Playwright headless browser
- Filterable by role, job type, experience level via config

Both sources use identical keyword filtering and scoring from `filters.py`.

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### Config

```bash
cp config.yaml.example config.yaml
```

```yaml
resume: /path/to/resume.pdf
preferences:
  remote: preferred
  notes: |
    I prefer early-stage startups. Looking to work with Rust or Python.
    Not interested in fintech.
filters:
  max_seniority: mid       # intern, junior, mid, senior, staff+ (filter out above)
  coding_only: true         # filter out non-coding roles
tracking:
  max_tracked: 20           # max jobs in tracked pipeline (overflow → backlog)
waas:
  role: eng                 # eng, sales, operations, marketing, product, any
  job_type: fulltime        # fulltime, intern, contract, cofounder, any
  min_experience: "0,1"     # 0, 1, 3, 6, 11, any (comma-separated for OR)
```

### Environment Variables

Add to the project `.env` file (gitignored):

| Variable | Purpose |
|----------|---------|
| `ANTHROPIC_API_KEY` | Claude API for CLI ranking |
| `HN_JOBS_EMAIL_TO` | Email recipient |
| `HN_JOBS_EMAIL_FROM` | Email sender |
| `HN_JOBS_EMAIL_PASSWORD` | Gmail app password |
| `WAAS_USERNAME` | YC account for full WAAS access |
| `WAAS_PASSWORD` | YC account password |
| `TRACKING_DIR` | Directory for tracking JSON files (keeps paths out of git) |

Without WAAS credentials, scraping is limited to ~30 jobs.

## CLI Usage

```bash
# Save HTML results locally (no email, no ranking)
python3 hn_jobs.py --dry-run --no-rank

# Print to terminal
python3 hn_jobs.py --no-email

# Rank against resume and save HTML
python3 hn_jobs.py --dry-run --resume /path/to/resume.pdf

# Scan and send email
python3 hn_jobs.py
```

| Flag | Description |
|------|-------------|
| `--no-email` | Print results to terminal |
| `--dry-run` | Save HTML preview to `results/` |
| `--resume PDF` | Override config resume path |
| `--no-rank` | Skip Claude ranking |

## Claude Desktop (MCP Server)

The MCP server exposes the full scan and job tracking pipeline to Claude Desktop via stdio transport. Claude Desktop acts as the ranker — no API key needed.

### Setup

Add to `claude_desktop_config.json`. On WSL, use `bash -c` to source `.env`:

```json
{
  "mcpServers": {
    "hn-jobs": {
      "type": "stdio",
      "command": "wsl",
      "args": [
        "bash", "-c",
        "set -a; source /path/to/project/.env; set +a; /path/to/project/.venv/bin/python3 /path/to/project/mcp_server.py"
      ]
    }
  }
}
```

### Scanning Tools

| Tool | Description |
|------|-------------|
| `scan_jobs(months, ignore_seen)` | Scan HN threads, return results |
| `scan_waas(ignore_seen, group_by_company)` | Scan WAAS, auto-track top N jobs. Returns run metadata only |
| `scan_all(ignore_seen, months, group_by_company)` | Scan both sources, dedup by company (HN priority) |
| `get_job_details(job_url)` | Get full job description (cached on disk, falls back to HTTP) |

### Job Tracking Tools

Jobs flow through a persistent pipeline stored in JSON files at `TRACKING_DIR`:

| Tool | Description |
|------|-------------|
| `get_tracked_jobs()` | Active pipeline — top N open jobs (with or without analysis) |
| `get_applied_jobs()` | Jobs marked as applied |
| `get_dismissed_jobs()` | Jobs marked as dismissed |
| `get_longshot_jobs()` | Interesting but unlikely jobs worth keeping visible |
| `update_job_analysis(job_url, ...)` | Write fit analysis for a tracked job |
| `mark_applied(job_url)` | Move tracked → applied, backfill from backlog. Returns promoted job |
| `mark_dismissed(job_url)` | Move tracked → dismissed, backfill from backlog. Returns promoted job |
| `mark_longshot(job_url)` | Move tracked → longshot, backfill from backlog. Returns promoted job |
| `mark_open(job_url)` | Move applied/dismissed/longshot → tracked (demotes lowest score if at cap) |
| `swap_role(job_url, new_job_url)` | Replace a tracked job with an alternate role from the same company |
| `validate_tracked_jobs()` | Check open/dismissed/longshot jobs against WAAS, remove dead, backfill |
| `reset_tracking()` | Clear all tracking files and seen_waas (requires server restart) |

### Config & Resume Tools

| Tool | Description |
|------|-------------|
| `get_resume()` | Extract resume text from configured PDF |
| `get_preferences()` | Return preferences from config.yaml |
| `get_config()` | Return full config |
| `update_config(...)` | Update config.yaml settings |
| `get_latest_results()` | Return most recent saved results JSON |

### Job Tracking Pipeline

Six JSON files at `TRACKING_DIR`, keyed by WAAS job URL:

- **`tracked_jobs.json`** — Active pipeline, max N jobs (default 20). Filled by score from backlog.
- **`backlog_jobs.json`** — Overflow jobs ranked by score, waiting for a tracked slot.
- **`applied_jobs.json`** — Moved from tracked by `mark_applied`. Permanent, not validated.
- **`dismissed_jobs.json`** — Moved from tracked by `mark_dismissed`. Validated (dead listings removed).
- **`longshot_jobs.json`** — Moved from tracked by `mark_longshot`. Validated (dead listings removed).
- **`job_descriptions.json`** — Cached full descriptions. Auto-pruned when jobs leave tracked/backlog/applied/longshot.

Each tracked entry: company, yc_batch, company_size, job_title, seniority, salary_range, location, remote, other_roles, score, status, date_added, date_applied, analysis.

### Daily Workflow

1. `validate_tracked_jobs` — remove dead listings, backfill from backlog
2. `scan_waas` — discover new jobs, auto-track top N by score
3. `get_tracked_jobs` — find unanalyzed jobs (analysis = null)
4. For each: `get_job_details` → `update_job_analysis` (or `mark_dismissed`/`mark_longshot` if poor fit)
5. Display tracked/applied/dismissed/longshot in React artifact

### Filters

All filters apply during scanning before jobs enter tracking:

- **Keywords** — scored by category: AI tooling (3), Systems (2), General AI+SWE (1)
- **Negative keywords** — staff/principal/director/VP/10+ years → filtered out
- **Seniority** — configurable max level (intern/junior/mid/senior/staff+)
- **Job type** — coding vs non-coding (engineering management = non-coding)
- **Location** — non-US, non-remote jobs filtered out

Active filters are included in `scan_waas` response metadata.

## How It Works

1. **Find threads** — queries Algolia for recent "Who is hiring?" posts
2. **Fetch comments** — pulls top-level comments (parallelized, 20 workers)
3. **Keyword matching** — scores posts by category
4. **Filter cascade** — negative keywords → seniority → job type → location
5. **Job board scraping** — extracts titles from Greenhouse, Lever, Ashby links
6. **Tracking** — top N by score enter tracked pipeline, rest go to backlog
7. **Analysis** — Claude Desktop analyzes each tracked job against resume
8. **Output** — React artifact, HTML email, local file, or terminal

## Dependencies

- `requests` — HN API + job board scraping
- `pyyaml` — config loading
- `pymupdf` — PDF resume extraction
- `anthropic` — Claude API (CLI ranking only)
- `playwright` — headless browser for WAAS
- `beautifulsoup4` — HTML parsing
- `mcp` — MCP server for Claude Desktop
