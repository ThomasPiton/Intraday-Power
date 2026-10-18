"""Small polite HTTP helper shared by the v2 sources (no key required anywhere).

Retries network errors and 5xx with exponential backoff, honours ``Retry-After`` on 429 and
returns None on 404 or persistent failure so one missing series never kills a run.
"""
from __future__ import annotations

import logging
import time

import requests

log = logging.getLogger(__name__)
UA = {"User-Agent": "intraday-power (+https://github.com/ThomasPiton/Intraday-Power)"}
_session = requests.Session()


def get(url: str, params: dict | None = None, timeout: float = 120, attempts: int = 5,
        min_gap_s: float = 0.0, headers: dict | None = None, _last: dict = {}) -> requests.Response | None:
    """GET with retries. ``min_gap_s`` spaces consecutive calls to the same host; ``headers``
    replaces the default User-Agent (some firewalls reject a UA containing a URL)."""
    host = url.split("/")[2]
    for attempt in range(attempts):
        wait = _last.get(host, 0.0) + min_gap_s - time.time()
        if wait > 0:
            time.sleep(wait)
        _last[host] = time.time()
        try:
            r = _session.get(url, params=params, headers=UA if headers is None else headers, timeout=timeout)
        except requests.RequestException as exc:
            log.warning("GET %s network error: %s", url, exc)
            time.sleep(min(120, 5 * 2 ** attempt))
            continue
        if r.status_code == 429:
            ra = float(r.headers.get("Retry-After", 60))
            log.warning("429 on %s, Retry-After=%.0fs", url, ra)
            time.sleep(min(ra, 900) + 1)
            continue
        if r.status_code == 404:
            log.info("GET %s %s: 404", url, params)
            return None
        if r.status_code >= 500:
            log.warning("GET %s HTTP %s, retrying", url, r.status_code)
            time.sleep(min(120, 5 * 2 ** attempt))
            continue
        if not r.ok:
            log.error("GET %s %s: HTTP %s %s", url, params, r.status_code, r.text[:300])
            return None
        return r
    log.error("GET %s %s: giving up after %d attempts", url, params, attempts)
    return None


def get_json(url: str, params: dict | None = None, **kw):
    r = get(url, params, **kw)
    if r is None:
        return None
    try:
        return r.json()
    except ValueError:
        log.error("GET %s: invalid JSON %r", url, r.text[:200])
        return None
