"""Energy-Charts API client (Fraunhofer ISE), /v2 endpoints, no key required.

Rate limit: about 2 requests/min per endpoint and per IP (token bucket, dynamically lowered under
load). The client spaces calls per endpoint, honours ``Retry-After`` on HTTP 429, retries
5xx/network errors with exponential backoff and caches responses on disk for a short TTL.
"""
from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from pathlib import Path

import pandas as pd
import requests

from .. import config, storage

log = logging.getLogger(__name__)
UA = {"User-Agent": "intraday-power (+https://github.com/ThomasPiton/Intraday-Power)"}


class EnergyChartsClient:
    def __init__(self, min_gap_s: float = config.EC_MIN_GAP_S, cache_dir: Path | None = None,
                 cache_ttl_s: float = 600, max_attempts: int = 6, session: requests.Session | None = None):
        self.min_gap_s = min_gap_s
        self.cache_dir = cache_dir or (config.DATA / "cache" / "energy_charts")
        self.cache_ttl_s = cache_ttl_s
        self.max_attempts = max_attempts
        self.session = session or requests.Session()
        self._last_call: dict[str, float] = {}
        self._locks: dict[str, threading.Lock] = {}

    def _lock(self, endpoint: str) -> threading.Lock:
        return self._locks.setdefault(endpoint, threading.Lock())

    def _cache_path(self, endpoint: str, params: dict) -> Path:
        h = hashlib.sha1(json.dumps([endpoint, sorted(params.items())]).encode()).hexdigest()[:16]
        return self.cache_dir / f"{endpoint.strip('/').replace('/', '_')}_{h}.json"

    def get(self, endpoint: str, params: dict) -> dict | None:
        """GET ``endpoint``; returns parsed JSON, or None on 404 / persistent failure."""
        cp = self._cache_path(endpoint, params)
        if cp.exists() and time.time() - cp.stat().st_mtime < self.cache_ttl_s:
            return json.loads(cp.read_text())
        with self._lock(endpoint):
            for attempt in range(self.max_attempts):
                wait = self._last_call.get(endpoint, 0.0) + self.min_gap_s - time.time()
                if wait > 0:
                    time.sleep(wait)
                self._last_call[endpoint] = time.time()
                try:
                    r = self.session.get(config.EC_BASE + endpoint, params=params, headers=UA, timeout=180)
                except requests.RequestException as exc:
                    log.warning("EC %s %s network error: %s", endpoint, params, exc)
                    time.sleep(min(300, 15 * 2 ** attempt))
                    continue
                if r.status_code == 429:
                    ra = float(r.headers.get("Retry-After", 60))
                    log.warning("EC 429 on %s, Retry-After=%.0fs", endpoint, ra)
                    time.sleep(ra + 1)
                    continue
                if r.status_code == 404:
                    log.info("EC %s %s: 404 no content", endpoint, params)
                    return None
                if r.status_code >= 500:
                    log.warning("EC %s HTTP %s, retrying", endpoint, r.status_code)
                    time.sleep(min(300, 15 * 2 ** attempt))
                    continue
                r.raise_for_status()
                j = r.json()
                cp.parent.mkdir(parents=True, exist_ok=True)
                cp.write_text(json.dumps(j))
                return j
        log.error("EC %s %s: giving up after %d attempts", endpoint, params, self.max_attempts)
        return None


def payload_to_frame(j: dict | None) -> pd.DataFrame:
    """Convert a /v2 envelope to a frame with a UTC ``ts`` column and one column per series."""
    if not j or not j.get("data"):
        return pd.DataFrame()
    df = pd.DataFrame([{"ts": d["timestamp"], **d["values"]} for d in j["data"]])
    df["ts"] = pd.to_datetime(df["ts"], utc=True)
    df.attrs["interval_minutes"] = j.get("interval_minutes") or 60
    return df.sort_values("ts").reset_index(drop=True)


def to_quarter_hours(df: pd.DataFrame, cols: list[str], interval_minutes: int | None = None) -> pd.DataFrame:
    """Expand coarser-than-15-min rows to quarter-hours by forward-fill and flag them.

    A row's native resolution is the smaller of its distance to the previous and to the next row
    (so a hole in the data is never filled); an isolated row uses the envelope's
    ``interval_minutes`` (or 15). Adds boolean ``ffill`` = True for every quarter-hour derived from
    a coarser (e.g. hourly) native period.
    """
    if df.empty:
        return df
    df = df.sort_values("ts").reset_index(drop=True)
    d = df["ts"].diff().dt.total_seconds().div(60)
    step = pd.concat([d, d.shift(-1)], axis=1).min(axis=1)
    fallback = interval_minutes or 15
    step = step.where(step <= 60, fallback).fillna(fallback)
    n = (step // 15).clip(lower=1, upper=4).astype(int)
    rep = df.loc[df.index.repeat(n)].copy()
    offs = rep.groupby(level=0).cumcount()
    rep["ts"] = rep["ts"] + pd.to_timedelta(offs * 15, unit="min")
    rep["ffill"] = n.loc[rep.index].values > 1
    return rep[["ts", *cols, "ffill"]].reset_index(drop=True)


# --------------------------------------------------------------------------- fetchers
def fetch_price(client: EnergyChartsClient, zone: str, start: str, end: str) -> int:
    z = config.ZONES[zone]
    j = client.get("/v2/price", {"bzn": z.bzn, "start": start, "end": end})
    df = payload_to_frame(j)
    if df.empty:
        log.warning("price %s %s..%s: no data", zone, start, end)
        return 0
    if j.get("license") != config.EC_PRICE_LICENSE:
        log.warning("price %s: unexpected license %r", zone, j.get("license"))
    df = df.rename(columns={"day_ahead_price": "price"})
    qh = to_quarter_hours(df, ["price"])
    return storage.upsert(qh, "energy_charts", f"price_{zone}", ["ts"])


def fetch_forecast(client: EnergyChartsClient, zone: str, ptype: str, ftype: str,
                   start: str, end: str) -> int:
    z = config.ZONES[zone]
    j = client.get("/v2/public_power_forecast",
                   {"country": z.country, "production_type": ptype, "forecast_type": ftype,
                    "start": start, "end": end})
    df = payload_to_frame(j)
    if df.empty:
        log.warning("forecast %s %s %s %s..%s: no data", zone, ptype, ftype, start, end)
        return 0
    col = f"{ptype}_{config.EC_FORECAST_TYPES[ftype]}"
    df = df.rename(columns={ptype: col})
    qh = to_quarter_hours(df, [col]).rename(columns={"ffill": f"{col}_ffill"})
    return storage.upsert(qh, "energy_charts", f"forecast_{zone}", ["ts"])


def fetch_actuals(client: EnergyChartsClient, zone: str, start: str, end: str) -> int:
    z = config.ZONES[zone]
    j = client.get("/v2/public_power", {"country": z.country, "start": start, "end": end})
    df = payload_to_frame(j)
    if df.empty:
        log.warning("actuals %s %s..%s: no data", zone, start, end)
        return 0
    cols = [c for c in config.EC_PRODUCTION_TYPES if c in df]
    qh = to_quarter_hours(df[["ts", *cols]], cols, j.get("interval_minutes"))
    return storage.upsert(qh, "energy_charts", f"actual_{zone}", ["ts"])


# --------------------------------------------------------------------------- v2 fetchers (FR, DE-LU)
def fetch_power(client: EnergyChartsClient, zone: str, start: str, end: str) -> int:
    """Every public_power series (load, residual load, each fuel) at native resolution, no forward-fill."""
    z = config.ZONES[zone]
    df = payload_to_frame(client.get("/v2/public_power", {"country": z.country, "start": start, "end": end}))
    if df.empty:
        log.warning("power %s %s..%s: no data", zone, start, end)
        return 0
    return storage.upsert(df, "energy_charts", f"power_{zone}", ["ts"])


def _snake(name: str) -> str:
    return "".join(ch if ch.isalnum() else "_" for ch in name.lower()).strip("_")


def _unix_frame(j: dict | None, key: str) -> pd.DataFrame:
    """Classic (non-/v2) envelope: ``unix_seconds`` + a list of {name, data} under ``key``."""
    if not j or not j.get("unix_seconds"):
        return pd.DataFrame()
    df = pd.DataFrame({_snake(s["name"]): s["data"] for s in j.get(key, [])})
    df.insert(0, "ts", pd.to_datetime(j["unix_seconds"], unit="s", utc=True))
    return df


def fetch_installed(client: EnergyChartsClient, zone: str) -> int:
    """Installed net capacity (GW). Monthly for DE; Energy-Charts only has yearly values for FR."""
    z = config.ZONES[zone]
    step = "monthly" if z.country == "de" else "yearly"
    j = client.get("/installed_power", {"country": z.country, "time_step": step, "installation_decommission": "false"})
    if not j or not j.get("time"):
        return 0
    fmt = "%m.%Y" if step == "monthly" else "%Y"
    df = pd.DataFrame({_snake(p["name"]): p["data"] for p in j.get("production_types", [])})
    df.insert(0, "ts", pd.to_datetime(j["time"], format=fmt).tz_localize("UTC"))
    return storage.upsert(df, "energy_charts", f"installed_{zone}", ["ts"])


def fetch_cbpf(client: EnergyChartsClient, zone: str, start: str, end: str) -> int:
    """Cross-border physical flows per neighbour (GW; positive = import into ``zone``)."""
    z = config.ZONES[zone]
    df = _unix_frame(client.get("/cbpf", {"country": z.country, "start": start, "end": end}), "countries")
    if df.empty:
        return 0
    return storage.upsert(df, "energy_charts", f"cbpf_{zone}", ["ts"])


def fetch_frequency(client: EnergyChartsClient, start: str, end: str, region: str = "DE-Freiburg") -> int:
    """Grid frequency at 1 s, stored as per-minute mean/min/max (Hz)."""
    j = client.get("/frequency", {"region": region, "start": start, "end": end})
    if not j or not j.get("unix_seconds"):
        return 0
    s = pd.Series(j["data"], index=pd.to_datetime(j["unix_seconds"], unit="s", utc=True), dtype="float64")
    m = s.resample("1min").agg(["mean", "min", "max"]).dropna().rename_axis("ts").reset_index()
    m.columns = ["ts", "f_mean", "f_min", "f_max"]
    return storage.upsert(m, "energy_charts", "frequency_DE", ["ts"])
