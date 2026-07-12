"""Work at a Startup (YC) source — authenticated scrape into `job_matches`.

workatastartup.com is hard-gated: every request (incl. ``companies.json``)
returns HTTP 406 unless it carries a logged-in YC session cookie. There is
no public/unauthenticated feed. So this source reads your session cookie
from a gitignored secret and replays the same JSON request the web app makes.

Providing the cookie
---------------------
Either set the env var ``WAAS_COOKIE`` or drop the raw ``Cookie:`` header
value into ``.secrets/waas_cookie.txt`` (gitignored). To grab it: log into
workatastartup.com, open DevTools → Network → any ``companies.json`` request
→ copy the full ``cookie`` request header. Cookies expire, so expect to
refresh it periodically.

The ``query`` config value is the querystring from your filtered board URL
(everything after ``companies?``), e.g.
``role=product&remote=yes&minExperience=3&locations=San%20Francisco%2C%20CA%2C%20US``.

NOTE: the exact ``companies.json`` response schema can't be verified without
a live cookie, so the parser is defensive and logs the top-level shape on the
first successful run — finalize the field mapping against that output.
"""

from __future__ import annotations

import os
from pathlib import Path

import requests

from sources.newpmjobs import is_product_role

BASE = "https://www.workatastartup.com/companies.json"
SECRET_FILE = Path(__file__).resolve().parent.parent / ".secrets" / "waas_cookie.txt"


def _load_cookie(cfg: dict) -> str:
    cookie = os.environ.get("WAAS_COOKIE", "").strip()
    if cookie:
        return cookie
    path = Path(cfg.get("cookie_file") or SECRET_FILE)
    if path.exists():
        return path.read_text(encoding="utf-8").strip()
    return ""


def _iter_roles(payload) -> list[dict]:
    """Yield (company, role) dicts from the JSON, tolerant of schema variants.

    WaaS has historically nested jobs under each company. We accept a few
    likely shapes: {"companies":[{name/company_name, jobs:[{title,...}]}]},
    a bare list of companies, or a flat {"jobs":[...]} list.
    """
    rows: list[dict] = []

    def add(company: str, job: dict):
        title = (job.get("title") or job.get("role") or job.get("name") or "").strip()
        if not company or not title:
            return
        loc = job.get("location") or job.get("locations") or ""
        if isinstance(loc, list):
            loc = ", ".join(str(x) for x in loc)
        slug = job.get("slug") or job.get("id") or ""
        url = job.get("url") or job.get("apply_url") or (
            f"https://www.workatastartup.com/jobs/{slug}" if slug else ""
        )
        rows.append({"company": company.strip(), "title": title, "location": str(loc).strip(), "url": url})

    companies = None
    if isinstance(payload, dict):
        companies = payload.get("companies")
        if companies is None and isinstance(payload.get("jobs"), list):
            for j in payload["jobs"]:
                add((j.get("company_name") or (j.get("company") or {}).get("name") or ""), j)
            return rows
    elif isinstance(payload, list):
        companies = payload

    for c in companies or []:
        if not isinstance(c, dict):
            continue
        cname = c.get("name") or c.get("company_name") or ""
        for j in c.get("jobs") or c.get("open_roles") or c.get("roles") or []:
            if isinstance(j, dict):
                add(cname, j)
    return rows


def fetch(cfg: dict | None = None) -> list[dict]:
    """Pull WaaS companies.json with the user's session cookie → job dicts."""
    cfg = cfg or {}
    cookie = _load_cookie(cfg)
    if not cookie:
        print("  WaaS: no session cookie (set WAAS_COOKIE or .secrets/waas_cookie.txt) — skipping")
        return []

    headers = {
        "Cookie": cookie,
        "Accept": "application/json, text/plain, */*",
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"
        ),
        "X-Requested-With": "XMLHttpRequest",
        "Referer": "https://www.workatastartup.com/companies",
    }
    query = (cfg.get("query") or "role=product&remote=yes").lstrip("?")
    url = f"{BASE}?{query}"
    try:
        resp = requests.get(url, headers=headers, timeout=25)
        resp.raise_for_status()
        payload = resp.json()
    except Exception as e:
        print(f"  WaaS error: {e} (cookie likely expired — refresh it)")
        return []

    if cfg.get("debug"):
        shape = list(payload.keys()) if isinstance(payload, dict) else f"list[{len(payload)}]"
        print(f"  WaaS payload shape: {shape}")

    product_only = cfg.get("product_only", True)
    out: list[dict] = []
    for r in _iter_roles(payload):
        if product_only and not is_product_role(r["title"]):
            continue
        out.append({
            "company_name": r["company"],
            "company_description": "",
            "role_title": r["title"],
            "location": r["location"],
            "url": r["url"],
            "priority": "",
            "status": "",
            "source": "WaaS",
            "date_found": "",  # stamped at insert time (no reliable post date in feed)
        })
    return out
