"""v2 views: descriptive aggregates for the Today / Weather→Demand / Weather→Supply / Price Drivers pages.

"Observe, don't model": every number is either a stored observation, an official forecast, or a
transparent statistic on them (means, OLS slopes, binned medians, counts). Definitions are written
next to each function and repeated in docs/content/explain.json.
"""
from __future__ import annotations

import logging

import holidays
import numpy as np
import pandas as pd

from . import config, storage, weather
from .sources import eco2mix, open_meteo

log = logging.getLogger(__name__)
PARIS = config.TZ_DISPLAY
COUNTRIES = config.V2_COUNTRIES
LABEL = {"FR": "France", "DE-LU": "Germany"}
SHORT = {"FR": "FR", "DE-LU": "DE"}
TZ = {"FR": "Europe/Paris", "DE-LU": "Europe/Berlin"}
HEATING_BELOW_C, COOLING_ABOVE_C = 15.0, 20.0
DUNKEL_CF, DUNKEL_MIN_DAYS = 0.05, 2


def _hourly(df: pd.DataFrame) -> pd.DataFrame:
    return df.resample("1h").mean() if len(df) else df


def _qh_series(source: str, series: str, col: str, start, end) -> pd.Series:
    d = storage.read(source, series, start, end)
    if d.empty or col not in d:
        return pd.Series(dtype="float64")
    return d.set_index("ts")[col].sort_index()


# --------------------------------------------------------------------------- weather indices
def weather_hourly(start, end) -> dict[str, pd.DataFrame]:
    """{var: ts x country} from reanalysis, extended with the latest forecast after the last observation."""
    fc = open_meteo.read_latest("forecast")
    out = {}
    for name, group, var in (("temp", "temp", "temperature_2m"), ("wind100", "wind", "wind_speed_100m"),
                             ("ghi", "solar", "shortwave_radiation"), ("cloud", "solar", "cloud_cover")):
        obs = weather.obs_index(group, var, start, end)
        f = weather.country_index(fc[fc["group"] == group], group, var) if len(fc) else pd.DataFrame()
        if len(f):
            f = f[(f.index >= pd.Timestamp(start)) & (f.index < pd.Timestamp(end))]
            if len(obs):
                f = f[f.index > obs.dropna(how="all").index.max()]
            obs = pd.concat([obs, f]).sort_index() if len(obs) else f
        out[name] = obs
    return out


# --------------------------------------------------------------------------- power, per country
def _installed(zone: str, index: pd.DatetimeIndex) -> pd.DataFrame:
    """Installed wind and solar (GW) aligned on ``index`` (step function, last known value carried)."""
    inst = storage.read("energy_charts", f"installed_{zone}")
    if inst.empty:
        return pd.DataFrame(index=index, columns=["wind_cap", "solar_cap"], dtype="float64")
    inst = inst.set_index("ts").sort_index()
    wind = inst.get("wind_onshore", 0).fillna(0) + inst.get("wind_offshore", 0).fillna(0)
    solar = inst["solar_ac"] if "solar_ac" in inst else inst.get("solar")
    cap = pd.DataFrame({"wind_cap": wind, "solar_cap": solar})
    full = cap.reindex(cap.index.union(index)).ffill()
    return full.reindex(index)


def power_hourly(zone: str, start, end) -> pd.DataFrame:
    """Hourly frame (MW unless stated): load, load_fc_da, load_fc_id, wind, solar, nuclear, hydro, gas,
    coal, residual (= load - wind - solar), price (EUR/MWh), temp/wind100/ghi/cloud, wind_cf, solar_cf."""
    start, end = pd.Timestamp(start), pd.Timestamp(end)
    if zone == "FR":
        e = eco2mix.read(start, end)
        if e.empty:
            df = pd.DataFrame()
        else:
            h = _hourly(e.drop(columns=["nature"]))
            df = pd.DataFrame({"load": h["consommation"], "load_fc_da": h["prevision_j1"], "load_fc_id": h["prevision_j"],
                               "wind": h["eolien"], "solar": h["solaire"], "nuclear": h["nucleaire"],
                               "hydro": h["hydraulique"], "gas": h["gaz"], "coal": h["charbon"],
                               "net_imports": h["ech_physiques"], "co2": h["taux_co2"]})
    else:
        p = storage.read("energy_charts", f"power_{zone}", start, end)
        s = storage.read("smard", "de", start, end)
        h = _hourly(p.set_index("ts")) if len(p) else pd.DataFrame()
        hs = _hourly(s.set_index("ts")) if len(s) else pd.DataFrame()
        g = lambda c: h[c] if c in h else 0.0  # noqa: E731
        df = pd.DataFrame(index=h.index.union(hs.index))
        if len(h):
            df["wind"] = g("wind_onshore") + g("wind_offshore")
            df["solar"] = h.get("solar")
            df["nuclear"] = h.get("nuclear")
            df["hydro"] = g("hydro_run_of_river") + g("hydro_water_reservoir") + g("hydro_pumped_storage")
            df["gas"] = h.get("fossil_gas")
            df["coal"] = g("fossil_brown_coal_lignite") + g("fossil_hard_coal")
            df["lignite"] = h.get("fossil_brown_coal_lignite")
            df["net_imports"] = h.get("cross_border_electricity_trading")
        load_ec = h.get("load") if len(h) else None
        df["load"] = hs["load"].reindex(df.index) if "load" in hs else load_ec
        if load_ec is not None:
            df["load"] = df["load"].fillna(load_ec.reindex(df.index))
        df["load_fc_da"] = hs["load_forecast"].reindex(df.index) if "load_forecast" in hs else np.nan
        df["load_fc_id"] = np.nan
    if df.empty:
        return df
    df = df[(df.index >= start) & (df.index < end)]
    px = _qh_series("energy_charts", f"price_{zone}", "price", start, end)
    df = df.reindex(df.index.union(px.resample("1h").mean().index if len(px) else []))
    df["price"] = px.resample("1h").mean() if len(px) else np.nan
    df["residual"] = df["load"] - df["wind"].fillna(0) - df["solar"].fillna(0)
    df.loc[df["load"].isna() | (df["wind"].isna() & df["solar"].isna()), "residual"] = np.nan
    w = weather_hourly(start, end)
    for k, v in w.items():
        df[k] = v[zone].reindex(df.index) if zone in v else np.nan
    cap = _installed(zone, df.index)
    df["wind_cf"] = df["wind"] / 1000 / cap["wind_cap"]
    df["solar_cf"] = df["solar"] / 1000 / cap["solar_cap"]
    df["res_cf"] = (df["wind"].fillna(0) + df["solar"].fillna(0)) / 1000 / (cap["wind_cap"] + cap["solar_cap"])
    df.loc[df["wind"].isna() & df["solar"].isna(), "res_cf"] = np.nan
    return df.sort_index()


# --------------------------------------------------------------------------- helpers
def season(month: int) -> str:
    """Three classes (not four): a 4-colour scatter fails the colour-vision checks, see DECISIONS."""
    return {12: "Winter", 1: "Winter", 2: "Winter", 6: "Summer", 7: "Summer", 8: "Summer"}.get(month, "Spring/Autumn")


def working_days(idx: pd.DatetimeIndex, zone: str) -> np.ndarray:
    hol = holidays.France() if zone == "FR" else holidays.Germany()
    return np.array([(d.weekday() < 5) and (d.date() not in hol) for d in idx])


def ols(x: np.ndarray, y: np.ndarray) -> dict | None:
    m = np.isfinite(x) & np.isfinite(y)
    if m.sum() < 30:
        return None
    x, y = x[m], y[m]
    b, a = np.polyfit(x, y, 1)
    r2 = 1 - ((y - (a + b * x)) ** 2).sum() / ((y - y.mean()) ** 2).sum()
    return {"slope": float(b), "intercept": float(a), "r2": float(r2), "n": int(m.sum()),
            "x_min": float(x.min()), "x_max": float(x.max())}


def binned(x: pd.Series, y: pd.Series, width: float, lo: float, hi: float, min_n: int = 20) -> list[dict]:
    d = pd.DataFrame({"x": x, "y": y}).dropna()
    d = d[(d["x"] >= lo) & (d["x"] < hi)]
    if d.empty:
        return []
    d["b"] = (np.floor(d["x"] / width) * width + width / 2).round(3)
    g = d.groupby("b")["y"]
    out = pd.DataFrame({"n": g.size(), "p10": g.quantile(0.1), "p50": g.median(), "p90": g.quantile(0.9)})
    out = out[out["n"] >= min_n]
    return [{"x": float(k), **{c: float(v) for c, v in r.items()}} for k, r in out.iterrows()]


def _local_day_bounds(now: pd.Timestamp, offset_days: int = 0, tz: str = PARIS):
    lo = now.tz_convert(tz).normalize() + pd.Timedelta(days=offset_days)
    return lo.tz_convert("UTC"), (lo + pd.Timedelta(days=1)).tz_convert("UTC")


def _ts(idx) -> list[str]:
    return [t.isoformat() for t in pd.DatetimeIndex(idx)]


def _r(s, d=2) -> list:
    return [None if v is None or not np.isfinite(v) else round(float(v), d) for v in np.asarray(s, dtype="float64")]


def _sample(df: pd.DataFrame, n: int, seed: int = 7) -> pd.DataFrame:
    return df.sample(n, random_state=seed).sort_index() if len(df) > n else df


# =========================================================================== Weather -> Demand
def demand_view(frames: dict[str, pd.DataFrame], now: pd.Timestamp) -> dict:
    out = {"countries": {}, "rte_gw_per_degc": config.RTE_GW_PER_DEGC,
           "heating_below_c": HEATING_BELOW_C, "cooling_above_c": COOLING_ABOVE_C}
    normals = weather.load_normals()
    fc = open_meteo.read_latest("forecast")
    ens = open_meteo.read_latest("ensemble")
    for z, df in frames.items():
        c: dict = {}
        if df.empty:
            out["countries"][z] = c
            continue
        # --- temperature vs consumption, daily means, working days only
        d = weather.daily_local(df[["load", "temp"]], TZ[z]).dropna()
        d = d[d.index < now.tz_convert(TZ[z]).normalize()]
        wd = working_days(d.index, z)
        dw = d[wd]
        c["scatter"] = {"date": [x.strftime("%Y-%m-%d") for x in dw.index], "temp": _r(dw["temp"], 1),
                        "load_gw": _r(dw["load"] / 1000, 2), "season": [season(x.month) for x in dw.index],
                        "n_excluded_non_working": int((~wd).sum())}
        heat = dw[dw["temp"] < HEATING_BELOW_C]
        cool = dw[dw["temp"] > COOLING_ABOVE_C]
        c["slope_heating"] = ols(heat["temp"].values, heat["load"].values / 1000)
        c["slope_cooling"] = ols(cool["temp"].values, cool["load"].values / 1000)
        # --- consumption vs official forecasts, last 14 days + tomorrow, hourly
        lo = now.floor("h") - pd.Timedelta(days=14)
        hi = _local_day_bounds(now, 1, TZ[z])[1]
        w = df[(df.index >= lo) & (df.index < hi)]
        c["vs_forecast"] = {"ts": _ts(w.index), "load": _r(w["load"] / 1000), "fc_da": _r(w["load_fc_da"] / 1000),
                            "fc_id": _r(w["load_fc_id"] / 1000), "temp": _r(w["temp"], 1)}
        last30 = df[(df.index >= now - pd.Timedelta(days=30)) & (df.index < now)]
        for k in ("load_fc_da", "load_fc_id"):
            e = (last30[k] - last30["load"]).dropna()
            if len(e) > 48:
                c[f"err_{k}"] = {"mae_gw": float(e.abs().mean() / 1000), "bias_gw": float(e.mean() / 1000),
                                 "mape": float((e.abs() / last30["load"].reindex(e.index)).mean()), "n_hours": len(e)}
        # --- temperature anomaly: next 15 days vs normal
        c["anomaly"] = _anomaly(z, fc, ens, normals, now, c.get("slope_heating"))
        # observed anomaly of the last 30 days, for context
        obs = weather.daily_local(df["temp"], TZ[z]).dropna()
        obs = obs[(obs.index >= (now - pd.Timedelta(days=30)).tz_convert(TZ[z]).normalize()) &
                  (obs.index < now.tz_convert(TZ[z]).normalize())]
        if len(obs) and not normals.empty:
            nm = weather.normal_for(obs.index, normals)[z]
            c["anomaly_past"] = {"date": [x.strftime("%Y-%m-%d") for x in obs.index], "temp": _r(obs, 1),
                                 "normal": _r(nm, 1)}
        out["countries"][z] = c
    return out


def _anomaly(z, fc, ens, normals, now, slope) -> dict:
    res: dict = {"date": []}
    if fc.empty or normals.empty:
        return res
    t = weather.country_index(fc[fc["group"] == "temp"], "temp", "temperature_2m")
    if z not in t:
        return res
    day0 = now.tz_convert(TZ[z]).normalize()
    dt = weather.daily_local(t[z], TZ[z]).dropna()
    dt = dt[dt.index >= day0]
    nm = weather.normal_for(dt.index, normals)[z]
    res = {"date": [x.strftime("%Y-%m-%d") for x in dt.index], "temp": _r(dt, 1), "normal": _r(nm, 1),
           "anomaly": _r(dt - nm, 1), "issued": fc["issued"].max()}
    if len(ens):
        e = ens[(ens["group"] == "temp") & (ens["country"] == z)]
        if len(e):
            piv = e.pivot_table(index="ts", columns="member", values="value")
            daily = weather.daily_local(piv, TZ[z])
            daily = daily[daily.index >= day0].dropna(how="all")
            q = daily.quantile([0.1, 0.5, 0.9], axis=1).T
            q = q.reindex(dt.index)
            res.update({"ens_p10": _r(q[0.1], 1), "ens_p50": _r(q[0.5], 1), "ens_p90": _r(q[0.9], 1),
                        "ens_members": int(piv.shape[1]), "ens_model": str(e.get("model", pd.Series(["ECMWF ENS"])).iloc[0])})
    # Indicative demand effect. FR: RTE rule of thumb; DE: our observed heating slope.
    # Only meaningful in the heating season (normal temperature below the heating threshold).
    if z == "FR":
        gw_per_c, basis = config.RTE_GW_PER_DEGC, "RTE rule of thumb (~2.4 GW per °C, winter)"
    elif slope:
        gw_per_c, basis = -slope["slope"], "observed heating slope (this page, working days below 15 °C)"
    else:
        gw_per_c, basis = None, None
    if gw_per_c:
        anom = (dt - nm)
        eff = (-anom * gw_per_c).where(nm < HEATING_BELOW_C)
        res.update({"effect_gw": _r(eff, 1), "effect_basis": basis, "gw_per_c": round(float(gw_per_c), 2)})
    return res


# =========================================================================== Weather -> Supply
def supply_view(frames: dict[str, pd.DataFrame], now: pd.Timestamp) -> dict:
    out: dict = {"countries": {}}
    fc = open_meteo.read_latest("forecast")
    ens = open_meteo.read_latest("ensemble")
    grid0 = pd.Timestamp(config.WEATHER_GRID_START, tz="UTC")
    for z, df in frames.items():
        c: dict = {}
        if df.empty:
            out["countries"][z] = c
            continue
        h = df[(df.index >= grid0) & (df.index < now)]
        # --- wind power curve: 100 m wind index vs wind capacity factor
        wcf = h[["wind100", "wind_cf"]].dropna()
        wcf = wcf[(wcf["wind_cf"] >= 0) & (wcf["wind_cf"] <= 1.2)]
        c["wind_curve"] = {"bins": binned(wcf["wind100"], wcf["wind_cf"], 0.5, 0, 20),
                           "sample": _r(_sample(wcf, 2500).values.ravel(), 3), "n": len(wcf),
                           "from": wcf.index.min() if len(wcf) else None}
        # --- solar: GHI index vs solar capacity factor (daylight hours)
        scf = h[["ghi", "solar_cf"]].dropna()
        scf = scf[(scf["ghi"] > 5) & (scf["solar_cf"] >= 0) & (scf["solar_cf"] <= 1.2)]
        c["solar_curve"] = {"bins": binned(scf["ghi"], scf["solar_cf"], 50, 0, 1100),
                            "sample": _r(_sample(scf, 2500).values.ravel(), 3), "n": len(scf)}
        # --- sunny vs cloudy day, last 60 complete days
        recent = df[(df.index >= now - pd.Timedelta(days=60)) & (df.index < _local_day_bounds(now, 0, TZ[z])[0])]
        dg = weather.daily_local(recent[["ghi", "solar"]], TZ[z]).dropna()
        if len(dg) >= 5:
            pick = {"sunny": dg["ghi"].idxmax(), "cloudy": dg["ghi"].idxmin()}
            days = {}
            for k, day in pick.items():
                lo = day.tz_convert("UTC")
                seg = df[(df.index >= lo) & (df.index < lo + pd.Timedelta(days=1))]
                days[k] = {"date": day.strftime("%Y-%m-%d"), "hours": [t.tz_convert(TZ[z]).hour for t in seg.index],
                           "solar_gw": _r(seg["solar"] / 1000), "ghi": _r(seg["ghi"], 0), "cloud": _r(seg["cloud"], 0),
                           "energy_gwh": float(seg["solar"].sum() / 1000)}
            c["sun_vs_cloud"] = days
        # --- next 7 days: weather index (deterministic + ensemble) next to official production forecasts
        c["outlook"] = _outlook(z, df, fc, ens, now)
        out["countries"][z] = c
    out["map"] = _map(fc, now)
    return out


def _outlook(z, df, fc, ens, now) -> dict:
    lo, hi = now.floor("h") - pd.Timedelta(days=2), now.floor("h") + pd.Timedelta(days=7)
    idx = pd.date_range(lo, hi, freq="1h", inclusive="left")
    res: dict = {"ts": _ts(idx)}
    for name, group, var in (("wind100", "wind", "wind_speed_100m"), ("ghi", "solar", "shortwave_radiation")):
        if len(fc):
            f = weather.country_index(fc[fc["group"] == group], group, var)
            res[f"{name}_fc"] = _r(f[z].reindex(idx), 1) if z in f else []
        if len(ens):
            e = ens[(ens["group"] == group) & (ens["country"] == z)]
            if len(e):
                q = e.pivot_table(index="ts", columns="member", values="value").quantile([0.1, 0.5, 0.9], axis=1).T
                q = q.reindex(idx)
                res[f"{name}_p10"], res[f"{name}_p50"], res[f"{name}_p90"] = _r(q[0.1], 1), _r(q[0.5], 1), _r(q[0.9], 1)
        res[f"{name}_obs"] = _r(df[name].reindex(idx).where(idx <= now), 1)
    # official production forecasts (Energy-Charts: TSO forecasts for D and D+1) and actuals
    f = storage.read("energy_charts", f"forecast_{z}", lo, hi)
    if len(f):
        f = f.set_index("ts").resample("1h").mean()
        wind = f.get("wind_onshore_da", 0) + f.get("wind_offshore_da", 0) if "wind_onshore_da" in f else None
        res["wind_official_gw"] = _r((wind / 1000).reindex(idx)) if wind is not None else []
        res["solar_official_gw"] = _r((f["solar_da"] / 1000).reindex(idx)) if "solar_da" in f else []
    res["wind_actual_gw"] = _r((df["wind"] / 1000).reindex(idx))
    res["solar_actual_gw"] = _r((df["solar"] / 1000).reindex(idx))
    res["ens_model"] = "ECMWF IFS ENS 0.25° (51 members)" if len(ens) else None
    res["issued"] = fc["issued"].max() if len(fc) else None
    return res


def _map(fc: pd.DataFrame, now: pd.Timestamp) -> dict:
    if fc.empty:
        return {}
    pts = {p.id: p for p in config.WIND_POINTS + config.SOLAR_POINTS}
    times = pd.date_range(now.ceil("3h"), now.ceil("3h") + pd.Timedelta(hours=48), freq="3h")
    f = fc[fc["ts"].isin(times)]
    wind = f[f["group"] == "wind"].pivot_table(index="point", columns="ts", values="wind_speed_100m")
    wdir = f[f["group"] == "wind"].pivot_table(index="point", columns="ts", values="wind_direction_100m")
    cloud = f[f["group"] == "solar"].pivot_table(index="point", columns="ts", values="cloud_cover")
    points = []
    for pid, row in wind.iterrows():
        p = pts[pid]
        points.append({"id": pid, "country": p.country, "lat": p.lat, "lon": p.lon, "kind": "wind",
                       "v": _r(row.reindex(times), 1), "dir": _r(wdir.loc[pid].reindex(times), 0)})
    for pid, row in cloud.iterrows():
        p = pts[pid]
        points.append({"id": pid, "country": p.country, "lat": p.lat, "lon": p.lon, "kind": "cloud",
                       "v": _r(row.reindex(times), 0)})
    return {"times": _ts(times), "points": points, "issued": fc["issued"].max()}


# =========================================================================== Price drivers
def dunkelflaute(df: pd.DataFrame, zone: str) -> tuple[pd.DataFrame, list[dict]]:
    """Daily renewable capacity factor and 'Dunkelflaute' episodes.

    A day is *dark and still* when its mean wind+solar output is below 5% of installed wind+solar
    capacity; an episode is at least 2 consecutive such days.
    """
    d = weather.daily_local(df[["res_cf", "price", "residual", "wind_cf", "solar_cf"]], TZ[zone])
    d = d.dropna(subset=["res_cf"])
    low = d["res_cf"] < DUNKEL_CF
    eps, run = [], []
    for day, is_low in low.items():
        if is_low:
            run.append(day)
        elif run:
            eps.append(run)
            run = []
    if run:
        eps.append(run)
    episodes = []
    for e in eps:
        if len(e) < DUNKEL_MIN_DAYS:
            continue
        seg = d.loc[e[0]:e[-1]]
        episodes.append({"start": e[0].strftime("%Y-%m-%d"), "end": e[-1].strftime("%Y-%m-%d"), "days": len(e),
                         "res_cf": float(seg["res_cf"].mean()), "price": float(seg["price"].mean()),
                         "residual_gw": float(seg["residual"].mean() / 1000)})
    return d, episodes


def negative_prices(zone: str, start, end) -> dict:
    px = _qh_series("energy_charts", f"price_{zone}", "price", start, end)
    if px.empty:
        return {}
    loc = px.copy()
    loc.index = loc.index.tz_convert(TZ[zone])
    # Before 1 Oct 2025 the DA was hourly (forward-filled to 4 QH): count hours as QH/4 consistently.
    neg = (loc < 0)
    monthly = neg.resample("MS").sum() / 4
    daily = neg.resample("D").sum()
    dmin = loc.resample("D").min()
    days = daily[daily > 0].tail(15).iloc[::-1]
    return {"monthly": {"month": [m.strftime("%Y-%m") for m in monthly.index], "hours": _r(monthly, 2)},
            "recent_days": [{"date": d.strftime("%Y-%m-%d"), "qh": int(n), "min": float(dmin[d])} for d, n in days.items()],
            "total_hours_12m": float(neg[neg.index >= loc.index.max() - pd.Timedelta(days=365)].sum() / 4)}


def drivers_view(frames: dict[str, pd.DataFrame], now: pd.Timestamp) -> tuple[dict, dict]:
    """Returns (drivers.json payload, {zone: days payload}) for 'A day in the market'."""
    out: dict = {"countries": {}, "dunkel_cf": DUNKEL_CF, "dunkel_min_days": DUNKEL_MIN_DAYS}
    days_out = {}
    for z, df in frames.items():
        c: dict = {}
        if df.empty:
            out["countries"][z] = c
            continue
        # residual load vs price, last 12 months, hourly
        y = df[(df.index >= now - pd.Timedelta(days=365)) & (df.index < now)][["residual", "price"]].dropna()
        months = (y.index.tz_convert(TZ[z]).year * 12 + y.index.tz_convert(TZ[z]).month)
        y = y.assign(age=(months.max() - months))
        smp = _sample(y, 4000)
        c["merit"] = {"residual_gw": _r(smp["residual"] / 1000), "price": _r(smp["price"], 1),
                      "age_months": smp["age"].astype(int).tolist(),
                      "bins": binned(y["residual"] / 1000, y["price"], 2.5, -40, 90, min_n=15), "n": len(y),
                      "corr": float(y["residual"].corr(y["price"])) if len(y) > 30 else None}
        daily, eps = dunkelflaute(df[df.index < now], z)
        last = daily[daily.index >= daily.index.max() - pd.Timedelta(days=365)] if len(daily) else daily
        c["dunkelflaute"] = {"episodes": eps[::-1], "daily": {"date": [d.strftime("%Y-%m-%d") for d in last.index],
                                                            "res_cf": _r(last["res_cf"], 3), "price": _r(last["price"], 1)}}
        c["negative"] = negative_prices(z, now - pd.Timedelta(days=730), now + pd.Timedelta(days=2))
        # remarkable days (last 12 months, complete local days)
        c["remarkable"] = _remarkable(daily[daily.index >= daily.index.max() - pd.Timedelta(days=365)], df, z, eps)
        days_out[z] = _days_payload(df, z, now, [r["date"] for r in c["remarkable"]])
        out["countries"][z] = c
    return out, days_out


def _remarkable(daily: pd.DataFrame, df: pd.DataFrame, z: str, eps: list[dict]) -> list[dict]:
    if daily.empty:
        return []
    picks = []
    dp = daily["price"].dropna()
    if len(dp):
        picks.append((dp.idxmax(), "Most expensive day", f"daily average {dp.max():.0f} €/MWh"))
        picks.append((dp.idxmin(), "Cheapest day", f"daily average {dp.min():.0f} €/MWh"))
    if len(daily["res_cf"].dropna()):
        picks.append((daily["res_cf"].idxmax(), "Most renewable day", f"wind+solar at {daily['res_cf'].max():.0%} of capacity"))
    t = weather.daily_local(df["temp"], TZ[z]).reindex(daily.index).dropna()
    if len(t):
        picks.append((t.idxmin(), "Coldest day", f"{t.min():.1f} °C population-weighted"))
    if eps:
        e = eps[-1]
        picks.append((pd.Timestamp(e["start"], tz=TZ[z]), "Dunkelflaute",
                      f"{e['days']} days below {DUNKEL_CF:.0%} renewable capacity factor"))
    seen, out = set(), []
    for d, title, why in picks:
        k = d.strftime("%Y-%m-%d")
        if k not in seen:
            seen.add(k)
            out.append({"date": k, "title": title, "why": why})
    return out


DAY_COLS = ("temp", "wind100", "ghi", "load", "wind", "solar", "residual", "price")


def _days_payload(df: pd.DataFrame, z: str, now: pd.Timestamp, extra_dates: list[str], n_days: int = 90) -> dict:
    """Hourly columns for the last ``n_days`` local days (incl. tomorrow if DA is published) + remarkable days."""
    tz = TZ[z]
    end = _local_day_bounds(now, 2, tz)[0]
    start = (now.tz_convert(tz).normalize() - pd.Timedelta(days=n_days)).tz_convert("UTC")
    w = df[(df.index >= start) & (df.index < end)]
    for ds in extra_dates:
        lo = pd.Timestamp(ds, tz=tz).tz_convert("UTC")
        if lo < start:
            w = pd.concat([df[(df.index >= lo) & (df.index < lo + pd.Timedelta(days=1))], w])
    w = w.sort_index()
    w = w[~w.index.duplicated()]
    keep = w[list(DAY_COLS)].notna().any(axis=1)
    w = w[keep]
    gw = {"load", "wind", "solar", "residual"}
    return {"ts": _ts(w.index), **{k: _r(w[k] / 1000 if k in gw else w[k], 2 if k in gw else 1) for k in DAY_COLS},
            "tz": tz}


# =========================================================================== Today
def today_view(frames: dict[str, pd.DataFrame], now: pd.Timestamp, demand: dict, drivers: dict) -> dict:
    out: dict = {"countries": {}, "brief": [], "generated": now}
    for z, df in frames.items():
        c: dict = {}
        tz = TZ[z]
        t0, t1 = _local_day_bounds(now, 0, tz)
        y0, _ = _local_day_bounds(now, -1, tz)
        m0, m1 = _local_day_bounds(now, 1, tz)
        px = _qh_series("energy_charts", f"price_{z}", "price", y0, m1)
        today = px[(px.index >= t0) & (px.index < t1)]
        yday = px[(px.index >= y0) & (px.index < t0)]
        tmr = px[(px.index >= m0) & (px.index < m1)]
        if len(today):
            hourly = today.resample("1h").mean()
            c["price"] = {"avg": float(today.mean()), "min": float(today.min()), "max": float(today.max()),
                          "peak_hour": hourly.idxmax(), "peak_hour_price": float(hourly.max()),
                          "trough_hour": hourly.idxmin(), "neg_qh": int((today < 0).sum()),
                          "yday_avg": float(yday.mean()) if len(yday) else None,
                          "tomorrow_avg": float(tmr.mean()) if len(tmr) >= 90 else None,
                          "curve": {"ts": _ts(px[px.index >= t0].index), "v": _r(px[px.index >= t0], 2)}}
        if not df.empty:
            dd = df[(df.index >= t0) & (df.index < m1)]
            past = dd[dd.index <= now]
            c["load"] = {"ts": _ts(dd.index), "load": _r(dd["load"] / 1000), "fc_da": _r(dd["load_fc_da"] / 1000)}
            last = past.dropna(subset=["load"]).tail(1)
            if len(last):
                c["load_now"] = {"ts": last.index[0], "gw": float(last["load"].iloc[0] / 1000),
                                 "fc_da_gw": float(last["load_fc_da"].iloc[0] / 1000) if pd.notna(last["load_fc_da"].iloc[0]) else None}
            r = past[["wind", "solar", "load"]].dropna()
            if len(r):
                c["res_share_today"] = float((r["wind"] + r["solar"]).sum() / r["load"].sum())
            c["res"] = {"ts": _ts(dd.index), "wind": _r(dd["wind"] / 1000), "solar": _r(dd["solar"] / 1000)}
            tp = weather.daily_local(df["temp"], tz)
            nm = weather.normal_for(tp.index)
            dkey = now.tz_convert(tz).normalize()
            if dkey in tp.index and z in nm:
                c["temp_today"] = {"temp": float(tp[dkey]), "normal": float(nm.loc[dkey, z])}
        an = demand.get("countries", {}).get(z, {}).get("anomaly", {})
        if an.get("anomaly"):
            nxt = [a for a in an["anomaly"][1:8] if a is not None]
            if nxt:
                c["next7_anomaly"] = float(np.mean(nxt))
        out["countries"][z] = c
    out["brief"] = brief(out["countries"], drivers, now)
    return out


def _hh(ts, tz) -> str:
    return pd.Timestamp(ts).tz_convert(tz).strftime("%H:%M")


def brief(cs: dict, drivers: dict, now: pd.Timestamp) -> list[dict]:
    """Rule-based market brief: a few short sentences, each with the rule that produced it."""
    out = []
    for z, c in cs.items():
        p = c.get("price")
        s = SHORT[z]
        if p:
            d = p["avg"] - p["yday_avg"] if p.get("yday_avg") is not None else None
            chg = f" ({'+' if d >= 0 else '−'}{abs(d):.0f} vs yesterday)" if d is not None else ""
            out.append({"zone": z, "rule": "peak", "text": f"{s} peak today {_hh(p['peak_hour'], TZ[z])} at "
                        f"{p['peak_hour_price']:.0f} €/MWh; day average {p['avg']:.0f} €/MWh{chg}."})
            if p["neg_qh"] > 0:
                res = c.get("res", {})
                driver = "solar"
                if res.get("wind") and res.get("solar"):
                    wsum = sum(v for v in res["wind"] if v is not None)
                    ssum = sum(v for v in res["solar"] if v is not None)
                    driver = "solar" if ssum >= wsum else "wind"
                out.append({"zone": z, "rule": "negative", "text": f"{s} has {p['neg_qh']} negative quarter-hours today "
                            f"(min {p['min']:.0f} €/MWh), driven by {driver}."})
            if p.get("tomorrow_avg") is not None:
                out.append({"zone": z, "rule": "tomorrow", "text": f"{s} day-ahead for tomorrow is published: "
                            f"average {p['tomorrow_avg']:.0f} €/MWh."})
        a = c.get("next7_anomaly")
        if a is not None:
            if abs(a) >= 1:
                out.append({"zone": z, "rule": "anomaly", "text": f"Next 7 days {abs(a):.0f} °C "
                            f"{'colder' if a < 0 else 'warmer'} than normal in {LABEL[z]}."})
            else:
                out.append({"zone": z, "rule": "anomaly", "text": f"Next 7 days close to normal temperatures in {LABEL[z]}."})
        sh = c.get("res_share_today")
        if sh is not None and sh >= 0.5:
            out.append({"zone": z, "rule": "res_share", "text": f"Wind and solar cover {sh:.0%} of {s} demand so far today."})
    for z, d in (drivers.get("countries") or {}).items():
        eps = (d.get("dunkelflaute") or {}).get("episodes") or []
        if eps and pd.Timestamp(eps[0]["end"], tz=TZ[z]) >= now.tz_convert(TZ[z]).normalize() - pd.Timedelta(days=1):
            out.append({"zone": z, "rule": "dunkelflaute", "text": f"{SHORT[z]} is in a Dunkelflaute episode since "
                        f"{eps[0]['start']} (wind+solar below {DUNKEL_CF:.0%} of capacity)."})
    pf, pd_ = cs.get("FR", {}).get("price"), cs.get("DE-LU", {}).get("price")
    if pf and pd_:
        sp = pf["avg"] - pd_["avg"]
        out.append({"zone": None, "rule": "spread", "text": f"FR−DE day-ahead spread today: {'+' if sp >= 0 else '−'}{abs(sp):.0f} €/MWh "
                    f"({'France' if sp >= 0 else 'Germany'} more expensive)."})
    return out


# =========================================================================== all
def build_frames(now: pd.Timestamp, start: str | None = None) -> dict[str, pd.DataFrame]:
    start = start or config.V2_HISTORY_START
    end = now + pd.Timedelta(days=3)
    frames = {}
    for z in COUNTRIES:
        try:
            frames[z] = power_hourly(z, pd.Timestamp(start, tz="UTC"), end)
        except Exception:
            log.exception("power frame %s failed", z)
            frames[z] = pd.DataFrame()
    return frames
