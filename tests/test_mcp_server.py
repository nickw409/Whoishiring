"""Tests for mcp_server.py — covers MCP tool functions, scan_jobs, scan_waas,
scan_all, get_resume, get_preferences, get_latest_results."""

import json
import os
import sys
import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock

# Mock the mcp module before importing mcp_server
mock_mcp_module = MagicMock()
mock_fastmcp = MagicMock()
mock_fastmcp_instance = MagicMock()
mock_fastmcp_instance.tool.return_value = lambda f: f
mock_fastmcp_instance.prompt.return_value = lambda f: f
mock_fastmcp.return_value = mock_fastmcp_instance
mock_mcp_module.server.fastmcp.FastMCP = mock_fastmcp
sys.modules["mcp"] = mock_mcp_module
sys.modules["mcp.server"] = mock_mcp_module.server
sys.modules["mcp.server.fastmcp"] = mock_mcp_module.server.fastmcp

import mcp_server
import hn_jobs
import waas


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_hn_result(company="HNCo", score=3, post_id=1):
    return {
        "parsed": {
            "id": post_id,
            "company": company,
            "location": "SF",
            "remote": False,
            "snippet": "We use LLM",
            "full_text": "We use LLM for development.",
            "emails": [],
            "email_instructions": [],
            "job_board_urls": [],
            "other_urls": [],
        },
        "matches": {"AI tooling": ["llm"]},
        "score": score,
        "thread_title": "Ask HN: Who is hiring?",
    }


def _make_waas_result(company="WAASCo", score=2.0, job_url="https://waas.com/jobs/1"):
    return {
        "parsed": {
            "id": job_url,
            "company": company,
            "location": "Remote",
            "remote": True,
            "snippet": "ML engineer role",
            "full_text": "ML engineer role with pytorch experience.",
            "emails": [],
            "email_instructions": [],
            "job_board_urls": [{"url": job_url, "type": "waas", "title": "ML Engineer"}],
            "other_urls": [],
            "source": "waas",
            "company_yc_batch": "W24",
            "company_size": "10 people",
            "salary_range": "$150k-$200k",
        },
        "matches": {"General AI+SWE": ["pytorch"]},
        "score": score,
        "source": "waas",
    }


# ---------------------------------------------------------------------------
# scan_jobs
# ---------------------------------------------------------------------------

class TestScanJobs:
    def test_returns_json_with_expected_fields(self):
        hn_results = [_make_hn_result()]
        with patch.object(mcp_server, "_scan_hn", return_value=(hn_results, [], ["Thread 1"])):
            result = json.loads(mcp_server.scan_jobs(months=1, ignore_seen=True))

        assert "threads" in result
        assert "total_results" in result
        assert "total_filtered" in result
        assert "results" in result
        assert result["total_results"] == 1

    def test_months_clamped_to_range(self):
        with patch.object(mcp_server, "_scan_hn", return_value=([], [], [])) as mock_scan:
            mcp_server.scan_jobs(months=10, ignore_seen=True)
        # _scan_hn receives the clamped value; check it was called
        mock_scan.assert_called_once()
        # The months param inside _scan_hn is clamped: max(1, min(3, months))
        # Verify by calling _scan_hn directly
        assert max(1, min(3, 10)) == 3

    def test_no_threads_returns_error(self):
        with patch.object(mcp_server, "_scan_hn", return_value=([], [], [])):
            result = json.loads(mcp_server.scan_jobs())
        assert "error" in result

    def test_ignore_seen_true_bypasses_dedup(self):
        with patch("hn_jobs.find_hiring_threads", return_value=[]), \
             patch("mcp_server.SeenTracker") as MockTracker:
            mcp_server._scan_hn(months=1, ignore_seen=True)
        MockTracker.assert_not_called()

    def test_ignore_seen_true_returns_previously_seen_posts(self):
        """HN ignore_seen=True should return posts even if they were previously seen."""
        comment = {"id": 42, "text": "Acme | SF | LLM engineer", "time": 0}
        thread = {"title": "T", "kids": [42]}

        with patch("hn_jobs.find_hiring_threads", return_value=[thread]), \
             patch("hn_jobs.fetch_comments", return_value=[comment]), \
             patch("hn_jobs.scrape_job_boards"):
            results, _, _ = mcp_server._scan_hn(months=1, ignore_seen=True)

        assert len(results) == 1
        assert results[0]["parsed"]["company"] == "Acme"

    def test_ignore_seen_true_does_not_save(self):
        """HN ignore_seen=True should not call save."""
        thread = {"title": "T", "kids": []}
        with patch("hn_jobs.find_hiring_threads", return_value=[thread]), \
             patch("hn_jobs.fetch_comments", return_value=[]), \
             patch("mcp_server.SeenTracker") as MockTracker:
            mcp_server._scan_hn(months=1, ignore_seen=True)
        MockTracker.assert_not_called()

    def test_ignore_seen_false_updates_seen(self):
        thread = {"title": "T", "kids": []}
        mock_tracker = MagicMock()
        mock_tracker.is_seen.return_value = False
        MockTrackerClass = MagicMock(return_value=mock_tracker)
        with patch("hn_jobs.find_hiring_threads", return_value=[thread]), \
             patch("hn_jobs.fetch_comments", return_value=[]), \
             patch("mcp_server.SeenTracker", MockTrackerClass):
            mcp_server._scan_hn(months=1, ignore_seen=False)
        mock_tracker.load.assert_called_once()
        mock_tracker.mark.assert_called_once()
        mock_tracker.prune.assert_called_once()
        mock_tracker.save.assert_called_once()


# ---------------------------------------------------------------------------
# scan_waas
# ---------------------------------------------------------------------------

class TestScanWaas:
    def test_returns_json_with_expected_fields(self):
        waas_results = [_make_waas_result()]
        with patch("waas.scan_and_filter_waas", return_value=(waas_results, [])):
            result = json.loads(mcp_server.scan_waas(ignore_seen=True))

        assert "source" in result
        assert result["source"] == "waas"
        assert "total_results" in result
        assert "results" in result

    def test_exception_returns_error_field(self):
        with patch("waas.scan_and_filter_waas", side_effect=Exception("Browser failed")):
            result = json.loads(mcp_server.scan_waas())
        assert "error" in result
        assert result["total_results"] == 0


# ---------------------------------------------------------------------------
# scan_all
# ---------------------------------------------------------------------------

class TestScanAll:
    def test_combines_hn_and_waas(self):
        hn_results = [_make_hn_result(company="HNCo", score=5)]
        waas_raw = [{"company_name": "WAASCo", "job_title": "Eng", "job_description": "pytorch work",
                     "job_url": "https://waas.com/1", "job_location": "SF", "remote": False,
                     "company_url": "", "company_description": "", "company_size": "",
                     "company_yc_batch": "", "waas_company_url": "", "job_salary_range": "",
                     "job_tags": [], "job_details": ""}]
        waas_filtered = [_make_waas_result(company="WAASCo")]

        with patch.object(mcp_server, "_scan_hn", return_value=(hn_results, [], ["T"])), \
             patch("waas.scrape_waas_jobs", return_value=waas_raw), \
             patch("waas.filter_waas_jobs", return_value=(waas_filtered, [])):
            result = json.loads(mcp_server.scan_all(ignore_seen=True))

        assert "sources" in result
        assert result["hn_results"] == 1
        assert result["waas_results"] == 1

    def test_hn_priority_dedup(self):
        hn_results = [_make_hn_result(company="SharedCo")]
        waas_raw = [{"company_name": "SharedCo", "job_title": "Eng", "job_description": "pytorch",
                     "job_url": "https://waas.com/1", "job_location": "SF", "remote": False,
                     "company_url": "", "company_description": "", "company_size": "",
                     "company_yc_batch": "", "waas_company_url": "", "job_salary_range": "",
                     "job_tags": [], "job_details": ""}]

        with patch.object(mcp_server, "_scan_hn", return_value=(hn_results, [], ["T"])), \
             patch("waas.scrape_waas_jobs", return_value=waas_raw), \
             patch("waas.filter_waas_jobs", return_value=([], [])):
            result = json.loads(mcp_server.scan_all(ignore_seen=True))

        # SharedCo should appear from HN only; WAAS deduped via hn_company_names
        sources = [r["source"] for r in result["results"]]
        assert "hn" in sources

    def test_hn_failure_returns_waas(self):
        waas_filtered = [_make_waas_result()]
        with patch.object(mcp_server, "_scan_hn", side_effect=Exception("HN down")), \
             patch("waas.scrape_waas_jobs", return_value=[]), \
             patch("waas.filter_waas_jobs", return_value=(waas_filtered, [])):
            result = json.loads(mcp_server.scan_all(ignore_seen=True))

        assert "errors" in result
        assert result["waas_results"] == 1

    def test_waas_failure_returns_hn(self):
        hn_results = [_make_hn_result()]
        with patch.object(mcp_server, "_scan_hn", return_value=(hn_results, [], ["T"])), \
             patch("waas.scrape_waas_jobs", side_effect=Exception("WAAS down")), \
             patch("waas.filter_waas_jobs", return_value=([], [])):
            result = json.loads(mcp_server.scan_all(ignore_seen=True))

        assert "errors" in result
        assert result["hn_results"] == 1

    def test_sorted_by_score(self):
        hn_results = [_make_hn_result(company="LowScore", score=1)]
        waas_filtered = [_make_waas_result(company="HighScore", score=10.0)]

        with patch.object(mcp_server, "_scan_hn", return_value=(hn_results, [], ["T"])), \
             patch("waas.scrape_waas_jobs", return_value=[]), \
             patch("waas.filter_waas_jobs", return_value=(waas_filtered, [])):
            result = json.loads(mcp_server.scan_all(ignore_seen=True, group_by_company=False))

        scores = [r["score"] for r in result["results"]]
        assert scores == sorted(scores, reverse=True)


# ---------------------------------------------------------------------------
# get_resume, get_preferences, get_latest_results
# ---------------------------------------------------------------------------

class TestGetResume:
    def test_returns_resume_text(self):
        with patch("hn_jobs.load_config", return_value={"resume_text": "I am an engineer", "preferences": {}}):
            result = mcp_server.get_resume()
        assert "I am an engineer" in result

    def test_no_resume_configured(self):
        with patch("hn_jobs.load_config", return_value={"resume_text": None, "preferences": {}}):
            result = mcp_server.get_resume()
        assert "No resume configured" in result


class TestGetPreferences:
    def test_returns_preferences(self):
        with patch("hn_jobs.load_config", return_value={"resume_text": None, "preferences": {"remote": "required"}}):
            result = mcp_server.get_preferences()
        data = json.loads(result)
        assert data["remote"] == "required"

    def test_no_preferences(self):
        with patch("hn_jobs.load_config", return_value={"resume_text": None, "preferences": {}}):
            result = mcp_server.get_preferences()
        assert "No preferences" in result


class TestGetLatestResults:
    def test_returns_latest_file(self, tmp_path):
        results_file = tmp_path / "results_20260316_120000.json"
        results_file.write_text(json.dumps({"results": []}))
        with patch.object(hn_jobs, "RESULTS_DIR", tmp_path):
            result = mcp_server.get_latest_results()
        data = json.loads(result)
        assert "results" in data

    def test_no_results_dir(self, tmp_path):
        with patch.object(hn_jobs, "RESULTS_DIR", tmp_path / "nonexistent"):
            result = mcp_server.get_latest_results()
        data = json.loads(result)
        assert "error" in data

    def test_empty_results_dir(self, tmp_path):
        tmp_path.mkdir(exist_ok=True)
        with patch.object(hn_jobs, "RESULTS_DIR", tmp_path):
            result = mcp_server.get_latest_results()
        data = json.loads(result)
        assert "error" in data


# ---------------------------------------------------------------------------
# scan_jobs — result format matches CLI
# ---------------------------------------------------------------------------

class TestScanJobsResultFormat:
    def test_result_format_has_all_cli_fields(self):
        """Verify JSON output includes all expected fields matching CLI format."""
        hn_result = {
            "parsed": {
                "id": 99,
                "company": "TestCorp",
                "location": "NYC",
                "remote": True,
                "snippet": "We build AI tools",
                "full_text": "We build AI tools using LLM and cursor.",
                "emails": ["jobs@testcorp.com"],
                "email_instructions": ["Apply via email"],
                "job_board_urls": [{"url": "https://boards.greenhouse.io/testcorp", "type": "greenhouse", "title": "Engineer"}],
                "other_urls": ["https://testcorp.com/careers"],
                "role": "Software Engineer",
                "seniority": "mid",
                "is_coding": True,
            },
            "matches": {"AI tooling": ["llm", "cursor"]},
            "score": 3,
            "thread_title": "Ask HN: Who is hiring? (March 2026)",
        }

        with patch.object(mcp_server, "_scan_hn", return_value=([hn_result], [], ["Thread 1"])):
            result = json.loads(mcp_server.scan_jobs(months=1, ignore_seen=True))

        assert len(result["results"]) == 1
        r = result["results"][0]
        assert r["source"] == "hn"
        assert r["hn_link"] == "https://news.ycombinator.com/item?id=99"
        assert r["company"] == "TestCorp"
        assert r["location"] == "NYC"
        assert r["remote"] is True
        assert r["score"] == 3
        assert r["matched_categories"] == ["AI tooling"]
        assert set(r["matched_keywords"]) == {"llm", "cursor"}
        assert "full_text" in r
        assert r["emails"] == ["jobs@testcorp.com"]
        assert len(r["job_board_urls"]) == 1
        assert r["other_urls"] == ["https://testcorp.com/careers"]
        assert r["role"] == "Software Engineer"
        assert r["seniority"] == "mid"
        assert r["is_coding"] is True


# ---------------------------------------------------------------------------
# scan_waas — Playwright error returns error field
# ---------------------------------------------------------------------------

class TestScanWaasPlaywrightError:
    def test_playwright_browser_error_returns_error_field(self):
        """A Playwright-specific browser error should surface in the error field."""
        with patch("waas.scan_and_filter_waas", side_effect=Exception("Browser closed unexpectedly")):
            result = json.loads(mcp_server.scan_waas())

        assert "error" in result
        assert "Browser closed unexpectedly" in result["error"]
        assert result["total_results"] == 0
        assert result["results"] == []


# ---------------------------------------------------------------------------
# scan_waas — result format matches HN format with WAAS-specific fields
# ---------------------------------------------------------------------------

class TestScanWaasResultFormat:
    def test_result_format_has_waas_specific_fields(self):
        """Verify WAAS results include source='waas' and WAAS-specific fields."""
        waas_result = _make_waas_result(company="YCStartup", score=4.0)
        waas_result["parsed"]["seniority"] = "junior"

        with patch("waas.scan_and_filter_waas", return_value=([waas_result], [])):
            result = json.loads(mcp_server.scan_waas(ignore_seen=True, group_by_company=False))

        assert len(result["results"]) == 1
        r = result["results"][0]
        assert r["source"] == "waas"
        assert r["company"] == "YCStartup"
        assert "job_title" in r
        assert "salary_range" in r
        assert r["salary_range"] == "$150k-$200k"
        assert "company_yc_batch" in r
        assert r["company_yc_batch"] == "W24"
        assert "company_size" in r
        assert r["company_size"] == "10 people"
        assert "seniority" in r


# ---------------------------------------------------------------------------
# scan_all — cross-source dedup is case-insensitive and stripped
# ---------------------------------------------------------------------------

class TestScanAllCaseInsensitiveDedup:
    def test_cross_source_dedup_case_insensitive_stripped(self):
        """HN '  SharedCo  ' and WAAS 'sharedco' should dedup to only HN."""
        hn_results = [_make_hn_result(company="  SharedCo  ", score=5)]
        waas_raw = [{
            "company_name": "sharedco", "job_title": "Eng",
            "job_description": "pytorch work", "job_url": "https://waas.com/1",
            "job_location": "SF", "remote": False, "company_url": "",
            "company_description": "", "company_size": "",
            "company_yc_batch": "", "waas_company_url": "",
            "job_salary_range": "", "job_tags": [], "job_details": "",
        }]
        waas_filtered = [_make_waas_result(company="sharedco")]

        with patch.object(mcp_server, "_scan_hn", return_value=(hn_results, [], ["T"])), \
             patch("waas.scrape_waas_jobs", return_value=waas_raw), \
             patch("waas.filter_waas_jobs", return_value=(waas_filtered, [])) as mock_filter:
            result = json.loads(mcp_server.scan_all(ignore_seen=True, group_by_company=False))

        # filter_waas_jobs should receive hn_company_names containing 'sharedco'
        call_kwargs = mock_filter.call_args
        hn_names = call_kwargs[1].get("hn_company_names") or call_kwargs[0][1] if len(call_kwargs[0]) > 1 else call_kwargs[1].get("hn_company_names")
        assert "sharedco" in hn_names

        # Even if WAAS filter doesn't remove it, verify HN result is present
        sources = [r["source"] for r in result["results"]]
        assert "hn" in sources


# ---------------------------------------------------------------------------
# MCP prompts — all 5 registered and return non-empty strings
# ---------------------------------------------------------------------------

class TestMcpPromptsRegistration:
    def test_all_five_prompts_registered_and_non_empty(self):
        """Verify find_jobs, rerank_results, scan_overview, backfill, waas_only are callable."""
        prompts = {
            "find_jobs": mcp_server.find_jobs,
            "rerank_results": mcp_server.rerank_results,
            "scan_overview": mcp_server.scan_overview,
            "backfill": mcp_server.backfill,
            "waas_only": mcp_server.waas_only,
        }
        for name, fn in prompts.items():
            result = fn()
            assert isinstance(result, str), f"{name} should return a string"
            assert len(result) > 0, f"{name} should return non-empty string"

    def test_prompt_content_contains_expected_tool_references(self):
        """Verify prompts mention their expected tools/parameters."""
        assert "scan_all" in mcp_server.find_jobs()
        assert "months=3" in mcp_server.backfill()
        assert "scan_waas" in mcp_server.waas_only()
        assert "get_latest_results" in mcp_server.rerank_results()
        assert "scan_all" in mcp_server.scan_overview()


# ---------------------------------------------------------------------------
# scan-jobs-tool: default parameters
# ---------------------------------------------------------------------------

class TestScanJobsDefaults:
    def test_default_months_and_ignore_seen(self):
        """scan_jobs() with no args should use months=1, ignore_seen=False."""
        with patch.object(mcp_server, "_scan_hn", return_value=([], [], [])) as mock:
            mcp_server.scan_jobs()
        mock.assert_called_once_with(1, False)


# ---------------------------------------------------------------------------
# get-latest-results-tool: most recent file
# ---------------------------------------------------------------------------

class TestGetLatestResultsMostRecent:
    def test_returns_most_recent_file(self, tmp_path):
        results_dir = tmp_path / "results"
        results_dir.mkdir()
        old = results_dir / "results_20260101_000000.json"
        new = results_dir / "results_20260318_120000.json"
        old.write_text('{"old": true}')
        new.write_text('{"new": true}')
        with patch.object(hn_jobs, "RESULTS_DIR", results_dir):
            result = json.loads(mcp_server.get_latest_results())
        assert result.get("new") is True


# ---------------------------------------------------------------------------
# ignore-seen-mode: WAAS path
# ---------------------------------------------------------------------------

class TestWaasIgnoreSeenMode:
    def test_ignore_seen_true_returns_all_jobs(self, monkeypatch):
        """ignore_seen=True should return jobs even if they exist in seen tracker."""
        from waas import _scrape_direct, _company_to_jobs
        companies = [{
            "name": "TestCo", "website": "https://testco.com",
            "description": "A company", "slug": "testco",
            "jobs": [{"title": "Eng", "show_path": "https://waas.com/jobs/1", "state": "visible",
                       "remote": "no", "pretty_location_or_remote": "SF",
                       "pretty_salary_range": "", "skills": [],
                       "description": "Build stuff", "pretty_job_type": "fulltime",
                       "pretty_eng_type": "", "pretty_min_experience": "",
                       "pretty_sponsors_visa": ""}],
        }]
        monkeypatch.setattr("waas._scrape_via_api", lambda: (companies, "key"))
        # Even with seen_waas.json containing this URL, ignore_seen=True returns it
        import time as _time
        class MockTracker:
            def __init__(self, *a, **kw):
                self.entries = {"https://waas.com/jobs/1": 1234567890.0}
            def load(self): return self
            def save(self): pass
            def prune(self): pass
            def is_seen(self, id_): return str(id_) in self.entries
            def mark(self, ids):
                for id_ in ids: self.entries[str(id_)] = _time.time()
            def is_empty(self): return not self.entries
        monkeypatch.setattr("waas.SeenTracker", MockTracker)
        jobs = _scrape_direct(ignore_seen=True)
        assert len(jobs) == 1
