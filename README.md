# HN "Who's Hiring" Job Scanner

Scans Hacker News monthly "Who is hiring?" threads, filters posts by keyword categories, optionally ranks results against your resume using Claude, and delivers results via HTML email, local file, or Claude Desktop.

## Sources

This scanner pulls from two sources:

### HN Who's Hiring
- Monthly threads from Hacker News "Ask HN: Who is hiring?"
- Fetched via Algolia search API
- Scans current month + configurable history (1-3 months)

### Work at a Startup
- YC's job board at workatastartup.com
- Scraped using Playwright headless browser
- Always scans current live listings

Both sources use identical keyword filtering and scoring. When using `scan_all`, results are deduplicated by company name with HN listings taking priority.

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### Config

Copy the example and edit:

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
```

### Environment Variables

**For CLI ranking (optional):**
- `ANTHROPIC_API_KEY` — enables Claude-powered job ranking against your resume

**For email delivery (optional):**
- `HN_JOBS_EMAIL_TO`
- `HN_JOBS_EMAIL_FROM`
- `HN_JOBS_EMAIL_PASSWORD` (Gmail app password)

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

### Flags

| Flag | Description |
|------|-------------|
| `--no-email` | Print results to terminal instead of sending email |
| `--dry-run` | Save HTML preview to `results/` directory |
| `--resume PDF` | Path to resume PDF (overrides config.yaml) |
| `--no-rank` | Skip Claude ranking, use keyword score ordering |

## Claude Desktop (MCP Server)

The MCP server lets Claude Desktop scan and rank jobs directly in conversation — no API key needed since Claude Desktop is the ranker.

### Setup

Add to your Claude Desktop config (`claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "hn-jobs": {
      "type": "stdio",
      "command": "wsl",
      "args": [
        "/path/to/project/.venv/bin/python3",
        "/path/to/project/mcp_server.py"
      ]
    }
  }
}
```

If not on WSL, use the Python path directly:

```json
{
  "mcpServers": {
    "hn-jobs": {
      "type": "stdio",
      "command": "/path/to/project/.venv/bin/python3",
      "args": ["/path/to/project/mcp_server.py"]
    }
  }
}
```

Restart Claude Desktop after adding the config.

### Tools

| Tool | Description |
|------|-------------|
| `scan_jobs` | Run the full scan pipeline. Args: `months` (1-3), `ignore_seen` (skip dedup) |
| `scan_waas` | Scan Work at a Startup for matching jobs. Args: `ignore_seen` |
| `scan_all` | Scan both HN and WAAS, deduplicate by company. Args: `months`, `ignore_seen` |
| `get_resume` | Extract and return resume text from configured PDF |
| `get_preferences` | Return job preferences from config.yaml |
| `get_latest_results` | Return most recent saved results without re-scanning |

### Prompts

The MCP server registers prompt templates (`find_jobs`, `rerank_results`, `scan_overview`, `backfill`) that should appear as slash commands in the client. **Note:** As of March 2026, Claude Desktop (claude.ai) does not surface MCP prompts in its UI. The prompts are returned via `prompts/list` but the client ignores them. Use the example prompts below manually instead.

### Example Prompts

**Full scan with ranking:**
> Use get_resume and get_preferences to understand my background, then scan_jobs with ignore_seen=true to find this month's matching positions. Rank every job from best to worst fit and give me your top 15 with a reason for each.

**Re-rank previous results:**
> Get my resume and preferences, then use get_latest_results to load the previous scan. Rank the results for me and give me your top 10.

**Quick scan without ranking:**
> Scan for this month's jobs with ignore_seen=true and summarize what's out there. Group them by category.

**Scan multiple months:**
> Scan the last 3 months of jobs with months=3 and ignore_seen=true. Find anything that's a strong fit for my resume.

**YC startups only:**
> Use scan_waas with ignore_seen=true to find engineering jobs from YC startups. Get my resume and rank the results by fit.

**Targeted search:**
> Scan for jobs, get my resume, and find roles that specifically mention Rust or systems programming. Which ones would be the best fit?

## How It Works

1. **Find threads** — queries Algolia for recent "Who is hiring?" posts by `whoishiring`
2. **Fetch comments** — pulls all top-level comments (parallelized, 20 workers)
3. **Keyword matching** — scores posts by category (AI tooling=3, Systems=2, General AI+SWE=1)
4. **Negative filter** — posts matching senior/management keywords go to "Filtered Out" section
5. **Location filter** — non-US, non-remote jobs are filtered out
6. **Job board scraping** — extracts job titles from Greenhouse, Lever, Ashby links
7. **Claude ranking** (optional) — sends all results + resume to Claude, re-orders by fit
8. **Output** — HTML email, local HTML file, terminal, or Claude Desktop conversation

### First Run

On first run (empty `seen_posts.json`), scans the current month plus 2 prior months. Subsequent runs scan only the latest thread and skip already-seen posts. Use `ignore_seen=true` in the MCP server to bypass this.

## Output

- `results/preview_<timestamp>.html` — browsable HTML (with `--dry-run`)
- `results/results_<timestamp>.json` — structured JSON (always saved)

## Dependencies

- `requests` — HN API + job board scraping
- `pyyaml` — config file loading
- `pymupdf` — PDF resume text extraction
- `anthropic` — Claude API for CLI ranking
- `playwright` — headless browser for WAAS scraping
- `beautifulsoup4` — HTML parsing for WAAS page
- `mcp` — MCP server for Claude Desktop integration
