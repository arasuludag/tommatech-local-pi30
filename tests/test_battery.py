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
         step=5.0, day=DAY, pv=None, ac_out=None):
    """Push `seconds` of samples at `step` cadence. Returns the end timestamp."""
    now = start
    end = start + seconds
    while now <= end:
        tracker.update(
            voltage=voltage, charge_current=charge, discharge_current=discharge,
            pv_power=pv, ac_output_power=ac_out,
            bulk_setpoint=BULK, float_setpoint=FLOAT,
            now_monotonic=now, now_local_date=day,
        )
        now += step
    return now - step


# -- counting ----------------------------------------------------------
def test_seed_and_capacity():
    tracker = make_tracker()
    assert tracker.soc_percent == 50.0
    assert tracker.amp_hours == 200.0
    assert tracker.calibrated is False, "a seed is a guess, not a calibration"


def test_charge_integration_applies_efficiency():
    tracker = make_tracker()
    # 40 A for 1 h at 0.9 efficiency = 36 Ah on top of 200 Ah.
    feed(tracker, seconds=3600, voltage=52.0, charge=40.0)
    assert tracker.amp_hours == 236.0


def test_charge_uses_the_ammeter_not_the_balance():
    """Charge current is verified accurate, so an absurd balance is ignored."""
    tracker = make_tracker()
    feed(tracker, seconds=3600, voltage=52.0, charge=40.0, pv=0.0, ac_out=5000.0)
    assert tracker.amp_hours == 236.0


def test_long_gap_is_not_integrated():
    """A collector drop or HA restart must not invent charge."""
    tracker = make_tracker()
    tracker.update(voltage=52.0, charge_current=40.0, discharge_current=0.0,
                   pv_power=None, ac_output_power=None,
                   bulk_setpoint=BULK, float_setpoint=FLOAT,
                   now_monotonic=0.0, now_local_date=DAY)
    tracker.update(voltage=52.0, charge_current=40.0, discharge_current=0.0,
                   pv_power=None, ac_output_power=None,
                   bulk_setpoint=BULK, float_setpoint=FLOAT,
                   now_monotonic=MAX_SAMPLE_GAP_S + 3600, now_local_date=DAY)
    assert tracker.amp_hours == 200.0


def test_soc_clamps_at_both_ends():
    tracker = make_tracker()
    feed(tracker, seconds=36000, voltage=52.0, charge=60.0)
    assert tracker.soc_percent == 100.0
    tracker2 = make_tracker()
    feed(tracker2, seconds=36000, voltage=48.0, discharge=60.0)
    assert tracker2.soc_percent == 0.0


# -- discharge reconstruction ------------------------------------------
def test_discharge_reconstructed_when_ammeter_sleeps():
    """The real bug: at ~105 W the ammeter read 0.6 W. It must not read as idle.

    300 W load / 0.92 + 30 W idle = 356.1 W = 7.12 A at 50 V.
    """
    tracker = make_tracker(inverter_efficiency=0.92, inverter_idle_w=30.0)
    feed(tracker, seconds=3600, voltage=50.0, discharge=0.0, pv=0.0, ac_out=300.0)
    assert tracker.stage == STAGE_IDLE, "the inverter still claims nothing is moving"
    assert tracker.amp_hours == 192.9, "but the bank was drained anyway"


def test_pv_offsets_the_reconstructed_discharge():
    tracker = make_tracker(inverter_efficiency=0.92, inverter_idle_w=30.0)
    # PV exactly covers load + losses, so nothing should come out of the bank.
    feed(tracker, seconds=3600, voltage=50.0, discharge=0.0, pv=356.087, ac_out=300.0)
    assert tracker.amp_hours == 200.0


def test_larger_drain_wins():
    """If the ammeter ever reads high, believe it — erring low on SOC is safe."""
    tracker = make_tracker(inverter_efficiency=0.92, inverter_idle_w=30.0)
    feed(tracker, seconds=3600, voltage=50.0, discharge=20.0, pv=0.0, ac_out=300.0)
    assert tracker.amp_hours == 180.0, "20 A ammeter beats the 7.1 A balance"


def test_discharge_falls_back_to_ammeter_without_balance_inputs():
    tracker = make_tracker()
    feed(tracker, seconds=3600, voltage=50.0, discharge=20.0)
    assert tracker.amp_hours == 180.0


def test_net_power_is_signed():
    tracker = make_tracker(inverter_efficiency=0.92, inverter_idle_w=30.0)
    feed(tracker, seconds=60, voltage=52.0, charge=40.0)
    assert tracker.net_power_w > 0
    tracker2 = make_tracker(inverter_efficiency=0.92, inverter_idle_w=30.0)
    feed(tracker2, seconds=60, voltage=50.0, pv=0.0, ac_out=300.0)
    assert tracker2.net_power_w < 0


# -- stage detection ---------------------------------------------------
def test_stage_classification():
    cases = [
        (52.0, 40.0, 0.0, STAGE_BULK),
        (BULK, 20.0, 0.0, STAGE_ABSORPTION),
        (BULK + 0.4, 12.0, 0.0, STAGE_ABSORPTION),   # overshoot still counts
        (FLOAT, 3.0, 0.0, STAGE_FLOAT),
        (50.0, 0.0, 15.0, STAGE_DISCHARGING),
        (51.0, 0.0, 0.0, STAGE_IDLE),
    ]
    for voltage, charge, discharge, expected in cases:
        tracker = make_tracker()
        feed(tracker, seconds=60, voltage=voltage, charge=charge, discharge=discharge)
        assert tracker.stage == expected, f"{voltage} V / {charge} A -> {tracker.stage}"


def test_plateau_tolerance_covers_regulation_wander():
    """This unit sits roughly +/-0.3 V off its setpoint."""
    tracker = make_tracker()
    feed(tracker, seconds=60, voltage=BULK - 0.3, charge=20.0)
    assert tracker.stage == STAGE_ABSORPTION


def test_brief_wander_does_not_reset_the_plateau():
    tracker = make_tracker(pinned_hold_s=600.0)
    end = feed(tracker, seconds=900, voltage=BULK, charge=20.0)
    assert tracker.voltage_pinned is True

    # 15 s off the plateau — shorter than the debounce, so nothing resets.
    end = feed(tracker, seconds=15, voltage=BULK - 0.9, charge=30.0, start=end + 5)
    assert tracker.stage == STAGE_ABSORPTION
    assert tracker.voltage_pinned is True

    # A sustained departure does commit.
    feed(tracker, seconds=60, voltage=BULK - 0.9, charge=30.0, start=end + 5)
    assert tracker.stage == STAGE_BULK
    assert tracker.voltage_pinned is False


def test_voltage_pinned_requires_hold():
    tracker = make_tracker(pinned_hold_s=600.0)
    end = feed(tracker, seconds=300, voltage=BULK, charge=20.0)
    assert tracker.voltage_pinned is False, "5 min is not yet a plateau"
    feed(tracker, seconds=400, voltage=BULK, charge=20.0, start=end + 5)
    assert tracker.voltage_pinned is True


# -- absorption --------------------------------------------------------
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
    feed(tracker, seconds=7200, voltage=54.5, charge=5.0)
    assert tracker.absorption_complete_today is False
    assert tracker.calibrated is False


def test_float_transition_also_completes():
    """Cloud can interrupt the taper; a genuine float — low current — counts."""
    tracker = make_tracker(absorption_hold_s=900.0)
    end = feed(tracker, seconds=600, voltage=BULK, charge=25.0)
    assert tracker.absorption_complete_today is False
    feed(tracker, seconds=1000, voltage=FLOAT, charge=4.0, start=end + 5)
    assert tracker.absorption_complete_today is True


def test_float_sag_under_load_does_not_complete():
    """Regression, 2026-08-18 10:45.

    Voltage sagged through the float band for ~2.5 min during a load-driven
    oscillation while the bank was still swallowing 20 A, and the old
    voltage-only fallback latched a false completion.
    """
    tracker = make_tracker(absorption_hold_s=900.0)
    end = feed(tracker, seconds=1200, voltage=BULK, charge=25.0)
    assert tracker.absorption_complete_today is False
    feed(tracker, seconds=180, voltage=FLOAT, charge=20.0, start=end + 5)
    assert tracker.absorption_complete_today is False


def test_oscillating_between_plateaus_still_completes():
    """Absorption <-> float swings at tail current must not reset the hold."""
    tracker = make_tracker(absorption_hold_s=900.0)
    now = feed(tracker, seconds=120, voltage=BULK, charge=25.0)
    now = feed(tracker, seconds=500, voltage=BULK, charge=5.0, start=now + 5)
    assert tracker.absorption_complete_today is False
    now = feed(tracker, seconds=200, voltage=FLOAT, charge=5.0, start=now + 5)
    feed(tracker, seconds=400, voltage=BULK, charge=5.0, start=now + 5)
    assert tracker.absorption_complete_today is True


def test_sag_below_both_plateaus_resets_the_tail_hold():
    """Dropping off the plateau entirely means the bank was not full."""
    tracker = make_tracker(absorption_hold_s=900.0)
    now = feed(tracker, seconds=800, voltage=BULK, charge=5.0)
    assert tracker.absorption_complete_today is False
    now = feed(tracker, seconds=120, voltage=52.0, charge=5.0, start=now + 5)
    feed(tracker, seconds=600, voltage=BULK, charge=5.0, start=now + 5)
    assert tracker.absorption_complete_today is False, "hold restarts from zero"


def test_brief_spike_does_not_break_the_tail_hold():
    """Regression, 2026-08-19.

    The bank sat at a 1-5 A mean for over three hours at the plateau, but brief
    spikes to 9-26 A landed in nearly every five-minute window. Requiring an
    unbroken run reset the timer constantly and absorption never latched.
    """
    tracker = make_tracker(absorption_hold_s=900.0)
    now = feed(tracker, seconds=600, voltage=BULK, charge=5.0)
    now = feed(tracker, seconds=30, voltage=BULK, charge=20.0, start=now + 5)
    feed(tracker, seconds=600, voltage=BULK, charge=5.0, start=now + 5)
    assert tracker.absorption_complete_today is True


def test_sustained_rise_does_break_the_tail_hold():
    """A long excursion means the bank really is accepting current again."""
    tracker = make_tracker(absorption_hold_s=900.0)
    now = feed(tracker, seconds=600, voltage=BULK, charge=5.0)
    now = feed(tracker, seconds=120, voltage=BULK, charge=20.0, start=now + 5)
    feed(tracker, seconds=600, voltage=BULK, charge=5.0, start=now + 5)
    assert tracker.absorption_complete_today is False, "hold should restart"


def test_float_alone_does_not_complete():
    """Float without having reached the bulk plateau first proves nothing."""
    tracker = make_tracker()
    feed(tracker, seconds=3600, voltage=FLOAT, charge=4.0)
    assert tracker.absorption_complete_today is False


# -- daily bookkeeping -------------------------------------------------
def test_missed_days_counter():
    tracker = make_tracker()
    day = DAY
    now = feed(tracker, seconds=1200, voltage=BULK, charge=5.0, day=day)
    assert tracker.absorption_complete_today is True

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

    # A few seconds of carryover is expected: the stage debounce means
    # absorption isn't abandoned the instant voltage moves. Immaterial in
    # practice, since at local midnight the bank is discharging, not absorbing.
    feed(tracker, seconds=60, voltage=52.0, charge=30.0, start=now + 5,
         day=DAY + timedelta(days=1))
    assert tracker.absorption_complete_today is False
    assert tracker.absorption_minutes_today < 1.0
    assert tracker.soc_percent == 100.0, "charge carries over; only latches reset"


# -- persistence and config -------------------------------------------
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
                    pv_power=None, ac_output_power=None,
                    bulk_setpoint=BULK, float_setpoint=FLOAT,
                    now_monotonic=99999.0, now_local_date=DAY)
    assert restored.amp_hours == before


def test_configure_rescales_on_capacity_change():
    tracker = make_tracker()          # 400 Ah, seeded to 50% = 200 Ah
    tracker.configure(BatteryConfig(capacity_ah=600.0, charge_efficiency=0.9))
    assert tracker.soc_percent == 50.0, "percentage is what carries, not amp-hours"
    assert tracker.amp_hours == 300.0


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
