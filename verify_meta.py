#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Machine-readable freshness / write verification metadata for ExtraScan jobs.

Schema (JSON object):
  schema_version, job, status, target_session, checked_at_utc,
  input_bars {min,max,fresh,screened,frac,unique,missing_count},
  sources [], sheets {attempted,written,skipped_reason},
  notes []

status values:
  OK | OK_NO_NEW_SIGNAL | FAIL_STALE | UPSTREAM_PENDING | FAIL_CONFIG | DRY_RUN
"""
from __future__ import annotations

import json
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any


SCHEMA_VERSION = 1


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def bar_distribution(bar_dates: list[date], expected: date) -> dict[str, Any]:
    n = len(bar_dates)
    if n == 0:
        return {
            "screened": 0,
            "fresh": 0,
            "frac": 0.0,
            "min": None,
            "max": None,
            "expected": expected.isoformat(),
            "unique": [],
            "missing_count": 0,
        }
    fresh = sum(1 for d in bar_dates if d == expected)
    return {
        "screened": n,
        "fresh": fresh,
        "frac": round(fresh / n, 4),
        "min": min(bar_dates).isoformat(),
        "max": max(bar_dates).isoformat(),
        "expected": expected.isoformat(),
        "unique": sorted({d.isoformat() for d in bar_dates}),
        "missing_count": 0,
    }


def write_verify_meta(path: Path | str, payload: dict[str, Any]) -> Path:
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    body = {"schema_version": SCHEMA_VERSION, "checked_at_utc": utc_now_iso(), **payload}
    out.write_text(json.dumps(body, indent=2, sort_keys=False) + "\n", encoding="utf-8")
    return out
