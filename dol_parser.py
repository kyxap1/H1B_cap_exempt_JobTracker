"""Download DOL OFLC LCA Excel files and parse cap-exempt H1B employers."""

from __future__ import annotations

import sys
from collections import Counter
from pathlib import Path

try:
    import pandas as pd
except ImportError:
    sys.exit("pandas required: pip install pandas openpyxl")

try:
    from curl_cffi import requests as cf_requests
except ImportError:
    sys.exit("curl_cffi required: pip install curl_cffi")

from config import CAP_EXEMPT_NAICS, CAP_EXEMPT_KEYWORDS, normalize_name, title_case

# ---------------------------------------------------------------------------
# Column resolution (DOL changes names slightly between fiscal years)
# ---------------------------------------------------------------------------

NEEDED_COLS = ["VISA_CLASS", "CASE_STATUS", "EMPLOYER_NAME",
               "TOTAL_WORKERS", "WORKSITE_STATE", "NAICS_CODE"]

COL_ALIASES = {
    "NAICS_CODE":    ["NAICS_CODE", "NAICS", "NAIC_CODE"],
    "WORKSITE_STATE":["WORKSITE_STATE", "WORKLOC1_STATE", "WORK_STATE"],
    "TOTAL_WORKERS": ["TOTAL_WORKERS", "NBR_WORKERS", "TOTAL_WORKER_POSITIONS"],
}


def resolve_cols(available: list[str]) -> dict[str, str]:
    """Return {canonical_name: actual_column_name} for this file's headers."""
    avail_upper = {c.upper(): c for c in available}
    mapping = {}
    for canonical, aliases in COL_ALIASES.items():
        for alias in aliases:
            if alias.upper() in avail_upper:
                mapping[canonical] = avail_upper[alias.upper()]
                break
    for col in NEEDED_COLS:
        if col not in mapping and col.upper() in avail_upper:
            mapping[col] = avail_upper[col.upper()]
    return mapping


# ---------------------------------------------------------------------------
# Cap-exempt detection
# ---------------------------------------------------------------------------

def is_cap_exempt(employer_name: str, naics: str) -> bool:
    naics_str = str(naics).strip() if naics else ""
    if any(naics_str.startswith(p) for p in CAP_EXEMPT_NAICS):
        return True
    return any(kw in employer_name.lower() for kw in CAP_EXEMPT_KEYWORDS)


# ---------------------------------------------------------------------------
# Download
# ---------------------------------------------------------------------------

def download_file(url: str, dest: Path) -> None:
    if dest.exists():
        print(f"  [cached] {dest.name}")
        return
    print(f"  downloading {dest.name} ...", flush=True)
    r = cf_requests.get(url, impersonate="chrome", stream=True, timeout=300)
    r.raise_for_status()
    total = int(r.headers.get("content-length", 0))
    # Live \r progress bar only on a terminal; in logs/non-TTY print at
    # coarse milestones so we don't flood the output with thousands of lines.
    is_tty = sys.stdout.isatty()
    downloaded = 0
    next_milestone = 10
    with open(dest, "wb") as f:
        for chunk in r.iter_content(chunk_size=1024 * 1024):
            f.write(chunk)
            downloaded += len(chunk)
            if not total:
                continue
            pct = downloaded / total * 100
            if is_tty:
                print(f"\r  {pct:5.1f}%  ({downloaded // 1_000_000}MB / {total // 1_000_000}MB)",
                      end="", flush=True)
            elif pct >= next_milestone:
                print(f"  {pct:5.1f}%  ({downloaded // 1_000_000}MB / {total // 1_000_000}MB)",
                      flush=True)
                next_milestone += 10
    if is_tty and total:
        print()


# ---------------------------------------------------------------------------
# Parse
# ---------------------------------------------------------------------------

def parse_year(path: Path, year: int) -> dict[str, dict]:
    """Return {normalized_name: {name, state, count}} for cap-exempt H1B employers."""
    print(f"  reading {path.name} ...", flush=True)
    col_map = resolve_cols(list(pd.read_excel(path, nrows=0, engine="openpyxl").columns))

    missing = [c for c in NEEDED_COLS if c not in col_map]
    if missing:
        print(f"  [warn] missing columns for {year}: {missing}")

    df = pd.read_excel(path, usecols=list(set(col_map.values())), engine="openpyxl", dtype=str)
    df = df.rename(columns={v: k for k, v in col_map.items()})
    for col in NEEDED_COLS:
        if col not in df.columns:
            df[col] = ""

    df = df[df["VISA_CLASS"].str.upper().str.strip() == "H-1B"]
    df = df[df["CASE_STATUS"].str.upper().str.strip().isin(["CERTIFIED", "CERTIFIED - WITHDRAWN"])]
    df["TOTAL_WORKERS"] = pd.to_numeric(df["TOTAL_WORKERS"], errors="coerce").fillna(0).astype(int)

    results: dict[str, dict] = {}
    for _, row in df.iterrows():
        emp   = title_case(str(row["EMPLOYER_NAME"]).strip())
        naics = str(row.get("NAICS_CODE", "")).strip()
        if not emp or emp.lower() == "nan" or not is_cap_exempt(emp, naics):
            continue
        key = normalize_name(emp)
        if key not in results:
            results[key] = {"name": emp, "state": "", "count": 0, "state_counts": Counter()}
        results[key]["count"] += int(row["TOTAL_WORKERS"])
        state = str(row.get("WORKSITE_STATE", "")).strip()
        if state and state.lower() != "nan" and len(state) == 2:
            results[key]["state_counts"][state.upper()] += int(row["TOTAL_WORKERS"])

    for v in results.values():
        sc = v.pop("state_counts")
        if sc:
            top, top_n = sc.most_common(1)[0]
            v["state"] = top if top_n / sum(sc.values()) >= 0.6 else "Multiple"

    print(f"  {year}: {len(results)} cap-exempt employers found")
    return results
