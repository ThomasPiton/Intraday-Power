"""Orchestration used by the CLI: fetch, backtest, live decide/settle, weekly retrain."""
from __future__ import annotations

import json
import logging
import threading
from pathlib import Path

import numpy as np
import pandas as pd

from . import config, storage
from .bots import ml_bot
from .bots.base import Journal, features_hash, pnl_eur
from .bots.ml_bot import MLBot
from .bots.rule_bot import RuleBot, calibrate_threshold
from .features import FEATURES, build_features, decision_time, label_frame, load_be_inputs
from .sources import elia
from .sources import energy_charts as ec

log = logging.getLogger(__name__)
FETCH_STATE = config.RAW / "_fetch_state.json"
ELIA_START = pd.Timestamp(config.ELIA_HISTORY_START, tz="Europe/Brussels").tz_convert("UTC")


def now_utc() -> pd.Timestamp:
    return pd.Timestamp.now(tz="UTC")


def _state() -> dict:
    return json.loads(FETCH_STATE.read_text()) if FETCH_STATE.exists() else {}


def _save_state(s: dict) -> None:
    FETCH_STATE.parent.mkdir(parents=True, exist_ok=True)
    FETCH_STATE.write_text(json.dumps(s, indent=1))


def _parallel(jobs: list[list]) -> None:
    """Run lists of (fn, args) sequentially within a list and the lists in parallel
    (one list per Energy-Charts endpoint, since rate limits are per endpoint)."""
    def runner(seq):
        for fn, *args in seq:
            try:
                fn(*args)
            except Exception:  # one failed series must not kill the run
                log.exception("fetch failed: %s %s", fn.__name__, args)
    threads = [threading.Thread(target=runner, args=(seq,)) for seq in jobs if seq]
    for t in threads:
        t.start()
    for t in threads:
        t.join()


# --------------------------------------------------------------------------- fetch
def fetch_energy_charts(start: str, end: str, prices: bool = True, forecasts: tuple[str, ...] = ("day-ahead", "intraday"),
                        actuals: bool = True, client: ec.EnergyChartsClient | None = None) -> None:
    client = client or ec.EnergyChartsClient()
    jobs = [
        [(ec.fetch_price, client, z, start, end) for z in config.ZONES] if prices else [],
        [(ec.fetch_forecast, client, z, p, f, start, end)
         for f in forecasts for z in config.ZONES for p in config.EC_PRODUCTION_TYPES],
        [(ec.fetch_actuals, client, z, start, end) for z in config.ZONES] if actuals else [],
    ]
    _parallel(jobs)


def fetch_elia(start: pd.Timestamp, end: pd.Timestamp, minute: bool = True) -> None:
    for fn in (elia.fetch_imb_qh, elia.fetch_si_fc, *( (elia.fetch_imb_min,) if minute else ())):
        try:
            log.info("%s: %d rows", fn.__name__, fn(start, end))
        except Exception:
            log.exception("Elia fetch failed: %s", fn.__name__)
    try:
        elia.fetch_si_min(max(start, end - pd.Timedelta(days=2)), end)
    except Exception:
        log.exception("Elia fetch failed: si_min")


def fetch_live(force_ec: bool = False, only: str | None = None) -> None:
    """15-min job: Elia every run; Energy-Charts forecasts/actuals hourly; DA prices when missing.

    ``only="elia"`` / ``only="ec"`` lets the workflow decide right after the (fast) Elia fetch and
    run the (rate-limited, slow) Energy-Charts fetch afterwards.
    """
    now = now_utc()
    st = _state()
    if only != "ec":
        _fetch_live_elia(now)
    if only != "elia":
        _fetch_live_ec(now, st, force_ec)
    st["last_fetch"] = now.isoformat()
    _save_state(st)


def _fetch_live_elia(now: pd.Timestamp) -> None:
    # Elia: re-read the last 2 days (hist + NRT) so late/validated values are picked up.
    last = storage.last_timestamp("elia", "imb_qh")
    e_start = now.floor("D") - pd.Timedelta(days=1)
    if last is None or last < e_start - pd.Timedelta(days=2):
        e_start = now.floor("D") - pd.Timedelta(days=45)       # cache lost: rebuild recent history
        log.warning("Elia store empty/stale, rebuilding from %s", e_start)
    fetch_elia(e_start, now + pd.Timedelta(hours=1))


def _fetch_live_ec(now: pd.Timestamp, st: dict, force_ec: bool) -> None:
    local = now.tz_convert("Europe/Paris")
    d0 = (local - pd.Timedelta(days=1)).strftime("%Y-%m-%d")
    d2 = (local + pd.Timedelta(days=1)).strftime("%Y-%m-%d")
    if storage.last_timestamp("energy_charts", "price_BE") is None:
        d0 = (local - pd.Timedelta(days=45)).strftime("%Y-%m-%d")
    last_ec = pd.Timestamp(st["ec_hourly"]) if "ec_hourly" in st else None
    need_hourly = force_ec or last_ec is None or now - last_ec >= pd.Timedelta(minutes=55)
    # DA prices: tomorrow's auction is published ~12:45 CET; fetch whenever tomorrow is incomplete.
    tomorrow_end = (local.normalize() + pd.Timedelta(days=2)).tz_convert("UTC") - config.QH
    last_px = storage.last_timestamp("energy_charts", "price_BE")
    need_prices = last_px is None or (last_px < tomorrow_end and local.hour >= 12) or \
        last_px < (local.normalize() + pd.Timedelta(days=1)).tz_convert("UTC") - config.QH
    last_da_fc = pd.Timestamp(st["ec_dafc"]) if "ec_dafc" in st else None
    need_dafc = force_ec or last_da_fc is None or now - last_da_fc >= pd.Timedelta(hours=6)
    if need_hourly or need_prices:
        # One forecast type per run: both = 30 calls at 31 s on one endpoint, longer than the job
        # timeout. When the DA forecasts are due they replace that hour's intraday refresh.
        fcs = (("day-ahead",) if need_dafc else ("intraday",)) if need_hourly else ()
        fetch_energy_charts(d0, d2, prices=need_prices, forecasts=fcs, actuals=need_hourly)
        if need_hourly:
            st["ec_hourly"] = now.isoformat()
            if "day-ahead" in fcs:
                st["ec_dafc"] = now.isoformat()


def backfill_raw(ec_start: str = config.EC_HISTORY_START) -> None:
    """Full history: Energy-Charts since ec_start (one call per series), Elia since MARI go-live."""
    now = now_utc()
    end = (now.tz_convert("Europe/Paris") + pd.Timedelta(days=1)).strftime("%Y-%m-%d")
    t = threading.Thread(target=fetch_energy_charts, args=(ec_start, end))
    t.start()
    fetch_elia(ELIA_START - pd.Timedelta(hours=2), now + pd.Timedelta(hours=1))
    t.join()
    st = _state()
    st["ec_hourly"] = st["ec_dafc"] = st["backfill"] = now.isoformat()
    _save_state(st)


# --------------------------------------------------------------------------- backtest
def backtest_window() -> tuple[pd.Timestamp, pd.Timestamp]:
    end = config.LIVE_START_UTC or (now_utc().floor("15min") - pd.Timedelta(hours=1))
    return ELIA_START, end


def calibrate() -> float:
    start, _ = backtest_window()
    cal_end = start + pd.DateOffset(months=config.CALIBRATION_MONTHS)
    qhs = pd.date_range(start, cal_end, freq="15min", inclusive="left")
    inp = load_be_inputs(start, cal_end)
    F, L = build_features(inp, qhs), label_frame(inp, qhs)
    best, table = calibrate_threshold(F, L)
    log.info("calibration table:\n%s", table.to_string(index=False))
    (config.STATE / "rule_calibration.csv").parent.mkdir(parents=True, exist_ok=True)
    table.to_csv(config.STATE / "rule_calibration.csv", index=False)
    return best


def run_backtest() -> pd.DataFrame:
    """Walk-forward backtest from the first ML cut-off to the live start (both bots, same QHs)."""
    start, end = backtest_window()
    qhs = pd.date_range(start, end, freq="15min", inclusive="left")
    inp = load_be_inputs(start, end)
    F, L = build_features(inp, qhs), label_frame(inp, qhs)
    cuts = ml_bot.cutoffs(start, end)
    models = {}
    for c in cuts:
        clf = ml_bot.train(F[F.index < c], L[L.index < c], c)
        if clf is not None:
            ml_bot.save(clf, c, n_train=int(((L.index + 2 * config.QH <= c) & L["spread"].notna()).sum()))
            models[c] = clf
            log.info("trained ML model cut-off %s", c.date())
    ml = MLBot(models)
    rule = RuleBot()
    first = cuts[0] if cuts else end
    mask = F.index >= first
    Fb, Lb = F[mask], L[mask]
    frames = []
    for bot in (rule, ml):
        pos = bot.decide(Fb)
        df = pd.DataFrame({"bot": bot.name, "qh_start_utc": Fb.index, "position": pos.values,
                           "da_price": Lb["da"].values, "imbalance_price": Lb["imb"].values})
        if bot is ml:
            df["proba"] = ml.proba(Fb).values
        frames.append(df)
    bt = pd.concat(frames, ignore_index=True)
    bt["pnl"] = pnl_eur(bt["position"], bt["imbalance_price"], bt["da_price"])
    bt = bt[bt["imbalance_price"].notna() & bt["da_price"].notna()]
    config.BACKTEST.mkdir(parents=True, exist_ok=True)
    bt.to_parquet(config.BACKTEST / "trades.parquet", index=False)
    return bt


# --------------------------------------------------------------------------- live
def ensure_weekly_model(now: pd.Timestamp | None = None) -> pd.Timestamp | None:
    """Train the model for the latest Monday cut-off if it does not exist yet (idempotent)."""
    now = now or now_utc()
    cut = (now.normalize() - pd.Timedelta(days=now.weekday()))
    if ml_bot.model_path(cut).exists():
        return cut
    existing = sorted(config.MODELS.glob("ml_*.txt.gz"))
    if not existing:
        log.warning("no ML model at all; run the backfill first")
        return None
    hist = storage.read("elia", "imb_qh")
    if hist.empty or hist["ts"].min() > ELIA_START + pd.Timedelta(days=1):
        log.warning("Elia history incomplete in the store; refetching full history for training")
        fetch_elia(ELIA_START - pd.Timedelta(hours=2), now)
    qhs = pd.date_range(ELIA_START, cut, freq="15min", inclusive="left")
    inp = load_be_inputs(ELIA_START, cut)
    F, L = build_features(inp, qhs), label_frame(inp, qhs)
    clf = ml_bot.train(F, L, cut)
    if clf is None:
        return None
    ml_bot.save(clf, cut, n_train=int(L["spread"].notna().sum()))
    log.info("trained weekly ML model %s", cut.date())
    return cut


def live_candidates(now: pd.Timestamp) -> pd.DatetimeIndex:
    lo = (now + config.LIVE_WINDOW_CLOSE).ceil("15min")
    hi = now + config.LIVE_WINDOW_OPEN
    return pd.date_range(lo, hi, freq="15min") if lo <= hi else pd.DatetimeIndex([], tz="UTC")


def decide_live(now: pd.Timestamp | None = None, journal: Journal | None = None) -> pd.DataFrame:
    now = now or now_utc()
    journal = journal or Journal()
    qhs = live_candidates(now)
    if len(qhs) == 0:
        log.info("no QH in the decision window at %s", now)
        return pd.DataFrame()
    inp = load_be_inputs(now - pd.Timedelta(days=2), qhs.max() + config.QH)
    F = build_features(inp, qhs)
    ready = F["da"].notna() & F["si_l3"].notna()
    for q in F.index[~ready]:
        log.warning("QH %s skipped: inputs not yet published (da=%s, si_l3=%s)", q, F.at[q, "da"], F.at[q, "si_l3"])
    F = F[ready]
    if F.empty:
        return pd.DataFrame()
    rows = []
    for bot in (RuleBot(), MLBot()):
        pos = bot.decide(F)
        for q, p in pos.items():
            rows.append({"bot": bot.name, "qh_start_utc": q, "decided_at_utc": now.floor("s"),
                         "position": int(p), "features_hash": features_hash(F.loc[q]),
                         "model": bot.model_id(q)})
    df = pd.DataFrame(rows)
    n = journal.append_decisions(df)
    log.info("appended %d decisions for %s", n, list(F.index))
    return df


def settle_live(now: pd.Timestamp | None = None, journal: Journal | None = None) -> int:
    now = now or now_utc()
    journal = journal or Journal()
    t = journal.trades()
    pending = t[t["status"] == "pending"]
    if pending.empty:
        return 0
    qhs = pd.DatetimeIndex(pending["qh_start_utc"].unique())
    inp = load_be_inputs(qhs.min() - pd.Timedelta(days=1), qhs.max() + config.QH)
    L = label_frame(inp, qhs.sort_values())
    pending = pending.join(L, on="qh_start_utc").dropna(subset=["imb", "da"])
    if pending.empty:
        return 0
    out = pd.DataFrame({"bot": pending["bot"], "qh_start_utc": pending["qh_start_utc"],
                        "da_price": pending["da"], "imbalance_price": pending["imb"],
                        "pnl": pnl_eur(pending["position"], pending["imb"], pending["da"]).round(4),
                        "settled_at_utc": now.floor("s")})
    return journal.append_settlements(out)


def live_start() -> pd.Timestamp | None:
    if config.LIVE_START_UTC is not None:
        return config.LIVE_START_UTC
    d = Journal().decisions()
    return d["qh_start_utc"].min() if len(d) else None
