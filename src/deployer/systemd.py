"""The systemd surface, scoped to the units this deployer is allowed to touch.

Five operations: restart a unit, start a unit, run an instance of a
template unit to completion, read a unit's state, and read one property of
a unit. Everything goes over D-Bus; there is no ``systemctl``
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

import asyncio
from collections.abc import Awaitable, Callable, Container
from dataclasses import dataclass
from types import TracebackType
from typing import Any, Final, Protocol, Self

from dbus_fast import Message, MessageType
from dbus_fast.aio import MessageBus

__all__ = ["AUTO_UPDATE_UNIT", "Bus", "SystemdError", "SystemdUnits", "UnitState"]

SYSTEMD_BUS: Final = "org.freedesktop.systemd1"
SYSTEMD_PATH: Final = "/org/freedesktop/systemd1"
MANAGER_IFACE: Final = "org.freedesktop.systemd1.Manager"
UNIT_IFACE: Final = "org.freedesktop.systemd1.Unit"
SERVICE_IFACE: Final = "org.freedesktop.systemd1.Service"
PROPS_IFACE: Final = "org.freedesktop.DBus.Properties"

#: Readable but never writable: the hold guard lives on this unit.
AUTO_UPDATE_UNIT: Final = "podman-auto-update.service"

#: How long to wait for a restart to finish before giving up on knowing.
#: Generous: a container has to stop, be removed and be created again, and the
#: unit's own TimeoutStartSec is usually longer than this.
RESTART_TIMEOUT_SECONDS: Final = 120.0
RESTART_POLL_SECONDS: Final = 1.0

#: The whole surface. Checked by ``tests/test_systemd.py``.
ALLOWED_OPERATIONS: Final = frozenset(
    {"restart", "start", "run", "state", "property", "hold_guard_installed", "close", "connect"}
)


class Bus(Protocol):
    """The bus operations this client uses.

    A protocol rather than ``MessageBus`` itself, so a test can pass a
    recording stub through the constructor instead of replacing a private
    attribute. ``MessageBus`` satisfies it structurally.
    """

    async def call(self, msg: Any) -> Any: ...
    def disconnect(self) -> None: ...


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

    def __init__(
        self,
        allowed_units: Container[str],
        bus: Bus | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        """``allowed_units`` is the set ``restart`` and ``start`` will act on.

        Anything outside it is refused. A template in it (``name@.service``)
        is what ``run`` may start an instance of, and only ``run``. The registry supplies this, so the
        client cannot widen its own scope; reads are not restricted, because
        reading tells this program nothing it should not know and the
        auto-update unit has to be readable for the hold guard check.

        ``bus`` and ``sleep`` are for tests. In production both are left out:
        the system bus is connected on first use, and waiting for a restart
        waits in real time.
        """
        self._sleep = sleep
        self._allowed = allowed_units
        self._bus: Bus | None = bus

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

    def _require_template_in_scope(self, instance: str) -> None:
        """Refuse an instance of a template the registry does not name."""
        prefix, at, rest = instance.partition("@")
        named = rest.removesuffix(".service")
        # Alphanumeric only: the instance name is the one input this program
        # passes to a unit a person wrote, and `%i` lands in its command line.
        if not at or not named.isalnum() or not named.isascii() or f"{prefix}@.service" not in self._allowed:
            raise SystemdError(
                f"refusing to run {instance!r}: it is not an instance of a template unit "
                "this deployer's registry names"
            )

    def _connected_bus(self) -> Bus:
        if self._bus is None:
            raise SystemdError("not connected to the system bus")
        return self._bus

    async def _call(self, member: str, signature: str = "", body: list[Any] | None = None) -> Any:
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
        """Restart a unit named in the registry, and WAIT for it to come back.

        ``RestartUnit`` **queues a job and returns**. It does not wait, and that
        cost a real deploy: verification ran immediately afterwards, read
        ``ActiveState`` as ``deactivating`` -- the old container was still
        stopping -- and failed on its first sample. The rollback then did the
        same thing, so a perfectly good service was reported as "down, needs a
        person" while it was coming up fine. Every deploy would have done it.

        Waiting on ``ActiveEnterTimestamp`` rather than on ``ActiveState``
        alone, because the old instance is still ``active`` for the moment
        between the job being queued and the stop beginning. A timestamp newer
        than the one read before the restart is proof that a NEW activation
        happened; "it is active" is not.

        Raises if the unit ends up failed, or if the restart has not completed
        within :data:`RESTART_TIMEOUT_SECONDS` -- a deploy that cannot be told
        to have finished must not be verified as if it had.
        """
        self._require_in_scope(unit, "restart")
        before = await self._active_enter(unit)
        await self._call("RestartUnit", "ss", [unit, "replace"])

        waited = 0.0
        while True:
            state = await self.state(unit)
            if state.failed or state.active_state == "failed":
                raise SystemdError(f"{unit} failed to restart: {state.active_state}, result {state.result!r}")
            if state.active_state == "active" and await self._active_enter(unit) > before:
                return
            if waited >= RESTART_TIMEOUT_SECONDS:
                raise SystemdError(
                    f"{unit} was still {state.active_state} {RESTART_TIMEOUT_SECONDS:.0f}s after "
                    "the restart was queued, so whether it came back is unknown"
                )
            await self._sleep(RESTART_POLL_SECONDS)
            waited += RESTART_POLL_SECONDS

    async def _active_enter(self, unit: str) -> int:
        """``ActiveEnterTimestamp`` in microseconds, or 0 for a unit never started."""
        value = await self.property(unit, "ActiveEnterTimestamp", UNIT_IFACE)
        return int(value) if isinstance(value, int) else 0

    async def start(self, unit: str) -> None:
        """Start a unit named in the registry."""
        self._require_in_scope(unit, "start")
        await self._call("StartUnit", "ss", [unit, "replace"])

    async def run(self, instance: str, timeout: float) -> None:
        """Start an instance of a registry template, and WAIT for it to finish.

        For a oneshot that does a job and exits, such as a pre-deploy database
        dump, and which has ``RemainAfterExit=yes``. That is not optional: a
        oneshot without it goes inactive the moment it succeeds, and systemd
        garbage-collects an inactive unit at once, so the next ``LoadUnit``
        returns a fresh unit with every timestamp 0 -- a success that looks
        exactly like never having run. That was RT #1633: the dump finished in
        five seconds and this waited 300 for a timestamp that never came.

        ``StartUnit`` returns once the job is queued, so this waits on
        timestamps read before the start, never on a state alone:

        * success is ``active`` with a newer ``ActiveEnterTimestamp``. A oneshot
          reaches active only once ExecStart has exited 0;
        * failure is ``failed`` (systemd keeps a failed unit loaded) or
          ``inactive``, either with a newer ``InactiveEnterTimestamp``. An
          instance that failed on an earlier ticket is ``failed`` before it is
          started again, and must not be read as this run.

        Either way the instance is then stopped, and reset if it failed, so
        per-run instances do not pile up. A cleanup error never changes the
        verdict. Raises unless the run succeeded within ``timeout``.
        """
        self._require_template_in_scope(instance)
        active_before = await self._active_enter(instance)
        inactive_before = await self._inactive_enter(instance)
        await self._call("StartUnit", "ss", [instance, "replace"])

        waited = 0.0
        while True:
            state = await self.state(instance)
            if state.active_state == "active" and await self._active_enter(instance) > active_before:
                await self._discard(instance, failed=False)
                return
            ended = state.active_state in ("failed", "inactive")
            if ended and await self._inactive_enter(instance) > inactive_before:
                await self._discard(instance, failed=True)
                if state.active_state == "failed":
                    raise SystemdError(f"{instance} failed: failed, result {state.result!r}")
                raise SystemdError(
                    f"{instance} ended inactive and did not stay active, result {state.result!r}: "
                    "a pre-deploy unit needs RemainAfterExit=yes"
                )
            if waited >= timeout:
                raise SystemdError(
                    f"{instance} was still {state.active_state} {timeout:.0f}s after it was started. "
                    "If it ran and succeeded, it lacks RemainAfterExit=yes: systemd collected it, "
                    "and a collected unit cannot be told from one that never ran"
                )
            await self._sleep(RESTART_POLL_SECONDS)
            waited += RESTART_POLL_SECONDS

    async def _discard(self, instance: str, failed: bool) -> None:
        """Stop a finished instance, and reset it if it failed. Never raises.

        Housekeeping only: the verdict is already decided, and a good dump
        must not become a refusal because systemd would not stop a unit.
        """
        members = [("StopUnit", "ss", [instance, "replace"])]
        if failed:
            members.append(("ResetFailedUnit", "s", [instance]))
        for member, signature, body in members:
            try:
                await self._call(member, signature, body)
            except SystemdError:
                continue

    async def _inactive_enter(self, unit: str) -> int:
        """``InactiveEnterTimestamp`` in microseconds, or 0 for a unit never stopped."""
        value = await self.property(unit, "InactiveEnterTimestamp", UNIT_IFACE)
        return int(value) if isinstance(value, int) else 0

    async def state(self, unit: str) -> UnitState:
        """Read a unit's active state, sub state and last result.

        Readable for any unit: reading tells this program nothing it should not
        know, and the auto-update unit has to be readable for the hold guard
        check.
        """
        path = await self._unit_path(unit)
        # ActiveState and SubState are Unit properties; Result is a Service
        # one. Reading Result off the Unit interface fails, and since a failed
        # read raises, that would have made every state() call an error.
        return UnitState(
            name=unit,
            active_state=str(await self._property(path, UNIT_IFACE, "ActiveState") or ""),
            sub_state=str(await self._property(path, UNIT_IFACE, "SubState") or ""),
            result=str(await self._property(path, SERVICE_IFACE, "Result") or ""),
        )

    async def hold_guard_installed(self) -> bool:
        """Whether ``ExecCondition`` on the auto-update unit is non-empty.

        A read failure raises rather than answering False, so "the guard is
        missing" and "systemd could not be asked" stay distinguishable. Both
        stop a deploy; only one of them means someone should go and install a
        file.
        """
        condition = await self.property(AUTO_UPDATE_UNIT, "ExecCondition", SERVICE_IFACE)
        return bool(condition)

    async def property(self, unit: str, name: str, interface: str = SERVICE_IFACE) -> Any:
        """Read one property of a unit. Read-only, any unit.

        Raises :class:`SystemdError` if the read itself fails. It does **not**
        return ``None`` for a failed read, because the caller that matters most
        cannot tell those apart: the hold guard check treats an empty
        ``ExecCondition`` as "the guard is not installed", and a D-Bus error
        that arrived as ``None`` would be reported as a missing guard. Wrong
        reason, and one that sends a person looking at the wrong file.

        A genuinely absent property comes back as its empty value, which is
        what systemd itself returns.
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
            detail = reply.body if reply is not None else "no reply"
            raise SystemdError(f"reading {interface}.{name} on {path} failed: {detail}")
        # A dbus Variant carries its value on `.value`; a plain value does not.
        answer = reply.body[0]
        return getattr(answer, "value", answer)
