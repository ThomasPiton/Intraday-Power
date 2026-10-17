"""Shared fixtures. Random inputs here exist only to exercise code paths in tests; they are never
written to the data store or shown in the app."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from intraday_power.features import BEInputs


def make_inputs(start: str = "2025-11-01", days: int = 20, seed: int = 0) -> BEInputs:
    rng = np.random.default_rng(seed)
    qh = pd.date_range(pd.Timestamp(start, tz="UTC"), periods=96 * days, freq="15min")
    n = len(qh)
    si = np.cumsum(rng.normal(0, 60, n)) * 0.3 + rng.normal(0, 150, n)
    da = 90 + 30 * np.sin(np.arange(n) / 96 * 2 * np.pi) + rng.normal(0, 5, n)
    imb = da - 0.4 * si + rng.normal(0, 40, n)
    imb_qh = pd.DataFrame({"ts": qh, "si": si, "imb_price": imb, "mip": imb, "mdp": imb, "alpha": 0.0,
                           "quality": "NotValidated"})
    imb_min = pd.DataFrame({"ts": qh, "min_last": imb + rng.normal(0, 5, n), "min_mean": imb,
                            "min_slope": rng.normal(0, 1, n), "si_min_last": si, "n_min": 15.0})
    minutes = pd.date_range(qh[0], qh[-1] + pd.Timedelta(minutes=14), freq="1min")
    target = minutes.floor("15min") + pd.Timedelta(minutes=15)
    probs = rng.dirichlet(np.ones(6), len(minutes))
    si_fc = pd.DataFrame({"pred_time": minutes, "target": target, "si_fc": rng.normal(0, 200, len(minutes)),
                          "p_lt_m400": probs[:, 0], "p_m400_m200": probs[:, 1], "p_m200_0": probs[:, 2],
                          "p_0_200": probs[:, 3], "p_200_400": probs[:, 4], "p_gt_400": probs[:, 5]})
    return BEInputs(da=pd.DataFrame({"ts": qh, "price": da}), imb_qh=imb_qh, imb_min=imb_min, si_fc=si_fc)


@pytest.fixture
def inputs() -> BEInputs:
    return make_inputs()
