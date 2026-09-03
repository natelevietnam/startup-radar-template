"""Remove job_matches whose posting is gone (404/410 or an explicit closed page).

Conservative by design: a row is deleted ONLY on a strong dead signal. Anything
ambiguous — bot-blocks (401/403/406/429), timeouts, connection errors, 5xx, or a
200 page without a closed-marker — is KEPT, so live jobs are never pruned by
mistake.

Usage:
    python prune_dead_jobs.py [--dry-run]

Intended to run weekly via a launchd agent against the local SQLite DB the
dashboard reads. Safe to run by hand any time.
"""

from __future__ import annotations

import concurrent.futures as cf
import re
import sqlite3
import sys
import time
from datetime import datetime
from urllib.parse import parse_qs, urlparse

import requests

import database
from config_loader import load_config

_UA = {
    "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"),
    "Accept-Language": "en-US,en;q=0.9",
}

# Strong "this posting is gone" markers on a 200 page (lowercased substring match).
_DEAD_TXT = (
    "no longer available", "no longer accepting", "position has been filled",
    "this job is no longer", "posting is closed", "job not found", "req is closed",
    "this role is no longer", "not currently accepting", "position is no longer",
    "this position is closed", "opening is no longer", "404 - not found",
    "page not found", "oops! we can't find",
)

_TIMEOUT = 12
_WORKERS = 4  # low: LinkedIn 429s aggressively above this


# --- ATS-specific probes -------------------------------------------------
#
# Two HTML-level blind spots motivated these (found 2026-08-27, both silently
# keeping dead rows alive for weeks):
#
#   1. Wellfound's `?job_listing_slug=` form is a client-rendered shell that
#      returns 200 and an identical ~243KB body for EVERY slug — including a
#      slug that never existed. Its canonical `/jobs/<slug>` path does
#      discriminate: 410 for a removed posting, 404 for one that never
#      existed, 200 for a live one.
#   2. A dead Greenhouse req redirects to `/<token>?error=true`, i.e. the
#      board index, which is a 200 page with no closed-marker text.
#
# Where an ATS publishes a job-board API, ask it directly instead of reading
# HTML: the answer is authoritative and immune to both problems. Each probe
# returns (is_dead, reason) or None to mean "not my URL shape / can't tell",
# in which case the caller falls through to the next probe and finally to the
# generic HTML check.

def _probe_greenhouse(url: str):
    m = re.search(r"greenhouse\.io/(?:embed/job_app\?for=)?([^/?#]+)/jobs/(\d+)", url)
    if not m:
        return None
    token, job_id = m.groups()
    api = f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs/{job_id}"
    resp = requests.get(api, headers=_UA, timeout=_TIMEOUT)
    if resp.status_code == 404:
        # Confirm the board itself resolves; otherwise the token is simply
        # wrong (some companies proxy Greenhouse under their own domain with
        # a token we can't derive) and a 404 says nothing about the job.
        board = requests.get(f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs",
                             headers=_UA, timeout=_TIMEOUT)
        if board.status_code != 200:
            return None
        return True, "greenhouse-api 404"
    if resp.status_code == 200:
        return False, "greenhouse-api live"
    return None


def _probe_ashby(url: str):
    m = re.search(r"jobs\.ashbyhq\.com/([^/?#]+)/([0-9a-f-]{36})", url)
    if not m:
        return None
    org, posting_id = m.groups()
    resp = requests.get(f"https://api.ashbyhq.com/posting-api/job-board/{org}",
                        headers=_UA, timeout=_TIMEOUT)
    if resp.status_code != 200:
        return None
    ids = {j.get("id") for j in resp.json().get("jobs", [])}
    if not ids:
        return None
    return (posting_id not in ids,
            "ashby-api not-on-board" if posting_id not in ids else "ashby-api live")


def _probe_smartrecruiters(url: str):
    m = re.search(r"jobs\.smartrecruiters\.com/([^/?#]+)/(\d+)", url)
    if not m:
        return None
    company, posting_id = m.groups()
    resp = requests.get(
        f"https://api.smartrecruiters.com/v1/companies/{company}/postings/{posting_id}",
        headers=_UA, timeout=_TIMEOUT)
    if resp.status_code == 404:
        return True, "smartrecruiters-api 404"
    if resp.status_code == 200:
        return False, "smartrecruiters-api live"
    return None


def _probe_wellfound(url: str):
    if "wellfound.com" not in url:
        return None
    slug = parse_qs(urlparse(url).query).get("job_listing_slug", [None])[0]
    if not slug:
        m = re.search(r"/jobs/([^/?#]+)", url)
        slug = m.group(1) if m else None
    if not slug:
        return None
    resp = requests.get(f"https://wellfound.com/jobs/{slug}", headers=_UA,
                        timeout=_TIMEOUT, allow_redirects=True)
    if resp.status_code == 410:
        return True, "wellfound 410 Gone"
    if resp.status_code == 404:
        return True, "wellfound 404"
    if resp.status_code == 200:
        return False, "wellfound live"
    return None


def _probe_linkedin(url: str):
    """LinkedIn rate-limits hard (429) but does discriminate once it answers.

    Job URLs come in two shapes and BOTH must be handled — `/jobs/view/<id>`
    and the slug form `/jobs/view/<slug>-at-<company>-<id>`. Matching only the
    numeric form silently skipped every slug-form row.

    A 429 returns None (unconfirmed → kept), never a dead verdict.
    """
    if "linkedin.com/jobs/view/" not in url:
        return None
    m = re.search(r"/jobs/view/(?:[\w-]*?-)?(\d{6,})/?", url)
    if not m:
        return None
    target = f"https://www.linkedin.com/jobs/view/{m.group(1)}/"
    # Budget generously. At 3 attempts / 5s the sweep 429'd out on two thirds of
    # LinkedIn rows and reported them as unconfirmed, while a slower hand-run
    # resolved every one of them as definitively dead or live.
    for attempt in range(6):
        resp = requests.get(target, headers=_UA, timeout=_TIMEOUT, allow_redirects=True)
        if resp.status_code == 429:
            time.sleep(8 * (attempt + 1))
            continue
        if resp.status_code in (404, 410):
            return True, f"linkedin HTTP {resp.status_code}"
        if resp.status_code == 200:
            body = resp.text[:200_000].lower()
            for marker in _DEAD_TXT:
                if marker in body:
                    return True, f"linkedin 200 but '{marker}'"
            return False, "linkedin live"
        return None
    return None  # still throttled — unconfirmed, keep


_PROBES = (_probe_greenhouse, _probe_ashby, _probe_smartrecruiters, _probe_wellfound,
           _probe_linkedin)


def _db_path() -> str:
    cfg = load_config()
    return ((cfg.get("output", {}).get("sqlite", {}) or {}).get("path")) or "startup_radar.db"


def _classify(row: dict) -> tuple[dict, bool, str]:
    """Return (row, is_dead, reason). is_dead True only on strong signals."""
    url = row["url"]

    # Ask the ATS's own API first where one exists. These are authoritative and
    # sidestep both the JS-shell and the redirect-to-board-index problems that
    # make the HTML check below unreliable on Wellfound and Greenhouse.
    for probe in _PROBES:
        try:
            verdict = probe(url)
        except Exception:
            verdict = None  # network hiccup on a probe — fall through, never prune
        if verdict is not None:
            return row, verdict[0], verdict[1]

    try:
        resp = requests.get(url, headers=_UA, timeout=_TIMEOUT, allow_redirects=True)
        code = resp.status_code
        if code in (404, 410):
            return row, True, f"HTTP {code}"
        if code == 200:
            # A dead Greenhouse req 302s to the board index as `?error=true`;
            # the resulting page is a healthy 200 with no closed-marker text.
            if "error=true" in resp.url:
                return row, True, "redirected to board index (error=true)"
            body = resp.text[:200_000].lower()
            for marker in _DEAD_TXT:
                if marker in body:
                    return row, True, f"200 but '{marker}'"
            return row, False, "HTTP 200"
        return row, False, f"HTTP {code}"  # blocked / transient — keep
    except Exception as e:  # timeout, connection error, etc. — keep
        return row, False, type(e).__name__


def main(dry_run: bool = False) -> int:
    db = _db_path()
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    # Never sweep a job the user has already decided about. A posting you
    # applied to is *expected* to 404 once it is filled, and deleting the row
    # here would drop the only local record of that decision — after which the
    # append-only cloud DB re-offers the job as a fresh Uncategorized row on
    # the next sync. Decided rows are kept regardless of link health.
    placeholders = ",".join("?" * len(database.DECIDED_STATUSES))
    rows = [dict(r) for r in con.execute(
        "SELECT id, company_name, role_title, url FROM job_matches "
        "WHERE TRIM(COALESCE(url,'')) <> '' "
        f"AND COALESCE(status,'') NOT IN ({placeholders})",
        database.DECIDED_STATUSES,
    ).fetchall()]

    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{stamp}] dead-link sweep on {db} — checking {len(rows)} posting(s)"
          f"{' (dry-run)' if dry_run else ''}")

    dead: list[tuple[dict, str]] = []
    blocked = 0
    with cf.ThreadPoolExecutor(max_workers=_WORKERS) as ex:
        for row, is_dead, reason in ex.map(_classify, rows):
            if is_dead:
                dead.append((row, reason))
            elif not (reason == "HTTP 200" or reason.endswith("live")):
                # Only an affirmative liveness signal counts as live; anything
                # else (bot-block, timeout, 5xx) is unconfirmed and kept.
                blocked += 1

    live = len(rows) - len(dead) - blocked
    print(f"  live={live}  dead={len(dead)}  unconfirmed(blocked/error)={blocked}")
    for row, reason in sorted(dead, key=lambda x: x[0]["id"]):
        print(f"  DEAD [{row['id']}] {row['company_name']} | {row['role_title'][:45]} | {reason}")

    if dead and not dry_run:
        con.executemany("DELETE FROM job_matches WHERE id = ?",
                        [(row["id"],) for row, _ in dead])
        # Tombstone every pruned row. Without this the cloud DB, which never
        # learns about local deletions, re-inserts each one as Uncategorized
        # on the next sync — the sweep would undo itself every morning.
        con.executemany(
            "INSERT OR IGNORE INTO deleted_jobs (company_name, role_title) VALUES (?, ?)",
            [(row["company_name"], row["role_title"]) for row, _ in dead],
        )
        con.commit()
        print(f"  removed {len(dead)} dead posting(s) and tombstoned them; "
              f"{con.execute('SELECT COUNT(*) FROM job_matches').fetchone()[0]} rows remain")
    elif dead:
        print(f"  dry-run: would remove {len(dead)} posting(s)")
    else:
        print("  nothing to remove")

    con.close()
    return 0


if __name__ == "__main__":
    sys.exit(main(dry_run="--dry-run" in sys.argv))
