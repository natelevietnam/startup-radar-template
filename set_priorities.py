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
import filters
from config_loader import load_config

ROOT = Path(__file__).resolve().parent
DOSSIERS = ROOT / "fit_dossiers.json"

HIGH_SCORE = 70   # with a clear gate
LOW_SCORE = 55    # below this, Low regardless of gate

# How far the High bar bends for the industries ranked highest in
# targets.industry_priority, keyed by 0-based rank. Fintech is the clearest
# signal on the resume, so a fintech row with a clear gate reaches High at 65
# where a generic row needs 70. Nothing here can push a row DOWN: an unranked
# industry (rank None, or a rank not in this table) simply gets no bonus, and
# the Low rule is untouched — this bends the top bar, never the bottom one.
INDUSTRY_BONUS = {0: 5, 1: 3}


def _dossiers() -> dict:
    return {database.canon_company(d["co"]): d
            for d in json.loads(DOSSIERS.read_text())}


def _filter() -> filters.JobFilter:
    return filters.JobFilter(load_config())


def industry_rank(row, flt) -> "int | None":
    """Where this row's industry sits in targets.industry_priority."""
    return flt.industry_rank(row["company_name"],
                             row["company_description"] or "",
                             row["role_title"] or "")


def classify(row: sqlite3.Row, dossiers: dict, rank=None) -> str:
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
    bar = HIGH_SCORE - INDUSTRY_BONUS.get(rank, 0)
    if status == "ready" and score >= bar:
        return "High"
    return "Medium"


def main(dry_run: bool = False) -> int:
    dossiers = _dossiers()
    con = sqlite3.connect(database.DB_PATH if hasattr(database, "DB_PATH")
                          else str(ROOT / "startup_radar.db"))
    con.row_factory = sqlite3.Row
    rows = con.execute(
        "SELECT id, company_name, COALESCE(company_description,'') AS company_description, "
        "role_title, COALESCE(priority,'') AS priority, industry_rank "
        "FROM job_matches WHERE TRIM(COALESCE(status,'')) = ''"
    ).fetchall()

    flt = _filter()
    ranks = {r["id"]: industry_rank(r, flt) for r in rows}
    wanted = {r["id"]: classify(r, dossiers, ranks[r["id"]]) for r in rows}

    changes = [(r["id"], r["priority"], wanted[r["id"]])
               for r in rows if wanted[r["id"]] != (r["priority"] or "")]
    rank_changes = [(ranks[r["id"]], r["id"]) for r in rows
                    if ranks[r["id"]] != r["industry_rank"]]
    tally = Counter(wanted.values())
    ranked = Counter(v for v in ranks.values() if v is not None)

    print(f"{len(rows)} undecided row(s) · "
          + " · ".join(f"{k or '(blank)'}={tally[k]}" for k in ("High", "Medium", "Low", ""))
          + f" · {len(changes)} change(s)" + (" (dry-run)" if dry_run else ""))
    print("  industry rank · "
          + " · ".join(f"{k}={ranked[k]}" for k in sorted(ranked))
          + f" · unranked={len(rows) - sum(ranked.values())}"
          + f" · {len(rank_changes)} to store")

    if not dry_run:
        if changes:
            con.executemany("UPDATE job_matches SET priority = ? WHERE id = ?",
                            [(new, rid) for rid, _old, new in changes])
        if rank_changes:
            con.executemany("UPDATE job_matches SET industry_rank = ? WHERE id = ?",
                            rank_changes)
        con.commit()
    con.close()
    return len(changes)


if __name__ == "__main__":
    sys.exit(0 if main(dry_run="--dry-run" in sys.argv) >= 0 else 1)
