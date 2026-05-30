"""
Scrape IT job listings from H1B cap-exempt employer careers pages.

Reads h1b_cap_exempt_sponsors.csv, visits each careers page,
extracts IT jobs, and appends new results to it_jobs.csv.

Scheduled via cron_jobs.py to run at 6 AM and 3 PM PST.
"""

from __future__ import annotations

import csv
import json
import sys
import time
import random

from playwright.sync_api import sync_playwright
from playwright_stealth import Stealth

from config import (
    COMPANIES_CSV, JOBS_CSV, SEEN_URLS_FILE,
    AGGREGATORS, POLITE_DELAY, now_pst, is_us_location,
)
from ats_scrapers import scrape_it_jobs


# ---------------------------------------------------------------------------
# State helpers
# ---------------------------------------------------------------------------

def load_seen_urls() -> set[str]:
    if SEEN_URLS_FILE.exists():
        try:
            return set(json.loads(SEEN_URLS_FILE.read_text()))
        except Exception:
            pass
    return set()


def save_seen_urls(urls: set[str]) -> None:
    SEEN_URLS_FILE.write_text(json.dumps(sorted(urls), indent=2))


def load_companies() -> list[dict]:
    if not COMPANIES_CSV.exists():
        sys.exit(f"Not found: {COMPANIES_CSV}\nRun scrapper.py first.")
    companies = []
    with open(COMPANIES_CSV, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            url = row.get("careers_page", "").strip()
            if url and not any(agg in url.lower() for agg in AGGREGATORS):
                companies.append(row)
    return companies


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    companies  = load_companies()
    seen_urls  = load_seen_urls()
    scraped_at = now_pst()

    print(f"\n=== IT Job Scraper  {scraped_at} ===")
    print(f"Companies with careers pages: {len(companies)}")

    new_jobs: list[dict] = []

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
            company     = row["company"]
            careers_url = row["careers_page"]
            print(f"[{i}/{len(companies)}] {company}", flush=True)

            jobs  = scrape_it_jobs(careers_url, page)
            added = 0
            for job in jobs:
                if not job["url"] or job["url"] in seen_urls:
                    continue
                if not is_us_location(job.get("location", "")):
                    continue
                seen_urls.add(job["url"])
                new_jobs.append({
                    "company":       company,
                    "job_title":     job["title"],
                    "job_url":       job["url"],
                    "location":      job["location"],
                    "date_posted":   job["date_posted"],
                    "scraped_at_pst": scraped_at,
                })
                added += 1

            print(f"  {added} new IT job(s)")
            time.sleep(random.uniform(*POLITE_DELAY))

        browser.close()

    if new_jobs:
        write_header = not JOBS_CSV.exists()
        with open(JOBS_CSV, "a", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(
                f,
                fieldnames=["company", "job_title", "job_url",
                            "location", "date_posted", "scraped_at_pst"],
            )
            if write_header:
                writer.writeheader()
            writer.writerows(new_jobs)
        print(f"\n{len(new_jobs)} new IT jobs → {JOBS_CSV}")
    else:
        print("\nNo new IT jobs found this run.")

    save_seen_urls(seen_urls)


if __name__ == "__main__":
    main()
