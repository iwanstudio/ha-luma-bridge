"""Luma Bridge: a small local API so the Luma launcher can show HA tiles."""

from __future__ import annotations

import hmac

from aiohttp import web
from homeassistant.components.http import HomeAssistantView
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.typing import ConfigType

from .const import ACTION, DOMAIN


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Set up the integration schema. Config entries do the real work."""
    hass.data.setdefault(DOMAIN, {"entries": {}, "views": False})
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Register the TV endpoints once and remember this entry."""
    bucket = hass.data[DOMAIN]
    bucket["entries"][entry.entry_id] = entry
    if not bucket["views"]:
        hass.http.register_view(LumaTilesView())
        hass.http.register_view(LumaPressView())
        bucket["views"] = True
    entry.async_on_unload(entry.add_update_listener(async_reload_entry))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Drop the entry. The views stay, they just stop matching its token."""
    hass.data[DOMAIN]["entries"].pop(entry.entry_id, None)
    return True


async def async_reload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload when the chosen entities change."""
    await hass.config_entries.async_reload(entry.entry_id)


def _presented_token(request: web.Request) -> str:
    header = request.headers.get("Authorization", "")
    if header.lower().startswith("bearer "):
        return header[7:].strip()
    return request.headers.get("X-Luma-Token", "").strip()


def _entry_for_request(hass: HomeAssistant, request: web.Request) -> ConfigEntry | None:
    presented = _presented_token(request)
    if not presented:
        return None
    for entry in hass.data[DOMAIN]["entries"].values():
        expected = str(entry.data.get("token", ""))
        if expected and hmac.compare_digest(presented.encode(), expected.encode()):
            return entry
    return None


def _tile(hass: HomeAssistant, entity_id: str) -> dict[str, str]:
    domain = entity_id.split(".", 1)[0]
    state = hass.states.get(entity_id)
    name = entity_id
    current = "unavailable"
    if state is not None:
        name = str(state.attributes.get("friendly_name") or entity_id)
        current = state.state
    return {
        "entity_id": entity_id,
        "name": name,
        "domain": domain,
        "state": current,
        "action": ACTION.get(domain, "toggle"),
    }


class LumaTilesView(HomeAssistantView):
    """Current tiles for the TV. Only the entities chosen in this integration."""

    url = "/api/luma_bridge/tiles"
    name = "api:luma_bridge:tiles"
    requires_auth = False

    async def get(self, request: web.Request) -> web.Response:
        """Return the configured tiles and their current state."""
        hass: HomeAssistant = request.app["hass"]
        entry = _entry_for_request(hass, request)
        if entry is None:
            return self.json_message("unauthorized", status_code=401)
        entities = entry.options.get("entities", [])
        return self.json({"tiles": [_tile(hass, entity_id) for entity_id in entities]})


class LumaPressView(HomeAssistantView):
    """One press of a tile. The service depends on the entity domain."""

    url = "/api/luma_bridge/press"
    name = "api:luma_bridge:press"
    requires_auth = False

    async def post(self, request: web.Request) -> web.Response:
        """Run the tap action for one configured entity."""
        hass: HomeAssistant = request.app["hass"]
        entry = _entry_for_request(hass, request)
        if entry is None:
            return self.json_message("unauthorized", status_code=401)
        try:
            body = await request.json()
        except ValueError:
            return self.json_message("invalid JSON", status_code=400)
        entity_id = str(body.get("entity_id", ""))
        allowed = set(entry.options.get("entities", []))
        if entity_id not in allowed:
            return self.json_message("entity is not on the Luma board", status_code=404)
        domain = entity_id.split(".", 1)[0]
        service = ACTION.get(domain)
        if service is None:
            return self.json_message("domain is not supported", status_code=400)
        await hass.services.async_call(
            domain,
            service,
            {"entity_id": entity_id},
            blocking=False,
        )
        return self.json({"ok": True, "tile": _tile(hass, entity_id)})
