"""Throwaway probe of the v2 data sources (Plan V2, Step 0).

One small request per source/endpoint: HTTP status, latency, auth needed or not,
first/last timestamp, a sample of fields and the licence when the API returns one.
Writes scripts/probe_report_v2.json.

Usage: python scripts/probe_sources_v2.py [name-substring ...]
"""
from __future__ import annotations

import json
import sys
import time
from datetime import date, timedelta
from pathlib import Path

import requests

OUT = Path(__file__).with_name("probe_report_v2.json")
UA = {"User-Agent": "intraday-power-probe (github.com/ThomasPiton/Intraday-Power)"}
TODAY = date.today()
D = lambda n: (TODAY - timedelta(days=n)).isoformat()  # noqa: E731

PARIS, BERLIN = (48.85, 2.35), (52.52, 13.40)
LATS = f"{PARIS[0]},{BERLIN[0]}"
LONS = f"{PARIS[1]},{BERLIN[1]}"
OM_VARS = "temperature_2m,wind_speed_10m,wind_speed_100m,wind_gusts_10m,shortwave_radiation,direct_radiation,diffuse_radiation,cloud_cover"
ODRE = "https://odre.opendatasoft.com/api/explore/v2.1"

# (name, url, params)
PROBES: list[tuple[str, str, dict]] = [
    # ---------------------------------------------------------------- Open-Meteo
    ("open_meteo/forecast_16d", "https://api.open-meteo.com/v1/forecast",
     {"latitude": LATS, "longitude": LONS, "hourly": OM_VARS, "forecast_days": 16, "timezone": "UTC"}),
    ("open_meteo/ensemble_ecmwf", "https://ensemble-api.open-meteo.com/v1/ensemble",
     {"latitude": PARIS[0], "longitude": PARIS[1], "hourly": "temperature_2m,wind_speed_100m,shortwave_radiation",
      "models": "ecmwf_ifs025", "forecast_days": 15}),
    ("open_meteo/ensemble_icon", "https://ensemble-api.open-meteo.com/v1/ensemble",
     {"latitude": BERLIN[0], "longitude": BERLIN[1], "hourly": "temperature_2m,wind_speed_100m,shortwave_radiation",
      "models": "icon_seamless", "forecast_days": 7}),
    ("open_meteo/historical_forecast_2021", "https://historical-forecast-api.open-meteo.com/v1/forecast",
     {"latitude": PARIS[0], "longitude": PARIS[1], "hourly": OM_VARS, "start_date": "2021-03-01", "end_date": "2021-03-02"}),
    ("open_meteo/historical_forecast_recent", "https://historical-forecast-api.open-meteo.com/v1/forecast",
     {"latitude": PARIS[0], "longitude": PARIS[1], "hourly": OM_VARS, "start_date": D(3), "end_date": D(1)}),
    ("open_meteo/previous_runs", "https://previous-runs-api.open-meteo.com/v1/forecast",
     {"latitude": PARIS[0], "longitude": PARIS[1], "hourly": "temperature_2m,temperature_2m_previous_day1,temperature_2m_previous_day5",
      "past_days": 3, "forecast_days": 1}),
    ("open_meteo/era5_1940", "https://archive-api.open-meteo.com/v1/archive",
     {"latitude": PARIS[0], "longitude": PARIS[1], "hourly": OM_VARS, "models": "era5",
      "start_date": "1940-01-01", "end_date": "1940-01-02"}),
    ("open_meteo/era5_latest", "https://archive-api.open-meteo.com/v1/archive",
     {"latitude": PARIS[0], "longitude": PARIS[1], "hourly": OM_VARS, "models": "era5",
      "start_date": D(10), "end_date": D(0)}),
    ("open_meteo/era5_multi_point_year", "https://archive-api.open-meteo.com/v1/archive",
     {"latitude": LATS, "longitude": LONS, "hourly": "temperature_2m,wind_speed_100m", "models": "era5",
      "start_date": D(400), "end_date": D(35)}),
    # ---------------------------------------------------------------- ODRÉ / éCO2mix
    ("odre/catalog_eco2mix", f"{ODRE}/catalog/datasets", {"where": 'search("eco2mix")', "limit": 50, "select": "dataset_id"}),
    ("odre/catalog_nucleaire", f"{ODRE}/catalog/datasets", {"where": 'search("nucleaire")', "limit": 50, "select": "dataset_id"}),
    ("odre/catalog_parc", f"{ODRE}/catalog/datasets", {"where": 'search("parc")', "limit": 50, "select": "dataset_id"}),
    ("odre/eco2mix_tr_last", f"{ODRE}/catalog/datasets/eco2mix-national-tr/records",
     {"order_by": "date_heure desc", "limit": 2, "where": "consommation is not null"}),
    ("odre/eco2mix_tr_first", f"{ODRE}/catalog/datasets/eco2mix-national-tr/records",
     {"order_by": "date_heure asc", "limit": 1}),
    ("odre/eco2mix_cons_def_first", f"{ODRE}/catalog/datasets/eco2mix-national-cons-def/records",
     {"order_by": "date_heure asc", "limit": 1}),
    ("odre/eco2mix_cons_def_last", f"{ODRE}/catalog/datasets/eco2mix-national-cons-def/records",
     {"order_by": "date_heure desc", "limit": 1}),
    # ---------------------------------------------------------------- Energy-Charts (new endpoints)
    ("energy_charts/installed_power_de", "https://api.energy-charts.info/installed_power",
     {"country": "de", "time_step": "monthly", "installation_decommission": "false"}),
    ("energy_charts/installed_power_fr", "https://api.energy-charts.info/installed_power",
     {"country": "fr", "time_step": "yearly", "installation_decommission": "false"}),
    ("energy_charts/cbpf_de", "https://api.energy-charts.info/cbpf", {"country": "de", "start": D(2), "end": D(1)}),
    ("energy_charts/frequency", "https://api.energy-charts.info/frequency",
     {"region": "DE-Freiburg", "start": D(1), "end": D(1)}),
    ("energy_charts/public_power_fr", "https://api.energy-charts.info/public_power", {"country": "fr", "start": D(1), "end": D(1)}),
    ("energy_charts/total_power_fr", "https://api.energy-charts.info/total_power", {"country": "fr", "start": D(1), "end": D(1)}),
    # ---------------------------------------------------------------- SMARD (filter 410 = load, 411 = load forecast, 4169 = DE-LU price)
    ("smard/index_410_quarterhour", "https://www.smard.de/app/chart_data/410/DE/index_quarterhour.json", {}),
    ("smard/index_411_quarterhour", "https://www.smard.de/app/chart_data/411/DE/index_quarterhour.json", {}),
    ("smard/index_4169_hour", "https://www.smard.de/app/chart_data/4169/DE-LU/index_hour.json", {}),
    # ---------------------------------------------------------------- Elexon Insights
    ("elexon/demand_outturn", "https://data.elexon.co.uk/bmrs/api/v1/demand/outturn",
     {"settlementDateFrom": D(2), "settlementDateTo": D(1), "format": "json"}),
    ("elexon/system_prices", f"https://data.elexon.co.uk/bmrs/api/v1/balancing/settlement/system-prices/{D(2)}", {"format": "json"}),
    ("elexon/market_index", "https://data.elexon.co.uk/bmrs/api/v1/balancing/pricing/market-index",
     {"from": f"{D(2)}T00:00Z", "to": f"{D(1)}T00:00Z", "format": "json"}),
    ("elexon/remit", "https://data.elexon.co.uk/bmrs/api/v1/remit/list/by-publish",
     {"from": f"{D(1)}T00:00Z", "to": f"{D(0)}T00:00Z", "format": "json"}),
    ("elexon/availability_uou2t14d", "https://data.elexon.co.uk/bmrs/api/v1/datasets/UOU2T14D", {"format": "json"}),
    ("elexon/availability_fou2t14d", "https://data.elexon.co.uk/bmrs/api/v1/datasets/FOU2T14D", {"format": "json"}),
    # ---------------------------------------------------------------- Energinet
    ("energinet/imbalance_price", "https://api.energidataservice.dk/dataset/ImbalancePrice", {"limit": 3}),
    ("energinet/co2_emis", "https://api.energidataservice.dk/dataset/CO2Emis", {"limit": 3}),
    ("energinet/co2_emis_prog", "https://api.energidataservice.dk/dataset/CO2EmisProg", {"limit": 3}),
    # ---------------------------------------------------------------- NVE reservoirs
    ("nve/magasin_siste_uke", "https://biapi.nve.no/magasinstatistikk/api/Magasinstatistikk/HentOffentligDataSisteUke", {}),
    ("nve/magasin_all", "https://biapi.nve.no/magasinstatistikk/api/Magasinstatistikk/HentOffentligData", {}),
    ("nve/magasin_min_max_median", "https://biapi.nve.no/magasinstatistikk/api/Magasinstatistikk/HentOffentligDataMinMaxMedian", {}),
    # ---------------------------------------------------------------- ENTSOG
    ("entsog/physical_flow", "https://transparency.entsog.eu/api/v1/operationaldatas",
     {"indicator": "Physical Flow", "periodType": "day", "from": D(3), "to": D(1), "limit": 5, "timezone": "UCT"}),
    ("entsog/nomination", "https://transparency.entsog.eu/api/v1/operationaldatas",
     {"indicator": "Nomination", "periodType": "day", "from": D(3), "to": D(1), "limit": 5, "timezone": "UCT"}),
    # ---------------------------------------------------------------- Elering, REE
    ("elering/nps_price", "https://dashboard.elering.ee/api/nps/price",
     {"start": f"{D(2)}T00:00:00.000Z", "end": f"{D(1)}T00:00:00.000Z"}),
    ("ree/day_ahead_prices", "https://apidatos.ree.es/en/datos/mercados/precios-mercados-tiempo-real",
     {"start_date": f"{D(2)}T00:00", "end_date": f"{D(1)}T23:59", "time_trunc": "hour"}),
    # ---------------------------------------------------------------- data gaps (expected to fail or need a key)
    ("gap/entsoe_no_token", "https://web-api.tp.entsoe.eu/api", {"documentType": "A44", "in_Domain": "10YFR-RTE------C"}),
]


def summarise(body) -> dict:
    """Shape of a JSON body: top-level keys, list length, first and last element (truncated)."""
    out: dict = {"type": type(body).__name__}
    if isinstance(body, dict):
        out["keys"] = list(body)[:25]
        for k in ("license", "licence", "attribution", "total_count", "deprecated", "unit"):
            if k in body:
                out[k] = body[k]
        lst = next((body[k] for k in ("results", "data", "records", "operationalDatas", "timestamps", "items")
                    if isinstance(body.get(k), list)), None)
        if lst is None and isinstance(body.get("hourly"), dict):
            t = body["hourly"].get("time", [])
            out["hourly_vars"] = list(body["hourly"])[:40]
            out["n"], out["first"], out["last"] = len(t), t[:1], t[-1:]
            nn = [v for v in body["hourly"].get("temperature_2m", []) if v is not None]
            out["temperature_non_null"] = len(nn)
            return out
        if lst is not None:
            out["n"], out["first"], out["last"] = len(lst), lst[:1], lst[-1:]
    elif isinstance(body, list):
        out["n"], out["first"], out["last"] = len(body), body[:1], body[-1:]
        if body and isinstance(body[0], dict) and "hourly" in body[0]:
            out["locations"] = len(body)
            out["first"] = summarise(body[0])
            out["last"] = None
    return out


def probe(name: str, url: str, params: dict) -> dict:
    t0 = time.time()
    try:
        r = requests.get(url, params=params, headers=UA, timeout=90)
    except requests.RequestException as exc:
        return {"error": str(exc)[:300]}
    res = {"http": r.status_code, "seconds": round(time.time() - t0, 2),
           "bytes": len(r.content), "content_type": r.headers.get("content-type"),
           "rate_headers": {k: v for k, v in r.headers.items() if "ratelimit" in k.lower() or k.lower() == "retry-after"}}
    try:
        res["summary"] = summarise(r.json())
    except ValueError:
        res["text"] = r.text[:400]
    return res


if __name__ == "__main__":
    filters = sys.argv[1:]
    report = json.loads(OUT.read_text()) if OUT.exists() else {}
    for name, url, params in PROBES:
        if filters and not any(f in name for f in filters):
            continue
        res = probe(name, url, params)
        report[name] = {"url": url, "params": params, **res}
        print(f"{name:40s} {res.get('http', 'ERR')!s:>4} {json.dumps(res.get('summary', res.get('text', res.get('error'))), default=str)[:600]}",
              flush=True)
        time.sleep(0.5)
    OUT.write_text(json.dumps(report, indent=1, default=str))
    print("wrote", OUT)
