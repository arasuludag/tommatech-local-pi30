"""Tommatech Local (PI30 over EyBond WiFi collector)."""
from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant

from .const import CONF_DEVADDR, CONF_HOST, DEFAULT_DEVADDR, DOMAIN
from .coordinator import InverterCoordinator

PLATFORMS = [Platform.SENSOR, Platform.BINARY_SENSOR, Platform.NUMBER, Platform.SELECT]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    coordinator = InverterCoordinator(
        hass,
        host=entry.data[CONF_HOST],
        devaddr=entry.data.get(CONF_DEVADDR, DEFAULT_DEVADDR),
        entry_id=entry.entry_id,
        options=entry.options,
    )
    await coordinator.async_start()
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
    entry.async_on_unload(entry.add_update_listener(async_options_updated))
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_options_updated(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Apply changed options without tearing down the collector session."""
    coordinator: InverterCoordinator = hass.data[DOMAIN][entry.entry_id]
    coordinator.apply_options(entry.options)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        coordinator: InverterCoordinator = hass.data[DOMAIN].pop(entry.entry_id)
        await coordinator.async_stop()
    return unloaded
