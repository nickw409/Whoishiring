"""Tests for hn_jobs.py — covers thread discovery, keyword matching, scoring,
filtering, comment parsing, job board scraping, dedup, Claude ranking, and output."""

import json
import os
import time
import pytest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch, MagicMock, call

import hn_jobs
from filters import SeenTracker


def _mock_tracker_class(entries=None):
    """Create a mock SeenTracker class that returns a tracker with given entries."""
    class MockSeenTracker:
        def __init__(self, *args, **kwargs):
            self.entries = dict(entries) if entries else {}
            self._saved = False
        def load(self): return self
        def save(self): self._saved = True
        def prune(self): pass
        def is_seen(self, id_): return str(id_) in self.entries
        def mark(self, ids):
            now = time.time()
            for id_ in ids:
                self.entries[str(id_)] = now
        def is_empty(self): return len(self.entries) == 0
    return MockSeenTracker


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_result(company="TestCo", location="San Francisco, CA", remote=False,
                 score=3, matches=None, neg_matches=None, claude_rank=None,
                 claude_reason=None, post_id=1):
    """Create a result dict matching the pipeline format."""
    parsed = {
        "id": post_id,
        "time": 1700000000,
        "company": company,
        "location": location,
        "remote": remote,
        "snippet": "We are building cool stuff with AI tooling and rust...",
        "full_text": "We are building cool stuff with AI tooling and rust systems programming.",
        "emails": ["jobs@testco.com"],
        "email_instructions": [{"email": "jobs@testco.com", "context": "Apply to jobs@testco.com"}],
        "job_board_urls": [{"url": "https://boards.greenhouse.io/testco/123", "type": "greenhouse", "title": "Engineer"}],
        "other_urls": ["https://testco.com/careers"],
    }
    if matches is None:
        matches = {"AI tooling": ["ai tools"]}
    item = {"parsed": parsed, "matches": matches, "score": score}
    if neg_matches:
        item["neg_matches"] = neg_matches
    if claude_rank is not None:
        item["claude_rank"] = claude_rank
    if claude_reason is not None:
        item["claude_reason"] = claude_reason
    return item


# ---------------------------------------------------------------------------
# Thread Discovery
# ---------------------------------------------------------------------------

class TestFindHiringThreads:
    def test_returns_matching_threads(self):
        algolia_resp = MagicMock()
        algolia_resp.json.return_value = {
            "hits": [
                {"title": "Ask HN: Who is hiring? (March 2026)", "objectID": "111"},
            ]
        }
        algolia_resp.raise_for_status = MagicMock()

        firebase_resp = MagicMock()
        firebase_resp.json.return_value = {"id": 111, "title": "Ask HN: Who is hiring? (March 2026)", "kids": []}
        firebase_resp.raise_for_status = MagicMock()

        with patch("hn_jobs.requests.get") as mock_get:
            mock_get.side_effect = [algolia_resp, firebase_resp]
            threads = hn_jobs.find_hiring_threads(max_threads=1)

        assert len(threads) == 1
        assert threads[0]["id"] == 111

    def test_filters_non_matching_titles(self):
        algolia_resp = MagicMock()
        algolia_resp.json.return_value = {
            "hits": [
                {"title": "Ask HN: Who wants to be hired?", "objectID": "222"},
            ]
        }
        algolia_resp.raise_for_status = MagicMock()

        with patch("hn_jobs.requests.get", return_value=algolia_resp):
            threads = hn_jobs.find_hiring_threads(max_threads=1)

        assert threads == []

    def test_algolia_failure_returns_empty(self):
        import requests
        with patch("hn_jobs.requests.get", side_effect=requests.RequestException("timeout")):
            threads = hn_jobs.find_hiring_threads()

        assert threads == []

    def test_max_threads_controls_hits_per_page(self):
        algolia_resp = MagicMock()
        algolia_resp.json.return_value = {"hits": []}
        algolia_resp.raise_for_status = MagicMock()

        with patch("hn_jobs.requests.get", return_value=algolia_resp) as mock_get:
            hn_jobs.find_hiring_threads(max_threads=3)

        params = mock_get.call_args[1]["params"]
        assert params["hitsPerPage"] == 3


class TestHnGet:
    def test_success_first_attempt(self):
        resp = MagicMock()
        resp.json.return_value = {"id": 1, "text": "hello"}
        resp.raise_for_status = MagicMock()

        with patch("hn_jobs.requests.get", return_value=resp):
            result = hn_jobs.hn_get("item/1")

        assert result == {"id": 1, "text": "hello"}

    def test_retries_on_failure(self):
        import requests
        resp_ok = MagicMock()
        resp_ok.json.return_value = {"id": 1}
        resp_ok.raise_for_status = MagicMock()

        with patch("hn_jobs.requests.get", side_effect=[
            requests.RequestException("fail"),
            requests.RequestException("fail"),
            resp_ok,
        ]), patch("hn_jobs.time.sleep"):
            result = hn_jobs.hn_get("item/1", retries=3)

        assert result == {"id": 1}

    def test_returns_none_after_all_retries(self):
        import requests
        with patch("hn_jobs.requests.get", side_effect=requests.RequestException("fail")), \
             patch("hn_jobs.time.sleep"):
            result = hn_jobs.hn_get("item/1", retries=3)

        assert result is None


class TestFetchComments:
    def test_fetches_all_kids(self):
        thread = {"kids": [1, 2, 3]}

        def mock_fetch_one(cid):
            return {"id": cid, "type": "comment", "text": f"comment {cid}"}

        with patch("hn_jobs._fetch_one_comment", side_effect=mock_fetch_one):
            comments = hn_jobs.fetch_comments(thread)

        assert len(comments) == 3
        ids = {c["id"] for c in comments}
        assert ids == {1, 2, 3}

    def test_uses_20_workers(self):
        thread = {"kids": [1]}
        with patch("hn_jobs._fetch_one_comment", return_value={"id": 1}), \
             patch("hn_jobs.ThreadPoolExecutor", wraps=hn_jobs.ThreadPoolExecutor) as mock_pool:
            hn_jobs.fetch_comments(thread)
        mock_pool.assert_called_once_with(max_workers=20)

    def test_skips_deleted_comments(self):
        with patch("hn_jobs.hn_get", return_value={"id": 1, "type": "comment", "deleted": True}):
            result = hn_jobs._fetch_one_comment(1)
        assert result is None

    def test_skips_dead_comments(self):
        with patch("hn_jobs.hn_get", return_value={"id": 1, "type": "comment", "dead": True}):
            result = hn_jobs._fetch_one_comment(1)
        assert result is None

    def test_skips_non_comment_types(self):
        with patch("hn_jobs.hn_get", return_value={"id": 1, "type": "story"}):
            result = hn_jobs._fetch_one_comment(1)
        assert result is None

    def test_empty_kids(self):
        thread = {"kids": []}
        comments = hn_jobs.fetch_comments(thread)
        assert comments == []

    def test_no_kids_key(self):
        thread = {}
        comments = hn_jobs.fetch_comments(thread)
        assert comments == []


class TestFirstRunBackfill:
    def test_is_first_run_empty_seen(self):
        tracker = SeenTracker("/dev/null", "posts")
        assert tracker.is_empty() is True

    def test_is_not_first_run(self):
        tracker = SeenTracker("/dev/null", "posts")
        tracker.mark(["123"])
        assert tracker.is_empty() is False

    def test_is_first_run_missing_posts_key(self):
        tracker = SeenTracker("/dev/null", "posts")
        assert tracker.is_empty() is True


# ---------------------------------------------------------------------------
# Keyword Matching & Scoring
# ---------------------------------------------------------------------------

class TestMatchKeywords:
    def test_matches_ai_tooling(self):
        matches = hn_jobs.match_keywords("We use claude code and copilot for development")
        assert "AI tooling" in matches
        assert "claude code" in matches["AI tooling"]
        assert "copilot" in matches["AI tooling"]

    def test_matches_systems(self):
        matches = hn_jobs.match_keywords("Building high-performance GPU systems with CUDA")
        assert "Systems" in matches
        assert "cuda" in matches["Systems"]
        assert "gpu" in matches["Systems"]

    def test_matches_general_ai(self):
        matches = hn_jobs.match_keywords("ML engineer working with pytorch deep learning")
        assert "General AI+SWE" in matches
        assert "pytorch" in matches["General AI+SWE"]

    def test_case_insensitive(self):
        matches = hn_jobs.match_keywords("We use COPILOT and LLM tools")
        assert "AI tooling" in matches

    def test_word_boundary(self):
        # "rust" shouldn't match "trustworthy"
        matches = hn_jobs.match_keywords("We value trustworthy engineers")
        assert "Systems" not in matches

    def test_no_matches(self):
        matches = hn_jobs.match_keywords("We need a marketing manager")
        assert matches == {}

    def test_multiple_categories(self):
        matches = hn_jobs.match_keywords("Build LLM systems with rust and pytorch")
        assert "AI tooling" in matches
        assert "Systems" in matches
        assert "General AI+SWE" in matches


class TestScoreMatches:
    def test_single_category_ai_tooling(self):
        matches = {"AI tooling": ["copilot", "llm"]}
        assert hn_jobs.score_matches(matches) == 3  # per-category, not per-keyword

    def test_single_category_systems(self):
        matches = {"Systems": ["rust"]}
        assert hn_jobs.score_matches(matches) == 2

    def test_all_categories(self):
        matches = {
            "AI tooling": ["llm"],
            "Systems": ["rust"],
            "General AI+SWE": ["pytorch"],
        }
        assert hn_jobs.score_matches(matches) == 6  # 3+2+1

    def test_empty_matches(self):
        assert hn_jobs.score_matches({}) == 0


class TestMatchNegative:
    def test_matches_negative_keywords(self):
        result = hn_jobs.match_negative("Looking for a staff engineer with 10+ years experience")
        assert "staff engineer" in result
        assert "10+ years" in result

    def test_no_negative_matches(self):
        result = hn_jobs.match_negative("Junior software engineer position")
        assert result == []

    def test_case_insensitive(self):
        result = hn_jobs.match_negative("Engineering Manager role")
        assert "engineering manager" in result

    def test_word_boundary(self):
        # "director of" should match but "directory" shouldn't trigger it
        result = hn_jobs.match_negative("Director of Engineering")
        assert "director of" in result


# ---------------------------------------------------------------------------
# Location Filtering
# ---------------------------------------------------------------------------

class TestIsOutsideUs:
    def test_remote_always_passes(self):
        assert hn_jobs.is_outside_us({"remote": True, "location": "London"}) is False

    def test_us_city_passes(self):
        assert hn_jobs.is_outside_us({"remote": False, "location": "San Francisco, CA"}) is False

    def test_non_us_city_fails(self):
        assert hn_jobs.is_outside_us({"remote": False, "location": "London, United Kingdom"}) is True

    def test_empty_location_passes(self):
        assert hn_jobs.is_outside_us({"remote": False, "location": ""}) is False

    def test_no_location_passes(self):
        assert hn_jobs.is_outside_us({"remote": False, "location": None}) is False

    def test_us_state_abbreviation(self):
        assert hn_jobs.is_outside_us({"remote": False, "location": "Austin, TX"}) is False

    def test_europe_keyword(self):
        assert hn_jobs.is_outside_us({"remote": False, "location": "Europe"}) is True

    def test_non_us_country(self):
        assert hn_jobs.is_outside_us({"remote": False, "location": "Berlin, Germany"}) is True


# ---------------------------------------------------------------------------
# Comment Parsing
# ---------------------------------------------------------------------------

class TestStripHtml:
    def test_strips_p_tags(self):
        assert "hello\n\nworld" == hn_jobs.strip_html("hello<p>world")

    def test_strips_br_tags(self):
        assert "hello\nworld" == hn_jobs.strip_html("hello<br>world")
        assert "hello\nworld" == hn_jobs.strip_html("hello<br/>world")

    def test_strips_other_tags(self):
        assert "click here" == hn_jobs.strip_html('<a href="http://x.com">click here</a>')

    def test_decodes_entities(self):
        assert "A & B" == hn_jobs.strip_html("A &amp; B")


class TestExtractUrls:
    def test_extracts_href_urls(self):
        html = '<a href="https://example.com/jobs">Apply</a>'
        urls = hn_jobs.extract_urls(html)
        assert "https://example.com/jobs" in urls

    def test_extracts_plain_text_urls(self):
        html = "Apply at https://example.com/careers please"
        urls = hn_jobs.extract_urls(html)
        assert "https://example.com/careers" in urls

    def test_decodes_html_entities_in_href(self):
        html = '<a href="https://example.com/jobs?a=1&amp;b=2">Apply</a>'
        urls = hn_jobs.extract_urls(html)
        assert "https://example.com/jobs?a=1&b=2" in urls


class TestExtractEmails:
    def test_extracts_emails(self):
        text = "Send resume to jobs@example.com or hr@company.io"
        emails = hn_jobs.extract_emails(text)
        assert "jobs@example.com" in emails
        assert "hr@company.io" in emails

    def test_no_emails(self):
        assert hn_jobs.extract_emails("No email here") == []


class TestExtractEmailInstructions:
    def test_captures_context(self):
        text = "Please apply by emailing jobs@example.com with your resume attached"
        instructions = hn_jobs.extract_email_instructions(text)
        assert len(instructions) == 1
        assert instructions[0]["email"] == "jobs@example.com"
        assert "resume" in instructions[0]["context"]


class TestClassifyUrl:
    def test_greenhouse(self):
        assert hn_jobs.classify_url("https://boards.greenhouse.io/company/123") == "greenhouse"

    def test_lever(self):
        assert hn_jobs.classify_url("https://jobs.lever.co/company/abc") == "lever"

    def test_ashby(self):
        assert hn_jobs.classify_url("https://jobs.ashbyhq.com/company") == "ashby"

    def test_other(self):
        assert hn_jobs.classify_url("https://company.com/careers") == "other"


class TestParseComment:
    def test_parses_pipe_delimited_header(self):
        comment = {
            "id": 12345,
            "time": 1700000000,
            "text": "Acme Corp | San Francisco, CA | Remote | Full-time<p>We are hiring engineers.",
        }
        parsed = hn_jobs.parse_comment(comment)
        assert parsed["company"] == "Acme Corp"
        assert parsed["remote"] is True
        assert "San Francisco" in parsed["location"]

    def test_snippet_truncation(self):
        long_text = "x" * 500
        comment = {"id": 1, "time": 0, "text": long_text}
        parsed = hn_jobs.parse_comment(comment)
        assert len(parsed["snippet"]) <= 303  # 300 + "..."
        assert parsed["snippet"].endswith("...")

    def test_snippet_no_truncation(self):
        short_text = "Short post"
        comment = {"id": 1, "time": 0, "text": short_text}
        parsed = hn_jobs.parse_comment(comment)
        assert parsed["snippet"] == short_text
        assert not parsed["snippet"].endswith("...")

    def test_separates_job_board_and_other_urls(self):
        comment = {
            "id": 1, "time": 0,
            "text": 'Apply at <a href="https://boards.greenhouse.io/co/123">here</a> or <a href="https://company.com">website</a>',
        }
        parsed = hn_jobs.parse_comment(comment)
        assert len(parsed["job_board_urls"]) == 1
        assert parsed["job_board_urls"][0]["type"] == "greenhouse"
        assert "https://company.com" in parsed["other_urls"]


# ---------------------------------------------------------------------------
# Job Board Scraping
# ---------------------------------------------------------------------------

class TestScrapeJobTitle:
    def test_json_ld_extraction(self):
        page = '<script type="application/ld+json">{"title": "Senior Engineer"}</script>'
        resp = MagicMock()
        resp.text = page
        resp.raise_for_status = MagicMock()
        with patch("hn_jobs.requests.get", return_value=resp):
            assert hn_jobs.scrape_job_title("https://boards.greenhouse.io/co/1") == "Senior Engineer"

    def test_json_ld_name_field(self):
        page = '<script type="application/ld+json">{"name": "Backend Dev"}</script>'
        resp = MagicMock()
        resp.text = page
        resp.raise_for_status = MagicMock()
        with patch("hn_jobs.requests.get", return_value=resp):
            assert hn_jobs.scrape_job_title("https://boards.greenhouse.io/co/1") == "Backend Dev"

    def test_json_ld_list_wrapped(self):
        page = '<script type="application/ld+json">[{"title": "ML Engineer"}]</script>'
        resp = MagicMock()
        resp.text = page
        resp.raise_for_status = MagicMock()
        with patch("hn_jobs.requests.get", return_value=resp):
            assert hn_jobs.scrape_job_title("https://boards.greenhouse.io/co/1") == "ML Engineer"

    def test_og_title_extraction(self):
        page = '<meta property="og:title" content="Frontend Engineer">'
        resp = MagicMock()
        resp.text = page
        resp.raise_for_status = MagicMock()
        with patch("hn_jobs.requests.get", return_value=resp):
            assert hn_jobs.scrape_job_title("https://lever.co/co/1") == "Frontend Engineer"

    def test_og_title_reversed_attributes(self):
        page = '<meta content="DevOps Lead" property="og:title">'
        resp = MagicMock()
        resp.text = page
        resp.raise_for_status = MagicMock()
        with patch("hn_jobs.requests.get", return_value=resp):
            assert hn_jobs.scrape_job_title("https://lever.co/co/1") == "DevOps Lead"

    def test_meta_title_extraction(self):
        page = '<meta name="title" content="SRE Position">'
        resp = MagicMock()
        resp.text = page
        resp.raise_for_status = MagicMock()
        with patch("hn_jobs.requests.get", return_value=resp):
            assert hn_jobs.scrape_job_title("https://lever.co/co/1") == "SRE Position"

    def test_html_title_fallback(self):
        page = "<title>Platform Engineer at Startup</title>"
        resp = MagicMock()
        resp.text = page
        resp.raise_for_status = MagicMock()
        with patch("hn_jobs.requests.get", return_value=resp):
            assert hn_jobs.scrape_job_title("https://lever.co/co/1") == "Platform Engineer at Startup"

    def test_cascade_priority(self):
        # JSON-LD should win over OG
        page = '''<script type="application/ld+json">{"title": "From JSON-LD"}</script>
        <meta property="og:title" content="From OG">
        <title>From Title</title>'''
        resp = MagicMock()
        resp.text = page
        resp.raise_for_status = MagicMock()
        with patch("hn_jobs.requests.get", return_value=resp):
            assert hn_jobs.scrape_job_title("https://lever.co/co/1") == "From JSON-LD"

    def test_http_error_returns_none(self):
        import requests
        with patch("hn_jobs.requests.get", side_effect=requests.RequestException("500")):
            assert hn_jobs.scrape_job_title("https://lever.co/co/1") is None

    def test_long_title_skipped(self):
        page = f'<meta property="og:title" content="{"x" * 250}">'
        resp = MagicMock()
        resp.text = page
        resp.raise_for_status = MagicMock()
        # OG title too long, no other source => None
        with patch("hn_jobs.requests.get", return_value=resp):
            assert hn_jobs.scrape_job_title("https://lever.co/co/1") is None

    def test_html_entity_decoding(self):
        page = '<meta property="og:title" content="Engineer &amp; Lead">'
        resp = MagicMock()
        resp.text = page
        resp.raise_for_status = MagicMock()
        with patch("hn_jobs.requests.get", return_value=resp):
            assert hn_jobs.scrape_job_title("https://lever.co/co/1") == "Engineer & Lead"


class TestScrapeJobBoards:
    def test_politeness_delay(self):
        parsed = {
            "job_board_urls": [
                {"url": "https://boards.greenhouse.io/co/1", "type": "greenhouse", "title": None},
                {"url": "https://boards.greenhouse.io/co/2", "type": "greenhouse", "title": None},
            ]
        }
        with patch("hn_jobs.scrape_job_title", return_value="Engineer"), \
             patch("hn_jobs.time.sleep") as mock_sleep:
            hn_jobs.scrape_job_boards(parsed)

        assert mock_sleep.call_count == 2
        mock_sleep.assert_called_with(0.5)

    def test_user_agent_header(self):
        resp = MagicMock()
        resp.text = "<title>Job</title>"
        resp.raise_for_status = MagicMock()
        with patch("hn_jobs.requests.get", return_value=resp) as mock_get:
            hn_jobs.scrape_job_title("https://boards.greenhouse.io/co/1")

        headers = mock_get.call_args[1]["headers"]
        assert "HNJobScanner" in headers["User-Agent"]


# ---------------------------------------------------------------------------
# Deduplication (HN)
# ---------------------------------------------------------------------------

class TestHnDedup:
    def test_load_empty(self, tmp_path):
        tracker = SeenTracker(tmp_path / "seen.json", "posts")
        tracker.load()
        assert tracker.entries == {}

    def test_load_existing(self, tmp_path):
        f = tmp_path / "seen.json"
        f.write_text(json.dumps({"posts": {"123": 1234567890.0}}))
        tracker = SeenTracker(f, "posts")
        tracker.load()
        assert "123" in tracker.entries

    def test_load_corrupt_json(self, tmp_path):
        f = tmp_path / "seen.json"
        f.write_text("not json{{{")
        tracker = SeenTracker(f, "posts")
        tracker.load()
        assert tracker.entries == {}

    def test_save(self, tmp_path):
        f = tmp_path / "seen.json"
        tracker = SeenTracker(f, "posts")
        tracker.mark(["456"])
        tracker.save()
        data = json.loads(f.read_text())
        assert "456" in data["posts"]

    def test_mark(self):
        tracker = SeenTracker("/dev/null", "posts")
        with patch("filters.time.time", return_value=1000.0):
            tracker.mark(["100", "200"])
        assert "100" in tracker.entries
        assert "200" in tracker.entries
        assert tracker.entries["100"] == 1000.0

    def test_prune_removes_old(self):
        now = time.time()
        old = now - (200 * 86400)
        recent = now - (10 * 86400)
        tracker = SeenTracker("/dev/null", "posts")
        tracker._data["posts"] = {"old": old, "recent": recent}
        tracker.prune()
        assert "old" not in tracker.entries
        assert "recent" in tracker.entries

    def test_prune_keeps_all_recent(self):
        now = time.time()
        tracker = SeenTracker("/dev/null", "posts")
        tracker._data["posts"] = {"a": now, "b": now - 86400}
        tracker.prune()
        assert len(tracker.entries) == 2


# ---------------------------------------------------------------------------
# Claude Ranking
# ---------------------------------------------------------------------------

class TestBuildRankingPrompt:
    def test_includes_resume_and_jobs(self):
        results = [_make_result()]
        prompt = hn_jobs.build_ranking_prompt(results, "I am a software engineer", {"remote": "preferred"})
        assert "I am a software engineer" in prompt
        assert "Remote preference: preferred" in prompt
        assert "TestCo" in prompt

    def test_no_preferences(self):
        results = [_make_result()]
        prompt = hn_jobs.build_ranking_prompt(results, "resume text", {})
        assert "No specific preferences" in prompt


class TestRankJobsWithClaude:
    @pytest.fixture(autouse=True)
    def _mock_anthropic(self):
        """Mock the anthropic module in sys.modules so 'import anthropic' works."""
        self.mock_anthropic = MagicMock()
        import sys
        sys.modules["anthropic"] = self.mock_anthropic
        yield
        sys.modules.pop("anthropic", None)

    def test_no_api_key_returns_original(self):
        results = [_make_result()]
        with patch.dict(os.environ, {}, clear=True):
            os.environ.pop("ANTHROPIC_API_KEY", None)
            ranked = hn_jobs.rank_jobs_with_claude(results, "resume", {})
        assert ranked is results

    def test_successful_ranking(self):
        results = [_make_result(post_id=1), _make_result(post_id=2, company="OtherCo")]
        mock_response = MagicMock()
        mock_response.content = [MagicMock(text=json.dumps([
            {"index": 1, "reason": "Better fit"},
            {"index": 0, "reason": "Good match"},
        ]))]
        self.mock_anthropic.Anthropic.return_value.messages.create.return_value = mock_response

        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"}):
            ranked = hn_jobs.rank_jobs_with_claude(results, "resume", {})

        assert ranked[0]["claude_rank"] == 1
        assert ranked[0]["claude_reason"] == "Better fit"
        assert ranked[1]["claude_rank"] == 2

    def test_api_error_returns_original(self):
        results = [_make_result()]
        self.mock_anthropic.Anthropic.return_value.messages.create.side_effect = Exception("API error")

        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"}):
            ranked = hn_jobs.rank_jobs_with_claude(results, "resume", {})

        assert ranked is results

    def test_invalid_json_returns_original(self):
        results = [_make_result()]
        mock_response = MagicMock()
        mock_response.content = [MagicMock(text="not json")]
        self.mock_anthropic.Anthropic.return_value.messages.create.return_value = mock_response

        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"}):
            ranked = hn_jobs.rank_jobs_with_claude(results, "resume", {})

        assert ranked is results

    def test_code_fence_stripping(self):
        results = [_make_result(post_id=1)]
        mock_response = MagicMock()
        mock_response.content = [MagicMock(text='```json\n[{"index": 0, "reason": "Good"}]\n```')]
        self.mock_anthropic.Anthropic.return_value.messages.create.return_value = mock_response

        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"}):
            ranked = hn_jobs.rank_jobs_with_claude(results, "resume", {})

        assert ranked[0]["claude_rank"] == 1

    def test_missing_jobs_appended(self):
        results = [_make_result(post_id=1), _make_result(post_id=2, company="B")]
        mock_response = MagicMock()
        mock_response.content = [MagicMock(text=json.dumps([
            {"index": 0, "reason": "Best"},
        ]))]
        self.mock_anthropic.Anthropic.return_value.messages.create.return_value = mock_response

        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"}):
            ranked = hn_jobs.rank_jobs_with_claude(results, "resume", {})

        assert len(ranked) == 2
        assert ranked[0]["claude_rank"] == 1
        assert "claude_rank" not in ranked[1]


# ---------------------------------------------------------------------------
# Resume Extraction
# ---------------------------------------------------------------------------

class TestExtractResumeText:
    def test_extracts_text(self, tmp_path):
        mock_page = MagicMock()
        mock_page.get_text.return_value = "  John Doe - Software Engineer  "
        mock_doc = MagicMock()
        mock_doc.__iter__ = MagicMock(return_value=iter([mock_page]))

        pdf_path = tmp_path / "resume.pdf"
        pdf_path.touch()

        with patch("hn_jobs.fitz.open", return_value=mock_doc):
            result = hn_jobs.extract_resume_text(str(pdf_path))

        assert result == "John Doe - Software Engineer"

    def test_missing_file_exits(self):
        with pytest.raises(SystemExit):
            hn_jobs.extract_resume_text("/nonexistent/resume.pdf")


class TestLoadConfig:
    def test_no_config_file(self, tmp_path):
        with patch.object(hn_jobs, "CONFIG_FILE", tmp_path / "nonexistent.yaml"):
            config = hn_jobs.load_config()
        assert config["resume_text"] is None
        assert config["preferences"] == {}

    def test_with_resume_override(self, tmp_path):
        with patch.object(hn_jobs, "CONFIG_FILE", tmp_path / "nonexistent.yaml"), \
             patch("hn_jobs.extract_resume_text", return_value="resume content"):
            config = hn_jobs.load_config(resume_override="/some/resume.pdf")
        assert config["resume_text"] == "resume content"

    def test_with_config_yaml(self, tmp_path):
        import yaml
        config_file = tmp_path / "config.yaml"
        config_file.write_text(yaml.dump({"preferences": {"remote": "preferred", "notes": "I like AI"}}))

        with patch.object(hn_jobs, "CONFIG_FILE", config_file):
            config = hn_jobs.load_config()
        assert config["preferences"]["remote"] == "preferred"
        assert config["resume_text"] is None


# ---------------------------------------------------------------------------
# HTML Output
# ---------------------------------------------------------------------------

class TestHighlightKeywords:
    def test_wraps_keywords_with_mark(self):
        result = hn_jobs.highlight_keywords("We use copilot and LLM tools", ["copilot", "llm"])
        assert "<mark" in result
        assert "copilot" in result
        assert "LLM" in result

    def test_case_insensitive_highlight(self):
        result = hn_jobs.highlight_keywords("COPILOT is great", ["copilot"])
        assert "<mark" in result


class TestFormatPostHtml:
    def test_rank_badge_present(self):
        result = _make_result()
        html = hn_jobs.format_post_html(result["parsed"], result["matches"], claude_rank=1, claude_reason="Great fit")
        assert "#1" in html
        assert "Great fit" in html

    def test_rank_badge_absent(self):
        result = _make_result()
        html = hn_jobs.format_post_html(result["parsed"], result["matches"])
        assert "#" not in html or "#{" not in html  # no rank badge

    def test_filtered_out_shows_blocked_keywords(self):
        result = _make_result()
        html = hn_jobs.format_post_html(result["parsed"], result["matches"], neg_matches=["staff engineer"])
        assert "staff engineer" in html
        assert "Blocked by" in html


class TestBuildEmailHtml:
    def test_includes_thread_titles(self):
        html = hn_jobs.build_email_html([], [], ["Ask HN: Who is hiring? (March 2026)"])
        assert "March 2026" in html

    def test_includes_result_cards(self):
        results = [_make_result()]
        html = hn_jobs.build_email_html(results, [], ["Thread"])
        assert "TestCo" in html

    def test_includes_filtered_out_section(self):
        filtered = [_make_result(neg_matches=["staff engineer"])]
        html = hn_jobs.build_email_html([], filtered, ["Thread"])
        assert "Filtered Out" in html

    def test_no_results_message(self):
        html = hn_jobs.build_email_html([], [], ["Thread"])
        assert "No matching posts found" in html


class TestSendEmail:
    def test_sends_via_smtp(self):
        mock_server = MagicMock()
        with patch("hn_jobs.smtplib.SMTP_SSL") as mock_smtp:
            mock_smtp.return_value.__enter__ = MagicMock(return_value=mock_server)
            mock_smtp.return_value.__exit__ = MagicMock(return_value=False)
            hn_jobs.send_email("<html>body</html>", "to@x.com", "from@x.com", "pass")

        mock_server.login.assert_called_once_with("from@x.com", "pass")
        mock_server.sendmail.assert_called_once()
        # Subject should contain month/year (may be MIME-encoded)
        sent_msg = mock_server.sendmail.call_args[0][2]
        month_year = datetime.now().strftime("%B_%Y")  # e.g. "March_2026"
        # Check for both plain and MIME-encoded variants
        assert "March" in sent_msg and "2026" in sent_msg


# ---------------------------------------------------------------------------
# Terminal Output
# ---------------------------------------------------------------------------

class TestPrintResults:
    def test_prints_results(self, capsys):
        results = [_make_result(claude_rank=1, claude_reason="Best match")]
        hn_jobs.print_results(results, [])
        output = capsys.readouterr().out
        assert "TestCo" in output
        assert "#1" in output
        assert "Best match" in output

    def test_prints_filtered_out(self, capsys):
        filtered = [_make_result(neg_matches=["staff engineer"])]
        hn_jobs.print_results([_make_result()], filtered)
        output = capsys.readouterr().out
        assert "Filtered Out" in output
        assert "staff engineer" in output

    def test_no_results_message(self, capsys):
        hn_jobs.print_results([], [])
        output = capsys.readouterr().out
        assert "No matching posts" in output


# ---------------------------------------------------------------------------
# JSON Results Save & Dry Run (integration via main)
# ---------------------------------------------------------------------------

class TestDryRunAndJsonSave:
    def test_dry_run_saves_html(self, tmp_path):
        with patch.object(hn_jobs, "RESULTS_DIR", tmp_path), \
             patch("hn_jobs.load_config", return_value={"resume_text": None, "preferences": {}, "filters": {"max_seniority": None, "coding_only": False}}), \
             patch("hn_jobs.SeenTracker", _mock_tracker_class({"1": 999})), \
             patch("hn_jobs.find_hiring_threads", return_value=[{"title": "T", "kids": [10]}]), \
             patch("hn_jobs.fetch_comments", return_value=[{"id": 10, "text": "Acme | SF | LLM engineer needed", "time": 0}]), \
             patch("hn_jobs.scrape_job_boards"), \
             patch("sys.argv", ["hn_jobs.py", "--dry-run"]):
            hn_jobs.main()

        html_files = list(tmp_path.glob("preview_*.html"))
        assert len(html_files) == 1

        json_files = list(tmp_path.glob("results_*.json"))
        assert len(json_files) == 1
        data = json.loads(json_files[0].read_text())
        assert "results" in data
        assert "threads" in data
        assert "filtered_out_count" in data


# ---------------------------------------------------------------------------
# Environment Variables
# ---------------------------------------------------------------------------

class TestEnvironmentVariables:
    def test_missing_email_creds_exits(self):
        with patch.dict(os.environ, {}, clear=True), \
             patch("hn_jobs.load_config", return_value={"resume_text": None, "preferences": {}, "filters": {"max_seniority": None, "coding_only": False}}), \
             patch("sys.argv", ["hn_jobs.py"]), \
             pytest.raises(SystemExit):
            # Remove email env vars
            os.environ.pop("HN_JOBS_EMAIL_TO", None)
            os.environ.pop("HN_JOBS_EMAIL_FROM", None)
            os.environ.pop("HN_JOBS_EMAIL_PASSWORD", None)
            hn_jobs.main()


# ---------------------------------------------------------------------------
# First-run backfill integration
# ---------------------------------------------------------------------------

class TestFirstRunBackfillIntegration:
    def test_first_run_uses_3_threads(self, tmp_path):
        with patch.object(hn_jobs, "RESULTS_DIR", tmp_path), \
             patch.object(hn_jobs, "SEEN_FILE", tmp_path / "seen.json"), \
             patch("hn_jobs.find_hiring_threads", return_value=[]) as mock_find, \
             patch("hn_jobs.load_config", return_value={"resume_text": None, "preferences": {}, "filters": {"max_seniority": None, "coding_only": False}}), \
             patch("sys.argv", ["hn_jobs.py", "--dry-run"]), \
             pytest.raises(SystemExit):
            hn_jobs.main()

        mock_find.assert_called_once_with(max_threads=3)

    def test_subsequent_run_uses_1_thread(self, tmp_path):
        seen_file = tmp_path / "seen.json"
        seen_file.write_text(json.dumps({"posts": {"999": time.time()}}))

        with patch.object(hn_jobs, "RESULTS_DIR", tmp_path), \
             patch.object(hn_jobs, "SEEN_FILE", seen_file), \
             patch("hn_jobs.find_hiring_threads", return_value=[]) as mock_find, \
             patch("hn_jobs.load_config", return_value={"resume_text": None, "preferences": {}, "filters": {"max_seniority": None, "coding_only": False}}), \
             patch("sys.argv", ["hn_jobs.py", "--dry-run"]), \
             pytest.raises(SystemExit):
            hn_jobs.main()

        mock_find.assert_called_once_with(max_threads=1)


# ---------------------------------------------------------------------------
# Negative keyword filter integration
# ---------------------------------------------------------------------------

class TestNegativeFilterIntegration:
    def test_positive_and_negative_goes_to_filtered_out(self, tmp_path):
        # Post matches LLM (positive) and "staff engineer" (negative)
        comment = {"id": 50, "text": "Acme | SF | LLM staff engineer position", "time": 0}
        with patch.object(hn_jobs, "RESULTS_DIR", tmp_path), \
             patch("hn_jobs.load_config", return_value={"resume_text": None, "preferences": {}, "filters": {"max_seniority": None, "coding_only": False}}), \
             patch("hn_jobs.SeenTracker", _mock_tracker_class()), \
             patch("hn_jobs.find_hiring_threads", return_value=[{"title": "T", "kids": [50]}]), \
             patch("hn_jobs.fetch_comments", return_value=[comment]), \
             patch("hn_jobs.scrape_job_boards"), \
             patch("sys.argv", ["hn_jobs.py", "--dry-run"]):
            hn_jobs.main()

        json_files = list(tmp_path.glob("results_*.json"))
        data = json.loads(json_files[0].read_text())
        assert data["filtered_out_count"] == 1
        assert len(data["results"]) == 0

    def test_only_negative_discarded_entirely(self, tmp_path):
        # Post matches only "staff engineer" (negative), no positive keywords
        comment = {"id": 60, "text": "Acme | SF | Staff Engineer role no AI here", "time": 0}
        with patch.object(hn_jobs, "RESULTS_DIR", tmp_path), \
             patch("hn_jobs.load_config", return_value={"resume_text": None, "preferences": {}, "filters": {"max_seniority": None, "coding_only": False}}), \
             patch("hn_jobs.SeenTracker", _mock_tracker_class()), \
             patch("hn_jobs.find_hiring_threads", return_value=[{"title": "T", "kids": [60]}]), \
             patch("hn_jobs.fetch_comments", return_value=[comment]), \
             patch("hn_jobs.scrape_job_boards"), \
             patch("sys.argv", ["hn_jobs.py", "--dry-run"]):
            hn_jobs.main()

        json_files = list(tmp_path.glob("results_*.json"))
        data = json.loads(json_files[0].read_text())
        # No positive match -> silently discarded, not in filtered_out
        assert data["filtered_out_count"] == 0
        assert len(data["results"]) == 0


# ---------------------------------------------------------------------------
# Additional parse_comment tests
# ---------------------------------------------------------------------------

class TestParseCommentAdditional:
    def test_no_pipe_delimiters(self):
        comment = {"id": 1, "time": 0, "text": "Acme Corp is hiring engineers"}
        parsed = hn_jobs.parse_comment(comment)
        assert parsed["company"] == "Acme Corp is hiring engineers"
        assert parsed["remote"] is False

    def test_location_hint_extraction(self):
        # NYC has a location hint, so it should be picked as location
        comment = {"id": 1, "time": 0, "text": "Acme | NYC | Remote | Full-time"}
        parsed = hn_jobs.parse_comment(comment)
        assert "NYC" in parsed["location"]

    def test_skip_patterns_exclude_role_as_location(self):
        # "Senior Engineer" starts with "senior" which matches _skip_patterns
        comment = {"id": 1, "time": 0, "text": "Acme | Senior Engineer | SF | Remote"}
        parsed = hn_jobs.parse_comment(comment)
        assert parsed["location"] != "Senior Engineer"

    def test_onsite_not_remote(self):
        comment = {"id": 1, "time": 0, "text": "Acme | NYC | Onsite"}
        parsed = hn_jobs.parse_comment(comment)
        assert parsed["remote"] is False


# ---------------------------------------------------------------------------
# Additional email instruction tests
# ---------------------------------------------------------------------------

class TestExtractEmailInstructionsAdditional:
    def test_context_window_boundary(self):
        # extract_email_instructions calls strip_html, then extracts ±120 chars around email
        prefix = "a " * 100  # 200 chars
        suffix = " b" * 100  # 200 chars
        text = f"{prefix}jobs@example.com{suffix}"
        instructions = hn_jobs.extract_email_instructions(text)
        assert len(instructions) == 1
        ctx = instructions[0]["context"]
        # Context is at most 120 + email + 120 chars, but the full text is 400+ so it must be truncated
        assert len(ctx) < len(text)

    def test_multiple_emails(self):
        text = "Email jobs@a.com for role A or hr@b.com for role B"
        instructions = hn_jobs.extract_email_instructions(text)
        assert len(instructions) == 2
        emails = {i["email"] for i in instructions}
        assert emails == {"jobs@a.com", "hr@b.com"}


# ---------------------------------------------------------------------------
# Additional classify_url tests
# ---------------------------------------------------------------------------

class TestClassifyUrlAdditional:
    def test_boards_greenhouse_variant(self):
        assert hn_jobs.classify_url("https://boards.greenhouse.io/company/123") == "greenhouse"

    def test_jobs_lever_variant(self):
        assert hn_jobs.classify_url("https://jobs.lever.co/company/abc") == "lever"


class TestExtractUrlsAdditional:
    def test_trailing_punctuation_stripped(self):
        text = "Visit https://example.com/jobs. for details"
        urls = hn_jobs.extract_urls(text)
        assert "https://example.com/jobs" in urls


# ---------------------------------------------------------------------------
# HN dedup integration
# ---------------------------------------------------------------------------

class TestHnDedupIntegration:
    def test_seen_comment_skipped(self, tmp_path):
        comment = {"id": 77, "text": "Acme | SF | LLM engineer needed", "time": 0}
        with patch.object(hn_jobs, "RESULTS_DIR", tmp_path), \
             patch("hn_jobs.load_config", return_value={"resume_text": None, "preferences": {}, "filters": {"max_seniority": None, "coding_only": False}}), \
             patch("hn_jobs.SeenTracker", _mock_tracker_class({"77": time.time()})), \
             patch("hn_jobs.find_hiring_threads", return_value=[{"title": "T", "kids": [77]}]), \
             patch("hn_jobs.fetch_comments", return_value=[comment]), \
             patch("hn_jobs.scrape_job_boards"), \
             patch("sys.argv", ["hn_jobs.py", "--dry-run"]):
            hn_jobs.main()

        json_files = list(tmp_path.glob("results_*.json"))
        data = json.loads(json_files[0].read_text())
        # Comment 77 was already seen, so no results
        assert len(data["results"]) == 0


# ---------------------------------------------------------------------------
# Additional HTML email tests
# ---------------------------------------------------------------------------

class TestHtmlEmailAdditional:
    def test_smtp_host_and_port(self):
        with patch("hn_jobs.smtplib.SMTP_SSL") as mock_smtp:
            mock_server = MagicMock()
            mock_smtp.return_value.__enter__ = MagicMock(return_value=mock_server)
            mock_smtp.return_value.__exit__ = MagicMock(return_value=False)
            hn_jobs.send_email("<html></html>", "to@x.com", "from@x.com", "pass")

        mock_smtp.assert_called_once_with("smtp.gmail.com", 465)

    def test_result_card_order_preserved(self):
        r1 = _make_result(company="First", score=5)
        r2 = _make_result(company="Second", score=3)
        html = hn_jobs.build_email_html([r1, r2], [], ["Thread"])
        first_pos = html.index("First")
        second_pos = html.index("Second")
        assert first_pos < second_pos


# ---------------------------------------------------------------------------
# Additional terminal output tests
# ---------------------------------------------------------------------------

class TestPrintResultsAdditional:
    def test_score_displayed(self, capsys):
        results = [_make_result(score=5)]
        hn_jobs.print_results(results, [])
        output = capsys.readouterr().out
        assert "score: 5" in output

    def test_location_displayed(self, capsys):
        results = [_make_result(location="San Francisco, CA")]
        hn_jobs.print_results(results, [])
        output = capsys.readouterr().out
        assert "San Francisco, CA" in output

    def test_matched_keywords_displayed(self, capsys):
        results = [_make_result(matches={"AI tooling": ["copilot", "llm"]})]
        hn_jobs.print_results(results, [])
        output = capsys.readouterr().out
        assert "copilot" in output
        assert "llm" in output

    def test_application_links_displayed(self, capsys):
        results = [_make_result()]
        hn_jobs.print_results(results, [])
        output = capsys.readouterr().out
        assert "greenhouse" in output.lower() or "Engineer" in output
        assert "jobs@testco.com" in output


# ---------------------------------------------------------------------------
# JSON results save additional
# ---------------------------------------------------------------------------

class TestJsonResultsSaveAdditional:
    def test_result_fields_complete(self, tmp_path):
        comment = {"id": 88, "text": "Acme | San Francisco, CA | LLM engineer needed", "time": 0}
        with patch.object(hn_jobs, "RESULTS_DIR", tmp_path), \
             patch("hn_jobs.load_config", return_value={"resume_text": None, "preferences": {}, "filters": {"max_seniority": None, "coding_only": False}}), \
             patch("hn_jobs.SeenTracker", _mock_tracker_class()), \
             patch("hn_jobs.find_hiring_threads", return_value=[{"title": "T", "kids": [88]}]), \
             patch("hn_jobs.fetch_comments", return_value=[comment]), \
             patch("hn_jobs.scrape_job_boards"), \
             patch("sys.argv", ["hn_jobs.py", "--dry-run"]):
            hn_jobs.main()

        json_files = list(tmp_path.glob("results_*.json"))
        data = json.loads(json_files[0].read_text())
        assert "scan_time" in data
        assert len(data["results"]) == 1
        r = data["results"][0]
        for field in ["id", "company", "location", "remote", "score",
                      "matched_categories", "matched_keywords", "job_board_urls", "emails"]:
            assert field in r, f"Missing field: {field}"

    def test_json_saved_with_no_email_mode(self, tmp_path):
        comment = {"id": 89, "text": "Acme | SF | LLM engineer", "time": 0}
        with patch.object(hn_jobs, "RESULTS_DIR", tmp_path), \
             patch("hn_jobs.load_config", return_value={"resume_text": None, "preferences": {}, "filters": {"max_seniority": None, "coding_only": False}}), \
             patch("hn_jobs.SeenTracker", _mock_tracker_class()), \
             patch("hn_jobs.find_hiring_threads", return_value=[{"title": "T", "kids": [89]}]), \
             patch("hn_jobs.fetch_comments", return_value=[comment]), \
             patch("hn_jobs.scrape_job_boards"), \
             patch("sys.argv", ["hn_jobs.py", "--no-email"]):
            hn_jobs.main()

        json_files = list(tmp_path.glob("results_*.json"))
        assert len(json_files) == 1


# ---------------------------------------------------------------------------
# Additional load_config tests
# ---------------------------------------------------------------------------

class TestLoadConfigAdditional:
    def test_config_yaml_with_resume_key(self, tmp_path):
        import yaml
        config_file = tmp_path / "config.yaml"
        config_file.write_text(yaml.dump({"resume": "/path/to/resume.pdf"}))

        with patch.object(hn_jobs, "CONFIG_FILE", config_file), \
             patch("hn_jobs.extract_resume_text", return_value="extracted text") as mock_extract:
            config = hn_jobs.load_config()

        mock_extract.assert_called_once_with("/path/to/resume.pdf")
        assert config["resume_text"] == "extracted text"

    def test_resume_override_takes_precedence(self, tmp_path):
        import yaml
        config_file = tmp_path / "config.yaml"
        config_file.write_text(yaml.dump({"resume": "/config/resume.pdf"}))

        with patch.object(hn_jobs, "CONFIG_FILE", config_file), \
             patch("hn_jobs.extract_resume_text", return_value="override text") as mock_extract:
            config = hn_jobs.load_config(resume_override="/cli/resume.pdf")

        mock_extract.assert_called_once_with("/cli/resume.pdf")
        assert config["resume_text"] == "override text"

    def test_empty_config_yaml(self, tmp_path):
        config_file = tmp_path / "config.yaml"
        config_file.write_text("")  # empty YAML file

        with patch.object(hn_jobs, "CONFIG_FILE", config_file):
            config = hn_jobs.load_config()

        assert config["resume_text"] is None
        assert config["preferences"] == {}


# ---------------------------------------------------------------------------
# HTML email structure
# ---------------------------------------------------------------------------

class TestHtmlEmailStructure:
    def test_html_has_orange_header(self):
        html = hn_jobs.build_email_html([], [], ["Thread"])
        assert "ff6600" in html  # HN orange
        assert "Who's Hiring" in html

    def test_email_flow_integration(self, tmp_path):
        """Verify main() builds HTML and sends via SMTP when not dry-run."""
        comment = {"id": 99, "text": "Acme | SF | LLM engineer needed", "time": 0}
        with patch.object(hn_jobs, "RESULTS_DIR", tmp_path), \
             patch("hn_jobs.load_config", return_value={"resume_text": None, "preferences": {}, "filters": {"max_seniority": None, "coding_only": False}}), \
             patch("hn_jobs.SeenTracker", _mock_tracker_class()), \
             patch("hn_jobs.find_hiring_threads", return_value=[{"title": "T", "kids": [99]}]), \
             patch("hn_jobs.fetch_comments", return_value=[comment]), \
             patch("hn_jobs.scrape_job_boards"), \
             patch("hn_jobs.send_email") as mock_send, \
             patch.dict(os.environ, {
                 "HN_JOBS_EMAIL_TO": "to@x.com",
                 "HN_JOBS_EMAIL_FROM": "from@x.com",
                 "HN_JOBS_EMAIL_PASSWORD": "pass",
             }), \
             patch("sys.argv", ["hn_jobs.py"]):
            hn_jobs.main()

        mock_send.assert_called_once()
        html_body = mock_send.call_args[0][0]
        assert "Acme" in html_body


# ---------------------------------------------------------------------------
# Additional keyword matching
# ---------------------------------------------------------------------------

class TestKeywordMatchingAdditional:
    def test_ai_ml_slash_keyword(self):
        """The ai/ml keyword requires special regex handling (slash escaping)."""
        matches = hn_jobs.match_keywords("Looking for an ai/ml engineer")
        assert "General AI+SWE" in matches
        assert "ai/ml" in matches["General AI+SWE"]


class TestScoreMatchesAdditional:
    def test_two_categories_ai_plus_systems(self):
        """AI tooling (3) + Systems (2) = 5 as stated in the spec."""
        matches = {"AI tooling": ["llm"], "Systems": ["rust"]}
        assert hn_jobs.score_matches(matches) == 5


# ---------------------------------------------------------------------------
# Auto-prune in main()
# ---------------------------------------------------------------------------

class TestAutoPruneInMain:
    def test_prune_called_during_main(self, tmp_path):
        comment = {"id": 101, "text": "Acme | SF | LLM engineer", "time": 0}
        mock_tracker = MagicMock()
        mock_tracker.is_empty.return_value = True
        mock_tracker.is_seen.return_value = False
        MockTrackerClass = MagicMock(return_value=mock_tracker)
        with patch.object(hn_jobs, "RESULTS_DIR", tmp_path), \
             patch("hn_jobs.load_config", return_value={"resume_text": None, "preferences": {}, "filters": {"max_seniority": None, "coding_only": False}}), \
             patch("hn_jobs.SeenTracker", MockTrackerClass), \
             patch("hn_jobs.find_hiring_threads", return_value=[{"title": "T", "kids": [101]}]), \
             patch("hn_jobs.fetch_comments", return_value=[comment]), \
             patch("hn_jobs.scrape_job_boards"), \
             patch("sys.argv", ["hn_jobs.py", "--dry-run"]):
            hn_jobs.main()

        mock_tracker.prune.assert_called_once()


# ---------------------------------------------------------------------------
# Ranking fallback integration
# ---------------------------------------------------------------------------

class TestRankingFallbackIntegration:
    def test_no_rank_flag_skips_ranking(self, tmp_path):
        comment = {"id": 102, "text": "Acme | SF | LLM engineer needed", "time": 0}
        with patch.object(hn_jobs, "RESULTS_DIR", tmp_path), \
             patch("hn_jobs.load_config", return_value={"resume_text": "resume", "preferences": {}, "filters": {"max_seniority": None, "coding_only": False}}), \
             patch("hn_jobs.SeenTracker", _mock_tracker_class()), \
             patch("hn_jobs.find_hiring_threads", return_value=[{"title": "T", "kids": [102]}]), \
             patch("hn_jobs.fetch_comments", return_value=[comment]), \
             patch("hn_jobs.scrape_job_boards"), \
             patch("hn_jobs.rank_jobs_with_claude") as mock_rank, \
             patch("sys.argv", ["hn_jobs.py", "--dry-run", "--no-rank"]):
            hn_jobs.main()

        mock_rank.assert_not_called()

    def test_missing_resume_skips_ranking(self, tmp_path):
        comment = {"id": 103, "text": "Acme | SF | LLM engineer needed", "time": 0}
        with patch.object(hn_jobs, "RESULTS_DIR", tmp_path), \
             patch("hn_jobs.load_config", return_value={"resume_text": None, "preferences": {}, "filters": {"max_seniority": None, "coding_only": False}}), \
             patch("hn_jobs.SeenTracker", _mock_tracker_class()), \
             patch("hn_jobs.find_hiring_threads", return_value=[{"title": "T", "kids": [103]}]), \
             patch("hn_jobs.fetch_comments", return_value=[comment]), \
             patch("hn_jobs.scrape_job_boards"), \
             patch("hn_jobs.rank_jobs_with_claude") as mock_rank, \
             patch("sys.argv", ["hn_jobs.py", "--dry-run"]):
            hn_jobs.main()

        mock_rank.assert_not_called()

    def test_no_api_key_warning_to_stderr(self, capsys):
        import sys as _sys
        results = [_make_result()]
        with patch.dict(os.environ, {}, clear=True):
            os.environ.pop("ANTHROPIC_API_KEY", None)
            hn_jobs.rank_jobs_with_claude(results, "resume", {})

        stderr = capsys.readouterr().err
        assert "ANTHROPIC_API_KEY" in stderr


# ---------------------------------------------------------------------------
# Full ranking integration through main()
# ---------------------------------------------------------------------------

class TestRankingIntegration:
    @pytest.fixture(autouse=True)
    def _mock_anthropic(self):
        import sys as _sys
        self.mock_anthropic = MagicMock()
        _sys.modules["anthropic"] = self.mock_anthropic
        yield
        _sys.modules.pop("anthropic", None)

    def test_full_ranking_path_in_main(self, tmp_path):
        comment = {"id": 104, "text": "Acme | SF | LLM engineer needed", "time": 0}
        mock_response = MagicMock()
        mock_response.content = [MagicMock(text=json.dumps([
            {"index": 0, "reason": "Great fit for your background"},
        ]))]
        self.mock_anthropic.Anthropic.return_value.messages.create.return_value = mock_response

        with patch.object(hn_jobs, "RESULTS_DIR", tmp_path), \
             patch("hn_jobs.load_config", return_value={"resume_text": "I am an engineer", "preferences": {}, "filters": {"max_seniority": None, "coding_only": False}}), \
             patch("hn_jobs.SeenTracker", _mock_tracker_class()), \
             patch("hn_jobs.find_hiring_threads", return_value=[{"title": "T", "kids": [104]}]), \
             patch("hn_jobs.fetch_comments", return_value=[comment]), \
             patch("hn_jobs.scrape_job_boards"), \
             patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"}), \
             patch("sys.argv", ["hn_jobs.py", "--dry-run"]):
            hn_jobs.main()

        # Check rank badge in HTML output
        html_files = list(tmp_path.glob("preview_*.html"))
        assert len(html_files) == 1
        html = html_files[0].read_text()
        assert "#1" in html
        assert "Great fit" in html

    def test_correct_model_passed_to_api(self):
        results = [_make_result()]
        mock_response = MagicMock()
        mock_response.content = [MagicMock(text=json.dumps([{"index": 0, "reason": "Good"}]))]
        self.mock_anthropic.Anthropic.return_value.messages.create.return_value = mock_response

        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"}):
            hn_jobs.rank_jobs_with_claude(results, "resume", {})

        call_kwargs = self.mock_anthropic.Anthropic.return_value.messages.create.call_args[1]
        assert call_kwargs["model"] == "claude-opus-4-6"


# ---------------------------------------------------------------------------
# Multi-page PDF extraction
# ---------------------------------------------------------------------------

class TestExtractResumeMultiPage:
    def test_multi_page_pdf(self, tmp_path):
        page1 = MagicMock()
        page1.get_text.return_value = "Page 1 content. "
        page2 = MagicMock()
        page2.get_text.return_value = "Page 2 content."
        mock_doc = MagicMock()
        mock_doc.__iter__ = MagicMock(return_value=iter([page1, page2]))

        pdf_path = tmp_path / "resume.pdf"
        pdf_path.touch()

        with patch("hn_jobs.fitz.open", return_value=mock_doc):
            result = hn_jobs.extract_resume_text(str(pdf_path))

        assert "Page 1 content" in result
        assert "Page 2 content" in result


# ---------------------------------------------------------------------------
# Environment variables: --no-email/--dry-run bypass email cred check
# ---------------------------------------------------------------------------

class TestEnvironmentVariablesAdditional:
    def test_no_email_flag_skips_cred_check(self, tmp_path):
        """--no-email should not require email credentials."""
        comment = {"id": 105, "text": "Acme | SF | LLM engineer", "time": 0}
        with patch.object(hn_jobs, "RESULTS_DIR", tmp_path), \
             patch("hn_jobs.load_config", return_value={"resume_text": None, "preferences": {}, "filters": {"max_seniority": None, "coding_only": False}}), \
             patch("hn_jobs.SeenTracker", _mock_tracker_class()), \
             patch("hn_jobs.find_hiring_threads", return_value=[{"title": "T", "kids": [105]}]), \
             patch("hn_jobs.fetch_comments", return_value=[comment]), \
             patch("hn_jobs.scrape_job_boards"), \
             patch.dict(os.environ, {}, clear=True), \
             patch("sys.argv", ["hn_jobs.py", "--no-email"]):
            # Should not raise SystemExit even without email creds
            hn_jobs.main()

    def test_dry_run_skips_cred_check(self, tmp_path):
        """--dry-run should not require email credentials."""
        comment = {"id": 106, "text": "Acme | SF | LLM engineer", "time": 0}
        with patch.object(hn_jobs, "RESULTS_DIR", tmp_path), \
             patch("hn_jobs.load_config", return_value={"resume_text": None, "preferences": {}, "filters": {"max_seniority": None, "coding_only": False}}), \
             patch("hn_jobs.SeenTracker", _mock_tracker_class()), \
             patch("hn_jobs.find_hiring_threads", return_value=[{"title": "T", "kids": [106]}]), \
             patch("hn_jobs.fetch_comments", return_value=[comment]), \
             patch("hn_jobs.scrape_job_boards"), \
             patch.dict(os.environ, {}, clear=True), \
             patch("sys.argv", ["hn_jobs.py", "--dry-run"]):
            hn_jobs.main()


# ---------------------------------------------------------------------------
# Seniority estimation
# ---------------------------------------------------------------------------

class TestEstimateSeniority:
    def test_staff_from_title(self):
        assert hn_jobs.estimate_seniority("Staff Software Engineer") == "staff+"

    def test_principal_from_title(self):
        assert hn_jobs.estimate_seniority("Principal Engineer") == "staff+"

    def test_senior_from_title(self):
        assert hn_jobs.estimate_seniority("Senior Backend Engineer") == "senior"

    def test_sr_dot_from_title(self):
        assert hn_jobs.estimate_seniority("Sr. Software Engineer") == "senior"

    def test_lead_from_title(self):
        assert hn_jobs.estimate_seniority("Engineering Lead") == "senior"

    def test_junior_from_title(self):
        assert hn_jobs.estimate_seniority("Junior Developer") == "junior"

    def test_intern_from_title(self):
        assert hn_jobs.estimate_seniority("Engineering Intern") == "intern"

    def test_founding_returns_unknown(self):
        assert hn_jobs.estimate_seniority("Founding Engineer") == "unknown"

    def test_plain_title_with_high_experience(self):
        assert hn_jobs.estimate_seniority("Software Engineer", "Requires 10+ years") == "senior"

    def test_plain_title_with_mid_experience(self):
        assert hn_jobs.estimate_seniority("Software Engineer", "3+ years of experience") == "mid"

    def test_plain_title_with_low_experience(self):
        assert hn_jobs.estimate_seniority("Software Engineer", "1+ years") == "junior"

    def test_no_signals_returns_unknown(self):
        assert hn_jobs.estimate_seniority("Software Engineer") == "unknown"

    def test_title_takes_priority_over_description(self):
        assert hn_jobs.estimate_seniority("Junior Developer", "10+ years experience") == "junior"


class TestSeniorityExceeds:
    def test_senior_exceeds_mid(self):
        assert hn_jobs.seniority_exceeds("senior", "mid") is True

    def test_mid_does_not_exceed_senior(self):
        assert hn_jobs.seniority_exceeds("mid", "senior") is False

    def test_same_level_does_not_exceed(self):
        assert hn_jobs.seniority_exceeds("mid", "mid") is False

    def test_unknown_never_exceeds(self):
        assert hn_jobs.seniority_exceeds("unknown", "junior") is False

    def test_staff_exceeds_mid(self):
        assert hn_jobs.seniority_exceeds("staff+", "mid") is True

    def test_intern_does_not_exceed_anything(self):
        assert hn_jobs.seniority_exceeds("intern", "intern") is False
        assert hn_jobs.seniority_exceeds("intern", "junior") is False


# ---------------------------------------------------------------------------
# Job type classification
# ---------------------------------------------------------------------------

class TestIsCodingJob:
    def test_software_engineer(self):
        assert hn_jobs.is_coding_job("Software Engineer") is True

    def test_backend_developer(self):
        assert hn_jobs.is_coding_job("Backend Developer") is True

    def test_ml_engineer(self):
        assert hn_jobs.is_coding_job("ML Engineer") is True

    def test_product_manager(self):
        assert hn_jobs.is_coding_job("Product Manager") is False

    def test_engineering_manager(self):
        assert hn_jobs.is_coding_job("Engineering Manager") is False

    def test_designer(self):
        assert hn_jobs.is_coding_job("UX Designer") is False

    def test_sales(self):
        assert hn_jobs.is_coding_job("Account Executive") is False

    def test_recruiter(self):
        assert hn_jobs.is_coding_job("Recruiter") is False

    def test_ambiguous_title_coding_description(self):
        assert hn_jobs.is_coding_job("Team Member", "We need a backend engineer") is True

    def test_ambiguous_title_non_coding_description(self):
        assert hn_jobs.is_coding_job("Team Member", "Looking for a product manager") is False

    def test_ambiguous_title_no_description(self):
        # benefit of the doubt
        assert hn_jobs.is_coding_job("Team Member") is True

    def test_sre(self):
        assert hn_jobs.is_coding_job("SRE") is True

    def test_devops(self):
        assert hn_jobs.is_coding_job("DevOps Engineer") is True

    def test_director_of_engineering(self):
        assert hn_jobs.is_coding_job("Director of Engineering") is False

    def test_cto(self):
        assert hn_jobs.is_coding_job("CTO") is False


class TestParseCommentNewFields:
    def test_role_extracted_from_header(self):
        comment = {"id": 200, "text": "Acme | Senior Backend Engineer | SF, CA | Remote", "time": 0}
        parsed = hn_jobs.parse_comment(comment)
        assert "engineer" in parsed["role"].lower()
        assert parsed["seniority"] == "senior"
        assert parsed["is_coding"] is True

    def test_non_coding_role_detected(self):
        comment = {"id": 201, "text": "Acme | Product Manager | NYC", "time": 0}
        parsed = hn_jobs.parse_comment(comment)
        assert parsed["is_coding"] is False

    def test_seniority_from_description(self):
        comment = {"id": 202, "text": "Acme | Engineer\n\nRequires 10+ years of experience", "time": 0}
        parsed = hn_jobs.parse_comment(comment)
        assert parsed["seniority"] == "senior"


# ---------------------------------------------------------------------------
# parse-company-location-remote: Salary excluded from role/location
# ---------------------------------------------------------------------------

class TestParseCommentSalaryExclusion:
    def test_salary_excluded_from_role_and_location(self):
        comment = {"id": 300, "time": 0, "text": "Acme | Engineer | SF, CA | $150k-$200k"}
        parsed = hn_jobs.parse_comment(comment)
        assert "Engineer" in parsed["role"]
        assert "SF, CA" in parsed["location"]
        assert "$150k" not in parsed["role"]
        assert "$150k" not in parsed["location"]

    def test_non_location_parts_excluded(self):
        comment = {"id": 301, "time": 0, "text": "Acme | Engineer | Full-time | Contract"}
        parsed = hn_jobs.parse_comment(comment)
        assert parsed["location"] == ""


# ---------------------------------------------------------------------------
# html-email-delivery: Subject line and inline styles
# ---------------------------------------------------------------------------

class TestHtmlEmailDeliverySubject:
    def test_subject_line_format(self):
        from email import message_from_string
        from email.header import decode_header
        mock_server = MagicMock()
        with patch("hn_jobs.smtplib.SMTP_SSL") as mock_smtp:
            mock_smtp.return_value.__enter__ = MagicMock(return_value=mock_server)
            mock_smtp.return_value.__exit__ = MagicMock(return_value=False)
            hn_jobs.send_email("<html>test</html>", "to@x.com", "from@x.com", "pass")

        sent_raw = mock_server.sendmail.call_args[0][2]
        msg = message_from_string(sent_raw)
        parts = decode_header(msg["Subject"])
        subject = parts[0][0]
        if isinstance(subject, bytes):
            subject = subject.decode(parts[0][1] or "utf-8")
        month_year = datetime.now().strftime("%B %Y")
        assert "HN Who" in subject
        assert month_year in subject


class TestHtmlEmailDeliveryInlineStyles:
    def test_result_card_inline_styles(self):
        results = [_make_result()]
        html_output = hn_jobs.build_email_html(results, [], ["Thread"])
        assert "border:1px solid" in html_output
        assert "border-radius:8px" in html_output


# ---------------------------------------------------------------------------
# json-results-save: role, other_urls, seniority, is_coding fields
# ---------------------------------------------------------------------------

class TestJsonResultsFieldsDetailed:
    def _run_dry_run(self, tmp_path, comment):
        with patch.object(hn_jobs, "RESULTS_DIR", tmp_path), \
             patch("hn_jobs.load_config", return_value={"resume_text": None, "preferences": {}, "filters": {"max_seniority": None, "coding_only": False}}), \
             patch("hn_jobs.SeenTracker", _mock_tracker_class()), \
             patch("hn_jobs.find_hiring_threads", return_value=[{"title": "T", "kids": [comment["id"]]}]), \
             patch("hn_jobs.fetch_comments", return_value=[comment]), \
             patch("hn_jobs.scrape_job_boards"), \
             patch("sys.argv", ["hn_jobs.py", "--dry-run"]):
            hn_jobs.main()
        json_files = list(tmp_path.glob("results_*.json"))
        return json.loads(json_files[0].read_text())

    def test_role_field_in_json(self, tmp_path):
        comment = {"id": 310, "text": "Acme | Backend Engineer | SF | LLM tools", "time": 0}
        data = self._run_dry_run(tmp_path, comment)
        assert len(data["results"]) >= 1
        assert "role" in data["results"][0]

    def test_other_urls_field_in_json(self, tmp_path):
        comment = {"id": 311, "text": 'Acme | SF | LLM engineer <a href="https://acme.com/careers">careers</a>', "time": 0}
        data = self._run_dry_run(tmp_path, comment)
        assert len(data["results"]) >= 1
        assert "other_urls" in data["results"][0]

    def test_seniority_and_is_coding_in_json(self, tmp_path):
        comment = {"id": 312, "text": "Acme | Senior Backend Engineer | SF | LLM tools", "time": 0}
        data = self._run_dry_run(tmp_path, comment)
        assert len(data["results"]) >= 1
        r = data["results"][0]
        assert "seniority" in r
        assert "is_coding" in r


# ---------------------------------------------------------------------------
# load-config: filters from config.yaml
# ---------------------------------------------------------------------------

class TestLoadConfigFilters:
    def test_filters_from_config_yaml(self, tmp_path):
        import yaml
        config_file = tmp_path / "config.yaml"
        config_file.write_text(yaml.dump({
            "filters": {"max_seniority": "mid", "coding_only": True},
        }))
        with patch.object(hn_jobs, "CONFIG_FILE", config_file):
            config = hn_jobs.load_config()
        assert config["filters"]["max_seniority"] == "mid"
        assert config["filters"]["coding_only"] is True

    def test_default_filters_when_no_filters_key(self, tmp_path):
        import yaml
        config_file = tmp_path / "config.yaml"
        config_file.write_text(yaml.dump({"preferences": {"remote": "preferred"}}))
        with patch.object(hn_jobs, "CONFIG_FILE", config_file):
            config = hn_jobs.load_config()
        assert config["filters"]["max_seniority"] is None
        assert config["filters"]["coding_only"] is False


# ---------------------------------------------------------------------------
# process-threads: direct unit tests
# ---------------------------------------------------------------------------

class TestProcessThreads:
    def _make_comment(self, cid, text):
        return {"id": cid, "text": text, "time": 0}

    def test_return_tuple_structure_and_sorting(self):
        c1 = self._make_comment(400, "Acme | SF | LLM engineer needed with rust and pytorch")
        c2 = self._make_comment(401, "Beta | NYC | We use copilot for AI coding")
        thread = {"title": "T", "kids": [400, 401]}

        with patch("hn_jobs.fetch_comments", return_value=[c1, c2]), \
             patch("hn_jobs.scrape_job_boards"):
            results, filtered_out, all_seen_ids = hn_jobs.process_threads(
                [thread], None, {"coding_only": False, "max_seniority": None},
            )

        # Returns a 3-tuple
        assert isinstance(results, list)
        assert isinstance(filtered_out, list)
        assert isinstance(all_seen_ids, list)

        # Results sorted by score descending
        if len(results) > 1:
            for i in range(len(results) - 1):
                assert results[i]["score"] >= results[i + 1]["score"]

        # seen_tracker=None means all comments processed (both IDs present)
        assert "400" in all_seen_ids or "401" in all_seen_ids

    def test_scrape_false_skips_scraping(self):
        c1 = self._make_comment(410, "Acme | SF | LLM engineer needed")
        thread = {"title": "T", "kids": [410]}

        with patch("hn_jobs.fetch_comments", return_value=[c1]), \
             patch("hn_jobs.scrape_job_boards") as mock_scrape:
            hn_jobs.process_threads(
                [thread], None, {"coding_only": False, "max_seniority": None},
                scrape=False,
            )

        mock_scrape.assert_not_called()

    def test_coding_only_filters_non_coding(self):
        # Product Manager matches LLM but is non-coding
        c1 = self._make_comment(420, "Acme | Product Manager | SF | We need someone for LLM strategy")
        thread = {"title": "T", "kids": [420]}

        with patch("hn_jobs.fetch_comments", return_value=[c1]), \
             patch("hn_jobs.scrape_job_boards"):
            results, filtered_out, _ = hn_jobs.process_threads(
                [thread], None, {"coding_only": True, "max_seniority": None},
            )

        assert len(results) == 0
        assert len(filtered_out) >= 1
        assert any("non-coding" in r for item in filtered_out for r in item.get("neg_matches", []))

    def test_max_seniority_filters_senior_roles(self):
        # "Senior Engineer" — not a negative keyword, but seniority="senior" exceeds max "mid"
        c1 = self._make_comment(430, "Acme | Senior Engineer | SF | LLM tools and AI coding")
        thread = {"title": "T", "kids": [430]}

        with patch("hn_jobs.fetch_comments", return_value=[c1]), \
             patch("hn_jobs.scrape_job_boards"):
            results, filtered_out, _ = hn_jobs.process_threads(
                [thread], None, {"coding_only": False, "max_seniority": "mid"},
            )

        assert len(results) == 0
        assert len(filtered_out) >= 1
        assert any("seniority" in r for item in filtered_out for r in item.get("neg_matches", []))

    def test_seen_tracker_skips_seen_ids(self):
        c1 = self._make_comment(440, "Acme | SF | LLM engineer needed")
        c2 = self._make_comment(441, "Beta | NYC | We use copilot")
        thread = {"title": "T", "kids": [440, 441]}

        tracker = SeenTracker("/dev/null", "posts")
        tracker.mark(["440"])  # mark c1 as seen

        with patch("hn_jobs.fetch_comments", return_value=[c1, c2]), \
             patch("hn_jobs.scrape_job_boards"):
            results, filtered_out, all_seen_ids = hn_jobs.process_threads(
                [thread], tracker, {"coding_only": False, "max_seniority": None},
            )

        # c1 (440) was already seen, should be skipped
        assert "440" not in all_seen_ids
        assert "441" in all_seen_ids

    def test_filtered_out_sorted_by_score(self):
        # Two comments that both get filtered (non-US location)
        c1 = self._make_comment(450, "Acme | Engineer | London | LLM tools")
        c2 = self._make_comment(451, "Beta | Engineer | Berlin | LLM tools and rust and cuda")
        thread = {"title": "T", "kids": [450, 451]}

        with patch("hn_jobs.fetch_comments", return_value=[c1, c2]), \
             patch("hn_jobs.scrape_job_boards"):
            _, filtered_out, _ = hn_jobs.process_threads(
                [thread], None, {"coding_only": False, "max_seniority": None},
            )

        if len(filtered_out) > 1:
            for i in range(len(filtered_out) - 1):
                assert filtered_out[i]["score"] >= filtered_out[i + 1]["score"]

    def test_multiple_threads_iterated(self):
        c1 = self._make_comment(460, "Acme | SF | LLM engineer")
        c2 = self._make_comment(461, "Beta | NYC | We use copilot")
        t1 = {"title": "T1", "kids": [460]}
        t2 = {"title": "T2", "kids": [461]}

        with patch("hn_jobs.fetch_comments", side_effect=[[c1], [c2]]), \
             patch("hn_jobs.scrape_job_boards"):
            results, _, all_seen_ids = hn_jobs.process_threads(
                [t1, t2], None, {"coding_only": False, "max_seniority": None},
            )

        assert "460" in all_seen_ids
        assert "461" in all_seen_ids

    def test_no_keyword_match_silently_discarded(self):
        c1 = self._make_comment(470, "Acme | SF | We sell insurance")
        thread = {"title": "T", "kids": [470]}

        with patch("hn_jobs.fetch_comments", return_value=[c1]), \
             patch("hn_jobs.scrape_job_boards"):
            results, filtered_out, all_seen_ids = hn_jobs.process_threads(
                [thread], None, {"coding_only": False, "max_seniority": None},
            )

        assert len(results) == 0
        assert len(filtered_out) == 0
        assert "470" in all_seen_ids  # still tracked as seen


# ---------------------------------------------------------------------------
# html-email-delivery: sorted by Claude rank, HN orange header
# ---------------------------------------------------------------------------

class TestHtmlEmailRankSorting:
    def test_claude_ranked_results_show_rank_badges(self):
        r1 = _make_result(company="First", claude_rank=1, claude_reason="Best fit")
        r2 = _make_result(company="Second", claude_rank=2, claude_reason="Good fit", post_id=2)
        html_output = hn_jobs.build_email_html([r1, r2], [], ["Thread"])
        # Rank badges should appear in order
        pos1 = html_output.find("#1")
        pos2 = html_output.find("#2")
        assert pos1 < pos2

    def test_html_contains_orange_header(self):
        html_output = hn_jobs.build_email_html([], [], ["Thread"])
        assert "#ff6600" in html_output


# ---------------------------------------------------------------------------
# terminal-output: --no-email triggers print
# ---------------------------------------------------------------------------

class TestTerminalOutputIntegration:
    def test_no_email_triggers_terminal(self, tmp_path, capsys):
        comment = {"id": 480, "text": "Acme | SF | LLM engineer", "time": 0}
        with patch.object(hn_jobs, "RESULTS_DIR", tmp_path), \
             patch("hn_jobs.load_config", return_value={"resume_text": None, "preferences": {}, "filters": {"max_seniority": None, "coding_only": False}}), \
             patch("hn_jobs.SeenTracker", _mock_tracker_class()), \
             patch("hn_jobs.find_hiring_threads", return_value=[{"title": "T", "kids": [480]}]), \
             patch("hn_jobs.fetch_comments", return_value=[comment]), \
             patch("hn_jobs.scrape_job_boards"), \
             patch("sys.argv", ["hn_jobs.py", "--no-email"]):
            hn_jobs.main()
        output = capsys.readouterr().out
        assert "Acme" in output


# ---------------------------------------------------------------------------
# waas-weighted-scoring: exact numeric scores
# ---------------------------------------------------------------------------

class TestWaasWeightedScoringExact:
    def test_requirements_section_score(self):
        from waas import _weighted_score
        job = {
            "job_title": "Engineer",
            "job_description": "Requirements\nMust have experience with rust",
            "job_tags": [],
        }
        matches = {"Systems": ["rust"]}
        score = _weighted_score(job, matches)
        # Systems(2) * requirements(2.0) = 4.0
        assert score == 4.0

    def test_description_section_score(self):
        from waas import _weighted_score
        job = {
            "job_title": "Engineer",
            "job_description": "We use rust in our stack",
            "job_tags": [],
        }
        matches = {"Systems": ["rust"]}
        score = _weighted_score(job, matches)
        # Systems(2) * description(1.0) = 2.0
        assert score == 2.0
