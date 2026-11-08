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

But that is two questions, not one, and running them together made every
service here undeployable. *Has it come up yet* is answered by waiting, with
retries, up to ``ready``. *Having come up, does it stay up* is answered by
sampling, with the first failure fatal, for ``settle``. A container unit is
``active`` the moment conmon reports the process started, which is several
seconds before the process inside is listening, so a settle window that began
at the restart spent its first sample in that gap and called a healthy deploy
broken. Measured on the first real ticket: mcp-pcloud went from "open" to
"rolled back, needs a person" in twelve seconds, with a settle of thirty, on a
service that was fine.

The two checks the host can answer without waiting -- is the unit active, is
the right image running -- stay fatal in both phases. Only the probe is given
time, because only the probe is asking something that has to finish starting.
"""

from __future__ import annotations

import asyncio
import json
import ssl
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final, Protocol

import httpx

from .podman import PodmanError
from .registry import Probe, Service
from .systemd import UnitState

__all__ = ["Host", "Outcome", "ProbeResult", "probe_once", "verify"]

#: How often the three checks are repeated inside the settle window.
SAMPLE_INTERVAL_SECONDS: Final = 5.0
PROBE_TIMEOUT_SECONDS: Final = 10.0
#: How much of a container's log a ``log`` probe reads. Generous: a seat can
#: print a dozen lines before the one being waited for, and the read is
#: already floored at the container's start time.
LOG_PROBE_LINES: Final = 200
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
    """The container reads verification needs."""

    async def inspect_container(self, name: str) -> dict[str, Any]: ...

    async def logs(
        self,
        name: str,
        tail: int = ...,
        since: float | None = ...,
        strict: bool = ...,
    ) -> str: ...


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
    #: The unit came back running something that is not the candidate at all.
    #: Nothing was learned about the candidate, so the caller must not record
    #: the revision as one that broke this service -- the next ticket for it
    #: deserves a fair hearing once whatever started the wrong image is fixed.
    ran_something_else: bool = False

    @property
    def failed(self) -> bool:
        return not self.ok


def _client_for(probe: Probe) -> httpx.AsyncClient:
    """A client of this probe's own, trusting exactly what the row names.

    ``ssl.create_default_context`` and nothing else. ``check_hostname`` stays
    True, so OpenSSL verifies the chain, the validity dates and the name
    against ``server_name`` while the socket still goes to the URL's host.

    There is deliberately no ``verify=False`` here, and nothing reads
    ``getpeercert()`` to compare names by hand. Chain verification is not a
    MITM defence on a loopback socket -- it *is* the check being performed, and
    a hand-rolled comparison would be a second, worse implementation of the one
    thing this probe exists to do.
    """
    timeout = httpx.Timeout(PROBE_TIMEOUT_SECONDS)
    if probe.ca_file is None:
        return httpx.AsyncClient(timeout=timeout)
    return httpx.AsyncClient(timeout=timeout, verify=ssl.create_default_context(cafile=probe.ca_file))


def _tls_request_parts(probe: Probe) -> tuple[dict[str, str], dict[str, Any]]:
    """The headers and extensions that put ``server_name`` on the wire.

    One name, two places, because a TLS-terminating proxy needs both: the SNI
    picks which certificate Apache offers, the ``Host`` header picks which
    vhost answers, and a probe that set only one of them would be testing a
    different thing than it reports. httpx passes
    ``extensions["sni_hostname"]`` through to httpcore, which hands it to the
    TLS wrap as ``server_hostname`` -- which is also what ``check_hostname``
    verifies the certificate against. Measured against httpx 0.28.1 and
    httpcore 1.0.9.
    """
    if not probe.server_name:
        return {}, {}
    return {"Host": probe.server_name}, {"sni_hostname": probe.server_name}


async def probe_once(probe: Probe, client: httpx.AsyncClient | None = None) -> ProbeResult:
    """Ask a service whether it is healthy, once.

    ``http`` is a GET compared against the expected status and, if the registry
    asks, a substring of the body. ``mcp`` is a JSON-RPC ``initialize`` POST,
    because several MCP servers in this fleet expose no health route at all and
    a status check against one would pass without testing anything.

    A row that pins the TLS identity is never run on the shared client -- see
    ``Probe.pins_tls`` for why one client cannot serve both kinds of row.
    """
    shared = None if probe.pins_tls else client
    owned = shared is None
    try:
        http = shared or _client_for(probe)
    except (OSError, ssl.SSLError) as exc:
        # The registry loads every ca_file it accepts, so reaching here means
        # the bundle changed under a running deployer. Still this program's
        # fault rather than the service's, and it says so rather than arriving
        # as a traceback in the middle of a maintenance window.
        return ProbeResult(ok=False, detail=f"{probe.url}: cannot use ca_file {probe.ca_file!r}: {exc}")
    headers, extensions = _tls_request_parts(probe)
    try:
        if probe.kind == "mcp":
            response = await http.post(
                probe.url,
                json=MCP_INITIALIZE,
                headers={"Accept": "application/json, text/event-stream", **headers},
                extensions=extensions,
            )
        else:
            response = await http.get(probe.url, headers=headers, extensions=extensions)
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


#: The never-started value libpod reports for ``State.StartedAt``. Parsing it
#: succeeds and yields a timestamp in the year 1, so it has to be recognised
#: rather than trusted.
_ZERO_TIME_PREFIX: Final = "0001-01-01"


@dataclass(frozen=True, slots=True)
class _Identity:
    """Which container this is, and how many times it has come back.

    Taken once at the start of the settle window and compared on every sample.
    A service that was replaced or restarted mid-window has not held, whatever
    it happens to be answering by the time the next sample lands -- and a
    crash-loop answers perfectly well between crashes.
    """

    container_id: str
    restarts: int
    #: ``State.StartedAt`` as a Unix timestamp, or ``None`` if the container
    #: has never run. This is the floor for a log read, so that a startup line
    #: from a previous run of the same container cannot answer for this one.
    started_at: float | None


def _identity(running: dict[str, Any]) -> _Identity:
    """Read the identity fields out of a libpod container inspection.

    Field names measured against podman 5.8.2 rather than assumed: ``Id`` and
    ``RestartCount`` are top level, ``StartedAt`` is under ``State``, and the
    timestamp carries nanoseconds and an offset.
    """
    state = running.get("State")
    raw_started = str((state or {}).get("StartedAt") or "") if isinstance(state, dict) else ""
    started: float | None = None
    if raw_started and not raw_started.startswith(_ZERO_TIME_PREFIX):
        try:
            started = datetime.fromisoformat(raw_started).timestamp()
        except ValueError:
            started = None
    restarts_raw = running.get("RestartCount")
    return _Identity(
        container_id=str(running.get("Id") or ""),
        restarts=restarts_raw if isinstance(restarts_raw, int) else 0,
        started_at=started,
    )


async def _host_facts(
    host: Host,
    service: Service,
    candidate_image_id: str,
    baseline: _Identity | None = None,
) -> tuple[Outcome | None, _Identity]:
    """The checks that need no waiting, or ``None`` if they all pass.

    None of these answers improves by being asked again. The unit was already
    seen active by the restart, so an inactive one now is a service that
    started and stopped; and a container running an image nobody staged is
    running it as firmly in sixty seconds as it is today. So these are fatal in
    the readiness phase as well, and only the probe is given time.

    With a ``baseline`` -- which the settle window passes and the readiness
    window does not -- it also fails a container that was replaced or restarted
    since that baseline was taken. This applies to every probe kind, not just
    ``log``: an HTTP service that crash-loops answers correctly in the gaps,
    and sampling a window without asking whether it is still the same process
    was measuring the gaps.
    """
    state = await host.systemd.state(service.unit)
    if not state.is_active:
        return Outcome(ok=False, reason=f"{service.unit} is {state.active_state}, not active"), _Identity(
            "", 0, None
        )
    if state.failed:
        return Outcome(ok=False, reason=f"{service.unit} reports result {state.result!r}"), _Identity(
            "", 0, None
        )

    running = await host.podman.inspect_container(service.container)
    here = _identity(running)
    running_image = str(running.get("Image") or "")
    if running_image != candidate_image_id:
        # The deploy "succeeded" onto something other than what was staged.
        return (
            Outcome(
                ok=False,
                reason=(
                    f"{service.container} is running {running_image or 'nothing'}, "
                    f"not the candidate {candidate_image_id}"
                ),
                ran_something_else=True,
            ),
            here,
        )

    if baseline is not None:
        if here.container_id != baseline.container_id:
            return (
                Outcome(
                    ok=False,
                    reason=(
                        f"{service.container} was replaced during the settle window "
                        f"({baseline.container_id[:12]} -> {here.container_id[:12] or 'nothing'})"
                    ),
                ),
                here,
            )
        if here.restarts != baseline.restarts:
            return (
                Outcome(
                    ok=False,
                    reason=(
                        f"{service.container} restarted during the settle window "
                        f"(RestartCount {baseline.restarts} -> {here.restarts})"
                    ),
                ),
                here,
            )
    return None, here


async def _log_says_ready(host: Host, service: Service, identity: _Identity) -> ProbeResult:
    """Whether the container has printed its startup line *since it started*.

    The floor is ``State.StartedAt``, never "the whole log". Without it a unit
    that starts an existing container rather than creating one would be
    answered by the previous run's startup line, which is the bug class this
    program keeps finding: the event is genuinely in the log, it is just not
    the event being asked about.

    A log that cannot be read is reported as that, not as a missing line. One
    is the service failing and the other is this program failing, and they do
    not want the same words on the ticket.
    """
    expect = service.probe.expect or ""
    if identity.started_at is None:
        return ProbeResult(ok=False, detail=f"{service.container} reports no start time yet")
    try:
        # int() truncation in the client floors this to the whole second the
        # container started in, which can only widen the window, never narrow
        # it past the startup line.
        written = await host.podman.logs(
            service.container, tail=LOG_PROBE_LINES, since=identity.started_at, strict=True
        )
    except PodmanError as exc:
        return ProbeResult(ok=False, detail=f"could not read {service.container}'s log: {exc}")
    if expect in written:
        return ProbeResult(ok=True, detail=f"{service.container} logged {expect!r}")
    return ProbeResult(ok=False, detail=f"{service.container} has not logged {expect!r} since it started")


async def _wait_until_ready(
    host: Host, service: Service, candidate_image_id: str
) -> tuple[Outcome, _Identity]:
    """Wait up to ``service.ready`` for the probe to answer once.

    Retrying is the point here, and it is not the same thing as retrying until
    something passes: the window is bounded, it is spent before the settle
    window rather than inside it, and the host facts above still end it
    immediately. A service that never answers inside ``ready`` has not started.
    """
    waited = 0.0
    while True:
        fatal, identity = await _host_facts(host, service, candidate_image_id)
        if fatal is not None:
            return fatal, identity

        if service.probe.reads_log:
            result = await _log_says_ready(host, service, identity)
        else:
            result = await probe_once(service.probe, host.client)
        if result.ok:
            return Outcome(ok=True, probe_detail=result.detail), identity

        if waited >= service.ready:
            missed = (
                "startup line never appeared" if service.probe.reads_log else "health probe never answered"
            )
            return (
                Outcome(
                    ok=False,
                    reason=f"{missed} within {service.ready}s of the restart",
                    probe_detail=result.detail,
                ),
                identity,
            )

        wait = min(SAMPLE_INTERVAL_SECONDS, service.ready - waited)
        await host.sleep(wait)
        waited += wait


async def verify(host: Host, service: Service, candidate_image_id: str) -> Outcome:
    """Wait up to ``service.ready`` for the service, then hold it for ``settle``.

    Inside the settle window the first failure returns; there is no retrying,
    because a check that retried until something passed would be measuring
    patience rather than health. Before that window there is nothing to measure
    yet -- see the module docstring.
    """
    ready, baseline = await _wait_until_ready(host, service, candidate_image_id)
    if ready.failed:
        return ready

    # Elapsed time, not a sample count. Counting samples under-covers any
    # settle that is not a multiple of the interval: settle=9 gave two samples
    # and one five-second wait, and reported success after five seconds. Here
    # the loop runs until the whole window has passed, and the last wait is
    # trimmed to whatever is left of it, so the final sample lands at the end.
    elapsed = 0.0

    while True:
        fatal, _ = await _host_facts(host, service, candidate_image_id, baseline)
        if fatal is not None:
            return fatal

        # A log probe is NOT re-asked here. The line it waited for is a
        # one-time event; grepping for it again would pass for the rest of the
        # window no matter what the service did next, which is a check that
        # cannot fail. What holds the window open instead is the identity
        # comparison above: same container, same restart count, still active.
        if not service.probe.reads_log:
            result = await probe_once(service.probe, host.client)
            if not result.ok:
                return Outcome(ok=False, reason="health probe failed", probe_detail=result.detail)

        if elapsed >= service.settle:
            if service.probe.reads_log:
                return Outcome(
                    ok=True,
                    probe_detail=(
                        f"no health probe; logged {service.probe.expect!r} then held "
                        f"{service.settle}s with no restart"
                    ),
                )
            return Outcome(ok=True, probe_detail=f"held for {service.settle}s")

        wait = min(SAMPLE_INTERVAL_SECONDS, service.settle - elapsed)
        await host.sleep(wait)
        elapsed += wait
