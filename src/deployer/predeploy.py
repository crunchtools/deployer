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
``<template>@<suffix>.service`` leaves a custom-format dump at
``<state>/dumps/<service>/<suffix>.dump``, where ``<suffix>`` is the outgoing
release's rollback tag suffix, so the dump and the image it belongs with are
named alike.

What this module checks is that the file is there, was written by this run, and
is a ``pg_dump -Fc`` archive. A unit that exits 0 having written nothing must
not pass for a dump. A failure of any of that is a failed step, and a failed
step stops the deploy before RT or Nagios is told anything.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Final, Protocol

from .registry import Service
from .rollback import ROLLBACK_TAGS_KEPT
from .statefile import safe_component
from .systemd import SystemdError

__all__ = ["DUMPS_DIR", "DUMPS_KEPT", "PG_DUMP_MAGIC", "PreDeployError", "dump_dir", "pre_deploy"]

DUMPS_DIR: Final = "dumps"
#: The first bytes of every ``pg_dump -Fc`` archive.
PG_DUMP_MAGIC: Final = b"PGDMP"
#: Kept like rollback tags, because each dump belongs with one.
DUMPS_KEPT: Final = ROLLBACK_TAGS_KEPT
#: A tag suffix is a commit or an image id, so hex in practice. Anything else
#: would have to be escaped to be a systemd instance name, and is refused.
_SUFFIX: Final = re.compile(r"[A-Za-z0-9]+")


class PreDeployError(Exception):
    """The pre-deploy step did not produce what it must. The deploy stops."""


class Runs(Protocol):
    """The one systemd operation this step needs."""

    async def run(self, instance: str, timeout: float) -> None: ...


def dump_dir(state_dir: Path, service: Service) -> Path:
    """Where a service's dumps live."""
    return Path(state_dir) / DUMPS_DIR / safe_component(service.name, "service name")


async def pre_deploy(systemd: Runs, service: Service, state_dir: Path, suffix: str, started: float) -> Path:
    """Run the service's pre-deploy step. Returns the dump it left.

    ``started`` is the wall-clock time just before this call, which the dump's
    modification time must not predate. Raises :class:`PreDeployError` for
    anything short of a fresh, well-formed dump.
    """
    step = service.pre_deploy
    if step is None:
        raise PreDeployError(f"{service.name} has no pre_deploy step")
    if not _SUFFIX.fullmatch(suffix):
        raise PreDeployError(f"{suffix!r} cannot name a unit instance")
    instance = step.instance(suffix)
    try:
        await systemd.run(instance, step.timeout)
    except SystemdError as exc:
        raise PreDeployError(f"the pre-deploy dump did not succeed: {exc}") from None
    directory = dump_dir(state_dir, service)
    path = directory / f"{suffix}.dump"
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

    # Keep the newest, like the rollback tags these belong with, and sweep the
    # partials failed runs left: after a run that succeeded none is worth
    # anything. A file that will not go is not worth failing a deploy over;
    # the Nagios disk check notices accumulation.
    dumps = sorted(directory.glob("*.dump"), key=lambda p: p.stat().st_mtime, reverse=True)
    for old in [*dumps[DUMPS_KEPT:], *directory.glob(".*.partial")]:
        old.unlink(missing_ok=True)
    return path
