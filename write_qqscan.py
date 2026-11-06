#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Fetch Strong_Stocks_V3_*.txt + Maggiestocks_*.txt and write QQscan A / J.

Upstream filename convention (observed):
  Asia-morning publish day D (UTC) carries YYYYMMDD=D and covers the prior
  complete US cash session (D-1 calendar day). Example: US session 2026-11-05
  → Strong_Stocks_V3_20261106.txt committed ~2026-11-06 03:32 UTC.

File date is metadata only — txt bodies are symbol lists with no bar dates.
"""
from __future__ import annotations

import argparse
import os
import re
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import requests

from run_extrascan import last_complete_us_session_date
from sheets_writer import QQSCAN_HEADERS, write_column
from verify_meta import write_verify_meta

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


def expected_upstream_file_date(target_session: Optional[date] = None) -> date:
    """Filename YYYYMMDD required for a complete US session.

    Observed rule: file date = target_session + 1 calendar day
    (Asia-morning publish after that US close). No UTC-hour exception —
    accepting session+0 before 02:00 UTC previously wrote the prior batch.
    """
    session = target_session or last_complete_us_session_date()
    return session + timedelta(days=1)


def _assert_fresh(
    name: str,
    kind: str,
    *,
    allow_stale: bool,
    min_date: date,
) -> None:
    got = _file_date(name)
    if got >= min_date:
        return
    msg = (
        f"UPSTREAM_PENDING {kind}: selected {name} (file_date {got.isoformat()}), "
        f"need >={min_date.isoformat()} for target US session "
        f"{(min_date - timedelta(days=1)).isoformat()}. "
        f"Upstream not published yet — refuse prior batch (do not write Sheet)."
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
    min_date: date,
) -> tuple[str, str]:
    """Pick newest dated txt; optionally wait until file date >= min_date.

    wait_secs retained for backward compatibility; prefer wait_secs=0 and
    repository_dispatch / backup cron rather than long polling.
    """
    deadline = time.time() + max(0, wait_secs)
    last_err: Optional[BaseException] = None
    while True:
        name, url = _latest_txt(owner_repo, prefix)
        try:
            _assert_fresh(name, kind, allow_stale=allow_stale, min_date=min_date)
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
        help="Do not fail when newest file date is older than expected",
    )
    p.add_argument(
        "--wait-secs",
        type=int,
        default=int(os.environ.get("QQSCAN_WAIT_SECS", "0")),
        help="Seconds to poll for required dated file before failing (env QQSCAN_WAIT_SECS)",
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

    target_session = last_complete_us_session_date()
    need_file = expected_upstream_file_date(target_session)
    print(
        f"target_session={target_session.isoformat()} "
        f"expected_upstream_file_date={need_file.isoformat()} "
        f"(file_date = session+1; filename is metadata, not proof of bar OHLC)",
        flush=True,
    )

    meta_path = Path("out/qqscan_verify.json")
    sheets_info: dict = {"attempted": False, "written": False, "skipped_reason": None}
    sources_meta: list[dict] = []

    # Resolve BOTH sources before any Sheet write (atomic dependency check).
    try:
        strong_name, strong_url = _latest_fresh(
            STRONG_REPO,
            STRONG_PREFIX,
            "Strong_Stocks",
            allow_stale=allow_stale,
            wait_secs=args.wait_secs,
            min_date=need_file,
        )
        maggie_name, maggie_url = _latest_fresh(
            MAGGIE_REPO,
            MAGGIE_PREFIX,
            "Maggiestocks",
            allow_stale=allow_stale,
            wait_secs=args.wait_secs,
            min_date=max(need_file, _file_date(strong_name)),
        )
    except RuntimeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        write_verify_meta(
            meta_path,
            {
                "job": "qqscan_aj",
                "status": "UPSTREAM_PENDING",
                "target_session": target_session.isoformat(),
                "expected_upstream_file_date": need_file.isoformat(),
                "sources": sources_meta,
                "sheets": {
                    "attempted": False,
                    "written": False,
                    "skipped_reason": "UPSTREAM_PENDING",
                },
                "notes": [
                    "No Sheet write — refused prior-batch filenames.",
                    "Filename date is publish metadata; txt has no per-symbol bar dates.",
                    str(exc),
                ],
            },
        )
        print(f"wrote {meta_path} status=UPSTREAM_PENDING", flush=True)
        return 3

    strong = _fetch_symbols(strong_url)
    maggie = _fetch_symbols(maggie_url)
    print(f"Source A: {STRONG_REPO}/{strong_name} n={len(strong)}", flush=True)
    print(f"Source J: {MAGGIE_REPO}/{maggie_name} n={len(maggie)}", flush=True)
    sources_meta = [
        {
            "column": "A",
            "repo": STRONG_REPO,
            "file": strong_name,
            "file_date": _file_date(strong_name).isoformat(),
            "n": len(strong),
            "maps_to_session_estimate": (
                _file_date(strong_name) - timedelta(days=1)
            ).isoformat(),
            "bar_dates_in_file": False,
        },
        {
            "column": "J",
            "repo": MAGGIE_REPO,
            "file": maggie_name,
            "file_date": _file_date(maggie_name).isoformat(),
            "n": len(maggie),
            "maps_to_session_estimate": (
                _file_date(maggie_name) - timedelta(days=1)
            ).isoformat(),
            "bar_dates_in_file": False,
        },
    ]

    status = "OK"
    exit_code = 0
    if args.dry_run:
        sheets_info["skipped_reason"] = "dry_run"
        status = "DRY_RUN"
        print("dry-run OK (both sources resolved; no Sheet write)", flush=True)
    else:
        sheets_info["attempted"] = True
        n_a = write_column(sa, args.sheet_id, TAB, "A", strong, header=QQSCAN_HEADERS["A"])
        n_j = write_column(sa, args.sheet_id, TAB, "J", maggie, header=QQSCAN_HEADERS["J"])
        sheets_info["written"] = True
        sheets_info["A"] = n_a
        sheets_info["J"] = n_j
        print(f"QQscan done A={n_a} J={n_j}", flush=True)

    write_verify_meta(
        meta_path,
        {
            "job": "qqscan_aj",
            "status": status,
            "target_session": target_session.isoformat(),
            "expected_upstream_file_date": need_file.isoformat(),
            "sources": sources_meta,
            "sheets": sheets_info,
            "notes": [
                "Filename→session mapping is estimated (file_date - 1 day); "
                "txt bodies have no OHLC bar dates (metadata limitation).",
                "Both A and J validated before any Sheet write.",
            ],
        },
    )
    print(f"wrote {meta_path} status={status}", flush=True)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
