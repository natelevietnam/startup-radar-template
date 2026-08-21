"""startups.gallery newsletter (hi@startups.gallery) — company watchlist source.

"What's trending on startups gallery #NN", roughly weekly. Despite being
subscribed from a job-hunting angle, it carries **no job postings**: across
issues #81 and #82 the string "product manager" appears zero times. What it
does carry is 12-15 freshly funded companies per issue, which makes it a good
early-stage company feed — so this lands in the ``startups`` watchlist, not
``job_matches``.

(The site's own /jobs board is a separate thing and isn't scrapable — it's a
Framer SSG page whose listings are client-rendered from a CMS collection that
returns 403 to non-browser clients.)

Two sections, both anchored on the same pattern — ``<Company>
[https://startups.gallery/companies/<slug>]``:

1. Funding roundup bullets::

       * Rillet [.../companies/rillet] raised a $100M Series C at $1B to
         replace NetSuite and SAP with an ERP where AI agents keep the books.

2. Company spotlights, where the anchor is a heading followed by bullets::

       Tandem Health [.../companies/tandem-health]
        * What they do: AI medical scribe.
        * Funding: $50M Series A on June 30, 2025
        * What's cool: Stockholm-based, ...
        * 🔥 Hiring now: 48 open roles [ashby url] in Stockholm, Paris, Remote

Anchoring on the company link handles both, so there's no need to detect which
section we're in. Amounts occasionally arrive in other currencies ("raised
A$11.5M, ~$8M") — we take the first USD figure and skip the rest rather than
guessing at conversion.
"""

from __future__ import annotations

import re

from models import Startup
from sources.gmail import _get_service, _extract_body

SENDER = "@startups.gallery"

# "<Company> [https://startups.gallery/companies/<slug>]" — the newsletter
# sometimes omits the space before the following word, hence no trailing \s.
_CO_RE = re.compile(
    r"(?P<name>[A-Z][\w&.,'’\-\+ ]{0,48}?)\s*\[\s*"
    r"https://startups\.gallery/companies/(?P<slug>[a-z0-9\-]+)\s*\]"
)
_STAGE_RE = re.compile(
    r"\b(pre-?seed|seed|series\s+[a-k]|growth round|debt|credit facility)\b",
    re.IGNORECASE,
)
# First plain-USD figure. "A$11.5M" is skipped by requiring a non-letter before $.
_AMOUNT_RE = re.compile(r"(?<![A-Za-z])\$(?P<num>\d[\d.,]*)\s*(?P<mag>[MB]|million|billion)\b")
_WHATTHEYDO_RE = re.compile(r"What they do:\s*(?P<t>[^\n*]{3,200})")
_HIRING_RE = re.compile(
    r"Hiring now:\s*(?P<n>\d+)\s*open roles?\s*\[\s*(?P<url>https?://\S+?)\s*\]"
    r"\s*(?:at\s+)?(?:in\s+)?(?P<locs>[^\n(]{0,80})"
)


def _clean(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").replace("͏", "").replace("​", "")).strip()


def fetch(cfg: dict) -> list[Startup]:
    import database

    service = _get_service()
    lookback = int(cfg.get("lookback_days", 120))

    resp = service.users().messages().list(
        userId="me", q=f"from:{SENDER} newer_than:{lookback}d", maxResults=25,
    ).execute()
    messages = resp.get("messages", [])

    out: list[Startup] = []
    new_ids: list[str] = []
    seen: set[str] = set()

    for meta in messages:
        msg_id = meta["id"]
        if database.is_processed("startups_gallery", msg_id):
            continue

        msg = service.users().messages().get(
            userId="me", id=msg_id, format="full",
        ).execute()
        body = _extract_body(msg.get("payload", {})).replace("\r\n", "\n")
        new_ids.append(msg_id)

        hits = list(_CO_RE.finditer(body))
        for i, m in enumerate(hits):
            name = _clean(m.group("name")).strip(" .,-")
            slug = m.group("slug")
            # Leading prose can bleed into the name ("...and Rillet"); the
            # slug is authoritative, so fall back to it when they disagree.
            if not name or len(name) > 48:
                name = slug.replace("-", " ").title()
            if slug in seen:
                continue
            seen.add(slug)

            end = hits[i + 1].start() if i + 1 < len(hits) else len(body)
            window = _clean(body[m.end():end])[:900]

            stage_m = _STAGE_RE.search(window)
            amt_m = _AMOUNT_RE.search(window)
            stage = stage_m.group(1).title() if stage_m else ""
            amount = ""
            if amt_m:
                mag = amt_m.group("mag").lower()
                unit = "million" if mag in ("m", "million") else "billion"
                amount = f"${amt_m.group('num')}{unit}"

            what = _WHATTHEYDO_RE.search(window)
            hiring = _HIRING_RE.search(window)
            desc_bits = []
            if what:
                desc_bits.append(_clean(what.group("t")).rstrip("."))
            elif window:
                desc_bits.append(window[:180].rstrip())
            if hiring:
                desc_bits.append(f"{hiring.group('n')} open roles")

            out.append(Startup(
                company_name=name,
                description=" • ".join(desc_bits),
                funding_stage=stage,
                amount_raised=amount,
                location=_clean(hiring.group("locs")).strip(" .,") if hiring else "",
                website=hiring.group("url").split("?")[0] if hiring else "",
                source="Startups Gallery",
                source_url=f"https://startups.gallery/companies/{slug}",
            ))

    database.mark_processed("startups_gallery", new_ids)
    return out
