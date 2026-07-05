"""Connectivity, charging-state, and fault binary sensors."""
from __future__ import annotations

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass, BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import warnings_excluding_informational
from .entity import InverterEntity

DIAG = EntityCategory.DIAGNOSTIC


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry,
                            add_entities: AddEntitiesCallback) -> None:
    coordinator = hass.data[DOMAIN][entry.entry_id]
    eid = entry.entry_id
    add_entities([
        InverterOnline(coordinator, eid),
        InverterFault(coordinator, eid),
        StatusBit(coordinator, eid, "load_on", "Load On", icon="mdi:power-plug"),
        StatusBit(coordinator, eid, "charging", "Charging",
                  device_class=BinarySensorDeviceClass.BATTERY_CHARGING),
        StatusBit(coordinator, eid, "scc_charging", "Solar Charging", icon="mdi:solar-power"),
        StatusBit(coordinator, eid, "ac_charging", "AC Charging", icon="mdi:transmission-tower",
                  enabled_default=False),
    ])


class InverterOnline(InverterEntity, BinarySensorEntity):
    _attr_name = "Online"
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY

    def __init__(self, coordinator, entry_id) -> None:
        super().__init__(coordinator, entry_id)
        self._attr_unique_id = f"{entry_id}_online"

    @property
    def available(self) -> bool:  # meaningful even while disconnected
        return True

    @property
    def is_on(self) -> bool:
        return self.coordinator.data.get("connected", False)


class InverterFault(InverterEntity, BinarySensorEntity):
    """On when any non-informational warning/fault bit is set.

    'Line fail' is excluded — this site is off-grid, so absent AC input is
    the permanent normal condition, not a fault.
    """

    _attr_name = "Problem"
    _attr_device_class = BinarySensorDeviceClass.PROBLEM
    _attr_entity_category = DIAG

    def __init__(self, coordinator, entry_id) -> None:
        super().__init__(coordinator, entry_id)
        self._attr_unique_id = f"{entry_id}_fault"

    @property
    def is_on(self):
        warnings = self.coordinator.data.get("WARNINGS")
        if warnings is None:
            return None
        return len(warnings_excluding_informational(warnings)) > 0

    @property
    def extra_state_attributes(self):
        return {
            "active": self.coordinator.data.get("WARNINGS"),
            "raw_qpiws": self.coordinator.data.get("WARN_RAW"),
        }


class StatusBit(InverterEntity, BinarySensorEntity):
    def __init__(self, coordinator, entry_id, bit_key, name,
                 device_class=None, icon=None, enabled_default=True) -> None:
        super().__init__(coordinator, entry_id)
        self._attr_unique_id = f"{entry_id}_{bit_key}"
        self._attr_name = name
        self._attr_device_class = device_class
        self._attr_icon = icon
        self._attr_entity_registry_enabled_default = enabled_default
        self._bit_key = bit_key

    @property
    def is_on(self):
        bits = self.coordinator.data.get("GS", {}).get("status_bits")
        if not bits:
            return None
        return bits.get(self._bit_key)
