"""Peripheral free sources used only by the Data Hub (PLAN_V2 §3), no key:

* Energinet Energi Data Service (DK): imbalance price DK1 (15 min), CO2 intensity DK1 (5 min)
* NVE (NO): weekly hydro reservoir filling, with the historical min/median/max per ISO week
* Elering (EE): day-ahead prices for Finland and the Baltics (15 min)
* REE (ES): Spanish day-ahead spot price
"""
from __future__ import annotations

import json
import logging

import pandas as pd

from .. import storage
from . import http

log = logging.getLogger(__name__)
ENERGINET = "https://api.energidataservice.dk/dataset"
NVE = "https://biapi.nve.no/magasinstatistikk/api/Magasinstatistikk"
ELERING = "https://dashboard.elering.ee/api/nps/price"
REE = "https://apidatos.ree.es/en/datos/mercados/precios-mercados-tiempo-real"
LICENSES = {
    "energinet": "Energinet, Energi Data Service (CC BY 4.0)",
    "nve": "NVE magasinstatistikk (NLOD 2.0)",
    "elering": "Elering AS dashboard API (Nord Pool data as published by Elering)",
    "ree": "Red Eléctrica (REData API), reuse with attribution",
}


# --------------------------------------------------------------------------- Energinet
def _energinet(dataset: str, start: pd.Timestamp, end: pd.Timestamp, area: str = "DK1") -> pd.DataFrame:
    j = http.get_json(f"{ENERGINET}/{dataset}", {"start": start.strftime("%Y-%m-%dT%H:%M"),
                                                 "end": end.strftime("%Y-%m-%dT%H:%M"),
                                                 "filter": json.dumps({"PriceArea": [area]}), "limit": 0},
                      headers={}, min_gap_s=2)  # Energinet's firewall returns 403 for our usual UA
    return pd.DataFrame((j or {}).get("records", []))


def fetch_energinet(start: pd.Timestamp, end: pd.Timestamp) -> int:
    n = 0
    df = _energinet("ImbalancePrice", start, end)
    if len(df):
        out = pd.DataFrame({"ts": pd.to_datetime(df["TimeUTC"], utc=True), "imb_price": df["ImbalancePriceEUR"],
                            "spot": df["SpotPriceEUR"]}).dropna(subset=["imb_price", "spot"], how="all")
        n += storage.upsert(out, "energinet", "imbalance_DK1", ["ts"])
    df = _energinet("CO2Emis", start, end)
    if len(df):
        s = pd.Series(df["CO2Emission"].values, index=pd.to_datetime(df["Minutes5UTC"], utc=True)).sort_index()
        out = s.resample("1h").mean().dropna().rename("co2_g_kwh").rename_axis("ts").reset_index()
        n += storage.upsert(out, "energinet", "co2_DK1", ["ts"])
    return n


# --------------------------------------------------------------------------- NVE
def fetch_nve() -> int:
    """Whole weekly history (since 1995) plus the min/median/max table; both are small."""
    rows = http.get_json(f"{NVE}/HentOffentligData", timeout=180)
    n = 0
    if rows:
        df = pd.DataFrame(rows)
        df["ts"] = pd.to_datetime(df["dato_Id"], utc=True)
        df["area"] = df["omrType"] + df["omrnr"].astype(str)
        out = df.pivot_table(index="ts", columns="area", values="fyllingsgrad").reset_index()
        n += storage.upsert(out, "nve", "reservoir", ["ts"])
    mm = http.get_json(f"{NVE}/HentOffentligDataMinMaxMedian")
    if mm:
        d = pd.DataFrame(mm)
        d["area"] = d["omrType"] + d["omrnr"].astype(str)
        (storage.series_dir("nve", "minmax")).mkdir(parents=True, exist_ok=True)
        d.to_parquet(storage.series_dir("nve", "minmax") / "table.parquet", index=False)
    return n


def nve_minmax() -> pd.DataFrame:
    p = storage.series_dir("nve", "minmax") / "table.parquet"
    return pd.read_parquet(p) if p.exists() else pd.DataFrame()


# --------------------------------------------------------------------------- Elering
def fetch_elering(start: pd.Timestamp, end: pd.Timestamp) -> int:
    frames = []
    d = start
    while d < end:
        e = min(end, d + pd.Timedelta(days=30))
        j = http.get_json(ELERING, {"start": d.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
                                    "end": e.strftime("%Y-%m-%dT%H:%M:%S.000Z")})
        for area, pts in ((j or {}).get("data") or {}).items():
            if pts:
                frames.append(pd.DataFrame(pts).assign(area=area.upper()))
        d = e
    if not frames:
        return 0
    df = pd.concat(frames)
    df["ts"] = pd.to_datetime(df["timestamp"], unit="s", utc=True)
    out = df.pivot_table(index="ts", columns="area", values="price").reset_index()
    return storage.upsert(out, "elering", "prices", ["ts"])


# --------------------------------------------------------------------------- REE
def fetch_ree(start: pd.Timestamp, end: pd.Timestamp) -> int:
    rows = []
    d = start.tz_convert("Europe/Madrid").normalize()
    while d < end:
        e = min(end.tz_convert("Europe/Madrid"), d + pd.Timedelta(days=7))
        j = http.get_json(REE, {"start_date": d.strftime("%Y-%m-%dT%H:%M"), "end_date": e.strftime("%Y-%m-%dT%H:%M"),
                                "time_trunc": "hour"}, min_gap_s=0.5)
        for inc in (j or {}).get("included", []):
            if inc.get("type") == "Spot market price":
                rows += inc["attributes"]["values"]
        d = e
    if not rows:
        return 0
    df = pd.DataFrame(rows)
    out = pd.DataFrame({"ts": pd.to_datetime(df["datetime"], utc=True), "ES": df["value"].astype(float)})
    return storage.upsert(out, "ree", "prices", ["ts"])
