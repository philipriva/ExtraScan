#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Fetch Strong_Stocks_V3_*.txt + Maggiestocks_*.txt (+ sidecars) → QQscan A / J.

Verification order (fail-closed before any Sheet mutation):
  1) Resolve both list files for expected publish date (target_session + 1)
  2) Fetch both bodies + optional *.txt.meta.json sidecars
  3) Sidecar session / payload hash / count / producer status / selected bars
  4) Only then write A and J (both validated first)

Status:
  VERIFIED — both sidecars pass (Sheet write allowed as OK)
  UNKNOWN — one/both missing sidecar (filename-only; do not elevate to verified;
            Sheet write skipped by default unless --allow-filename-only)
  UPSTREAM_PENDING — required dated files not published yet
  REJECT — sidecar present but mismatch/stale (no write)
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Optional

import requests

from qqscan_upstream_meta import (
    meta_name_for_list_name,
    meta_url_for_list_url,
    verify_sidecar,
)
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
        if name.endswith(".meta.json"):
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


def _fetch_meta(list_name: str, list_url: str, owner_repo: str) -> dict[str, Any] | None:
    """Try raw sidecar next to list; return None if missing."""
    candidates = [
        meta_url_for_list_url(list_url),
        f"https://raw.githubusercontent.com/{owner_repo}/main/{meta_name_for_list_name(list_name)}",
    ]
    for url in candidates:
        try:
            r = requests.get(url, headers=_headers(), timeout=30)
            if r.status_code == 404:
                continue
            r.raise_for_status()
            return json.loads(r.text)
        except Exception as exc:
            print(f"sidecar fetch miss {url}: {exc}", flush=True)
    return None


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
        "--allow-filename-only",
        action="store_true",
        help="Permit Sheet write when sidecars missing (status stays UNKNOWN; not verified)",
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
    allow_filename_only = args.allow_filename_only or os.environ.get(
        "QQSCAN_ALLOW_FILENAME_ONLY", ""
    ).strip() in ("1", "true", "TRUE", "yes")

    target_session = last_complete_us_session_date()
    need_file = expected_upstream_file_date(target_session)
    print(
        f"target_session={target_session.isoformat()} "
        f"expected_upstream_file_date={need_file.isoformat()} "
        f"(sidecar required for VERIFIED; filename alone is UNKNOWN)",
        flush=True,
    )

    meta_path = Path("out/qqscan_verify.json")
    sheets_info: dict = {"attempted": False, "written": False, "skipped_reason": None}
    sources_meta: list[dict] = []

    # 1) Resolve BOTH list files before any Sheet write.
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
                "bar_proof": False,
                "sources": sources_meta,
                "sheets": {
                    "attempted": False,
                    "written": False,
                    "skipped_reason": "UPSTREAM_PENDING",
                },
                "notes": [str(exc)],
            },
        )
        print(f"wrote {meta_path} status=UPSTREAM_PENDING", flush=True)
        return 3

    # 2) Fetch bodies + sidecars (still no Sheet mutation).
    strong = _fetch_symbols(strong_url)
    maggie = _fetch_symbols(maggie_url)
    strong_meta = _fetch_meta(strong_name, strong_url, STRONG_REPO)
    maggie_meta = _fetch_meta(maggie_name, maggie_url, MAGGIE_REPO)
    print(f"Source A: {STRONG_REPO}/{strong_name} n={len(strong)} meta={bool(strong_meta)}", flush=True)
    print(f"Source J: {MAGGIE_REPO}/{maggie_name} n={len(maggie)} meta={bool(maggie_meta)}", flush=True)

    a_status, a_notes = verify_sidecar(
        meta=strong_meta or {},
        tickers=strong,
        target_session=target_session,
        source_kind="Strong_Stocks",
    )
    if strong_meta is None:
        a_status, a_notes = "UNKNOWN", ["Strong_Stocks: sidecar missing"]

    j_status, j_notes = verify_sidecar(
        meta=maggie_meta or {},
        tickers=maggie,
        target_session=target_session,
        source_kind="Maggiestocks",
    )
    if maggie_meta is None:
        j_status, j_notes = "UNKNOWN", ["Maggiestocks: sidecar missing"]

    sources_meta = [
        {
            "column": "A",
            "repo": STRONG_REPO,
            "file": strong_name,
            "file_date": _file_date(strong_name).isoformat(),
            "n": len(strong),
            "sidecar": bool(strong_meta),
            "verify_status": a_status,
            "producer_status": (strong_meta or {}).get("status"),
            "target_session": (strong_meta or {}).get("target_session"),
            "notes": a_notes,
        },
        {
            "column": "J",
            "repo": MAGGIE_REPO,
            "file": maggie_name,
            "file_date": _file_date(maggie_name).isoformat(),
            "n": len(maggie),
            "sidecar": bool(maggie_meta),
            "verify_status": j_status,
            "producer_status": (maggie_meta or {}).get("status"),
            "target_session": (maggie_meta or {}).get("target_session"),
            "notes": j_notes,
        },
    ]

    # 3) Decide gate — any REJECT blocks; UNKNOWN blocks unless allow_filename_only.
    notes_all = a_notes + j_notes
    if a_status == "REJECT" or j_status == "REJECT":
        status = "REJECT"
        sheets_info["skipped_reason"] = "REJECT_SIDECAR"
        exit_code = 5
        write_ok = False
    elif a_status == "VERIFIED" and j_status == "VERIFIED":
        status = "OK"  # both verified
        write_ok = True
        exit_code = 0
    elif a_status == "UNKNOWN" or j_status == "UNKNOWN":
        status = "UNKNOWN"
        write_ok = bool(allow_filename_only)
        sheets_info["skipped_reason"] = None if write_ok else "UNKNOWN_NO_SIDECAR"
        exit_code = 0 if write_ok else 6
    else:
        status = "UNKNOWN"
        write_ok = False
        sheets_info["skipped_reason"] = "UNKNOWN"
        exit_code = 6

    if args.dry_run:
        sheets_info["skipped_reason"] = "dry_run"
        status = "DRY_RUN" if status == "OK" else status
        write_ok = False
        exit_code = 0
        print(f"dry-run gate status={status} write_ok={write_ok}", flush=True)
    elif write_ok:
        sheets_info["attempted"] = True
        n_a, rb_a = write_column(
            sa, args.sheet_id, TAB, "A", strong, header=QQSCAN_HEADERS["A"], do_readback=True
        )
        n_j, rb_j = write_column(
            sa, args.sheet_id, TAB, "J", maggie, header=QQSCAN_HEADERS["J"], do_readback=True
        )
        sheets_info["written"] = True
        sheets_info["A"] = n_a
        sheets_info["J"] = n_j
        sheets_info["readback"] = {"A": rb_a, "J": rb_j}
        print(f"QQscan done A={n_a} J={n_j} status={status}", flush=True)
    else:
        print(
            f"NO SHEET WRITE — status={status} A={a_status} J={j_status}",
            flush=True,
        )

    write_verify_meta(
        meta_path,
        {
            "job": "qqscan_aj",
            "status": status,
            "target_session": target_session.isoformat(),
            "expected_upstream_file_date": need_file.isoformat(),
            "bar_proof": status == "OK",
            "sources": sources_meta,
            "sheets": sheets_info,
            "notes": notes_all
            + [
                "VERIFIED requires both sidecars: session match, payload hash/count, "
                "producer OK/OK_NO_NEW_SIGNAL, selected bars on target session.",
                "Filename alone never elevates to verified OK.",
            ],
        },
    )
    print(f"wrote {meta_path} status={status}", flush=True)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
