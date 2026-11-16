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
            "InactiveEnterTimestamp": 1_000_000,
        }
        #: How a oneshot template instance behaves when `run` starts it: whether
        #: ExecStart exits 0, and whether the unit has RemainAfterExit=yes.
        self.run_succeeds = True
        self.remain_after_exit = True
        self.run_stuck_at = "activating"
        #: The start job's result, as systemd's JobRemoved signal reports it.
        #: `dependency` is a Requisite= unit that is down: the job ends at once
        #: and the instance itself never changes state. None sends no signal at
        #: all, the way a bus that refused the match rule would behave.
        self.job_result: str | None = "done"
        self.handlers: list[Any] = []
        #: The match rules added and removed on the bus daemon, in order.
        self.matches: list[tuple[str, str]] = []
        #: JobRemoved signals for other units and jobs, sent before ours.
        self.noise: list[Any] = []

    async def call(self, message: Any) -> Any:
        self.calls.append((message.member, list(message.body)))
        if self._none:
            return None
        if self._error or message.member == self.error_on:
            return SimpleNamespace(message_type=MessageType.ERROR, body=["boom"])
        member, args = message.member, list(message.body)
        if member in ("AddMatch", "RemoveMatch", "Subscribe"):
            self.matches += [(member, args[0])] if args else []
            body: list[Any] = []
        elif member == "StartUnit" and "@" in args[0]:
            self._start_job(args[0])
            body = [JOB]
        elif member == "LoadUnit":
            # A valid object path: dbus-fast validates it when the next
            # Message is built, so a stub value fails there instead of in the
            # code under test.
            body = ["/org/freedesktop/systemd1/unit/stub"]
        elif member == "Get":
            body = [self.properties.get(args[1], self._reply)]
        else:
            if member in ("StopUnit", "ResetFailedUnit") and "@" in args[0]:
                # Stopped or reset, the instance is inactive and systemd
                # collects it at once.
                self._collected()
            elif member == "RestartUnit" and self.comes_back:
                # What systemd does once the job has actually run.
                self.properties["ActiveState"] = self.ends
                self.properties["ActiveEnterTimestamp"] += 5_000_000
                self.properties["Result"] = (
                    "exit-code" if self.ends == "failed" else self.properties["Result"]
                )
            elif member == "RestartUnit":
                # The job was queued and nothing has happened yet. By default
                # the old instance is stopping, which is the state a real
                # deploy read and called a failure.
                self.properties["ActiveState"] = self.stuck_at
            body = [self._reply]
        return SimpleNamespace(message_type=MessageType.METHOD_RETURN, body=body)

    def _oneshot_ran(self) -> None:
        """A oneshot instance runs and exits, the way systemd really handles it.

        Exit 0 with RemainAfterExit=yes: active (exited), ActiveEnterTimestamp
        moves. Exit 0 without it: inactive, and an inactive oneshot with the
        default CollectMode is garbage-collected at once, so the next LoadUnit
        returns a fresh unit with every timestamp 0 (crunchtools/rotv#897,
        RT #1633). A non-zero exit: failed, which systemd keeps loaded.
        Nothing moves if the job never ran.
        """
        if not self.comes_back:
            self.properties["ActiveState"] = self.run_stuck_at
        elif not self.run_succeeds:
            self.properties.update(ActiveState="failed", Result="exit-code")
            self.properties["InactiveEnterTimestamp"] += 5_000_000
        elif self.remain_after_exit:
            self.properties.update(ActiveState="active", SubState="exited", Result="success")
            self.properties["ActiveEnterTimestamp"] += 5_000_000
        else:
            self._collected()

    def _start_job(self, unit: str) -> None:
        """Run the start job, then announce its end to whoever is listening.

        A job that ends `done` runs the unit; any other result -- `dependency`
        for a Requisite= unit that is down -- leaves the unit exactly as it was,
        and only the signal says so.
        """
        if self.job_result in ("done", None):
            self._oneshot_ran()
        if self.job_result is None:
            return
        signal = SimpleNamespace(
            message_type=MessageType.SIGNAL,
            path="/org/freedesktop/systemd1",
            interface="org.freedesktop.systemd1.Manager",
            member="JobRemoved",
            body=[42, JOB, unit, self.job_result],
        )
        for message in [*self.noise, signal]:
            for handler in list(self.handlers):
                # A handler that claimed a signal would hide it from the bus.
                assert not handler(message)

    def add_message_handler(self, handler: Any) -> None:
        self.handlers.append(handler)

    def remove_message_handler(self, handler: Any) -> None:
        self.handlers.remove(handler)

    def _collected(self) -> None:
        """What LoadUnit returns for an instance systemd has garbage-collected."""
        self.properties.update(
            ActiveState="inactive",
            SubState="dead",
            Result="success",
            ActiveEnterTimestamp=0,
            InactiveEnterTimestamp=0,
        )

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


TEMPLATE = "rootsofthevalley.org-pg_dump@.service"
JOB = "/org/freedesktop/systemd1/job/42"
INSTANCE = "rootsofthevalley.org-pg_dump@0123456789ab.service"


async def test_run_starts_an_instance_and_waits_for_it_to_succeed() -> None:
    bus = FakeBus()
    await connected(bus, {IN_SCOPE, TEMPLATE}).run(INSTANCE, timeout=60)
    members = [member for member, _body in bus.calls]
    assert ("StartUnit", [INSTANCE, "replace"]) in bus.calls
    assert "Get" in members[members.index("StartUnit") + 1 :]


async def test_run_refuses_an_instance_of_a_template_the_registry_does_not_name() -> None:
    with pytest.raises(SystemdError, match="not an instance of a template unit"):
        await connected(FakeBus(), {IN_SCOPE}).run(INSTANCE, timeout=60)


async def test_run_refuses_a_plain_unit_even_one_in_the_registry() -> None:
    # `run` starts template instances and nothing else: a registry unit is
    # restarted, never run, and naming the template itself starts nothing.
    for unit in (IN_SCOPE, TEMPLATE):
        with pytest.raises(SystemdError, match="not an instance"):
            await connected(FakeBus(), {IN_SCOPE, TEMPLATE}).run(unit, timeout=60)


async def test_restart_and_start_refuse_a_template_instance() -> None:
    client = connected(FakeBus(), {IN_SCOPE, TEMPLATE})
    with pytest.raises(SystemdError, match="registry names"):
        await client.restart(INSTANCE)
    with pytest.raises(SystemdError, match="registry names"):
        await client.start(INSTANCE)


async def test_a_run_that_fails_raises_and_resets_the_instance() -> None:
    bus = fresh_instance()
    bus.run_succeeds = False
    with pytest.raises(SystemdError, match="failed: failed, result 'exit-code'"):
        await connected(bus, {TEMPLATE}).run(INSTANCE, timeout=60)
    assert ("StopUnit", [INSTANCE, "replace"]) in bus.calls
    assert ("ResetFailedUnit", [INSTANCE]) in bus.calls


async def test_a_run_that_never_finishes_raises() -> None:
    bus = FakeBus()
    bus.comes_back = False
    with pytest.raises(SystemdError, match="still activating 60s after"):
        await connected(bus, {TEMPLATE}).run(INSTANCE, timeout=60)


async def test_an_earlier_failed_run_is_not_read_as_this_one() -> None:
    # The instance failed on an earlier ticket and has not run again yet:
    # `failed`, but with the old timestamp, so it is still this run's start.
    bus = FakeBus()
    bus.comes_back = False
    bus.run_stuck_at = "failed"
    bus.properties["Result"] = "exit-code"
    with pytest.raises(SystemdError, match="was still failed 60s after"):
        await connected(bus, {TEMPLATE}).run(INSTANCE, timeout=60)


@pytest.mark.parametrize("named", ["", "abc-def", "a b", "x;rm", "\u00e9t\u00e9"])
async def test_run_refuses_an_instance_name_that_is_not_alphanumeric(named: str) -> None:
    # %i lands in the template's command line; the client, not the caller,
    # keeps it to a rollback tag suffix.
    with pytest.raises(SystemdError, match="not an instance"):
        await connected(FakeBus(), {TEMPLATE}).run(
            f"rootsofthevalley.org-pg_dump@{named}.service", timeout=60
        )


def fresh_instance() -> FakeBus:
    """A bus whose instance was never started: not loaded, every timestamp 0."""
    bus = FakeBus()
    bus._collected()
    return bus


async def test_a_dump_that_succeeds_is_seen_though_systemd_collects_finished_oneshots() -> None:
    # Regression, RT #1633: the dump finished in 5s, the instance was
    # collected, and `run` waited 300s for a timestamp that a fresh unit never
    # has. With RemainAfterExit=yes a good run stays active (exited), and
    # success is a newer ActiveEnterTimestamp.
    bus = fresh_instance()
    await connected(bus, {TEMPLATE}).run(INSTANCE, timeout=60)
    # Stopped afterwards, so per-run instances do not pile up.
    assert ("StopUnit", [INSTANCE, "replace"]) in bus.calls
    assert ("ResetFailedUnit", [INSTANCE]) not in bus.calls


async def test_a_good_dump_is_not_refused_because_it_could_not_be_stopped() -> None:
    bus = fresh_instance()
    bus.error_on = "StopUnit"
    await connected(bus, {TEMPLATE}).run(INSTANCE, timeout=60)


async def test_a_unit_without_remain_after_exit_times_out_and_says_why() -> None:
    # The unit as v0.10.0 shipped it. Its success is indistinguishable from
    # never having started, so it must time out, never pass, and the reason
    # has to point at the fix.
    bus = fresh_instance()
    bus.remain_after_exit = False
    with pytest.raises(SystemdError, match="RemainAfterExit=yes"):
        await connected(bus, {TEMPLATE}).run(INSTANCE, timeout=60)


async def test_an_instance_that_ends_inactive_and_stays_loaded_is_a_failure() -> None:
    # Inactive with a newer InactiveEnterTimestamp: it ran and did not stay
    # active, so whatever it did, it is not a run this program can call good.
    bus = fresh_instance()
    bus.comes_back = False
    bus.run_stuck_at = "inactive"
    bus.properties["InactiveEnterTimestamp"] = 0

    async def ran(_seconds: float) -> None:
        bus.properties["InactiveEnterTimestamp"] = 9_000_000

    client = SystemdUnits(allowed_units={TEMPLATE}, bus=bus, sleep=ran)
    with pytest.raises(SystemdError, match="did not stay active"):
        await client.run(INSTANCE, timeout=60)


class CountingSleep:
    """A sleep that does not, and counts how often it was asked to."""

    def __init__(self) -> None:
        self.polls = 0

    async def __call__(self, _seconds: float) -> None:
        self.polls += 1


async def test_a_start_job_that_ends_dependency_fails_fast_and_names_it() -> None:
    # crunchtools/deployer#56. Requisite=rootsofthevalley.org.service with
    # rotv down: the start job ends `dependency` and the instance never
    # changes state, so its timestamps can never answer. Only the job's
    # result knows, and waiting the whole timeout to then blame
    # RemainAfterExit sends a person after the wrong fix.
    bus = fresh_instance()
    bus.job_result = "dependency"
    sleep = CountingSleep()
    client = SystemdUnits(allowed_units={TEMPLATE}, bus=bus, sleep=sleep)
    with pytest.raises(SystemdError, match="start job ended 'dependency'"):
        await client.run(INSTANCE, timeout=300)
    assert sleep.polls <= 1


@pytest.mark.parametrize("result", ["failed", "canceled", "timeout", "skipped"])
async def test_any_start_job_result_but_done_is_a_failure(result: str) -> None:
    bus = fresh_instance()
    bus.job_result = result
    with pytest.raises(SystemdError, match=f"start job ended {result!r}"):
        await connected(bus, {TEMPLATE}).run(INSTANCE, timeout=60)


async def test_the_job_watch_is_taken_down_after_the_run() -> None:
    bus = fresh_instance()
    await connected(bus, {TEMPLATE}).run(INSTANCE, timeout=60)
    assert bus.handlers == []
    assert [member for member, _rule in bus.matches] == ["AddMatch", "RemoveMatch"]
    assert "member='JobRemoved'" in bus.matches[0][1]


async def test_a_bus_that_sends_no_signal_still_sees_a_good_run() -> None:
    # The signal makes failure fast; it is not what proves success. A bus
    # that delivers nothing falls back to the unit's own timestamps.
    bus = fresh_instance()
    bus.job_result = None
    await connected(bus, {TEMPLATE}).run(INSTANCE, timeout=60)


async def test_a_signal_for_another_unit_or_job_is_ignored() -> None:
    bus = fresh_instance()
    bus.noise = [
        SimpleNamespace(
            message_type=MessageType.SIGNAL,
            path="/org/freedesktop/systemd1",
            interface="org.freedesktop.systemd1.Manager",
            member="JobRemoved",
            body=[7, "/org/freedesktop/systemd1/job/7", "other@x.service", "dependency"],
        ),
        # Ours by unit, but an older job: its result is not this run's.
        SimpleNamespace(
            message_type=MessageType.SIGNAL,
            path="/org/freedesktop/systemd1",
            interface="org.freedesktop.systemd1.Manager",
            member="JobRemoved",
            body=[8, "/org/freedesktop/systemd1/job/8", INSTANCE, "dependency"],
        ),
    ]
    await connected(bus, {TEMPLATE}).run(INSTANCE, timeout=60)


async def test_the_timeout_names_both_reasons_a_unit_can_go_unseen() -> None:
    bus = fresh_instance()
    bus.remain_after_exit = False
    bus.job_result = None
    with pytest.raises(SystemdError, match="start job never ran.*Requisite=.*RemainAfterExit=yes"):
        await connected(bus, {TEMPLATE}).run(INSTANCE, timeout=60)
