"""Throwaway probe of the free data sources (Step 0).

Runs one worker thread per Energy-Charts endpoint (rate limits are per endpoint),
plus an Elia catalog probe. Writes a JSON report to scripts/probe_report.json.

Usage: python scripts/probe_sources.py [--skip-ec] [--skip-elia]
"""
from __future__ import annotations

import json
import sys
import threading
import time
from datetime import date
from pathlib import Path

import pandas as pd
import requests

EC = "https://api.energy-charts.info"
ELIA = "https://opendata.elia.be/api/explore/v2.1"
OUT = Path(__file__).with_name("probe_report.json")
TODAY = date.today().isoformat()
UA = {"User-Agent": "intraday-power-probe (github.com/ThomasPiton/Intraday-Power)"}

ZONES = ["FR", "DE-LU", "BE", "NL", "DK1"]
COUNTRIES = ["fr", "de", "be", "nl", "dk"]
PTYPES = ["solar", "wind_onshore", "wind_offshore", "wind"]
FTYPES = ["day-ahead", "intraday"]

report: dict = {"energy_charts": {}, "elia": {}}
lock = threading.Lock()


def ec_get(path: str, params: dict, min_gap: float = 31.0, state: dict = {}) -> requests.Response:
    """GET with per-endpoint spacing, Retry-After handling and backoff."""
    for attempt in range(6):
        last = state.get(path, 0.0)
        wait = last + min_gap - time.time()
        if wait > 0:
            time.sleep(wait)
        state[path] = time.time()
        try:
            r = requests.get(EC + path, params=params, headers=UA, timeout=180)
        except requests.RequestException as exc:
            print(f"  {path} {params} network error {exc}; backoff", flush=True)
            time.sleep(30 * (attempt + 1))
            continue
        if r.status_code == 429:
            ra = float(r.headers.get("Retry-After", 60))
            print(f"  429 on {path}, Retry-After={ra}", flush=True)
            time.sleep(ra + 1)
            continue
        if r.status_code >= 500:
            time.sleep(30 * (attempt + 1))
            continue
        return r
    return r


def summarise(j: dict, col: str | None = None) -> dict:
    df = pd.DataFrame([{"ts": d["timestamp"], **d["values"]} for d in j.get("data", [])])
    meta = {k: j.get(k) for k in ("resolution", "available_from", "available_until", "license", "timezone", "unit")}
    meta["series"] = [s["id"] for s in j.get("series", [])]
    if df.empty:
        return {**meta, "rows": 0}
    df["ts"] = pd.to_datetime(df["ts"], utc=True)
    cols = [col] if col and col in df else [c for c in df.columns if c != "ts"]
    nn = df.dropna(subset=cols, how="all")
    out = {**meta, "rows": len(df), "non_null_rows": len(nn)}
    if nn.empty:
        return out
    out["first_non_null"] = nn["ts"].min().isoformat()
    out["last_non_null"] = nn["ts"].max().isoformat()
    # resolution segments: step between consecutive timestamps, grouped by run
    step = nn["ts"].diff().dt.total_seconds().div(60)
    seg = []
    cur, start = None, nn["ts"].iloc[0]
    for ts, s in zip(nn["ts"], step):
        if pd.isna(s) or s > 120:  # gaps > 2h are holes, not resolution
            continue
        if s != cur:
            if cur is not None:
                seg.append({"from": start.isoformat(), "step_min": cur})
            cur, start = s, ts
    if cur is not None:
        seg.append({"from": start.isoformat(), "step_min": cur})
    # compress noise (DST etc.): keep segments lasting > 3 days
    out["resolution_segments"] = seg[:20]
    return out


def probe_price() -> None:
    for z in ZONES:
        r = ec_get("/v2/price", {"bzn": z, "start": "2015-01-01", "end": TODAY})
        res = summarise(r.json()) if r.ok else {"http": r.status_code, "body": r.text[:200]}
        print("price", z, json.dumps(res)[:400], flush=True)
        with lock:
            report["energy_charts"].setdefault("price", {})[z] = res


def probe_forecast() -> None:
    for c in COUNTRIES:
        for p in PTYPES:
            for f in FTYPES:
                r = ec_get("/v2/public_power_forecast",
                           {"country": c, "production_type": p, "forecast_type": f,
                            "start": "2015-01-01", "end": TODAY})
                res = summarise(r.json()) if r.ok else {"http": r.status_code, "body": r.text[:200]}
                print("fc", c, p, f, json.dumps(res)[:300], flush=True)
                with lock:
                    report["energy_charts"].setdefault("forecast", {})[f"{c}|{p}|{f}"] = res


def probe_actuals() -> None:
    for c in COUNTRIES:
        r = ec_get("/v2/public_power", {"country": c, "start": "2015-01-01", "end": TODAY})
        if r.ok:
            j = r.json()
            res = {k: summarise(j, k) for k in ("solar", "wind_onshore", "wind_offshore")}
            res = {k: {kk: v.get(kk) for kk in ("resolution", "first_non_null", "last_non_null", "rows", "license", "resolution_segments")} for k, v in res.items()}
        else:
            res = {"http": r.status_code, "body": r.text[:200]}
        print("actual", c, json.dumps(res)[:400], flush=True)
        with lock:
            report["energy_charts"].setdefault("public_power", {})[c] = res


ELIA_QUERIES = [
    "imbalance price", "system imbalance", "imbalance", "forecast", "wind power", "photovoltaic", "solar",
]


def probe_elia() -> None:
    seen: dict[str, dict] = {}
    for q in ELIA_QUERIES:
        r = requests.get(f"{ELIA}/catalog/datasets",
                         params={"limit": 100, "where": f'search("{q}")'},
                         headers=UA, timeout=60)
        if not r.ok:
            print("elia catalog", q, r.status_code, r.text[:200])
            continue
        for d in r.json().get("results", []):
            m = d.get("metas", {}).get("default", {})
            seen[d["dataset_id"]] = {
                "title": m.get("title"),
                "modified": m.get("modified"),
                "records": m.get("records_count"),
                "fields": [(f["name"], f["type"]) for f in d.get("fields", [])],
            }
    report["elia"]["catalog"] = seen
    for k, v in sorted(seen.items()):
        print(f"elia {k:8s} {v['records']!s:>10} {v['title']}", flush=True)


def elia_detail(ids: list[str]) -> None:
    """Time range, latency and sample for selected datasets."""
    det = {}
    now = pd.Timestamp.now(tz="UTC")
    for ds in ids:
        try:
            base = f"{ELIA}/catalog/datasets/{ds}/records"
            first = requests.get(base, params={"order_by": "datetime asc", "limit": 1}, headers=UA, timeout=60).json()
            last = requests.get(base, params={"order_by": "datetime desc", "limit": 2}, headers=UA, timeout=60).json()
            info = {"total_count": last.get("total_count"),
                    "first": (first.get("results") or [{}])[0],
                    "last": (last.get("results") or [{}])[:2]}
            lt = (last.get("results") or [{}])[0].get("datetime")
            if lt:
                info["lag_vs_now_min"] = round((now - pd.Timestamp(lt)).total_seconds() / 60, 1)
            det[ds] = info
            print("elia detail", ds, json.dumps(info, default=str)[:700], flush=True)
        except Exception as exc:  # probe only
            det[ds] = {"error": str(exc)}
            print("elia detail", ds, "ERR", exc)
    report["elia"]["detail"] = det


if __name__ == "__main__":
    threads = []
    if "--skip-ec" not in sys.argv:
        for fn in (probe_price, probe_forecast, probe_actuals):
            t = threading.Thread(target=fn)
            t.start()
            threads.append(t)
    if "--skip-elia" not in sys.argv:
        probe_elia()
        ids = [a for a in sys.argv[1:] if not a.startswith("--")]
        if ids:
            elia_detail(ids)
    for t in threads:
        t.join()
    if OUT.exists():
        old = json.loads(OUT.read_text())
        for k in report:
            old.setdefault(k, {}).update(report[k])
        merged = old
    else:
        merged = report
    OUT.write_text(json.dumps(merged, indent=1, default=str))
    print("wrote", OUT)
