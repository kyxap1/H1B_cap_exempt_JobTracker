
import csv
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

PROJECT_DIR = Path(__file__).parent
PYTHON = sys.executable
SCRAPPER = PROJECT_DIR / "scrapper.py"
JOB_SCRAPER = PROJECT_DIR / "IT_job.py"
COMPANIES_CSV = PROJECT_DIR / "h1b_cap_exempt_sponsors.csv"
JOBS_CSV = PROJECT_DIR / "it_jobs.csv"
PST = ZoneInfo("America/Los_Angeles")


def log(msg: str) -> None:
    ts = datetime.now(PST).strftime("%Y-%m-%d %H:%M:%S PST")
    print(f"[{ts}] {msg}", flush=True)


def companies_need_scraping() -> bool:
    """Return True if scrapper.py needs to run."""
    if not COMPANIES_CSV.exists():
        return True
    with open(COMPANIES_CSV, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    if not rows:
        return True
    # Re-run scrapper if more than 80% of companies have no careers page
    missing = sum(1 for r in rows if not r.get("careers_page", "").strip())
    return missing / len(rows) > 0.8


def run(script: Path, label: str) -> bool:
    log(f"Starting {label} ...")
    result = subprocess.run([PYTHON, str(script)], cwd=PROJECT_DIR)
    if result.returncode == 0:
        log(f"{label} completed successfully.")
        return True
    else:
        log(f"{label} exited with code {result.returncode}.")
        return False


def main() -> None:
    log("=== Pipeline started ===")

    # Step 1: Build company + careers page list if needed
    if companies_need_scraping():
        log("Companies CSV missing or careers pages not filled — running scrapper.py")
        if not run(SCRAPPER, "scrapper.py"):
            log("scrapper.py failed. Aborting pipeline.")
            sys.exit(1)
    else:
        with open(COMPANIES_CSV, newline="", encoding="utf-8") as f:
            total = sum(1 for _ in csv.DictReader(f))
        log(f"Companies CSV already populated ({total} companies). Skipping scrapper.py.")

    # Step 2: Scrape new IT jobs from careers pages
    run(JOB_SCRAPER, "job_scraper.py")

    # Summary
    if JOBS_CSV.exists():
        with open(JOBS_CSV, newline="", encoding="utf-8") as f:
            job_count = sum(1 for _ in csv.DictReader(f))
        log(f"Total IT jobs in {JOBS_CSV.name}: {job_count}")

    log("=== Pipeline finished ===")


if __name__ == "__main__":
    main()
