#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Overwrite ExtraScan tabs via service account (open_by_key — folder moves OK)."""
from __future__ import annotations

import json
import time
from typing import Any

CHUNK = 4000


def _client(sa_json: str):
    import gspread
    from google.oauth2.service_account import Credentials

    info = json.loads(sa_json) if sa_json.lstrip().startswith("{") else None
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    if info is None:
        creds = Credentials.from_service_account_file(sa_json, scopes=scopes)
    else:
        creds = Credentials.from_service_account_info(info, scopes=scopes)
    return gspread.authorize(creds)


def _ensure_worksheet(sh, title: str):
    try:
        return sh.worksheet(title)
    except Exception:
        return sh.add_worksheet(title=title, rows=10000, cols=6)


def _clear_and_write(ws, rows: list[list[Any]]) -> None:
    ws.clear()
    if not rows:
        return
    start = 0
    while start < len(rows):
        end = min(start + CHUNK, len(rows))
        ws.update(f"A{start + 1}", rows[start:end], value_input_option="RAW")
        start = end
        time.sleep(0.4)


def write_tabs(
    sa_json: str,
    spreadsheet_id: str,
    tab_rows: dict[str, list[list[Any]]],
) -> None:
    """tab_rows: tab_title -> rows including header."""
    gc = _client(sa_json)
    sh = gc.open_by_key(spreadsheet_id)
    for title, rows in tab_rows.items():
        ws = _ensure_worksheet(sh, title)
        print(f"Sheets: write {title} n={max(0, len(rows) - 1)}", flush=True)
        _clear_and_write(ws, rows)
        time.sleep(0.5)
    print("Sheets: done", flush=True)
