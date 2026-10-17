"""Write the front-end JSON files (docs/data/*.json). Each file stays well under ~300 KB:
recent windows and aggregates only, never full history."""
from __future__ import annotations

import json
import logging
import math
from pathlib import Path

import numpy as np
import pandas as pd

from . import battery, config, storage
from .bots.base import Journal
from .metrics import daily_pnl, pnl_by_hour, stats
from .pipeline import live_start, now_utc
from .revisions import load_thresholds, revision_frame

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


def _today_paris(now: pd.Timestamp) -> tuple[pd.Timestamp, pd.Timestamp]:
    lo = now.tz_convert(PARIS).normalize()
    return lo.tz_convert("UTC"), (lo + pd.Timedelta(days=1)).tz_convert("UTC")


def _ts_list(idx) -> list[str]:
    return [t.isoformat() for t in pd.DatetimeIndex(idx)]


# --------------------------------------------------------------------------- data health
SERIES = [
    ("energy_charts", f"price_{z}", "price", f"DA price {z}") for z in config.ZONES
] + [
    ("energy_charts", f"forecast_{z}", "solar_id", f"ID forecast {z}") for z in config.ZONES
] + [
    ("energy_charts", f"actual_{z}", "solar", f"Actuals {z}") for z in config.ZONES
] + [
    ("elia", "imb_qh", "imb_price", "BE imbalance price (QH)"),
    ("elia", "imb_min_recent", "imb_price", "BE imbalance price (min)"),
    ("elia", "si_min", "si", "BE system imbalance (min)"),
    ("elia", "si_fc", "si_fc", "BE SI forecast (Elia)"),
]


def data_health() -> list[dict]:
    out = []
    for src, ser, col, label in SERIES:
        try:
            last = storage.last_timestamp(src, ser, col)
        except Exception:  # corrupted partition etc.
            last = None
        out.append({"series": label, "source": src, "last": last})
    return out


# --------------------------------------------------------------------------- loaders
def _prices(start: pd.Timestamp, end: pd.Timestamp) -> pd.DataFrame:
    cols = {}
    for z in config.ZONES:
        p = storage.read("energy_charts", f"price_{z}", start, end)
        if len(p):
            cols[z] = p.set_index("ts")["price"]
    return pd.DataFrame(cols).sort_index()


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


# --------------------------------------------------------------------------- desk
def build_alerts(now: pd.Timestamp, prices: pd.DataFrame, thresholds: dict) -> list[dict]:
    alerts = []
    t0, t1 = _today_paris(now)
    horizon = prices[(prices.index >= t0) & (prices.index < t1 + pd.Timedelta(days=1))]
    for z in horizon.columns:
        neg = horizon[z][horizon[z] < 0]
        if len(neg):
            alerts.append({"ts": neg.index.min(), "type": "NEG_PRICE", "zone": z, "level": "warn",
                           "msg": f"{z}: {len(neg)} negative DA quarter-hours, min {neg.min():.2f} EUR/MWh "
                                  f"({neg.index.min().tz_convert(PARIS):%a %H:%M})"})
    sp_th = thresholds.get("xzone_spread_p95_eur")
    if sp_th and len(horizon.columns) > 1:
        h = horizon.dropna()
        spread = h.max(axis=1) - h.min(axis=1)
        big = spread[spread > sp_th]
        if len(big):
            q = big.idxmax()
            alerts.append({"ts": q, "type": "XZONE_SPREAD", "zone": f"{h.loc[q].idxmax()}-{h.loc[q].idxmin()}",
                           "level": "info",
                           "msg": f"Cross-zone DA spread {big.max():.1f} EUR/MWh at {q.tz_convert(PARIS):%a %H:%M} "
                                  f"({h.loc[q].idxmax()} vs {h.loc[q].idxmin()}); {len(big)} QH above p95 {sp_th:.0f}"})
    rev_th = thresholds.get("revision_p95_mw", {})
    for z in config.ZONES:
        th = rev_th.get(z)
        if not th:
            continue
        r = revision_frame(z, now - pd.Timedelta(hours=6), now + pd.Timedelta(hours=12))
        if r.empty:
            continue
        big = r["revision_mw"][r["revision_mw"].abs() > th]
        if len(big):
            q = big.abs().idxmax()
            alerts.append({"ts": q, "type": "REVISION", "zone": z, "level": "warn",
                           "msg": f"{z}: renewables revision {big[q]:+,.0f} MW at {q.tz_convert(PARIS):%a %H:%M} "
                                  f"(|rev| > p95 {th:,.0f} MW on {len(big)} QH)"})
    spike_th = thresholds.get("be_imb_spike_p99_eur")
    imb = storage.read("elia", "imb_qh", now - pd.Timedelta(hours=6), now)
    if spike_th and len(imb) and "BE" in prices:
        s = imb.set_index("ts")["imb_price"] - prices["BE"].reindex(imb["ts"]).values
        big = s[s.abs() > spike_th]
        for q, v in big.tail(5).items():
            alerts.append({"ts": q, "type": "IMB_SPIKE", "zone": "BE", "level": "alert",
                           "msg": f"BE imbalance price {imb.set_index('ts').at[q, 'imb_price']:,.0f} EUR/MWh at "
                                  f"{q.tz_convert(PARIS):%H:%M}, {v:+,.0f} vs DA (|x| > p99 {spike_th:.0f})"})
    return sorted(alerts, key=lambda a: a["ts"], reverse=True)


def export_desk(now: pd.Timestamp, trades: pd.DataFrame, thresholds: dict) -> dict:
    t0, t1 = _today_paris(now)
    prices = _prices(t0 - pd.Timedelta(days=1), t1 + pd.Timedelta(days=1))
    cur = now.floor("15min")
    ticker = []
    for z in config.ZONES:
        s = prices.get(z)
        if s is None or s.dropna().empty:
            ticker.append({"zone": z, "price": None})
            continue
        today = s[(s.index >= t0) & (s.index < t1)].dropna()
        ticker.append({"zone": z, "qh": cur, "price": s.get(cur), "prev": s.get(cur - config.QH),
                       "next": s.get(cur + config.QH),
                       "day_avg": today.mean() if len(today) else None,
                       "day_min": today.min() if len(today) else None,
                       "day_max": today.max() if len(today) else None})
    strip = prices[(prices.index >= t0) & (prices.index < t1)]

    bots_today = {}
    live = trades[trades["segment"] == "live"] if len(trades) else trades
    for b in config.BOTS:
        g = live[(live["bot"] == b) & (live["qh_start_utc"] >= t0) & (live["qh_start_utc"] < t1)] if len(live) else live
        settled = g[g["status"] == "settled"] if len(g) else g
        lastpos = live[live["bot"] == b].sort_values("qh_start_utc").tail(1) if len(live) else live
        bots_today[b] = {
            "pnl": float(settled["pnl"].sum()) if len(settled) else 0.0,
            "trades": int((settled["position"] != 0).sum()) if len(settled) else 0,
            "pending": int((g["status"] == "pending").sum()) if len(g) else 0,
            "has_live": bool(len(live)),
            "last_qh": lastpos["qh_start_utc"].iloc[0] if len(lastpos) else None,
            "last_position": int(lastpos["position"].iloc[0]) if len(lastpos) else None,
        }

    be = {}
    si = storage.read("elia", "si_min", now - pd.Timedelta(hours=3), now + pd.Timedelta(hours=1))
    if len(si):
        last = si.dropna(subset=["si"]).iloc[-1]
        be["si_min"] = {"ts": last["ts"], "si": last["si"]}
        tail = si[si["ts"] >= now - pd.Timedelta(hours=2)]
        be["si_spark"] = {"ts": _ts_list(tail["ts"]), "si": tail["si"].tolist()}
    imb = storage.read("elia", "imb_qh", now - pd.Timedelta(hours=6), now + pd.Timedelta(hours=1))
    if len(imb):
        last = imb.dropna(subset=["imb_price"]).iloc[-1]
        da = prices["BE"].get(last["ts"]) if "BE" in prices else None
        be["imb_qh"] = {"ts": last["ts"], "price": last["imb_price"], "si": last["si"], "da": da,
                        "quality": last["quality"]}
    mn = storage.read("elia", "imb_min_recent", now - pd.Timedelta(hours=2), now + pd.Timedelta(hours=1))
    if len(mn):
        last = mn.dropna(subset=["imb_price"]).iloc[-1]
        be["imb_min"] = {"ts": last["ts"], "price": last["imb_price"]}
    fc = storage.read("elia", "si_fc", now - pd.Timedelta(hours=1), now + pd.Timedelta(hours=1))
    if len(fc):
        last = fc.sort_values("pred_time").iloc[-1]
        be["si_fc"] = {"pred_time": last["pred_time"], "target": last["target"], "si_fc": last["si_fc"],
                       "p_short": last["p_lt_m400"] + last["p_m400_m200"], "p_long": last["p_200_400"] + last["p_gt_400"]}
    return {"ticker": ticker, "strip": {"ts": _ts_list(strip.index), **{z: strip[z].tolist() for z in strip}},
            "bots_today": bots_today, "be": be, "alerts": build_alerts(now, prices, thresholds)[:30]}


# --------------------------------------------------------------------------- revisions
def export_revisions(now: pd.Timestamp, thresholds: dict) -> dict:
    t0, t1 = _today_paris(now)
    y0 = t0 - pd.Timedelta(days=1)
    grid = pd.date_range(y0, t1, freq="15min", inclusive="left")
    heat, zones_series = [], {}
    for zi, z in enumerate(config.ZONES):
        r = revision_frame(z, y0 - pd.Timedelta(days=1), t1 + pd.Timedelta(days=1))
        if r.empty:
            zones_series[z] = None
            continue
        rv = r["revision_mw"].reindex(grid)
        for xi, v in enumerate(rv.values):
            if pd.notna(v):
                heat.append([xi, zi, round(float(v), 1)])
        w = r[(r.index >= y0 - pd.Timedelta(days=1)) & (r.index < t1 + pd.Timedelta(days=1))]
        zones_series[z] = {"ts": _ts_list(w.index), "da": w["da_total"].round(1).tolist(),
                           "id": w["id_total"].round(1).tolist(), "actual": w["actual_total"].round(1).tolist(),
                           "rev_pct": w["revision_pct"].round(2).tolist()}
    # BE: revision vs (imbalance - DA), last 30 days
    start = now - pd.Timedelta(days=30)
    r = revision_frame("BE", start, now)
    imb = storage.read("elia", "imb_qh", start, now)
    da = storage.read("energy_charts", "price_BE", start, now)
    scatter = {"points": [], "corr": None, "n": 0}
    if len(r) and len(imb) and len(da):
        j = pd.DataFrame({"rev": r["revision_mw"],
                          "spread": imb.set_index("ts")["imb_price"] - da.set_index("ts")["price"]}).dropna()
        if len(j) > 30:
            scatter = {"points": j.round(1).values.tolist(), "corr": float(j["rev"].corr(j["spread"])),
                       "n": len(j), "from": j.index.min(), "to": j.index.max()}
    return {"grid": _ts_list(grid), "zones": list(config.ZONES), "heatmap": heat, "series": zones_series,
            "unavailable": config.REVISION_UNAVAILABLE,
            "be_scatter": scatter, "thresholds": thresholds.get("revision_p95_mw", {})}


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


# --------------------------------------------------------------------------- all
def export_all(now: pd.Timestamp | None = None) -> None:
    now = now or now_utc()
    thresholds = load_thresholds()
    trades = _all_trades()
    files = {
        "desk": lambda: export_desk(now, trades, thresholds),
        "revisions": lambda: export_revisions(now, thresholds),
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
    _write("meta", {"last_update_utc": now.floor("s"), "live_start_utc": live_start(),
                    "regime_change_utc": config.IMBALANCE_REGIME_CHANGE_UTC,
                    "stale_after_min": config.STALE_AFTER_MIN, "health": data_health(),
                    "thresholds": thresholds, "exports_ok": ok, "repo": config.REPO_URL,
                    "decision_lead_min": int(config.DECISION_LEAD.total_seconds() // 60),
                    "rt_lag_qh": config.RT_LAG_QH})
