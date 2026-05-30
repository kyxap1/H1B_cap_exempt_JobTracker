"""Find company careers pages via Google search (Serper.dev API)."""

from __future__ import annotations

import json
import os
import sys

import requests

from config import CAREERS_KEYWORDS, AGGREGATOR_BLOCKLIST, polite_sleep

SERPER_API_KEY = os.environ.get("SERPER_API_KEY", "")


def google_search(query: str, num: int = 10) -> list[str]:
    if not SERPER_API_KEY:
        sys.exit(
            "\nSerper API key not set. Google blocks automated search without an API key.\n"
            "1. Get a free key at https://serper.dev (2,500 searches/month, no credit card)\n"
            "2. Run:  export SERPER_API_KEY=your_key_here  then re-run the script."
        )
    try:
        resp = requests.post(
            "https://google.serper.dev/search",
            headers={"X-API-KEY": SERPER_API_KEY, "Content-Type": "application/json"},
            data=json.dumps({"q": query, "num": num, "gl": "us", "hl": "en"}),
            timeout=15,
        )
        resp.raise_for_status()
        return [r["link"] for r in resp.json().get("organic", [])]
    except Exception as e:
        print(f"  [serper error] {e}")
        return []


def find_careers_page(company: str) -> str:
    for query in [f"{company} careers", f"{company} jobs"]:
        for u in google_search(query):
            low = u.lower()
            if any(bad in low for bad in AGGREGATOR_BLOCKLIST):
                continue
            if any(k in low for k in CAREERS_KEYWORDS):
                return u
        polite_sleep()

    # Fallback: first non-aggregator result
    for u in google_search(f"{company} official site", num=5):
        if not any(bad in u.lower() for bad in AGGREGATOR_BLOCKLIST):
            return u
    return ""


