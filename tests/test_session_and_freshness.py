"""Unit tests for ExtraScan session dating, freshness gates, and QQscan file mapping."""
from __future__ import annotations

import json
import sys
import unittest
from datetime import date, datetime
from pathlib import Path
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

import pandas as pd

from run_extrascan import (
    freshness_ok,
    last_complete_us_session_date,
    session_bar_date,
)
from us_session import (
    CalendarUnavailableError,
    drop_forming_bar,
    is_early_close,
    is_trading_session,
    session_close_et,
    xnys_calendar,
)
from verify_meta import bar_distribution, write_verify_meta
from sheets_writer import readback_receipt, write_column
import write_qqscan as qq
import write_pulse_events as pe
import write_zhuli_lm as zl

NY = ZoneInfo("America/New_York")


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

    def test_utc_evening_not_converted_back(self):
        # 2026-11-05 05:00 UTC == 2026-11-05 00:00 ET; still session label 11-05
        # if Yahoo labeled as UTC midnight of session day.
        ts = pd.Timestamp("2026-11-05 00:00:00+00:00")
        self.assertEqual(session_bar_date(ts), date(2026, 11, 5))


class TestXnysCalendar(unittest.TestCase):
    def test_calendar_loads(self):
        cal = xnys_calendar()
        self.assertTrue(cal.is_session(pd.Timestamp("2026-11-05")))

    def test_thanksgiving_not_session(self):
        # 2026-11-26 Thanksgiving
        self.assertFalse(is_trading_session(date(2026, 11, 26)))

    def test_day_after_thanksgiving_early_close(self):
        d = date(2026, 11, 27)
        self.assertTrue(is_trading_session(d))
        self.assertTrue(is_early_close(d))
        close = session_close_et(d)
        self.assertEqual((close.hour, close.minute), (13, 0))

    def test_regular_close_1600(self):
        d = date(2026, 11, 5)
        close = session_close_et(d)
        self.assertEqual((close.hour, close.minute), (16, 0))

    def test_christmas_not_session(self):
        self.assertFalse(is_trading_session(date(2026, 12, 25)))


class TestLastCompleteSession(unittest.TestCase):
    def test_after_close_weekday(self):
        self.assertEqual(
            last_complete_us_session_date(datetime(2026, 11, 5, 16, 0, tzinfo=NY)),
            date(2026, 11, 5),
        )

    def test_before_close_weekday(self):
        self.assertEqual(
            last_complete_us_session_date(datetime(2026, 11, 5, 15, 59, tzinfo=NY)),
            date(2026, 11, 4),
        )

    def test_weekend_rolls_to_friday(self):
        self.assertEqual(
            last_complete_us_session_date(datetime(2026, 11, 8, 12, 0, tzinfo=NY)),
            date(2026, 11, 6),
        )

    def test_saturday(self):
        self.assertEqual(
            last_complete_us_session_date(datetime(2026, 11, 7, 10, 0, tzinfo=NY)),
            date(2026, 11, 6),
        )

    def test_thanksgiving_day_uses_prior_session(self):
        # Thanksgiving 2026-11-26 — last complete is Wed 2026-11-25
        self.assertEqual(
            last_complete_us_session_date(datetime(2026, 11, 26, 12, 0, tzinfo=NY)),
            date(2026, 11, 25),
        )

    def test_early_close_complete_after_1300(self):
        # 2026-11-27 early close 13:00 — at 13:00 session is complete
        self.assertEqual(
            last_complete_us_session_date(datetime(2026, 11, 27, 13, 0, tzinfo=NY)),
            date(2026, 11, 27),
        )

    def test_early_close_incomplete_before_1300(self):
        self.assertEqual(
            last_complete_us_session_date(datetime(2026, 11, 27, 12, 59, tzinfo=NY)),
            date(2026, 11, 25),  # prior session (skip Thanksgiving 26)
        )

    def test_naive_datetime_treated_as_et(self):
        self.assertEqual(
            last_complete_us_session_date(datetime(2026, 11, 5, 16, 0)),
            date(2026, 11, 5),
        )


class TestDropForming(unittest.TestCase):
    def _df(self, last: str) -> pd.DataFrame:
        idx = pd.DatetimeIndex([pd.Timestamp("2026-11-03"), pd.Timestamp(last)])
        return pd.DataFrame(
            {"Open": 1, "High": 1, "Low": 1, "Close": 1, "Volume": 1}, index=idx
        )

    def test_drops_before_regular_close(self):
        d = self._df("2026-11-05")
        out = drop_forming_bar(d, now=datetime(2026, 11, 5, 15, 0, tzinfo=NY))
        self.assertEqual(session_bar_date(out.index[-1]), date(2026, 11, 3))

    def test_keeps_after_regular_close(self):
        d = self._df("2026-11-05")
        out = drop_forming_bar(d, now=datetime(2026, 11, 5, 16, 0, tzinfo=NY))
        self.assertEqual(session_bar_date(out.index[-1]), date(2026, 11, 5))

    def test_early_close_drops_before_1300(self):
        d = self._df("2026-11-27")
        out = drop_forming_bar(d, now=datetime(2026, 11, 27, 12, 0, tzinfo=NY))
        self.assertEqual(session_bar_date(out.index[-1]), date(2026, 11, 3))

    def test_early_close_keeps_after_1300(self):
        d = self._df("2026-11-27")
        out = drop_forming_bar(d, now=datetime(2026, 11, 27, 13, 0, tzinfo=NY))
        self.assertEqual(session_bar_date(out.index[-1]), date(2026, 11, 27))


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

    def test_missing_inflates_denom_not_pass(self):
        # 9/9 screened fresh would pass without missing; 9/(9+2)=0.818 fails 0.90
        expected = date(2026, 11, 5)
        bars = [expected] * 9
        ok_no_miss, _ = freshness_ok(bars, expected, missing_count=0)
        ok_miss, info = freshness_ok(bars, expected, missing_count=2)
        self.assertTrue(ok_no_miss)
        self.assertFalse(ok_miss)
        self.assertEqual(info["denom"], 11)
        self.assertEqual(info["missing_count"], 2)

    def test_future_bars_not_fresh(self):
        expected = date(2026, 11, 5)
        bars = [date(2026, 11, 6)] * 10
        ok, info = freshness_ok(bars, expected)
        self.assertFalse(ok)
        self.assertEqual(info["future_count"], 10)

    def test_intraday_partial_universe_fail_closed(self):
        expected = date(2026, 11, 5)
        bars = [expected] * 50  # only half downloaded
        ok, info = freshness_ok(bars, expected, missing_count=50)
        self.assertFalse(ok)
        self.assertEqual(info["frac"], 0.5)


class TestQqscanFileMapping(unittest.TestCase):
    def test_session_to_file_date(self):
        self.assertEqual(
            qq.expected_upstream_file_date(date(2026, 11, 5)),
            date(2026, 11, 6),
        )

    def test_reject_prior_batch_before_utc2(self):
        need = qq.expected_upstream_file_date(date(2026, 11, 5))
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

        with patch.object(
            qq, "last_complete_us_session_date", return_value=date(2026, 11, 5)
        ), patch.object(
            qq,
            "_latest_txt",
            side_effect=[("Strong_Stocks_V3_20261105.txt", "http://example/s")],
        ), patch.object(qq, "write_column", side_effect=fake_write), patch.dict(
            "os.environ", {"GOOGLE_SERVICE_ACCOUNT_JSON": "{}"}, clear=False
        ):
            rc = qq.main(["--wait-secs", "0"])
        self.assertEqual(rc, 3)
        self.assertEqual(writes, [])
        meta = json.loads(Path("out/qqscan_verify.json").read_text())
        self.assertEqual(meta["status"], "UPSTREAM_PENDING")
        self.assertFalse(meta["sheets"]["written"])

    def test_both_ready_filename_only_is_unknown_not_ok(self):
        writes = []

        def fake_write(sa, sheet_id, tab, col, symbols, header=None, do_readback=False):
            writes.append(col)
            receipt = {
                "written_n": len(symbols),
                "readback_n": len(symbols),
                "count_match": True,
                "sample_match": True,
                "readback_sample": list(symbols)[:10],
            }
            if do_readback:
                return len(symbols), receipt
            return len(symbols)

        def latest(owner_repo, prefix):
            if "my-stock" in owner_repo:
                return "Strong_Stocks_V3_20261106.txt", "http://example/s"
            return "Maggiestocks_20261106.txt", "http://example/m"

        with patch.object(
            qq, "last_complete_us_session_date", return_value=date(2026, 11, 5)
        ), patch.object(qq, "_latest_txt", side_effect=latest), patch.object(
            qq, "_fetch_symbols", side_effect=[["AAA"], ["BBB"]]
        ), patch.object(qq, "write_column", side_effect=fake_write), patch.dict(
            "os.environ", {"GOOGLE_SERVICE_ACCOUNT_JSON": "{}"}, clear=False
        ):
            rc = qq.main(["--wait-secs", "0"])
        self.assertEqual(rc, 0)
        self.assertEqual(writes, ["A", "J"])
        meta = json.loads(Path("out/qqscan_verify.json").read_text())
        self.assertEqual(meta["status"], "UNKNOWN")
        self.assertFalse(meta.get("bar_proof", True))
        self.assertTrue(meta["sheets"]["written"])
        self.assertEqual(meta["expected_upstream_file_date"], "2026-11-06")
        self.assertFalse(meta["sources"][0]["bar_dates_in_file"])
        self.assertIn("readback", meta["sheets"])


class TestPulseStaleSkipsSheets(unittest.TestCase):
    def test_stale_bars_skip_write(self):
        idx = pd.date_range("2026-10-01", periods=150, freq="D")
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

        with patch.object(
            pe, "last_complete_us_session_date", return_value=date(2026, 11, 5)
        ), patch.object(pe, "download", return_value={"AAA": df}), patch.object(
            pe, "drop_forming", side_effect=lambda d: d
        ), patch.object(pe, "screen_symbol", return_value=[]), patch.object(
            pe, "ff_state_row", return_value=None
        ), patch.object(pe, "write_tab", side_effect=fake_write_tab), patch.object(
            pe,
            "freshness_ok",
            return_value=(
                False,
                {
                    "screened": 1,
                    "fresh": 0,
                    "frac": 0.0,
                    "expected": "2026-11-05",
                    "unique_last_bars": ["2026-11-04"],
                },
            ),
        ), patch.dict("os.environ", {"GOOGLE_SERVICE_ACCOUNT_JSON": "{}"}, clear=False):
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
                code = pe.main()
        self.assertEqual(code, 4)
        self.assertEqual(writes, [])
        meta = json.loads(Path("out/pulse_verify.json").read_text())
        self.assertEqual(meta["status"], "FAIL_STALE")
        self.assertFalse(meta["sheets"]["written"])

    def test_fresh_zero_events_ok_no_new_signal(self):
        idx = pd.date_range("2026-10-01", periods=150, freq="D")
        idx = idx[:-1].append(pd.DatetimeIndex([pd.Timestamp("2026-11-05")]))
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
        univ = Path("out/_test_univ2.csv")
        univ.parent.mkdir(parents=True, exist_ok=True)
        univ.write_text("ticker,layer\nAAA,test\n", encoding="utf-8")
        writes = []

        def fake_write_tab(*a, **k):
            writes.append(a[2] if len(a) > 2 else a)
            return {"written_n": 0, "readback_n": 0, "count_match": True}

        with patch.object(
            pe, "last_complete_us_session_date", return_value=date(2026, 11, 5)
        ), patch.object(pe, "download", return_value={"AAA": df}), patch.object(
            pe, "drop_forming", side_effect=lambda d: d
        ), patch.object(pe, "screen_symbol", return_value=[]), patch.object(
            pe, "ff_state_row", return_value=None
        ), patch.object(pe, "write_tab", side_effect=fake_write_tab), patch.dict(
            "os.environ", {"GOOGLE_SERVICE_ACCOUNT_JSON": "{}"}, clear=False
        ):
            argv = [
                "write_pulse_events.py",
                "--universe",
                str(univ),
                "--out-csv",
                "out/pulse_events2.csv",
                "--out-ff-csv",
                "out/pulse_ff_state2.csv",
            ]
            with patch.object(sys, "argv", argv):
                code = pe.main()
        self.assertEqual(code, 0)
        self.assertEqual(len(writes), 2)  # events + ff_state still written when fresh
        meta = json.loads(Path("out/pulse_verify.json").read_text())
        self.assertEqual(meta["status"], "OK_NO_NEW_SIGNAL")
        self.assertTrue(meta["sheets"]["written"])
        self.assertIn("readback", meta["sheets"])


class TestVerifyMetaSchema(unittest.TestCase):
    def test_write_schema(self):
        path = write_verify_meta(
            "out/_schema_test.json",
            {
                "job": "unit",
                "status": "OK_NO_NEW_SIGNAL",
                "target_session": "2026-11-05",
                "input_bars": bar_distribution(
                    [date(2026, 11, 5)] * 3, date(2026, 11, 5), missing_count=1
                ),
                "sheets": {
                    "attempted": False,
                    "written": False,
                    "skipped_reason": "dry_run",
                },
            },
        )
        data = json.loads(Path(path).read_text())
        self.assertEqual(data["schema_version"], 1)
        self.assertEqual(data["status"], "OK_NO_NEW_SIGNAL")
        self.assertEqual(data["input_bars"]["min"], "2026-11-05")
        self.assertEqual(data["input_bars"]["denom"], 4)
        self.assertIn("checked_at_utc", data)


class TestReadbackReceipt(unittest.TestCase):
    def test_receipt_helper(self):
        r = readback_receipt(
            written_n=3, read_values=["A", "B", "C"], expected_sample=["A", "B"]
        )
        self.assertTrue(r["count_match"])
        self.assertTrue(r["sample_match"])

    def test_write_column_readback_mocked(self):
        ws = MagicMock()
        ws.row_count = 1000
        ws.get.return_value = [["hdr"], ["AAA"], ["BBB"]]
        sh = MagicMock()
        sh.worksheet.return_value = ws
        gc = MagicMock()
        gc.open_by_key.return_value = sh

        with patch("sheets_writer._client", return_value=gc), patch(
            "sheets_writer._ensure_worksheet", return_value=ws
        ):
            n, receipt = write_column(
                "{}",
                "sheet",
                "QQscan",
                "A",
                ["AAA", "BBB"],
                header="hdr",
                do_readback=True,
            )
        self.assertEqual(n, 2)
        self.assertTrue(receipt["count_match"])
        self.assertTrue(receipt["sample_match"])
        ws.batch_clear.assert_called()
        ws.update.assert_called()


class TestZhuliStaleSkipsSheets(unittest.TestCase):
    def test_stale_skips_write(self):
        writes = []

        def fake_write(*a, **k):
            writes.append(a)
            return 0

        with patch.object(
            zl,
            "screen",
            return_value=(
                ["AAA"],
                ["BBB"],
                {"L": 1, "M": 1, "bar_by_ticker": {}, "missing_count": 0},
                [date(2026, 11, 4)] * 10,
            ),
        ), patch.object(
            zl, "last_complete_us_session_date", return_value=date(2026, 11, 5)
        ), patch.object(zl, "write_column", side_effect=fake_write), patch.object(
            zl, "refresh_qqscan_headers_preserving"
        ), patch.object(
            zl, "universe_us_ex_etf_fund", return_value=["AAA"]
        ), patch.dict("os.environ", {"GOOGLE_SERVICE_ACCOUNT_JSON": "{}"}, clear=False):
            rc = zl.main([])
        self.assertEqual(rc, 4)
        self.assertEqual(writes, [])
        meta = json.loads(Path("out/zhuli_verify.json").read_text())
        self.assertEqual(meta["status"], "FAIL_STALE")


class TestWorkflowSafety(unittest.TestCase):
    def test_no_push_triggers_on_production_writers(self):
        root = Path(".github/workflows")
        for name in (
            "daily_extrascan.yml",
            "qqscan.yml",
            "pulse_events.yml",
            "zhuli_lm.yml",
            "etf_leaders.yml",
        ):
            text = (root / name).read_text()
            # YAML 'on:' mapping must not include push/pull_request for writers
            self.assertNotRegex(text, r"(?m)^\\s*push\\s*:")
            self.assertNotRegex(text, r"(?m)^\\s*pull_request\\s*:")

    def test_unit_test_workflow_exists_offline(self):
        text = Path(".github/workflows/unit_tests.yml").read_text()
        self.assertIn("unittest discover", text)
        self.assertIn("GOOGLE_SERVICE_ACCOUNT_JSON: \"\"", text)


if __name__ == "__main__":
    unittest.main()
