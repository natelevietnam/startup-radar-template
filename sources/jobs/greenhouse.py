"""Greenhouse public job-board API.

    https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true

Token is the slug in job-boards.greenhouse.io/{token}.

Two quirks drive the parsing here. `content` arrives HTML-escaped, so it has to
be unescaped before tags are stripped. And Greenhouse publishes no structured
pay field on this endpoint — Vercel's 90 postings carry none — so compensation
has to be read out of the body text or left unknown, which is the honest
default.
"""

from __future__ import annotations

from typing import Optional

from . import _shape
from ._client import get_json

PROVIDER = "greenhouse"
SOURCE = "Greenhouse board"
_URL = "https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true"


def _location(job: dict) -> str:
    loc = ((job.get("location") or {}) or {}).get("name") or ""
    if loc.strip():
        return loc.strip()
    # Some boards leave location null and describe it only via offices.
    offices = [o.get("location") or o.get("name") or "" for o in job.get("offices") or []]
    return next((o for o in offices if o.strip()), "")


def fetch_board(token: str, company_name: str = "", *, use_cache: bool = True) -> list[dict]:
    payload = get_json(_URL.format(token=token), use_cache=use_cache)
    jobs = (payload or {}).get("jobs") if isinstance(payload, dict) else None
    out: list[dict] = []
    for job in jobs or []:
        body = _shape.strip_html(job.get("content") or "")
        lo, hi = _shape.parse_comp_from_text(body)
        out.append(_shape.row(
            # Greenhouse is the only one of the three that returns the employer
            # name, so prefer it over the token when the caller passes nothing.
            company_name=company_name or job.get("company_name") or token,
            role_title=job.get("title") or "",
            location=_location(job),
            url=job.get("absolute_url") or "",
            provider=PROVIDER,
            # `id` is the number in absolute_url, which is what requisition_id()
            # in database.py already extracts from a stored Greenhouse URL — so
            # the two identities agree. internal_job_id and requisition_id are
            # different numbers and would not.
            provider_job_id=job.get("id") or "",
            source=SOURCE,
            posted_at=_shape.iso_date(job.get("first_published") or job.get("updated_at")),
            description_text=body,
            comp_min=lo, comp_max=hi,
        ))
    return out
