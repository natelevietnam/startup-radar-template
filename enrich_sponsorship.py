"""Read each posting's visa-sponsorship stance and store it.

No feed supplies this. `company_description` is a metadata blurb the source
adapters assemble from industry, level and comp — a sponsorship statement
exists only in the posting body, so it has to be fetched. A scan of the whole
Uncategorized queue on 2026-09-07 found the stance stored on exactly zero rows.

Coverage is partial by nature: roughly a third of postings are client-rendered
shells or bot-blocked boards whose requirements never appear in the fetched
HTML. That is fine, and it is the reason for the central rule here:

    a stance that could not be read is NOT a refusal

Rows we cannot parse keep `sponsorship = NULL` and are never excluded. Silence
is likewise never a refusal: most employers who happily sponsor say nothing at
all, so only an explicit statement counts.

Four things the classifier is careful about, each learned from a real posting:

  * Anthropic writes "Visa sponsorship: We do sponsor visas!" and then hedges,
    "we aren't able to successfully sponsor visas for every role and every
    candidate". Read alone that second sentence looks like a refusal, so a
    clear offer ANYWHERE in the document overrides a sentence-level match.
  * IntegriChain phrases it as a property of the candidate, not the employer:
    "must have valid work authorization that does not now and/or will not in
    the future require sponsorship". No "we don't sponsor" appears at all.
  * C3.ai does the same in the negative: "authorized to work in the United
    States without the need for current or future company sponsorship".
  * Job boards inject their own guesses — "Stedi has a track record of offering
    H1B sponsorships" is the aggregator talking, not the employer, and it is a
    positive signal rather than a refusal.

Where an ATS publishes a job-board API, ask it instead of reading HTML: the
answer is the authoritative posting text and is immune to client rendering.

Usage:
    python enrich_sponsorship.py [--dry-run] [--apply] [--limit N] [--recheck]

    (default)   fetch and store the stance for rows that lack one
    --dry-run   report what would be stored, write nothing
    --apply     additionally file explicit refusals as Not Interested
    --recheck   re-fetch rows already carrying a value
"""

from __future__ import annotations

import concurrent.futures as cf
import html
import json
import re
import sqlite3
import sys
from pathlib import Path

import requests

import filters
from config_loader import load_config

ROOT = Path(__file__).resolve().parent
DB = ROOT / "startup_radar.db"

_UA = {
    "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"),
    "Accept-Language": "en-US,en;q=0.9",
}
_TIMEOUT = 15
_WORKERS = 4          # low: LinkedIn 429s aggressively above this

# Stored in job_matches.sponsorship. SILENT is recorded rather than left NULL so
# a posting we read and found nothing in is not re-fetched on every subsequent
# run; NULL then means only "never successfully read". Use --recheck to re-read
# a stored row after a posting changes.
REFUSED = "refused"
OFFERED = "offered"
SILENT = "silent"

# --- explicit refusal ------------------------------------------------------
# Each pattern must express that sponsorship will NOT be provided. Matched
# against whitespace-collapsed lowercase text, one sentence at a time.
_REFUSE = [
    r"\b(are|is|am)?\s*(not|un)able to (provide|offer|sponsor|support)[^.]{0,40}(sponsor|visa)",
    r"\b(do|does|will|can)\s*n[o']?t\s+(provide|offer|sponsor|support)[^.]{0,40}(sponsor|visa)",
    r"\bwill not sponsor\b",
    r"\bcannot sponsor\b",
    r"\bno (visa |work )?sponsorship\b",
    r"\bsponsorship is not (available|offered|provided|possible)\b",
    # Label form, no verb: Wellfound renders a structured field as
    # "Visa Sponsorship Not Available". The pattern above needs the "is" and
    # missed it, while the OFFER list already matched the positive label
    # "Visa Sponsorship Available" — so a board that states the refusal
    # plainly was read as silent while its sponsoring twin was read as an
    # offer. That asymmetry kept non-sponsoring roles on the board.
    r"\bsponsorship:?\s+not\s+(available|offered|provided)\b",
    r"\bnot eligible for (visa |work |employment )?sponsorship\b",
    r"\b(unable|not able) to sponsor\b",
    r"\bwho do not require (visa |work |employment )?sponsorship\b",
    r"\bmust not require (visa |work |employment )?sponsorship\b",
    r"\bwe are unable to (provide|offer) (immigration|visa|work) ",
    r"\bunable to (provide|offer) (immigration|visa) (support|sponsorship|assistance)\b",
    # Phrased as a property of the candidate rather than the employer:
    r"\b(does|do|will|shall|can)\s*n[o']?t\b[^.]{0,90}\brequires?\b[^.]{0,40}\bsponsorship\b",
    r"\bwithout the need for\b[^.]{0,70}\bsponsorship\b",
    r"\bwithout\b[^.]{0,50}\b(company|employer|employment|visa|immigration) sponsorship\b",
    r"\bwork authorization\b[^.]{0,90}\bnot\b[^.]{0,60}\bsponsorship\b",
]

# Within one sentence, these make it an offer rather than a refusal.
_OFFER = [
    r"\b(we|do|will|can|happy to|able to|glad to)\s*(do\s*)?sponsor\b",
    r"\bsponsorship (is )?(available|offered|provided|supported)\b",
    r"\bwe (offer|provide|support) (visa |work )?sponsorship\b",
    r"\bvisa sponsorship:?\s*(yes|available)\b",
    r"\bwilling to sponsor\b",
]

# A clear offer ANYWHERE in the document beats a sentence-level refusal match.
# This is what keeps Anthropic's "we aren't able to sponsor every role" hedge
# from reading as a refusal.
_DOC_OFFER = [
    r"\bvisa sponsorship:?\s*we do sponsor\b",
    r"\bwe do sponsor visas\b",
    r"\b(visa|green.card)[^.]{0,30}sponsorship available\b",
    r"\bhas a track record of offering h1b sponsorships\b",
]

_REFUSE_RE = [re.compile(p) for p in _REFUSE]
_OFFER_RE = [re.compile(p) for p in _OFFER]
_DOC_OFFER_RE = [re.compile(p) for p in _DOC_OFFER]


def _strip_html(raw: str) -> str:
    # Unescape FIRST. Greenhouse's `content` field arrives HTML-escaped, so
    # stripping tags before unescaping leaves literal "<br>" in the text — which
    # both pollutes the stored evidence and destroys the sentence boundaries the
    # classifier splits on. Twice, because some boards double-escape.
    text = html.unescape(html.unescape(raw or ""))
    text = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", text)
    text = re.sub(r"(?i)<br\s*/?>|</p>|</li>|</div>", ". ", text)
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def classify(text: str) -> tuple[str | None, str]:
    """Return (stance, evidence). stance is REFUSED, OFFERED or None."""
    low = re.sub(r"\s+", " ", text or "").lower()
    if "sponsor" not in low:
        return None, ""
    for rx in _DOC_OFFER_RE:
        m = rx.search(low)
        if m:
            return OFFERED, low[max(0, m.start() - 60):m.end() + 60].strip()
    for sentence in re.split(r"(?<=[.!?;])\s+|•", low):
        if "sponsor" not in sentence:
            continue
        if any(o.search(sentence) for o in _OFFER_RE):
            return OFFERED, sentence.strip()[:300]
        for rx in _REFUSE_RE:
            if rx.search(sentence):
                return REFUSED, sentence.strip()[:300]
    return None, ""


# --- posting text, authoritative source first ------------------------------
def _text_greenhouse(url: str):
    m = re.search(r"greenhouse\.io/(?:embed/job_app\?for=)?([^/?#]+)/jobs/(\d+)", url)
    if not m:
        return None
    token, job_id = m.groups()
    resp = requests.get(f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs/{job_id}",
                        headers=_UA, timeout=_TIMEOUT)
    if resp.status_code != 200:
        return None
    return "greenhouse-api", _strip_html(resp.json().get("content", ""))


def _text_ashby(url: str):
    m = re.search(r"jobs\.ashbyhq\.com/([^/?#]+)/([0-9a-f-]{36})", url)
    if not m:
        return None
    org, job_id = m.groups()
    resp = requests.get(f"https://api.ashbyhq.com/posting-api/job-board/{org}",
                        headers=_UA, timeout=_TIMEOUT)
    if resp.status_code != 200:
        return None
    for job in resp.json().get("jobs", []):
        if job_id in json.dumps(job):
            return "ashby-api", _strip_html(
                job.get("descriptionHtml") or job.get("descriptionPlain") or "")
    return None


def _text_smartrecruiters(url: str):
    m = re.search(r"jobs\.smartrecruiters\.com/([^/?#]+)/(\d+)", url)
    if not m:
        return None
    company, posting_id = m.groups()
    resp = requests.get(
        f"https://api.smartrecruiters.com/v1/companies/{company}/postings/{posting_id}",
        headers=_UA, timeout=_TIMEOUT)
    if resp.status_code != 200:
        return None
    return "smartrecruiters-api", _strip_html(json.dumps(resp.json().get("jobAd", {})))


def _text_wellfound(url: str):
    """Wellfound's posting text, via the canonical /jobs/<slug> path.

    The `?job_listing_slug=` form the feeds hand us is a client-rendered
    shell: it returns HTTP 200 and a byte-identical 239KB body for EVERY
    slug, carrying site navigation and nothing else. That body strips to
    ~10,500 characters, so it sails past the short-body guard in `_fetch`
    and gets classified as `silent` — read, said nothing — when in truth it
    was never read at all. Wellfound rows were therefore never excluded no
    matter what the posting said.

    The canonical path does serve the posting: a JSON-LD JobPosting plus a
    structured "Visa Sponsorship: Available / Not Available" field, which is
    the most reliable sponsorship signal any source on this board publishes.
    Both are returned, the label first so a sentence in the description
    cannot outvote the employer's own structured answer.
    """
    m = re.search(r"wellfound\.com/(?:jobs\?job_listing_slug=|jobs/)([\w-]+)", url)
    if not m:
        return None
    resp = requests.get(f"https://wellfound.com/jobs/{m.group(1)}",
                        headers=_UA, timeout=_TIMEOUT, allow_redirects=True)
    if resp.status_code != 200:
        return None
    body = resp.text
    parts = []
    label = re.search(r"Visa\s+Sponsorship\s*:?\s*(Not\s+Available|Available)",
                      _strip_html(body), re.IGNORECASE)
    if label:
        parts.append(f"Visa Sponsorship {label.group(1)}.")
    for blk in re.findall(r"<script[^>]+application/ld\+json[^>]*>(.*?)</script>",
                          body, re.S):
        try:
            doc = json.loads(blk)
        except Exception:
            continue
        if isinstance(doc, dict) and doc.get("@type") == "JobPosting":
            parts.append(_strip_html(doc.get("description", "")))
    if not parts:
        return None
    return "wellfound-canonical", " ".join(parts)


_TEXT_SOURCES = (_text_greenhouse, _text_ashby, _text_smartrecruiters,
                 _text_wellfound)


def _fetch(row: dict) -> tuple[dict, str | None, str, str]:
    """Return (row, stance, evidence, note). stance None means 'unreadable'."""
    url = (row.get("url") or "").strip()
    if not url:
        return row, None, "", "no url"
    try:
        via, text = "", ""
        for source in _TEXT_SOURCES:
            got = source(url)
            if got:
                via, text = got
                break
        if not text:
            resp = requests.get(url, headers=_UA, timeout=_TIMEOUT, allow_redirects=True)
            if resp.status_code != 200:
                return row, None, "", f"HTTP {resp.status_code}"
            via, text = "html", _strip_html(resp.text)
    except Exception as exc:                       # timeout, DNS, TLS — unreadable
        return row, None, "", type(exc).__name__

    # A body this short is a client-rendered shell, not the posting — unread,
    # not silent, so it stays NULL and gets retried on the next run.
    if len(text) < 400:
        return row, None, "", f"{via} too thin"
    stance, evidence = classify(text)
    if stance is None:
        return row, SILENT, "", via
    return row, stance, evidence, via


def main(argv: list[str]) -> int:
    dry = "--dry-run" in argv
    apply_cuts = "--apply" in argv
    recheck = "--recheck" in argv
    limit = 0
    if "--limit" in argv:
        limit = int(argv[argv.index("--limit") + 1])

    flt = filters.JobFilter(load_config())
    if not flt.require_visa_sponsorship:
        print("targets.require_visa_sponsorship is not set — nothing to enforce.")
        return 0

    con = sqlite3.connect(str(DB))
    con.row_factory = sqlite3.Row
    where = "" if recheck else " AND sponsorship IS NULL"
    rows = [dict(r) for r in con.execute(
        "SELECT id, company_name, role_title, url FROM job_matches "
        "WHERE TRIM(COALESCE(status,'')) = '' AND TRIM(COALESCE(url,'')) <> ''" + where
        + " ORDER BY id" + (f" LIMIT {limit}" if limit else ""))]
    print(f"reading {len(rows)} posting(s){' (dry-run)' if dry else ''}")

    found, refused, unread = [], [], 0
    with cf.ThreadPoolExecutor(max_workers=_WORKERS) as ex:
        for row, stance, evidence, note in ex.map(_fetch, rows):
            if stance is None:
                unread += 1
                continue
            found.append((row, stance, evidence))
            if flt.sponsorship_excluded(stance):
                refused.append((row, evidence))

    silent = sum(1 for _, s, _ in found if s == SILENT)
    print(f"  read: {len(found)} ({silent} said nothing) · unreadable: {unread} · "
          f"explicit refusals: {len(refused)}")
    for row, evidence in refused:
        print(f"    #{row['id']} {row['company_name'][:22]:<22} {row['role_title'][:40]}")
        print(f"        \"{evidence[:120]}\"")

    if dry:
        con.close()
        return len(refused)

    con.executemany(
        "UPDATE job_matches SET sponsorship = ?, sponsorship_evidence = ? WHERE id = ?",
        [(s, e[:400], r["id"]) for r, s, e in found])
    con.commit()
    print(f"  stored {len(found)} stance(s)")

    if apply_cuts:
        # Every undecided refusal, not only the ones read on this run — the
        # default pass reads only rows lacking a value, which is most of them.
        cur = con.execute(
            "UPDATE job_matches SET status = 'Not Interested', "
            "notes = TRIM(COALESCE(notes,'') || ' [no visa sponsorship]') "
            "WHERE TRIM(COALESCE(status,'')) = '' AND sponsorship = ?", (REFUSED,))
        con.commit()
        print(f"  filed {cur.rowcount} row(s) as Not Interested")
    elif refused:
        print("  (not filed — pass --apply to act on them)")
    con.close()
    return len(refused)


if __name__ == "__main__":
    sys.exit(0 if main(sys.argv[1:]) >= 0 else 1)
