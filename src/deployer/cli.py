"""Command line entry point.

The verbs are deliberately few, and each one is something a person might also
need to do by hand during an incident:

``run``
    Drain the queue. Takes a lock, so two of these cannot overlap.
``rollback <service>``
    Put a service back on its newest rollback image without waiting for a
    ticket.
``unhold <service>``
    Clear a hold once the fix has shipped. The only way a hold ever clears
    other than the service verifying on a later ticket.
``holds``
    What is held, since when, and for which ticket.

``--dry-run`` is how this is exercised on a live host without touching a unit.

Where the registry and the state directory live is a property of the
deployment, not of this program, so both come from the environment
(``DEPLOYER_CONFIG`` and ``DEPLOYER_STATE``) and the unit supplies them. There
is no built-in path: a missing one exits 2 -- "could not run" -- rather than
guessing at a location and reporting on whatever happens to be there.

**Exit codes**, as the CLI Tool profile sets them:

``0``
    Every ticket drained reached a decided state. That includes a **refusal** --
    a service not in the registry, a commit nothing built, a requestor not
    listed: refusing correctly is the tool working, and the ticket is stalled
    and owned by whoever filed it, which is the channel. It also includes a
    ticket waiting for its build, and a second run standing down because one
    is already going. For ``rollback`` it is a rollback that verified.
``1``
    A deploy failed, or a rollback failed to verify. Exit 1 even when the
    rollback itself went perfectly, because it leaves a held service and a
    stalled ticket and a oneshot unit exiting 0 with a service held would
    report success for a state nobody has dealt with. The wake-up that costs is
    one per failure, not one per idle minute, which is the shape the whole
    design is built around.
``2``
    Could not run: a missing path, an unloadable registry, an unreachable RT,
    the auto-update hold guard being absent, or a verb asked to do something
    this service's registry row forbids. Read in python-sdlc's sense as well as
    the profile's "usage error" -- both mean *this never got far enough to
    try*, which is what a caller needs to tell apart from a failed deploy.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from contextlib import AsyncExitStack
from dataclasses import dataclass
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Final

import httpx

from . import __version__
from .attempts import AttemptLog
from .hold import Hold, HoldStore, MissingGuardError, require_guard
from .nagios import NagiosCommands, NagiosCredentials, NagiosError, write_status
from .lock import AlreadyRunning, only_one
from .podman import PodmanError, PodmanImages
from .registry import Registry, RegistryError, Service, load_registry
from .rollback import RollbackError, roll_back
from .rt import RtClient, RtCredentials, RtError
from .runner import (
    OUTCOME_COULD_NOT_RUN,
    ImageHost,
    UnitHost,
    OUTCOME_OK,
    OUTCOME_REFUSED,
    OUTCOME_ROLLBACK_FAILED,
    OUTCOME_ROLLED_BACK,
    Clients,
    RunReport,
    Runner,
)
from .systemd import SystemdError, SystemdUnits
from .verify import Host

EXIT_OK = 0
EXIT_ERROR = 1
# "Could not run": bad arguments, missing or invalid config, or a precondition
# that is not met. The caller needs this distinct from a failed deploy.
EXIT_COULD_NOT_RUN = 2

CONFIG_ENV = "DEPLOYER_CONFIG"
STATE_ENV = "DEPLOYER_STATE"
QUEUE_ENV = "DEPLOYER_QUEUE"
STATUS_FILE_NAME = "deployer.status"

#: What a run exits with, as the CLI Tool profile sets the codes. A table
#: rather than a chain of conditions, so the contract can be read in one place
#: and a test can check it covers every outcome a run can produce.
EXIT_FOR_OUTCOME: Final[dict[str, int]] = {
    OUTCOME_OK: EXIT_OK,
    # A refusal is a DECIDED state, not a failure: the ticket named a service
    # that is not in the registry, or a commit nothing built, or came from
    # somebody not listed. Refusing correctly is the tool working, and the
    # ticket is stalled and owned by whoever filed it. That is the channel.
    OUTCOME_REFUSED: EXIT_OK,
    # A deploy that failed. Exit 1 even when the rollback itself went
    # perfectly: it leaves a held service and a stalled ticket, and a unit
    # exiting 0 with a service held would report success for a state nobody
    # has dealt with.
    OUTCOME_ROLLED_BACK: EXIT_ERROR,
    OUTCOME_ROLLBACK_FAILED: EXIT_ERROR,
    OUTCOME_COULD_NOT_RUN: EXIT_COULD_NOT_RUN,
}

#: Everything that means "this program could not do its job", as opposed to "it
#: did its job and the deploy was bad". Caught in one place so every verb
#: reports a configuration or connectivity problem the same way.
COULD_NOT_RUN = (RegistryError, RtError, NagiosError, PodmanError, SystemdError, MissingGuardError)


def build_parser() -> argparse.ArgumentParser:
    """Build the deployer's command-line parser.

    Separate from :func:`main` so the tests can check the grammar -- which verbs
    exist, which take a service -- without running anything.
    """
    parser = argparse.ArgumentParser(
        prog="deployer",
        description="Drain the RT Deploy queue: deploy by commit, verify, roll back once.",
    )
    parser.add_argument("--version", action="version", version=f"deployer {__version__}")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Say what would happen and change nothing. No pull, no tag, no restart.",
    )
    parser.add_argument(
        "--config",
        default=os.environ.get(CONFIG_ENV),
        help=f"Service registry path (default: ${CONFIG_ENV})",
    )
    parser.add_argument(
        "--state",
        default=os.environ.get(STATE_ENV),
        help=f"State directory holding holds, attempts and ticket state (default: ${STATE_ENV})",
    )

    sub = parser.add_subparsers(dest="verb", required=True)
    sub.add_parser("run", help="Drain the Deploy queue, oldest ticket first")

    p_rollback = sub.add_parser("rollback", help="Put a service back on its rollback image")
    p_rollback.add_argument("service")

    p_unhold = sub.add_parser("unhold", help="Clear a service's hold")
    p_unhold.add_argument("service")

    sub.add_parser("holds", help="List what is held, since when, and for which ticket")
    return parser


def missing_paths(args: argparse.Namespace) -> list[str]:
    """Names of the required path settings that were given neither way."""
    return [name for name, value in ((CONFIG_ENV, args.config), (STATE_ENV, args.state)) if not value]


def main(argv: Sequence[str] | None = None) -> int:
    """Parse arguments and dispatch. Returns the process exit code."""
    parser = build_parser()
    args = parser.parse_args(argv)

    absent = missing_paths(args)
    if absent:
        print(
            f"deployer: no {' and no '.join(absent)}; set the variable or pass the flag",
            file=sys.stderr,
        )
        return EXIT_COULD_NOT_RUN

    try:
        registry = load_registry(args.config)
    except RegistryError as exc:
        print(f"deployer: {exc}", file=sys.stderr)
        return EXIT_COULD_NOT_RUN

    return asyncio.run(_dispatch(args, registry, os.environ))


async def _dispatch(args: argparse.Namespace, registry: Registry, env: Mapping[str, str]) -> int:
    """Run one verb. Async because every verb that touches the host is."""
    if args.verb == "holds":
        return _show_holds(Path(args.state))
    if args.verb == "unhold":
        return _unhold(registry, args.service, Path(args.state), dry_run=args.dry_run)
    if args.verb == "rollback":
        return await _rollback(registry, args.service, Path(args.state), dry_run=args.dry_run)
    return await _run(args, registry, env)


# ------------------------------------------------------------------ verbs


def _show_holds(state: Path) -> int:
    """List the holds. Reads nothing but the state directory, so it works on a
    host where podman, systemd or RT are the thing that is broken."""
    holds = sorted(HoldStore(state), key=lambda hold: hold.at)
    if not holds:
        print("no holds")
        return EXIT_OK
    for hold in holds:
        hours = hold.age_seconds / 3600
        print(
            f"{hold.unit}  service={hold.service}  ticket={hold.ticket}  "
            f"revision={hold.revision[:12]}  age={hours:.1f}h"
        )
    print(
        f"\n{len(holds)} hold(s). podman auto-update is skipping these services; "
        "`deployer unhold <service>` clears one."
    )
    return EXIT_OK


def _unhold(registry: Registry, name: str, state: Path, dry_run: bool) -> int:
    """Clear a hold, and the rollback count that goes with it.

    Both, because they are one decision. A service that hit the
    three-rollbacks-in-a-day limit is refused by the gate rather than by the
    hold, so clearing only the hold would leave it refused for the rest of the
    day with nothing to show why.

    **Under the run lock.** This writes the same two records a failing deploy
    writes: it clears a hold and rewrites the rollback log. Run while a deploy
    is finishing, it can drop a hold that deploy just placed -- which is the
    worst possible outcome, because the broken image then comes back at the
    next auto-update with nothing saying why.
    """
    try:
        service = registry.get(name)
    except RegistryError as exc:
        print(f"deployer: {exc}", file=sys.stderr)
        return EXIT_COULD_NOT_RUN

    try:
        with only_one(state):
            return _clear(service, state, dry_run)
    except AlreadyRunning as exc:
        print(
            f"deployer: {exc}. It is writing the same holds; wait for it to finish and try again.",
            file=sys.stderr,
        )
        return EXIT_COULD_NOT_RUN


def _clear(service: Service, state: Path, dry_run: bool) -> int:
    """Clearing itself, with the lock already held."""
    holds = HoldStore(state)
    hold = holds.get(service.unit)
    attempts = AttemptLog(state)
    if dry_run:
        print(
            f"would clear the hold on {service.unit} "
            f"({'held' if hold else 'not held'}) and forget "
            f"{attempts.recent_rollbacks(service.name)} recent rollback(s)"
        )
        return EXIT_OK

    cleared = holds.clear(service.unit)
    forgotten = attempts.forget_rollbacks(service.name)
    if not cleared:
        print(f"{service.unit} was not held; forgot {forgotten} recent rollback(s)")
        return EXIT_OK
    print(
        f"cleared the hold on {service.unit} (ticket {hold.ticket if hold else '?'}) and forgot "
        f"{forgotten} recent rollback(s). podman auto-update will no longer skip it."
    )
    return EXIT_OK


@dataclass(frozen=True, slots=True)
class Hands:
    """The clients a by-hand rollback needs, so the verb can be tested.

    Built from the real thing by :func:`_rollback` when nothing is passed in.
    This path mutates a production service, so it deserves tests rather than
    only a careful reading.
    """

    podman: ImageHost
    systemd: UnitHost
    probes: httpx.AsyncClient | None = None


async def _rollback(
    registry: Registry, name: str, state: Path, dry_run: bool, hands: Hands | None = None
) -> int:
    """Put a service back on its newest rollback image, by hand.

    No ticket and no Nagios window -- a person doing this on purpose will open
    their own window if they want one. A hold, though, yes: ``:latest`` still
    carries the image being rolled away from, so without one the next
    auto-update puts it straight back.

    **Under the same lock as a run.** This moves ``:latest`` and restarts a
    unit, and a run that is mid-staging has already read ``:latest`` and will
    put it back where it found it. The two interleaved would leave the tag
    pointing at whichever of them wrote last, which is not necessarily what is
    running. One at a time, and this one says so and stops rather than waiting.
    """
    try:
        service = registry.get(name)
    except RegistryError as exc:
        print(f"deployer: {exc}", file=sys.stderr)
        return EXIT_COULD_NOT_RUN

    if not service.rolls_back_automatically:
        # Declared manual in the registry because it carries migrations. The
        # registry is the place that knows; honouring it here is the point of
        # writing it down.
        print(
            f"deployer: {service.name} is rollback = manual in the registry, which means its "
            "schema changes are not safely reversible by a robot. Roll it back by hand:\n"
            f"  podman tag <rollback-image> {service.image}:latest\n"
            f"  systemctl restart {service.unit}",
            file=sys.stderr,
        )
        return EXIT_COULD_NOT_RUN

    try:
        with only_one(state):
            return await _restore(registry, service, state, dry_run, hands)
    except AlreadyRunning as exc:
        print(
            f"deployer: {exc}. A deploy is in flight and it is moving the same tag; "
            "wait for it to finish and try again.",
            file=sys.stderr,
        )
        return EXIT_COULD_NOT_RUN
    except (*COULD_NOT_RUN, RollbackError) as exc:
        print(f"deployer: {exc}", file=sys.stderr)
        return EXIT_COULD_NOT_RUN


async def _restore(
    registry: Registry, service: Service, state: Path, dry_run: bool, hands: Hands | None
) -> int:
    """The rollback itself, with the lock already held."""
    async with AsyncExitStack() as stack:
        if hands is None:
            podman = await stack.enter_async_context(PodmanImages())
            systemd = await stack.enter_async_context(SystemdUnits(_units(registry)))
            probes = await stack.enter_async_context(httpx.AsyncClient())
            hands = Hands(podman=podman, systemd=systemd, probes=probes)
        # The guard matters here for the same reason it matters on a run. This
        # rollback puts an older image live while `:latest` still carries the
        # newer, broken one, so unless a hold is honoured the broken image
        # comes back at the next podman auto-update.
        await require_guard(hands.systemd)
        staged = await _newest_rollback(hands.podman, service)
        if dry_run:
            print(f"would put {service.unit} back on {staged.rollback_tag} and hold it")
            return EXIT_OK
        host = Host(systemd=hands.systemd, podman=hands.podman, client=hands.probes)
        restored = await roll_back(hands.podman, hands.systemd, host, service, staged)

    # Ticket 0 means "no ticket -- a person did this by hand".
    HoldStore(state).place(
        Hold(
            unit=service.unit,
            service=service.name,
            ticket=0,
            revision=staged.outgoing_revision or staged.outgoing_image_id,
        )
    )

    if restored.needs_a_person:
        print(
            f"deployer: {service.unit} did not come back on {staged.rollback_tag}: {restored.reason}",
            file=sys.stderr,
        )
        return EXIT_ERROR
    print(
        f"{service.unit} is back on {staged.rollback_tag} and verified, and is now held. "
        f"podman auto-update will skip it until `deployer unhold {service.name}`."
    )
    return EXIT_OK


async def _run(args: argparse.Namespace, registry: Registry, env: Mapping[str, str]) -> int:
    """Drain the queue under a lock, and leave a record Nagios can read."""
    state = Path(args.state)
    status = state / STATUS_FILE_NAME
    try:
        with only_one(state):
            report = await _drain(args, registry, env, state)
    except AlreadyRunning as exc:
        # Expected: the timer fires every couple of minutes and a deploy takes
        # longer than that. Not an error, and nothing is written -- the run
        # that holds the lock owns the status file.
        print(f"deployer: {exc}; standing down", file=sys.stderr)
        return EXIT_OK
    except COULD_NOT_RUN as exc:
        if not args.dry_run:
            write_status(status, OUTCOME_COULD_NOT_RUN, str(exc), holds=list(HoldStore(state)))
        print(f"deployer: {exc}", file=sys.stderr)
        return EXIT_COULD_NOT_RUN

    if args.dry_run:
        print(f"dry run: {report.detail}")
        return EXIT_OK

    write_status(status, report.outcome, report.detail, holds=list(HoldStore(state)))
    print(f"{report.outcome}: {report.detail} ({report.tickets} ticket(s))")
    return EXIT_FOR_OUTCOME[report.outcome]


async def _drain(
    args: argparse.Namespace, registry: Registry, env: Mapping[str, str], state: Path
) -> RunReport:
    """Open every client, drain, and close them again."""
    rt_credentials = RtCredentials.from_env(env)
    nagios_credentials = NagiosCredentials.from_env(env)
    async with (
        RtClient(rt_credentials) as rt,
        PodmanImages() as podman,
        SystemdUnits(_units(registry)) as systemd,
        NagiosCommands(nagios_credentials) as nagios,
        httpx.AsyncClient() as probes,
    ):
        runner = Runner(
            registry=registry,
            state_dir=state,
            clients=Clients(rt=rt, podman=podman, systemd=systemd, nagios=nagios, probes=probes),
            queue=env.get(QUEUE_ENV) or "Deploy",
            dry_run=args.dry_run,
        )
        return await runner.drain()


# ------------------------------------------------------------------ helpers


def _units(registry: Registry) -> frozenset[str]:
    """The units this process is allowed to touch: the registry's, and no others."""
    return frozenset(registry.get(name).unit for name in registry.names)


@dataclass(frozen=True, slots=True)
class _ByHand:
    """A rollback target with no deploy behind it.

    Satisfies :class:`deployer.rollback.Target`, which is the two fields a
    rollback actually needs. The rest is for the message printed afterwards.
    """

    outgoing_image_id: str
    latest_tag: str
    rollback_tag: str
    outgoing_revision: str | None


async def _newest_rollback(podman: ImageHost, service: Service) -> _ByHand:
    """The newest ``rollback-*`` image for a service."""
    basename = service.image.rsplit("/", maxsplit=1)[-1]
    pattern = f"localhost/{basename}:rollback-"
    images = await podman.list_images(f"{pattern}*")
    for image in images:
        for tag in image.tags:
            if tag.startswith(pattern):
                return _ByHand(
                    outgoing_image_id=image.id,
                    latest_tag=f"{service.image}:latest",
                    rollback_tag=tag,
                    outgoing_revision=image.revision,
                )
    raise PodmanError(
        f"no {pattern}* image for {service.name}: there is nothing to roll back to. "
        "A rollback image is only created by a deploy."
    )


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
