"""Score a posting against the forward-deployed hypothesis in `job_matching`.

A SECOND OPINION, NEVER A GATE. `targets.*` decides what reaches the board;
this only ranks what is already there, and writes job_matches.lens_tier /
lens_score / lens_reasons. Nothing here can cut a row, which is why the spec's
"hard filters zero the score and set status = dismissed" clause is deliberately
not implemented: the pipeline already dismisses on years (enrich_experience),
on explicit sponsorship refusal (enrich_sponsorship) and on title/company/
location (filters.JobFilter). A second gate that disagreed with those would be
worse than no gate at all.

The reasons are the product. A number alone is not auditable, and this lens is
a hypothesis under test rather than a settled rule — so every component that
moves the score says so in plain words, including the ones that score zero.

Pure functions, no network, no database. Config is injected so the caller
controls which config is in play, which is what makes the tests cheap.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Optional

# Location tokens in targets.locations that mean "remote", not "a place". Used
# to split that one list into the two buckets this scorer pays differently,
# rather than duplicating the location list under job_matching where it would
# drift from the one the ingest filter uses.
_REMOTE_TOKENS = ("remote",)


@dataclass
class LensScore:
    """What the lens concluded, and why."""

    tier: Optional[int] = None
    score: int = 0
    reasons: list[str] = field(default_factory=list)

    def as_row(self) -> dict:
        """The three job_matches columns this maps onto."""
        import json

        return {
            "lens_tier": self.tier,
            "lens_score": self.score,
            "lens_reasons": json.dumps(self.reasons, ensure_ascii=False),
        }


def _norm(text: str) -> str:
    """Lowercase, collapse punctuation to single spaces.

    Titles arrive as "Forward-Deployed PM", "Forward Deployed PM" and
    "Forward deployed product manager / Solutions". Matching has to see those
    as the same shape, so punctuation becomes whitespace rather than being
    stripped — stripping would weld "AI/ML" into "aiml" and stop "ai" matching.
    """
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", (text or "").lower())).strip()


def _phrase_in(needle: str, haystack: str) -> bool:
    """Whole-phrase containment on normalised text.

    Word-boundary anchored so the single-word signals cannot fire inside a
    longer word: "agent" must not match "agentic" is NOT the rule we want —
    agentic is a genuine hit — but "llm" must not match "llms" is also wrong,
    and "evals" must not match "revals" is right. The boundary check gives the
    last of those without hand-maintaining a stem list, and the first two are
    handled by listing both forms in config where it matters.
    """
    n = _norm(needle)
    if not n:
        return False
    return re.search(rf"(?<![a-z0-9]){re.escape(n)}(?![a-z0-9])", haystack) is not None


def _tier_of(title: str, cfg: dict) -> Optional[int]:
    """Which tier this title belongs to, or None.

    Tier 1 wins over tier 3 when both somehow match, because tier 1 is the
    hypothesis and tier 3 is a consolation. Tier 2 is not implemented — see
    the config comment for why.
    """
    t = _norm(title)
    tiers = (cfg.get("title_tiers") or {})
    for name, num in (("tier_1", 1), ("tier_3", 3)):
        for phrase in tiers.get(name) or []:
            if _phrase_in(phrase, t):
                return num
    return None


def company_allows_tier_3(company: Optional[dict], cfg: dict) -> bool:
    """Is this company a strong enough fit for a tier-3 role to be worth it?

    "Extremely strong company/culture fit" in the only terms the repo can
    evaluate: the cached dossier's score and gate status. A company with no
    dossier returns False on purpose — strength cannot be asserted about a
    company nobody has researched.
    """
    req = cfg.get("tier_3_requires") or {}
    if not company:
        return False
    score = company.get("score")
    if not isinstance(score, (int, float)):
        return False
    if score < req.get("min_company_score", 75):
        return False
    want = (req.get("gate_status") or "").strip().lower()
    got = ((company.get("gates") or {}).get("status") or "").strip().lower()
    return got == want if want else True


def _location_points(location: str, cfg: dict, target_locations) -> tuple[int, str]:
    loc = _norm(location)
    pts = cfg.get("location_points") or {}
    if not loc:
        return 0, ""
    places = [p for p in (target_locations or [])
              if not any(tok in _norm(p) for tok in _REMOTE_TOKENS)]
    for p in places:
        if _phrase_in(p, loc):
            return int(pts.get("bay_area", 0)), f"Bay Area location ({location})"
    if any(tok in loc for tok in _REMOTE_TOKENS):
        return int(pts.get("remote_us", 0)), "Remote (US-eligible)"
    return 0, ""


def _comp_points(posting: dict, cfg: dict) -> tuple[int, str]:
    comp = cfg.get("compensation") or {}
    floor = int(comp.get("floor_usd", 0))
    target = int(comp.get("target_usd", floor))
    known = bool(posting.get("comp_known")) and posting.get("comp_min") is not None
    if not known:
        return int(comp.get("points_if_unknown", 0)), "No band published (not penalised)"
    low = int(posting["comp_min"])
    if low >= target:
        return int(comp.get("points_at_target", 0)), f"Band opens at or above ${target:,} (${low:,})"
    if low >= floor:
        return int(comp.get("points_above_floor", 0)), f"Band clears the ${floor:,} floor (${low:,})"
    return (int(comp.get("points_below_floor", 0)),
            f"Band opens BELOW the ${floor:,} floor (${low:,}) — negotiating against the ceiling")


def _signal_points(description: str, cfg: dict) -> tuple[int, list[str]]:
    sig = cfg.get("description_signals") or {}
    body = _norm(description)
    if not body:
        return 0, []
    reasons: list[str] = []
    total = 0
    for key, per_key, cap_key, label in (
        ("strong", "strong_points", "strong_cap", "strong"),
        ("weak", "weak_points", "weak_cap", "weak"),
    ):
        hits = [p for p in (sig.get(key) or []) if _phrase_in(p, body)]
        if not hits:
            continue
        per = int(sig.get(per_key, 0))
        capped = min(len(hits) * per, int(sig.get(cap_key, 10 ** 6)))
        total += capped
        shown = ", ".join(sorted(hits)[:4]) + ("…" if len(hits) > 4 else "")
        reasons.append(f"{len(hits)} {label} signal{'s' if len(hits) != 1 else ''} ({shown})")
    return total, reasons


def _recency_points(posted_at, cfg: dict, today: Optional[date] = None) -> tuple[int, str]:
    rec = cfg.get("recency") or {}
    if not posted_at:
        return 0, ""
    if isinstance(posted_at, datetime):
        d = posted_at.date()
    elif isinstance(posted_at, date):
        d = posted_at
    else:
        try:
            d = date.fromisoformat(str(posted_at)[:10])
        except ValueError:
            return 0, ""
    days = ((today or date.today()) - d).days
    within = int(rec.get("within_days", 14))
    if 0 <= days <= within:
        return int(rec.get("points", 0)), f"Posted {days} day{'s' if days != 1 else ''} ago"
    return 0, ""


def score_posting(posting: dict, cfg: dict, company: Optional[dict] = None,
                  target_locations=None, today: Optional[date] = None) -> LensScore:
    """Score one posting. `cfg` is the `job_matching` block, not the whole config.

    `posting` keys used: role_title, location, description_text, comp_min,
    comp_known, sponsorship, posted_at. `company` is that employer's cached
    dossier, needed only to decide whether a tier-3 title counts.
    `target_locations` is targets.locations, reused rather than duplicated.
    """
    reasons: list[str] = []
    total = 0

    tier = _tier_of(posting.get("role_title", ""), cfg)
    if tier == 3 and not company_allows_tier_3(company, cfg):
        # Keep the tier for the record, but pay nothing: a foot-in-the-door role
        # is only worth a look when the company itself clears a high bar.
        reasons.append("Tier 3 title, but company is not a strong enough fit to count it")
    elif tier is not None:
        pts = int((cfg.get("tier_points") or {}).get(f"tier_{tier}", 0))
        total += pts
        reasons.append(f"Tier {tier} title match (+{pts})")
    else:
        # Not a taxonomy title. Still scored, because description signals are
        # how a mislabelled posting gets caught — that is their whole purpose.
        reasons.append("No taxonomy title match — scored on signals alone")

    sig_pts, sig_reasons = _signal_points(posting.get("description_text", ""), cfg)
    total += sig_pts
    reasons.extend(sig_reasons)
    if not posting.get("description_text"):
        reasons.append("No posting body stored — signals could not be read")

    loc_pts, loc_reason = _location_points(posting.get("location", ""), cfg, target_locations)
    total += loc_pts
    if loc_reason:
        reasons.append(loc_reason)

    comp_pts, comp_reason = _comp_points(posting, cfg)
    total += comp_pts
    if comp_reason:
        reasons.append(comp_reason)

    if (posting.get("sponsorship") or "").strip().lower() == "offered":
        pts = int(cfg.get("visa_points_positive", 0))
        total += pts
        reasons.append(f"Sponsorship stated as offered (+{pts})")

    rec_pts, rec_reason = _recency_points(posting.get("posted_at"), cfg, today)
    total += rec_pts
    if rec_reason:
        reasons.append(rec_reason)

    cap = int(cfg.get("score_cap", 100))
    if total > cap:
        reasons.append(f"Components summed to {total}; clamped to {cap}")
        total = cap

    return LensScore(tier=tier, score=max(0, total), reasons=reasons)
