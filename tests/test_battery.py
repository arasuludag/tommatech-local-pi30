"""Tests for the charge tracker.

`battery.py` has no Home Assistant imports, so this runs with plain pytest —
or directly, without pytest installed at all:

    python3 tests/test_battery.py
"""
from __future__ import annotations

import importlib.util
import sys
from datetime import date, timedelta
from pathlib import Path

# Loaded straight from the file rather than as `tommatech_local.battery`: the
# package __init__ imports Home Assistant, and the whole point of battery.py is
# that its logic doesn't need it.
_PATH = (
    Path(__file__).resolve().parents[1]
    / "custom_components" / "tommatech_local" / "battery.py"
)
_spec = importlib.util.spec_from_file_location("tommatech_battery", _PATH)
battery = importlib.util.module_from_spec(_spec)
# Registered before exec: dataclasses resolves field types via sys.modules.
sys.modules["tommatech_battery"] = battery
_spec.loader.exec_module(battery)

BatteryConfig = battery.BatteryConfig
BatteryTracker = battery.BatteryTracker
MAX_SAMPLE_GAP_S = battery.MAX_SAMPLE_GAP_S
STAGE_ABSORPTION = battery.STAGE_ABSORPTION
STAGE_BULK = battery.STAGE_BULK
STAGE_DISCHARGING = battery.STAGE_DISCHARGING
STAGE_FLOAT = battery.STAGE_FLOAT
STAGE_IDLE = battery.STAGE_IDLE

BULK = 57.5
FLOAT = 53.6
DAY = date(2026, 8, 16)


def make_tracker(**overrides) -> BatteryTracker:
    config = BatteryConfig(capacity_ah=400.0, charge_efficiency=0.9, **overrides)
    tracker = BatteryTracker(config)
    tracker.seed_percent(50.0)
    return tracker


def feed(tracker, *, seconds, voltage, charge=0.0, discharge=0.0, start=0.0,
         step=5.0, day=DAY):
    """Push `seconds` of samples at `step` cadence. Returns the end timestamp."""
    now = start
    end = start + seconds
    while now <= end:
        tracker.update(
            voltage=voltage, charge_current=charge, discharge_current=discharge,
            bulk_setpoint=BULK, float_setpoint=FLOAT,
            now_monotonic=now, now_local_date=day,
        )
        now += step
    return now - step


def test_seed_and_capacity():
    tracker = make_tracker()
    assert tracker.soc_percent == 50.0
    assert tracker.amp_hours == 200.0
    assert tracker.calibrated is False, "a seed is a guess, not a calibration"


def test_charge_integration_applies_efficiency():
    tracker = make_tracker()
    # 40 A for 1 h at 0.9 efficiency = 36 Ah on top of 200 Ah.
    feed(tracker, seconds=3600, voltage=52.0, charge=40.0, step=5.0)
    assert tracker.amp_hours == 236.0


def test_discharge_integration_is_unscaled():
    tracker = make_tracker()
    # 20 A out for 1 h = 20 Ah, no efficiency factor on the way out.
    feed(tracker, seconds=3600, voltage=50.0, discharge=20.0, step=5.0)
    assert tracker.amp_hours == 180.0


def test_long_gap_is_not_integrated():
    """A collector drop or HA restart must not invent charge."""
    tracker = make_tracker()
    tracker.update(voltage=52.0, charge_current=40.0, discharge_current=0.0,
                   bulk_setpoint=BULK, float_setpoint=FLOAT,
                   now_monotonic=0.0, now_local_date=DAY)
    tracker.update(voltage=52.0, charge_current=40.0, discharge_current=0.0,
                   bulk_setpoint=BULK, float_setpoint=FLOAT,
                   now_monotonic=MAX_SAMPLE_GAP_S + 3600, now_local_date=DAY)
    assert tracker.amp_hours == 200.0


def test_stage_classification():
    tracker = make_tracker()
    cases = [
        (52.0, 40.0, 0.0, STAGE_BULK),
        (BULK, 20.0, 0.0, STAGE_ABSORPTION),
        (BULK + 0.4, 12.0, 0.0, STAGE_ABSORPTION),   # overshoot still counts
        (FLOAT, 3.0, 0.0, STAGE_FLOAT),
        (50.0, 0.0, 15.0, STAGE_DISCHARGING),
        (51.0, 0.0, 0.0, STAGE_IDLE),
    ]
    for voltage, charge, discharge, expected in cases:
        tracker.update(voltage=voltage, charge_current=charge,
                       discharge_current=discharge, bulk_setpoint=BULK,
                       float_setpoint=FLOAT, now_monotonic=0.0, now_local_date=DAY)
        assert tracker.stage == expected, f"{voltage} V / {charge} A -> {tracker.stage}"


def test_absorption_completes_on_tail_current():
    tracker = make_tracker(absorption_hold_s=900.0, tail_current_fraction=0.02)
    # 30 A at the plateau is above the 8 A tail — not done yet.
    end = feed(tracker, seconds=1800, voltage=BULK, charge=30.0)
    assert tracker.absorption_complete_today is False
    # Drop to 6 A and hold past the 15 min window.
    feed(tracker, seconds=1000, voltage=BULK, charge=6.0, start=end + 5)
    assert tracker.absorption_complete_today is True
    assert tracker.soc_percent == 100.0, "completion re-zeroes the counter"
    assert tracker.calibrated is True


def test_absorption_does_not_complete_below_plateau():
    """Aug 17's failure mode: current is low but voltage never holds."""
    tracker = make_tracker()
    feed(tracker, seconds=7200, voltage=54.0, charge=5.0)
    assert tracker.absorption_complete_today is False
    assert tracker.calibrated is False


def test_float_transition_also_completes():
    """Cloud can interrupt the taper; the inverter's own float call still counts."""
    tracker = make_tracker(absorption_hold_s=900.0)
    end = feed(tracker, seconds=600, voltage=BULK, charge=25.0)
    assert tracker.absorption_complete_today is False
    feed(tracker, seconds=300, voltage=FLOAT, charge=4.0, start=end + 5)
    assert tracker.absorption_complete_today is True


def test_float_alone_does_not_complete():
    """Float without having reached the bulk plateau first proves nothing."""
    tracker = make_tracker()
    feed(tracker, seconds=3600, voltage=FLOAT, charge=4.0)
    assert tracker.absorption_complete_today is False


def test_voltage_pinned_requires_hold():
    tracker = make_tracker(pinned_hold_s=600.0)
    end = feed(tracker, seconds=300, voltage=BULK, charge=20.0)
    assert tracker.voltage_pinned is False, "5 min is not yet a plateau"
    end = feed(tracker, seconds=400, voltage=BULK, charge=20.0, start=end + 5)
    assert tracker.voltage_pinned is True
    # Falling off the plateau clears it immediately.
    feed(tracker, seconds=10, voltage=52.0, charge=40.0, start=end + 5)
    assert tracker.voltage_pinned is False


def test_missed_days_counter():
    tracker = make_tracker()
    day = DAY
    now = 0.0

    # Day 1: absorbs properly.
    now = feed(tracker, seconds=1200, voltage=BULK, charge=5.0, start=now, day=day)
    assert tracker.absorption_complete_today is True

    # Day 2 and 3: never gets there.
    for offset in (1, 2):
        day = DAY + timedelta(days=offset)
        now = feed(tracker, seconds=600, voltage=52.0, charge=30.0,
                   start=now + 5, day=day)
    assert tracker.days_since_absorption == 1, "one full day has ended unabsorbed"

    day = DAY + timedelta(days=3)
    feed(tracker, seconds=600, voltage=52.0, charge=30.0, start=now + 5, day=day)
    assert tracker.days_since_absorption == 2, "two full days missed -> block EV"


def test_day_rollover_resets_daily_latches():
    tracker = make_tracker()
    now = feed(tracker, seconds=1200, voltage=BULK, charge=5.0)
    assert tracker.absorption_complete_today is True
    assert tracker.absorption_minutes_today > 0

    feed(tracker, seconds=10, voltage=52.0, charge=30.0, start=now + 5,
         day=DAY + timedelta(days=1))
    assert tracker.absorption_complete_today is False
    assert tracker.absorption_minutes_today == 0.0
    assert tracker.soc_percent == 100.0, "charge carries over; only latches reset"


def test_restore_round_trip():
    tracker = make_tracker()
    feed(tracker, seconds=1200, voltage=BULK, charge=5.0)
    saved = tracker.as_dict()

    restored = BatteryTracker(BatteryConfig(capacity_ah=400.0))
    restored.restore(saved)
    assert restored.soc_percent == tracker.soc_percent
    assert restored.absorption_complete_today is True
    assert restored.calibrated is True
    assert restored.needs_seed is False


def test_restore_does_not_integrate_across_downtime():
    tracker = make_tracker()
    feed(tracker, seconds=600, voltage=52.0, charge=40.0)
    restored = BatteryTracker(BatteryConfig(capacity_ah=400.0, charge_efficiency=0.9))
    restored.restore(tracker.as_dict())
    before = restored.amp_hours
    # First sample after a restart lands at an arbitrary monotonic value.
    restored.update(voltage=52.0, charge_current=40.0, discharge_current=0.0,
                    bulk_setpoint=BULK, float_setpoint=FLOAT,
                    now_monotonic=99999.0, now_local_date=DAY)
    assert restored.amp_hours == before


def test_configure_rescales_on_capacity_change():
    tracker = make_tracker()          # 400 Ah, seeded to 50% = 200 Ah
    tracker.configure(BatteryConfig(capacity_ah=600.0, charge_efficiency=0.9))
    assert tracker.soc_percent == 50.0, "percentage is what carries, not amp-hours"
    assert tracker.amp_hours == 300.0


def test_soc_clamps_at_both_ends():
    tracker = make_tracker()
    feed(tracker, seconds=36000, voltage=52.0, charge=60.0)
    assert tracker.soc_percent == 100.0
    tracker2 = make_tracker()
    feed(tracker2, seconds=36000, voltage=48.0, discharge=60.0)
    assert tracker2.soc_percent == 0.0


def test_tail_current_scales_with_capacity():
    assert BatteryConfig(capacity_ah=400.0).tail_current_a == 8.0
    assert BatteryConfig(capacity_ah=200.0).tail_current_a == 4.0


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if not name.startswith("test_") or not callable(fn):
            continue
        try:
            fn()
            print(f"  ok   {name}")
        except AssertionError as err:
            failures += 1
            print(f"  FAIL {name}: {err}")
    print(f"\n{failures} failure(s)")
    sys.exit(1 if failures else 0)
