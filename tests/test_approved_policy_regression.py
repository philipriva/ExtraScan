"""Regression: approved PR #1 freshness policy = fresh/(screened+missing) >= 0.90.

Missing tickers stay in the denominator; no coverage substitute gate.
"""
import unittest
from datetime import date

from us_session import DEFAULT_MIN_FRESH_FRAC, freshness_ok

EXP = date(2026, 11, 5)
OLD = date(2026, 11, 4)


class ApprovedPolicyRegression(unittest.TestCase):
    def test_default_threshold_is_0_90(self):
        self.assertEqual(DEFAULT_MIN_FRESH_FRAC, 0.90)

    def test_zhuli_case_fresh5492_screened5510_missing1100_fails(self):
        bars = [EXP] * 5492 + [OLD] * 18
        ok, info = freshness_ok(bars, EXP, missing_count=1100)
        self.assertFalse(ok)
        self.assertEqual(info["denom"], 6610)
        self.assertLess(info["frac"], 0.90)

    def test_all_screened_fresh_but_coverage_083_fails(self):
        bars = [EXP] * 830
        ok, info = freshness_ok(bars, EXP, missing_count=170)
        self.assertFalse(ok)
        self.assertAlmostEqual(info["frac"], 0.83, places=2)

    def test_missing20_total100_fails(self):
        ok, info = freshness_ok([EXP] * 80, EXP, missing_count=20)
        self.assertFalse(ok)
        self.assertEqual(info["denom"], 100)

    def test_strong_case_10278_of_11810_fails(self):
        ok, info = freshness_ok([EXP] * 10278, EXP, missing_count=1532)
        self.assertFalse(ok)
        self.assertEqual(info["denom"], 11810)

    def test_daily_case_5572_of_6610_fails(self):
        bars = [EXP] * 5572 + [OLD] * 210
        ok, _ = freshness_ok(bars, EXP, missing_count=828)
        self.assertFalse(ok)

    def test_exactly_090_passes(self):
        ok, _ = freshness_ok([EXP] * 90, EXP, missing_count=10)
        self.assertTrue(ok)


if __name__ == "__main__":
    unittest.main()
