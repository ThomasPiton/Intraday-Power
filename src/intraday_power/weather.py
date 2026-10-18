"""Country weather indices from the point grids, and temperature normals.

* temperature: population-weighted mean of 10 cities per country
* wind (100 m) and solar (GHI, cloud cover): simple mean of the country's grid points
"""
from __future__ import annotations

import logging

import numpy as np
import pandas as pd

from . import config, storage

log = logging.getLogger(__name__)
NORMALS_PATH = config.STATE / "temp_normals.csv"


def _points(group: str) -> pd.DataFrame:
    pts = config.WEATHER_GROUPS[group][0]
    return pd.DataFrame([{"point": p.id, "country": p.country, "w": p.weight if group == "temp" else 1.0}
                         for p in pts])


def country_index(df: pd.DataFrame, group: str, var: str) -> pd.DataFrame:
    """Long frame [ts, point, var] -> wide frame ts x country (weighted mean over available points)."""
    if df.empty or var not in df:
        return pd.DataFrame()
    d = df[["ts", "point", var]].merge(_points(group), on="point").dropna(subset=[var])
    d["wx"] = d[var] * d["w"]
    g = d.groupby(["ts", "country"])[["wx", "w"]].sum()
    return (g["wx"] / g["w"]).unstack("country").sort_index()


def obs_index(group: str, var: str, start=None, end=None) -> pd.DataFrame:
    return country_index(storage.read("weather", f"obs_{group}", start, end), group, var)


def daily_local(s: pd.Series | pd.DataFrame, tz: str = config.TZ_DISPLAY, how: str = "mean"):
    """Aggregate a UTC-indexed series to local calendar days (complete days only for hourly data)."""
    x = s.copy()
    x.index = x.index.tz_convert(tz)
    g = x.resample("D")
    out = getattr(g, how)()
    n = g.count()
    if isinstance(out, pd.DataFrame):
        return out.where(n >= 20)
    return out.where(n >= 20)


# --------------------------------------------------------------------------- normals
def compute_normals(years: tuple[int, int] = config.NORMALS_YEARS) -> pd.DataFrame:
    """Daily normal temperature per country by day of year (1..366), smoothed ±15 days (circular)."""
    t = obs_index("temp", "temperature_2m", f"{years[0]}-01-01", f"{years[1] + 1}-01-01")
    if t.empty:
        log.warning("no temperature history: normals not computed")
        return pd.DataFrame()
    d = daily_local(t)
    d = d[(d.index.year >= years[0]) & (d.index.year <= years[1])]
    doy = d.groupby(d.index.dayofyear).mean()
    doy = doy.reindex(range(1, 367)).interpolate(limit_direction="both")
    ext = pd.concat([doy.iloc[-15:], doy, doy.iloc[:15]])
    sm = ext.rolling(31, center=True).mean().iloc[15:-15]
    sm.index.name = "doy"
    n_years = d.index.year.nunique()
    sm.attrs["years"] = f"{years[0]}-{years[1]} ({n_years} years)"
    NORMALS_PATH.parent.mkdir(parents=True, exist_ok=True)
    sm.round(2).to_csv(NORMALS_PATH)
    log.info("temperature normals written (%s)", sm.attrs["years"])
    return sm


def load_normals() -> pd.DataFrame:
    if not NORMALS_PATH.exists():
        return pd.DataFrame()
    return pd.read_csv(NORMALS_PATH, index_col="doy")


def normal_for(dates: pd.DatetimeIndex, normals: pd.DataFrame | None = None) -> pd.DataFrame:
    n = load_normals() if normals is None else normals
    if n.empty:
        return pd.DataFrame(index=dates)
    return pd.DataFrame(n.reindex(np.asarray(dates.dayofyear)).values, index=dates, columns=n.columns)
