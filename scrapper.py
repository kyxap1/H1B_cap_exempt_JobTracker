"""
Build H1B cap-exempt sponsor list from DOL OFLC LCA data.

Pipeline:
  1. Download DOL Excel files  → lca_data/
  2. Parse + aggregate         → top 200 cap-exempt employers
  3. Find careers pages        → via Google (Serper.dev API)
  4. Write output              → h1b_cap_exempt_sponsors.csv

Requires: export SERPER_API_KEY=your_key_here
"""

from __future__ import annotations

import csv
import json

from config import (
    DATA_DIR, COMPANIES_CSV, CHECKPOINT, DOL_FILES, TOP_N,
    Company, normalize_name, polite_sleep,
)
from dol_parser import download_file, parse_year
from careers_finder import find_careers_page


def load_checkpoint() -> dict:
    if CHECKPOINT.exists():
        try:
            return json.loads(CHECKPOINT.read_text())
        except json.JSONDecodeError:
            print("[warn] checkpoint corrupt; ignoring")
    return {}


def save_checkpoint(data: dict) -> None:
    CHECKPOINT.write_text(json.dumps(data, indent=2))


def main() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    state      = load_checkpoint()
    list_data  = state.get("list_data", {})
    enrichment = state.get("enrichment", {})

    # Phase 1 — Download
    print("\n=== Phase 1: Downloading DOL LCA data ===")
    for url in DOL_FILES.values():
        download_file(url, DATA_DIR / url.split("/")[-1])

    # Phase 2 — Parse + merge years
    print("\n=== Phase 2: Parsing LCA data ===")
    companies: dict[str, Company] = {}
    for year, url in DOL_FILES.items():
        key = str(year)
        if key in list_data and list_data[key]:
            print(f"[checkpoint] {year}: {len(list_data[key])} employers")
            year_data = list_data[key]
        else:
            year_data = parse_year(DATA_DIR / url.split("/")[-1], year)
            list_data[key] = year_data
            state["list_data"] = list_data
            save_checkpoint(state)

        for norm_key, info in year_data.items():
            c = companies.setdefault(norm_key, Company(name=info["name"]))
            if len(info["name"]) > len(c.name):
                c.name = info["name"]
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

    # Phase 4 — Write CSV
    with open(COMPANIES_CSV, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["company", "careers_page", "state",
                         "h1b_2026", "h1b_2025", "h1b_2024"])
        for c in top:
            writer.writerow([c.name, c.careers_page, c.state,
                             c.counts.get(2026, ""),
                             c.counts.get(2025, ""),
                             c.counts.get(2024, "")])
    print(f"\nDone → {COMPANIES_CSV}")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nInterrupted. Progress saved; re-run to resume.")
