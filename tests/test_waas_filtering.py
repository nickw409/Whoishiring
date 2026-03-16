"""Tests for WAAS scraper, filtering, and API functions."""

import json
import time
import pytest
from unittest.mock import patch, MagicMock

from waas import (
    _waas_to_parsed,
    _company_to_jobs,
    filter_waas_jobs,
    scan_and_filter_waas,
    scrape_waas_jobs,
    load_waas_seen,
    save_waas_seen,
    mark_waas_seen,
    prune_waas_seen,
    WAAS_PRUNE_DAYS,
    WAAS_BASE_URL,
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


def _make_api_company(**overrides):
    """Create a company dict matching the /companies/fetch API response."""
    base = {
        "name": "TestCo",
        "website": "https://testco.com",
        "description": "A test company building things",
        "one_liner": "We build things",
        "team_size": 15,
        "batch": "W24",
        "slug": "testco",
        "location": "San Francisco, CA",
        "jobs": [{
            "id": 99999,
            "company_id": 1234,
            "state": "visible",
            "title": "Software Engineer",
            "description": "Build systems with Rust and CUDA for high-performance computing",
            "salary_min": 120000,
            "salary_max": 180000,
            "show_path": "https://www.workatastartup.com/jobs/99999",
            "remote": "no",
            "pretty_job_type": "Full-time",
            "pretty_eng_type": "Backend",
            "pretty_location_or_remote": "San Francisco, CA",
            "pretty_salary_range": "$120K - $180K",
            "pretty_min_experience": "3+ years",
            "pretty_sponsors_visa": "Will sponsor visa",
            "skills": [
                {"id": 1, "name": "Rust", "popularity": 50},
                {"id": 2, "name": "CUDA", "popularity": 20},
            ],
        }],
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
# _company_to_jobs
# ---------------------------------------------------------------------------

class TestCompanyToJobs:
    def test_basic_conversion(self):
        co = _make_api_company()
        jobs = _company_to_jobs(co)
        assert len(jobs) == 1
        j = jobs[0]
        assert j["company_name"] == "TestCo"
        assert j["company_url"] == "https://testco.com"
        assert j["company_yc_batch"] == "W24"
        assert j["company_size"] == "15 people"
        assert j["waas_company_url"] == f"{WAAS_BASE_URL}/companies/testco"
        assert j["job_title"] == "Software Engineer"
        assert j["job_salary_range"] == "$120K - $180K"
        assert "Rust" in j["job_tags"]
        assert "CUDA" in j["job_tags"]
        assert "Rust" in j["job_description"]

    def test_skips_non_visible_jobs(self):
        co = _make_api_company()
        co["jobs"][0]["state"] = "hidden"
        jobs = _company_to_jobs(co)
        assert len(jobs) == 0

    def test_remote_only(self):
        co = _make_api_company()
        co["jobs"][0]["remote"] = "only"
        jobs = _company_to_jobs(co)
        assert jobs[0]["remote"] is True

    def test_remote_yes(self):
        co = _make_api_company()
        co["jobs"][0]["remote"] = "yes"
        jobs = _company_to_jobs(co)
        assert jobs[0]["remote"] is True

    def test_remote_no(self):
        co = _make_api_company()
        co["jobs"][0]["remote"] = "no"
        co["jobs"][0]["pretty_location_or_remote"] = "San Francisco, CA"
        jobs = _company_to_jobs(co)
        assert jobs[0]["remote"] is False

    def test_remote_from_location_string(self):
        co = _make_api_company()
        co["jobs"][0]["remote"] = "no"
        co["jobs"][0]["pretty_location_or_remote"] = "SF, CA / Remote (US)"
        jobs = _company_to_jobs(co)
        assert jobs[0]["remote"] is True

    def test_absolute_url_from_show_path(self):
        co = _make_api_company()
        co["jobs"][0]["show_path"] = "/jobs/12345"
        jobs = _company_to_jobs(co)
        assert jobs[0]["job_url"] == f"{WAAS_BASE_URL}/jobs/12345"

    def test_already_absolute_url(self):
        co = _make_api_company()
        co["jobs"][0]["show_path"] = "https://www.workatastartup.com/jobs/12345"
        jobs = _company_to_jobs(co)
        assert jobs[0]["job_url"] == "https://www.workatastartup.com/jobs/12345"

    def test_no_team_size(self):
        co = _make_api_company(team_size=None)
        jobs = _company_to_jobs(co)
        assert jobs[0]["company_size"] == ""

    def test_no_slug(self):
        co = _make_api_company(slug="")
        jobs = _company_to_jobs(co)
        assert jobs[0]["waas_company_url"] == ""

    def test_multiple_jobs(self):
        co = _make_api_company()
        co["jobs"].append({
            "id": 88888,
            "company_id": 1234,
            "state": "visible",
            "title": "Frontend Engineer",
            "description": "Build UIs",
            "show_path": "/jobs/88888",
            "remote": "only",
            "pretty_job_type": "Full-time",
            "pretty_eng_type": "Frontend",
            "pretty_location_or_remote": "Remote",
            "pretty_salary_range": "$100K - $140K",
            "pretty_min_experience": "2+ years",
            "pretty_sponsors_visa": "",
            "skills": [],
        })
        jobs = _company_to_jobs(co)
        assert len(jobs) == 2
        assert jobs[0]["job_title"] == "Software Engineer"
        assert jobs[1]["job_title"] == "Frontend Engineer"

    def test_no_jobs(self):
        co = _make_api_company(jobs=[])
        jobs = _company_to_jobs(co)
        assert jobs == []

    def test_empty_skills(self):
        co = _make_api_company()
        co["jobs"][0]["skills"] = []
        jobs = _company_to_jobs(co)
        assert jobs[0]["job_tags"] == []

    def test_details_string(self):
        co = _make_api_company()
        jobs = _company_to_jobs(co)
        details = jobs[0]["job_details"]
        assert "Full-time" in details
        assert "San Francisco" in details


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
        assert results[0]["score"] == 6

    def test_multiple_negative_keywords(self):
        job = _make_job(
            job_title="Staff Engineer / Engineering Manager",
            job_description="Work on Rust and CUDA systems",
        )
        _, filtered_out = filter_waas_jobs([job])
        assert len(filtered_out) == 1
        assert len(filtered_out[0]["filter_reason"]) >= 2

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
        assert len(results) == 1
        assert len(filtered_out) == 2

    def test_empty_description_skipped(self):
        job = _make_job(job_description="", job_title="Some Role", job_tags=[])
        results, filtered_out = filter_waas_jobs([job])
        assert len(results) == 0
        assert len(filtered_out) == 0

    def test_no_location_not_filtered(self):
        job = _make_job(
            job_location="",
            job_description="Build GPU systems with CUDA",
            remote=False,
        )
        results, _ = filter_waas_jobs([job])
        assert len(results) == 1

    def test_keywords_matched_from_tags(self):
        job = _make_job(
            job_description="Work at our company",
            job_title="Engineer",
            job_tags=["Rust", "CUDA", "Systems Programming"],
        )
        results, _ = filter_waas_jobs([job])
        assert len(results) == 1
        assert "Systems" in results[0]["matches"]


# ---------------------------------------------------------------------------
# Deduplication
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
# scrape_waas_jobs (mocked API)
# ---------------------------------------------------------------------------

class TestScrapeWaasJobs:
    def test_returns_list(self, monkeypatch):
        companies = [_make_api_company()]
        monkeypatch.setattr("waas._scrape_via_api", lambda: (companies, "key"))
        monkeypatch.setattr("waas.load_waas_seen", lambda: {"jobs": {}})
        monkeypatch.setattr("waas.save_waas_seen", lambda s: None)
        jobs = scrape_waas_jobs(ignore_seen=True)
        assert isinstance(jobs, list)
        assert len(jobs) == 1

    def test_all_required_fields(self, monkeypatch):
        companies = [_make_api_company()]
        monkeypatch.setattr("waas._scrape_via_api", lambda: (companies, "key"))
        monkeypatch.setattr("waas.load_waas_seen", lambda: {"jobs": {}})
        monkeypatch.setattr("waas.save_waas_seen", lambda s: None)
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
        companies = [_make_api_company()]
        monkeypatch.setattr("waas._scrape_via_api", lambda: (companies, "key"))
        monkeypatch.setattr("waas.load_waas_seen", lambda: {"jobs": {}})
        monkeypatch.setattr("waas.save_waas_seen", lambda s: None)
        jobs = scrape_waas_jobs(ignore_seen=True)
        for job in jobs:
            assert isinstance(job["remote"], bool)
            assert isinstance(job["job_tags"], list)
            for key in ["company_name", "job_title", "job_url", "job_description"]:
                assert isinstance(job[key], str)

    def test_dedup_filters_seen(self, monkeypatch):
        companies = [_make_api_company()]
        monkeypatch.setattr("waas._scrape_via_api", lambda: (companies, "key"))
        monkeypatch.setattr("waas.load_waas_seen", lambda: {
            "jobs": {"https://www.workatastartup.com/jobs/99999": time.time()}
        })
        monkeypatch.setattr("waas.save_waas_seen", lambda s: None)
        jobs = scrape_waas_jobs(ignore_seen=False)
        assert len(jobs) == 0

    def test_ignore_seen_skips_save(self, monkeypatch):
        companies = [_make_api_company()]
        monkeypatch.setattr("waas._scrape_via_api", lambda: (companies, "key"))
        monkeypatch.setattr("waas.load_waas_seen", lambda: {"jobs": {}})
        save_called = {"count": 0}
        monkeypatch.setattr("waas.save_waas_seen", lambda s: save_called.update(count=save_called["count"] + 1))
        jobs = scrape_waas_jobs(ignore_seen=True)
        assert len(jobs) == 1
        assert save_called["count"] == 0

    def test_api_failure_returns_empty(self, monkeypatch):
        monkeypatch.setattr("waas._scrape_via_api", lambda: ([], ""))
        monkeypatch.setattr("waas.load_waas_seen", lambda: {"jobs": {}})
        jobs = scrape_waas_jobs(ignore_seen=True)
        assert jobs == []

    def test_bad_company_skipped(self, monkeypatch):
        companies = [_make_api_company(), {"bad": "data"}]
        monkeypatch.setattr("waas._scrape_via_api", lambda: (companies, "key"))
        monkeypatch.setattr("waas.load_waas_seen", lambda: {"jobs": {}})
        monkeypatch.setattr("waas.save_waas_seen", lambda s: None)
        jobs = scrape_waas_jobs(ignore_seen=True)
        assert len(jobs) == 1  # bad company skipped, good one kept


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
        import asyncio
        from waas import _is_in_asyncio_loop

        result = None
        async def check():
            nonlocal result
            result = _is_in_asyncio_loop()

        asyncio.run(check())
        assert result is True

    def test_not_in_asyncio_outside_loop(self):
        from waas import _is_in_asyncio_loop
        assert _is_in_asyncio_loop() is False

    def test_subprocess_fallback_in_asyncio(self, monkeypatch):
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

    def test_direct_outside_asyncio(self, monkeypatch):
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
        import subprocess as sp
        from waas import _scrape_via_subprocess

        def mock_run(args, **kwargs):
            raise sp.TimeoutExpired(args, 300)

        monkeypatch.setattr("subprocess.run", mock_run)
        jobs = _scrape_via_subprocess(ignore_seen=True)
        assert jobs == []


# ---------------------------------------------------------------------------
# _scrape_via_api (mocked Playwright)
# ---------------------------------------------------------------------------

class TestScrapeViaApi:
    def test_no_credentials_returns_empty(self, monkeypatch):
        from waas import _scrape_via_api
        monkeypatch.delenv("WAAS_USERNAME", raising=False)
        monkeypatch.delenv("WAAS_PASSWORD", raising=False)
        companies, key = _scrape_via_api()
        assert companies == []
        assert key == ""

    def test_empty_username_returns_empty(self, monkeypatch):
        from waas import _scrape_via_api
        monkeypatch.setenv("WAAS_USERNAME", "")
        monkeypatch.setenv("WAAS_PASSWORD", "secret")
        companies, key = _scrape_via_api()
        assert companies == []

    def test_empty_password_returns_empty(self, monkeypatch):
        from waas import _scrape_via_api
        monkeypatch.setenv("WAAS_USERNAME", "user")
        monkeypatch.setenv("WAAS_PASSWORD", "")
        companies, key = _scrape_via_api()
        assert companies == []
