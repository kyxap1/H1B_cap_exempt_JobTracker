"""Shared constants, paths, data model, and utility functions."""

from __future__ import annotations

import re
import time
import random
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

# ---------------------------------------------------------------------------
# Paths
#
# Everything the pipeline produces lives under two top-level dirs (both
# gitignored) so the repo root stays clean:
#   data/   — outputs worth keeping (DOL downloads, sponsor list, jobs, state)
#   cache/  — regenerable caches (ATS detection cache; also the container HOME)
# ---------------------------------------------------------------------------

PROJECT_DIR    = Path(__file__).parent
DATA_DIR       = PROJECT_DIR / "data"
CACHE_DIR      = PROJECT_DIR / "cache"

LCA_DIR        = DATA_DIR / "lca"                              # DOL Excel downloads
COMPANIES_JSON = DATA_DIR / "h1b-cap-exempt-sponsors.json"
CHECKPOINT     = DATA_DIR / "h1b-cap-exempt-checkpoint.json"
JOBS_JSONL     = DATA_DIR / "it-jobs.jsonl"
SEEN_URLS_FILE = DATA_DIR / "seen-job-urls.json"
ATS_CACHE      = CACHE_DIR / "ats-cache.json"
SERPER_CACHE   = CACHE_DIR / "serper-cache.json"              # Google search results

# Create the dirs on import so every entry point (sponsors / jobs / cron) can
# write its outputs without each one repeating mkdir calls.
for _d in (DATA_DIR, CACHE_DIR, LCA_DIR):
    _d.mkdir(parents=True, exist_ok=True)

# ---------------------------------------------------------------------------
# Pipeline settings
# ---------------------------------------------------------------------------

TOP_N        = 1000
POLITE_DELAY = (0.6, 1.4)
PST          = ZoneInfo("America/Los_Angeles")

DOL_FILES = {
    2024: "https://www.dol.gov/sites/dolgov/files/ETA/oflc/pdfs/LCA_Disclosure_Data_FY2024_Q4.xlsx",
    2025: "https://www.dol.gov/sites/dolgov/files/ETA/oflc/pdfs/LCA_Disclosure_Data_FY2025_Q3.xlsx",
    2026: "https://www.dol.gov/sites/dolgov/files/ETA/oflc/pdfs/LCA_Disclosure_Data_FY2026_Q1.xlsx",
}

CAP_EXEMPT_NAICS = ("6113", "6221", "8139")

CAP_EXEMPT_KEYWORDS = (
    "university", "medical center", "institute",
    "national laboratory", "association",
)

# ---------------------------------------------------------------------------
# Careers-finder settings
# ---------------------------------------------------------------------------

CAREERS_KEYWORDS = ("career", "careers", "job", "jobs", "employment", "work-with-us")

AGGREGATOR_BLOCKLIST = (
    "linkedin.com", "indeed.com", "glassdoor.com", "ziprecruiter.com",
    "h1bgrader.com", "myvisajobs.com", "monster.com", "wikipedia.org",
    "google.com", "youtube.com", "facebook.com", "twitter.com",
    # Job boards / higher-ed aggregators: they host an employer's listings but
    # are not the employer's own portal, so they're the wrong careers_page.
    "simplyhired.com", "careerbuilder.com", "dice.com", "snagajob.com",
    "chronicle.com", "insidehighered.com", "higheredjobs.com",
    "academickeys.com", "hercjobs.org", "jobs.ac.uk", "schooljobs.com",
)

# jobs.py / ats_scrapers.py import this name; it's the same blocklist.
AGGREGATORS = AGGREGATOR_BLOCKLIST

# ---------------------------------------------------------------------------
# ATS (applicant-tracking-system) domains we know how to scrape
# ---------------------------------------------------------------------------

ATS_DOMAINS = (
    "greenhouse.io", "lever.co", "myworkdayjobs.com", "workday.com",
    "smartrecruiters.com", "yello.co", "icims.com", "jobvite.com",
    "ashbyhq.com", "taleo.net", "successfactors.com",
)

# ---------------------------------------------------------------------------
# Job-title filter  ──  EDIT THIS to change which roles you track
# ---------------------------------------------------------------------------

# Keywords passed to ATS search and matched (on word boundaries) against job
# titles in the fallback scraper. "aws" is used instead of "infrastructure"
# because the latter is noisy on non-tech employers (plumbers, facilities).
JOB_KEYWORDS = (
    "devops",
    "devsecops",
    "aws",
)

_JOB_RE = re.compile(
    r"\b(" + "|".join(re.escape(k) for k in JOB_KEYWORDS) + r")\b", re.IGNORECASE
)

# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class Company:
    name: str
    counts: dict = field(default_factory=dict)
    state: str = ""
    careers_page: str = ""

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def polite_sleep() -> None:
    time.sleep(random.uniform(*POLITE_DELAY))


def title_case(name: str) -> str:
    """Convert ALL CAPS company names to title case."""
    if not name.isupper():
        return name
    return name.title()


def normalize_name(name: str) -> str:
    s = name.lower().strip()
    s = re.sub(r"[^a-z0-9]+", " ", s)
    s = re.sub(r"\b(inc|llc|llp|corp|corporation|company|co|ltd|the)\b", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def now_pst() -> str:
    """Current timestamp string in US Pacific time."""
    return datetime.now(PST).strftime("%Y-%m-%d %H:%M:%S PST")


def is_it_job(title: str) -> bool:
    """True if a job title matches one of the roles we track (see JOB_KEYWORDS).

    Word-boundary match so "aws" does not hit "laws"/"draws" etc."""
    if not title:
        return False
    return bool(_JOB_RE.search(title))


_US_STATE_CODES = frozenset(
    "AL AK AZ AR CA CO CT DE FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN "
    "MS MO MT NE NV NH NJ NM NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA "
    "WA WV WI WY DC".split()
)

_US_SIGNALS = ("united states", "u.s.", "usa", "remote")

# Full state names (ATSes often write "Rochester, Minnesota" with no 2-letter code).
_US_STATE_NAMES = frozenset(
    "alabama alaska arizona arkansas california colorado connecticut delaware "
    "florida georgia hawaii idaho illinois indiana iowa kansas kentucky louisiana "
    "maine maryland massachusetts michigan minnesota mississippi missouri montana "
    "nebraska nevada hampshire jersey mexico york carolina dakota ohio oklahoma "
    "oregon pennsylvania rhode tennessee texas utah vermont virginia washington "
    "wisconsin wyoming columbia".split()
)


def is_us_location(location: str) -> bool:
    """True for US (or empty/unknown) locations; False for clearly non-US ones."""
    if not location:
        return True  # unknown location — keep rather than drop
    low = location.lower()
    if any(sig in low for sig in _US_SIGNALS):
        return True
    words = set(re.split(r"[^a-z]+", low))
    if words & _US_STATE_NAMES:
        return True
    tokens = re.split(r"[,\s/]+", location.upper())
    return any(tok in _US_STATE_CODES for tok in tokens)
