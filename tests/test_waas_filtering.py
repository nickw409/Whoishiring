"""Tests for WAAS filtering functions."""

import json
import time
import pytest
from unittest.mock import patch, MagicMock

from waas import (
    _waas_to_parsed,
    filter_waas_jobs,
    scan_and_filter_waas,
    scrape_waas_jobs,
    load_waas_seen,
    save_waas_seen,
    mark_waas_seen,
    prune_waas_seen,
    WAAS_PRUNE_DAYS,
)


def _make_job(**overrides):
    """Create a raw WAAS job dict with sensible defaults."""
    base = {
        "company_name": "TestCo",
        "company_url": "https://testco.com",
        "company_description": "A test company",
        "company_size": "10 people",
        "company_yc_batch": "W24",
        "waas_company_url": "https://www.workatastartup.com/companies/testco",
        "job_title": "Software Engineer",
        "job_url": "https://www.workatastartup.com/jobs/99999",
        "job_salary_range": "$120k - $150k",
        "job_location": "San Francisco, CA",
        "job_tags": ["Backend", "Full-time"],
        "job_details": "fulltime | San Francisco, CA | Backend",
        "job_description": "Build stuff with Python and JavaScript",
        "remote": False,
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# _waas_to_parsed
# ---------------------------------------------------------------------------

class TestWaasToParsed:
    def test_format(self):
        job = _make_job(
            company_name="Acme",
            job_title="Engineer",
            job_url="https://waas.com/jobs/123",
            job_description="Build stuff with Rust and CUDA",
            job_location="San Francisco, CA",
            remote=True,
        )
        parsed = _waas_to_parsed(job)
        assert parsed["id"] == "https://waas.com/jobs/123"
        assert parsed["company"] == "Acme"
        assert parsed["location"] == "San Francisco, CA"
        assert parsed["remote"] is True
        assert parsed["source"] == "waas"
        assert parsed["job_board_urls"][0]["type"] == "waas"
        assert parsed["job_board_urls"][0]["title"] == "Engineer"

    def test_missing_required_field(self):
        job = _make_job()
        del job["company_name"]
        with pytest.raises(KeyError):
            _waas_to_parsed(job)

    def test_missing_optional_field(self):
        job = _make_job()
        del job["remote"]
        del job["company_url"]
        parsed = _waas_to_parsed(job)
        assert parsed["remote"] is False
        assert parsed["other_urls"] == []

    def test_snippet_boundary_exact_300(self):
        job = _make_job(job_description="x" * 300)
        parsed = _waas_to_parsed(job)
        assert parsed["snippet"] == "x" * 300
        assert "..." not in parsed["snippet"]

    def test_snippet_short(self):
        job = _make_job(job_description="short desc")
        parsed = _waas_to_parsed(job)
        assert parsed["snippet"] == "short desc"

    def test_snippet_long(self):
        job = _make_job(job_description="x" * 500)
        parsed = _waas_to_parsed(job)
        assert parsed["snippet"] == "x" * 300 + "..."

    def test_all_parsed_keys_present(self):
        parsed = _waas_to_parsed(_make_job())
        expected_keys = {
            "id", "time", "company", "location", "remote", "snippet",
            "full_text", "emails", "email_instructions", "job_board_urls",
            "other_urls", "source", "company_yc_batch", "company_size",
            "salary_range",
        }
        assert set(parsed.keys()) == expected_keys

    def test_empty_company_url_excluded_from_other_urls(self):
        job = _make_job(company_url="")
        parsed = _waas_to_parsed(job)
        assert parsed["other_urls"] == []

    def test_emails_always_empty(self):
        parsed = _waas_to_parsed(_make_job())
        assert parsed["emails"] == []
        assert parsed["email_instructions"] == []


# ---------------------------------------------------------------------------
# filter_waas_jobs
# ---------------------------------------------------------------------------

class TestFilterWaasJobs:
    def test_matches_keywords(self):
        job = _make_job(
            job_description="We use Rust and CUDA for high-performance computing",
            job_title="Systems Engineer",
        )
        results, filtered_out = filter_waas_jobs([job])
        assert len(results) == 1
        assert "Systems" in results[0]["matches"]
        assert results[0]["score"] == 2
        assert results[0]["source"] == "waas"

    def test_negative_keyword(self):
        job = _make_job(
            job_title="Staff Engineer",
            job_description="Work on Rust systems programming",
        )
        results, filtered_out = filter_waas_jobs([job])
        assert len(results) == 0
        assert len(filtered_out) == 1
        assert any("staff engineer" in r for r in filtered_out[0]["filter_reason"])

    def test_non_us_location(self):
        job = _make_job(
            job_location="London, England",
            job_description="Build ML pipelines with PyTorch",
            remote=False,
        )
        results, filtered_out = filter_waas_jobs([job])
        assert len(results) == 0
        assert len(filtered_out) == 1
        assert any("non-US" in r for r in filtered_out[0]["filter_reason"])

    def test_remote_overrides_non_us_location(self):
        job = _make_job(
            job_location="Berlin, Germany",
            job_description="Build GPU-accelerated CUDA kernels",
            remote=True,
        )
        results, filtered_out = filter_waas_jobs([job])
        assert len(results) == 1
        assert len(filtered_out) == 0

    def test_cross_source_dedup(self):
        job = _make_job(company_name="Acme Corp")
        job["job_description"] = "Work with Rust on systems programming"
        results, filtered_out = filter_waas_jobs([job], hn_company_names={"acme corp"})
        assert len(results) == 0
        assert len(filtered_out) == 0

    def test_cross_source_dedup_case_insensitive(self):
        job = _make_job(company_name="ACME CORP  ")
        job["job_description"] = "Work with Rust on systems programming"
        results, filtered_out = filter_waas_jobs([job], hn_company_names={"acme corp"})
        assert len(results) == 0
        assert len(filtered_out) == 0

    def test_no_keyword_match_skipped(self):
        job = _make_job(
            job_title="Insurance Agent",
            job_description="We sell insurance",
            job_tags=[],
        )
        results, filtered_out = filter_waas_jobs([job])
        assert len(results) == 0
        assert len(filtered_out) == 0

    def test_empty_raw_jobs(self):
        results, filtered_out = filter_waas_jobs([])
        assert results == []
        assert filtered_out == []

    def test_missing_job_tags(self):
        job = _make_job(job_description="Build GPU-accelerated CUDA kernels")
        del job["job_tags"]
        results, filtered_out = filter_waas_jobs([job])
        assert len(results) == 1

    def test_none_job_tags(self):
        job = _make_job(
            job_description="Build GPU-accelerated CUDA kernels",
            job_tags=None,
        )
        results, filtered_out = filter_waas_jobs([job])
        assert len(results) == 1

    def test_hn_company_names_none_vs_empty(self):
        job = _make_job(job_description="Work with Rust on systems")
        r1, _ = filter_waas_jobs([job], hn_company_names=None)
        assert len(r1) == 1
        r2, _ = filter_waas_jobs([job], hn_company_names=set())
        assert len(r2) == 1

    def test_filter_reason_not_in_results(self):
        job = _make_job(job_description="Build GPU systems with CUDA")
        results, _ = filter_waas_jobs([job])
        assert len(results) == 1
        assert "filter_reason" not in results[0]

    def test_multiple_categories_scored_correctly(self):
        job = _make_job(
            job_description="Build agentic LLM systems using Rust and CUDA for machine learning",
        )
        results, _ = filter_waas_jobs([job])
        assert len(results) == 1
        assert results[0]["score"] == 6  # AI tooling(3) + Systems(2) + General AI+SWE(1)

    def test_multiple_negative_keywords(self):
        job = _make_job(
            job_title="Staff Engineer / Engineering Manager",
            job_description="Work on Rust and CUDA systems",
        )
        _, filtered_out = filter_waas_jobs([job])
        assert len(filtered_out) == 1
        reasons = filtered_out[0]["filter_reason"]
        assert len(reasons) >= 2

    def test_multiple_jobs_mixed_results(self):
        jobs = [
            _make_job(job_url="https://waas.com/1", job_description="Build with Rust and CUDA"),
            _make_job(job_url="https://waas.com/2", job_description="Sell insurance"),
            _make_job(job_url="https://waas.com/3", job_title="Staff Engineer",
                      job_description="Work on LLM systems"),
            _make_job(job_url="https://waas.com/4", job_description="PyTorch deep learning",
                      job_location="Tokyo, Japan", remote=False),
        ]
        results, filtered_out = filter_waas_jobs(jobs)
        assert len(results) == 1  # only the Rust/CUDA one
        assert len(filtered_out) == 2  # staff engineer + Japan location
        # Insurance one is silently skipped (no keyword match)

    def test_empty_description_skipped(self):
        job = _make_job(job_description="", job_title="Some Role", job_tags=[])
        results, filtered_out = filter_waas_jobs([job])
        assert len(results) == 0
        assert len(filtered_out) == 0

    def test_no_location_not_filtered(self):
        """Jobs with empty location get benefit of the doubt."""
        job = _make_job(
            job_location="",
            job_description="Build GPU systems with CUDA",
            remote=False,
        )
        results, _ = filter_waas_jobs([job])
        assert len(results) == 1

    def test_keywords_matched_from_tags(self):
        """Tags contribute to keyword matching."""
        job = _make_job(
            job_description="Work at our company",
            job_title="Engineer",
            job_tags=["Rust", "CUDA", "Systems Programming"],
        )
        results, _ = filter_waas_jobs([job])
        assert len(results) == 1
        assert "Systems" in results[0]["matches"]


# ---------------------------------------------------------------------------
# Deduplication functions
# ---------------------------------------------------------------------------

class TestDedup:
    def test_load_seen_missing_file(self, tmp_path, monkeypatch):
        monkeypatch.setattr("waas.WAAS_SEEN_FILE", tmp_path / "nonexistent.json")
        seen = load_waas_seen()
        assert seen == {"jobs": {}}

    def test_load_seen_corrupt_json(self, tmp_path, monkeypatch):
        bad_file = tmp_path / "seen_waas.json"
        bad_file.write_text("not valid json{{{")
        monkeypatch.setattr("waas.WAAS_SEEN_FILE", bad_file)
        seen = load_waas_seen()
        assert seen == {"jobs": {}}

    def test_load_seen_missing_jobs_key(self, tmp_path, monkeypatch):
        bad_file = tmp_path / "seen_waas.json"
        bad_file.write_text('{"other": 123}')
        monkeypatch.setattr("waas.WAAS_SEEN_FILE", bad_file)
        seen = load_waas_seen()
        assert seen == {"jobs": {}}

    def test_load_seen_valid(self, tmp_path, monkeypatch):
        f = tmp_path / "seen_waas.json"
        data = {"jobs": {"https://waas.com/jobs/1": 1234567890.0}}
        f.write_text(json.dumps(data))
        monkeypatch.setattr("waas.WAAS_SEEN_FILE", f)
        seen = load_waas_seen()
        assert "https://waas.com/jobs/1" in seen["jobs"]

    def test_save_and_load_roundtrip(self, tmp_path, monkeypatch):
        f = tmp_path / "seen_waas.json"
        monkeypatch.setattr("waas.WAAS_SEEN_FILE", f)
        original = {"jobs": {"https://waas.com/1": 100.0, "https://waas.com/2": 200.0}}
        save_waas_seen(original)
        loaded = load_waas_seen()
        assert loaded == original

    def test_mark_seen_empty_list(self):
        seen = mark_waas_seen({"jobs": {}}, [])
        assert seen == {"jobs": {}}

    def test_mark_seen_adds_urls(self):
        seen = mark_waas_seen({"jobs": {}}, ["https://waas.com/1", "https://waas.com/2"])
        assert "https://waas.com/1" in seen["jobs"]
        assert "https://waas.com/2" in seen["jobs"]
        assert isinstance(seen["jobs"]["https://waas.com/1"], float)

    def test_mark_seen_preserves_existing(self):
        seen = {"jobs": {"https://waas.com/old": 100.0}}
        seen = mark_waas_seen(seen, ["https://waas.com/new"])
        assert "https://waas.com/old" in seen["jobs"]
        assert "https://waas.com/new" in seen["jobs"]

    def test_prune_removes_old_entries(self):
        old_ts = time.time() - (WAAS_PRUNE_DAYS + 10) * 86400
        new_ts = time.time() - 10
        seen = {"jobs": {"old_url": old_ts, "new_url": new_ts}}
        pruned = prune_waas_seen(seen)
        assert "old_url" not in pruned["jobs"]
        assert "new_url" in pruned["jobs"]

    def test_prune_handles_invalid_timestamps(self):
        seen = {"jobs": {"bad": "not_a_number", "good": time.time()}}
        pruned = prune_waas_seen(seen)
        assert "bad" not in pruned["jobs"]
        assert "good" in pruned["jobs"]

    def test_prune_empty(self):
        seen = {"jobs": {}}
        pruned = prune_waas_seen(seen)
        assert pruned == {"jobs": {}}


# ---------------------------------------------------------------------------
# scrape_waas_jobs (mocked browser)
# ---------------------------------------------------------------------------

LISTING_HTML = """
<html><body>
<div class="mb-2 flex w-full rounded-md border border-gray-200 bg-beige-lighter p-2">
  <a href="/companies/acme" target="company"></a>
  <div class="company-details text-lg">
    <span class="font-bold">Acme Corp (W24)</span>
    <span class="text-gray-600">AI-powered widgets</span>
  </div>
  <div class="job-name">
    <a href="/jobs/100" data-jobid="100">ML Engineer</a>
  </div>
  <p class="job-details">
    <span>fulltime</span>
    <span>San Francisco, CA</span>
    <span>Backend</span>
  </p>
</div>
<div class="mb-2 flex w-full rounded-md border border-gray-200 bg-beige-lighter p-2">
  <a href="/companies/beta" target="company"></a>
  <div class="company-details text-lg">
    <span class="font-bold">Beta Inc (S23)</span>
    <span class="text-gray-600">Cloud infrastructure</span>
  </div>
  <div class="job-name">
    <a href="/jobs/200" data-jobid="200">Senior Rust Developer</a>
  </div>
  <p class="job-details">
    <span>fulltime</span>
    <span>Remote</span>
    <span>Systems</span>
  </p>
</div>
</body></html>
"""

JOB_PAGE_HTML = """
<html><body>
<div class="prose">Company description here</div>
<div class="prose">Skills: Rust, Python, CUDA
We are building a high-performance machine learning platform.
Looking for engineers who love LLM tooling and agentic systems.</div>
<div class="prose">Rust, Python</div>
<div class="my-2 flex flex-wrap">
  <span>San Francisco, CA</span>
  <span>Full-time</span>
  <span>3+ years</span>
</div>
</body></html>
"""


def _make_mock_page(listing_html=LISTING_HTML, job_html=JOB_PAGE_HTML):
    """Create a mock Playwright page that returns canned HTML."""
    page = MagicMock()
    page.evaluate.return_value = 1000  # scrollHeight never changes (no infinite scroll)

    call_count = {"goto": 0}
    def goto_side_effect(url, **kwargs):
        call_count["goto"] += 1

    page.goto.side_effect = goto_side_effect

    def content_side_effect():
        # First call = listing page, subsequent = job pages
        if call_count["goto"] <= 1:
            return listing_html
        return job_html

    page.content.side_effect = content_side_effect
    page.wait_for_selector.return_value = True
    page.wait_for_timeout.return_value = None
    return page


class TestScrapeWaasJobs:
    def _patch_browser(self, monkeypatch, page=None):
        if page is None:
            page = _make_mock_page()
        mock_browser = MagicMock()
        mock_browser.new_page.return_value = page
        mock_pw = MagicMock()

        def fake_create():
            return mock_pw, mock_browser

        monkeypatch.setattr("waas._create_browser", fake_create)
        monkeypatch.setattr("waas.WAAS_SEEN_FILE", MagicMock(exists=lambda: False))
        monkeypatch.setattr("waas.load_waas_seen", lambda: {"jobs": {}})
        monkeypatch.setattr("waas.save_waas_seen", lambda s: None)
        return mock_pw, mock_browser, page

    def test_returns_list(self, monkeypatch):
        self._patch_browser(monkeypatch)
        jobs = scrape_waas_jobs(ignore_seen=True)
        assert isinstance(jobs, list)

    def test_parses_company_name(self, monkeypatch):
        self._patch_browser(monkeypatch)
        jobs = scrape_waas_jobs(ignore_seen=True)
        assert len(jobs) == 2
        assert jobs[0]["company_name"] == "Acme Corp"
        assert jobs[1]["company_name"] == "Beta Inc"

    def test_extracts_yc_batch(self, monkeypatch):
        self._patch_browser(monkeypatch)
        jobs = scrape_waas_jobs(ignore_seen=True)
        assert jobs[0]["company_yc_batch"] == "W24"
        assert jobs[1]["company_yc_batch"] == "S23"

    def test_builds_absolute_urls(self, monkeypatch):
        self._patch_browser(monkeypatch)
        jobs = scrape_waas_jobs(ignore_seen=True)
        assert jobs[0]["job_url"] == "https://www.workatastartup.com/jobs/100"
        assert jobs[0]["waas_company_url"] == "https://www.workatastartup.com/companies/acme"

    def test_detects_remote(self, monkeypatch):
        self._patch_browser(monkeypatch)
        jobs = scrape_waas_jobs(ignore_seen=True)
        assert jobs[0]["remote"] is False
        assert jobs[1]["remote"] is True

    def test_extracts_location(self, monkeypatch):
        self._patch_browser(monkeypatch)
        jobs = scrape_waas_jobs(ignore_seen=True)
        assert jobs[0]["job_location"] == "San Francisco, CA"

    def test_fetches_full_description(self, monkeypatch):
        self._patch_browser(monkeypatch)
        jobs = scrape_waas_jobs(ignore_seen=True)
        # Job page prose[1] should be used as description
        assert "high-performance machine learning" in jobs[0]["job_description"]

    def test_all_required_fields_present(self, monkeypatch):
        self._patch_browser(monkeypatch)
        jobs = scrape_waas_jobs(ignore_seen=True)
        required = {
            "company_name", "company_url", "company_description", "company_size",
            "company_yc_batch", "waas_company_url", "job_title", "job_url",
            "job_salary_range", "job_location", "job_tags", "job_details",
            "job_description", "remote",
        }
        for job in jobs:
            assert set(job.keys()) == required

    def test_field_types(self, monkeypatch):
        self._patch_browser(monkeypatch)
        jobs = scrape_waas_jobs(ignore_seen=True)
        for job in jobs:
            assert isinstance(job["remote"], bool)
            assert isinstance(job["job_tags"], list)
            for key in ["company_name", "job_title", "job_url", "job_description"]:
                assert isinstance(job[key], str)

    def test_browser_cleanup_on_error(self, monkeypatch):
        mock_pw = MagicMock()
        mock_browser = MagicMock()
        mock_browser.new_page.side_effect = RuntimeError("browser crashed")

        monkeypatch.setattr("waas._create_browser", lambda: (mock_pw, mock_browser))
        monkeypatch.setattr("waas.load_waas_seen", lambda: {"jobs": {}})

        jobs = scrape_waas_jobs(ignore_seen=True)
        assert jobs == []
        mock_browser.close.assert_called_once()
        mock_pw.stop.assert_called_once()

    def test_browser_cleanup_on_navigation_error(self, monkeypatch):
        page = MagicMock()
        page.goto.side_effect = TimeoutError("navigation timeout")
        mock_browser = MagicMock()
        mock_browser.new_page.return_value = page
        mock_pw = MagicMock()

        monkeypatch.setattr("waas._create_browser", lambda: (mock_pw, mock_browser))
        monkeypatch.setattr("waas.load_waas_seen", lambda: {"jobs": {}})

        jobs = scrape_waas_jobs(ignore_seen=True)
        assert jobs == []
        mock_browser.close.assert_called_once()
        mock_pw.stop.assert_called_once()

    def test_job_page_failure_keeps_listing_data(self, monkeypatch):
        """If fetching individual job pages fails, keep the listing-page description."""
        page = MagicMock()
        page.evaluate.return_value = 1000
        call_count = {"n": 0}

        def goto_effect(url, **kwargs):
            call_count["n"] += 1
            if call_count["n"] > 1:
                raise TimeoutError("job page timeout")

        page.goto.side_effect = goto_effect
        page.content.return_value = LISTING_HTML
        page.wait_for_selector.return_value = True
        page.wait_for_timeout.return_value = None

        self._patch_browser(monkeypatch, page=page)
        jobs = scrape_waas_jobs(ignore_seen=True)
        assert len(jobs) == 2
        # Falls back to company description from listing
        assert jobs[0]["job_description"] == "AI-powered widgets"

    def test_dedup_filters_seen_jobs(self, monkeypatch):
        page = _make_mock_page()
        mock_browser = MagicMock()
        mock_browser.new_page.return_value = page
        mock_pw = MagicMock()

        saved = {}

        monkeypatch.setattr("waas._create_browser", lambda: (mock_pw, mock_browser))
        monkeypatch.setattr("waas.load_waas_seen", lambda: {
            "jobs": {"https://www.workatastartup.com/jobs/100": time.time()}
        })
        monkeypatch.setattr("waas.save_waas_seen", lambda s: saved.update(s))

        jobs = scrape_waas_jobs(ignore_seen=False)
        # Job 100 was already seen, only job 200 should remain
        assert len(jobs) == 1
        assert jobs[0]["job_url"] == "https://www.workatastartup.com/jobs/200"

    def test_ignore_seen_skips_save(self, monkeypatch):
        page = _make_mock_page()
        mock_browser = MagicMock()
        mock_browser.new_page.return_value = page
        mock_pw = MagicMock()

        save_called = {"count": 0}

        def mock_save(s):
            save_called["count"] += 1

        monkeypatch.setattr("waas._create_browser", lambda: (mock_pw, mock_browser))
        monkeypatch.setattr("waas.load_waas_seen", lambda: {"jobs": {}})
        monkeypatch.setattr("waas.save_waas_seen", mock_save)

        jobs = scrape_waas_jobs(ignore_seen=True)
        assert len(jobs) == 2
        assert save_called["count"] == 0

    def test_no_parallel_browsers(self, monkeypatch):
        """Verify only one browser is created per scrape call."""
        create_count = {"n": 0}
        page = _make_mock_page()
        mock_browser = MagicMock()
        mock_browser.new_page.return_value = page
        mock_pw = MagicMock()

        def counting_create():
            create_count["n"] += 1
            return mock_pw, mock_browser

        monkeypatch.setattr("waas._create_browser", counting_create)
        monkeypatch.setattr("waas.load_waas_seen", lambda: {"jobs": {}})
        monkeypatch.setattr("waas.save_waas_seen", lambda s: None)

        scrape_waas_jobs(ignore_seen=True)
        assert create_count["n"] == 1
        mock_browser.new_page.assert_called_once()

    def test_politeness_delay_between_job_pages(self, monkeypatch):
        """Verify wait_for_timeout is called between job page fetches."""
        page = _make_mock_page()
        self._patch_browser(monkeypatch, page=page)
        scrape_waas_jobs(ignore_seen=True)

        # wait_for_timeout is called for scrolling (1500ms) + per job page (500ms)
        timeout_calls = [c.args[0] for c in page.wait_for_timeout.call_args_list]
        # Should have 500ms delays for job page fetches
        assert 500 in timeout_calls

    def test_empty_page_returns_empty(self, monkeypatch):
        page = _make_mock_page(listing_html="<html><body></body></html>")
        self._patch_browser(monkeypatch, page=page)
        jobs = scrape_waas_jobs(ignore_seen=True)
        assert jobs == []

    def test_card_missing_job_link_skipped(self, monkeypatch):
        html = """
        <html><body>
        <div class="mb-2 flex w-full rounded-md border border-gray-200 bg-beige-lighter p-2">
          <div class="company-details"><span class="font-bold">NoJob Co (W24)</span></div>
          <div class="job-name"></div>
        </div>
        </body></html>
        """
        page = _make_mock_page(listing_html=html)
        self._patch_browser(monkeypatch, page=page)
        jobs = scrape_waas_jobs(ignore_seen=True)
        assert jobs == []


# ---------------------------------------------------------------------------
# scan_and_filter_waas (mocked scraper)
# ---------------------------------------------------------------------------

class TestScanAndFilterWaas:
    def test_end_to_end(self, monkeypatch):
        raw = [
            _make_job(job_url="https://waas.com/1", job_description="Build with Rust and CUDA"),
            _make_job(job_url="https://waas.com/2", job_description="Sell insurance"),
        ]
        monkeypatch.setattr("waas.scrape_waas_jobs", lambda ignore_seen=False: raw)
        results, filtered_out = scan_and_filter_waas(ignore_seen=True)
        assert len(results) == 1
        assert results[0]["parsed"]["company"] == "TestCo"

    def test_passes_ignore_seen(self, monkeypatch):
        captured = {}

        def mock_scrape(ignore_seen=False):
            captured["ignore_seen"] = ignore_seen
            return []

        monkeypatch.setattr("waas.scrape_waas_jobs", mock_scrape)
        scan_and_filter_waas(ignore_seen=True)
        assert captured["ignore_seen"] is True

    def test_passes_hn_company_names(self, monkeypatch):
        raw = [_make_job(company_name="Dupe Co", job_description="Build CUDA systems")]
        monkeypatch.setattr("waas.scrape_waas_jobs", lambda **kw: raw)
        results, _ = scan_and_filter_waas(hn_company_names={"dupe co"})
        assert len(results) == 0

    def test_scraper_exception_propagates(self, monkeypatch):
        monkeypatch.setattr("waas.scrape_waas_jobs", lambda **kw: (_ for _ in ()).throw(RuntimeError("boom")))
        with pytest.raises(RuntimeError):
            scan_and_filter_waas()


# ---------------------------------------------------------------------------
# Asyncio / subprocess fallback
# ---------------------------------------------------------------------------

class TestAsyncioFallback:
    def test_detects_asyncio_loop(self):
        """_is_in_asyncio_loop returns True inside a running loop."""
        import asyncio
        from waas import _is_in_asyncio_loop

        result = None
        async def check():
            nonlocal result
            result = _is_in_asyncio_loop()

        asyncio.run(check())
        assert result is True

    def test_not_in_asyncio_outside_loop(self):
        """_is_in_asyncio_loop returns False when no loop is running."""
        from waas import _is_in_asyncio_loop
        assert _is_in_asyncio_loop() is False

    def test_subprocess_fallback_in_asyncio(self, monkeypatch):
        """scrape_waas_jobs uses subprocess when inside asyncio loop."""
        import asyncio
        from waas import scrape_waas_jobs

        called = {"subprocess": False, "direct": False}

        def mock_subprocess(ignore_seen):
            called["subprocess"] = True
            return [{"mock": True}]

        def mock_direct(ignore_seen):
            called["direct"] = True
            return [{"mock": True}]

        monkeypatch.setattr("waas._scrape_via_subprocess", mock_subprocess)
        monkeypatch.setattr("waas._scrape_direct", mock_direct)

        async def run():
            return scrape_waas_jobs(ignore_seen=True)

        result = asyncio.run(run())
        assert called["subprocess"] is True
        assert called["direct"] is False
        assert result == [{"mock": True}]

    def test_direct_outside_asyncio(self, monkeypatch):
        """scrape_waas_jobs uses direct path when not in asyncio."""
        from waas import scrape_waas_jobs

        called = {"subprocess": False, "direct": False}

        def mock_subprocess(ignore_seen):
            called["subprocess"] = True
            return []

        def mock_direct(ignore_seen):
            called["direct"] = True
            return [{"mock": True}]

        monkeypatch.setattr("waas._scrape_via_subprocess", mock_subprocess)
        monkeypatch.setattr("waas._scrape_direct", mock_direct)

        result = scrape_waas_jobs(ignore_seen=True)
        assert called["direct"] is True
        assert called["subprocess"] is False

    def test_subprocess_passes_ignore_seen_flag(self, monkeypatch):
        """--ignore-seen flag is passed to subprocess."""
        import subprocess as sp
        from waas import _scrape_via_subprocess

        captured_args = {}

        def mock_run(args, **kwargs):
            captured_args["args"] = args
            result = MagicMock()
            result.returncode = 0
            result.stdout = "[]"
            result.stderr = ""
            return result

        monkeypatch.setattr("subprocess.run", mock_run)
        _scrape_via_subprocess(ignore_seen=True)
        assert "--ignore-seen" in captured_args["args"]

    def test_subprocess_omits_flag_when_false(self, monkeypatch):
        """--ignore-seen flag is not passed when ignore_seen=False."""
        import subprocess as sp
        from waas import _scrape_via_subprocess

        captured_args = {}

        def mock_run(args, **kwargs):
            captured_args["args"] = args
            result = MagicMock()
            result.returncode = 0
            result.stdout = "[]"
            result.stderr = ""
            return result

        monkeypatch.setattr("subprocess.run", mock_run)
        _scrape_via_subprocess(ignore_seen=False)
        assert "--ignore-seen" not in captured_args["args"]

    def test_subprocess_returns_parsed_json(self, monkeypatch):
        """Subprocess output is parsed as JSON."""
        from waas import _scrape_via_subprocess

        def mock_run(args, **kwargs):
            result = MagicMock()
            result.returncode = 0
            result.stdout = '[{"company_name": "Test", "job_title": "Eng"}]'
            result.stderr = ""
            return result

        monkeypatch.setattr("subprocess.run", mock_run)
        jobs = _scrape_via_subprocess(ignore_seen=True)
        assert len(jobs) == 1
        assert jobs[0]["company_name"] == "Test"

    def test_subprocess_failure_returns_empty(self, monkeypatch):
        """Subprocess failure returns empty list."""
        from waas import _scrape_via_subprocess

        def mock_run(args, **kwargs):
            result = MagicMock()
            result.returncode = 1
            result.stdout = ""
            result.stderr = "crash"
            return result

        monkeypatch.setattr("subprocess.run", mock_run)
        jobs = _scrape_via_subprocess(ignore_seen=True)
        assert jobs == []

    def test_subprocess_invalid_json_returns_empty(self, monkeypatch):
        """Subprocess returning invalid JSON returns empty list."""
        from waas import _scrape_via_subprocess

        def mock_run(args, **kwargs):
            result = MagicMock()
            result.returncode = 0
            result.stdout = "not json{{"
            result.stderr = ""
            return result

        monkeypatch.setattr("subprocess.run", mock_run)
        jobs = _scrape_via_subprocess(ignore_seen=True)
        assert jobs == []

    def test_subprocess_timeout_returns_empty(self, monkeypatch):
        """Subprocess timeout returns empty list."""
        import subprocess as sp
        from waas import _scrape_via_subprocess

        def mock_run(args, **kwargs):
            raise sp.TimeoutExpired(args, 300)

        monkeypatch.setattr("subprocess.run", mock_run)
        jobs = _scrape_via_subprocess(ignore_seen=True)
        assert jobs == []
