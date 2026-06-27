"""
Scrape devops / devsecops / aws jobs from H1B cap-exempt employer careers pages.

For each company:
  1. Detect the ATS once (render the careers page, fingerprint it) and cache the
     resolved search endpoint in ats_cache.json — subsequent runs skip the browser.
  2. If the ATS is known (e.g. Workday), keyword-search its API over plain HTTP.
  3. Otherwise fall back to scraping the careers landing page.

New jobs are appended to it_jobs.jsonl (one JSON object per line) as they are
found, so an interrupted run keeps its progress.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import random

from playwright.sync_api import sync_playwright
from playwright_stealth import Stealth

import ats
from config import (
    COMPANIES_CSV, JOBS_JSONL, SEEN_URLS_FILE, ATS_CACHE,
    AGGREGATORS, POLITE_DELAY, JOB_KEYWORDS, now_pst, is_us_location,
)
from ats_scrapers import scrape_it_jobs

PAGE_TIMEOUT = 30_000
RENDER_WAIT = 4_000


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
    import csv
    if not COMPANIES_CSV.exists():
        sys.exit(f"Not found: {COMPANIES_CSV}\nRun scraper.py first.")
    companies = []
    with open(COMPANIES_CSV, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            url = row.get("careers_page", "").strip()
            if url and not any(agg in url.lower() for agg in AGGREGATORS):
                companies.append(row)
    return companies


# ---------------------------------------------------------------------------
# Detection (cached)
# ---------------------------------------------------------------------------

def detect_ats(page, careers_url: str) -> dict | None:
    """Render the careers page and fingerprint its ATS. Returns
    {"ats": ..., "endpoint": ...} or None."""
    try:
        page.goto(careers_url, timeout=PAGE_TIMEOUT, wait_until="domcontentloaded")
        page.wait_for_timeout(RENDER_WAIT)
    except Exception as e:
        print(f"  [detect error] {e}")
        return None
    return ats.detect(page.content(), page.url)


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

            label = info.get("ats") or "fallback"
            print(f"[{i}/{len(companies)}] {company}  ({label})", flush=True)

            if info.get("ats"):
                jobs = jobs_via_api(info)
            else:
                jobs = jobs_via_fallback(page, careers_url)

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
