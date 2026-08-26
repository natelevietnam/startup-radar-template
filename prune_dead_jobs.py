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
import sqlite3
import sys
from datetime import datetime

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
_WORKERS = 12


def _db_path() -> str:
    cfg = load_config()
    return ((cfg.get("output", {}).get("sqlite", {}) or {}).get("path")) or "startup_radar.db"


def _classify(row: dict) -> tuple[dict, bool, str]:
    """Return (row, is_dead, reason). is_dead True only on strong signals."""
    url = row["url"]
    try:
        resp = requests.get(url, headers=_UA, timeout=_TIMEOUT, allow_redirects=True)
        code = resp.status_code
        if code in (404, 410):
            return row, True, f"HTTP {code}"
        if code == 200:
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
            elif reason not in ("HTTP 200",):
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
