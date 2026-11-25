"""Viewing history of the living-room TV. No Home Assistant imports.

Counts every viewing minute, inside and outside the no-TV time. The parental
limits keep their own counter in lock.py.
"""

from __future__ import annotations

from datetime import date, timedelta

KEEP_DAYS = 60
LAUNCHERS = {"com.ecklf.luma", "com.webos.app.home"}


def viewed_app(tv_on: bool, app_id: str | None, source: str | None) -> str | None:
    """The name of what is being watched, or None on the launcher or a TV that is off."""
    if not tv_on or app_id in LAUNCHERS:
        return None
    return (source or "").strip() or None


class History:
    """Seconds and launches per app per local day."""

    def __init__(self) -> None:
        self.days: dict[str, dict[str, list[int]]] = {}
        self.last_app: str | None = None

    def tick(self, day: str, seconds: int, app: str | None) -> None:
        started = app != self.last_app
        self.last_app = app
        if day not in self.days:
            self._trim(day)
        if not app:
            return
        entry = self.days.setdefault(day, {}).setdefault(app, [0, 0])
        entry[0] += seconds
        if started:
            entry[1] += 1

    def _trim(self, today: str) -> None:
        oldest = (date.fromisoformat(today) - timedelta(days=KEEP_DAYS - 1)).isoformat()
        self.days = {day: apps for day, apps in self.days.items() if day >= oldest}

    def total_minutes(self, day: str) -> float:
        return round(sum(entry[0] for entry in self.days.get(day, {}).values()) / 60, 1)

    def ranking(self, today: str, days: int, top: int = 10) -> list[dict]:
        """Apps of the last `days` days including today, longest watched first."""
        first = (date.fromisoformat(today) - timedelta(days=days - 1)).isoformat()
        sums: dict[str, list[int]] = {}
        for day, apps in self.days.items():
            if first <= day <= today:
                for name, (seconds, launches) in apps.items():
                    entry = sums.setdefault(name, [0, 0])
                    entry[0] += seconds
                    entry[1] += launches
        ordered = sorted(sums.items(), key=lambda item: (-item[1][0], item[0]))
        return [
            {"name": name, "minutes": round(seconds / 60, 1), "launches": launches}
            for name, (seconds, launches) in ordered[:top]
        ]

    def to_dict(self) -> dict:
        return {
            day: {name: list(entry) for name, entry in apps.items()}
            for day, apps in self.days.items()
        }

    @classmethod
    def from_dict(cls, data: dict) -> "History":
        history = cls()
        history.days = {
            str(day): {str(n): [int(e[0]), int(e[1])] for n, e in dict(apps).items()}
            for day, apps in dict(data).items()
        }
        return history
