#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Post-close 主力能量 screener → ExtraScan QQscan columns L + M.

L: weekly 主力做多 上穿 主力做空 (Tongdaxin / Banker 主力能量 tab)
M: daily  主力做多 上穿 主力做空

Shared filters (daily bars):
  market cap > 100M, volume > 300k, close > $5, close > EMA200

Universe: US equities ex-ETF/fund (same as yellowALL / universe_us_ex_etf_fund).
One yfinance daily download pass; weekly bars via W-FRI resample.
Does not touch A/G/H/J or Fund Flow tabs. Reuses GOOGLE_SERVICE_ACCOUNT_JSON.
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

import pandas as pd
import yfinance as yf

from marketdata import (
    _run_with_timeout,
    download_daily,
    format_status,
    freshness_ok,
    last_complete_us_session_date,
)
from run_extrascan import (
    BATCH,
    DOWNLOAD_BACKOFF_SEC,
    DOWNLOAD_RETRIES,
    LOOKBACK_DAYS,
    SYMBOL_RETRY_RETRIES,
    _extract_batch_frames,
    asof_et_str,
    drop_forming,
    hkt_now_str,
    ny_now,
    parse_symbols,
    universe_us_ex_etf_fund,
)
from sheets_writer import QQSCAN_HEADERS, _client, write_column

DEFAULT_SHEET_ID = "1FSOmey7ALHFuYXxZSgjZnjWA5zpmbiv48PWAJD_yYRk"
TAB_QQ = "QQscan"

MCAP_MIN = 100_000_000
VOL_MIN = 300_000
CLOSE_MIN = 5.0
EMA_LEN = 200
MIN_DAILY_BARS = 220  # EMA200 + cushion
MIN_WEEKLY_BARS = 40  # HHV36 warm-up + cushion
MCAP_WORKERS = 8


def sma_cn(src: pd.Series, length: int) -> pd.Series:
    """Tongdaxin SMA(X, N, 1): y = (src + (N - 1) * y_prev) / N."""
    vals = src.astype(float).tolist()
    out: list[float] = []
    y = float("nan")
    for v in vals:
        if pd.isna(v):
            out.append(float("nan"))
            continue
        if pd.isna(y):
            y = float(v)
        else:
            y = (float(v) + (length - 1) * y) / length
        out.append(y)
    return pd.Series(out, index=src.index)


def zhuli_bull_bear_series(df: pd.DataFrame):
    """Philip / Tongdaxin 主力能量 (9)(36) — same as banker_screener_app_new.

    bear = SMA_CN((HHV36-C)/(HHV36-LLV36)*100, 3)
    bull = SMA_CN((C-LLV9)/(HHV9-LLV9)*100, 5) - 8
    """
    if df is None or len(df) < 36:
        return None, None
    h = df["High"].astype(float)
    l = df["Low"].astype(float)
    c = df["Close"].astype(float)
    hh36 = h.rolling(36, min_periods=36).max()
    ll36 = l.rolling(36, min_periods=36).min()
    rng36 = hh36 - ll36
    var1 = (hh36 - c) / rng36 * 100.0
    var1 = var1.mask(rng36.eq(0) & rng36.notna(), 0.0)
    bear = sma_cn(var1, 3)

    hh9 = h.rolling(9, min_periods=9).max()
    ll9 = l.rolling(9, min_periods=9).min()
    rng9 = hh9 - ll9
    var2 = (c - ll9) / rng9 * 100.0
    var2 = var2.mask(rng9.eq(0) & rng9.notna(), 0.0)
    bull = sma_cn(var2, 5) - 8.0
    return bull, bear


def zhuli_cross_up(df: pd.DataFrame) -> bool:
    """主力做多 上穿 主力做空：bull crosses above bear on last bar."""
    bull, bear = zhuli_bull_bear_series(df)
    if bull is None or bear is None or len(bull) < 2:
        return False
    b0, b1 = float(bull.iloc[-1]), float(bull.iloc[-2])
    e0, e1 = float(bear.iloc[-1]), float(bear.iloc[-2])
    if any(pd.isna(x) for x in (b0, b1, e0, e1)):
        return False
    return b0 > e0 and b1 <= e1


def zhuli_cross_down(df: pd.DataFrame) -> bool:
    """主力做多 下穿 主力做空：bull crosses below bear on last bar."""
    bull, bear = zhuli_bull_bear_series(df)
    if bull is None or bear is None or len(bull) < 2:
        return False
    b0, b1 = float(bull.iloc[-1]), float(bull.iloc[-2])
    e0, e1 = float(bear.iloc[-1]), float(bear.iloc[-2])
    if any(pd.isna(x) for x in (b0, b1, e0, e1)):
        return False
    return b0 < e0 and b1 >= e1


def normalize_ohlcv(df: pd.DataFrame) -> pd.DataFrame | None:
    cols = {str(c).lower(): c for c in df.columns}
    need = ("open", "high", "low", "close", "volume")
    if not all(k in cols for k in need):
        return None
    out = pd.DataFrame(
        {
            "Open": pd.to_numeric(df[cols["open"]], errors="coerce"),
            "High": pd.to_numeric(df[cols["high"]], errors="coerce"),
            "Low": pd.to_numeric(df[cols["low"]], errors="coerce"),
            "Close": pd.to_numeric(df[cols["close"]], errors="coerce"),
            "Volume": pd.to_numeric(df[cols["volume"]], errors="coerce"),
        }
    ).dropna()
    if not isinstance(out.index, pd.DatetimeIndex):
        out.index = pd.to_datetime(out.index)
    return out if len(out) >= MIN_DAILY_BARS else None


def to_weekly(d: pd.DataFrame) -> pd.DataFrame:
    """Resample daily OHLCV to W-FRI (same as us-consider / mcdx_exclude)."""
    w = (
        d.resample("W-FRI")
        .agg(
            {
                "Open": "first",
                "High": "max",
                "Low": "min",
                "Close": "last",
                "Volume": "sum",
            }
        )
        .dropna(subset=["Close"])
    )
    return w


def drop_forming_weekly(w: pd.DataFrame) -> pd.DataFrame:
    """Drop incomplete W-FRI bar before that Friday 16:00 ET (Banker-style)."""
    if w is None or w.empty:
        return w
    now = ny_now()
    last = pd.Timestamp(w.index[-1])
    # Yahoo / resample label = week Friday; treat as forming until Fri 16:00 ET
    fri_close = datetime(last.year, last.month, last.day, 16, 0)
    if now < fri_close:
        return w.iloc[:-1]
    return w


def daily_filters_ok(d: pd.DataFrame) -> bool:
    """close>$5, vol>300k, close>EMA200 on last completed daily bar."""
    if d is None or len(d) < MIN_DAILY_BARS:
        return False
    c = d["Close"].astype(float)
    v = d["Volume"].astype(float)
    close = float(c.iloc[-1])
    vol = float(v.iloc[-1])
    if pd.isna(close) or pd.isna(vol):
        return False
    if close <= CLOSE_MIN or vol <= VOL_MIN:
        return False
    ema200 = c.ewm(span=EMA_LEN, adjust=False, min_periods=EMA_LEN).mean().iloc[-1]
    if pd.isna(ema200) or close <= float(ema200):
        return False
    return True


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
            print(
                f"  batch short {len(got)}/{len(batch)} "
                f"(attempt {attempt + 1}/{DOWNLOAD_RETRIES})",
                flush=True,
            )
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
                f"  {sym}: error {e} (symbol retry {attempt + 1}/{SYMBOL_RETRY_RETRIES})",
                flush=True,
            )
        time.sleep(DOWNLOAD_BACKOFF_SEC * (attempt + 1))
    return None


def download(syms: list[str]) -> dict[str, pd.DataFrame]:
    expected = last_complete_us_session_date()
    frames, stats = download_daily(syms, expected, lookback_days=LOOKBACK_DAYS, batch_size=BATCH)
    download.last_stats = stats
    download.last_expected = expected
    return frames


MCAP_CALL_TIMEOUT_SEC = 30  # per lookup; yfinance has no cancel
MCAP_RETRY_ROUNDS = 4  # retries after the first pass, errored symbols only
MCAP_BACKOFF_SEC = 20  # round r waits r * backoff
MCAP_RETRY_WORKERS = 2  # gentler on Yahoo while rate-limited
MCAP_BUDGET_SEC = 600  # whole mcap step incl. retries (job timeout 60 min)
MCAP_MAX_ERR_FRAC = 0.10  # errors/candidates above this BLOCKS the write


def market_cap_lookup(sym: str) -> Optional[float]:
    """Market cap from Yahoo, or None when it could not be read (error/missing)."""
    try:
        t = yf.Ticker(sym)
        cap = None
        try:
            fi = getattr(t, "fast_info", None)
            if fi is not None:
                cap = getattr(fi, "market_cap", None) or (
                    fi.get("market_cap") if hasattr(fi, "get") else None
                )
        except Exception:
            pass
        if cap is None:
            info = t.info or {}
            cap = info.get("marketCap")
        if cap is None:
            return None
        cap = float(cap)
        return None if pd.isna(cap) else cap
    except Exception:
        return None


def market_cap_ok(sym: str) -> bool:
    cap = market_cap_lookup(sym)
    return cap is not None and cap > MCAP_MIN


def filter_mcap(
    symbols: list[str],
    *,
    lookup=None,
    sleep=time.sleep,
    clock=time.monotonic,
    budget: float = MCAP_BUDGET_SEC,
    rounds: int = MCAP_RETRY_ROUNDS,
    backoff: float = MCAP_BACKOFF_SEC,
    call_timeout: float = MCAP_CALL_TIMEOUT_SEC,
) -> tuple[list[str], dict]:
    """Return (symbols with mcap > MCAP_MIN, info).

    A lookup that errors / times out / returns no cap is an *error*, not a fail;
    errored symbols are retried with backoff inside a wall-clock budget.
    A valid cap <= MCAP_MIN is a normal fail. info has candidates, mcap_ok
    (valid lookups), mcap_pass, mcap_fail, mcap_err, err_symbols, rounds.
    """
    lookup = lookup or market_cap_lookup
    syms = sorted(set(symbols))
    info = {"candidates": len(syms), "mcap_ok": 0, "mcap_pass": 0, "mcap_fail": 0,
            "mcap_err": 0, "err_symbols": [], "rounds": 0}
    if not syms:
        return [], info
    print(f"mcap check n={len(syms)} workers={MCAP_WORKERS}", flush=True)
    start = clock()
    caps: dict[str, float] = {}
    pending = list(syms)
    for r in range(rounds + 1):
        if r:
            left = budget - (clock() - start)
            wait = backoff * r
            if left <= wait + 1:
                print(f"  mcap budget exhausted before retry {r}; err={len(pending)}", flush=True)
                break
            print(f"  mcap retry {r}/{rounds} n={len(pending)} after {wait:.0f}s", flush=True)
            sleep(wait)
        left = budget - (clock() - start)
        if left <= 1:
            break
        timeout = min(call_timeout, left)
        workers = MCAP_WORKERS if r == 0 else MCAP_RETRY_WORKERS
        info["rounds"] = r + 1
        nxt: list[str] = []
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futs = {ex.submit(_run_with_timeout, lookup, timeout, s): s for s in pending}
            for i, fut in enumerate(as_completed(futs), 1):
                sym = futs[fut]
                try:
                    cap = fut.result()
                except Exception:
                    cap = None
                if cap is None:
                    nxt.append(sym)
                else:
                    caps[sym] = float(cap)
                if r == 0 and (i % 25 == 0 or i == len(futs)):
                    print(f"  mcap {i}/{len(futs)} ok_so_far={len(caps)}", flush=True)
        pending = sorted(nxt)
        if not pending:
            break
    ok = sorted(s for s, c in caps.items() if c > MCAP_MIN)
    info.update(mcap_ok=len(caps), mcap_pass=len(ok), mcap_fail=len(caps) - len(ok),
                mcap_err=len(pending), err_symbols=pending)
    print(f"  mcap done ok={len(caps)} pass={len(ok)} err={len(pending)} rounds={info['rounds']}",
          flush=True)
    return ok, info


def mcap_lookup_ok(info: dict, max_err_frac: float = MCAP_MAX_ERR_FRAC) -> bool:
    """False when errored lookups exceed max_err_frac of mcap candidates."""
    n = int(info.get("candidates") or 0)
    if n == 0:
        return True
    return (int(info.get("mcap_err") or 0) / n) <= max_err_frac


def screen(syms: list[str]) -> tuple[list[str], list[str], dict]:
    """Return (weekly_L_hits, daily_M_hits, stats)."""
    print(f"universe={len(syms)} asof={asof_et_str()} hkt={hkt_now_str()}", flush=True)
    data = download(syms)
    cand_l: list[str] = []
    cand_m: list[str] = []
    stats = {
        "universe": len(syms),
        "downloaded": len(data),
        "norm_fail": 0,
        "filter_fail": 0,
        "screened": 0,
        "cross_l": 0,
        "cross_m": 0,
    }
    # Last completed daily bar of every symbol that reached the screen
    # (downloaded, normalized, >= MIN_DAILY_BARS after drop_forming) — the same
    # screened-only denominator Daily ExtraScan uses for its .90 gate.
    bar_dates: list = []
    for sym in syms:
        raw = data.get(sym)
        if raw is None:
            continue
        d = normalize_ohlcv(raw)
        if d is None:
            stats["norm_fail"] += 1
            continue
        d = drop_forming(d)
        if d is None or len(d) < MIN_DAILY_BARS:
            stats["norm_fail"] += 1
            continue
        bar_dates.append(pd.Timestamp(d.index[-1]).date())
        if not daily_filters_ok(d):
            stats["filter_fail"] += 1
            continue
        stats["screened"] += 1
        try:
            if zhuli_cross_up(d):
                cand_m.append(sym)
                stats["cross_m"] += 1
        except Exception as exc:
            print(f"[skip M] {sym}: {exc}", file=sys.stderr, flush=True)

        try:
            w = drop_forming_weekly(to_weekly(d))
            if w is not None and len(w) >= MIN_WEEKLY_BARS and zhuli_cross_up(w):
                cand_l.append(sym)
                stats["cross_l"] += 1
        except Exception as exc:
            print(f"[skip L] {sym}: {exc}", file=sys.stderr, flush=True)

    print(
        f"pre-mcap L={len(cand_l)} M={len(cand_m)} screened={stats['screened']} "
        f"filter_fail={stats['filter_fail']} norm_fail={stats['norm_fail']}",
        flush=True,
    )
    # Mcap only on technical hits (union) to avoid 6k info calls.
    need_cap = sorted(set(cand_l) | set(cand_m))
    cap_list, mcap_info = filter_mcap(need_cap)
    cap_ok = set(cap_list)
    hits_l = sorted(s for s in cand_l if s in cap_ok)
    hits_m = sorted(s for s in cand_m if s in cap_ok)
    stats["mcap_pass"] = len(cap_ok)
    stats["mcap"] = mcap_info
    stats["L"] = len(hits_l)
    stats["M"] = len(hits_m)
    print(f"post-mcap L={len(hits_l)} M={len(hits_m)}", flush=True)
    stats["bar_dates"] = bar_dates
    return hits_l, hits_m, stats


def lm_freshness(bar_dates: list, expected, universe_n: int) -> tuple[bool, dict]:
    """.90 gate with Daily's screened-only denominator (threshold unchanged).

    Uses marketdata.freshness_ok without universe_n, i.e. the Daily definition:
    fresh = screened symbols whose last bar == expected session,
    denom = screened symbols. Full universe size is reported for info only.
    """
    ok, info = freshness_ok(list(bar_dates), expected)
    info["universe"] = universe_n
    info["denom"] = "screened"
    return ok, info


def _looks_like_header(cell: str) -> bool:
    s = (cell or "").strip()
    if not s:
        return False
    if s in QQSCAN_HEADERS.values():
        return True
    markers = (
        "Strong_Stocks",
        "Maggiestocks",
        "yellowETF",
        "yellowALL",
        "whiteball",
        "主力",
        "holdings",
        "∪",
    )
    return any(m in s for m in markers)


def refresh_qqscan_headers_preserving(sa: str, sheet_id: str) -> None:
    """Ensure row-1 labels on A/G/H/J/L/M without dropping existing symbols.

    If row 1 is still a ticker (legacy), prepend the label and rewrite the column.
    If row 1 is already a header (any known wording), replace with canonical label only.
    """
    gc = _client(sa)
    sh = gc.open_by_key(sheet_id)
    try:
        ws = sh.worksheet(TAB_QQ)
    except Exception as exc:
        print(f"QQscan tab missing; skip header refresh: {exc}", flush=True)
        return
    col_index = {c: i for i, c in enumerate("ABCDEFGHIJKLMNOPQRSTUVWXYZ", 1)}
    for col, label in QQSCAN_HEADERS.items():
        idx = col_index[col]
        vals = [ (v or "").strip() for v in ws.col_values(idx) ]
        vals = [v for v in vals]  # keep empties? strip trailing empties below
        while vals and vals[-1] == "":
            vals.pop()
        if not vals:
            write_column(sa, sheet_id, TAB_QQ, col, [], header=label)
            continue
        if vals[0] == label:
            continue
        if _looks_like_header(vals[0]):
            symbols = vals[1:]
        else:
            # Legacy: row1 was first symbol
            symbols = vals
        write_column(sa, sheet_id, TAB_QQ, col, symbols, header=label)
        print(f"header preserve {col}: symbols={len(symbols)}", flush=True)

def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(
        description="主力能量 weekly→QQscan L, daily→QQscan M"
    )
    p.add_argument(
        "--sheet-id",
        default=os.environ.get("EXTRASCAN_SHEET_ID", DEFAULT_SHEET_ID),
    )
    p.add_argument("--dry-run", action="store_true")
    p.add_argument(
        "--symbols",
        default="",
        help="Comma-separated smoke list (skips full universe)",
    )
    p.add_argument(
        "--skip-headers-refresh",
        action="store_true",
        help="Do not patch A/G/H/J/L/M row-1 labels",
    )
    args = p.parse_args(argv)

    sa = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()
    if not args.dry_run and not sa:
        print("ERROR: set GOOGLE_SERVICE_ACCOUNT_JSON", file=sys.stderr)
        return 2

    if args.symbols.strip():
        syms = parse_symbols(args.symbols)
        print(f"smoke symbols n={len(syms)}", flush=True)
    else:
        syms = universe_us_ex_etf_fund()
        print(f"universe_us_ex_etf_fund n={len(syms)}", flush=True)

    hits_l, hits_m, stats = screen(syms)
    bar_dates = stats.pop("bar_dates", [])
    mcap_info = stats.pop("mcap", None) or {"candidates": 0, "mcap_ok": 0, "mcap_err": 0}
    print("stats:", stats, flush=True)
    print(f"L sample: {hits_l[:20]}", flush=True)
    print(f"M sample: {hits_m[:20]}", flush=True)

    expected = getattr(download, "last_expected", last_complete_us_session_date())
    dl = getattr(download, "last_stats", None)
    if dl is None:
        ok, info = False, {}
        yahoo = retry = stooq = fresh_n = denom_n = 0
        extra = "denom=screened"
    else:
        ok, info = lm_freshness(bar_dates, expected, len(syms))
        yahoo, retry, stooq = dl.yahoo, dl.retry, dl.stooq
        fresh_n, denom_n = int(info.get("fresh") or 0), int(info.get("screened") or 0)
        extra = f"denom=screened universe={len(syms)} universe_fresh={dl.fresh}/{dl.requested}"
        if not denom_n:
            ok = False
    mcap_ok_gate = mcap_lookup_ok(mcap_info)
    extra += (f" mcap_ok={mcap_info.get('mcap_ok', 0)} mcap_err={mcap_info.get('mcap_err', 0)}"
              f" candidates={mcap_info.get('candidates', 0)}")
    reasons = ([] if ok else ["freshness"]) + ([] if mcap_ok_gate else ["mcap_lookup"])
    if reasons:
        extra += " reason=" + ",".join(reasons)
    result_word = "BLOCKED" if reasons else ("DRY_RUN" if args.dry_run else "WROTE")
    print(format_status(session=expected, writer="Zhuli", fresh=fresh_n, denom=denom_n or 1,
                        yahoo=yahoo, retry=retry, stooq=stooq, result=result_word,
                        extra=extra), flush=True)
    if not ok:
        print(f"BLOCKED (freshness < 0.90): {info} — not written", file=sys.stderr, flush=True)
        return 3
    if not mcap_ok_gate:
        print(
            f"BLOCKED (mcap lookup errors {mcap_info.get('mcap_err')}/{mcap_info.get('candidates')}"
            f" > {MCAP_MAX_ERR_FRAC:.0%}): {mcap_info.get('err_symbols')} — not written",
            file=sys.stderr, flush=True,
        )
        return 4

    if args.dry_run:
        print("dry-run OK (no Sheet write)", flush=True)
        return 0

    n_l = write_column(
        sa, args.sheet_id, TAB_QQ, "L", hits_l, header=QQSCAN_HEADERS["L"]
    )
    n_m = write_column(
        sa, args.sheet_id, TAB_QQ, "M", hits_m, header=QQSCAN_HEADERS["M"]
    )
    if not args.skip_headers_refresh:
        refresh_qqscan_headers_preserving(sa, args.sheet_id)
    print(f"QQscan done L={n_l} M={n_m}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
