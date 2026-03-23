# HN "Who's Hiring" Job Scanner

An automated job discovery and tracking pipeline that scrapes multiple sources, applies a multi-stage filter cascade, and exposes a 20-tool MCP server so Claude Desktop can analyze job fit against a resume — all without requiring an API key.

## What It Does

Aggregates job postings from two sources (Hacker News "Who is hiring?" threads via Algolia API, and YC's Work at a Startup board via Playwright headless browser), runs them through a shared multi-stage filter pipeline, maintains a bounded tracking system with backlog promotion, and provides a full MCP (Model Context Protocol) tool interface for an LLM to analyze, rank, and manage a job search pipeline end-to-end.

## Architecture

```
  Algolia API ──→ hn_jobs.py ──┐
                               ├──→ filters.py ──→ mcp_server.py ──→ Claude Desktop
  Playwright  ──→ waas.py   ──┘         │              │
                                    Shared filter    20 MCP tools
                                    pipeline         6 JSON stores
                                                     Description cache
```

### Multi-Source Ingestion

- **HN threads**: Algolia search API → parallelized comment fetch (20-worker ThreadPoolExecutor) → HTML comment parsing to extract company, location, remote status, emails, job board URLs
- **WAAS**: Playwright headless browser with authenticated session → infinite-scroll scraping → structured field extraction (title, salary, batch, company size, seniority)
- Both sources feed into the same `filters.py` pipeline — keyword scoring, negative filters, seniority estimation, job type classification, and location detection share a single implementation

### Filter Pipeline (`filters.py`)

Five-stage filter cascade, each with configurable behavior:

1. **Weighted keyword scoring** — Three categories scored by relevance (AI tooling: 3, Systems: 2, General AI+SWE: 1). Scoring is per-category, not per-keyword — multiple hits in one category don't stack. All matching uses compiled `\b` word-boundary regex, case-insensitive.

2. **Negative keyword filter** — Detects senior/management titles (staff, principal, director, VP) and high experience thresholds (10+, 15+ years). Matched posts aren't silently dropped — they go to a "Filtered Out" section so nothing is lost.

3. **Seniority estimation** — Infers seniority from job title keywords and description experience-year requirements. Maps to a level scale (intern → junior → mid → senior → staff+). Configurable max level — jobs above the threshold are filtered. Unknown seniority is never filtered (benefit of the doubt).

4. **Job type classification** — Classifies roles as coding (engineer, developer, SRE, etc.) vs non-coding (PM, designer, sales, recruiter). Engineering management is classified as non-coding. Unknown types are kept.

5. **Location filter** — Regex matching against known US and non-US cities/countries. Non-US, non-remote jobs are filtered. No detected location = kept (benefit of the doubt).

### Bounded Job Tracking System

Six JSON files managed atomically (read-modify-write) by the MCP server:

```
scan_waas ──→ [all new jobs] ──→ backlog_jobs.json (overflow, ranked by score)
                                        │
                                        ▼ (top N promoted)
                                 tracked_jobs.json (max N active, default 20)
                                        │
                          ┌──────────────┼──────────────┐
                          ▼              ▼              ▼
                   applied_jobs    dismissed_jobs  longshot_jobs
                   (permanent)     (validated)     (validated)
```

- **Tracked** — Bounded active pipeline (max N, configurable). Only the highest-scoring jobs from backlog fill these slots. Every `mark_applied`, `mark_dismissed`, or `mark_longshot` call frees a slot and auto-promotes the top backlog entry, returning the promoted job in the response to avoid redundant `get_tracked_jobs` calls.
- **Backlog** — Unbounded overflow, sorted by keyword score. Jobs that pass all filters but don't make the top N cut. Promotion happens automatically when tracked slots open.
- **Applied** — Permanent record. Not validated against WAAS (job might be filled but the application record matters).
- **Dismissed / Longshot** — Validated periodically against WAAS — dead listings are pruned automatically.

### Description Caching

Full job descriptions are cached to disk during scanning (`job_descriptions.json`). `get_job_details` reads from cache first, falls back to live HTTP + BeautifulSoup parsing. Cache is auto-pruned — descriptions are removed when jobs leave all active stores (tracked, backlog, applied, longshot). Applied jobs retain their descriptions permanently.

### MCP Server (20 Tools)

Stdio-transport MCP server for Claude Desktop. Claude Desktop acts as the LLM ranker, eliminating the need for an API key. The server owns all state — Claude never writes to JSON files directly, only through tool calls.

**Scanning**: `scan_jobs`, `scan_waas`, `scan_all`, `get_job_details`
- `scan_waas` returns only run metadata (counts, timing, active filters) — not job data. Job data flows through the tracking system.
- `scan_all` combines HN + WAAS with cross-source dedup by company name (case-insensitive, whitespace-stripped). HN takes priority.
- `get_job_details` serves from disk cache, falls back to live fetch with structured extraction (JSON-LD → Open Graph → meta tags → title tag).

**Tracking**: `get_tracked_jobs`, `get_applied_jobs`, `get_dismissed_jobs`, `get_longshot_jobs`, `update_job_analysis`, `mark_applied`, `mark_dismissed`, `mark_longshot`, `mark_open`, `swap_role`, `validate_tracked_jobs`, `reset_tracking`
- `swap_role` replaces a tracked job with an alternate role URL from the same company (e.g., a better-fit position discovered through `other_roles`), clearing stale analysis.
- `validate_tracked_jobs` checks open/dismissed/longshot jobs against WAAS, removes dead listings, and backfills from backlog.
- `mark_*` tools return the newly promoted backlog job inline, enabling a dismiss-and-analyze loop without re-fetching the full tracked list.

**Config**: `get_resume`, `get_preferences`, `get_config`, `update_config`, `get_latest_results`
- `get_resume` extracts text from a configured PDF via PyMuPDF.
- `update_config` writes to `config.yaml` for runtime filter/preference changes.

### Daily Workflow (Automated via MCP Prompt)

1. `validate_tracked_jobs` — prune dead listings from tracked/dismissed/longshot, backfill from backlog
2. `scan_waas` — discover new jobs, auto-track top N by score, overflow to backlog
3. `get_tracked_jobs` → for each unanalyzed job: `get_job_details` → `update_job_analysis` (or `mark_dismissed`/`mark_longshot` with inline backfill loop)
4. Render tracked/applied/dismissed/longshot into a React artifact

### Deduplication

- **Within-source**: `seen_posts.json` (HN comment IDs), `seen_waas.json` (WAAS job URLs). Auto-pruned after 6 months.
- **Cross-source**: `scan_all` deduplicates by company name (case-insensitive, stripped). HN takes priority since it has richer context.
- **First-run backfill**: Empty seen files trigger a 3-month HN backfill scan.

## Tech Stack

| Component | Technology | Purpose |
|-----------|-----------|---------|
| HN ingestion | `requests` + Algolia API | Parallelized comment fetching (20-worker ThreadPoolExecutor) |
| WAAS scraping | `playwright` | Headless browser with auth, infinite scroll handling |
| HTML parsing | `beautifulsoup4` | Job board scraping (Greenhouse, Lever, Ashby) |
| Resume parsing | `pymupdf` | PDF text extraction |
| Config | `pyyaml` | YAML config with runtime updates |
| LLM integration | `anthropic` (CLI) / MCP stdio (Desktop) | Resume-based job ranking |
| MCP server | `mcp` (FastMCP) | 20-tool stdio server for Claude Desktop |
| Filter pipeline | `re` (compiled word-boundary regex) | Shared across both sources |
| State management | JSON files (atomic read-modify-write) | 6 tracking stores + description cache |

## Setup

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp config.yaml.example config.yaml  # edit with your preferences
```

### Environment Variables (`.env`, gitignored)

| Variable | Purpose |
|----------|---------|
| `ANTHROPIC_API_KEY` | Claude API (CLI ranking mode only) |
| `WAAS_USERNAME` / `WAAS_PASSWORD` | YC account for full WAAS access (~30 jobs without) |
| `HN_JOBS_EMAIL_TO` / `FROM` / `PASSWORD` | Email delivery (Gmail app password) |
| `TRACKING_DIR` | Directory for tracking JSON files (keeps paths out of git) |

### Claude Desktop MCP Config

```json
{
  "mcpServers": {
    "hn-jobs": {
      "type": "stdio",
      "command": "wsl",
      "args": [
        "bash", "-c",
        "set -a; source /path/to/.env; set +a; /path/to/.venv/bin/python3 /path/to/mcp_server.py"
      ]
    }
  }
}
```

## CLI Usage

```bash
python3 hn_jobs.py --dry-run --no-rank   # HTML preview, no ranking
python3 hn_jobs.py --no-email             # Terminal output
python3 hn_jobs.py --dry-run --resume resume.pdf  # Rank against resume
python3 hn_jobs.py                        # Full scan + email delivery
```
