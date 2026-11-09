#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Daily Market Pulse event table → Google Sheet ``MS145 Pulse events``.

Tabs:
  events   — trigger-day signals: asof|ticker|signal|signal_date|tf
             weak→strong: white_ball | yellow_candle (secondary) | zhuli_cross_up_D |
               zhuli_cross_up_W | mcdx_golden_D
             foreshadow bottoms-only: whale_hit (⑥) | pulse_bottom_5 (⑤)
             strong→weak: zhuli_cross_down_D | zhuli_cross_down_W | mcdx_death_D
             Banker Entry ≤3 unused (not emitted). yellow = 附註 only, not a hard gate.
             ⑤⑥ have no top emit.
  ff_state — one row per universe ticker (last settled daily bar):
             asof|ticker|fundtrend|bull|color|ft_ge_50|white_circle|yellow|layer
             Pulse interprets green dominance / windows; Sheet does not aggregate.
  universe — optional refresh from pulse_universe.csv

Universe = pulse_universe.csv (MS145 proxy + radar patches + macro sleeve).
Reuses ExtraScan Fund Flow (fund_flow_state / flags) + zhuli cross up/down; MCDX golden/death from Pine Craft.
ETFs/macros: no stock mcap/vol/$5/EMA200 filters — OHLC crosses only.
Yahoo auto_adjust=True. Overwrites ``events`` + ``ff_state`` tabs RAW.
Pipeline authorized 2026-10-24 Philip「寫入」; see docs/timing-filter-LOCKED.md.
"""
from __future__ import annotations

import argparse
import csv
import os
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import yfinance as yf

from run_extrascan import (
    BATCH,
    DOWNLOAD_BACKOFF_SEC,
    DOWNLOAD_RETRIES,
    LOOKBACK_DAYS,
    MIN_BARS,
    SYMBOL_RETRY_RETRIES,
    _extract_batch_frames,
    asof_et_str,
    drop_forming,
    fund_flow_flags,
    fund_flow_state,
    hkt_now_str,
    ny_now,
)
from sheets_writer import _clear_and_write, _client, _ensure_worksheet
from write_zhuli_lm import (
    MIN_WEEKLY_BARS,
    drop_forming_weekly,
    to_weekly,
    zhuli_cross_down,
    zhuli_cross_up,
)
from pulse_layers_56 import (
    build_breadth_ok,
    pulse_bottom_5_fire,
    whale_hit_last,
)

DEFAULT_SHEET_ID = "11ew_PpHdAnFDn6TWTsGp8qX7GXoYxQOtpvIt_DHmqZ8"
TAB_EVENTS = "events"
TAB_FF_STATE = "ff_state"
TAB_UNIVERSE = "universe"
UNIVERSE_CSV = Path(__file__).resolve().parent / "pulse_universe.csv"
HEADER = ["asof", "ticker", "signal", "signal_date", "tf"]
FF_STATE_HEADER = [
    "asof",
    "ticker",
    "fundtrend",
    "bull",
    "color",
    "ft_ge_50",
    "white_circle",
    "yellow",
    "layer",
]

# MCDX needs HHV/LLV 100 + SMA 10; weekly zhuli needs ~40 bars.
MIN_DAILY_FOR_MCDX = 120
MCDX_LEN = 100
MCDX_SMA = 10


def asof_date_str() -> str:
    """US session calendar date (America/New_York)."""
    try:
        return datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d")
    except Exception:
        return datetime.utcnow().strftime("%Y-%m-%d")


def bar_date_str(idx) -> str:
    ts = pd.Timestamp(idx)
    if ts.tzinfo is not None:
        ts = ts.tz_convert("America/New_York").tz_localize(None)
    return ts.strftime("%Y-%m-%d")


def load_universe(path: Path) -> list[tuple[str, str]]:
    """Return [(ticker, layer), ...] deduped, file order."""
    rows: list[tuple[str, str]] = []
    seen: set[str] = set()
    with path.open(encoding="utf-8") as f:
        for r in csv.DictReader(f):
            t = (r.get("ticker") or "").strip().upper().replace(".", "-")
            if not t or t in seen:
                continue
            seen.add(t)
            rows.append((t, (r.get("layer") or "").strip() or "unknown"))
    return rows


def normalize_ohlc(df: pd.DataFrame) -> pd.DataFrame | None:
    cols = {str(c).lower(): c for c in df.columns}
    need = ("open", "high", "low", "close")
    if not all(k in cols for k in need):
        return None
    out = pd.DataFrame(
        {
            "Open": pd.to_numeric(df[cols["open"]], errors="coerce"),
            "High": pd.to_numeric(df[cols["high"]], errors="coerce"),
            "Low": pd.to_numeric(df[cols["low"]], errors="coerce"),
            "Close": pd.to_numeric(df[cols["close"]], errors="coerce"),
        },
        index=df.index,
    )
    if "volume" in cols:
        out["Volume"] = pd.to_numeric(df[cols["volume"]], errors="coerce")
    else:
        out["Volume"] = 0.0
    out = out.dropna(subset=["Open", "High", "Low", "Close"])
    if not isinstance(out.index, pd.DatetimeIndex):
        out.index = pd.to_datetime(out.index)
    return out if len(out) >= MIN_BARS else None


def _yf_download_once(tickers: list[str]):
    start = (datetime.utcnow() - timedelta(days=LOOKBACK_DAYS)).strftime("%Y-%m-%d")
    return yf.download(
        tickers,
        start=start,
        interval="1d",
        group_by="ticker",
        auto_adjust=True,
        threads=True,
        progress=False,
    )


def download_batch_with_retry(batch: list[str]) -> dict[str, pd.DataFrame]:
    got: dict[str, pd.DataFrame] = {}
    last_err: Exception | None = None
    for attempt in range(DOWNLOAD_RETRIES):
        try:
            raw = _yf_download_once(batch)
            got = _extract_batch_frames(raw, batch)
            if len(got) >= max(1, int(len(batch) * 0.5)):
                return got
        except Exception as e:
            last_err = e
            print(
                f"  batch error attempt {attempt + 1}/{DOWNLOAD_RETRIES}: {e}",
                flush=True,
            )
        time.sleep(DOWNLOAD_BACKOFF_SEC * (attempt + 1))
    if last_err is not None and not got:
        print(f"  batch failed after retries: {last_err}", flush=True)
    return got


def download_symbol_with_retry(sym: str) -> pd.DataFrame | None:
    for attempt in range(SYMBOL_RETRY_RETRIES):
        try:
            raw = _yf_download_once([sym])
            frames = _extract_batch_frames(raw, [sym])
            if sym in frames and not frames[sym].empty:
                return frames[sym]
        except Exception as e:
            print(
                f"  {sym}: error {e} (retry {attempt + 1}/{SYMBOL_RETRY_RETRIES})",
                flush=True,
            )
        time.sleep(DOWNLOAD_BACKOFF_SEC * (attempt + 1))
    return None


def download(syms: list[str]) -> dict[str, pd.DataFrame]:
    out: dict[str, pd.DataFrame] = {}
    n = len(syms)
    for i in range(0, n, BATCH):
        bt = syms[i : i + BATCH]
        print(f"download {min(i + BATCH, n)}/{n}", flush=True)
        got = download_batch_with_retry(bt)
        out.update(got)
        missing = [s for s in bt if s not in out]
        if missing:
            for s in missing:
                frame = download_symbol_with_retry(s)
                if frame is not None:
                    out[s] = frame
                time.sleep(0.05)
        time.sleep(0.15)
    return out


def fmcdx(price: pd.Series, high: pd.Series, low: pd.Series, length: int) -> pd.Series:
    """Pine Craft fmcdx: clip((price - LLV)/( (HHV-LLV)/100 ), 0, 100)."""
    llv = low.rolling(length, min_periods=length).min()
    hhv = high.rolling(length, min_periods=length).max()
    span = (hhv - llv) / 100.0
    raw = (price - llv) / span
    raw = raw.mask(span.eq(0) & span.notna(), 0.0)
    return raw.clip(lower=0.0, upper=100.0)


def mcdx_golden_cross(df: pd.DataFrame) -> bool:
    """sPC cross above sLC on last bar (Pine Craft MCDX golden)."""
    if df is None or len(df) < MIN_DAILY_FOR_MCDX:
        return False
    c = df["Close"].astype(float)
    h = df["High"].astype(float)
    l = df["Low"].astype(float)
    pc = fmcdx(c * 0.96, h, l, MCDX_LEN)
    lc = 100.0 - fmcdx(c * 1.04, h, l, MCDX_LEN)
    s_pc = pc.rolling(MCDX_SMA, min_periods=MCDX_SMA).mean()
    s_lc = lc.rolling(MCDX_SMA, min_periods=MCDX_SMA).mean()
    if len(s_pc) < 2:
        return False
    a0, a1 = float(s_pc.iloc[-1]), float(s_pc.iloc[-2])
    b0, b1 = float(s_lc.iloc[-1]), float(s_lc.iloc[-2])
    if any(pd.isna(x) for x in (a0, a1, b0, b1)):
        return False
    return a0 > b0 and a1 <= b1


def mcdx_death_cross(df: pd.DataFrame) -> bool:
    """sPC cross below sLC on last bar (Pine Craft MCDX death)."""
    if df is None or len(df) < MIN_DAILY_FOR_MCDX:
        return False
    c = df["Close"].astype(float)
    h = df["High"].astype(float)
    l = df["Low"].astype(float)
    pc = fmcdx(c * 0.96, h, l, MCDX_LEN)
    lc = 100.0 - fmcdx(c * 1.04, h, l, MCDX_LEN)
    s_pc = pc.rolling(MCDX_SMA, min_periods=MCDX_SMA).mean()
    s_lc = lc.rolling(MCDX_SMA, min_periods=MCDX_SMA).mean()
    if len(s_pc) < 2:
        return False
    a0, a1 = float(s_pc.iloc[-1]), float(s_pc.iloc[-2])
    b0, b1 = float(s_lc.iloc[-1]), float(s_lc.iloc[-2])
    if any(pd.isna(x) for x in (a0, a1, b0, b1)):
        return False
    return a0 < b0 and a1 >= b1


def screen_symbol(
    sym: str,
    d: pd.DataFrame,
    asof: str,
    *,
    spy: pd.DataFrame | None = None,
    breadth_ok: pd.Series | None = None,
) -> list[list[str]]:
    """Emit trigger-day rows for one ticker (FF/zhuli/MCDX + ⑤⑥ foreshadow)."""
    rows: list[list[str]] = []
    signal_date = bar_date_str(d.index[-1])

    try:
        yellow, white = fund_flow_flags(d)
    except Exception as exc:
        print(f"[skip FF] {sym}: {exc}", file=sys.stderr, flush=True)
        yellow, white = False, False
    if yellow:
        rows.append([asof, sym, "yellow_candle", signal_date, "D"])
    if white:
        rows.append([asof, sym, "white_ball", signal_date, "D"])

    try:
        if zhuli_cross_up(d):
            rows.append([asof, sym, "zhuli_cross_up_D", signal_date, "D"])
        if zhuli_cross_down(d):
            rows.append([asof, sym, "zhuli_cross_down_D", signal_date, "D"])
    except Exception as exc:
        print(f"[skip zhuli D] {sym}: {exc}", file=sys.stderr, flush=True)

    try:
        if mcdx_golden_cross(d):
            rows.append([asof, sym, "mcdx_golden_D", signal_date, "D"])
        if mcdx_death_cross(d):
            rows.append([asof, sym, "mcdx_death_D", signal_date, "D"])
    except Exception as exc:
        print(f"[skip mcdx] {sym}: {exc}", file=sys.stderr, flush=True)

    try:
        w = drop_forming_weekly(to_weekly(d))
        if w is not None and len(w) >= MIN_WEEKLY_BARS:
            w_date = bar_date_str(w.index[-1])
            if zhuli_cross_up(w):
                rows.append([asof, sym, "zhuli_cross_up_W", w_date, "W"])
            if zhuli_cross_down(w):
                rows.append([asof, sym, "zhuli_cross_down_W", w_date, "W"])
    except Exception as exc:
        print(f"[skip zhuli W] {sym}: {exc}", file=sys.stderr, flush=True)

    # ⑥ Whale — bottoms foreshadow only (no top variant)
    try:
        if whale_hit_last(d):
            rows.append([asof, sym, "whale_hit", signal_date, "D"])
    except Exception as exc:
        print(f"[skip whale] {sym}: {exc}", file=sys.stderr, flush=True)

    # ⑤ Pulse 見底讀 — RS stop_ll onset + RSP/SPY breadth within +10TD
    # Skip self-RS for SPY; still allow QQQ/RSP etc.
    if spy is not None and breadth_ok is not None and sym != "SPY":
        try:
            fires, onset_ts = pulse_bottom_5_fire(d, spy, breadth_ok)
            if fires:
                # signal_date = onset (Pulse research label); asof = run day
                sd = bar_date_str(onset_ts) if onset_ts is not None else signal_date
                rows.append([asof, sym, "pulse_bottom_5", sd, "D"])
        except Exception as exc:
            print(f"[skip pulse_bottom_5] {sym}: {exc}", file=sys.stderr, flush=True)

    return rows


def ff_state_row(sym: str, d: pd.DataFrame, asof: str, layer: str) -> list[str] | None:
    """One ff_state row for last settled bar, or None if FF state unavailable."""
    try:
        st = fund_flow_state(d)
    except Exception as exc:
        print(f"[skip ff_state] {sym}: {exc}", file=sys.stderr, flush=True)
        return None
    if st is None:
        return None
    return [
        asof,
        sym,
        f"{st['fundtrend']:.4f}",
        f"{st['bull']:.4f}",
        str(st["color"]),
        "TRUE" if st["ft_ge_50"] else "FALSE",
        "TRUE" if st["white_circle"] else "FALSE",
        "TRUE" if st["yellow"] else "FALSE",
        layer,
    ]


def write_tab(
    sa_json: str,
    spreadsheet_id: str,
    tab: str,
    header: list[str],
    rows: list[list[str]],
) -> None:
    gc = _client(sa_json)
    sh = gc.open_by_key(spreadsheet_id)
    ws = _ensure_worksheet(sh, tab)
    body = [header] + rows
    need = max(len(body) + 10, 100)
    if ws.row_count < need:
        ws.resize(rows=need)
    print(f"Sheets: overwrite {tab} data_rows={len(rows)}", flush=True)
    _clear_and_write(ws, body)


def maybe_refresh_universe_tab(
    sa_json: str, spreadsheet_id: str, univ: list[tuple[str, str]], path: Path
) -> None:
    """Optional: rewrite universe tab from CSV (keeps Sheet in sync with repo)."""
    gc = _client(sa_json)
    sh = gc.open_by_key(spreadsheet_id)
    ws = _ensure_worksheet(sh, TAB_UNIVERSE)
    body: list[list[str]] = [
        ["ticker", "layer", "group_or_role", "name", "note"]
    ]
    with path.open(encoding="utf-8") as f:
        for r in csv.DictReader(f):
            body.append(
                [
                    r.get("ticker", ""),
                    r.get("layer", ""),
                    r.get("group_or_role", ""),
                    r.get("name", ""),
                    r.get("note", ""),
                ]
            )
    _clear_and_write(ws, body)
    print(f"Sheets: refreshed {TAB_UNIVERSE} n={len(body) - 1}", flush=True)


def main() -> int:
    ap = argparse.ArgumentParser(description="Pulse events + ff_state → Sheet")
    ap.add_argument("--universe", type=Path, default=UNIVERSE_CSV)
    ap.add_argument("--sheet-id", default=os.environ.get("PULSE_SHEET_ID", DEFAULT_SHEET_ID))
    ap.add_argument("--out-csv", type=Path, default=Path("out/pulse_events.csv"))
    ap.add_argument(
        "--out-ff-csv",
        type=Path,
        default=Path("out/pulse_ff_state.csv"),
        help="Local CSV for ff_state tab",
    )
    ap.add_argument(
        "--refresh-universe",
        action="store_true",
        help="Also overwrite universe tab from pulse_universe.csv",
    )
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="Compute + local CSV only; skip Sheets even if secret present",
    )
    args = ap.parse_args()

    if not args.universe.exists():
        print(f"FATAL: universe file missing: {args.universe}", file=sys.stderr)
        return 2

    univ = load_universe(args.universe)
    layer_of = {t: layer for t, layer in univ}
    syms = [t for t, _ in univ]
    asof = asof_date_str()
    print(
        f"pulse_events universe={len(syms)} asof={asof} "
        f"hkt={hkt_now_str()} et={asof_et_str()}",
        flush=True,
    )

    data = download(syms)
    events: list[list[str]] = []
    ff_rows: list[list[str]] = []
    stats = {
        "universe": len(syms),
        "downloaded": len(data),
        "norm_fail": 0,
        "screened": 0,
        "ff_state": 0,
        "by_signal": {},
    }

    # Benchmarks for ⑤ (must be in universe: SPY + RSP)
    spy_df = None
    rsp_df = None
    raw_spy = data.get("SPY")
    if raw_spy is not None:
        spy_df = drop_forming(normalize_ohlc(raw_spy))
    raw_rsp = data.get("RSP")
    if raw_rsp is not None:
        rsp_df = drop_forming(normalize_ohlc(raw_rsp))
    breadth_ok = (
        build_breadth_ok(spy_df, rsp_df)
        if spy_df is not None
        else pd.Series(dtype=bool)
    )
    if spy_df is None:
        print("[warn] SPY missing — pulse_bottom_5 skipped", file=sys.stderr, flush=True)
    elif rsp_df is None:
        print("[warn] RSP missing — breadth_ok all False", file=sys.stderr, flush=True)

    for sym in syms:
        raw = data.get(sym)
        if raw is None:
            continue
        d = normalize_ohlc(raw)
        if d is None:
            stats["norm_fail"] += 1
            continue
        d = drop_forming(d)
        if d is None or len(d) < MIN_BARS:
            stats["norm_fail"] += 1
            continue
        stats["screened"] += 1
        rows = screen_symbol(
            sym, d, asof, spy=spy_df, breadth_ok=breadth_ok
        )
        for row in rows:
            sig = row[2]
            stats["by_signal"][sig] = stats["by_signal"].get(sig, 0) + 1
        events.extend(rows)

        ff = ff_state_row(sym, d, asof, layer_of.get(sym, "unknown"))
        if ff is not None:
            ff_rows.append(ff)
            stats["ff_state"] += 1

    events.sort(key=lambda r: (r[2], r[1]))
    ff_rows.sort(key=lambda r: r[1])

    args.out_csv.parent.mkdir(parents=True, exist_ok=True)
    with args.out_csv.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(HEADER)
        w.writerows(events)
    print(
        f"wrote {args.out_csv} rows={len(events)} screened={stats['screened']} "
        f"dl={stats['downloaded']} norm_fail={stats['norm_fail']} "
        f"by_signal={stats['by_signal']}",
        flush=True,
    )

    args.out_ff_csv.parent.mkdir(parents=True, exist_ok=True)
    with args.out_ff_csv.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(FF_STATE_HEADER)
        w.writerows(ff_rows)
    print(f"wrote {args.out_ff_csv} rows={len(ff_rows)}", flush=True)

    sa = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()
    if args.dry_run:
        print("Sheets: dry-run skip", flush=True)
        return 0
    if not sa:
        print("Sheets: skip (no GOOGLE_SERVICE_ACCOUNT_JSON)", flush=True)
        return 0

    write_tab(sa, args.sheet_id, TAB_EVENTS, HEADER, events)
    write_tab(sa, args.sheet_id, TAB_FF_STATE, FF_STATE_HEADER, ff_rows)
    if args.refresh_universe:
        maybe_refresh_universe_tab(sa, args.sheet_id, univ, args.universe)
    print("done", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
