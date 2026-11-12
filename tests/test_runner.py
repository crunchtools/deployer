"""The state machine: one ticket, start to finish.

The properties worth pinning, in the order they matter:

* nothing refuses *after* the host has been touched, and no refusal is recorded
  as an attempt;
* a failed deploy rolls back once, holds the service, and hands the ticket back
  with enough on it to fix the thing;
* a rollback that does not come back cancels its own maintenance window,
  because Nagios saying so is the only fix path left;
* a hold is not a gate -- the next ticket, carrying the fix, is exactly what
  should get through, and verifying it is how the hold clears.

The fakes model the host rather than the calls: ``restart`` moves the running
container to whatever ``:latest`` currently points at, and the probe answers
according to which image is running. That way a test for "the new image is
broken" is written as a broken image, and the sequence has to be right for it
to pass.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

import httpx
import pytest

from deployer.attempts import ROLLBACK_LIMIT, AttemptLog
from deployer.cli import EXIT_ERROR, EXIT_FOR_OUTCOME, EXIT_OK, EXIT_COULD_NOT_RUN
from deployer.hold import Hold, HoldStore, MissingGuardError
from deployer.nagios import NagiosError
from deployer.podman import PodmanError, ImageRef
from deployer.registry import Probe, Registry, Service
from deployer.rollback import ROLLBACK_TAGS_KEPT
from deployer.rt import RtError, Ticket
from deployer.systemd import SystemdError
from deployer.runner import (
    OUTCOMES,
    UnreadableEnvironment,
    OUTCOME_COULD_NOT_RUN,
    OUTCOME_OK,
    OUTCOME_REFUSED,
    OUTCOME_ROLLBACK_FAILED,
    OUTCOME_ROLLED_BACK,
    Clients,
    Runner,
    _window_minutes,
    container_environment,
    redact,
)

IMAGE = "quay.io/crunchtools/acquacotta"
UNIT = "acquacotta.crunchtools.com.service"
WANTED = "635cdda3c8632d1b5aed4cc22943895205ce5138"
OTHER = "aaaa111122223333444455556666777788889999"
OUTGOING_ID = "sha256:" + "a" * 64
CANDIDATE_ID = "sha256:" + "b" * 64
NOW = 1_800_000_000.0

SERVICE = Service(
    name="acquacotta",
    repo="crunchtools/acquacotta",
    image=IMAGE,
    unit=UNIT,
    probe=Probe(kind="http", url="http://127.0.0.1:8080/health"),
    nagios_host="ctr-acquacotta.crunchtools.com",
    requestors=("developer1",),
    # Zero so a verification is one sample and no test waits a minute. The
    # settle loop's own timing is pinned in test_verify.py.
    settle=0,
    # Zero for the same reason, and it matters more: a readiness window retries
    # a failing probe, so a non-zero one here would make every test that
    # deliberately fails a probe wait the whole window out.
    ready=0,
    build_deadline=1800,
)
REGISTRY = Registry({"acquacotta": SERVICE})
#: The same service with a `log` probe, for the paths whose wording has to
#: differ when there is no URL and nothing answered over the network.
READY_LINE = "taking commands"
SEAT_REGISTRY = Registry({"acquacotta": replace(SERVICE, probe=Probe(kind="log", expect=READY_LINE))})


def _fields(service: Service) -> dict[str, Any]:
    """The service's own values, so a test can clone it with one changed."""
    return {name: getattr(service, name) for name in service.__slots__}


def body(service: str = "acquacotta", repo: str = "crunchtools/acquacotta", commit: str = WANTED) -> str:
    return f"service: {service}\nrepo: {repo}\ncommit: {commit}\n"


def ticket(
    ticket_id: int = 1601,
    text: str | None = None,
    creator: str = "developer1",
    age_seconds: float = 60.0,
    queue: str = "Deploy",
) -> Ticket:
    return Ticket(
        id=ticket_id,
        queue=queue,
        status="new",
        subject="deploy acquacotta",
        creator=creator,
        owner="Nobody",
        body=body() if text is None else text,
        created=NOW - age_seconds,
    )


# --------------------------------------------------------------------- fakes


class FakeRt:
    """RT, as far as the runner can tell."""

    def __init__(self, *tickets: Ticket) -> None:
        self.tickets = {item.id: item for item in tickets}
        self.unreadable: tuple[int, ...] = ()
        self.refuse_owner = False
        self.comments: list[tuple[int, str]] = []
        self.fields: list[tuple[int, dict[str, str]]] = []
        self.fail_set_fields = False
        #: Fail only the call that sets this status, so a failure *after* a
        #: verified deploy can be told from one before it.
        self.fail_status = ""

    async def open_tickets(self, queue: str) -> tuple[int, ...]:
        return tuple(sorted(self.tickets)) + self.unreadable

    async def ticket(self, ticket_id: int) -> Ticket:
        if ticket_id in self.unreadable:
            raise RtError(f"ticket {ticket_id}: cannot read Created ''")
        return self.tickets[ticket_id]

    async def comment(self, ticket_id: int, text: str) -> None:
        self.comments.append((ticket_id, text))

    async def set_fields(self, ticket_id: int, **fields: str) -> None:
        if self.fail_set_fields or fields.get("status") == self.fail_status:
            raise RtError("RT returned HTTP 503")
        if self.refuse_owner and "owner" in fields:
            raise RtError("RT refused the owner")
        self.fields.append((ticket_id, dict(fields)))

    def said(self, fragment: str) -> bool:
        return any(fragment in text for _, text in self.comments)

    def status(self) -> list[str]:
        return [fields["status"] for _, fields in self.fields if "status" in fields]


class FakePodman:
    """The image store and the running container, as one little world."""

    def __init__(self) -> None:
        self.running = OUTGOING_ID
        self.by_ref: dict[str, str] = {f"{IMAGE}:latest": OUTGOING_ID}
        #: What the image in the registry carries. A test sets this to
        #: something other than the ticket's commit to mean "CI has not built
        #: it yet".
        self.candidate_revision = WANTED
        #: What the candidate image's OCI labels and digest carry. Both go on
        #: the ticket, and both are optional on a real image, so a test can set
        #: either to None to exercise the fallback wording.
        self.candidate_version: str | None = "2.34.0"
        self.candidate_digest: str | None = (
            "sha256:0f1e2d3c4b5a69788796a5b4c3d2e1f00f1e2d3c4b5a69788796a5b4c3d2e1f0"
        )
        self.revisions = {OUTGOING_ID: OTHER}
        #: Whether the registry holds gated-<commit> for the wanted commit
        #: (a repository on constitution's container-release). Off by
        #: default, so the label path stays the one most tests exercise.
        self.gated = False
        #: What the registry's :main and :latest resolve to when gated. None is
        #: the candidate itself (promoted); anything else is another image.
        self.main_id: str | None = None
        self.latest_id: str | None = None
        self.tagged: list[tuple[str, str]] = []
        self.untagged: list[tuple[str, str]] = []
        self.removed: list[str] = []
        self.rollback_images: tuple[ImageRef, ...] = ()
        self.log_text = "ValueError: no database\n"
        #: The container's identity, in the shape a `log` probe reads it.
        self.container_id = "c0ffee"
        self.started_at = "2027-01-01T00:00:00.000000000Z"
        self.fail_tag_on: str | None = None
        self.fail_inspect = False
        self.reference = f"{IMAGE}:latest"
        self.fail_untag = False
        self.fail_list_images = False
        #: A Config.Env shape this cannot read, to exercise failing closed.
        self.broken_env = False
        self.env: tuple[str, ...] = ()

    async def inspect_container(self, name: str) -> dict[str, Any]:
        if self.fail_inspect:
            raise PodmanError("no such container")
        config = {"Env": "PATH=/x" if self.broken_env else list(self.env)}
        # ImageName is the reference the unit created the container from, which
        # is what the unit-shape gate reads. Id, RestartCount and
        # State.StartedAt are what a `log` probe reads: the first two pin the
        # container's identity across the settle window, and the third is the
        # floor for the log read. They are here in the libpod shape measured on
        # podman 5.8.2, not a convenient one.
        return {
            "Image": self.running,
            "ImageName": self.reference,
            "Config": config,
            "Id": self.container_id,
            "RestartCount": 0,
            "State": {"StartedAt": self.started_at},
        }

    async def inspect_image(self, name: str) -> ImageRef:
        key = self.by_ref.get(name, name)
        if key == CANDIDATE_ID:
            return self._ref(
                CANDIDATE_ID,
                self.candidate_revision,
                version=self.candidate_version,
                digest=self.candidate_digest,
            )
        if key in self.revisions:
            return self._ref(key, self.revisions[key])
        raise PodmanError(f"no such image {name}")

    @staticmethod
    def _ref(
        image_id: str,
        revision: str,
        version: str | None = None,
        digest: str | None = None,
    ) -> ImageRef:
        labels = {"org.opencontainers.image.revision": revision}
        if version is not None:
            labels["org.opencontainers.image.version"] = version
        return ImageRef(
            id=image_id,
            digest=digest,
            tags=(f"{IMAGE}:latest",),
            labels=labels,
        )

    async def pull(self, reference: str) -> str:
        if (":gated-" in reference or reference.endswith(":main")) and not self.gated:
            raise PodmanError(f"{reference}: manifest unknown")
        moved = {f"{IMAGE}:main": self.main_id, f"{IMAGE}:latest": self.latest_id}.get(reference)
        self.by_ref[reference] = moved or CANDIDATE_ID
        return moved or CANDIDATE_ID

    async def tag(self, name: str, reference: str) -> None:
        if self.fail_tag_on and self.fail_tag_on in reference:
            raise PodmanError(f"cannot tag {reference}")
        self.tagged.append((name, reference))
        self.by_ref[reference] = self.by_ref.get(name, name)

    async def untag(self, name: str, reference: str) -> None:
        if self.fail_untag:
            raise PodmanError("the tag will not go")
        self.untagged.append((name, reference))
        self.by_ref.pop(reference, None)

    async def remove_image(self, name: str, force: bool = False) -> None:
        self.removed.append(name)

    async def list_images(self, reference: str | None = None) -> tuple[ImageRef, ...]:
        if self.fail_list_images:
            raise PodmanError("the image store is unreadable")
        return self.rollback_images

    async def logs(
        self,
        name: str,
        tail: int = 40,
        since: float | None = None,
        strict: bool = False,
    ) -> str:
        del since, strict
        return self.log_text

    def latest_points_at(self) -> str:
        return self.by_ref[f"{IMAGE}:latest"]


class FakeSystemd:
    """A unit whose restart puts the container on whatever ``:latest`` is."""

    def __init__(self, podman: FakePodman) -> None:
        self.podman = podman
        self.guard = True
        self.pulls_on_start = False
        self.restarts: list[str] = []
        self.active = "active"
        self.result = "success"
        #: Called on every restart, after it is recorded. The seam for a test
        #: that needs the world to change mid-deploy -- a container whose logs
        #: differ after the rollback, or a systemd that goes away during one.
        self.on_restart: Callable[[], None] = lambda: None

    async def property(self, unit: str, name: str, interface: str = "") -> Any:
        if name == "ExecStart":
            return [("/usr/bin/podman", ["podman", "run", "--name", "acquacotta", f"{IMAGE}:latest"], False)]
        if name == "ExecStartPre":
            if self.pulls_on_start:
                return [("/usr/bin/podman", ["podman", "pull", f"{IMAGE}:latest"], False)]
            return []
        return ""

    async def state(self, unit: str) -> Any:
        from deployer.systemd import UnitState

        return UnitState(name=unit, active_state=self.active, sub_state="running", result=self.result)

    async def restart(self, unit: str) -> None:
        self.restarts.append(unit)
        self.podman.running = self.podman.latest_points_at()
        # After the world has moved, so a hook can say what the restart really
        # produced -- a different image, different logs, or an exception.
        self.on_restart()

    async def hold_guard_installed(self) -> bool:
        return self.guard


class FakeNagios:
    def __init__(self) -> None:
        self.opened: list[tuple[str, int, str]] = []
        self.cancelled: list[str] = []
        self.fail_open = False
        self.fail_cancel = False

    async def open_downtime(self, host: str, minutes: int, comment: str, service: str | None = None) -> None:
        if self.fail_open:
            raise NagiosError("cannot reach Nagios")
        self.opened.append((host, minutes, comment))

    async def cancel_downtime(self, host: str, service: str | None = None) -> None:
        if self.fail_cancel:
            raise NagiosError("cannot reach Nagios")
        self.cancelled.append(host)


class World:
    """One host, one RT, and a runner pointed at them."""

    def __init__(self, state: Path, *tickets: Ticket, registry: Registry | None = None) -> None:
        self.rt = FakeRt(*tickets)
        self.podman = FakePodman()
        self.systemd = FakeSystemd(self.podman)
        self.nagios = FakeNagios()
        self.state = state
        self.holds = HoldStore(state)
        self.attempts = AttemptLog(state)
        #: Image ids whose probe answers 503. A test for "the new image is
        #: broken" is written as a broken image, so the sequence has to be
        #: right for it to pass.
        self.broken: set[str] = set()

        def handler(_request: httpx.Request) -> httpx.Response:
            unwell = self.podman.running in self.broken
            return httpx.Response(503 if unwell else 200, text="nope" if unwell else "ok")

        self.runner = Runner(
            registry=registry or REGISTRY,
            state_dir=state,
            clients=Clients(
                rt=self.rt,
                podman=self.podman,
                systemd=self.systemd,
                nagios=self.nagios,
                probes=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
            ),
            now=lambda: NOW,
        )

    async def drain(self) -> Any:
        return await self.runner.drain()


# ------------------------------------------------------------- nothing to do


async def test_an_empty_queue_is_ok_and_touches_nothing(tmp_path: Path) -> None:
    world = World(tmp_path)
    report = await world.drain()
    assert (report.outcome, report.tickets) == (OUTCOME_OK, 0)
    assert report.detail == "nothing to do"
    assert world.systemd.restarts == []
    assert world.podman.tagged == []


async def test_the_guard_is_checked_before_any_ticket_is_read(tmp_path: Path) -> None:
    # The order is the point: if a hold cannot be honoured then a rollback
    # cannot be made to stick, and nothing should be deployed at all.
    world = World(tmp_path, ticket())
    world.systemd.guard = False
    with pytest.raises(MissingGuardError, match="hold guard is not installed"):
        await world.drain()
    assert world.rt.comments == []
    assert world.systemd.restarts == []


# ------------------------------------------------------------- a good deploy


async def test_a_good_deploy_goes_live_and_resolves(tmp_path: Path) -> None:
    world = World(tmp_path, ticket())
    report = await world.drain()

    assert report.outcome == OUTCOME_OK
    assert "deployed" in report.detail
    assert world.systemd.restarts == [UNIT]
    assert world.podman.latest_points_at() == CANDIDATE_ID
    assert world.rt.status() == ["open", "resolved"]
    assert world.rt.said("Deployed.")


async def test_the_window_is_opened_before_the_restart_and_closed_after(tmp_path: Path) -> None:
    world = World(tmp_path, ticket())
    await world.drain()
    host, minutes, comment = world.nagios.opened[0]
    assert host == SERVICE.nagios_host
    assert minutes >= 1
    assert "1601" in comment
    assert world.nagios.cancelled == [SERVICE.nagios_host]


async def test_the_outgoing_image_keeps_a_rollback_tag(tmp_path: Path) -> None:
    world = World(tmp_path, ticket())
    await world.drain()
    rollback = [ref for _, ref in world.podman.tagged if ":rollback-" in ref]
    assert len(rollback) == 1
    assert world.rt.said(rollback[0])


async def test_the_candidate_tag_is_dropped_once_it_has_served_its_purpose(tmp_path: Path) -> None:
    # Otherwise candidate tags pile up exactly like the rollback tags that
    # rotation exists to control.
    world = World(tmp_path, ticket())
    await world.drain()
    assert any(":candidate-" in ref for _, ref in world.podman.untagged)


async def test_older_rollback_tags_are_rotated(tmp_path: Path) -> None:
    world = World(tmp_path, ticket())
    kept_plus_three = ROLLBACK_TAGS_KEPT + 3
    world.podman.rollback_images = tuple(
        ImageRef(id=f"sha256:{n}", digest=None, tags=(f"localhost/acquacotta:rollback-{n}",), labels={})
        for n in range(kept_plus_three)
    )
    await world.drain()
    assert len(world.podman.removed) == kept_plus_three - ROLLBACK_TAGS_KEPT
    assert world.rt.said("older rollback tag")


async def test_a_good_deploy_clears_a_standing_hold(tmp_path: Path) -> None:
    # A hold is not a gate. The next ticket carries the fix, and verifying it
    # is how the hold clears -- otherwise nothing could ever clear it but a
    # person, and the service would stay out of auto-update for ever.
    world = World(tmp_path, ticket())
    world.holds.place(Hold(unit=UNIT, service="acquacotta", ticket=1600, revision=OTHER, at=NOW - 3600))
    report = await world.drain()
    assert report.outcome == OUTCOME_OK
    assert not world.holds.held(UNIT)
    assert world.rt.said("hold on this service is cleared")


async def test_a_nagios_failure_closing_the_window_does_not_fail_a_good_deploy(tmp_path: Path) -> None:
    world = World(tmp_path, ticket())
    world.nagios.fail_cancel = True
    report = await world.drain()
    assert report.outcome == OUTCOME_OK
    assert world.rt.status() == ["open", "resolved"]


# ------------------------------------------------------------------ refusals


@pytest.mark.parametrize(
    ("text", "fragment"),
    [
        ("nonsense without a colon", "not a 'key: value' line"),
        ("service: acquacotta\nrepo: crunchtools/acquacotta\n", "commit"),
        (body(service="nothing-here"), "nothing-here"),
        (body(repo="someone/else"), "someone/else"),
    ],
)
async def test_a_ticket_that_cannot_be_believed_is_refused(tmp_path: Path, text: str, fragment: str) -> None:
    world = World(tmp_path, ticket(text=text))
    report = await world.drain()
    assert report.outcome == OUTCOME_REFUSED
    assert world.rt.said(fragment)


async def test_a_creator_who_is_not_a_requestor_is_refused(tmp_path: Path) -> None:
    world = World(tmp_path, ticket(creator="someone-else"))
    report = await world.drain()
    assert report.outcome == OUTCOME_REFUSED
    assert world.rt.said("may not request a deploy")
    assert world.rt.said("developer1")


async def test_a_follower_ticket_deploys_a_row_that_lists_the_follower(tmp_path: Path) -> None:
    # release-follow's tickets are accepted by the same requestors check as a
    # seat's: the opt-in IS the authorisation, so there is nothing extra here.
    registry = Registry({"acquacotta": replace(SERVICE, requestors=("developer1", "follower"))})
    world = World(tmp_path, ticket(creator="follower"), registry=registry)
    report = await world.drain()
    assert report.outcome == OUTCOME_OK
    assert world.systemd.restarts == [UNIT]


async def test_a_follower_ticket_is_refused_for_a_row_that_does_not_list_it(tmp_path: Path) -> None:
    world = World(tmp_path, ticket(creator="follower"))
    report = await world.drain()
    assert report.outcome == OUTCOME_REFUSED
    assert world.systemd.restarts == []
    assert world.rt.said("'follower' may not request a deploy")


async def test_a_ticket_from_another_queue_is_refused(tmp_path: Path) -> None:
    world = World(tmp_path, ticket(queue="Professional"))
    report = await world.drain()
    assert report.outcome == OUTCOME_REFUSED
    assert world.rt.said("not 'Deploy'")


async def test_a_revision_that_was_rolled_back_is_never_tried_again(tmp_path: Path) -> None:
    world = World(tmp_path, ticket())
    world.attempts.mark_rolled_back("acquacotta", WANTED)
    report = await world.drain()
    assert report.outcome == OUTCOME_REFUSED
    assert world.rt.said("will not be tried again")


async def test_an_errata_rebuild_does_not_launder_a_condemned_commit(tmp_path: Path) -> None:
    # The fleet CVE lane rebuilds a service on the SAME git commit and files a
    # Deploy ticket for the new image. If that commit was rolled back before,
    # the fresh image id must not rescue it: `_gate` condemns by (service,
    # commit), and it refuses BEFORE staging, so the rebuild is never even
    # pulled. A new image id laundering a known-bad commit back onto the host
    # is exactly the mistake this gate exists to stop.
    world = World(tmp_path, ticket())
    world.attempts.mark_rolled_back("acquacotta", WANTED)
    # The candidate is a genuinely different image (the errata rebuild), but the
    # gate never gets as far as pulling it.
    report = await world.drain()
    assert report.outcome == OUTCOME_REFUSED
    assert world.rt.said("will not be tried again")
    # Refused before the host was touched: nothing staged, nothing deployed.
    assert world.systemd.restarts == []
    assert world.podman.tagged == []
    assert world.podman.latest_points_at() == OUTGOING_ID


async def test_too_many_rollbacks_refuses_the_service(tmp_path: Path) -> None:
    world = World(tmp_path, ticket())
    for _ in range(ROLLBACK_LIMIT):
        world.attempts.record_rollback("acquacotta")
    report = await world.drain()
    assert report.outcome == OUTCOME_REFUSED
    assert world.rt.said("deployer unhold acquacotta")


async def test_a_refusal_changes_nothing_on_the_host(tmp_path: Path) -> None:
    world = World(tmp_path, ticket(creator="someone-else"))
    await world.drain()
    assert world.podman.tagged == []
    assert world.systemd.restarts == []
    assert world.nagios.opened == []
    assert world.podman.latest_points_at() == OUTGOING_ID


async def test_a_refusal_is_not_an_attempt(tmp_path: Path) -> None:
    # Nothing was learned about the image, so the next ticket for the same
    # commit -- filed correctly this time -- gets a fair hearing.
    world = World(tmp_path, ticket(creator="someone-else"))
    await world.drain()
    assert not world.attempts.was_rolled_back("acquacotta", WANTED)
    assert world.attempts.recent_rollbacks("acquacotta") == 0


async def test_a_refusal_hands_the_ticket_back(tmp_path: Path) -> None:
    world = World(tmp_path, ticket(creator="someone-else"))
    await world.drain()
    assert world.rt.fields[-1][1] == {"status": "stalled", "owner": "someone-else"}


async def test_an_owner_rt_will_not_accept_does_not_cost_the_stall(tmp_path: Path) -> None:
    # Stalling is the part that matters: it takes the ticket out of this
    # program's queue so it is not reconsidered every two minutes.
    world = World(tmp_path, ticket(creator="someone-else"))
    world.rt.refuse_owner = True
    await world.drain()
    assert world.rt.fields[-1][1] == {"status": "stalled"}


async def test_a_pull_on_start_unit_is_refused(tmp_path: Path) -> None:
    world = World(tmp_path, ticket())
    world.systemd.pulls_on_start = True
    report = await world.drain()
    assert report.outcome == OUTCOME_REFUSED
    assert world.rt.said("pulls on start")
    assert world.systemd.restarts == []


# ------------------------------------------------------------------- waiting


async def test_a_commit_with_no_image_yet_waits(tmp_path: Path) -> None:
    # A seat files its ticket the moment it merges and CI builds afterwards, so
    # for the first build_deadline a mismatch means "not yet".
    world = World(tmp_path, ticket(age_seconds=120))
    world.podman.candidate_revision = OTHER
    report = await world.drain()
    assert report.outcome == OUTCOME_OK
    assert "waiting" in report.detail
    assert world.systemd.restarts == []
    # Still queued: the status is untouched, so the next run picks it up.
    assert world.rt.status() == []
    assert world.rt.said("Waiting for a build")


async def test_waiting_is_said_once_not_every_two_minutes(tmp_path: Path) -> None:
    world = World(tmp_path, ticket(age_seconds=120))
    world.podman.candidate_revision = OTHER
    await world.drain()
    await world.drain()
    await world.drain()
    assert len(world.rt.comments) == 1


async def test_a_commit_that_never_built_goes_stale(tmp_path: Path) -> None:
    world = World(tmp_path, ticket(age_seconds=SERVICE.build_deadline + 60))
    world.podman.candidate_revision = OTHER
    report = await world.drain()
    assert report.outcome == OUTCOME_REFUSED
    assert world.rt.said("check the repository's actions")
    assert world.rt.status() == ["stalled"]


async def test_going_stale_is_not_an_attempt_either(tmp_path: Path) -> None:
    # The image never ran, so nothing was learned about it.
    world = World(tmp_path, ticket(age_seconds=SERVICE.build_deadline + 60))
    world.podman.candidate_revision = OTHER
    await world.drain()
    assert not world.attempts.was_rolled_back("acquacotta", WANTED)


async def test_staging_puts_latest_back_whatever_the_verdict(tmp_path: Path) -> None:
    # The invariant the whole sequence exists for: a refused or waiting ticket
    # must not leave :latest on an unverified image, or a Restart=always crash
    # would deploy it.
    world = World(tmp_path, ticket(age_seconds=120))
    world.podman.candidate_revision = OTHER
    await world.drain()
    assert world.podman.latest_points_at() == OUTGOING_ID


# ---------------------------------------------------------------- rollbacks


async def test_a_broken_deploy_rolls_back(tmp_path: Path) -> None:
    world = World(tmp_path, ticket())
    world.broken = {CANDIDATE_ID}
    report = await world.drain()
    assert report.outcome == OUTCOME_ROLLED_BACK
    assert world.podman.latest_points_at() == OUTGOING_ID
    assert world.systemd.restarts == [UNIT, UNIT]
    assert world.rt.said("rolled back")


async def test_a_broken_deploy_holds_the_service(tmp_path: Path) -> None:
    world = World(tmp_path, ticket())
    world.broken = {CANDIDATE_ID}
    await world.drain()
    hold = world.holds.get(UNIT)
    assert hold is not None
    assert (hold.ticket, hold.revision) == (1601, WANTED)
    assert world.rt.said("skipped by podman auto-update")


async def test_a_broken_deploy_records_the_attempt(tmp_path: Path) -> None:
    world = World(tmp_path, ticket())
    world.broken = {CANDIDATE_ID}
    await world.drain()
    assert world.attempts.was_rolled_back("acquacotta", WANTED)
    assert world.attempts.recent_rollbacks("acquacotta") == 1


async def test_a_broken_deploy_hands_the_ticket_back_with_the_evidence(tmp_path: Path) -> None:
    world = World(tmp_path, ticket())
    world.broken = {CANDIDATE_ID}
    await world.drain()
    assert world.rt.status() == ["open", "stalled"]
    assert world.rt.fields[-1][1]["owner"] == "developer1"
    # The developer needs what the service said as it failed; "the probe
    # returned 503" is not that.
    assert world.rt.said("ValueError: no database")
    assert world.rt.said(WANTED)


async def test_a_rollback_that_came_back_still_closes_the_window(tmp_path: Path) -> None:
    world = World(tmp_path, ticket())
    world.broken = {CANDIDATE_ID}
    await world.drain()
    assert world.nagios.cancelled == [SERVICE.nagios_host]


async def test_a_rollback_that_does_not_come_back_needs_a_person(tmp_path: Path) -> None:
    # Both images broken. The window is cancelled on purpose: at this point
    # Nagios saying the service is down is the only remaining fix path.
    world = World(tmp_path, ticket())
    world.broken = {CANDIDATE_ID, OUTGOING_ID}
    report = await world.drain()
    assert report.outcome == OUTCOME_ROLLBACK_FAILED
    assert "needs a person" in report.detail
    assert world.nagios.cancelled == [SERVICE.nagios_host]
    assert world.rt.said("THE ROLLBACK DID NOT COME BACK")


async def test_a_unit_that_will_not_restart_is_a_failed_deploy_not_a_crash(tmp_path: Path) -> None:
    world = World(tmp_path, ticket())
    world.systemd.active = "failed"
    world.systemd.result = "exit-code"
    report = await world.drain()
    assert report.outcome == OUTCOME_ROLLBACK_FAILED
    assert world.holds.held(UNIT)


# ------------------------------------------------------- RT and Nagios first


async def test_an_unreachable_rt_before_the_restart_leaves_the_host_alone(tmp_path: Path) -> None:
    world = World(tmp_path, ticket())
    world.rt.fail_set_fields = True
    report = await world.drain()
    assert report.outcome == OUTCOME_COULD_NOT_RUN
    assert world.systemd.restarts == []
    assert world.podman.latest_points_at() == OUTGOING_ID
    assert not world.holds.held(UNIT)


async def test_an_unreachable_nagios_before_the_restart_leaves_the_host_alone(tmp_path: Path) -> None:
    world = World(tmp_path, ticket())
    world.nagios.fail_open = True
    report = await world.drain()
    assert report.outcome == OUTCOME_COULD_NOT_RUN
    assert world.systemd.restarts == []
    assert world.podman.latest_points_at() == OUTGOING_ID


# ---------------------------------------------------------------- the queue


async def test_an_unreadable_ticket_does_not_stop_the_ones_behind_it(tmp_path: Path) -> None:
    # The queue is other people's work. One unreadable ticket blocking the
    # rest would be a denial of service anybody who can file a ticket could
    # cause.
    world = World(tmp_path, ticket())
    world.rt.unreadable = (1700,)
    report = await world.drain()
    assert report.tickets == len(world.rt.tickets) + len(world.rt.unreadable)
    assert world.systemd.restarts == [UNIT]
    assert report.outcome == OUTCOME_COULD_NOT_RUN


async def test_the_worst_outcome_is_the_one_reported(tmp_path: Path) -> None:
    world = World(tmp_path, ticket(1601), ticket(1602, text=body(repo="wrong/repo")))
    report = await world.drain()
    assert report.tickets == len(world.rt.tickets)
    assert report.outcome == OUTCOME_REFUSED


async def test_two_refusals_for_different_reasons_are_both_said(tmp_path: Path) -> None:
    world = World(tmp_path, ticket(creator="someone-else"))
    await world.drain()
    world.rt.tickets[1601] = ticket(text=body(repo="wrong/repo"))
    await world.drain()
    # Both reasons said, not just "a second comment appeared".
    assert world.rt.said("may not request a deploy")
    assert world.rt.said("wrong/repo")


async def test_the_same_refusal_twice_is_said_once(tmp_path: Path) -> None:
    world = World(tmp_path, ticket(creator="someone-else"))
    await world.drain()
    await world.drain()
    assert len(world.rt.comments) == 1


# ----------------------------------------------------------------- dry runs


async def test_a_dry_run_changes_nothing(tmp_path: Path) -> None:
    world = World(tmp_path, ticket())
    world.runner.dry_run = True
    report = await world.drain()
    assert report.outcome == OUTCOME_OK
    assert "would deploy" in report.detail
    assert (world.podman.tagged, world.systemd.restarts, world.nagios.opened) == ([], [], [])
    assert world.rt.comments == []
    assert world.rt.fields == []


async def test_a_dry_run_still_reports_a_unit_it_would_refuse(tmp_path: Path) -> None:
    # Reading the unit's shape is a read, and it is the refusal most worth
    # knowing about before a real run.
    world = World(tmp_path, ticket())
    world.runner.dry_run = True
    world.systemd.pulls_on_start = True
    report = await world.drain()
    assert report.outcome == OUTCOME_REFUSED
    assert "pulls on start" in report.detail


# ------------------------------------------------- already running the commit


async def test_a_revision_already_live_is_finished_without_a_restart(tmp_path: Path) -> None:
    # The path a ticket takes when the deploy landed and RT could not be told.
    # Restarting a healthy service to arrive where it already is would be a
    # production restart in exchange for nothing.
    world = World(tmp_path, ticket())
    world.podman.running = CANDIDATE_ID
    report = await world.drain()
    assert report.outcome == OUTCOME_OK
    assert "already live" in report.detail
    assert world.systemd.restarts == []
    assert world.nagios.opened == []
    assert world.rt.status() == ["resolved"]
    assert world.rt.said("Already deployed.")


async def test_the_already_live_comment_does_not_claim_a_url_answered(tmp_path: Path) -> None:
    """The second place the receipt could lie, found in production on 2026-11-08.

    ``_proof_phrase`` exists because the success comment used to end "and
    <url> answered throughout" on every tier, which is an invention on a
    ``log`` row. The already-live comment carried its OWN copy of that
    sentence with the URL interpolated directly, so RT 1589 resolved josui
    saying "and  answered for 120s" -- a blank where the proof belongs, on the
    one tier that proves least. Fixing it in one place left the other lying.
    """
    world = World(tmp_path, ticket(), registry=SEAT_REGISTRY)
    world.podman.running = CANDIDATE_ID
    world.podman.log_text = f"josui: serving: {READY_LINE} on /tmp/sock after 1s\n"
    report = await world.drain()
    assert report.outcome == OUTCOME_OK
    assert world.systemd.restarts == []
    assert world.rt.said("Already deployed.")
    assert world.rt.said(READY_LINE)
    # The bug verbatim: a sentence with nothing in front of "answered".
    assert not world.rt.said(" answered for ")


async def test_a_revision_already_live_clears_its_hold(tmp_path: Path) -> None:
    world = World(tmp_path, ticket())
    world.podman.running = CANDIDATE_ID
    world.holds.place(Hold(unit=UNIT, service="acquacotta", ticket=1600, revision=OTHER, at=NOW))
    await world.drain()
    assert not world.holds.held(UNIT)


async def test_an_errata_rebuild_at_the_same_commit_deploys_not_skips_as_already_live(
    tmp_path: Path,
) -> None:
    # The fleet CVE lane rebuilds a service on the SAME git commit and files a
    # Deploy ticket. The rebuild carries the same revision label but a NEW image
    # id -- that is what an errata rebuild is. The already-live short-circuit
    # fires only when the revision MATCHES *and* the outgoing and candidate
    # image ids are equal; a new image id at the same commit must fall through
    # to `_live` and actually deploy and verify. Otherwise the whole point of
    # the CVE lane -- shipping a rebuilt image without a code change -- would be
    # silently skipped as "already deployed".
    world = World(tmp_path, ticket())
    # The running container is at the wanted commit already (same git commit),
    # but it is the pre-errata image id; the candidate pulls to a different one.
    world.podman.revisions = {OUTGOING_ID: WANTED}
    report = await world.drain()
    assert report.outcome == OUTCOME_OK
    assert "deployed" in report.detail
    # Not short-circuited: it restarted the unit and moved :latest to the new
    # image, rather than resolving the ticket untouched as already-live.
    assert world.systemd.restarts == [UNIT]
    assert world.podman.latest_points_at() == CANDIDATE_ID
    assert world.rt.said("Deployed.")
    assert not world.rt.said("Already deployed.")


async def test_a_revision_already_live_but_unhealthy_is_refused_not_restarted(tmp_path: Path) -> None:
    # Running the right revision and not answering is a person's problem. A
    # robot that restarted to fix it would be guessing.
    world = World(tmp_path, ticket())
    world.podman.running = CANDIDATE_ID
    world.broken = {CANDIDATE_ID}
    report = await world.drain()
    assert report.outcome == OUTCOME_REFUSED
    assert world.systemd.restarts == []
    assert world.rt.said("will not restart a service that is already on the right revision")
    assert not world.holds.held(UNIT)


# ----------------------------------------------------------- the exit codes


def test_every_outcome_a_run_can_produce_has_an_exit_code() -> None:
    # A new outcome without a row here would be a KeyError at the end of a
    # real run, after the work was done.
    assert set(EXIT_FOR_OUTCOME) == set(OUTCOMES)


@pytest.mark.parametrize(
    ("outcome", "code"),
    [
        (OUTCOME_OK, EXIT_OK),
        # A decided state, not a failure: refusing correctly is the tool
        # working, and the ticket is stalled and owned by whoever filed it.
        (OUTCOME_REFUSED, EXIT_OK),
        (OUTCOME_ROLLED_BACK, EXIT_ERROR),
        (OUTCOME_ROLLBACK_FAILED, EXIT_ERROR),
        (OUTCOME_COULD_NOT_RUN, EXIT_COULD_NOT_RUN),
    ],
)
def test_the_exit_codes_follow_the_profile(outcome: str, code: int) -> None:
    assert EXIT_FOR_OUTCOME[outcome] == code


async def test_a_clean_rollback_still_exits_nonzero(tmp_path: Path) -> None:
    # It leaves a held service and a stalled ticket. A unit that exited 0 with
    # a service held would be reporting success for a state nobody has dealt
    # with, and the wake-up it costs is per failure rather than per minute.
    world = World(tmp_path, ticket())
    world.broken = {CANDIDATE_ID}
    report = await world.drain()
    assert EXIT_FOR_OUTCOME[report.outcome] == EXIT_ERROR


async def test_a_verified_deploy_exits_zero(tmp_path: Path) -> None:
    world = World(tmp_path, ticket())
    report = await world.drain()
    assert EXIT_FOR_OUTCOME[report.outcome] == EXIT_OK


# ------------------------------------------- what goes on a public ticket


async def test_a_secret_from_the_environment_is_not_published(tmp_path: Path) -> None:
    # The comment goes on an RT ticket. A failing service usually prints the
    # thing that failed, and the thing that failed is often a credential it was
    # handed in its environment.
    world = World(tmp_path, ticket())
    world.broken = {CANDIDATE_ID}
    world.podman.env = ("FLASK_SECRET_KEY=hunter2-and-then-some", "PATH=/app/venv/bin")
    world.podman.log_text = (
        "sqlalchemy.exc.OperationalError: auth failed for key hunter2-and-then-some\n"
        "running from /app/venv/bin\n"
    )
    await world.drain()
    published = "\n".join(text for _, text in world.rt.comments)
    assert "hunter2-and-then-some" not in published
    assert "<redacted $FLASK_SECRET_KEY>" in published
    # PATH is on the allowlist: never a credential, and a log is hard to read
    # without it.
    assert "running from /app/venv/bin" in published


async def test_the_longest_secret_wins_when_one_contains_another(tmp_path: Path) -> None:
    world = World(tmp_path, ticket())
    world.broken = {CANDIDATE_ID}
    world.podman.env = ("SHORT=abcdefgh", "LONG=abcdefghijklmnop")
    world.podman.log_text = "token=abcdefghijklmnop\n"
    await world.drain()
    published = "\n".join(text for _, text in world.rt.comments)
    assert "<redacted $LONG>" in published
    assert "abcdefgh" not in published


async def test_an_unreadable_environment_means_no_logs_rather_than_raw_logs(tmp_path: Path) -> None:
    # Fails closed. If the output cannot be checked for secrets, publishing it
    # anyway would turn a failed read into a leak. Called directly: through a
    # whole drain this would depend on how many times staging and verification
    # happen to inspect the container.
    world = World(tmp_path)
    world.podman.log_text = "secret-looking thing\n"
    world.podman.fail_inspect = True
    tail = await world.runner._log_tail(SERVICE)
    assert "secret-looking thing" not in tail
    assert "could not be checked for secrets" in tail


@pytest.mark.parametrize(
    ("env", "text", "expected"),
    [
        # The case this exists for.
        (["TOKEN=super-secret-value"], "auth failed: super-secret-value", "auth failed: <redacted $TOKEN>"),
        # On the allowlist: never a credential, and a log is hard to read
        # without it.
        (["PATH=/app/venv/bin"], "ran /app/venv/bin/x", "ran /app/venv/bin/x"),
        # NOT on the allowlist, so it goes whatever it is called. A blocklist of
        # secret-looking names would have published this one.
        (["DB=hunter7"], "auth failed for hunter7", "auth failed for <redacted $DB>"),
        # Removed wherever it appears, including inside an unrelated word. That
        # is the right way round: a mangled log line is recoverable, a
        # published key is not.
        (["API_KEY=rejt"], "abc rejted", "abc <redacted $API_KEY>ed"),
        # An entry with no value has nothing to redact.
        (["EMPTY="], "nothing here", "nothing here"),
        # Several occurrences, not just the first.
        (
            ["KEY=abcdefghij"],
            "abcdefghij then abcdefghij",
            "<redacted $KEY> then <redacted $KEY>",
        ),
    ],
)
def test_redaction_replaces_what_it_should_and_leaves_the_rest(
    env: list[str], text: str, expected: str
) -> None:
    secrets = container_environment({"Config": {"Env": env}})
    assert redact(text, secrets) == expected


@pytest.mark.parametrize(
    "inspected",
    [{}, {"Config": None}, {"Config": {}}, {"Config": {"Env": None}}, {"Config": {"Env": "x"}}],
)
def test_an_environment_shape_this_cannot_read_is_an_error_not_an_empty_set(
    inspected: dict[str, Any],
) -> None:
    # Returning {} here would read as "this container has no secrets" and the
    # caller would publish the logs unredacted. Failing open on the one path
    # where failing open means leaking.
    with pytest.raises(UnreadableEnvironment):
        container_environment(inspected)


def test_a_genuinely_empty_environment_is_not_an_error() -> None:
    # An empty list is a shape this can read. Only an unreadable one raises.
    assert container_environment({"Config": {"Env": []}}) == {}


async def test_an_rt_failure_after_a_verified_deploy_is_recovered_on_the_next_run(
    tmp_path: Path,
) -> None:
    # The failure the already-live path exists for, end to end. The first run
    # deploys and verifies, then cannot resolve the ticket. The second run must
    # finish the ticket WITHOUT restarting a healthy service.
    world = World(tmp_path, ticket())
    world.rt.fail_status = "resolved"
    with pytest.raises(RtError):
        await world.drain()
    assert world.systemd.restarts == [UNIT]
    assert world.podman.latest_points_at() == CANDIDATE_ID

    # Second run: RT is back, the ticket is still open.
    world.rt.fail_status = ""
    report = await world.drain()
    assert report.outcome == OUTCOME_OK
    assert "already live" in report.detail
    # The one thing that must not happen.
    assert world.systemd.restarts == [UNIT]
    assert world.rt.status()[-1] == "resolved"


async def test_a_short_secret_stops_the_output_being_published_at_all(tmp_path: Path) -> None:
    # Too short to substitute out of the text without wrecking it, and
    # publishing it because redacting would be ugly is the decision this
    # program must not make. The message names the variable.
    world = World(tmp_path, ticket())
    world.broken = {CANDIDATE_ID}
    world.podman.env = ("DB=ab",)
    world.podman.log_text = "connected to ab as root\n"
    await world.drain()
    published = "\n".join(text for _, text in world.rt.comments)
    assert "connected to" not in published
    assert "not published" in published
    assert "DB" in published


async def test_the_unredacted_tail_is_kept_on_the_host(tmp_path: Path) -> None:
    # What the ticket cannot carry, the host keeps. One file per service,
    # overwritten, root-only.
    world = World(tmp_path, ticket())
    world.broken = {CANDIDATE_ID}
    world.podman.env = ("TOKEN=a-long-enough-secret",)
    world.podman.log_text = "failed with a-long-enough-secret\n"
    await world.drain()

    kept = tmp_path / "failures" / "acquacotta.log"
    assert kept.read_text(encoding="utf-8") == "failed with a-long-enough-secret\n"
    assert kept.stat().st_mode & 0o077 == 0
    assert (tmp_path / "failures").stat().st_mode & 0o077 == 0
    # And the ticket says where it is, without repeating what is in it.
    published = "\n".join(text for _, text in world.rt.comments)
    assert str(kept) in published
    assert "a-long-enough-secret" not in published


async def test_the_logs_come_from_the_failed_image_not_the_restored_one(tmp_path: Path) -> None:
    # podman's logs endpoint is addressed by container NAME, so once the
    # rollback has restarted the unit that name is the container running the
    # OLD image. Read afterwards the ticket would carry the output of the
    # thing that works, which is worse than carrying none.
    world = World(tmp_path, ticket())
    world.broken = {CANDIDATE_ID}
    world.podman.log_text = "the candidate died here\n"

    def the_rollback_brings_a_working_container() -> None:
        # The second restart is the rollback's; the first is the deploy's.
        if len(world.systemd.restarts) > 1:
            world.podman.log_text = "the restored container is fine\n"

    world.systemd.on_restart = the_rollback_brings_a_working_container
    await world.drain()
    published = "\n".join(text for _, text in world.rt.comments)
    assert "the candidate died here" in published
    assert "the restored container is fine" not in published


async def test_a_rollback_that_raises_is_a_person_not_a_crash(tmp_path: Path) -> None:
    # attempted=True on purpose: the tag or the restart may well have happened
    # before the exception, so the state of the service is unknown and
    # "nothing was touched" is the one thing that cannot be said.
    world = World(tmp_path, ticket())
    world.broken = {CANDIDATE_ID}

    def systemd_goes_away_during_the_rollback() -> None:
        if len(world.systemd.restarts) > 1:
            raise SystemdError("systemd went away mid-rollback")

    world.systemd.on_restart = systemd_goes_away_during_the_rollback
    report = await world.drain()
    assert report.outcome == OUTCOME_ROLLBACK_FAILED
    assert world.holds.held(UNIT)
    assert world.rt.said("THE ROLLBACK DID NOT COME BACK")
    assert world.rt.said("systemd went away mid-rollback")


async def test_an_env_shape_that_cannot_be_read_stops_publication(tmp_path: Path) -> None:
    # The inspect succeeded, so the PodmanError branch does not fire; it is the
    # SHAPE that is unreadable. Publishing anyway would be failing open on the
    # one path where failing open means leaking.
    world = World(tmp_path)
    world.podman.broken_env = True
    world.podman.log_text = "secret-looking thing\n"
    tail = await world.runner._log_tail(SERVICE)
    assert "secret-looking thing" not in tail
    assert "Config.Env is str, not a list" in tail


async def test_a_tail_that_cannot_be_kept_on_the_host_says_so(tmp_path: Path) -> None:
    # The note matters because the ticket's redacted copy is then the only
    # record, and a reader should know that rather than assume the path works.
    world = World(tmp_path)
    (tmp_path / "failures").write_text("not a directory", encoding="utf-8")
    tail = await world.runner._log_tail(SERVICE)
    assert "could not be kept on the host" in tail


async def test_cleanup_failures_reach_the_ticket_rather_than_vanishing(tmp_path: Path) -> None:
    # None of these should fail a verified deploy, and none should disappear
    # either: a docstring once claimed they went in the status file and
    # nothing put them there.
    world = World(tmp_path, ticket())
    world.nagios.fail_cancel = True
    world.podman.fail_untag = True
    world.podman.fail_list_images = True
    report = await world.drain()
    assert report.outcome == OUTCOME_OK
    assert world.rt.status() == ["open", "resolved"]
    assert world.rt.said("could not be cancelled")
    assert world.rt.said("could not be removed")
    assert world.rt.said("could not be rotated")


async def test_cleanup_failures_reach_the_ticket_on_a_rollback_too(tmp_path: Path) -> None:
    world = World(tmp_path, ticket())
    world.broken = {CANDIDATE_ID}
    world.nagios.fail_cancel = True
    world.podman.fail_untag = True
    report = await world.drain()
    assert report.outcome == OUTCOME_ROLLED_BACK
    assert world.rt.said("could not be cancelled")
    assert world.rt.said("could not be removed")


async def test_a_pinned_service_is_refused_and_told_why(tmp_path: Path) -> None:
    # Pinning a unit to a semver tag takes it OUT of this deployer's scope:
    # moving :latest would not change what it runs. The message says so, and
    # says what to do instead, because "pin it" was the plan's own words for
    # onboarding and it is wrong.
    world = World(tmp_path, ticket())
    world.podman.reference = f"{IMAGE}:2.34.0"
    report = await world.drain()
    assert report.outcome == OUTCOME_REFUSED
    assert world.rt.said("is running")
    assert world.rt.said("would not change what it runs")
    assert world.systemd.restarts == []


async def test_a_unit_that_starts_the_wrong_image_does_not_blame_the_revision(
    tmp_path: Path,
) -> None:
    # A unit edited behind this program's back starts something that is not the
    # candidate. The deploy fails and rolls back, which is right -- but nothing
    # was learned about the revision, and blacklisting it would mean a
    # perfectly good commit could never be deployed to this service again.
    world = World(tmp_path, ticket())

    def start_something_else() -> None:
        world.podman.running = "sha256:" + "c" * 64

    world.systemd.on_restart = start_something_else
    report = await world.drain()

    assert report.outcome in {OUTCOME_ROLLED_BACK, OUTCOME_ROLLBACK_FAILED}
    assert not world.attempts.was_rolled_back("acquacotta", WANTED)
    # Still counted as a rollback of the service, and still held: something is
    # wrong with this service even if the revision is innocent.
    assert world.attempts.recent_rollbacks("acquacotta") == 1
    assert world.holds.held(UNIT)
    assert world.rt.said("not the candidate")


async def test_an_empty_log_says_where_to_look_instead(tmp_path: Path) -> None:
    # Two different things produce an empty tail and the comment must not pick
    # one. Measured on acquacotta: a container running its own init writes
    # nothing to the container's stdout ever. Measured on mcp-pcloud: a
    # container a few seconds old has not printed yet. The first wording
    # asserted the init reason for whichever service failed and so told ticket
    # 1571 that mcp-pcloud "runs its own init", which is false. "(nothing)"
    # is also wrong -- it reads as "the service failed silently".
    world = World(tmp_path, ticket())
    world.broken = {CANDIDATE_ID}
    world.podman.log_text = ""
    await world.drain()
    published = "\n".join(text for _, text in world.rt.comments)
    assert "had written nothing" in published
    assert "podman logs acquacotta" in published
    assert "journalctl -n 50" in published
    # And no invented reason for which of the two it was.
    assert "runs its own init" not in published
    assert "journalctl" in published


# --- the maintenance window's length ---


@pytest.mark.parametrize(
    ("ready", "settle", "least"),
    [
        # acquacotta's real numbers: the rollback path is 2 x (120 restart +
        # 180 ready + 60 settle) = 720s, and the window must outlast it.
        (180, 60, 12),
        # mcp-pcloud's and mcp-nagios's: 2 x (120 + 60 + 30) = 420s.
        (60, 30, 7),
    ],
)
def test_the_window_outlasts_a_rollback(ready: int, settle: int, least: int) -> None:
    """The window has to cover the FAILING path, not the succeeding one.

    The first version was `settle + margin`, which predated both the restart
    wait and the `ready` window: acquacotta got six minutes against a good
    path of up to 360 seconds and a rollback path near 720, so a rollback that
    was WORKING would have paged somebody in the middle of it -- and the one
    page this design raises deliberately, a rollback that did not come back,
    would have been indistinguishable from that noise.

    Asserted as a floor rather than an exact figure, because the margin on top
    is a judgement call and this is the part that is not.
    """
    service = SERVICE.__class__(**{**_fields(SERVICE), "ready": ready, "settle": settle})
    assert _window_minutes(service) >= least


def test_the_window_is_sized_per_service_not_from_a_constant() -> None:
    # The whole point of `ready` and `settle` being per service is that
    # acquacotta is not an MCP server. A window that ignored them would be
    # wrong for one of the two.
    slow = SERVICE.__class__(**{**_fields(SERVICE), "ready": 180, "settle": 60})
    quick = SERVICE.__class__(**{**_fields(SERVICE), "ready": 60, "settle": 30})
    assert _window_minutes(slow) > _window_minutes(quick)


async def test_a_deploy_opens_a_window_of_that_length(tmp_path: Path) -> None:
    # And the number actually reaches Nagios, rather than being computed and
    # then not used.
    world = World(tmp_path, ticket())
    await world.drain()
    assert world.nagios.opened, "no window was opened"
    _host, minutes, _comment = world.nagios.opened[0]
    assert minutes == _window_minutes(SERVICE)


# --- what the ticket records about the image that went out ---


async def test_a_deployed_ticket_records_the_version_and_the_digest(tmp_path: Path) -> None:
    """Three identifiers, because they answer three different questions.

    The commit says WHICH CODE, the version says WHICH RELEASE, and the digest
    is the only one of the three that means the same thing in a registry, in a
    signature and on another host. The comment carried the commit and a local
    image id, which is the one identifier that means nothing anywhere else --
    and `ImageRef.version` had existed for this from the start, with a
    docstring saying so, unused.
    """
    world = World(tmp_path, ticket())
    await world.drain()
    published = "\n".join(text for _, text in world.rt.comments)
    assert "2.34.0" in published
    assert "sha256:0f1e2d3c4b5a6978" in published


@pytest.mark.parametrize(
    ("version", "digest"),
    [
        (None, None),
        (None, "sha256:0f1e2d3c4b5a6978"),
        ("2.34.0", None),
    ],
)
async def test_a_missing_label_says_so_rather_than_printing_none(
    tmp_path: Path, version: str | None, digest: str | None
) -> None:
    """Each field independently, including the two mixed cases.

    Both are optional on a real image and for different reasons: a build
    outside the pipeline sets no version, and a locally built image has no
    registry digest. Testing only the all-or-none cases would hide a
    regression that fell back to one field's value for both -- Gatehouse's
    point, and the fields are independent, so the test has to be too.

    `None` on a ticket reads as a bug in the robot rather than a fact about
    the image, which is why the absence is spelled out in words.
    """
    world = World(tmp_path, ticket())
    world.podman.candidate_version = version
    world.podman.candidate_digest = digest
    await world.drain()
    published = "\n".join(text for _, text in world.rt.comments)
    assert f"version {version or 'unlabelled'}" in published
    assert f"digest {digest or 'none reported'}" in published
    assert "None" not in published


async def test_a_gated_digest_deploys_although_its_label_names_the_pr_head(tmp_path: Path) -> None:
    """Build once per PR (RT#1550): the label is the PR head, the ticket the merge."""
    world = World(tmp_path, ticket())
    world.podman.gated = True
    world.podman.candidate_revision = OTHER
    await world.drain()
    assert (CANDIDATE_ID, f"{IMAGE}:gated-{WANTED}") in world.podman.untagged
    assert world.systemd.restarts
    assert world.rt.said(f"is running {WANTED[:7]} (built from {OTHER[:12]}")


async def test_an_unmerged_pull_request_head_is_refused(tmp_path: Path) -> None:
    """gated-<head> exists and its label matches, but nothing promoted it."""
    world = World(tmp_path, ticket(age_seconds=SERVICE.build_deadline + 60))
    world.podman.gated = True
    world.podman.main_id = "sha256:main-elsewhere"
    world.podman.latest_id = "sha256:latest-elsewhere"
    report = await world.drain()
    assert report.outcome == OUTCOME_REFUSED
    assert not world.systemd.restarts
    assert world.rt.said("neither main nor a release promoted it")
    assert world.rt.said("pull request that never merged")
    assert world.rt.status() == ["stalled"]
    assert world.podman.latest_points_at() == OUTGOING_ID


async def test_an_unmerged_pull_request_head_waits_while_young(tmp_path: Path) -> None:
    world = World(tmp_path, ticket(age_seconds=120))
    world.podman.gated = True
    world.podman.main_id = "sha256:main-elsewhere"
    world.podman.latest_id = "sha256:latest-elsewhere"
    await world.drain()
    assert not world.systemd.restarts
    assert world.rt.said("Waiting for a build")
    assert world.podman.latest_points_at() == OUTGOING_ID


async def test_a_released_commit_deploys_after_main_moved_on(tmp_path: Path) -> None:
    world = World(tmp_path, ticket())
    world.podman.gated = True
    world.podman.main_id = "sha256:main-elsewhere"
    await world.drain()
    assert world.systemd.restarts


async def test_without_a_gated_tag_a_label_mismatch_still_waits(tmp_path: Path) -> None:
    world = World(tmp_path, ticket())
    world.podman.candidate_revision = OTHER
    await world.drain()
    assert not world.systemd.restarts
