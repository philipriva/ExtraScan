#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""QQscan new columns C / D / E / F (additive; does NOT touch A/G/H/J/L/M).

C: etf_list.txt ETFs firing TD 賣出9 (Philip mapping S9 = RED count == 9,
   i.e. 9th consecutive close < close[4])  ∪ their holdings leaders
D: etf_list.txt ETFs firing TD 買入1 (Philip mapping B1 = GREEN count == 1,
   i.e. 1st close > close[4] after a non-green bar) ∪ their holdings leaders
E: all US stocks ex-ETF/fund (universe_us_ex_etf_fund) with daily 主力做多
   上穿 主力做空 on the last completed session (no holdings step), then
   Philip filters (2026-11-10): close > $5, close > daily EMA9,
   close > daily EMA200, market cap > 100M USD (no volume filter).
   Mcap source = same as QQscan M (yfinance fast_info.market_cap, then
   info.marketCap); missing mcap → excluded and counted (never guessed).
F: etf_list.txt ETFs with daily 主力做多 上穿 主力做空 ∪ their holdings leaders

Definitions are reused, not re-invented:
- TD setup counts: Philip's SEA combined screener / us-exclude-screener
  ``td_setup_counts`` ("User mapping (opposite of classic DeMark names)":
  S1/S9 = RED = close < close[4]; B1/B9 = GREEN = close > close[4]).
- 主力能量 cross: ``write_zhuli_lm.zhuli_cross_up`` (same as QQscan M).
- Holdings leaders: ``write_etf_leaders.scrape_etf_holdings`` +
  ``analyze_holdings`` (same as QQscan G; ETF tickers themselves included).

Safety:
- Writes only QQscan C/D/E/F via ``write_column`` (per-column clear+write).
- Headers live in QQSCAN_CDEF_HEADERS, NOT in QQSCAN_HEADERS, so existing
  writers' header-refresh loops never touch C–F.
- Fail-closed freshness: expected bar = last complete US session (or --asof);
  each selected symbol's last bar must equal it, and fresh/universe (full
  denominator) must be >= 0.90 for that column group, else that group is not
  written (prior values left untouched) and exit code is non-zero.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date
from pathlib import Path
from typing import Iterable, Optional

import pandas as pd

from run_extrascan import (
    download,
    drop_forming,
    last_complete_us_session_date,
    load_etf_list,
    parse_symbols,
    universe_us_ex_etf_fund,
)

DEFAULT_SHEET_ID = "1FSOmey7ALHFuYXxZSgjZnjWA5zpmbiv48PWAJD_yYRk"
TAB_QQ = "QQscan"
MIN_FRESH_FRAC = 0.90
# 主力能量 needs HHV/LLV36 + 1 prior bar for the cross; TD needs close[4].
MIN_BARS_ZHULI = 38
MIN_BARS_TD = 6
# Column E filters (Philip 2026-11-10). No volume filter by request.
E_MCAP_MIN = 100_000_000
E_CLOSE_MIN = 5.0
E_EMA_FAST = 9
E_EMA_SLOW = 200

# Kept separate from sheets_writer.QQSCAN_HEADERS on purpose (see docstring).
QQSCAN_CDEF_HEADERS: dict[str, str] = {
    "C": "TD賣出9 ETF + holdings leaders",
    "D": "TD買入1 ETF + holdings leaders",
    "E": "日線主力做多上穿做空 (全美股 ex-ETF/fund; mcap>100M, >$5, >EMA9, >EMA200)",
    "F": "日線主力做多上穿做空 ETF + holdings leaders",
}
ALLOWED_COLS = tuple(QQSCAN_CDEF_HEADERS)
ETF_COLS = ("C", "D", "F")


# ---------------------------------------------------------------- signals
def td_setup_counts(close: pd.Series) -> tuple[pd.Series, pd.Series]:
    """Return (red/S count, green/B count). S=RED=close<close[4]; B=GREEN=close>close[4].

    Verbatim logic of Philip's td_setup_counts (us-exclude-screener/run_exclude.py,
    SEA combined screener). Counts are not capped at 9.
    """
    c = close.astype(float)
    b_count = pd.Series(0, index=c.index, dtype=int)
    s_count = pd.Series(0, index=c.index, dtype=int)
    bc = sc = 0
    vals = c.tolist()
    for i in range(len(vals)):
        if i < 4 or pd.isna(vals[i]) or pd.isna(vals[i - 4]):
            bc = 0
            sc = 0
        else:
            bc = bc + 1 if vals[i] < vals[i - 4] else 0
            sc = sc + 1 if vals[i] > vals[i - 4] else 0
        b_count.iloc[i] = bc
        s_count.iloc[i] = sc
    return b_count, s_count


def td_code_hit(close: pd.Series, code: str) -> bool:
    """code in {B1,B9,S1,S9}; evaluated on the last bar (same as td_hit)."""
    if close is None or len(close) < MIN_BARS_TD:
        return False
    red, green = td_setup_counts(close)
    r, g = int(red.iloc[-1]), int(green.iloc[-1])
    return {"S1": r == 1, "S9": r == 9, "B1": g == 1, "B9": g == 9}.get(code.upper(), False)


def td_buy1(close: pd.Series) -> bool:
    """TD 買入1 = Philip B1 (GREEN count == 1)."""
    return td_code_hit(close, "B1")


def td_sell9(close: pd.Series) -> bool:
    """TD 賣出9 = Philip S9 (RED count == 9)."""
    return td_code_hit(close, "S9")


def zhuli_daily_cross_up(d: pd.DataFrame) -> bool:
    from write_zhuli_lm import zhuli_cross_up

    if d is None or len(d) < MIN_BARS_ZHULI:
        return False
    return bool(zhuli_cross_up(d))


# ---------------------------------------------------------------- data prep
def prepare_daily(raw: pd.DataFrame, asof: Optional[date]) -> Optional[pd.DataFrame]:
    """OHLC(V) numeric frame, completed bars only, truncated to <= asof."""
    if raw is None or raw.empty:
        return None
    cols = {str(c).lower(): c for c in raw.columns}
    if not all(k in cols for k in ("high", "low", "close")):
        return None
    out = pd.DataFrame(
        {
            k.capitalize(): pd.to_numeric(raw[cols[k]], errors="coerce")
            for k in ("open", "high", "low", "close")
            if k in cols
        }
    ).dropna(subset=["High", "Low", "Close"])
    if not isinstance(out.index, pd.DatetimeIndex):
        out.index = pd.to_datetime(out.index)
    if getattr(out.index, "tz", None) is not None:
        out.index = out.index.tz_localize(None)
    out = out.sort_index()
    if asof is not None:
        out = out[out.index.date <= asof]
    else:
        out = drop_forming(out)
    return out if out is not None and not out.empty else None


def evaluate(
    frames: dict[str, pd.DataFrame],
    universe: list[str],
    expected: date,
    checks: dict[str, callable],
    asof: Optional[date] = None,
) -> dict:
    """Run named checks over universe; only symbols whose last bar == expected count.

    Freshness uses the FULL universe as denominator (missing/stale = not fresh).
    """
    hits: dict[str, list[str]] = {k: [] for k in checks}
    fresh = 0
    stale: list[str] = []
    missing: list[str] = []
    last_bars: dict[str, int] = {}
    for sym in universe:
        d = prepare_daily(frames.get(sym), asof)
        if d is None:
            missing.append(sym)
            continue
        lb = pd.Timestamp(d.index[-1]).date()
        last_bars[lb.isoformat()] = last_bars.get(lb.isoformat(), 0) + 1
        if lb != expected:
            stale.append(sym)
            continue
        fresh += 1
        for name, fn in checks.items():
            try:
                if fn(d):
                    hits[name].append(sym)
            except Exception as exc:  # pragma: no cover - defensive
                print(f"[skip {name}] {sym}: {exc}", file=sys.stderr, flush=True)
    n = len(universe)
    frac = (fresh / n) if n else 0.0
    return {
        "hits": {k: sorted(set(v)) for k, v in hits.items()},
        "universe": n,
        "fresh": fresh,
        "frac": round(frac, 4),
        "fresh_ok": n > 0 and frac >= MIN_FRESH_FRAC,
        "expected": expected.isoformat(),
        "stale": sorted(stale),
        "missing": sorted(missing),
        "last_bar_hist": dict(sorted(last_bars.items())),
    }


def fetch_mcap(sym: str) -> Optional[float]:
    """Market cap via the same source/order as write_zhuli_lm.market_cap_ok (M).

    Returns None when unavailable (caller excludes + counts; never guessed).
    """
    import yfinance as yf

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


def _ema_last(c: pd.Series, n: int) -> float:
    # Same EMA style as write_zhuli_lm.daily_filters_ok (adjust=False, min_periods=n).
    return float(c.ewm(span=n, adjust=False, min_periods=n).mean().iloc[-1])


def e_price_filter_reason(d: pd.DataFrame) -> Optional[str]:
    """None if passes price/EMA filters, else the first failing filter name."""
    c = d["Close"].astype(float)
    close = float(c.iloc[-1])
    if pd.isna(close) or close <= E_CLOSE_MIN:
        return "close_le_5"
    e9 = _ema_last(c, E_EMA_FAST)
    if pd.isna(e9) or close <= e9:
        return "close_le_ema9"
    if len(c) < E_EMA_SLOW:
        return "bars_lt_200_for_ema200"
    e200 = _ema_last(c, E_EMA_SLOW)
    if pd.isna(e200) or close <= e200:
        return "close_le_ema200"
    return None


def apply_e_filters(
    frames: dict[str, pd.DataFrame],
    cross_hits: list[str],
    asof: Optional[date],
    mcap_fn=None,
    workers: int = 8,
) -> tuple[list[str], dict]:
    """Sequential funnel on cross hits: price/EMA filters, then mcap (only survivors)."""
    from concurrent.futures import ThreadPoolExecutor

    mcap_fn = mcap_fn or fetch_mcap
    drops: dict[str, list[str]] = {
        "close_le_5": [],
        "close_le_ema9": [],
        "bars_lt_200_for_ema200": [],
        "close_le_ema200": [],
        "mcap_missing": [],
        "mcap_le_100m": [],
    }
    survivors: list[str] = []
    for sym in cross_hits:
        d = prepare_daily(frames.get(sym), asof)
        reason = e_price_filter_reason(d)
        if reason:
            drops[reason].append(sym)
        else:
            survivors.append(sym)
    caps: dict[str, Optional[float]] = {}
    if survivors:
        with ThreadPoolExecutor(max_workers=workers) as ex:
            for sym, cap in zip(survivors, ex.map(mcap_fn, survivors)):
                caps[sym] = cap
    kept: list[str] = []
    for sym in survivors:
        cap = caps.get(sym)
        if cap is None:
            drops["mcap_missing"].append(sym)
        elif cap <= E_MCAP_MIN:
            drops["mcap_le_100m"].append(sym)
        else:
            kept.append(sym)
    stats = {
        "cross_hits": len(cross_hits),
        "after_price_ema": len(survivors),
        "kept": len(kept),
        "dropped_counts": {k: len(v) for k, v in drops.items()},
        "dropped": {k: sorted(v) for k, v in drops.items()},
    }
    return sorted(kept), stats


def column_with_leaders(etfs: Iterable[str], leaders_by_etf: dict[str, list[str]]) -> list[str]:
    """Same shape as QQscan G: ETF tickers always ∪ their holdings leaders."""
    out: set[str] = set()
    for e in etfs:
        out.add(e)
        out.update(leaders_by_etf.get(e, []))
    return sorted(out)


def scrape_leaders(etfs: list[str]) -> dict[str, list[str]]:
    """Reuse write_etf_leaders scrape + analyze verbatim (one pass per ETF)."""
    from playwright.sync_api import sync_playwright

    from write_etf_leaders import analyze_holdings, scrape_etf_holdings

    out: dict[str, list[str]] = {}
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36"
            )
        )
        page = context.new_page()
        for i, etf in enumerate(etfs, 1):
            syms = scrape_etf_holdings(page, etf)
            out[etf] = analyze_holdings(etf, syms)
            print(
                f"[{i}/{len(etfs)}] {etf} holdings={len(syms)} leaders={len(out[etf])}",
                flush=True,
            )
        browser.close()
    return out


# ---------------------------------------------------------------- main
def parse_cols(text: str) -> list[str]:
    cols = [c.strip().upper() for c in (text or "").replace("，", ",").split(",") if c.strip()]
    bad = [c for c in cols if c not in ALLOWED_COLS]
    if bad:
        raise ValueError(f"columns {bad} not allowed; only {ALLOWED_COLS}")
    return cols


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(description="QQscan C/D/E/F (TD S9 / TD B1 / zhuli daily)")
    p.add_argument("--sheet-id", default=os.environ.get("EXTRASCAN_SHEET_ID", DEFAULT_SHEET_ID))
    p.add_argument("--columns", default="C,D,E,F", help="Subset of C,D,E,F")
    p.add_argument("--etf-list", default="etf_list.txt")
    p.add_argument("--asof", default="", help="YYYY-MM-DD: truncate bars to this session (dry-run/backtest)")
    p.add_argument("--dry-run", action="store_true", help="Compute only; never write Sheet")
    p.add_argument("--skip-holdings", action="store_true", help="ETF columns = ETF tickers only")
    p.add_argument("--symbols", default="", help="Smoke list replacing the E universe")
    p.add_argument("--out-json", default="", help="Write full result JSON here")
    args = p.parse_args(argv)

    cols = parse_cols(args.columns)
    asof = date.fromisoformat(args.asof) if args.asof else None
    if asof is not None and not args.dry_run:
        print("ERROR: --asof is for dry-run only", file=sys.stderr)
        return 2
    expected = asof or last_complete_us_session_date()
    sa = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()
    if not args.dry_run and not sa:
        print("ERROR: set GOOGLE_SERVICE_ACCOUNT_JSON to write", file=sys.stderr)
        return 2
    print(f"QQscan CDEF cols={cols} expected_session={expected} dry_run={args.dry_run}", flush=True)

    result: dict = {"expected": expected.isoformat(), "columns": {}, "groups": {}}
    values: dict[str, list[str]] = {}
    blocked: list[str] = []

    # --- ETF group (C/D/F) ---
    etf_cols = [c for c in cols if c in ETF_COLS]
    if etf_cols:
        etf_path = Path(args.etf_list)
        etfs = load_etf_list(etf_path)
        frames, _failed = download(etfs)
        checks = {}
        if "C" in etf_cols:
            checks["C"] = lambda d: td_sell9(d["Close"])
        if "D" in etf_cols:
            checks["D"] = lambda d: td_buy1(d["Close"])
        if "F" in etf_cols:
            checks["F"] = zhuli_daily_cross_up
        ev = evaluate(frames, etfs, expected, checks, asof)
        result["groups"]["ETF"] = {k: v for k, v in ev.items() if k != "hits"}
        print(
            f"[ETF] universe={ev['universe']} fresh={ev['fresh']} frac={ev['frac']} "
            f"ok={ev['fresh_ok']} bars={ev['last_bar_hist']}",
            flush=True,
        )
        if not ev["fresh_ok"]:
            blocked += etf_cols
        else:
            signal_etfs = sorted({e for c in etf_cols for e in ev["hits"][c]})
            leaders: dict[str, list[str]] = {}
            if signal_etfs and not args.skip_holdings:
                leaders = scrape_leaders(signal_etfs)
            for c in etf_cols:
                values[c] = column_with_leaders(ev["hits"][c], leaders)
                result["columns"][c] = {
                    "signal_etfs": ev["hits"][c],
                    "holdings_leaders": sorted(set(values[c]) - set(ev["hits"][c])),
                    "n": len(values[c]),
                }

    # --- US stocks group (E) ---
    if "E" in cols:
        uni = parse_symbols(args.symbols) if args.symbols.strip() else universe_us_ex_etf_fund()
        frames, _failed = download(uni)
        ev = evaluate(frames, uni, expected, {"E": zhuli_daily_cross_up}, asof)
        result["groups"]["US"] = {k: v for k, v in ev.items() if k not in ("hits", "stale", "missing")}
        result["groups"]["US"]["n_stale"] = len(ev["stale"])
        result["groups"]["US"]["n_missing"] = len(ev["missing"])
        print(
            f"[US] universe={ev['universe']} fresh={ev['fresh']} frac={ev['frac']} "
            f"ok={ev['fresh_ok']} bars={ev['last_bar_hist']}",
            flush=True,
        )
        if not ev["fresh_ok"]:
            blocked.append("E")
        else:
            kept, fstats = apply_e_filters(frames, ev["hits"]["E"], asof)
            print(f"[US] E funnel {fstats['dropped_counts']} cross={fstats['cross_hits']} kept={fstats['kept']}", flush=True)
            values["E"] = kept
            result["columns"]["E"] = {
                "n": len(kept),
                "symbols": kept,
                "cross_hits_unfiltered": ev["hits"]["E"],
                "filters": fstats,
            }

    for c in sorted(values):
        print(f"{c} n={len(values[c])} sample={values[c][:20]}", flush=True)
    result["blocked"] = blocked
    if args.out_json:
        Path(args.out_json).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

    if not args.dry_run:
        from sheets_writer import write_column

        for c in sorted(values):
            assert c in ALLOWED_COLS  # never touch other QQscan columns
            n = write_column(sa, args.sheet_id, TAB_QQ, c, values[c], header=QQSCAN_CDEF_HEADERS[c])
            print(f"QQscan {c} written n={n}", flush=True)
    else:
        print("dry-run OK (no Sheet write)", flush=True)

    if blocked:
        print(f"BLOCKED (freshness < {MIN_FRESH_FRAC}): {blocked} — not written", file=sys.stderr, flush=True)
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
