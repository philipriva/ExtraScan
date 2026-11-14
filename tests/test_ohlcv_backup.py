from datetime import date

import ohlcv_backup as b


def test_empty_slots_pick_A_first():
    assert b.pick_slot({"A": "slot_A_empty.tar", "B": "slot_B_empty.tar"}, date(2026, 11, 15)) == ("A", "ok")


def test_oldest_slot_overwritten_keep_last_two():
    n = {"A": "ohlcv_store_backup_2026-11-15_slotA.tar", "B": "slot_B_empty.tar"}
    assert b.pick_slot(n, date(2026, 11, 22))[0] == "B"
    n = {"A": "ohlcv_store_backup_2026-11-15_slotA.tar", "B": "ohlcv_store_backup_2026-11-22_slotB.tar"}
    assert b.pick_slot(n, date(2026, 11, 29))[0] == "A"


def test_skip_if_this_week_exists_unless_force():
    n = {"A": "ohlcv_store_backup_2026-11-14_slotA.tar", "B": "slot_B_empty.tar"}
    assert b.pick_slot(n, date(2026, 11, 15))[0] is None
    assert b.pick_slot(n, date(2026, 11, 15), force=True)[0] == "B"
