1. Call `validate_tracked_jobs` to remove expired listings from tracked, dismissed, and longshot files. Auto-promotes from backlog to fill freed tracked slots.
2. Call `scan_waas` (ignore_seen=false) to discover new jobs. The server auto-appends top N to tracked_jobs.json and the rest to backlog_jobs.json. `scan_waas` returns only run metadata (counts) — not job data.
3. Call `get_tracked_jobs` to retrieve the current open jobs.
4. Call `get_resume` and `get_preferences`.
5. For each job where `analysis` is null:
   - Call `get_job_details(job_url)` to read the full posting.
   - Evaluate fit against the resume and preferences. Three outcomes:
     - **Good fit**: Call `update_job_analysis` with the fields below.
     - **Not a fit**: Call `mark_dismissed(job_url)`. The response includes a `next_job` field with the newly promoted backlog job — analyze that one next without calling `get_tracked_jobs` again. Continue this dismiss-and-analyze loop until you find a fit or `next_job` is absent (backlog empty).
     - **Interesting but unlikely** (stretch role, exciting company but weak match): Call `mark_longshot(job_url)`. Same behavior as dismiss — returns `next_job` from backlog. Longshots stay visible in the tracker UI for later review.
   - **Experience requirement guidance**: The user has ~1 year professional experience. Do NOT dismiss a job purely because it asks for 2+ years — that is close enough to be worth a longshot at minimum. Jobs asking 3+ years are a stretch but may still be longshot-worthy if the role/company is compelling. Only dismiss on experience grounds if it asks for 5+ years or explicitly requires senior-level depth.
   - For `update_job_analysis`, provide:
     - **fit_explanation**: Detailed explanation connecting the user's specific resume experience to this role's requirements. Be concrete — reference specific projects, technologies, and accomplishments.
     - **odds**: "low", "medium", or "high" — how likely the user is to get this job given ~1 year professional experience, BS CS Dec 2024, and the specific requirements listed.
     - **odds_reasoning**: Why you picked that level. Be honest.
     - **salary_vs_col**: Compare salary against livable for the job's location. Baselines: ~$75K SoCal, ~$95-110K SF, ~$95-120K NYC, ~$80-90K Seattle. If remote, note user lives in Laguna Niguel, CA with no relocation cost.
6. Call `get_tracked_jobs`, `get_applied_jobs`, `get_dismissed_jobs`, and `get_longshot_jobs`. Create the tracker artifact using the React template from the job-tracker skill found in the job-tracker dir inside C:\Users\nwile\OneDrive\Documents\claude-cowork. Embed all four datasets:
   ```
   const TRACKED = [...]   // from get_tracked_jobs
   const APPLIED = [...]   // from get_applied_jobs
   const DISMISSED = [...] // from get_dismissed_jobs
   const LONGSHOT = [...]  // from get_longshot_jobs
   ```
   The React UI code is identical every run — only the data consts change.

The tracker is read-only (no action buttons). Status changes (apply, dismiss, longshot, reopen) are done via chat — the user tells Claude and Claude calls the MCP tools (`mark_applied`, `mark_dismissed`, `mark_longshot`, `mark_open`).

Do NOT re-analyze jobs that already have analysis. Do NOT re-filter what the server already filtered. Unanalyzed jobs only appear when the server promotes from backlog or appends from a new scan.
