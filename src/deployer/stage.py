"""The unit-shape gate and the candidate staging sequence.

Two jobs, both before anything is deployed.

**The shape gate.** This deployer only understands one kind of unit: one whose
``ExecStart`` ends in ``<image>:latest`` and which does not pull on start.
Seven units on gloval pull on start and one is pinned to a semver tag, and for
those the sequence below would either be undone by the unit's own pull or would
deploy a tag the unit does not use. Those are refused, not handled -- a
refusal is a decided state, and mis-handling is how a deploy robot breaks
something quietly.

**The staging sequence.** The order matters more than it looks:

1. Inspect the *running container* and tag **its** image as
   ``rollback-<outgoing-revision>``. Not the local ``:latest`` -- the two can
   differ, and that gap is exactly how a surprise gets deployed. The gateway
   once ran 6.8.0 while ``:latest`` had been 6.11.0 for eight hours; a restart
   for an unrelated reason shipped it and it was OOM-killed (RT#1559).
2. Pull ``:latest``.
3. Tag what arrived as ``candidate-<revision>``.
4. Put ``:latest`` straight back on the outgoing image.

After step 4 the local ``:latest`` is where it started, so a ticket that is
refused, waiting, or abandoned half-way cannot turn an unrelated
``Restart=always`` crash into a deploy. The candidate is reachable only by a
tag nothing else uses, and deploying is the single later act of moving
``:latest`` onto it.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from typing import Any, Final, Protocol

from .podman import ImageRef, PodmanError
from .registry import Service
from .systemd import SERVICE_IFACE

__all__ = [
    "ImageOps",
    "Staged",
    "StageError",
    "UnitOps",
    "Verdict",
    "classify_revision",
    "stage",
    "tag_suffix",
    "unit_shape_ok",
]


class ImageOps(Protocol):
    """The image operations staging needs.

    A protocol rather than the concrete client, so the tests can pass a fake
    that records tag calls without casting. It also states, in one place, how
    little of the podman surface staging actually touches.
    """

    async def inspect_container(self, name: str) -> dict[str, Any]: ...
    async def inspect_image(self, name: str) -> ImageRef: ...
    async def pull(self, reference: str) -> str: ...
    async def tag(self, name: str, reference: str) -> None: ...


class UnitOps(Protocol):
    """The unit operations the shape gate needs: one property read."""

    async def property(self, unit: str, name: str, interface: str = ...) -> Any: ...


_PULL_RE: Final = re.compile(r"\bpodman\b.*\bpull\b")
#: How much of a revision or image id goes into a tag.
TAG_SUFFIX_LENGTH: Final = 12


def tag_suffix(revision: str | None, image_id: str) -> str:
    """The ``rollback-``/``candidate-`` suffix for a tag.

    Prefers the commit, because that is what a person reading
    ``podman images`` wants to see. Falls back to the image id with its
    algorithm prefix removed: a tag cannot contain a colon, so ``sha256:abc``
    would make an invalid reference and the tag -- the only route back to the
    outgoing image -- would silently not exist.
    """
    chosen = revision or image_id.rpartition(":")[2] or image_id
    return chosen[:TAG_SUFFIX_LENGTH]


class StageError(Exception):
    """Staging refused or could not complete. A decided state, never a deploy."""


class Verdict(Enum):
    """What to do about a candidate whose revision does not match the ticket."""

    MATCH = "match"
    WAITING = "waiting"
    STALE = "stale"


@dataclass(frozen=True, slots=True)
class Staged:
    """What staging produced, and what rollback will need if it comes to that."""

    outgoing_image_id: str
    outgoing_revision: str | None
    rollback_tag: str
    candidate_image_id: str
    candidate_revision: str | None
    #: The release that went out, and the digest that identifies it anywhere
    #: else. Both go on the ticket: a commit answers "which code", a version
    #: answers "which release", and a digest is the only one of the three that
    #: means the same thing in a registry, in a signature and on another host.
    candidate_version: str | None
    candidate_digest: str | None
    candidate_tag: str
    latest_tag: str


def _exec_lines(value: Any) -> tuple[str, ...]:
    """Flatten systemd's ``ExecStart``/``ExecStartPre`` into command strings.

    The property is an array of structures whose second member is the argv.
    Anything that does not look like that is returned as its own string rather
    than dropped: a shape this cannot read must fail the gate, not pass it.
    """
    if not value:
        return ()
    lines: list[str] = []
    for entry in value:
        argv = entry[1] if isinstance(entry, (list, tuple)) and len(entry) > 1 else None
        if isinstance(argv, (list, tuple)):
            lines.append(" ".join(str(part) for part in argv))
        else:
            lines.append(str(entry))
    return tuple(lines)


async def unit_shape_ok(systemd: UnitOps, service: Service, running: Mapping[str, Any]) -> None:
    """Refuse a unit this deployer does not understand.

    Three questions, and the first is the one that matters. Raises
    :class:`StageError` naming the reason; none of these counts as a deploy
    attempt, because the unit is simply out of scope for 0.1.0.

    **What is the container running now?** ``ImageName`` is the reference the
    unit created it from, so this is evidence rather than a guess. It has to be
    evidence: ``podman run [options] IMAGE [command]`` cannot be parsed into
    "which word is the image" without knowing which of podman's options take
    values, and an earlier version of this gate that looked for the reference
    anywhere in the command line would have accepted a unit that ran something
    else and merely mentioned ``:latest`` in an environment variable.

    **Would the NEXT restart start the same thing?** The container answers for
    the present; a unit edited since it started does not. So ``ExecStart`` must
    also name ``<image>:latest``, and must not name the same image at another
    tag -- which of the two a restart would pick is not something to guess at.

    That second check is deliberately **not** a guarantee, and it cannot be
    one: a unit edited to run a different image entirely, with ours left behind
    as an option value, passes it. Verification is the layer that catches
    that -- it compares the running container's image id against the candidate
    and rolls back on a mismatch, and sets ``ran_something_else`` so the
    revision is not blamed for it.

    **Does the unit pull on start?** Its own pull would undo the staging this
    program does.
    """
    latest = f"{service.image}:latest"

    reference = str(running.get("ImageName") or "")
    if reference != latest:
        raise StageError(
            f"{service.unit}: {service.container} is running {reference or 'an unnamed image'}, "
            f"not {latest}. Moving {latest} would not change what it runs, so this service is "
            "out of scope: a pinned service is deployed by changing its unit, which this program "
            "cannot do."
        )

    exec_start = _exec_lines(await systemd.property(service.unit, "ExecStart", SERVICE_IFACE))
    if not exec_start:
        raise StageError(f"{service.unit}: cannot read ExecStart, so its shape is unknown")

    words = [word for line in exec_start for word in line.split()]
    if latest not in words:
        raise StageError(
            f"{service.unit}: the container runs {latest} but ExecStart does not name it. "
            "The unit has been changed since the container started, and a restart would not "
            "start what is running now."
        )
    elsewhere = [word for word in words if word.startswith(f"{service.image}:") and word != latest]
    if elsewhere:
        raise StageError(
            f"{service.unit}: ExecStart names both {latest} and {elsewhere[0]}. Which of the two "
            "a restart would start is not something this program should guess at."
        )

    pre = _exec_lines(await systemd.property(service.unit, "ExecStartPre", SERVICE_IFACE))
    pulls = [line for line in pre if _PULL_RE.search(line)]
    if pulls:
        raise StageError(
            f"{service.unit}: ExecStartPre pulls on start ({pulls[0]!r}). "
            "Out of scope: the unit's own pull would undo the staging this deployer does."
        )


def classify_revision(
    candidate_revision: str | None,
    wanted: str,
    ticket_age_seconds: int,
    service: Service,
) -> Verdict:
    """Whether the candidate is the commit the ticket asked for.

    A mismatch is not automatically a failure. A seat files its ticket the
    moment it merges, and the image is built by CI afterwards, so for the first
    ``build_deadline`` a mismatch means *not yet*. After that it means the build
    is not coming and a person should look.
    """
    if candidate_revision and candidate_revision.lower() == wanted.lower():
        return Verdict.MATCH
    if ticket_age_seconds < service.build_deadline:
        return Verdict.WAITING
    return Verdict.STALE


async def _restore_latest(podman: ImageOps, outgoing_id: str, latest: str) -> None:
    """Put ``:latest`` back on the outgoing image, swallowing nothing silently.

    Called from a ``finally``, so it must not mask the exception that is already
    on its way out. A failure here is re-raised only when there is no other
    exception in flight; otherwise the original one is the more useful of the
    two and this one is attached to it.
    """
    try:
        await podman.tag(outgoing_id, latest)
    except PodmanError as exc:
        raise StageError(
            f"could not put {latest} back on the outgoing image {outgoing_id}: {exc}. "
            "The local tag may point at an unverified image; check before restarting anything."
        ) from exc


async def stage(podman: ImageOps, systemd: UnitOps, service: Service, wanted: str) -> Staged:
    """Tag the outgoing image, pull, tag the candidate, and restore ``:latest``.

    Leaves the host exactly as it found it apart from two new local tags, so
    the caller is free to refuse the ticket afterwards without having changed
    what any unit would start.
    """
    latest = f"{service.image}:latest"
    basename = service.image.rsplit("/", maxsplit=1)[-1]

    try:
        running = await podman.inspect_container(service.container)
    except PodmanError as exc:
        raise StageError(f"{service.name}: cannot inspect the running container: {exc}") from None

    # Gated on what that inspect found, so the container is read once.
    await unit_shape_ok(systemd, service, running)

    outgoing_id = str(running.get("Image") or "")
    if not outgoing_id:
        raise StageError(f"{service.name}: the running container reports no image id")

    outgoing = await podman.inspect_image(outgoing_id)
    outgoing_revision = outgoing.revision
    rollback_tag = f"localhost/{basename}:rollback-{tag_suffix(outgoing_revision, outgoing_id)}"
    await podman.tag(outgoing_id, rollback_tag)

    # The pull moves the local :latest the moment it lands, so from here on
    # restoring it has to happen on every path out -- including a failure
    # between the pull and the candidate tag. Leaving :latest on an unverified
    # image is the exact hazard this sequence exists to prevent.
    try:
        candidate_id = await podman.pull(latest)
        candidate = await podman.inspect_image(candidate_id)
        candidate_tag = f"localhost/{basename}:candidate-{tag_suffix(candidate.revision, candidate_id)}"
        await podman.tag(candidate_id, candidate_tag)
    finally:
        await _restore_latest(podman, outgoing_id, latest)

    return Staged(
        outgoing_image_id=outgoing_id,
        outgoing_revision=outgoing_revision,
        rollback_tag=rollback_tag,
        candidate_image_id=candidate_id,
        candidate_revision=candidate.revision,
        candidate_version=candidate.version,
        candidate_digest=candidate.digest,
        candidate_tag=candidate_tag,
        latest_tag=latest,
    )
