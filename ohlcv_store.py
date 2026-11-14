#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Shared US daily OHLCV store (Drive parquet shards).

Long history: Yahoo (yfinance, auto_adjust=False -> split-adjusted, NOT dividend
adjusted), each ticker from listing (period=max). Daily increments: Massive grouped
daily (ONE request per session, adjusted=true = split-adjusted only). Yahoo is the
fallback for symbols Massive misses and the spot-check reference.

Unified format (one row per ticker+session):
  ticker   Yahoo-style symbol (BRK-B, ABC-WT, ABC-PA ...) - Yahoo format wins
  date     NYSE session date (Massive t ms-epoch -> America/New_York date)
  open/high/low/close  float64 rounded to 4 dp, split-adjusted
  adj_close  Yahoo dividend+split adjusted close (NaN on Massive bars)
  volume   int64 shares (split-adjusted), rounded
  source   'yahoo' | 'massive'

Storage: shard_NN.parquet (crc32(ticker) % 16) + manifest.json in the Drive folder
OHLCV_STORE_FOLDER_ID. Files are pre-created by the owner (the service account has
no Drive quota and never creates files). Manifest is written last.

This module NEVER writes any screener Sheet. Only the catalog spreadsheet tabs
store_summary / store_catalog are (re)written.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import random
import re
import sys
import time
import zlib
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Iterable, Optional

import pandas as pd

NY = "America/New_York"
N_SHARDS = 16
COLS = ["ticker", "date", "open", "high", "low", "close", "adj_close", "volume", "source"]
PRICE_COLS = ["open", "high", "low", "close", "adj_close"]
PRICE_DP = 4
MIN_FRESH = 0.90
SPLIT_HI, SPLIT_LO = 1.8, 0.55          # prev_close / new_close outside -> treat as split
COMPARE_TOL = 0.005                     # 0.5 % close diff flag
COMPARE_FIXED = ["AAPL", "NVDA", "QQQ", "SPY", "PBW"]
COMPARE_RANDOM = 20
COMPARE_MAX_FLAG_FRAC = 0.20            # > 20 % flagged -> formats broken -> BLOCK
MASSIVE_URL = "https://api.massive.com/v2/aggs/grouped/locale/us/market/stocks/{day}"
MASSIVE_MIN_GAP_SEC = 13.0              # free tier ~5 calls/min
DEFAULT_FOLDER_ID = "1ZLSzOu_ytfjcDUENkW6o0JBmh2b03F3R"
DEFAULT_CATALOG_ID = "1yuAfmzDP08S3axz4R1vrU2gquQMWk4Sck87tWQLk7us"
NASDAQ_LISTED = "https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt"
OTHER_LISTED = "https://www.nasdaqtrader.com/dynamic/SymDir/otherlisted.txt"

ROOT = Path(__file__).resolve().parent


def log(msg: str) -> None:
    print(msg, flush=True)


# ---------------------------------------------------------------- symbols ----
def to_yahoo(sym: str) -> str:
    """Nasdaq-Trader / ExtraScan symbol -> Yahoo symbol (Yahoo format wins)."""
    s = str(sym).strip().upper().replace(".", "-").replace("/", "-")
    s = re.sub(r"-WS$", "-WT", s)
    s = re.sub(r"-WS-([A-Z])$", r"-WT\1", s)
    return s


def massive_candidates(t: str) -> list[str]:
    """Massive ticker -> ordered Yahoo-style candidates.

    BRK.B->BRK-B, VAL.WS->VAL-WT, SFpB->SF-PB, PHXEp->PHXE-P, NMZr->NMZR/NMZ-R/NMZ-RI.
    """
    t = str(t).strip()
    m = re.fullmatch(r"([A-Z0-9.]+)p([A-Z]?)", t)
    if m:
        base = m.group(1).replace(".", "-")
        return [f"{base}-P{m.group(2)}"]
    m = re.fullmatch(r"([A-Z0-9.]+)r", t)
    if m:
        base = m.group(1).replace(".", "-")
        return [f"{base}R", f"{base}-R", f"{base}-RI"]
    m = re.fullmatch(r"([A-Z0-9.]+)w", t)
    if m:
        base = m.group(1).replace(".", "-")
        return [f"{base}W", f"{base}-WT"]
    y = to_yahoo(t)
    out = [y]
    if y.endswith("-WT"):
        out.append(y[:-3] + "-WS")
    return out


def resolve_massive(t: str, known: set[str]) -> str:
    c = massive_candidates(t)
    for x in c:
        if x in known:
            return x
    return c[0]


def shard_of(ticker: str) -> int:
    return zlib.crc32(ticker.encode()) % N_SHARDS


# ------------------------------------------------------------------ dates ----
def ms_to_session(ms: int) -> date:
    return pd.Timestamp(int(ms), unit="ms", tz="UTC").tz_convert(NY).date()


def index_to_dates(idx) -> list[date]:
    ix = pd.DatetimeIndex(idx)
    if ix.tz is not None:
        ix = ix.tz_convert(NY).tz_localize(None)
    return [d.date() for d in ix]


def expected_session(now: Optional[datetime] = None) -> date:
    sys.path.insert(0, str(ROOT))
    import marketdata  # existing NYSE calendar logic (unchanged)

    return marketdata.last_complete_us_session_date(now)


def is_session(d: date) -> bool:
    import marketdata

    return marketdata.is_nyse_trading_day(d)


def sessions_between(last: date, expected: date) -> list[date]:
    out, d = [], last + timedelta(days=1)
    while d <= expected:
        if is_session(d):
            out.append(d)
        d += timedelta(days=1)
    return out


# ------------------------------------------------------------ normalizing ----
def normalize(df: pd.DataFrame) -> pd.DataFrame:
    """Enforce the unified schema, rounding and dtypes; dedupe (ticker,date)."""
    if df is None or len(df) == 0:
        return empty_frame()
    d = df.copy()
    for c in COLS:
        if c not in d.columns:
            d[c] = float("nan") if c == "adj_close" else None
    d = d[COLS]
    d["ticker"] = d["ticker"].astype(str)
    d["date"] = pd.to_datetime(d["date"]).dt.normalize()
    for c in PRICE_COLS:
        d[c] = pd.to_numeric(d[c], errors="coerce").astype("float64").round(PRICE_DP)
    d = d.dropna(subset=["open", "high", "low", "close"])
    d["volume"] = pd.to_numeric(d["volume"], errors="coerce").fillna(0).round().astype("int64")
    d["source"] = d["source"].astype(str)
    bad = ~d["source"].isin(["yahoo", "massive"])
    if bad.any():
        raise ValueError(f"unknown source values: {sorted(d.loc[bad, 'source'].unique())[:5]}")
    d = d.sort_values(["ticker", "date"]).drop_duplicates(["ticker", "date"], keep="last")
    return d.reset_index(drop=True)


def empty_frame() -> pd.DataFrame:
    d = pd.DataFrame({c: pd.Series(dtype="float64") for c in COLS})
    d["ticker"] = d["ticker"].astype(str)
    d["source"] = d["source"].astype(str)
    d["date"] = pd.to_datetime(d["date"])
    d["volume"] = d["volume"].astype("int64")
    return d


def yahoo_frame_to_rows(ticker: str, f: pd.DataFrame) -> pd.DataFrame:
    """One yfinance (auto_adjust=False) ticker frame -> unified rows."""
    if f is None or len(f) == 0:
        return empty_frame()
    f = f.copy()
    if isinstance(f.columns, pd.MultiIndex):
        f.columns = f.columns.get_level_values(-1)
    f = f.dropna(how="all")
    if len(f) == 0:
        return empty_frame()
    get = lambda c: f[c] if c in f.columns else float("nan")  # noqa: E731
    rows = pd.DataFrame({
        "ticker": ticker,
        "date": pd.to_datetime(index_to_dates(f.index)),
        "open": get("Open").values,
        "high": get("High").values,
        "low": get("Low").values,
        "close": get("Close").values,
        "adj_close": (get("Adj Close").values if "Adj Close" in f.columns else float("nan")),
        "volume": get("Volume").values,
        "source": "yahoo",
    })
    return normalize(rows)


def massive_results_to_rows(results: list[dict], known: set[str], session: date) -> pd.DataFrame:
    """Massive grouped results -> unified rows (only bars whose NY date == session)."""
    recs = []
    for r in results or []:
        t = r.get("T")
        vals = [r.get(k) for k in ("o", "h", "l", "c")]
        if not t or any(v is None for v in vals) or r.get("t") is None:
            continue
        if ms_to_session(r["t"]) != session:
            continue
        recs.append({
            "ticker": resolve_massive(t, known), "date": pd.Timestamp(session),
            "open": vals[0], "high": vals[1], "low": vals[2], "close": vals[3],
            "adj_close": float("nan"), "volume": r.get("v") or 0, "source": "massive",
        })
    return normalize(pd.DataFrame(recs)) if recs else empty_frame()


def merge_rows(base: pd.DataFrame, new: pd.DataFrame) -> pd.DataFrame:
    """Append/override bars (new wins on same ticker+date)."""
    if new is None or len(new) == 0:
        return base
    if base is None or len(base) == 0:
        return normalize(new)
    return normalize(pd.concat([base, new], ignore_index=True))


def replace_tickers(base: pd.DataFrame, full: pd.DataFrame) -> pd.DataFrame:
    """Replace whole histories of the tickers in ``full`` (split re-pull)."""
    if full is None or len(full) == 0:
        return base
    keep = base[~base["ticker"].isin(set(full["ticker"]))]
    return normalize(pd.concat([keep, full], ignore_index=True))


def split_suspects(prev_close: dict, new: pd.DataFrame) -> list[str]:
    out = []
    for t, c in zip(new["ticker"], new["close"]):
        p = prev_close.get(t)
        if p is None or not c or c <= 0 or p <= 0:
            continue
        r = p / c
        if r > SPLIT_HI or r < SPLIT_LO:
            out.append(t)
    return out


def compare_closes(massive: dict, yahoo: dict, tol: float = COMPARE_TOL) -> list[dict]:
    """[{ticker, massive, yahoo, diff, flag}] for tickers present in both."""
    out = []
    for t, m in massive.items():
        y = yahoo.get(t)
        if y is None or not m:
            out.append({"ticker": t, "massive": m, "yahoo": y, "diff": None, "flag": False})
            continue
        diff = abs(m - y) / abs(y) if y else None
        out.append({"ticker": t, "massive": m, "yahoo": y, "diff": diff,
                    "flag": bool(diff is not None and diff > tol)})
    return out


# ---------------------------------------------------------------- universe ----
def _dir(url: str) -> pd.DataFrame:
    import requests

    t = requests.get(url, timeout=60).text
    t = "\n".join(l for l in t.splitlines() if l and not l.startswith("File Creation"))
    return pd.read_csv(io.StringIO(t), sep="|", dtype=str)


def build_universe() -> list[str]:
    """Same source as us_exclude (Nasdaq Trader, Test Issue=N, no $/^) + etf_list + pulse."""
    a = _dir(NASDAQ_LISTED).rename(columns={"Symbol": "sym"})
    b = _dir(OTHER_LISTED).rename(columns={"ACT Symbol": "sym"})
    df = pd.concat([x[[c for c in ("sym", "Test Issue") if c in x.columns]] for x in (a, b)])
    df = df.dropna(subset=["sym"])
    if "Test Issue" in df.columns:
        df = df[df["Test Issue"].fillna("N") == "N"]
    df = df[~df.sym.str.contains(r"[\$\^]")]
    syms = {to_yahoo(s) for s in df.sym}
    for line in (ROOT / "etf_list.txt").read_text(encoding="utf-8").splitlines():
        s = line.split("#", 1)[0].strip()
        if s:
            syms.add(to_yahoo(s))
    try:
        pu = pd.read_csv(ROOT / "pulse_universe.csv", dtype=str)
        syms |= {to_yahoo(s) for s in pu["ticker"].dropna() if not str(s).startswith("^")}
    except Exception as e:
        log(f"pulse_universe skipped: {type(e).__name__}")
    return sorted(s for s in syms if s)


# ------------------------------------------------------------------ Yahoo ----
def _is_rate_limited(errors: dict) -> bool:
    txt = " ".join(str(v) for v in (errors or {}).values()).lower()
    return "rate limit" in txt or "too many requests" in txt or "429" in txt


def yahoo_download(tickers: list[str], *, period: Optional[str] = "max", start: Optional[str] = None,
                   sleep: Callable = time.sleep, max_tries: int = 6, dl: Optional[Callable] = None
                   ) -> tuple[dict, bool]:
    """Batch download (auto_adjust=False). Returns ({ticker: rows}, rate_limited_at_end).

    On 429 / rate-limit: sleep 60, 120, 240 ... s and retry the SAME batch.
    """
    import yfinance as yf

    dl = dl or yf.download
    kw = dict(interval="1d", group_by="ticker", auto_adjust=False, actions=False,
              threads=True, progress=False)
    if start:
        kw["start"] = start
    else:
        kw["period"] = period
    for attempt in range(max_tries):
        limited = False
        try:
            raw = dl(tickers, **kw)
            import yfinance.shared as yfs

            errs = dict(getattr(yfs, "_ERRORS", {}) or {})
            limited = _is_rate_limited(errs)
        except Exception as e:
            raw = None
            limited = "ratelimit" in type(e).__name__.lower() or "429" in str(e)
        got: dict = {}
        if raw is not None and len(raw):
            for t in tickers:
                try:
                    f = raw[t] if isinstance(raw.columns, pd.MultiIndex) and t in raw.columns.get_level_values(0) else (raw if len(tickers) == 1 else None)
                except Exception:
                    f = None
                if f is not None:
                    r = yahoo_frame_to_rows(t, f)
                    if len(r):
                        got[t] = r
        if limited and len(got) < len(tickers) * 0.5:
            wait = min(60 * (2 ** attempt), 900)
            log(f"yahoo rate-limited (attempt {attempt + 1}); sleep {wait}s")
            sleep(wait)
            continue
        return got, False
    return {}, True


# ---------------------------------------------------------------- Massive ----
_last_massive_call = [0.0]


def massive_grouped(session: date, *, get: Optional[Callable] = None, sleep: Callable = time.sleep) -> Optional[list]:
    """ONE grouped-daily request for the whole market. Key never printed."""
    key = os.environ.get("MASSIVE_API_KEY", "").strip()
    if not key:
        log("massive: no key")
        return None
    import requests

    get = get or requests.get
    for attempt in range(3):
        gap = time.monotonic() - _last_massive_call[0]
        if gap < MASSIVE_MIN_GAP_SEC:
            sleep(MASSIVE_MIN_GAP_SEC - gap)
        _last_massive_call[0] = time.monotonic()
        try:
            r = get(MASSIVE_URL.format(day=session.isoformat()), params={"adjusted": "true"},
                    headers={"Authorization": f"Bearer {key}"}, timeout=60)
        except Exception as e:
            log(f"massive {session}: {type(e).__name__}")
            continue
        if r.status_code == 429:
            log(f"massive {session}: HTTP 429; sleep 65s")
            sleep(65)
            continue
        if r.status_code != 200:
            log(f"massive {session}: HTTP {r.status_code}")
            return None
        res = (r.json() or {}).get("results") or []
        log(f"massive {session}: {len(res)} rows")
        return res
    return None


# ------------------------------------------------------------------ Drive ----
class Drive:
    def __init__(self, sa_json: str, folder_id: str, readonly: bool = False):
        from google.oauth2.service_account import Credentials
        from googleapiclient.discovery import build

        scope = "https://www.googleapis.com/auth/drive" + (".readonly" if readonly else "")
        info = json.loads(sa_json)
        self.creds = Credentials.from_service_account_info(info, scopes=[scope])
        self.svc = build("drive", "v3", credentials=self.creds, cache_discovery=False)
        self.folder = folder_id
        self._ids: Optional[dict] = None

    def ids(self) -> dict:
        if self._ids is None:
            q = f"'{self.folder}' in parents and trashed=false"
            res, tok = {}, None
            while True:
                r = self.svc.files().list(q=q, fields="nextPageToken,files(id,name,size)",
                                          pageSize=200, pageToken=tok,
                                          supportsAllDrives=True, includeItemsFromAllDrives=True).execute()
                for f in r.get("files", []):
                    res[f["name"]] = f["id"]
                tok = r.get("nextPageToken")
                if not tok:
                    break
            self._ids = res
        return self._ids

    def fid(self, name: str) -> str:
        i = self.ids().get(name)
        if not i:
            raise SystemExit(f"Drive file '{name}' missing in store folder (SA cannot create; owner must pre-create)")
        return i

    def download(self, name: str, dest: Path) -> Path:
        from googleapiclient.http import MediaIoBaseDownload

        req = self.svc.files().get_media(fileId=self.fid(name), supportsAllDrives=True)
        with open(dest, "wb") as fh:
            dl = MediaIoBaseDownload(fh, req, chunksize=64 * 1024 * 1024)
            done = False
            while not done:
                _, done = dl.next_chunk(num_retries=5)
        return dest

    def upload(self, name: str, src: Path, mime: str = "application/octet-stream") -> dict:
        from googleapiclient.http import MediaFileUpload

        media = MediaFileUpload(str(src), mimetype=mime, resumable=True, chunksize=32 * 1024 * 1024)
        r = self.svc.files().update(fileId=self.fid(name), media_body=media,
                                    fields="id,size,md5Checksum,modifiedTime",
                                    supportsAllDrives=True).execute(num_retries=5)
        self._prune(r["id"])
        return r

    def _prune(self, file_id: str, keep: int = 2) -> None:
        """Keep only the newest ``keep`` revisions (owner quota)."""
        try:
            revs = self.svc.revisions().list(fileId=file_id, fields="revisions(id,modifiedTime)").execute().get("revisions", [])
            for rv in revs[:-keep]:
                try:
                    self.svc.revisions().delete(fileId=file_id, revisionId=rv["id"]).execute()
                except Exception:
                    pass
        except Exception:
            pass


def md5_file(p: Path) -> str:
    h = hashlib.md5()
    with open(p, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def read_shard(p: Path) -> pd.DataFrame:
    if not p.exists() or p.stat().st_size < 64:
        return empty_frame()
    try:
        return normalize(pd.read_parquet(p))
    except Exception:
        return empty_frame()


def write_shard(df: pd.DataFrame, p: Path) -> None:
    d = normalize(df)
    d["ticker"] = d["ticker"].astype("category")
    d["source"] = d["source"].astype("category")
    d.to_parquet(p, index=False, compression="zstd")


# --------------------------------------------------------------- catalog ----
def write_catalog(sa_json: str, sheet_id: str, summary: dict, per_ticker: pd.DataFrame) -> None:
    """Rewrite tabs store_summary / store_catalog in the catalog spreadsheet only."""
    import gspread
    from google.oauth2.service_account import Credentials

    creds = Credentials.from_service_account_info(
        json.loads(sa_json), scopes=["https://www.googleapis.com/auth/spreadsheets"])
    sh = gspread.authorize(creds).open_by_key(sheet_id)

    def tab(title, rows, cols):
        try:
            ws = sh.worksheet(title)
        except gspread.WorksheetNotFound:
            ws = sh.add_worksheet(title=title, rows=rows, cols=cols)
        if ws.row_count < rows:
            ws.resize(rows=rows)
        return ws

    ws = tab("store_summary", 60, 2)
    ws.clear()
    ws.update(values=[["key", "value"]] + [[k, str(v)] for k, v in summary.items()], range_name="A1")
    vals = [list(per_ticker.columns)] + per_ticker.astype(str).values.tolist()
    ws = tab("store_catalog", len(vals) + 10, len(per_ticker.columns))
    ws.clear()
    for i in range(0, len(vals), 5000):
        ws.update(values=vals[i:i + 5000], range_name=f"A{i + 1}")
        time.sleep(1)


def per_ticker_catalog(frames: Iterable[pd.DataFrame]) -> pd.DataFrame:
    parts = []
    for d in frames:
        if len(d) == 0:
            continue
        g = d.groupby("ticker", observed=True)
        parts.append(pd.DataFrame({
            "first_date": g["date"].min().dt.date.astype(str),
            "last_date": g["date"].max().dt.date.astype(str),
            "rows": g.size(),
            "last_source": g["source"].last().astype(str),
        }))
    if not parts:
        return pd.DataFrame(columns=["ticker", "first_date", "last_date", "rows", "last_source", "shard"])
    c = pd.concat(parts).reset_index().rename(columns={"index": "ticker"})
    c["shard"] = c["ticker"].map(shard_of)
    return c.sort_values("ticker").reset_index(drop=True)


# ------------------------------------------------------------- operations ----
def part_tickers(universe: list[str], part: int, parts: int) -> list[str]:
    return [t for i, t in enumerate(sorted(universe)) if i % parts == part]


def cmd_backfill_part(a) -> int:
    """Download period=max for one part; resumable via existing part dir contents."""
    out = Path(a.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    uni = json.loads(Path(a.universe_json).read_text())
    mine = part_tickers(uni, a.part, a.parts)
    done_p = out / "done.json"
    done = set(json.loads(done_p.read_text())) if done_p.exists() else set()
    todo = [t for t in mine if t not in done]
    log(f"part {a.part}/{a.parts}: assigned={len(mine)} already_done={len(done & set(mine))} todo={len(todo)}")
    t0 = time.monotonic()
    chunk_i = len(list(out.glob("rows_*.parquet")))
    buf: list[pd.DataFrame] = []
    n_ok = n_empty = 0
    limited_stop = False
    for i in range(0, len(todo), a.batch):
        if (time.monotonic() - t0) / 60 > a.budget_min:
            log("time budget reached; stopping cleanly (rerun resumes)")
            break
        b = todo[i:i + a.batch]
        got, limited = yahoo_download(b, period="max")
        if limited:
            log("persistent rate limit; stopping cleanly (rerun resumes)")
            limited_stop = True
            break
        for t in b:
            if t in got:
                buf.append(got[t])
                n_ok += 1
            else:
                n_empty += 1
            done.add(t)
        if sum(len(x) for x in buf) > 2_000_000 or i + a.batch >= len(todo):
            if buf:
                normalize(pd.concat(buf, ignore_index=True)).to_parquet(out / f"rows_{chunk_i:04d}.parquet", index=False, compression="zstd")
                chunk_i += 1
                buf = []
            done_p.write_text(json.dumps(sorted(done)))
        if (i // a.batch) % 10 == 0:
            log(f"progress part {a.part}: {min(i + a.batch, len(todo))}/{len(todo)} ok={n_ok} empty={n_empty} "
                f"elapsed={(time.monotonic() - t0) / 60:.1f}m")
        time.sleep(a.pause)
    if buf:
        normalize(pd.concat(buf, ignore_index=True)).to_parquet(out / f"rows_{chunk_i:04d}.parquet", index=False, compression="zstd")
    done_p.write_text(json.dumps(sorted(done)))
    complete = set(mine) <= done
    (out / "status.json").write_text(json.dumps({"part": a.part, "assigned": len(mine), "done": len(done & set(mine)),
                                                 "complete": complete, "rate_limited": limited_stop}))
    log(f"PART part={a.part} assigned={len(mine)} done={len(done & set(mine))} ok_this_run={n_ok} "
        f"empty_this_run={n_empty} complete={'yes' if complete else 'no'} minutes={(time.monotonic() - t0) / 60:.1f}")
    return 0


def sample_compare(session: date, mrows: pd.DataFrame, seed: int) -> tuple[list[dict], int]:
    pool = sorted(set(mrows["ticker"]) - set(COMPARE_FIXED))
    rnd = random.Random(seed).sample(pool, min(COMPARE_RANDOM, len(pool)))
    names = [t for t in COMPARE_FIXED if t in set(mrows["ticker"])] + rnd
    mclose = dict(zip(mrows["ticker"], mrows["close"]))
    got, _ = yahoo_download(names, period=None, start=(session - timedelta(days=10)).isoformat())
    yclose = {}
    for t, r in got.items():
        r = r[r["date"] == pd.Timestamp(session)]
        if len(r):
            yclose[t] = float(r["close"].iloc[0])
    res = compare_closes({t: mclose[t] for t in names}, yclose)
    flagged = sum(1 for x in res if x["flag"])
    for x in res:
        d = "NA" if x["diff"] is None else f"{x['diff'] * 100:.3f}%"
        log(f"COMPARE {session} {x['ticker']} massive={x['massive']} yahoo={x['yahoo']} diff={d}{' FLAG' if x['flag'] else ''}")
    return res, flagged


def _load_universe(a) -> list[str]:
    if a.universe_json and Path(a.universe_json).exists():
        return json.loads(Path(a.universe_json).read_text())
    return build_universe()


def cmd_universe(a) -> int:
    u = build_universe()
    Path(a.out).write_text(json.dumps(u))
    log(f"universe={len(u)}")
    return 0


def _publish(drv: Drive, shards: dict, session: date, uni_n: int, fresh_n: int, extra: dict,
             sa_json: str, catalog_id: str, workdir: Path, mode: str) -> dict:
    manifest = {"session": session.isoformat(), "mode": mode, "built_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "schema": COLS, "adjustment": "split-adjusted, not dividend-adjusted (adj_close = Yahoo dividend-adjusted)",
                "n_shards": N_SHARDS, "universe": uni_n, "fresh": fresh_n,
                "fresh_frac": round(fresh_n / max(uni_n, 1), 4), "shards": {}, **extra}
    total_rows = 0
    for i in range(N_SHARDS):
        p = workdir / f"shard_{i:02d}.parquet"
        write_shard(shards[i], p)
        r = drv.upload(p.name, p)
        if r.get("md5Checksum") and r["md5Checksum"] != md5_file(p):
            raise SystemExit(f"readback md5 mismatch {p.name}")
        manifest["shards"][p.name] = {"rows": int(len(shards[i])), "tickers": int(shards[i]["ticker"].nunique()),
                                      "md5": md5_file(p), "bytes": p.stat().st_size}
        total_rows += len(shards[i])
    manifest["rows"] = int(total_rows)
    manifest["bytes"] = int(sum(v["bytes"] for v in manifest["shards"].values()))
    mp = workdir / "manifest.json"
    mp.write_text(json.dumps(manifest, indent=1))
    drv.upload("manifest.json", mp, mime="application/json")
    cat = per_ticker_catalog(shards.values())
    summary = {k: v for k, v in manifest.items() if k not in ("shards", "schema")}
    summary["tickers"] = len(cat)
    try:
        write_catalog(sa_json, catalog_id, summary, cat)
    except Exception as e:
        log(f"catalog write failed: {type(e).__name__}: {str(e)[:200]}")
    return manifest


def cmd_assemble(a) -> int:
    """Merge backfill parts, fill recent missing bars via Massive, compare, publish."""
    sa = os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"]
    uni = _load_universe(a)
    exp = expected_session()
    parts_dir = Path(a.parts_dir)
    statuses = [json.loads(p.read_text()) for p in parts_dir.glob("*/status.json")]
    incomplete = [s["part"] for s in statuses if not s.get("complete")]
    if len(statuses) < a.parts or incomplete:
        log(f"STATUS session={exp} writer=ohlcv_store mode=backfill parts_ok={len(statuses) - len(incomplete)}/{a.parts} "
            f"incomplete={incomplete} result=BLOCKED reason=parts_incomplete (rerun resumes)")
        return 3
    shards = {i: [] for i in range(N_SHARDS)}
    for p in sorted(parts_dir.glob("*/rows_*.parquet")):
        d = pd.read_parquet(p)
        d["ticker"] = d["ticker"].astype(str)
        for i, g in d.groupby(d["ticker"].map(shard_of)):
            shards[i].append(g)
    shards = {i: normalize(pd.concat(v, ignore_index=True)) if v else empty_frame() for i, v in shards.items()}
    known = set().union(*[set(s["ticker"]) for s in shards.values()])
    yahoo_n = len(known)
    # Fill last sessions via Massive only where Yahoo lacks the bar (max 3 sessions).
    m_added = 0
    recent = [d for d in sessions_between(exp - timedelta(days=7), exp)][-3:]
    cmp_res, flagged = [], 0
    for s in recent:
        res = massive_grouped(s)
        if not res:
            continue
        m = massive_results_to_rows(res, known, s)
        m = m[m["ticker"].isin(known)]
        if s == exp:
            cmp_res, flagged = sample_compare(s, m, seed=int(s.strftime("%Y%m%d")))
        for i in range(N_SHARDS):
            have = set(shards[i].loc[shards[i]["date"] == pd.Timestamp(s), "ticker"])
            add = m[(m["ticker"].map(shard_of) == i) & ~m["ticker"].isin(have)]
            if len(add):
                shards[i] = merge_rows(shards[i], add)
                m_added += len(add)
    fresh = sum(int((s["date"] == pd.Timestamp(exp)).groupby(s["ticker"]).any().sum()) for s in shards.values() if len(s))
    n_cmp = sum(1 for x in cmp_res if x["diff"] is not None)
    frac = fresh / max(len(uni), 1)
    block = frac < MIN_FRESH or (n_cmp and flagged / n_cmp > COMPARE_MAX_FLAG_FRAC)
    st = (f"STATUS session={exp} writer=ohlcv_store mode=backfill universe={len(uni)} yahoo_tickers={yahoo_n} "
          f"fresh={fresh}/{len(uni)}={frac:.4f} massive_fill={m_added} compare_flag={flagged}/{n_cmp}")
    if block:
        log(st + " result=BLOCKED")
        return 3
    work = Path(a.work_dir)
    work.mkdir(parents=True, exist_ok=True)
    drv = Drive(sa, a.folder_id)
    man = _publish(drv, shards, exp, len(uni), fresh, {"yahoo_tickers": yahoo_n, "massive_fill": m_added,
                   "compare_flag": f"{flagged}/{n_cmp}"}, sa, a.catalog_id, work, "backfill")
    log(st + f" rows={man['rows']} bytes={man['bytes']} result=WROTE")
    return 0


def cmd_daily(a) -> int:
    sa = os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"]
    exp = expected_session()
    work = Path(a.work_dir)
    work.mkdir(parents=True, exist_ok=True)
    drv = Drive(sa, a.folder_id)
    mp = drv.download("manifest.json", work / "manifest_prev.json")
    try:
        prev = json.loads(mp.read_text())
        last = date.fromisoformat(prev["session"])
    except Exception:
        log(f"STATUS session={exp} writer=ohlcv_store mode=daily result=BLOCKED reason=no_store_yet (run backfill)")
        return 3
    if last >= exp and not a.force:
        log(f"STATUS session={exp} writer=ohlcv_store mode=daily store_session={last} result=SKIP (already done)")
        return 0
    todo = sessions_between(last, exp)
    if len(todo) > 5:
        log(f"STATUS session={exp} writer=ohlcv_store mode=daily store_session={last} gap={len(todo)} result=BLOCKED reason=gap>5 (run backfill)")
        return 3
    if not todo:
        todo = [exp]
    for p in prev.get("shards", {}):
        drv.download(p, work / p)
        if prev["shards"][p].get("md5") and md5_file(work / p) != prev["shards"][p]["md5"]:
            raise SystemExit(f"shard md5 mismatch on download: {p}")
    shards = {i: read_shard(work / f"shard_{i:02d}.parquet") for i in range(N_SHARDS)}
    known = set().union(*[set(s["ticker"]) for s in shards.values()])
    uni = _load_universe(a)
    uni_set = set(uni)
    mstat, splits, cmp_note = 0, [], ""
    for s in todo:
        res = massive_grouped(s)
        if not res:
            log(f"massive unavailable for {s}; Yahoo fallback only")
            continue
        m = massive_results_to_rows(res, known | uni_set, s)
        m = m[m["ticker"].isin(known | uni_set)]
        if s == exp:
            cmp_res, flagged = sample_compare(s, m, seed=int(s.strftime("%Y%m%d")))
            n_cmp = sum(1 for x in cmp_res if x["diff"] is not None)
            cmp_note = f"{flagged}/{n_cmp}"
            if n_cmp and flagged / n_cmp > COMPARE_MAX_FLAG_FRAC:
                log(f"STATUS session={exp} writer=ohlcv_store mode=daily compare_flag={cmp_note} result=BLOCKED reason=format_mismatch")
                return 3
        prev_close = {}
        for sh in shards.values():
            if len(sh):
                before = sh[sh["date"] < pd.Timestamp(s)]
                prev_close.update(before.groupby("ticker", observed=True)["close"].last().to_dict())
        sp = split_suspects(prev_close, m)
        splits += sp
        for i in range(N_SHARDS):
            add = m[m["ticker"].map(shard_of) == i]
            shards[i] = merge_rows(shards[i], add)
        mstat += len(m)
    # Split suspects + new listings: full Yahoo history (period=max), merged.
    new_listings = sorted(uni_set - known)[: a.max_new]
    repull = sorted(set(splits))[: a.max_repull] + new_listings
    n_repull = 0
    for i in range(0, len(repull), 40):
        got, _ = yahoo_download(repull[i:i + 40], period="max")
        for t, rows in got.items():
            k = shard_of(t)
            mine = shards[k][(shards[k]["ticker"] == t) & (shards[k]["source"] == "massive")]
            mine = mine[~mine["date"].isin(set(rows["date"]))]
            shards[k] = replace_tickers(shards[k], merge_rows(rows, mine))
            n_repull += 1
        time.sleep(2)
    # Yahoo fallback for active tickers still missing the expected bar.
    active, missing = set(), []
    cutoff = pd.Timestamp(exp) - pd.Timedelta(days=21)
    for sh in shards.values():
        if len(sh):
            g = sh.groupby("ticker", observed=True)["date"].max()
            active |= set(g[g >= cutoff].index)
            missing += list(g[(g >= cutoff) & (g < pd.Timestamp(exp))].index)
    missing = sorted(set(missing) & (uni_set | known))[: a.max_fallback]
    n_fb = 0
    for i in range(0, len(missing), 100):
        got, lim = yahoo_download(missing[i:i + 100], period=None, start=(exp - timedelta(days=10)).isoformat())
        if lim:
            break
        for t, rows in got.items():
            k = shard_of(t)
            have = set(shards[k].loc[shards[k]["ticker"] == t, "date"])
            add = rows[~rows["date"].isin(have)]
            if len(add):
                shards[k] = merge_rows(shards[k], add)
                n_fb += 1
        time.sleep(1.5)
    fresh = sum(int((s["date"] == pd.Timestamp(exp)).groupby(s["ticker"]).any().sum()) for s in shards.values() if len(s))
    frac = fresh / max(len(uni), 1)
    st = (f"STATUS session={exp} writer=ohlcv_store mode=daily sessions={','.join(map(str, todo))} universe={len(uni)} "
          f"fresh={fresh}/{len(uni)}={frac:.4f} massive={mstat} yahoo_fallback={n_fb} split_repull={len(set(splits))} "
          f"new_listing={len(new_listings)} repulled={n_repull} compare_flag={cmp_note}")
    if frac < MIN_FRESH:
        log(st + " result=BLOCKED (old version kept)")
        return 3
    man = _publish(drv, shards, exp, len(uni), fresh, {"massive": mstat, "yahoo_fallback": n_fb,
                   "split_repull": sorted(set(splits))[:50], "compare_flag": cmp_note}, sa, a.catalog_id, work, "daily")
    log(st + f" rows={man['rows']} bytes={man['bytes']} result=WROTE")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["universe", "backfill-part", "assemble", "daily"])
    ap.add_argument("--out", default="universe.json")
    ap.add_argument("--universe-json", default="")
    ap.add_argument("--part", type=int, default=0)
    ap.add_argument("--parts", type=int, default=8)
    ap.add_argument("--batch", type=int, default=40)
    ap.add_argument("--pause", type=float, default=2.0)
    ap.add_argument("--budget-min", type=float, default=300)
    ap.add_argument("--out-dir", default="part")
    ap.add_argument("--parts-dir", default="parts")
    ap.add_argument("--work-dir", default="store_work")
    ap.add_argument("--folder-id", default=os.environ.get("OHLCV_STORE_FOLDER_ID", DEFAULT_FOLDER_ID))
    ap.add_argument("--catalog-id", default=os.environ.get("OHLCV_CATALOG_ID", DEFAULT_CATALOG_ID))
    ap.add_argument("--max-new", type=int, default=400)
    ap.add_argument("--max-repull", type=int, default=200)
    ap.add_argument("--max-fallback", type=int, default=1500)
    ap.add_argument("--force", action="store_true")
    a = ap.parse_args(argv)
    return {"universe": cmd_universe, "backfill-part": cmd_backfill_part,
            "assemble": cmd_assemble, "daily": cmd_daily}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
