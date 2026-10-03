"""Lever public postings API.

    https://api.lever.co/v0/postings/{token}?mode=json

Token is the slug in jobs.lever.co/{token}. The response is a bare JSON list,
not an object with a "jobs" key.

Lever splits the posting body across three fields — descriptionPlain is only
the opening section, with the requirements in descriptionBodyPlain and the
closing matter in additionalPlain. Reading only the first loses most of the
text the description signals exist to match on, so all three are joined.
"""

from __future__ import annotations

from . import _shape
from ._client import get_json

PROVIDER = "lever"
SOURCE = "Lever board"
_URL = "https://api.lever.co/v0/postings/{token}?mode=json"


def _location(job: dict) -> str:
    cats = job.get("categories") or {}
    loc = (cats.get("location") or "").strip()
    workplace = (job.get("workplaceType") or "").strip().lower()
    if workplace == "remote" and "remote" not in loc.lower():
        return f"{loc} (Remote)" if loc else "Remote"
    return loc


def _body(job: dict) -> str:
    parts = [job.get("descriptionPlain") or "",
             job.get("descriptionBodyPlain") or "",
             job.get("additionalPlain") or ""]
    joined = "\n".join(p for p in parts if p.strip())
    return joined or _shape.strip_html(job.get("description") or "")


def fetch_board(token: str, company_name: str = "", *, use_cache: bool = True) -> list[dict]:
    payload = get_json(_URL.format(token=token), use_cache=use_cache)
    if not isinstance(payload, list):
        return []
    out: list[dict] = []
    for job in payload:
        body = _body(job)
        lo, hi = _shape.parse_comp_from_text(
            (job.get("salaryRange") or {}).get("text", "") if isinstance(job.get("salaryRange"), dict) else "")
        if lo is None:
            lo, hi = _shape.parse_comp_from_text(body)
        out.append(_shape.row(
            company_name=company_name or token,
            role_title=job.get("text") or "",
            location=_location(job),
            url=job.get("hostedUrl") or job.get("applyUrl") or "",
            provider=PROVIDER,
            provider_job_id=job.get("id") or "",
            source=SOURCE,
            posted_at=_shape.iso_date(job.get("createdAt")),
            description_text=body,
            comp_min=lo, comp_max=hi,
        ))
    return out
