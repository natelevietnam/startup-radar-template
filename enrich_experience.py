"""Read each posting's stated product-experience requirement and store it.

No feed supplies this. `company_description` is a metadata blurb the source
adapters assemble from industry, level and comp — the years requirement exists
only in the posting body, so it has to be fetched.

Coverage is partial by nature: roughly half of postings are client-rendered
shells whose requirements never appear in the fetched HTML. That is fine, and
it is the reason for the central rule here:

    a requirement that could not be read is NOT a requirement that was exceeded

Rows we cannot parse keep `years_required = NULL` and are never excluded.

Three things the parser is careful about, each learned from a real posting:

  * "3-5+ years of product management" is a RANGE — the bar is its lower bound
    (3), not 5. Taking the upper bound would over-exclude.
  * Visa states "8 or more years" as a basic qualification and "9 or more" as
    preferred. A "preferred" bar is not the requirement, so anything following
    preferred/nice-to-have/bonus wording is skipped.
  * "8+ years of relevant work experience" is not a claim about product
    experience. Generic years are recorded separately and never drive exclusion.

Usage:
    python enrich_experience.py [--dry-run] [--apply] [--limit N] [--recheck]

    (default)   fetch and store the requirement for rows that lack one
    --dry-run   report what would be stored, write nothing
    --apply     additionally file rows above the cap as Not Interested
    --recheck   re-fetch rows already carrying a value
"""

from __future__ import annotations

import concurrent.futures as cf
import re
import sqlite3
import sys
from pathlib import Path

import requests

import database
import filters
from config_loader import load_config

ROOT = Path(__file__).resolve().parent
DB = ROOT / "startup_radar.db"

_UA = {
    "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"),
    "Accept-Language": "en-US,en;q=0.9",
}
_TIMEOUT = 12
_WORKERS = 6

# "3-5+ years", "8 or more years", "minimum of 6 years", "at least 5 years"
_NUM = re.compile(
    r"(?:minimum(?:\s+of)?\s+|at\s+least\s+)?"
    r"(?:(\d{1,2})\s*[-–to]{1,3}\s*)?(\d{1,2})\s*(?:\+|or\s+more|plus)?\s*year",
    re.IGNORECASE,
)
_PREFERRED = re.compile(r"(preferred|nice[- ]to[- ]have|bonus|desired|a plus)", re.IGNORECASE)
# The requirement must be ABOUT product work, not merely mention the word. Google
# writes "8 years of work experience using analytics to solve product or business
# problems" — there "product" modifies *problems*, and the bar is generic work
# experience. Salesforce writes "8+ years technical program management, product
# operations" — program management, not product management. Both were false
# positives until this demanded an actual product-practitioner phrase.
_PRODUCT = re.compile(
    r"\bproduct\s+(?:management|manager|leadership|owner|strategy)\b", re.IGNORECASE)


def extract(html: str) -> tuple[int | None, int | None, str]:
    """(product_years, generic_years, evidence). None where nothing was read."""
    text = re.sub(r"<[^>]+>", " ", html or "")
    text = re.sub(r"\s+", " ", text)
    prod = gen = None
    evidence = ""
    for m in _NUM.finditer(text):
        lo, hi = m.group(1), m.group(2)
        years = int(lo or hi)                      # a range means its lower bound
        if not 1 <= years <= 25:
            continue
        before = text[max(0, m.start() - 170):m.start()]
        if _PREFERRED.search(before[-170:]):
            continue                               # a preferred bar is not the bar
        after = text[m.end():m.end() + 90]
        if _PRODUCT.search(after) or _PRODUCT.search(before[-60:]):
            if prod is None or years < prod:
                prod = years
                evidence = (before[-70:] + m.group(0) + after[:70]).strip()
        elif gen is None or years < gen:
            gen = years
    return prod, gen, evidence


def _fetch(row: dict) -> tuple[dict, int | None, str, str]:
    try:
        resp = requests.get(row["url"], headers=_UA, timeout=_TIMEOUT)
    except Exception as exc:                       # timeout, DNS, TLS — unreadable
        return row, None, "", type(exc).__name__
    if resp.status_code != 200:
        return row, None, "", f"HTTP {resp.status_code}"
    prod, gen, evidence = extract(resp.text)
    if prod is None:
        return row, None, "", ("generic %dy only" % gen) if gen else "no requirement found"
    return row, prod, evidence, "ok"


def main(argv: list[str]) -> int:
    dry = "--dry-run" in argv
    apply_cuts = "--apply" in argv
    recheck = "--recheck" in argv
    limit = 0
    if "--limit" in argv:
        limit = int(argv[argv.index("--limit") + 1])

    flt = filters.JobFilter(load_config())
    if flt.max_years_experience is None:
        print("targets.max_years_experience is not set — nothing to enforce.")
        return 0

    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    where = "" if recheck else " AND years_required IS NULL"
    rows = [dict(r) for r in con.execute(
        "SELECT id, company_name, role_title, url FROM job_matches "
        "WHERE TRIM(COALESCE(status,'')) = '' AND TRIM(COALESCE(url,'')) <> ''" + where
        + " ORDER BY id" + (f" LIMIT {limit}" if limit else ""))]
    print(f"reading {len(rows)} posting(s) · cap {flt.max_years_experience}y"
          f"{' (dry-run)' if dry else ''}")

    found, over, cleared, unread = [], [], [], 0
    with cf.ThreadPoolExecutor(max_workers=_WORKERS) as ex:
        for row, years, evidence, note in ex.map(_fetch, rows):
            if years is None:
                unread += 1
                # On a recheck a row may already carry a value that the current
                # parser no longer produces — a tightened rule, or a posting that
                # changed. Leaving it would keep enforcing a figure nothing
                # supports, so the stale value is cleared. Only when the page was
                # actually readable: a timeout must not erase good data.
                if recheck and note.startswith(("no requirement", "generic")):
                    cleared.append(row)
                continue
            found.append((row, years, evidence))
            if flt.experience_excluded(years):
                over.append((row, years, evidence))

    print(f"  requirement read: {len(found)} · unreadable: {unread} · "
          f"above the cap: {len(over)}"
          + (f" · stale values to clear: {len(cleared)}" if cleared else ""))
    for row, years, _ in sorted(over, key=lambda t: -t[1]):
        print(f"    {years:>2}y  #{row['id']} {row['company_name'][:22]:<22} "
              f"{row['role_title'][:44]}")

    if dry:
        return len(over)

    con.executemany(
        "UPDATE job_matches SET years_required = ?, years_evidence = ? WHERE id = ?",
        [(y, e[:400], r["id"]) for r, y, e in found])
    if cleared:
        con.executemany(
            "UPDATE job_matches SET years_required = NULL, years_evidence = '' "
            "WHERE id = ?", [(r["id"],) for r in cleared])
    con.commit()
    print(f"  stored {len(found)} requirement(s)"
          + (f", cleared {len(cleared)}" if cleared else ""))

    if apply_cuts and over:
        con.executemany(
            "UPDATE job_matches SET status = 'Not Interested', "
            "notes = TRIM(COALESCE(notes,'') || ' [over " + str(flt.max_years_experience)
            + "y product experience]') WHERE id = ? AND TRIM(COALESCE(status,'')) = ''",
            [(r["id"],) for r, _, _ in over])
        con.commit()
        print(f"  filed {len(over)} row(s) as Not Interested")
    elif over:
        print("  (not filed — pass --apply to act on them)")
    con.close()
    return len(over)


if __name__ == "__main__":
    sys.exit(0 if main(sys.argv[1:]) >= 0 else 1)
