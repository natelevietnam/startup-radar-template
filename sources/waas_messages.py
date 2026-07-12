"""Work at a Startup inbound-message source → tracker leads.

WaaS (YC) doesn't send a job-digest newsletter; instead it emails you when a
*company messages you* — from ``workatastartup@ycombinator.com`` with subject
"<First> from <Company> sent you a message". Those are warm inbound leads, not
job postings, so they flow into the Application Tracker rather than
``job_matches``:

    * a ``startups`` watchlist row (company profile parsed from the email),
    * an "In Progress" ``tracker_status`` (only if none exists — never clobber),
    * a "Note" ``activities`` row capturing the message text + WaaS thread link.

Reuses the OAuth machinery in ``sources.gmail``. Idempotent via
``processed_items`` (source ``waas_messages``).
"""

from __future__ import annotations

import re

from sources.gmail import _get_service, _extract_body

_HEADER_RE = re.compile(
    r"^(?P<name>.+?)\s+from\s+(?P<company>.+?)\s+sent you the following message:",
    re.MULTILINE,
)
# Everything after the header up to the reply/CTA block is the latest message.
_MSG_END_RE = re.compile(r"\n(?:Reply to |Not interested \(|Or reply to this email)")
_PROFILE_RE = re.compile(r"(https://www\.workatastartup\.com/companies/\d+)")
_CONVO_RE = re.compile(r"(https://www\.workatastartup\.com/conversations\?t=[^\s)]+)")
_WEBSITE_RE = re.compile(r"Website:\s*(https?://\S+)")
_LOCATION_RE = re.compile(r"^\*?\s*Location:\s*(.+)$", re.MULTILINE)
_VERTICAL_RE = re.compile(r"^\*?\s*Vertical:\s*(.+)$", re.MULTILINE)


def _parse(body: str, subject: str) -> dict | None:
    if not body:
        return None
    body = body.replace("\r\n", "\n")
    hm = _HEADER_RE.search(body)
    if not hm:
        return None
    contact = hm.group("name").strip()
    company = hm.group("company").strip()
    if not company or len(company) > 60:
        return None

    # Latest message text: from just after the header to the reply CTA.
    after = body[hm.end():]
    em = _MSG_END_RE.search(after)
    message = after[: em.start()].strip() if em else after.strip()
    message = re.sub(r"\n{2,}", "\n", message).strip()

    # Contact title from the signature line "<Title>, <Company>".
    title = ""
    tm = re.search(rf"\n([^\n,]{{2,40}}),\s*{re.escape(company)}\b", body)
    if tm:
        title = tm.group(1).strip()

    # "More about <Company>" profile block.
    website = (_WEBSITE_RE.search(body) or [None, ""])[1] if _WEBSITE_RE.search(body) else ""
    location = (_LOCATION_RE.search(body).group(1).strip() if _LOCATION_RE.search(body) else "")
    vertical = (_VERTICAL_RE.search(body).group(1).strip().replace(" -> ", " › ")
                if _VERTICAL_RE.search(body) else "")
    profile = _PROFILE_RE.search(body)
    convo = _CONVO_RE.search(body)

    # Tagline: the line right under "More about <Company>".
    tagline = ""
    ab = re.search(rf"More about {re.escape(company)}\s*\n-+\s*\n(.+)", body)
    if ab:
        tagline = ab.group(1).strip()

    desc = " • ".join(b for b in (tagline, vertical) if b)
    note = f"Inbound via Work at a Startup — {contact}"
    if title:
        note += f" ({title})"
    note += f":\n{message[:800]}"
    if convo:
        note += f"\n\nThread: {convo.group(1)}"

    return {
        "company_name": company,
        "contact_name": contact,
        "contact_title": title,
        "description": desc,
        "location": location,
        "website": website,
        "source_url": profile.group(1) if profile else "",
        "note": note,
    }


def fetch(cfg: dict) -> list[dict]:
    """Ingest WaaS inbound messages into the tracker. Returns the leads written."""
    import database
    from models import Startup

    lookback = int((cfg or {}).get("lookback_days", 90))
    service = _get_service()
    q = (f'from:workatastartup@ycombinator.com subject:"sent you a message" '
         f'newer_than:{lookback}d')
    resp = service.users().messages().list(userId="me", q=q, maxResults=50).execute()
    messages = resp.get("messages", [])

    written: list[dict] = []
    new_ids: list[str] = []
    for meta in messages:
        msg_id = meta["id"]
        if database.is_processed("waas_messages", msg_id):
            continue
        msg = service.users().messages().get(userId="me", id=msg_id, format="full").execute()
        headers = {h["name"]: h["value"] for h in msg.get("payload", {}).get("headers", [])}
        subject = headers.get("Subject", "")
        date = (headers.get("Date", "") or "")
        body = _extract_body(msg.get("payload", {}))

        lead = _parse(body, subject)
        new_ids.append(msg_id)  # mark processed either way; unparseable = nothing to gain re-trying
        if not lead:
            continue

        # Email date → YYYY-MM-DD (fall back handled by insert_activity default).
        m = re.search(r"(\d{1,2})\s+(\w{3})\s+(\d{4})", date)
        months = {mo: f"{i:02d}" for i, mo in enumerate(
            ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"], 1)}
        act_date = f"{m.group(3)}-{months.get(m.group(2), '01')}-{int(m.group(1)):02d}" if m else None

        company = lead["company_name"]
        if company.lower().strip() not in database.get_existing_companies():
            database.insert_startups([Startup(
                company_name=company, description=lead["description"],
                location=lead["location"], website=lead["website"],
                source="WaaS Inbound", source_url=lead["source_url"],
            )])
        if not database.get_tracker_status(company):
            database.upsert_tracker_status(company, "In Progress", "", "")
        act = {
            "company_name": company, "role_title": "",
            "activity_type": "Note", "contact_name": lead["contact_name"],
            "contact_title": lead["contact_title"], "contact_email": "",
            "follow_up_date": "", "notes": lead["note"],
        }
        if act_date:
            act["date"] = act_date
        else:
            from datetime import datetime
            act["date"] = datetime.now().strftime("%Y-%m-%d")
        database.insert_activity(act)
        written.append(lead)

    if new_ids:
        database.mark_processed("waas_messages", new_ids)
    return written
