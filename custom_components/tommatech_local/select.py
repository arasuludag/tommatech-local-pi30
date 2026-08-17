"""Source-priority selects and the max AC (generator) charging current."""
from __future__ import annotations

import logging

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    AC_CHARGE_CURRENT_FALLBACK, CHARGER_PRIORITY_MAP, DOMAIN,
    OUTPUT_PRIORITY_MAP,
)
from .entity import InverterEntity

_LOGGER = logging.getLogger(__name__)

# Both spellings of the "set max utility charging current" command are seen in
# the wild: with a leading parallel-machine digit, and without. Probed in this
# order on first use; whichever ACKs is remembered for the session.
COMMAND_FORMS = ("MUCHGC0{value:02d}", "MUCHGC{value:02d}")


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry,
                            add_entities: AddEntitiesCallback) -> None:
    coordinator = hass.data[DOMAIN][entry.entry_id]
    add_entities([
        PrioritySelect(coordinator, entry.entry_id, "output_priority", "Output Source Priority",
                       "output_source_priority", "POP", OUTPUT_PRIORITY_MAP),
        PrioritySelect(coordinator, entry.entry_id, "charger_priority", "Charger Source Priority",
                       "charger_source_priority", "PCP", CHARGER_PRIORITY_MAP),
        MaxACChargeCurrentSelect(coordinator, entry.entry_id),
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


class MaxACChargeCurrentSelect(InverterEntity, SelectEntity):
    """Cap on charge current drawn from the AC input — i.e. the generator.

    Off-grid this only bites while a genset is running, and the right value
    depends on which set is connected: too high stalls or overloads a small
    one, too low wastes runtime. Hence a control rather than a fixed setting.

    Options are discovered from the inverter (QMUCHGCR at connect) rather than
    hard-coded, because the accepted ladder varies by model and firmware. If
    the unit doesn't answer, AC_CHARGE_CURRENT_FALLBACK is offered instead.

    Two command spellings exist in the wild — `MUCHGC` with a leading parallel
    machine digit (`MUCHGC030`) and without (`MUCHGC30`). We try the documented
    3-digit form, fall back to the 2-digit form if the inverter NAKs, and
    remember whichever the unit accepted.
    """

    _attr_name = "Max AC Charging Current"
    _attr_icon = "mdi:engine"

    def __init__(self, coordinator, entry_id) -> None:
        super().__init__(coordinator, entry_id)
        self._attr_unique_id = f"{entry_id}_max_ac_charging_current"
        self._command_form: str | None = None

    def _live_value(self) -> int | None:
        raw = self.coordinator.data.get("PIRI", {}).get("max_ac_charging_current")
        return None if raw is None else int(raw)

    def _values(self) -> list[int]:
        discovered = self.coordinator.data.get("AC_CHARGE_CURRENTS")
        values = set(discovered or AC_CHARGE_CURRENT_FALLBACK)
        # The live value can sit outside the advertised ladder — set from the
        # inverter's own panel, or a firmware that under-reports. Fold it in so
        # current_option is always a member of options, which HA requires.
        live = self._live_value()
        if live is not None:
            values.add(live)
        return sorted(values)

    @property
    def options(self) -> list[str]:
        return [f"{v} A" for v in self._values()]

    @property
    def current_option(self) -> str | None:
        live = self._live_value()
        return None if live is None else f"{live} A"

    async def async_select_option(self, option: str) -> None:
        try:
            value = int(option.split()[0])
        except (ValueError, IndexError):
            _LOGGER.warning("Unparseable AC charge current option: %r", option)
            return

        forms = (self._command_form,) if self._command_form else COMMAND_FORMS
        for form in forms:
            if await self.coordinator.async_set_command(form.format(value=value)):
                self._command_form = form
                return

        # A remembered form that stops working (firmware change) must not pin
        # us to it forever — forget it so the next attempt re-probes both.
        self._command_form = None
        _LOGGER.error(
            "Inverter rejected max AC charging current %d A (tried: %s)",
            value, ", ".join(f.format(value=value) for f in forms),
        )
