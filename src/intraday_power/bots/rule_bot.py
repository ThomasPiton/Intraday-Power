"""Rule bot: lean against the expected Belgian system imbalance.

Signal s(Q) = Elia's latest next-QH SI forecast available at d(Q) (it targets Q-1); if no
vintage is available, the last published quarter-hour SI (Q-3).

Elia convention: SI < 0 means the system is SHORT, which pushes the imbalance price above DA.
    s < -T  -> +1 MW (long DA, settle at the high imbalance price)
    s > +T  -> -1 MW
    else    ->  0
T is calibrated once on the first CALIBRATION_MONTHS of history and frozen in config.py.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from .. import config
from .base import Bot, pnl_eur


def signal(features: pd.DataFrame) -> pd.Series:
    return features["sifc"].fillna(features["si_l3"])


class RuleBot(Bot):
    name = "rule"

    def __init__(self, threshold_mw: float | None = None):
        t = config.RULE_THRESHOLD_MW if threshold_mw is None else threshold_mw
        if t is None:
            raise RuntimeError("Rule-bot threshold not calibrated; run `python -m intraday_power calibrate`.")
        self.threshold_mw = float(t)

    def decide(self, features: pd.DataFrame) -> pd.Series:
        s = signal(features)
        pos = np.where(s < -self.threshold_mw, 1, np.where(s > self.threshold_mw, -1, 0))
        return pd.Series(pos, index=features.index, dtype=int)

    def model_id(self, qh: pd.Timestamp) -> str:
        return f"T={self.threshold_mw:g}MW"


def calibrate_threshold(features: pd.DataFrame, labels: pd.DataFrame,
                        grid: tuple[float, ...] = tuple(range(0, 501, 25))) -> tuple[float, pd.DataFrame]:
    """Pick T maximising total P&L on the calibration window. Returns (T, table of all candidates)."""
    rows = []
    for t in grid:
        pos = RuleBot(t).decide(features)
        ok = labels["spread"].notna()
        pnl = pnl_eur(pos[ok], labels.loc[ok, "imb"], labels.loc[ok, "da"])
        n = int((pos[ok] != 0).sum())
        rows.append({"threshold_mw": t, "pnl_eur": float(pnl.sum()), "trades": n,
                     "pnl_per_trade": float(pnl.sum() / n) if n else np.nan})
    table = pd.DataFrame(rows)
    best = float(table.loc[table["pnl_eur"].idxmax(), "threshold_mw"])
    return best, table
