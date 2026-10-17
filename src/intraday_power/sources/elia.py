"""Elia Open Data (Opendatasoft Explore API v2.1), no key required.

Bulk reads use ``/exports/parquet`` (no row limit, unlike ``/records``: limit<=100, offset+limit<=10000).
Historical datasets lag about one day; their near-real-time twins hold "today" only, so every
fetch reads both and upserts into the same store series. All timestamps are UTC.
"""
from __future__ import annotations

import io
import logging
import time

import numpy as np
import pandas as pd
import requests

from .. import config, storage

log = logging.getLogger(__name__)
UA = {"User-Agent": "intraday-power (+https://github.com/ThomasPiton/Intraday-Power)"}


def _iso(ts: pd.Timestamp) -> str:
    return pd.Timestamp(ts).tz_convert("UTC").strftime("%Y-%m-%dT%H:%M:%SZ")


def export(dataset: str, time_field: str, start: pd.Timestamp, end: pd.Timestamp,
           select: tuple[str, ...], attempts: int = 5) -> pd.DataFrame:
    """Rows of ``dataset`` with ``start <= time_field < end`` as a DataFrame (UTC datetimes)."""
    url = f"{config.ELIA_BASE}/catalog/datasets/{dataset}/exports/parquet"
    params = {"where": f"{time_field} >= '{_iso(start)}' AND {time_field} < '{_iso(end)}'",
              "select": ",".join(select)}
    for i in range(attempts):
        try:
            r = requests.get(url, params=params, headers=UA, timeout=300)
            if r.status_code == 429 or r.status_code >= 500:
                wait = float(r.headers.get("Retry-After", 10 * 2 ** i))
                log.warning("Elia %s HTTP %s, retry in %.0fs", dataset, r.status_code, wait)
                time.sleep(wait)
                continue
            r.raise_for_status()
            df = pd.read_parquet(io.BytesIO(r.content))
            for c in df.columns:
                if pd.api.types.is_datetime64_any_dtype(df[c]) or c in ("datetime", "quarterhour",
                                                                         "predictiontimeutc",
                                                                         "predictions_forecastedtimeutc"):
                    df[c] = pd.to_datetime(df[c], utc=True)
            return df
        except requests.RequestException as exc:
            log.warning("Elia %s network error %s", dataset, exc)
            time.sleep(10 * 2 ** i)
    log.error("Elia %s: giving up for %s..%s", dataset, start, end)
    return pd.DataFrame()


def _months(start: pd.Timestamp, end: pd.Timestamp) -> list[tuple[pd.Timestamp, pd.Timestamp]]:
    edges = list(pd.date_range(start.normalize().replace(day=1), end, freq="MS", tz="UTC"))
    edges = [max(e, start) for e in edges] + [end]
    if edges[0] > start:
        edges.insert(0, start)
    return [(a, b) for a, b in zip(edges[:-1], edges[1:]) if b > a]


def _read_both(key: str, start: pd.Timestamp, end: pd.Timestamp) -> pd.DataFrame:
    """Historical dataset month by month, then the NRT dataset for the recent tail."""
    ds = config.ELIA[key]
    frames = []
    if ds.hist:
        for a, b in _months(start, end):
            frames.append(export(ds.hist, ds.time_field, a, b, ds.fields))
    if ds.nrt and ds.nrt != ds.hist and end > pd.Timestamp.now(tz="UTC") - pd.Timedelta(days=3):
        frames.append(export(ds.nrt, ds.time_field, max(start, end - pd.Timedelta(days=3)), end, ds.fields))
    frames = [f for f in frames if not f.empty]
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


# --------------------------------------------------------------------------- series
def fetch_imb_qh(start: pd.Timestamp, end: pd.Timestamp) -> int:
    df = _read_both("imb_qh", start, end)
    if df.empty:
        log.warning("imb_qh %s..%s: no data", start, end)
        return 0
    df = df.rename(columns={"datetime": "ts", "systemimbalance": "si", "imbalanceprice": "imb_price",
                            "marginalincrementalprice": "mip", "marginaldecrementalprice": "mdp",
                            "qualitystatus": "quality"})
    return storage.upsert(df[["ts", "si", "imb_price", "mip", "mdp", "alpha", "quality"]],
                          "elia", "imb_qh", ["ts"])


def minute_aggregates(df: pd.DataFrame) -> pd.DataFrame:
    """Per quarter-hour: last, mean and OLS slope (EUR/MWh per minute) of the minute imbalance price."""
    df = df.dropna(subset=["imbalanceprice"]).sort_values("datetime")
    if df.empty:
        return pd.DataFrame(columns=["ts", "min_last", "min_mean", "min_slope", "si_min_last", "n_min"])
    x = (df["datetime"] - df["quarterhour"]).dt.total_seconds() / 60
    y = df["imbalanceprice"]
    g = pd.DataFrame({"qh": df["quarterhour"], "x": x, "y": y, "xy": x * y, "xx": x * x,
                      "si": df["systemimbalance"]}).groupby("qh")
    n = g["x"].count()
    sx, sy, sxy, sxx = g["x"].sum(), g["y"].sum(), g["xy"].sum(), g["xx"].sum()
    den = n * sxx - sx ** 2
    slope = (n * sxy - sx * sy) / den.where(den != 0)
    out = pd.DataFrame({"min_last": g["y"].last(), "min_mean": sy / n,
                        "min_slope": slope.where(n >= 3), "si_min_last": g["si"].last(),
                        "n_min": n.astype(float)})
    return out.reset_index().rename(columns={"qh": "ts"})


def fetch_imb_min(start: pd.Timestamp, end: pd.Timestamp) -> int:
    """Minute imbalance prices -> per-QH aggregates (raw minutes are kept for the last 3 days only)."""
    df = _read_both("imb_min", start, end)
    if df.empty:
        log.warning("imb_min %s..%s: no data", start, end)
        return 0
    n = storage.upsert(minute_aggregates(df), "elia", "imb_min_agg", ["ts"])
    recent = df[df["datetime"] >= pd.Timestamp.now(tz="UTC") - pd.Timedelta(days=3)]
    if len(recent):
        storage.upsert(recent.rename(columns={"datetime": "ts", "systemimbalance": "si",
                                              "imbalanceprice": "imb_price"})[["ts", "si", "imb_price"]],
                       "elia", "imb_min_recent", ["ts"])
    return n


def fetch_si_min(start: pd.Timestamp, end: pd.Timestamp) -> int:
    df = _read_both("si_min", start, end)
    if df.empty:
        log.warning("si_min %s..%s: no data", start, end)
        return 0
    df = df.rename(columns={"datetime": "ts", "systemimbalance": "si"})
    return storage.upsert(df[["ts", "si", "ace"]], "elia", "si_min", ["ts"])


SI_FC_RENAME = {
    "predictiontimeutc": "pred_time",
    "predictions_forecastedtimeutc": "target",
    "predictions_silinearregressionforecast": "si_fc",
    "predictions_categoricalsiprediction_from_minus_inf_to_minus_400": "p_lt_m400",
    "predictions_categoricalsiprediction_from_minus_400_to_minus_200": "p_m400_m200",
    "predictions_categoricalsiprediction_from_minus_200_to_0": "p_m200_0",
    "predictions_categoricalsiprediction_from_0_to_200": "p_0_200",
    "predictions_categoricalsiprediction_from_200_to_400": "p_200_400",
    "predictions_categoricalsiprediction_from_400_to_inf": "p_gt_400",
}


def fetch_si_fc(start: pd.Timestamp, end: pd.Timestamp) -> int:
    ds = config.ELIA["si_fc"]
    frames = [export(ds.hist, ds.time_field, a, b, ds.fields) for a, b in _months(start, end)]
    frames = [f for f in frames if not f.empty]
    if not frames:
        log.warning("si_fc %s..%s: no data", start, end)
        return 0
    df = pd.concat(frames, ignore_index=True).rename(columns=SI_FC_RENAME)
    return storage.upsert(df[list(SI_FC_RENAME.values())], "elia", "si_fc", ["pred_time"])
