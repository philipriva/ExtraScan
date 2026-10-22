#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Fetch latest Strong_Stocks_V3_*.txt + Maggiestocks_*.txt and write QQscan A / J."""
from __future__ import annotations

import argparse
import os
import re
import sys
import time
from datetime import date, datetime, timedelta, timezone
from typing import Optional

import requests

from sheets_writer import write_column

DEFAULT_SHEET_ID = "1FSOmey7ALHFuYXxZSgjZnjWA5zpmbiv48PWAJD_yYRk"
TAB = "QQscan"

STRONG_REPO = "philipriva/my-stock-scanner"
STRONG_PREFIX = "Strong_Stocks_V3_"
MAGGIE_REPO = "philipriva/Q-scanner"
MAGGIE_PREFIX = "Maggiestocks_"

DATE_RE = re.compile(r"_(\d{8})\.txt$")


def _headers() -> dict[str, str]:
    h = {"Accept": "application/vnd.github+json", "User-Agent": "ExtraScan-QQscan"}
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if token:
        h["Authorization"] = f"Bearer {token}"
    return h


def _latest_via_api(owner_repo: str, prefix: str) -> tuple[str, str]:
    url = f"https://api.github.com/repos/{owner_repo}/contents/"
    r = requests.get(url, headers=_headers(), timeout=60)
    r.raise_for_status()
    items = r.json()
    if not isinstance(items, list):
        raise RuntimeError(f"Unexpected contents response for {owner_repo}")
    candidates: list[tuple[str, str, str]] = []
    for it in items:
        name = it.get("name") or ""
        if not name.startswith(prefix) or not name.endswith(".txt"):
            continue
        m = DATE_RE.search(name)
        if not m:
            continue
        download = it.get("download_url") or (
            f"https://raw.githubusercontent.com/{owner_repo}/main/{name}"
        )
        candidates.append((m.group(1), name, download))
    if not candidates:
        raise FileNotFoundError(f"No {prefix}YYYYMMDD.txt in {owner_repo}")
    candidates.sort(key=lambda t: t[0], reverse=True)
    _d, name, download = candidates[0]
    return name, download


def _latest_via_raw_walk(owner_repo: str, prefix: str, days: int = 14) -> tuple[str, str]:
    """When API is rate-limited, probe raw URLs for today..today-days."""
    sess = requests.Session()
    sess.headers.update({"User-Agent": "ExtraScan-QQscan"})
    today = date.today()
    for i in range(days):
        d = today - timedelta(days=i)
        name = f"{prefix}{d.strftime('%Y%m%d')}.txt"
        url = f"https://raw.githubusercontent.com/{owner_repo}/main/{name}"
        r = sess.head(url, timeout=30, allow_redirects=True)
        if r.status_code == 200:
            return name, url
        # some CDNs dislike HEAD — try GET range
        if r.status_code in (403, 405):
            g = sess.get(url, timeout=30)
            if g.status_code == 200 and g.text.strip():
                return name, url
    raise FileNotFoundError(
        f"No {prefix}YYYYMMDD.txt found via raw walk ({days}d) in {owner_repo}"
    )


def _latest_txt(owner_repo: str, prefix: str) -> tuple[str, str]:
    try:
        return _latest_via_api(owner_repo, prefix)
    except Exception as exc:
        print(f"API list failed ({exc}); raw date walk…", flush=True)
        return _latest_via_raw_walk(owner_repo, prefix)


def _file_date(name: str) -> date:
    m = DATE_RE.search(name)
    if not m:
        raise ValueError(f"No YYYYMMDD in filename: {name}")
    s = m.group(1)
    return date(int(s[:4]), int(s[4:6]), int(s[6:8]))


def _expected_min_date(now_utc: Optional[datetime] = None) -> date:
    """Strong/Maggie dated files typically land ~01:40–01:52 UTC on calendar day D.

    After 02:00 UTC we require file date >= today (UTC). Before that, yesterday is OK
    (still inside the overnight publish window).
    """
    now = now_utc or datetime.now(timezone.utc)
    d = now.date()
    if now.hour < 2:
        return d - timedelta(days=1)
    return d


def _assert_fresh(name: str, kind: str, *, allow_stale: bool) -> None:
    got = _file_date(name)
    need = _expected_min_date()
    if got >= need:
        return
    msg = (
        f"STALE {kind}: selected {name} (date {got.isoformat()}), "
        f"need >={need.isoformat()} UTC. Upstream not published yet, or this run "
        f"is too early / a missed prior schedule left Sheet on an old file."
    )
    if allow_stale:
        print(f"WARNING: {msg}", flush=True)
        return
    raise RuntimeError(msg)


def _latest_fresh(
    owner_repo: str,
    prefix: str,
    kind: str,
    *,
    allow_stale: bool,
    wait_secs: int,
) -> tuple[str, str]:
    """Pick newest dated txt; optionally wait for today's file before failing."""
    deadline = time.time() + max(0, wait_secs)
    last_err: Optional[BaseException] = None
    while True:
        name, url = _latest_txt(owner_repo, prefix)
        try:
            _assert_fresh(name, kind, allow_stale=allow_stale)
            return name, url
        except RuntimeError as exc:
            last_err = exc
            remaining = deadline - time.time()
            if allow_stale or remaining <= 0:
                raise
            sleep_for = min(60.0, max(5.0, remaining))
            print(
                f"{exc} — retry in {sleep_for:.0f}s (wait budget {remaining:.0f}s left)…",
                flush=True,
            )
            time.sleep(sleep_for)
    assert last_err is not None
    raise last_err


def _fetch_symbols(download_url: str) -> list[str]:
    r = requests.get(download_url, headers=_headers(), timeout=60)
    r.raise_for_status()
    return [ln.strip() for ln in r.text.splitlines() if ln.strip()]


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(description="Write QQscan columns A and J from scanner txts")
    p.add_argument("--sheet-id", default=os.environ.get("EXTRASCAN_SHEET_ID", DEFAULT_SHEET_ID))
    p.add_argument("--dry-run", action="store_true", help="Fetch and print counts only")
    p.add_argument(
        "--allow-stale",
        action="store_true",
        help="Do not fail when newest file date is older than expected UTC day",
    )
    p.add_argument(
        "--wait-secs",
        type=int,
        default=int(os.environ.get("QQSCAN_WAIT_SECS", "0")),
        help="Seconds to poll for today's dated file before failing (env QQSCAN_WAIT_SECS)",
    )
    args = p.parse_args(argv)

    sa = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()
    if not args.dry_run and not sa:
        print("ERROR: set GOOGLE_SERVICE_ACCOUNT_JSON", file=sys.stderr)
        return 2

    allow_stale = args.allow_stale or os.environ.get("QQSCAN_ALLOW_STALE", "").strip() in (
        "1",
        "true",
        "TRUE",
        "yes",
    )

    try:
        strong_name, strong_url = _latest_fresh(
            STRONG_REPO,
            STRONG_PREFIX,
            "Strong_Stocks",
            allow_stale=allow_stale,
            wait_secs=args.wait_secs,
        )
        maggie_name, maggie_url = _latest_fresh(
            MAGGIE_REPO,
            MAGGIE_PREFIX,
            "Maggiestocks",
            allow_stale=allow_stale,
            wait_secs=args.wait_secs,
        )
    except RuntimeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 3

    strong = _fetch_symbols(strong_url)
    maggie = _fetch_symbols(maggie_url)
    print(f"Source A: {STRONG_REPO}/{strong_name} n={len(strong)}", flush=True)
    print(f"Source J: {MAGGIE_REPO}/{maggie_name} n={len(maggie)}", flush=True)
    print(
        f"Freshness OK (UTC today={datetime.now(timezone.utc).date().isoformat()}, "
        f"min={_expected_min_date().isoformat()})",
        flush=True,
    )

    if args.dry_run:
        print("dry-run OK", flush=True)
        return 0

    n_a = write_column(sa, args.sheet_id, TAB, "A", strong)
    n_j = write_column(sa, args.sheet_id, TAB, "J", maggie)
    print(f"QQscan done A={n_a} J={n_j}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
