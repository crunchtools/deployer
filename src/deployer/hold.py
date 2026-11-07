"""Holds: the record that a service was rolled back and must not be updated.

A rolled-back service gets a file. While that file exists, the host's nightly
``podman auto-update`` is skipped rather than failed, so the image that was just
rolled back is not quietly deployed again at 03:00.

**A hold never expires.** Expiry would mean a known-bad image going live because
nobody got to the alert in time, and a red check that persists is the better
failure. A hold clears when the service verifies on a later ticket, or when a
person runs ``deployer unhold``.

The drop-in that implements the skip is **static config**, installed beside the
deployer's own unit and reviewed as config. This program does not write it and
cannot: it has no unit-file write and no ``daemon-reload``. What it does instead
is refuse to run at all when the drop-in is absent -- see :func:`require_guard`.
A hold that nothing honours is worse than no hold, because it reads as
protection that is not there.
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from .statefile import safe_component, write_whole

__all__ = ["Hold", "HoldStore", "MissingGuardError", "require_guard"]


class MissingGuardError(Exception):
    """The auto-update hold guard is not installed. A "could not run"."""


class GuardReader(Protocol):
    """The one read needed to check the guard."""

    async def hold_guard_installed(self) -> bool: ...


async def require_guard(systemd: GuardReader) -> None:
    """Refuse to proceed unless the hold guard is installed.

    Called once per run, before any ticket is looked at. If a hold could not be
    enforced, then a rollback cannot be made to stick, and deploying at all
    would be taking a risk the program has no way to undo.

    A failed read raises out of ``hold_guard_installed`` rather than arriving
    here as ``False``, so "the guard is missing" and "systemd could not be
    asked" stay distinguishable. Both stop a deploy; only one of them means
    someone should go and install a file.
    """
    if not await systemd.hold_guard_installed():
        raise MissingGuardError(
            "the podman auto-update hold guard is not installed: "
            "ExecCondition on podman-auto-update.service is empty, so a hold "
            "would not be honoured and a rollback would be undone at the next "
            "auto-update. Install 50-deployer-hold.conf and try again."
        )


@dataclass(frozen=True, slots=True)
class Hold:
    """Why a service is held, and since when."""

    unit: str
    service: str
    ticket: int
    revision: str
    at: float = field(default_factory=time.time)

    @property
    def age_seconds(self) -> float:
        return max(0.0, time.time() - self.at)

    def as_json(self) -> str:
        return json.dumps(
            {
                "unit": self.unit,
                "service": self.service,
                "ticket": self.ticket,
                "revision": self.revision,
                "at": self.at,
            },
            indent=2,
            sort_keys=True,
        )

    @classmethod
    def from_json(cls, text: str, unit: str) -> Hold:
        """Read a hold back, tolerating a file written by an older version.

        A hold whose contents cannot be parsed is still a hold: the file's
        existence is what the guard tests, so losing the detail must not lose
        the protection. Unknown fields read as blanks and the unit comes from
        the filename.
        """
        try:
            raw: Any = json.loads(text)
        except json.JSONDecodeError:
            raw = {}
        if not isinstance(raw, dict):
            raw = {}
        ticket = raw.get("ticket")
        at = raw.get("at")
        return cls(
            unit=unit,
            service=str(raw.get("service") or ""),
            ticket=int(ticket) if isinstance(ticket, int) else 0,
            revision=str(raw.get("revision") or ""),
            at=float(at) if isinstance(at, (int, float)) else 0.0,
        )


class HoldStore:
    """The hold files under one state directory."""

    def __init__(self, state_dir: str | Path) -> None:
        self._root = Path(state_dir) / "hold"

    def _path(self, unit: str) -> Path:
        return self._root / safe_component(unit, "unit name")

    def place(self, hold: Hold) -> Path:
        """Write a hold. Overwrites an existing one for the same unit."""
        return write_whole(self._path(hold.unit), hold.as_json())

    def get(self, unit: str) -> Hold | None:
        """The hold on ``unit``, or ``None``."""
        path = self._path(unit)
        try:
            return Hold.from_json(path.read_text(encoding="utf-8"), unit)
        except FileNotFoundError:
            return None

    def held(self, unit: str) -> bool:
        return self._path(unit).exists()

    def clear(self, unit: str) -> bool:
        """Remove the hold on ``unit``. Returns whether there was one."""
        try:
            self._path(unit).unlink()
        except FileNotFoundError:
            return False
        return True

    def __iter__(self) -> Iterator[Hold]:
        """Every hold, oldest first, so a report leads with the stalest."""
        if not self._root.is_dir():
            return iter(())
        holds = [
            Hold.from_json(path.read_text(encoding="utf-8"), path.name)
            for path in sorted(self._root.iterdir())
            if path.is_file() and not path.name.startswith(".")
        ]
        return iter(sorted(holds, key=lambda hold: hold.at))

    def __len__(self) -> int:
        return sum(1 for _ in self)
