"""Parental lock rules for the living-room TV. No Home Assistant imports."""

from __future__ import annotations

import hashlib
import hmac
import math
import os
from dataclasses import dataclass, field
from datetime import datetime, tzinfo

PIN_MAX = 8
PIN_ROUNDS = 100_000
MAX_LOCKOUT = 3600

APP_KEYS = {
    "youtube": "youtube",
    "netflix": "netflix",
    "prime video": "prime_video",
    "amazon prime video": "prime_video",
    "disney+": "disney",
    "disney plus": "disney",
    "hbo max": "hbo_max",
    "max": "hbo_max",
}

NAMES = {
    "youtube": "YouTube",
    "netflix": "Netflix",
    "prime_video": "Prime Video",
    "disney": "Disney+",
    "hbo_max": "HBO Max",
}


def new_salt() -> bytes:
    return os.urandom(16)


def hash_pin(pin: str, salt: bytes) -> str:
    return hashlib.pbkdf2_hmac("sha256", pin.encode(), salt, PIN_ROUNDS).hex()


def _valid_pin(pin: str) -> bool:
    return bool(pin) and len(pin) <= PIN_MAX and pin.isascii() and pin.isdigit()


def verify_pin(pin: str, salt: bytes, digest: str) -> bool:
    if not _valid_pin(pin):
        return False
    return hmac.compare_digest(hash_pin(pin, salt), digest)


class PinGuard:
    """Pause PIN tries after repeated failures."""

    def __init__(self, max_fails: int = 3, lockout: int = 60) -> None:
        self.max_fails = max_fails
        self.lockout = lockout
        self.fails = 0
        self.until = 0.0
        self.streak = 0

    def check(self, now: float) -> int:
        return max(0, math.ceil(self.until - now))

    def fail(self, now: float) -> None:
        self.fails += 1
        if self.fails >= self.max_fails:
            wait = min(self.lockout * (2**self.streak), MAX_LOCKOUT)
            self.until = now + wait
            self.streak += 1
            self.fails = 0

    def ok(self) -> None:
        self.fails = 0
        self.until = 0.0
        self.streak = 0


@dataclass
class LockInputs:
    window_on: bool
    now: float
    window_ends: float | None
    allowed: bool
    kid_until: float
    parent_until: float
    daily_limit_s: int
    daily_used_s: int
    app_limits_s: dict[str, int]
    app_used_s: dict[str, int]
    current_app: str | None
    tv_on: bool
    tz: tzinfo | None = None
    manual_lock: bool = False


@dataclass
class LockResult:
    locked: bool = False
    reason: str = ""
    until: str = ""
    blocked_apps: list[str] = field(default_factory=list)
    kid_mode: bool = False


def _clock(timestamp: float | None, tz: tzinfo | None = None) -> str:
    if timestamp is None:
        return ""
    return datetime.fromtimestamp(timestamp, tz).strftime("%H:%M")


def grant_minutes(old_until: float, new_until: float, now: float) -> int:
    """Whole minutes a new grant adds on top of what was still left."""
    return max(0, round((new_until - max(old_until, now)) / 60))


def grant_text(minutes: int, until: float, tz: tzinfo | None = None) -> str:
    label = f"{minutes // 60} h" if minutes % 60 == 0 and minutes >= 60 else f"{minutes} min"
    return f"Dostałeś +{label}. Oglądaj do {_clock(until, tz)}."


def bonus_for_grant(old_until: float, new_until: float, now: float, exhausted: bool) -> int:
    """Seconds a parent just added on top of an already used-up daily limit."""
    if not exhausted:
        return 0
    return max(0, int(new_until - max(old_until, now)))


def evaluate(i: LockInputs) -> LockResult:
    if i.manual_lock:
        return LockResult(locked=True, reason="Zablokowane przez rodziców")
    if not i.window_on:
        return LockResult()
    until = _clock(i.window_ends, i.tz)
    if i.allowed or i.parent_until > i.now:
        return LockResult(until=until)
    if i.kid_until <= i.now:
        reason = "Czas bez telewizora"
        if until:
            reason += f" do {until}"
        return LockResult(locked=True, reason=reason, until=until)
    if i.daily_limit_s > 0 and i.daily_used_s >= i.daily_limit_s:
        return LockResult(
            locked=True, reason="Limit dzienny wyczerpany", until=until, kid_mode=True
        )
    blocked = [
        NAMES.get(key, key)
        for key, limit in i.app_limits_s.items()
        if limit > 0 and i.app_used_s.get(key, 0) >= limit
    ]
    return LockResult(blocked_apps=blocked, until=until, kid_mode=True)


def app_key(source: str | None, extras: dict[str, str]) -> str | None:
    if not source:
        return None
    name = source.strip().lower()
    return APP_KEYS.get(name) or extras.get(name)


class UsageCounter:
    """Viewing seconds for one local day, total and per app."""

    def __init__(self, day: str) -> None:
        self.day = day
        self.total = 0
        self.apps: dict[str, int] = {}

    def tick(self, day: str, seconds: int, app: str | None, counting: bool) -> None:
        if day != self.day:
            self.day = day
            self.total = 0
            self.apps = {}
        if not counting:
            return
        self.total += seconds
        if app:
            self.apps[app] = self.apps.get(app, 0) + seconds

    def to_dict(self) -> dict:
        return {"day": self.day, "total": self.total, "apps": dict(self.apps)}

    @classmethod
    def from_dict(cls, data: dict) -> "UsageCounter":
        counter = cls(str(data.get("day", "")))
        counter.total = int(data.get("total", 0))
        counter.apps = {str(k): int(v) for k, v in dict(data.get("apps", {})).items()}
        return counter


def request_payload(reason: str) -> dict:
    """The phone notification that asks the parents for more TV time."""
    return {
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
            "url": "/kontrola-rodzicielska/salon",
            "clickAction": "/kontrola-rodzicielska/salon",
            "actions": [
                {"action": "LUMA_GRANT_15", "title": "+15 min"},
                {"action": "LUMA_GRANT_30", "title": "+30 min"},
                {"action": "LUMA_DENY", "title": "Odrzuć i wyłącz TV", "destructive": True},
            ],
        },
    }
