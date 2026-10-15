"""Coulomb-counted state of charge and charge-stage detection.

Deliberately free of Home Assistant imports so the logic can be exercised on
its own — see tests/test_battery.py.

Why this exists
---------------
PI30 reports a `battery_capacity` percentage, but on a bank with no BMS the
inverter is only inferring it from terminal voltage, and terminal voltage under
load is dominated by IR drop (the reference bank is ~6.8 mOhm, so a 70 A draw
moves it half a volt with no change in charge whatsoever). Two things need better than
that guess:

* a state of charge trustworthy enough to reserve capacity for the night, and
* an honest answer to "did the bank actually finish absorbing today?", which on
  gel is the difference between a healthy bank and a slowly sulfating one.

Which current to believe
------------------------
On the reference unit the two ammeters are not equally good, and it matters a
great deal.

`battery_charge_current` is sound. Checked against the PV-minus-load energy
balance across a full day it tracks within ~5% everywhere from 2 A to 42 A, so
it is used directly, and the absorption tail test can rely on it.

`battery_discharge_current` is not usable. Measured overnight, when PV is zero
and the bank must therefore supply the entire load plus conversion losses, it
reported 192 W against a 312 W load, 38 W against 139 W, and 0.6 W against
105 W — it degrades with load and reads flat zero below roughly 120 W. Across
one night it accounted for 883 Wh of a ~1950 Wh draw. Integrating that would
have the counter drifting several points optimistic every single night, which
is exactly backwards for an evening reserve decision.

So discharge is reconstructed from the energy balance instead — load, adjusted
for conversion efficiency and the inverter's own idle draw, minus whatever PV
is contributing. Where the two disagree the larger drain wins, because
underestimating the drain is the dangerous direction.

Drift is bounded regardless: the integral is re-zeroed to 100% once a day at
the end of absorption, when the bank is full by definition, so no error has to
survive longer than one solar cycle.

That re-zero is therefore the one moment that must not be wrong, and it is
gated on charge current rather than voltage. This inverter swings between its
float and bulk setpoints under load, so "the voltage is sitting at float" does
not mean "the charger has finished" — only a sustained tail current does.

The same caution applies to the stage this module reports, and did not always.
Float was classified on voltage alone, so the long current-limited crawl up
through the float band every morning read as Float — and Float is a surplus
signal that a load controller may start opportunistic loads on. Entering float now
also requires that absorption happened today and that the bank has stopped
accepting current.
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

# A stage change must persist this long before it is committed. The reference
# unit does not hold its bulk setpoint tightly — it wanders roughly +/-0.3 V and
# occasionally overshoots — and without debouncing, each brief excursion off
# the plateau would reset the absorption and plateau hold timers and they would
# never mature. Only the committed stage is exposed or acted on.
STAGE_DEBOUNCE_S = 30.0

# How long charge current may exceed the tail threshold without invalidating an
# absorption hold. Measured on the reference system: the bank sat at a 1-5 A
# mean for over three hours at the plateau, but brief spikes to 9-26 A landed in
# nearly every five-minute window (load steps from large switching loads).
# Requiring an unbroken
# run reset the timer constantly and absorption never latched despite the bank
# genuinely being at tail current. Excursions shorter than this are credited
# back rather than treated as the bank accepting again.
TAIL_BREAK_TOLERANCE_S = 90.0

# The same idea applied to the PLATEAU test, and for the same reason one step
# further out. A large load running off surplus (an EV charger, say) takes the
# array's output, the bus falls off the setpoint, and the committed stage
# bounces Absorption -> Bulk -> Discharging every one to five minutes; on the
# reference system it did so for six hours straight. Every one of those
# excursions used to wipe _tail_since outright, so the 15-minute hold could
# never assemble and the day ended "unabsorbed" with over an hour at the
# setpoint on the clock. Anything gating that same load on
# days_since_absorption then locks it out of a nearly full bank: the load
# suppresses the latch that authorises it.
#
# Deliberately longer than TAIL_BREAK_TOLERANCE_S: a current spike is one load
# step, an off-plateau excursion is the array losing the setpoint, which on a
# cloudy day takes minutes to recover. Time inside an excursion is credited
# back, never counted, so the hold still measures real at-plateau seconds.
PLATEAU_BREAK_TOLERANCE_S = 180.0


@dataclass
class BatteryConfig:
    """Tunables. Defaults describe the reference bank: 48 V, 400 Ah, gel.

    Every field except absorption_satisfied_s is exposed in the options flow,
    so another bank is configured there rather than here.
    """

    capacity_ah: float = 400.0
    # Coulombic (not energy) efficiency — we count amp-hours, not watt-hours.
    # Lead-acid returns roughly 0.90 of the charge put in; the daily re-zero at
    # absorption absorbs whatever this gets wrong.
    charge_efficiency: float = 0.90
    # DC->AC conversion efficiency and the inverter's own housekeeping draw,
    # used to reconstruct discharge from the load. Both are deliberately
    # adjustable: they are the only fitted parameters in the counter, and the
    # honest way to trim them is to watch overnight drift against the morning
    # re-zero.
    inverter_efficiency: float = 0.92
    inverter_idle_w: float = 30.0
    # Absorption is finished when acceptance falls to this fraction of C.
    # 0.02 = C/50 = 8 A on a 400 Ah bank, which is where the taper flattened
    # out on the reference system, and comfortably inside the range where the
    # charge ammeter was verified accurate.
    tail_current_fraction: float = 0.02
    # How close to a setpoint counts as sitting on that plateau. Sized for the
    # observed +/-0.3 V regulation wander, with margin.
    plateau_tolerance_v: float = 0.35
    absorption_hold_s: float = 900.0   # 15 min at tail current
    pinned_hold_s: float = 600.0       # 10 min on a plateau before trusting it
    # Second, time-based completion path, independent of the tail evidence.
    # Cumulative seconds at the absorption setpoint that count as the bank
    # having had its absorption for the day. 2 h is the textbook gel absorption
    # period from the ~80% at which a gel bank reaches its setpoint, and these
    # seconds are only accrued while the bus is genuinely voltage-limited at
    # the setpoint — a bank being robbed by a large load drops off the plateau
    # and stops accruing. This exists because the tail hold is evidence of a
    # FULL bank and can be denied all day by a choppy sky or a large load,
    # while the day counter it
    # feeds is supposed to answer a much weaker question: has this bank had a
    # proper absorption recently. Two hours at the setpoint answers that yes.
    absorption_satisfied_s: float = 7200.0

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
        self._pending_stage: str | None = None
        self._pending_since: float | None = None
        self._tail_since: float | None = None
        self._break_since: float | None = None
        self._net_power_w: float | None = None
        self._today: date | None = None
        self._absorption_done_today = False
        self._recalibrated_today = False
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
            "recalibrated_today": self._recalibrated_today,
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
        # Absent in state written before the satisfied/recalibrated split: fall
        # back to the old flag, which used to mean both things at once.
        self._recalibrated_today = bool(
            stored.get("recalibrated_today", self._absorption_done_today)
        )
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
        pv_power: float | None = None,
        ac_output_power: float | None = None,
        load_on_line: bool = False,
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

        net_a = self._net_current(
            voltage, charge_current, discharge_current, pv_power, ac_output_power,
            load_on_line,
        )
        self._net_power_w = round(net_a * voltage, 1)

        self._commit_stage(
            self._classify(
                voltage, charge_current, discharge_current, bulk_setpoint, float_setpoint
            ),
            now_monotonic,
        )

        if interval is not None:
            self._integrate(net_a, interval)
            if self._stage == STAGE_ABSORPTION:
                self._absorption_seconds_today += interval
                self._saw_absorption_today = True

        self._check_absorption_complete(charge_current, now_monotonic)

    # -- internals -----------------------------------------------------
    def _net_current(
        self,
        voltage: float,
        charge_current: float,
        discharge_current: float,
        pv_power: float | None,
        ac_output_power: float | None,
        load_on_line: bool,
    ) -> float:
        """Signed battery current in amps: positive in, negative out.

        See the module docstring for why the two directions come from
        different sources.
        """
        if voltage <= 0:
            return 0.0
        if charge_current > IDLE_CURRENT_A:
            return charge_current * self.config.charge_efficiency

        balance_a = 0.0
        # The balance assumes the bank is what feeds the load. In Line mode it
        # is not: the inverter passes its AC input (grid, generator, or a
        # vehicle's V2L outlet) straight through to the load, and that input
        # also powers the inverter itself. Reconstructing from it would book
        # the whole load against the bank — a phantom 5-7 A drain at an ordinary evening
        # load, every sample the charger happens to be idle. Only the ammeter
        # is left, and with the bank genuinely idle its low reading is honest.
        if not load_on_line and pv_power is not None and ac_output_power is not None:
            deficit_w = (
                ac_output_power / self.config.inverter_efficiency
                + self.config.inverter_idle_w
                - pv_power
            )
            balance_a = max(0.0, deficit_w) / voltage
        # Whichever source claims the larger drain wins. The ammeter is known
        # to under-read badly; if it ever reads high instead, believing it is
        # still the conservative direction for a reserve decision.
        return -max(discharge_current, balance_a)

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
        # Float is a band, but voltage alone cannot identify it. This code used
        # to assume a bulk ramp "passes through in seconds, so STAGE_DEBOUNCE_S
        # discards the transit". It does not. The array is current-limited
        # during the morning ramp, so the bank CRAWLS through the band: on the
        # reference system it sat inside for thirteen to twenty-two minutes
        # while accepting 21-36 A, and reported Float throughout — long enough
        # for a controller that starts loads on Absorption or Float to open
        # one into a bank still swallowing 28 A.
        #
        # Entry therefore tests the two things that actually separate a float
        # from a ramp, neither of which is voltage: absorption must have
        # happened today, because a charger only ever floats AFTER it has
        # absorbed, and the bank must not be accepting current, on the same
        # tail threshold the absorption latch uses.
        #
        # Both tests gate ENTRY only — once float is established, staying there
        # is decided by voltage, as before. That asymmetry is deliberate: it
        # keeps a load-step current spike from knocking the stage out from
        # under _check_absorption_complete, whose tail hold has its own and
        # deliberately longer TAIL_BREAK_TOLERANCE_S for exactly those spikes.
        if float_setpoint is not None and abs(voltage - float_setpoint) <= tol:
            if self._stage == STAGE_FLOAT:
                return STAGE_FLOAT
            if (
                self._saw_absorption_today
                and charge_current <= self.config.tail_current_a
            ):
                return STAGE_FLOAT
        return STAGE_BULK

    def _commit_stage(self, stage: str, now_monotonic: float) -> None:
        """Adopt a new stage only once it has persisted past the debounce."""
        if stage == self._stage:
            self._pending_stage = None
            self._pending_since = None
            if self._stage_since is None:
                self._stage_since = now_monotonic
            return
        # Explicit None check, not truthiness: a monotonic clock legitimately
        # reads 0.0, and treating that as "unset" would wedge the timer.
        if stage != self._pending_stage or self._pending_since is None:
            self._pending_stage = stage
            self._pending_since = now_monotonic
            return
        if now_monotonic - self._pending_since < STAGE_DEBOUNCE_S:
            return
        self._stage = stage
        # Credit the hold from when the stage actually began, not from now.
        self._stage_since = self._pending_since
        self._pending_stage = None
        self._pending_since = None

    def _integrate(self, net_a: float, interval: float) -> None:
        if self._ah is None:
            return
        self._ah = max(
            0.0, min(self.config.capacity_ah, self._ah + net_a * interval / 3600.0)
        )

    def _check_absorption_complete(self, charge_current: float, now_monotonic: float) -> None:
        cfg = self.config
        # Path two, and it is checked first because it needs nothing from the
        # tail logic below: enough cumulative time at the setpoint IS an
        # absorption, whether or not acceptance was ever observed to fall to
        # the tail. This is the path that keeps days_since_absorption honest on
        # days when a large load or the sky denies the tail hold. It deliberately
        # does NOT re-zero the SOC counter — 2 h of voltage-limited charging
        # says the bank had its absorption, not that it reached 100%.
        if (
            not self._absorption_done_today
            and self._absorption_seconds_today >= cfg.absorption_satisfied_s
        ):
            self._mark_absorption_satisfied(
                f"{self._absorption_seconds_today / 60:.0f} min cumulative at the setpoint"
            )
        # The tail path keeps running even once the day is satisfied, because
        # only it earns the counter re-zero, and that calibration is worth more
        # than the day counter it shares a latch with.
        if self._recalibrated_today:
            return
        # Either plateau can end absorption — the inverter sometimes drops to
        # float before the taper at bulk has flattened — but BOTH paths require
        # acceptance to have actually fallen to the tail.
        #
        # Voltage alone proves nothing here. The reference unit oscillates
        # between its float and bulk setpoints under load, and a sag passing
        # through the float band was enough to latch a false completion while
        # the bank was still taking 20 A. Current is the only
        # honest signal that a bank is full; the plateau just says which
        # regulation point it is sitting on.
        at_plateau = self._stage == STAGE_ABSORPTION or (
            self._stage == STAGE_FLOAT and self._saw_absorption_today
        )
        if not at_plateau:
            # Tolerated and credited back exactly like a current excursion,
            # on the longer PLATEAU_BREAK_TOLERANCE_S. The old code reset here
            # unconditionally, so one Bulk or Discharging sample threw away
            # however many minutes of hold had accumulated — and under a large
            # load that sample arrives every one to five minutes, so the
            # hold never matured. See the constant.
            self._note_hold_break(now_monotonic, PLATEAU_BREAK_TOLERANCE_S)
            return
        if charge_current > cfg.tail_current_a:
            # Tolerate a brief excursion; only a sustained one means the bank
            # has genuinely started accepting current again.
            self._note_hold_break(now_monotonic, TAIL_BREAK_TOLERANCE_S)
            return
        if self._break_since is not None:
            # Credit the excursion back so the hold measures real time at tail.
            if self._tail_since is not None:
                self._tail_since += now_monotonic - self._break_since
            self._break_since = None
        if self._tail_since is None:
            self._tail_since = now_monotonic
        elif now_monotonic - self._tail_since >= cfg.absorption_hold_s:
            self._mark_absorption_complete(f"tail current held at {self._stage}")

    def _note_hold_break(self, now_monotonic: float, tolerance: float) -> None:
        """One excursion tracker, two tolerances.

        Deliberately a single field rather than one per break kind. A large
        load produces excursions that begin as a current rise and continue
        as an off-plateau drop, and with a tracker per kind both credited the
        same wall-clock seconds back to the hold — pushing _tail_since forward
        faster than time passed, so the latch could never mature. The tolerance
        applied is whatever the current sample calls for; the elapsed time is
        measured from when the excursion began, whichever kind started it.
        """
        if self._break_since is None:
            self._break_since = now_monotonic
        elif now_monotonic - self._break_since >= tolerance:
            self._tail_since = None
            self._break_since = None

    def _mark_absorption_satisfied(self, reason: str) -> None:
        """The bank has had its absorption today — day counter only.

        Kept apart from _mark_absorption_complete on purpose. The two facts
        were once latched together and they are not the same claim: "this bank
        has been properly recharged recently" (what days_since_absorption
        reports, and what a load controller would gate on) is much weaker than
        "the counter can be trusted to read 100% right now".
        """
        self._absorption_done_today = True
        self._days_since_absorption = 0
        self._tail_since = None
        self._break_since = None
        _LOGGER.info("Absorption satisfied for today (%s)", reason)

    def _mark_absorption_complete(self, reason: str) -> None:
        """Tail-current evidence: the bank is full, so also re-zero the counter."""
        self._mark_absorption_satisfied(reason)
        self._recalibrated_today = True
        self._ah = self.config.capacity_ah
        self._calibrated = True
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
        self._recalibrated_today = False
        self._saw_absorption_today = False
        self._absorption_seconds_today = 0.0
        self._tail_since = None
        self._break_since = None

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
    def net_power_w(self) -> float | None:
        """Signed battery power actually being integrated: + in, - out.

        Not the same as the inverter's own charge/discharge power sensors —
        the discharge side here is reconstructed. Comparing the two is the
        quickest way to see the ammeter shortfall.
        """
        return self._net_power_w

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
