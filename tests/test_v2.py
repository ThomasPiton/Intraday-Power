"""v2: storage with a string key, weather indices, descriptive statistics, detectors, brief rules,
SMARD units and the Data Hub registry. Synthetic inputs only exercise code paths."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from intraday_power import config, datahub_registry, storage, views, weather
from intraday_power.sources import smard


# --------------------------------------------------------------------------- storage
def test_upsert_keeps_string_key(tmp_path):
    ts = pd.date_range("2026-01-01T00:00Z", periods=3, freq="1h")
    df = pd.DataFrame({"ts": list(ts) * 2, "point": ["T-Paris"] * 3 + ["T-Lyon"] * 3, "temperature_2m": np.arange(6.0)})
    storage.upsert(df, "weather", "obs_temp", ["ts", "point"], root=tmp_path)
    out = storage.read("weather", "obs_temp", root=tmp_path)
    assert set(out["point"]) == {"T-Paris", "T-Lyon"} and len(out) == 6
    assert str(out["ts"].dt.tz) == "UTC"


# --------------------------------------------------------------------------- weather
def test_country_index_is_population_weighted():
    ts = pd.Timestamp("2026-01-01T00:00Z")
    paris, lyon = config.TEMP_POINTS[0], config.TEMP_POINTS[1]
    df = pd.DataFrame({"ts": [ts, ts], "point": [paris.id, lyon.id], "temperature_2m": [10.0, 0.0]})
    idx = weather.country_index(df, "temp", "temperature_2m")
    expected = 10.0 * paris.weight / (paris.weight + lyon.weight)
    assert idx.loc[ts, "FR"] == pytest.approx(expected)


def test_wind_index_is_simple_mean():
    ts = pd.Timestamp("2026-01-01T00:00Z")
    pts = [p for p in config.WIND_POINTS if p.country == "DE-LU"][:3]
    df = pd.DataFrame({"ts": [ts] * 3, "point": [p.id for p in pts], "wind_speed_100m": [3.0, 6.0, 9.0]})
    assert weather.country_index(df, "wind", "wind_speed_100m").loc[ts, "DE-LU"] == pytest.approx(6.0)


def test_normal_for_maps_day_of_year():
    n = pd.DataFrame({"FR": np.arange(1, 367, dtype=float)}, index=pd.Index(range(1, 367), name="doy"))
    d = pd.DatetimeIndex(["2026-01-01", "2026-12-31"], tz="Europe/Paris")
    assert weather.normal_for(d, n)["FR"].tolist() == [1.0, 365.0]


# --------------------------------------------------------------------------- statistics
def test_ols_recovers_slope():
    x = np.linspace(-5, 15, 100)
    fit = views.ols(x, 70 - 2.4 * x)
    assert fit["slope"] == pytest.approx(-2.4) and fit["r2"] == pytest.approx(1.0)
    assert views.ols(x[:10], x[:10]) is None          # fewer than 30 points


def test_binned_medians_and_min_count():
    x = pd.Series(np.repeat([1.2, 3.7], 30).tolist() + [9.9])
    y = pd.Series(np.repeat([0.1, 0.5], 30).tolist() + [1.0])
    b = views.binned(x, y, 0.5, 0, 20)
    assert [r["x"] for r in b] == [1.25, 3.75]        # lone 9.9 bin dropped (n < 20)
    assert b[1]["p50"] == pytest.approx(0.5)


def test_season_has_three_classes():
    assert {views.season(m) for m in range(1, 13)} == {"Winter", "Spring/Autumn", "Summer"}


def _frame(res_cf_by_day: list[float], zone="DE-LU") -> pd.DataFrame:
    idx = pd.date_range("2026-01-01", periods=24 * len(res_cf_by_day), freq="1h", tz=views.TZ[zone]).tz_convert("UTC")
    cf = np.repeat(res_cf_by_day, 24)
    return pd.DataFrame({"res_cf": cf, "price": 100.0, "residual": 50_000.0, "wind_cf": cf, "solar_cf": 0.0}, index=idx)


def test_dunkelflaute_needs_two_consecutive_low_days():
    low, ok = views.DUNKEL_CF / 2, 0.3
    _, eps = views.dunkelflaute(_frame([ok, low, ok, low, low, low, ok]), "DE-LU")
    assert len(eps) == 1 and eps[0]["days"] == 3 and eps[0]["start"] == "2026-01-04"


def test_dunkelflaute_episode_at_end_of_data():
    low = views.DUNKEL_CF / 2
    _, eps = views.dunkelflaute(_frame([0.3, low, low]), "DE-LU")
    assert len(eps) == 1 and eps[0]["end"] == "2026-01-03"


# --------------------------------------------------------------------------- brief
def test_brief_rules():
    now = pd.Timestamp("2026-10-18T10:00Z")
    peak = pd.Timestamp("2026-10-18T17:00Z")
    cs = {
        "FR": {"price": {"avg": 100.0, "yday_avg": 90.0, "peak_hour": peak, "peak_hour_price": 150.0, "neg_qh": 0,
                         "min": 20.0, "tomorrow_avg": None}, "next7_anomaly": -3.2},
        "DE-LU": {"price": {"avg": 80.0, "yday_avg": None, "peak_hour": peak, "peak_hour_price": 120.0, "neg_qh": 6,
                            "min": -5.0, "tomorrow_avg": 70.0},
                  "res": {"wind": [1.0, 1.0], "solar": [5.0, 6.0]}, "res_share_today": 0.62, "next7_anomaly": 0.4},
    }
    txt = [b["text"] for b in views.brief(cs, {}, now)]
    assert "FR peak today 19:00 at 150 €/MWh; day average 100 €/MWh (+10 vs yesterday)." in txt
    assert any("DE has 6 negative quarter-hours" in t and "driven by solar" in t for t in txt)
    assert "Next 7 days 3 °C colder than normal in France." in txt
    assert "Next 7 days close to normal temperatures in Germany." in txt
    assert any(t.startswith("DE day-ahead for tomorrow is published") for t in txt)
    assert any("FR−DE day-ahead spread today: +20" in t for t in txt)
    assert any("62% of DE demand" in t for t in txt)


# --------------------------------------------------------------------------- sources / registry
def test_smard_quarter_hour_energy_to_power(monkeypatch):
    monkeypatch.setattr(smard.http, "get_json", lambda *a, **k: {"series": [[1767225600000, 10000.0], [1767226500000, None]]})
    s = smard._block(410, "DE", 0)
    assert len(s) == 1 and s.iloc[0] == 40000.0       # 10 000 MWh per 15 min = 40 GW


def test_registry_rows_are_well_formed():
    ids = [r.id for r in datahub_registry.R]
    assert len(ids) == len(set(ids))
    cats = {c for c, _ in datahub_registry.CATEGORIES}
    for r in datahub_registry.R:
        assert r.cat in cats and r.cat != "gaps"
        assert r.source in datahub_registry.SOURCES
        assert r.desc and r.unit and r.freq
    assert {c for c, _ in datahub_registry.CATEGORIES} - {r.cat for r in datahub_registry.R} == {"gaps"}
    assert datahub_registry.DATA_GAPS
