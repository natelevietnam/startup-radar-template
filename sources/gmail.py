"""Gmail source (optional) — pulls funding announcements from email newsletters.

Requires Google Cloud OAuth setup. See README "Optional: Gmail source" for
step-by-step instructions.

Setup summary:
  1. Create a Google Cloud project
  2. Enable Gmail API
  3. Create OAuth Desktop app credentials
  4. Download as credentials.json into the project root
  5. First run will prompt for consent and cache token.json

This file is intentionally minimal — the interactive /setup skill will
generate per-newsletter parsers tailored to whatever newsletters you
actually subscribe to.
"""

from __future__ import annotations

import base64
import re
from datetime import datetime
from pathlib import Path

from models import Startup

BASE_DIR = Path(__file__).parent.parent
CREDENTIALS_FILE = BASE_DIR / "credentials.json"
TOKEN_FILE = BASE_DIR / "token.json"
SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]


def _get_service():
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build

    creds = None
    if TOKEN_FILE.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not CREDENTIALS_FILE.exists():
                raise FileNotFoundError(
                    f"credentials.json not found at {CREDENTIALS_FILE}. "
                    "See README section 'Optional: Gmail source'."
                )
            flow = InstalledAppFlow.from_client_secrets_file(str(CREDENTIALS_FILE), SCOPES)
            creds = flow.run_local_server(port=0)
        TOKEN_FILE.write_text(creds.to_json())

    return build("gmail", "v1", credentials=creds)


def _decode(data: str) -> str:
    if not data:
        return ""
    return base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")


def _extract_body(payload: dict) -> str:
    if payload.get("body", {}).get("data"):
        return _decode(payload["body"]["data"])
    parts = payload.get("parts", [])
    for p in parts:
        if p.get("mimeType") == "text/plain" and p.get("body", {}).get("data"):
            return _decode(p["body"]["data"])
    for p in parts:
        body = _extract_body(p)
        if body:
            return body
    return ""


_STAGE_RE = re.compile(
    r"\b(Pre-?Seed|Seed(?:\s+round)?|Series\s+[A-F]\d?\+?)\b",
    re.IGNORECASE,
)

# Deal-listing pattern shared by Axios Pro Rata, Term Sheet, and most other
# VC newsletters. Anchors on the "<Company>, a/an <context>, raised $X" idiom
# to avoid grabbing common words next to "raised" in prose (the old generic
# regex extracted "had", "has", "agents" from sentences like "X had raised").
_DEAL_RE = re.compile(
    r"""
    # Company name: case-sensitive override so lowercase prose ("alongside.",
    # "previously", "had") doesn't get pulled in as continuation words.
    (?-i:(?P<company>[A-Za-z][\w&'\-]{1,40}(?:\s+[A-Z][\w&'\-]+){0,3}))
    ,\s+
    an?\s+                                                            # "a" or "an"
    (?P<context>[^.]{5,300}?)                                         # city-based desc
    \s+raised\s+
    (?P<amount>(?:\$|€|£)\s*[\d,.]+\s*(?:million|billion|m|b)\b)      # amount
    """,
    re.VERBOSE | re.IGNORECASE,
)

# Words the company-name regex must reject — they're grammatical fillers
# that frequently precede "raised" in newsletter prose.
_COMMON_WORDS = frozenset({
    "had", "has", "have", "is", "was", "were", "are", "they", "we", "i",
    "she", "he", "it", "who", "that", "which", "what", "when", "where",
    "company", "companies", "startup", "startups", "firm", "group", "team",
    "agents", "agency", "meanwhile", "separately", "also", "reportedly",
    "recently", "after", "previously", "earlier", "today", "yesterday",
    "round", "ago", "since", "while", "though", "although",
})

_LOCATION_RE = re.compile(r"([A-Z][^,.]{1,40}?)-based\b")


def _strip_html(body: str) -> str:
    """Convert an HTML-or-plaintext email body to plain text and normalize
    whitespace so deal sentences end up on a single line.

    Newsletter HTML often wraps each deal in nested anchors and paragraphs
    that produce broken text like `URL\\nCompany\\n, a NYC-based...` after
    HTML stripping. We collapse URLs, runs of whitespace, and space-before-
    punctuation so the deal regex sees `Company, a NYC-based ... raised $X`
    as a single contiguous sentence.
    """
    if not body:
        return ""
    if "<" in body and ">" in body:
        from bs4 import BeautifulSoup
        text = BeautifulSoup(body, "html.parser").get_text("\n", strip=True)
    else:
        text = body
    text = re.sub(r"https?://\S+", " ", text)
    text = re.sub(r"\s+", " ", text)
    text = re.sub(r"\s+([,.])", r"\1", text)
    return text


def _parse_body(body: str, subject: str) -> list[Startup]:
    """Extract funded startups from a newsletter body.

    The pattern looks for the canonical VC-deal phrasing
    "<Company>, a/an <city>-based <desc>, raised $X". This format is
    consistent across Axios Pro Rata, Term Sheet, and most other deal
    digests, so a single tuned parser covers them all.
    """
    text = _strip_html(body)
    if not text:
        return []

    found: list[Startup] = []
    seen: set[str] = set()

    for m in _DEAL_RE.finditer(text):
        company = m.group("company").strip().rstrip(",")
        if not company or company.lower() in _COMMON_WORDS:
            continue
        if company.isdigit() or len(company) < 2:
            continue
        # Skip duplicate companies within the same email
        key = company.lower()
        if key in seen:
            continue
        seen.add(key)

        context = m.group("context").strip()[:300]
        amount = m.group("amount").replace(" ", "")

        # Funding stage from the immediate trailing 100 chars (e.g. "in Series B funding")
        trail = text[m.end():m.end() + 120]
        stage_m = _STAGE_RE.search(trail)
        stage = stage_m.group(0).strip() if stage_m else ""

        loc_m = _LOCATION_RE.search(context)
        location = loc_m.group(1).strip() if loc_m else ""

        found.append(Startup(
            company_name=company,
            description=f"{company}, {context}",
            funding_stage=stage,
            amount_raised=amount,
            location=location,
            source=f"Gmail: {subject[:60]}",
        ))
    return found


def fetch(gmail_cfg: dict) -> list[Startup]:
    import database

    service = _get_service()
    label_name = gmail_cfg.get("label", "Startup Funding")

    labels_resp = service.users().labels().list(userId="me").execute()
    label_id = None
    for lbl in labels_resp.get("labels", []):
        if lbl["name"] == label_name:
            label_id = lbl["id"]
            break
    if not label_id:
        print(f"  Gmail label '{label_name}' not found")
        return []

    # Scope to the configured newsletter senders. The label alone is not
    # enough: users routinely route job alerts into the same label, and since
    # this call takes the 50 *most recent* labelled messages, a high-volume
    # sender (LinkedIn alerts run ~7/day) crowds the newsletters out of the
    # window entirely — the source then reports 0 candidates while the mail
    # is sitting right there. Filtering by From makes the pull immune to
    # whatever else shares the label.
    list_kwargs = {"userId": "me", "labelIds": [label_id], "maxResults": 50}
    senders = gmail_cfg.get("senders", {}) or {}
    if senders:
        list_kwargs["q"] = "(" + " OR ".join(f"from:{a}" for a in senders) + ")"

    results = service.users().messages().list(**list_kwargs).execute()
    messages = results.get("messages", [])

    startups: list[Startup] = []
    new_ids: list[str] = []

    for msg_meta in messages:
        msg_id = msg_meta["id"]
        if database.is_processed("gmail", msg_id):
            continue

        msg = service.users().messages().get(
            userId="me", id=msg_id, format="full",
        ).execute()
        headers = {h["name"]: h["value"] for h in msg.get("payload", {}).get("headers", [])}
        subject = headers.get("Subject", "")
        body = _extract_body(msg.get("payload", {}))
        startups.extend(_parse_body(body, subject))
        new_ids.append(msg_id)

    database.mark_processed("gmail", new_ids)
    return startups
