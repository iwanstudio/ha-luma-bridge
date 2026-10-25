"""Luma Bridge: a small local API so the Luma launcher can show HA tiles."""

from __future__ import annotations

import hmac
import ipaddress
import json
import time
from pathlib import Path

from aiohttp import ClientError, ClientTimeout, web
from homeassistant.components.http import HomeAssistantView
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.typing import ConfigType

from .const import ACTION, DOMAIN

HERE = Path(__file__).parent
SCAN_HTML = (HERE / "scan.html").read_text(encoding="utf-8")
JSQR = (HERE / "jsqr.min.js").read_bytes()


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Set up the integration schema. Config entries do the real work."""
    hass.data.setdefault(DOMAIN, {"entries": {}, "views": False, "scans": {}})
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Register the TV endpoints once and remember this entry."""
    bucket = hass.data[DOMAIN]
    bucket["entries"][entry.entry_id] = entry
    if not bucket["views"]:
        hass.http.register_view(LumaTilesView())
        hass.http.register_view(LumaPressView())
        hass.http.register_view(LumaScanView())
        hass.http.register_view(LumaAdoptView())
        hass.http.register_view(LumaScriptView())
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


COLOR_MODES = {"hs", "xy", "rgb", "rgbw", "rgbww"}


def _rgb(state) -> list[int] | None:
    """The color a bulb is showing, not a leftover value from when it was off."""
    if state is None or state.state not in ("on", "open"):
        return None
    if str(state.attributes.get("color_mode") or "") not in COLOR_MODES:
        return None
    rgb = state.attributes.get("rgb_color")
    if not isinstance(rgb, (list, tuple)) or len(rgb) < 3:
        return None
    try:
        return [max(0, min(255, int(channel))) for channel in list(rgb)[:3]]
    except (TypeError, ValueError):
        return None


def _tile(hass: HomeAssistant, entity_id: str, names: dict[str, str]) -> dict:
    domain = entity_id.split(".", 1)[0]
    state = hass.states.get(entity_id)
    custom = str(names.get(entity_id, "")).strip()
    name = custom or entity_id
    current = "unavailable"
    if state is not None:
        if not custom:
            name = str(state.attributes.get("friendly_name") or entity_id)
        current = state.state
    tile = {
        "entity_id": entity_id,
        "name": name,
        "domain": domain,
        "state": current,
        "action": ACTION.get(domain, "toggle"),
    }
    rgb = _rgb(state)
    if rgb is not None:
        tile["rgb"] = rgb
    return tile


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
        names = entry.options.get("names", {})
        return self.json({"tiles": [_tile(hass, entity_id, names) for entity_id in entities]})


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
            blocking=True,
        )
        return self.json({"ok": True, "tile": _tile(hass, entity_id, entry.options.get("names", {}))})


def _private_host(host: str) -> bool:
    """Only talk to an address on the home network."""
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return ip.is_private and not ip.is_loopback and not ip.is_unspecified


def _scan(hass: HomeAssistant, session: str) -> dict | None:
    pending = hass.data[DOMAIN]["scans"].get(session)
    if not pending or pending["expires"] < time.monotonic():
        hass.data[DOMAIN]["scans"].pop(session, None)
        return None
    return pending


class LumaScanView(HomeAssistantView):
    """Phone page that reads the QR code shown on the TV."""

    url = "/api/luma_bridge/scan"
    name = "api:luma_bridge:scan"
    requires_auth = False

    async def get(self, request: web.Request) -> web.Response:
        """Serve the scanner. The session comes from the integration options."""
        hass: HomeAssistant = request.app["hass"]
        session = request.query.get("session", "")
        pending = _scan(hass, session)
        if pending is None:
            return web.Response(
                text="Ten kod wygasł. W Home Assistant otwórz parowanie jeszcze raz.",
                content_type="text/plain",
                charset="utf-8",
            )
        blob = json.dumps({"session": session, "url": pending["ha_url"]}).replace("<", "\\u003c")
        return web.Response(
            text=SCAN_HTML.replace("__CONFIG__", blob),
            content_type="text/html",
            charset="utf-8",
        )


class LumaScriptView(HomeAssistantView):
    """QR decoder used when the phone has no BarcodeDetector."""

    url = "/api/luma_bridge/jsqr.min.js"
    name = "api:luma_bridge:jsqr"
    requires_auth = False

    async def get(self, request: web.Request) -> web.Response:
        """Return the bundled decoder."""
        return web.Response(body=JSQR, content_type="text/javascript", charset="utf-8")


class LumaAdoptView(HomeAssistantView):
    """Push this Home Assistant's address and code to the TV that showed the QR."""

    url = "/api/luma_bridge/adopt"
    name = "api:luma_bridge:adopt"
    requires_auth = False

    async def post(self, request: web.Request) -> web.Response:
        """Call the TV, then close the pairing step."""
        hass: HomeAssistant = request.app["hass"]
        try:
            body = await request.json()
        except ValueError:
            return self.json_message("Nieczytelne dane", status_code=400)
        session = str(body.get("session", ""))
        pending = _scan(hass, session)
        if pending is None:
            return self.json_message("Kod wygasł. Otwórz parowanie jeszcze raz.", status_code=403)
        host = str(body.get("host", "")).strip()
        try:
            port = int(body.get("port", 0))
        except (TypeError, ValueError):
            port = 0
        secret = str(body.get("secret", "")).strip()
        ha_url = str(body.get("url") or pending["ha_url"]).strip().rstrip("/")
        if not _private_host(host) or not 1024 <= port <= 65535 or not secret:
            return self.json_message("Kod z telewizora jest nieprawidłowy.", status_code=400)
        if not (ha_url.startswith("http://") or ha_url.startswith("https://")) or " " in ha_url:
            return self.json_message("Adres Home Assistant jest nieprawidłowy.", status_code=400)
        entry = hass.data[DOMAIN]["entries"].get(pending["entry_id"])
        if entry is None:
            return self.json_message("Integracja nie jest już skonfigurowana.", status_code=404)
        payload = {"secret": secret, "url": ha_url, "token": str(entry.data.get("token", ""))}
        client = async_get_clientsession(hass)
        try:
            async with client.post(
                f"http://{host}:{port}/pair",
                json=payload,
                timeout=ClientTimeout(total=4),
                allow_redirects=False,
            ) as response:
                if response.status != 200:
                    return self.json_message("Telewizor odrzucił połączenie.", status_code=502)
                await response.read()
        except (ClientError, TimeoutError):
            return self.json_message(
                "Nie ma połączenia z telewizorem. Telefon i telewizor muszą być w tej samej sieci.",
                status_code=502,
            )
        hass.data[DOMAIN]["scans"].pop(session, None)
        try:
            await hass.config_entries.options.async_configure(
                pending["flow_id"], {"paired": True}
            )
        except Exception:
            return self.json({"ok": True, "dialog": False})
        return self.json({"ok": True})
