#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Sidecar scan metadata bound to ticker-list payloads.

Filename YYYYMMDD remains Asia publish day (= target_session + 1 calendar day)
for backward compatibility with ExtraScan filename mapping. The sidecar carries
the real XNYS target_session and per-ticker last bar dates from OHLC indexes.
"""
from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from us_session import DEFAULT_MIN_FRESH_FRAC, freshness_ok, last_complete_us_session_date

SCHEMA_VERSION = 1


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def publish_file_date(target_session: date) -> date:
    """Asia-morning publish date convention: session + 1 calendar day."""
    from datetime import timedelta

    return target_session + timedelta(days=1)


def payload_hash(tickers: Iterable[str]) -> str:
    """Stable hash over normalized ticker payload (order-independent)."""
    norm = sorted({str(t).strip().upper() for t in tickers if str(t).strip()})
    blob = "\n".join(norm).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def meta_path_for(list_path: Path | str) -> Path:
    p = Path(list_path)
    return p.with_suffix(p.suffix + ".meta.json") if p.suffix else Path(str(p) + ".meta.json")
    # Strong_Stocks_V3_YYYYMMDD.txt → Strong_Stocks_V3_YYYYMMDD.txt.meta.json


def build_scan_meta(
    *,
    source_id: str,
    list_filename: str,
    tickers: list[str],
    target_session: date,
    universe_n: int,
    bar_dates_all: list[date],
    missing_count: int,
    selected_bar_dates: dict[str, str],
    download_failed: int = 0,
    excluded_reasons: dict[str, int] | None = None,
    producer_version: str = "scan.py",
    min_frac: float = DEFAULT_MIN_FRESH_FRAC,
    notes: list[str] | None = None,
) -> dict[str, Any]:
    ok_fresh, fresh_info = freshness_ok(
        bar_dates_all, target_session, min_frac=min_frac, missing_count=missing_count
    )
    tickers_norm = [str(t).strip().upper() for t in tickers if str(t).strip()]
    # Selected bars must also cover target session (fail-closed if picks are stale).
    selected_dates = []
    for t in tickers_norm:
        raw = selected_bar_dates.get(t) or selected_bar_dates.get(t.lower())
        if raw:
            selected_dates.append(date.fromisoformat(raw))
    selected_fresh = (
        all(d == target_session for d in selected_dates) if selected_dates else True
    )
    if not ok_fresh:
        status = "FAIL_STALE"
    elif len(tickers_norm) == 0:
        status = "OK_NO_NEW_SIGNAL"
    elif not selected_fresh:
        status = "FAIL_STALE"
    else:
        status = "OK"

    pub = publish_file_date(target_session)
    return {
        "schema_version": SCHEMA_VERSION,
        "source_id": source_id,
        "status": status,
        "target_session": target_session.isoformat(),
        "publish_file_date": pub.isoformat(),
        "list_file": list_filename,
        "produced_at_utc": utc_now_iso(),
        "producer_version": producer_version,
        "payload": {
            "count": len(tickers_norm),
            "sha256": payload_hash(tickers_norm),
            "tickers_sample": tickers_norm[:20],
        },
        "universe": {
            "n": universe_n,
            "download_failed": download_failed,
            "missing_count": missing_count,
            "bars_screened": len(bar_dates_all),
            "excluded_reasons": excluded_reasons or {},
        },
        "input_bars": {
            "expected": target_session.isoformat(),
            "fresh": fresh_info.get("fresh"),
            "screened": fresh_info.get("screened"),
            "missing_count": fresh_info.get("missing_count"),
            "frac": fresh_info.get("frac"),
            "denom": fresh_info.get("denom"),
            "min": fresh_info.get("unique_last_bars", [None])[0]
            if fresh_info.get("unique_last_bars")
            else None,
            "max": fresh_info.get("unique_last_bars", [None])[-1]
            if fresh_info.get("unique_last_bars")
            else None,
            "unique": fresh_info.get("unique_last_bars"),
            "fresh_ok": ok_fresh,
            "min_frac": min_frac,
        },
        "selected_last_bars": {
            "n": len(selected_dates),
            "all_match_target": selected_fresh,
            "by_ticker": {k: selected_bar_dates[k] for k in list(selected_bar_dates)[:500]},
            "truncated": len(selected_bar_dates) > 500,
        },
        "notes": notes
        or [
            "target_session from XNYS last complete session; bar dates from OHLC index labels.",
            "list_file date = publish_file_date (session+1) for ExtraScan filename compat.",
            "FAIL_STALE / missing bars are fail-closed; OK_NO_NEW_SIGNAL = fresh universe, zero picks.",
        ],
    }


def write_list_and_meta(
    list_path: Path | str,
    tickers: list[str],
    meta: dict[str, Any],
) -> tuple[Path, Path]:
    lp = Path(list_path)
    lp.write_text(
        "".join(f"{t}\n" for t in tickers),
        encoding="utf-8",
    )
    mp = meta_path_for(lp)
    # Ensure meta payload matches what we wrote
    meta = dict(meta)
    meta["list_file"] = lp.name
    meta["payload"] = {
        "count": len(tickers),
        "sha256": payload_hash(tickers),
        "tickers_sample": [str(t).strip().upper() for t in tickers[:20]],
    }
    mp.write_text(json.dumps(meta, indent=2, sort_keys=False) + "\n", encoding="utf-8")
    return lp, mp


def load_meta(path: Path | str) -> dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def verify_meta_against_list(meta: dict[str, Any], tickers: list[str]) -> tuple[bool, str]:
    """Consumer-side binding check: hash + count must match list body."""
    norm = [str(t).strip().upper() for t in tickers if str(t).strip()]
    payload = meta.get("payload") or {}
    if int(payload.get("count", -1)) != len(norm):
        return False, f"count mismatch meta={payload.get('count')} list={len(norm)}"
    got = payload_hash(norm)
    expect = payload.get("sha256")
    if not expect or got != expect:
        return False, f"sha256 mismatch meta={expect} list={got}"
    return True, "ok"
