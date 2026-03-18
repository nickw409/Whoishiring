"""Shared filtering primitives for HN and WAAS job scanning pipelines."""

import json
import re
import time
from pathlib import Path

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

PRUNE_DAYS = 180  # 6 months

KEYWORD_CATEGORIES = {
    "AI tooling": {
        "weight": 3,
        "keywords": [
            "claude code", "copilot", "cursor", "ai-assisted", "ai tools",
            "ai coding", "agentic", "llm", "ai engineer",
        ],
    },
    "Systems": {
        "weight": 2,
        "keywords": [
            "rust", "cuda", "gpu", "simd", "high-performance", "hpc",
            "systems programming",
        ],
    },
    "General AI+SWE": {
        "weight": 1,
        "keywords": [
            "machine learning", "tensorflow", "pytorch", "deep learning",
            "computer vision", "ml engineer", "ai/ml",
        ],
    },
}

NEGATIVE_KEYWORDS = [
    "staff engineer", "principal engineer", "engineering manager",
    "director of", "vp of", "10+ years", "15+ years",
]

SENIORITY_LEVELS = ["intern", "junior", "mid", "senior", "staff+"]

# ---------------------------------------------------------------------------
# Pre-compiled regexes
# ---------------------------------------------------------------------------

_kw_patterns = {}
for _cat, _info in KEYWORD_CATEGORIES.items():
    for _kw in _info["keywords"]:
        _escaped = re.escape(_kw)
        _kw_patterns[_kw] = re.compile(r"\b" + _escaped + r"\b", re.IGNORECASE)

_neg_patterns = {}
for _kw in NEGATIVE_KEYWORDS:
    _escaped = re.escape(_kw)
    _neg_patterns[_kw] = re.compile(r"\b" + _escaped + r"\b", re.IGNORECASE)

_EXPERIENCE_RE = re.compile(r"(\d+)\+?\s*(?:years?|yrs?)\b", re.IGNORECASE)

_NON_CODING_TITLE_RE = re.compile(
    r"\b(product manager|project manager|program manager|scrum master"
    r"|designer|ux designer|ui designer|graphic designer|brand designer"
    r"|marketing|content|copywriter|communications"
    r"|sales|account executive|business development|bdr|sdr"
    r"|recruiter|talent|people ops|human resources|hr\b"
    r"|operations manager|office manager|executive assistant"
    r"|finance|accounting|controller|bookkeeper"
    r"|legal|compliance|general counsel"
    r"|customer success|customer support|support engineer"
    r"|data analyst|business analyst|product analyst"
    r"|cfo|cmo|coo|chief of staff)\b",
    re.IGNORECASE,
)

_CODING_TITLE_RE = re.compile(
    r"\b(engineer|developer|programmer|architect|sre|devops|swe"
    r"|frontend|backend|fullstack|full.stack"
    r"|software|platform|infrastructure|security engineer"
    r"|machine learning|ml engineer|ai engineer|data scientist"
    r"|embedded|firmware|systems)\b",
    re.IGNORECASE,
)

_ENG_MGMT_RE = re.compile(
    r"\b(engineering manager|eng manager|director of engineering"
    r"|vp of engineering|head of engineering|cto)\b",
    re.IGNORECASE,
)

_NON_US_RE = re.compile(
    r"\b(london|berlin|munich|hamburg|frankfurt|paris|amsterdam|rotterdam"
    r"|barcelona|madrid|rome|milan|zurich|vienna|warsaw|prague|stockholm"
    r"|oslo|copenhagen|helsinki|brussels|lisbon|dublin|budapest|bucharest"
    r"|toronto|vancouver|montreal|calgary|ottawa"
    r"|sydney|melbourne|brisbane|perth"
    r"|singapore|tokyo|osaka|seoul|beijing|shanghai|hong kong|shenzhen"
    r"|bangalore|bengaluru|mumbai|delhi|hyderabad|pune"
    r"|tel aviv|istanbul|dubai|abu dhabi"
    r"|uk|united kingdom|england|scotland|wales"
    r"|canada|germany|france|spain|italy|netherlands|sweden|norway|denmark"
    r"|finland|switzerland|austria|belgium|poland|czech|portugal|ireland"
    r"|australia|new zealand|japan|china|india|south korea"
    r"|israel|turkey|uae|brazil|mexico|argentina"
    r"|europe|emea|apac|latam)\b",
    re.IGNORECASE,
)

_US_RE = re.compile(
    r"\b(usa|united states|new york|nyc|san francisco|los angeles|chicago"
    r"|seattle|boston|austin|denver|atlanta|miami|dallas|houston|portland"
    r"|phoenix|san diego|minneapolis|detroit|baltimore|washington dc"
    r"|bay area|silicon valley|,\s*[A-Z]{2})\b",
    re.IGNORECASE,
)

# ---------------------------------------------------------------------------
# Keyword matching & scoring
# ---------------------------------------------------------------------------

def match_keywords(text):
    """Return dict of {category: [matched keywords]} for text."""
    matches = {}
    for cat, info in KEYWORD_CATEGORIES.items():
        cat_matches = []
        for kw in info["keywords"]:
            if _kw_patterns[kw].search(text):
                cat_matches.append(kw)
        if cat_matches:
            matches[cat] = cat_matches
    return matches


def match_negative(text):
    """Return list of matched negative keywords."""
    return [kw for kw, pat in _neg_patterns.items() if pat.search(text)]


def score_matches(matches):
    """Score based on matched categories (per-category, not per-keyword)."""
    total = 0
    for cat in matches:
        total += KEYWORD_CATEGORIES[cat]["weight"]
    return total


# ---------------------------------------------------------------------------
# Seniority estimation
# ---------------------------------------------------------------------------

def estimate_seniority(title, text=""):
    """Estimate seniority level from job title and description text.

    Returns one of: intern, junior, mid, senior, staff+, unknown.
    """
    title_lower = title.lower()

    if any(kw in title_lower for kw in ["staff", "principal"]):
        return "staff+"
    if any(kw in title_lower for kw in ["senior", "sr.", "sr "]):
        return "senior"
    if "lead" in title_lower:
        return "senior"
    if "founding" in title_lower:
        return "unknown"
    if "intern" in title_lower:
        return "intern"
    if any(kw in title_lower for kw in ["junior", "jr.", "jr "]):
        return "junior"

    if text:
        exp_matches = _EXPERIENCE_RE.findall(text[:1500])
        if exp_matches:
            max_years = max(int(y) for y in exp_matches)
            if max_years >= 8:
                return "senior"
            if max_years >= 4:
                return "mid"
            if max_years >= 2:
                return "mid"
            return "junior"

    return "unknown"


def seniority_exceeds(seniority, max_level):
    """Return True if seniority is above max_level.

    Unknown seniority is never filtered (benefit of the doubt).
    """
    if seniority == "unknown" or max_level not in SENIORITY_LEVELS:
        return False
    if seniority not in SENIORITY_LEVELS:
        return False
    return SENIORITY_LEVELS.index(seniority) > SENIORITY_LEVELS.index(max_level)


# ---------------------------------------------------------------------------
# Job type classification
# ---------------------------------------------------------------------------

def is_coding_job(title, text=""):
    """Return True if the job appears to be a coding/IC engineering role.

    Engineering management roles return False. Unknown roles return True
    (benefit of the doubt).
    """
    if _ENG_MGMT_RE.search(title):
        return False
    if _NON_CODING_TITLE_RE.search(title):
        return False
    if _CODING_TITLE_RE.search(title):
        return True
    if text:
        first_500 = text[:500]
        if _CODING_TITLE_RE.search(first_500):
            return True
        if _NON_CODING_TITLE_RE.search(first_500):
            return False
    return True


# ---------------------------------------------------------------------------
# Location filtering
# ---------------------------------------------------------------------------

def is_outside_us(parsed):
    """Return True if the job is clearly located outside the US and not remote."""
    if parsed["remote"]:
        return False
    location = parsed["location"]
    if not location:
        return False
    if _US_RE.search(location):
        return False
    return bool(_NON_US_RE.search(location))


# ---------------------------------------------------------------------------
# Filter cascade
# ---------------------------------------------------------------------------

def apply_filters(parsed, neg_matches, *, coding_only=False, max_seniority=None):
    """Apply the filtering cascade to a parsed job.

    Returns None if accepted, or a list of filter reasons if rejected.
    """
    if neg_matches:
        return list(neg_matches)
    if is_outside_us(parsed):
        return ["non-US location"]
    if coding_only and not parsed.get("is_coding", True):
        return [f"non-coding role: {parsed.get('role', 'unknown')}"]
    if max_seniority and seniority_exceeds(
        parsed.get("seniority", "unknown"), max_seniority
    ):
        return [f"seniority too high: {parsed.get('seniority', 'unknown')}"]
    return None


# ---------------------------------------------------------------------------
# Deduplication
# ---------------------------------------------------------------------------

class SeenTracker:
    """Generic dedup tracker backed by a JSON file."""

    def __init__(self, filepath, key):
        self.filepath = Path(filepath)
        self.key = key
        self._data = {key: {}}

    def load(self):
        """Load seen data from disk."""
        if self.filepath.exists():
            try:
                data = json.loads(self.filepath.read_text())
                if self.key in data:
                    self._data = data
                else:
                    self._data = {self.key: {}}
            except (json.JSONDecodeError, KeyError):
                self._data = {self.key: {}}
        return self

    def save(self):
        """Save seen data to disk."""
        self.filepath.write_text(json.dumps(self._data, indent=2))

    def is_seen(self, id_):
        """Check if an ID has been seen."""
        return str(id_) in self._data[self.key]

    def mark(self, ids):
        """Mark IDs as seen with current timestamp."""
        now = time.time()
        for id_ in ids:
            self._data[self.key][str(id_)] = now

    def prune(self):
        """Remove entries older than PRUNE_DAYS."""
        cutoff = time.time() - (PRUNE_DAYS * 86400)
        self._data[self.key] = {
            k: ts for k, ts in self._data[self.key].items()
            if isinstance(ts, (int, float)) and ts > cutoff
        }

    def is_empty(self):
        """True if no entries (i.e., first run)."""
        return len(self._data.get(self.key, {})) == 0

    @property
    def entries(self):
        """Direct access to the entries dict."""
        return self._data[self.key]
