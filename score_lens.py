"""Write job_matches.lens_tier / lens_score / lens_reasons.

A second opinion, never a gate: this only ranks rows that are already on the
board. It does not insert, delete, or change any status — compare
set_priorities.py, which is the same shape of pass over the same table.

Tier 3 needs the company's cached dossier, so the dossier file is read here and
passed in; job_lens itself stays free of file access so it can be tested without
one.

Usage:
    python score_lens.py [--dry-run] [--all]

By default only rows lacking a lens_score are scored. --all re-scores
everything, which is what to run after editing the taxonomy or the weights.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from collections import Counter
from pathlib import Path

import database
import job_lens
from config_loader import load_config

ROOT = Path(__file__).resolve().parent
DOSSIERS = ROOT / "fit_dossiers.json"


def _dossiers() -> dict:
    """Cached dossiers by canonical company name.

    Later entries win on a duplicate key, matching set_priorities._dossiers so
    the two passes cannot disagree about which dossier a company has.
    """
    try:
        return {database.canon_company(d["co"]): d
                for d in json.loads(DOSSIERS.read_text())}
    except (OSError, ValueError):
        return {}


def main(argv: list[str]) -> int:
    dry = "--dry-run" in argv
    rescore_all = "--all" in argv

    cfg = load_config()
    lens_cfg = cfg.get("job_matching") or {}
    if not lens_cfg.get("enabled", True):
        print("job_matching.enabled is false — nothing to do")
        return 0
    target_locations = (cfg.get("targets") or {}).get("locations") or []
    dossiers = _dossiers()

    conn = sqlite3.connect(database.DB_PATH)
    conn.row_factory = sqlite3.Row
    where = "" if rescore_all else " WHERE lens_score IS NULL"
    rows = conn.execute(
        "SELECT id, company_name, role_title, location, description_text, "
        "       comp_min, comp_max, comp_known, sponsorship, posted_at, "
        "       lens_score FROM job_matches" + where).fetchall()

    updates = []
    tiers: Counter = Counter()
    for r in rows:
        result = job_lens.score_posting(
            dict(r), lens_cfg,
            company=dossiers.get(database.canon_company(r["company_name"])),
            target_locations=target_locations)
        payload = result.as_row()
        tiers[result.tier] += 1
        updates.append((payload["lens_tier"], payload["lens_score"],
                        payload["lens_reasons"], r["id"]))

    scored = [u for u in updates if u[1] is not None]
    above = sum(1 for u in updates if u[1] >= int(lens_cfg.get("digest_min_score", 60)))
    print(f"{len(rows)} row(s) scored{' (dry-run)' if dry else ''} · "
          + " · ".join(f"tier {k or '-'}={v}" for k, v in sorted(
              tiers.items(), key=lambda kv: (kv[0] is None, kv[0])))
          + f" · {above} at or above the digest threshold")

    if not dry and updates:
        conn.executemany(
            "UPDATE job_matches SET lens_tier = ?, lens_score = ?, "
            "lens_reasons = ? WHERE id = ?", updates)
        conn.commit()
    conn.close()
    return len(scored)


if __name__ == "__main__":
    sys.exit(0 if main(sys.argv[1:]) >= 0 else 1)
