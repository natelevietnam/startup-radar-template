"""Generate the PM-Fit dashboard HTML from the current Uncategorized Job Matches.

Reads the live `job_matches` rows (status not in applied/wishlist/interested/
not-interested), joins them against the cached deep-research dossiers in
`fit_dossiers.json`, and renders `fit_artifact_template.html` to
`reports/pm_fit_dashboard.html`.

- Companies that already have a dossier render as full scored cards.
- Companies new to the pipeline render as light "pending" cards (JD facts +
  cheap gate flags) so the artifact stays in sync without re-running research.
- Companies whose dossier predates one of their current openings render as
  "stale": already researched, but the company came back with a role we hadn't
  seen, which is a decent proxy for "something changed there".

Research is never redone just because the dashboard was opened. Each dossier
records `researchedAt` and the `researchedRoles` known at that moment; a
company only goes stale when a role appears that isn't on that list.

This is pure Python (no LLM) and safe to run on every dashboard refresh. The
caller (the startup-radar skill) re-publishes the output to the Artifact URL,
and is the piece that acts on `--stale` by re-running deep research.

Usage:
    python generate_fit_artifact.py                 # write the HTML
    python generate_fit_artifact.py --missing       # Uncategorized companies lacking a dossier
    python generate_fit_artifact.py --stale         # researched companies with new openings
    python generate_fit_artifact.py --stale --auto  # ...only those scoring >= AUTO_REFRESH_SCORE
    python generate_fit_artifact.py --mark-researched "Ramp" "Google"
    python generate_fit_artifact.py --artifact-url    # the publish URL, for scripts/skills
"""
from __future__ import annotations

import json
import re
import sqlite3
import sys
from datetime import date, datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DB = ROOT / "startup_radar.db"
DOSSIERS = ROOT / "fit_dossiers.json"
TEMPLATE = ROOT / "fit_artifact_template.html"
OUT = ROOT / "reports" / "pm_fit_dashboard.html"

# Fallback only. The artifact URL is account-specific — republishing from a
# different Claude account mints a new one — so it lives in config.yaml
# (`output.fit_artifact_url`) and is read through artifact_url() below.
# Everything that needs it (app.py, the startup-radar skill) must call that
# function rather than hardcode a copy; a previous migration updated two of
# the three hardcoded copies and left the skill publishing to a dead URL.
_FALLBACK_ARTIFACT_URL = "https://claude.ai/code/artifact/288b4ad5-27e3-466b-a005-7f2b5b10e635"  # artifact-url-ok

COMP_FLOOR = 150_000  # base-salary deal-breaker
CATEGORIZED = {"applied", "wishlist", "interested", "not interested"}

# A stale dossier at or above this score is worth re-researching automatically;
# below it, the company is flagged and refreshed on demand. Mirrors
# config.yaml deepdive.thresholds.strong (7.5) on the dossiers' 0-100 scale.
AUTO_REFRESH_SCORE = 75


def _norm(name: str) -> str:
    """Normalize a company name for matching (lowercase, drop suffixes/punct)."""
    n = (name or "").strip().lower()
    n = re.sub(r"\b(inc|llc|corp|co|ltd|technologies|labs)\b\.?", "", n)
    n = re.sub(r"[^a-z0-9]+", " ", n).strip()
    return n


def _load_config() -> dict:
    try:
        import yaml  # PyYAML ships with the project
        return yaml.safe_load((ROOT / "config.yaml").read_text()) or {}
    except Exception:
        return {}


def _load_config_excludes() -> set[str]:
    ex = (_load_config().get("targets", {}) or {}).get("excluded_companies", []) or []
    return {_norm(x) for x in ex}


def artifact_url() -> str:
    """The claude.ai Artifact URL this dashboard republishes to.

    Single source of truth: `output.fit_artifact_url` in config.yaml. Import
    this rather than copying the literal — see _FALLBACK_ARTIFACT_URL above.
    """
    url = (_load_config().get("output", {}) or {}).get("fit_artifact_url") or ""
    return url.strip() or _FALLBACK_ARTIFACT_URL


def _tombstoned(conn: sqlite3.Connection) -> set[tuple[str, str]]:
    try:
        cols = {r[1] for r in conn.execute("pragma table_info(deleted_jobs)")}
        if not {"company_name", "role_title"} <= cols:
            return set()
        return {
            (_norm(c), (r or "").strip().lower())
            for c, r in conn.execute("select company_name, role_title from deleted_jobs")
        }
    except sqlite3.OperationalError:
        return set()


def _comp_range(text: str) -> tuple[int | None, int | None]:
    """Pull a $ salary range out of a free-text blob; returns (min,max) or (None,None)."""
    nums = [int(n.replace(",", "")) for n in re.findall(r"\$\s*([\d][\d,]{3,})", text or "")]
    nums = [n for n in nums if n >= 1000]  # ignore stray small numbers
    if not nums:
        return None, None
    return min(nums), max(nums)


def _norm_role(title: str) -> str:
    """Normalize a role title for set membership (case/whitespace-insensitive)."""
    return re.sub(r"\s+", " ", (title or "").strip().lower())


def _new_since_research(dossier: dict, open_roles: list[str]) -> list[str]:
    """Roles open now that weren't known when this company was researched.

    An *absent* `researchedRoles` means the baseline is unknown (hand-added
    dossier, or one written before this field existed) — treated as current,
    since flagging every such company would make the signal useless. Run
    `--mark-researched` to give it a baseline.

    An *empty* `researchedRoles` is different: it records that the company had
    no roles on the board when it was researched, so anything open now is new.
    """
    if "researchedRoles" not in dossier:
        return []
    known = {_norm_role(t) for t in dossier["researchedRoles"] or []}
    return [r for r in open_roles if _norm_role(r) not in known]


def _loc_short(location: str) -> str:
    loc = (location or "").strip()
    return (loc[:38] + "…") if len(loc) > 40 else (loc or "—")


def _pending_flags(location: str, comp_max: int | None) -> tuple[list[str], str]:
    """Cheap, deterministic gate flags for a not-yet-researched company."""
    flags: list[str] = []
    loc = (location or "").lower()
    if comp_max is not None and comp_max < COMP_FLOOR:
        flags.append(f"Base may be &lt; $150K (posted max ${comp_max:,})")
    is_ny = "new york" in loc or re.search(r"\bny\b|nyc", loc)
    has_alt = any(k in loc for k in ("san francisco", "sf", "bay area", "remote", "ca", "seattle", "mountain view", "palo alto"))
    if is_ny and not has_alt:
        flags.append("NY-only location — fails no-relocation gate")
    if "est" in loc and not any(k in loc for k in ("san francisco", "sf", "remote - usa", "remote, us")):
        flags.append("EST-timezone — check SF compatibility")
    clears = "N" if (comp_max is not None and comp_max < COMP_FLOOR) else "?"
    return flags, clears


def build_data() -> tuple[list[dict], dict]:
    dossiers = {(_norm(d["co"])): d for d in json.loads(DOSSIERS.read_text())}
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    excludes = _load_config_excludes()
    tombs = _tombstoned(conn)

    rows = conn.execute(
        "select company_name, role_title, location, url, company_description, status "
        "from job_matches"
    ).fetchall()

    # Group current Uncategorized rows by company.
    by_co: dict[str, dict] = {}
    for r in rows:
        if (r["status"] or "").strip().lower() in CATEGORIZED:
            continue
        key = _norm(r["company_name"])
        if key in excludes:
            continue
        if (key, (r["role_title"] or "").strip().lower()) in tombs:
            continue
        slot = by_co.setdefault(key, {"name": r["company_name"], "roles": [], "locs": [],
                                      "urls": [], "descs": []})
        if r["role_title"] and r["role_title"] not in slot["roles"]:
            slot["roles"].append(r["role_title"])
        if r["location"]:
            slot["locs"].append(r["location"])
        if r["url"]:
            slot["urls"].append(r["url"])
        if r["company_description"]:
            slot["descs"].append(r["company_description"])

    data: list[dict] = []
    pending_cos: list[str] = []
    flagged_cos: list[str] = []
    stale: list[dict] = []

    for key, slot in by_co.items():
        open_roles = slot["roles"]
        if key in dossiers:
            d = dict(dossiers[key])            # cached deep dossier
            d["openRoles"] = open_roles        # live roles from the pipeline
            new_roles = _new_since_research(d, open_roles)
            if new_roles:
                score = d.get("score")
                d["stale"] = True
                d["newRoles"] = new_roles
                stale.append({
                    "co": slot["name"],
                    "score": score,
                    "researchedAt": d.get("researchedAt", ""),
                    "newRoles": new_roles,
                    "auto": isinstance(score, (int, float)) and score >= AUTO_REFRESH_SCORE,
                })
            data.append(d)
            _, cmax = _comp_range(" ".join(slot["descs"]))
            if cmax is not None and cmax < COMP_FLOOR:
                flagged_cos.append(slot["name"])
        else:
            desc = " ".join(slot["descs"])
            cmin, cmax = _comp_range(desc)
            loc = slot["locs"][0] if slot["locs"] else ""
            flags, clears = _pending_flags(loc, cmax)
            comp_txt = (f"${cmin:,}–${cmax:,}" if cmin and cmax and cmin != cmax
                        else (f"${cmax:,}" if cmax else "comp not posted"))
            snap = [f"<b>{len(open_roles)}</b> open role(s)", f"Loc: {_loc_short(loc)}",
                    f"Comp: {comp_txt}", "Not yet deep-researched"]
            data.append({
                "co": slot["name"], "role": open_roles[0] if open_roles else "—",
                "openRoles": open_roles, "pending": True, "score": None,
                "locShort": _loc_short(loc), "clears": clears,
                "headline": "New in your pipeline — not yet deep-researched.",
                "snap": snap, "flags": flags, "url": slot["urls"][0] if slot["urls"] else "",
                "search0": desc,
            })
            pending_cos.append(slot["name"])

    conn.close()

    researched = len(data) - len(pending_cos)
    total_roles = sum(len(v["roles"]) for v in by_co.values())
    counts = (f"<b>{len(data)}</b> companies · <b>{total_roles}</b> open roles · "
              f"<b>{researched}</b> deep-researched · <b>{len(pending_cos)}</b> new/pending")
    if stale:
        counts += f" · <b>{len(stale)}</b> re-posted since research"

    note_bits = []
    if pending_cos:
        note_bits.append("<b>New since the deep-research run</b> (shown as pending — open the posting, "
                         "or deep-dive on demand): " + ", ".join(sorted(pending_cos)) + ".")
    if stale:
        auto = sorted(s["co"] for s in stale if s["auto"])
        manual = sorted(s["co"] for s in stale if not s["auto"])
        bit = ("<b>Researched companies that re-posted</b> — their dossier predates a role now open, "
               "so the read may have moved: ")
        if auto:
            bit += f"auto-refreshing {', '.join(auto)}"
            bit += ("; " if manual else ".")
        if manual:
            bit += f"flagged for on-demand refresh: {', '.join(manual)}."
        note_bits.append(bit)
    if flagged_cos:
        note_bits.append("<b>Comp gate:</b> posted base may fall under ~$150K for " +
                         ", ".join(sorted(set(flagged_cos))) + ".")
    note_bits.append("This view is generated from your <b>Uncategorized Job Matches</b>; companies you "
                     "categorize (interested/applied/etc.) drop off automatically.")
    gate_note = " ".join(note_bits)

    meta = {"counts": counts, "gate_note": gate_note, "stale": stale}
    return data, meta


def mark_researched(names: list[str], when: str = "") -> int:
    """Stamp `researchedAt` + `researchedRoles` on the named dossiers.

    Call this after re-running deep research so the company stops reporting as
    stale. The baseline is every role title currently on the board for that
    company — including categorized ones, so re-opening a role you'd already
    triaged doesn't read as new.
    """
    stamp = when or date.today().isoformat()
    entries = json.loads(DOSSIERS.read_text())
    wanted = {_norm(n) for n in names}

    conn = sqlite3.connect(DB)
    roles_by_co: dict[str, set[str]] = {}
    for co, role in conn.execute("select company_name, role_title from job_matches"):
        roles_by_co.setdefault(_norm(co), set()).add((role or "").strip())
    conn.close()

    updated = 0
    for e in entries:
        key = _norm(e.get("co", ""))
        if key not in wanted:
            continue
        e["researchedAt"] = stamp
        e["researchedRoles"] = sorted(r for r in roles_by_co.get(key, set()) if r)
        updated += 1

    if updated:
        DOSSIERS.write_text(json.dumps(entries, ensure_ascii=False, indent=1))
    missed = wanted - {_norm(e.get("co", "")) for e in entries}
    for m in sorted(missed):
        print(f"  ! no dossier for {m!r} — nothing to stamp")
    return updated


def render(data: list[dict], meta: dict) -> str:
    tmpl = TEMPLATE.read_text()
    stamp = datetime.now(timezone.utc).astimezone().strftime("%d %b %Y, %H:%M %Z")
    html = tmpl.replace("/*DATA*/", json.dumps(data, ensure_ascii=False)[1:-1])
    html = html.replace("{{GENERATED_AT}}", stamp)
    html = html.replace("{{COUNTS}}", meta["counts"])
    html = html.replace("{{GATE_NOTE}}", meta["gate_note"])
    return html


def main(argv: list[str]) -> int:
    if "--artifact-url" in argv:
        print(artifact_url())
        return 0

    if "--mark-researched" in argv:
        names = argv[argv.index("--mark-researched") + 1:]
        if not names:
            print("usage: --mark-researched \"Company A\" [\"Company B\" ...]")
            return 2
        n = mark_researched(names)
        print(f"Stamped {n} dossier(s) as researched today.")
        return 0

    data, meta = build_data()

    if "--missing" in argv:
        missing = [d["co"] for d in data if d.get("pending")]
        print("\n".join(missing) if missing else "(no un-researched companies in Uncategorized)")
        return 0

    if "--stale" in argv:
        rows = meta["stale"]
        if "--auto" in argv:
            rows = [s for s in rows if s["auto"]]
        if not rows:
            print("(no researched companies have re-posted since their dossier)")
            return 0
        for s in sorted(rows, key=lambda x: -(x["score"] or 0)):
            tag = "AUTO" if s["auto"] else "flag"
            score = s["score"] if s["score"] is not None else "—"
            print(f"{s['co']}\t{score}\t{tag}\tresearched {s['researchedAt'] or '?'}\t"
                  f"new: {'; '.join(s['newRoles'])}")
        return 0

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(render(data, meta))
    n_pending = sum(1 for d in data if d.get("pending"))
    n_stale = len(meta["stale"])
    n_auto = sum(1 for s in meta["stale"] if s["auto"])
    print(f"Wrote {OUT.relative_to(ROOT)} — {len(data)} companies "
          f"({len(data) - n_pending} researched, {n_pending} pending, "
          f"{n_stale} re-posted since research / {n_auto} above the auto-refresh score).")
    print(f"Publish to: {artifact_url()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
