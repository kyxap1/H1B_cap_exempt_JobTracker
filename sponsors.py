"""
Build H1B cap-exempt sponsor list from DOL OFLC LCA data.

Pipeline:
  1. Download DOL Excel files  → data/lca/
  2. Parse + aggregate         → top N cap-exempt employers
  3. Find careers pages        → via Google (Serper.dev API)
  4. Write output              → data/h1b-cap-exempt-sponsors.json

Requires: export SERPER_API_KEY=your_key_here
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from datetime import datetime

from config import (
    LCA_DIR, COMPANIES_JSON, CHECKPOINT, DOL_FILES, TOP_N,
    Company, normalize_name, polite_sleep, title_case,
)
from dol_parser import download_file, parse_year
from careers_finder import find_careers_page, _score_careers


def json_has_data(path) -> bool:
    """True if the sponsors JSON exists and holds at least one company."""
    if not path.exists():
        return False
    try:
        return bool(json.loads(path.read_text()))
    except (json.JSONDecodeError, OSError):
        return False


def backup_json() -> None:
    """Copy the existing sponsors JSON next to itself before it gets overwritten."""
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    dest = COMPANIES_JSON.with_name(f"{COMPANIES_JSON.stem}.{stamp}.bak.json")
    shutil.copy2(COMPANIES_JSON, dest)
    print(f"[backup] existing JSON saved → {dest.name}")


def load_checkpoint() -> dict:
    if CHECKPOINT.exists():
        try:
            return json.loads(CHECKPOINT.read_text())
        except json.JSONDecodeError:
            print("[warn] checkpoint corrupt; ignoring")
    return {}


def save_checkpoint(data: dict) -> None:
    CHECKPOINT.write_text(json.dumps(data, indent=2))


def refresh_careers(threshold: int = 4, limit: int | None = None) -> None:
    """Re-resolve careers_page for companies whose current URL scores below
    `threshold` (i.e. looks like an HR brochure rather than a real job portal),
    keeping the new URL only when it scores strictly better. The sponsor JSON is
    backed up first; the checkpoint cache is updated so the change persists."""
    if not json_has_data(COMPANIES_JSON):
        sys.exit(f"No data in {COMPANIES_JSON.name}; run the build first.")
    records = json.loads(COMPANIES_JSON.read_text())
    backup_json()

    state = load_checkpoint()
    enrichment = state.get("enrichment", {})

    weak = [r for r in records if _score_careers((r.get("careers_page") or "").strip()) < threshold]
    if limit:
        weak = weak[:limit]
    print(f"{len(weak)} careers pages score below {threshold} — re-resolving"
          + (f" (limited to {limit})" if limit else ""))

    changed = 0
    for i, rec in enumerate(weak, 1):
        company = rec["company"]
        old = (rec.get("careers_page") or "").strip()
        print(f"[{i}/{len(weak)}] {company}\n  old: {old or '(none)'}  (score {_score_careers(old)})")
        try:
            new = find_careers_page(company)
        except Exception as e:
            print(f"  [error] {e}")
            continue
        if new and _score_careers(new) > _score_careers(old):
            rec["careers_page"] = new
            enrichment.setdefault(normalize_name(company), {})["careers_page"] = new
            changed += 1
            print(f"  new: {new}  (score {_score_careers(new)})  [UPDATED]")
        else:
            print("  kept (no better candidate)")
        polite_sleep()

    state["enrichment"] = enrichment
    save_checkpoint(state)
    COMPANIES_JSON.write_text(json.dumps(records, ensure_ascii=False, indent=2))
    print(f"\nUpdated {changed}/{len(weak)} careers pages → {COMPANIES_JSON.name}")


def main(force: bool = False) -> None:
    if json_has_data(COMPANIES_JSON) and not force:
        print(f"{COMPANIES_JSON.name} already exists and is non-empty — nothing to do.")
        print("Re-run with --force to rebuild it (Excel files are reused, not re-downloaded).")
        return
    if force and COMPANIES_JSON.exists():
        backup_json()

    state      = load_checkpoint()
    list_data  = state.get("list_data", {})
    enrichment = state.get("enrichment", {})

    # Phase 1 — Download
    print("\n=== Phase 1: Downloading DOL LCA data ===")
    for url in DOL_FILES.values():
        download_file(url, LCA_DIR / url.split("/")[-1])

    # Phase 2 — Parse + merge years
    print("\n=== Phase 2: Parsing LCA data ===")
    companies: dict[str, Company] = {}
    for year, url in DOL_FILES.items():
        key = str(year)
        if key in list_data and list_data[key]:
            print(f"[checkpoint] {year}: {len(list_data[key])} employers")
            year_data = list_data[key]
        else:
            year_data = parse_year(LCA_DIR / url.split("/")[-1], year)
            list_data[key] = year_data
            state["list_data"] = list_data
            save_checkpoint(state)

        for norm_key, info in year_data.items():
            name = title_case(info["name"])
            c = companies.setdefault(norm_key, Company(name=name))
            if len(name) > len(c.name):
                c.name = name
            c.counts[year] = info["count"]
            if not c.state and info.get("state"):
                c.state = info["state"]

    top = sorted(companies.values(),
                 key=lambda c: max(c.counts.values()) if c.counts else 0,
                 reverse=True)[:TOP_N]
    print(f"\n{len(companies)} unique employers → top {len(top)} selected")

    # Phase 3 — Careers pages
    print("\n=== Phase 3: Careers page lookup ===")
    for i, c in enumerate(top, 1):
        cached = enrichment.get(normalize_name(c.name), {})
        if "careers_page" in cached:
            c.careers_page = cached["careers_page"]
            continue
        print(f"[{i}/{len(top)}] {c.name}")
        try:
            c.careers_page = find_careers_page(c.name)
        except Exception as e:
            print(f"  [error] {e}")
            c.careers_page = ""
        cached["careers_page"] = c.careers_page
        enrichment[normalize_name(c.name)] = cached
        state["enrichment"] = enrichment
        save_checkpoint(state)
        polite_sleep()

    # Phase 4 — Write JSON
    records = [
        {
            "company": c.name,
            "careers_page": c.careers_page,
            "state": c.state,
            "h1b_2026": c.counts.get(2026),
            "h1b_2025": c.counts.get(2025),
            "h1b_2024": c.counts.get(2024),
        }
        for c in top
    ]
    COMPANIES_JSON.write_text(json.dumps(records, ensure_ascii=False, indent=2))
    print(f"\nDone → {COMPANIES_JSON}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Build H1B cap-exempt sponsor list.")
    parser.add_argument(
        "--force", action="store_true",
        help="rebuild even if h1b-cap-exempt-sponsors.json already exists "
             "(a timestamped backup is made first)",
    )
    parser.add_argument(
        "--refresh-careers", action="store_true",
        help="re-resolve brochure-like careers pages to real job portals "
             "(updates the existing JSON in place; a backup is made first)",
    )
    parser.add_argument(
        "--limit", type=int, default=None,
        help="with --refresh-careers, only process the first N weak entries",
    )
    args = parser.parse_args()
    try:
        if args.refresh_careers:
            refresh_careers(limit=args.limit)
        else:
            main(force=args.force)
    except KeyboardInterrupt:
        print("\nInterrupted. Progress saved; re-run to resume.")
