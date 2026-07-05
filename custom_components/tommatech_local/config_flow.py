"""Config flow — asks for the collector dongle IP."""
from __future__ import annotations

import voluptuous as vol

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult

from .const import CONF_DEVADDR, CONF_HOST, DEFAULT_DEVADDR, DOMAIN


class TommatechLocalConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_user(self, user_input=None) -> ConfigFlowResult:
        if user_input is not None:
            await self.async_set_unique_id(f"{DOMAIN}_{user_input[CONF_HOST]}")
            self._abort_if_unique_id_configured()
            return self.async_create_entry(
                title=f"Tommatech Inverter ({user_input[CONF_HOST]})",
                data=user_input,
            )

        schema = vol.Schema(
            {
                vol.Required(CONF_HOST, default="10.0.0.34"): str,
                vol.Optional(CONF_DEVADDR, default=DEFAULT_DEVADDR): int,
            }
        )
        return self.async_show_form(step_id="user", data_schema=schema)
