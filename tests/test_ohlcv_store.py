"""Format-unification tests for the shared OHLCV store (no network)."""
from datetime import date

import pandas as pd
import pytest

import ohlcv_store as st
import store_shadow as sh


def test_ticker_mapping_yahoo_wins():
    assert st.to_yahoo("BRK.B") == "BRK-B"
    assert st.to_yahoo("brk-b") == "BRK-B"
    assert st.to_yahoo("VAL.WS") == "VAL-WT"
    assert st.massive_candidates("BRK.B") == ["BRK-B"]
    assert st.massive_candidates("SFpB") == ["SF-PB"]
    assert st.massive_candidates("PHXEp") == ["PHXE-P"]
    assert st.massive_candidates("VAL.WS")[0] == "VAL-WT"
    assert st.resolve_massive("NMZr", {"NMZ-R"}) == "NMZ-R"
    assert st.resolve_massive("QREDr", {"QREDR"}) == "QREDR"
    assert st.resolve_massive("AAPL", set()) == "AAPL"


def test_massive_ms_epoch_to_ny_session_date():
    # 2026-11-13 16:00 America/New_York == 21:00 UTC; Massive t is often 21:00Z or 05:00Z next? -> NY date
    assert st.ms_to_session(int(pd.Timestamp("2026-11-13T21:00:00Z").value // 10**6)) == date(2026, 11, 13)
    # Late-evening UTC that is still the same NY day
    assert st.ms_to_session(int(pd.Timestamp("2026-11-14T03:59:00Z").value // 10**6)) == date(2026, 11, 13)


def _yahoo_frame():
    idx = pd.DatetimeIndex(["2026-11-12", "2026-11-13"])
    return pd.DataFrame({"Open": [10.123456, 11.0], "High": [12, 12], "Low": [9, 10], "Close": [11.00004, 11.5],
                         "Adj Close": [10.9, 11.5], "Volume": [1000.4, 2000.6]}, index=idx)


def test_yahoo_rows_unified_schema_and_rounding():
    r = st.yahoo_frame_to_rows("BRK-B", _yahoo_frame())
    assert list(r.columns) == st.COLS
    assert r["open"].iloc[0] == 10.1235 and r["close"].iloc[0] == 11.0
    assert r["volume"].dtype == "int64" and r["volume"].tolist() == [1000, 2001]
    assert set(r["source"]) == {"yahoo"}
    assert [d.date() for d in r["date"]] == [date(2026, 11, 12), date(2026, 11, 13)]


def test_yahoo_tz_aware_index_uses_ny_date():
    f = _yahoo_frame()
    f.index = pd.DatetimeIndex(["2026-11-12 05:00", "2026-11-13 05:00"]).tz_localize("UTC")
    r = st.yahoo_frame_to_rows("X", f)
    assert [d.date() for d in r["date"]] == [date(2026, 11, 12), date(2026, 11, 13)]


def test_massive_rows_same_format_and_merge_consistent():
    t = int(pd.Timestamp("2026-11-14T02:00:00Z").value // 10**6)  # NY 11-13 21:00
    res = [{"T": "BRK.B", "o": 500.12346, "h": 505, "l": 499, "c": 503.5, "v": 123456.7, "t": t},
           {"T": "SFpB", "o": 20, "h": 20, "l": 20, "c": 20, "v": 10, "t": t},
           {"T": "BAD", "o": None, "h": 1, "l": 1, "c": 1, "v": 1, "t": t}]
    m = st.massive_results_to_rows(res, {"BRK-B"}, date(2026, 11, 13))
    assert list(m.columns) == st.COLS
    assert set(m["ticker"]) == {"BRK-B", "SF-PB"}
    row = m[m["ticker"] == "BRK-B"].iloc[0]
    assert row["open"] == 500.1235 and row["volume"] == 123457 and row["source"] == "massive"
    assert pd.isna(row["adj_close"])
    y = st.yahoo_frame_to_rows("BRK-B", _yahoo_frame().iloc[:1])
    merged = st.merge_rows(y, m)
    b = merged[merged["ticker"] == "BRK-B"]
    assert b["source"].tolist() == ["yahoo", "massive"]
    assert merged.dtypes.to_dict() == st.normalize(merged).dtypes.to_dict()


def test_massive_drops_bars_for_other_dates():
    t = int(pd.Timestamp("2026-11-12T21:00:00Z").value // 10**6)
    assert len(st.massive_results_to_rows([{"T": "A", "o": 1, "h": 1, "l": 1, "c": 1, "v": 1, "t": t}], set(), date(2026, 11, 13))) == 0


def test_new_source_wins_on_same_day():
    y = st.yahoo_frame_to_rows("A", _yahoo_frame())
    t = int(pd.Timestamp("2026-11-13T21:00:00Z").value // 10**6)
    m = st.massive_results_to_rows([{"T": "A", "o": 1, "h": 1, "l": 1, "c": 1, "v": 1, "t": t}], set(), date(2026, 11, 13))
    out = st.merge_rows(y, m)
    assert len(out) == 2 and out["source"].iloc[-1] == "massive"


def test_split_detection_and_full_replace():
    m = pd.DataFrame({"ticker": ["A", "B", "C"], "close": [25.0, 99.0, 210.0]})
    assert st.split_suspects({"A": 100.0, "B": 100.0, "C": 100.0}, m) == ["A", "C"]
    base = st.yahoo_frame_to_rows("A", _yahoo_frame())
    full = st.yahoo_frame_to_rows("A", _yahoo_frame() / 4)
    out = st.replace_tickers(base, full)
    assert out["close"].iloc[-1] == round(11.5 / 4, 4)


def test_compare_flags_over_half_percent():
    res = st.compare_closes({"AAPL": 100.0, "NVDA": 100.6, "QQQ": 10.0}, {"AAPL": 100.4, "NVDA": 100.0})
    flags = {r["ticker"]: r["flag"] for r in res}
    assert flags == {"AAPL": False, "NVDA": True, "QQQ": False}


def test_rate_limit_sleeps_and_retries_same_batch():
    import yfinance.shared as yf_shared

    calls, sleeps = [], []

    def dl(tickers, **kw):
        calls.append(list(tickers))
        if len(calls) == 1:
            yf_shared._ERRORS = {"A": "YFRateLimitError('Too Many Requests. Rate limited.')"}
            return pd.DataFrame()
        yf_shared._ERRORS = {}
        return pd.concat({"A": _yahoo_frame()}, axis=1)

    got, limited = st.yahoo_download(["A"], dl=dl, sleep=sleeps.append)
    assert not limited and "A" in got and calls == [["A"], ["A"]] and sleeps == [60]


def test_auto_adjust_false_requested():
    seen = {}

    def dl(tickers, **kw):
        seen.update(kw)
        return pd.DataFrame()

    st.yahoo_download(["A"], dl=dl, sleep=lambda s: None)
    assert seen["auto_adjust"] is False and seen["period"] == "max"


def test_shadow_yf_like_auto_adjust_matches_yfinance_semantics():
    r = st.yahoo_frame_to_rows("A", _yahoo_frame())
    f = sh.yf_like(r, auto_adjust=True)
    assert f["Close"].round(4).tolist() == [10.9, 11.5]
    assert round(f["Open"].iloc[0], 4) == round(10.1235 * 10.9 / 11.0, 4)
    g = sh.yf_like(r, auto_adjust=False)
    assert g["Close"].tolist() == [11.0, 11.5] and "Adj Close" in g


def test_part_split_is_complete_and_disjoint():
    u = [f"T{i}" for i in range(101)]
    parts = [st.part_tickers(u, k, 8) for k in range(8)]
    assert sorted(sum(parts, [])) == sorted(u)


def test_shard_assignment_stable():
    assert st.shard_of("AAPL") == st.shard_of("AAPL") and 0 <= st.shard_of("BRK-B") < st.N_SHARDS
