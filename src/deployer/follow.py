"""release-follow — file a Deploy ticket when a followed service falls behind its release.

Ashigaru cuts releases on its own for repositories that opt in, and nothing was
filing the Deploy ticket that would put them live, so services drifted (rotv
fell 37 releases behind). The seats that file Deploy tickets by hand cannot see
a release they did not make, and Ashigaru holds no RT credential and must not
get one. This is the missing filer, and it is deliberately small:

    for each registry row that lists ``follower`` in ``requestors``:
        read <image>:latest from the registry (id, revision label, build time)
        read what the service's container is running (from a host snapshot)
        if they differ, :latest is newer, and no ticket already covers it:
            file a Deploy ticket in the ordinary body format, as RT user follower

Model-free, like the rest of this package. It decides only *that a ticket
should exist*; whether the deploy happens, and how it is proved, stays with the
runner and the registry, exactly as for a ticket a seat files. The opt-in and
the authorisation are one fact: a row that lists ``follower`` is a row whose
follower tickets the runner accepts, and ``registry.load_registry`` refuses
``follower`` on any row that must not deploy unattended.

**No container engine.** The running side comes from a JSON snapshot the unit
writes on the host before this starts (``podman ps`` and ``podman images``),
mounted read-only. The podman socket is root-equivalent, and a ticket filer has
no business holding it.

**Never a downgrade.** The runner can deploy a merged commit main promoted
before any release carries it, so "running differs from :latest" can mean the
service is *ahead*. :latest is followed only when it was built after what is
running.

**Never the same ticket twice.** Not while any live Deploy ticket (new, open or
stalled, from anyone) names the same service and commit; and not ever, for a
commit this follower has already filed for that service -- a follower ticket a
person resolved or rejected stays answered until the next release.

A follower ticket the runner hands back is stalled and owned by ``follower``,
which no person reads. So every stalled follower ticket is written to the status
file, and the Nagios check that reads it turns WARNING (see ``write_status``).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Final, Protocol

from . import __version__
from .oci import OciError, RegistryReader, RemoteImage
from .podman import REVISION_LABEL
from .registry import FOLLOWER, Registry, RegistryError, Service, load_registry
from .rt import RtClient, RtCredentials, RtError, Ticket
from .statefile import frame_field
from .statefile import write_frame
from .ticket import Request, TicketError, parse_ticket

__all__ = [
    "Action",
    "Decision",
    "FollowReport",
    "Running",
    "Snapshot",
    "SnapshotError",
    "decide",
    "follow",
    "main",
    "subject_for",
    "write_status",
]

EXIT_OK: Final = 0
#: The pass ran, but at least one followed service could not be judged.
EXIT_DEGRADED: Final = 1
EXIT_COULD_NOT_RUN: Final = 2

#: Fixed, not configurable: this files Deploy tickets and nothing else.
QUEUE: Final = "Deploy"
SUBJECT_PREFIX: Final = "release-follow:"

#: Where things are is a property of the deployment, so it comes from the unit.
#: No built-in paths: a missing one is exit 2, never a guess.
CONFIG_ENV: Final = "DEPLOYER_CONFIG"
SNAPSHOT_ENV: Final = "FOLLOW_SNAPSHOT"
STATUS_ENV: Final = "FOLLOW_STATUS"
CONTAINERS_FILE: Final = "containers.json"
IMAGES_FILE: Final = "images.json"

STATUS_PREFIX: Final = "FOLLOWER"
OUTCOME_OK: Final = "ok"
OUTCOME_DEGRADED: Final = "degraded"
OUTCOME_COULD_NOT_RUN: Final = "could-not-run"

_SHA_RE: Final = re.compile(r"\A[0-9a-f]{40}\Z")
#: A service name that is safe inside a quoted RT query and a Subject.
_NAME_RE: Final = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._-]*\Z")


class SnapshotError(Exception):
    """The host snapshot of running containers could not be read."""


@dataclass(frozen=True, slots=True)
class Running:
    """What one container is running, according to the snapshot."""

    image_id: str
    revision: str | None
    created: float | None


class Snapshot:
    """``podman ps --format json`` and ``podman images --format json``, joined."""

    def __init__(self, containers: Sequence[Any], images: Sequence[Any]) -> None:
        """Join running containers to the images they run.

        ``containers`` is podman's ``ps`` JSON: entries with ``Names`` (a list),
        ``ImageID`` and ``State``; only ``running`` ones count. ``images`` is
        podman's ``images`` JSON: entries with ``Id``, ``Created`` (epoch
        seconds) and ``Labels``. The two are matched on the image id with any
        ``sha256:`` prefix removed, because podman writes it both ways. An entry
        of any other shape is skipped rather than guessed at.
        """
        by_id: dict[str, Mapping[str, Any]] = {}
        for image in images:
            if isinstance(image, dict) and isinstance(image.get("Id"), str):
                by_id[_bare(image["Id"])] = image
        self._running: dict[str, Running] = {}
        for entry in containers:
            if not isinstance(entry, dict) or entry.get("State") != "running":
                continue
            image_id = _bare(str(entry.get("ImageID") or ""))
            if not image_id:
                continue
            image = by_id.get(image_id, {})
            labels = image.get("Labels")
            revision = labels.get(REVISION_LABEL) if isinstance(labels, dict) else None
            created = image.get("Created")
            running = Running(
                image_id=f"sha256:{image_id}",
                revision=revision if isinstance(revision, str) and revision else None,
                created=float(created) if isinstance(created, (int, float)) and created > 0 else None,
            )
            for name in entry.get("Names") or ():
                self._running[str(name)] = running

    @classmethod
    def load(cls, directory: str | Path) -> Snapshot:
        """Read ``containers.json`` and ``images.json`` from ``directory``.

        Raises :class:`SnapshotError` if either is missing, not JSON, or not a list.
        """
        root = Path(directory)
        return cls(_read_list(root / CONTAINERS_FILE), _read_list(root / IMAGES_FILE))

    def running(self, container: str) -> Running | None:
        """What the container of that name runs, or None if it is not running."""
        return self._running.get(container)


def _bare(image_id: str) -> str:
    """An image id without its ``sha256:`` prefix, which podman writes inconsistently."""
    return image_id.rpartition(":")[2]


def _read_list(path: Path) -> list[Any]:
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise SnapshotError(f"cannot read {path}: {exc}") from None
    except ValueError as exc:
        raise SnapshotError(f"{path} is not JSON: {exc}") from None
    if not isinstance(parsed, list):
        raise SnapshotError(f"{path} is not a JSON list")
    return parsed


class Action(Enum):
    """What one followed service needs."""

    CURRENT = "current"
    AHEAD = "ahead"
    FILE = "file"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class Decision:
    """What :func:`decide` concluded for one service.

    ``action`` is what to do, ``detail`` a short reason for the status file,
    and ``commit`` the full revision to file a ticket for -- set only when
    ``action`` is :attr:`Action.FILE`.
    """

    action: Action
    detail: str
    commit: str | None = None


def decide(remote: RemoteImage, running: Running | None) -> Decision:
    """Whether ``remote`` (:latest) should be put live over ``running``. Pure."""
    if running is None:
        return Decision(Action.ERROR, "no running container in the snapshot")
    revision = (remote.revision or "").lower()
    if remote.image_id == running.image_id or (revision and (running.revision or "").lower() == revision):
        return Decision(Action.CURRENT, f"running {revision[:12] or ':latest'}")
    if not _SHA_RE.match(revision):
        return Decision(Action.ERROR, f":latest has no usable revision label ({remote.revision!r})")
    if remote.created is None or running.created is None:
        # Without both build times there is no telling a release from a
        # downgrade, and guessing wrong would ticket a rollback nobody wanted.
        return Decision(Action.ERROR, "cannot tell which build is newer: a build time is missing")
    if remote.created <= running.created:
        return Decision(Action.AHEAD, f"running is newer than :latest {revision[:12]}")
    return Decision(Action.FILE, f":latest is {revision[:12]}", commit=revision)


def subject_for(service: str, commit: str) -> str:
    """The Subject of a follower ticket: ``release-follow: <service> <full commit>``.

    Exact and deterministic, because it is also the key that finds a ticket this
    follower filed before, whatever its status.
    """
    return f"{SUBJECT_PREFIX} {service} {commit}"


def body_for(service: Service, commit: str) -> str:
    """The ordinary Deploy body. Nothing else: the runner's grammar refuses prose."""
    return f"service: {service.name}\nrepo: {service.repo}\ncommit: {commit}\n"


class Tickets(Protocol):
    """The RT operations the follower uses."""

    async def live_tickets(self, queue: str) -> tuple[int, ...]: ...
    async def search(self, query: str) -> tuple[int, ...]: ...
    async def ticket(self, ticket_id: int) -> Ticket: ...
    async def create_ticket(self, queue: str, subject: str, text: str) -> int: ...


class Images(Protocol):
    async def image(self, image: str, tag: str = ...) -> RemoteImage: ...


@dataclass(frozen=True, slots=True)
class ServiceResult:
    """One followed service's result: filed, pending, answered, would-file, or a Decision's action."""

    service: str
    result: str
    detail: str
    ticket: int | None = None


@dataclass(frozen=True, slots=True)
class Stalled:
    """A follower ticket the runner handed back. ``?`` where its body did not parse."""

    ticket: int
    service: str
    commit: str


@dataclass(frozen=True, slots=True)
class FollowReport:
    """One pass.

    ``outcome`` is ``ok`` (every followed row judged), ``degraded`` (at least one
    could not be) or ``could-not-run``; ``detail`` is a one-line summary;
    ``services`` holds one result per followed row and ``stalled`` every
    follower ticket the runner handed back.
    """

    outcome: str
    detail: str
    services: tuple[ServiceResult, ...] = ()
    stalled: tuple[Stalled, ...] = ()


def followed(registry: Registry) -> tuple[Service, ...]:
    """The rows that opted in, by listing ``follower`` as a requestor."""
    return tuple(
        registry.services[name]
        for name in registry.names
        if FOLLOWER in {who.strip().lower() for who in registry.services[name].requestors}
    )


async def _live_requests(rt: Tickets) -> dict[tuple[str, str], int]:
    """(service, commit) -> ticket id, for every live Deploy ticket that parses."""
    found: dict[tuple[str, str], int] = {}
    for ticket_id in await rt.live_tickets(QUEUE):
        request = _request(await rt.ticket(ticket_id))
        if request is not None:
            found.setdefault((request.service, request.commit), ticket_id)
    return found


def _request(ticket: Ticket) -> Request | None:
    try:
        return parse_ticket(ticket.body)
    except TicketError:
        # Someone else's malformed ticket. The runner refuses it; here it simply
        # covers nothing.
        return None


async def _stalled(rt: Tickets) -> tuple[Stalled, ...]:
    """Every follower ticket the runner handed back. Nobody reads ``follower``'s queue."""
    ids = await rt.search(f"Queue = '{QUEUE}' AND Creator = '{FOLLOWER}' AND Status = 'stalled'")
    found: list[Stalled] = []
    for ticket_id in ids:
        request = _request(await rt.ticket(ticket_id))
        found.append(
            Stalled(ticket_id, request.service if request else "?", request.commit if request else "?")
        )
    return tuple(found)


async def _one(
    service: Service,
    images: Images,
    rt: Tickets,
    context: tuple[Snapshot, Mapping[tuple[str, str], int], bool],
) -> ServiceResult:
    snapshot, live, dry_run = context
    if not _NAME_RE.match(service.name):
        return ServiceResult(service.name, Action.ERROR.value, "service name is not safe to put in a query")
    try:
        remote = await images.image(service.image)
    except OciError as exc:
        return ServiceResult(service.name, Action.ERROR.value, str(exc))
    decision = decide(remote, snapshot.running(service.container))
    if decision.action is not Action.FILE or decision.commit is None:
        return ServiceResult(service.name, decision.action.value, decision.detail)
    return await _file(service, decision.commit, rt, live, dry_run)


async def _file(
    service: Service, commit: str, rt: Tickets, live: Mapping[tuple[str, str], int], dry_run: bool
) -> ServiceResult:
    """File the ticket, unless one already covers this commit or once did."""
    existing = live.get((service.name, commit))
    if existing is not None:
        return ServiceResult(service.name, "pending", f"ticket {existing} covers {commit[:12]}", existing)
    subject = subject_for(service.name, commit)
    earlier = await rt.search(f"Queue = '{QUEUE}' AND Creator = '{FOLLOWER}' AND Subject = '{subject}'")
    if earlier:
        return ServiceResult(service.name, "answered", f"already filed {commit[:12]}", earlier[-1])
    if dry_run:
        return ServiceResult(service.name, "would-file", f":latest is {commit[:12]}")
    filed = await rt.create_ticket(QUEUE, subject, body_for(service, commit))
    return ServiceResult(service.name, "filed", f":latest is {commit[:12]}", filed)


async def follow(
    registry: Registry, images: Images, rt: Tickets, snapshot: Snapshot, dry_run: bool = False
) -> FollowReport:
    """One pass over every followed row. RT errors propagate: they are "could not run"."""
    rows = followed(registry)
    live = await _live_requests(rt)
    results = [await _one(row, images, rt, (snapshot, live, dry_run)) for row in rows]
    stalled = await _stalled(rt)
    errors = [result for result in results if result.result == Action.ERROR.value]
    filed = sum(1 for result in results if result.result == "filed")
    detail = f"{len(rows)} followed, {filed} filed, {len(errors)} unjudged, {len(stalled)} stalled"
    return FollowReport(OUTCOME_DEGRADED if errors else OUTCOME_OK, detail, tuple(results), stalled)


def write_status(path: str | Path, report: FollowReport, now: float | None = None) -> Path:
    """The last pass, in nagios-agent's collector frame.

    For the Nagios check (nagios-agent, not this repo): WARNING on any
    ``stalled`` line, on an outcome other than ``ok``, on a ``generated`` older
    than three passes, or on a file without ``end=1``.
    """
    stamp = time.time() if now is None else now
    head = f"generated={stamp:.0f} outcome={frame_field(report.outcome)} detail={frame_field(report.detail)}"
    services = [
        f"service name={frame_field(r.service)} result={frame_field(r.result)} "
        f"ticket={r.ticket or '-'} detail={frame_field(r.detail)}"
        for r in report.services
    ]
    stalled = [
        f"stalled ticket={s.ticket} service={frame_field(s.service)} commit={frame_field(s.commit[:12])}"
        for s in report.stalled
    ]
    return write_frame(Path(path), STATUS_PREFIX, [head, *services, *stalled])


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="release-follow",
        description="File an RT Deploy ticket for each followed service whose :latest is newer "
        "than what it runs.",
    )
    parser.add_argument("--version", action="version", version=f"release-follow {__version__}")
    parser.add_argument(
        "--config",
        default=os.environ.get(CONFIG_ENV),
        help=f"services.toml (default: ${CONFIG_ENV}; required)",
    )
    parser.add_argument(
        "--snapshot",
        default=os.environ.get(SNAPSHOT_ENV),
        help=f"Directory holding {CONTAINERS_FILE} and {IMAGES_FILE} (default: ${SNAPSHOT_ENV}; required)",
    )
    parser.add_argument(
        "--status",
        default=os.environ.get(STATUS_ENV),
        help=f"Status file for Nagios (default: ${STATUS_ENV}; required unless --dry-run)",
    )
    parser.add_argument("--dry-run", action="store_true", help="Say what would be filed; file nothing")
    return parser


def _complain(message: str) -> int:
    print(f"release-follow: {message}", file=sys.stderr)
    return EXIT_COULD_NOT_RUN


def _missing(args: argparse.Namespace) -> list[str]:
    missing = [] if args.config else [CONFIG_ENV]
    missing += [] if args.snapshot else [SNAPSHOT_ENV]
    missing += [] if args.status or args.dry_run else [STATUS_ENV]
    return missing


async def _run(registry: Registry, creds: RtCredentials, snapshot: Snapshot, dry_run: bool) -> FollowReport:
    async with RtClient(creds) as rt, RegistryReader() as images:
        return await follow(registry, images, rt, snapshot, dry_run)


def _print(report: FollowReport) -> None:
    for result in report.services:
        ticket = f" (ticket {result.ticket})" if result.ticket else ""
        print(f"{result.service}: {result.result}{ticket}: {result.detail}")
    for stalled in report.stalled:
        print(f"stalled follower ticket {stalled.ticket}: {stalled.service} {stalled.commit[:12]}")
    print(report.detail)


def _record(status: str | None, report: FollowReport) -> bool:
    """Write the status file. False if it could not be: then Nagios is blind."""
    if not status:
        return True
    try:
        write_status(status, report)
    except OSError as exc:
        print(f"release-follow: cannot write {status}: {exc}", file=sys.stderr)
        return False
    return True


def main(argv: Sequence[str] | None = None) -> int:
    """One follow pass. 0 every followed row judged; 1 some could not be; 2 could not run."""
    args = _build_parser().parse_args(argv)
    missing = _missing(args)
    if missing:
        return _complain(f"not configured: {', '.join(missing)} not set")
    try:
        creds = RtCredentials.from_env(os.environ)
        registry = load_registry(args.config)
        snapshot = Snapshot.load(args.snapshot)
    except (RtError, RegistryError, SnapshotError) as exc:
        report = FollowReport(OUTCOME_COULD_NOT_RUN, str(exc))
        _record(None if args.dry_run else args.status, report)
        return _complain(str(exc))
    # The credential is the boundary the registry's opt-in leans on: these
    # tickets must arrive as `follower`, or the requestors check means nothing.
    if creds.username.strip().lower() != FOLLOWER:
        why = f"RT_USER must be {FOLLOWER!r}; this files as nobody else"
        _record(None if args.dry_run else args.status, FollowReport(OUTCOME_COULD_NOT_RUN, why))
        return _complain(why)
    try:
        report = asyncio.run(_run(registry, creds, snapshot, args.dry_run))
    except RtError as exc:
        report = FollowReport(OUTCOME_COULD_NOT_RUN, f"RT: {exc}")
    _print(report)
    recorded = _record(None if args.dry_run else args.status, report)
    if report.outcome == OUTCOME_COULD_NOT_RUN:
        return EXIT_COULD_NOT_RUN
    # An unwritten status file leaves the Nagios check reading the last pass.
    healthy = recorded and report.outcome == OUTCOME_OK
    return EXIT_OK if healthy else EXIT_DEGRADED


if __name__ == "__main__":
    raise SystemExit(main())
