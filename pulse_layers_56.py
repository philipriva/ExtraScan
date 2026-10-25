#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Pulse foreshadow layers ⑤ + ⑥ for MS145 events (bottoms-only).

⑥ whale_hit — Tongdaxin-style energy: yesterday red AND today yellow AND
   today's yellow height < yesterday red height.
   Same math as banker_screener_app_new.whale_hit / run_ff_whale.compute_whale_series.

⑤ pulse_bottom_5 — Market Pulse 見底讀 (foreshadow, not the hard gate ③):
   RS(ticker/SPY) stop lower-lows onset (60/10) AND within +10 TD market breadth
   cue: RSP/SPY 20d slope > 0.
   Live emit fires on the day breadth first confirms (no lookahead); research
   tables labeled the onset date. Bottoms-only — no top variant.

Banker Entry L1 still unused (not emitted).
"""
from __future__ import annotations

import pandas as pd

from run_extrascan import ema, xsa

# Whale needs ~150 bars; RS stop-LL needs 60.
MIN_BARS_WHALE = 150
MIN_BARS_L5 = 80
RS_LOOKBACK = 60
RS_TROUGH_WIN = 10
BREADTH_CONFIRM_TD = 10


def xrf(s: pd.Series, n: int) -> pd.Series:
    return s.shift(n)


def xcn(cond: pd.Series, length: int) -> pd.Series:
    c = cond == True  # noqa: E712
    total = c.astype(int)
    for i in range(1, length):
        total = total + (c.shift(i) == True).astype(int)  # noqa: E712
    return total


def pine_cross(a: pd.Series, b: pd.Series) -> pd.Series:
    return (
        ((a > b) & (a.shift(1) <= b.shift(1)))
        | ((a < b) & (a.shift(1) >= b.shift(1)))
    ) == True  # noqa: E712


def compute_whale_series(df: pd.DataFrame) -> pd.Series:
    """Full-series whale_hit. True only on pattern-complete day."""
    h = df["High"].astype(float)
    l = df["Low"].astype(float)
    c = df["Close"].astype(float)

    low25 = l.rolling(25, min_periods=25).min()
    high25 = h.rolling(25, min_periods=25).max()
    rng25 = high25 - low25
    var01 = ema(((c - low25) / rng25 * 100.0).where(rng25 != 0.0), 5)
    denom = ema(c - low25, 5)
    var02 = (rng25 / denom).where((denom != 0.0) & denom.notna())
    var03 = pine_cross(var02, var01)
    var04 = (xrf(var02, 2) < xrf(var02, 1)) & (var02 < xrf(var02, 1))
    var05 = (xcn(var03, 7) > 0) & (var04 == True)  # noqa: E712
    is_red = var05 & (xcn(var05, 5) == 1)

    var09 = ema(c, 5)
    var10 = l.rolling(33, min_periods=33).min()
    feed = pd.Series(0.0, index=df.index, dtype=float)
    feed = feed.where(~(l <= var10), var09)
    var11 = ema(feed, 3) * 40.0
    var12 = var11.where(var11 <= 100.0, var11 * 0.312)

    var22 = xrf(l, 1)
    abs3 = xsa((l - var22).abs(), 3, 1.0)
    pos3 = xsa((l - var22).clip(lower=0.0), 3, 1.0)
    var23 = (abs3 / pos3 * 100.0).where(pos3 != 0.0)
    var24 = ema(var23 * 10.0, 3)
    var25 = l.rolling(38, min_periods=38).min()
    var26 = var24.rolling(38, min_periods=38).max()
    mix = pd.Series(0.0, index=df.index, dtype=float)
    mix = mix.where(~(l <= var25), (var24 + var26 * 2.0) / 2.0)
    var28 = ema(mix, 3) / 618.0

    is_yellow = var12.fillna(0.0) > 0.0
    red_h = pd.concat([var12.fillna(0.0), var28.fillna(0.0)], axis=1).max(axis=1)
    yel_h = var12.fillna(0.0)

    valid = var01.notna() & var02.notna()
    prev_red = (is_red == True).shift(1).fillna(False)  # noqa: E712
    today_yel = is_yellow == True  # noqa: E712
    height_ok = yel_h < red_h.shift(1)
    hit = valid & prev_red & today_yel & height_ok.fillna(False)
    hit = hit & valid.shift(1).fillna(False)
    return hit.fillna(False).astype(bool)


def whale_hit_last(df: pd.DataFrame) -> bool:
    """True if last settled bar completes Whale (昨紅今黃較短)."""
    if df is None or len(df) < MIN_BARS_WHALE:
        return False
    series = compute_whale_series(df)
    return bool(series.iloc[-1]) if len(series) else False


def rolling_rs_stop_ll(
    rsn: pd.Series, lookback: int = RS_LOOKBACK, trough_win: int = RS_TROUGH_WIN
) -> pd.Series:
    recent = rsn.rolling(trough_win, min_periods=trough_win).min()
    prior = rsn.shift(trough_win).rolling(
        lookback - trough_win, min_periods=lookback - trough_win
    ).min()
    # astype(bool) — avoid object-dtype where ``~`` becomes -2 (truthy)
    return (recent > prior).fillna(False).astype(bool)


def build_breadth_ok(spy: pd.DataFrame, rsp: pd.DataFrame | None) -> pd.Series:
    """Market breadth cue: RSP/SPY 20d slope > 0. Index = SPY dates."""
    if spy is None or len(spy) < 30:
        return pd.Series(dtype=bool)
    spy_c = spy["Close"].astype(float)
    if rsp is None or len(rsp) < 30:
        return pd.Series(False, index=spy_c.index)
    idx = spy_c.index.intersection(rsp.index)
    if len(idx) < 30:
        return pd.Series(False, index=spy_c.index)
    ratio = rsp.loc[idx, "Close"].astype(float) / spy_c.loc[idx]
    slope = ratio - ratio.shift(20)
    breadth = (slope > 0).reindex(spy_c.index).fillna(False)
    return breadth.astype(bool)


def pulse_bottom_5_fire(
    sym: pd.DataFrame,
    spy: pd.DataFrame,
    breadth_ok: pd.Series,
) -> tuple[bool, pd.Timestamp | None]:
    """Return (fires_today, onset_ts).

    Fires on the day breadth first confirms within +10TD of a recent
    RS stop_ll onset (same rule as Pulse ``layer5_first_fire``, live /
    no lookahead). ``onset_ts`` is the structure-turn date (research label).
    """
    if sym is None or spy is None or len(sym) < MIN_BARS_L5 or len(spy) < MIN_BARS_L5:
        return False, None
    if breadth_ok is None or len(breadth_ok) == 0:
        return False, None

    idx = sym.index.intersection(spy.index).intersection(breadth_ok.index)
    if len(idx) < MIN_BARS_L5:
        return False, None

    close = sym.loc[idx, "Close"].astype(float)
    spy_c = spy.loc[idx, "Close"].astype(float)
    b_ok = breadth_ok.loc[idx].fillna(False).astype(bool)

    rs = close / spy_c
    stop_ll = rolling_rs_stop_ll(rs)
    prev_sll = stop_ll.shift(1).fillna(False).astype(bool)
    onset = stop_ll.astype(bool) & (~prev_sll)

    # Breadth: RSP/SPY slope when RSP present; else SPY-only soft cue (rs%Δ20>0)
    # matching Pulse layer5_first_fire pre-RSP / missing-RSP path.
    rs_d20 = (rs / rs.shift(20) - 1.0) * 100.0
    cue = b_ok.astype(bool).copy()
    # Where breadth series is all-False because RSP missing on early dates,
    # fall back per-day: if b_ok is False, still allow soft cue.
    # Soft cue used when RSP path never True in window — handled below per onset.

    t = idx[-1]
    look_n = BREADTH_CONFIRM_TD + 1
    look = idx[-look_n:] if len(idx) >= look_n else idx
    onset_dates = list(look[onset.loc[look]])
    if not onset_dates:
        return False, None

    rsp_era = pd.Timestamp("2003-05-01")

    for od in onset_dates:
        loc = int(idx.get_loc(od)) if not isinstance(idx.get_loc(od), slice) else idx.get_loc(od).start
        end_i = min(loc + BREADTH_CONFIRM_TD, len(idx) - 1)
        end_dt = idx[end_i]
        if end_dt > t:
            end_dt = t
        seg_idx = idx[(idx >= od) & (idx <= end_dt)]

        if od >= rsp_era and bool(b_ok.loc[seg_idx].any()):
            ok_mask = b_ok.loc[seg_idx]
        elif od >= rsp_era:
            # RSP era but slope not yet positive — no soft substitute
            ok_mask = b_ok.loc[seg_idx]
        else:
            # Pre-RSP: soft cue = 20d RS%Δ > 0
            ok_mask = (rs_d20.loc[seg_idx] > 0).fillna(False)

        ok_days = seg_idx[ok_mask.astype(bool).reindex(seg_idx).fillna(False)]
        if len(ok_days) == 0:
            continue
        first_confirm = ok_days[0]
        if first_confirm == t:
            return True, pd.Timestamp(od)
    return False, None
