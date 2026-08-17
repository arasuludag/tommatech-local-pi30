"""Config flow — collector dongle IP, plus battery-tracking options."""
from __future__ import annotations

import voluptuous as vol

from homeassistant.config_entries import (
    ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow,
)
from homeassistant.core import callback

from .const import (
    CONF_ABSORPTION_HOLD_MIN, CONF_BANK_CAPACITY_AH, CONF_CHARGE_EFFICIENCY,
    CONF_DEVADDR, CONF_HOST, CONF_PINNED_HOLD_MIN, CONF_PLATEAU_TOLERANCE_V,
    CONF_TAIL_CURRENT_FRACTION, DEFAULT_ABSORPTION_HOLD_MIN,
    DEFAULT_BANK_CAPACITY_AH, DEFAULT_CHARGE_EFFICIENCY, DEFAULT_DEVADDR,
    DEFAULT_PINNED_HOLD_MIN, DEFAULT_PLATEAU_TOLERANCE_V,
    DEFAULT_TAIL_CURRENT_FRACTION, DOMAIN,
)


class TommatechLocalConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return TommatechLocalOptionsFlow()

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


class TommatechLocalOptionsFlow(OptionsFlow):
    """Tunables for the coulomb counter and charge-stage detection.

    Applied live — changing them does not drop the collector session. See
    battery.py for what each one gates.
    """

    async def async_step_init(self, user_input=None) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(data=user_input)

        options = self.config_entry.options
        schema = vol.Schema(
            {
                vol.Optional(
                    CONF_BANK_CAPACITY_AH,
                    default=options.get(CONF_BANK_CAPACITY_AH, DEFAULT_BANK_CAPACITY_AH),
                ): vol.All(vol.Coerce(float), vol.Range(min=10, max=5000)),
                vol.Optional(
                    CONF_CHARGE_EFFICIENCY,
                    default=options.get(CONF_CHARGE_EFFICIENCY, DEFAULT_CHARGE_EFFICIENCY),
                ): vol.All(vol.Coerce(float), vol.Range(min=0.5, max=1.0)),
                vol.Optional(
                    CONF_TAIL_CURRENT_FRACTION,
                    default=options.get(
                        CONF_TAIL_CURRENT_FRACTION, DEFAULT_TAIL_CURRENT_FRACTION
                    ),
                ): vol.All(vol.Coerce(float), vol.Range(min=0.002, max=0.2)),
                vol.Optional(
                    CONF_PLATEAU_TOLERANCE_V,
                    default=options.get(
                        CONF_PLATEAU_TOLERANCE_V, DEFAULT_PLATEAU_TOLERANCE_V
                    ),
                ): vol.All(vol.Coerce(float), vol.Range(min=0.05, max=1.0)),
                vol.Optional(
                    CONF_ABSORPTION_HOLD_MIN,
                    default=options.get(
                        CONF_ABSORPTION_HOLD_MIN, DEFAULT_ABSORPTION_HOLD_MIN
                    ),
                ): vol.All(vol.Coerce(int), vol.Range(min=1, max=120)),
                vol.Optional(
                    CONF_PINNED_HOLD_MIN,
                    default=options.get(CONF_PINNED_HOLD_MIN, DEFAULT_PINNED_HOLD_MIN),
                ): vol.All(vol.Coerce(int), vol.Range(min=1, max=120)),
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)
