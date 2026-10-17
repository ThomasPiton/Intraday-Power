"""As-of feature construction for the Belgian bots.

One function, ``build_features``, serves both the backtest and the live runs, so the two can
never diverge. For a quarter-hour Q (index = QH start, UTC) with decision time
d(Q) = Q_start - DECISION_LEAD (20 min, i.e. minute 10 of Q-2), a feature may only use:

* the day-ahead price of Q's delivery day (auction cleared on D-1);
* quarter-hour real-time series (system imbalance, imbalance price, minute-price aggregates)
  for QHs <= Q - RT_LAG_QH (= Q-3, which ended 30 min before Q and is published 1-7 min after);
* Elia's next-QH SI forecast vintages with ``pred_time <= d(Q)`` (they target Q-1);
* the calendar.

``tests/test_lookahead.py`` perturbs everything outside that information set and checks that
the features and both bots' decisions for Q are unchanged.
"""
from __future__ import annotations

from dataclasses import dataclass

import holidays
import numpy as np
import pandas as pd

from . import config, storage

BE_TZ = "Europe/Brussels"
FEATURES = [
    "da", "da_shape", "da_delta",
    "si_l3", "si_l4", "si_mean_l3_6",
    "spread_l3", "spread_mean_l3_6",
    "min_last_l3", "min_mean_l3", "min_slope_l3",
    "sifc", "sifc_p_short", "sifc_p_long",
    "hour", "quarter", "weekday", "holiday",
]


@dataclass
class BEInputs:
    """Raw Belgian inputs, all UTC. Each frame keyed by its time column."""
    da: pd.DataFrame        # ts, price
    imb_qh: pd.DataFrame    # ts, si, imb_price, ...
    imb_min: pd.DataFrame   # ts (QH start), min_last, min_mean, min_slope, ...
    si_fc: pd.DataFrame     # pred_time, target, si_fc, p_*


def load_be_inputs(start: pd.Timestamp, end: pd.Timestamp) -> BEInputs:
    pad = pd.Timedelta(days=2)
    return BEInputs(
        da=storage.read("energy_charts", "price_BE", start - pad, end + pad),
        imb_qh=storage.read("elia", "imb_qh", start - pad, end),
        imb_min=storage.read("elia", "imb_min_agg", start - pad, end),
        si_fc=storage.read("elia", "si_fc", start - pad, end),
    )


def _on_grid(df: pd.DataFrame, col: str, grid: pd.DatetimeIndex, key: str = "ts") -> pd.Series:
    if df is None or df.empty or col not in df:
        return pd.Series(np.nan, index=grid)
    s = df.drop_duplicates(key, keep="last").set_index(key)[col].astype(float)
    return s.reindex(grid)


def si_forecast_asof(si_fc: pd.DataFrame, lead_before_target: pd.Timedelta) -> pd.DataFrame:
    """Latest Elia SI-forecast vintage per target QH with ``pred_time <= target - lead``."""
    if si_fc is None or si_fc.empty:
        return pd.DataFrame(columns=["si_fc", "p_short", "p_long"])
    f = si_fc[si_fc["pred_time"] <= si_fc["target"] - lead_before_target]
    f = f.sort_values("pred_time").drop_duplicates("target", keep="last").set_index("target")
    return pd.DataFrame({
        "si_fc": f["si_fc"],
        "p_short": f["p_lt_m400"] + f["p_m400_m200"],
        "p_long": f["p_200_400"] + f["p_gt_400"],
    })


def build_features(inp: BEInputs, qhs: pd.DatetimeIndex) -> pd.DataFrame:
    """Feature matrix indexed by QH start (UTC) for every QH in ``qhs``."""
    qhs = pd.DatetimeIndex(qhs).tz_convert("UTC")
    lag = config.RT_LAG_QH
    # Regular grid wide enough for lags; shifts on a regular grid are exact QH offsets.
    grid = pd.date_range(qhs.min() - config.QH * (lag + 8), qhs.max(), freq="15min", tz="UTC")
    out = pd.DataFrame(index=grid)

    # --- day-ahead price (known for the whole delivery day)
    da = _on_grid(inp.da, "price", grid)
    out["da"] = da
    local_day = grid.tz_convert(BE_TZ).normalize()
    out["da_shape"] = da - da.groupby(local_day).transform("mean")
    out["da_delta"] = da - da.shift(1)

    # --- real-time quarter-hour series, lagged >= RT_LAG_QH
    si = _on_grid(inp.imb_qh, "si", grid)
    imb = _on_grid(inp.imb_qh, "imb_price", grid)
    spread = imb - da
    out["si_l3"] = si.shift(lag)
    out["si_l4"] = si.shift(lag + 1)
    out["si_mean_l3_6"] = si.shift(lag).rolling(4, min_periods=2).mean()
    out["spread_l3"] = spread.shift(lag)
    out["spread_mean_l3_6"] = spread.shift(lag).rolling(4, min_periods=2).mean()
    for c in ("min_last", "min_mean", "min_slope"):
        out[f"{c}_l3"] = _on_grid(inp.imb_min, c, grid).shift(lag)
    # minute price level relative to the DA of the same (lagged) QH
    out["min_last_l3"] = out["min_last_l3"] - da.shift(lag)
    out["min_mean_l3"] = out["min_mean_l3"] - da.shift(lag)

    # --- Elia SI forecast as-of d(Q). Vintages target the *next* QH, so the latest vintage
    # published by d(Q) = Q - 20 min targets Q-1 and was issued <= (Q-1) - 5 min.
    lead_vs_prev = config.DECISION_LEAD - config.QH
    fc = si_forecast_asof(inp.si_fc, lead_vs_prev).reindex(grid)
    out["sifc"] = fc["si_fc"].shift(1)
    out["sifc_p_short"] = fc["p_short"].shift(1)
    out["sifc_p_long"] = fc["p_long"].shift(1)

    # --- calendar (local Brussels time)
    loc = grid.tz_convert(BE_TZ)
    out["hour"] = loc.hour
    out["quarter"] = loc.minute // 15
    out["weekday"] = loc.weekday
    be_hol = holidays.BE(years=range(loc.year.min(), loc.year.max() + 1))
    out["holiday"] = np.array([d in be_hol for d in loc.date], dtype=int)

    return out.loc[qhs, FEATURES]


def decision_time(qh_start: pd.Timestamp | pd.DatetimeIndex):
    return qh_start - config.DECISION_LEAD


def label_frame(inp: BEInputs, qhs: pd.DatetimeIndex) -> pd.DataFrame:
    """Settlement inputs per QH: DA price, imbalance price and the spread (imb - DA)."""
    qhs = pd.DatetimeIndex(qhs)
    da = _on_grid(inp.da, "price", qhs)
    imb = _on_grid(inp.imb_qh, "imb_price", qhs)
    return pd.DataFrame({"da": da, "imb": imb, "spread": imb - da}, index=qhs)
