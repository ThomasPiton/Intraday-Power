"""RTE éCO2mix national data via ODRÉ (Licence Ouverte v2.0 / Etalab), no key.

Three datasets join without a gap (checked 2026-10-18):
  eco2mix-national-cons-def  "définitives" 2012 -> last year, "consolidées" this year -> ~2 months ago
  eco2mix-national-tr        real time, rolling window (~7 weeks) up to now, plus tomorrow's J-1 forecast
Actuals are 30-min in the definitive data and 15-min in real time; RTE forecasts are 15-min.
Stored as ``eco2mix/national`` keyed by ``ts`` (UTC), with the ``nature`` column kept.
"""
from __future__ import annotations

import io
import logging

import pandas as pd

from .. import config, storage
from . import http

log = logging.getLogger(__name__)
BASE = "https://odre.opendatasoft.com/api/explore/v2.1/catalog/datasets"
LICENSE = "Licence Ouverte v2.0 (Etalab), RTE éCO2mix via ODRÉ"
KEEP = ["consommation", "prevision_j1", "prevision_j", "nucleaire", "eolien", "eolien_terrestre", "eolien_offshore",
        "solaire", "hydraulique", "hydraulique_fil_eau_eclusee", "hydraulique_lacs", "hydraulique_step_turbinage",
        "pompage", "gaz", "charbon", "fioul", "bioenergies", "stockage_batterie", "destockage_batterie",
        "ech_physiques", "taux_co2", "ech_comm_angleterre", "ech_comm_espagne", "ech_comm_italie",
        "ech_comm_suisse", "ech_comm_allemagne_belgique"]


def _export(dataset: str, where: str | None = None) -> pd.DataFrame:
    r = http.get(f"{BASE}/{dataset}/exports/parquet", {"where": where} if where else None, timeout=600)
    if r is None:
        return pd.DataFrame()
    return pd.read_parquet(io.BytesIO(r.content))


def _tidy(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df
    out = pd.DataFrame({"ts": pd.to_datetime(df["date_heure"], utc=True)})
    for c in KEEP:
        out[c] = pd.to_numeric(df[c], errors="coerce") if c in df else float("nan")
    out["nature"] = df["nature"].astype(str).values
    return out.sort_values("ts")


def fetch_definitive(start: str = config.V2_HISTORY_START) -> int:
    df = _tidy(_export("eco2mix-national-cons-def", f"date_heure >= '{start}'"))
    log.info("eco2mix cons-def from %s: %d rows", start, len(df))
    return storage.upsert(df, "eco2mix", "national", ["ts"])


def fetch_realtime(days: int | None = 2) -> int:
    where = None
    if days is not None:
        since = (pd.Timestamp.now(tz="UTC") - pd.Timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S")
        where = f"date_heure >= '{since}'"
    df = _tidy(_export("eco2mix-national-tr", where))
    return storage.upsert(df, "eco2mix", "national", ["ts"])


def fetch_capacity() -> int:
    """Yearly installed capacity by fuel (MW). ``NC`` (not communicated) becomes null."""
    r = http.get_json(f"{BASE}/parc-prod-par-filiere/records", {"limit": 100, "order_by": "annee"})
    if not r or not r.get("results"):
        return 0
    df = pd.DataFrame(r["results"])
    for c in df.columns:
        if c != "annee":
            df[c] = pd.to_numeric(df[c], errors="coerce")
    df["ts"] = pd.to_datetime(df.pop("annee") + "-01-01", utc=True)
    return storage.upsert(df[["ts", *[c for c in df.columns if c != "ts"]]], "eco2mix", "capacity", ["ts"])


def read(start=None, end=None) -> pd.DataFrame:
    df = storage.read("eco2mix", "national", start, end)
    return df.set_index("ts").sort_index() if len(df) else df
