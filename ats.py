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


# Broad ATS fingerprints — used to LABEL a careers / portal page even when we
# don't (yet) have a search adapter for it. detect() below resolves a search
# endpoint only for the subset we can query; this wider table drives coverage
# stats and the decision to follow a landing page's "view jobs" link. Order
# matters: the first match wins, so list the more specific patterns first.
_FINGERPRINTS: tuple[tuple[str, "re.Pattern[str]"], ...] = tuple(
    (name, re.compile(pat, re.IGNORECASE)) for name, pat in (
        ("workday",         r"myworkdayjobs\.com|\.workday\.com|workdaycdn"),
        ("icims",           r"\.icims\.com"),
        ("phenom",          r"phenom|radancy|jibe|/search-jobs"),
        ("greenhouse",      r"greenhouse\.io|boards\.greenhouse|grnh\.se"),
        ("lever",           r"jobs\.lever\.co|//[^\"']*lever\.co/"),
        ("smartrecruiters", r"smartrecruiters\.com"),
        ("ashby",           r"ashbyhq\.com"),
        ("jobvite",         r"jobvite\.com"),
        ("taleo",           r"taleo\.net|taleo\.com|tbe\.taleo"),
        ("successfactors",  r"successfactors|sapsf|/careersection"),
        ("oracle-cloud",    r"oraclecloud\.com|/hcmui/|/recruitingce|fa\.oraclecloud"),
        ("oracle-irec",     r"irecruitment|/oa_html/"),
        ("brassring",       r"brassring\.com|kenexa"),
        ("peoplesoft",      r"/psc/|peoplesoft|/psp/|hcmprd"),
        ("peopleadmin",     r"peopleadmin\.com|schooljobs\.com"),
        ("cornerstone",     r"\.csod\.com"),
        ("ultipro",         r"ultipro\.com|ukg\.com"),
        ("adp",             r"workforcenow\.adp\.com|myjobs\.adp|recruiting\.adp"),
        ("paycom",          r"paycomonline\.net"),
        ("paylocity",       r"paylocity\.com"),
        ("interfolio",      r"interfolio\.com"),
        ("neogov",          r"neogov\.com|governmentjobs\.com"),
        # PageUp betrays itself statically in the URL path (its branded sites use
        # /en-us/listing/ and /en-us/filter/), so we catch it without racing the
        # late-loading widget XHR that branded domains hide it behind.
        ("pageup",          r"pageuppeople\.com|/en-us/(listing|filter)\b"),
        ("dayforce",        r"dayforcehcm|dayforce\.com"),
        ("workable",        r"workable\.com"),
        ("bamboohr",        r"bamboohr\.com"),
        ("jazzhr",          r"applytojob\.com|jazzhr"),
        ("recruitee",       r"recruitee\.com"),
    )
)


def fingerprint(text: str) -> str | None:
    """Return the ATS name a page belongs to (or None) by matching known host /
    path signatures anywhere in `text` (pass the final URL + rendered HTML). This
    only LABELS the ATS; detect() resolves a usable search endpoint for the
    subset we have adapters for."""
    for name, rx in _FINGERPRINTS:
        if rx.search(text):
            return name
    return None


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
    ep = _detect_icims(html, final_url)
    if ep:
        return {"ats": "icims", "endpoint": ep}
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


# iCIMS classic: a branded careers domain proxies <tenant>.icims.com; the search
# results render inside an iframe (...&in_iframe=1) where each job row carries its
# title, location, posted date, and position type as labeled fields.
_ICIMS_LINK = re.compile(r"https://([a-z0-9][a-z0-9-]*)\.icims\.com", re.IGNORECASE)


def _detect_icims(html: str, final_url: str) -> str | None:
    m = _ICIMS_LINK.search(final_url) or _ICIMS_LINK.search(html)
    if not m:
        return None
    tenant = m.group(1)
    return f"https://{tenant}.icims.com/jobs/search"


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
    if ats == "icims":
        return _search_icims(endpoint, keyword, limit)
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


_ICIMS_JOB = re.compile(r"/jobs/\d+/.+/job\b", re.IGNORECASE)


def _search_icims(endpoint: str, keyword: str, limit: int) -> list[dict]:
    host = endpoint.split("/jobs/search")[0]  # https://<tenant>.icims.com
    url = (f"{endpoint}?ss=1&searchKeyword={quote(keyword)}"
           f"&searchRelation=keyword_all&in_iframe=1")
    r = cf.get(url, impersonate="chrome", timeout=30)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    jobs, seen = [], set()
    for a in soup.select("a[href]"):
        href = a.get("href", "")
        if not _ICIMS_JOB.search(href):
            continue
        job_url = href.split("?")[0]
        if job_url.startswith("/"):
            job_url = host + job_url
        if job_url in seen:
            continue
        seen.add(job_url)
        row = a.find_parent("div", class_="row") or a.parent
        title = (_icims_field(row, "Requisition Title")
                 or _icims_field(row, "Title")
                 or re.sub(r"^(Requisition Title|Title)\s+", "",
                           a.get_text(" ", strip=True)))
        raw_loc = _icims_field(row, "Job Locations") or _icims_field(row, "Job Location")
        location, country = _icims_location(raw_loc)
        m = re.search(r"Position Type\s+([A-Za-z/ \-]+?)\s+(?:Department|Position Category|Requisition|FTE|HR Mission|$)",
                      row.get_text(" ", strip=True) if row else "")
        jobs.append({
            "title": title.strip(),
            "url": job_url,
            "location": location,
            "country": country,
            "time_type": m.group(1).strip() if m else "",
            "posted": _icims_field(row, "Posted Date"),
        })
        if len(jobs) >= limit:
            break
    return jobs


def _icims_field(row, label: str) -> str:
    """Read an iCIMS list field by its screen-reader label (e.g. "Job Locations").
    The label sits in a <span class="field-label"> whose parent holds the value."""
    if row is None:
        return ""
    for sp in row.select("span.field-label"):
        if sp.get_text(strip=True).rstrip(":").lower() == label.lower():
            val = sp.parent.get_text(" ", strip=True)
            return val.replace(sp.get_text(" ", strip=True), "", 1).strip()
    return ""


def _icims_location(raw: str) -> tuple[str, str]:
    """iCIMS encodes location as COUNTRY-STATE-CITY (e.g. "US-OR-Portland"),
    so the country code is the structured first segment. Returns (pretty, country)."""
    raw = (raw or "").strip()
    if not raw:
        return "", ""
    first = re.split(r"\s*\|\s*|\s{2,}|\n", raw)[0].strip()
    parts = [p.strip() for p in first.split("-") if p.strip()]
    country = parts[0] if parts else ""
    if len(parts) >= 3:
        pretty = f"{parts[2]}, {parts[1]}"
    elif len(parts) == 2:
        pretty = parts[1]
    else:
        pretty = first
    return pretty, country


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
