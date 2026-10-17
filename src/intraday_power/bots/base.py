"""Bot interface, P&L definition and the append-only live journal.

Journal files (plain CSV so every append is a readable one-line git diff):

* ``data/journal/decisions.csv``  bot, qh_start_utc, decided_at_utc, position, features_hash, model
* ``data/journal/settlements.csv`` bot, qh_start_utc, da_price, imbalance_price, pnl, settled_at_utc

Decision rows are never modified or deleted: ``append_decisions`` only appends, refuses any row
with ``decided_at_utc >= qh_start_utc`` and refuses a second decision for the same (bot, QH).
A settlement is appended once, when Elia has published the imbalance price of the QH.
"""
from __future__ import annotations

import hashlib
import json
import logging
from abc import ABC, abstractmethod
from pathlib import Path

import numpy as np
import pandas as pd

from .. import config

log = logging.getLogger(__name__)

DECISION_COLS = ["bot", "qh_start_utc", "decided_at_utc", "position", "features_hash", "model"]
SETTLEMENT_COLS = ["bot", "qh_start_utc", "da_price", "imbalance_price", "pnl", "settled_at_utc"]
TS_FMT = "%Y-%m-%dT%H:%M:%SZ"


class LookaheadError(ValueError):
    """Raised when a decision would be timestamped at or after the start of its quarter-hour."""


class Bot(ABC):
    name: str

    @abstractmethod
    def decide(self, features: pd.DataFrame) -> pd.Series:
        """Positions in {-1, 0, +1} for every row (QH) of the as-of feature matrix."""

    def model_id(self, qh: pd.Timestamp) -> str:
        return ""


def pnl_eur(position, imbalance_price, da_price, cost: float = config.COST_EUR_PER_MWH):
    """p x (imb - DA) x 0.25 h - cost x |p| x 0.25 h, in EUR for a 1 MW quarter-hour position."""
    p = np.asarray(position, dtype=float)
    return p * (np.asarray(imbalance_price, float) - np.asarray(da_price, float)) * 0.25 - cost * np.abs(p) * 0.25


def features_hash(row: pd.Series) -> str:
    payload = json.dumps({k: (None if pd.isna(v) else round(float(v), 6)) for k, v in row.items()},
                         sort_keys=True)
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


# --------------------------------------------------------------------------- journal
class Journal:
    def __init__(self, root: Path | None = None):
        self.root = root or config.JOURNAL
        self.decisions_path = self.root / "decisions.csv"
        self.settlements_path = self.root / "settlements.csv"

    @staticmethod
    def _read(path: Path, cols: list[str]) -> pd.DataFrame:
        if not path.exists() or path.stat().st_size == 0:
            return pd.DataFrame(columns=cols)
        df = pd.read_csv(path)
        for c in ("qh_start_utc", "decided_at_utc", "settled_at_utc"):
            if c in df:
                df[c] = pd.to_datetime(df[c], utc=True)
        return df

    def decisions(self) -> pd.DataFrame:
        return self._read(self.decisions_path, DECISION_COLS)

    def settlements(self) -> pd.DataFrame:
        return self._read(self.settlements_path, SETTLEMENT_COLS)

    @staticmethod
    def _append(path: Path, df: pd.DataFrame, cols: list[str]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        out = df[cols].copy()
        for c in ("qh_start_utc", "decided_at_utc", "settled_at_utc"):
            if c in out:
                out[c] = pd.to_datetime(out[c], utc=True).dt.strftime(TS_FMT)
        header = not path.exists() or path.stat().st_size == 0
        with path.open("a", newline="", encoding="utf-8") as fh:
            out.to_csv(fh, header=header, index=False, lineterminator="\n")

    def append_decisions(self, df: pd.DataFrame) -> int:
        if df.empty:
            return 0
        qh = pd.to_datetime(df["qh_start_utc"], utc=True)
        at = pd.to_datetime(df["decided_at_utc"], utc=True)
        if (at >= qh).any():
            bad = df[at >= qh]
            raise LookaheadError(f"decision timestamped at/after QH start:\n{bad}")
        existing = self.decisions()
        if len(existing):
            seen = set(zip(existing["bot"], existing["qh_start_utc"]))
            mask = [(b, q) not in seen for b, q in zip(df["bot"], qh)]
            df = df[mask]
        df = df.drop_duplicates(["bot", "qh_start_utc"], keep="first")
        if df.empty:
            return 0
        self._append(self.decisions_path, df, DECISION_COLS)
        return len(df)

    def append_settlements(self, df: pd.DataFrame) -> int:
        if df.empty:
            return 0
        existing = self.settlements()
        if len(existing):
            seen = set(zip(existing["bot"], existing["qh_start_utc"]))
            qh = pd.to_datetime(df["qh_start_utc"], utc=True)
            df = df[[(b, q) not in seen for b, q in zip(df["bot"], qh)]]
        if df.empty:
            return 0
        self._append(self.settlements_path, df, SETTLEMENT_COLS)
        return len(df)

    def trades(self) -> pd.DataFrame:
        """Decisions left-joined with their settlement (status pending / settled)."""
        d, s = self.decisions(), self.settlements()
        if d.empty:
            return pd.DataFrame(columns=DECISION_COLS + SETTLEMENT_COLS[2:] + ["status"])
        t = d.merge(s, on=["bot", "qh_start_utc"], how="left")
        t["status"] = np.where(t["imbalance_price"].notna(), "settled", "pending")
        return t
