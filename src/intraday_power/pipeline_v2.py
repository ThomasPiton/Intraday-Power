"""v2 orchestration: fetch every v2 source at its own cadence, and the one-off backfill.

``fetch_v2`` runs every 15 min (update.yml) and skips the sources that are not due yet
(``config.V2_CADENCE_MIN``); the time of the last successful run of each job is kept in
``data/raw/_fetch_state_v2.json`` (which lives in the Actions cache with the raw data). If a store
is empty (cache lost) the job rebuilds a recent window instead of the last few days.
"""
from __future__ import annotations

import json
import logging
import threading

import pandas as pd

from . import config, storage, weather
from .sources import eco2mix, elexon, entsog, open_meteo, peripheral, smard
from .sources import energy_charts as ec

log = logging.getLogger(__name__)
STATE = config.RAW / "_fetch_state_v2.json"
REBUILD_DAYS = 45


def _now() -> pd.Timestamp:
    return pd.Timestamp.now(tz="UTC")


def _state() -> dict:
    return json.loads(STATE.read_text()) if STATE.exists() else {}


def _save(st: dict) -> None:
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(st, indent=1))


def _since(source: str, series: str, days: int, now: pd.Timestamp) -> pd.Timestamp:
    """Start of the window to refetch: ``days`` back, or REBUILD_DAYS if the store is empty/stale."""
    last = storage.last_timestamp(source, series)
    start = now.normalize() - pd.Timedelta(days=days)
    if last is None or last < start - pd.Timedelta(days=3):
        log.warning("%s/%s empty or stale (last=%s): rebuilding %d days", source, series, last, REBUILD_DAYS)
        return now.normalize() - pd.Timedelta(days=REBUILD_DAYS)
    return start


def _d(ts: pd.Timestamp) -> str:
    return ts.tz_convert("Europe/Paris").strftime("%Y-%m-%d")


# --------------------------------------------------------------------------- jobs
def job_eco2mix(now):
    if storage.last_timestamp("eco2mix", "national", "consommation") is None:
        eco2mix.fetch_definitive((now - pd.Timedelta(days=REBUILD_DAYS + 60)).strftime("%Y-%m-%d"))
        eco2mix.fetch_realtime(None)
    else:
        eco2mix.fetch_realtime(2)


def job_ec_power(now, client):
    for z in config.V2_COUNTRIES:
        s = _since("energy_charts", f"power_{z}", 1, now)
        ec.fetch_power(client, z, _d(s), _d(now + pd.Timedelta(days=1)))


def job_ec_frequency(now, client):
    ec.fetch_frequency(client, _d(now - pd.Timedelta(hours=3)), _d(now))


def job_ec_cbpf(now, client):
    for z in config.V2_COUNTRIES:
        s = _since("energy_charts", f"cbpf_{z}", 2, now)
        ec.fetch_cbpf(client, z, _d(s), _d(now))


def job_weather_fc(now):
    open_meteo.fetch_forecast()


def job_weather_obs(now):
    for g in config.WEATHER_GROUPS:
        s = _since("weather", f"obs_{g}", 10, now)
        open_meteo.fetch_obs(g, s.strftime("%Y-%m-%d"), now.strftime("%Y-%m-%d"))
    if not weather.NORMALS_PATH.exists():
        weather.compute_normals()


def job_weather_ens(now):
    open_meteo.fetch_ensemble()


def job_smard(now):
    smard.fetch(_since("smard", "de", 8, now))


def job_elexon(now):
    end = now + pd.Timedelta(days=1)
    elexon.fetch_demand(_since("elexon", "demand", 2, now), end)
    elexon.fetch_system_prices(_since("elexon", "system_prices", 2, now), end)
    elexon.fetch_market_index(_since("elexon", "market_index", 2, now), end)
    elexon.fetch_availability()


def job_energinet(now):
    peripheral.fetch_energinet(_since("energinet", "imbalance_DK1", 2, now), now + pd.Timedelta(hours=1))


def job_elering(now):
    peripheral.fetch_elering(_since("elering", "prices", 3, now), now.normalize() + pd.Timedelta(days=2))


def job_ree(now):
    peripheral.fetch_ree(_since("ree", "prices", 3, now), now.normalize() + pd.Timedelta(days=2))


def job_entsog(now):
    for z in config.V2_COUNTRIES:
        entsog.fetch(z, _since("entsog", f"entries_{z}", 10, now), now)


def job_nve(now):
    peripheral.fetch_nve()


def job_capacity(now, client):
    for z in config.V2_COUNTRIES:
        ec.fetch_installed(client, z)
    eco2mix.fetch_capacity()
    # RTE moves "consolidated" data forward about once a month: refresh the recent definitive window.
    eco2mix.fetch_definitive((now - pd.Timedelta(days=120)).strftime("%Y-%m-%d"))


EC_JOBS = {"ec_power": job_ec_power, "ec_frequency": job_ec_frequency, "ec_cbpf": job_ec_cbpf,
           "capacity": job_capacity}
JOBS = {"eco2mix": job_eco2mix, "weather_fc": job_weather_fc, "weather_obs": job_weather_obs,
        "weather_ens": job_weather_ens, "smard": job_smard, "elexon": job_elexon, "energinet": job_energinet,
        "elering": job_elering, "ree": job_ree, "entsog": job_entsog, "nve": job_nve}


def fetch_v2(force: bool = False, only: list[str] | None = None, now: pd.Timestamp | None = None) -> dict:
    """Run the due jobs. Energy-Charts jobs share one rate-limited client and run in one thread;
    the other sources run in a second thread. Returns {job: "ok" | "failed" | "skipped"}."""
    now = now or _now()
    st = _state()
    lock = threading.Lock()
    result: dict[str, str] = {}

    def due(name: str) -> bool:
        if only and name not in only:
            return False
        if force or name not in st:
            return True
        return now - pd.Timestamp(st[name]) >= pd.Timedelta(minutes=config.V2_CADENCE_MIN[name] - 2)

    def run(name, fn, *args):
        if not due(name):
            result[name] = "skipped"
            return
        try:
            fn(now, *args)
            with lock:
                st[name] = now.isoformat()
            result[name] = "ok"
        except Exception:  # one failed source must not stop the others
            log.exception("v2 job %s failed", name)
            result[name] = "failed"

    client = ec.EnergyChartsClient()
    t = threading.Thread(target=lambda: [run(n, f, client) for n, f in EC_JOBS.items()])
    t.start()
    for n, f in JOBS.items():
        run(n, f)
    t.join()
    _save(st)
    log.info("v2 fetch: %s", {k: v for k, v in result.items() if v != "skipped"})
    return result


# --------------------------------------------------------------------------- backfill
def backfill_v2(weather_history: bool = True) -> None:
    """Full v2 history: power data since V2_HISTORY_START, peripheral rows V2_PERIPHERAL_DAYS,
    weather grids since WEATHER_GRID_START and city temperatures since WEATHER_TEMP_START."""
    now = _now()
    p0 = now.normalize() - pd.Timedelta(days=config.V2_PERIPHERAL_DAYS)
    client = ec.EnergyChartsClient()

    def ec_part():
        for z in config.V2_COUNTRIES:
            ec.fetch_power(client, z, config.V2_HISTORY_START, _d(now + pd.Timedelta(days=1)))
            ec.fetch_installed(client, z)
            ec.fetch_cbpf(client, z, _d(p0), _d(now))
        ec.fetch_frequency(client, _d(now - pd.Timedelta(days=1)), _d(now))

    def weather_part():
        if not weather_history:
            return
        for g, start in (("wind", config.WEATHER_GRID_START), ("solar", config.WEATHER_GRID_START),
                         ("temp", config.WEATHER_TEMP_START)):
            open_meteo.fetch_obs(g, start, now.strftime("%Y-%m-%d"))
        weather.compute_normals()
        open_meteo.fetch_forecast()
        open_meteo.fetch_ensemble()

    threads = [threading.Thread(target=ec_part), threading.Thread(target=weather_part)]
    for t in threads:
        t.start()
    steps = [
        lambda: eco2mix.fetch_definitive(config.V2_HISTORY_START), lambda: eco2mix.fetch_realtime(None),
        eco2mix.fetch_capacity, lambda: smard.fetch(config.V2_HISTORY_START),
        lambda: elexon.fetch_demand(p0, now + pd.Timedelta(days=1)),
        lambda: elexon.fetch_system_prices(p0, now + pd.Timedelta(days=1)),
        lambda: elexon.fetch_market_index(p0, now + pd.Timedelta(days=1)), elexon.fetch_availability,
        lambda: peripheral.fetch_energinet(p0, now), peripheral.fetch_nve,
        lambda: peripheral.fetch_elering(p0, now.normalize() + pd.Timedelta(days=2)),
        lambda: peripheral.fetch_ree(p0, now.normalize() + pd.Timedelta(days=2)),
        *[(lambda z=z: entsog.fetch(z, p0, now)) for z in config.V2_COUNTRIES],
    ]
    for fn in steps:
        try:
            fn()
        except Exception:
            log.exception("v2 backfill step failed")
    for t in threads:
        t.join()
    st = _state()
    for name in config.V2_CADENCE_MIN:
        st[name] = now.isoformat()
    _save(st)
