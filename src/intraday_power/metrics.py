"""Trading statistics for a set of settled bot decisions (one row per decided QH)."""
from __future__ import annotations

import numpy as np
import pandas as pd

from .config import TZ_DISPLAY


def daily_pnl(trades: pd.DataFrame) -> pd.Series:
    """P&L per Europe/Paris calendar day (days with decisions but no trade count as 0)."""
    if trades.empty:
        return pd.Series(dtype=float)
    day = trades["qh_start_utc"].dt.tz_convert(TZ_DISPLAY).dt.normalize().dt.tz_localize(None)
    return trades.groupby(day)["pnl"].sum()


def max_drawdown(cum: pd.Series) -> float:
    if cum.empty:
        return 0.0
    return float((cum - cum.cummax()).min())


def stats(trades: pd.DataFrame) -> dict:
    """``trades`` needs columns qh_start_utc, position, pnl (settled rows only)."""
    t = trades.dropna(subset=["pnl"])
    active = t[t["position"] != 0]
    n = len(active)
    d = daily_pnl(t)
    sharpe = float(d.mean() / d.std() * np.sqrt(365)) if len(d) > 1 and d.std() > 0 else np.nan
    return {
        "total_pnl": float(t["pnl"].sum()),
        "pnl_per_trade": float(active["pnl"].mean()) if n else np.nan,
        "hit_ratio": float((active["pnl"] > 0).mean()) if n else np.nan,
        "sharpe": sharpe,
        "max_drawdown": max_drawdown(d.cumsum()),
        "exposure": float(n / len(t)) if len(t) else np.nan,
        "trades": int(n),
        "decided_qh": int(len(t)),
        "days": int(len(d)),
        "first": t["qh_start_utc"].min().isoformat() if len(t) else None,
        "last": t["qh_start_utc"].max().isoformat() if len(t) else None,
    }


def pnl_by_hour(trades: pd.DataFrame) -> pd.Series:
    t = trades.dropna(subset=["pnl"])
    h = t["qh_start_utc"].dt.tz_convert(TZ_DISPLAY).dt.hour
    return t.groupby(h)["pnl"].sum().reindex(range(24), fill_value=0.0)
