"""Renewable forecast revisions: (wind + solar)_intraday - (wind + solar)_day-ahead, per zone and QH."""
from __future__ import annotations

import json

import numpy as np
import pandas as pd

from . import config, storage

COMPONENTS = config.EC_PRODUCTION_TYPES


def revision_frame(zone: str, start: pd.Timestamp | None = None, end: pd.Timestamp | None = None) -> pd.DataFrame:
    """Columns: da_total, id_total, actual_total, revision_mw, revision_pct (index = QH start UTC).

    A component (solar, wind onshore, wind offshore) contributes only where both its day-ahead and
    intraday forecasts exist; a QH needs at least solar and onshore wind to get a revision.
    """
    if zone in config.REVISION_UNAVAILABLE:
        return pd.DataFrame()
    fc = storage.read("energy_charts", f"forecast_{zone}", start, end)
    if fc.empty:
        return pd.DataFrame()
    fc = fc.set_index("ts").sort_index()
    act = storage.read("energy_charts", f"actual_{zone}", start, end)
    act = act.set_index("ts").sort_index() if not act.empty else pd.DataFrame(index=fc.index)
    da_parts, id_parts, act_parts, ok = [], [], [], []
    for c in COMPONENTS:
        d, i = fc.get(f"{c}_da"), fc.get(f"{c}_id")
        if d is None or i is None:
            continue
        both = d.notna() & i.notna()
        da_parts.append(d.where(both))
        id_parts.append(i.where(both))
        if c in act:
            act_parts.append(act[c].reindex(fc.index).where(both))
        if c in ("solar", "wind_onshore"):
            ok.append(both)
    if not ok:
        return pd.DataFrame()
    valid = np.logical_and.reduce(ok)
    da_total = pd.concat(da_parts, axis=1).sum(axis=1, min_count=1).where(valid)
    id_total = pd.concat(id_parts, axis=1).sum(axis=1, min_count=1).where(valid)
    act_total = (pd.concat(act_parts, axis=1).sum(axis=1, min_count=len(act_parts)).where(valid)
                 if act_parts else pd.Series(np.nan, index=fc.index))
    out = pd.DataFrame({"da_total": da_total, "id_total": id_total, "actual_total": act_total})
    out["revision_mw"] = out["id_total"] - out["da_total"]
    out["revision_pct"] = 100 * out["revision_mw"] / out["da_total"].where(out["da_total"].abs() > 50)
    return out.dropna(subset=["revision_mw"])


THRESHOLDS_PATH = config.STATE / "thresholds.json"


def compute_thresholds(since: str = "2024-01-01") -> dict:
    """Alert thresholds from the backfill: p95 |revision| per zone, p95 cross-zone DA spread,
    p99 |BE imbalance - DA|."""
    start = pd.Timestamp(since, tz="UTC")
    out: dict = {"since": since, "computed_at": pd.Timestamp.now(tz="UTC").isoformat(), "revision_p95_mw": {}}
    for z in config.ZONES:
        r = revision_frame(z, start)
        if len(r):
            out["revision_p95_mw"][z] = round(float(r["revision_mw"].abs().quantile(0.95)), 1)
    prices = {}
    for z in config.ZONES:
        p = storage.read("energy_charts", f"price_{z}", start)
        if len(p):
            prices[z] = p.set_index("ts")["price"]
    if prices:
        px = pd.DataFrame(prices).dropna()
        out["xzone_spread_p95_eur"] = round(float((px.max(axis=1) - px.min(axis=1)).quantile(0.95)), 1)
    imb = storage.read("elia", "imb_qh")
    if len(imb) and "BE" in prices:
        s = (imb.set_index("ts")["imb_price"] - prices["BE"]).dropna()
        out["be_imb_spike_p99_eur"] = round(float(s.abs().quantile(0.99)), 1)
    THRESHOLDS_PATH.parent.mkdir(parents=True, exist_ok=True)
    THRESHOLDS_PATH.write_text(json.dumps(out, indent=1))
    return out


def load_thresholds() -> dict:
    return json.loads(THRESHOLDS_PATH.read_text()) if THRESHOLDS_PATH.exists() else {}
