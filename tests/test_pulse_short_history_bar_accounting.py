import json
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

import numpy as np
import pandas as pd

import write_pulse_events as wp

EXP = date(2026, 11, 5)


def frame(n, last="2026-11-05", nan=False):
    idx = pd.bdate_range(end=last, periods=n)
    v = np.full(n, np.nan) if nan else np.linspace(10, 20, n)
    return pd.DataFrame({"Open": v, "High": v, "Low": v, "Close": v, "Volume": v}, index=idx)


def run(data, syms):
    tmp = Path(tempfile.mkdtemp())
    seen = []
    argv = ["x", "--dry-run", "--universe", str(tmp / "u.csv"), "--out-csv", str(tmp / "e.csv"), "--out-ff-csv", str(tmp / "f.csv")]
    (tmp / "u.csv").write_text("x")
    with mock.patch.object(sys, "argv", argv), \
         mock.patch.object(wp, "load_universe", return_value=[(s, "L") for s in syms]), \
         mock.patch.object(wp, "download", return_value=data), \
         mock.patch.object(wp, "drop_forming", side_effect=lambda d: d), \
         mock.patch.object(wp, "last_complete_us_session_date", return_value=EXP), \
         mock.patch.object(wp, "screen_symbol", side_effect=lambda s, d, *a, **k: seen.append((s, len(d))) or []), \
         mock.patch.object(wp, "ff_state_row", return_value=None), \
         mock.patch.object(wp, "freshness_ok", wraps=wp.freshness_ok) as fo:
        wp.main()
    args, kw = fo.call_args
    return seen, wp.freshness_ok(*args, **kw)


class T(unittest.TestCase):
    def test_short_fresh_counts_fresh_not_screened(self):
        long = wp.MIN_BARS + 5
        seen, (ok, info) = run({"SPY": frame(long), "F": frame(long), "S": frame(5)}, ["F", "S"])
        self.assertEqual([s for s, _ in seen], ["F"])
        self.assertEqual(info["missing_count"], 0)
        self.assertTrue(ok)

    def test_nan_missing_and_stale_short(self):
        long = wp.MIN_BARS + 5
        _, (ok, info) = run({"F": frame(long), "N": frame(long, nan=True), "S": frame(5, last="2026-11-03")}, ["F", "N", "S", "G"])
        self.assertEqual(info["missing_count"], 2)
        self.assertEqual(info["stale_count"], 1)
        self.assertFalse(ok)


if __name__ == "__main__":
    unittest.main()
