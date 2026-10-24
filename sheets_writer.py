#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Overwrite ExtraScan tabs via service account (open_by_key — folder moves OK)."""
from __future__ import annotations

import json
import time
from typing import Any

CHUNK = 4000

# QQscan row-1 source labels (symbols start at row 2 when header is set).
QQSCAN_HEADERS: dict[str, str] = {
    "A": "Strong_Stocks (my-stock-scanner)",
    "G": "yellowETF∪whiteETF + holdings leaders",
    "H": "yellowALL∪whiteballALL",
    "J": "Maggiestocks (Q-scanner)",
    "L": "週線主力做多上穿做空",
    "M": "日線主力做多上穿做空",
}


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


def _ensure_worksheet(sh, title: str, rows: int = 10000, cols: int = 26):
    try:
        return sh.worksheet(title)
    except Exception:
        return sh.add_worksheet(title=title, rows=rows, cols=cols)


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


def write_column(
    sa_json: str,
    spreadsheet_id: str,
    tab_title: str,
    column_letter: str,
    values: list[str],
    header: str | None = None,
) -> int:
    """Clear one column and write values.

    If ``header`` is set (or found in QQSCAN_HEADERS for that column), row 1 is
    the label and symbols start at row 2. Returns symbol count (excludes header).
    """
    gc = _client(sa_json)
    sh = gc.open_by_key(spreadsheet_id)
    ws = _ensure_worksheet(sh, tab_title)
    col = column_letter.upper().strip()
    label = header
    if label is None:
        label = QQSCAN_HEADERS.get(col)
    body: list[list[str]] = []
    if label:
        body.append([label])
    body.extend([[v] for v in values])
    need_rows = max(len(body), 1000)
    if ws.row_count < need_rows:
        ws.resize(rows=need_rows)
    ws.batch_clear([f"{col}:{col}"])
    if not body:
        print(f"Sheets: {tab_title}!{col} cleared (0 rows)", flush=True)
        return 0
    start = 0
    while start < len(body):
        end = min(start + CHUNK, len(body))
        ws.update(f"{col}{start + 1}", body[start:end], value_input_option="RAW")
        start = end
        time.sleep(0.4)
    print(
        f"Sheets: write {tab_title}!{col} symbols={len(values)}"
        + (f" header={label!r}" if label else ""),
        flush=True,
    )
    return len(values)


def update_qqscan_headers(
    sa_json: str,
    spreadsheet_id: str,
    tab_title: str = "QQscan",
    columns: dict[str, str] | None = None,
) -> None:
    """Write/refresh row-1 labels only — does not clear symbol rows."""
    labels = columns or QQSCAN_HEADERS
    gc = _client(sa_json)
    sh = gc.open_by_key(spreadsheet_id)
    ws = _ensure_worksheet(sh, tab_title)
    cells = []
    for col, text in labels.items():
        c = col.upper().strip()
        cells.append({"range": f"{c}1", "values": [[text]]})
    if not cells:
        return
    ws.batch_update(cells, value_input_option="RAW")
    print(
        f"Sheets: {tab_title} row1 headers updated: {', '.join(sorted(labels))}",
        flush=True,
    )


def write_tabs(
    sa_json: str,
    spreadsheet_id: str,
    tab_rows: dict[str, list[list[Any]]],
) -> None:
    """tab_rows: tab_title -> data rows only (no header)."""
    gc = _client(sa_json)
    sh = gc.open_by_key(spreadsheet_id)
    for title, rows in tab_rows.items():
        ws = _ensure_worksheet(sh, title)
        print(f"Sheets: write {title} n={len(rows)}", flush=True)
        _clear_and_write(ws, rows)
        time.sleep(0.5)
    print("Sheets: done", flush=True)
