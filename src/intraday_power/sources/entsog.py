"""ENTSOG Transparency Platform, no key: daily gas entry flows into FR and DE by origin.

ENTSOG's ``aggregatedData`` endpoint returns nothing (checked 2026-10-18), so the entry points are
taken from ``interconnections`` (points whose *to* side is in the country and whose *from* side
is another country or an LNG terminal), then ``operationaldatas`` gives their physical flows.
Units: kWh/d per gas day (06:00-06:00 local); stored as GWh/d per origin. These are gross entry
flows: gas transiting to a third country is included.
"""
from __future__ import annotations

import logging

import pandas as pd

from .. import storage
from . import http

log = logging.getLogger(__name__)
BASE = "https://transparency.entsog.eu/api/v1"
LICENSE = "ENTSOG Transparency Platform (free re-use with attribution)"
COUNTRIES = {"FR": "FR", "DE-LU": "DE"}


def entry_points(country: str) -> dict[str, str]:
    """pointDirection key -> origin label ("LNG", "NO", "NL", ...).

    The entry side (the country's TSO) is used when it publishes data; otherwise the neighbour's exit
    side (some NL->DE points are only reported by the Dutch TSO)."""
    j = http.get_json(f"{BASE}/interconnections", {"limit": -1, "toCountryKey": country}, timeout=180)
    out, seen = {}, set()
    for i in (j or {}).get("interconnections", []):
        infra = i.get("fromInfrastructureTypeLabel") or ""
        lng = "LNG" in infra
        if i["fromCountryKey"] == country and not lng:
            continue                      # domestic link, or a storage site inside the country
        if i["pointKey"] in seen:
            continue
        if i.get("toHasData"):
            key = f'{i["toOperatorKey"]}{i["toPointKey"]}{i["toDirectionKey"]}'
        elif i.get("fromHasData") and i.get("fromOperatorKey"):
            key = f'{i["fromOperatorKey"]}{i["fromPointKey"]}{i["fromDirectionKey"]}'
        else:
            continue
        seen.add(i["pointKey"])
        out[key.lower()] = "LNG" if lng else i["fromCountryKey"]
    return out


def fetch(zone: str, start: pd.Timestamp, end: pd.Timestamp) -> int:
    country = COUNTRIES[zone]
    pts = entry_points(country)
    if not pts:
        return 0
    rows = []
    d = start.normalize()
    while d < end:
        e = min(end, d + pd.Timedelta(days=31))
        j = http.get_json(f"{BASE}/operationaldatas", {
            "indicator": "Physical Flow", "periodType": "day", "from": d.date().isoformat(),
            "to": e.date().isoformat(), "pointDirection": ",".join(pts), "limit": -1, "timezone": "CET"},
            timeout=300)
        rows += (j or {}).get("operationaldatas", [])
        d = e
    if not rows:
        return 0
    df = pd.DataFrame(rows)
    df["key"] = (df["operatorKey"] + df["pointKey"] + df["directionKey"]).str.lower()
    df["origin"] = df["key"].map(pts)
    df["gwh"] = pd.to_numeric(df["value"], errors="coerce") / 1e6
    df["ts"] = pd.to_datetime(df["periodFrom"].str[:10]).dt.tz_localize("UTC")
    # complete gas days, published for at least 24 h (TSOs report with a lag)
    df = df[pd.to_datetime(df["periodTo"], utc=True) <= pd.Timestamp.now(tz="UTC") - pd.Timedelta(hours=24)]
    df = df.drop_duplicates(subset=["key", "ts"], keep="last")
    out = df.pivot_table(index="ts", columns="origin", values="gwh", aggfunc="sum")
    out["total"] = out.sum(axis=1)
    return storage.upsert(out.reset_index(), "entsog", f"entries_{zone}", ["ts"])
