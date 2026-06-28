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
import xml.etree.ElementTree as ET
from urllib.parse import parse_qs, quote, urljoin, urlparse

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
        ("peopleadmin",     r"peopleadmin|pa-hrsuite|schooljobs\.com"),
        ("cornerstone",     r"\.csod\.com"),
        ("healthcaresource", r"hctsportals\.com|healthcaresource"),
        ("talentbrew",      r"talentbrew\.com|tmpwebeng\.com"),
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
    ep = _detect_pageup(html, final_url)
    if ep:
        # PageUp branded sites sit behind an AWS WAF JS challenge, so unlike the
        # others this one is searched through the browser, not plain HTTP.
        return {"ats": "pageup", "endpoint": ep, "transport": "browser"}
    ep = _detect_oracle(html, final_url)
    if ep:
        return {"ats": "oracle-cloud", "endpoint": ep}
    ep = _detect_taleo(html, final_url)
    if ep:
        return {"ats": "taleo", "endpoint": ep}
    ep = _detect_peopleadmin(html, final_url)
    if ep:
        return {"ats": "peopleadmin", "endpoint": ep}
    # Clean-JSON-API ATSes (public boards endpoints, no browser needed).
    ep = _detect_greenhouse(html, final_url)
    if ep:
        return {"ats": "greenhouse", "endpoint": ep}
    ep = _detect_lever(html, final_url)
    if ep:
        return {"ats": "lever", "endpoint": ep}
    ep = _detect_smartrecruiters(html, final_url)
    if ep:
        return {"ats": "smartrecruiters", "endpoint": ep}
    ep = _detect_ashby(html, final_url)
    if ep:
        return {"ats": "ashby", "endpoint": ep}
    ep = _detect_workable(html, final_url)
    if ep:
        return {"ats": "workable", "endpoint": ep}
    ep = _detect_ultipro(html, final_url)
    if ep:
        return {"ats": "ultipro", "endpoint": ep}
    ep = _detect_paylocity(html, final_url)
    if ep:
        return {"ats": "paylocity", "endpoint": ep}
    ep = _detect_interfolio(html, final_url)
    if ep:
        return {"ats": "interfolio", "endpoint": ep}
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


# PageUp career sites live at <origin>[/<site>]/en-us/(listing|filter|job)/...
# The path segment before /en-us/ (e.g. "/st") is the tenant's site code and must
# be preserved when we build the keyword-search URL.
_PAGEUP_PATH = re.compile(
    r"(https?://[^\s\"'<>]+?)/en-us/(?:listing|filter|job)\b", re.IGNORECASE
)


def _detect_pageup(html: str, final_url: str) -> str | None:
    m = _PAGEUP_PATH.search(final_url) or _PAGEUP_PATH.search(html)
    if m:
        return f"{m.group(1)}/en-us/filter/"
    # Branded domains expose PageUp only through the careers-static.pageuppeople.com
    # widget host (seen in the page's network calls). Resolve the search URL from
    # the careers page's own origin.
    if "pageuppeople.com" in (final_url + html).lower():
        parsed = urlparse(final_url or "")
        if parsed.scheme and parsed.netloc:
            return f"{parsed.scheme}://{parsed.netloc}/en-us/filter/"
    return None


# Oracle Recruiting Cloud (Fusion HCM) external candidate sites live on a pod host
# <name>.fa.<dc>.oraclecloud.com and expose a public REST resource we can query
# directly. The careers URL carries the site code as /sites/<CX_n>/, which the
# REST finder needs; default to CX_1 (the common case) when it isn't in the URL.
_ORACLE_LINK = re.compile(
    r"https://([a-z0-9-]+\.fa\.[a-z0-9-]+\.oraclecloud\.com)", re.IGNORECASE
)
_ORACLE_SITE = re.compile(r"/sites/([A-Za-z0-9_]+)", re.IGNORECASE)


def _detect_oracle(html: str, final_url: str) -> str | None:
    m = _ORACLE_LINK.search(final_url) or _ORACLE_LINK.search(html)
    if not m:
        return None
    host = m.group(1)
    sm = _ORACLE_SITE.search(final_url) or _ORACLE_SITE.search(html)
    site = sm.group(1) if sm else "CX_1"
    return (f"https://{host}/hcmRestApi/resources/latest/"
            f"recruitingCEJobRequisitions?siteNumber={site}")


# Taleo Enterprise career sites live at <tenant>.taleo.net/careersection/<cs>/...
# and serve results from a JSON REST endpoint (rest/jobboard/searchjobs) that
# needs the section's numeric `portal` id (embedded in the page) plus a session
# cookie from a prior GET. We carry host + careersection + portal in the endpoint
# URL; _search_taleo rebuilds the REST call from them. (Taleo Business Edition on
# *.tbe.taleo.net is a different product and is intentionally not matched here.)
_TALEO_LINK = re.compile(
    r"https://([a-z0-9-]+\.taleo\.net)/careersection/([^/?#]+)/", re.IGNORECASE
)
_TALEO_PORTAL = re.compile(r"portal[=\"':\s]+(\d{5,})", re.IGNORECASE)


def _detect_taleo(html: str, final_url: str) -> str | None:
    blob = f"{final_url}\n{html}"
    m = _TALEO_LINK.search(final_url) or _TALEO_LINK.search(html)
    if not m:
        return None
    host, cs = m.group(1), m.group(2)
    pm = _TALEO_PORTAL.search(blob)
    if not pm:
        # Without the portal id the REST endpoint can't be queried; leave it for
        # the fingerprint to label rather than returning a broken endpoint.
        return None
    return f"https://{host}/careersection/{cs}/jobsearch.ftl?portal={pm.group(1)}"


# PeopleAdmin career sites expose an Atom job feed at /postings/search.atom. Most
# live on <tenant>.peopleadmin.com, but many run on the institution's own domain
# (e.g. uscjobs.sc.edu, uvmjobs.com) — those still carry "peopleadmin" markers in
# their HTML (the product name and a pa-hrsuite asset bucket), so we fall back to
# the page's own origin for the search base. (schooljobs.com shares the broad
# fingerprint but is NeoGov's product, so it's handled there, not here.)
_PEOPLEADMIN_LINK = re.compile(r"https://([a-z0-9-]+\.peopleadmin\.com)", re.IGNORECASE)
_PEOPLEADMIN_MARK = re.compile(r"peopleadmin|pa-hrsuite", re.IGNORECASE)


def _detect_peopleadmin(html: str, final_url: str) -> str | None:
    m = _PEOPLEADMIN_LINK.search(final_url) or _PEOPLEADMIN_LINK.search(html)
    if m:
        return f"https://{m.group(1)}/postings/search"
    if _PEOPLEADMIN_MARK.search(final_url) or _PEOPLEADMIN_MARK.search(html):
        parsed = urlparse(final_url or "")
        if parsed.scheme and parsed.netloc and "schooljobs.com" not in parsed.netloc:
            return f"{parsed.scheme}://{parsed.netloc}/postings/search"
    return None


_ATOM_NS = {"a": "http://www.w3.org/2005/Atom"}


def _search_peopleadmin(endpoint: str, keyword: str, limit: int) -> list[dict]:
    """Read a PeopleAdmin career site's Atom job feed (/postings/search.atom),
    which returns structured entries (title, link, published date) — far cleaner
    than scraping the responsive HTML grid. The feed has no discrete location
    field, and every tenant in this dataset is a US university, so country is
    tagged US for the US-location filter."""
    url = f"{endpoint}.atom?query={quote(keyword)}"
    r = cf.get(url, impersonate="chrome", timeout=30)
    r.raise_for_status()
    root = ET.fromstring(r.content)
    jobs = []
    for e in root.findall("a:entry", _ATOM_NS)[:limit]:
        title = (e.findtext("a:title", default="", namespaces=_ATOM_NS) or "").strip()
        if not title:
            continue
        link_el = e.find("a:link", _ATOM_NS)
        href = (link_el.get("href") if link_el is not None else "") or \
            (e.findtext("a:id", default="", namespaces=_ATOM_NS) or "").strip()
        jobs.append({
            "title": title,
            "url": href,
            "location": "",
            "country": "US",
            "time_type": "",
            "posted": (e.findtext("a:published", default="", namespaces=_ATOM_NS) or "").strip(),
        })
    return jobs


# Greenhouse, Lever, SmartRecruiters, Ashby and Workable each expose a public
# JSON board API keyed by a single org slug, so detection is just "find the slug,
# build the API URL". The slug appears in the branded careers URL or an embedded
# board script; for Greenhouse the embed form (?for=<token>) takes priority over
# the host-path form so we don't mistake the literal "embed" segment for a token.
_GREENHOUSE_FOR = re.compile(
    r"greenhouse\.io/embed/job_board(?:/js)?\?for=([a-z0-9_]+)", re.IGNORECASE)
_GREENHOUSE_HOST = re.compile(
    r"(?:boards|job-boards)\.greenhouse\.io/(?:embed/job_board\?for=)?([a-z0-9_]+)",
    re.IGNORECASE)


def _detect_greenhouse(html: str, final_url: str) -> str | None:
    blob = f"{final_url}\n{html}"
    m = _GREENHOUSE_FOR.search(blob) or _GREENHOUSE_HOST.search(blob)
    if not m or m.group(1) in ("embed", "job_board"):
        return None
    return f"https://boards-api.greenhouse.io/v1/boards/{m.group(1)}/jobs?content=true"


_LEVER_LINK = re.compile(r"jobs\.lever\.co/([a-z0-9][a-z0-9-]*)", re.IGNORECASE)


def _detect_lever(html: str, final_url: str) -> str | None:
    m = _LEVER_LINK.search(final_url) or _LEVER_LINK.search(html)
    if not m:
        return None
    return f"https://api.lever.co/v0/postings/{m.group(1)}?mode=json"


# careers.smartrecruiters.com/<Company> (the path segment is the API identifier).
_SR_LINK = re.compile(
    r"(?:careers|jobs)\.smartrecruiters\.com/([A-Za-z0-9._-]+)", re.IGNORECASE)


def _detect_smartrecruiters(html: str, final_url: str) -> str | None:
    m = _SR_LINK.search(final_url) or _SR_LINK.search(html)
    if not m:
        return None
    return f"https://api.smartrecruiters.com/v1/companies/{m.group(1)}/postings"


_ASHBY_LINK = re.compile(r"jobs\.ashbyhq\.com/([A-Za-z0-9._-]+)", re.IGNORECASE)


def _detect_ashby(html: str, final_url: str) -> str | None:
    m = _ASHBY_LINK.search(final_url) or _ASHBY_LINK.search(html)
    if not m:
        return None
    return f"https://api.ashbyhq.com/posting-api/job-board/{m.group(1)}"


# Workable hosts branded boards at apply.workable.com/<account>/ and queries them
# through a public POST search endpoint; the account slug is the API identifier.
_WORKABLE_LINK = re.compile(
    r"(?:apply\.workable\.com/|([a-z0-9-]+)\.workable\.com)", re.IGNORECASE)
_WORKABLE_APPLY = re.compile(r"apply\.workable\.com/([a-z0-9-]+)", re.IGNORECASE)


def _detect_workable(html: str, final_url: str) -> str | None:
    blob = f"{final_url}\n{html}"
    m = _WORKABLE_APPLY.search(blob)
    account = m.group(1) if m else None
    if not account:
        m = re.search(r"([a-z0-9-]+)\.workable\.com", blob, re.IGNORECASE)
        account = m.group(1) if m else None
    if not account or account in ("apply", "www"):
        return None
    return f"https://apply.workable.com/api/v3/accounts/{account}/jobs"


# UKG/UltiPro recruiting boards live at recruiting[N].ultipro.com/<tenant>/JobBoard/
# <board-guid>/ and answer a JSON POST (LoadSearchResults) with fully structured
# rows (title, address, posted date). We carry host + tenant + board in the
# endpoint and rebuild the POST / detail URLs from it.
_ULTIPRO_LINK = re.compile(
    r"(recruiting\d*\.(?:ultipro|ukg)\.com)/([A-Za-z0-9]+)/JobBoard/([0-9a-fA-F-]{36})",
    re.IGNORECASE)


def _detect_ultipro(html: str, final_url: str) -> str | None:
    m = _ULTIPRO_LINK.search(final_url) or _ULTIPRO_LINK.search(html)
    if not m:
        return None
    host, tenant, board = m.group(1), m.group(2), m.group(3)
    return f"https://{host}/{tenant}/JobBoard/{board}"


# Paylocity recruiting boards are server-rendered (Angular Universal): the full
# job list is inlined into the page as a `window.pageData` JSON object and the
# in-page search just filters it client-side — there is no per-query XHR API. So
# we fetch the board once and read that JSON payload (structured city/state/
# country per job), which is the data source, not screen-scraped markup. The
# board is keyed by a company GUID in the URL.
_PAYLOCITY_LINK = re.compile(
    r"recruiting\.paylocity\.com/recruiting/jobs/All/([0-9a-fA-F-]{36})"
    r"(?:/([^/?#\"']+))?", re.IGNORECASE)


def _detect_paylocity(html: str, final_url: str) -> str | None:
    m = _PAYLOCITY_LINK.search(final_url) or _PAYLOCITY_LINK.search(html)
    if not m:
        return None
    guid = m.group(1)
    name = m.group(2) or "Careers"
    return f"https://recruiting.paylocity.com/recruiting/jobs/All/{guid}/{name}"


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
    if ats == "oracle-cloud":
        return _search_oracle(endpoint, keyword, limit)
    if ats == "taleo":
        return _search_taleo(endpoint, keyword, limit)
    if ats == "peopleadmin":
        return _search_peopleadmin(endpoint, keyword, limit)
    if ats == "greenhouse":
        return _search_greenhouse(endpoint, keyword, limit)
    if ats == "lever":
        return _search_lever(endpoint, keyword, limit)
    if ats == "smartrecruiters":
        return _search_smartrecruiters(endpoint, keyword, limit)
    if ats == "ashby":
        return _search_ashby(endpoint, keyword, limit)
    if ats == "workable":
        return _search_workable(endpoint, keyword, limit)
    if ats == "ultipro":
        return _search_ultipro(endpoint, keyword, limit)
    if ats == "paylocity":
        return _search_paylocity(endpoint, keyword, limit)
    if ats == "interfolio":
        return _search_interfolio(endpoint, keyword, limit)
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


def _search_oracle(endpoint: str, keyword: str, limit: int) -> list[dict]:
    """Query an Oracle Recruiting Cloud pod's public REST resource. The job rows
    come back fully structured (title, location, country code, posted date), so
    no per-job detail fetch is needed."""
    parsed = urlparse(endpoint)
    host = f"{parsed.scheme}://{parsed.netloc}"
    base = f"{host}{parsed.path}"
    site = parse_qs(parsed.query).get("siteNumber", ["CX_1"])[0]
    finder = (f'findReqs;siteNumber={site},limit={limit},'
              f'keyword="{keyword}",sortBy="POSTING_DATES_DESC"')
    r = cf.get(
        base,
        impersonate="chrome",
        timeout=30,
        headers={"Accept": "application/json"},
        params={
            "onlyData": "true",
            "expand": "requisitionList.secondaryLocations,flexFieldsFacet.values",
            "finder": finder,
        },
    )
    r.raise_for_status()
    items = r.json().get("items", [])
    reqs = items[0].get("requisitionList", []) if items else []
    jobs = []
    for rq in reqs:
        jid = str(rq.get("Id", "")).strip()
        secondary = rq.get("secondaryLocations") or []
        extra = ", ".join(s.get("Name", "") for s in secondary if s.get("Name"))
        location = (rq.get("PrimaryLocation") or "").strip()
        if extra:
            location = f"{location}; {extra}" if location else extra
        jobs.append({
            "title": (rq.get("Title") or "").strip(),
            "url": (f"{host}/hcmUI/CandidateExperience/en/sites/{site}/job/{jid}"
                    if jid else ""),
            "location": location,
            "country": (rq.get("PrimaryLocationCountry") or "").strip(),
            "time_type": (rq.get("WorkplaceTypeCode") or "").strip(),
            "posted": (rq.get("PostedDate") or "").strip(),
        })
        if len(jobs) >= limit:
            break
    return jobs


# Body the Taleo job board expects; KEYWORD is filled in per query. sortBy "3"
# with descending order is the site's "most recent" sort.
_TALEO_BODY = {
    "multilineEnabled": False,
    "sortingSelection": {"sortBySelectionParam": "3", "ascendingSortingOrder": "false"},
    "fieldData": {"fields": {"KEYWORD": "", "LOCATION": ""}, "valid": True},
    "filterSelectionParam": {"searchFilterSelections": [
        {"id": "ORGANIZATION", "selectedValues": []},
        {"id": "LOCATION", "selectedValues": []},
        {"id": "JOB_FIELD", "selectedValues": []},
        {"id": "POSTING_DATE", "selectedValues": []}]},
    "advancedSearchFiltersSelectionParam": {"searchFilterSelections": []},
    "pageNo": 1,
}


def _taleo_location(locs: list) -> tuple[str, str]:
    """Taleo encodes each location as STATE-City-Site (e.g. "MN-Minneapolis-Main"),
    occasionally prefixed with the country. Returns (pretty, country)."""
    if not locs:
        return "", ""
    first = str(locs[0])
    parts = [p.strip() for p in first.split("-") if p.strip()]
    country = ""
    if parts and re.fullmatch(r"[A-Za-z]{2}", parts[0]):
        country = "US"
        pretty = f"{parts[1]}, {parts[0]}" if len(parts) >= 2 else first
    elif "united states" in first.lower():
        country = "US"
        pretty = ", ".join(parts[1:3]) or first
    else:
        pretty = ", ".join(parts[:2]) or first
    if len(locs) > 1:
        pretty += f" (+{len(locs) - 1} more)"
    return pretty, country


def _search_taleo(endpoint: str, keyword: str, limit: int) -> list[dict]:
    """Query a Taleo Enterprise career section's job board. Each result's `column`
    list holds the configured cell values; `linkedColumn` / `locationsColumns`
    give the indexes of the title and location cells (the order is tenant-specific,
    so we follow those hints rather than assume fixed positions)."""
    parsed = urlparse(endpoint)
    host = parsed.netloc
    cs = parsed.path.split("/careersection/")[1].split("/")[0]
    portal = parse_qs(parsed.query).get("portal", [""])[0]
    base = f"https://{host}"

    sess = cf.Session(impersonate="chrome")
    sess.get(f"{base}/careersection/{cs}/jobsearch.ftl?lang=en", timeout=30)
    body = json.loads(json.dumps(_TALEO_BODY))
    body["fieldData"]["fields"]["KEYWORD"] = keyword
    r = sess.post(
        f"{base}/careersection/rest/jobboard/searchjobs?lang=en&portal={portal}",
        json=body, timeout=30,
        headers={"tz": "GMT+00:00", "accept": "application/json, text/javascript, */*; q=0.01"},
    )
    r.raise_for_status()
    jobs = []
    for jr in r.json().get("requisitionList", [])[:limit]:
        cols = jr.get("column", [])
        li = jr.get("linkedColumn", 0)
        title = cols[li] if isinstance(li, int) and 0 <= li < len(cols) else (cols[0] if cols else "")
        location, country = "", ""
        loc_idxs = jr.get("locationsColumns", [])
        if loc_idxs and isinstance(loc_idxs[0], int) and loc_idxs[0] < len(cols):
            raw = cols[loc_idxs[0]]
            try:
                locs = json.loads(raw) if str(raw).lstrip().startswith("[") else [raw]
            except (json.JSONDecodeError, ValueError):
                locs = [raw]
            location, country = _taleo_location(locs)
        contest = str(jr.get("contestNo", "")).strip()
        jobs.append({
            "title": str(title).strip(),
            "url": (f"{base}/careersection/{cs}/jobdetail.ftl?job={contest}&lang=en"
                    if contest else ""),
            "location": location,
            "country": country,
            "time_type": "",
            "posted": "",
        })
    return jobs


def _kw_match(keyword: str, *fields: str) -> bool:
    """True if the keyword (case-insensitive) appears in any of the given fields.
    Used by the board APIs that have no server-side keyword filter, so we fetch
    the full posting list once and filter locally."""
    if not keyword:
        return True
    kw = keyword.lower()
    return any(kw in (f or "").lower() for f in fields)


def _search_greenhouse(endpoint: str, keyword: str, limit: int) -> list[dict]:
    """Greenhouse job board API: one GET returns every live posting (title, URL,
    location, updated date) with no server-side keyword filter, so we match the
    keyword against the title locally."""
    r = cf.get(endpoint, impersonate="chrome", timeout=30,
               headers={"Accept": "application/json"})
    r.raise_for_status()
    jobs = []
    for j in r.json().get("jobs", []):
        title = (j.get("title") or "").strip()
        if not _kw_match(keyword, title):
            continue
        jobs.append({
            "title": title,
            "url": j.get("absolute_url", ""),
            "location": (j.get("location") or {}).get("name", "").strip(),
            "country": "",
            "time_type": "",
            "posted": (j.get("updated_at") or "")[:10],
        })
        if len(jobs) >= limit:
            break
    return jobs


def _search_lever(endpoint: str, keyword: str, limit: int) -> list[dict]:
    """Lever postings API: returns a flat JSON list of postings. No server-side
    keyword filter, so we match the keyword against the title locally."""
    r = cf.get(endpoint, impersonate="chrome", timeout=30,
               headers={"Accept": "application/json"})
    r.raise_for_status()
    jobs = []
    for p in r.json():
        title = (p.get("text") or "").strip()
        if not _kw_match(keyword, title):
            continue
        cats = p.get("categories") or {}
        jobs.append({
            "title": title,
            "url": p.get("hostedUrl", ""),
            "location": (cats.get("location") or "").strip(),
            "country": "",
            "time_type": (cats.get("commitment") or "").strip(),
            "posted": (p.get("createdAt") and _epoch_ms_date(p["createdAt"])) or "",
        })
        if len(jobs) >= limit:
            break
    return jobs


def _search_smartrecruiters(endpoint: str, keyword: str, limit: int) -> list[dict]:
    """SmartRecruiters posting API: supports a server-side keyword filter via `q`,
    and returns structured location (city / region / 2-letter country)."""
    company = endpoint.rstrip("/").split("/companies/")[1].split("/")[0]
    r = cf.get(endpoint, impersonate="chrome", timeout=30,
               headers={"Accept": "application/json"},
               params={"q": keyword, "limit": min(limit, 100)})
    r.raise_for_status()
    jobs = []
    for c in r.json().get("content", []):
        loc = c.get("location") or {}
        location = ", ".join(x for x in (loc.get("city"), loc.get("region")) if x)
        jid = c.get("id", "")
        jobs.append({
            "title": (c.get("name") or "").strip(),
            "url": f"https://jobs.smartrecruiters.com/{company}/{jid}" if jid else "",
            "location": location,
            "country": (loc.get("country") or "").upper(),
            "time_type": (c.get("typeOfEmployment") or {}).get("label", ""),
            "posted": (c.get("releasedDate") or "")[:10],
        })
        if len(jobs) >= limit:
            break
    return jobs


def _search_ashby(endpoint: str, keyword: str, limit: int) -> list[dict]:
    """Ashby job-board API: one GET returns every listed posting (title, jobUrl,
    location, employmentType). No server-side keyword filter — match locally."""
    r = cf.get(endpoint, impersonate="chrome", timeout=30,
               headers={"Accept": "application/json"})
    r.raise_for_status()
    jobs = []
    for j in r.json().get("jobs", []):
        title = (j.get("title") or "").strip()
        if not _kw_match(keyword, title):
            continue
        jobs.append({
            "title": title,
            "url": j.get("jobUrl", ""),
            "location": (j.get("location") or "").strip(),
            "country": "",
            "time_type": (j.get("employmentType") or "").strip(),
            "posted": (j.get("publishedAt") or "")[:10],
        })
        if len(jobs) >= limit:
            break
    return jobs


def _search_workable(endpoint: str, keyword: str, limit: int) -> list[dict]:
    """Workable account search API: POST the keyword and read back structured
    postings (title, shortcode, city / region / 2-letter country code)."""
    account = endpoint.rstrip("/").split("/accounts/")[1].split("/")[0]
    r = cf.post(endpoint, impersonate="chrome", timeout=30,
                headers={"Accept": "application/json", "Content-Type": "application/json"},
                json={"query": keyword})
    r.raise_for_status()
    jobs = []
    for j in r.json().get("results", []):
        loc = j if isinstance(j.get("location"), str) else (j.get("location") or {})
        if isinstance(loc, str):
            location, country = loc, ""
        else:
            location = ", ".join(x for x in (loc.get("city"), loc.get("region")) if x)
            country = (loc.get("countryCode") or loc.get("country") or "").upper()
        shortcode = j.get("shortcode", "")
        jobs.append({
            "title": (j.get("title") or "").strip(),
            "url": j.get("url") or (f"https://apply.workable.com/{account}/j/{shortcode}/"
                                    if shortcode else ""),
            "location": location,
            "country": country,
            "time_type": (j.get("type") or "").strip(),
            "posted": (j.get("published") or j.get("created") or "")[:10],
        })
        if len(jobs) >= limit:
            break
    return jobs


def _ultipro_location(locs: list) -> tuple[str, str]:
    """Read the first UltiPro location's structured address. Country comes back as
    a 3-letter code (USA), normalized to the 2-letter US the location filter uses."""
    if not locs:
        return "", ""
    addr = (locs[0].get("Address") or {})
    city = addr.get("City") or ""
    state = (addr.get("State") or {}).get("Code") or ""
    cc = (addr.get("Country") or {}).get("Code") or ""
    country = "US" if cc in ("USA", "US") else cc
    pretty = ", ".join(x for x in (city, state) if x)
    if len(locs) > 1:
        pretty += f" (+{len(locs) - 1} more)"
    return pretty, country


def _search_ultipro(endpoint: str, keyword: str, limit: int) -> list[dict]:
    """Query a UKG/UltiPro recruiting board via its JSON search endpoint. Rows are
    structured (title, address, posted date), so no per-job detail fetch needed."""
    r = cf.post(
        f"{endpoint}/JobBoardView/LoadSearchResults",
        impersonate="chrome", timeout=30,
        headers={"Accept": "application/json", "Content-Type": "application/json"},
        json={"opportunitySearch": {"Top": limit, "Skip": 0, "SearchText": keyword,
                                    "Sort": [{"Order": "Desc", "PropertyName": "PostedDate"}]}},
    )
    r.raise_for_status()
    jobs = []
    for o in r.json().get("opportunities", [])[:limit]:
        location, country = _ultipro_location(o.get("Locations") or [])
        oid = o.get("Id", "")
        full = o.get("FullTime")
        jobs.append({
            "title": (o.get("Title") or "").strip(),
            "url": f"{endpoint}/OpportunityDetail?opportunityId={oid}" if oid else "",
            "location": location,
            "country": country,
            "time_type": "Full time" if full else ("Part time" if full is False else ""),
            "posted": (o.get("PostedDate") or "")[:10],
        })
    return jobs


# Interfolio faculty boards (apply.interfolio.com/<institution>/positions) are an
# Angular SPA fed by a public JSON search API on logic.interfolio.com, keyed by
# the institution/tenant id in the URL. The API filters by keyword server-side.
_INTERFOLIO_LINK = re.compile(r"apply\.interfolio\.com/(\d+)/positions", re.IGNORECASE)


def _detect_interfolio(html: str, final_url: str) -> str | None:
    m = _INTERFOLIO_LINK.search(final_url) or _INTERFOLIO_LINK.search(html)
    if not m:
        return None
    return f"https://logic.interfolio.com/byc-search/{m.group(1)}/public_job_boards"


def _search_interfolio(endpoint: str, keyword: str, limit: int) -> list[dict]:
    """Query an Interfolio institution's public job board API. Results are
    structured (name, location, id, open date); `search` filters server-side."""
    r = cf.get(endpoint, impersonate="chrome", timeout=30,
               headers={"Accept": "application/json"},
               params={"search": keyword, "limit": limit, "page": 1,
                       "sort_by": "name", "sort_order": "asc", "unit_name": ""})
    r.raise_for_status()
    jobs = []
    for j in r.json().get("results", [])[:limit]:
        pid = j.get("id")
        jobs.append({
            "title": (j.get("name") or "").strip(),
            "url": f"https://apply.interfolio.com/{pid}" if pid else "",
            "location": (j.get("location") or "").strip(),
            "country": "",
            "time_type": "",
            "posted": (j.get("open_date_raw") or "")[:10],
        })
    return jobs


_PAYLOCITY_DATA = re.compile(r"window\.pageData\s*=\s*(\{.*?\});", re.S)


def _search_paylocity(endpoint: str, keyword: str, limit: int) -> list[dict]:
    """Read a Paylocity board's inlined `window.pageData` JSON (the full job list)
    and filter by keyword locally — the site itself searches client-side, so one
    fetch returns everything with structured city / state / country per job."""
    html = cf.get(endpoint, impersonate="chrome", timeout=30).text
    m = _PAYLOCITY_DATA.search(html)
    if not m:
        return []
    data = json.loads(m.group(1))
    jobs = []
    for j in data.get("Jobs", []):
        title = (j.get("JobTitle") or "").strip()
        if not _kw_match(keyword, title):
            continue
        loc = j.get("JobLocation") or {}
        city, state = loc.get("City") or "", loc.get("State") or ""
        cc = loc.get("Country") or ""
        country = "US" if cc in ("USA", "US") else cc
        location = ", ".join(x for x in (city, state) if x) or (j.get("LocationName") or "")
        if j.get("IsRemote"):
            location = (location + " (Remote)").strip()
        jid = j.get("JobId")
        jobs.append({
            "title": title,
            "url": (f"https://recruiting.paylocity.com/Recruiting/Jobs/Details/{jid}"
                    if jid else ""),
            "location": location,
            "country": country,
            "time_type": "",
            "posted": (j.get("PublishedDate") or "")[:10],
        })
        if len(jobs) >= limit:
            break
    return jobs


def _epoch_ms_date(ms) -> str:
    """Format a Lever epoch-millisecond timestamp as YYYY-MM-DD (best effort)."""
    try:
        from datetime import datetime, timezone
        return datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
    except Exception:
        return ""


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
