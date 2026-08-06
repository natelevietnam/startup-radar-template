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


_WF_EMPLOYEES_RE = re.compile(
    r"^\s*(?P<company>.+?)\s*/\s*[\d,]+(?:-[\d,]+)?\+?\s*Employees\s*$",
    re.MULTILINE,
)
_WF_URL_RE = re.compile(r"https://wellfound\.com/jobs\?job_listing_slug\S+")


def _parse_wellfound(body: str, subject: str, msg_url: str) -> list[dict]:
    """Parse Wellfound job-alert emails (team@hi.wellfound.com).

    Plaintext layout, one block per role::

        <Role Title>

        <Company> / <NN-NN> Employees

         $<min>–<max>k | <locations> | years of exp | <type>

        <badges...>

        Learn More
        <https://wellfound.com/jobs?job_listing_slug=<id>-<slug>>

    Anchor on the "<Company> / <N> Employees" line: the title is the nearest
    preceding non-empty line, and the comp/location line + apply URL follow.
    Salary is sometimes absent (line starts with " | <location> | ..."). The
    plaintext mangles the apply URL's "=" into "D" (a quoted-printable
    artifact); we restore it so the link resolves.
    """
    if not body:
        return []
    body = body.replace("\r\n", "\n")

    out: list[dict] = []
    matches = list(_WF_EMPLOYEES_RE.finditer(body))
    for i, m in enumerate(matches):
        company = m.group("company").strip().strip("*").strip()
        if not company or "@" in company or len(company) > 60:
            continue

        # Title = last non-empty line before the employees line.
        preceding = body[:m.start()].rstrip().splitlines()
        role = ""
        for line in reversed(preceding):
            if line.strip():
                role = line.strip()
                break
        if not role or len(role) > 90:
            continue

        # Search window: from this block up to the next role (or end).
        end = matches[i + 1].start() if i + 1 < len(matches) else len(body)
        window = body[m.end():end]

        location, comp = "", ""
        for line in window.splitlines():
            if "years of exp" in line:
                segs = [s.strip() for s in line.split("|")]
                try:
                    yi = next(k for k, s in enumerate(segs) if "years of exp" in s)
                    if yi >= 1:
                        location = segs[yi - 1]
                except StopIteration:
                    pass
                comp = next((s for s in segs if "$" in s), "")
                break

        url_m = _WF_URL_RE.search(window)
        url = url_m.group(0).replace("job_listing_slugD", "job_listing_slug=").rstrip(">") if url_m else msg_url

        desc_bits = [b for b in (comp, m.group(0).split("/", 1)[-1].strip()) if b]
        out.append({
            "company_name": company,
            "company_description": " • ".join(desc_bits),
            "role_title": role,
            "location": location,
            "url": url,
            "priority": "",
            "status": "",
            "source": "Wellfound",
        })
    return out


# --- LinkedIn job alerts (jobalerts-noreply@linkedin.com) -------------------

# Blocks are separated by a run of hyphens.
_LI_SEP_RE = re.compile(r"^-{10,}$", re.MULTILINE)

# "View job: https://www.linkedin.com/comm/jobs/view/<id>/?<tracking...>"
_LI_URL_RE = re.compile(
    r"View job:\s*(?P<url>https://www\.linkedin\.com/comm/jobs/view/(?P<jid>\d+)/[^\s]*)"
)

# Interstitial lines LinkedIn inserts between location and the apply link.
# Matched case-insensitively against the whole stripped line.
_LI_BADGES = {
    "fast growing",
    "this company is actively hiring",
    "actively recruiting",
    "be an early applicant",
    "easy apply",
    "promoted",
    "viewed",
    "new",
    "your profile matches this job",
    "alum work here",
    "school alum work here",
    "connections work here",
}


def _parse_linkedin_jobalerts(body: str, subject: str, msg_url: str) -> list[dict]:
    """Parse LinkedIn job-alert digests (jobalerts-noreply@linkedin.com).

    Plaintext layout, one block per role, blocks split by a hyphen rule::

        Your job alert for <alert name> in <location>     <- header, first block only
        <Role Title>
        <Company>
        <Location>
        Fast growing                                      <- optional badge line(s)
        View job: https://www.linkedin.com/comm/jobs/view/<id>/?<tracking>

    LinkedIn alerts are keyword-matched, so a "product manager" alert routinely
    returns program managers, product marketers and product designers. We reuse
    ``newpmjobs.is_product_role`` to keep only genuine PM roles — without it a
    single digest can add a dozen off-function rows, and ``main.py`` applies
    only seniority filtering to ``gmail_jobs`` output (no role/location gate).

    The apply URL carries a long tracking query; we keep just the canonical
    ``/jobs/view/<id>/`` form so the same posting dedupes across digests.
    """
    if not body:
        return []
    from sources.newpmjobs import is_product_role

    body = body.replace("\r\n", "\n")
    out: list[dict] = []
    seen_ids: set[str] = set()

    for block in _LI_SEP_RE.split(body):
        m = _LI_URL_RE.search(block)
        if not m:
            continue
        jid = m.group("jid")
        if jid in seen_ids:
            continue

        lines: list[str] = []
        for raw in block[: m.start()].splitlines():
            s = raw.strip()
            if not s or s.lower() in _LI_BADGES:
                continue
            if s.lower().startswith("your job alert for"):
                continue
            lines.append(s)

        if len(lines) < 2:
            continue
        role, company = lines[0], lines[1]
        location = lines[2] if len(lines) > 2 else ""
        # A salary line ("$120,000/yr - $160,000/yr") can sit where location does.
        comp = ""
        if location.startswith("$"):
            comp, location = location, (lines[3] if len(lines) > 3 else "")

        if not company or len(company) > 60 or len(role) > 90:
            continue
        if not is_product_role(role):
            continue

        seen_ids.add(jid)
        out.append({
            "company_name": company,
            "company_description": comp,
            "role_title": role,
            "location": location,
            "url": f"https://www.linkedin.com/jobs/view/{jid}/",
            "priority": "",
            "status": "",
            "source": "LinkedIn Job Alert",
        })
    return out


# --- Jobright.ai alerts (noreply@jobright.ai) -------------------------------


def _parse_jobright(body: str, subject: str, msg_url: str) -> list[dict]:
    """Parse Jobright.ai job alerts (noreply@jobright.ai).

    These are HTML-only (no text/plain part), but the markup is genuinely
    structured: one ``id="job-container"`` per role, holding

        job-company-name        "The New York Times"
        job-company-categories  "Digital Media · Late Stage"
        job-title               "Senior Product Manager, Platforms"
        job-match-percentage    "84 %"
        job-tag (repeated)      "$144K/yr - $165K/yr" | "New York, NY" | "5+ referrals"

    We read the DOM rather than regexing the flattened text: in flat text the
    company/industry boundary is genuinely ambiguous ("The New York Times
    Digital Media · Late Stage"), and separating them would need a hardcoded
    industry vocabulary that breaks on every new industry Jobright adds.

    The ids repeat within one document — invalid HTML, but normal for email —
    so we scope every lookup to its own container.

    Tag order varies (salary is often absent), so tags are classified by shape
    rather than position. The apply URL keeps only the canonical
    ``/jobs/info/<id>`` path so a posting dedupes across digests.
    """
    if not body:
        return []
    from bs4 import BeautifulSoup

    from sources.newpmjobs import is_product_role

    soup = BeautifulSoup(body, "html.parser")
    out: list[dict] = []
    seen: set[str] = set()

    for cont in soup.find_all(id="job-container"):
        def _txt(key: str) -> str:
            el = cont.find(id=key)
            return el.get_text(" ", strip=True) if el else ""

        company = _txt("job-company-name").strip()
        role = _txt("job-title").strip()
        if not company or not role or len(company) > 60 or len(role) > 90:
            continue
        if not is_product_role(role):
            continue

        tags = [t.get_text(" ", strip=True) for t in cont.find_all(id="job-tag")]
        comp = next((t for t in tags if t.startswith("$")), "")
        location = next(
            (t for t in tags if not t.startswith("$") and "referral" not in t.lower()),
            "",
        )

        anchor = cont.find("a", href=True)
        url = anchor["href"].split("?")[0] if anchor else msg_url
        jid = url.rstrip("/").rsplit("/", 1)[-1]
        if jid in seen:
            continue
        seen.add(jid)

        # "Digital Media · Late Stage" plus salary and Jobright's match score —
        # the score is their fit estimate, not ours, so it stays descriptive.
        bits = [b for b in (comp, _txt("job-company-categories"),
                            _txt("job-match-percentage").replace(" ", "")) if b]
        out.append({
            "company_name": company,
            "company_description": " • ".join(bits),
            "role_title": role,
            "location": location,
            "url": url,
            "priority": "",
            "status": "",
            "source": "Jobright",
        })
    return out


PARSERS: dict[str, Callable[[str, str, str], list[dict]]] = {
    "dgcsearch": _parse_dgcsearch,
    "wellfound": _parse_wellfound,
    "linkedin_jobalerts": _parse_linkedin_jobalerts,
    "jobright": _parse_jobright,
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
