"""Verification tests.

The two that earn their place: a restart that came back on the *old* image must
fail, and a probe that answers once and then stops must fail. Both would pass a
naive "is it up?" check, and both are real deploys that did not happen.
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
# One at t=0 plus one per interval, so the end of the window is covered too.
SAMPLES = int(SETTLE // SAMPLE_INTERVAL_SECONDS) + 1


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


async def test_a_settle_shorter_than_the_interval_samples_once() -> None:
    # The explicit policy: one sample at the start and no wait, rather than
    # whatever integer division happens to produce.
    systemd = FakeSystemd()
    async with responder(200) as client:
        outcome = await verify(Host(systemd, FakePodman(), nosleep, client), service(settle=1), CANDIDATE)
    assert outcome.ok
    assert systemd.calls == 1


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
    assert "no JSON-RPC result" in result.detail


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
