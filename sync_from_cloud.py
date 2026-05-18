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

REPO_DIR = Path(__file__).parent
LOCAL_DB = REPO_DIR / "startup_radar.db"
WORKFLOW = "daily.yml"
ARTIFACT_NAME = "startup-radar-db"

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


def _merge_table(cloud: sqlite3.Connection, local: sqlite3.Connection, table: str) -> tuple[int, int]:
    """Copy rows from cloud → local. Returns (inserted, skipped_duplicates)."""
    cols = _column_names(cloud, table)
    if not cols:
        return (0, 0)
    insertable = [c for c in cols if c != "id"]
    placeholders = ",".join("?" * len(insertable))
    col_list = ",".join(insertable)

    rows = cloud.execute(
        f"SELECT {col_list} FROM {table}"
    ).fetchall()

    inserted = skipped = 0
    for row in rows:
        try:
            local.execute(
                f"INSERT INTO {table} ({col_list}) VALUES ({placeholders})",
                row,
            )
            inserted += 1
        except sqlite3.IntegrityError:
            skipped += 1
    local.commit()
    return inserted, skipped


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
            for table in PIPELINE_TABLES:
                inserted, skipped = _merge_table(cloud, local, table)
                print(f"  {table}: +{inserted} new, {skipped} already present")
        finally:
            cloud.close()
            local.close()

    print("\nDone. Refresh `streamlit run app.py` to see the latest.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
