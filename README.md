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
then re-zeroing that integral once a day at the one moment the true state is
known for free: the end of absorption, when the bank is full by definition.
Drift therefore never has to survive longer than one solar cycle.

| Entity | What it means |
| --- | --- |
| `sensor.*_battery_soc_counted` | Coulomb-counted SOC. The `calibrated` attribute is `false` until the first absorption re-zero — before that it is an integral started from the inverter's guess. |
| `sensor.*_battery_charge_stage` | Bulk / Absorption / Float / Discharging / Idle, classified from voltage against the live setpoints plus current direction. |
| `binary_sensor.*_absorption_complete_today` | The bank reached full today. Latches until local midnight. |
| `binary_sensor.*_battery_voltage_pinned` | The bank has held a CV plateau long enough to trust it, so it is voltage-limited rather than current-limited — the MPPT is throttling and there is unharvested headroom. |
| `sensor.*_days_since_absorption` | Whole days that have *ended* without a completed absorption. `0` means it absorbed today or yesterday. |
| `sensor.*_absorption_minutes_today` | Time spent on the bulk plateau today. |

Absorption is marked complete when charge current falls below the tail
threshold (default C/50) and holds there at the plateau — or, as a fallback,
when the inverter drops to float of its own accord after a spell at the bulk
plateau, since cloud can interrupt the taper before it flattens.

Tunables live in the integration's **Configure** dialog (bank capacity, charge
efficiency, tail-current fraction, plateau tolerance, hold times) and apply
live without dropping the collector session. Defaults describe 4S2P OUTDO
OT200-12(GEL): 48 V, 400 Ah.

State survives restarts via HA's storage helper; the first sample after a
restart is deliberately not integrated, so downtime can't invent charge.

## Tests

`battery.py` has no Home Assistant imports, so its logic runs standalone:

```
python3 tests/test_battery.py     # or: pytest tests/
```

## Notes

- Poll cadence is defined in `const.py` (`POLL_INTERVALS`). The PI30 serial
  bus comfortably handles ~1 cmd/s; defaults stay well under that.
- Verified against: Tommatech 7.2 kW 48 V (devcode 0x0102 heartbeat,
  dess devcode 2449), collector PN `W08243532916xx`, protocol `PI30`,
  firmware `VERFW:00069.02`.
- Protocol framing was reverse-engineered/validated live; see
  `protocol.py` docstring for the wire format.
- The max AC charging current select discovers its options from the inverter
  (`QMUCHGCR` at connect) rather than hard-coding a ladder, and probes both
  known spellings of the set command (`MUCHGC030` / `MUCHGC30`), keeping
  whichever ACKs. **Not yet verified on this unit** — check the entity picks
  up sane options and that a change reads back in `Max AC Charging Current`.
- Inspired by [ubombi/ha-smartess-local](https://github.com/ubombi/ha-smartess-local)
  (P17 variant of the same transport).
