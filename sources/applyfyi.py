"""ApplyFYI source — pulls a curated *company* directory into `startups`.

ApplyFYI (applyfyi.com) publishes curated, filterable company collections
(e.g. an "AI PM roundup"). It is a Next.js app-router site: there is **no**
public JSON API and no ``__NEXT_DATA__`` blob. Instead the company list is
embedded in the streamed React Server Component payload as a sequence of
``self.__next_f.push([1,"<escaped-json>"])`` calls. We reassemble those
chunks, locate the ``"companies":[...]`` array, and map each entry to a
``startups`` row.

Important: the server payload carries **company-level** metadata only
(name, HQ, funding stage, industry tags) — it does *not* include individual
open roles (those load lazily client-side). So this feeds the watchlist /
DeepDive pipeline, not ``job_matches``. Per-URL the server renders a bounded
first page (~25 companies); list several collection URLs in config to widen
coverage.
"""

from __future__ import annotations

import json
import re

import requests

from models import Startup

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) startup-radar-template/1.0"
    )
}

# Whole PM-filtered board. Override / extend via config `sources.applyfyi.urls`.
DEFAULT_URLS = ["https://www.applyfyi.com/companies?fn=pm"]

_PUSH_RE = re.compile(r'self\.__next_f\.push\(\[1,("(?:[^"\\]|\\.)*")\]\)', re.S)


def _decode_rsc(html: str) -> str:
    """Reassemble the streamed RSC payload from all __next_f push chunks."""
    parts: list[str] = []
    for raw in _PUSH_RE.findall(html):
        try:
            parts.append(json.loads(raw))
        except (ValueError, json.JSONDecodeError):
            continue
    return "".join(parts)


def _extract_companies(buf: str) -> list[dict]:
    """Find the `"companies":[...]` array in the decoded RSC and parse it."""
    key = '"companies":'
    i = buf.find(key)
    if i < 0:
        return []
    start = i + len(key)
    if start >= len(buf) or buf[start] != "[":
        return []
    depth = 0
    end = -1
    for k in range(start, len(buf)):
        ch = buf[k]
        if ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
            if depth == 0:
                end = k + 1
                break
    if end < 0:
        return []
    try:
        arr = json.loads(buf[start:end])
    except (ValueError, json.JSONDecodeError):
        return []
    return arr if isinstance(arr, list) else []


def _stage(value: str) -> str:
    """Normalize ApplyFYI funding codes to something StartupFilter understands.

    Single-letter codes ("A".."G") are Series rounds; pass others through.
    """
    if not value:
        return ""
    v = value.strip()
    if len(v) == 1 and v.isalpha():
        return f"Series {v.upper()}"
    return v


def _location(company: dict) -> str:
    city = (company.get("hq_city") or "").strip()
    state = (company.get("hq_state") or "").strip()
    return ", ".join(p for p in (city, state) if p)


def _description(company: dict) -> str:
    desc = (company.get("description") or "").strip()
    tags = company.get("industry_tags")
    if isinstance(tags, list) and tags:
        tag_str = ", ".join(str(t) for t in tags)
        return f"{desc} [{tag_str}]" if desc else f"[{tag_str}]"
    return desc


def fetch(cfg: dict | None = None) -> list[Startup]:
    """Fetch the configured ApplyFYI collection URL(s) → Startup objects.

    Deduped by company name here so a company appearing in multiple
    collections is only emitted once; the caller still dedupes against
    the DB's existing/rejected sets.
    """
    cfg = cfg or {}
    urls = cfg.get("urls") or DEFAULT_URLS
    seen: set[str] = set()
    out: list[Startup] = []
    for url in urls:
        try:
            resp = requests.get(url, headers=HEADERS, timeout=25)
            resp.raise_for_status()
        except Exception as e:
            print(f"  ApplyFYI error ({url}): {e}")
            continue
        companies = _extract_companies(_decode_rsc(resp.text))
        for c in companies:
            name = (c.get("name") or "").strip()
            if not name or name.lower() in seen:
                continue
            seen.add(name.lower())
            domain = (c.get("domain") or "").strip()
            out.append(Startup(
                company_name=name,
                description=_description(c),
                funding_stage=_stage(c.get("funding_stage") or ""),
                amount_raised="",
                location=_location(c),
                website=f"https://{domain}" if domain else "",
                source="ApplyFYI",
                source_url=url,
            ))
    return out
