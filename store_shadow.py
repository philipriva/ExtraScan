#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""SHADOW: recompute a reader from the OHLCV store and compare with the official Sheet.

NEVER writes Sheets:
  * Google credentials are forced to READ-ONLY scopes (any write -> HTTP 403), and
  * every gspread write method is replaced by a recorder (no request is sent).
Data: yfinance.download / Ticker.history are served from the local store shards
(downloaded read-only); Massive and Stooq are disabled, so results reflect the
store only. Non-OHLCV Yahoo calls (e.g. market cap) are left untouched.

Output: one line per reader
  SHADOW reader=<name> session=<d> identical=yes|no targets=<n> diff=<summary>
"""
from __future__ import annotations

import argparse
import json
import os
import runpy
import sys
import traceback
from pathlib import Path

import pandas as pd

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import ohlcv_store as st  # noqa: E402

READONLY = ["https://www.googleapis.com/auth/spreadsheets.readonly",
            "https://www.googleapis.com/auth/drive.readonly"]
_CAPTURE: list[dict] = []


# ------------------------------------------------------------ store frames ----
class StoreReader:
    def __init__(self, store_dir: Path):
        self.dir = Path(store_dir)
        self.cache: dict = {}

    def get(self, tickers: list[str]) -> dict:
        need = [t for t in tickers if t not in self.cache]
        by_shard: dict = {}
        for t in need:
            by_shard.setdefault(st.shard_of(st.to_yahoo(t)), []).append(t)
        for k, ts in by_shard.items():
            p = self.dir / f"shard_{k:02d}.parquet"
            ymap = {st.to_yahoo(t): t for t in ts}
            try:
                d = pd.read_parquet(p, filters=[("ticker", "in", list(ymap))])
                d["ticker"] = d["ticker"].astype(str)
            except Exception:
                d = st.empty_frame()
            for t in ts:
                self.cache[t] = d[d["ticker"] == st.to_yahoo(t)]
        return {t: self.cache.get(t) for t in tickers}


def yf_like(rows: pd.DataFrame, auto_adjust: bool) -> pd.DataFrame:
    """Unified rows -> yfinance-style frame (DatetimeIndex, Open..Volume)."""
    if rows is None or len(rows) == 0:
        return pd.DataFrame(columns=["Open", "High", "Low", "Close", "Volume"])
    r = rows.sort_values("date")
    f = pd.DataFrame({"Open": r["open"].values, "High": r["high"].values, "Low": r["low"].values,
                      "Close": r["close"].values, "Volume": r["volume"].astype("float64").values},
                     index=pd.DatetimeIndex(r["date"].values, name="Date"))
    adj = pd.Series(r["adj_close"].values, index=f.index)
    if auto_adjust:
        # yfinance auto_adjust: ratio = AdjClose/Close applied to O/H/L/C. Massive bars
        # (no adj) are newest bars -> ratio 1 unless a later Yahoo bar has a ratio.
        ratio = (adj / f["Close"]).bfill().fillna(1.0)
        for c in ("Open", "High", "Low", "Close"):
            f[c] = f[c] * ratio
    else:
        f["Adj Close"] = adj.fillna(f["Close"])
    return f


def _slice(f: pd.DataFrame, period=None, start=None, end=None) -> pd.DataFrame:
    if start is not None:
        f = f[f.index >= pd.Timestamp(start)]
    if end is not None:
        f = f[f.index < pd.Timestamp(end)]
    if period and period != "max":
        n = {"d": 1, "mo": 31, "y": 366}
        for suf, mult in sorted(n.items(), key=lambda x: -len(x[0])):
            if str(period).endswith(suf):
                num = int(str(period)[: -len(suf)])
                if suf == "d":
                    f = f.iloc[-num:]
                else:
                    f = f[f.index >= f.index.max() - pd.Timedelta(days=num * mult)] if len(f) else f
                break
    return f


def install_patches(store: StoreReader) -> None:
    import yfinance as yf

    def fake_download(tickers, period=None, start=None, end=None, interval="1d", group_by="column",
                      auto_adjust=True, **kw):
        ts = tickers.replace(",", " ").split() if isinstance(tickers, str) else list(tickers)
        got = store.get(ts)
        frames = {t: _slice(yf_like(got[t], auto_adjust), period, start, end) for t in ts}
        frames = {t: f for t, f in frames.items() if len(f)}
        if not frames:
            return pd.DataFrame()
        return pd.concat(frames, axis=1)  # columns (ticker, field) == group_by='ticker'

    def fake_history(self, period="1mo", interval="1d", start=None, end=None, auto_adjust=True, **kw):
        t = getattr(self, "ticker", "")
        return _slice(yf_like(store.get([t])[t], auto_adjust), period, start, end)

    yf.download = fake_download
    yf.Ticker.history = fake_history
    os.environ.pop("MASSIVE_API_KEY", None)
    try:
        import marketdata
        marketdata._fetch_stooq = lambda *a, **k: None
        marketdata._fetch_massive_grouped = lambda *a, **k: None
    except Exception:
        pass

    # Read-only Google credentials (hard guarantee: writes would get HTTP 403).
    from google.oauth2 import service_account as sa
    C = sa.Credentials
    o_info, o_file = C.from_service_account_info.__func__, C.from_service_account_file.__func__
    C.from_service_account_info = classmethod(lambda cls, info, **kw: o_info(cls, info, **{**kw, "scopes": READONLY}))
    C.from_service_account_file = classmethod(lambda cls, fn, **kw: o_file(cls, fn, **{**kw, "scopes": READONLY}))

    # Record (never send) every gspread write.
    import gspread
    W, S = gspread.Worksheet, gspread.Spreadsheet

    def rec(kind):
        def f(self, *a, **k):
            ss = getattr(self, "spreadsheet", self)
            _CAPTURE.append({"kind": kind, "sheet": getattr(ss, "id", ""), "tab": getattr(self, "title", ""),
                             "args": a, "kwargs": k})
            return {}
        return f

    for m in ("update", "batch_update", "clear", "append_rows", "append_row", "update_cells", "update_cell",
              "update_acell", "resize", "insert_rows", "delete_rows", "format", "batch_clear", "update_title",
              "update_index", "add_rows", "add_cols", "batch_format", "merge_cells", "update_note", "clear_basic_filter"):
        if hasattr(W, m):
            setattr(W, m, rec(f"ws.{m}"))
    for m in ("batch_update", "values_update", "values_append", "values_clear", "values_batch_update",
              "del_worksheet", "duplicate_sheet", "reorder_worksheets", "update_title"):
        if hasattr(S, m):
            setattr(S, m, rec(f"ss.{m}"))
    orig_add = S.add_worksheet

    def fake_add(self, title, rows=1000, cols=26, index=None):
        _CAPTURE.append({"kind": "ss.add_worksheet", "sheet": self.id, "tab": title, "args": (), "kwargs": {}})
        return gspread.Worksheet(self, {"title": title, "sheetId": -1, "index": 0,
                                        "gridProperties": {"rowCount": rows, "columnCount": cols}})
    S.add_worksheet = fake_add
    _ = orig_add


# ---------------------------------------------------------------- compare ----
def _norm_vals(v) -> list[list[str]]:
    rows = [[str(c).strip() for c in (r if isinstance(r, (list, tuple)) else [r])] for r in (v or [])]
    rows = [r[: max([i + 1 for i, c in enumerate(r) if c] or [0])] for r in rows]
    while rows and not any(rows[-1]):
        rows.pop()
    return rows


def compare_captured(sa_json: str) -> tuple[bool, list[str]]:
    """Compare recorded value-writes with what the official Sheet holds now."""
    import gspread
    from google.oauth2.service_account import Credentials

    gc = gspread.authorize(Credentials.from_service_account_info(json.loads(sa_json), scopes=READONLY))
    writes = []
    for c in _CAPTURE:
        if c["kind"] != "ws.update":
            continue
        a, k = c["args"], c["kwargs"]
        vals = k.get("values")
        rng = k.get("range_name")
        if vals is None and a:
            if isinstance(a[0], list):
                vals, rng = a[0], (a[1] if len(a) > 1 else rng)
            else:
                rng, vals = a[0], (a[1] if len(a) > 1 else vals)
        writes.append((c["sheet"], c["tab"], rng or "A1", vals))
    if not writes:
        return False, ["no_value_writes_captured"]
    diffs = []
    for sid, tab, rng, vals in writes:
        try:
            ws = gc.open_by_key(sid).worksheet(tab)
            off = ws.get(rng if ":" in str(rng) else None) if ":" in str(rng) else None
            if off is None:
                start = gspread.utils.a1_to_rowcol(str(rng).split("!")[-1])
                nr, nc = len(vals or []), max([len(r) if isinstance(r, list) else 1 for r in vals or [[]]] or [1])
                end = gspread.utils.rowcol_to_a1(start[0] + max(nr, 1) - 1, start[1] + max(nc, 1) - 1)
                off = ws.get(f"{gspread.utils.rowcol_to_a1(*start)}:{end}")
        except Exception as e:
            diffs.append(f"{tab}!{rng}:read_error:{type(e).__name__}")
            continue
        a_, b_ = _norm_vals(vals), _norm_vals(off)
        if a_ != b_:
            fa = {x for r in a_ for x in r if x}
            fb = {x for r in b_ for x in r if x}
            diffs.append(f"{tab}!{rng}: shadow_only={sorted(fa - fb)[:8]} official_only={sorted(fb - fa)[:8]} "
                         f"rows {len(a_)} vs {len(b_)}")
    return not diffs, diffs or [f"{len(writes)} ranges identical"]


def cmd_run(a) -> int:
    a.script = str(Path(a.script).resolve())
    store = StoreReader(Path(a.store_dir))
    man = json.loads((Path(a.store_dir) / "manifest.json").read_text())
    sys.path.insert(0, str(Path(a.script).resolve().parent))
    os.chdir(Path(a.script).resolve().parent)
    install_patches(store)
    sys.argv = [a.script] + a.args
    code = 0
    try:
        runpy.run_path(a.script, run_name="__main__")
    except SystemExit as e:
        code = e.code if isinstance(e.code, int) else (0 if e.code is None else 1)
    except Exception:
        traceback.print_exc(limit=3)
        code = 1
    ok, diffs = compare_captured(os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"])
    exp = st.expected_session()
    stale = man.get("session") != exp.isoformat()
    line = (f"SHADOW reader={a.reader} session={exp} store_session={man.get('session')} "
            f"identical={'n/a' if stale else ('yes' if ok else 'no')} exit={code} writes_captured={len(_CAPTURE)} "
            f"diff={' | '.join(diffs)[:1500]}")
    print(line, flush=True)
    Path(a.result).write_text(json.dumps({"reader": a.reader, "identical": (None if stale else ok), "line": line,
                                          "diffs": diffs, "exit": code}))
    return 0


def cmd_fetch(a) -> int:
    d = Path(a.store_dir)
    d.mkdir(parents=True, exist_ok=True)
    drv = st.Drive(os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"], a.folder_id, readonly=True)
    drv.download("manifest.json", d / "manifest.json")
    man = json.loads((d / "manifest.json").read_text())
    for name, meta in man.get("shards", {}).items():
        drv.download(name, d / name)
        if meta.get("md5") and st.md5_file(d / name) != meta["md5"]:
            raise SystemExit(f"md5 mismatch {name}")
    print(f"store fetched session={man.get('session')} rows={man.get('rows')}", flush=True)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    f = sub.add_parser("fetch")
    f.add_argument("--store-dir", default="store")
    f.add_argument("--folder-id", default=os.environ.get("OHLCV_STORE_FOLDER_ID", st.DEFAULT_FOLDER_ID))
    r = sub.add_parser("run")
    r.add_argument("--reader", required=True)
    r.add_argument("--store-dir", default="store")
    r.add_argument("--script", required=True)
    r.add_argument("--result", required=True)
    r.add_argument("args", nargs=argparse.REMAINDER)
    a = ap.parse_args()
    if a.cmd == "run":
        a.store_dir = str(Path(a.store_dir).resolve())
        a.result = str(Path(a.result).resolve())
        if a.args and a.args[0] == "--":
            a.args = a.args[1:]
        return cmd_run(a)
    return cmd_fetch(a)


if __name__ == "__main__":
    sys.exit(main())
