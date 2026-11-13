"""Zhuli mcap lookup guard: retry errors, BLOCK write when errors > 10%."""
from datetime import date

import marketdata as m
import write_zhuli_lm as z

EXP = date(2026, 11, 12)


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t += s


def _flaky(caps, fail_times):
    calls = {}

    def lookup(sym):
        calls[sym] = calls.get(sym, 0) + 1
        if calls[sym] <= fail_times.get(sym, 0):
            return None
        return caps[sym]

    return lookup, calls


def test_threshold_and_genuine_fail_unchanged():
    assert z.MCAP_MIN == 100_000_000 and z.MCAP_MAX_ERR_FRAC == 0.10
    c = Clock()
    lookup, _ = _flaky({"BIG": 5e9, "SMALL": 5e7, "EDGE": 1e8}, {})
    ok, info = z.filter_mcap(["BIG", "SMALL", "EDGE"], lookup=lookup, sleep=c.sleep, clock=c)
    assert ok == ["BIG"]
    assert info["mcap_ok"] == 3 and info["mcap_fail"] == 2 and info["mcap_err"] == 0
    assert z.mcap_lookup_ok(info)


def test_errors_retried_then_recovered():
    c = Clock()
    lookup, calls = _flaky({"A": 5e9, "B": 5e9}, {"B": 2})
    ok, info = z.filter_mcap(["A", "B"], lookup=lookup, sleep=c.sleep, clock=c)
    assert ok == ["A", "B"] and info["mcap_err"] == 0 and calls["B"] == 3 and calls["A"] == 1
    assert info["rounds"] == 3


def test_persistent_errors_counted_and_block():
    c = Clock()
    caps = {f"S{i}": 5e9 for i in range(10)}
    lookup, _ = _flaky(caps, {"S0": 99, "S1": 99})
    ok, info = z.filter_mcap(list(caps), lookup=lookup, sleep=c.sleep, clock=c)
    assert info["mcap_err"] == 2 and info["err_symbols"] == ["S0", "S1"]
    assert len(ok) == 8
    assert not z.mcap_lookup_ok(info)  # 2/10 > 0.10


def test_ten_percent_boundary():
    assert z.mcap_lookup_ok({"candidates": 10, "mcap_err": 1})
    assert not z.mcap_lookup_ok({"candidates": 10, "mcap_err": 2})
    assert not z.mcap_lookup_ok({"candidates": 3, "mcap_err": 1})
    assert z.mcap_lookup_ok({"candidates": 0, "mcap_err": 0})


def test_budget_stops_retries():
    c = Clock()
    lookup, calls = _flaky({"X": 5e9}, {"X": 99})
    ok, info = z.filter_mcap(["X"], lookup=lookup, sleep=c.sleep, clock=c, budget=50, backoff=20)
    assert ok == [] and info["mcap_err"] == 1
    assert calls["X"] <= 3 and c.t <= 50


def test_lookup_exception_is_error_not_fail():
    c = Clock()

    def boom(sym):
        raise RuntimeError("YFRateLimitError")

    ok, info = z.filter_mcap(["A"], lookup=boom, sleep=c.sleep, clock=c, rounds=1, backoff=0)
    assert ok == [] and info["mcap_err"] == 1 and info["mcap_fail"] == 0


def _main(monkeypatch, capsys, mcap_info):
    def fake_screen(syms):
        return [], [], {"universe": 3, "bar_dates": [EXP] * 100, "mcap": dict(mcap_info)}

    monkeypatch.setattr(z, "screen", fake_screen)
    z.download.last_stats = m.DownloadStats(requested=3, fresh=3)
    z.download.last_expected = EXP
    writes = []
    monkeypatch.setattr(z, "write_column", lambda *a, **k: writes.append(a) or 1)
    rc = z.main(["--dry-run", "--symbols", "A,B,C"])
    status = [l for l in capsys.readouterr().out.splitlines() if l.startswith("STATUS ")]
    assert len(status) == 1 and not writes
    return rc, status[0]


def test_main_blocks_on_mcap_errors(monkeypatch, capsys):
    rc, st = _main(monkeypatch, capsys, {"candidates": 81, "mcap_ok": 0, "mcap_err": 81, "err_symbols": []})
    assert rc == 4
    assert "result=BLOCKED" in st and "reason=mcap_lookup" in st
    assert "mcap_ok=0 mcap_err=81 candidates=81" in st


def test_main_passes_with_few_errors(monkeypatch, capsys):
    rc, st = _main(monkeypatch, capsys, {"candidates": 81, "mcap_ok": 78, "mcap_err": 3})
    assert rc == 0 and "result=DRY_RUN" in st and "reason=" not in st
    assert "mcap_ok=78 mcap_err=3 candidates=81" in st
