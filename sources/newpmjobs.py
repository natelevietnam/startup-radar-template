"""NewPMJobs.com source — pulls PM job listings from a public JSON API.

NewPMJobs.com (built by Vik Agarwal) tracks product-management postings
across companies. The site is a client-rendered Next.js SPA that requires
auth for personalized features, but it exposes an unauthenticated
``/api/feed`` endpoint returning every active job as structured JSON.

We pull that feed, map each entry to a ``job_matches`` row, and let the
caller dedupe + filter. No API key, no scraping, no headless browser.
"""

from __future__ import annotations

import requests
from typing import Iterable

API_URL = "https://api.newpmjobs.com/api/feed"
HEADERS = {"User-Agent": "startup-radar-template/1.0 (https://github.com/natelevietnam/startup-radar-template)"}


def _format_company_description(job: dict) -> str:
    """Best-effort context line so the dashboard shows something meaningful."""
    company = job.get("company") or {}
    bits: list[str] = []
    industry = company.get("industry")
    if industry:
        bits.append(industry)
    level = job.get("level")
    if level:
        bits.append(f"level: {level}")
    comp = job.get("comp")
    if isinstance(comp, dict):
        # comp might carry {min, max, currency} or similar — flatten lightly.
        amt_min = comp.get("min")
        amt_max = comp.get("max")
        cur = comp.get("currency") or "$"
        if amt_min and amt_max:
            bits.append(f"{cur}{amt_min:,}–{cur}{amt_max:,}")
    elif isinstance(comp, str):
        bits.append(comp)
    return " • ".join(bits)


def fetch(cfg: dict | None = None) -> list[dict]:
    """Pull the public NewPMJobs feed and return JobMatch-shaped dicts.

    The feed is small (~50 active jobs) and refreshes server-side, so we
    just grab the whole thing each run; dedup is handled downstream via
    the (company_name, role_title) unique index on job_matches.
    """
    cfg = cfg or {}
    try:
        resp = requests.get(API_URL, headers=HEADERS, timeout=20)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        print(f"  NewPMJobs error: {e}")
        return []

    jobs = data.get("jobs") or []
    out: list[dict] = []
    for j in jobs:
        if j.get("status") and j["status"] != "active":
            continue
        company = (j.get("company") or {}).get("name") or ""
        role = j.get("title") or ""
        if not company or not role:
            continue
        out.append({
            "company_name": company.strip(),
            "company_description": _format_company_description(j),
            "role_title": role.strip(),
            "location": (j.get("location") or "").strip(),
            "url": j.get("urlPath") or "",
            "priority": "",
            "status": "",
            "source": "NewPMJobs",
            "date_found": (j.get("firstSeenAt") or "")[:10],
        })
    return out
