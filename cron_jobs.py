
import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from config import PROJECT_DIR, COMPANIES_JSON, JOBS_JSONL, PST

PYTHON = sys.executable
SPONSORS = PROJECT_DIR / "sponsors.py"
JOBS = PROJECT_DIR / "jobs.py"


def log(msg: str) -> None:
    ts = datetime.now(PST).strftime("%Y-%m-%d %H:%M:%S PST")
    print(f"[{ts}] {msg}", flush=True)


def companies_need_scraping() -> bool:
    """Return True if sponsors.py needs to run."""
    if not COMPANIES_JSON.exists():
        return True
    try:
        rows = json.loads(COMPANIES_JSON.read_text())
    except (json.JSONDecodeError, OSError):
        return True
    if not rows:
        return True
    # Re-run scraper if more than 80% of companies have no careers page
    missing = sum(1 for r in rows if not (r.get("careers_page") or "").strip())
    return missing / len(rows) > 0.8


def run(script: Path, label: str, extra_args: list[str] | None = None) -> bool:
    log(f"Starting {label} ...")
    result = subprocess.run([PYTHON, str(script), *(extra_args or [])], cwd=PROJECT_DIR)
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
        log("Companies JSON missing or careers pages not filled — running sponsors.py --force")
        if not run(SPONSORS, "sponsors.py", ["--force"]):
            log("sponsors.py failed. Aborting pipeline.")
            sys.exit(1)
    else:
        total = len(json.loads(COMPANIES_JSON.read_text()))
        log(f"Companies JSON already populated ({total} companies). Skipping sponsors.py.")

    # Step 2: Scrape new IT jobs from careers pages
    run(JOBS, "jobs.py")

    # Summary
    if JOBS_JSONL.exists():
        job_count = sum(1 for line in JOBS_JSONL.read_text().splitlines() if line.strip())
        log(f"Total IT jobs in {JOBS_JSONL.name}: {job_count}")

    log("=== Pipeline finished ===")


if __name__ == "__main__":
    main()
