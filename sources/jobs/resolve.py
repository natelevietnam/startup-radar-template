"""Work out which ATS board a tracked company posts on, and remember it.

Three strategies, cheapest first, because the request budget is the scarce
thing:

  1. Stored URLs. A company that has ever surfaced a posting usually did so
     with its own ATS URL attached, and database.company_slug() already reads
     the board token out of one. Costs nothing and is the most reliable signal
     there is — it came from a real posting.
  2. The company's website. Fetch the home page and /careers, look for a link
     to one of the three hosts.
  3. The company name as a token. Slugify and ask each provider. Last because
     it is up to three requests for a guess.

Every outcome is written to ats_boards, including failure. Without that, each
run would re-attempt the same unresolvable companies and spend the whole budget
on dead ends.
"""

from __future__ import annotations

import re
from typing import Iterable, Optional
from urllib.parse import urlparse

import requests

import database
from . import PROVIDERS
from ._client import HEADERS, TIMEOUT, BoardError, _throttle, get_json

# ATS host -> provider. Keyed on the hosts database.py already recognises, so a
# URL the rest of the pipeline treats as an ATS link resolves here too.
_HOST_PROVIDER = {
    "jobs.ashbyhq.com": "ashby",
    "boards.greenhouse.io": "greenhouse",
    "job-boards.greenhouse.io": "greenhouse",
    "boards.eu.greenhouse.io": "greenhouse",
    "jobs.lever.co": "lever",
}

# Board links as they appear in a careers page's HTML.
_LINK_RE = re.compile(
    r"https?://(jobs\.ashbyhq\.com|job-boards\.greenhouse\.io|boards\.greenhouse\.io"
    r"|boards\.eu\.greenhouse\.io|jobs\.lever\.co)/([A-Za-z0-9._-]{2,60})",
    re.IGNORECASE)

_CAREERS_PATHS = ("/careers", "/jobs", "/careers/", "/company/careers")


def provider_for_url(url: str) -> Optional[str]:
    try:
        host = urlparse(url or "").netloc.lower().replace("www.", "")
    except ValueError:
        return None
    return _HOST_PROVIDER.get(host)


def _from_stored_urls(canon: str) -> Optional[tuple[str, str]]:
    for url in database.ats_urls_for_company(canon):
        provider = provider_for_url(url)
        if not provider:
            continue
        token = database.company_slug(url)
        if token:
            return provider, token
    return None


def _fetch_text(url: str) -> str:
    _throttle(urlparse(url).netloc)
    try:
        resp = requests.get(url, headers={**HEADERS, "Accept": "text/html"},
                            timeout=TIMEOUT, allow_redirects=True)
    except requests.RequestException:
        return ""
    return resp.text if resp.status_code == 200 else ""


def _from_website(website: str) -> Optional[tuple[str, str]]:
    if not website:
        return None
    base = website.strip()
    if not base.startswith("http"):
        base = "https://" + base
    base = base.rstrip("/")

    for candidate in (base, *(base + p for p in _CAREERS_PATHS)):
        html = _fetch_text(candidate)
        if not html:
            continue
        m = _LINK_RE.search(html)
        if m:
            host, token = m.group(1).lower(), m.group(2)
            # Greenhouse embeds its own asset paths under the same hosts; a
            # token of "embed" or "assets" is the widget, not a company.
            if token.lower() in ("embed", "assets", "static", "js", "css"):
                continue
            return _HOST_PROVIDER[host], token.lower()
    return None


def _slug_candidates(company_name: str) -> list[str]:
    base = (company_name or "").strip().lower()
    squashed = re.sub(r"[^a-z0-9]", "", base)
    hyphened = re.sub(r"[^a-z0-9]+", "-", base).strip("-")
    # Deduplicated, order preserved: the squashed form is the most common.
    out = []
    for c in (squashed, hyphened):
        if c and len(c) >= 2 and c not in out:
            out.append(c)
    return out


def _board_mentions_company(rows: Iterable[dict], company_name: str) -> bool:
    """Does this board look like it belongs to the company we asked about?

    Guessing a token from a name can land on a DIFFERENT company that happens to
    own the slug, and a wrong employer is the most expensive mistake this
    pipeline makes — it breaks dedupe, lets applied roles resurface, and gets a
    dossier written against the wrong business. SonarSource arriving labelled
    "Arovy" cost a manual correction; two companies sharing the name "Sonar" is
    exactly the shape that produces.

    So a guessed board has to corroborate itself: the company's name, with
    separators removed, must appear in a POSTING BODY. Boards almost always open
    with an "About <company>" section, which is what this reads. Only applied to
    the name-slug strategy — a token taken from a real posting URL or a link on
    the company's own careers page needs no corroboration.

    Deliberately reads description_text and NOTHING ELSE. The row's company_name
    is the value the caller passed into fetch_board, so including it made this
    check compare the input against itself and return True for every board — it
    would have corroborated Arovy against SonarSource's board, the exact case it
    exists to catch. A test caught that; the live spot-check had not, because it
    called this function directly with rows fetched under a different name.
    """
    needle = database._despace(company_name)
    if len(needle) < 3:
        return False                 # too short to be evidence of anything
    for r in rows:
        if needle in database._despace((r.get("description_text") or "")[:3000]):
            return True
    return False


def _from_slug(company_name: str) -> Optional[tuple[str, str]]:
    """Ask each provider whether it has a board under this name.

    A board that answers 200 but lists zero postings is NOT accepted: every
    provider returns an empty list for a token that merely looks plausible, and
    taking it would cache a wrong board that then yields nothing forever.

    Nor is a board that never mentions the company — see
    _board_mentions_company for why that check exists.
    """
    for token in _slug_candidates(company_name):
        for provider, mod in PROVIDERS.items():
            try:
                rows = mod.fetch_board(token, company_name)
            except BoardError:
                continue
            if rows and _board_mentions_company(rows, company_name):
                return provider, token
    return None


def resolve_one(company: dict) -> dict:
    """Resolve one company. Always returns a result dict; never raises."""
    canon, name = company["canon"], company["company_name"]
    for strategy, fn, arg in (
        ("stored-url", _from_stored_urls, canon),
        ("website", _from_website, company.get("website") or ""),
        ("name-slug", _from_slug, name),
    ):
        try:
            hit = fn(arg)
        except Exception as e:                      # a strategy must not end the run
            hit, err = None, f"{strategy}: {type(e).__name__}: {e}"
            database.upsert_board(canon, name, None, None, error=err)
            return {"canon": canon, "company_name": name, "ok": False,
                    "strategy": strategy, "error": err}
        if hit:
            provider, token = hit
            database.upsert_board(canon, name, provider, token)
            return {"canon": canon, "company_name": name, "ok": True,
                    "strategy": strategy, "provider": provider, "token": token}

    database.upsert_board(canon, name, None, None,
                          error="no board found via stored URL, website or name")
    return {"canon": canon, "company_name": name, "ok": False,
            "strategy": "exhausted", "error": "no board found"}


def resolve_pending(limit: int = 35, rank: Optional[dict] = None) -> list[dict]:
    """Attempt up to `limit` companies, most promising first.

    `rank` maps a canonical company name to its dossier score, so the request
    budget goes to companies already known to be a strong fit before it goes to
    one nobody has researched. Ties fall back to the recency ordering
    companies_needing_resolution already applies.
    """
    pending = database.companies_needing_resolution(limit=0)
    if rank:
        pending.sort(key=lambda c: rank.get(c["canon"], -1), reverse=True)
    return [resolve_one(c) for c in pending[:limit]]


def poll_resolved(use_cache: bool = True) -> list[dict]:
    """Every posting on every resolved board, in job_matches row shape."""
    rows: list[dict] = []
    for board in database.resolved_boards():
        mod = PROVIDERS.get(board["provider"])
        if not mod:
            continue
        try:
            rows.extend(mod.fetch_board(board["token"], board["company_name"],
                                        use_cache=use_cache))
        except BoardError as e:
            database.upsert_board(board["canon"], board["company_name"],
                                  board["provider"], board["token"], error=str(e))
    return rows
