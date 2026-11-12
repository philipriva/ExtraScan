"""Unit tests for QQscan C/D/E/F (no network, no Sheets)."""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import sheets_writer  # noqa: E402
import write_qqscan_cdef as m  # noqa: E402


def _frame(closes, start="2026-01-01"):
    idx = pd.bdate_range(start, periods=len(closes))
    c = pd.Series(closes, index=idx, dtype=float)
    return pd.DataFrame({"Open": c, "High": c + 1, "Low": c - 1, "Close": c, "Volume": 1e6})


# ---------- TD definition (Philip mapping) ----------
def test_td_counts_match_reference_definition():
    # 5 flat bars, then rising: close > close[4] → GREEN (B) count grows
    closes = [10, 10, 10, 10, 10, 11, 12, 13, 14, 15]
    red, green = m.td_setup_counts(pd.Series(closes, dtype=float))
    assert list(green)[-5:] == [1, 2, 3, 4, 5]
    assert int(red.iloc[-1]) == 0


def test_td_buy1_is_green_count_one():
    closes = [10, 10, 10, 10, 10, 11]  # first close > close[4]
    assert m.td_buy1(pd.Series(closes, dtype=float))
    assert not m.td_buy1(pd.Series(closes + [12], dtype=float))  # green=2


def test_td_sell9_is_red_count_exactly_nine():
    base = [100.0] * 5
    falling = [99 - i for i in range(9)]
    s = pd.Series(base + falling, dtype=float)
    red, _ = m.td_setup_counts(s)
    assert int(red.iloc[-1]) == 9
    assert m.td_sell9(s)
    assert not m.td_sell9(pd.Series(base + falling + [80], dtype=float))  # red=10
    assert not m.td_sell9(pd.Series(base + falling[:-1], dtype=float))  # red=8


def test_td_matches_us_exclude_reference_impl():
    """Same counts as Philip's us-exclude-screener td_setup_counts (copied verbatim)."""
    def ref(close):
        c = close.astype(float)
        b = pd.Series(0, index=c.index, dtype=int)
        s = pd.Series(0, index=c.index, dtype=int)
        bc = sc = 0
        v = c.tolist()
        for i in range(len(v)):
            if i < 4 or pd.isna(v[i]) or pd.isna(v[i - 4]):
                bc = sc = 0
            else:
                bc = bc + 1 if v[i] < v[i - 4] else 0
                sc = sc + 1 if v[i] > v[i - 4] else 0
            b.iloc[i] = bc
            s.iloc[i] = sc
        return b, s

    rng = np.random.default_rng(7)
    s = pd.Series(100 + rng.standard_normal(300).cumsum())
    a1, a2 = m.td_setup_counts(s)
    r1, r2 = ref(s)
    assert a1.equals(r1) and a2.equals(r2)


# ---------- zhuli reuse ----------
def test_zhuli_cross_reuses_existing_function():
    import write_zhuli_lm

    rng = np.random.default_rng(1)
    for seed in range(20):
        closes = 50 + np.random.default_rng(seed).standard_normal(120).cumsum()
        d = _frame(closes)
        assert m.zhuli_daily_cross_up(d) == write_zhuli_lm.zhuli_cross_up(d)


def test_zhuli_too_short_is_false():
    assert not m.zhuli_daily_cross_up(_frame([10.0] * 20))


# ---------- freshness gate ----------
def test_evaluate_full_denominator_and_stale_excluded():
    exp = date(2026, 11, 9)
    fresh = _frame([10, 10, 10, 10, 10, 11], start="2026-11-02")  # ends 11-09
    assert fresh.index[-1].date() == exp
    stale = _frame([10, 10, 10, 10, 10, 11], start="2026-10-30")  # ends 11-06
    frames = {"AAA": fresh, "BBB": stale}
    ev = m.evaluate(frames, ["AAA", "BBB", "CCC"], exp, {"D": lambda d: m.td_buy1(d["Close"])})
    assert ev["hits"]["D"] == ["AAA"]  # stale BBB never selected
    assert ev["fresh"] == 1 and ev["universe"] == 3
    assert ev["frac"] == pytest.approx(1 / 3, abs=1e-4)
    assert not ev["fresh_ok"]
    assert ev["missing"] == ["CCC"] and ev["stale"] == ["BBB"]


def test_evaluate_asof_truncates_future_bars():
    d = _frame([10, 10, 10, 10, 10, 11, 12], start="2026-11-02")  # ends 11-10
    ev = m.evaluate({"X": d}, ["X"], date(2026, 11, 9), {"D": lambda f: m.td_buy1(f["Close"])}, asof=date(2026, 11, 9))
    assert ev["fresh_ok"] and ev["hits"]["D"] == ["X"]


# ---------- column assembly / isolation ----------
def test_column_with_leaders_like_g():
    out = m.column_with_leaders(["XLK", "SMH"], {"XLK": ["NVDA", "AAPL"], "SMH": ["NVDA"]})
    assert out == ["AAPL", "NVDA", "SMH", "XLK"]


def test_only_cdef_allowed():
    assert m.parse_cols("C,d, E,F") == ["C", "D", "E", "F"]
    for bad in ("A", "G", "H", "J", "L", "M", "B"):
        with pytest.raises(ValueError):
            m.parse_cols(bad)


def test_existing_headers_untouched():
    # Existing writers iterate QQSCAN_HEADERS; C–F must NOT be added there.
    assert set(sheets_writer.QQSCAN_HEADERS) == {"A", "G", "H", "J", "L", "M"}
    assert set(m.QQSCAN_CDEF_HEADERS) == {"C", "D", "E", "F"}


def test_asof_requires_dry_run():
    assert m.main(["--asof", "2026-11-09", "--columns", "C"]) == 2


def test_write_path_only_touches_cdef(monkeypatch):
    calls = []
    monkeypatch.setenv("GOOGLE_SERVICE_ACCOUNT_JSON", "{}")
    monkeypatch.setattr(sheets_writer, "write_column", lambda sa, sid, tab, col, vals, header=None: calls.append((tab, col)) or len(vals))
    exp = date(2026, 11, 9)
    fresh = _frame([10, 10, 10, 10, 10, 11], start="2026-11-02")
    monkeypatch.setattr(m, "last_complete_us_session_date", lambda: exp)
    monkeypatch.setattr(m, "load_etf_list", lambda p: ["AAA"])
    monkeypatch.setattr(m, "download", lambda syms: ({s: fresh for s in syms}, []))
    rc = m.main(["--columns", "C,D,F", "--skip-holdings"])
    assert rc == 0
    assert sorted(calls) == [("QQscan", "C"), ("QQscan", "D"), ("QQscan", "F")]


def test_stale_blocks_write(monkeypatch):
    calls = []
    monkeypatch.setenv("GOOGLE_SERVICE_ACCOUNT_JSON", "{}")
    monkeypatch.setattr(sheets_writer, "write_column", lambda *a, **k: calls.append(a) or 0)
    stale = _frame([10, 10, 10, 10, 10, 11], start="2026-10-30")
    monkeypatch.setattr(m, "last_complete_us_session_date", lambda: date(2026, 11, 9))
    monkeypatch.setattr(m, "parse_symbols", lambda t: ["AAA", "BBB"])
    monkeypatch.setattr(m, "download", lambda syms: ({s: stale for s in syms}, []))
    rc = m.main(["--columns", "E", "--symbols", "AAA,BBB"])
    assert rc == 3 and calls == []


# ---------- Column E filters (Philip 2026-11-10) ----------
def _rising(n=260, start=10.0, step=0.1, end="2026-11-09"):
    idx = pd.bdate_range(end=end, periods=n)
    return _frame([start + i * step for i in range(n)], start=idx[0])


def test_e_price_filter_pass_and_reasons():
    assert m.e_price_filter_reason(_rising(start=10)) is None
    assert m.e_price_filter_reason(_rising(start=1, step=0.001)) == "close_le_5"
    # long rise then sharp 3-bar drop: close < EMA9 but still > EMA200
    up = [10 + i * 0.1 for i in range(257)] + [30, 29, 28]
    assert m.e_price_filter_reason(_frame(up, start="2025-10-01")) == "close_le_ema9"
    assert m.e_price_filter_reason(_rising(n=150)) == "bars_lt_200_for_ema200"
    # long fall then a 5-bar bounce: > EMA9 but < EMA200
    down = [60 - i * 0.15 for i in range(255)] + [22, 23, 24, 25, 26]
    assert m.e_price_filter_reason(_frame(down, start="2025-10-01")) == "close_le_ema200"


def test_e_no_volume_filter():
    d = _rising(start=10)
    d["Volume"] = 1.0  # tiny volume must not matter
    assert m.e_price_filter_reason(d) is None


def test_apply_e_filters_mcap_missing_excluded_and_counted():
    good = _rising(start=10)
    frames = {s: good for s in ("BIG", "SMALL", "NOCAP", "EDGE")}
    frames["CHEAP"] = _rising(start=1, step=0.001)
    caps = {"BIG": 5e9, "SMALL": 5e7, "NOCAP": None, "EDGE": 100_000_000.0}
    kept, st = m.apply_e_filters(frames, ["BIG", "SMALL", "NOCAP", "EDGE", "CHEAP"], None, mcap_fn=caps.get)
    assert kept == ["BIG"]
    dc = st["dropped_counts"]
    assert dc["mcap_missing"] == 1 and st["dropped"]["mcap_missing"] == ["NOCAP"]
    assert dc["mcap_le_100m"] == 2  # SMALL and exactly-100M EDGE (strict >)
    assert dc["close_le_5"] == 1
    assert st["cross_hits"] == 5 and st["after_price_ema"] == 4 and st["kept"] == 1


def test_e_mcap_only_queried_for_price_survivors():
    asked = []
    frames = {"A": _rising(start=10), "B": _rising(start=1, step=0.001)}
    m.apply_e_filters(frames, ["A", "B"], None, mcap_fn=lambda s: asked.append(s) or 1e9)
    assert asked == ["A"]


def test_e_write_path_applies_filters(monkeypatch):
    calls = []
    monkeypatch.setenv("GOOGLE_SERVICE_ACCOUNT_JSON", "{}")
    monkeypatch.setattr(sheets_writer, "write_column", lambda sa, sid, tab, col, vals, header=None: calls.append((tab, col, list(vals))) or len(vals))
    exp = date(2026, 11, 9)
    monkeypatch.setattr(m, "last_complete_us_session_date", lambda: exp)
    monkeypatch.setattr(m, "download", lambda syms: ({s: _rising() for s in syms}, []))
    monkeypatch.setattr(m, "zhuli_daily_cross_up", lambda d: True)
    monkeypatch.setattr(m, "fetch_mcap", lambda s: {"AAA": 2e9}.get(s))
    rc = m.main(["--columns", "E", "--symbols", "AAA,BBB"])
    assert rc == 0
    assert calls == [("QQscan", "E", ["AAA"])]


# ---------- backup skip (schedule) ----------
def test_session_close_utc_dst_and_standard():
    assert m.session_close_utc(date(2026, 11, 9)).isoformat() == "2026-11-09T21:00:00+00:00"  # EST
    assert m.session_close_utc(date(2026, 10, 30)).isoformat() == "2026-10-30T20:00:00+00:00"  # EDT


def _run(i, ev="schedule", concl="success", ts="2026-11-09T22:05:00Z"):
    return {"id": i, "event": ev, "conclusion": concl, "created_at": ts}


def test_primary_already_wrote_rules():
    exp = date(2026, 11, 9)
    assert m.primary_already_wrote([_run(1)], exp)["id"] == 1
    assert m.primary_already_wrote([_run(1, ev="workflow_run")], exp)["id"] == 1  # Daily ExtraScan hook is a primary
    assert m.primary_already_wrote([_run(1, ev="workflow_run", concl="failure")], exp) is None
    assert m.primary_already_wrote([_run(1, concl="failure")], exp) is None  # blocked/stale primary
    assert m.primary_already_wrote([_run(1, ev="workflow_dispatch")], exp) is None  # may be dry-run
    assert m.primary_already_wrote([_run(1, ts="2026-11-06T22:05:00Z")], exp) is None  # older session
    assert m.primary_already_wrote([_run(7)], exp, current_run_id=7) is None  # itself


def test_backup_skips_without_write(monkeypatch):
    calls = []
    monkeypatch.setenv("GOOGLE_SERVICE_ACCOUNT_JSON", "{}")
    monkeypatch.setattr(sheets_writer, "write_column", lambda *a, **k: calls.append(a) or 0)
    monkeypatch.setattr(m, "last_complete_us_session_date", lambda: date(2026, 11, 9))
    monkeypatch.setattr(m, "_fetch_workflow_runs", lambda: [_run(1)])
    monkeypatch.setattr(m, "download", lambda syms: (_ for _ in ()).throw(AssertionError("no download")))
    assert m.main(["--skip-if-primary-done"]) == 0 and calls == []


def test_backup_skips_when_workflow_run_primary_wrote(monkeypatch):
    calls = []
    monkeypatch.setenv("GOOGLE_SERVICE_ACCOUNT_JSON", "{}")
    monkeypatch.setattr(sheets_writer, "write_column", lambda *a, **k: calls.append(a) or 0)
    monkeypatch.setattr(m, "last_complete_us_session_date", lambda: date(2026, 11, 9))
    monkeypatch.setattr(m, "_fetch_workflow_runs", lambda: [_run(9, ev="workflow_run")])
    monkeypatch.setattr(m, "download", lambda syms: (_ for _ in ()).throw(AssertionError("no download")))
    assert m.main(["--skip-if-primary-done"]) == 0 and calls == []


def test_backup_runs_when_primary_missing(monkeypatch):
    calls = []
    monkeypatch.setenv("GOOGLE_SERVICE_ACCOUNT_JSON", "{}")
    monkeypatch.setattr(sheets_writer, "write_column", lambda sa, sid, tab, col, vals, header=None: calls.append(col) or len(vals))
    exp = date(2026, 11, 9)
    fresh = _frame([10, 10, 10, 10, 10, 11], start="2026-11-02")
    monkeypatch.setattr(m, "last_complete_us_session_date", lambda: exp)
    monkeypatch.setattr(m, "_fetch_workflow_runs", lambda: [_run(1, concl="failure")])
    monkeypatch.setattr(m, "load_etf_list", lambda p: ["AAA"])
    monkeypatch.setattr(m, "download", lambda syms: ({s: fresh for s in syms}, []))
    assert m.main(["--skip-if-primary-done", "--columns", "C,D,F", "--skip-holdings"]) == 0
    assert sorted(calls) == ["C", "D", "F"]


def test_backup_api_error_runs_normally_but_stale_still_blocks(monkeypatch):
    calls = []
    monkeypatch.setenv("GOOGLE_SERVICE_ACCOUNT_JSON", "{}")
    monkeypatch.setattr(sheets_writer, "write_column", lambda *a, **k: calls.append(a) or 0)
    monkeypatch.setattr(m, "last_complete_us_session_date", lambda: date(2026, 11, 9))
    monkeypatch.setattr(m, "_fetch_workflow_runs", lambda: (_ for _ in ()).throw(RuntimeError("api down")))
    stale = _frame([10, 10, 10, 10, 10, 11], start="2026-10-30")
    monkeypatch.setattr(m, "parse_symbols", lambda t: ["AAA"])
    monkeypatch.setattr(m, "download", lambda syms: ({s: stale for s in syms}, []))
    assert m.main(["--skip-if-primary-done", "--columns", "E", "--symbols", "AAA"]) == 3 and calls == []
