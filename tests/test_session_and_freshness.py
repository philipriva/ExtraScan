"""Unit tests for ExtraScan session dating, freshness gates, and QQscan file mapping."""
from __future__ import annotations

import json
import unittest
from datetime import date, datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pandas as pd

from run_extrascan import (
    freshness_ok,
    last_complete_us_session_date,
    session_bar_date,
)
from verify_meta import bar_distribution, write_verify_meta
import write_qqscan as qq
import write_pulse_events as pe


class TestSessionBarDate(unittest.TestCase):
    def test_utc_midnight_keeps_session_label(self):
        ts = pd.Timestamp("2026-11-05", tz="UTC")
        self.assertEqual(session_bar_date(ts), date(2026, 11, 5))
        self.assertEqual(pe.bar_date_str(ts), "2026-11-05")

    def test_naive_session_label(self):
        self.assertEqual(session_bar_date(pd.Timestamp("2026-11-05")), date(2026, 11, 5))

    def test_et_afternoon_same_day(self):
        ts = pd.Timestamp("2026-11-05 16:00:00", tz="America/New_York")
        self.assertEqual(session_bar_date(ts), date(2026, 11, 5))


class TestLastCompleteSession(unittest.TestCase):
    def test_after_close_weekday(self):
        self.assertEqual(
            last_complete_us_session_date(datetime(2026, 11, 5, 16, 0)),
            date(2026, 11, 5),
        )

    def test_before_close_weekday(self):
        self.assertEqual(
            last_complete_us_session_date(datetime(2026, 11, 5, 15, 59)),
            date(2026, 11, 4),
        )

    def test_weekend_rolls_to_friday(self):
        # Sunday 2026-11-08 → Friday 2026-11-06
        self.assertEqual(
            last_complete_us_session_date(datetime(2026, 11, 8, 12, 0)),
            date(2026, 11, 6),
        )

    def test_saturday(self):
        self.assertEqual(
            last_complete_us_session_date(datetime(2026, 11, 7, 10, 0)),
            date(2026, 11, 6),
        )


class TestFreshnessOk(unittest.TestCase):
    def test_fresh_pass(self):
        expected = date(2026, 11, 5)
        bars = [expected] * 9 + [date(2026, 11, 4)]
        ok, info = freshness_ok(bars, expected, min_frac=0.90)
        self.assertTrue(ok)
        self.assertEqual(info["fresh"], 9)

    def test_stale_fail(self):
        expected = date(2026, 11, 5)
        bars = [date(2026, 11, 4)] * 10
        ok, info = freshness_ok(bars, expected, min_frac=0.90)
        self.assertFalse(ok)
        self.assertEqual(info["frac"], 0.0)

    def test_empty_fail(self):
        ok, info = freshness_ok([], date(2026, 11, 5))
        self.assertFalse(ok)
        self.assertEqual(info["screened"], 0)


class TestQqscanFileMapping(unittest.TestCase):
    def test_session_to_file_date(self):
        # US session 11-05 → upstream file 20261106
        self.assertEqual(
            qq.expected_upstream_file_date(date(2026, 11, 5)),
            date(2026, 11, 6),
        )

    def test_reject_prior_batch_before_utc2(self):
        # Previously UTC hour<2 accepted yesterday's file; must refuse.
        need = qq.expected_upstream_file_date(date(2026, 11, 5))  # 2026-11-06
        with self.assertRaises(RuntimeError) as ctx:
            qq._assert_fresh(
                "Strong_Stocks_V3_20261105.txt",
                "Strong_Stocks",
                allow_stale=False,
                min_date=need,
            )
        self.assertIn("UPSTREAM_PENDING", str(ctx.exception))

    def test_accept_matching_file(self):
        need = date(2026, 11, 6)
        qq._assert_fresh(
            "Strong_Stocks_V3_20261106.txt",
            "Strong_Stocks",
            allow_stale=False,
            min_date=need,
        )


class TestQqscanAtomicWrite(unittest.TestCase):
    def test_stale_strong_writes_nothing(self):
        writes = []

        def fake_write(*args, **kwargs):
            writes.append(args)
            return 0

        with patch.object(qq, "last_complete_us_session_date", return_value=date(2026, 11, 5)), patch.object(
            qq, "_latest_txt", side_effect=[
                ("Strong_Stocks_V3_20261105.txt", "http://example/s"),
            ]
        ), patch.object(qq, "write_column", side_effect=fake_write), patch.dict(
            "os.environ", {"GOOGLE_SERVICE_ACCOUNT_JSON": "{}"}, clear=False
        ):
            # _latest_fresh will call _latest_txt once and fail assert
            rc = qq.main(["--wait-secs", "0"])
        self.assertEqual(rc, 3)
        self.assertEqual(writes, [])
        meta = json.loads(Path("out/qqscan_verify.json").read_text())
        self.assertEqual(meta["status"], "UPSTREAM_PENDING")
        self.assertFalse(meta["sheets"]["written"])

    def test_both_ready_then_write_both(self):
        writes = []

        def fake_write(sa, sheet_id, tab, col, symbols, header=None):
            writes.append(col)
            return len(symbols)

        def latest(owner_repo, prefix):
            if "my-stock" in owner_repo:
                return "Strong_Stocks_V3_20261106.txt", "http://example/s"
            return "Maggiestocks_20261106.txt", "http://example/m"

        with patch.object(qq, "last_complete_us_session_date", return_value=date(2026, 11, 5)), patch.object(
            qq, "_latest_txt", side_effect=latest
        ), patch.object(qq, "_fetch_symbols", side_effect=[["AAA"], ["BBB"]]), patch.object(
            qq, "write_column", side_effect=fake_write
        ), patch.dict("os.environ", {"GOOGLE_SERVICE_ACCOUNT_JSON": "{}"}, clear=False):
            rc = qq.main(["--wait-secs", "0"])
        self.assertEqual(rc, 0)
        self.assertEqual(writes, ["A", "J"])
        meta = json.loads(Path("out/qqscan_verify.json").read_text())
        self.assertEqual(meta["status"], "OK")
        self.assertTrue(meta["sheets"]["written"])
        self.assertEqual(meta["expected_upstream_file_date"], "2026-11-06")
        self.assertFalse(meta["sources"][0]["bar_dates_in_file"])


class TestPulseStaleSkipsSheets(unittest.TestCase):
    def test_stale_bars_skip_write(self):
        # Build minimal OHLC ending 2026-11-04 while target is 2026-11-05
        idx = pd.date_range("2026-10-01", periods=150, freq="D")
        # force last label to 2026-11-04
        idx = idx[:-1].append(pd.DatetimeIndex([pd.Timestamp("2026-11-04")]))
        df = pd.DataFrame(
            {
                "Open": 1.0,
                "High": 1.1,
                "Low": 0.9,
                "Close": 1.0,
                "Volume": 1_000_000,
            },
            index=idx,
        )

        univ = Path("out/_test_univ.csv")
        univ.parent.mkdir(parents=True, exist_ok=True)
        univ.write_text("ticker,layer\nAAA,test\n", encoding="utf-8")

        writes = []

        def fake_write_tab(*a, **k):
            writes.append(a)
            return None

        with patch.object(pe, "last_complete_us_session_date", return_value=date(2026, 11, 5)), patch.object(
            pe, "download", return_value={"AAA": df}
        ), patch.object(pe, "drop_forming", side_effect=lambda d: d), patch.object(
            pe, "screen_symbol", return_value=[]
        ), patch.object(pe, "ff_state_row", return_value=None), patch.object(
            pe, "write_tab", side_effect=fake_write_tab
        ), patch.object(pe, "freshness_ok", return_value=(False, {"screened": 1, "fresh": 0, "frac": 0.0, "expected": "2026-11-05", "unique_last_bars": ["2026-11-04"]})), patch.dict(
            "os.environ", {"GOOGLE_SERVICE_ACCOUNT_JSON": "{}"}, clear=False
        ):
            rc = pe.main.__wrapped__ if False else None
            # call via argparse
            import sys
            argv = [
                "write_pulse_events.py",
                "--universe",
                str(univ),
                "--out-csv",
                "out/pulse_events.csv",
                "--out-ff-csv",
                "out/pulse_ff_state.csv",
            ]
            with patch.object(sys, "argv", argv):
                # pe.main reads argv from parse_args internally with no argv arg
                # so patch sys.argv
                code = pe.main()
        self.assertEqual(code, 4)
        self.assertEqual(writes, [])
        meta = json.loads(Path("out/pulse_verify.json").read_text())
        self.assertEqual(meta["status"], "FAIL_STALE")
        self.assertFalse(meta["sheets"]["written"])


class TestVerifyMetaSchema(unittest.TestCase):
    def test_write_schema(self):
        path = write_verify_meta(
            "out/_schema_test.json",
            {
                "job": "unit",
                "status": "OK_NO_NEW_SIGNAL",
                "target_session": "2026-11-05",
                "input_bars": bar_distribution([date(2026, 11, 5)] * 3, date(2026, 11, 5)),
                "sheets": {"attempted": False, "written": False, "skipped_reason": "dry_run"},
            },
        )
        data = json.loads(Path(path).read_text())
        self.assertEqual(data["schema_version"], 1)
        self.assertEqual(data["status"], "OK_NO_NEW_SIGNAL")
        self.assertEqual(data["input_bars"]["min"], "2026-11-05")
        self.assertIn("checked_at_utc", data)


class TestFreshZeroEventsStillOk(unittest.TestCase):
    def test_status_no_new_signal_concept(self):
        # Documented contract: fresh bars + zero events => OK_NO_NEW_SIGNAL not FAIL_STALE
        expected = date(2026, 11, 5)
        ok, _ = freshness_ok([expected] * 5, expected)
        self.assertTrue(ok)
        events = []
        status = "OK_NO_NEW_SIGNAL" if ok and len(events) == 0 else ("OK" if ok else "FAIL_STALE")
        self.assertEqual(status, "OK_NO_NEW_SIGNAL")




class TestZhuliStaleSkipsSheets(unittest.TestCase):
    def test_stale_skips_write(self):
        import write_zhuli_lm as zl
        writes = []

        def fake_write(*a, **k):
            writes.append(a)
            return 0

        with patch.object(zl, "screen", return_value=(["AAA"], ["BBB"], {"L": 1, "M": 1, "bar_by_ticker": {}}, [date(2026, 11, 4)] * 10)), patch.object(
            zl, "last_complete_us_session_date", return_value=date(2026, 11, 5)
        ), patch.object(zl, "write_column", side_effect=fake_write), patch.object(
            zl, "refresh_qqscan_headers_preserving"
        ), patch.object(zl, "universe_us_ex_etf_fund", return_value=["AAA"]), patch.dict(
            "os.environ", {"GOOGLE_SERVICE_ACCOUNT_JSON": "{}"}, clear=False
        ):
            rc = zl.main([])
        self.assertEqual(rc, 4)
        self.assertEqual(writes, [])
        meta = json.loads(Path("out/zhuli_verify.json").read_text())
        self.assertEqual(meta["status"], "FAIL_STALE")


if __name__ == "__main__":
    unittest.main()
