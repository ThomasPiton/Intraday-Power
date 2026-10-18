"""Write the front-end JSON files (docs/data/*.json). Each file stays well under ~300 KB:
recent windows and aggregates only, never full history."""
from __future__ import annotations

import json
import logging
import math
from pathlib import Path

import numpy as np
import pandas as pd

from . import battery, config
from .bots.base import Journal
from .metrics import daily_pnl, pnl_by_hour, stats
from .pipeline import live_start, now_utc

log = logging.getLogger(__name__)
PARIS = config.TZ_DISPLAY


def _clean(o):
    """JSON-safe: NaN/inf -> None, timestamps -> ISO, numpy scalars -> Python."""
    if isinstance(o, dict):
        return {str(k): _clean(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_clean(v) for v in o]
    if isinstance(o, (pd.Timestamp,)):
        return None if pd.isna(o) else o.isoformat()
    if isinstance(o, (np.integer,)):
        return int(o)
    if isinstance(o, (float, np.floating)):
        return None if not math.isfinite(float(o)) else round(float(o), 3)
    if o is pd.NaT:
        return None
    return o


def _write(name: str, payload: dict, out_dir: Path | None = None) -> Path:
    d = out_dir or config.DOCS_DATA
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{name}.json"
    p.write_text(json.dumps(_clean(payload), separators=(",", ":"), allow_nan=False))
    kb = p.stat().st_size / 1024
    if kb > 300:
        log.warning("%s is %.0f KB (> 300 KB budget)", p.name, kb)
    return p


# --------------------------------------------------------------------------- loaders
def _all_trades() -> pd.DataFrame:
    """Backtest + live settled/pending rows with a ``segment`` column."""
    frames = []
    bt_path = config.BACKTEST / "trades.parquet"
    ls = live_start()
    if bt_path.exists():
        bt = pd.read_parquet(bt_path)
        if ls is not None:
            bt = bt[bt["qh_start_utc"] < ls]
        bt["segment"] = "backtest"
        bt["status"] = "settled"
        bt["decided_at_utc"] = bt["qh_start_utc"] - config.DECISION_LEAD
        frames.append(bt)
    live = Journal().trades()
    if len(live):
        live = live.assign(segment="live")
        frames.append(live)
    if not frames:
        return pd.DataFrame(columns=["bot", "qh_start_utc", "position", "pnl", "segment", "status"])
    t = pd.concat(frames, ignore_index=True)
    t["qh_start_utc"] = pd.to_datetime(t["qh_start_utc"], utc=True)
    return t.sort_values(["qh_start_utc", "bot"]).reset_index(drop=True)


# --------------------------------------------------------------------------- bots
def export_bots(trades: pd.DataFrame) -> dict:
    ls = live_start()
    out: dict = {"live_start": ls, "regime_change": config.IMBALANCE_REGIME_CHANGE_UTC,
                 "rule_threshold_mw": config.RULE_THRESHOLD_MW, "ml_proba_threshold": config.ML_PROBA_THRESHOLD,
                 "cost_eur_mwh": config.COST_EUR_PER_MWH, "cum": {}, "stats": {}, "by_hour": {}, "trades": []}
    if trades.empty:
        return out
    settled = trades[trades["status"] == "settled"]
    for b in config.BOTS:
        g = settled[settled["bot"] == b]
        d = daily_pnl(g)
        out["cum"][b] = {"date": [x.strftime("%Y-%m-%d") for x in d.index], "cum": d.cumsum().round(2).tolist(),
                         "daily": d.round(2).tolist()}
        out["stats"][b] = {seg: stats(g[g["segment"] == seg]) if seg != "all" else stats(g)
                           for seg in ("backtest", "live", "all")}
        out["by_hour"][b] = {seg: pnl_by_hour(g[g["segment"] == seg] if seg != "all" else g).round(2).tolist()
                             for seg in ("backtest", "live", "all")}
    last = trades[trades["position"] != 0].sort_values("qh_start_utc").tail(50).iloc[::-1]
    out["trades"] = [{"bot": r.bot, "decided_at": r.decided_at_utc, "qh": r.qh_start_utc, "position": int(r.position),
                      "da": r.da_price, "imb": r.imbalance_price, "pnl": r.pnl, "status": r.status,
                      "segment": r.segment} for r in last.itertuples()]
    out["pending"] = int((trades["status"] == "pending").sum())
    metas = sorted(config.MODELS.glob("ml_*.json"))
    if metas:
        m = json.loads(metas[-1].read_text())
        imp = m.get("importance_gain", {})
        tot = sum(imp.values()) or 1
        out["feature_importance"] = {"cutoff": m["cutoff"], "n_models": len(metas),
                                     "items": sorted(([k, v / tot] for k, v in imp.items()), key=lambda x: -x[1])}
    cal = config.STATE / "rule_calibration.csv"
    if cal.exists():
        out["rule_calibration"] = pd.read_csv(cal).to_dict("records")
    return out


# --------------------------------------------------------------------------- battery
def export_battery(now: pd.Timestamp) -> dict:
    path = config.STATE / "battery_daily.parquet"
    if not path.exists():
        return {"league": [], "profiles": {}}
    df = pd.read_parquet(path)
    today = now.tz_convert(PARIS).normalize().tz_localize(None)
    league = []
    for z, g in df.groupby("zone"):
        g = g.set_index("date").sort_index()
        past = g[g.index <= today]
        league.append({
            "zone": z,
            "today": g["revenue_eur"].get(today),
            "tomorrow": g["revenue_eur"].get(today + pd.Timedelta(days=1)),
            "d7": past["revenue_eur"].iloc[-7:].sum(), "d30": past["revenue_eur"].iloc[-30:].sum(),
            "total": past["revenue_eur"].sum(), "days": len(past),
            "since": past.index.min().strftime("%Y-%m-%d") if len(past) else None,
            "cycles_30d": past["cycles"].iloc[-30:].mean(),
            "spread_30d": past["spread_eur"].iloc[-30:].mean(),
        })
    hist = df[df["date"] >= today - pd.Timedelta(days=90)].pivot(index="date", columns="zone", values="revenue_eur")
    profiles = {}
    for z in config.ZONES:
        try:
            profiles[z] = battery.profile(z)
        except Exception:
            log.exception("battery profile %s", z)
            profiles[z] = None
    return {"league": league, "profiles": profiles,
            "history": {"date": [d.strftime("%Y-%m-%d") for d in hist.index],
                        **{z: hist[z].round(2).tolist() for z in hist.columns}},
            "params": {"power_mw": config.BATTERY_POWER_MW, "energy_mwh": config.BATTERY_ENERGY_MWH,
                       "rte": config.BATTERY_RTE, "max_cycles": config.BATTERY_MAX_CYCLES,
                       "soc0_mwh": config.BATTERY_SOC0_MWH}}


# --------------------------------------------------------------------------- v2 pages
def export_v2(now: pd.Timestamp) -> dict[str, bool]:
    """Today / Demand / Supply / Drivers (+ per-country day files) / Data Hub."""
    from . import datahub_registry, views
    ok = {}
    frames = views.build_frames(now)

    def run(name, fn):
        try:
            res = fn()
            ok[name] = True
            return res
        except Exception:
            log.exception("export %s failed", name)
            ok[name] = False
            return None

    demand = run("demand", lambda: views.demand_view(frames, now)) or {}
    if ok["demand"]:
        _write("demand", demand)
    supply = run("supply", lambda: views.supply_view(frames, now))
    if supply is not None:
        _write("supply", supply)
    drv = run("drivers", lambda: views.drivers_view(frames, now))
    drivers = {}
    if drv is not None:
        drivers, days = drv
        _write("drivers", drivers)
        for z, payload in days.items():
            _write(f"days_{views.SHORT[z]}", payload)
    today = run("today", lambda: views.today_view(frames, now, demand, drivers))
    if today is not None:
        _write("today", today)
    hub = run("datahub", lambda: datahub_registry.build(now))
    if hub is not None:
        _write("datahub", hub[0])
        for rid, detail in hub[1].items():       # one small file per row, loaded when the row is opened
            _write(rid, detail, config.DOCS_DATA / "hub")
    return ok


# --------------------------------------------------------------------------- all
def export_all(now: pd.Timestamp | None = None) -> None:
    now = now or now_utc()
    trades = _all_trades()
    files = {
        "bots": lambda: export_bots(trades),
        "battery": lambda: export_battery(now),
    }
    ok = {}
    for name, fn in files.items():
        try:
            _write(name, fn())
            ok[name] = True
        except Exception:
            log.exception("export %s failed", name)
            ok[name] = False
    ok.update(export_v2(now))
    _write("meta", {"last_update_utc": now.floor("s"), "live_start_utc": live_start(),
                    "regime_change_utc": config.IMBALANCE_REGIME_CHANGE_UTC,
                    "stale_after_min": config.STALE_AFTER_MIN, "exports_ok": ok, "repo": config.REPO_URL,
                    "decision_lead_min": int(config.DECISION_LEAD.total_seconds() // 60),
                    "rt_lag_qh": config.RT_LAG_QH, "attribution": config.OPEN_METEO_ATTRIBUTION})
