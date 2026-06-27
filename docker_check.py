"""Smoke test run inside the container: compile, config exports, filter logic."""
import ast
import py_compile
import sys

import config as c

# 1) every config name imported by the other modules must exist
need = {}
for f in ["jobs.py", "ats_scrapers.py", "sponsors.py",
          "careers_finder.py", "dol_parser.py", "cron_jobs.py"]:
    for n in ast.walk(ast.parse(open(f).read())):
        if isinstance(n, ast.ImportFrom) and n.module == "config":
            for a in n.names:
                need.setdefault(a.name, []).append(f)
missing = [k for k in need if not hasattr(c, k)]
print("config exports needed:", sorted(need))
print("MISSING from config:", missing or "none — all present")

# 2) job-title filter
should_pass = [
    "Senior DevOps Engineer", "DevSecOps Engineer",
    "Cloud Infrastructure Engineer", "Infrastructure Engineer II",
    "Network Infrastructure Architect",
]
should_reject = [
    "Cybersecurity Analyst", "Site Reliability Engineer (SRE)",
    "Securities Trading Associate", "Registered Nurse",
    "Data Scientist", "Marketing Manager",
]
ok = all(c.is_it_job(t) for t in should_pass) and not any(c.is_it_job(t) for t in should_reject)
for t in should_pass:
    print(("  PASS" if c.is_it_job(t) else "  !!FAIL"), t)
for t in should_reject:
    print(("  reject" if not c.is_it_job(t) else "  !!FAIL"), t)

# 3) heavy deps actually import (proves requirements installed)
import pandas, bs4, playwright, curl_cffi, openpyxl, requests  # noqa
from importlib.metadata import version
from playwright.sync_api import sync_playwright
print("deps import OK: pandas", pandas.__version__, "| playwright", version("playwright"))

# 4) playwright can actually launch chromium and read a title
with sync_playwright() as p:
    b = p.chromium.launch(headless=True)
    pg = b.new_context().new_page()
    pg.set_content("<h1>DevOps Engineer</h1><a href='/jobs/1'>x</a>")
    title = pg.locator("h1").inner_text()
    b.close()
print("chromium launch OK, read:", title)

print("\nMISSING:", missing, "| FILTER_OK:", ok)
sys.exit(0 if (not missing and ok) else 1)
