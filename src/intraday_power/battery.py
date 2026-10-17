"""Battery League: theoretical max revenue of a 1 MW / 2 MWh battery on DA prices (perfect foresight).

Per zone and local delivery day, solve the LP over the day's quarter-hour prices p_t (dt = 0.25 h):

    max  sum_t p_t (d_t - c_t) dt
    s.t. 0 <= c_t, d_t <= P                          (grid-side charge / discharge, MW)
         soc_t = soc_0 + dt * sum_{k<=t} (eta c_k - d_k / eta),  0 <= soc_t <= E
         soc_T = soc_0                                (SoC start = end = 1 MWh)
         dt * sum_t d_t / eta <= cycles * E           (<= 2 full cycles/day = 4 MWh discharged)
    eta = sqrt(0.88) on each leg. Solved with scipy.optimize.linprog(method="highs").

No binary "no simultaneous charge/discharge" constraint: with losses it is never optimal except at
negative prices, where burning energy through round-trip losses is genuinely revenue-positive.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

import numpy as np
import pandas as pd
from scipy.optimize import linprog

from . import config, storage

log = logging.getLogger(__name__)


@dataclass
class DayResult:
    revenue_eur: float
    cycles: float
    charge_mw: np.ndarray
    discharge_mw: np.ndarray
    soc_mwh: np.ndarray


def solve_day(prices: np.ndarray, power: float = config.BATTERY_POWER_MW,
              energy: float = config.BATTERY_ENERGY_MWH, rte: float = config.BATTERY_RTE,
              max_cycles: float = config.BATTERY_MAX_CYCLES, soc0: float = config.BATTERY_SOC0_MWH,
              dt: float = 0.25) -> DayResult:
    p = np.asarray(prices, dtype=float)
    n = len(p)
    eta = np.sqrt(rte)
    # x = [c_0..c_{n-1}, d_0..d_{n-1}]
    cost = np.concatenate([p * dt, -p * dt])
    L = np.tril(np.ones((n, n)))
    S = np.hstack([L * eta * dt, -L * dt / eta])          # soc_t - soc0
    A_ub = np.vstack([S, -S, np.concatenate([np.zeros(n), np.full(n, dt / eta)])[None, :]])
    b_ub = np.concatenate([np.full(n, energy - soc0), np.full(n, soc0), [max_cycles * energy]])
    A_eq = S[-1:, :]
    b_eq = np.array([0.0])
    res = linprog(cost, A_ub=A_ub, b_ub=b_ub, A_eq=A_eq, b_eq=b_eq,
                  bounds=[(0, power)] * (2 * n), method="highs")
    if res.status != 0:
        raise RuntimeError(f"battery LP failed: {res.message}")
    c, d = res.x[:n], res.x[n:]
    soc = soc0 + S @ res.x
    return DayResult(revenue_eur=float(-res.fun), cycles=float((d * dt / eta).sum() / energy),
                     charge_mw=c, discharge_mw=d, soc_mwh=soc)


def zone_day_prices(zone: str, start: str | pd.Timestamp, end: str | pd.Timestamp) -> dict:
    """{local date: price Series} for every complete local day (92/96/100 QH) of ``zone`` in [start, end)."""
    tz = config.ZONES[zone].tz
    start, end = pd.Timestamp(start), pd.Timestamp(end)
    start = start.tz_localize(tz) if start.tzinfo is None else start
    end = end.tz_localize(tz) if end.tzinfo is None else end
    df = storage.read("energy_charts", f"price_{zone}", start.tz_convert("UTC"), end.tz_convert("UTC"))
    if df.empty:
        return {}
    s = df.set_index("ts")["price"].dropna()
    out = {}
    for day, g in s.groupby(s.index.tz_convert(tz).date):
        lo = pd.Timestamp(day).tz_localize(tz)
        hi = (pd.Timestamp(day) + pd.Timedelta(days=1)).tz_localize(tz)
        if len(g) == int((hi - lo) / config.QH):
            out[day] = g
    return out


def run(zones: list[str] | None = None, start: str = config.BATTERY_START,
        end: pd.Timestamp | None = None, recompute_last_days: int | None = None) -> pd.DataFrame:
    """Solve every complete day and upsert results into ``data/state/battery_daily.parquet``."""
    path = config.STATE / "battery_daily.parquet"
    old = pd.read_parquet(path) if path.exists() else pd.DataFrame()
    end = end or pd.Timestamp.now(tz="UTC") + pd.Timedelta(days=2)
    rows = []
    for z in zones or list(config.ZONES):
        s = pd.Timestamp(start)
        if recompute_last_days is not None:
            s = pd.Timestamp.now(tz="UTC").normalize() - pd.Timedelta(days=recompute_last_days)
        for day, g in zone_day_prices(z, s, end).items():
            r = solve_day(g.values)
            rows.append({"zone": z, "date": pd.Timestamp(day), "revenue_eur": r.revenue_eur,
                         "cycles": r.cycles, "n_qh": len(g),
                         "spread_eur": float(g.max() - g.min()), "mean_price": float(g.mean())})
    new = pd.DataFrame(rows)
    if new.empty:
        return old
    out = pd.concat([old, new]).drop_duplicates(["zone", "date"], keep="last").sort_values(["zone", "date"])
    path.parent.mkdir(parents=True, exist_ok=True)
    out.to_parquet(path, index=False)
    return out


def profile(zone: str, day: pd.Timestamp | None = None) -> dict | None:
    """Dispatch profile for one local day (default: latest complete day available, i.e. today or D+1)."""
    tz = config.ZONES[zone].tz
    today = pd.Timestamp.now(tz=tz).normalize()
    days = zone_day_prices(zone, today - pd.Timedelta(days=2), today + pd.Timedelta(days=2))
    if not days:
        return None
    if day is None:
        cand = [d for d in days if pd.Timestamp(d) <= today.tz_localize(None)]
        day = max(cand) if cand else max(days)
    g = days[day]
    r = solve_day(g.values)
    return {"zone": zone, "date": str(day), "ts": [t.isoformat() for t in g.index],
            "price": g.round(2).tolist(), "charge": r.charge_mw.round(3).tolist(),
            "discharge": r.discharge_mw.round(3).tolist(), "soc": r.soc_mwh.round(3).tolist(),
            "revenue_eur": round(r.revenue_eur, 2), "cycles": round(r.cycles, 3)}
