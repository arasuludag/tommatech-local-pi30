"""Coulomb-counted state of charge and charge-stage detection.

Deliberately free of Home Assistant imports so the logic can be exercised on
its own — see tests/test_battery.py.

Why this exists
---------------
PI30 reports a `battery_capacity` percentage, but on a bank with no BMS the
inverter is only inferring it from terminal voltage, and terminal voltage under
load is dominated by IR drop (this bank is ~6.8 mOhm, so a 70 A draw moves it
half a volt with no change in charge whatsoever). Two things need better than
that guess:

* a state of charge trustworthy enough to reserve capacity for the night, and
* an honest answer to "did the bank actually finish absorbing today?", which on
  gel is the difference between a healthy bank and a slowly sulfating one.

Both come from integrating the charge/discharge current the inverter *does*
report accurately, then re-zeroing the integral at the one moment each day when
the true state is known for free: the end of absorption, when the bank is full
by definition. Drift accumulated over a day is discarded every afternoon, so
the counter never has to be right for longer than one solar cycle.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date
from typing import Any

_LOGGER = logging.getLogger(__name__)

STAGE_BULK = "bulk"
STAGE_ABSORPTION = "absorption"
STAGE_FLOAT = "float"
STAGE_DISCHARGING = "discharging"
STAGE_IDLE = "idle"
STAGE_UNKNOWN = "unknown"

_STAGE_LABELS = {
    STAGE_BULK: "Bulk",
    STAGE_ABSORPTION: "Absorption",
    STAGE_FLOAT: "Float",
    STAGE_DISCHARGING: "Discharging",
    STAGE_IDLE: "Idle",
    STAGE_UNKNOWN: "Unknown",
}

# Below this the bank counts as neither charging nor discharging. PI30 reports
# whole amps, so anything under 1 A is quantisation noise either way.
IDLE_CURRENT_A = 0.5

# A longer gap than this means samples were lost (collector drop, HA restart).
# Integrating the last known current across the gap would invent charge that
# never happened, so the interval is dropped instead of guessed.
MAX_SAMPLE_GAP_S = 60.0

# How long the bank must sit at the float plateau before the inverter's own
# bulk->float transition is accepted as proof that absorption finished.
FLOAT_CONFIRM_S = 120.0


@dataclass
class BatteryConfig:
    """Tunables. Defaults describe 4S2P OUTDO OT200-12(GEL): 48 V, 400 Ah."""

    capacity_ah: float = 400.0
    # Coulombic (not energy) efficiency — we count amp-hours, not watt-hours.
    # Lead-acid returns roughly 0.90 of the charge put in; the daily re-zero at
    # absorption absorbs whatever this gets wrong.
    charge_efficiency: float = 0.90
    # Absorption is finished when acceptance falls to this fraction of C.
    # 0.02 = C/50 = 8 A on a 400 Ah bank, which is where the Aug 16 taper
    # flattened out on this system.
    tail_current_fraction: float = 0.02
    # How close to a setpoint counts as sitting on that plateau.
    plateau_tolerance_v: float = 0.15
    absorption_hold_s: float = 900.0   # 15 min at tail current
    pinned_hold_s: float = 600.0       # 10 min on a plateau before trusting it

    @property
    def tail_current_a(self) -> float:
        return self.capacity_ah * self.tail_current_fraction


class BatteryTracker:
    """Accumulates charge and classifies the inverter's charge stage.

    Fed one sample per QPIGS poll (5 s). All timing is driven by the caller's
    monotonic clock so a wall-clock correction can't corrupt the integral, and
    by the caller's local date so the daily latches roll over at local
    midnight rather than UTC.
    """

    def __init__(self, config: BatteryConfig | None = None) -> None:
        self.config = config or BatteryConfig()
        self._ah: float | None = None
        self._calibrated = False
        self._last_ts: float | None = None
        self._stage = STAGE_UNKNOWN
        self._stage_since: float | None = None
        self._tail_since: float | None = None
        self._today: date | None = None
        self._absorption_done_today = False
        self._saw_absorption_today = False
        self._absorption_seconds_today = 0.0
        self._days_since_absorption: int | None = None

    # -- configuration -------------------------------------------------
    def configure(self, config: BatteryConfig) -> None:
        """Apply new tunables in place, rescaling the counter if the declared
        bank capacity changed so the stored charge keeps its meaning."""
        if self._ah is not None and config.capacity_ah != self.config.capacity_ah:
            fraction = self._ah / self.config.capacity_ah
            self._ah = fraction * config.capacity_ah
        self.config = config

    # -- persistence ---------------------------------------------------
    def as_dict(self) -> dict[str, Any]:
        return {
            "amp_hours": self._ah,
            "calibrated": self._calibrated,
            "date": self._today.isoformat() if self._today else None,
            "absorption_done_today": self._absorption_done_today,
            "saw_absorption_today": self._saw_absorption_today,
            "absorption_seconds_today": round(self._absorption_seconds_today),
            "days_since_absorption": self._days_since_absorption,
        }

    def restore(self, stored: dict[str, Any] | None) -> None:
        """Reload persisted state after a restart.

        `_last_ts` is deliberately left unset: the first sample after a restart
        must not integrate across however long HA was down.
        """
        if not stored:
            return
        self._ah = stored.get("amp_hours")
        self._calibrated = bool(stored.get("calibrated", False))
        stored_date = stored.get("date")
        if stored_date:
            try:
                self._today = date.fromisoformat(stored_date)
            except ValueError:
                self._today = None
        self._absorption_done_today = bool(stored.get("absorption_done_today", False))
        self._saw_absorption_today = bool(stored.get("saw_absorption_today", False))
        self._absorption_seconds_today = float(stored.get("absorption_seconds_today", 0) or 0)
        self._days_since_absorption = stored.get("days_since_absorption")

    @property
    def needs_seed(self) -> bool:
        return self._ah is None

    def seed_percent(self, percent: float) -> None:
        """Provisional starting point, normally the inverter's own guess.

        Only ever used to give the counter somewhere to start; `calibrated`
        stays False until the first real absorption re-zero, so consumers can
        tell a guess from a measurement.
        """
        pct = max(0.0, min(100.0, float(percent)))
        self._ah = self.config.capacity_ah * pct / 100.0
        self._calibrated = False

    # -- ingest --------------------------------------------------------
    def update(
        self,
        *,
        voltage: float | None,
        charge_current: float | None,
        discharge_current: float | None,
        bulk_setpoint: float | None,
        float_setpoint: float | None,
        now_monotonic: float,
        now_local_date: date,
    ) -> None:
        self._roll_day(now_local_date)

        interval: float | None = None
        if self._last_ts is not None:
            gap = now_monotonic - self._last_ts
            if 0 < gap <= MAX_SAMPLE_GAP_S:
                interval = gap
        self._last_ts = now_monotonic

        if voltage is None or charge_current is None or discharge_current is None:
            return

        stage = self._classify(
            voltage, charge_current, discharge_current, bulk_setpoint, float_setpoint
        )
        if stage != self._stage:
            self._stage = stage
            self._stage_since = now_monotonic
            if stage != STAGE_ABSORPTION:
                self._tail_since = None
        elif self._stage_since is None:
            self._stage_since = now_monotonic

        if interval is not None:
            self._integrate(charge_current, discharge_current, interval)
            if stage == STAGE_ABSORPTION:
                self._absorption_seconds_today += interval
                self._saw_absorption_today = True

        self._check_absorption_complete(charge_current, now_monotonic)

    # -- internals -----------------------------------------------------
    def _classify(
        self,
        voltage: float,
        charge_current: float,
        discharge_current: float,
        bulk_setpoint: float | None,
        float_setpoint: float | None,
    ) -> str:
        if discharge_current > IDLE_CURRENT_A:
            return STAGE_DISCHARGING
        if charge_current <= IDLE_CURRENT_A:
            return STAGE_IDLE
        tol = self.config.plateau_tolerance_v
        # Absorption is a floor, not a band: the bus overshoots the setpoint on
        # load-drop transients (58.1 V seen against a 57.7 V setpoint).
        if bulk_setpoint is not None and voltage >= bulk_setpoint - tol:
            return STAGE_ABSORPTION
        # Float is a band. A bulk ramp sweeps up through the float voltage on
        # its way to the bulk setpoint, and that must not read as float — it
        # passes through in seconds, so the pinned_hold_s requirement on
        # `voltage_pinned` discards the transit anyway.
        if float_setpoint is not None and abs(voltage - float_setpoint) <= tol:
            return STAGE_FLOAT
        return STAGE_BULK

    def _integrate(self, charge_current: float, discharge_current: float, interval: float) -> None:
        if self._ah is None:
            return
        net_a = charge_current * self.config.charge_efficiency - discharge_current
        self._ah = max(
            0.0, min(self.config.capacity_ah, self._ah + net_a * interval / 3600.0)
        )

    def _check_absorption_complete(self, charge_current: float, now_monotonic: float) -> None:
        if self._absorption_done_today:
            return
        cfg = self.config
        if self._stage == STAGE_ABSORPTION:
            if charge_current <= cfg.tail_current_a:
                if self._tail_since is None:
                    self._tail_since = now_monotonic
                elif now_monotonic - self._tail_since >= cfg.absorption_hold_s:
                    self._mark_absorption_complete("tail current held at plateau")
            else:
                self._tail_since = None
        elif (
            self._stage == STAGE_FLOAT
            and self._saw_absorption_today
            and self._stage_since is not None
            and now_monotonic - self._stage_since >= FLOAT_CONFIRM_S
        ):
            # The inverter dropped to float by itself after a spell at the bulk
            # plateau. Its stage machine is cruder than the tail-current test,
            # but a float transition is still the charger declaring the bank
            # full — and refusing to latch here would strand the counter on
            # days where cloud interrupts the taper before it flattens.
            self._mark_absorption_complete("inverter entered float")

    def _mark_absorption_complete(self, reason: str) -> None:
        self._absorption_done_today = True
        self._days_since_absorption = 0
        self._ah = self.config.capacity_ah
        self._calibrated = True
        self._tail_since = None
        _LOGGER.info("Absorption complete (%s); SOC counter re-zeroed to 100%%", reason)

    def _roll_day(self, today: date) -> None:
        if self._today == today:
            return
        if self._today is not None:
            # A day just ended. Count it only if it ended without absorbing.
            if self._absorption_done_today:
                self._days_since_absorption = 0
            elif self._days_since_absorption is None:
                self._days_since_absorption = 1
            else:
                self._days_since_absorption += 1
        self._today = today
        self._absorption_done_today = False
        self._saw_absorption_today = False
        self._absorption_seconds_today = 0.0
        self._tail_since = None

    # -- exposed state -------------------------------------------------
    @property
    def soc_percent(self) -> float | None:
        if self._ah is None:
            return None
        return round(self._ah / self.config.capacity_ah * 100, 1)

    @property
    def amp_hours(self) -> float | None:
        return None if self._ah is None else round(self._ah, 1)

    @property
    def calibrated(self) -> bool:
        """False while the counter is still running from a seeded guess."""
        return self._calibrated

    @property
    def stage(self) -> str:
        return self._stage

    @property
    def stage_label(self) -> str:
        return _STAGE_LABELS.get(self._stage, self._stage)

    @property
    def absorption_complete_today(self) -> bool:
        return self._absorption_done_today

    @property
    def absorption_minutes_today(self) -> float:
        return round(self._absorption_seconds_today / 60.0, 1)

    @property
    def days_since_absorption(self) -> int | None:
        """Whole days that have ended without a completed absorption.

        0 means the bank absorbed today or yesterday, so no full day has been
        missed. None until the first midnight rollover is observed.
        """
        return self._days_since_absorption

    @property
    def voltage_pinned(self) -> bool | None:
        """True when the bank has held a CV plateau long enough to trust it.

        This is the surplus signal. Sitting pinned at a setpoint means the bank
        is voltage-limited rather than current-limited: it is taking only what
        it wants, so the MPPT is throttling and there is headroom the array is
        not currently harvesting. It says surplus is *likely* — the proof is
        still per-step, by checking that added load doesn't reduce charge
        current.
        """
        if self._stage == STAGE_UNKNOWN:
            return None
        if self._stage not in (STAGE_ABSORPTION, STAGE_FLOAT):
            return False
        if self._stage_since is None or self._last_ts is None:
            return False
        return (self._last_ts - self._stage_since) >= self.config.pinned_hold_s
