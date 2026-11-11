#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ETF Holdings: full Schwab holdings per ETF (no filtering) -> CSV/JSON artifact.

Same source and ticker rule as write_etf_leaders.scrape_etf_holdings (QQscan G):
schwab.wallst.com holdings page, 60 per page, follow Next; a row's first cell is
kept as a ticker when it is upper-case and 1-5 chars (so money-market tickers are
kept and '--' cash/commodity lines are dropped, exactly like G).

Additions (standalone, does not touch existing modules / Sheets):
- reads only the holdings table (#tthHoldingsTable) and checks completeness
  against Schwab's "Viewing a-b of N matches" footer;
- per-ETF status ok / empty / failed (+error); failed ETFs retried once.

Output dir (default out/): holdings.csv (row1 = ETF, rows below = tickers),
holdings.json, status.csv. No Sheet writes.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
import time
from datetime import datetime, timezone

URL = (
    "https://www.schwab.wallst.com/schwab/Prospect/research/etfs/schwabETF/"
    "index.asp?type=holdings&symbol={etf}"
)
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36"
)
VIEW_RE = re.compile(r"Viewing\s+([\d,]+)\s*-\s*([\d,]+)\s+of\s+([\d,]+)\s+matches", re.I)
MAX_PAGES = 200


def is_g_ticker(s: str) -> bool:
    """Identical rule to write_etf_leaders.scrape_etf_holdings."""
    return bool(s) and s.isupper() and 1 <= len(s) <= 5


def load_list(path: str) -> list[str]:
    out: list[str] = []
    for tok in open(path, encoding="utf-8").read().split():
        t = tok.strip().upper()
        if t and t not in out:
            out.append(t)
    return out


def _viewing(page):
    try:
        m = VIEW_RE.search(page.inner_text("body"))
    except Exception:
        return None
    if not m:
        return None
    return tuple(int(x.replace(",", "")) for x in m.groups())


def _first_cells(page) -> list[str]:
    return page.evaluate(
        """() => Array.from(document.querySelectorAll('#tthHoldingsTable tr'))
              .filter(tr => tr.querySelectorAll('td').length > 0)
              .map(tr => (tr.querySelector('td').innerText || '').trim())"""
    )


def scrape_one(page, etf: str) -> dict:
    rec = {"etf": etf, "status": "failed", "error": "", "tickers": [],
           "table_rows": 0, "non_ticker_rows": 0, "schwab_total": None,
           "pages": 0, "as_of": ""}
    resp = page.goto(URL.format(etf=etf), wait_until="domcontentloaded", timeout=45000)
    code = resp.status if resp else None
    if code is None or code >= 400:
        rec["error"] = f"http {code}"
        return rec
    # wait for the page body to render either the table or the no-data layout
    deadline = time.time() + 20
    while time.time() < deadline:
        if page.locator("#tthHoldingsTable").count() > 0:
            break
        body = page.inner_text("body") if page.locator("body").count() else ""
        if "Holdings are subject to change" in body or "not available" in body:
            break
        time.sleep(0.5)
    body = page.inner_text("body")
    m = re.search(r"All Holdings\s+as of\s+(\d{2}/\d{2}/\d{4})", body)
    rec["as_of"] = m.group(1) if m else ""
    if page.locator("#tthHoldingsTable").count() == 0:
        if "All Holdings" in body and ("Holdings are subject to change" in body or "not available" in body):
            rec["status"] = "empty"
            rec["error"] = "no holdings table on page"
            return rec
        rec["error"] = f"holdings page not recognised (title={page.title()!r})"
        return rec

    try:
        btn = page.locator("a:text-is('60')").first
        if btn.count() > 0 and btn.is_visible(timeout=3000):
            before = _viewing(page)
            btn.click()
            t0 = time.time()
            while time.time() - t0 < 15:
                v = _viewing(page)
                if v is None or v != before:
                    break
                time.sleep(0.3)
            time.sleep(0.5)
    except Exception:
        pass

    # Schwab sorts by % weight; tied weights reshuffle between page requests
    # (seen on IWM: ~470 duplicate / missing rows). Sort by Symbol instead so
    # pagination is deterministic.
    rec["sorted_by_symbol"] = False
    try:
        th = page.locator("#tthHoldingsTable th[sortname='Symbol']").first
        if th.count() > 0:
            for _ in range(2):
                before = _first_cells(page)
                th.click()
                t0 = time.time()
                while time.time() - t0 < 15:
                    cls = th.get_attribute("class") or ""
                    if "tsAscending" in cls and _first_cells(page) != before:
                        break
                    time.sleep(0.3)
                if "tsAscending" in (th.get_attribute("class") or ""):
                    rec["sorted_by_symbol"] = True
                    break
            time.sleep(0.5)
    except Exception:
        pass
    if not rec["sorted_by_symbol"] and (_viewing(page) or (0, 0, 0))[2] > 60:
        rec["error"] = "could not sort by Symbol for multi-page table"
        return rec

    seen: list[str] = []
    cells_all: list[str] = []
    last_view = None
    for pnum in range(1, MAX_PAGES + 1):
        view = _viewing(page)
        cells = _first_cells(page)
        rec["pages"] = pnum
        if view is not None:
            rec["schwab_total"] = view[2]
        cells_all.extend(cells)
        for c in cells:
            if is_g_ticker(c) and c not in seen:
                seen.append(c)
        if view is None or view[1] >= view[2]:
            break
        # Schwab pager shows page numbers in groups of 5 and "Next >" can jump a
        # whole group, so prefer the explicit next page number; use Next only
        # when that number is not rendered, then verify the row offset.
        want_page = pnum + 1
        want_start = view[1] + 1
        num = page.locator(f"li[pagenumber='{want_page}'] a").first
        if num.count() > 0 and num.is_visible():
            num.click()
        else:
            nxt = page.locator("a:has-text('Next')").filter(has_text="Next").first
            if nxt.count() == 0 or not nxt.is_visible():
                rec["error"] = f"no link to page {want_page} at {view}"
                break
            nxt.click()
        last_view = view
        t0 = time.time()
        changed = False
        while time.time() - t0 < 20:
            v = _viewing(page)
            if v is not None and v != last_view:
                changed = True
                break
            time.sleep(0.3)
        if changed and v[0] != want_start:
            # Next jumped past the wanted page: go back to it via its number.
            num = page.locator(f"li[pagenumber='{want_page}'] a").first
            if num.count() > 0 and num.is_visible():
                num.click()
                t0 = time.time()
                while time.time() - t0 < 20:
                    v = _viewing(page)
                    if v is not None and v[0] == want_start:
                        break
                    time.sleep(0.3)
            if v is None or v[0] != want_start:
                rec["error"] = f"pager skipped: wanted start {want_start}, got {v}"
                break
        if not changed:
            rec["error"] = f"page did not advance after {last_view}"
            break
        # wait until the table rows themselves are replaced (footer can update first)
        expect_rows = min(view[2], v[1]) - v[0] + 1
        t0 = time.time()
        while time.time() - t0 < 15:
            cur = _first_cells(page)
            if cur != cells and len(cur) == expect_rows:
                break
            time.sleep(0.3)
        time.sleep(0.3)
    rec["table_rows"] = len(cells_all)
    rec["non_ticker_rows"] = sum(1 for c in cells_all if not is_g_ticker(c))
    rec["non_ticker_cells"] = sorted({c for c in cells_all if not is_g_ticker(c)})[:30]
    rec["dup_ticker_rows"] = sum(1 for c in cells_all if is_g_ticker(c)) - len(seen)
    rec["tickers"] = seen
    total = rec["schwab_total"]
    if rec["error"]:
        rec["status"] = "failed"
    elif total is not None and len(cells_all) < total:
        rec["status"] = "failed"
        rec["error"] = f"incomplete: {len(cells_all)} rows < {total} matches"
    elif seen:
        rec["status"] = "ok"
    else:
        rec["status"] = "empty"
        rec["error"] = f"{len(cells_all)} rows, none with a ticker symbol"
    return rec


def run(etfs: list[str]) -> list[dict]:
    from playwright.sync_api import sync_playwright

    results: dict[str, dict] = {}
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        ctx = browser.new_context(user_agent=UA)
        page = ctx.new_page()

        def attempt(etf: str, n: int) -> dict:
            try:
                r = scrape_one(page, etf)
            except Exception as e:  # timeout / block / navigation error
                r = {"etf": etf, "status": "failed", "error": f"{type(e).__name__}: {str(e)[:200]}",
                     "tickers": [], "table_rows": 0, "non_ticker_rows": 0,
                     "schwab_total": None, "pages": 0, "as_of": ""}
            r["attempts"] = n
            print(f"[{etf}] try{n} {r['status']} tickers={len(r['tickers'])} rows={r['table_rows']} "
                  f"total={r['schwab_total']} pages={r['pages']} {r['error']}", flush=True)
            return r

        for i, etf in enumerate(etfs, 1):
            print(f"--- {i}/{len(etfs)} {etf}", flush=True)
            results[etf] = attempt(etf, 1)
        failed = [e for e in etfs if results[e]["status"] == "failed"]
        if failed:
            print(f"Retrying {len(failed)} failed: {failed}", flush=True)
            for etf in failed:
                time.sleep(2)
                results[etf] = attempt(etf, 2)
        browser.close()
    return [results[e] for e in etfs]


def write_outputs(recs: list[dict], out: str) -> None:
    os.makedirs(out, exist_ok=True)
    maxlen = max([len(r["tickers"]) for r in recs] + [0])
    with open(os.path.join(out, "holdings.csv"), "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow([r["etf"] for r in recs])
        for i in range(maxlen):
            w.writerow([r["tickers"][i] if i < len(r["tickers"]) else "" for r in recs])
    with open(os.path.join(out, "status.csv"), "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        cols = ["etf", "status", "count", "table_rows", "non_ticker_rows", "schwab_total",
                "pages", "as_of", "attempts", "dup_ticker_rows", "error"]
        w.writerow(cols)
        for r in recs:
            w.writerow([r["etf"], r["status"], len(r["tickers"]), r["table_rows"], r["non_ticker_rows"],
                        r["schwab_total"], r["pages"], r["as_of"], r.get("attempts"), r.get("dup_ticker_rows", 0), r["error"]])
    meta = {
        "generated_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "run_id": os.environ.get("GITHUB_RUN_ID"),
        "sha": os.environ.get("GITHUB_SHA"),
        "n_etfs": len(recs),
        "counts": {s: sum(1 for r in recs if r["status"] == s) for s in ("ok", "empty", "failed")},
        "etfs": recs,
    }
    with open(os.path.join(out, "holdings.json"), "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=1)
    print(json.dumps(meta["counts"]), flush=True)


def main(argv=None) -> int:
    here = os.path.dirname(os.path.abspath(__file__))
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--list", default=os.path.join(here, "etf_list.txt"))
    ap.add_argument("--only", default="", help="comma list override")
    ap.add_argument("--out", default="out")
    a = ap.parse_args(argv)
    etfs = [s.strip().upper() for s in a.only.split(",") if s.strip()] or load_list(a.list)
    print(f"ETFs: {len(etfs)}", flush=True)
    recs = run(etfs)
    write_outputs(recs, a.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
