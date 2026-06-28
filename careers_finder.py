"""Find company careers pages via Google search (Serper.dev API)."""

from __future__ import annotations

import json
import os
import re
import sys
from urllib.parse import urlparse

import requests

import ats
from config import CAREERS_KEYWORDS, AGGREGATOR_BLOCKLIST, polite_sleep

SERPER_API_KEY = os.environ.get("SERPER_API_KEY", "")

# URL shapes that mark an actual job portal (vs. an HR brochure page).
_PORTAL_URL = re.compile(
    r"/(jobs?/search|en-us/(?:listing|filter)|job[-_]?search|careers?/portal|"
    r"requisition|openings?|job[-_]?openings|search/?(?:$|\?))",
    re.IGNORECASE,
)
# Marketing / brochure paths we'd rather not land on.
_BROCHURE = re.compile(
    r"/(about|why[-_]|benefits|culture|life[-_]at|total[-_]rewards|our[-_]team|"
    r"students?|alumni|news|blog|diversity|contact|leadership)",
    re.IGNORECASE,
)
_PORTAL_HOST = ("jobs.", "careers.", "apply.", "employment.", "recruiting.", "jobsearch.")


def google_search(query: str, num: int = 10) -> list[str]:
    if not SERPER_API_KEY:
        sys.exit(
            "\nSerper API key not set. Google blocks automated search without an API key.\n"
            "1. Get a free key at https://serper.dev (2,500 searches/month, no credit card)\n"
            "2. Run:  export SERPER_API_KEY=your_key_here  then re-run the script."
        )
    try:
        resp = requests.post(
            "https://google.serper.dev/search",
            headers={"X-API-KEY": SERPER_API_KEY, "Content-Type": "application/json"},
            data=json.dumps({"q": query, "num": num, "gl": "us", "hl": "en"}),
            timeout=15,
        )
        resp.raise_for_status()
        return [r["link"] for r in resp.json().get("organic", [])]
    except Exception as e:
        print(f"  [serper error] {e}")
        return []


def _score_careers(url: str) -> int:
    """Rank a search result by how much it looks like a real job portal rather
    than an HR brochure page. Higher is better; -999 means reject (aggregator)."""
    low = url.lower()
    if any(bad in low for bad in AGGREGATOR_BLOCKLIST):
        return -999
    score = 0
    if ats.fingerprint(low):
        score += 10                      # already on a known ATS host / path
    if _PORTAL_URL.search(low):
        score += 6
    host = urlparse(low).netloc.split(":")[0]
    if host.startswith(_PORTAL_HOST):
        score += 3
    if any(k in low for k in CAREERS_KEYWORDS):
        score += 1
    if _BROCHURE.search(low):
        score -= 4
    return score


def find_careers_page(company: str) -> str:
    """Resolve a company's real job portal. Picks the highest-scoring result
    across a few queries, preferring an actual ATS / job-search URL over the HR
    brochure page that a bare "<company> careers" search usually returns first."""
    candidates: list[str] = []
    for query in (f"{company} careers", f"{company} jobs", f"{company} job openings apply"):
        candidates.extend(google_search(query))
        polite_sleep()

    best, best_score = "", 0
    for u in dict.fromkeys(candidates):   # dedupe, preserve order
        s = _score_careers(u)
        if s > best_score:
            best, best_score = u, s
    if best:
        return best

    # Fallback: first non-aggregator result for the official site.
    for u in google_search(f"{company} official site", num=5):
        if not any(bad in u.lower() for bad in AGGREGATOR_BLOCKLIST):
            return u
    return ""


