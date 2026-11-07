"""Systemd client tests.

The scope check is the point: ``restart`` and ``start`` must refuse a unit the
registry does not name, and that refusal has to live in the client rather than
in its caller, because a caller can forget.
"""

from __future__ import annotations

import pytest

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
