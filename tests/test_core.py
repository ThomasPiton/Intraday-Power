from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from intraday_power import config, storage
from intraday_power.battery import solve_day
from intraday_power.bots.base import Journal, LookaheadError, pnl_eur
from intraday_power.metrics import max_drawdown, stats
from intraday_power.pipeline import live_candidates
from intraday_power.sources.energy_charts import to_quarter_hours


# --------------------------------------------------------------------------- storage
def test_upsert_idempotent_and_merges_columns(tmp_path):
    ts = pd.date_range("2025-01-31T22:00Z", periods=12, freq="15min")
    a = pd.DataFrame({"ts": ts, "x_da": np.arange(12.0)})
    assert storage.upsert(a, "s", "f", ["ts"], root=tmp_path) == 12
    assert storage.upsert(a, "s", "f", ["ts"], root=tmp_path) == 0          # no-op
    b = pd.DataFrame({"ts": ts[6:], "x_id": np.arange(6.0)})
    storage.upsert(b, "s", "f", ["ts"], root=tmp_path)
    out = storage.read("s", "f", root=tmp_path)
    assert len(out) == 12 and out["x_da"].notna().all() and out["x_id"].notna().sum() == 6
    assert sorted(p.stem for p in (tmp_path / "s" / "f").glob("*.parquet")) == ["2025-01", "2025-02"]
    c = pd.DataFrame({"ts": ts[:1], "x_da": [99.0]})
    storage.upsert(c, "s", "f", ["ts"], root=tmp_path)
    assert storage.read("s", "f", root=tmp_path)["x_da"].iloc[0] == 99.0


def test_to_quarter_hours_flags_hourly_history():
    ts = pd.to_datetime(["2025-09-30T21:00Z", "2025-09-30T22:00Z", "2025-09-30T22:15Z", "2025-09-30T22:30Z"])
    df = pd.DataFrame({"ts": ts, "price": [1.0, 2.0, 3.0, 4.0]})
    q = to_quarter_hours(df, ["price"], interval_minutes=15)
    assert len(q) == 7
    assert q["ffill"].tolist() == [True] * 4 + [False] * 3
    assert q["price"].tolist() == [1, 1, 1, 1, 2, 3, 4]


def test_to_quarter_hours_does_not_fill_gaps_in_15min_data():
    ts = pd.to_datetime(["2025-10-01T00:00Z", "2025-10-01T00:15Z", "2025-10-01T03:00Z"])
    q = to_quarter_hours(pd.DataFrame({"ts": ts, "p": [1.0, 2.0, 3.0]}), ["p"], interval_minutes=15)
    # a hole stays a hole: no row is invented between 00:15 and 03:00, nor after the last row
    assert q["ts"].tolist() == list(ts) and not q["ffill"].any()


def test_to_quarter_hours_last_row_uses_native_step():
    ts = pd.date_range("2026-10-18T21:00Z", periods=4, freq="15min")
    q = to_quarter_hours(pd.DataFrame({"ts": ts, "p": [1.0, 2, 3, 4]}), ["p"], interval_minutes=None)
    assert q["ts"].max() == ts[-1] and len(q) == 4
    h = pd.date_range("2024-01-01T00:00Z", periods=3, freq="60min")
    q = to_quarter_hours(pd.DataFrame({"ts": h, "p": [1.0, 2, 3]}), ["p"], interval_minutes=None)
    assert len(q) == 12 and q["ffill"].all()


# --------------------------------------------------------------------------- journal
def test_journal_rejects_decision_at_or_after_qh_start(tmp_path):
    j = Journal(tmp_path)
    q = pd.Timestamp("2026-10-17T10:00Z")
    bad = pd.DataFrame([{"bot": "rule", "qh_start_utc": q, "decided_at_utc": q, "position": 1,
                         "features_hash": "x", "model": ""}])
    with pytest.raises(LookaheadError):
        j.append_decisions(bad)
    assert j.decisions().empty


def test_journal_is_append_only(tmp_path):
    j = Journal(tmp_path)
    q = pd.Timestamp("2026-10-17T10:00Z")
    row = {"bot": "rule", "qh_start_utc": q, "decided_at_utc": q - pd.Timedelta(minutes=10), "position": 1,
           "features_hash": "abc", "model": "T=150MW"}
    assert j.append_decisions(pd.DataFrame([row])) == 1
    before = j.decisions_path.read_bytes()
    # a second decision for the same (bot, QH) is ignored, the first one is never rewritten
    assert j.append_decisions(pd.DataFrame([{**row, "position": -1}])) == 0
    row2 = {**row, "qh_start_utc": q + config.QH, "decided_at_utc": q}
    j.append_decisions(pd.DataFrame([row2]))
    after = j.decisions_path.read_bytes()
    assert after.startswith(before)
    s = pd.DataFrame([{"bot": "rule", "qh_start_utc": q, "da_price": 100.0, "imbalance_price": 140.0,
                       "pnl": 9.875, "settled_at_utc": q + pd.Timedelta(minutes=20)}])
    assert j.append_settlements(s) == 1 and j.append_settlements(s) == 0
    t = j.trades()
    assert t.set_index("qh_start_utc").loc[q, "status"] == "settled"
    assert t.set_index("qh_start_utc").loc[q + config.QH, "status"] == "pending"


def test_live_window_only_future_qh():
    now = pd.Timestamp("2026-10-17T10:04:00Z")
    c = live_candidates(now)
    assert list(c) == [pd.Timestamp("2026-10-17T10:15Z")]
    assert all(q - now >= config.LIVE_WINDOW_CLOSE for q in c)
    assert list(live_candidates(pd.Timestamp("2026-10-17T10:14:00Z"))) == [pd.Timestamp("2026-10-17T10:30Z")]


# --------------------------------------------------------------------------- pnl / metrics
def test_pnl_formula():
    # long 1 MW, imb 140 vs DA 100: 40*0.25 - 0.5*0.25 = 9.875
    assert pnl_eur(1, 140, 100) == pytest.approx(9.875)
    assert pnl_eur(-1, 140, 100) == pytest.approx(-10.125)
    assert pnl_eur(0, 140, 100) == 0


def test_stats():
    t = pd.DataFrame({"qh_start_utc": pd.date_range("2026-01-01", periods=4, freq="12h", tz="UTC"),
                      "position": [1, 0, -1, 1], "pnl": [10.0, 0.0, -5.0, 2.0]})
    s = stats(t)
    assert s["trades"] == 3 and s["total_pnl"] == 7.0 and s["hit_ratio"] == pytest.approx(2 / 3)
    assert s["exposure"] == 0.75
    assert max_drawdown(pd.Series([0, 10, 4, 12, 1])) == -11


# --------------------------------------------------------------------------- battery
@pytest.mark.parametrize("seed", range(5))
def test_battery_sanity(seed):
    rng = np.random.default_rng(seed)
    p = rng.normal(80, 60, 96)
    r = solve_day(p)
    eta = np.sqrt(config.BATTERY_RTE)
    assert r.revenue_eur >= -1e-6
    assert r.soc_mwh.min() >= -1e-6 and r.soc_mwh.max() <= config.BATTERY_ENERGY_MWH + 1e-6
    assert abs(r.soc_mwh[-1] - config.BATTERY_SOC0_MWH) < 1e-6
    assert (r.discharge_mw * 0.25 / eta).sum() <= config.BATTERY_MAX_CYCLES * config.BATTERY_ENERGY_MWH + 1e-6
    assert r.charge_mw.max() <= 1 + 1e-9 and r.discharge_mw.max() <= 1 + 1e-9
    # revenue equals the cash flow of the dispatch
    assert r.revenue_eur == pytest.approx(float((p * (r.discharge_mw - r.charge_mw) * 0.25).sum()), abs=1e-4)


def test_battery_flat_price_earns_nothing():
    assert solve_day(np.full(96, 50.0)).revenue_eur == pytest.approx(0, abs=1e-6)


def test_battery_dst_day_lengths():
    for n in (92, 100):
        r = solve_day(np.linspace(0, 100, n))
        assert len(r.soc_mwh) == n and r.revenue_eur > 0
