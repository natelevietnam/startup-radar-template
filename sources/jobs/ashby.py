"""Ashby public job-board API.

    https://api.ashbyhq.com/posting-api/job-board/{token}?includeCompensation=true

Token is the slug in jobs.ashbyhq.com/{token} — case-sensitive, and often not
lowercase ("Jerry.ai", "AfterQuery"), which is why resolution stores it verbatim
rather than normalising it.

Ashby is the best of the three for this purpose: it publishes structured
compensation, a plain-text description, and an isListed flag that says whether
a posting is actually public.
"""

from __future__ import annotations

from typing import Optional

from . import _shape
from ._client import get_json

PROVIDER = "ashby"
SOURCE = "Ashby board"
_URL = "https://api.ashbyhq.com/posting-api/job-board/{token}?includeCompensation=true"


def _salary(comp: Optional[dict]) -> tuple[Optional[int], Optional[int]]:
    """Annual base-salary range out of Ashby's compensation tiers.

    Only components whose compensationType is Salary on a 1 YEAR interval, and
    only USD. Equity components carry their own min/max and would otherwise be
    read as pay; an hourly or monthly interval would be read as an annual
    figure. Both mistakes set comp_known=1 on a wrong number, which is worse
    than leaving it unknown.
    """
    if not isinstance(comp, dict):
        return None, None
    lows: list[int] = []
    highs: list[int] = []
    for tier in comp.get("compensationTiers") or []:
        for c in tier.get("components") or []:
            if (c.get("compensationType") or "").strip().lower() != "salary":
                continue
            if (c.get("interval") or "").strip().upper() not in ("1 YEAR", "YEAR"):
                continue
            if (c.get("currencyCode") or "USD").upper() != "USD":
                continue
            lo, hi = c.get("minValue"), c.get("maxValue")
            if isinstance(lo, (int, float)) and isinstance(hi, (int, float)):
                lows.append(int(lo))
                highs.append(int(hi))
    if not lows:
        # Fall back to the human summary Ashby also publishes, e.g. "$160K - $210K".
        return _shape.parse_comp_from_text(
            comp.get("scrapeableCompensationSalarySummary")
            or comp.get("compensationTierSummary") or "")
    return min(lows), max(highs)


def _location(job: dict) -> str:
    """Primary location, with remote stated when the board says so.

    secondaryLocations is deliberately ignored for the stored value: the
    location filter reads this string, and concatenating four cities makes a
    San Francisco posting look like a match for every one of them.
    """
    loc = (job.get("location") or "").strip()
    if job.get("isRemote") and "remote" not in loc.lower():
        return f"{loc} (Remote)" if loc else "Remote"
    return loc


def fetch_board(token: str, company_name: str = "", *, use_cache: bool = True) -> list[dict]:
    payload = get_json(_URL.format(token=token), use_cache=use_cache)
    jobs = (payload or {}).get("jobs") if isinstance(payload, dict) else None
    out: list[dict] = []
    for job in jobs or []:
        # isListed False means the posting exists but is not published. Taking
        # it would put a role on the board that nobody can apply to.
        if job.get("isListed") is False:
            continue
        lo, hi = _salary(job.get("compensation"))
        out.append(_shape.row(
            company_name=company_name or token,
            role_title=job.get("title") or "",
            location=_location(job),
            url=job.get("jobUrl") or job.get("applyUrl") or "",
            provider=PROVIDER,
            provider_job_id=job.get("id") or "",
            source=SOURCE,
            posted_at=_shape.iso_date(job.get("publishedAt")),
            description_text=job.get("descriptionPlain")
                             or _shape.strip_html(job.get("descriptionHtml") or ""),
            comp_min=lo, comp_max=hi,
        ))
    return out
