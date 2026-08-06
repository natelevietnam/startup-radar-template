"""Ali Rohde Jobs (alirohdejobs@substack.com) — company watchlist source.

A weekly Substack with exactly three sections: Chief of Staff, BizOps and VC
roles. It carries **no** product roles — across 12 editions, 0 of 520 job
lines passed ``newpmjobs.is_product_role`` and none contained "product
manager" — so the roles themselves are useless for ``job_matches``.

The *companies* are worth having, though. Every line tags industry and
funding stage, which makes this a decent early-stage company feed:

    Chief of Staff [ <substack redirect> ], Vapi (AI, Series B), SF / Hybrid
    Chief of Staff, GTM [ <url> ], Slingshot (Healthcare x AI, Series A), NYC
    AI Chief of Staff [ <url> ], Product.ai (Ecommerce), Santa Monica, CA

So this source harvests companies into the ``startups`` watchlist and discards
the role titles entirely. Reuses the Gmail OAuth in ``sources.gmail``.

Note the stage segment is optional — "Product.ai (Ecommerce)" has an industry
but no stage — and the location can itself contain commas ("Santa Monica,
CA"), so location is taken as everything after the parenthetical.

Lines with **no** parenthetical at all are skipped by design, not by accident:
those are the VC-section entries ("First Round Capital, SF / NYC"), which name
investment firms rather than startups and carry no industry or stage. Over 14
editions that is 111 of 598 lines. If you ever want the firms too, relax
``_REST_RE`` — but they do not belong in a startup watchlist.
"""

from __future__ import annotations

import re

from models import Startup
from sources.gmail import _get_service, _extract_body

SENDER = "alirohdejobs@substack.com"

# "<Title> [ <url> ], <Company> (<Industry>[, <Stage>]), <Location>"
_LINE_RE = re.compile(
    r"^(?P<title>.+?)\s*\[\s*(?P<url>https?://\S+?)\s*\]\s*,\s*(?P<rest>.+)$"
)
_REST_RE = re.compile(
    r"^(?P<company>.+?)\s*\((?P<paren>[^)]*)\)\s*,\s*(?P<location>.+?)\s*$"
)

# Stage tokens Ali uses. Anything else in the parenthetical is an industry.
_STAGE_RE = re.compile(
    r"^(pre-?seed|seed|series\s+[a-k]|public|acquired|bootstrapped|growth|late\s+stage)$",
    re.IGNORECASE,
)


def _split_paren(paren: str) -> tuple[str, str]:
    """Split "AI, Series B" into ("AI", "Series B"). Stage is optional."""
    parts = [p.strip() for p in paren.split(",") if p.strip()]
    if not parts:
        return "", ""
    if len(parts) >= 2 and _STAGE_RE.match(parts[-1]):
        return ", ".join(parts[:-1]), parts[-1]
    if len(parts) == 1 and _STAGE_RE.match(parts[0]):
        return "", parts[0]
    return ", ".join(parts), ""


def fetch(cfg: dict) -> list[Startup]:
    import database

    service = _get_service()
    lookback = int(cfg.get("lookback_days", 120))
    industries = [i.lower() for i in (cfg.get("industries") or [])]

    resp = service.users().messages().list(
        userId="me", q=f"from:{SENDER} newer_than:{lookback}d", maxResults=25,
    ).execute()
    messages = resp.get("messages", [])

    out: list[Startup] = []
    new_ids: list[str] = []
    seen: set[str] = set()

    for meta in messages:
        msg_id = meta["id"]
        if database.is_processed("alirohde", msg_id):
            continue

        msg = service.users().messages().get(
            userId="me", id=msg_id, format="full",
        ).execute()
        headers = {h["name"]: h["value"] for h in msg.get("payload", {}).get("headers", [])}
        subject = headers.get("Subject", "")
        body = _extract_body(msg.get("payload", {}))

        for raw in body.split("\n"):
            m = _LINE_RE.match(raw.strip())
            if not m:
                continue
            rest = _REST_RE.match(m.group("rest"))
            if not rest:
                continue

            company = rest.group("company").strip().rstrip(",").strip()
            if not company or len(company) > 60:
                continue
            key = company.lower()
            if key in seen:
                continue

            industry, stage = _split_paren(rest.group("paren"))
            # Optional industry gate — the newsletter is broad (media, auto,
            # insurtech), so restrict to the industries actually of interest.
            if industries and industry:
                if not any(w in industry.lower() for w in industries):
                    continue

            seen.add(key)
            out.append(Startup(
                company_name=company,
                description=industry,
                funding_stage=stage,
                amount_raised="",
                location=rest.group("location").strip(),
                website="",
                source="Ali Rohde Jobs",
                source_url=f"https://mail.google.com/mail/u/0/#inbox/{msg_id}",
            ))

        new_ids.append(msg_id)

    database.mark_processed("alirohde", new_ids)
    return out
