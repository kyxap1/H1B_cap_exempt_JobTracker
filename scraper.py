"""
Build H1B cap-exempt sponsor list from DOL OFLC LCA data.

Pipeline:
  1. Download DOL Excel files  → lca_data/
  2. Parse + aggregate         → top N cap-exempt employers
  3. Write output              → h1b_cap_exempt_sponsors.csv
"""

from __future__ import annotations

import csv
import json

from config import (
    DATA_DIR, COMPANIES_CSV, CHECKPOINT, DOL_FILES, TOP_N,
    Company, normalize_name,
)
from dol_parser import download_file, parse_year


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
    state     = load_checkpoint()
    list_data = state.get("list_data", {})

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

    # Phase 3 — Write CSV
    with open(COMPANIES_CSV, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["company", "state", "h1b_2026", "h1b_2025", "h1b_2024"])
        for c in top:
            writer.writerow([c.name, c.state,
                             c.counts.get(2026, ""),
                             c.counts.get(2025, ""),
                             c.counts.get(2024, "")])
    print(f"\nDone → {COMPANIES_CSV}")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nInterrupted. Progress saved; re-run to resume.")
