"""Month-partitioned Parquet store with idempotent upserts.

Layout: ``data/raw/<source>/<series>/YYYY-MM.parquet``. Every frame has one or more key
columns; the first key is a UTC timestamp that drives the monthly partition. Upserting the
same rows twice is a no-op; newer non-null values win, columns not present in the new frame
are preserved (so day-ahead and intraday forecasts can be fetched by separate calls).
"""
from __future__ import annotations

import logging
from pathlib import Path

import pandas as pd

from . import config

log = logging.getLogger(__name__)


def series_dir(source: str, series: str, root: Path | None = None) -> Path:
    return (root or config.RAW) / source / series


def _ensure_utc(s: pd.Series) -> pd.Series:
    s = pd.to_datetime(s, utc=True)
    return s.dt.as_unit("ns")


def upsert(df: pd.DataFrame, source: str, series: str, keys: list[str],
           root: Path | None = None) -> int:
    """Merge ``df`` into the store. Returns the number of rows written to disk (all partitions touched)."""
    if df is None or df.empty:
        return 0
    df = df.copy()
    for k in keys:
        if pd.api.types.is_datetime64_any_dtype(df[k]) or df[k].dtype == object:
            df[k] = _ensure_utc(df[k])
    df = df.drop_duplicates(subset=keys, keep="last")
    d = series_dir(source, series, root)
    d.mkdir(parents=True, exist_ok=True)
    month = df[keys[0]].dt.strftime("%Y-%m")
    written = 0
    for m, part in df.groupby(month):
        path = d / f"{m}.parquet"
        new = part.set_index(keys)
        if path.exists():
            old = pd.read_parquet(path).set_index(keys)
            merged = new.combine_first(old)
            # combine_first keeps old values where new is NaN; columns are the union.
            merged = merged[[c for c in old.columns.union(new.columns, sort=False)]]
        else:
            merged = new
        merged = merged.sort_index()
        if path.exists():
            prev = pd.read_parquet(path).set_index(keys).sort_index()
            if prev.shape == merged.shape and prev.columns.equals(merged.columns) and prev.equals(merged):
                continue
        tmp = path.with_suffix(".tmp")
        merged.reset_index().to_parquet(tmp, index=False, compression="zstd")
        tmp.replace(path)
        written += len(merged)
    return written


def read(source: str, series: str, start: pd.Timestamp | str | None = None,
         end: pd.Timestamp | str | None = None, root: Path | None = None) -> pd.DataFrame:
    """Read rows whose first key lies in [start, end). Returns an empty frame if nothing is stored."""
    d = series_dir(source, series, root)
    if not d.exists():
        return pd.DataFrame()
    start = pd.Timestamp(start, tz="UTC") if isinstance(start, str) else start
    end = pd.Timestamp(end, tz="UTC") if isinstance(end, str) else end
    frames = []
    for path in sorted(d.glob("*.parquet")):
        m = pd.Timestamp(path.stem + "-01", tz="UTC")
        if start is not None and m + pd.offsets.MonthBegin(1) <= start:
            continue
        if end is not None and m >= end:
            continue
        frames.append(pd.read_parquet(path))
    if not frames:
        return pd.DataFrame()
    df = pd.concat(frames, ignore_index=True)
    k = df.columns[0]
    if start is not None:
        df = df[df[k] >= start]
    if end is not None:
        df = df[df[k] < end]
    return df.reset_index(drop=True)


def last_timestamp(source: str, series: str, column: str | None = None,
                   root: Path | None = None) -> pd.Timestamp | None:
    """Latest key timestamp (optionally the latest with a non-null ``column``)."""
    d = series_dir(source, series, root)
    if not d.exists():
        return None
    for path in sorted(d.glob("*.parquet"), reverse=True):
        df = pd.read_parquet(path)
        if column is not None and column in df:
            df = df[df[column].notna()]
        if len(df):
            return df.iloc[:, 0].max()
    return None
