"""Deciding whether a deploy actually held.

Three things must all be true, continuously, for the service's settle window:

1. the unit is active;
2. the container that is running *is* the candidate image;
3. the health probe answers as the registry says it should.

The second one is the easy one to leave out and the expensive one to miss. A
restart can come back cleanly on the *old* image -- because the tag moved back,
or because the pull never landed, or because something else restarted the unit
in between -- and a check that only asked "is it active and does it answer"
would call that a successful deploy of a commit that is not running.

The window matters as much as the checks. A service that answers once and then
falls over thirty seconds later has not deployed; it has failed slowly. So the
probe is sampled repeatedly and a single bad answer ends it, rather than
polling until something passes.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Final, Protocol

import httpx

from .registry import Probe, Service
from .systemd import UnitState

__all__ = ["Host", "Outcome", "ProbeResult", "probe_once", "verify"]

#: How often the three checks are repeated inside the settle window.
SAMPLE_INTERVAL_SECONDS: Final = 5.0
PROBE_TIMEOUT_SECONDS: Final = 10.0
#: The JSON-RPC handshake used as an MCP server's health signal.
MCP_INITIALIZE: Final = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "deployer", "version": "0"},
    },
}


class UnitReader(Protocol):
    """The unit read verification needs."""

    async def state(self, unit: str) -> UnitState: ...


class ContainerReader(Protocol):
    """The container read verification needs."""

    async def inspect_container(self, name: str) -> dict[str, Any]: ...


def _is_jsonrpc_answer(body: str) -> bool:
    """Whether ``body`` really is a JSON-RPC answer to ``initialize``.

    Parsed rather than searched for the text ``"result"``: an HTML error page
    that happens to contain the word would have passed a substring check, and
    the point of this probe is to prove something is answering MCP.

    Streamable HTTP may frame the answer as server-sent events, so ``data:``
    lines are unwrapped before parsing. Either a ``result`` or an ``error`` is
    an answer -- a server that refuses the handshake is still running.
    """
    candidates = [body]
    if "data:" in body:
        candidates = [line.partition("data:")[2].strip() for line in body.splitlines() if "data:" in line]
    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if (
            isinstance(parsed, dict)
            and parsed.get("jsonrpc") == "2.0"
            and ("result" in parsed or "error" in parsed)
        ):
            return True
    return False


@dataclass(frozen=True, slots=True)
class Host:
    """What verification talks to, bundled so the signature stays readable.

    ``sleep`` and ``client`` are seams for the tests -- a settle window is a
    minute of real time otherwise, and the probe needs a stubbed transport.
    """

    systemd: UnitReader
    podman: ContainerReader
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep
    client: httpx.AsyncClient | None = None


@dataclass(frozen=True, slots=True)
class ProbeResult:
    """One answer from a health probe."""

    ok: bool
    detail: str


@dataclass(frozen=True, slots=True)
class Outcome:
    """Whether the deploy held, and if not, what was wrong when it did not."""

    ok: bool
    reason: str = ""
    probe_detail: str = ""

    @property
    def failed(self) -> bool:
        return not self.ok


async def probe_once(probe: Probe, client: httpx.AsyncClient | None = None) -> ProbeResult:
    """Ask a service whether it is healthy, once.

    ``http`` is a GET compared against the expected status and, if the registry
    asks, a substring of the body. ``mcp`` is a JSON-RPC ``initialize`` POST,
    because several MCP servers in this fleet expose no health route at all and
    a status check against one would pass without testing anything.
    """
    owned = client is None
    http = client or httpx.AsyncClient(timeout=httpx.Timeout(PROBE_TIMEOUT_SECONDS))
    try:
        if probe.kind == "mcp":
            response = await http.post(
                probe.url,
                json=MCP_INITIALIZE,
                headers={"Accept": "application/json, text/event-stream"},
            )
        else:
            response = await http.get(probe.url)
    except httpx.HTTPError as exc:
        return ProbeResult(ok=False, detail=f"{probe.kind} {probe.url}: {exc}")
    finally:
        if owned:
            await http.aclose()

    if response.status_code != probe.expect_status:
        return ProbeResult(
            ok=False,
            detail=f"{probe.url}: expected {probe.expect_status}, got {response.status_code}",
        )
    body = response.text
    if probe.expect_body and probe.expect_body not in body:
        return ProbeResult(ok=False, detail=f"{probe.url}: body does not contain {probe.expect_body!r}")
    if probe.kind == "mcp" and not _is_jsonrpc_answer(body):
        return ProbeResult(ok=False, detail=f"{probe.url}: initialize returned no JSON-RPC answer")
    return ProbeResult(ok=True, detail=f"{probe.url}: {response.status_code}")


async def verify(host: Host, service: Service, candidate_image_id: str) -> Outcome:
    """Hold the three checks for ``service.settle`` seconds.

    The first failure returns; there is no retrying, because a check that
    retried until something passed would be measuring patience rather than
    health.
    """
    # Elapsed time, not a sample count. Counting samples under-covers any
    # settle that is not a multiple of the interval: settle=9 gave two samples
    # and one five-second wait, and reported success after five seconds. Here
    # the loop runs until the whole window has passed, and the last wait is
    # trimmed to whatever is left of it, so the final sample lands at the end.
    elapsed = 0.0

    while True:
        state = await host.systemd.state(service.unit)
        if not state.is_active:
            return Outcome(ok=False, reason=f"{service.unit} is {state.active_state}, not active")
        if state.failed:
            return Outcome(ok=False, reason=f"{service.unit} reports result {state.result!r}")

        running = await host.podman.inspect_container(service.container)
        running_image = str(running.get("Image") or "")
        if running_image != candidate_image_id:
            # The deploy "succeeded" onto something other than what was staged.
            return Outcome(
                ok=False,
                reason=(
                    f"{service.container} is running {running_image or 'nothing'}, "
                    f"not the candidate {candidate_image_id}"
                ),
            )

        result = await probe_once(service.probe, host.client)
        if not result.ok:
            return Outcome(ok=False, reason="health probe failed", probe_detail=result.detail)

        if elapsed >= service.settle:
            return Outcome(ok=True, probe_detail=f"held for {service.settle}s")

        wait = min(SAMPLE_INTERVAL_SECONDS, service.settle - elapsed)
        await host.sleep(wait)
        elapsed += wait
