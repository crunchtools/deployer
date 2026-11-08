"""The ``log`` probe: proving a service that answers nothing over the network.

The agent seats — Kagetora, Josui, and the developer seats behind them — expose
no port. Nagios watches each with a single check that says "the container is
running", and the registry's other two probe kinds both need something to send
a request to. Leaving them out meant leaving six-plus containers taking
whatever ``:latest`` held at their last restart, which is the problem this
program exists to end.

What is being tested is mostly the *floor*. A unit that starts an existing
container rather than creating one keeps the previous run's output, so a probe
that greps the whole log is answered by the last run's startup line and reports
a service ready before it has done anything. That is the same family as reading
a restart as finished the moment it was requested: the event really is in the
log, it is just not the event being asked about.

The other half is the settle window. The line a ``log`` probe waits for is a
one-time event, so re-grepping it would pass for the rest of the window no
matter what the service did — a check that cannot fail. The window is instead
held open by the container's identity, and these tests break that deliberately.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import pytest

from deployer.podman import PodmanError
from deployer.registry import Probe, Service
from deployer.runner import _proof_phrase
from deployer.systemd import UnitState
from deployer.verify import SAMPLE_INTERVAL_SECONDS, Host, verify

CANDIDATE = "sha256:candidate"
SETTLE = 60
READY = 60
READY_LINE = "claude-agent: accepted bypass permissions mode"
# Measured off a real container on podman 5.8.2: nanoseconds and an offset.
STARTED_AT = "2026-11-07T18:01:07.968650234-05:00"
STARTED_EPOCH = datetime.fromisoformat(STARTED_AT).timestamp()
# Named rather than inlined: these counts ARE the assertions, not incidental
# literals. Two failed reads then the line that arrives; and one failed read
# then the line, after which the settle window must add no reads at all.
READS_FOR_A_LATE_LINE = 3
READS_FOR_READINESS_ONLY = 2


def seat(**over: Any) -> Service:
    base: dict[str, Any] = {
        "name": "josui.crunchtools.com",
        "repo": "crunchtools/josui",
        "image": "quay.io/crunchtools/josui",
        "unit": "josui.crunchtools.com.service",
        "probe": Probe(kind="log", expect=READY_LINE),
        "nagios_host": "ctr-josui.crunchtools.com",
        "requestors": ("scott",),
        "settle": SETTLE,
        "ready": READY,
    }
    return Service(**{**base, **over})


class FakeSystemd:
    async def state(self, unit: str) -> UnitState:
        return UnitState(name=unit, active_state="active", sub_state="running", result="success")


class FakeSeatPodman:
    """A seat container whose log and identity can both be steered per sample.

    ``log_sequence`` is what the log read returns on each successive call, the
    last entry repeating. ``identities`` is the same for ``Id``/``RestartCount``,
    so a replacement or a restart can be dropped into the middle of a window.
    """

    def __init__(
        self,
        log_sequence: list[str | Exception] | None = None,
        identities: list[tuple[str, int]] | None = None,
        started_at: str | None = STARTED_AT,
    ) -> None:
        self._logs = log_sequence if log_sequence is not None else [READY_LINE]
        self._identities = identities or [("abc123def456", 0)]
        self._started_at = started_at
        #: Every ``since`` this was asked for. The point of the whole exercise.
        self.since_calls: list[float | None] = []
        self.log_reads = 0
        self.inspections = 0

    async def inspect_container(self, name: str) -> dict[str, Any]:
        del name
        index = min(self.inspections, len(self._identities) - 1)
        container_id, restarts = self._identities[index]
        self.inspections += 1
        state: dict[str, Any] = {"Running": True}
        if self._started_at is not None:
            state["StartedAt"] = self._started_at
        return {"Image": CANDIDATE, "Id": container_id, "RestartCount": restarts, "State": state}

    async def logs(
        self,
        name: str,
        tail: int = 100,
        since: float | None = None,
        strict: bool = False,
    ) -> str:
        del name, tail, strict
        self.since_calls.append(since)
        entry = self._logs[min(self.log_reads, len(self._logs) - 1)]
        self.log_reads += 1
        if isinstance(entry, Exception):
            raise entry
        return entry


async def nosleep(_seconds: float) -> None:
    return None


def host(podman: FakeSeatPodman) -> Host:
    return Host(systemd=FakeSystemd(), podman=podman, sleep=nosleep)


@pytest.mark.asyncio
async def test_startup_line_present_verifies() -> None:
    podman = FakeSeatPodman(log_sequence=[f"booting\n{READY_LINE}\n"])
    outcome = await verify(host(podman), seat(), CANDIDATE)
    assert outcome.ok
    assert READY_LINE in outcome.probe_detail
    assert "no health probe" in outcome.probe_detail


@pytest.mark.asyncio
async def test_log_is_read_only_from_the_container_start_time() -> None:
    """The floor, which is the whole reason this kind is not a plain grep.

    If ``since`` were omitted, a seat whose unit restarts an existing container
    would be declared ready by the *previous* run's startup line.
    """
    podman = FakeSeatPodman(log_sequence=[READY_LINE])
    outcome = await verify(host(podman), seat(), CANDIDATE)
    assert outcome.ok
    assert podman.since_calls, "the log was never read"
    assert all(since == STARTED_EPOCH for since in podman.since_calls), podman.since_calls


@pytest.mark.asyncio
async def test_absent_startup_line_fails_as_never_appeared() -> None:
    podman = FakeSeatPodman(log_sequence=["booting\nstill booting\n"])
    outcome = await verify(host(podman), seat(ready=10), CANDIDATE)
    assert outcome.failed
    assert "startup line never appeared" in outcome.reason
    assert "has not logged" in outcome.probe_detail


@pytest.mark.asyncio
async def test_a_line_that_arrives_late_still_verifies() -> None:
    """Retrying inside ``ready`` is the point; a seat is not instant."""
    podman = FakeSeatPodman(log_sequence=["booting\n", "booting\n", READY_LINE])
    outcome = await verify(host(podman), seat(), CANDIDATE)
    assert outcome.ok
    assert podman.log_reads == READS_FOR_A_LATE_LINE


@pytest.mark.asyncio
async def test_unreadable_log_is_not_reported_as_a_missing_line() -> None:
    """ "I could not read it" and "it does not say that" are different faults.

    One is the service's and one is this program's, and a ticket that confuses
    them sends a person to the wrong file.
    """
    podman = FakeSeatPodman(log_sequence=[PodmanError("socket is gone")])
    outcome = await verify(host(podman), seat(ready=0), CANDIDATE)
    assert outcome.failed
    assert "could not read" in outcome.probe_detail
    assert "has not logged" not in outcome.probe_detail


@pytest.mark.asyncio
async def test_never_started_container_is_not_ready() -> None:
    """libpod reports the year 1 for a container that has not run.

    It parses, which is exactly why it has to be recognised rather than
    trusted — a timestamp in the year 1 as a log floor would read the whole
    log.
    """
    podman = FakeSeatPodman(started_at="0001-01-01T00:00:00Z")
    outcome = await verify(host(podman), seat(ready=0), CANDIDATE)
    assert outcome.failed
    assert "no start time" in outcome.probe_detail
    assert podman.log_reads == 0, "the log must not be read without a floor"


@pytest.mark.asyncio
async def test_missing_start_time_is_not_ready() -> None:
    podman = FakeSeatPodman(started_at=None)
    outcome = await verify(host(podman), seat(ready=0), CANDIDATE)
    assert outcome.failed
    assert "no start time" in outcome.probe_detail


@pytest.mark.asyncio
async def test_a_replaced_container_fails_the_settle_window() -> None:
    """Same image, same unit, different container: the deploy did not hold."""
    podman = FakeSeatPodman(
        identities=[("aaaaaaaaaaaa", 0), ("aaaaaaaaaaaa", 0), ("bbbbbbbbbbbb", 0)],
    )
    outcome = await verify(host(podman), seat(), CANDIDATE)
    assert outcome.failed
    assert "was replaced during the settle window" in outcome.reason


@pytest.mark.asyncio
async def test_a_restart_fails_the_settle_window() -> None:
    """A crash-loop answers perfectly well between crashes."""
    podman = FakeSeatPodman(identities=[("aaaaaaaaaaaa", 0), ("aaaaaaaaaaaa", 1)])
    outcome = await verify(host(podman), seat(), CANDIDATE)
    assert outcome.failed
    assert "restarted during the settle window" in outcome.reason
    assert "RestartCount 0 -> 1" in outcome.reason


def test_the_ticket_does_not_claim_a_url_answered_for_a_log_probe() -> None:
    """The comment is the only receipt a deploy leaves, so it must not invent.

    The success comment used to end "and <url> answered throughout" for every
    service. On a ``log`` row there is no URL and nothing answered, so that
    sentence would have been a fabrication on the one tier that proves least --
    exactly where a reader most needs to know what was actually checked.
    """
    phrase = _proof_phrase(seat())
    assert "answered throughout" not in phrase
    assert READY_LINE in phrase
    assert "no health probe" in phrase

    http = _proof_phrase(seat(probe=Probe(kind="http", url="http://127.0.0.1:8080/health")))
    assert "http://127.0.0.1:8080/health answered throughout" == http


@pytest.mark.asyncio
async def test_the_log_is_not_re_read_during_the_settle_window() -> None:
    """Re-grepping a one-time line would pass for the rest of the window.

    Once it has appeared it has appeared; asking again measures nothing. The
    window is held open by the identity checks instead, and this asserts the
    log is consulted exactly as many times as readiness needed.
    """
    podman = FakeSeatPodman(log_sequence=["booting\n", READY_LINE])
    outcome = await verify(host(podman), seat(), CANDIDATE)
    assert outcome.ok
    assert podman.log_reads == READS_FOR_READINESS_ONLY, (
        f"log read {podman.log_reads} times; settle must not re-read"
    )
    # The window really did run: one inspection per readiness attempt plus one
    # per settle sample.
    assert podman.inspections > int(SETTLE // SAMPLE_INTERVAL_SECONDS)
