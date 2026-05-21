"""Gmail jobs source — parses recruiter emails into job_matches.

Reads emails from configured senders (e.g. specific recruiters) and dispatches
each one to a per-sender parser. Each parser extracts one or more roles in the
shape expected by ``database.insert_job_matches``.

Reuses the OAuth machinery in ``sources.gmail``.
"""

from __future__ import annotations

import re
from typing import Callable

from sources.gmail import _get_service, _extract_body


_COMP_RE = re.compile(
    r"\$\s*\d{2,3}(?:,\d{3})?\s*K?\s*[-–—]\s*\$?\s*\d{2,3}(?:,\d{3})?\s*K?(?:\s*\+\s*equity)?",
    re.IGNORECASE,
)

_LOC_EMOJI_RE = re.compile(r"📍\s*([^|\n💰]+?)(?=\s*(?:\||💰|$))")

_LOC_FALLBACK_RE = re.compile(
    r"(Fully remote"
    r"|Remote(?:\s+\(?[\w\s,]*\)?)?"
    r"|San Francisco[^|.\n]*"
    r"|SF\b[^|.\n]*"
    r"|NYC\b[^|.\n]*"
    r"|New York[^|.\n]*"
    r"|Onsite[^|.\n]*)",
    re.IGNORECASE,
)

# Match "<Company> — <Role>" headers. The role can be terminated by end-of-line,
# or by inline delimiters like 📍 / | / 💰 (Denis batches multiple roles per email).
_HEADER_RE = re.compile(
    r"^[\*\s>]*([A-Z][\w&.'\s\-]{1,60}?)\s*[—–]\s*"
    r"([A-Za-z][\w\s/&,+\-.]{2,80}?)"
    r"(?=\s*(?:📍|\||\(|💰|$))",
    re.MULTILINE,
)


def _parse_dgcsearch(body: str, subject: str, msg_url: str) -> list[dict]:
    """Parse recruiter emails from Denis Goncharov-Carey (dgcsearch.com).

    Observed format:
        <Company> — <Role Title>
        <Location>, $<comp range>. <Description...>

    Some emails batch multiple roles. The signature block starts with
    'Denis Goncharov-Carey'; we trim there to avoid false positives.
    """
    if not body:
        return []

    body = body.replace("\r\n", "\n")
    # Forwarded emails contain "Denis Goncharov-Carey" in the embedded "From:"
    # header at the top. Using rfind ensures we cut at the trailing signature
    # block rather than the leading forwarded-header, preserving the JD body.
    sig_idx = body.rfind("Denis Goncharov-Carey")
    if sig_idx > 0:
        body = body[:sig_idx]

    out: list[dict] = []
    matches = list(_HEADER_RE.finditer(body))
    for i, m in enumerate(matches):
        company = m.group(1).strip().strip("*").strip()
        role = m.group(2).strip().strip("*").strip()

        if "@" in company or len(company) > 60 or len(role) > 80:
            continue
        if not company or not role:
            continue

        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(body)
        chunk = body[start:end].strip()
        # Include the rest of the header line in the search window so inline
        # "📍 Location | 💰 Comp" data on the same line as the title is captured.
        line_end = body.find("\n", m.end())
        if line_end < 0:
            line_end = len(body)
        header_tail = body[m.end():line_end]
        search_window = f"{header_tail}\n{chunk}"

        loc_m = _LOC_EMOJI_RE.search(search_window) or _LOC_FALLBACK_RE.search(search_window)
        location = (loc_m.group(1) if loc_m.lastindex else loc_m.group(0)).strip().rstrip(",.|") if loc_m else ""

        comp_m = _COMP_RE.search(search_window)
        comp = comp_m.group(0).strip() if comp_m else ""

        desc = re.sub(r"\s+", " ", chunk.replace("\n", " ")).strip()[:400]
        if comp and comp not in desc:
            desc = f"{comp}. {desc}"

        out.append({
            "company_name": company,
            "company_description": desc,
            "role_title": role,
            "location": location,
            "url": msg_url,
            "priority": "",
            "status": "",
            "source": f"Gmail: {subject[:60]}",
        })
    return out


PARSERS: dict[str, Callable[[str, str, str], list[dict]]] = {
    "dgcsearch": _parse_dgcsearch,
}


def fetch(jobs_cfg: dict) -> list[dict]:
    """Fetch recent emails from configured senders and parse each via its parser."""
    import database

    senders = jobs_cfg.get("senders", {}) or {}
    if not senders:
        print("  No senders configured for gmail_jobs")
        return []

    service = _get_service()

    sender_q = " OR ".join(f"from:{addr}" for addr in senders.keys())
    lookback_days = int(jobs_cfg.get("lookback_days", 60))
    q = f"({sender_q}) newer_than:{lookback_days}d"

    resp = service.users().messages().list(
        userId="me", q=q, maxResults=50,
    ).execute()
    messages = resp.get("messages", [])

    out: list[dict] = []
    new_ids: list[str] = []

    unparsed_senders: set[str] = set()

    for meta in messages:
        msg_id = meta["id"]
        if database.is_processed("gmail_jobs", msg_id):
            continue

        msg = service.users().messages().get(
            userId="me", id=msg_id, format="full",
        ).execute()
        headers = {h["name"]: h["value"] for h in msg.get("payload", {}).get("headers", [])}
        subject = headers.get("Subject", "")
        from_addr = headers.get("From", "").lower()

        parser_key = None
        for addr, key in senders.items():
            if addr.lower() in from_addr:
                parser_key = key
                break
        if not parser_key:
            # Sender doesn't match config; mark processed so we don't keep
            # refetching unrelated mail that slipped through the gmail query.
            new_ids.append(msg_id)
            continue

        parser = PARSERS.get(parser_key)
        if not parser:
            # Parser stub not yet written. Do NOT mark processed — once the
            # parser is added, the next run will pick up this message.
            unparsed_senders.add(parser_key)
            continue

        body = _extract_body(msg.get("payload", {}))
        msg_url = f"https://mail.google.com/mail/u/0/#inbox/{msg_id}"
        out.extend(parser(body, subject, msg_url))
        new_ids.append(msg_id)

    if unparsed_senders:
        for key in sorted(unparsed_senders):
            print(
                f"  [pending parser] '{key}' has unread mail but no parser registered yet. "
                f"Add one to PARSERS in sources/gmail_jobs.py — the message will be parsed on next run."
            )

    database.mark_processed("gmail_jobs", new_ids)
    return out
