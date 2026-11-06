import unittest
from datetime import date
from unittest import mock

import numpy as np
import pandas as pd

import run_extrascan as rx
from us_session import freshness_ok

EXP = date(2026, 11, 5)


def frame(n, last="2026-11-05", nan=False):
    idx = pd.bdate_range(end=last, periods=n)
    v = np.full(n, np.nan) if nan else np.linspace(10, 20, n)
    return pd.DataFrame({"Open": v, "High": v, "Low": v, "Close": v}, index=idx)


def run(data, syms, flags=lambda d: (False, False)):
    dl_failed = [s for s in syms if s not in data]
    with mock.patch.object(rx, "download", return_value=(data, dl_failed)), \
         mock.patch.object(rx, "drop_forming", side_effect=lambda d: d), \
         mock.patch.object(rx, "fund_flow_flags", side_effect=flags), \
         mock.patch.object(rx, "last_complete_us_session_date", return_value=EXP):
        return rx.screen_split(syms, "t")


class T(unittest.TestCase):
    def test_short_fresh_is_fresh_not_eligible(self):
        called = []
        _, _, st = run({"F": frame(200), "S": frame(30)}, ["F", "S"], lambda d: called.append(len(d)) or (False, False))
        self.assertEqual(called, [200])
        self.assertEqual(st["missing_count"], 0)
        self.assertEqual(st["actual_bar_count"], 2)
        self.assertEqual(st["signal_eligible"], 1)
        self.assertEqual(st["short_history_ineligible"], 1)
        self.assertTrue(st["fresh_ok"])

    def test_nan_and_failed_stay_missing(self):
        _, _, st = run({"F": frame(200), "N": frame(200, nan=True)}, ["F", "N", "G"])
        self.assertEqual(st["missing_count"], 2)
        self.assertFalse(st["fresh_ok"])

    def test_short_stale_is_stale(self):
        _, _, st = run({"S": frame(30, last="2026-11-03")}, ["S"])
        self.assertFalse(st["fresh_ok"])
        self.assertEqual(st["fresh_info"]["stale_count"], 1)

    def test_bulk_loss_fails(self):
        syms = [f"T{i}" for i in range(100)]
        _, _, st = run({s: frame(200) for s in syms[:85]}, syms)
        self.assertFalse(st["fresh_ok"])
        self.assertEqual(st["fresh_info"]["denom"], 100)


if __name__ == "__main__":
    unittest.main()
