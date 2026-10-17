# Decisions log

Choices made while building, with the reason. Newest at the bottom of each section.

## Step 0 — data sources (probe run 2026-10-17, `scripts/probe_sources.py`)

| Source | Finding | Consequence |
|---|---|---|
| Energy-Charts `/v2/price` FR, DE-LU, BE, NL, DK1 | One call returns the whole history (FR/BE/NL/DK1 from 2015-01, DE-LU from 2018-10). Hourly until 2025-09-30, 15-min from 2025-10-01 (SDAC 15-min MTU). `license` = "CC BY 4.0 … from Bundesnetzagentur \| SMARD.de" for all 5. `interval_minutes` is `null` on mixed-resolution ranges. | Native resolution inferred per row from neighbours (never from a gap); hourly rows forward-filled to 4 QH and flagged `ffill`. |
| `/v2/public_power_forecast` | `production_type=wind` → 404 everywhere: use `wind_onshore` + `wind_offshore`. `day-ahead` from 2015; `intraday` from 2016–2018 (BE, DE, NL, FR) and 2023-07 (DK). BE hourly, DE/NL 15-min. **NL "intraday" is a copy of day-ahead** (differs on < 0.5% of QHs). | NL revisions shown as "n/a" with the reason, never as zeros (`config.REVISION_UNAVAILABLE`). |
| `/v2/public_power` | Actuals from 2015 for all 5 countries; BE hourly. | — |
| Energy-Charts rate limit | Documented: token bucket per IP and endpoint, 2/min (price: burst 2), lowered under load, 429 + `Retry-After`. | Client spaces calls 31 s per endpoint, runs endpoints in parallel threads, honours `Retry-After`, backs off on 5xx, caches responses on disk. |
| Elia catalog | Confirmed: `ods134` QH imbalance price, `ods133` minute price, `ods126` minute SI (history since 2024-05-21 22:00 UTC, ~1 day latency). NRT twins: `ods162`, `ods161`, `ods169` (today only). SI forecast: `ods147` next QH, `ods136` current QH. Wind `ods031`/`ods086`, PV `ods032`/`ods087`. | Ids hard-coded in `config.ELIA`. |
| Elia latency | QH imbalance price appears 1.0–1.4 min after the QH ends (4 observations); minute series ~2 min. | Lag Q−3 (see bots) leaves > 25 min of margin. |
| Elia `ods147` | One vintage per minute **with publication time** (`predictiontimeutc`) since 2023-10-06; each vintage targets the next QH. | Usable as-of in the backtest → used by both bots (allowed by the brief's condition). |
| Elia API limits | `/records`: limit ≤ 100, offset+limit ≤ 10 000. `/exports/parquet`: no limit. All timestamps UTC. | Bulk reads month by month via `/exports/parquet`. |
| Elia sign convention | Sep 2026: corr(SI, imbalance price) = −0.66; mean price 203 €/MWh when SI < 0 vs 90 when SI > 0. | **SI < 0 = system short = high price.** |
| Elia regime change | Simulated datasets `ods211`/`ods212`: new imbalance price formula "live as of 24/11/2026 10h, conditional on CREG approval". | Marked on charts (`IMBALANCE_REGIME_CHANGE_UTC`); NRT dataset ids may need to change after that date. |

## Architecture

- **Raw data is not committed.** 160 MB of Parquet rewritten every 15 min would bloat git by GBs per month. `data/raw/` lives in the GitHub Actions cache (restored and re-saved each run) and is rebuildable from the APIs: the live fetch rebuilds 45 days automatically if the cache is lost, the weekly retrain refetches Elia history if needed, and `backfill.yml` rebuilds everything.
- **Committed:** the live journal, ML models, backtest results, small state files (thresholds, battery daily results, calibration table) and `docs/data/*.json`.
- **Journal is CSV, not Parquet** (`data/journal/decisions.csv`, `settlements.csv`): appends become one-line git diffs, so the append-only property is auditable in the git history. Settlements are a separate append-only file, so decision rows are never rewritten.
- Extra modules beyond the brief's layout: `pipeline.py` (orchestration used by the CLI) and `revisions.py` (revision maths and alert thresholds).
- The minute imbalance price is stored as per-QH aggregates (last, mean, OLS slope) plus the last 3 days of raw minutes; `ods126` history is not stored (the QH SI comes with `ods134`; the Desk uses NRT `ods169`).
- `update.yml` runs at minutes 2/17/32/47 (off-peak minutes are more punctual on GitHub) and calls `decide` **right after the Elia fetch**, before the slow, rate-limited Energy-Charts fetch, so the decision window is not missed.
- `daily.yml` has two crons (11:30 and 12:30 UTC) and a gate step that only continues when it is 13:xx in Paris: DST handled without date maths.
- Weekly ML retrain: `ensure_weekly_model()` is idempotent and is called by both `decide` (so the first run after Monday 00:00 UTC trains before deciding) and `daily.yml`.
- ECharts 5.5.0 from cdnjs (5.5.1 is not on cdnjs).

## Bots and anti-lookahead

- **Decision time d(Q) = Q − 20 min; real-time QH data up to Q−3 (stricter than the brief's Q−2).** Q−2 ends 15 min before Q and is published ~1 min later; with GitHub cron jitter a live run cannot reliably act in that gap. Using Q−3 everywhere keeps backtest and live identical.
- Live window: a run at t decides QHs with Q − 17 min ≤ t ≤ Q − 2 min (typically the next QH, not "Q+1 and Q+2": deciding Q+2 would need a longer lag). A QH is skipped (no row) if its Q−3 data is not yet published. QHs with no run in their window are not traded and coverage is visible as "decided QH".
- **Renewables revisions are not a bot feature.** Energy-Charts and Elia keep only the final intraday forecast without its issue time, so the backtest could leak information. Revisions stay on the Forecast Revisions tab and in the BE scatter (ex-post diagnostic). Accepted by the owner on 2026-10-17.
- Rule bot signal: Elia SI forecast as of d(Q) (it targets Q−1), else SI of Q−3.
- Rule threshold calibrated on 2024-05-21 → 2024-08-21 by **maximum total P&L over T ∈ {0, 25, …, 500} MW** (criterion fixed before running). Result: **T = 0 MW** (113 k€ vs 111 k€ at 25 MW), frozen in `config.py`. The calibration window is excluded from reported stats for both bots (backtest starts at the first ML cut-off, 2024-08-26).
- ML: LightGBM (200 trees, 15 leaves, min 200 samples/leaf), trade if P > 0.6 → +1 or P < 0.4 → −1. Labels used for training must be published before the cut-off (QH end + 15 min ≤ cut-off). Models stored gzipped (`ml_YYYY-MM-DD.txt.gz` + `.json` with gain importance).
- Sanity check on real data (2026-10-17): the perturbation test re-run on 40 random real QHs → 0 changed decisions. A rule on the 30-min-old SI alone has a ~64% hit ratio; the backtest edge is the persistence of the Belgian imbalance spread, and Methodology says plainly that it is not capturable at scale.
- Live settlement uses Elia's first-published (not-validated) imbalance price.

## Battery

- Cycle limit applied to energy discharged **from the cells** (Σ d/η ≤ 4 MWh/day).
- Days with missing QHs are skipped (no partial-day LP); DST days have 92/100 QHs.
- Run from 2023-01-01 for all zones (total since backfill ≈ 3.8 years).

## Not done / owner action

- The skills-registry bootstrap from the global CLAUDE.md was not run (it would add `CLAUDE.md` and `skills/` to this public repo); run it yourself if you want it.
- `LIVE_START_UTC` in `config.py` must be set to the first QH decided by the first scheduled run on GitHub (until then the UI uses the first journal row).
