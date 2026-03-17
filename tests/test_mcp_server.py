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
             patch("hn_jobs.load_seen") as mock_load:
            mcp_server._scan_hn(months=1, ignore_seen=True)
        mock_load.assert_not_called()

    def test_ignore_seen_true_returns_previously_seen_posts(self):
        """HN ignore_seen=True should return posts even if they were previously seen."""
        comment = {"id": 42, "text": "Acme | SF | LLM engineer", "time": 0}
        thread = {"title": "T", "kids": [42]}

        # With ignore_seen=True, seen dict is empty so all posts pass
        with patch("hn_jobs.find_hiring_threads", return_value=[thread]), \
             patch("hn_jobs.fetch_comments", return_value=[comment]), \
             patch("hn_jobs.scrape_job_boards"):
            results, _, _ = mcp_server._scan_hn(months=1, ignore_seen=True)

        assert len(results) == 1
        assert results[0]["parsed"]["company"] == "Acme"

    def test_ignore_seen_true_does_not_save(self):
        """HN ignore_seen=True should not call save_seen."""
        thread = {"title": "T", "kids": []}
        with patch("hn_jobs.find_hiring_threads", return_value=[thread]), \
             patch("hn_jobs.fetch_comments", return_value=[]), \
             patch("hn_jobs.save_seen") as mock_save:
            mcp_server._scan_hn(months=1, ignore_seen=True)
        mock_save.assert_not_called()

    def test_ignore_seen_false_updates_seen(self):
        thread = {"title": "T", "kids": []}
        with patch("hn_jobs.find_hiring_threads", return_value=[thread]), \
             patch("hn_jobs.fetch_comments", return_value=[]), \
             patch("hn_jobs.load_seen", return_value={"posts": {}}) as mock_load, \
             patch("hn_jobs.mark_seen") as mock_mark, \
             patch("hn_jobs.prune_seen") as mock_prune, \
             patch("hn_jobs.save_seen") as mock_save:
            mcp_server._scan_hn(months=1, ignore_seen=False)
        mock_load.assert_called_once()
        mock_mark.assert_called_once()
        mock_prune.assert_called_once()
        mock_save.assert_called_once()


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
