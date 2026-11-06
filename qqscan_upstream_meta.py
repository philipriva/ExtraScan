#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Validate Strong/Maggie list + sidecar metadata for QQscan A/J."""
from __future__ import annotations

import hashlib
import json
from datetime import date
from typing import Any


def payload_hash(tickers: list[str]) -> str:
    norm = sorted({str(t).strip().upper() for t in tickers if str(t).strip()})
    return hashlib.sha256("\n".join(norm).encode("utf-8")).hexdigest()


def meta_url_for_list_url(download_url: str) -> str:
    # raw.githubusercontent.com/.../file.txt → file.txt.meta.json
    if download_url.endswith(".txt"):
        return download_url + ".meta.json"
    return download_url + ".meta.json"


def meta_name_for_list_name(name: str) -> str:
    return name + ".meta.json" if name.endswith(".txt") else name + ".meta.json"


def verify_sidecar(
    *,
    meta: dict[str, Any],
    tickers: list[str],
    target_session: date,
    source_kind: str,
) -> tuple[str, list[str]]:
    """Return (status, notes). status in VERIFIED|UNKNOWN|REJECT|UPSTREAM_PENDING.

    VERIFIED = sidecar present, session match, hash/count match, producer OK/OK_NO_NEW_SIGNAL,
               selected bars cover target (or empty list with OK_NO_NEW_SIGNAL).
    REJECT = sidecar present but mismatch/stale — must not write.
    UNKNOWN = no usable sidecar (filename-only path).
    """
    notes: list[str] = []
    if not meta:
        return "UNKNOWN", ["no sidecar metadata"]

    payload = meta.get("payload") or {}
    norm = [str(t).strip().upper() for t in tickers if str(t).strip()]
    if int(payload.get("count", -1)) != len(norm):
        return "REJECT", [
            f"{source_kind}: count mismatch meta={payload.get('count')} list={len(norm)}"
        ]
    expect_hash = payload.get("sha256")
    got_hash = payload_hash(norm)
    if not expect_hash or expect_hash != got_hash:
        return "REJECT", [
            f"{source_kind}: sha256 mismatch meta={expect_hash} list={got_hash}"
        ]

    meta_session = meta.get("target_session")
    if meta_session != target_session.isoformat():
        return "REJECT", [
            f"{source_kind}: target_session mismatch meta={meta_session} "
            f"expected={target_session.isoformat()}"
        ]

    status = meta.get("status")
    if status == "FAIL_STALE":
        return "REJECT", [f"{source_kind}: producer status FAIL_STALE"]
    if status not in ("OK", "OK_NO_NEW_SIGNAL"):
        return "REJECT", [f"{source_kind}: producer status={status}"]

    # selected last bars coverage
    sel = meta.get("selected_last_bars") or {}
    if status == "OK":
        if not sel.get("all_match_target", False):
            return "REJECT", [f"{source_kind}: selected_last_bars not all target session"]
        if int(sel.get("n") or 0) != len(norm):
            # allow truncated by_ticker map but n must match
            notes.append(
                f"{source_kind}: selected_last_bars.n={sel.get('n')} list={len(norm)}"
            )
            if int(sel.get("n") or -1) != len(norm):
                return "REJECT", notes
        ib = meta.get("input_bars") or {}
        if not ib.get("fresh_ok", False):
            return "REJECT", [f"{source_kind}: input_bars.fresh_ok is false"]

    notes.append(f"{source_kind}: sidecar VERIFIED status={status}")
    return "VERIFIED", notes
