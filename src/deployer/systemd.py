"""The systemd surface, scoped to the units this deployer is allowed to touch.

Four operations: restart a unit, start a unit, read a unit's state, and read
one property of a unit. Everything goes over D-Bus; there is no ``systemctl``
in the image.

Two things are deliberately absent:

``daemon-reload``
    Because nothing here writes a unit file or a drop-in. Every file systemd
    must honour on this host is static config installed beside the deployer's
    own unit and reviewed as config. A program that could both write a drop-in
    and make systemd read it could change what any unit does.

any write to a unit outside the registry
    ``restart`` and ``start`` refuse a unit the registry does not name, with
    one read-only exception: ``podman-auto-update.service``, whose
    ``ExecCondition`` must be checked before any ticket is run. Checking it is
    reading; the drop-in that sets it is installed by hand.

The scope check is here, in the client, rather than in the caller. A caller can
forget.
"""

from __future__ import annotations

from collections.abc import Container
from dataclasses import dataclass
from types import TracebackType
from typing import Any, Final, Self

from dbus_fast import Message, MessageType
from dbus_fast.aio import MessageBus

__all__ = ["AUTO_UPDATE_UNIT", "SystemdError", "SystemdUnits", "UnitState"]

SYSTEMD_BUS: Final = "org.freedesktop.systemd1"
SYSTEMD_PATH: Final = "/org/freedesktop/systemd1"
MANAGER_IFACE: Final = "org.freedesktop.systemd1.Manager"
UNIT_IFACE: Final = "org.freedesktop.systemd1.Unit"
SERVICE_IFACE: Final = "org.freedesktop.systemd1.Service"
PROPS_IFACE: Final = "org.freedesktop.DBus.Properties"

#: Readable but never writable: the hold guard lives on this unit.
AUTO_UPDATE_UNIT: Final = "podman-auto-update.service"

#: The whole surface. Checked by ``tests/test_systemd.py``.
ALLOWED_OPERATIONS: Final = frozenset({"restart", "start", "state", "property", "close", "connect"})


class SystemdError(Exception):
    """A systemd call failed, or was refused as out of scope."""


@dataclass(frozen=True, slots=True)
class UnitState:
    """What this program needs to know about a unit's state."""

    name: str
    active_state: str
    sub_state: str
    result: str

    @property
    def is_active(self) -> bool:
        return self.active_state == "active"

    @property
    def failed(self) -> bool:
        return self.active_state == "failed" or self.result not in ("success", "")


class SystemdUnits:
    """Restart, start and read units named in the registry, over D-Bus."""

    def __init__(self, allowed_units: Container[str]) -> None:
        self._allowed = allowed_units
        self._bus: MessageBus | None = None

    async def __aenter__(self) -> Self:
        await self.connect()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.close()

    async def connect(self) -> None:
        if self._bus is None:
            from dbus_fast.constants import BusType

            try:
                self._bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
            except OSError as exc:
                raise SystemdError(f"cannot reach the system bus: {exc}") from None

    async def close(self) -> None:
        if self._bus is not None:
            self._bus.disconnect()
            self._bus = None

    def _require_in_scope(self, unit: str, operation: str) -> None:
        """Refuse a unit the registry does not name.

        The exception for the auto-update unit is read-only and is enforced by
        this method never being called for it from ``restart`` or ``start``.
        """
        if unit not in self._allowed:
            raise SystemdError(
                f"refusing to {operation} {unit!r}: it is not a unit this deployer's registry names"
            )

    def _connected_bus(self) -> MessageBus:
        if self._bus is None:
            raise SystemdError("not connected to the system bus")
        return self._bus

    async def _call(self, member: str, signature: str = "", body: list[Any] | None = None) -> Message:
        await self.connect()
        reply = await self._connected_bus().call(
            Message(
                destination=SYSTEMD_BUS,
                path=SYSTEMD_PATH,
                interface=MANAGER_IFACE,
                member=member,
                signature=signature,
                body=body or [],
            )
        )
        if reply is None or reply.message_type == MessageType.ERROR:
            detail = reply.body if reply is not None else "no reply"
            raise SystemdError(f"{MANAGER_IFACE}.{member} failed: {detail}")
        return reply

    async def restart(self, unit: str) -> None:
        """Restart a unit named in the registry."""
        self._require_in_scope(unit, "restart")
        await self._call("RestartUnit", "ss", [unit, "replace"])

    async def start(self, unit: str) -> None:
        """Start a unit named in the registry."""
        self._require_in_scope(unit, "start")
        await self._call("StartUnit", "ss", [unit, "replace"])

    async def state(self, unit: str) -> UnitState:
        """Read a unit's active state, sub state and last result.

        Readable for any unit: reading tells this program nothing it should not
        know, and the auto-update unit has to be readable for the hold guard
        check.
        """
        path = await self._unit_path(unit)
        return UnitState(
            name=unit,
            active_state=str(await self._property(path, UNIT_IFACE, "ActiveState") or ""),
            sub_state=str(await self._property(path, UNIT_IFACE, "SubState") or ""),
            result=str(await self._property(path, UNIT_IFACE, "Result") or ""),
        )

    async def property(self, unit: str, name: str, interface: str = SERVICE_IFACE) -> Any:
        """Read one property of a unit. Read-only, any unit.

        This is how ``ExecCondition`` on the auto-update unit is checked: an
        empty value means the hold guard is not installed, and the deployer
        refuses to run rather than taking a hold it has no way to enforce.
        """
        return await self._property(await self._unit_path(unit), interface, name)

    async def _unit_path(self, unit: str) -> str:
        """The D-Bus object path for a unit, loading it if systemd has not."""
        reply = await self._call("LoadUnit", "s", [unit])
        return str(reply.body[0])

    async def _property(self, path: str, interface: str, name: str) -> Any:
        await self.connect()
        reply = await self._connected_bus().call(
            Message(
                destination=SYSTEMD_BUS,
                path=path,
                interface=PROPS_IFACE,
                member="Get",
                signature="ss",
                body=[interface, name],
            )
        )
        if reply is None or reply.message_type == MessageType.ERROR:
            return None
        # A dbus Variant carries its value on `.value`; a plain value does not.
        answer = reply.body[0]
        return getattr(answer, "value", answer)
