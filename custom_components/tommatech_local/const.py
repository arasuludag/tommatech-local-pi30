"""Constants + PI30 field maps for the Tommatech Local integration."""
from __future__ import annotations

DOMAIN = "tommatech_local"

CONF_HOST = "host"          # dongle IP (for targeted UDP redirect)
CONF_DEVADDR = "devaddr"    # RS485 address to forward to (0xFF works)

DEFAULT_TCP_PORT = 8899
DEFAULT_UDP_PORT = 58899
DEFAULT_DEVADDR = 0xFF

# Poll cadence (seconds). QPIGS at 5s ~= 0.2 cmd/s on the RS485 bus;
# SolarAssistant drives the same protocol at ~1 cmd/s, so ample margin.
POLL_INTERVALS = {
    "QPIGS": 5,     # live status
    "QPIGS2": 10,   # PV2 string (dual-MPPT confirmed on this unit)
    "QMOD": 15,     # operating mode
    "QPIRI": 60,    # ratings + current setpoints
    "QPIWS": 30,    # warning/fault bits
    "QET": 300,     # lifetime generated energy
    "QLT": 300,     # lifetime load energy
    "QEY": 900,     # this-year generated energy (year appended at send)
    "QLY": 900,     # this-year load energy (year appended at send)
}

# Commands sent once per connection (static identity).
STARTUP_COMMANDS = ("QID", "QVFW", "QGMN")

# --- QPIGS field map: key -> index (tokens already carry decimals) ---
# Verified live against PN W0824353291671 (PI30 / devcode 0x0102).
QPIGS_FIELDS = {
    "grid_voltage": 0,
    "grid_frequency": 1,
    "ac_output_voltage": 2,
    "ac_output_frequency": 3,
    "ac_output_apparent_power": 4,
    "ac_output_active_power": 5,
    "output_load_percent": 6,
    "bus_voltage": 7,
    "battery_voltage": 8,
    "battery_charge_current": 9,
    "battery_capacity": 10,
    "heat_sink_temperature": 11,
    "pv1_input_current": 12,
    "pv1_input_voltage": 13,
    "battery_voltage_scc": 14,
    "battery_discharge_current": 15,
    # 16 = device status bits (handled separately)
    "battery_voltage_offset_fan": 17,
    "eeprom_version": 18,
    "pv1_charging_power": 19,
    # 20 = device status 2 (handled separately)
}

# QPIGS field 16, 8 bits b7..b0 (verified: sample 00010110 = load on,
# charging, SCC charging — matched actual state).
DEVICE_STATUS_BITS = {
    "load_on": 4,        # b4
    "charging": 2,       # b2
    "scc_charging": 1,   # b1
    "ac_charging": 0,    # b0
}

QPIGS2_FIELDS = {
    "pv2_input_current": 0,
    "pv2_input_voltage": 1,
    "pv2_charging_power": 2,
}

# --- QPIRI field map (ratings + live setpoints) ----------------------
QPIRI_FIELDS = {
    "grid_rating_voltage": 0,
    "grid_rating_current": 1,
    "ac_output_rating_voltage": 2,
    "ac_output_rating_frequency": 3,
    "ac_output_rating_current": 4,
    "ac_output_rating_apparent_power": 5,
    "ac_output_rating_active_power": 6,
    "battery_rating_voltage": 7,
    "battery_recharge_voltage": 8,     # PBCV
    "battery_under_voltage": 9,        # PSDV (cutoff)
    "battery_bulk_voltage": 10,        # PCVV
    "battery_float_voltage": 11,       # PBFT
    "battery_type": 12,
    "max_ac_charging_current": 13,
    "max_charging_current": 14,        # MCHGC
    "input_voltage_range": 15,
    "output_source_priority": 16,      # POP
    "charger_source_priority": 17,     # PCP
    "parallel_max_number": 18,
    "machine_type": 19,
    "topology": 20,
    "output_mode": 21,
    "battery_redischarge_voltage": 22, # PBDV
}

# QPIWS warning bits (standard PI30 order, index within the bit string).
QPIWS_BITS = {
    1: "Inverter fault",
    2: "Bus over-voltage",
    3: "Bus under-voltage",
    4: "Bus soft fail",
    5: "Line fail (no grid)",
    6: "Output short",
    7: "Inverter voltage too low",
    8: "Inverter voltage too high",
    9: "Over temperature",
    10: "Fan locked",
    11: "Battery voltage high",
    12: "Battery low alarm",
    14: "Battery under shutdown",
    16: "Overload",
    17: "EEPROM fault",
    18: "Inverter over-current",
    19: "Inverter soft fail",
    20: "Self test fail",
    21: "OP DC voltage over",
    22: "Battery open",
    23: "Current sensor fail",
    24: "Battery short",
    25: "Power limit",
    26: "PV voltage high",
    27: "MPPT overload fault",
    28: "MPPT overload warning",
    29: "Battery too low to charge",
}
# Bits that indicate a normal condition for an off-grid site, not a problem.
# Still shown in "Active Warnings" for context, but don't trip the Problem sensor.
QPIWS_INFORMATIONAL = {5}

# Bits this unit asserts persistently that are NOT real faults, and which we
# drop before they reach any entity. The vendor DESS cloud reads the same raw
# QPIWS and never reports these, and the inverter is fully functional (firmware
# reads, QPIRI setpoint read-backs, and set commands all succeed — a genuine
# EEPROM fault would break them). The true bit remains visible in the Problem
# sensor's `raw_qpiws` attribute for diagnostics.
QPIWS_SUPPRESSED = {17}  # "EEPROM fault" — chronic false positive on this PI30

MODE_MAP = {
    "P": "Power on", "S": "Standby", "L": "Line", "B": "Battery",
    "F": "Fault", "H": "Power saving", "D": "Shutdown",
}

OUTPUT_PRIORITY_MAP = {"00": "Utility first", "01": "Solar first", "02": "SBU priority"}
CHARGER_PRIORITY_MAP = {
    "00": "Utility first", "01": "Solar first",
    "02": "Solar + Utility", "03": "Solar only",
}
BATTERY_TYPE_MAP = {0: "AGM", 1: "Flooded", 2: "User-defined", 3: "Pylontech",
                    4: "Shinheung", 5: "WECO", 6: "Soltaro", 7: "LIb", 8: "LIC"}
INPUT_RANGE_MAP = {0: "Appliance", 1: "UPS"}
