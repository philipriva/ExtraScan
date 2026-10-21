#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Read yellowETF + whiteETF from ExtraScan Sheet, scrape Schwab holdings,
filter leaders (price>EMA200 & EMA50>EMA200 & price>=0.90*ATH), write QQscan!F.

Financial conditions match Philip's local script exactly — do not invent logic.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import warnings
from typing import Optional

warnings.simplefilter(action="ignore", category=FutureWarning)

try:
    import yfinance as yf
    import pandas as pd
except ImportError:
    print("缺少套件: pip install yfinance pandas", file=sys.stderr)
    sys.exit(1)

from sheets_writer import write_column, _client

DEFAULT_SHEET_ID = "1FSOmey7ALHFuYXxZSgjZnjWA5zpmbiv48PWAJD_yYRk"
TAB_QQ = "QQscan"
ETF_TABS = ("yellowETF", "whiteETF")


def _read_col_a(sa_json: str, spreadsheet_id: str, tab: str) -> list[str]:
    gc = _client(sa_json)
    sh = gc.open_by_key(spreadsheet_id)
    try:
        ws = sh.worksheet(tab)
    except Exception:
        print(f"Tab missing: {tab}", flush=True)
        return []
    vals = ws.col_values(1)
    out: list[str] = []
    for v in vals:
        s = (v or "").strip().upper()
        if s:
            out.append(s)
    return out


def analyze_holdings(etf_symbol: str, symbols: list[str]) -> list[str]:
    if not symbols:
        return []

    cleaned_symbols = [s.replace("/", "-").replace(".", "-") for s in symbols]
    print(f"\n--- 正在分析 {etf_symbol} 的 {len(cleaned_symbols)} 隻成份股 ---", flush=True)

    results: list[str] = []
    batch_size = 15
    for i in range(0, len(cleaned_symbols), batch_size):
        batch = cleaned_symbols[i : i + batch_size]
        try:
            data = yf.download(
                batch,
                period="max",
                interval="1d",
                group_by="ticker",
                progress=False,
                threads=True,
            )
            if data is None or data.empty:
                continue

            for symbol in batch:
                try:
                    if len(batch) == 1:
                        df = data
                    else:
                        if symbol not in data.columns.levels[0]:
                            continue
                        df = data[symbol]

                    close_data = df["Close"].dropna()

                    # 必須有足夠歷史天數 (至少 200 天供 EMA200 計算)
                    if len(close_data) < 200:
                        continue

                    current_price = close_data.iloc[-1]
                    all_time_high = close_data.max()

                    ema50 = close_data.ewm(span=50, adjust=False).mean().iloc[-1]
                    ema200 = close_data.ewm(span=200, adjust=False).mean().iloc[-1]

                    # === 核心量化條件交集 (已移除 9EMA/21EMA 與 TD Setup 1) ===
                    cond_long = (current_price > ema200) and (ema50 > ema200)  # 長線多頭排列
                    cond_ath = current_price >= (all_time_high * 0.90)  # 距離 ATH < 10%

                    if cond_long and cond_ath:
                        results.append(symbol)
                except Exception:
                    continue
        except Exception:
            continue

    return sorted(list(set(results)))


def scrape_etf_holdings(page, etf_symbol: str) -> list[str]:
    url = (
        "https://www.schwab.wallst.com/schwab/Prospect/research/etfs/schwabETF/"
        f"index.asp?type=holdings&symbol={etf_symbol}"
    )
    print(f"\n🚀 正在啟動抓取: {etf_symbol}", flush=True)

    all_symbols: list[str] = []
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=30000)
        time.sleep(2)

        try:
            btn_60 = page.locator("a:text-is('60')").first
            if btn_60.is_visible(timeout=3000):
                btn_60.click()
                time.sleep(3)
        except Exception:
            pass

        page_num = 1
        while True:
            try:
                page.wait_for_selector("td", timeout=10000)
            except Exception:
                break

            rows = page.locator("tr").all()
            page_symbols: list[str] = []
            for row in rows:
                try:
                    cells = row.locator("td")
                    if cells.count() > 0:
                        symbol_text = cells.first.inner_text().strip()
                        if (
                            symbol_text
                            and symbol_text.isupper()
                            and 1 <= len(symbol_text) <= 5
                        ):
                            page_symbols.append(symbol_text)
                except Exception:
                    continue

            all_symbols.extend(page_symbols)
            print(f"  第 {page_num} 頁: 找到 {len(page_symbols)} 隻股票。", flush=True)

            next_clicked = False
            next_link = page.locator("a:has-text('Next')").filter(has_text="Next").first
            if next_link.count() > 0 and next_link.is_visible():
                is_disabled = next_link.evaluate(
                    "el => el.classList.contains('disabled') || el.hasAttribute('disabled')"
                )
                if not is_disabled:
                    next_link.click()
                    time.sleep(3)
                    next_clicked = True

            if not next_clicked:
                next_page_str = str(page_num + 1)
                num_link = page.locator(f"a:text-is('{next_page_str}')").first
                if num_link.count() > 0 and num_link.is_visible():
                    num_link.click()
                    time.sleep(3)
                    next_clicked = True

            if next_clicked:
                page_num += 1
            else:
                break

    except Exception as e:
        print(f"  ❌ 抓取 {etf_symbol} 失敗: {e}", flush=True)

    return list(set(all_symbols))


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(
        description="ETF holdings strength → QQscan column F"
    )
    p.add_argument(
        "--sheet-id",
        default=os.environ.get("EXTRASCAN_SHEET_ID", DEFAULT_SHEET_ID),
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Read ETFs + scan; print leaders; do not write Sheet",
    )
    p.add_argument(
        "--etfs",
        default="",
        help="Comma-separated ETF override (skip Sheet read)",
    )
    args = p.parse_args(argv)

    sa = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()
    if not args.etfs and not sa:
        print("ERROR: set GOOGLE_SERVICE_ACCOUNT_JSON", file=sys.stderr)
        return 2
    if not args.dry_run and not sa:
        print("ERROR: set GOOGLE_SERVICE_ACCOUNT_JSON to write", file=sys.stderr)
        return 2

    if args.etfs.strip():
        etf_list = [
            s.strip().upper()
            for s in args.etfs.replace("，", ",").split(",")
            if s.strip()
        ]
        print(f"ETF override: {etf_list}", flush=True)
    else:
        yellow = _read_col_a(sa, args.sheet_id, "yellowETF")
        white = _read_col_a(sa, args.sheet_id, "whiteETF")
        etf_list = sorted(set(yellow) | set(white))
        print(
            f"From Sheet: yellowETF={len(yellow)} whiteETF={len(white)} "
            f"union={len(etf_list)} → {', '.join(etf_list) or '(none)'}",
            flush=True,
        )

    if not etf_list:
        print("No ETF symbols; clearing QQscan!F", flush=True)
        if not args.dry_run:
            n = write_column(sa, args.sheet_id, TAB_QQ, "F", [])
            print(f"QQscan F cleared n={n}", flush=True)
        return 0

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print(
            "缺少 playwright: pip install playwright && playwright install chromium",
            file=sys.stderr,
        )
        return 1

    all_leaders: set[str] = set()
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36"
            )
        )
        page = context.new_page()

        for etf in etf_list:
            symbols = scrape_etf_holdings(page, etf)
            leaders = analyze_holdings(etf, symbols)
            all_leaders.update(leaders)
            print(f"✅ {etf} 掃描完成 leaders={len(leaders)}", flush=True)
            print("-" * 30, flush=True)

        browser.close()

    leaders_sorted = sorted(all_leaders)
    print(f"\n📊 Unique leaders across ETFs: {len(leaders_sorted)}", flush=True)
    for s in leaders_sorted[:50]:
        print(s, flush=True)
    if len(leaders_sorted) > 50:
        print(f"... +{len(leaders_sorted) - 50} more", flush=True)

    if args.dry_run:
        print("dry-run OK (no Sheet write)", flush=True)
        return 0

    n_f = write_column(sa, args.sheet_id, TAB_QQ, "F", leaders_sorted)
    print(f"QQscan done F={n_f}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
