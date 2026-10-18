"""Open-Meteo (CC BY 4.0, free for non-commercial use, no key): reanalysis, forecast, ensemble.

Quota: ~10 000 weighted calls/day, 5 000/hour, 600/min per IP. A call weighs
``locations × max(1, variables/10) × max(1, days/14)``; ensemble members count as variables.
The client keeps a rolling budget below those limits and sleeps when needed.

Stored series (``data/raw/weather/``):
  obs_<group>      hourly reanalysis per point (archive API, best match = ERA5 family; the last
                   days come from the ECMWF IFS analysis), keys [ts, point]
  forecast.parquet latest 16-day deterministic forecast per point (best match), overwritten
  ensemble.parquet latest ECMWF ENS country index per member, overwritten
"""
from __future__ import annotations

import logging
import math
import time
from collections import deque

import pandas as pd

from .. import config, storage
from . import http

log = logging.getLogger(__name__)
ARCHIVE = "https://archive-api.open-meteo.com/v1/archive"
FORECAST = "https://api.open-meteo.com/v1/forecast"
ENSEMBLE = "https://ensemble-api.open-meteo.com/v1/ensemble"
WEATHER_DIR = config.RAW / "weather"
HOUR_BUDGET, MINUTE_BUDGET = 4000, 450


class _Budget:
    """Rolling weighted-call budget (per process)."""

    def __init__(self):
        self.calls: deque[tuple[float, float]] = deque()

    def spend(self, weight: float) -> None:
        while True:
            now = time.time()
            while self.calls and now - self.calls[0][0] > 3600:
                self.calls.popleft()
            hour = sum(w for _, w in self.calls)
            minute = sum(w for t, w in self.calls if now - t < 60)
            if hour + weight <= HOUR_BUDGET and minute + weight <= MINUTE_BUDGET:
                self.calls.append((now, weight))
                return
            sleep = 61 if hour + weight <= HOUR_BUDGET else max(60, 3600 - (now - self.calls[0][0]) + 1)
            log.info("Open-Meteo budget: %.0f/h, %.0f/min used, sleeping %.0fs", hour, minute, sleep)
            time.sleep(sleep)


budget = _Budget()


def weight(n_points: int, n_vars: int, days: float) -> float:
    return n_points * max(1.0, n_vars / 10) * max(1.0, math.ceil(days / 14))


def _frames(j, points: list[config.WeatherPoint], block: str = "hourly") -> pd.DataFrame:
    """Open-Meteo returns a dict for one location and a list for several (same order)."""
    js = j if isinstance(j, list) else [j]
    out = []
    for p, loc in zip(points, js):
        h = loc.get(block) or {}
        if not h.get("time"):
            continue
        df = pd.DataFrame(h)
        df["ts"] = pd.to_datetime(df.pop("time"), utc=True)
        df.insert(1, "point", p.id)
        out.append(df)
    return pd.concat(out, ignore_index=True) if out else pd.DataFrame()


def _batches(points, n=10):
    for i in range(0, len(points), n):
        yield points[i:i + n]


def _call(url: str, points, params: dict, days: float, n_vars: int):
    budget.spend(weight(len(points), n_vars, days))
    return http.get_json(url, {"latitude": ",".join(f"{p.lat}" for p in points),
                               "longitude": ",".join(f"{p.lon}" for p in points),
                               "timezone": "UTC", "wind_speed_unit": "ms", **params}, timeout=300)


# --------------------------------------------------------------------------- reanalysis
def fetch_obs(group: str, start: str, end: str, chunk_days: int = 366) -> int:
    """Hourly reanalysis for every point of ``group`` between ``start`` and ``end`` (dates)."""
    points, variables = config.WEATHER_GROUPS[group]
    written = 0
    s, e = pd.Timestamp(start), pd.Timestamp(end)
    while s <= e:
        ce = min(e, s + pd.Timedelta(days=chunk_days - 1))
        days = (ce - s).days + 1
        for pts in _batches(points):
            j = _call(ARCHIVE, pts, {"start_date": s.date().isoformat(), "end_date": ce.date().isoformat(),
                                     "hourly": ",".join(variables)}, days, len(variables))
            if j is None:
                continue
            df = _frames(j, pts).dropna(subset=list(variables), how="all")
            written += storage.upsert(df, "weather", f"obs_{group}", ["ts", "point"])
        log.info("weather obs %s %s..%s done", group, s.date(), ce.date())
        s = ce + pd.Timedelta(days=1)
    return written


# --------------------------------------------------------------------------- forecast
def fetch_forecast(days: int = 16) -> pd.DataFrame:
    """Latest deterministic forecast for every point (and the last 2 days, for continuity)."""
    frames = []
    for group, (points, variables) in config.WEATHER_GROUPS.items():
        for pts in _batches(points):
            j = _call(FORECAST, pts, {"hourly": ",".join(variables), "forecast_days": days, "past_days": 2},
                      days + 2, len(variables))
            if j is not None:
                frames.append(_frames(j, pts).assign(group=group))
    if not frames:
        return pd.DataFrame()
    df = pd.concat(frames, ignore_index=True)
    df["issued"] = pd.Timestamp.now(tz="UTC").floor("min")
    WEATHER_DIR.mkdir(parents=True, exist_ok=True)
    df.to_parquet(WEATHER_DIR / "forecast.parquet", index=False, compression="zstd")
    return df


def fetch_ensemble(days: int = 15, model: str = "ecmwf_ifs025") -> pd.DataFrame:
    """ECMWF ENS per member, reduced at once to one country index per group (see ``country_index``)."""
    from ..weather import country_index
    out = []
    for group, var in config.WEATHER_ENSEMBLE_VAR.items():
        points = config.WEATHER_GROUPS[group][0]
        frames = []
        for pts in _batches(points):
            j = _call(ENSEMBLE, pts, {"hourly": var, "models": model, "forecast_days": days}, days, 51)
            if j is not None:
                frames.append(_frames(j, pts))
        if not frames:
            continue
        df = pd.concat(frames, ignore_index=True)
        members = [c for c in df.columns if c == var or c.startswith(f"{var}_member")]
        long = df.melt(id_vars=["ts", "point"], value_vars=members, var_name="member", value_name=var)
        long["member"] = long["member"].str.replace(f"{var}_member", "", regex=False).replace({var: "00"}).astype(int)
        for m, g in long.groupby("member"):
            idx = country_index(g.drop(columns="member"), group, var)
            idx = idx.reset_index().melt(id_vars="ts", var_name="country", value_name="value")
            out.append(idx.assign(group=group, member=m))
    if not out:
        return pd.DataFrame()
    ens = pd.concat(out, ignore_index=True).dropna(subset=["value"])
    ens["issued"] = pd.Timestamp.now(tz="UTC").floor("min")
    ens["model"] = model
    WEATHER_DIR.mkdir(parents=True, exist_ok=True)
    ens.to_parquet(WEATHER_DIR / "ensemble.parquet", index=False, compression="zstd")
    return ens


def read_latest(name: str) -> pd.DataFrame:
    p = WEATHER_DIR / f"{name}.parquet"
    return pd.read_parquet(p) if p.exists() else pd.DataFrame()
