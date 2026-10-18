"""Static configuration: zones, data-source ids, strategy parameters, paths.

Every dataset id and field name below was confirmed by ``scripts/probe_sources.py``
on 2026-10-17 (see DECISIONS.md, "Step 0").
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pandas as pd

# --------------------------------------------------------------------------- paths
ROOT = Path(__file__).resolve().parents[2]
DATA = ROOT / "data"
RAW = DATA / "raw"
MODELS = DATA / "models"
JOURNAL = DATA / "journal"
BACKTEST = DATA / "backtest"
STATE = DATA / "state"
DOCS_DATA = ROOT / "docs" / "data"

TZ_DISPLAY = "Europe/Paris"
QH = pd.Timedelta(minutes=15)

# --------------------------------------------------------------------------- zones
@dataclass(frozen=True)
class Zone:
    code: str          # bidding-zone label used everywhere in the app
    bzn: str           # Energy-Charts /v2/price `bzn`
    country: str       # Energy-Charts /v2/public_power[_forecast] `country`
    tz: str


ZONES: dict[str, Zone] = {
    "FR": Zone("FR", "FR", "fr", "Europe/Paris"),
    "DE-LU": Zone("DE-LU", "DE-LU", "de", "Europe/Berlin"),
    "BE": Zone("BE", "BE", "be", "Europe/Brussels"),
    "NL": Zone("NL", "NL", "nl", "Europe/Amsterdam"),
    # Energy-Charts publishes generation/forecasts for Denmark as a whole (DK1+DK2);
    # we map country "dk" to the DK1 price zone and say so in Methodology.
    "DK1": Zone("DK1", "DK1", "dk", "Europe/Copenhagen"),
}

# --------------------------------------------------------------------------- Energy-Charts
EC_BASE = "https://api.energy-charts.info"
EC_MIN_GAP_S = 31.0            # 2 req/min/endpoint documented; burst ignored on purpose
EC_PRODUCTION_TYPES = ("solar", "wind_onshore", "wind_offshore")   # "wind" returns 404
EC_FORECAST_TYPES = {"day-ahead": "da", "intraday": "id"}
EC_HISTORY_START = "2015-01-01"
# Zones where Energy-Charts' "intraday" forecast is a copy of the day-ahead one (checked 2026-10-17:
# |ID - DA| > 0.5 MW on < 0.5% of QHs since 2024), so no revision can be measured. Shown as "n/a".
REVISION_UNAVAILABLE = {
    "NL": "Energy-Charts' NL intraday forecast is identical to the day-ahead forecast (no intraday update published), so no revision can be measured.",
}
EC_PRICE_LICENSE = "CC BY 4.0 (creativecommons.org/licenses/by/4.0) from Bundesnetzagentur | SMARD.de"

# --------------------------------------------------------------------------- Elia
ELIA_BASE = "https://opendata.elia.be/api/explore/v2.1"
ELIA_HISTORY_START = "2024-05-22"   # MARI go-live; ods134/133 start 2024-05-21T22:00Z


@dataclass(frozen=True)
class EliaDataset:
    hist: str | None          # historical dataset (D-1 latency)
    nrt: str | None           # near-real-time dataset (today only)
    time_field: str
    fields: tuple[str, ...]


ELIA = {
    # Imbalance price per quarter-hour. NRT ods162 appears 1-7 min after the QH ends.
    "imb_qh": EliaDataset("ods134", "ods162", "datetime",
                          ("datetime", "qualitystatus", "systemimbalance", "alpha",
                           "marginalincrementalprice", "marginaldecrementalprice", "imbalanceprice")),
    # Imbalance price per minute (cumulative within the QH). NRT ods161 lags ~2 min.
    "imb_min": EliaDataset("ods133", "ods161", "datetime",
                           ("datetime", "quarterhour", "systemimbalance", "imbalanceprice")),
    # Current system imbalance per minute: NRT only (Desk). QH SI comes from imb_qh.
    "si_min": EliaDataset(None, "ods169", "datetime", ("datetime", "systemimbalance", "ace")),
    # Elia SI forecast for the next QH: one vintage per minute since 2023-10-06,
    # with its publication time (predictiontimeutc) -> usable as-of in backtest.
    "si_fc": EliaDataset("ods147", "ods147", "predictiontimeutc",
                         ("predictiontimeutc", "predictions_forecastedtimeutc",
                          "predictions_silinearregressionforecast",
                          "predictions_categoricalsiprediction_from_minus_inf_to_minus_400",
                          "predictions_categoricalsiprediction_from_minus_400_to_minus_200",
                          "predictions_categoricalsiprediction_from_minus_200_to_0",
                          "predictions_categoricalsiprediction_from_0_to_200",
                          "predictions_categoricalsiprediction_from_200_to_400",
                          "predictions_categoricalsiprediction_from_400_to_inf")),
}
# Elia sign convention (checked empirically): SI < 0 => system SHORT => high imbalance price.
# Elia announced a new imbalance-price formula from 2026-11-24 10:00 CET (subject to CREG
# approval; simulated today in ods211/ods212). Stats are split at this date.
IMBALANCE_REGIME_CHANGE_UTC = pd.Timestamp("2026-11-24T09:00Z")

# --------------------------------------------------------------------------- bots
COST_EUR_PER_MWH = 0.5
# Information protocol (see Methodology): the decision for QH Q is taken as of
# d(Q) = Q_start - DECISION_LEAD. Quarter-hour real-time data are usable for QHs <= Q-RT_LAG_QH
# (published 1-7 min after their end, i.e. well before d(Q)). Elia SI-forecast vintages are
# usable if predictiontime <= d(Q).
DECISION_LEAD = pd.Timedelta(minutes=20)
RT_LAG_QH = 3
# Live: a run at time t decides QH Q only if Q_start - LIVE_WINDOW_OPEN <= t <= Q_start - LIVE_WINDOW_CLOSE,
# so that every vintage needed as-of d(Q) has already been published (minute data lag ~2 min).
LIVE_WINDOW_OPEN = pd.Timedelta(minutes=17)
LIVE_WINDOW_CLOSE = pd.Timedelta(minutes=2)

BOTS = ("rule", "ml")
CALIBRATION_MONTHS = 3
# Rule-bot threshold, calibrated ONCE on 2024-05-22 -> 2024-08-22 (max total P&L over 0..500 MW, see
# data/state/rule_calibration.csv; run on 2026-10-17) by `python -m intraday_power
# calibrate` and frozen here (MW). Do not edit after the backtest has been published.
RULE_THRESHOLD_MW: float | None = 0.0

ML_PROBA_THRESHOLD = 0.6
ML_PARAMS = dict(n_estimators=200, learning_rate=0.05, num_leaves=15, min_child_samples=200,
                 subsample=0.8, subsample_freq=1, colsample_bytree=0.8, reg_lambda=1.0,
                 random_state=42, verbose=-1)

# First QH (UTC) that belongs to the live period. Set after the first scheduled run on GitHub.
LIVE_START_UTC: pd.Timestamp | None = None

# --------------------------------------------------------------------------- battery
BATTERY_POWER_MW = 1.0
BATTERY_ENERGY_MWH = 2.0
BATTERY_RTE = 0.88
BATTERY_MAX_CYCLES = 2.0
BATTERY_SOC0_MWH = 1.0
BATTERY_START = "2023-01-01"

# --------------------------------------------------------------------------- front-end
STALE_AFTER_MIN = 45
REPO_URL = "https://github.com/ThomasPiton/Intraday-Power"

# =========================================================================== v2: weather -> demand/supply -> prices
# Countries observed in v2 (PLAN_V2 §1). Keys match ZONES so prices/battery share the same code.
V2_COUNTRIES = ("FR", "DE-LU")
V2_HISTORY_START = "2023-01-01"         # power data backfill (battery league starts here too)
V2_PERIPHERAL_DAYS = 120                # Data Hub rows from non-core countries keep ~4 months
WEATHER_TEMP_START = "2016-01-01"       # temperature history: scatter + 10-year normals
WEATHER_GRID_START = "2024-10-01"       # wind/solar grids: power curves need ~2 years
NORMALS_YEARS = (2016, 2025)            # inclusive; see DECISIONS "V2 weather"
RTE_GW_PER_DEGC = 2.4                   # RTE rule of thumb, winter, France (shown as such)


@dataclass(frozen=True)
class WeatherPoint:
    id: str
    country: str        # "FR" | "DE-LU"
    lat: float
    lon: float
    weight: float = 1.0  # population (millions, rounded functional urban area) for temperature


# Temperature: 10 cities per country, weighted by rounded Eurostat functional-urban-area population.
TEMP_POINTS = [WeatherPoint(f"T-{c}", "FR", la, lo, w) for c, la, lo, w in [
    ("Paris", 48.86, 2.35, 12.9), ("Lyon", 45.76, 4.84, 2.3), ("Marseille", 43.30, 5.37, 1.9),
    ("Toulouse", 43.60, 1.44, 1.4), ("Lille", 50.63, 3.06, 1.2), ("Bordeaux", 44.84, -0.58, 1.3),
    ("Nantes", 47.22, -1.55, 1.0), ("Strasbourg", 48.57, 7.75, 0.8), ("Nice", 43.70, 7.27, 1.0),
    ("Rennes", 48.11, -1.68, 0.75)]] + [WeatherPoint(f"T-{c}", "DE-LU", la, lo, w) for c, la, lo, w in [
    ("Berlin", 52.52, 13.40, 5.3), ("Hamburg", 53.55, 9.99, 3.3), ("Muenchen", 48.14, 11.58, 2.9),
    ("Koeln", 50.94, 6.96, 2.0), ("Frankfurt", 50.11, 8.68, 2.6), ("Stuttgart", 48.78, 9.18, 2.8),
    ("Duesseldorf", 51.23, 6.78, 1.5), ("Leipzig", 51.34, 12.37, 1.0), ("Dortmund", 51.51, 7.47, 1.2),
    ("Bremen", 53.08, 8.80, 1.3)]]

# Wind: ~15 points per country on the main onshore/offshore wind areas. Simple mean, no learned weights.
WIND_POINTS = [WeatherPoint(f"W-{n}", "DE-LU", la, lo) for n, la, lo in [
    ("NorthSea-W", 54.35, 6.30), ("NorthSea-E", 54.75, 7.05), ("Baltic", 54.60, 14.00),
    ("SchleswigH-N", 54.55, 9.10), ("SchleswigH-W", 54.05, 9.05), ("EastFrisia", 53.45, 7.55),
    ("LowerSaxony-W", 52.85, 8.40), ("LowerSaxony-E", 52.85, 10.40), ("Mecklenburg", 53.85, 12.30),
    ("Uckermark", 53.20, 13.80), ("Brandenburg-S", 52.00, 13.60), ("SaxonyAnhalt", 51.95, 11.40),
    ("Westphalia", 51.75, 8.40), ("Thuringia", 50.95, 10.10), ("Rhineland", 49.80, 7.80)]] + [
    WeatherPoint(f"W-{n}", "FR", la, lo) for n, la, lo in [
    ("StNazaire-off", 47.15, -2.55), ("Fecamp-off", 49.90, 0.20), ("StBrieuc-off", 48.85, -2.55),
    ("Somme", 49.95, 2.30), ("PasDeCalais", 50.40, 2.60), ("Aisne", 49.55, 3.60), ("Marne", 48.95, 4.10),
    ("Aube", 48.35, 4.10), ("Meuse", 49.00, 5.40), ("Beauce", 48.10, 1.70), ("Brittany", 48.20, -2.80),
    ("Vendee", 46.70, -1.10), ("Poitou", 46.30, 0.20), ("Aude", 43.10, 2.80), ("Drome", 44.40, 4.75)]]

# Solar: ~10 points per country on the sunniest / most equipped areas. Simple mean.
SOLAR_POINTS = [WeatherPoint(f"S-{n}", "DE-LU", la, lo) for n, la, lo in [
    ("Freiburg", 48.00, 7.85), ("Stuttgart", 48.80, 9.20), ("Muenchen", 48.15, 11.60),
    ("Augsburg", 48.37, 10.90), ("Regensburg", 49.00, 12.10), ("Nuernberg", 49.45, 11.08),
    ("Wuerzburg", 49.80, 9.95), ("Mannheim", 49.50, 8.50), ("Brandenburg", 52.30, 13.60),
    ("Leipzig", 51.35, 12.40)]] + [WeatherPoint(f"S-{n}", "FR", la, lo) for n, la, lo in [
    ("Landes", 44.30, -0.80), ("Toulouse", 43.60, 1.45), ("Montpellier", 43.60, 3.90),
    ("Provence", 43.60, 5.40), ("Var", 43.40, 6.20), ("Drome", 44.70, 4.90), ("Charente", 45.80, 0.20),
    ("Nantes", 47.20, -1.50), ("Centre", 47.40, 1.70), ("GrandEst", 48.70, 6.20)]]

WEATHER_GROUPS = {
    "temp": (TEMP_POINTS, ("temperature_2m",)),
    "wind": (WIND_POINTS, ("wind_speed_100m", "wind_direction_100m", "wind_speed_10m")),
    "solar": (SOLAR_POINTS, ("shortwave_radiation", "direct_radiation", "diffuse_radiation", "cloud_cover")),
}
# Ensemble (ECMWF IFS 0.25° ENS, 50 members + control, 15 days): one variable per group.
WEATHER_ENSEMBLE_VAR = {"temp": "temperature_2m", "wind": "wind_speed_100m", "solar": "shortwave_radiation"}
OPEN_METEO_ATTRIBUTION = "Weather data by Open-Meteo.com (CC BY 4.0), ERA5 reanalysis by Copernicus/ECMWF"

# Fetch cadences (minutes) for the v2 job; it runs every 15 min and skips sources not due yet.
V2_CADENCE_MIN = {
    "eco2mix": 15, "energinet": 60, "ec_power": 60, "ec_frequency": 60, "weather_fc": 60,
    "smard": 60, "elexon": 60, "elering": 360, "ree": 360, "entsog": 360, "weather_obs": 360,
    "weather_ens": 720, "ec_cbpf": 360, "nve": 1440, "capacity": 10080,
}
