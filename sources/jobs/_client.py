"""Shared HTTP for the ATS board connectors: identity, rate limit, cache.

The three board APIs are public and unauthenticated, which is exactly why they
deserve to be used politely. Three rules, all from the spec:

  * a descriptive User-Agent, matching what the other sources in this repo send
  * at most one request per second PER HOST, tracked here rather than in each
    connector, so three connectors cannot each think they are the only caller
  * raw responses cached on disk, so a re-run inside the TTL re-reads instead of
    re-fetching — the spec asks for it, and it is also what makes developing
    against a 2.4MB Lever payload bearable

robots.txt is not fetched per request. These are documented public JSON APIs
intended for programmatic use, and a crawl-delay directive on a board's website
does not govern its API; the one-per-second limit here is the stricter promise.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

import requests

# Same identity the other sources in this repo send, so a board operator who
# looks at their logs sees one well-named client rather than several.
HEADERS = {
    "User-Agent": (
        "startup-radar-template/1.0 "
        "(https://github.com/natelevietnam/startup-radar-template)"
    ),
    "Accept": "application/json",
}

TIMEOUT = 25
MIN_INTERVAL_SECONDS = 1.0
CACHE_DIR = Path(__file__).resolve().parents[2] / ".cache" / "boards"
CACHE_TTL_SECONDS = 6 * 60 * 60

# Per-host, and guarded: daily_run may thread, and two threads sharing a host
# would otherwise each see a stale "last request" and both fire immediately.
_last_request: dict[str, float] = {}
_lock = threading.Lock()


class BoardError(RuntimeError):
    """A board could not be read. Carries no traceback worth propagating."""


def _throttle(host: str) -> None:
    with _lock:
        now = time.monotonic()
        wait = MIN_INTERVAL_SECONDS - (now - _last_request.get(host, 0.0))
        if wait > 0:
            time.sleep(wait)
        _last_request[host] = time.monotonic()


def _cache_path(url: str) -> Path:
    return CACHE_DIR / f"{hashlib.sha256(url.encode()).hexdigest()[:24]}.json"


def _read_cache(url: str, ttl: int) -> Optional[object]:
    p = _cache_path(url)
    try:
        if time.time() - p.stat().st_mtime > ttl:
            return None
        return json.loads(p.read_text())
    except (OSError, ValueError):
        return None


def _write_cache(url: str, payload: object) -> None:
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        tmp = _cache_path(url).with_suffix(".tmp")
        tmp.write_text(json.dumps(payload))
        os.replace(tmp, _cache_path(url))
    except (OSError, TypeError, ValueError):
        pass          # a cache that cannot be written must never fail a run


def get_json(url: str, *, ttl: int = CACHE_TTL_SECONDS,
             use_cache: bool = True) -> object:
    """Fetch and parse JSON, honouring the cache and the per-host rate limit.

    Raises BoardError on anything that means "this board did not answer" — a
    non-200, a transport failure, or a body that is not JSON. Callers record
    that against the company rather than retrying in a loop.
    """
    if use_cache:
        cached = _read_cache(url, ttl)
        if cached is not None:
            return cached

    host = urlparse(url).netloc
    _throttle(host)
    try:
        resp = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
    except requests.RequestException as e:
        raise BoardError(f"{type(e).__name__}: {e}") from e

    if resp.status_code == 404:
        raise BoardError("HTTP 404 — no such board token")
    if resp.status_code != 200:
        raise BoardError(f"HTTP {resp.status_code}")
    try:
        payload = resp.json()
    except ValueError as e:
        raise BoardError(f"response was not JSON: {e}") from e

    if use_cache:
        _write_cache(url, payload)
    return payload
