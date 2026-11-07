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

from pathlib import Path
from typing import Any

import httpx
import pytest

from deployer.attempts import ROLLBACK_LIMIT, AttemptLog
from deployer.hold import Hold, HoldStore, MissingGuardError
from deployer.nagios import NagiosError
from deployer.podman import PodmanError, ImageRef
from deployer.registry import Probe, Registry, Service
from deployer.rollback import ROLLBACK_TAGS_KEPT
from deployer.rt import RtError, Ticket
from deployer.runner import (
    OUTCOME_COULD_NOT_RUN,
    OUTCOME_OK,
    OUTCOME_REFUSED,
    OUTCOME_ROLLBACK_FAILED,
    OUTCOME_ROLLED_BACK,
    Clients,
    Runner,
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
    build_deadline=1800,
)
REGISTRY = Registry({"acquacotta": SERVICE})


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

    async def open_tickets(self, queue: str) -> tuple[int, ...]:
        return tuple(sorted(self.tickets)) + self.unreadable

    async def ticket(self, ticket_id: int) -> Ticket:
        if ticket_id in self.unreadable:
            raise RtError(f"ticket {ticket_id}: cannot read Created ''")
        return self.tickets[ticket_id]

    async def comment(self, ticket_id: int, text: str) -> None:
        self.comments.append((ticket_id, text))

    async def set_fields(self, ticket_id: int, **fields: str) -> None:
        if self.fail_set_fields:
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
        self.revisions = {OUTGOING_ID: OTHER}
        self.tagged: list[tuple[str, str]] = []
        self.untagged: list[tuple[str, str]] = []
        self.removed: list[str] = []
        self.rollback_images: tuple[ImageRef, ...] = ()
        self.log_text = "ValueError: no database\n"
        self.fail_tag_on: str | None = None

    async def inspect_container(self, name: str) -> dict[str, Any]:
        return {"Image": self.running}

    async def inspect_image(self, name: str) -> ImageRef:
        key = self.by_ref.get(name, name)
        if key == CANDIDATE_ID:
            return self._ref(CANDIDATE_ID, self.candidate_revision)
        if key in self.revisions:
            return self._ref(key, self.revisions[key])
        raise PodmanError(f"no such image {name}")

    @staticmethod
    def _ref(image_id: str, revision: str) -> ImageRef:
        return ImageRef(
            id=image_id,
            digest=None,
            tags=(f"{IMAGE}:latest",),
            labels={"org.opencontainers.image.revision": revision},
        )

    async def pull(self, reference: str) -> str:
        self.by_ref[reference] = CANDIDATE_ID
        return CANDIDATE_ID

    async def tag(self, name: str, reference: str) -> None:
        if self.fail_tag_on and self.fail_tag_on in reference:
            raise PodmanError(f"cannot tag {reference}")
        self.tagged.append((name, reference))
        self.by_ref[reference] = self.by_ref.get(name, name)

    async def untag(self, name: str, reference: str) -> None:
        self.untagged.append((name, reference))
        self.by_ref.pop(reference, None)

    async def remove_image(self, name: str, force: bool = False) -> None:
        self.removed.append(name)

    async def list_images(self, reference: str | None = None) -> tuple[ImageRef, ...]:
        return self.rollback_images

    async def logs(self, name: str, tail: int = 40) -> str:
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

    def __init__(self, state: Path, *tickets: Ticket) -> None:
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
            registry=REGISTRY,
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
