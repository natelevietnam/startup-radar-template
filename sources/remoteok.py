"""Remote OK source — pulls remote job listings from a public JSON API.

Remote OK (remoteok.com) aggregates remote-friendly roles, many at startups.
It exposes an unauthenticated ``/api`` endpoint returning active jobs as
structured JSON — no API key, no scraping, no headless browser.

The feed's first element is a metadata/legal object (``last_updated`` +
``legal``), not a job; we skip it. By default we keep only true
Product-Management roles (reusing :func:`sources.newpmjobs.is_product_role`)
so the feed stays consistent with the other job sources; set
``product_only: false`` in config to ingest every role.
"""

from __future__ import annotations

import html
import re

import requests

from sources.newpmjobs import is_product_role

API_URL = "https://remoteok.com/api"
HEADERS = {"User-Agent": "startup-radar-template/1.0 (https://github.com/natelevietnam/startup-radar-template)"}

_TAG_RE = re.compile(r"<[^>]+>")


def _strip_html(text: str) -> str:
    """Remote OK descriptions are HTML; flatten to plain text for storage."""
    if not text:
        return ""
    return html.unescape(_TAG_RE.sub(" ", text)).strip()


def _format_company_description(job: dict) -> str:
    """Best-effort context line so the dashboard shows something meaningful."""
    bits: list[str] = []
    tags = job.get("tags")
    if isinstance(tags, list) and tags:
        bits.append(", ".join(str(t) for t in tags[:5]))
    lo, hi = job.get("salary_min"), job.get("salary_max")
    if isinstance(lo, (int, float)) and isinstance(hi, (int, float)) and hi > 0:
        bits.append(f"${int(lo):,}–${int(hi):,}")
    return " • ".join(bits)


def fetch(cfg: dict | None = None) -> list[dict]:
    """Pull the public Remote OK feed and return JobMatch-shaped dicts.

    The feed carries ~100 of the most recent active jobs and refreshes
    server-side, so we grab the whole thing each run; dedup is handled
    downstream via the (company_name, role_title) unique index on
    job_matches.
    """
    cfg = cfg or {}
    try:
        resp = requests.get(API_URL, headers=HEADERS, timeout=20)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        print(f"  Remote OK error: {e}")
        return []

    if not isinstance(data, list):
        return []

    product_only = cfg.get("product_only", True)
    out: list[dict] = []
    for j in data:
        # Skip the leading metadata/legal object (no "position" key).
        if not isinstance(j, dict) or not j.get("position"):
            continue
        company = (j.get("company") or "").strip()
        role = (j.get("position") or "").strip()
        if not company or not role:
            continue
        if product_only and not is_product_role(role):
            continue
        out.append({
            "company_name": company,
            "company_description": _format_company_description(j),
            "role_title": role,
            "location": (j.get("location") or "").strip(),
            "url": j.get("url") or j.get("apply_url") or "",
            "priority": "",
            "status": "",
            "source": "Remote OK",
            "date_found": (j.get("date") or "")[:10],
        })
    return out
