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



def normalize_readback_rows(raw: list[list[Any]]) -> list[list[Any]]:
    """Readback normalization for count checks.

    Allowed only: canonical empty (``[]`` / ``[[]]`` / all-blank rows) and
    trimming of TRAILING all-blank rows (Sheets API omits them anyway).
    Interior blank rows are kept so they are counted (never silently removed).
    """
    rows = [list(r) for r in (raw or [])]

    def _blank(r: list[Any]) -> bool:
        return not any(str(c).strip() for c in r)

    while rows and _blank(rows[-1]):
        rows.pop()
    return rows

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


def read_column(
    sa_json: str,
    spreadsheet_id: str,
    tab_title: str,
    column_letter: str,
    *,
    max_rows: int = 5000,
    skip_header: bool = True,
) -> list[str]:
    """Readonly fetch of one column (symbols only when skip_header)."""
    gc = _client(sa_json)
    sh = gc.open_by_key(spreadsheet_id)
    ws = sh.worksheet(tab_title)
    col = column_letter.upper().strip()
    values = ws.get(f"{col}1:{col}{max_rows}") or []
    flat = [(row[0] if row else "") for row in values]
    if skip_header and flat:
        # Drop row-1 label when it matches known header or is non-ticker-ish.
        label = QQSCAN_HEADERS.get(col)
        if label and flat[0] == label:
            flat = flat[1:]
        elif skip_header:
            flat = flat[1:]
    return [v.strip() for v in flat if str(v).strip()]


def read_tab_rows(
    sa_json: str,
    spreadsheet_id: str,
    tab_title: str,
    *,
    max_rows: int = 5000,
) -> list[list[str]]:
    """Readonly fetch of a tab's values (A1-style grid)."""
    gc = _client(sa_json)
    sh = gc.open_by_key(spreadsheet_id)
    ws = sh.worksheet(tab_title)
    values = ws.get(f"A1:Z{max_rows}") or []
    return [[str(c) for c in row] for row in values]


def readback_receipt(
    *,
    written_n: int,
    read_values: list[str],
    expected_sample: list[str] | None = None,
) -> dict[str, Any]:
    """Build a write/readback receipt without mutating Sheets."""
    read_n = len(read_values)
    sample_ok = None
    if expected_sample is not None:
        exp = [s.strip() for s in expected_sample if str(s).strip()]
        got = read_values[: len(exp)]
        sample_ok = got == exp
    return {
        "written_n": written_n,
        "readback_n": read_n,
        "count_match": written_n == read_n,
        "sample_match": sample_ok,
        "readback_sample": read_values[:10],
    }


def write_column(
    sa_json: str,
    spreadsheet_id: str,
    tab_title: str,
    column_letter: str,
    values: list[str],
    header: str | None = None,
    *,
    do_readback: bool = False,
) -> int | tuple[int, dict[str, Any]]:
    """Clear one column and write values.

    If ``header`` is set (or found in QQSCAN_HEADERS for that column), row 1 is
    the label and symbols start at row 2. Returns symbol count (excludes header).
    When do_readback=True, returns (count, receipt); default False returns int only.
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
        receipt = readback_receipt(written_n=0, read_values=[], expected_sample=[])
        return (0, receipt) if do_readback else 0
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
    if not do_readback:
        return len(values)
    # Re-open read path via same worksheet object for receipt.
    raw = ws.get(f"{col}1:{col}{max(len(body), 1)}") or []
    flat = [(row[0] if row else "") for row in raw]
    if label and flat and flat[0] == label:
        syms = [v.strip() for v in flat[1:] if str(v).strip()]
    else:
        syms = [v.strip() for v in flat if str(v).strip()]
    receipt = readback_receipt(
        written_n=len(values),
        read_values=syms,
        expected_sample=values[: min(10, len(values))],
    )
    print(
        f"Sheets: readback {tab_title}!{col} n={receipt['readback_n']} "
        f"count_match={receipt['count_match']} sample_match={receipt['sample_match']}",
        flush=True,
    )
    return len(values), receipt


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
    *,
    do_readback: bool = True,
) -> dict[str, Any] | None:
    """tab_rows: tab_title -> data rows only (no header)."""
    gc = _client(sa_json)
    sh = gc.open_by_key(spreadsheet_id)
    receipts: dict[str, Any] = {}
    mismatched: list[str] = []
    for title, rows in tab_rows.items():
        ws = _ensure_worksheet(sh, title)
        print(f"Sheets: write {title} n={len(rows)}", flush=True)
        _clear_and_write(ws, rows)
        if do_readback:
            got = normalize_readback_rows(ws.get(f"A1:C{max(len(rows), 1)}") or [])
            receipts[title] = {
                "written_n": len(rows),
                "readback_n": len(got),
                "count_match": len(got) == len(rows),
                "readback_sample": got[:3],
            }
            if not receipts[title]["count_match"]:
                mismatched.append(title)
            print(
                f"Sheets: readback {title} n={len(got)} "
                f"count_match={receipts[title]['count_match']}",
                flush=True,
            )
        time.sleep(0.5)
    if mismatched:
        # Fail closed: readback must equal what was written (interior blank rows count).
        raise RuntimeError(f"Sheets readback count mismatch: {mismatched} receipts={receipts}")
    print("Sheets: done", flush=True)
    return receipts if do_readback else None
