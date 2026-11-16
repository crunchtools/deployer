"""The step before the restart: a database dump, or no deploy.

Rollback here swaps images and never the database. A service whose migrations
keep the previous release working can be rolled back that way safely, and the
dump is the net under it: a copy of the data from just before this release's
migrations ran, for the day one of them is wrong in a way no rollback undoes.

The dump is taken by a host template unit, not by this program. This program
cannot exec into a container (see :mod:`deployer`), and the alternative --
``pg_dump`` over the network -- would mean opening a database that listens
only on its own container's loopback. Starting a unit the registry names is a
capability it already holds, and what that unit runs is a file a person
installed and reviewed. The contract with that unit is one path: instance
``<template>@<name>.service`` leaves a custom-format dump at
``<state>/dumps/<service>/<name>.dump``, where ``<name>`` is the outgoing
release's rollback tag suffix followed by ``t<ticket>``, so the dump is named
for the image it belongs with and no two runs share a file.

What this module checks is that the file is there, was written by this run, and
is a ``pg_dump -Fc`` archive. A unit that exits 0 having written nothing must
not pass for a dump. A failure of any of that is a failed step, and a failed
step stops the deploy before RT or Nagios is told anything.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Final, Protocol

from .registry import PreDeploy, Service
from .rollback import ROLLBACK_TAGS_KEPT
from .statefile import safe_component
from .systemd import SERVICE_IFACE, SystemdError

__all__ = [
    "DUMPS_DIR",
    "DUMPS_KEPT",
    "PG_DUMP_MAGIC",
    "PreDeployError",
    "check_timeout",
    "dump_dir",
    "pre_deploy",
    "rotate_dumps",
]

DUMPS_DIR: Final = "dumps"
#: The first bytes of every ``pg_dump -Fc`` archive.
PG_DUMP_MAGIC: Final = b"PGDMP"
#: Releases whose dumps are kept, like rollback tags, because each dump
#: belongs with one.
DUMPS_KEPT: Final = ROLLBACK_TAGS_KEPT
#: A tag suffix is a commit or an image id, so hex in practice, then ``t`` and
#: the ticket. Anything else would have to be escaped to be a systemd instance
#: name, and is refused.
_NAME: Final = re.compile(r"[A-Za-z0-9]+")
#: systemd reports TimeoutStartSec as TimeoutStartUSec, in microseconds.
_USEC_PER_SECOND: Final = 1_000_000


class PreDeployError(Exception):
    """The pre-deploy step did not produce what it must. The deploy stops."""


class Runs(Protocol):
    """The one systemd operation this step needs."""

    async def run(self, instance: str, timeout: float) -> None: ...
    async def property(self, unit: str, name: str, interface: str = ...) -> Any: ...


def dump_dir(state_dir: Path, service: Service) -> Path:
    """Where a service's dumps live."""
    return Path(state_dir) / DUMPS_DIR / safe_component(service.name, "service name")


async def check_timeout(systemd: Runs, step: PreDeploy, instance: str) -> None:
    """Refuse a unit whose TimeoutStartSec is unreadable, infinite or over the step's timeout."""
    try:
        limit = await systemd.property(instance, "TimeoutStartUSec", SERVICE_IFACE)
    except SystemdError as exc:
        raise PreDeployError(f"cannot read {instance}'s TimeoutStartSec: {exc}") from None
    if not isinstance(limit, int) or limit > step.timeout * _USEC_PER_SECOND:
        raise PreDeployError(
            f"{instance} may run for {limit!r}us but the registry waits {step.timeout}s: the unit's "
            "TimeoutStartSec must not exceed pre_deploy.timeout, so systemd ends a hung dump first"
        )


async def pre_deploy(systemd: Runs, service: Service, state_dir: Path, name: str, started: float) -> Path:
    """Run the service's pre-deploy step. Returns the dump it left.

    ``name`` is this run's own: the outgoing rollback tag suffix and the
    ticket, ``<suffix>t<ticket>``. Per run, so a retry after a rollback --
    whose outgoing release is the same -- writes a new file beside the dump
    taken before the first attempt's migrations, never over it. ``started``
    is the wall-clock time just before this call, which the dump's
    modification time must not predate.

    Nothing is rotated here. A step that fails, or a deploy that is refused or
    rolled back, must leave every dump where it was; :func:`rotate_dumps` runs
    only after a deploy holds.

    Raises :class:`PreDeployError` for anything short of a fresh, well-formed
    dump taken by a unit that systemd will stop before this program stops
    waiting for it.
    """
    step = service.pre_deploy
    if step is None:
        raise PreDeployError(f"{service.name} has no pre_deploy step")
    if not _NAME.fullmatch(name):
        raise PreDeployError(f"{name!r} cannot name a unit instance")
    instance = step.instance(name)
    await check_timeout(systemd, step, instance)
    try:
        await systemd.run(instance, step.timeout)
    except SystemdError as exc:
        raise PreDeployError(f"the pre-deploy dump did not succeed: {exc}") from None
    path = dump_dir(state_dir, service) / f"{name}.dump"
    try:
        stat = path.stat()
        with path.open("rb") as dump:
            head = dump.read(len(PG_DUMP_MAGIC))
    except OSError as exc:
        raise PreDeployError(f"the unit succeeded but left no readable dump at {path}: {exc}") from None
    # A second's slack for filesystem timestamp granularity. The host and this
    # container read one kernel clock.
    if stat.st_mtime < started - 1:
        raise PreDeployError(f"the unit succeeded but {path} predates this run, so it did not write it")
    if head != PG_DUMP_MAGIC:
        raise PreDeployError(f"{path} is not a pg_dump custom-format archive (no {PG_DUMP_MAGIC!r} header)")
    return path


def rotate_dumps(state_dir: Path, service: Service) -> int:
    """After a deploy held: keep the dumps of the newest releases, like rollback tags.

    Grouped by outgoing release (the name before ``t<ticket>``), and every
    attempt in a kept release is kept. So the dump taken before the first
    attempt's migrations survives any number of retries of that release, and
    goes only when the rollback tag it belongs with would. Partials left by
    failed runs are swept. Best-effort: returns how many files went, and a
    file that will not go is left for the Nagios disk check.
    """
    directory = dump_dir(state_dir, service)
    try:
        newest: dict[str, float] = {}
        dumps = list(directory.glob("*.dump"))
        for dump in dumps:
            release = dump.stem.rpartition("t")[0] or dump.stem
            newest[release] = max(newest.get(release, 0.0), dump.stat().st_mtime)
        partials = list(directory.glob(".*.partial"))
    except OSError:
        return 0
    kept = set(sorted(newest, key=newest.__getitem__, reverse=True)[:DUMPS_KEPT])
    removed = 0
    for old in [d for d in dumps if (d.stem.rpartition("t")[0] or d.stem) not in kept] + partials:
        try:
            old.unlink(missing_ok=True)
        except OSError:
            continue
        removed += 1
    return removed
