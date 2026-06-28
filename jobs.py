"""
Scrape devops / devsecops / aws jobs from H1B cap-exempt employer careers pages.

For each company:
  1. Detect the ATS once (render the careers page, fingerprint it) and cache the
     resolved search endpoint in cache/ats-cache.json — subsequent runs skip the browser.
  2. If the ATS is known (e.g. Workday), keyword-search its API over plain HTTP.
  3. Otherwise fall back to scraping the careers landing page.

New jobs are appended to data/it-jobs.jsonl (one JSON object per line) as they are
found, so an interrupted run keeps its progress.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
import random
from urllib.parse import urljoin, urlparse

from playwright.sync_api import sync_playwright
from playwright_stealth import Stealth

import ats
from config import (
    COMPANIES_JSON, JOBS_JSONL, SEEN_URLS_FILE, ATS_CACHE,
    AGGREGATORS, POLITE_DELAY, JOB_KEYWORDS, now_pst, is_us_location,
)
from ats_scrapers import scrape_it_jobs

PAGE_TIMEOUT = 30_000
# Short initial settle; _poll_ats does the real waiting and exits early once the
# ATS reveals itself, so a long fixed wait here would only slow the common case.
RENDER_WAIT = 2_000


# ---------------------------------------------------------------------------
# State helpers
# ---------------------------------------------------------------------------

def _load_json(path, default):
    if path.exists():
        try:
            return json.loads(path.read_text())
        except Exception:
            pass
    return default


def load_seen_urls() -> set[str]:
    return set(_load_json(SEEN_URLS_FILE, []))


def save_seen_urls(urls: set[str]) -> None:
    SEEN_URLS_FILE.write_text(json.dumps(sorted(urls), indent=2))


def load_companies() -> list[dict]:
    if not COMPANIES_JSON.exists():
        sys.exit(f"Not found: {COMPANIES_JSON}\nRun sponsors.py first.")
    companies = []
    for row in json.loads(COMPANIES_JSON.read_text()):
        url = (row.get("careers_page") or "").strip()
        if url and not any(agg in url.lower() for agg in AGGREGATORS):
            companies.append(row)
    return companies


# ---------------------------------------------------------------------------
# Detection (cached)
# ---------------------------------------------------------------------------

# Anchor text / href that leads from an HR landing page to the real job portal,
# and the patterns that lead somewhere else (admissions, login, benefits) and must
# be rejected — these are the false positives seen on .edu / hospital HR pages.
_PORTAL_TEXT = re.compile(
    r"search\s+jobs|view\s+(all\s+)?(open\s+)?(jobs|positions|openings)|"
    r"job\s+(openings|search|opportunities)|current\s+openings|browse\s+jobs|"
    r"all\s+jobs|open\s+positions|external\s+applicant|see\s+(all\s+)?jobs|"
    r"find\s+jobs|view\s+careers",
    re.IGNORECASE,
)
_PORTAL_HREF = re.compile(
    r"/(jobs|careers|search|openings|positions|requisition|vacanc|"
    r"job-search|job_search|joblist|listings?)\b",
    re.IGNORECASE,
)
_PORTAL_REJECT = re.compile(
    r"admiss|applynow|apply/pages|financial[-_ ]?aid|/aid\b|scholarship|"
    r"alumni|giving|donate|undergrad|graduate-program|tuition|"
    r"login|sign[-_ ]?in|register|/pfml|wellness|benefits-|leave-of|"
    # ATS-vendor marketing pages (a "powered by PageUp" footer, etc.) — not a portal
    r"powered-by|/faqs?\b|/about-us|/our-clients|/customers|/products|/resources",
    re.IGNORECASE,
)


def find_jobs_portal(page) -> str:
    """From an HR landing page, pick the link most likely to be the real job
    portal. Scores anchors by job-portal text/href signals (and a known-ATS host
    in the href) and rejects the admissions / login / benefits links that pollute
    university HR pages. Returns "" if nothing scores high enough."""
    base = page.url
    best, best_score = "", 0
    try:
        anchors = page.locator("a[href]").all()
    except Exception:
        return ""
    for a in anchors:
        try:
            text = (a.inner_text() or "").strip()
            href = a.get_attribute("href") or ""
        except Exception:
            continue
        if not href or href.startswith(("#", "mailto:", "tel:", "javascript")):
            continue
        full = urljoin(base, href)
        blob = f"{text} {full}"
        if _PORTAL_REJECT.search(blob):
            continue
        score = 0
        if _PORTAL_TEXT.search(text):
            score += 3
        if _PORTAL_HREF.search(full):
            score += 2
        if ats.fingerprint(full):
            score += 5
        if urlparse(full).netloc != urlparse(base).netloc:
            score += 1
        if score > best_score:
            best, best_score = full, score
    return best if best_score >= 3 else ""


def _detect_blob(page, net: list[str]) -> str:
    """Everything we fingerprint against: final URL + rendered HTML + the URLs of
    every request the page fired. SPA careers pages reveal their ATS only through
    network calls (a PageUp widget, a Workday cxs XHR), not the initial HTML."""
    try:
        content = page.content()
    except Exception:
        content = ""
    return f"{page.url} {content} {' '.join(net)}"


def _poll_ats(page, net: list[str], budget_ms: int = 6000, step_ms: int = 1000):
    """Poll the rendered page + captured network until an ATS reveals itself, or
    the budget runs out. Returns (info, label): `info` is a searchable endpoint
    dict (best), else `label` is the fingerprinted ATS name, else both None.

    Early exit on the first signal: SPA careers pages inject their ATS widget /
    fire their first XHR at unpredictable times, so a fixed wait is either too
    short (misses it) or wastefully long. A searchable ATS is always caught by
    detect() before fingerprint(), so any fingerprint hit means a non-searchable
    ATS and there is nothing to gain by waiting longer."""
    waited = 0
    while True:
        blob = _detect_blob(page, net)
        info = ats.detect(blob, page.url)
        if info:
            return info, None
        label = ats.fingerprint(blob)
        if label:
            return None, label
        if waited >= budget_ms:
            return None, None
        page.wait_for_timeout(step_ms)
        waited += step_ms


def detect_ats(page, careers_url: str) -> dict | None:
    """Render the careers page and fingerprint its ATS. Returns
    {"ats": ..., "endpoint": ...} for a searchable ATS, or — when the page is an
    HR landing page whose real portal we resolved but can't yet search —
    {"ats": None, "portal": <url>, "fingerprint": <name>}, or None.

    Detection looks at three things, in order: the rendered HTML, the URLs of all
    requests the page fired (SPA widgets / XHRs betray the ATS), and — if neither
    is itself a searchable ATS — the "view jobs" link followed to the real portal.
    """
    net: list[str] = []
    handler = lambda r: net.append(r.url)
    page.on("request", handler)
    try:
        try:
            page.goto(careers_url, timeout=PAGE_TIMEOUT, wait_until="domcontentloaded")
            page.wait_for_timeout(RENDER_WAIT)
        except Exception as e:
            print(f"  [detect error] {e}")
            return None

        info, label = _poll_ats(page, net)
        if info:
            return info
        # A label here means the careers page IS the ATS portal (just not one we
        # can search yet). No label → it's an HR brochure; follow its job link.
        portal = page.url if label else ""
        if not label:
            link = find_jobs_portal(page)
            if link and link != page.url:
                print(f"  -> following job-portal link: {link}")
                try:
                    page.goto(link, timeout=PAGE_TIMEOUT, wait_until="domcontentloaded")
                    page.wait_for_timeout(RENDER_WAIT)
                    info, label = _poll_ats(page, net)
                    if info:
                        return info
                    portal = page.url
                except Exception as e:
                    print(f"  [portal error] {e}")

        if label:
            print(f"  [looks like {label} — no search adapter yet, will scrape]")
        if portal or label:
            return {"ats": None, "portal": portal, "fingerprint": label}
        return None
    finally:
        page.remove_listener("request", handler)


# ---------------------------------------------------------------------------
# Job collection
# ---------------------------------------------------------------------------

def jobs_via_api(info: dict) -> list[dict]:
    """Keyword-search the company's ATS API; tag each hit with matched_keyword."""
    out, seen = [], set()
    for kw in JOB_KEYWORDS:
        try:
            hits = ats.search(info["ats"], info["endpoint"], kw)
        except Exception as e:
            print(f"  [search error: {kw}] {e}")
            continue
        for j in hits:
            if not j["url"] or j["url"] in seen:
                continue
            seen.add(j["url"])
            out.append({**j, "matched_keyword": kw})
    return out


def jobs_via_fallback(page, careers_url: str) -> list[dict]:
    """Old landing-page scrape for ATSes we don't have an adapter for."""
    jobs = scrape_it_jobs(careers_url, page)
    return [{**j, "matched_keyword": None} for j in jobs]


def is_us_job(job: dict) -> bool:
    """Prefer the ATS's structured country field; fall back to the location
    string heuristic only when no country is available (e.g. fallback scrape)."""
    country = (job.get("country") or "").strip().lower()
    if country:
        return country in ("us", "usa", "u.s.") or "united states" in country
    return is_us_location(job.get("location", ""))


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main(limit: int | None = None) -> None:
    companies = load_companies()
    if limit:
        companies = companies[:limit]
    seen_urls = load_seen_urls()
    ats_cache = _load_json(ATS_CACHE, {})
    scraped_at = now_pst()

    print(f"\n=== Job Scraper  {scraped_at} ===")
    print(f"Companies: {len(companies)}"
          + (f" (limited to first {limit})" if limit else ""))
    print(f"Keywords: {', '.join(JOB_KEYWORDS)}")

    total_new = 0
    out = open(JOBS_JSONL, "a", encoding="utf-8")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0 Safari/537.36"
            ),
            viewport={"width": 1400, "height": 900},
            locale="en-US",
        )
        page = ctx.new_page()
        Stealth().apply_stealth_sync(page)

        for i, row in enumerate(companies, 1):
            company = row["company"]
            careers_url = row["careers_page"]

            # Detect once, then reuse the cached endpoint.
            if careers_url not in ats_cache:
                info = detect_ats(page, careers_url)
                ats_cache[careers_url] = info or {"ats": None}
                ATS_CACHE.write_text(json.dumps(ats_cache, indent=2))
            info = ats_cache[careers_url]

            label = info.get("ats") or info.get("fingerprint") or "fallback"
            print(f"[{i}/{len(companies)}] {company}  ({label})", flush=True)

            if info.get("ats"):
                jobs = jobs_via_api(info)
            else:
                jobs = jobs_via_fallback(page, info.get("portal") or careers_url)

            added = 0
            for j in jobs:
                if not j["url"] or j["url"] in seen_urls:
                    continue
                if not is_us_job(j):
                    continue
                seen_urls.add(j["url"])
                rec = {
                    "company": company,
                    "ats": info.get("ats"),
                    "matched_keyword": j.get("matched_keyword"),
                    "title": j["title"],
                    "url": j["url"],
                    "location": j.get("location", ""),
                    "country": j.get("country", ""),
                    "time_type": j.get("time_type", ""),
                    "posted": j.get("posted", j.get("date_posted", "")),
                    "scraped_at_pst": scraped_at,
                }
                out.write(json.dumps(rec, ensure_ascii=False) + "\n")
                added += 1

            if added:
                out.flush()
                save_seen_urls(seen_urls)
            total_new += added
            print(f"  {added} new job(s)")
            time.sleep(random.uniform(*POLITE_DELAY))

        browser.close()

    out.close()
    save_seen_urls(seen_urls)
    print(f"\n{total_new} new jobs appended to {JOBS_JSONL.name}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Scrape devops/devsecops/aws jobs.")
    parser.add_argument(
        "--limit", type=int, default=None,
        help="only process the first N companies (for a quick test run)",
    )
    args = parser.parse_args()
    main(limit=args.limit)
