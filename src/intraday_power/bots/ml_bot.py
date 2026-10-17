"""ML bot: LightGBM classifier of sign(imbalance price - DA), walk-forward.

* Retrain cut-offs are Mondays 00:00 UTC. The model with cut-off r is trained on every QH whose
  imbalance price was published before r (QH end + 15 min <= r) and is used for QHs with
  d(Q) >= r until the next cut-off. The first cut-off is >= CALIBRATION_MONTHS after the start.
* Position = +1 if P(imb > DA) > 0.6, -1 if P(imb > DA) < 0.4, else 0.
* Each model is saved as ``data/models/ml_<YYYY-MM-DD>.txt.gz`` (LightGBM text format).
"""
from __future__ import annotations

import gzip
import json
import logging
from pathlib import Path

import lightgbm as lgb
import numpy as np
import pandas as pd

from .. import config
from ..features import FEATURES, decision_time
from .base import Bot

log = logging.getLogger(__name__)


def cutoffs(first_qh: pd.Timestamp, last_qh: pd.Timestamp) -> list[pd.Timestamp]:
    """Mondays 00:00 UTC from (first_qh + CALIBRATION_MONTHS) up to last_qh."""
    start = (first_qh + pd.DateOffset(months=config.CALIBRATION_MONTHS)).normalize()
    return list(pd.date_range(start, last_qh, freq="W-MON"))


def model_path(cutoff: pd.Timestamp, root: Path | None = None) -> Path:
    return (root or config.MODELS) / f"ml_{cutoff:%Y-%m-%d}.txt.gz"


def train(features: pd.DataFrame, labels: pd.DataFrame, cutoff: pd.Timestamp,
          min_rows: int = 2000) -> lgb.LGBMClassifier | None:
    """Fit on QHs whose outcome was public before ``cutoff``."""
    known = labels.index + 2 * config.QH <= cutoff
    y = labels["spread"]
    m = known & y.notna() & (y != 0)
    if m.sum() < min_rows:
        log.warning("ML train %s: only %d labelled rows, skipping", cutoff, m.sum())
        return None
    clf = lgb.LGBMClassifier(**config.ML_PARAMS)
    clf.fit(features.loc[m, FEATURES], (y[m] > 0).astype(int))
    return clf


def save(clf: lgb.LGBMClassifier, cutoff: pd.Timestamp, n_train: int, root: Path | None = None) -> Path:
    p = model_path(cutoff, root)
    p.parent.mkdir(parents=True, exist_ok=True)
    txt = clf.booster_.model_to_string()
    with gzip.open(p, "wt", encoding="utf-8") as fh:
        fh.write(txt)
    meta = {"cutoff": cutoff.isoformat(), "n_train": int(n_train), "features": FEATURES,
            "importance_gain": dict(zip(FEATURES, clf.booster_.feature_importance("gain").round(1).tolist()))}
    p.with_suffix("").with_suffix(".json").write_text(json.dumps(meta, indent=1))
    return p


def load_models(root: Path | None = None) -> dict[pd.Timestamp, lgb.Booster]:
    out = {}
    for p in sorted((root or config.MODELS).glob("ml_*.txt.gz")):
        cutoff = pd.Timestamp(p.name[3:13], tz="UTC")
        with gzip.open(p, "rt", encoding="utf-8") as fh:
            out[cutoff] = lgb.Booster(model_str=fh.read())
    return out


class MLBot(Bot):
    name = "ml"

    def __init__(self, models: dict[pd.Timestamp, lgb.Booster | lgb.LGBMClassifier] | None = None,
                 proba_threshold: float = config.ML_PROBA_THRESHOLD):
        self.models = dict(sorted((models if models is not None else load_models()).items()))
        self.th = proba_threshold

    def _cutoff_for(self, qhs: pd.DatetimeIndex) -> pd.Series:
        cuts = pd.DatetimeIndex(list(self.models))
        d = decision_time(qhs)
        idx = cuts.searchsorted(d, side="right") - 1
        return pd.Series([cuts[i] if i >= 0 else pd.NaT for i in idx], index=qhs)

    def proba(self, features: pd.DataFrame) -> pd.Series:
        p = pd.Series(np.nan, index=features.index)
        if not self.models:
            return p
        cut = self._cutoff_for(features.index)
        for c, rows in cut.groupby(cut).groups.items():
            m = self.models[c]
            X = features.loc[rows, FEATURES]
            p.loc[rows] = m.predict_proba(X)[:, 1] if hasattr(m, "predict_proba") else m.predict(X)
        return p

    def decide(self, features: pd.DataFrame) -> pd.Series:
        p = self.proba(features)
        pos = np.where(p > self.th, 1, np.where(p < 1 - self.th, -1, 0))
        return pd.Series(pos, index=features.index, dtype=int)

    def model_id(self, qh: pd.Timestamp) -> str:
        c = self._cutoff_for(pd.DatetimeIndex([qh])).iloc[0]
        return "" if pd.isna(c) else f"ml_{c:%Y-%m-%d}"
