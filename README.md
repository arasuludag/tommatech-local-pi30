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
  output & charger source priority (`select.*`). Commands ACK/NAK-checked.

## Notes

- Poll cadence is defined in `const.py` (`POLL_INTERVALS`). The PI30 serial
  bus comfortably handles ~1 cmd/s; defaults stay well under that.
- Verified against: Tommatech 7.2 kW 48 V (devcode 0x0102 heartbeat,
  dess devcode 2449), collector PN `W08243532916xx`, protocol `PI30`,
  firmware `VERFW:00069.02`.
- Protocol framing was reverse-engineered/validated live; see
  `protocol.py` docstring for the wire format.
- Inspired by [ubombi/ha-smartess-local](https://github.com/ubombi/ha-smartess-local)
  (P17 variant of the same transport).
