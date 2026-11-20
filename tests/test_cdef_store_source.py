"""Store data source for QQscan CDEF (no network, no Sheets, no Drive)."""
from __future__ import annotations

import json
import sys
from datetime import date, datetime
from pathlib import Path

import pandas as pd
import pytest

pytest.importorskip("pyarrow")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import cdef_store_source as css  # noqa: E402
import ohlcv_store as st  # noqa: E402
import sheets_writer  # noqa: E402
import write_qqscan_cdef as m  # noqa: E402

EXP = date(2026, 11, 18)
NOW = datetime(2026, 11, 20)


def _rows(ticker, closes, end=EXP, adj=None, source="yahoo"):
    idx = pd.bdate_range(end=pd.Timestamp(end), periods=len(closes))
    c = pd.Series(closes, dtype=float)
    return pd.DataFrame({
        "ticker": ticker, "date": idx, "open": c.values, "high": (c + 1).values, "low": (c - 1).values,
        "close": c.values, "adj_close": (adj if adj is not None else c.values), "volume": 1000, "source": source,
    })


def _make_store(tmp_path, frames, session=EXP, corrupt=None):
    shards = {i: [] for i in range(st.N_SHARDS)}
    for f in frames:
        shards[st.shard_of(str(f["ticker"].iloc[0]))].append(f)
    man = {"session": session.isoformat(), "n_shards": st.N_SHARDS, "shards": {}}
    for i in range(st.N_SHARDS):
        df = st.normalize(pd.concat(shards[i])) if shards[i] else st.empty_frame()
        p = tmp_path / f"shard_{i:02d}.parquet"
        df.to_parquet(p, index=False)
        man["shards"][p.name] = {"rows": len(df), "md5": st.md5_file(p)}
    if corrupt:
        (tmp_path / corrupt).write_bytes(b"x" * 100)
    (tmp_path / "manifest.json").write_text(json.dumps(man))
    return tmp_path


def _no_yahoo(*a, **k):
    raise AssertionError("Yahoo must not be called for tickers present in the store")


# ---------- manifest / fail-closed ----------
def test_manifest_behind_target_is_not_ready(tmp_path):
    d = _make_store(tmp_path, [_rows("AAA", [1] * 10, end=date(2026, 11, 17))], session=date(2026, 11, 17))
    with pytest.raises(css.StoreNotReady, match="< target"):
        css.check_manifest(d, EXP)


def test_manifest_md5_mismatch_is_not_ready(tmp_path):
    d = _make_store(tmp_path, [_rows("AAA", [1] * 10)], corrupt="shard_03.parquet")
    with pytest.raises(css.StoreNotReady, match="md5"):
        css.check_manifest(d, EXP)


def test_manifest_missing_is_not_ready(tmp_path):
    with pytest.raises(css.StoreNotReady):
        css.check_manifest(tmp_path, EXP)


def test_manifest_ahead_of_target_ok(tmp_path):
    d = _make_store(tmp_path, [_rows("AAA", [1] * 10, end=date(2026, 11, 19))], session=date(2026, 11, 19))
    assert css.check_manifest(d, EXP)["session"] == "2026-11-19"


# ---------- frame conversion ----------
def test_rows_to_frame_auto_adjusts_like_yfinance():
    # adj/close = 0.5 on old bars, Massive tail (NaN adj) -> ratio 1 (bfill none -> fillna 1)
    r = _rows("AAA", [10, 10, 10, 10], adj=[5, 5, 10, float("nan")])
    f = css.rows_to_frame(r)
    assert list(f["Close"]) == [5, 5, 10, 10]
    assert list(f["High"]) == [5.5, 5.5, 11, 11]
    assert list(f.columns) == ["Open", "High", "Low", "Close", "Volume"]


def test_lookback_window_matches_yahoo_path(tmp_path):
    d = _make_store(tmp_path, [_rows("AAA", list(range(1, 2001)))])
    frames, stats = css.load_frames(["AAA"], EXP, d, lookback_days=1500, now=NOW, yahoo_fill=_no_yahoo)
    assert frames["AAA"].index.min() >= pd.Timestamp("2022-10-11")
    assert stats.store == 1 and stats.store_absent == 0


def test_symbol_mapping_dot_to_dash(tmp_path):
    d = _make_store(tmp_path, [_rows("BRK-B", [1] * 10)])
    frames, _ = css.load_frames(["BRK.B"], EXP, d, now=NOW, yahoo_fill=_no_yahoo)
    assert "BRK.B" in frames


def test_absent_tickers_filled_from_yahoo_and_counted(tmp_path):
    d = _make_store(tmp_path, [_rows("AAA", [1] * 10)])
    seen = []

    class S:
        yahoo, retry, stooq, massive = 1, 0, 0, 0

    def fill(syms, exp):
        seen.append(list(syms))
        return {"BBB": css.rows_to_frame(_rows("BBB", [2] * 10))}, S()

    frames, stats = css.load_frames(["AAA", "BBB", "CCC"], EXP, d, now=NOW, yahoo_fill=fill)
    assert seen == [["BBB", "CCC"]]
    assert set(frames) == {"AAA", "BBB"} and stats.absent == ["BBB", "CCC"] and stats.yahoo == 1


def test_yahoo_fill_failure_leaves_absent_not_fresh(tmp_path):
    d = _make_store(tmp_path, [_rows("AAA", [1] * 10)])

    def boom(syms, exp):
        raise RuntimeError("rate limited")

    frames, stats = css.load_frames(["AAA", "BBB"], EXP, d, now=NOW, yahoo_fill=boom)
    assert set(frames) == {"AAA"}
    ev = m.evaluate(frames, ["AAA", "BBB"], EXP, {"x": lambda df: True})
    assert ev["fresh"] == 1 and ev["universe"] == 2 and ev["missing"] == ["BBB"]


# ---------- writer integration ----------
def _patch_common(monkeypatch, calls):
    monkeypatch.setenv("GOOGLE_SERVICE_ACCOUNT_JSON", "{}")
    monkeypatch.setattr(sheets_writer, "write_column", lambda sa, sid, tab, col, vals, header=None: calls.append(col) or len(vals))
    monkeypatch.setattr(m, "last_complete_us_session_date", lambda: EXP)
    monkeypatch.setattr(m, "load_etf_list", lambda p: ["AAA", "BBB"])
    monkeypatch.setattr(m, "download_daily", _no_yahoo)


def test_store_mode_not_ready_blocks_and_writes_nothing(tmp_path, monkeypatch):
    calls = []
    _patch_common(monkeypatch, calls)
    d = _make_store(tmp_path, [_rows("AAA", [1] * 10, end=date(2026, 11, 17))], session=date(2026, 11, 17))
    out = tmp_path / "r.json"
    rc = m.main(["--columns", "C,D,F", "--skip-holdings", "--data-source", "store", "--store-dir", str(d), "--out-json", str(out)])
    assert rc == 3 and calls == []
    assert json.loads(out.read_text())["blocked"] == ["C", "D", "F"]


def test_store_mode_same_signals_as_yahoo_mode(tmp_path, monkeypatch):
    """Identical bars via store vs via download_daily -> identical columns and fresh."""
    closes_a = [10, 10, 10, 10, 10, 11]          # B1 on last bar
    closes_b = [100.0] * 5 + [99 - i for i in range(9)]  # S9 on last bar
    ra, rb = _rows("AAA", closes_a), _rows("BBB", closes_b)
    d = _make_store(tmp_path, [ra, rb])

    calls = []
    _patch_common(monkeypatch, calls)
    out_s = tmp_path / "s.json"
    rc = m.main(["--columns", "C,D,F", "--skip-holdings", "--dry-run", "--data-source", "store",
                 "--store-dir", str(d), "--out-json", str(out_s)])
    assert rc == 0 and calls == []

    frames = {"AAA": css.rows_to_frame(ra), "BBB": css.rows_to_frame(rb)}

    class S:
        yahoo = retry = stooq = massive = 0

    monkeypatch.setattr(m, "download_daily", lambda syms, exp=None, **k: ({s: frames[s] for s in syms}, S()))
    out_y = tmp_path / "y.json"
    assert m.main(["--columns", "C,D,F", "--skip-holdings", "--dry-run", "--out-json", str(out_y)]) == 0
    s, y = json.loads(out_s.read_text()), json.loads(out_y.read_text())
    assert s["columns"] == y["columns"]
    assert s["columns"]["C"]["signal_etfs"] == ["BBB"] and s["columns"]["D"]["signal_etfs"] == ["AAA"]
    assert s["groups"]["ETF"]["frac"] == y["groups"]["ETF"]["frac"] == 1.0
    assert s["data_source"] == "store" and "data_source" not in y  # yahoo JSON unchanged


def test_store_mode_stale_store_ticker_counts_not_fresh(tmp_path, monkeypatch):
    calls = []
    _patch_common(monkeypatch, calls)
    d = _make_store(tmp_path, [_rows("AAA", [1] * 10), _rows("BBB", [1] * 10, end=date(2026, 11, 17))])
    out = tmp_path / "r.json"
    rc = m.main(["--columns", "F", "--skip-holdings", "--data-source", "store", "--store-dir", str(d), "--out-json", str(out)])
    # 1/2 fresh < .90 -> blocked, nothing written
    assert rc == 3 and calls == []
    assert json.loads(out.read_text())["groups"]["ETF"]["frac"] == 0.5


def test_store_mode_without_sa_and_without_dir_blocks(monkeypatch, tmp_path):
    calls = []
    _patch_common(monkeypatch, calls)
    monkeypatch.delenv("GOOGLE_SERVICE_ACCOUNT_JSON")
    monkeypatch.chdir(tmp_path)
    assert m.main(["--columns", "C", "--dry-run", "--data-source", "store"]) == 3 and calls == []


def test_yahoo_default_never_imports_store_layer(monkeypatch):
    calls = []
    _patch_common(monkeypatch, calls)

    class S:
        yahoo = retry = stooq = massive = 0

    fr = css.rows_to_frame(_rows("AAA", [1] * 10))
    monkeypatch.setattr(m, "download_daily", lambda syms, exp=None, **k: ({s: fr for s in syms}, S()))
    monkeypatch.setattr(m, "_store_fetcher", lambda *a, **k: (_ for _ in ()).throw(AssertionError("store used")))
    assert m.main(["--columns", "F", "--skip-holdings", "--dry-run"]) == 0
