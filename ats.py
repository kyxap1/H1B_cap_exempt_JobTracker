"""ATS detection + keyword search adapters.

Most cap-exempt employers run a branded careers domain (e.g. jobs.mayoclinic.org)
that is really a Workday / Phenom / iCIMS tenant underneath. We detect which one
from the landing page once, cache the resolved API endpoint, then query that
endpoint directly over HTTP with the user's keywords — no per-page browser render.

Detection needs the rendered HTML (passed in by the caller, which already has a
Playwright page); the search itself is plain HTTP via curl_cffi.
"""

from __future__ import annotations

import re

from curl_cffi import requests as cf

# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------

# https://<tenant>.<dc>.myworkdayjobs.com/[locale/]<site>
_WORKDAY_LINK = re.compile(
    r"https://([a-z0-9-]+)\.(wd\d+)\.myworkdayjobs\.com/"
    r"(?:[a-z]{2}-[A-Z]{2}/)?([^/?#\"'&]+)",
    re.IGNORECASE,
)


def detect(html: str, final_url: str = "") -> dict | None:
    """Identify the ATS behind a careers page and resolve its search endpoint.

    Returns {"ats": <type>, "endpoint": <url>} or None if unrecognized.
    `html` is the rendered page source; `final_url` is the post-redirect URL.
    """
    ep = _detect_workday(html, final_url)
    if ep:
        return {"ats": "workday", "endpoint": ep}
    return None


def _detect_workday(html: str, final_url: str) -> str | None:
    # Either the page IS the workday site, or it links to one.
    m = _WORKDAY_LINK.search(final_url) or _WORKDAY_LINK.search(html)
    if not m:
        return None
    tenant, dc, site = m.group(1), m.group(2), m.group(3)
    return f"https://{tenant}.{dc}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs"


# ---------------------------------------------------------------------------
# Search adapters
# ---------------------------------------------------------------------------

def search(ats: str, endpoint: str, keyword: str, limit: int = 20) -> list[dict]:
    """Dispatch to the adapter for `ats`. Returns a list of job dicts:
    {title, url, location, posted}. Raises on HTTP error."""
    if ats == "workday":
        return _search_workday(endpoint, keyword, limit)
    raise ValueError(f"no search adapter for ats={ats!r}")


def _search_workday(endpoint: str, keyword: str, limit: int) -> list[dict]:
    host = endpoint.split("/wday/")[0]
    r = cf.post(
        endpoint,
        impersonate="chrome",
        timeout=30,
        headers={"Accept": "application/json", "Content-Type": "application/json"},
        json={"appliedFacets": {}, "limit": limit, "offset": 0, "searchText": keyword},
    )
    r.raise_for_status()
    data = r.json()
    jobs = []
    for jp in data.get("jobPostings", []):
        path = jp.get("externalPath", "")
        jobs.append({
            "title": jp.get("title", "").strip(),
            "url": (host + path) if path else "",
            "location": jp.get("locationsText", "").strip(),
            "posted": jp.get("postedOn", "").strip(),
        })
    return jobs
