"""Shared constants, paths, data model, and utility functions."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

PROJECT_DIR   = Path(__file__).parent
DATA_DIR      = PROJECT_DIR / "lca_data"
COMPANIES_CSV = PROJECT_DIR / "h1b_cap_exempt_sponsors.csv"
CHECKPOINT    = PROJECT_DIR / "h1b_cap_exempt_checkpoint.json"

# ---------------------------------------------------------------------------
# Pipeline settings
# ---------------------------------------------------------------------------

TOP_N = 1000

DOL_FILES = {
    2024: "https://www.dol.gov/sites/dolgov/files/ETA/oflc/pdfs/LCA_Disclosure_Data_FY2024_Q4.xlsx",
    2025: "https://www.dol.gov/sites/dolgov/files/ETA/oflc/pdfs/LCA_Disclosure_Data_FY2025_Q3.xlsx",
    2026: "https://www.dol.gov/sites/dolgov/files/ETA/oflc/pdfs/LCA_Disclosure_Data_FY2026_Q1.xlsx",
}

CAP_EXEMPT_NAICS = ("6113", "6221", "8139")

CAP_EXEMPT_KEYWORDS = (
    "university", "medical center", "institute",
    "national laboratory", "association",
)

# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class Company:
    name: str
    counts: dict = field(default_factory=dict)
    state: str = ""

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def normalize_name(name: str) -> str:
    s = name.lower().strip()
    s = re.sub(r"[^a-z0-9]+", " ", s)
    s = re.sub(r"\b(inc|llc|llp|corp|corporation|company|co|ltd|the)\b", " ", s)
    return re.sub(r"\s+", " ", s).strip()
