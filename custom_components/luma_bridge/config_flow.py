"""Config flow for Luma Bridge."""

from __future__ import annotations

import secrets
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry, ConfigFlow, OptionsFlow
from homeassistant.core import callback
from homeassistant.helpers.selector import selector

from .const import DOMAIN, DOMAINS


def _code() -> str:
    """A short code that can be typed on the TV keyboard."""
    raw = secrets.token_hex(8)
    return "-".join(raw[i : i + 4] for i in range(0, 16, 4))


def _entity_schema(default: list[str]) -> vol.Schema:
    return vol.Schema(
        {
            vol.Required("entities", default=default): selector(
                {
                    "entity": {
                        "multiple": True,
                        "domain": DOMAINS,
                    }
                }
            )
        }
    )


class LumaBridgeConfigFlow(ConfigFlow, domain=DOMAIN):
    """One bridge per Home Assistant. The TV stores only the address and the code."""

    VERSION = 1

    def __init__(self) -> None:
        self._entities: list[str] = []
        self._token = ""

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> Any:
        """Pick the entities that should appear on the TV."""
        if self._async_current_entries():
            return self.async_abort(reason="single_instance_allowed")
        if user_input is not None:
            self._entities = list(user_input["entities"])
            self._token = _code()
            return await self.async_step_token()
        return self.async_show_form(step_id="user", data_schema=_entity_schema([]))

    async def async_step_token(self, user_input: dict[str, Any] | None = None) -> Any:
        """Show the code once. It stays available later in the options."""
        if user_input is not None:
            return self.async_create_entry(
                title="Luma",
                data={"token": self._token},
                options={"entities": self._entities},
            )
        return self.async_show_form(
            step_id="token",
            data_schema=vol.Schema({}),
            description_placeholders={"token": self._token},
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> LumaBridgeOptionsFlow:
        """Allow changing the tile list without rotating the TV code."""
        return LumaBridgeOptionsFlow()


class LumaBridgeOptionsFlow(OptionsFlow):
    """Edit the entities. The code is shown again so it can be retyped."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> Any:
        """Save a new entity list."""
        if user_input is not None:
            return self.async_create_entry(data={"entities": list(user_input["entities"])})
        current = list(self.config_entry.options.get("entities", []))
        return self.async_show_form(
            step_id="init",
            data_schema=_entity_schema(current),
            description_placeholders={"token": str(self.config_entry.data.get("token", ""))},
        )
