# Tommatech Local (PI30)

Fully local Home Assistant integration for Tommatech / DESS / Axpert-class
(Voltronic PI30) solar inverters that carry an EyBond WiFi collector dongle —
no cloud, no dessmonitor.com.

## How it works

The collector dongle never serves data; it dials **out** to whichever server a
UDP broadcast names (the same mechanism the SmartESS app uses for its local
mode). This integration:

1. Broadcasts `set>server=<HA_IP>:8899;` to the dongle on UDP 58899.
2. Accepts the dongle's TCP connection on port 8899.
3. Speaks the EyBond transport (heartbeat FC=1, Forward2Device FC=4) and
   relays classic Voltronic **PI30 Q-protocol** commands (`QPIGS`, `QPIRI`,
   `QMOD`, `QPIWS`, `QET`, ...) to the inverter.

The redirect is transient: if HA stops announcing (host down), the dongle
falls back to its configured cloud server by itself — automatic failover.

## Entities

- Live status every 5 s: battery V/A/SOC, charge/discharge power (derived),
  AC output W/VA/V/Hz, load %, PV1+PV2 V/A/W, PV total, heat sink temp,
  bus voltage, operating mode, device status bits (load on / charging /
  solar charging / AC charging).
- Setpoint read-backs (QPIRI, 60 s): bulk, float, cutoff, back-to-battery,
  back-to-discharge voltages; max charging currents; battery type.
- Energy counters (native, kWh): PV total + this year, load total + this year.
- Warnings: decoded QPIWS bit map, `Problem` binary sensor (ignores
  "Line fail" — normal for off-grid sites).
- Writable: bulk/float/cutoff/recharge/re-discharge voltage (`number.*`),
  output & charger source priority, max AC charging current (`select.*`).
  Commands ACK/NAK-checked.
- Derived battery state (see **Battery tracking** below): counted SOC,
  charge stage, absorption-complete flag, voltage-pinned flag.

## Battery tracking

A bank with no BMS gets no trustworthy SOC from the inverter — PI30's
`battery_capacity` is inferred from terminal voltage, and terminal voltage
under load is dominated by IR drop. `battery.py` derives better numbers by
integrating the charge/discharge current the inverter *does* report accurately,
then re-zeroing that integral at the one moment the true state is known for
free: the end of absorption, when the bank is full by definition. That bounds
drift to one solar cycle only on days that reach it. See **Drift between
re-zeros** for the days that don't.

### Which ammeter is believed

On the reference unit (see **Notes**) the two current readings are not equally
trustworthy, which changes what the counter integrates. Other PI30 units may
behave differently; check yours the same way before trusting either.

**Charge current is usable, but not net.** On the reference unit, PV minus load
minus the charge reading closes to within a few tens of watts at every power
level, from dawn to a 3.4 kW load at noon. No inverter runs on nothing, so the
reading must be derived from PV and load rather than measured at the bank, and
it includes the inverter's own consumption. Booked as stored charge, that ran
the counter about 13 Ah high per sunny day (measured by
`last_calibration_error_ah`). So while PV and load readings are available, the
counter integrates

    battery_in = (charge_current - inverter_idle_w / battery_voltage) * charge_efficiency

MPPT and conversion losses are not subtracted, because nothing pins them down
at high power; expect a smaller residual drift, still optimistic. The
absorption tail test keeps the raw reading, because its threshold was set
against it.

**Discharge current is not usable.** Measured overnight, when PV is zero and
the bank must supply the entire load plus conversion losses, it reported 192 W
against a 312 W load, 38 W against 139 W, and 0.6 W against 105 W — it degrades
with load and reads flat zero below roughly 120 W. Over one night it accounted
for 883 Wh of an approximately 1950 Wh draw. Integrating that would drift the
counter several points optimistic every night, which is backwards for a reserve
decision.

So discharge is reconstructed from the energy balance instead:

    battery_out = ac_output_power / inverter_efficiency + inverter_idle_w - pv_power

Where the reconstruction and the ammeter disagree, the larger drain wins —
underestimating the drain is the dangerous direction. `inverter_efficiency` and
`inverter_idle_w` are the only fitted parameters in the counter; trim them
against `last_calibration_error_ah` at each re-zero.

The one exception is **Line mode** (QMOD `L`): with the grid, a generator or a
vehicle's V2L outlet on the AC input, the inverter passes that input straight
through to the load, so the balance describes the AC source rather than the
bank. There the reconstruction is skipped and only the ammeters count; AC
charging still shows up on the charge ammeter like PV charging does, and
nothing is taken off it, because the AC input powers the inverter too.

The stock `battery_discharge_power` sensor is left untouched — it reports what
the inverter claims. Compare it against `battery_net_power` to see the gap.

| Entity | What it means |
| --- | --- |
| `sensor.*_battery_soc_counted` | Coulomb-counted SOC. Reads 100 % only after a re-zero today (see **Drift between re-zeros**). The `calibrated` attribute is `false` until the first absorption re-zero — before that it is an integral started from the inverter's guess — and never resets afterwards. `last_calibrated` is the date of the most recent re-zero, `overshoot_ah` the charge refused at the 99 % ceiling since then, and `last_calibration_error_ah` how far the counter had drifted when last re-zeroed (positive = it had been reading high). |
| `sensor.*_battery_net_power` | Signed battery power actually being integrated (+ in, - out), with discharge reconstructed as above. |
| `sensor.*_battery_charge_stage` | Bulk / Absorption / Float / Discharging / Idle, classified from voltage against the live setpoints plus current direction. |
| `binary_sensor.*_absorption_complete_today` | The bank has had its absorption today, by either path below. Latches until local midnight. |
| `binary_sensor.*_battery_voltage_pinned` | The bank has held a CV plateau long enough to trust it, so it is voltage-limited rather than current-limited — the MPPT is throttling and there is unharvested headroom. |
| `sensor.*_days_since_absorption` | Whole days that have *ended* without an absorption. `0` means it absorbed today or yesterday. Meant for gating opportunistic loads, e.g. holding off EV charging once it reaches `2`. |
| `sensor.*_absorption_minutes_today` | Time spent on the bulk plateau today. |

Absorption is marked complete when charge current falls below the tail
threshold (default C/50, i.e. 8 A on a 400 Ah bank) and holds there for 15
minutes while the bank sits on a regulation plateau. Either plateau qualifies —
the inverter sometimes drops to float before the taper at bulk has flattened —
but **both paths require the tail current**. Voltage alone is not evidence: the
reference unit oscillates between its float and bulk setpoints under load, and an earlier
voltage-only float fallback latched a false completion while the bank was still
taking 20 A. This is the path that also **re-zeroes the SOC counter to 100 %**,
because only tail current proves the bank is actually full.

A second path marks the *day* satisfied without touching the counter:
**2 hours cumulative at the absorption setpoint** (`absorption_satisfied_s`).
The two claims are different — "this bank has been properly recharged recently"
is much weaker than "the counter reads 100 % right now" — and latching them
together cost the weaker one. The tail hold can be denied all day by a choppy
sky or a large load stealing the taper: an hour or more at the setpoint, no
tail hold, and the day counted as missed on a bank that was nearly full.

The reference unit does not hold its setpoint tightly; it wanders roughly ±0.3 V and
occasionally overshoots. Three things absorb that: the plateau tolerance
defaults to 0.35 V, stage changes are debounced for 30 s, and an excursion —
either off the plateau (180 s) or above the tail current (90 s) — is credited
back to the hold rather than resetting it, so the hold measures real
at-plateau-at-tail seconds.

Tunables live in the integration's **Configure** dialog (bank capacity, charge
efficiency, tail-current fraction, plateau tolerance, hold times) and apply
live without dropping the collector session. Defaults describe the reference
bank (48 V, 400 Ah lead-acid gel), so set at least the capacity to match yours.

### Drift between re-zeros

Short or overcast days and charging cut off early (a generator stopped while
the bank still takes a heavy current, say) never reach the tail. With no
re-zero, the counter's error compounds. On the reference system it grew by a
few points a day, always optimistic, for six days in a row, until the counter
read 83 % on a bank at roughly half charge. Gel makes this easy to miss:
it reaches the absorption setpoint well before it is full, so neither the
voltage nor the counter looks wrong while it happens.

Two things keep that visible:

- **The counter stops at 99 % until tail current proves the bank full that
  day.** An integral that reaches capacity by itself proves only its own
  arithmetic, and a bare 100 % looked exactly like a finished charge.
- **Charge refused at that ceiling is kept** (`overshoot_ah`). At the next
  re-zero the bank holds exactly its capacity, so the counter's contents plus
  the refused charge, minus capacity, is how far it had drifted. That figure is
  published as `last_calibration_error_ah`. It is the measurement to trim
  `charge_efficiency`, `inverter_efficiency` and `inverter_idle_w` against,
  instead of guessing from overnight voltage.

Until a re-zero has happened recently (`last_calibrated`), treat the counter as
an estimate that is probably high.

State survives restarts via HA's storage helper; the first sample after a
restart is deliberately not integrated, so downtime can't invent charge.

## Adapting to your system

Nothing about a particular site is hard-coded beyond the defaults above. On a
different PI30 unit:

- Enter your collector dongle's IP when adding the integration (your router's
  DHCP list will have it). The dongle must be able to reach Home Assistant on
  TCP 8899, and Home Assistant must reach the dongle on UDP 58899.
- Set the bank capacity under **Configure** and review the other tunables.
  Trim `inverter_efficiency` and `inverter_idle_w` against
  `last_calibration_error_ah` at each re-zero.
- Check your charge reading the same way before relying on it: average PV
  minus AC output minus battery charge power over an hour of steady charging.
  A residual near zero means the reading is derived from PV and load, as on the
  reference unit, and the counter corrects for it. A residual of roughly your
  idle draw plus a few percent of throughput means yours is measured at the
  bank; the counter will then read low by about the idle draw over each
  charging day, and `last_calibration_error_ah` will come out negative.
- `QPIWS_SUPPRESSED` in `const.py` drops one warning bit (EEPROM fault) that
  the reference unit asserts permanently. If yours doesn't, remove it so a real
  fault is not hidden.
- The `Problem` sensor treats "Line fail" as informational, which suits an
  off-grid site. On a grid-tied site, watch `Operating Mode` or `Grid Voltage`
  for outages instead.

## Tests

`battery.py` has no Home Assistant imports, so its logic runs standalone:

```
python3 tests/test_battery.py     # or: pytest tests/
```

## Notes

- Poll cadence is defined in `const.py` (`POLL_INTERVALS`). The PI30 serial
  bus comfortably handles ~1 cmd/s; defaults stay well under that.
- Reference unit: Tommatech 7.2 kW 48 V (devcode 0x0102 heartbeat,
  dess devcode 2449) on an EyBond collector, protocol `PI30`, firmware
  `VERFW:00069.02`. Measurements quoted in this README and the code comments
  come from it.
- Protocol framing was reverse-engineered/validated live; see
  `protocol.py` docstring for the wire format.
- The max AC charging current select discovers its options from the inverter
  (`QMUCHGCR` at connect) rather than hard-coding a ladder, and probes both
  known spellings of the set command (`MUCHGC030` / `MUCHGC30`), keeping
  whichever ACKs. Verified on the reference unit: a change reads back via
  QPIRI and the AC charge current follows it.
- Inspired by [ubombi/ha-smartess-local](https://github.com/ubombi/ha-smartess-local)
  (P17 variant of the same transport).
