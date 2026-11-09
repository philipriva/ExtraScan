import json
import unittest
from unittest import mock

import write_qqscan as qq


class R:
    def __init__(self, code, body=""):
        self.status_code = code; self.text = body
    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(self.status_code)


class MetaBranchFetch(unittest.TestCase):
    def test_branch_api_first(self):
        calls = []
        def get(url, headers=None, timeout=None):
            calls.append((url, headers.get("Accept")))
            return R(200, json.dumps({"ok": 1}))
        with mock.patch.object(qq.requests, "get", side_effect=get):
            m = qq._fetch_meta("Maggiestocks_20261110.txt", "https://raw.githubusercontent.com/o/r/main/Maggiestocks_20261110.txt", "o/r")
        self.assertEqual(m, {"ok": 1})
        self.assertEqual(calls[0][0], "https://api.github.com/repos/o/r/contents/Maggiestocks_20261110.txt.meta.json?ref=scan-meta")
        self.assertEqual(calls[0][1], "application/vnd.github.raw+json")

    def test_all_missing_returns_none(self):
        with mock.patch.object(qq.requests, "get", return_value=R(404)):
            self.assertIsNone(qq._fetch_meta("A_20261110.txt", "https://x/A_20261110.txt", "o/r"))

    def test_legacy_main_fallback(self):
        def get(url, headers=None, timeout=None):
            return R(200, '{"legacy": 1}') if "/main/" in url else R(404)
        with mock.patch.object(qq.requests, "get", side_effect=get):
            self.assertEqual(qq._fetch_meta("A_20261110.txt", "https://raw.githubusercontent.com/o/r/main/A_20261110.txt", "o/r"), {"legacy": 1})


if __name__ == "__main__":
    unittest.main()
