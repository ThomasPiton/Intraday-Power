"""Anti-lookahead: the decision for QH Q must not depend on anything published at/after Q
(and, stricter, on anything outside the information set as of d(Q) = Q - 20 min)."""
from __future__ import annotations

import copy

import numpy as np
import pandas as pd
import pytest

from intraday_power import config
from intraday_power.bots import ml_bot
from intraday_power.bots.ml_bot import MLBot
from intraday_power.bots.rule_bot import RuleBot
from intraday_power.features import BEInputs, build_features, decision_time, label_frame

from conftest import make_inputs

Q = pd.Timestamp("2025-11-15T10:45:00Z")


def perturb(inp: BEInputs, rt_from: pd.Timestamp, fc_after: pd.Timestamp, da_from: pd.Timestamp,
            seed: int = 1) -> BEInputs:
    """Scramble real-time rows with ts >= rt_from, SI-forecast vintages with pred_time > fc_after
    and DA prices with ts >= da_from."""
    rng = np.random.default_rng(seed)
    out = copy.deepcopy(inp)
    for df, key, start, cols in ((out.imb_qh, "ts", rt_from, ["si", "imb_price"]),
                                 (out.imb_min, "ts", rt_from, ["min_last", "min_mean", "min_slope"]),
                                 (out.da, "ts", da_from, ["price"])):
        m = df[key] >= start
        for c in cols:
            df.loc[m, c] = rng.normal(0, 1000, m.sum())
    m = out.si_fc["pred_time"] > fc_after
    for c in ("si_fc", "p_lt_m400", "p_m400_m200", "p_200_400", "p_gt_400"):
        out.si_fc.loc[m, c] = rng.uniform(0, 1000, m.sum())
    return out


def _decisions(inp: BEInputs, models) -> tuple[pd.Series, int, int]:
    F = build_features(inp, pd.DatetimeIndex([Q]))
    return F.iloc[0], int(RuleBot(100).decide(F).iloc[0]), int(MLBot(models).decide(F).iloc[0])


@pytest.fixture(scope="module")
def setup():
    inp = make_inputs()
    qhs = pd.date_range(pd.Timestamp("2025-11-01T00:00Z"), Q, freq="15min", inclusive="left")
    F, L = build_features(inp, qhs), label_frame(inp, qhs)
    cut = pd.Timestamp("2025-11-10T00:00Z")
    clf = ml_bot.train(F, L, cut, min_rows=100)
    assert clf is not None
    return inp, {cut: clf}


def _next_local_day(q: pd.Timestamp) -> pd.Timestamp:
    return (q.tz_convert("Europe/Brussels").normalize() + pd.Timedelta(days=1)).tz_convert("UTC")


def test_decision_unchanged_when_data_at_or_after_Q_is_perturbed(setup):
    inp, models = setup
    base = _decisions(inp, models)
    bad = perturb(inp, rt_from=Q, fc_after=Q - pd.Timedelta(seconds=1), da_from=_next_local_day(Q))
    new = _decisions(bad, models)
    pd.testing.assert_series_equal(base[0], new[0])
    assert base[1:] == new[1:]


def test_decision_unchanged_outside_asof_information_set(setup):
    """Stricter: everything not yet published at d(Q) = Q-20min (QHs >= Q-2, later vintages)."""
    inp, models = setup
    base = _decisions(inp, models)
    bad = perturb(inp, rt_from=Q - (config.RT_LAG_QH - 1) * config.QH, fc_after=decision_time(Q),
                  da_from=_next_local_day(Q))
    new = _decisions(bad, models)
    pd.testing.assert_series_equal(base[0], new[0])
    assert base[1:] == new[1:]


def test_perturbation_inside_information_set_is_detected(setup):
    """Guard against a vacuous test: touching Q-3 or a vintage before d(Q) must move the features."""
    inp, models = setup
    base = _decisions(inp, models)[0]
    bad = perturb(inp, rt_from=Q - config.RT_LAG_QH * config.QH, fc_after=decision_time(Q) - pd.Timedelta(minutes=3),
                  da_from=_next_local_day(Q))
    new = _decisions(bad, models)[0]
    assert not np.allclose(base[["si_l3", "sifc"]].astype(float), new[["si_l3", "sifc"]].astype(float))


def test_ml_model_selection_respects_cutoff():
    cut_old, cut_new = pd.Timestamp("2025-11-03T00:00Z"), pd.Timestamp("2025-11-10T00:00Z")
    bot = MLBot({cut_old: object(), cut_new: object()})
    # QH at 00:15 on the cut-off day: d(Q) = 23:55 the day before -> still the old model.
    assert bot.model_id(pd.Timestamp("2025-11-10T00:15Z")) == "ml_2025-11-03"
    assert bot.model_id(pd.Timestamp("2025-11-10T00:30Z")) == "ml_2025-11-10"


def test_training_uses_only_published_labels():
    inp = make_inputs()
    qhs = pd.date_range("2025-11-01", "2025-11-12", freq="15min", tz="UTC", inclusive="left")
    F, L = build_features(inp, qhs), label_frame(inp, qhs)
    cut = pd.Timestamp("2025-11-10T00:00Z")
    a = ml_bot.train(F, L, cut, min_rows=100)
    L2 = L.copy()
    late = L2.index + 2 * config.QH > cut
    L2.loc[late, "spread"] = -L2.loc[late, "spread"]      # flip every label not yet public
    b = ml_bot.train(F, L2, cut, min_rows=100)
    X = F.iloc[-50:][a.feature_name_]
    np.testing.assert_allclose(a.predict_proba(X), b.predict_proba(X))
