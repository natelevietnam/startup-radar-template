"""Sync pipeline results from the latest cloud cron run into the local DB.

The GitHub Actions workflow uploads ``startup_radar.db`` as an artifact on
every run. This script downloads the most recent successful run's artifact
and merges its pipeline-produced rows (startups, job matches, processed
items) into the local database — without touching user-local tables like
LinkedIn connections, application tracker, or activities.

Requirements: ``gh`` CLI authenticated against the fork.

Usage:
    python sync_from_cloud.py
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path


import re

from filters import _company_excluded, _excluded_company_patterns

REPO_DIR = Path(__file__).parent
LOCAL_DB = REPO_DIR / "startup_radar.db"
WORKFLOW = "daily.yml"
ARTIFACT_NAME = "startup-radar-db"


def _excluded_company_patterns_from_config() -> list:
    """Compiled excluded-company patterns from config.yaml (empty on any error)."""
    try:
        from config_loader import load_config
        targets = (load_config() or {}).get("targets", {}) or {}
        return _excluded_company_patterns(targets)
    except Exception:
        return []

# Tables produced by the daily pipeline — safe to merge from cloud.
PIPELINE_TABLES = ("startups", "job_matches", "processed_items")


def _fork_name_with_owner() -> str:
    """Derive owner/repo from origin's URL (the user's fork, not upstream)."""
    out = subprocess.run(
        ["git", "remote", "get-url", "origin"],
        cwd=REPO_DIR, capture_output=True, text=True, check=True,
    ).stdout.strip()
    m = re.search(r"github\.com[:/]([^/]+/[^/.]+)(?:\.git)?$", out)
    if not m:
        raise RuntimeError(f"Couldn't parse owner/repo from origin URL: {out}")
    return m.group(1)


_REPO_FLAG: list[str] | None = None


def _gh(*args: str) -> str:
    """Run a gh subcommand against the fork, returning stdout."""
    global _REPO_FLAG
    if _REPO_FLAG is None:
        _REPO_FLAG = ["--repo", _fork_name_with_owner()]
    result = subprocess.run(
        ["gh", *args, *_REPO_FLAG],
        cwd=REPO_DIR,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"gh {' '.join(args)} failed:\n{result.stderr}")
    return result.stdout


def _latest_successful_run_id() -> str:
    out = _gh(
        "run", "list",
        "--workflow", WORKFLOW,
        "--limit", "20",
        "--json", "databaseId,conclusion,status",
    )
    runs = json.loads(out or "[]")
    successful = [r for r in runs if r.get("conclusion") == "success"]
    if not successful:
        raise RuntimeError(f"No successful runs of {WORKFLOW} yet — nothing to sync.")
    return str(successful[0]["databaseId"])


def _download_artifact(run_id: str, dest: Path) -> Path:
    _gh(
        "run", "download", run_id,
        "--name", ARTIFACT_NAME,
        "--dir", str(dest),
    )
    db_path = dest / "startup_radar.db"
    if not db_path.exists():
        raise RuntimeError(f"Artifact downloaded but {db_path.name} not found in it.")
    return db_path


def _column_names(conn: sqlite3.Connection, table: str) -> list[str]:
    return [r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]


def _job_tombstones(local: sqlite3.Connection) -> set[tuple[str, str]]:
    """(company, role) keys the user has deleted locally — never re-add these.

    Lower/stripped so matching is case- and whitespace-insensitive, mirroring
    the COLLATE NOCASE unique index on job_matches.
    """
    has_table = local.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='deleted_jobs'"
    ).fetchone()
    if not has_table:
        return set()
    return {
        ((c or "").lower().strip(), (r or "").lower().strip())
        for c, r in local.execute("SELECT company_name, role_title FROM deleted_jobs")
    }


def _merge_table(cloud: sqlite3.Connection, local: sqlite3.Connection, table: str,
                 tombstones: set[tuple[str, str]] | None = None,
                 excl_co_patterns: list | None = None) -> tuple[int, int, int]:
    """Copy rows from cloud → local. Returns (inserted, skipped_duplicates, blocked).

    ``blocked`` counts rows skipped because they were deleted locally
    (tombstoned) or belong to an excluded company.
    """
    cols = _column_names(cloud, table)
    if not cols:
        return (0, 0, 0)
    insertable = [c for c in cols if c != "id"]
    placeholders = ",".join("?" * len(insertable))
    col_list = ",".join(insertable)

    # Locate key columns for tombstone / excluded-company filtering.
    ci = insertable.index("company_name") if "company_name" in insertable else None
    ri = insertable.index("role_title") if "role_title" in insertable else None
    can_tombstone = tombstones is not None and ci is not None and ri is not None
    can_exclude_co = bool(excl_co_patterns) and ci is not None

    rows = cloud.execute(
        f"SELECT {col_list} FROM {table}"
    ).fetchall()

    inserted = skipped = blocked = 0
    for row in rows:
        if can_exclude_co and _company_excluded(row[ci] or "", excl_co_patterns):
            blocked += 1
            continue
        if can_tombstone:
            key = ((row[ci] or "").lower().strip(), (row[ri] or "").lower().strip())
            if key in tombstones:
                blocked += 1
                continue
        try:
            local.execute(
                f"INSERT INTO {table} ({col_list}) VALUES ({placeholders})",
                row,
            )
            inserted += 1
        except sqlite3.IntegrityError:
            skipped += 1
    local.commit()
    return inserted, skipped, blocked


def main() -> int:
    if shutil.which("gh") is None:
        print("ERROR: gh CLI not installed. Install with `brew install gh`.", file=sys.stderr)
        return 1
    if not LOCAL_DB.exists():
        print(f"ERROR: local DB not found at {LOCAL_DB}. Run `python main.py` first.", file=sys.stderr)
        return 1

    print("Finding latest successful cloud run...")
    run_id = _latest_successful_run_id()
    print(f"  Run {run_id}")

    with tempfile.TemporaryDirectory(prefix="startup-radar-sync-") as tmp:
        tmp_path = Path(tmp)
        print(f"Downloading artifact {ARTIFACT_NAME}...")
        cloud_db = _download_artifact(run_id, tmp_path)
        print(f"  {cloud_db.stat().st_size:,} bytes")

        cloud = sqlite3.connect(cloud_db)
        local = sqlite3.connect(LOCAL_DB)
        try:
            tombstones = _job_tombstones(local)
            excl_co = _excluded_company_patterns_from_config()
            for table in PIPELINE_TABLES:
                is_jobs = table == "job_matches"
                inserted, skipped, blocked = _merge_table(
                    cloud, local, table,
                    tombstones=tombstones if is_jobs else None,
                    excl_co_patterns=excl_co if is_jobs else None,
                )
                msg = f"  {table}: +{inserted} new, {skipped} already present"
                if blocked:
                    msg += f", {blocked} skipped (deleted locally / excluded company)"
                print(msg)
        finally:
            cloud.close()
            local.close()

    print("\nDone. Refresh `streamlit run app.py` to see the latest.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
