"""Find company careers pages via Google search (Serper.dev API)."""

from __future__ import annotations

import json
import os
import re
import sys
from urllib.parse import urlparse

import requests

import ats
from config import CAREERS_KEYWORDS, AGGREGATOR_BLOCKLIST, SERPER_CACHE, polite_sleep

SERPER_API_KEY = os.environ.get("SERPER_API_KEY", "")

# A real job portal scores at least this; once we have one we stop firing
# further Serper queries for the same company (the free tier is rate-limited).
_STRONG_SCORE = 6
# Hard cap on Serper queries per company, so a full refresh of ~650 weak
# entries stays within the monthly quota (≤2 × 650 ≈ 1300 calls).
_MAX_QUERIES = 2

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


def _load_cache() -> dict:
    if SERPER_CACHE.exists():
        try:
            return json.loads(SERPER_CACHE.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def _save_cache(cache: dict) -> None:
    SERPER_CACHE.write_text(json.dumps(cache, ensure_ascii=False, indent=2))


def google_search(query: str, num: int = 10) -> list[str]:
    """Return result links for a query, reusing a persisted cache so the same
    query never costs a second Serper call. Cache key includes `num` because a
    different result count is a different request."""
    cache = _load_cache()
    key = f"{num}:{query}"
    if key in cache:
        print("  [cache] " + query)
        return cache[key]

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
        links = [r["link"] for r in resp.json().get("organic", [])]
    except Exception as e:
        print(f"  [serper error] {e}")
        return []

    cache[key] = links
    _save_cache(cache)
    return links


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
    queries = (f"{company} careers", f"{company} jobs", f"{company} job openings apply")
    candidates: list[str] = []
    best, best_score = "", 0
    for query in queries[:_MAX_QUERIES]:
        candidates.extend(google_search(query))
        polite_sleep()
        for u in dict.fromkeys(candidates):   # dedupe, preserve order
            s = _score_careers(u)
            if s > best_score:
                best, best_score = u, s
        if best_score >= _STRONG_SCORE:       # already found a real portal
            break
    if best:
        return best

    # Fallback: first non-aggregator result for the official site.
    for u in google_search(f"{company} official site", num=5):
        if not any(bad in u.lower() for bad in AGGREGATOR_BLOCKLIST):
            return u
    return ""


