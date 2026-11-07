"""Putting a service back, and keeping the way back from filling the disk.

Rollback is three steps: move ``:latest`` onto the rollback image, restart, and
verify again. It is attempted once. A second automatic attempt on the same
revision would be a loop, and a loop that restarts a production service is
worse than a red check.

If the rollback itself does not verify, the maintenance downtime is
**cancelled** so Nagios pages. That is deliberate and it is the only place this
program deliberately makes noise: a deploy robot that has broken a service and
cannot put it back has no remaining move, and the next thing that should happen
is a person waking up.

The tag rotation at the end is not housekeeping. Rollback images are the size
of the application -- rotv's are nearly 4 GB -- and unrotated ones have filled
a fleet host's disk before now. Two per image is the documented keep.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from .podman import ImageRef, PodmanError
from .registry import Service
from .stage import Staged
from .verify import Host, Outcome, verify

__all__ = ["ROLLBACK_TAGS_KEPT", "Restored", "RollbackError", "roll_back", "rotate_rollback_tags"]

#: How many ``rollback-*`` tags to keep per image. The rest are removed.
ROLLBACK_TAGS_KEPT: int = 2


class RollbackError(Exception):
    """The rollback could not be carried out at all."""


class TagOps(Protocol):
    """The image operations rollback needs."""

    async def tag(self, name: str, reference: str) -> None: ...
    async def untag(self, name: str, reference: str) -> None: ...
    async def remove_image(self, name: str, force: bool = ...) -> None: ...
    async def list_images(self, reference: str | None = ...) -> tuple[ImageRef, ...]: ...


class UnitRestarter(Protocol):
    """The unit operation rollback needs."""

    async def restart(self, unit: str) -> None: ...


@dataclass(frozen=True, slots=True)
class Restored:
    """What a rollback did, and whether the service came back."""

    attempted: bool
    verified: bool
    image_id: str
    reason: str = ""

    @property
    def needs_a_person(self) -> bool:
        """A rollback that was attempted and did not come back."""
        return self.attempted and not self.verified


async def roll_back(
    podman: TagOps,
    restarter: UnitRestarter,
    host: Host,
    service: Service,
    staged: Staged,
) -> Restored:
    """Move ``:latest`` back, restart, and verify. Once.

    Returns :class:`Restored`. ``needs_a_person`` is true when the service did
    not come back, which is the caller's signal to cancel the downtime and let
    Nagios page.
    """
    if not service.rolls_back_automatically:
        # Old code against a new schema is worse than a failed deploy with the
        # downtime still open, so a migration-bearing service is never rolled
        # back by this program.
        return Restored(
            attempted=False,
            verified=False,
            image_id=staged.outgoing_image_id,
            reason=f"{service.name} is rollback = manual; left as it is for a person",
        )

    try:
        await podman.tag(staged.outgoing_image_id, staged.latest_tag)
        await restarter.restart(service.unit)
    except PodmanError as exc:
        raise RollbackError(f"{service.name}: cannot roll back: {exc}") from None

    outcome: Outcome = await verify(host, service, staged.outgoing_image_id)
    return Restored(
        attempted=True,
        verified=outcome.ok,
        image_id=staged.outgoing_image_id,
        reason=outcome.reason or outcome.probe_detail,
    )


async def rotate_rollback_tags(
    podman: TagOps, service: Service, keep: int = ROLLBACK_TAGS_KEPT
) -> tuple[str, ...]:
    """Remove all but the newest ``keep`` ``rollback-*`` tags for a service.

    Returns the tags removed. Rollback images are application-sized and
    unrotated ones have filled a host's disk before now.
    """
    basename = service.image.rsplit("/", maxsplit=1)[-1]
    pattern = f"localhost/{basename}:rollback-*"
    images = await podman.list_images(pattern)

    # Newest first. list_images gives no creation time, so the order podman
    # returns is used, which is newest-first for the image list.
    tags: list[str] = []
    for image in images:
        tags.extend(tag for tag in image.tags if tag.startswith(f"localhost/{basename}:rollback-"))

    doomed = tuple(tags[keep:])
    for tag in doomed:
        try:
            await podman.untag(tag, tag)
            await podman.remove_image(tag)
        except PodmanError:
            # A tag that will not go is not worth failing a good deploy over;
            # the disk check in Nagios is what notices if it accumulates.
            continue
    return doomed
