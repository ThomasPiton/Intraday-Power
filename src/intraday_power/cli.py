"""Command line: ``python -m intraday_power <command>``.

fetch      15-min job: Elia NRT (+ Energy-Charts hourly, DA prices when missing)
decide     live decisions for the QHs in the decision window (append-only journal)
settle     settle pending live decisions once Elia has published the imbalance price
battery    solve the battery LP for recent days (``--full`` for the whole history)
train      weekly ML retrain (idempotent: trains this Monday's model if missing)
export     write docs/data/*.json for the front-end
backfill   full history fetch + rule calibration check + walk-forward backtest + battery + thresholds
calibrate  print the rule-bot threshold calibration (does not modify config.py)
"""
from __future__ import annotations

import argparse
import logging
import sys

from . import battery, config, pipeline, revisions


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="intraday_power", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=["fetch", "decide", "settle", "battery", "train", "export",
                                        "backfill", "calibrate", "backtest", "thresholds"])
    ap.add_argument("--full", action="store_true", help="battery: whole history")
    ap.add_argument("--force-ec", action="store_true", help="fetch: ignore the hourly Energy-Charts gate")
    ap.add_argument("--only", choices=["elia", "ec"], help="fetch: one source only")
    ap.add_argument("--skip-raw", action="store_true", help="backfill: reuse the local raw store")
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    log = logging.getLogger("intraday_power")

    if a.command == "fetch":
        pipeline.fetch_live(force_ec=a.force_ec, only=a.only)
    elif a.command == "decide":
        pipeline.ensure_weekly_model()
        pipeline.decide_live()
    elif a.command == "settle":
        log.info("settled %d decisions", pipeline.settle_live())
    elif a.command == "battery":
        battery.run(recompute_last_days=None if a.full else 3)
    elif a.command == "train":
        pipeline.ensure_weekly_model()
    elif a.command == "export":
        from . import export
        export.export_all()
    elif a.command == "calibrate":
        best = pipeline.calibrate()
        log.info("best threshold on calibration window: %s MW (config: %s MW)", best, config.RULE_THRESHOLD_MW)
    elif a.command == "thresholds":
        log.info("%s", revisions.compute_thresholds())
    elif a.command == "backtest":
        bt = pipeline.run_backtest()
        _print_backtest(bt)
    elif a.command == "backfill":
        if not a.skip_raw:
            pipeline.backfill_raw()
        best = pipeline.calibrate()
        if config.RULE_THRESHOLD_MW is None:
            log.error("RULE_THRESHOLD_MW is not frozen in config.py; calibrated value would be %s MW", best)
            return 1
        if best != config.RULE_THRESHOLD_MW:
            log.warning("calibration now gives %s MW but the frozen value %s MW is kept", best, config.RULE_THRESHOLD_MW)
        _print_backtest(pipeline.run_backtest())
        battery.run()
        revisions.compute_thresholds()
        from . import export
        export.export_all()
    return 0


def _print_backtest(bt) -> None:
    from .metrics import stats
    for b, g in bt.groupby("bot"):
        s = stats(g)
        print(f"{b:5s} total={s['total_pnl']:>10,.0f} EUR  trades={s['trades']:>6}  "
              f"pnl/trade={s['pnl_per_trade']:6.2f}  hit={s['hit_ratio']:.1%}  sharpe={s['sharpe']:.2f}  "
              f"maxDD={s['max_drawdown']:,.0f}  exposure={s['exposure']:.1%}  {s['first'][:10]}..{s['last'][:10]}")


if __name__ == "__main__":
    sys.exit(main())
