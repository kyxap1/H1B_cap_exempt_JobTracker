"""ATS-specific job scrapers + generic fallback + redirect detection."""

from __future__ import annotations

import re
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from config import ATS_DOMAINS, AGGREGATORS, is_it_job

PAGE_TIMEOUT  = 30_000
RENDER_WAIT   = 5_000


# ---------------------------------------------------------------------------
# Low-level helpers
# ---------------------------------------------------------------------------

def _text(el) -> str:
    try:
        return el.inner_text().strip()
    except Exception:
        return ""


def _href(el, base: str = "") -> str:
    try:
        href = el.get_attribute("href") or ""
        return urljoin(base, href) if href else ""
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# ATS-specific scrapers
# ---------------------------------------------------------------------------

def scrape_greenhouse(page, base: str) -> list[dict]:
    jobs = []
    for opening in page.locator(".opening").all():
        try:
            a = opening.locator("a").first
            title = _text(a)
            url = _href(a, "https://boards.greenhouse.io")
            loc_el = opening.locator(".location")
            location = _text(loc_el) if loc_el.count() else ""
            if title and url:
                jobs.append({"title": title, "url": url, "location": location, "date_posted": ""})
        except Exception:
            continue
    return jobs


def scrape_lever(page, base: str) -> list[dict]:
    jobs = []
    for posting in page.locator(".posting").all():
        try:
            a = posting.locator("a.posting-title").first
            title = _text(posting.locator("h5").first) or _text(a)
            url = _href(a, base)
            loc_el = posting.locator(".sort-by-location").first
            location = _text(loc_el) if loc_el.count() else ""
            if title and url:
                jobs.append({"title": title, "url": url, "location": location, "date_posted": ""})
        except Exception:
            continue
    return jobs


def scrape_workday(page, base: str) -> list[dict]:
    try:
        page.wait_for_selector("[data-automation-id='jobPostingTitle']", timeout=10_000)
    except Exception:
        pass
    jobs = []
    for el in page.locator("[data-automation-id='jobPostingTitle']").all():
        try:
            title = _text(el)
            a = el.locator("xpath=ancestor::a").first
            url = _href(a, base) if a.count() else ""
            loc_el = el.locator("xpath=ancestor::li").locator(
                "[data-automation-id='locations']"
            ).first
            location = _text(loc_el) if loc_el.count() else ""
            if title:
                jobs.append({"title": title, "url": url, "location": location, "date_posted": ""})
        except Exception:
            continue
    return jobs


def scrape_yello(page, base: str) -> list[dict]:
    jobs = []
    for a in page.locator("a.search-results__req_title").all():
        try:
            title = _text(a)
            url = _href(a, base)
            loc_el = a.locator("xpath=../..").locator("span").first
            location = _text(loc_el) if loc_el.count() else ""
            if title and url:
                jobs.append({"title": title, "url": url, "location": location, "date_posted": ""})
        except Exception:
            continue
    return jobs


def scrape_icims(page, base: str) -> list[dict]:
    jobs = []
    for el in page.locator(".iCIMS_JobTitle, [class*='job-title'], [id*='job-title']").all():
        try:
            a = el.locator("a").first
            title = _text(a) or _text(el)
            url = _href(a, base)
            if title:
                jobs.append({"title": title, "url": url, "location": "", "date_posted": ""})
        except Exception:
            continue
    return jobs


def scrape_jobvite(page, base: str) -> list[dict]:
    jobs = []
    for el in page.locator("[class*='jv-job'], .position").all():
        try:
            a = el.locator("a").first
            title = _text(a)
            url = _href(a, base)
            if title and url:
                jobs.append({"title": title, "url": url, "location": "", "date_posted": ""})
        except Exception:
            continue
    return jobs


def scrape_ashby(page, base: str) -> list[dict]:
    jobs = []
    for el in page.locator("[data-job-id], .ashby-job-posting-brief-title").all():
        try:
            a = el if el.get_attribute("href") else el.locator("xpath=ancestor::a").first
            title = _text(el)
            url = _href(a, base)
            if title and url:
                jobs.append({"title": title, "url": url, "location": "", "date_posted": ""})
        except Exception:
            continue
    return jobs


def scrape_smartrecruiters(page, base: str) -> list[dict]:
    jobs = []
    for item in page.locator(".job-item, [class*='job-listing']").all():
        try:
            a = item.locator("a").first
            title = _text(a)
            url = _href(a, base)
            loc_el = item.locator("[class*='location']").first
            location = _text(loc_el) if loc_el.count() else ""
            if title and url:
                jobs.append({"title": title, "url": url, "location": location, "date_posted": ""})
        except Exception:
            continue
    return jobs


def scrape_taleo(page, base: str) -> list[dict]:
    jobs = []
    for el in page.locator(
        ".requisitionListInterface_sortableColumn_Name a, "
        ".reqListTitle a, [class*='req-name'] a"
    ).all():
        try:
            title = _text(el)
            url = _href(el, base)
            if title and url:
                jobs.append({"title": title, "url": url, "location": "", "date_posted": ""})
        except Exception:
            continue
    return jobs


def scrape_successfactors(page, base: str) -> list[dict]:
    jobs = []
    for el in page.locator(
        "[class*='jobTitle'], .job-item__title, [data-key='jobTitle']"
    ).all():
        try:
            a = el if el.get_attribute("href") else el.locator("a").first
            title = _text(el)
            url = _href(a, base)
            if title and url:
                jobs.append({"title": title, "url": url, "location": "", "date_posted": ""})
        except Exception:
            continue
    return jobs


_PAGEUP_JOB = re.compile(r"/en-us/job/\d+/", re.IGNORECASE)


def scrape_pageup(page, base: str) -> list[dict]:
    """Parse a PageUp search-results table (rendered in the browser, since branded
    PageUp sites sit behind an AWS WAF JS challenge). Jobs are <tr> rows in
    #search-results-content; columns vary per tenant, so map them by their <th>
    header labels (Position / Location / Department / Open Date / Close Date)."""
    soup = BeautifulSoup(page.content(), "html.parser")
    labels = [th.get_text(" ", strip=True).lower()
              for th in soup.select("#search-results th, #search-results-content th")]
    jobs = []
    for tr in soup.select("#search-results-content tr"):
        a = tr.find("a", href=_PAGEUP_JOB)
        if not a:
            continue
        tds = tr.find_all("td", recursive=False)
        location, posted = "", ""
        for i, td in enumerate(tds):
            label = labels[i] if i < len(labels) else ""
            text = td.get_text(" ", strip=True)
            if ("location" in label or "campus" in label) and not location:
                location = text
            elif "open" in label and "date" in label:
                posted = text
        if not posted:
            m = _DATE.search(tr.get_text(" ", strip=True))
            posted = m.group(0) if m else ""
        jobs.append({
            "title": a.get_text(" ", strip=True),
            "url": urljoin(base, a.get("href", "")),
            "location": location,
            # PageUp rows carry no country and often only a bare campus name; these
            # tenants are all US H1B sponsors, so tag US so the US-location filter
            # doesn't drop valid jobs whose location lacks a state token.
            "country": "US",
            "time_type": "",
            "posted": posted,
        })
    return jobs


# ---------------------------------------------------------------------------
# Generic fallback
# ---------------------------------------------------------------------------

_JOB_PATH = re.compile(
    r"/(job|jobs|career|careers|position|positions|opening|"
    r"openings|vacancy|vacancies|requisition|posting|apply)/",
    re.IGNORECASE,
)

_DATE = re.compile(
    r"(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+\d{1,2},?\s+\d{4}|"
    r"\d{1,2}/\d{1,2}/\d{2,4}|\d{4}-\d{2}-\d{2}|"
    r"\d+\s+days?\s+ago|\d+\s+hours?\s+ago|today|yesterday",
    re.IGNORECASE,
)


def scrape_generic(page, base: str) -> list[dict]:
    soup = BeautifulSoup(page.content(), "html.parser")
    jobs, seen = [], set()
    for a in soup.find_all("a", href=True):
        href = a["href"]
        full_url = urljoin(base, href)
        if full_url in seen or any(agg in full_url.lower() for agg in AGGREGATORS):
            continue
        if not _JOB_PATH.search(full_url) and not _JOB_PATH.search(href):
            continue
        title = a.get_text(" ", strip=True)
        if not title or not (4 <= len(title) <= 150):
            continue
        date_posted = ""
        parent = a.parent
        for _ in range(3):
            if parent is None:
                break
            m = _DATE.search(parent.get_text(" ", strip=True))
            if m:
                date_posted = m.group(0)
                break
            parent = parent.parent
        seen.add(full_url)
        jobs.append({"title": title, "url": full_url, "location": "", "date_posted": date_posted})
    return jobs


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------

def find_ats_redirect(page) -> str:
    """Return ATS URL if the current page is a landing page that links to one."""
    soup = BeautifulSoup(page.content(), "html.parser")
    for a in soup.find_all("a", href=True):
        if any(ats in a["href"].lower() for ats in ATS_DOMAINS):
            return a["href"]
    return ""


def scrape_by_url(url: str, page) -> list[dict]:
    """Pick the right ATS scraper based on URL, fall back to generic."""
    u = url.lower()
    if "greenhouse.io" in u or "boards.greenhouse" in u:
        print("  [ATS: Greenhouse]")
        return scrape_greenhouse(page, url)
    if "lever.co" in u:
        print("  [ATS: Lever]")
        return scrape_lever(page, url)
    if "myworkdayjobs" in u or "workday" in u:
        print("  [ATS: Workday]")
        return scrape_workday(page, url)
    if "smartrecruiters" in u:
        print("  [ATS: SmartRecruiters]")
        return scrape_smartrecruiters(page, url)
    if "yello.co" in u:
        print("  [ATS: Yello]")
        return scrape_yello(page, url)
    if "icims.com" in u:
        print("  [ATS: iCIMS]")
        return scrape_icims(page, url)
    if "jobvite.com" in u or "jobs.jobvite" in u:
        print("  [ATS: Jobvite]")
        return scrape_jobvite(page, url)
    if "ashbyhq.com" in u:
        print("  [ATS: Ashby]")
        return scrape_ashby(page, url)
    if "taleo.net" in u:
        print("  [ATS: Taleo]")
        return scrape_taleo(page, url)
    if "successfactors" in u or "sap.com/careers" in u:
        print("  [ATS: SuccessFactors]")
        return scrape_successfactors(page, url)
    print("  [ATS: generic]")
    return scrape_generic(page, url)


def scrape_it_jobs(careers_url: str, page) -> list[dict]:
    """Load careers URL, follow ATS redirect if needed, return IT jobs only."""
    from playwright.sync_api import TimeoutError as PWTimeout
    try:
        page.goto(careers_url, timeout=PAGE_TIMEOUT, wait_until="domcontentloaded")
        page.wait_for_timeout(RENDER_WAIT)
    except PWTimeout:
        print(f"  [timeout] {careers_url}")
        return []
    except Exception as e:
        print(f"  [error] {e}")
        return []

    final_url = page.url

    # HTTP redirect already landed on an ATS page
    if any(ats in final_url.lower() for ats in ATS_DOMAINS):
        print(f"  -> redirected to ATS: {final_url}")
        jobs = scrape_by_url(final_url, page)
    else:
        # Scan HTML for a link to a known ATS
        ats_url = find_ats_redirect(page)
        if ats_url:
            print(f"  -> ATS link found: {ats_url}")
            try:
                page.goto(ats_url, timeout=PAGE_TIMEOUT, wait_until="domcontentloaded")
                page.wait_for_timeout(RENDER_WAIT)
            except Exception as e:
                print(f"  [ats error] {e}")
                return []
            jobs = scrape_by_url(page.url, page)
        else:
            jobs = scrape_by_url(final_url, page)

    raw = len(jobs)
    it_jobs = [j for j in jobs if j["title"] and is_it_job(j["title"])]
    print(f"  {raw} jobs found → {len(it_jobs)} IT jobs")
    return it_jobs
