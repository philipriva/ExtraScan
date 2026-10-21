#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""GitHub Actions: ExtraScan — Fund Flow 日線 黃燭 / 白圓 → Google Sheet 四個 tab

Tabs:
  yellowALL     — 全美股（剔 ETF/fund）黃燭
  whiteballALL  — 全美股（剔 ETF/fund）白圓
  yellowETF     — Nasdaq Trader ETF/fund 宇宙 黃燭（非固定名單）
  whiteETF      — Nasdaq Trader ETF/fund 宇宙 白圓

Formula = us-fundflow-tips-screener / banker_entry fund_flow_hit (yellow / white_circle).
TF = daily; drop forming bar before 16:15 ET.
"""
from __future__ import annotations

import argparse
import io
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import requests
import yfinance as yf

NASDAQ_LISTED = "https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt"
OTHER_LISTED = "https://www.nasdaqtrader.com/dynamic/SymDir/otherlisted.txt"

BATCH = 100
PERIOD = "2y"
MIN_BARS = 120
DOWNLOAD_RETRIES = 3
DOWNLOAD_BACKOFF_SEC = 2.0
SYMBOL_RETRY_RETRIES = 3

RSV_LEN, XSA1_LEN, XSA2_LEN, TYP_RANGE, BULL_EMA = 27, 5, 3, 34, 13
BANKER_BB_MAX, MOMENTUM_DROP_RATIO = 25.0, 0.95
RED, BLUE, GREEN, WHITE = 0, 1, 2, 3

_FUND_NAME_RE = re.compile(
    r"\b(ETF|ETN|ETP|FUND|TRUST|INCOME FUND|BOND FUND)\b",
    re.IGNORECASE,
)

TAB_ORDER = ("yellowALL", "whiteballALL", "yellowETF", "whiteETF")
def ema(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(span=n, adjust=False, min_periods=n).mean()


def xsa(series: pd.Series, length: int, wei: float) -> pd.Series:
    """Same recursive SMA as banker_entry_screener_app.pyw."""
    src = series.astype(float)
    ma = src.rolling(length, min_periods=length).mean()
    out = pd.Series(index=src.index, dtype=float)
    prev = float("nan")
    for i, (val, sma) in enumerate(zip(src, ma)):
        if pd.isna(val):
            out.iloc[i] = float("nan")
            continue
        if pd.isna(prev):
            if pd.isna(sma):
                out.iloc[i] = float("nan")
            else:
                prev = float(sma)
                out.iloc[i] = prev
        else:
            prev = (float(val) * wei + prev * (length - wei)) / length
            out.iloc[i] = prev
    return out


def safe_ratio(numer: pd.Series, a: pd.Series, b: pd.Series) -> pd.Series:
    rng = b - a
    return ((numer - a) / rng * 100.0).where(rng != 0.0)


def fund_flow_flags(df: pd.DataFrame) -> tuple[bool, bool]:
    """Return (yellow, white_circle) on latest completed bar.

    Matches banker_entry_screener_app.fund_flow_hit modes yellow / white_circle:
      yellow       = is_banker (fundtrend cross above bull & bull < 25)
      white_circle = prev status BLUE & today GREEN & not banker
    """
    if df is None or len(df) < MIN_BARS:
        return False, False
    o = df["Open"].astype(float)
    h = df["High"].astype(float)
    l = df["Low"].astype(float)
    c = df["Close"].astype(float)

    low27 = l.rolling(RSV_LEN, min_periods=RSV_LEN).min()
    high27 = h.rolling(RSV_LEN, min_periods=RSV_LEN).max()
    rsv = safe_ratio(c, low27, high27)
    xsa1 = xsa(rsv, XSA1_LEN, 1.0)
    xsa2 = xsa(xsa1, XSA2_LEN, 1.0)
    fundtrend = (3.0 * xsa1 - 2.0 * xsa2 - 50.0) * 1.032 + 50.0

    typ = (2.0 * c + h + l + o) / 5.0
    low34 = l.rolling(TYP_RANGE, min_periods=TYP_RANGE).min()
    high34 = h.rolling(TYP_RANGE, min_periods=TYP_RANGE).max()
    bull = ema(safe_ratio(typ, low34, high34), BULL_EMA)

    is_over = fundtrend > bull
    is_under = fundtrend < bull
    momentum_drop = fundtrend < (fundtrend.shift(1).fillna(0.0) * MOMENTUM_DROP_RATIO)
    crossover = (fundtrend > bull) & (fundtrend.shift(1) <= bull.shift(1))
    is_banker = (crossover & (bull < BANKER_BB_MAX)) == True
    is_rebound = is_under & (~momentum_drop)

    status = pd.Series(RED, index=df.index, dtype=int)
    status = status.mask(is_over & momentum_drop, WHITE)
    status = status.mask(is_over & ~momentum_drop, GREEN)
    status = status.mask(~is_over & is_rebound, BLUE)
    status = status.mask(~is_over & ~is_rebound, RED)

    white = ((status == BLUE).shift(1) == True) & (status == GREEN) & (~is_banker)

    yellow = bool(is_banker.iloc[-1]) if len(is_banker) else False
    white_circle = bool(white.iloc[-1]) if len(white) >= 2 else False
    return yellow, white_circle


def _dir(url: str) -> pd.DataFrame:
    t = requests.get(url, timeout=60).text
    t = "\n".join(l for l in t.splitlines() if l and not l.startswith("File Creation"))
    return pd.read_csv(io.StringIO(t), sep="|", dtype=str)


def _norm_sym(s: str) -> str:
    return str(s).strip().upper().replace(".", "-")


def _is_etf_or_fund_row(row) -> bool:
    etf = str(row.get("ETF") or "").strip().upper()
    if etf == "Y":
        return True
    name = str(row.get("Security Name") or "")
    return bool(name and _FUND_NAME_RE.search(name))


def _nasdaq_trader_frames() -> pd.DataFrame:
    """Nasdaq listed + other listed, normalized symbol, no test/$/^."""
    a = _dir(NASDAQ_LISTED).rename(columns={"Symbol": "sym"})
    b = _dir(OTHER_LISTED).rename(columns={"ACT Symbol": "sym"})
    pieces = []
    for df in (a, b):
        use = [c for c in ("sym", "Security Name", "Test Issue", "ETF") if c in df.columns]
        pieces.append(df[use])
    df = pd.concat(pieces, ignore_index=True).dropna(subset=["sym"])
    if "Test Issue" in df.columns:
        df = df[df["Test Issue"].fillna("N") == "N"]
    df = df[~df["sym"].astype(str).str.contains(r"[\$\^]", regex=True)]
    df = df.copy()
    df["sym"] = df["sym"].map(_norm_sym)
    df = df[df["sym"].astype(bool)]
    return df


def universe_us_ex_etf_fund() -> list[str]:
    """Nasdaq Trader US equities; EXCLUDE ETF/fund (same as us-consider-screener)."""
    df = _nasdaq_trader_frames()
    keep: set[str] = set()
    for _, row in df.iterrows():
        if not _is_etf_or_fund_row(row):
            keep.add(row["sym"])
    return sorted(keep)


def universe_us_etf_fund() -> list[str]:
    """Nasdaq Trader symbols that ARE ETF/fund (ETF=Y or fund-like name).

    Complement of universe_us_ex_etf_fund. Used for yellowETF / whiteETF so
    ETFs like SLYG/SPSM/XTN are included (not only curated etf_list.txt).
    """
    df = _nasdaq_trader_frames()
    keep: set[str] = set()
    for _, row in df.iterrows():
        if _is_etf_or_fund_row(row):
            keep.add(row["sym"])
    return sorted(keep)


def load_etf_list(path: Path) -> list[str]:
    """Optional curated override when --fixed-etf-list is set."""
    out, seen = [], set()
    for line in path.read_text(encoding="utf-8").splitlines():
        s = _norm_sym(line.split("#", 1)[0])
        if s and s not in seen:
            seen.add(s)
            out.append(s)
    return out


def parse_symbols(text: str) -> list[str]:
    out, seen = [], set()
    for p in text.replace(",", " ").split():
        s = _norm_sym(p)
        if s and s not in seen:
            seen.add(s)
            out.append(s)
    return out


def ny_now() -> datetime:
    try:
        return datetime.now(ZoneInfo("America/New_York")).replace(tzinfo=None)
    except Exception:
        return datetime.utcnow() - timedelta(hours=4)


def asof_et_str() -> str:
    try:
        return datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d %H:%M ET")
    except Exception:
        return datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")


def hkt_now_str() -> str:
    try:
        return datetime.now(ZoneInfo("Asia/Hong_Kong")).strftime("%Y-%m-%d %H:%M:%S Asia/Hong_Kong")
    except Exception:
        return datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC") + " (HKT unavailable)"


def drop_forming(d: pd.DataFrame) -> pd.DataFrame:
    """Drop today's bar until after 16:15 ET (same as tips/exclude)."""
    if d is None or d.empty:
        return d
    now = ny_now()
    last = pd.Timestamp(d.index[-1]).date()
    if last == now.date() and (now.hour, now.minute) < (16, 15):
        return d.iloc[:-1]
    return d


def normalize_ohlc(df: pd.DataFrame) -> pd.DataFrame | None:
    cols = {str(c).lower(): c for c in df.columns}
    if not all(k in cols for k in ("open", "high", "low", "close")):
        return None
    out = pd.DataFrame(
        {
            "Open": pd.to_numeric(df[cols["open"]], errors="coerce"),
            "High": pd.to_numeric(df[cols["high"]], errors="coerce"),
            "Low": pd.to_numeric(df[cols["low"]], errors="coerce"),
            "Close": pd.to_numeric(df[cols["close"]], errors="coerce"),
        }
    ).dropna()
    if not isinstance(out.index, pd.DatetimeIndex):
        out.index = pd.to_datetime(out.index)
    return out if len(out) >= MIN_BARS else None


def _extract_batch_frames(raw: pd.DataFrame, batch: list[str]) -> dict[str, pd.DataFrame]:
    got: dict[str, pd.DataFrame] = {}
    if raw is None or raw.empty:
        return got
    for s in batch:
        try:
            d = raw[s] if len(batch) > 1 else raw
            if isinstance(d.columns, pd.MultiIndex):
                d.columns = d.columns.get_level_values(-1)
            d = d.dropna(how="all")
            if not d.empty:
                got[s] = d
        except Exception:
            pass
    return got


def _yf_download_once(tickers: list[str]):
    return yf.download(
        tickers,
        period=PERIOD,
        interval="1d",
        group_by="ticker",
        auto_adjust=False,
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
            print(
                f"  {sym}: empty (symbol retry {attempt + 1}/{SYMBOL_RETRY_RETRIES})",
                flush=True,
            )
        except Exception as e:
            print(
                f"  {sym}: error {e} (symbol retry {attempt + 1}/{SYMBOL_RETRY_RETRIES})",
                flush=True,
            )
        time.sleep(DOWNLOAD_BACKOFF_SEC * (attempt + 1))
    return None


def download(syms: list[str]) -> tuple[dict[str, pd.DataFrame], list[str]]:
    out: dict[str, pd.DataFrame] = {}
    n = len(syms)
    for i in range(0, n, BATCH):
        bt = syms[i : i + BATCH]
        print(f"download {min(i + BATCH, n)}/{n}", flush=True)
        got = download_batch_with_retry(bt)
        out.update(got)
        missing = [s for s in bt if s not in out]
        if missing:
            print(f"  per-symbol retry for {len(missing)} missing", flush=True)
            for s in missing:
                frame = download_symbol_with_retry(s)
                if frame is not None:
                    out[s] = frame
                time.sleep(0.05)
        time.sleep(0.15)
    failed = [s for s in syms if s not in out]
    return out, failed


def screen_split(
    syms: list[str], label: str
) -> tuple[list[str], list[str], dict]:
    """Return (yellow_hits, white_hits, stats_dict). Separate — not union."""
    print(f"[{label}] universe={len(syms)}", flush=True)
    data, dl_failed = download(syms)
    yellow: list[str] = []
    white: list[str] = []
    norm_fail: list[str] = []
    screened = 0
    for sym in syms:
        raw = data.get(sym)
        if raw is None:
            continue
        ohlc = normalize_ohlc(raw)
        if ohlc is None:
            norm_fail.append(sym)
            continue
        ohlc = drop_forming(ohlc)
        if ohlc is None or len(ohlc) < MIN_BARS:
            norm_fail.append(sym)
            continue
        try:
            y, w = fund_flow_flags(ohlc)
        except Exception as exc:
            print(f"[skip] {sym}: {exc}", file=sys.stderr, flush=True)
            norm_fail.append(sym)
            continue
        screened += 1
        if y:
            yellow.append(sym)
        if w:
            white.append(sym)
    yellow = sorted(set(yellow))
    white = sorted(set(white))
    stats = {
        "label": label,
        "universe": len(syms),
        "download_ok": len(syms) - len(dl_failed),
        "download_failed": len(dl_failed),
        "normalize_failed": len(set(norm_fail)),
        "screened_ok": screened,
        "yellow": len(yellow),
        "white": len(white),
        "download_failed_symbols": sorted(dl_failed),
    }
    print(
        f"[{label}] Y={len(yellow)} W={len(white)} "
        f"dl_ok={stats['download_ok']} dl_fail={stats['download_failed']} "
        f"norm_fail={stats['normalize_failed']}",
        flush=True,
    )
    return yellow, white, stats


def rows_for(symbols: list[str], signal: str, asof: str) -> list[list[str]]:
    """Data rows only (no header) — A1 is first symbol."""
    return [[s, signal, asof] for s in symbols]


def write_local_csvs(outdir: Path, tab_map: dict[str, list[list[str]]]) -> None:
    outdir.mkdir(parents=True, exist_ok=True)
    for tab, rows in tab_map.items():
        path = outdir / f"{tab}.csv"
        path.write_text(
            "\n".join(",".join(r) for r in rows) + ("\n" if rows else ""),
            encoding="utf-8",
        )
        print(f"wrote {path} ({len(rows)})", flush=True)


def load_sheet_cfg(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def maybe_write_sheets(cfg: dict, tab_map: dict[str, list[list[str]]]) -> None:
    sa_json = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()
    if not sa_json:
        print("Sheets: skip (no GOOGLE_SERVICE_ACCOUNT_JSON)", flush=True)
        return
    sid = (cfg.get("spreadsheet") or {}).get("id")
    if not sid:
        print("Sheets: skip (no spreadsheet.id in sheet_ids.json)", flush=True)
        return
    from sheets_writer import write_tabs

    write_tabs(sa_json, sid, tab_map)


def main() -> int:
    ap = argparse.ArgumentParser(description="ExtraScan Fund Flow yellow/white → Sheets")
    ap.add_argument(
        "--symbols",
        default="",
        help="Smoke: comma/space symbols; fills ALL tabs from this list only",
    )
    ap.add_argument(
        "--etf-only",
        action="store_true",
        help="Skip full-US equities scan; only screen Nasdaq ETF/fund universe "
        "→ yellowETF/whiteETF (ALL tabs stay empty)",
    )
    ap.add_argument(
        "--skip-all-us",
        action="store_true",
        help="Alias of --etf-only for clarity",
    )
    ap.add_argument(
        "--etf-list",
        default="etf_list.txt",
        help="Curated ETF file used only with --fixed-etf-list",
    )
    ap.add_argument(
        "--fixed-etf-list",
        action="store_true",
        help="Use --etf-list file instead of full Nasdaq ETF/fund universe",
    )
    ap.add_argument(
        "--sheet-ids",
        default="sheet_ids.json",
        help="Path to sheet_ids.json",
    )
    ap.add_argument(
        "--out-dir",
        default="out",
        help="Local CSV output directory",
    )
    args = ap.parse_args()
    cwd = Path.cwd()
    asof = asof_et_str()
    etf_path = Path(args.etf_list)
    if not etf_path.is_absolute():
        etf_path = cwd / etf_path
    cfg_path = Path(args.sheet_ids)
    if not cfg_path.is_absolute():
        cfg_path = cwd / cfg_path
    cfg = load_sheet_cfg(cfg_path) if cfg_path.exists() else {}

    yellow_all: list[str] = []
    white_all: list[str] = []
    yellow_etf: list[str] = []
    white_etf: list[str] = []
    all_stats: list[dict] = []
    etf_only_mode = False

    if args.symbols.strip():
        syms = parse_symbols(args.symbols)
        y, w, st = screen_split(syms, "smoke")
        all_stats.append(st)
        # Smoke: put hits into ALL four tabs from same list (useful wiring test)
        yellow_all = yellow_etf = y
        white_all = white_etf = w
        for s in syms:
            tag = "—"
            if s in y and s in w:
                tag = "both"
            elif s in y:
                tag = "yellow"
            elif s in w:
                tag = "white_circle"
            print(f"  {s}: {tag}")
    else:
        etf_only = args.etf_only or args.skip_all_us
        etf_only_mode = bool(etf_only)
        if not etf_only:
            us = universe_us_ex_etf_fund()
            yellow_all, white_all, st_us = screen_split(us, "all-US-ex-ETF")
            all_stats.append(st_us)
        if args.fixed_etf_list:
            if not etf_path.exists():
                raise SystemExit(f"--fixed-etf-list set but missing file: {etf_path}")
            etf = load_etf_list(etf_path)
            etf_label = "fixed-ETF"
        else:
            etf = universe_us_etf_fund()
            etf_label = "all-ETF-fund"
        print(f"ETF universe source={etf_label} n={len(etf)}", flush=True)
        yellow_etf, white_etf, st_etf = screen_split(etf, etf_label)
        all_stats.append(st_etf)

    tab_map = {
        "yellowALL": rows_for(yellow_all, "yellow", asof),
        "whiteballALL": rows_for(white_all, "white_circle", asof),
        "yellowETF": rows_for(yellow_etf, "yellow", asof),
        "whiteETF": rows_for(white_etf, "white_circle", asof),
    }
    # Keep tab order stable
    tab_map = {k: tab_map[k] for k in TAB_ORDER}

    write_local_csvs(cwd / args.out_dir, tab_map)

    # --etf-only must not wipe ALL tabs on Sheets (local CSVs still have empties)
    sheets_map = (
        {k: tab_map[k] for k in ("yellowETF", "whiteETF")}
        if etf_only_mode
        else tab_map
    )

    stats_path = cwd / args.out_dir / "download_stats.txt"
    lines = [f"run_timestamp={hkt_now_str()}", f"asof_et={asof}", ""]
    for st in all_stats:
        lines.append(f"label={st['label']}")
        for k in (
            "universe",
            "download_ok",
            "download_failed",
            "normalize_failed",
            "screened_ok",
            "yellow",
            "white",
        ):
            lines.append(f"{k}={st[k]}")
        lines.append("download_failed_symbols:")
        fails = st.get("download_failed_symbols") or []
        lines.extend(fails if fails else ["(none)"])
        lines.append("---")
    lines.append(
        f"tabs: yellowALL={len(yellow_all)} whiteballALL={len(white_all)} "
        f"yellowETF={len(yellow_etf)} whiteETF={len(white_etf)}"
    )
    stats_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"wrote {stats_path}", flush=True)

    maybe_write_sheets(cfg, sheets_map)
    print(
        f"done | Y_ALL={len(yellow_all)} W_ALL={len(white_all)} "
        f"Y_ETF={len(yellow_etf)} W_ETF={len(white_etf)}",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())