"""ATS detection + keyword search adapters.

Most cap-exempt employers run a branded careers domain (e.g. jobs.mayoclinic.org)
that is really a Workday / Phenom / iCIMS tenant underneath. We detect which one
from the landing page once, cache the resolved API endpoint, then query that
endpoint directly over HTTP with the user's keywords — no per-page browser render.

Detection needs the rendered HTML (passed in by the caller, which already has a
Playwright page); the search itself is plain HTTP via curl_cffi.
"""

from __future__ import annotations

import json
import re
from urllib.parse import quote, urljoin, urlparse

from bs4 import BeautifulSoup
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
    ep = _detect_phenom(html, final_url)
    if ep:
        return {"ats": "phenom", "endpoint": ep}
    return None


def _detect_workday(html: str, final_url: str) -> str | None:
    # Either the page IS the workday site, or it links to one.
    m = _WORKDAY_LINK.search(final_url) or _WORKDAY_LINK.search(html)
    if not m:
        return None
    tenant, dc, site = m.group(1), m.group(2), m.group(3)
    return f"https://{tenant}.{dc}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs"


# Phenom / Radancy career sites render results server-side at /search-jobs?k=...
_ORG_IDS = re.compile(r"orgIds?[=\":\s]+(\d{3,})", re.IGNORECASE)


def _detect_phenom(html: str, final_url: str) -> str | None:
    low = html.lower()
    if "radancy" not in low and "phenom" not in low and "/search-jobs" not in low:
        return None
    parsed = urlparse(final_url or "")
    if not parsed.scheme:
        return None
    base = f"{parsed.scheme}://{parsed.netloc}/search-jobs"
    m = _ORG_IDS.search(html)
    return f"{base}?orgIds={m.group(1)}" if m else base


# ---------------------------------------------------------------------------
# Search adapters
# ---------------------------------------------------------------------------

def search(ats: str, endpoint: str, keyword: str, limit: int = 20) -> list[dict]:
    """Dispatch to the adapter for `ats`. Returns a list of job dicts:
    {title, url, location, posted}. Raises on HTTP error."""
    if ats == "workday":
        return _search_workday(endpoint, keyword, limit)
    if ats == "phenom":
        return _search_phenom(endpoint, keyword, limit)
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
    detail_base = endpoint[: -len("/jobs")]
    jobs = []
    for jp in data.get("jobPostings", []):
        path = jp.get("externalPath", "")
        job = {
            "title": jp.get("title", "").strip(),
            "url": (host + path) if path else "",
            "location": jp.get("locationsText", "").strip(),
            "time_type": jp.get("timeType", "").strip(),
            "posted": jp.get("postedOn", "").strip(),
            "country": "",
        }
        # The search summary has no country; the job detail does (structured).
        if path:
            try:
                info = cf.get(detail_base + path, impersonate="chrome", timeout=30,
                              headers={"Accept": "application/json"}).json()
                info = info.get("jobPostingInfo", {})
                job["country"] = (info.get("country") or {}).get("descriptor", "")
                job["time_type"] = info.get("timeType", job["time_type"])
            except Exception:
                pass
        jobs.append(job)
    return jobs


def _search_phenom(endpoint: str, keyword: str, limit: int) -> list[dict]:
    sep = "&" if "?" in endpoint else "?"
    url = f"{endpoint}{sep}k={quote(keyword)}"
    r = cf.get(url, impersonate="chrome", timeout=30)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    scope = soup.select_one("#search-results-list") or soup
    jobs = []
    for a in scope.select("a[href*='/job/']"):
        loc_el = a.find(attrs={"class": re.compile("location", re.I)})
        location = loc_el.get_text(" ", strip=True) if loc_el else ""
        title_el = a.find(["h2", "h3", "h4"])
        if title_el:
            title = title_el.get_text(" ", strip=True)
        else:
            title = a.get_text(" ", strip=True)
            if location:
                title = title.replace(location, "").strip()
        type_el = a.find(attrs={"class": re.compile(r"job-?type|category", re.I)})
        time_type = type_el.get_text(" ", strip=True) if type_el else ""
        if title:
            jobs.append({
                "title": title,
                "url": urljoin(endpoint, a["href"]),
                "location": location,
                "time_type": time_type,
                "posted": "",
                "country": "",
            })
        if len(jobs) >= limit:
            break
    # Enrich each hit with structured data from its job page (schema.org JSON-LD).
    for job in jobs:
        _enrich_phenom(job)
    return jobs


def _enrich_phenom(job: dict) -> None:
    """Pull structured country / location / employmentType from the job page's
    schema.org JobPosting JSON-LD."""
    try:
        html = cf.get(job["url"], impersonate="chrome", timeout=30).text
    except Exception:
        return
    jp = _ldjson_jobposting(html)
    if not jp:
        return
    loc = jp.get("jobLocation")
    if isinstance(loc, list):
        loc = loc[0] if loc else None
    addr = (loc or {}).get("address", {}) if isinstance(loc, dict) else {}
    country = addr.get("addressCountry", "")
    if isinstance(country, dict):
        country = country.get("name", "")
    locality = addr.get("addressLocality", "")
    region = addr.get("addressRegion", "")
    if country:
        job["country"] = country
    pretty = ", ".join(x for x in (locality, region) if x)
    if pretty:
        job["location"] = pretty
    etype = jp.get("employmentType")
    if etype:
        job["time_type"] = etype if isinstance(etype, str) else ", ".join(etype)


def _ldjson_jobposting(html: str) -> dict | None:
    soup = BeautifulSoup(html, "html.parser")
    for s in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(s.string or "")
        except Exception:
            continue
        for item in (data if isinstance(data, list) else [data]):
            if isinstance(item, dict) and item.get("@type") == "JobPosting":
                return item
    return None
