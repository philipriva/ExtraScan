"""Per-selected guard: universe fraction passing is NOT enough; every written
ticker's real last input bar must equal target_session."""
import json
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

import numpy as np
import pandas as pd

import run_extrascan as rx
import write_pulse_events as wp
import write_zhuli_lm as zl

EXP = date(2026, 11, 5)


def frame(n, last="2026-11-05"):
    idx = pd.bdate_range(end=last, periods=n)
    v = np.linspace(10, 20, n)
    return pd.DataFrame({"Open": v, "High": v, "Low": v, "Close": v, "Volume": v * 0 + 1e6}, index=idx)


def zhuli_main(L, M, bar_by, bars):
    writes = []
    with mock.patch.object(zl, "screen", return_value=(L, M, {"L": len(L), "M": len(M), "bar_by_ticker": bar_by, "missing_count": 0}, bars)), \
         mock.patch.object(zl, "last_complete_us_session_date", return_value=EXP), \
         mock.patch.object(zl, "write_column", side_effect=lambda *a, **k: writes.append(a) or (0, {})), \
         mock.patch.object(zl, "refresh_qqscan_headers_preserving"), \
         mock.patch.object(zl, "universe_us_ex_etf_fund", return_value=list(bar_by)), \
         mock.patch.dict("os.environ", {"GOOGLE_SERVICE_ACCOUNT_JSON": "{}"}, clear=False):
        rc = zl.main([])
    return rc, writes, json.loads(Path("out/zhuli_verify.json").read_text())


class Zhuli(unittest.TestCase):
    def test_universe_ok_but_one_selected_stale_refuses_write(self):
        bar_by = {f"T{i}": EXP.isoformat() for i in range(99)}
        bar_by["OLD"] = "2026-11-04"
        rc, writes, meta = zhuli_main(["T1", "OLD"], [], bar_by, [EXP] * 99 + [date(2026, 11, 4)])
        self.assertNotEqual(rc, 0)
        self.assertEqual(writes, [])
        self.assertEqual(meta["status"], "FAIL_SELECTED_NON_TARGET")
        self.assertEqual(meta["selected_non_target"], {"OLD": "2026-11-04"})

    def test_selected_without_bar_refuses(self):
        bar_by = {f"T{i}": EXP.isoformat() for i in range(100)}
        rc, writes, meta = zhuli_main(["GHOST"], [], bar_by, [EXP] * 100)
        self.assertNotEqual(rc, 0)
        self.assertEqual(writes, [])

    def test_unselected_stale_only_in_denominator(self):
        bar_by = {f"T{i}": EXP.isoformat() for i in range(95)}
        bar_by.update({f"S{i}": "2026-11-04" for i in range(5)})
        rc, writes, meta = zhuli_main(["T1"], ["T2"], bar_by, [EXP] * 95 + [date(2026, 11, 4)] * 5)
        self.assertEqual(rc, 0)
        self.assertEqual(len(writes), 2)
        self.assertEqual(meta["status"], "OK")
        self.assertEqual(set(meta["selected_bar_dates"].values()), {EXP.isoformat()})
        self.assertNotIn("truncated", json.dumps(meta["bar_by_ticker"]))


class Daily(unittest.TestCase):
    def run_split(self, data, flags):
        with mock.patch.object(rx, "download", return_value=(data, [])), \
             mock.patch.object(rx, "drop_forming", side_effect=lambda d: d), \
             mock.patch.object(rx, "fund_flow_flags", side_effect=flags), \
             mock.patch.object(rx, "last_complete_us_session_date", return_value=EXP):
            return rx.screen_split(list(data), "t")

    def test_selected_stale_fails_even_if_universe_ok(self):
        data = {f"T{i}": frame(200) for i in range(99)}
        data["OLD"] = frame(200, last="2026-11-04")
        y, _, st = self.run_split(data, lambda d: (True, False))
        self.assertTrue(st["universe_fresh_ok"])
        self.assertFalse(st["fresh_ok"])
        self.assertEqual(st["selected_non_target"], {"OLD": "2026-11-04"})

    def test_unselected_stale_ok(self):
        data = {f"T{i}": frame(200) for i in range(95)}
        data.update({f"S{i}": frame(200, last="2026-11-04") for i in range(5)})
        _, _, st = self.run_split(data, lambda d: (d.index[-1] == pd.Timestamp("2026-11-05"), False))
        self.assertTrue(st["fresh_ok"])
        self.assertEqual(set(st["selected_bar_dates"].values()), {EXP.isoformat()})
        self.assertEqual(len(st["bar_by_ticker"]), 100)


class Pulse(unittest.TestCase):
    def run_main(self, data, syms):
        tmp = Path(tempfile.mkdtemp()); (tmp / "u.csv").write_text("x")
        argv = ["x", "--universe", str(tmp / "u.csv"), "--out-csv", str(tmp / "e.csv"), "--out-ff-csv", str(tmp / "f.csv")]
        writes = []
        with mock.patch.object(sys, "argv", argv), \
             mock.patch.object(wp, "load_universe", return_value=[(s, "L") for s in syms]), \
             mock.patch.object(wp, "download", return_value=data), \
             mock.patch.object(wp, "drop_forming", side_effect=lambda d: d), \
             mock.patch.object(wp, "last_complete_us_session_date", return_value=EXP), \
             mock.patch.object(wp, "screen_symbol", side_effect=lambda s, d, asof, **k: [[asof, s, "x", "2026-10-30", "W"]]), \
             mock.patch.object(wp, "ff_state_row", return_value=None), \
             mock.patch.object(wp, "write_tab", side_effect=lambda *a, **k: writes.append(a) or {}), \
             mock.patch.dict("os.environ", {"GOOGLE_SERVICE_ACCOUNT_JSON": "{}"}, clear=False):
            rc = wp.main()
        return rc, writes, json.loads((tmp / "pulse_verify.json").read_text())

    def test_event_from_stale_input_refuses(self):
        n = wp.MIN_BARS + 5
        data = {f"T{i}": frame(n) for i in range(99)}
        data["OLD"] = frame(n, last="2026-11-04")
        rc, writes, meta = self.run_main(data, list(data))
        self.assertNotEqual(rc, 0)
        self.assertEqual(writes, [])
        self.assertEqual(meta["status"], "FAIL_SELECTED_NON_TARGET")

    def test_older_signal_date_with_fresh_input_ok(self):
        n = wp.MIN_BARS + 5
        data = {f"T{i}": frame(n) for i in range(10)}
        rc, writes, meta = self.run_main(data, list(data))
        self.assertEqual(rc, 0)
        self.assertEqual(meta["status"], "OK")


class EmptyReadback(unittest.TestCase):
    def test_empty_tab_readback_no_false_mismatch(self):
        import sheets_writer as sw
        ws = mock.MagicMock(); ws.get.return_value = [[]]
        sh = mock.MagicMock()
        with mock.patch.object(sw, "_client", return_value=mock.MagicMock(open_by_key=lambda k: sh)), \
             mock.patch.object(sw, "_ensure_worksheet", return_value=ws), \
             mock.patch.object(sw, "_clear_and_write"):
            r = sw.write_tabs("{}", "id", {"yellowETF": []}, do_readback=True)
        rec = r[1] if isinstance(r, tuple) else r
        self.assertTrue(rec["yellowETF"]["count_match"])


if __name__ == "__main__":
    unittest.main()
