"""Systemd client tests.

The scope check is the point: ``restart`` and ``start`` must refuse a unit the
registry does not name, and that refusal has to live in the client rather than
in its caller, because a caller can forget.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from dbus_fast import MessageType

from deployer.systemd import (
    ALLOWED_OPERATIONS,
    AUTO_UPDATE_UNIT,
    SystemdError,
    SystemdUnits,
    UnitState,
)

IN_SCOPE = "acquacotta.crunchtools.com.service"
OUT_OF_SCOPE = "rootsofthevalley.org.service"


def units() -> SystemdUnits:
    return SystemdUnits(allowed_units={IN_SCOPE})


def test_the_public_surface_is_exactly_what_is_declared() -> None:
    public = {name for name in dir(SystemdUnits) if not name.startswith("_")}
    assert public == set(ALLOWED_OPERATIONS)


def test_there_is_no_daemon_reload_and_no_unit_file_write() -> None:
    # Nothing here writes a unit file or a drop-in, so nothing here needs to
    # make systemd re-read one. Holding both would let this change any unit.
    for name in dir(SystemdUnits):
        lowered = name.lower()
        assert "reload" not in lowered
        assert "write" not in lowered
        assert "remove" not in lowered
        assert "mask" not in lowered


async def test_restart_refuses_a_unit_outside_the_registry() -> None:
    with pytest.raises(SystemdError, match="not a unit this deployer's registry names"):
        await units().restart(OUT_OF_SCOPE)


async def test_start_refuses_a_unit_outside_the_registry() -> None:
    with pytest.raises(SystemdError, match="not a unit this deployer's registry names"):
        await units().start(OUT_OF_SCOPE)


async def test_the_refusal_names_the_unit_and_the_operation() -> None:
    with pytest.raises(SystemdError) as exc:
        await units().start(OUT_OF_SCOPE)
    assert "start" in str(exc.value)
    assert OUT_OF_SCOPE in str(exc.value)


async def test_the_auto_update_unit_is_not_restartable_either() -> None:
    # It is readable so the hold guard can be checked, and that is all.
    with pytest.raises(SystemdError, match="not a unit this deployer's registry names"):
        await units().restart(AUTO_UPDATE_UNIT)


@pytest.mark.parametrize(
    ("active", "result", "is_active", "failed"),
    [
        ("active", "success", True, False),
        ("activating", "success", False, False),
        ("failed", "exit-code", False, True),
        ("active", "timeout", True, True),
        ("inactive", "", False, False),
    ],
)
def test_unit_state_reads_active_and_failed(active: str, result: str, is_active: bool, failed: bool) -> None:
    state = UnitState(name="x", active_state=active, sub_state="running", result=result)
    assert state.is_active is is_active
    # A unit can be active with a non-success Result -- a restart that failed
    # once and came back. Treating that as healthy would hide a flapping deploy.
    assert state.failed is failed


async def test_the_allowed_set_is_passed_in_not_discovered() -> None:
    # Proved through behaviour, not by reading a private attribute: the client
    # cannot widen its own scope, because the registry supplies the set.
    narrow = SystemdUnits(allowed_units={"a.service"})
    with pytest.raises(SystemdError, match="registry names"):
        await narrow.restart("b.service")


async def test_hold_guard_and_property_are_in_the_declared_surface() -> None:
    # hold_guard_installed is the one caller that must distinguish "empty" from
    # "could not read", which is why `property` raises rather than returning
    # None on a failed read.
    assert "hold_guard_installed" in ALLOWED_OPERATIONS
    assert "property" in ALLOWED_OPERATIONS


class FakeBus:
    """A stand-in for dbus-fast's MessageBus, recording the calls made.

    It models the one thing about systemd that matters here: ``RestartUnit``
    queues a job and returns, and the unit's ``ActiveEnterTimestamp`` only
    moves once the restart has actually happened. ``comes_back`` says whether
    it ever does.
    """

    def __init__(self, reply: Any = "ok", *, error: bool = False, none: bool = False) -> None:
        self.calls: list[tuple[str, list[Any]]] = []
        self._reply = reply
        self._error = error
        self._none = none
        #: Set by the tests that care. `comes_back` is whether the queued job
        #: ever runs; `ends` is the state it leaves the unit in when it does;
        #: `stuck_at` is the state while it has not; `error_on` fails one member.
        self.comes_back = True
        self.ends = "active"
        self.stuck_at = "deactivating"
        self.error_on: str | None = None
        self.properties: dict[str, Any] = {
            "ActiveState": "active",
            "SubState": "running",
            "Result": "success",
            "ActiveEnterTimestamp": 1_000_000,
        }

    async def call(self, message: Any) -> Any:
        self.calls.append((message.member, list(message.body)))
        if self._none:
            return None
        if self._error or message.member == self.error_on:
            return SimpleNamespace(message_type=MessageType.ERROR, body=["boom"])
        # LoadUnit must answer with a valid object path: dbus-fast validates it
        # when the next Message is built, so a stub value fails there instead
        # of in the code under test.
        if message.member == "LoadUnit":
            body: list[Any] = ["/org/freedesktop/systemd1/unit/stub"]
        elif message.member == "Get":
            _interface, name = message.body
            body = [self.properties.get(name, self._reply)]
        else:
            if message.member == "RestartUnit" and self.comes_back:
                # What systemd does once the job has actually run.
                self.properties["ActiveState"] = self.ends
                self.properties["ActiveEnterTimestamp"] += 5_000_000
                if self.ends == "failed":
                    self.properties["Result"] = "exit-code"
            elif message.member == "RestartUnit":
                # The job was queued and nothing has happened yet. By default
                # the old instance is stopping, which is the state a real
                # deploy read and called a failure.
                self.properties["ActiveState"] = self.stuck_at
            body = [self._reply]
        return SimpleNamespace(message_type=MessageType.METHOD_RETURN, body=body)

    def disconnect(self) -> None:
        return None


async def no_wait(_seconds: float) -> None:
    """A sleep that does not, so a 120-second timeout takes no time."""
    return None


def connected(bus: FakeBus, allowed: set[str] | None = None) -> SystemdUnits:
    """A client with a recording bus, passed in rather than patched over."""
    return SystemdUnits(allowed_units=allowed if allowed is not None else {IN_SCOPE}, bus=bus, sleep=no_wait)


async def test_restart_sends_restartunit_with_replace() -> None:
    bus = FakeBus()
    await connected(bus).restart(IN_SCOPE)
    assert ("RestartUnit", [IN_SCOPE, "replace"]) in bus.calls


async def test_restart_waits_for_the_unit_to_come_back() -> None:
    # RestartUnit queues a job and returns. Verifying immediately read
    # ActiveState as `deactivating` -- the old container still stopping -- and
    # failed on the first sample, then the rollback did the same, so a healthy
    # service was reported as "down, needs a person". Every deploy would have.
    bus = FakeBus()
    await connected(bus).restart(IN_SCOPE)
    members = [member for member, _body in bus.calls]
    # It read the timestamp before, restarted, and read the state after.
    assert members.index("RestartUnit") > 0
    assert "Get" in members[members.index("RestartUnit") + 1 :]


async def test_a_restart_that_never_comes_back_raises() -> None:
    # Better than verifying as if it had finished: a deploy that cannot be
    # told to have completed must not be called verified.
    bus = FakeBus()
    bus.comes_back = False
    with pytest.raises(SystemdError, match="was still deactivating"):
        await connected(bus).restart(IN_SCOPE)


async def test_a_restart_that_ends_failed_raises_at_once() -> None:
    bus = FakeBus()
    bus.ends = "failed"
    with pytest.raises(SystemdError, match="failed to restart"):
        await connected(bus).restart(IN_SCOPE)


async def test_an_unchanged_activation_timestamp_is_not_a_restart() -> None:
    # The old instance is still `active` in the moment between the job being
    # queued and the stop beginning, so "it is active" is not proof. A newer
    # ActiveEnterTimestamp is.
    bus = FakeBus()
    bus.comes_back = False
    bus.stuck_at = "active"
    with pytest.raises(SystemdError, match="was still active"):
        await connected(bus).restart(IN_SCOPE)


async def test_start_sends_startunit_with_replace() -> None:
    bus = FakeBus()
    await connected(bus).start(IN_SCOPE)
    assert bus.calls == [("StartUnit", [IN_SCOPE, "replace"])]


async def test_an_error_reply_to_a_call_raises() -> None:
    # Errors on the method call itself. The first call a restart makes is now
    # LoadUnit, because it reads the activation timestamp before restarting.
    bus = FakeBus()
    bus.error_on = "RestartUnit"
    with pytest.raises(SystemdError, match="RestartUnit failed"):
        await connected(bus).restart(IN_SCOPE)


async def test_an_error_reading_the_state_before_a_restart_raises() -> None:
    with pytest.raises(SystemdError, match="failed"):
        await connected(FakeBus(error=True)).restart(IN_SCOPE)


async def test_a_missing_reply_to_a_call_raises() -> None:
    # A bus that answers nothing is not a successful restart.
    with pytest.raises(SystemdError, match="no reply"):
        await connected(FakeBus(none=True)).restart(IN_SCOPE)


async def test_a_failed_property_read_raises_rather_than_answering_none() -> None:
    # The whole reason: hold_guard_installed must not read a D-Bus error as
    # "the guard is missing".
    with pytest.raises(SystemdError, match="failed"):
        await connected(FakeBus(error=True)).property(AUTO_UPDATE_UNIT, "ExecCondition")


async def test_hold_guard_reads_the_exec_condition() -> None:
    assert await connected(FakeBus(reply=["/usr/bin/test"])).hold_guard_installed() is True
    assert await connected(FakeBus(reply=[])).hold_guard_installed() is False
