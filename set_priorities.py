"""Populate job_matches.priority from the cached fit dossiers.

Priority answers "should I act on this", which is not the same question the fit
score answers. A company can score 82 and still be un-actionable because its
sponsorship, comp or location gate is unresolved; another can score 71 and be
ready to apply to today. So High requires BOTH a clear gate and a strong score:

    High    gate status "ready" AND score >= 70
    Low     gate status "blocked", OR score < 55
    Medium  everything else that has a dossier
    (blank) no dossier yet — brand-new arrivals stay unranked until researched

Two rules keep this from overwriting judgement:

  * A row already marked Low stays Low. Those marks are decisions the user made
    (or confirmed) about companies they keep rejecting, and a score-derived rule
    must not silently promote them back.
  * Rows the user has already decided about (Applied, Not Interested, ...) are
    never touched — priority only orders the undecided queue.

A pending application at the company does NOT cap the priority: a second role
at Visa is judged on its own merits, the same reasoning that keeps those rows
on the board with a badge instead of hidden.

Usage:
    python set_priorities.py [--dry-run]
"""

from __future__ import annotations

import json
import sqlite3
import sys
from collections import Counter
from pathlib import Path

import database

ROOT = Path(__file__).resolve().parent
DOSSIERS = ROOT / "fit_dossiers.json"

HIGH_SCORE = 70   # with a clear gate
LOW_SCORE = 55    # below this, Low regardless of gate


def _dossiers() -> dict:
    return {database.canon_company(d["co"]): d
            for d in json.loads(DOSSIERS.read_text())}


def classify(row: sqlite3.Row, dossiers: dict) -> str:
    """The priority this row should carry. "" means leave it unranked."""
    if (row["priority"] or "").strip().lower() == "low":
        return "Low"                       # a decision already made — keep it
    d = dossiers.get(database.canon_company(row["company_name"]))
    if not d:
        return ""                          # not researched yet
    score = d.get("score")
    if not isinstance(score, (int, float)):
        return ""
    status = (d.get("gates") or {}).get("status", "")
    if status == "blocked" or score < LOW_SCORE:
        return "Low"
    if status == "ready" and score >= HIGH_SCORE:
        return "High"
    return "Medium"


def main(dry_run: bool = False) -> int:
    dossiers = _dossiers()
    con = sqlite3.connect(database.DB_PATH if hasattr(database, "DB_PATH")
                          else str(ROOT / "startup_radar.db"))
    con.row_factory = sqlite3.Row
    rows = con.execute(
        "SELECT id, company_name, role_title, COALESCE(priority,'') AS priority "
        "FROM job_matches WHERE TRIM(COALESCE(status,'')) = ''"
    ).fetchall()

    changes = [(r["id"], r["priority"], want)
               for r in rows for want in [classify(r, dossiers)]
               if want != (r["priority"] or "")]
    tally = Counter(classify(r, dossiers) for r in rows)

    print(f"{len(rows)} undecided row(s) · "
          + " · ".join(f"{k or '(blank)'}={tally[k]}" for k in ("High", "Medium", "Low", ""))
          + f" · {len(changes)} change(s)" + (" (dry-run)" if dry_run else ""))

    if not dry_run and changes:
        con.executemany("UPDATE job_matches SET priority = ? WHERE id = ?",
                        [(new, rid) for rid, _old, new in changes])
        con.commit()
    con.close()
    return len(changes)


if __name__ == "__main__":
    sys.exit(0 if main(dry_run="--dry-run" in sys.argv) >= 0 else 1)
