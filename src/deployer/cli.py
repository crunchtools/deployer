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

**Exit codes.** ``0`` means the verb did what it was asked, including a deploy
that failed and was rolled back cleanly -- the rollback is a success of this
program, and a oneshot unit going ``failed`` for it would wake a model to be
told something the ticket already says. ``1`` is kept for a service that needs
a person: a rollback that did not come back. ``2`` is "could not run" and
covers a missing path, an unloadable registry, an unreachable RT, and the
auto-update hold guard being absent.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path

import httpx

from . import __version__
from .attempts import AttemptLog
from .hold import HoldStore, MissingGuardError
from .nagios import NagiosCommands, NagiosCredentials, NagiosError, write_status
from .lock import AlreadyRunning, only_one
from .podman import PodmanError, PodmanImages
from .registry import Registry, RegistryError, Service, load_registry
from .rollback import RollbackError, roll_back
from .rt import RtClient, RtCredentials, RtError
from .runner import (
    OUTCOME_COULD_NOT_RUN,
    OUTCOME_ROLLBACK_FAILED,
    Clients,
    RunReport,
    Runner,
)
from .stage import Staged
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
        return _unhold(registry, Path(args.state), args.service, dry_run=args.dry_run)
    if args.verb == "rollback":
        return await _rollback(registry, args.service, env, dry_run=args.dry_run)
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


def _unhold(registry: Registry, state: Path, name: str, dry_run: bool) -> int:
    """Clear a hold, and the rollback count that goes with it.

    Both, because they are one decision. A service that hit the
    three-rollbacks-in-a-day limit is refused by the gate rather than by the
    hold, so clearing only the hold would leave it refused for the rest of the
    day with nothing to show why.
    """
    try:
        service = registry.get(name)
    except RegistryError as exc:
        print(f"deployer: {exc}", file=sys.stderr)
        return EXIT_COULD_NOT_RUN

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


async def _rollback(registry: Registry, name: str, env: Mapping[str, str], dry_run: bool) -> int:
    """Put a service back on its newest rollback image, by hand.

    No ticket, no Nagios window and no hold: this is a person doing it on
    purpose, and the window and the hold exist to manage a robot's decisions.
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
        async with PodmanImages() as podman, SystemdUnits(_units(registry)) as systemd:
            staged = await _newest_rollback(podman, service)
            if dry_run:
                print(f"would put {service.unit} back on {staged.rollback_tag}")
                return EXIT_OK
            async with httpx.AsyncClient() as probes:
                host = Host(systemd=systemd, podman=podman, client=probes)
                restored = await roll_back(podman, systemd, host, service, staged)
    except (*COULD_NOT_RUN, RollbackError) as exc:
        print(f"deployer: {exc}", file=sys.stderr)
        return EXIT_COULD_NOT_RUN

    if restored.needs_a_person:
        print(
            f"deployer: {service.unit} did not come back on {staged.rollback_tag}: {restored.reason}",
            file=sys.stderr,
        )
        return EXIT_ERROR
    print(f"{service.unit} is back on {staged.rollback_tag} and verified")
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
    return EXIT_ERROR if report.outcome == OUTCOME_ROLLBACK_FAILED else EXIT_OK


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


async def _newest_rollback(podman: PodmanImages, service: Service) -> Staged:
    """The newest ``rollback-*`` image for a service, shaped as a rollback target.

    ``roll_back`` takes a :class:`Staged` because that is what a deploy
    produces. Here there was no deploy, so only the outgoing half is filled in;
    the candidate fields are empty and nothing reads them on this path.
    """
    basename = service.image.rsplit("/", maxsplit=1)[-1]
    pattern = f"localhost/{basename}:rollback-"
    images = await podman.list_images(f"{pattern}*")
    for image in images:
        for tag in image.tags:
            if tag.startswith(pattern):
                return Staged(
                    outgoing_image_id=image.id,
                    outgoing_revision=image.revision,
                    rollback_tag=tag,
                    candidate_image_id="",
                    candidate_revision=None,
                    candidate_tag="",
                    latest_tag=f"{service.image}:latest",
                )
    raise PodmanError(
        f"no {pattern}* image for {service.name}: there is nothing to roll back to. "
        "A rollback image is only created by a deploy."
    )


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
