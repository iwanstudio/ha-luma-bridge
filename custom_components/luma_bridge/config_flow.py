"""Config flow for Luma Bridge."""

from __future__ import annotations

import secrets
import time
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry, ConfigFlow, OptionsFlow
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.network import NoURLAvailableError, get_url
from homeassistant.helpers.selector import selector

from .const import DOMAIN, DOMAINS

NAME_LIMIT = 40


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


def _field_label(entity_id: str, friendly: str) -> str:
    """A form label without dots. The frontend treats dots as nested fields."""
    base = friendly.strip().replace(".", " ") if friendly.strip() else entity_id.replace(".", " ")
    ident = entity_id.replace(".", " ")
    if ident.casefold() in base.casefold():
        return base
    return f"{base} — {ident}"


def _names_schema(
    hass: HomeAssistant, entities: list[str], current: dict[str, str]
) -> tuple[vol.Schema, dict[str, str]]:
    """One text field per entity. The value is the name shown on the TV."""
    labels: dict[str, str] = {}
    used: set[str] = set()
    fields: dict[Any, Any] = {}
    for entity_id in entities:
        state = hass.states.get(entity_id)
        friendly = ""
        if state is not None:
            friendly = str(state.attributes.get("friendly_name") or "")
        label = _field_label(entity_id, friendly)
        if label in used:
            label = f"{label} {len(used) + 1}"
        used.add(label)
        labels[label] = entity_id
        saved = str(current.get(entity_id, "")).strip()
        fields[
            vol.Optional(label, description={"suggested_value": saved or friendly or entity_id})
        ] = selector({"text": {}})
    return vol.Schema(fields), labels


def _collect_names(user_input: dict[str, Any], labels: dict[str, str]) -> dict[str, str]:
    names: dict[str, str] = {}
    for label, entity_id in labels.items():
        value = str(user_input.get(label, "")).strip()
        if value:
            names[entity_id] = value[:NAME_LIMIT]
    return names


def _lan_url(hass: HomeAssistant) -> str:
    """Address the television can reach on the home network."""
    try:
        return get_url(
            hass,
            allow_external=False,
            allow_cloud=False,
            allow_ip=True,
        ).rstrip("/")
    except NoURLAvailableError:
        return str(getattr(hass.config, "internal_url", "") or "").rstrip("/")


def _https_url(hass: HomeAssistant) -> str:
    """Page for the phone. Camera access needs HTTPS when SSL is enabled."""
    try:
        return get_url(
            hass,
            require_ssl=True,
            allow_external=True,
            allow_cloud=True,
        ).rstrip("/")
    except NoURLAvailableError:
        external = str(getattr(hass.config, "external_url", "") or "").rstrip("/")
        if external.startswith("https://"):
            return external
        return ""


class LumaBridgeConfigFlow(ConfigFlow, domain=DOMAIN):
    """One bridge per Home Assistant. The TV stores only the address and the code."""

    VERSION = 1

    def __init__(self) -> None:
        self._entities: list[str] = []
        self._labels: dict[str, str] = {}
        self._names: dict[str, str] = {}
        self._token = ""

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> Any:
        """Pick the entities that should appear on the TV."""
        if self._async_current_entries():
            return self.async_abort(reason="single_instance_allowed")
        if user_input is not None:
            self._entities = list(user_input["entities"])
            if not self._entities:
                self._token = _code()
                return await self.async_step_token()
            return await self.async_step_names()
        return self.async_show_form(step_id="user", data_schema=_entity_schema([]))

    async def async_step_names(self, user_input: dict[str, Any] | None = None) -> Any:
        """Let each tile have the same name as on the wall tablet, not the system name."""
        if user_input is not None:
            self._names = _collect_names(user_input, self._labels)
            self._token = _code()
            return await self.async_step_token()
        schema, self._labels = _names_schema(self.hass, self._entities, {})
        return self.async_show_form(step_id="names", data_schema=schema)

    async def async_step_token(self, user_input: dict[str, Any] | None = None) -> Any:
        """Show the code once. It stays available later in the options."""
        if user_input is not None:
            return self.async_create_entry(
                title="Luma",
                data={"token": self._token},
                options={"entities": self._entities, "names": self._names},
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
    """Edit the entities and their TV names. The code stays the same."""

    def __init__(self) -> None:
        super().__init__()
        self._entities: list[str] = []
        self._labels: dict[str, str] = {}

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> Any:
        """Choose pairing or the tile list."""
        return self.async_show_menu(step_id="init", menu_options=["pair", "tiles", "doorbell"])

    async def async_step_pair(self, user_input: dict[str, Any] | None = None) -> Any:
        """Open the phone scanner. The TV shows the QR code."""
        if user_input is not None:
            if user_input.get("paired"):
                return self.async_abort(reason="paired")
            return self.async_abort(reason="pair_failed")
        session = secrets.token_urlsafe(18)
        lan = _lan_url(self.hass)
        page = _https_url(self.hass) or lan
        self.hass.data[DOMAIN].setdefault("scans", {})[session] = {
            "flow_id": self.flow_id,
            "entry_id": self.config_entry.entry_id,
            "ha_url": lan or page,
            "expires": time.monotonic() + 300,
        }
        return self.async_external_step(
            step_id="pair",
            url=f"{page}/api/luma_bridge/scan?session={session}",
        )

    async def async_step_tiles(self, user_input: dict[str, Any] | None = None) -> Any:
        """Choose the entities, then their names on the next screen."""
        if user_input is not None:
            self._entities = list(user_input["entities"])
            if not self._entities:
                return self.async_create_entry(data={"entities": [], "names": {}})
            return await self.async_step_names()
        current = list(self.config_entry.options.get("entities", []))
        return self.async_show_form(
            step_id="tiles",
            data_schema=_entity_schema(current),
            description_placeholders={"token": str(self.config_entry.data.get("token", ""))},
        )

    async def async_step_names(self, user_input: dict[str, Any] | None = None) -> Any:
        """Save a display name for every selected entity."""
        if user_input is not None:
            names = _collect_names(user_input, self._labels)
            return self.async_create_entry(data={"entities": self._entities, "names": names})
        current = dict(self.config_entry.options.get("names", {}))
        schema, self._labels = _names_schema(self.hass, self._entities, current)
        return self.async_show_form(step_id="names", data_schema=schema)

    async def async_step_doorbell(self, user_input: dict[str, Any] | None = None) -> Any:
        """Which sensor is the gate bell, and which camera should open on the TV."""
        if user_input is not None:
            options = dict(self.config_entry.options)
            options["doorbell"] = str(user_input.get("doorbell") or "")
            options["doorbell_camera"] = str(user_input.get("doorbell_camera") or "")
            return self.async_create_entry(data=options)
        bell = str(self.config_entry.options.get("doorbell") or "")
        camera = str(self.config_entry.options.get("doorbell_camera") or "")
        bell_field = vol.Optional("doorbell")
        camera_field = vol.Optional("doorbell_camera")
        if bell:
            bell_field = vol.Optional("doorbell", description={"suggested_value": bell})
        if camera:
            camera_field = vol.Optional("doorbell_camera", description={"suggested_value": camera})
        return self.async_show_form(
            step_id="doorbell",
            data_schema=vol.Schema(
                {
                    bell_field: selector(
                        {
                            "entity": {
                                "multiple": False,
                                "filter": [
                                    {"domain": "binary_sensor"},
                                    {"domain": "event"},
                                    {"domain": "button"},
                                    {"domain": "sensor"},
                                    {"domain": "switch"},
                                ],
                            }
                        }
                    ),
                    camera_field: selector({"entity": {"multiple": False, "domain": "camera"}}),
                }
            ),
        )
