"""Luma Bridge: a small local API so the Luma launcher can show HA tiles."""

from __future__ import annotations

import hmac
import ipaddress
import json
import math
import time
from pathlib import Path

from aiohttp import ClientError, ClientTimeout, web
from homeassistant.components.http import HomeAssistantView
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.event import async_track_state_change_event
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
        hass.http.register_view(LumaSnapshotView())
        hass.http.register_view(LumaAlertView())
        bucket["views"] = True
    unsub = _listen_doorbell(hass, entry)
    if unsub is not None:
        entry.async_on_unload(unsub)
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


PLAIN_MODES = {"onoff", "brightness", "white"}


def _channels(value) -> list[float] | None:
    """Read a color attribute whether HA stored ints or floats."""
    if not isinstance(value, (list, tuple)) or len(value) < 2:
        return None
    try:
        return [float(part) for part in list(value)[:3]]
    except (TypeError, ValueError):
        return None


def _visible(rgb: list[int]) -> list[int]:
    """Keep the hue, but lift a very dark color so the button still shows it."""
    peak = max(rgb)
    if peak == 0 or peak >= 180:
        return rgb
    scale = 180 / peak
    return [min(255, int(round(channel * scale))) for channel in rgb]


def _hs_to_rgb(hs: list[float]) -> list[int]:
    hue = hs[0] % 360
    saturation = hs[1] / 100 if hs[1] > 1 else hs[1]
    saturation = max(0.0, min(1.0, saturation))
    sector = hue / 60
    chroma = saturation
    mix = chroma * (1 - abs(sector % 2 - 1))
    red = green = blue = 0.0
    if sector < 1:
        red, green = chroma, mix
    elif sector < 2:
        red, green = mix, chroma
    elif sector < 3:
        green, blue = chroma, mix
    elif sector < 4:
        green, blue = mix, chroma
    elif sector < 5:
        red, blue = mix, chroma
    else:
        red, blue = chroma, mix
    offset = 1 - chroma
    return [int(round((channel + offset) * 255)) for channel in (red, green, blue)]


def _xy_to_rgb(xy: list[float]) -> list[int] | None:
    x, y = xy[0], xy[1]
    if y <= 0.0001:
        return None
    big_y = 1.0
    big_x = big_y / y * x
    big_z = big_y / y * (1 - x - y)

    def correct(channel: float) -> int:
        channel = max(0.0, channel)
        if channel <= 0.0031308:
            channel = 12.92 * channel
        else:
            channel = 1.055 * (channel ** (1 / 2.4)) - 0.055
        return max(0, min(255, int(round(channel * 255))))

    return [
        correct(3.2406 * big_x - 1.5372 * big_y - 0.4986 * big_z),
        correct(-0.9689 * big_x + 1.8758 * big_y + 0.0415 * big_z),
        correct(0.0557 * big_x - 0.2040 * big_y + 1.0570 * big_z),
    ]


def _kelvin_to_rgb(kelvin: float) -> list[int]:
    temperature = max(1000.0, min(40000.0, kelvin)) / 100
    if temperature <= 66:
        red = 255.0
        green = 99.4708025861 * math.log(temperature) - 161.1195681661
    else:
        red = 329.698727446 * ((temperature - 60) ** -0.1332047592)
        green = 288.1221695283 * ((temperature - 60) ** -0.0755148492)
    if temperature >= 66:
        blue = 255.0
    elif temperature <= 19:
        blue = 0.0
    else:
        blue = 138.5177312231 * math.log(temperature - 10) - 305.0447927307
    return [max(0, min(255, int(round(channel)))) for channel in (red, green, blue)]


def _rgb(state) -> list[int] | None:
    """The color the bulb is showing. A plain lamp stays unset, so the TV uses yellow."""
    if state is None or state.state not in ("on", "open"):
        return None
    mode = str(state.attributes.get("color_mode") or "").casefold()
    if mode in PLAIN_MODES:
        return None
    rgb = _channels(state.attributes.get("rgb_color"))
    if rgb is not None and len(rgb) >= 3:
        return _visible([max(0, min(255, int(round(channel)))) for channel in rgb])
    hs = _channels(state.attributes.get("hs_color"))
    if hs is not None:
        return _visible(_hs_to_rgb(hs))
    xy = _channels(state.attributes.get("xy_color"))
    if xy is not None and len(xy) >= 2:
        converted = _xy_to_rgb(xy)
        if converted is not None:
            return _visible(converted)
    mired = state.attributes.get("color_temp")
    try:
        if mired is not None and mode in {"", "color_temp"}:
            return _visible(_kelvin_to_rgb(1_000_000 / float(mired)))
    except (TypeError, ValueError, ZeroDivisionError):
        return None
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
        if domain == "camera":
            return self.json({"ok": True, "tile": _tile(hass, entity_id, entry.options.get("names", {}))})
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


def _is_ring(old, new) -> bool:
    """A doorbell press, not the sensor going quiet again."""
    if new is None or new.state in ("unknown", "unavailable", ""):
        return False
    domain = new.entity_id.split(".", 1)[0]
    if domain == "binary_sensor":
        return new.state == "on" and (old is None or old.state != "on")
    return old is None or old.state != new.state


def _listen_doorbell(hass: HomeAssistant, entry: ConfigEntry):
    """Remember a ring so the television can open the gate camera."""
    doorbell = str(entry.options.get("doorbell") or "")
    camera = str(entry.options.get("doorbell_camera") or "")
    if not doorbell or not camera:
        return None

    async def _changed(event) -> None:
        new = event.data.get("new_state")
        old = event.data.get("old_state")
        if not _is_ring(old, new):
            return
        hass.data[DOMAIN]["ring"] = {
            "id": int(time.time() * 1000),
            "camera": camera,
            "title": "Ktoś dzwoni",
            "at": time.time(),
            "entry_id": entry.entry_id,
        }

    return async_track_state_change_event(hass, [doorbell], _changed)


def _allowed_camera(entry: ConfigEntry, entity_id: str) -> bool:
    if entity_id == str(entry.options.get("doorbell_camera") or ""):
        return True
    return entity_id in set(entry.options.get("entities", []))


class LumaSnapshotView(HomeAssistantView):
    """One camera frame for a tile or the live preview."""

    url = "/api/luma_bridge/snapshot"
    name = "api:luma_bridge:snapshot"
    requires_auth = False

    async def get(self, request: web.Request) -> web.Response:
        """Return the current JPEG from a camera the TV is allowed to see."""
        hass: HomeAssistant = request.app["hass"]
        entry = _entry_for_request(hass, request)
        if entry is None:
            return self.json_message("unauthorized", status_code=401)
        entity_id = request.query.get("entity_id", "")
        if not entity_id.startswith("camera.") or not _allowed_camera(entry, entity_id):
            return self.json_message("camera is not on the Luma board", status_code=404)
        image = await _camera_bytes(hass, entity_id)
        if image is None:
            return self.json_message("no image", status_code=404)
        body, content_type = image
        return web.Response(body=body, content_type=content_type)


async def _camera_bytes(hass: HomeAssistant, entity_id: str) -> tuple[bytes, str] | None:
    """Ask the camera for a still, then fall back to its own image method."""
    try:
        from homeassistant.components.camera import async_get_image

        image = await async_get_image(hass, entity_id, timeout=10)
        content = getattr(image, "content", None)
        if content:
            return content, getattr(image, "content_type", None) or "image/jpeg"
    except Exception:
        pass
    try:
        component = hass.data.get("camera")
        camera = getattr(component, "get_entity", lambda _entity: None)(entity_id)
        if camera is None:
            return None
        content = await camera.async_camera_image()
        if content:
            return content, "image/jpeg"
    except Exception:
        return None
    return None


class LumaAlertView(HomeAssistantView):
    """The latest doorbell ring, if it is still fresh."""

    url = "/api/luma_bridge/alert"
    name = "api:luma_bridge:alert"
    requires_auth = False

    async def get(self, request: web.Request) -> web.Response:
        """Return the gate camera while the ring is less than 90 seconds old."""
        hass: HomeAssistant = request.app["hass"]
        entry = _entry_for_request(hass, request)
        if entry is None:
            return self.json_message("unauthorized", status_code=401)
        ring = hass.data[DOMAIN].get("ring")
        fresh = (
            isinstance(ring, dict)
            and ring.get("entry_id") == entry.entry_id
            and time.time() - float(ring.get("at", 0)) < 90
        )
        if not fresh:
            return self.json({"id": 0, "camera": "", "title": ""})
        return self.json(
            {"id": ring["id"], "camera": ring["camera"], "title": ring["title"]}
        )
