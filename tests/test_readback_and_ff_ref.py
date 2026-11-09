import unittest
from datetime import date
from unittest import mock

import sheets_writer as sw
import write_pulse_events as wp


def _write(rows, got):
    ws = mock.MagicMock(); ws.get.return_value = got
    sh = mock.MagicMock()
    with mock.patch.object(sw, "_client", return_value=mock.MagicMock(open_by_key=lambda k: sh)), \
         mock.patch.object(sw, "_ensure_worksheet", return_value=ws), \
         mock.patch.object(sw, "_clear_and_write"), mock.patch.object(sw.time, "sleep"):
        return sw.write_tabs("{}", "id", {"t": rows}, do_readback=True)


class ReadbackNormalize(unittest.TestCase):
    def test_canonical_empty(self):
        for got in ([], [[]], [[""]], [["", " "]]):
            self.assertEqual(sw.normalize_readback_rows(got), [])
            self.assertTrue(_write([], got)["t"]["count_match"])

    def test_trailing_blank_trimmed(self):
        self.assertEqual(sw.normalize_readback_rows([["A"], ["B"], [], [""]]), [["A"], ["B"]])
        self.assertTrue(_write([["A"], ["B"]], [["A"], ["B"], []])["t"]["count_match"])

    def test_interior_blank_kept_and_fails_closed(self):
        self.assertEqual(sw.normalize_readback_rows([["A"], [], ["B"]]), [["A"], [], ["B"]])
        with self.assertRaises(RuntimeError):
            _write([["A"], ["B"]], [["A"], [], ["B"]])

    def test_short_readback_fails_closed(self):
        with self.assertRaises(RuntimeError):
            _write([["A"], ["B"]], [["A"]])


class FfStateReference(unittest.TestCase):
    def test_reference_status(self):
        r = wp.ff_state_reference_status({"AAA": "2026-11-09", "BBB": "2026-11-06"}, date(2026, 11, 9))
        self.assertEqual(r["rows"], 2)
        self.assertEqual(r["non_target"], {"BBB": "2026-11-06"})
        self.assertEqual(r["status"], "REFERENCE_HAS_NON_TARGET")
        self.assertEqual(r["role"], "reference_only_not_write_gate")
        self.assertEqual(wp.ff_state_reference_status({"AAA": "2026-11-09"}, date(2026, 11, 9))["status"], "ALL_TARGET")

    def test_ff_header_still_9_cols(self):
        self.assertEqual(len(wp.FF_STATE_HEADER), 9)


if __name__ == "__main__":
    unittest.main()
