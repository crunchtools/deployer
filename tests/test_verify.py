"""Verification tests.

The three that earn their place: a restart that came back on the *old* image
must fail, a probe that answers once and then stops must fail, and a probe that
is refused for the first few seconds and then answers must SUCCEED. The first
two would pass a naive "is it up?" check and are real deploys that did not
happen. The third is the opposite mistake and the one that shipped: every
service here was undeployable because the settle window began at the restart,
and a container unit is active seconds before the process inside it listens.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from deployer.registry import Probe, Service
from deployer.systemd import UnitState
from deployer.verify import SAMPLE_INTERVAL_SECONDS, Host, probe_once, verify

CANDIDATE = "sha256:candidate"
OLD = "sha256:old"
SETTLE = 60
READY = 60
# One at t=0 plus one per interval, so the end of the window is covered too,
# plus the single readiness check a healthy service passes on its first try.
SAMPLES = int(SETTLE // SAMPLE_INTERVAL_SECONDS) + 2


def service(**over: Any) -> Service:
    base: dict[str, Any] = {
        "name": "acquacotta.crunchtools.com",
        "repo": "crunchtools/acquacotta",
        "image": "quay.io/crunchtools/acquacotta",
        "unit": "acquacotta.crunchtools.com.service",
        "probe": Probe(kind="http", url="http://127.0.0.1:8080/"),
        "nagios_host": "ctr-acquacotta.crunchtools.com",
        "requestors": ("developer1",),
        "settle": SETTLE,
        "ready": READY,
    }
    return Service(**{**base, **over})


class FakeSystemd:
    def __init__(self, active: str = "active", result: str = "success") -> None:
        self._active = active
        self._result = result
        self.calls = 0

    async def state(self, unit: str) -> UnitState:
        self.calls += 1
        return UnitState(name=unit, active_state=self._active, sub_state="running", result=self._result)


class FakePodman:
    def __init__(self, image: str = CANDIDATE) -> None:
        self._image = image

    async def inspect_container(self, name: str) -> dict[str, Any]:
        del name
        return {"Image": self._image}


async def nosleep(_seconds: float) -> None:
    return None


def refusing_then(refusals: int, status: int) -> httpx.AsyncClient:
    """A client that refuses the connection ``refusals`` times, then answers.

    Connection refused rather than a 503, because that is what the host
    actually does between conmon reporting the process started and the process
    binding its port: there is nothing listening to return a status at all.
    """
    left = [refusals]

    def handler(_request: httpx.Request) -> httpx.Response:
        if left[0] > 0:
            left[0] -= 1
            raise httpx.ConnectError("connection refused")
        return httpx.Response(status, text='{"result":{}}')

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def responder(*statuses: int) -> httpx.AsyncClient:
    """A client that answers with each status in turn, then repeats the last."""
    remaining = list(statuses)

    def handler(_request: httpx.Request) -> httpx.Response:
        status = remaining.pop(0) if len(remaining) > 1 else remaining[0]
        return httpx.Response(status, text='{"result":{}}')

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_a_healthy_deploy_holds_for_the_window() -> None:
    systemd = FakeSystemd()
    async with responder(200) as client:
        outcome = await verify(Host(systemd, FakePodman(), nosleep, client), service(), CANDIDATE)
    assert outcome.ok
    # Sampled across the window rather than checked once.
    assert systemd.calls == SAMPLES


async def test_a_restart_that_came_back_on_the_old_image_fails() -> None:
    # The expensive one to miss: active, answering, and running the wrong code.
    async with responder(200) as client:
        outcome = await verify(Host(FakeSystemd(), FakePodman(OLD), nosleep, client), service(), CANDIDATE)
    assert outcome.failed
    assert "not the candidate" in outcome.reason


async def test_a_container_running_nothing_fails_legibly() -> None:
    class Empty(FakePodman):
        async def inspect_container(self, name: str) -> dict[str, Any]:
            del name
            return {}

    async with responder(200) as client:
        outcome = await verify(Host(FakeSystemd(), Empty(), nosleep, client), service(), CANDIDATE)
    assert "running nothing" in outcome.reason


@pytest.mark.parametrize(
    ("active", "result", "fragment"),
    [
        ("activating", "success", "not active"),
        ("failed", "exit-code", "not active"),
        ("active", "timeout", "result 'timeout'"),
    ],
)
async def test_an_unhealthy_unit_fails(active: str, result: str, fragment: str) -> None:
    systemd = FakeSystemd(active=active, result=result)
    async with responder(200) as client:
        outcome = await verify(Host(systemd, FakePodman(), nosleep, client), service(), CANDIDATE)
    assert outcome.failed
    assert fragment in outcome.reason


async def test_a_probe_that_passes_then_fails_inside_the_window_fails() -> None:
    # Answering once and falling over is a slow failure, not a deploy.
    async with responder(200, 502) as client:
        outcome = await verify(Host(FakeSystemd(), FakePodman(), nosleep, client), service(), CANDIDATE)
    assert outcome.failed
    assert outcome.reason == "health probe failed"
    assert "502" in outcome.probe_detail


@pytest.mark.parametrize(
    ("settle", "expected_samples"),
    [
        (1, 3),  # shorter than one interval: ready, start, then the 1s remainder
        (5, 3),  # exactly one interval
        (9, 4),  # NOT a multiple: start, +5, +4 -- the remainder is still waited
        (60, 14),
    ],
)
async def test_the_whole_settle_window_is_covered(settle: int, expected_samples: int) -> None:
    # Counting samples under-covered any non-multiple settle: settle=9 used to
    # report success after five seconds. The loop now runs on elapsed time and
    # trims the last wait to what is left.
    waits: list[float] = []

    async def record(seconds: float) -> None:
        waits.append(seconds)

    systemd = FakeSystemd()
    async with responder(200) as client:
        outcome = await verify(Host(systemd, FakePodman(), record, client), service(settle=settle), CANDIDATE)
    assert outcome.ok
    assert systemd.calls == expected_samples
    assert sum(waits) == pytest.approx(float(settle))


async def test_a_short_settle_still_checks_once() -> None:
    async with responder(200) as client:
        outcome = await verify(
            Host(FakeSystemd(), FakePodman(), nosleep, client), service(settle=1), CANDIDATE
        )
    assert outcome.ok


async def test_an_http_probe_compares_the_status() -> None:
    probe = Probe(kind="http", url="http://x/health", expect_status=204)
    async with responder(200) as client:
        assert not (await probe_once(probe, client)).ok
    async with responder(204) as client:
        assert (await probe_once(probe, client)).ok


async def test_an_http_probe_can_require_a_body_substring() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text='{"status":"ok"}')

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        assert (await probe_once(Probe(kind="http", url="http://x/", expect_body="ok"), client)).ok
        bad = await probe_once(Probe(kind="http", url="http://x/", expect_body="degraded"), client)
    assert not bad.ok
    assert "does not contain" in bad.detail


async def test_an_mcp_probe_requires_a_jsonrpc_answer() -> None:
    # The reason mcp is its own kind: several servers here have no health route,
    # so a 200 from one proves nothing on its own.
    def empty(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>not an mcp server</html>")

    async with httpx.AsyncClient(transport=httpx.MockTransport(empty)) as client:
        result = await probe_once(Probe(kind="mcp", url="http://x/mcp"), client)
    assert not result.ok
    assert "no JSON-RPC answer" in result.detail


async def test_an_mcp_probe_accepts_a_jsonrpc_error_as_alive() -> None:
    # A server that refuses the handshake is still a server that is running.
    def refuses(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text='{"jsonrpc":"2.0","error":{"code":-32600}}')

    async with httpx.AsyncClient(transport=httpx.MockTransport(refuses)) as client:
        assert (await probe_once(Probe(kind="mcp", url="http://x/mcp"), client)).ok


async def test_an_unreachable_probe_is_a_failure_with_the_reason() -> None:
    def refused(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    async with httpx.AsyncClient(transport=httpx.MockTransport(refused)) as client:
        result = await probe_once(Probe(kind="http", url="http://x/"), client)
    assert not result.ok
    assert "connection refused" in result.detail


async def test_an_mcp_probe_rejects_a_body_that_merely_mentions_result() -> None:
    # A substring check passed this: an error page with the word "result" in it
    # proved nothing, and proving something answers MCP is the whole point.
    def html(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html><body>no result found</body></html>")

    async with httpx.AsyncClient(transport=httpx.MockTransport(html)) as client:
        assert not (await probe_once(Probe(kind="mcp", url="http://x/mcp"), client)).ok


async def test_an_mcp_probe_reads_a_server_sent_event_framed_answer() -> None:
    # Streamable HTTP may frame the answer as SSE, so data: lines are unwrapped.
    def sse(_request: httpx.Request) -> httpx.Response:
        body = 'event: message\ndata: {"jsonrpc":"2.0","id":1,"result":{"protocolVersion":"x"}}\n\n'
        return httpx.Response(200, text=body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(sse)) as client:
        assert (await probe_once(Probe(kind="mcp", url="http://x/mcp"), client)).ok


async def test_an_mcp_probe_rejects_json_without_the_jsonrpc_marker() -> None:
    def plain(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text='{"result": "ok"}')

    async with httpx.AsyncClient(transport=httpx.MockTransport(plain)) as client:
        assert not (await probe_once(Probe(kind="mcp", url="http://x/mcp"), client)).ok


async def test_a_probe_refused_at_first_and_then_answering_deploys() -> None:
    """The bug that shipped, and the reason this phase exists.

    mcp-pcloud's unit went active the moment conmon reported the process
    started; uvicorn bound its port a couple of seconds later. The old settle
    window spent its first sample in that gap, so the first real ticket this
    deployer ever ran went from open to "rolled back, needs a person" in twelve
    seconds -- against a service that was perfectly healthy.
    """
    systemd = FakeSystemd()
    # Refused, refused, then answering, and answering for the whole window.
    async with refusing_then(2, 200) as client:
        outcome = await verify(Host(systemd, FakePodman(), nosleep, client), service(), CANDIDATE)
    assert outcome.ok


async def test_a_probe_that_never_answers_fails_naming_the_window() -> None:
    # Bounded: "wait for it" is not "wait forever", and the message says which
    # window ran out so the next person does not have to guess.
    waits: list[float] = []

    async def record(seconds: float) -> None:
        waits.append(seconds)

    async with refusing_then(10_000, 200) as client:
        outcome = await verify(
            Host(FakeSystemd(), FakePodman(), record, client), service(ready=20), CANDIDATE
        )
    assert outcome.failed
    assert "never answered within 20s" in outcome.reason
    assert "connection refused" in outcome.probe_detail
    # Exactly the readiness window, and not one second of the settle window.
    assert sum(waits) == pytest.approx(20.0)


async def test_readiness_does_not_wait_on_a_unit_that_is_already_dead() -> None:
    # The two facts the host can answer without waiting stay fatal in this
    # phase too. A unit that started and stopped will not start by itself, and
    # a minute of polling a dead unit is a minute of nobody being told.
    waits: list[float] = []

    async def record(seconds: float) -> None:
        waits.append(seconds)

    async with refusing_then(10_000, 200) as client:
        outcome = await verify(
            Host(FakeSystemd(active="failed", result="exit-code"), FakePodman(), record, client),
            service(),
            CANDIDATE,
        )
    assert outcome.failed
    assert "not active" in outcome.reason
    assert waits == []


async def test_readiness_does_not_wait_on_the_wrong_image() -> None:
    # Same reasoning: a container running an image nobody staged is running it
    # just as firmly in sixty seconds, and ran_something_else must survive the
    # new phase or the revision gets blacklisted for someone else's mistake.
    async with refusing_then(10_000, 200) as client:
        outcome = await verify(Host(FakeSystemd(), FakePodman(OLD), nosleep, client), service(), CANDIDATE)
    assert outcome.failed
    assert outcome.ran_something_else
