import unittest
from datetime import date

from qqscan_upstream_meta import check_input_bars_policy, payload_hash, verify_sidecar
from scan_meta import build_scan_meta


def ib(**kw):
    base = {"policy_version": "fresh_over_universe_v1", "fresh": 95, "universe_total": 100,
            "actual_complete_bar_count": 98, "missing_count": 2, "stale_count": 3, "future_count": 0, "min_frac": 0.9}
    base.update(kw)
    return base


class T(unittest.TestCase):
    def test_ok(self):
        self.assertTrue(check_input_bars_policy(ib(), "s")[0])

    def test_legacy_unknown(self):
        ok, st, _ = check_input_bars_policy({"fresh_ok": True, "frac": 1.0}, "s")
        self.assertEqual((ok, st), (False, "UNKNOWN"))

    def test_weaker_min_frac_reject(self):
        self.assertEqual(check_input_bars_policy(ib(min_frac=0.8), "s")[1], "REJECT")

    def test_other_policy_reject(self):
        self.assertEqual(check_input_bars_policy(ib(policy_version="bar_frac_dual"), "s")[1], "REJECT")

    def test_sum_mismatch_reject(self):
        self.assertEqual(check_input_bars_policy(ib(missing_count=0), "s")[1], "REJECT")

    def test_strong_087_reject_even_if_fresh_ok_claimed(self):
        r = ib(fresh=10278, universe_total=11810, actual_complete_bar_count=10281, missing_count=1529, stale_count=3, fresh_ok=True)
        self.assertEqual(check_input_bars_policy(r, "s")[1], "REJECT")

    def test_incomplete_unknown(self):
        r = ib(); del r["stale_count"]
        self.assertEqual(check_input_bars_policy(r, "s")[1], "UNKNOWN")

    def test_producer_meta_roundtrip_verifies(self):
        exp = date(2026, 11, 5)
        bars = [exp] * 95 + [date(2026, 11, 4)] * 3
        meta = build_scan_meta(source_id="x", list_filename="S_20261106.txt", producer_version="t", target_session=exp, tickers=["AAA"],
                               selected_bar_dates={"AAA": exp.isoformat()}, bar_dates_all=bars,
                               universe_n=100, missing_count=2)
        st, notes = verify_sidecar(meta=meta, tickers=["AAA"], target_session=exp, source_kind="s")
        self.assertEqual(st, "VERIFIED", notes)
        self.assertEqual(meta["input_bars"]["actual_complete_bar_count"] + meta["input_bars"]["missing_count"], 100)

    def test_selected_non_target_rejects(self):
        exp = date(2026, 11, 5)
        meta = build_scan_meta(source_id="x", list_filename="S_20261106.txt", producer_version="t", target_session=exp, tickers=["AAA"],
                               selected_bar_dates={"AAA": "2026-11-04"}, bar_dates_all=[exp] * 100,
                               universe_n=100, missing_count=0)
        st, _ = verify_sidecar(meta=meta, tickers=["AAA"], target_session=exp, source_kind="s")
        self.assertEqual(st, "REJECT")


if __name__ == "__main__":
    unittest.main()
