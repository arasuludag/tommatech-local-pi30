"""Output/charger source priority selects."""
from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import CHARGER_PRIORITY_MAP, DOMAIN, OUTPUT_PRIORITY_MAP
from .entity import InverterEntity


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry,
                            add_entities: AddEntitiesCallback) -> None:
    coordinator = hass.data[DOMAIN][entry.entry_id]
    add_entities([
        PrioritySelect(coordinator, entry.entry_id, "output_priority", "Output Source Priority",
                       "output_source_priority", "POP", OUTPUT_PRIORITY_MAP),
        PrioritySelect(coordinator, entry.entry_id, "charger_priority", "Charger Source Priority",
                       "charger_source_priority", "PCP", CHARGER_PRIORITY_MAP),
    ])


class PrioritySelect(InverterEntity, SelectEntity):
    def __init__(self, coordinator, entry_id, key, name, piri_key, set_prefix, code_map) -> None:
        super().__init__(coordinator, entry_id)
        self._attr_unique_id = f"{entry_id}_{key}"
        self._attr_name = name
        self._piri_key = piri_key
        self._set_prefix = set_prefix
        self._code_map = code_map                       # "00" -> label
        self._label_to_code = {v: k for k, v in code_map.items()}
        self._attr_options = list(code_map.values())

    @property
    def current_option(self):
        raw = self.coordinator.data.get("PIRI", {}).get(self._piri_key)
        if raw is None:
            return None
        code = f"{int(raw):02d}"
        return self._code_map.get(code)

    async def async_select_option(self, option: str) -> None:
        code = self._label_to_code.get(option)
        if code is not None:
            await self.coordinator.async_set_command(f"{self._set_prefix}{code}")
