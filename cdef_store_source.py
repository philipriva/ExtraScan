#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Store data source for write_qqscan_cdef.py (``--data-source store``).

Data-reading layer ONLY. Screening (TD / 主力), universe, columns, denominator
and the .90 gate stay in write_qqscan_cdef.py and are unchanged.

Reads the shared OHLCV store (Drive folder OHLCV_STORE_FOLDER_ID):
``manifest.json`` + ``shard_NN.parquet`` written by ohlcv_store.py.

* Fail-closed: if manifest ``session`` < target session the store is not ready
  -> ``StoreNotReady`` (caller BLOCKS, writes nothing). md5 of every shard is
  checked against the manifest; mismatch -> ``StoreNotReady``.
* Frames match the Yahoo path's shape: yfinance ``auto_adjust=True`` style
  (O/H/L/C scaled by adj_close/close, bfilled, Massive bars ratio 1) and the
  same ``LOOKBACK_DAYS`` window as marketdata.download_daily.
* Tickers absent from the store are filled with marketdata.download_daily
  (Yahoo primary + its normal fallbacks). Still missing -> not fresh.
* Freshness itself is decided by write_qqscan_cdef.evaluate (last bar ==
  target session), exactly as in Yahoo mode.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Callable, Optional

import pandas as pd

import ohlcv_store as st


class StoreNotReady(RuntimeError):
    """Store cannot serve the target session (stale manifest, bad md5, unreadable)."""


@dataclass
class StoreStats:
    """Duck-types marketdata.DownloadStats for the STATUS line."""

    requested: int = 0
    store: int = 0          # tickers served from the store
    store_absent: int = 0   # tickers with no rows in the store
    yahoo: int = 0          # absent tickers filled by the Yahoo path
    retry: int = 0
    stooq: int = 0
    massive: int = 0
    absent: list = field(default_factory=list)


def fetch_store(store_dir: Path, folder_id: str, sa_json: str) -> Path:
    """Download manifest + shards read-only from Drive into ``store_dir`` (md5-checked)."""
    d = Path(store_dir)
    d.mkdir(parents=True, exist_ok=True)
    try:
        drv = st.Drive(sa_json, folder_id, readonly=True)
        drv.download("manifest.json", d / "manifest.json")
        man = json.loads((d / "manifest.json").read_text())
        for name in man.get("shards", {}):
            drv.download(name, d / name)
    except StoreNotReady:
        raise
    except BaseException as exc:  # SystemExit from Drive.fid included
        raise StoreNotReady(f"store download failed: {type(exc).__name__}: {str(exc)[:200]}") from None
    return d


def check_manifest(store_dir: Path, expected: date) -> dict:
    """Load manifest, verify session >= expected and shard md5s. Raises StoreNotReady."""
    d = Path(store_dir)
    mp = d / "manifest.json"
    if not mp.exists():
        raise StoreNotReady("manifest.json missing")
    try:
        man = json.loads(mp.read_text())
        sess = date.fromisoformat(str(man["session"]))
    except Exception as exc:
        raise StoreNotReady(f"manifest unreadable: {exc}") from None
    if sess < expected:
        raise StoreNotReady(f"store session {sess} < target {expected}")
    shards = man.get("shards") or {}
    if len(shards) != int(man.get("n_shards", st.N_SHARDS)):
        raise StoreNotReady(f"manifest lists {len(shards)} shards, n_shards={man.get('n_shards')}")
    for name, meta in shards.items():
        p = d / name
        if not p.exists():
            raise StoreNotReady(f"{name} missing")
        if meta.get("md5") and st.md5_file(p) != meta["md5"]:
            raise StoreNotReady(f"md5 mismatch {name}")
    return man


def rows_to_frame(rows: pd.DataFrame) -> pd.DataFrame:
    """Unified store rows -> yfinance auto_adjust=True style frame (Open..Volume)."""
    if rows is None or len(rows) == 0:
        return pd.DataFrame(columns=["Open", "High", "Low", "Close", "Volume"])
    r = rows.sort_values("date")
    f = pd.DataFrame(
        {
            "Open": r["open"].astype(float).values,
            "High": r["high"].astype(float).values,
            "Low": r["low"].astype(float).values,
            "Close": r["close"].astype(float).values,
            "Volume": r["volume"].astype("float64").values,
        },
        index=pd.DatetimeIndex(pd.to_datetime(r["date"]).values, name="Date"),
    )
    adj = pd.Series(pd.to_numeric(r["adj_close"], errors="coerce").values, index=f.index)
    ratio = (adj / f["Close"]).bfill().fillna(1.0)
    for c in ("Open", "High", "Low", "Close"):
        f[c] = f[c] * ratio
    return f


def read_store_rows(store_dir: Path, symbols: list[str]) -> dict[str, pd.DataFrame]:
    """{symbol: unified rows} for symbols present in the store (absent ones omitted)."""
    by_shard: dict[int, dict[str, str]] = {}
    for s in symbols:
        y = st.to_yahoo(s)
        by_shard.setdefault(st.shard_of(y), {})[y] = s
    out: dict[str, pd.DataFrame] = {}
    for k, ymap in sorted(by_shard.items()):
        p = Path(store_dir) / f"shard_{k:02d}.parquet"
        try:
            d = pd.read_parquet(p, filters=[("ticker", "in", list(ymap))])
        except Exception as exc:
            raise StoreNotReady(f"cannot read {p.name}: {exc}") from None
        if not len(d):
            continue
        d["ticker"] = d["ticker"].astype(str)
        for y, g in d.groupby("ticker", sort=False):
            if y in ymap and len(g):
                out[ymap[y]] = g
    return out


def load_frames(
    symbols: list[str],
    expected: date,
    store_dir: Path,
    *,
    lookback_days: Optional[int] = None,
    now: Optional[datetime] = None,
    yahoo_fill: Optional[Callable] = None,
) -> tuple[dict, StoreStats]:
    """Store frames for ``symbols``; absent tickers via Yahoo path. Caller must have
    run check_manifest first. Returns (frames, stats) like marketdata.download_daily."""
    from marketdata import LOOKBACK_DAYS, download_daily

    lb = LOOKBACK_DAYS if lookback_days is None else lookback_days
    start = pd.Timestamp(((now or datetime.utcnow()) - timedelta(days=lb)).date())
    syms = [str(s).strip() for s in symbols if str(s).strip()]
    stats = StoreStats(requested=len(syms))
    rows = read_store_rows(store_dir, syms)
    frames: dict = {}
    for s in syms:
        g = rows.get(s)
        if g is None:
            continue
        f = rows_to_frame(g)
        f = f[f.index >= start]
        if len(f):
            frames[s] = f
    stats.store = len(frames)
    absent = [s for s in syms if s not in frames]
    stats.store_absent = len(absent)
    stats.absent = absent
    if absent:
        fill = yahoo_fill or (lambda ss, exp: download_daily(ss, exp))
        try:
            got, yst = fill(absent, expected)
        except Exception as exc:
            print(f"[store] yahoo fill failed ({type(exc).__name__}); {len(absent)} absent stay not fresh", flush=True)
            got, yst = {}, None
        for s in absent:
            f = (got or {}).get(s)
            if f is not None and len(f):
                frames[s] = f
        if yst is not None:
            for k in ("yahoo", "retry", "stooq", "massive"):
                setattr(stats, k, int(getattr(yst, k, 0) or 0))
    return frames, stats


def default_folder_id() -> str:
    return os.environ.get("OHLCV_STORE_FOLDER_ID", st.DEFAULT_FOLDER_ID)
