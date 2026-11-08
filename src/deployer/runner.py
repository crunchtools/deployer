"""Draining the queue: one ticket, start to finish, and what it leaves behind.

Every other module in this package does one thing and says what it will not do.
This is the one that puts them in order, and the order is the design:

1. **The guard first, before any ticket is read.** If a hold could not be
   honoured then a rollback cannot be made to stick, and deploying at all would
   be taking a risk this program has no way to undo. One check, once a run.
2. **Everything that can refuse, refuses before the host is touched.** A bad
   body, an unlisted requestor, a repo that disagrees with the registry, a
   revision that already broke this service, a service rolled back too often --
   all of them are decided on information this program already has, and none of
   them counts as an attempt.
3. **Staging is reversible and happens before the decision to deploy.** It
   leaves two local tags and puts ``:latest`` back where it found it, so a
   ticket can still be refused afterwards without anything having changed about
   what a unit would start.
4. **RT and Nagios are told before the restart.** If either is unreachable the
   run stops with the host untouched. After the restart their failures are
   reported but never undo a good deploy.
5. **One rollback, then a person.** A rollback that does not come back cancels
   its own maintenance window, because at that point Nagios saying so out loud
   is the only remaining fix path.

The hold is **not** a gate on deploying. A held service is one whose last
deploy failed, and the next ticket -- carrying the fix -- is exactly what
should be allowed through; verifying it is how the hold clears. What a hold
stops is ``podman auto-update``, which would otherwise put the broken
``:latest`` back at the next run. The gate on repeating a mistake is the
attempt log: the same revision is never deployed to the same service twice.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass, field
from math import ceil
from pathlib import Path
from typing import Any, Final, Protocol

import httpx

from .attempts import ROLLBACK_LIMIT, AttemptLog
from .hold import Hold, HoldStore, require_guard
from .nagios import NagiosError
from .podman import ImageRef, PodmanError
from .registry import Registry, RegistryError, Service
from .rollback import Restored, RollbackError, roll_back, rotate_rollback_tags
from .rt import RtError, Ticket, TicketSaid
from .stage import StageError, Staged, Verdict, classify_revision, stage, unit_shape_ok
from .statefile import safe_component, write_whole
from .systemd import RESTART_TIMEOUT_SECONDS, SystemdError, UnitState
from .ticket import Request, TicketError, creator_may_request, parse_ticket
from .verify import Host, Outcome, verify

__all__ = [
    "DEFAULT_QUEUE",
    "OUTCOMES",
    "Clients",
    "Downtime",
    "ImageHost",
    "RunReport",
    "Runner",
    "TicketResult",
    "TicketSource",
    "UnitHost",
    "UnreadableEnvironment",
    "container_environment",
    "redact",
    "unsubstitutable",
]

DEFAULT_QUEUE: Final = "Deploy"
#: Added to the settle window when opening downtime, to cover the pull, the
#: restart and a rollback that has to settle all over again.
#: Added on top of the two full verification windows below, for the work that
#: is not either of them: the tag moves, the log capture, and the RT and Nagios
#: round trips at both ends. Erring long costs unmonitored minutes only if this
#: process dies mid-deploy, because a window is cancelled explicitly on success
#: and on a rollback that did not verify. Erring short pages somebody in the
#: middle of a rollback that is working, which is the mistake this replaced.
DOWNTIME_MARGIN_SECONDS: Final = 300
#: How much of a refusal goes in the "already said this" record. Long enough
#: that two different refusals are different, short enough to stay one line.
SAID_DETAIL_LIMIT: Final = 120
#: Environment names whose values are never credentials. Everything NOT on this
#: list is treated as a secret, whatever it is called and however long it is.
#:
#: Default deny: a blocklist of secret-looking names fails by publishing a
#: credential (``DB=hunter2`` sails through one), an allowlist fails by
#: redacting something harmless. Adding a name here is a code change.
PUBLISHABLE_NAMES: Final = frozenset(
    {
        "PATH",
        "HOME",
        "HOSTNAME",
        "PWD",
        "SHLVL",
        "TERM",
        "LANG",
        "LC_ALL",
        "TZ",
        "container",
        "VIRTUAL_ENV",
        "PYTHONPATH",
        "PYTHONUNBUFFERED",
        "PYTHONDONTWRITEBYTECODE",
        "PYTHON_VERSION",
    }
)
#: A value shorter than this cannot be taken out of text without wrecking it:
#: replacing every "1" in a traceback leaves nothing to read. A secret this
#: short therefore stops the output being published at all, rather than being
#: published because it was awkward.
MIN_SUBSTITUTABLE_LENGTH: Final = 4
#: Where the unredacted tail is kept, under the state directory.
FAILURE_LOG_DIR: Final = "failures"


class UnreadableEnvironment(Exception):
    """The container's environment could not be read, so output cannot be published."""


#: What the run as a whole is reported as, worst-first when ranked. These are
#: the strings ``check_deployer.sh`` reads out of the status file, so they are
#: part of an interface: ``ok`` is the only one it treats as healthy, and
#: ``rollback-failed`` is the only one it treats as critical on its own.
OUTCOME_OK: Final = "ok"
OUTCOME_REFUSED: Final = "refused"
OUTCOME_COULD_NOT_RUN: Final = "could-not-run"
OUTCOME_ROLLED_BACK: Final = "rolled-back"
OUTCOME_ROLLBACK_FAILED: Final = "rollback-failed"

#: Worst wins when a run handled several tickets.
OUTCOMES: Final = (
    OUTCOME_OK,
    OUTCOME_REFUSED,
    OUTCOME_COULD_NOT_RUN,
    OUTCOME_ROLLED_BACK,
    OUTCOME_ROLLBACK_FAILED,
)


@dataclass(frozen=True, slots=True)
class TicketResult:
    """What became of one ticket.

    ``detail`` is deliberately short: it goes in the status file, whose fields
    are space-separated and truncated. The long explanation goes on the ticket,
    where a person will read it.
    """

    outcome: str
    detail: str


@dataclass(frozen=True, slots=True)
class RunReport:
    """What became of the run."""

    outcome: str
    detail: str
    tickets: int


class TicketSource(Protocol):
    """The four RT operations the runner needs."""

    async def open_tickets(self, queue: str) -> tuple[int, ...]:
        """Ticket ids in ``queue`` that are new or open, oldest first."""
        ...

    async def ticket(self, ticket_id: int) -> Ticket:
        """One ticket, with its first comment as the body. Raises on a ticket
        whose fields cannot be read, including an unreadable ``Created``."""
        ...

    async def comment(self, ticket_id: int, text: str) -> None:
        """Add an internal comment. Not correspondence: this is not mail."""
        ...

    async def set_fields(self, ticket_id: int, **fields: str) -> None:
        """Set ticket fields, e.g. ``status="resolved"`` or ``owner="scott"``.
        Raises if RT refuses any of them, so the caller can retry with fewer."""
        ...


class ImageHost(Protocol):
    """The podman surface, as the runner uses it.

    A superset of what ``stage``, ``verify`` and ``rollback`` each declare, and
    still a strict subset of the client's own ``ALLOWED_OPERATIONS``: there is
    no create, start, run or exec to ask for.
    """

    async def inspect_container(self, name: str) -> dict[str, Any]:
        """A running container by name. ``Image`` is the id it is running and
        ``Config.Env`` its environment. Raises if there is no such container."""
        ...

    async def inspect_image(self, name: str) -> ImageRef:
        """An image by id, name or tag. Raises if it is not present locally."""
        ...

    async def pull(self, reference: str) -> str:
        """Pull and return the image id it resolved to. Raises if the pull
        reported an error, or ended without naming an id."""
        ...

    async def tag(self, name: str, reference: str) -> None:
        """Point ``reference`` at the image ``name``, moving it if it exists."""
        ...

    async def untag(self, name: str, reference: str) -> None:
        """Remove one reference from an image, leaving the image itself."""
        ...

    async def remove_image(self, name: str, force: bool = ...) -> None:
        """Remove by reference. The reference goes, and the image with it when
        that was the last one; an image tagged elsewhere is left alone."""
        ...

    async def list_images(self, reference: str | None = ...) -> tuple[ImageRef, ...]:
        """Images, newest first, optionally filtered by a reference pattern."""
        ...

    async def logs(
        self,
        name: str,
        tail: int = ...,
        since: float | None = ...,
        strict: bool = ...,
    ) -> str:
        """The last ``tail`` lines a container wrote, de-multiplexed.

        ``since`` is a Unix timestamp floor, which is what makes this usable as
        a readiness signal rather than only as a failure note. ``strict``
        re-raises instead of returning a note in its place: a caller deciding
        on readiness has to tell "the line is not there" from "the log could
        not be read"."""
        ...


class UnitHost(Protocol):
    """The systemd surface, as the runner uses it."""

    async def restart(self, unit: str) -> None:
        """Restart a unit and wait for the job. Raises if the unit is not one
        the client was told it may touch, or if systemd refuses the job."""
        ...

    async def state(self, unit: str) -> UnitState:
        """Active state, sub-state and last result. Raises if a property read
        fails -- an absent value and a failed read must not look the same."""
        ...

    async def property(self, unit: str, name: str, interface: str = ...) -> Any:
        """One D-Bus property, by interface. ``Result`` lives on the Service
        interface and ``ActiveState`` on the Unit one; reading either off the
        wrong interface raises rather than returning nothing."""
        ...

    async def hold_guard_installed(self) -> bool:
        """Whether ``podman-auto-update.service`` has a non-empty
        ``ExecCondition``. Raises if systemd could not be asked, so "the guard
        is missing" and "systemd is unreachable" stay distinguishable."""
        ...


class Downtime(Protocol):
    """Opening and closing a maintenance window."""

    async def open_downtime(self, host: str, minutes: int, comment: str, service: str | None = ...) -> None:
        """A fixed window starting now. Without ``service`` the host and every
        service on it go into downtime, which is what a container restart
        needs. Raises if Nagios did not confirm the command."""
        ...

    async def cancel_downtime(self, host: str, service: str | None = ...) -> None:
        """Close the window. Raises if Nagios did not confirm the command."""
        ...


@dataclass(frozen=True, slots=True)
class Clients:
    """Everything the runner talks to, bundled so its signature stays readable.

    ``sleep`` and ``probes`` are the seams the tests use: a settle window is a
    minute of real time otherwise, and a probe needs a stubbed transport.
    """

    rt: TicketSource
    podman: ImageHost
    systemd: UnitHost
    nagios: Downtime
    probes: httpx.AsyncClient | None = None
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep


def _window_minutes(service: Service) -> int:
    """How long to suppress Nagios for, in minutes.

    TWICE the whole verification, because a deploy that fails does all of it
    again: restart, wait for the service to come up, hold it for the settle
    window. The first version of this was `settle + margin`, which predated
    both the restart wait and the `ready` window and so sized the window from
    one of the three things it has to cover. acquacotta got six minutes
    against a good path of up to 120 + 180 + 60 = 360 seconds and a rollback
    path near 720, so any acquacotta rollback would have paged somebody in the
    middle of a rollback that was succeeding -- and the one page this design
    raises deliberately, a rollback that did NOT come back, would have arrived
    indistinguishable from it.

    Sized from the service's own numbers rather than a constant, because
    `ready` and `settle` are per service and the whole point of those keys is
    that acquacotta is not an MCP server.
    """
    one_pass = RESTART_TIMEOUT_SECONDS + service.ready + service.settle
    return max(1, ceil((2 * one_pass + DOWNTIME_MARGIN_SECONDS) / 60))


@dataclass(slots=True)
class Runner:
    """One pass over the queue."""

    registry: Registry
    state_dir: Path
    clients: Clients
    queue: str = DEFAULT_QUEUE
    dry_run: bool = False
    now: Callable[[], float] = time.time
    holds: HoldStore = field(init=False)
    attempts: AttemptLog = field(init=False)
    said: TicketSaid = field(init=False)
    host: Host = field(init=False)

    def __post_init__(self) -> None:
        self.state_dir = Path(self.state_dir)
        self.holds = HoldStore(self.state_dir)
        self.attempts = AttemptLog(self.state_dir)
        self.said = TicketSaid(str(self.state_dir))
        self.host = Host(
            systemd=self.clients.systemd,
            podman=self.clients.podman,
            sleep=self.clients.sleep,
            client=self.clients.probes,
        )

    # ---------------------------------------------------------------- the run

    async def drain(self) -> RunReport:
        """Handle every open ticket in the queue, oldest first.

        Oldest first and one at a time: two deploys at once would each restore
        ``:latest`` from under the other. A ticket that cannot be handled does
        not stop the ones behind it -- the queue is other people's work, and
        one unreadable ticket blocking the rest would be a denial of service
        anyone who can file a ticket could cause.
        """
        await require_guard(self.clients.systemd)
        ids = await self.clients.rt.open_tickets(self.queue)

        results: list[TicketResult] = []
        for ticket_id in ids:
            results.append(await self._ticket(ticket_id))

        if not results:
            return RunReport(OUTCOME_OK, "nothing to do", 0)
        # Worst wins. A run that deployed one service and broke another is
        # reported as the break: the status file has room for one verdict, and
        # it should be the one that needs somebody.
        worst = max(results, key=lambda result: OUTCOMES.index(result.outcome))
        return RunReport(worst.outcome, worst.detail, len(results))

    # ------------------------------------------------------------- one ticket

    async def _ticket(self, ticket_id: int) -> TicketResult:
        try:
            ticket = await self.clients.rt.ticket(ticket_id)
        except RtError as exc:
            # Nothing can be said on a ticket that could not be read, so this
            # is reported to the run rather than to the ticket -- which makes
            # the reason the only diagnostic there is. It goes in the detail,
            # short, because the status file's fields are truncated.
            return TicketResult(OUTCOME_COULD_NOT_RUN, f"t{ticket_id} unreadable: {exc}")

        try:
            request = parse_ticket(ticket.body)
            service = self.registry.get(request.service)
        except (TicketError, RegistryError) as exc:
            why = str(exc)
            return await self._refuse(ticket, why)

        refusal = self._gate(ticket, request, service)
        if refusal:
            return await self._refuse(ticket, refusal)

        return await self._deploy(ticket, request, service)

    def _gate(self, ticket: Ticket, request: Request, service: Service) -> str | None:
        """Every refusal that needs no I/O. Returns the reason, or ``None``.

        Separate from the deploy so that each of these can be tested as what it
        is -- a judgement on information already in hand -- and so that none of
        them can reach the host by accident.
        """
        if ticket.queue and ticket.queue != self.queue:
            return f"this ticket is in queue {ticket.queue!r}, not {self.queue!r}"
        if not creator_may_request(ticket.creator, service.requestors):
            listed = ", ".join(service.requestors) or "nobody"
            return (
                f"{ticket.creator!r} may not request a deploy of {service.name}; the registry lists {listed}"
            )
        if request.repo != service.repo:
            return (
                f"the ticket says repo {request.repo!r} but the registry has "
                f"{service.repo!r} for {service.name}"
            )
        if self.attempts.was_rolled_back(service.name, request.commit):
            return (
                f"{request.short_commit} has already been deployed to {service.name} "
                "and rolled back. Fix it and file a ticket for the new commit; "
                "this one will not be tried again."
            )
        if self.attempts.too_many_rollbacks(service.name):
            return (
                f"{service.name} has been rolled back {ROLLBACK_LIMIT} times in the last "
                f"24 hours, so it is refused until someone has looked. "
                f"`deployer unhold {service.name}` clears it."
            )
        return None

    # ---------------------------------------------------------------- deploys

    async def _deploy(self, ticket: Ticket, request: Request, service: Service) -> TicketResult:
        if self.dry_run:
            return await self._dry(ticket, request, service)

        try:
            # stage() checks the unit's shape itself, as its own precondition.
            staged = await stage(self.clients.podman, self.clients.systemd, service, request.commit)
        except (StageError, PodmanError, SystemdError) as exc:
            why = f"could not stage {service.name}: {exc}"
            return await self._refuse(ticket, why)

        age = max(0, int(self.now() - ticket.created))
        verdict = classify_revision(staged.candidate_revision, request.commit, age, service)
        if verdict is Verdict.MATCH and staged.outgoing_image_id == staged.candidate_image_id:
            return await self._already_live(ticket, request, service, staged)
        if verdict is Verdict.WAITING:
            return await self._waiting(ticket, request, service, age)
        if verdict is Verdict.STALE:
            await self._untag_candidate(staged)
            return await self._refuse(
                ticket,
                f"no image carrying {request.short_commit} was pushed for {service.name} within "
                f"{service.build_deadline // 60} minutes; the ticket is {age // 60} minutes old and "
                f"{service.image}:latest carries "
                f"{(staged.candidate_revision or 'no revision label')[:12]}. "
                "The build did not produce this commit -- check the repository's actions.",
            )
        return await self._live(ticket, request, service, staged)

    async def _dry(self, ticket: Ticket, request: Request, service: Service) -> TicketResult:
        """Say what would happen. No pull, no tag, no restart, no RT, no Nagios.

        The unit's shape is still checked, because that is a read and it is the
        refusal most worth knowing about before a real run.
        """
        try:
            running = await self.clients.podman.inspect_container(service.container)
            await unit_shape_ok(self.clients.systemd, service, running)
        except (StageError, SystemdError, PodmanError) as exc:
            return TicketResult(OUTCOME_REFUSED, f"t{ticket.id} would be refused: {exc}")
        return TicketResult(
            OUTCOME_OK,
            f"t{ticket.id} would deploy {service.name} {request.short_commit} to {service.unit}",
        )

    async def _already_live(
        self, ticket: Ticket, request: Request, service: Service, staged: Staged
    ) -> TicketResult:
        """The container is already running the candidate. Change nothing.

        Two ways to arrive here. The ordinary one: a previous run deployed this
        revision and verified it, and then RT could not be told -- the comment
        or the status change failed, so the ticket is still open and comes
        round again. The other: something else put that image live, which on a
        fleet where `:latest` moves on a timer is not rare.

        Either way the service is already where the ticket asks it to be, and
        restarting it to arrive where it already is would be a production
        restart in exchange for nothing. So this verifies and finishes the
        ticket. It is what makes a deploy idempotent, and therefore what makes
        an RT failure after a good deploy recoverable rather than something
        that restarts a healthy service on the next run.
        """
        outcome = await verify(self.host, service, staged.candidate_image_id)
        untagged = await self._untag_candidate(staged)
        if outcome.failed:
            # Running the right revision and unhealthy is not something to fix
            # by restarting. A person decides that.
            return await self._refuse(
                ticket,
                f"{service.unit} is already running {request.short_commit}, and it is not healthy: "
                f"{outcome.reason or outcome.probe_detail}. Nothing was changed; this deployer "
                "will not restart a service that is already on the right revision.",
            )

        cleared = self.holds.clear(service.unit)
        await self.clients.rt.comment(
            ticket.id,
            f"Already deployed. {service.unit} was already running {request.short_commit} "
            f"(image {staged.candidate_image_id[:19]}) when this ticket came round, and it "
            f"verified: the unit is active, the container is running that exact image, and "
            f"{_proof_phrase(service)}. Nothing was restarted.\n"
            "If you expected a deploy here, the likeliest explanation is that an earlier run "
            "deployed it and could not reach RT to say so."
            + ("\nThe hold on this service is cleared." if cleared else "")
            + _as_notes((untagged,)),
        )
        await self.clients.rt.set_fields(ticket.id, status="resolved")
        return TicketResult(OUTCOME_OK, f"{service.name} {request.short_commit} already live")

    async def _waiting(self, ticket: Ticket, request: Request, service: Service, age: int) -> TicketResult:
        """The image for this commit is not pushed yet, and the build has time left.

        Said once per commit, not once per check: this path runs every couple
        of minutes, and 700 identical comments a day would make the ticket
        useless to whoever eventually reads it. The ticket's status is left
        alone so that the next run picks it up again.
        """
        left = max(0, service.build_deadline - age) // 60
        await self._say(
            ticket,
            f"waiting:{request.commit}",
            f"Waiting for a build. No image carrying {request.short_commit} has been pushed to "
            f"{service.image} yet; {left} minutes of the {service.build_deadline // 60}-minute build "
            f"deadline are left. This is said once, not every time the queue is checked.",
        )
        return TicketResult(OUTCOME_OK, f"t{ticket.id} waiting for {request.short_commit}")

    async def _live(self, ticket: Ticket, request: Request, service: Service, staged: Staged) -> TicketResult:
        """Put the candidate live, and deal with what happens.

        The two calls before the window open are deliberately first: if RT or
        Nagios cannot be reached, this returns with the host untouched and the
        ticket still queued, which is a much better outcome than a deploy
        nobody is told about.
        """
        minutes = _window_minutes(service)
        try:
            await self.clients.rt.set_fields(ticket.id, status="open")
            await self.clients.nagios.open_downtime(
                service.nagios_host, minutes, f"deployer ticket {ticket.id}"
            )
        except (RtError, NagiosError) as exc:
            await self._untag_candidate(staged)
            return TicketResult(OUTCOME_COULD_NOT_RUN, f"t{ticket.id} not started: {exc}"[:80])

        try:
            await self.clients.podman.tag(staged.candidate_image_id, staged.latest_tag)
            await self.clients.systemd.restart(service.unit)
            outcome = await verify(self.host, service, staged.candidate_image_id)
        except (PodmanError, SystemdError) as exc:
            # Past the restart, so the unit may be running the candidate. This
            # is a failed deploy, not a refusal, and it rolls back like one.
            outcome = Outcome(ok=False, reason=f"the deploy itself failed: {exc}")

        if outcome.ok:
            return await self._verified(ticket, request, service, staged)
        return await self._failed(ticket, request, service, staged, outcome)

    async def _verified(
        self, ticket: Ticket, request: Request, service: Service, staged: Staged
    ) -> TicketResult:
        """It held. Close the window, tidy the tags, clear any hold, resolve."""
        window = await self._close_window(service)
        untagged = await self._untag_candidate(staged)
        rotated, rotation = await self._rotate(service)

        cleared = self.holds.clear(service.unit)
        version = staged.candidate_revision or "no revision label"
        await self.clients.rt.comment(
            ticket.id,
            f"Deployed. {service.unit} is running {version[:12]} "
            f"(version {staged.candidate_version or 'unlabelled'}, "
            f"digest {staged.candidate_digest or 'none reported'}, "
            f"image {staged.candidate_image_id[:19]}), verified for {service.settle}s: the unit is "
            f"active, the container is running that exact image, and {_proof_phrase(service)}.\n"
            f"Rollback image kept as {staged.rollback_tag}."
            + (f"\nRemoved {len(rotated)} older rollback tag(s)." if rotated else "")
            + ("\nThe hold on this service is cleared; auto-update is no longer skipped." if cleared else "")
            + _as_notes((window, untagged, rotation)),
        )
        await self.clients.rt.set_fields(ticket.id, status="resolved")
        self.said.record(ticket.id, f"deployed:{request.commit}")
        return TicketResult(OUTCOME_OK, f"{service.name} {request.short_commit} deployed")

    async def _failed(
        self,
        ticket: Ticket,
        request: Request,
        service: Service,
        staged: Staged,
        outcome: Outcome,
    ) -> TicketResult:
        """It did not hold. Roll back once, hold the service, hand it back."""
        # Not when the unit came back running something that is not the
        # candidate at all. Nothing was learned about this revision then -- the
        # fault is in whatever started the wrong image, usually a unit edited
        # behind this program's back -- and blacklisting it would mean a
        # perfectly good commit could never be deployed to this service again.
        if not outcome.ran_something_else:
            self.attempts.mark_rolled_back(service.name, request.commit)
        self.attempts.record_rollback(service.name)

        # BEFORE the rollback, not after. podman's logs endpoint is addressed by
        # container NAME, and once the rollback has restarted the unit that name
        # is the restored container running the old image. Read afterwards, the
        # ticket would carry the output of the thing that works.
        logs = await self._log_tail(service)

        try:
            restored = await roll_back(self.clients.podman, self.clients.systemd, self.host, service, staged)
        except (RollbackError, SystemdError) as exc:
            # attempted=True on purpose: the tag or the restart may well have
            # happened before the exception, so the state of the service is
            # unknown and "needs a person" is the honest answer. Reporting it
            # as not attempted would read as "nothing was touched", which is
            # the one thing that cannot be said here.
            restored = Restored(
                attempted=True,
                verified=False,
                image_id=staged.outgoing_image_id,
                reason=str(exc),
            )

        self.holds.place(
            Hold(
                unit=service.unit,
                service=service.name,
                ticket=ticket.id,
                revision=request.commit,
                at=self.now(),
            )
        )
        # Closed either way. On a rollback that came back the window has served
        # its purpose; on one that did not, closing it is the point -- Nagios
        # saying the service is down is the only fix path left.
        window = await self._close_window(service)
        untagged = await self._untag_candidate(staged)

        await self.clients.rt.comment(
            ticket.id,
            _failure_report(
                _Failure(
                    ticket=ticket,
                    request=request,
                    service=service,
                    staged=staged,
                    outcome=outcome,
                    restored=restored,
                    logs=logs,
                )
            )
            + _as_notes((window, untagged)),
        )
        await self._hand_back(ticket)

        if restored.needs_a_person:
            return TicketResult(OUTCOME_ROLLBACK_FAILED, f"{service.name} is down, needs a person")
        return TicketResult(OUTCOME_ROLLED_BACK, f"{service.name} {request.short_commit} rolled back")

    # ------------------------------------------------------------- small acts

    async def _log_tail(self, service: Service) -> str:
        """The container's last lines, safe to put on a ticket.

        A failing service usually prints the thing that failed, and the thing
        that failed is often a credential -- a connection string, a token, a key
        read from the environment. This text goes on an RT ticket, so whatever
        the service printed is published to everyone who can read the queue.

        So: the whole tail is written to the state directory where only root can
        read it, and what goes on the ticket is the same tail with every value
        from the container's environment taken out, bar an allowlist of names
        that are never credentials. A value too short to substitute out of text
        safely stops the output being published at all rather than being
        published because redacting it would be ugly.

        Fails closed on the read. If the environment cannot be read then the
        output cannot be checked, and publishing it anyway would turn a failed
        read into a leak.
        """
        try:
            inspected = await self.clients.podman.inspect_container(service.container)
            secrets = container_environment(inspected)
        except (PodmanError, UnreadableEnvironment) as exc:
            return (
                "[no logs: the container's environment could not be read, so its output could "
                f"not be checked for secrets first: {exc}]"
            )

        raw = await self.clients.podman.logs(service.container)
        if not raw.strip():
            # Two different things look identical here and this says so rather
            # than picking one. Measured on acquacotta: a container that runs
            # systemd or httpd inside itself writes nothing to the container's
            # own stdout at all. Measured on mcp-pcloud: a container a few
            # seconds old has not got round to printing yet. The earlier
            # wording asserted the first reason for whichever service failed,
            # and put "mcp-pcloud runs its own init" on a ticket, which is
            # false. An empty tail is worth reporting; a guess at why is not.
            return (
                "[the container had written nothing to stdout or stderr when this was read. "
                f"Either it logs inside itself or it had only just started: `podman logs "
                f"{service.container}` and `podman exec {service.container} journalctl -n 50` "
                "on the host.]"
            )
        kept = self._keep_failure_log(service, raw)
        too_short = unsubstitutable(secrets)
        if too_short:
            return (
                f"[not published: {', '.join(too_short)} hold values too short to remove from the "
                "output without wrecking it, so none of it can go on a ticket.]" + kept
            )
        return redact(raw, secrets) + kept

    def _keep_failure_log(self, service: Service, raw: str) -> str:
        """Keep the unredacted tail on the host, and say where it is.

        One file per service, overwritten, so this cannot grow without bound.
        The directory is 0700 and the file 0600: the secrets in it are already
        in the container's environment and in the unit's env file, so this adds
        no exposure to anyone who can read the host -- and the point is that
        nobody else can.
        """
        path = self.state_dir / FAILURE_LOG_DIR / f"{safe_component(service.name, 'service')}.log"
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.parent.chmod(0o700)
            written = write_whole(path, raw)
            written.chmod(0o600)
        except OSError as exc:
            return f"\n\n(the unredacted output could not be kept on the host: {exc})"
        return f"\n\n(the full, unredacted tail is on the host at {written}, root-only)"

    async def _close_window(self, service: Service) -> str:
        """Cancel the maintenance window. Returns a note if it could not be.

        Never fails the run: a window left open is a monitoring problem for a
        person to notice, and it is not worth turning a good deploy -- or a
        completed rollback -- into an error. But it is not nothing either, and
        the reason has to go somewhere a person will see it. It goes on the
        ticket, which is the one place somebody is already going to read.
        """
        try:
            await self.clients.nagios.cancel_downtime(service.nagios_host)
        except NagiosError as exc:
            return (
                f"The maintenance window on {service.nagios_host} could not be cancelled: {exc}. "
                "It expires on its own, but until then Nagios will not report on this service."
            )
        return ""

    async def _untag_candidate(self, staged: Staged) -> str:
        """Drop the candidate tag once it has served its purpose.

        After a good deploy the image is what ``:latest`` points at; after a
        failed one it is known-bad. Either way nothing should keep a name on
        it, or candidate tags pile up exactly like the rollback tags that
        rotation exists to control -- which is why a failure to drop one is
        reported rather than swallowed, even though it is harmless today.
        """
        if not staged.candidate_tag:
            return ""
        try:
            await self.clients.podman.untag(staged.candidate_image_id, staged.candidate_tag)
        except PodmanError as exc:
            return f"The candidate tag {staged.candidate_tag} could not be removed: {exc}."
        return ""

    async def _rotate(self, service: Service) -> tuple[tuple[str, ...], str]:
        """Remove all but the newest rollback tags. Returns what went, and a note.

        Rollback images are the size of the application and unrotated ones have
        filled a fleet host's disk before now, so a rotation that fails is
        worth saying out loud rather than discovering later as a full disk.
        """
        try:
            return await rotate_rollback_tags(self.clients.podman, service), ""
        except PodmanError as exc:
            return (), (
                f"Older rollback images for {service.name} could not be rotated: {exc}. "
                "They are application-sized; check the host's disk."
            )

    async def _refuse(self, ticket: Ticket, why: str) -> TicketResult:
        """Say why, hand the ticket back, and leave the host alone.

        A refusal is never an attempt: nothing is recorded against the
        revision, so a ticket refused for a bad body can be refiled correctly
        and deployed.
        """
        await self._say(ticket, _said_key("refused", why), f"Refused, and nothing was changed.\n\n{why}")
        await self._hand_back(ticket)
        return TicketResult(OUTCOME_REFUSED, f"t{ticket.id} refused")

    async def _hand_back(self, ticket: Ticket) -> None:
        """Stall the ticket and give it back to whoever filed it.

        Stalling is the part that matters: it takes the ticket out of this
        program's queue so it is not reconsidered every two minutes. An owner
        RT will not accept -- an unprivileged creator, say -- must not cost us
        that, so the owner is set in the same call and retried without it.
        """
        try:
            await self.clients.rt.set_fields(ticket.id, status="stalled", owner=ticket.creator)
        except RtError:
            await self.clients.rt.set_fields(ticket.id, status="stalled")

    async def _say(self, ticket: Ticket, state: str, text: str) -> None:
        """Comment, but only if this is not what was last said on the ticket."""
        if not self.said.changed(ticket.id, state):
            return
        await self.clients.rt.comment(ticket.id, text)
        # Recorded after the comment lands, so a comment RT refused is tried
        # again on the next run rather than silently skipped for ever.
        self.said.record(ticket.id, state)


def container_environment(inspected: Mapping[str, Any]) -> dict[str, str]:
    """The container's secrets: everything in its environment bar the allowlist.

    podman gives the environment as ``["NAME=value", ...]``. Everything with a
    value comes back except the names in :data:`PUBLISHABLE_NAMES`, because
    deciding what is a credential by looking at its name or its length is a
    guess, and the consequence of guessing wrong is a key on a ticket.

    **Raises** :class:`UnreadableEnvironment` on a shape it cannot read, and
    this is the important part. Returning an empty mapping for a malformed
    inspect would read as "this container has no secrets" and the caller would
    publish the logs unredacted -- failing open, on the one path where failing
    open means leaking. A container with a genuinely empty environment gives an
    empty *list*, which is read fine and returns ``{}``.
    """
    config = inspected.get("Config")
    if not isinstance(config, dict):
        raise UnreadableEnvironment(f"no Config in the inspect output: keys were {sorted(inspected)}")
    raw = config.get("Env")
    if not isinstance(raw, list):
        raise UnreadableEnvironment(f"Config.Env is {type(raw).__name__}, not a list")
    found: dict[str, str] = {}
    for entry in raw:
        name, sep, value = str(entry).partition("=")
        if sep and value and name not in PUBLISHABLE_NAMES:
            found[name] = value
    return found


def unsubstitutable(secrets: Mapping[str, str]) -> tuple[str, ...]:
    """Names whose values are too short to remove from text safely.

    When any exist, the output is not published at all. The alternative --
    publishing because redacting would be ugly -- is the decision this program
    should never make, and the message names the variables so somebody can
    either lengthen them or decide they are not secrets.
    """
    return tuple(sorted(n for n, v in secrets.items() if len(v) < MIN_SUBSTITUTABLE_LENGTH))


def redact(text: str, secrets: Mapping[str, str]) -> str:
    """Replace each secret with the name it was set under.

    Longest first, so a value that contains another is replaced as a whole
    rather than being left half-substituted by the shorter one.
    """
    for name, value in sorted(secrets.items(), key=lambda item: len(item[1]), reverse=True):
        text = text.replace(value, f"<redacted ${name}>")
    return text


def _proof_phrase(service: Service) -> str:
    """What the ticket may truthfully claim was proved.

    A ``log`` probe reaches nothing over the network, so the sentence that
    names a URL and says it "answered throughout" would be an invention. The
    comment is the only receipt a deploy leaves; it has to say which of the two
    things actually happened.
    """
    if service.probe.reads_log:
        return (
            f"it logged {service.probe.expect!r} after starting and then held "
            f"that same container, with no restart, for the whole window "
            f"(no health probe: this service answers nothing over the network)"
        )
    if service.probe.server_name:
        return (
            f"{service.probe.url} answered throughout, with a certificate that "
            f"verified as {service.probe.server_name}"
        )
    return f"{service.probe.url} answered throughout"


def _as_notes(reasons: Iterable[str]) -> str:
    """Render whatever could not be cleaned up, for the end of a ticket comment.

    Empty when nothing went wrong, which is the usual case, so the ordinary
    comment stays the length it was.
    """
    said = [reason for reason in reasons if reason]
    if not said:
        return ""
    return "\n\nAlso, and none of these changed the outcome above:\n" + "\n".join(
        f"  - {reason}" for reason in said
    )


def _said_key(stage_name: str, detail: str) -> str:
    """A one-line key for "what was last said", discriminating on the reason.

    The reason has to be part of it: two different refusals on one ticket are
    two different things to say. Whitespace is collapsed and the whole thing is
    truncated, because this is written to a file as a single line.
    """
    return f"{stage_name}:{' '.join(detail.split())[:SAID_DETAIL_LIMIT]}"


@dataclass(frozen=True, slots=True)
class _Failure:
    """Everything the failure comment reports on, gathered once."""

    ticket: Ticket
    request: Request
    service: Service
    staged: Staged
    outcome: Outcome
    restored: Restored
    logs: str


def _failure_report(failure: _Failure) -> str:
    """The comment a developer reads when their deploy did not hold.

    Written to answer the three questions in order: what did you try, why did
    it fail, and what is running now. The log tail is last because it is the
    longest and the least structured, not because it matters least.
    """
    ticket, request, service = failure.ticket, failure.request, failure.service
    staged, outcome, restored = failure.staged, failure.outcome, failure.restored
    came_back = (
        f"Rolled back to {staged.outgoing_revision or staged.outgoing_image_id[:19]} and verified."
        if restored.verified
        else f"THE ROLLBACK DID NOT COME BACK: {restored.reason}. "
        "The maintenance window has been cancelled so Nagios will say so."
    )
    return (
        f"Deploy failed and was rolled back.\n\n"
        f"service:  {service.name} ({service.unit})\n"
        f"wanted:   {request.commit}\n"
        f"ran:      image {staged.candidate_image_id[:19]}, revision "
        f"{(staged.candidate_revision or 'none')[:12]}\n"
        f"failed:   {outcome.reason or 'health probe failed'}\n"
        f"probe:    {outcome.probe_detail or service.probe.url}\n\n"
        f"{came_back}\n\n"
        f"This service is now held: {service.unit} will be skipped by podman auto-update until "
        f"`deployer unhold {service.name}` is run, so the image that just failed cannot come back "
        f"on its own tonight.\n\n"
        f"{request.short_commit} will not be deployed to {service.name} again. Fix it, cut a new "
        f"release, and file a new ticket for the new commit.\n\n"
        f"Last lines from the container:\n{failure.logs.strip() or '(nothing)'}\n"
        f"\n-- deployer, ticket {ticket.id}"
    )
