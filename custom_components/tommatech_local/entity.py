"""Shared base entity: device grouping + push updates via dispatcher."""
from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import Entity

from .const import DOMAIN
from .coordinator import InverterCoordinator, SIGNAL_UPDATE


class InverterEntity(Entity):
    _attr_should_poll = False
    _attr_has_entity_name = True

    def __init__(self, coordinator: InverterCoordinator, entry_id: str) -> None:
        self.coordinator = coordinator
        self._entry_id = entry_id
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry_id)},
            name="Tommatech Inverter",
            manufacturer="Tommatech",
            model="Axpert/Voltronic PI30 (local)",
        )

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(
            async_dispatcher_connect(self.hass, SIGNAL_UPDATE, self.async_write_ha_state)
        )

    @property
    def available(self) -> bool:
        return self.coordinator.data.get("connected", False)
