"""Elexon Insights (BMRS) API, no key. Open Government Licence-style "Elexon data licence".

Used for Great Britain rows in the Data Hub: demand outturn, system (imbalance) prices, the APX
market index (the N2EX rows are zero) and the 2-14 day availability forecast per fuel type.
"""
from __future__ import annotations

import logging

import pandas as pd

from .. import storage
from . import http

log = logging.getLogger(__name__)
BASE = "https://data.elexon.co.uk/bmrs/api/v1"
LICENSE = "Elexon BMRS data, BSC data licence (free re-use with attribution)"


def _data(path: str, params: dict) -> list[dict]:
    j = http.get_json(f"{BASE}{path}", {**params, "format": "json"}, min_gap_s=0.3)
    return (j or {}).get("data", []) if isinstance(j, dict) else []


def fetch_demand(start: pd.Timestamp, end: pd.Timestamp) -> int:
    """Initial demand outturn (INDO, MW) per settlement period."""
    rows = []
    d = start.normalize()
    while d < end:
        e = min(end, d + pd.Timedelta(days=7))
        rows += _data("/demand/outturn", {"settlementDateFrom": d.date().isoformat(),
                                         "settlementDateTo": (e - pd.Timedelta(days=1)).date().isoformat()})
        d = e
    if not rows:
        return 0
    df = pd.DataFrame(rows)
    df = pd.DataFrame({"ts": pd.to_datetime(df["startTime"], utc=True), "demand": df["initialDemandOutturn"]})
    return storage.upsert(df, "elexon", "demand", ["ts"])


def fetch_system_prices(start: pd.Timestamp, end: pd.Timestamp) -> int:
    rows = []
    for d in pd.date_range(start.normalize(), end, freq="D", inclusive="left"):
        rows += _data(f"/balancing/settlement/system-prices/{d.date().isoformat()}", {})
    if not rows:
        return 0
    df = pd.DataFrame(rows)
    df = pd.DataFrame({"ts": pd.to_datetime(df["startTime"], utc=True), "ssp": df["systemSellPrice"],
                       "sbp": df["systemBuyPrice"], "niv": df["netImbalanceVolume"]})
    return storage.upsert(df, "elexon", "system_prices", ["ts"])


def fetch_market_index(start: pd.Timestamp, end: pd.Timestamp) -> int:
    rows = []
    d = start.normalize()
    while d < end:
        e = min(end, d + pd.Timedelta(days=7))
        rows += _data("/balancing/pricing/market-index", {"from": d.strftime("%Y-%m-%dT%H:%MZ"),
                                                         "to": e.strftime("%Y-%m-%dT%H:%MZ"),
                                                         "dataProviders": "APXMIDP"})
        d = e
    rows = [r for r in rows if r.get("dataProvider") == "APXMIDP"]
    if not rows:
        return 0
    df = pd.DataFrame(rows)
    df = pd.DataFrame({"ts": pd.to_datetime(df["startTime"], utc=True), "mid_price": df["price"],
                       "mid_volume": df["volume"]})
    return storage.upsert(df, "elexon", "market_index", ["ts"])


def fetch_availability() -> int:
    """FOU2T14D: available capacity (MW) per fuel for each of the next 2-14 days, latest publication."""
    rows = _data("/datasets/FOU2T14D", {})
    if not rows:
        return 0
    df = pd.DataFrame(rows)
    df = df[~df["interconnector"].astype(bool)]
    out = df.pivot_table(index="forecastDate", columns="fuelType", values="outputUsable", aggfunc="sum")
    out.columns = [c.lower() for c in out.columns]
    out.index = pd.to_datetime(out.index).tz_localize("UTC")
    out = out.rename_axis("ts").reset_index()
    out["published"] = pd.to_datetime(df["publishTime"].max(), utc=True)
    return storage.upsert(out, "elexon", "availability", ["ts"])
