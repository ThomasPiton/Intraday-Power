"""SMARD.de (Bundesnetzagentur, CC BY 4.0), no key. Weekly JSON blocks per filter.

``chart_data/{filter}/{region}/index_quarterhour.json`` lists block start times (ms); each block
``{filter}_{region}_quarterhour_{ts}.json`` holds one week of [ms, value] pairs (MW, €/MWh for prices).
"""
from __future__ import annotations

import logging

import pandas as pd

from .. import config, storage
from . import http

log = logging.getLogger(__name__)
BASE = "https://www.smard.de/app/chart_data"
LICENSE = "CC BY 4.0, Bundesnetzagentur | SMARD.de"
# filter id -> (region, column)
FILTERS = {410: ("DE", "load"), 411: ("DE", "load_forecast"), 4359: ("DE", "residual_load")}


def _block(fid: int, region: str, ts: int) -> pd.Series:
    j = http.get_json(f"{BASE}/{fid}/{region}/{fid}_{region}_quarterhour_{ts}.json", min_gap_s=0.2)
    if not j or not j.get("series"):
        return pd.Series(dtype="float64")
    s = pd.DataFrame(j["series"], columns=["ms", "v"]).dropna()
    return pd.Series(s["v"].values * 4, index=pd.to_datetime(s["ms"], unit="ms", utc=True), dtype="float64")


def fetch(start: str | pd.Timestamp = config.V2_HISTORY_START, filters: dict = FILTERS) -> int:
    """Fetch every weekly block overlapping [start, now] for each filter."""
    start = pd.Timestamp(start, tz="UTC") if not isinstance(start, pd.Timestamp) else start
    written = 0
    for fid, (region, col) in filters.items():
        idx = http.get_json(f"{BASE}/{fid}/{region}/index_quarterhour.json")
        if not idx:
            continue
        blocks = [t for t in idx["timestamps"] if pd.Timestamp(t, unit="ms", tz="UTC") >= start - pd.Timedelta(days=7)]
        parts = [_block(fid, region, t) for t in blocks]
        parts = [p for p in parts if len(p)]
        if not parts:
            continue
        s = pd.concat(parts).sort_index()
        df = s[~s.index.duplicated(keep="last")].rename(col).rename_axis("ts").reset_index()
        written += storage.upsert(df, "smard", "de", ["ts"])
        log.info("smard %s %s: %d blocks", fid, col, len(parts))
    return written
