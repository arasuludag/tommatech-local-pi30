"""Writable voltage setpoints (state read back from QPIRI)."""
from __future__ import annotations

from dataclasses import dataclass

from homeassistant.components.number import (
    NumberDeviceClass, NumberEntity, NumberEntityDescription, NumberMode,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfElectricPotential
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .entity import InverterEntity

V = UnitOfElectricPotential.VOLT


@dataclass(frozen=True, kw_only=True)
class InvNumber(NumberEntityDescription):
    piri_key: str = ""       # QPIRI read-back field
    set_prefix: str = ""     # Voltronic set command prefix (PCVV etc.)


# fmt: off
NUMBERS: tuple[InvNumber, ...] = (
    InvNumber(key="bulk_voltage", name="Bulk Charging Voltage", piri_key="battery_bulk_voltage",
              set_prefix="PCVV", native_min_value=48.0, native_max_value=58.4, native_step=0.1),
    InvNumber(key="float_voltage", name="Float Charging Voltage", piri_key="battery_float_voltage",
              set_prefix="PBFT", native_min_value=48.0, native_max_value=58.4, native_step=0.1),
    InvNumber(key="cutoff_voltage", name="Battery Cut-off Voltage", piri_key="battery_under_voltage",
              set_prefix="PSDV", native_min_value=40.0, native_max_value=48.0, native_step=0.1),
    InvNumber(key="recharge_voltage", name="Back-to-Battery Voltage", piri_key="battery_recharge_voltage",
              set_prefix="PBCV", native_min_value=44.0, native_max_value=51.0, native_step=0.1),
    InvNumber(key="redischarge_voltage", name="Back-to-Discharge Voltage", piri_key="battery_redischarge_voltage",
              set_prefix="PBDV", native_min_value=0.0, native_max_value=58.4, native_step=0.1),
)
# fmt: on


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry,
                            add_entities: AddEntitiesCallback) -> None:
    coordinator = hass.data[DOMAIN][entry.entry_id]
    add_entities(InverterNumber(coordinator, entry.entry_id, d) for d in NUMBERS)


class InverterNumber(InverterEntity, NumberEntity):
    entity_description: InvNumber
    _attr_device_class = NumberDeviceClass.VOLTAGE
    _attr_native_unit_of_measurement = V
    _attr_mode = NumberMode.BOX

    def __init__(self, coordinator, entry_id, description: InvNumber) -> None:
        super().__init__(coordinator, entry_id)
        self.entity_description = description
        self._attr_unique_id = f"{entry_id}_{description.key}"

    @property
    def native_value(self):
        return self.coordinator.data.get("PIRI", {}).get(self.entity_description.piri_key)

    async def async_set_native_value(self, value: float) -> None:
        # Voltronic format: two integer digits, dot, one decimal (e.g. PCVV57.6)
        cmd = f"{self.entity_description.set_prefix}{value:04.1f}"
        await self.coordinator.async_set_command(cmd)
