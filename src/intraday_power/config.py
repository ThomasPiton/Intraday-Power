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
