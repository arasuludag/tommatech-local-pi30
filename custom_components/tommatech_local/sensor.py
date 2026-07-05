"""Live status, ratings, and energy sensors — full PI30 surface."""
from __future__ import annotations

from dataclasses import dataclass

from homeassistant.components.sensor import (
    SensorDeviceClass, SensorEntity, SensorEntityDescription, SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    PERCENTAGE, UnitOfApparentPower, UnitOfElectricCurrent,
    UnitOfElectricPotential, UnitOfEnergy, UnitOfFrequency, UnitOfPower,
    UnitOfTemperature,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import BATTERY_TYPE_MAP, DOMAIN, INPUT_RANGE_MAP, MODE_MAP
from .entity import InverterEntity

V = UnitOfElectricPotential.VOLT
A = UnitOfElectricCurrent.AMPERE
W = UnitOfPower.WATT
KWH = UnitOfEnergy.KILO_WATT_HOUR
DIAG = EntityCategory.DIAGNOSTIC
MEAS = SensorStateClass.MEASUREMENT


@dataclass(frozen=True, kw_only=True)
class InvSensor(SensorEntityDescription):
    bucket: str = "GS"       # coordinator.data sub-dict ("_root" = top level)
    source_key: str = ""


def _v(key, name, bucket, src, **kw):
    return InvSensor(key=key, name=name, bucket=bucket, source_key=src,
                     native_unit_of_measurement=V,
                     device_class=SensorDeviceClass.VOLTAGE,
                     state_class=MEAS, **kw)


SENSORS: tuple[InvSensor, ...] = (
    # --- battery ---
    _v("battery_voltage", "Battery Voltage", "GS", "battery_voltage"),
    InvSensor(key="battery_soc", name="Battery SOC", bucket="GS", source_key="battery_capacity",
              native_unit_of_measurement=PERCENTAGE, device_class=SensorDeviceClass.BATTERY,
              state_class=MEAS),
    InvSensor(key="battery_charge_current", name="Battery Charge Current", bucket="GS",
              source_key="battery_charge_current", native_unit_of_measurement=A,
              device_class=SensorDeviceClass.CURRENT, state_class=MEAS),
    InvSensor(key="battery_discharge_current", name="Battery Discharge Current", bucket="GS",
              source_key="battery_discharge_current", native_unit_of_measurement=A,
              device_class=SensorDeviceClass.CURRENT, state_class=MEAS),
    InvSensor(key="battery_charge_power", name="Battery Charge Power", bucket="_derived",
              source_key="charge_power", native_unit_of_measurement=W,
              device_class=SensorDeviceClass.POWER, state_class=MEAS),
    InvSensor(key="battery_discharge_power", name="Battery Discharge Power", bucket="_derived",
              source_key="discharge_power", native_unit_of_measurement=W,
              device_class=SensorDeviceClass.POWER, state_class=MEAS),
    _v("battery_voltage_scc", "Battery Voltage SCC", "GS", "battery_voltage_scc",
       entity_category=DIAG, entity_registry_enabled_default=False),
    # --- AC output ---
    InvSensor(key="ac_output_power", name="AC Output Power", bucket="GS",
              source_key="ac_output_active_power", native_unit_of_measurement=W,
              device_class=SensorDeviceClass.POWER, state_class=MEAS),
    InvSensor(key="ac_output_apparent_power", name="AC Output Apparent Power", bucket="GS",
              source_key="ac_output_apparent_power",
              native_unit_of_measurement=UnitOfApparentPower.VOLT_AMPERE,
              device_class=SensorDeviceClass.APPARENT_POWER, state_class=MEAS),
    _v("ac_output_voltage", "AC Output Voltage", "GS", "ac_output_voltage"),
    InvSensor(key="ac_output_frequency", name="AC Output Frequency", bucket="GS",
              source_key="ac_output_frequency", native_unit_of_measurement=UnitOfFrequency.HERTZ,
              device_class=SensorDeviceClass.FREQUENCY, state_class=MEAS),
    InvSensor(key="output_load_percent", name="Output Load", bucket="GS",
              source_key="output_load_percent", native_unit_of_measurement=PERCENTAGE,
              state_class=MEAS, icon="mdi:percent"),
    # --- grid (permanently 0 off-grid, still mapped) ---
    _v("grid_voltage", "Grid Voltage", "GS", "grid_voltage"),
    InvSensor(key="grid_frequency", name="Grid Frequency", bucket="GS",
              source_key="grid_frequency", native_unit_of_measurement=UnitOfFrequency.HERTZ,
              device_class=SensorDeviceClass.FREQUENCY, state_class=MEAS,
              entity_registry_enabled_default=False),
    # --- PV strings ---
    _v("pv1_voltage", "PV1 Voltage", "GS", "pv1_input_voltage", icon="mdi:solar-power"),
    InvSensor(key="pv1_current", name="PV1 Current", bucket="GS", source_key="pv1_input_current",
              native_unit_of_measurement=A, device_class=SensorDeviceClass.CURRENT,
              state_class=MEAS),
    InvSensor(key="pv1_power", name="PV1 Power", bucket="GS", source_key="pv1_charging_power",
              native_unit_of_measurement=W, device_class=SensorDeviceClass.POWER,
              state_class=MEAS, icon="mdi:solar-power"),
    _v("pv2_voltage", "PV2 Voltage", "GS2", "pv2_input_voltage", icon="mdi:solar-power"),
    InvSensor(key="pv2_current", name="PV2 Current", bucket="GS2", source_key="pv2_input_current",
              native_unit_of_measurement=A, device_class=SensorDeviceClass.CURRENT,
              state_class=MEAS),
    InvSensor(key="pv2_power", name="PV2 Power", bucket="GS2", source_key="pv2_charging_power",
              native_unit_of_measurement=W, device_class=SensorDeviceClass.POWER,
              state_class=MEAS, icon="mdi:solar-power"),
    InvSensor(key="pv_power_total", name="PV Power Total", bucket="_derived",
              source_key="pv_total", native_unit_of_measurement=W,
              device_class=SensorDeviceClass.POWER, state_class=MEAS, icon="mdi:solar-power"),
    # --- temperatures / internals ---
    InvSensor(key="heat_sink_temperature", name="Heat Sink Temperature", bucket="GS",
              source_key="heat_sink_temperature",
              native_unit_of_measurement=UnitOfTemperature.CELSIUS,
              device_class=SensorDeviceClass.TEMPERATURE, state_class=MEAS),
    _v("bus_voltage", "Bus Voltage", "GS", "bus_voltage", entity_category=DIAG),
    # --- setpoint read-backs (QPIRI) ---
    _v("bulk_voltage_setpoint", "Bulk Voltage Setpoint", "PIRI", "battery_bulk_voltage",
       entity_category=DIAG),
    _v("float_voltage_setpoint", "Float Voltage Setpoint", "PIRI", "battery_float_voltage",
       entity_category=DIAG),
    _v("cutoff_voltage_setpoint", "Cut-off Voltage Setpoint", "PIRI", "battery_under_voltage",
       entity_category=DIAG),
    _v("recharge_voltage_setpoint", "Back-to-Battery Voltage Setpoint", "PIRI",
       "battery_recharge_voltage", entity_category=DIAG),
    _v("redischarge_voltage_setpoint", "Back-to-Discharge Voltage Setpoint", "PIRI",
       "battery_redischarge_voltage", entity_category=DIAG),
    InvSensor(key="max_charging_current", name="Max Charging Current", bucket="PIRI",
              source_key="max_charging_current", native_unit_of_measurement=A,
              device_class=SensorDeviceClass.CURRENT, entity_category=DIAG),
    InvSensor(key="max_ac_charging_current", name="Max AC Charging Current", bucket="PIRI",
              source_key="max_ac_charging_current", native_unit_of_measurement=A,
              device_class=SensorDeviceClass.CURRENT, entity_category=DIAG,
              entity_registry_enabled_default=False),
    # --- energy counters (native, no Riemann needed) ---
    InvSensor(key="pv_energy_total", name="PV Energy Total", bucket="_root", source_key="ET_KWH",
              native_unit_of_measurement=KWH, device_class=SensorDeviceClass.ENERGY,
              state_class=SensorStateClass.TOTAL_INCREASING, icon="mdi:solar-power"),
    InvSensor(key="load_energy_total", name="Load Energy Total", bucket="_root", source_key="LT_KWH",
              native_unit_of_measurement=KWH, device_class=SensorDeviceClass.ENERGY,
              state_class=SensorStateClass.TOTAL_INCREASING),
    InvSensor(key="pv_energy_year", name="PV Energy This Year", bucket="_root", source_key="EY_KWH",
              native_unit_of_measurement=KWH, device_class=SensorDeviceClass.ENERGY,
              state_class=SensorStateClass.TOTAL_INCREASING, icon="mdi:solar-power",
              entity_category=DIAG),
    InvSensor(key="load_energy_year", name="Load Energy This Year", bucket="_root", source_key="LY_KWH",
              native_unit_of_measurement=KWH, device_class=SensorDeviceClass.ENERGY,
              state_class=SensorStateClass.TOTAL_INCREASING, entity_category=DIAG),
)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry,
                            add_entities: AddEntitiesCallback) -> None:
    coordinator = hass.data[DOMAIN][entry.entry_id]
    entities: list[SensorEntity] = [
        InverterSensor(coordinator, entry.entry_id, d) for d in SENSORS
    ]
    entities += [
        InverterTextSensor(coordinator, entry.entry_id, "operating_mode", "Operating Mode",
                           lambda d: MODE_MAP.get(d.get("MODE", ""), d.get("MODE")),
                           icon="mdi:state-machine"),
        InverterTextSensor(coordinator, entry.entry_id, "active_warnings", "Active Warnings",
                           lambda d: ", ".join(d.get("WARNINGS") or []) or "None",
                           icon="mdi:alert-circle-outline", category=DIAG),
        InverterTextSensor(coordinator, entry.entry_id, "battery_type", "Battery Type",
                           lambda d: _map_int(d, "PIRI", "battery_type", BATTERY_TYPE_MAP),
                           icon="mdi:battery-outline", category=DIAG),
        InverterTextSensor(coordinator, entry.entry_id, "input_voltage_range", "AC Input Range",
                           lambda d: _map_int(d, "PIRI", "input_voltage_range", INPUT_RANGE_MAP),
                           icon="mdi:sine-wave", category=DIAG, enabled_default=False),
        InverterTextSensor(coordinator, entry.entry_id, "firmware_version", "Firmware Version",
                           lambda d: d.get("firmware"), icon="mdi:chip", category=DIAG),
        InverterTextSensor(coordinator, entry.entry_id, "inverter_serial", "Inverter Serial",
                           lambda d: d.get("serial"), icon="mdi:identifier", category=DIAG,
                           enabled_default=False),
        InverterTextSensor(coordinator, entry.entry_id, "collector_pn", "Collector Serial",
                           lambda d: d.get("pn"), icon="mdi:wifi", category=DIAG,
                           enabled_default=False),
    ]
    add_entities(entities)


def _map_int(data: dict, bucket: str, key: str, mapping: dict):
    raw = data.get(bucket, {}).get(key)
    if raw is None:
        return None
    return mapping.get(int(raw), str(raw))


def _derived(data: dict, key: str):
    gs = data.get("GS", {})
    gs2 = data.get("GS2", {})
    v = gs.get("battery_voltage")
    if key == "charge_power":
        i = gs.get("battery_charge_current")
        return round(v * i, 1) if v is not None and i is not None else None
    if key == "discharge_power":
        i = gs.get("battery_discharge_current")
        return round(v * i, 1) if v is not None and i is not None else None
    if key == "pv_total":
        if "pv1_charging_power" not in gs:
            return None
        return round((gs.get("pv1_charging_power") or 0) + (gs2.get("pv2_charging_power") or 0), 1)
    return None


class InverterSensor(InverterEntity, SensorEntity):
    entity_description: InvSensor

    def __init__(self, coordinator, entry_id, description: InvSensor) -> None:
        super().__init__(coordinator, entry_id)
        self.entity_description = description
        self._attr_unique_id = f"{entry_id}_{description.key}"

    @property
    def native_value(self):
        d = self.entity_description
        data = self.coordinator.data
        if d.bucket == "_derived":
            return _derived(data, d.source_key)
        if d.bucket == "_root":
            return data.get(d.source_key)
        return data.get(d.bucket, {}).get(d.source_key)


class InverterTextSensor(InverterEntity, SensorEntity):
    def __init__(self, coordinator, entry_id, key, name, value_fn,
                 icon=None, category=None, enabled_default=True) -> None:
        super().__init__(coordinator, entry_id)
        self._attr_unique_id = f"{entry_id}_{key}"
        self._attr_name = name
        self._attr_icon = icon
        self._attr_entity_category = category
        self._attr_entity_registry_enabled_default = enabled_default
        self._value_fn = value_fn

    @property
    def native_value(self):
        return self._value_fn(self.coordinator.data)
