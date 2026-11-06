"""Short-history tickers with a real target-session bar are fresh-but-ineligible,
not missing. Empty / all-NaN / no-download stay missing. Denominator = universe."""
import unittest
from datetime import date
from unittest import mock

import numpy as np
import pandas as pd

import write_zhuli_lm as z
from us_session import freshness_ok

EXP = date(2026, 11, 5)


def frame(n, last="2026-11-05", nan=False):
    idx = pd.bdate_range(end=last, periods=n)
    v = np.full(n, np.nan) if nan else np.linspace(10, 20, n)
    return pd.DataFrame({"Open": v, "High": v, "Low": v, "Close": v, "Volume": v * 0 + (np.nan if nan else 1e6)}, index=idx)


def run(data, syms):
    with mock.patch.object(z, "download", return_value=data), \
         mock.patch.object(z, "filter_mcap", side_effect=lambda s: s), \
         mock.patch.object(z, "drop_forming", side_effect=lambda d: d):
        return z.screen(syms)


class T(unittest.TestCase):
    def test_short_history_fresh_counts_fresh_not_signal(self):
        syms = ["FULL", "SHORT"]
        _, _, st, bars = run({"FULL": frame(300), "SHORT": frame(30)}, syms)
        self.assertEqual(bars, [EXP, EXP])
        self.assertEqual(st["missing_count"], 0)
        self.assertEqual(st.get("short_history"), 1)
        self.assertEqual(st["screened"] + st["filter_fail"], 1)  # SHORT never screened

    def test_allnan_and_undownloaded_stay_missing(self):
        syms = ["FULL", "NAN", "GONE"]
        _, _, st, bars = run({"FULL": frame(300), "NAN": frame(300, nan=True)}, syms)
        self.assertEqual(len(bars), 1)
        self.assertEqual(st["missing_count"], 2)
        ok, info = freshness_ok(bars, EXP, missing_count=st["missing_count"])
        self.assertFalse(ok)
        self.assertEqual(info["denom"], 3)

    def test_short_history_stale_is_stale(self):
        _, _, st, bars = run({"S": frame(30, last="2026-11-03")}, ["S"])
        ok, _ = freshness_ok(bars, EXP, missing_count=st["missing_count"])
        self.assertEqual(bars, [date(2026, 11, 3)])
        self.assertFalse(ok)

    def test_bulk_download_loss_still_fails(self):
        syms = [f"T{i}" for i in range(100)]
        data = {s: frame(300) for s in syms[:80]}
        _, _, st, bars = run(data, syms)
        ok, info = freshness_ok(bars, EXP, missing_count=st["missing_count"])
        self.assertFalse(ok)
        self.assertEqual(info["denom"], 100)


if __name__ == "__main__":
    unittest.main()
