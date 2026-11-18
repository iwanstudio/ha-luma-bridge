"""Runtime for the parental lock: counting, Home bounce and approval requests."""

from __future__ import annotations

import logging
import time
from datetime import timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers.event import (
    async_call_later,
    async_track_state_change_event,
    async_track_time_interval,
)
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from . import lock

TICK = 10
REQUEST_WAIT = 120
DENIED_SHOW = 15
PENDING_MAX = 600
LAUNCHERS = {"com.ecklf.luma", "com.webos.app.home"}
OFF_STATES = {"off", "unavailable", "unknown", None}
DEFAULT_PLAYER = "media_player.lg_webos_tv"
SCHEDULE = "schedule.tv_salon_czas_bez_telewizora"
ALLOWED = "input_boolean.tv_salon_zezwolenie"
KID_UNTIL = "input_datetime.tv_salon_przedluzenie_do"
MANUAL = "input_boolean.tv_salon_blokada_reczna"
DAILY = "input_number.tv_salon_limit_dzienny_h"
EXTRA_COUNT = 3


_LOGGER = logging.getLogger(__name__)


def _stamp(state) -> float:
    try:
        return float(state.attributes.get("timestamp") or 0) if state else 0.0
    except (TypeError, ValueError):
        return 0.0


def _hours(hass: HomeAssistant, entity_id: str) -> int:
    state = hass.states.get(entity_id)
    try:
        return max(0, int(float(state.state) * 3600)) if state else 0
    except (TypeError, ValueError):
        return 0


class LockManager:
    """One TV, one schedule. All rules live in lock.py; this only feeds them."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self.entry = entry
        self.player = str(entry.options.get("parental_media_player") or DEFAULT_PLAYER)
        self.notify = list(entry.options.get("parental_notify") or [])
        self.salt = bytes.fromhex(str(entry.options.get("parental_pin_salt") or ""))
        self.digest = str(entry.options.get("parental_pin_hash") or "")
        self.guard = lock.PinGuard()
        self.counter = lock.UsageCounter(self._today())
        self.parent_active = False
        self.bonus_s = 0
        self.denied_until = 0.0
        self.pending_since = 0.0
        self.last_request = 0.0
        self.last_bounce = 0.0
        self.last_signature: tuple | None = None
        self._notice = ""
        self._notice_until = 0.0
        self._ticks = 0
        self.store: Store = Store(hass, 1, f"luma_bridge.lock.{entry.entry_id}")
        self._unsubs: list = []
        self._debounce = None

    def _today(self) -> str:
        return dt_util.now().strftime("%Y-%m-%d")

    async def start(self) -> None:
        saved = await self.store.async_load() or {}
        self.counter = lock.UsageCounter.from_dict(saved.get("counter", {"day": self._today()}))
        self.parent_active = bool(saved.get("parent_active", False))
        self.bonus_s = int(saved.get("bonus", 0))
        self.counter.tick(self._today(), 0, None, False)
        if self.counter.day != saved.get("counter", {}).get("day"):
            self.bonus_s = 0
        self.denied_until = float(saved.get("denied_until", 0))
        self.pending_since = float(saved.get("pending_since", 0))
        self._unsubs += [
            async_track_time_interval(self.hass, self._tick, timedelta(seconds=TICK)),
            async_track_state_change_event(
                self.hass, [self.player, SCHEDULE, KID_UNTIL, ALLOWED, MANUAL], self._changed
            ),
            self.hass.bus.async_listen("mobile_app_notification_action", self._action),
            self.hass.bus.async_listen("luma_bridge_denied", self._denied),
        ]
        self._publish()

    async def stop(self) -> None:
        for unsub in self._unsubs:
            unsub()
        self._unsubs = []
        if self._debounce:
            self._debounce()
        await self.store.async_save(self._saved())

    def _saved(self) -> dict[str, Any]:
        return {
            "counter": self.counter.to_dict(),
            "parent_active": self.parent_active,
            "bonus": self.bonus_s,
            "denied_until": self.denied_until,
            "pending_since": self.pending_since,
        }

    def _extras(self) -> dict[str, str]:
        extras: dict[str, str] = {}
        for number in range(1, EXTRA_COUNT + 1):
            state = self.hass.states.get(f"input_select.tv_salon_aplikacja_dodatkowa_{number}")
            if state and state.state not in ("", "unknown", "unavailable", "-"):
                extras[state.state.strip().lower()] = state.state.strip()
        return extras

    def _window_end(self) -> float | None:
        state = self.hass.states.get(SCHEDULE)
        if not state or state.state != "on":
            return None
        end = state.attributes.get("next_event")
        return end.timestamp() if end is not None else None

    def _inputs(self, now: float) -> lock.LockInputs:
        hass = self.hass
        schedule = hass.states.get(SCHEDULE)
        allowed = hass.states.get(ALLOWED)
        manual = hass.states.get(MANUAL)
        kid = hass.states.get(KID_UNTIL)
        player = hass.states.get(self.player)
        extras = self._extras()
        limits = {
            key: _hours(hass, f"input_number.tv_salon_limit_{key}_h") for key in lock.NAMES
        }
        for number in range(1, EXTRA_COUNT + 1):
            name = hass.states.get(f"input_select.tv_salon_aplikacja_dodatkowa_{number}")
            if name and name.state.strip().lower() in extras:
                limits[extras[name.state.strip().lower()]] = _hours(
                    hass, f"input_number.tv_salon_limit_dodatkowej_{number}_h"
                )
        source = player.attributes.get("source") if player else None
        return lock.LockInputs(
            window_on=bool(schedule and schedule.state == "on"),
            now=now,
            window_ends=self._window_end(),
            allowed=bool(allowed and allowed.state == "on"),
            kid_until=float((kid.attributes.get("timestamp") if kid else 0) or 0),
            parent_until=float("inf") if self.parent_active else 0.0,
            daily_limit_s=(self._daily_limit() + self.bonus_s) if self._daily_limit() else 0,
            daily_used_s=self.counter.total,
            app_limits_s=limits,
            app_used_s=dict(self.counter.apps),
            current_app=lock.app_key(source, extras),
            tv_on=bool(player and player.state not in OFF_STATES),
            tz=dt_util.DEFAULT_TIME_ZONE,
            manual_lock=bool(manual and manual.state == "on"),
        )

    def _daily_limit(self) -> int:
        limit = _hours(self.hass, DAILY)
        return limit

    def _evaluate(self, now: float) -> tuple[lock.LockInputs, lock.LockResult]:
        inputs = self._inputs(now)
        return inputs, lock.evaluate(inputs)

    def state(self) -> dict[str, Any]:
        now = time.time()
        inputs, result = self._evaluate(now)
        current = lock.NAMES.get(inputs.current_app, inputs.current_app) if inputs.current_app else ""
        notice = ""
        if current and current in result.blocked_apps:
            notice = f"{current}: limit na dziś wyczerpany"
        elif self._notice_until > now:
            notice = self._notice
        pending = (
            result.locked
            and self.pending_since > 0
            and now - self.pending_since < PENDING_MAX
        )
        return {
            "locked": result.locked,
            "reason": result.reason,
            "until": result.until,
            "pending": pending,
            "denied": self.denied_until > now,
            "blocked_apps": result.blocked_apps,
            "notice": notice,
            "pin_length": int(self.entry.options.get("parental_pin_length") or 0),
        }

    async def unlock(self, pin: str) -> dict[str, Any]:
        now = time.time()
        wait = self.guard.check(now)
        if wait:
            return {"ok": False, "retry_after": wait}
        valid = bool(self.digest) and await self.hass.async_add_executor_job(
            lock.verify_pin, pin, self.salt, self.digest
        )
        if not valid:
            self.guard.fail(now)
            return {"ok": False, "retry_after": self.guard.check(now)}
        self.guard.ok()
        self.parent_active = self._window_end() is not None
        await self._lift_manual()
        self.pending_since = 0.0
        await self.store.async_save(self._saved())
        self._publish()
        return {"ok": True, "retry_after": 0}

    async def request(self) -> dict[str, Any]:
        now = time.time()
        remaining = int(self.last_request + REQUEST_WAIT - now)
        if remaining > 0:
            return {"ok": False, "wait": remaining}
        if not any(item.startswith("notify.") for item in self.notify):
            return {"ok": False, "wait": 0}
        self.last_request = now
        self.pending_since = now
        self.denied_until = 0.0
        reason = self.state()["reason"] or "czas bez telewizora"
        for service in self.notify:
            domain, _, name = service.partition(".")
            if domain != "notify" or not name:
                continue
            await self.hass.services.async_call(
                "notify",
                name,
                {
                    "title": "Salon: prośba o czas",
                    "message": f"Telewizor w salonie jest zablokowany ({reason}). "
                    "Dotknij, by wybrać, ile dodać.",
                    "data": {
                        "tag": "luma_prosba_salon",
                        "group": "tv-prosby",
                        "ttl": 0,
                        "priority": "high",
                        "importance": "high",
                        "channel": "Prośby o czas TV",
                        "actions": [
                            {"action": "LUMA_GRANT_30", "title": "+30 min"},
                            {"action": "LUMA_GRANT_60", "title": "+1 h"},
                            {"action": "LUMA_DENY", "title": "Odrzuć", "destructive": True},
                        ],
                    },
                },
                blocking=False,
            )
        return {"ok": True, "wait": REQUEST_WAIT}

    @callback
    def _denied(self, event: Event) -> None:
        self.denied_until = time.time() + DENIED_SHOW
        self.pending_since = 0.0

    async def _action(self, event: Event) -> None:
        action = str(event.data.get("action") or "")
        if not action.startswith("LUMA_") or not self.pending_since:
            return
        if time.time() - self.pending_since > PENDING_MAX:
            self.pending_since = 0.0
            return
        if action.startswith("LUMA_GRANT_"):
            try:
                minutes = int(action.rsplit("_", 1)[1])
            except ValueError:
                return
            if not 1 <= minutes <= 240:
                return
            self.pending_since = 0.0
            await self.hass.services.async_call(
                "script",
                "turn_on",
                {
                    "entity_id": "script.tv_salon_przyznaj_czas",
                    "variables": {"minutes": minutes},
                },
            )
        elif action == "LUMA_DENY":
            self.pending_since = 0.0
            await self.hass.services.async_call(
                "script", "turn_on", {"entity_id": "script.tv_salon_odrzuc_prosbe"}
            )

    @callback
    def _changed(self, event: Event) -> None:
        if event.data.get("entity_id") == KID_UNTIL:
            old, new = event.data.get("old_state"), event.data.get("new_state")
            self._grant_bonus(old, new)
            self.hass.async_create_task(self._on_grant(old, new))
        if self._debounce:
            self._debounce()
        self._debounce = async_call_later(self.hass, 2, self._enforce_later)

    async def _lift_manual(self) -> None:
        state = self.hass.states.get(MANUAL)
        if state and state.state == "on":
            await self.hass.services.async_call(
                "input_boolean", "turn_off", {"entity_id": MANUAL}
            )

    async def _toast(self, text: str) -> None:
        try:
            await self.hass.services.async_call(
                "webostv",
                "command",
                {
                    "entity_id": self.player,
                    "command": "system.notifications/createToast",
                    "payload": {"message": text},
                },
                blocking=False,
            )
        except Exception:  # a missing toast must never break the lock
            _LOGGER.warning("Could not show the TV message", exc_info=True)

    async def _on_grant(self, old, new) -> None:
        now = time.time()
        new_ts = _stamp(new)
        minutes = lock.grant_minutes(_stamp(old), new_ts, now)
        if new_ts <= now or minutes < 1:
            return
        await self._lift_manual()
        player = self.hass.states.get(self.player)
        if player and player.state not in OFF_STATES:
            await self._toast(lock.grant_text(minutes, new_ts, dt_util.DEFAULT_TIME_ZONE))

    def _grant_bonus(self, old, new) -> None:
        def stamp(state) -> float:
            try:
                return float(state.attributes.get("timestamp") or 0) if state else 0.0
            except (TypeError, ValueError):
                return 0.0

        limit = self._daily_limit()
        exhausted = limit > 0 and self.counter.total >= limit + self.bonus_s
        self.bonus_s += lock.bonus_for_grant(stamp(old), stamp(new), time.time(), exhausted)

    async def _enforce_later(self, _now) -> None:
        self._debounce = None
        await self._enforce()
        self._publish()

    async def _tick(self, _now) -> None:
        now = time.time()
        today = self._today()
        if today != self.counter.day:
            self.bonus_s = 0
        inputs, result = self._evaluate(now)
        if not inputs.window_on:
            self.parent_active = False
        player = self.hass.states.get(self.player)
        on_launcher = bool(player and player.attributes.get("app_id") in LAUNCHERS)
        counting = (
            inputs.tv_on
            and inputs.window_on
            and result.kid_mode
            and not result.locked
            and not on_launcher
        )
        self.counter.tick(today, TICK, inputs.current_app, counting)
        self._ticks += 1
        if self._ticks % 3 == 0:
            await self.store.async_save(self._saved())
        await self._enforce()
        self._publish()

    async def _enforce(self) -> None:
        now = time.time()
        inputs, result = self._evaluate(now)
        if not inputs.tv_on:
            return
        player = self.hass.states.get(self.player)
        if player is None or player.attributes.get("app_id") in LAUNCHERS:
            return
        current = lock.NAMES.get(inputs.current_app, inputs.current_app) if inputs.current_app else ""
        if not (result.locked or (current and current in result.blocked_apps)):
            return
        signature = (player.attributes.get("app_id"), player.attributes.get("source"))
        if signature == self.last_signature and now - self.last_bounce < 60:
            return
        self.last_signature = signature
        self.last_bounce = now
        if not result.locked and current:
            self._notice = f"{current}: limit na dziś wyczerpany"
            self._notice_until = now + 20
        await self.hass.services.async_call(
            "webostv",
            "button",
            {"entity_id": self.player, "button": "HOME"},
            blocking=False,
        )

    def _publish(self) -> None:
        now = time.time()
        inputs, result = self._evaluate(now)
        states = self.hass.states
        states.async_set(
            "binary_sensor.luma_salon_zablokowany",
            "on" if result.locked else "off",
            {
                "friendly_name": "Salon: telewizor zablokowany",
                "reason": result.reason,
                "blocked_apps": result.blocked_apps,
                "kid_mode": result.kid_mode,
            },
        )
        states.async_set(
            "sensor.luma_salon_czas_dzis",
            round(self.counter.total / 60, 1),
            {"friendly_name": "Salon: czas dziś", "unit_of_measurement": "min"},
        )
        for key, name in lock.NAMES.items():
            states.async_set(
                f"sensor.luma_salon_czas_{key}",
                round(self.counter.apps.get(key, 0) / 60, 1),
                {"friendly_name": f"Salon: {name} dziś", "unit_of_measurement": "min"},
            )
        for number in range(1, EXTRA_COUNT + 1):
            state = self.hass.states.get(f"input_select.tv_salon_aplikacja_dodatkowa_{number}")
            name = state.state.strip() if state else ""
            states.async_set(
                f"sensor.luma_salon_czas_dodatkowej_{number}",
                round(self.counter.apps.get(name, 0) / 60, 1),
                {"friendly_name": f"Salon: dodatkowa {number} dziś", "unit_of_measurement": "min"},
            )
