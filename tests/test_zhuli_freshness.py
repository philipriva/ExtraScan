"""Zhuli L+M .90 gate uses Daily's screened-only denominator (threshold unchanged)."""
from datetime import date

import pandas as pd

import marketdata as m
import run_extrascan as rx
import write_zhuli_lm as z

EXP = date(2026, 11, 12)
OLD = date(2026, 11, 11)


def test_threshold_is_still_090():
    assert m.MIN_FRESH_FRAC == 0.90


def test_lm_freshness_matches_daily_definition():
    bars = [EXP] * 95 + [OLD] * 5
    ok, info = z.lm_freshness(bars, EXP, universe_n=6602)
    d_ok, d_info = rx.freshness_ok(bars, EXP)  # Daily ExtraScan's gate
    assert ok is d_ok is True
    assert info["fresh"] == d_info["fresh"] == 95
    assert info["screened"] == d_info["screened"] == 100
    assert info["frac"] == d_info["frac"] == 0.95
    assert info["universe"] == 6602 and info["denom"] == "screened"


def test_lm_freshness_boundary_and_block():
    assert z.lm_freshness([EXP] * 90 + [OLD] * 10, EXP, 6602)[0] is True
    assert z.lm_freshness([EXP] * 89 + [OLD] * 11, EXP, 6602)[0] is False
    assert z.lm_freshness([], EXP, 6602)[0] is False


def _run_main(monkeypatch, capsys, bar_dates, dl_fresh=5421, requested=6602):
    stats = m.DownloadStats(requested=requested, yahoo=6000, fresh=dl_fresh)

    def fake_screen(syms):
        return ["AAA"], ["BBB"], {"universe": len(syms), "bar_dates": list(bar_dates)}

    monkeypatch.setattr(z, "screen", fake_screen)
    z.download.last_stats = stats
    z.download.last_expected = EXP
    writes = []
    monkeypatch.setattr(z, "write_column", lambda *a, **k: writes.append(a) or 1)
    rc = z.main(["--dry-run", "--symbols", "AAA,BBB,CCC"])
    out = capsys.readouterr().out
    status = [l for l in out.splitlines() if l.startswith("STATUS ")]
    assert len(status) == 1 and not writes
    return rc, status[0]


def test_main_passes_on_screened_denominator(monkeypatch, capsys):
    rc, status = _run_main(monkeypatch, capsys, [EXP] * 996 + [OLD] * 4)
    assert rc == 0
    assert "writer=Zhuli" in status
    assert "fresh=996/1000=0.9960" in status
    assert "result=DRY_RUN" in status
    assert "denom=screened" in status and "universe_fresh=5421/6602" in status


def test_main_blocks_below_090(monkeypatch, capsys):
    rc, status = _run_main(monkeypatch, capsys, [EXP] * 80 + [OLD] * 20)
    assert rc == 3
    assert "fresh=80/100=0.8000" in status and "result=BLOCKED" in status


def test_screen_collects_bar_dates_before_filters(monkeypatch):
    idx = pd.bdate_range(end="2026-11-12", periods=260)
    up = pd.DataFrame({"Open": 10.0, "High": 10.5, "Low": 9.5,
                       "Close": [10 + i * 0.1 for i in range(260)], "Volume": 1_000_000}, index=idx)
    cheap = up.copy(); cheap["Close"] = 1.0  # fails close>$5 filter, still counted
    short = up.iloc[-50:]  # < MIN_DAILY_BARS: not screened, not counted
    monkeypatch.setattr(z, "download", lambda syms: {"UP": up, "CHEAP": cheap, "SHORT": short})
    monkeypatch.setattr(z, "drop_forming", lambda d: d)
    monkeypatch.setattr(z, "filter_mcap", lambda s: list(s))
    _, _, stats = z.screen(["UP", "CHEAP", "SHORT", "MISSING"])
    assert stats["bar_dates"] == [EXP, EXP]
    assert stats["filter_fail"] == 1
