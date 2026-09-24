"""NewPMJobs.com source — pulls PM job listings from a public JSON API.

NewPMJobs.com (built by Vik Agarwal) tracks product-management postings
across companies. The site is a client-rendered Next.js SPA that requires
auth for personalized features, but it exposes an unauthenticated
``/api/feed`` endpoint returning every active job as structured JSON.

We pull that feed, map each entry to a ``job_matches`` row, and let the
caller dedupe + filter. No API key, no scraping, no headless browser.
"""

from __future__ import annotations

import re

import requests

API_URL = "https://api.newpmjobs.com/api/feed"
HEADERS = {"User-Agent": "startup-radar-template/1.0 (https://github.com/natelevietnam/startup-radar-template)"}


# Despite the site name, the feed includes adjacent non-PM roles such as
# Business-Operations Program Managers and Chief-of-Staff postings at
# product-led orgs. These patterns mark a title as a *real* product role.
_PM_INCLUDE = (
    "product manager",
    "product managers",
    "product management",
    "product lead",
    # Word-boundary matching means "product lead" no longer fires inside
    # "Product Leader", so the variant needs its own entry.
    "product leader",
    "head of product",
    "director of product",
    "director, product",
    "vp of product",
    "vp, product",
    "chief product",
    "product owner",
    "founding pm",
)

# False friends — the title contains a PM-adjacent phrase but is not a PM role.
_PM_EXCLUDE = (
    "program manager",
    "project manager",
    "product marketing",
    "product designer",
    "product design",
    "product analyst",
    "chief of staff",
    "engineering manager",
    # Life-sciences regulatory/quality titles. In pharma, "product" names the
    # drug, not a software surface, so phrases like "Drug-Device Combination
    # Product Lead" or "Product Development Quality Assurance (CMC Product
    # Lead)" match _PM_INCLUDE while being regulatory roles. Added 2026-08-11
    # after Biogen's "Senior Regulatory CMC Drug-Device Combination Product
    # Lead" reached the board.
    "regulatory affairs",
    "regulatory cmc",
    "cmc",
    "drug-device",
    "drug device",
    "pharmacovigilance",
    "medical affairs",
    "clinical operations",
    "quality assurance",
)


def is_product_role(title: str) -> bool:
    """True if the role title looks like a Product-Management role
    (PM / Sr PM / Staff PM / Head of Product / CPO / Product Owner / ...).

    Matching is word-boundary aware so a term never fires inside a longer
    word (e.g. "cmc" must stand alone, not match "cmcarthur").
    """
    if not title:
        return False
    t = title.lower()
    if any(re.search(r"\b" + re.escape(bad) + r"\b", t) for bad in _PM_EXCLUDE):
        return False
    if any(re.search(r"\b" + re.escape(good) + r"\b", t) for good in _PM_INCLUDE):
        return True
    # Standalone "PM" abbreviation (word-boundary, uppercase only to avoid
    # matching "pm" inside words like "campaign").
    if re.search(r"\bPM\b", title):
        return True
    return False


def _thousands(amount: float, symbol: str = "$") -> str:
    """"$274,786" as "$275K" — an estimate should not read to the dollar."""
    return f"{symbol}{round(amount / 1000):,}K"


def _format_comp(comp: dict) -> str:
    """Describe the feed's ``comp`` object as the market data it actually is.

    This is **not** the band in the posting. The object carries ``median``,
    ``tier``, ``sourceReportCount`` and a ``sourceUrl`` pointing at
    ``levels.fyi/companies/<company>/salaries/product-manager``: it is
    levels.fyi's reported total compensation for that *title at that company*,
    which the feed attaches to every one of its postings.

    Rendering it as a bare range put fictions in front of real decisions —
    Samsara as "USD274,786–USD625,833", OpenAI and Hinge Health as flat
    "USD750,000–USD750,000" and "USD447,000–USD447,000" bands that were never
    ranges at all. So the source is named, the figures are rounded to the
    thousand to read as estimates, and a min that equals its max is rendered
    as the single data point it is rather than as a range.
    """
    lo, hi = comp.get("min"), comp.get("max")
    if not isinstance(lo, (int, float)) or not isinstance(hi, (int, float)):
        return ""
    if lo <= 0 or hi <= 0:
        return ""
    if hi < lo:
        lo, hi = hi, lo

    # Only USD carries a "$"; any other currency is named instead, so a figure
    # never reads as dollars because the dollar sign was hardcoded.
    cur = (comp.get("currency") or "USD").upper()
    sym = "$" if cur == "USD" else ""
    prefix = "levels.fyi" if cur == "USD" else f"levels.fyi {cur}"

    n = comp.get("sourceReportCount")
    if isinstance(n, int) and n > 0:
        reports = f"{n} report" + ("s" if n != 1 else "")
    else:
        reports = "report count unstated"

    if lo == hi:
        # One data point, not a band — levels.fyi returns min == max == median
        # when a single submission (or none) backs the figure.
        return f"{prefix} ~{_thousands(lo, sym)} ({reports})"

    median = comp.get("median")
    mid = (f", median {_thousands(median, sym)}"
           if isinstance(median, (int, float)) and lo <= median <= hi else "")
    return f"{prefix} {_thousands(lo, sym)}–{_thousands(hi, sym)}{mid} ({reports})"


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
        bit = _format_comp(comp)
        if bit:
            bits.append(bit)
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
    product_only = cfg.get("product_only", True)
    out: list[dict] = []
    for j in jobs:
        if j.get("status") and j["status"] != "active":
            continue
        company = (j.get("company") or {}).get("name") or ""
        role = j.get("title") or ""
        if not company or not role:
            continue
        if product_only and not is_product_role(role):
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
