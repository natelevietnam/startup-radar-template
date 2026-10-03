"""Turning a board payload into the row shape job_matches already stores.

Every connector returns the same dict, so main.py can treat a board exactly
like any other source and run it through the existing filter chain. The keys
are the JobMatch fields plus the columns added for board polling; nothing here
writes to the database.
"""

from __future__ import annotations

import html
import re
from datetime import datetime, timezone
from typing import Optional

# Salary ranges written in prose, for boards that publish no structured comp.
# Deliberately narrow: it only fires on a $NNN,NNN-$NNN,NNN or $NNNK-$NNNK pair
# so that a single figure, an hourly rate or an equity note cannot be read as a
# base-salary band. A wrong comp number is worse than no comp number, because
# comp_known=1 changes how the lens scores the row.
_RANGE_RE = re.compile(
    r"\$\s*(\d{2,3})(?:,(\d{3})|\s*[kK])\b"      # 150,000 | 150K
    r"\s*(?:-|–|—|to)\s*"
    r"\$?\s*(\d{2,3})(?:,(\d{3})|\s*[kK])\b"
)

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"[ \t\r\f\v]+")

# Phrases that mean the figure beside them is NOT a base salary. The comp floor
# is a base-salary deal-breaker, so reading an on-target-earnings number as base
# inflates it by however much of the package is commission — Vercel's "Account
# Executive" publishes "$170,000-$209,000 OTE", whose base could be half that.
# Found by spot-checking the parses rather than trusting them: two of the first
# four text-parsed bands on one board were OTE.
# Matched on WORD BOUNDARIES, not as substrings. "ote" as a bare substring is
# inside "remote", "quote", "note" and "promote" — so a perfectly good band
# followed by "for remote roles" was being thrown away.
_NOT_BASE = (
    r"ote", r"on[- ]target(?:\s+earnings)?", r"commissions?", r"variable\s+pay",
    r"total\s+(?:compensation|comp|cash|rewards|target)",
    r"(?:inclusive\s+of|including)\s+equity",
)
_NOT_BASE_RE = re.compile(
    r"(?<![a-z])(?:" + "|".join(_NOT_BASE) + r")(?![a-z])", re.IGNORECASE)
# Markers that a figure IS the base salary. These beat the list above when they
# sit closer to the number, because the label immediately before a range is
# what names it: "base pay range for this role is $X - $Y" versus "OTE range
# for this role is $X - $Y".
_IS_BASE = (r"base\s+salary", r"base\s+pay", r"annual\s+base",
            r"salary\s+range", r"base\s+range")
_IS_BASE_RE = re.compile(
    r"(?<![a-z])(?:" + "|".join(_IS_BASE) + r")(?![a-z])", re.IGNORECASE)

# How far back to look for whichever label is nearest the figure.
_LABEL_LOOKBEHIND = 70
# And how far forward for a suffix like "$X - $Y OTE", where the qualifier
# trails the number instead of labelling it. Short on purpose: a sentence two
# clauses later talking about commission is not describing this range.
_SUFFIX_LOOKAHEAD = 18


def strip_html(raw: str) -> str:
    """Greenhouse serves `content` HTML-escaped, so unescape before stripping.

    Unescaped first, then tags removed, then entities resolved again: the
    payload is double-encoded ("&lt;p&gt;&amp;nbsp;"), so one pass leaves
    literal &nbsp; behind in the text the signal matcher reads.
    """
    if not raw:
        return ""
    text = html.unescape(raw)
    text = _TAG_RE.sub(" ", text)
    text = html.unescape(text)
    text = _WS_RE.sub(" ", text)
    return "\n".join(line.strip() for line in text.splitlines() if line.strip())


def parse_comp_from_text(text: str) -> tuple[Optional[int], Optional[int]]:
    """Best-effort BASE-salary range out of prose. (None, None) when unsure.

    Scans every range in the text and takes the first whose surrounding words
    do not mark it as on-target earnings, commission or total compensation. A
    posting that states only an OTE figure therefore comes back unknown, which
    scores as "no band published" rather than as a band it does not have.
    """
    if not text:
        return None, None
    m = None
    for candidate in _RANGE_RE.finditer(text):
        before = text[max(0, candidate.start() - _LABEL_LOOKBEHIND):candidate.start()]
        after = text[candidate.end():candidate.end() + _SUFFIX_LOOKAHEAD]
        # Stop the suffix at the end of the sentence. "$180,000 - $220,000. OTE
        # with commission is higher." is a base band followed by a separate
        # sentence about something else, not an OTE figure.
        after = re.split(r"[.;\n]", after, 1)[0]

        # A qualifier trailing the number disqualifies it outright: "$170,000 -
        # $209,000 OTE" names itself.
        if _NOT_BASE_RE.search(after):
            continue

        # Otherwise the nearest preceding label wins, because the label
        # immediately before a range is what names it. Taking the LAST match of
        # each resolves "base salary ... OTE ... $X" and "OTE ... base ... $X"
        # to whichever word actually sits against this figure.
        bad = [m.start() for m in _NOT_BASE_RE.finditer(before)]
        good = [m.start() for m in _IS_BASE_RE.finditer(before)]
        if (max(bad) if bad else -1) > (max(good) if good else -1):
            continue

        m = candidate
        break
    if m is None:
        return None, None

    def val(hundreds: str, thousands: Optional[str]) -> int:
        return int(hundreds) * 1000 + int(thousands) if thousands else int(hundreds) * 1000

    lo, hi = val(m.group(1), m.group(2)), val(m.group(3), m.group(4))
    if lo > hi:
        lo, hi = hi, lo
    # Sanity band. Below 40k is an hourly or part-year figure; above 2M is a
    # total-contract or equity number, not a salary.
    if lo < 40_000 or hi > 2_000_000:
        return None, None
    return lo, hi


def iso_date(value) -> Optional[str]:
    """Normalise a board's publish timestamp to YYYY-MM-DD, or None.

    Ashby sends ISO8601 with an offset, Greenhouse the same, Lever milliseconds
    since the epoch. date_found stays the pipeline's own first-seen date; this
    is the board's.
    """
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        try:
            seconds = value / 1000 if value > 10 ** 11 else value
            return datetime.fromtimestamp(seconds, tz=timezone.utc).date().isoformat()
        except (OverflowError, OSError, ValueError):
            return None
    text = str(value).strip()
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date().isoformat()
    except ValueError:
        try:
            return datetime.strptime(text[:10], "%Y-%m-%d").date().isoformat()
        except ValueError:
            return None


def row(*, company_name: str, role_title: str, location: str, url: str,
        provider: str, provider_job_id: str, source: str,
        posted_at: Optional[str] = None, description_text: str = "",
        comp_min: Optional[int] = None, comp_max: Optional[int] = None,
        company_description: str = "") -> dict:
    """One posting, in the shape main.py and database.insert_job_matches expect."""
    known = comp_min is not None and comp_max is not None
    return {
        "company_name": (company_name or "").strip(),
        "company_description": company_description,
        "role_title": (role_title or "").strip(),
        "location": (location or "").strip(),
        "url": url,
        "source": source,
        "provider": provider,
        "provider_job_id": str(provider_job_id),
        "posted_at": posted_at,
        "description_text": description_text,
        "comp_min": comp_min,
        "comp_max": comp_max,
        "comp_known": 1 if known else 0,
    }
