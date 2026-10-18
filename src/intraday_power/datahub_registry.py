"""Data Hub registry: one entry per row of the Data Hub page (PLAN_V2 §2.5).

Each row says where the series comes from, its unit, native frequency, licence and a one-line
description; the front-end is built from the exported registry, never from hard-coded rows.
A loader returns a float Series indexed by UTC timestamps, already in the display unit.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Callable

import numpy as np
import pandas as pd

from . import config, storage, weather
from .sources import eco2mix, elexon, entsog, peripheral, smard

log = logging.getLogger(__name__)

CATEGORIES = [
    ("demand", "Demand"), ("renewables", "Renewables"), ("thermal", "Nuclear & thermal"), ("hydro", "Hydro"),
    ("grid", "Grid"), ("balancing", "Balancing"), ("prices", "Prices"), ("gas", "Gas & carbon"),
    ("gaps", "Data gaps"),
]
SOURCES = {
    "eco2mix": ("RTE éCO2mix via ODRÉ", eco2mix.LICENSE, "https://odre.opendatasoft.com"),
    "ec": ("Energy-Charts (Fraunhofer ISE)", "CC BY 4.0, Energy-Charts.info", "https://www.energy-charts.info"),
    "ec_price": ("Energy-Charts (Fraunhofer ISE)", config.EC_PRICE_LICENSE, "https://www.energy-charts.info"),
    "smard": ("SMARD.de (Bundesnetzagentur)", smard.LICENSE, "https://www.smard.de"),
    "elexon": ("Elexon Insights (BMRS)", elexon.LICENSE, "https://bmrs.elexon.co.uk"),
    "elia": ("Elia Open Data", "Elia Open Data terms", "https://opendata.elia.be"),
    "energinet": ("Energinet Energi Data Service", peripheral.LICENSES["energinet"], "https://www.energidataservice.dk"),
    "nve": ("NVE magasinstatistikk", peripheral.LICENSES["nve"], "https://www.nve.no"),
    "elering": ("Elering", peripheral.LICENSES["elering"], "https://dashboard.elering.ee"),
    "ree": ("Red Eléctrica (REData)", peripheral.LICENSES["ree"], "https://www.ree.es/en/datos"),
    "entsog": ("ENTSOG Transparency Platform", entsog.LICENSE, "https://transparency.entsog.eu"),
    "open_meteo": ("Open-Meteo (ERA5 + forecasts)", config.OPEN_METEO_ATTRIBUTION, "https://open-meteo.com"),
}


@dataclass(frozen=True)
class Row:
    id: str
    cat: str
    label: str
    country: str
    unit: str
    source: str
    freq: str
    loader: Callable[[pd.Timestamp], pd.Series]
    desc: str
    decimals: int = 1
    agg: str = "mean"            # daily aggregation for spark/detail: mean | sum | last
    forecast: bool = False       # values extend into the future (official forecast)
    tags: tuple = field(default_factory=tuple)


# --------------------------------------------------------------------------- loaders
def _col(source: str, series: str, col: str, scale: float = 1.0):
    def f(start: pd.Timestamp) -> pd.Series:
        d = storage.read(source, series, start)
        if d.empty or col not in d:
            return pd.Series(dtype="float64")
        return (d.set_index("ts")[col].astype("float64") * scale).dropna().sort_index()
    return f


def _sum(source: str, series: str, cols: list[str], scale: float = 1.0):
    def f(start):
        d = storage.read(source, series, start)
        if d.empty:
            return pd.Series(dtype="float64")
        d = d.set_index("ts")
        have = [c for c in cols if c in d]
        if not have:
            return pd.Series(dtype="float64")
        return (d[have].sum(axis=1, min_count=1) * scale).dropna().sort_index()
    return f


def _eco(col: str, scale: float = 1e-3):
    return _col("eco2mix", "national", col, scale)


def _wx(group: str, var: str, zone: str):
    def f(start):
        idx = weather.obs_index(group, var, start)
        return idx[zone].dropna() if zone in idx else pd.Series(dtype="float64")
    return f


def _cbpf_sum(zone: str):
    def f(start):
        d = storage.read("energy_charts", f"cbpf_{zone}", start)
        if d.empty:
            return pd.Series(dtype="float64")
        d = d.set_index("ts")
        return (d["sum"] if "sum" in d else d.sum(axis=1)).astype("float64").dropna()
    return f


def _nve_total(start):
    d = storage.read("nve", "reservoir")
    if d.empty:
        return pd.Series(dtype="float64")
    d = d.set_index("ts")
    col = next((c for c in ("NO0", "NO1") if c in d), None)
    if col is None:
        el = [c for c in d.columns if c.startswith("EL")]
        return (d[el].mean(axis=1) * 100).dropna()
    return (d[col] * 100).dropna()


def _elexon_avail(fuel: str):
    def f(start):
        d = storage.read("elexon", "availability", start - pd.Timedelta(days=30))
        if d.empty or fuel not in d:
            return pd.Series(dtype="float64")
        return (d.set_index("ts")[fuel] / 1000).dropna()
    return f


R: list[Row] = [
    # ---------------------------------------------------------------- 1 demand
    Row("fr_load", "demand", "Consumption", "FR", "GW", "eco2mix", "15–30 min", _eco("consommation"),
        "French national electricity consumption (RTE real-time, then consolidated/definitive)."),
    Row("fr_load_fc", "demand", "Consumption forecast D-1", "FR", "GW", "eco2mix", "15 min", _eco("prevision_j1"),
        "RTE's day-ahead consumption forecast, published the day before.", forecast=True),
    Row("de_load", "demand", "Load", "DE", "GW", "smard", "15 min", _col("smard", "de", "load", 1e-3),
        "German grid load (Netzlast) as published by the Bundesnetzagentur."),
    Row("de_load_fc", "demand", "Load forecast", "DE", "GW", "smard", "15 min", _col("smard", "de", "load_forecast", 1e-3),
        "TSOs' day-ahead load forecast for Germany.", forecast=True),
    Row("gb_demand", "demand", "Demand (INDO)", "GB", "GW", "elexon", "30 min", _col("elexon", "demand", "demand", 1e-3),
        "Great Britain initial national demand outturn (transmission-connected generation view)."),
    Row("fr_temp", "demand", "Temperature, population-weighted", "FR", "°C", "open_meteo", "1 h",
        _wx("temp", "temperature_2m", "FR"), "2 m temperature over 10 French cities weighted by urban-area population."),
    Row("de_temp", "demand", "Temperature, population-weighted", "DE", "°C", "open_meteo", "1 h",
        _wx("temp", "temperature_2m", "DE-LU"), "2 m temperature over 10 German cities weighted by urban-area population."),
    # ---------------------------------------------------------------- 2 renewables
    Row("fr_wind", "renewables", "Wind generation", "FR", "GW", "eco2mix", "15–30 min", _eco("eolien"),
        "French wind output, onshore + offshore."),
    Row("fr_solar", "renewables", "Solar generation", "FR", "GW", "eco2mix", "15–30 min", _eco("solaire"),
        "French solar output (estimate of total PV incl. distribution-connected)."),
    Row("de_wind_on", "renewables", "Wind onshore", "DE", "GW", "ec", "15 min", _col("energy_charts", "power_DE-LU", "wind_onshore", 1e-3),
        "German onshore wind output."),
    Row("de_wind_off", "renewables", "Wind offshore", "DE", "GW", "ec", "15 min", _col("energy_charts", "power_DE-LU", "wind_offshore", 1e-3),
        "German offshore wind output (North Sea and Baltic)."),
    Row("de_solar", "renewables", "Solar generation", "DE", "GW", "ec", "15 min", _col("energy_charts", "power_DE-LU", "solar", 1e-3),
        "German solar output."),
    Row("fr_wind100", "renewables", "Wind speed at 100 m (grid mean)", "FR", "m/s", "open_meteo", "1 h",
        _wx("wind", "wind_speed_100m", "FR"), "Simple mean over 15 points on French wind areas (3 offshore)."),
    Row("de_wind100", "renewables", "Wind speed at 100 m (grid mean)", "DE", "m/s", "open_meteo", "1 h",
        _wx("wind", "wind_speed_100m", "DE-LU"), "Simple mean over 15 points on German wind areas (3 offshore)."),
    Row("fr_ghi", "renewables", "Solar radiation (GHI, grid mean)", "FR", "W/m²", "open_meteo", "1 h",
        _wx("solar", "shortwave_radiation", "FR"), "Global horizontal irradiance, mean over 10 sunny/equipped French points.", decimals=0),
    Row("de_ghi", "renewables", "Solar radiation (GHI, grid mean)", "DE", "W/m²", "open_meteo", "1 h",
        _wx("solar", "shortwave_radiation", "DE-LU"), "Global horizontal irradiance, mean over 10 German points.", decimals=0),
    # ---------------------------------------------------------------- 3 nuclear & thermal
    Row("fr_nuclear", "thermal", "Nuclear generation", "FR", "GW", "eco2mix", "15–30 min", _eco("nucleaire"),
        "French nuclear output (fleet total; unit-level availability is a data gap)."),
    Row("fr_gas", "thermal", "Gas-fired generation", "FR", "GW", "eco2mix", "15–30 min", _eco("gaz"),
        "French gas-fired output (CCGT, OCGT, CHP)."),
    Row("de_lignite", "thermal", "Lignite generation", "DE", "GW", "ec", "15 min",
        _col("energy_charts", "power_DE-LU", "fossil_brown_coal_lignite", 1e-3), "German lignite output."),
    Row("de_coal", "thermal", "Hard coal generation", "DE", "GW", "ec", "15 min",
        _col("energy_charts", "power_DE-LU", "fossil_hard_coal", 1e-3), "German hard coal output."),
    Row("de_gas", "thermal", "Gas-fired generation", "DE", "GW", "ec", "15 min",
        _col("energy_charts", "power_DE-LU", "fossil_gas", 1e-3), "German gas-fired output."),
    Row("gb_nuclear_avail", "thermal", "Nuclear availability, next 14 days", "GB", "GW", "elexon", "daily",
        _elexon_avail("nuclear"), "Available GB nuclear capacity forecast per day (FOU2T14D), latest publication.",
        forecast=True, agg="last"),
    # ---------------------------------------------------------------- 4 hydro
    Row("fr_hydro", "hydro", "Hydro generation", "FR", "GW", "eco2mix", "15–30 min", _eco("hydraulique"),
        "French hydro output: run-of-river, lakes and pumped-storage turbining."),
    Row("fr_pumping", "hydro", "Pumped-storage pumping", "FR", "GW", "eco2mix", "15–30 min", _eco("pompage"),
        "Electricity used to pump water uphill (negative = consumption)."),
    Row("de_hydro", "hydro", "Run-of-river", "DE", "GW", "ec", "15 min",
        _col("energy_charts", "power_DE-LU", "hydro_run_of_river", 1e-3), "German run-of-river hydro output."),
    Row("no_reservoir", "hydro", "Reservoir filling", "NO", "%", "nve", "weekly", _nve_total,
        "Norwegian hydro reservoirs, share of capacity filled (all areas), published each Wednesday.", agg="last"),
    # ---------------------------------------------------------------- 5 grid
    Row("de_freq", "grid", "Grid frequency (Freiburg)", "DE", "Hz", "ec", "1 s → 1 min", _col("energy_charts", "frequency_DE", "f_mean"),
        "Continental Europe synchronous-area frequency measured in Freiburg; 50 Hz nominal.", decimals=3),
    Row("fr_exchanges", "grid", "Net physical exchanges", "FR", "GW", "eco2mix", "15–30 min", _eco("ech_physiques"),
        "Sum of physical flows on France's borders (positive = net import)."),
    Row("de_flows", "grid", "Net cross-border physical flows", "DE", "GW", "ec", "15 min", _cbpf_sum("DE-LU"),
        "Sum of physical flows on Germany's borders (positive = net import)."),
    Row("fr_gb_comm", "grid", "Commercial exchange with GB", "FR", "GW", "eco2mix", "15–30 min", _eco("ech_comm_angleterre"),
        "Scheduled commercial exchange FR–GB (negative = export from France)."),
    Row("fr_co2", "grid", "CO₂ intensity", "FR", "g/kWh", "eco2mix", "15–30 min", _col("eco2mix", "national", "taux_co2"),
        "CO₂ emitted per kWh produced in France (RTE estimate).", decimals=0),
    Row("dk1_co2", "grid", "CO₂ intensity", "DK1", "g/kWh", "energinet", "5 min → 1 h", _col("energinet", "co2_DK1", "co2_g_kwh"),
        "CO₂ per kWh consumed in western Denmark.", decimals=0),
    # ---------------------------------------------------------------- 6 balancing
    Row("be_imb", "balancing", "Imbalance price", "BE", "€/MWh", "elia", "15 min", _col("elia", "imb_qh", "imb_price"),
        "Elia's single imbalance price per quarter-hour (the v1 bots trade against it)."),
    Row("be_si", "balancing", "System imbalance", "BE", "MW", "elia", "15 min", _col("elia", "imb_qh", "si"),
        "Belgian system imbalance; negative = system short.", decimals=0),
    Row("gb_ssp", "balancing", "System price", "GB", "£/MWh", "elexon", "30 min", _col("elexon", "system_prices", "ssp"),
        "GB single imbalance price (SSP = SBP since 2015)."),
    Row("gb_niv", "balancing", "Net imbalance volume", "GB", "MWh", "elexon", "30 min", _col("elexon", "system_prices", "niv"),
        "Positive = system short, the SO bought energy.", decimals=0),
    Row("dk1_imb", "balancing", "Imbalance price", "DK1", "€/MWh", "energinet", "15 min", _col("energinet", "imbalance_DK1", "imb_price"),
        "Western Denmark imbalance price."),
    # ---------------------------------------------------------------- 7 prices
    *[Row(f"{z.lower().replace('-lu', '')}_da", "prices", "Day-ahead price", z.replace("-LU", ""), "€/MWh", "ec_price",
          "15 min (hourly before Oct 2025)", _col("energy_charts", f"price_{z}", "price"),
          f"SDAC day-ahead clearing price, {z} bidding zone.", forecast=True) for z in config.ZONES],
    Row("gb_mid", "prices", "Market index (APX)", "GB", "£/MWh", "elexon", "30 min", _col("elexon", "market_index", "mid_price"),
        "Volume-weighted short-term GB price from APX, a GB proxy since GB left SDAC."),
    Row("es_da", "prices", "Day-ahead price", "ES", "€/MWh", "ree", "15 min–1 h", _col("ree", "prices", "ES"),
        "Spanish day-ahead (OMIE) price as published by REE.", forecast=True),
    *[Row(f"{a.lower()}_da", "prices", "Day-ahead price", a, "€/MWh", "elering", "15 min", _col("elering", "prices", a),
          f"Nord Pool day-ahead price, {name}.", forecast=True) for a, name in (("FI", "Finland"), ("EE", "Estonia"), ("LT", "Lithuania"))],
    # ---------------------------------------------------------------- 8 gas
    Row("fr_gas_entries", "gas", "Gas entry flows", "FR", "GWh/d", "entsog", "daily", _col("entsog", "entries_FR", "total"),
        "Physical gas flows into France at borders and LNG terminals (gross, incl. transit).", decimals=0, agg="sum"),
    Row("fr_gas_lng", "gas", "LNG send-out", "FR", "GWh/d", "entsog", "daily", _col("entsog", "entries_FR", "LNG"),
        "Gas injected into the French grid from LNG terminals.", decimals=0, agg="sum"),
    Row("de_gas_entries", "gas", "Gas entry flows", "DE", "GWh/d", "entsog", "daily", _col("entsog", "entries_DE-LU", "total"),
        "Physical gas flows into Germany at borders and LNG terminals (gross, incl. transit).", decimals=0, agg="sum"),
    Row("de_gas_norway", "gas", "Gas from Norway", "DE", "GWh/d", "entsog", "daily", _col("entsog", "entries_DE-LU", "NO"),
        "Direct pipeline entries from Norway (Europipe I/II, Norpipe).", decimals=0, agg="sum"),
]

DATA_GAPS = [
    {"label": "EPEX continuous intraday prices", "why": "Not published for free; EPEX sells them. Imbalance prices are shown as the closest free real-time price signal."},
    {"label": "TTF gas price", "why": "ICE Endex data is licensed. Only physical gas flows (ENTSOG) are free."},
    {"label": "EUA carbon price", "why": "Exchange data (ICE, EEX) is licensed; no free official daily series."},
    {"label": "French nuclear availability per unit", "why": "Published on EDF's REMIT platform and ENTSO-E; no free key-less API. The fleet total comes from éCO2mix."},
    {"label": "ENTSO-E Transparency Platform", "why": "Needs a personal API token (free, but not key-less as required for this site)."},
    {"label": "Historic weather-forecast vintages", "why": "Open-Meteo keeps them, but no v2 view needs them: the site observes, it does not score forecasts."},
]


# --------------------------------------------------------------------------- export
def _daily(s: pd.Series, agg: str) -> pd.Series:
    x = s.copy()
    x.index = x.index.tz_convert(config.TZ_DISPLAY)
    g = x.resample("D")
    if agg == "sum":
        # daily series (ENTSOG) are one value per day already: 'sum' keeps them, finer data are averaged
        return g.mean()
    return g.last() if agg == "last" else g.mean()


def _round(v, d):
    return None if v is None or not np.isfinite(v) else round(float(v), d)


def build(now: pd.Timestamp) -> tuple[dict, dict]:
    """Returns (datahub.json payload, datahub_detail.json payload)."""
    start = now - pd.Timedelta(days=400)
    rows, detail = [], {}
    for r in R:
        try:
            s = r.loader(start)
        except Exception:
            log.exception("datahub row %s failed", r.id)
            s = pd.Series(dtype="float64")
        src = SOURCES[r.source]
        base = {"id": r.id, "cat": r.cat, "label": r.label, "country": r.country, "unit": r.unit,
                "source": src[0], "license": src[1], "url": src[2], "freq": r.freq, "desc": r.desc,
                "decimals": r.decimals, "forecast": r.forecast}
        if s.empty:
            rows.append({**base, "value": None, "ts": None, "change": None, "spark": [], "last_ts": None})
            continue
        s = s[~s.index.duplicated(keep="last")].sort_index()
        past = s[s.index <= now]
        cur = past if len(past) else s
        if r.forecast and r.agg == "last" and not len(past):
            cur = s
        t_cur = cur.index[-1]
        v_cur = float(cur.iloc[-1])
        if r.agg == "last" and r.forecast:      # forecast tables: show the first day ahead
            ahead = s[s.index >= now.normalize()]
            if len(ahead):
                t_cur, v_cur = ahead.index[0], float(ahead.iloc[0])
        step = s.index.to_series().diff().median()
        lag = pd.Timedelta(days=7) if step >= pd.Timedelta(days=6) else pd.Timedelta(days=1)
        prev = s[s.index <= t_cur - lag]
        change = v_cur - float(prev.iloc[-1]) if len(prev) and (t_cur - prev.index[-1]) < lag * 1.5 else None
        d = _daily(past if len(past) else s, r.agg).dropna()
        spark = d.iloc[-30:]
        rows.append({**base, "value": _round(v_cur, r.decimals), "ts": t_cur, "change": _round(change, r.decimals),
                     "change_vs": "1 week" if lag.days == 7 else "24 h", "last_ts": s.index[-1],
                     "spark": [_round(v, r.decimals) for v in spark.values]})
        h = s[s.index >= now - pd.Timedelta(days=7)]
        if len(h) and step < pd.Timedelta(hours=1):
            h = h.resample("1h").mean().dropna()
        detail[r.id] = {"daily": {"date": [x.strftime("%Y-%m-%d") for x in d.index[-365:]],
                                  "v": [_round(v, r.decimals) for v in d.values[-365:]]},
                        "recent": {"ts": [t.isoformat() for t in h.index], "v": [_round(v, r.decimals) for v in h.values]}}
    sources = [{"key": k, "name": v[0], "license": v[1], "url": v[2],
                "rows": sum(1 for r in R if r.source == k)} for k, v in SOURCES.items()]
    return ({"categories": [{"id": c, "label": l} for c, l in CATEGORIES], "rows": rows, "gaps": DATA_GAPS,
             "sources": sources, "generated": now}, detail)
