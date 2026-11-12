"""release-follow tests.

The ones that matter most: it never files a downgrade, never files twice for one
service and commit, files only for rows that opted in, and what it files is a
body the runner accepts from `follower` and from nobody it was not meant for.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from deployer import follow
from deployer.follow import (
    Action,
    FollowReport,
    Running,
    Snapshot,
    SnapshotError,
    Stalled,
    decide,
    subject_for,
    write_status,
)
from deployer.oci import OciError, RemoteImage
from deployer.podman import REVISION_LABEL
from deployer.registry import Probe, Registry, Service
from deployer.rt import RtError, Ticket
from deployer.ticket import creator_may_request, parse_ticket

LATEST = "635cdda3c8632d1b5aed4cc22943895205ce5138"
RUNNING = "f0effffab7fc0b0f8476c14f5794e0da60256eb0"
NEW_ID = "sha256:" + "b" * 64
OLD_ID = "sha256:" + "a" * 64
BUILT = 1_800_000_000.0
NAME = "mcp-nagios"
IMAGE = "quay.io/crunchtools/mcp-nagios"
FIRST_FILED = 9001
SEAT_TICKET = 1700


def service(name: str = NAME, requestors: tuple[str, ...] = ("scott", "follower")) -> Service:
    return Service(
        name=name,
        repo=f"crunchtools/{name}",
        image=f"quay.io/crunchtools/{name}",
        unit=f"{name}.crunchtools.com.service",
        probe=Probe(kind="mcp", url="http://127.0.0.1:8026/mcp"),
        nagios_host=f"ctr-{name}",
        requestors=requestors,
    )


def remote(
    revision: str | None = LATEST, created: float | None = BUILT + 60, image_id: str = NEW_ID
) -> RemoteImage:
    return RemoteImage(reference=f"{IMAGE}:latest", image_id=image_id, revision=revision, created=created)


def running(revision: str | None = RUNNING, created: float | None = BUILT, image_id: str = OLD_ID) -> Running:
    return Running(image_id=image_id, revision=revision, created=created)


# ------------------------------------------------------------------ decide


@pytest.mark.parametrize(
    ("latest", "now", "action"),
    [
        (remote(), None, Action.ERROR),
        (remote(), running(image_id=NEW_ID), Action.CURRENT),
        (remote(revision=None), running(), Action.ERROR),
        (remote(revision="v1.2.3"), running(), Action.ERROR),
        (remote(), running(revision=LATEST), Action.CURRENT),
        (remote(revision=LATEST.upper()), running(revision=LATEST), Action.CURRENT),
        (remote(created=None), running(), Action.ERROR),
        (remote(), running(created=None), Action.ERROR),
        (remote(created=BUILT), running(), Action.AHEAD),
        (remote(created=BUILT - 1), running(), Action.AHEAD),
        (remote(), running(), Action.FILE),
        (remote(), running(revision=None), Action.FILE),
    ],
)
def test_decide(latest: RemoteImage, now: Running | None, action: Action) -> None:
    assert decide(latest, now).action is action


def test_a_newer_latest_is_filed_for_its_revision() -> None:
    decision = decide(remote(), running())
    assert decision.commit == LATEST


def test_running_ahead_of_latest_is_never_a_ticket() -> None:
    # The runner deploys a merged commit main promoted before any release
    # carries it. Following :latest then would be a downgrade.
    decision = decide(remote(created=BUILT - 3600), running())
    assert decision.action is Action.AHEAD
    assert decision.commit is None


# --------------------------------------------------------------- snapshot


def ps_entry(name: str = NAME, image_id: str = "b" * 64, state: str = "running") -> dict[str, Any]:
    return {"Names": [name], "ImageID": image_id, "State": state}


def image_entry(
    image_id: str = "b" * 64, revision: str | None = RUNNING, created: object = BUILT
) -> dict[str, Any]:
    return {"Id": image_id, "Created": created, "Labels": {REVISION_LABEL: revision} if revision else None}


def test_the_snapshot_joins_containers_to_their_images() -> None:
    snap = Snapshot([ps_entry()], [image_entry()])
    assert snap.running(NAME) == Running(image_id=NEW_ID, revision=RUNNING, created=BUILT)


def test_the_snapshot_reads_prefixed_ids_either_side() -> None:
    snap = Snapshot([ps_entry(image_id=NEW_ID)], [image_entry(image_id=NEW_ID)])
    found = snap.running(NAME)
    assert found is not None and found.image_id == NEW_ID and found.revision == RUNNING


@pytest.mark.parametrize(
    "containers",
    [[ps_entry(state="exited")], [ps_entry(image_id="")], ["junk"], [ps_entry(name="other")]],
)
def test_a_container_that_is_not_running_here_is_absent(containers: list[Any]) -> None:
    assert Snapshot(containers, [image_entry()]).running(NAME) is None


@pytest.mark.parametrize(
    "images",
    [[], [image_entry(revision=None, created=0)], [image_entry(created="yesterday")], ["junk", {"Id": 5}]],
)
def test_an_image_the_snapshot_cannot_describe_has_no_revision_or_time(images: list[Any]) -> None:
    found = Snapshot([ps_entry()], images).running(NAME)
    assert found is not None and found.created is None
    assert found.revision in (None, RUNNING)


def test_a_snapshot_loads_from_its_directory(tmp_path: Path) -> None:
    (tmp_path / follow.CONTAINERS_FILE).write_text(json.dumps([ps_entry()]))
    (tmp_path / follow.IMAGES_FILE).write_text(json.dumps([image_entry()]))
    assert Snapshot.load(tmp_path).running(NAME) is not None


@pytest.mark.parametrize(
    ("containers", "fragment"),
    [(None, "cannot read"), ("{", "is not JSON"), ('{"a": 1}', "not a JSON list")],
)
def test_a_snapshot_that_cannot_be_read_says_why(
    tmp_path: Path, containers: str | None, fragment: str
) -> None:
    if containers is not None:
        (tmp_path / follow.CONTAINERS_FILE).write_text(containers)
    (tmp_path / follow.IMAGES_FILE).write_text("[]")
    with pytest.raises(SnapshotError, match=fragment):
        Snapshot.load(tmp_path)


# ------------------------------------------------------------------ a pass


def ticket(ticket_id: int, body: str, creator: str = "scott", status: str = "new") -> Ticket:
    return Ticket(
        id=ticket_id, queue="Deploy", status=status, subject="x", creator=creator, owner="Nobody", body=body
    )


def body(name: str = NAME, commit: str = LATEST) -> str:
    return f"service: {name}\nrepo: crunchtools/{name}\ncommit: {commit}\n"


class FakeRt:
    def __init__(
        self, *tickets: Ticket, stalled: tuple[int, ...] = (), answered: tuple[int, ...] = ()
    ) -> None:
        self.tickets = {t.id: t for t in tickets}
        self.stalled_ids = stalled
        self.answered = answered
        self.created: list[tuple[str, str, str]] = []
        self.queries: list[str] = []

    async def live_tickets(self, queue: str) -> tuple[int, ...]:
        assert queue == "Deploy"
        return tuple(self.tickets)

    async def search(self, query: str) -> tuple[int, ...]:
        self.queries.append(query)
        if "Status = 'stalled'" in query:
            return self.stalled_ids
        return self.answered

    async def ticket(self, ticket_id: int) -> Ticket:
        return self.tickets[ticket_id]

    async def create_ticket(self, queue: str, subject: str, text: str) -> int:
        self.created.append((queue, subject, text))
        return FIRST_FILED + len(self.created) - 1


class FakeImages:
    def __init__(self, **by_image: RemoteImage | Exception) -> None:
        self.by_image = by_image

    async def image(self, image: str, tag: str = "latest") -> RemoteImage:
        found = self.by_image.get(image.rsplit("/", 1)[-1], remote())
        if isinstance(found, Exception):
            raise found
        return found


def snapshot(**by_name: Running) -> Snapshot:
    """A snapshot built the way the host writes one: ps entries joined to images."""
    wanted = by_name or {NAME: running()}
    containers = [ps_entry(name=name, image_id=r.image_id) for name, r in wanted.items()]
    images = [
        image_entry(image_id=r.image_id, revision=r.revision, created=r.created) for r in wanted.values()
    ]
    return Snapshot(containers, images)


def registry(*rows: Service) -> Registry:
    return Registry({row.name: row for row in (rows or (service(),))})


async def test_a_service_behind_its_release_gets_one_ticket_in_the_ordinary_format() -> None:
    rt = FakeRt()
    report = await follow.follow(registry(), FakeImages(), rt, snapshot())
    assert report.outcome == follow.OUTCOME_OK
    assert [r.result for r in report.services] == ["filed"]
    ((queue, subject, text),) = rt.created
    assert (queue, subject) == ("Deploy", f"release-follow: {NAME} {LATEST}")
    request = parse_ticket(text)
    assert (request.service, request.repo, request.commit) == (NAME, f"crunchtools/{NAME}", LATEST)
    # And the runner would take it from follower for this row.
    assert creator_may_request("follower", service().requestors)


async def test_only_rows_that_list_the_follower_are_filed_for() -> None:
    rt = FakeRt()
    report = await follow.follow(
        registry(service(), service(name="mcp-pcloud", requestors=("scott",))),
        FakeImages(),
        rt,
        snapshot(**{NAME: running(), "mcp-pcloud": running()}),
    )
    assert [(r.service, r.result, r.followed) for r in report.services] == [
        (NAME, "filed", True),
        ("mcp-pcloud", "behind", False),
    ]
    ((_, subject, _),) = rt.created
    assert subject == subject_for(NAME, LATEST)


async def test_an_unfollowed_row_behind_its_release_is_reported_with_since_and_never_filed() -> None:
    # rotv drifted 37 releases and is not followed: deploy drift has to see it.
    rt = FakeRt()
    row = service(requestors=("scott",))
    report = await follow.follow(registry(row), FakeImages(), rt, snapshot())
    (result,) = report.services
    assert (result.result, result.since, result.followed) == ("behind", BUILT + 60, False)
    assert result.ticket is None
    assert rt.created == [] and not any("Subject" in q for q in rt.queries)
    assert report.outcome == follow.OUTCOME_OK
    assert report.detail == "0 followed of 1, 0 filed, 1 behind, 0 unjudged, 0 stalled"


@pytest.mark.parametrize(
    ("images", "result"),
    [
        (FakeImages(**{NAME: remote(image_id=OLD_ID)}), "current"),
        (FakeImages(**{NAME: remote(created=BUILT - 1)}), "ahead"),
        (FakeImages(**{NAME: OciError("registry down")}), "error"),
    ],
)
async def test_an_unfollowed_row_that_is_not_behind_carries_no_since(images: FakeImages, result: str) -> None:
    report = await follow.follow(registry(service(requestors=("scott",))), images, FakeRt(), snapshot())
    (only,) = report.services
    assert (only.result, only.since, only.followed) == (result, None, False)


@pytest.mark.parametrize(
    ("rt", "dry_run", "result"),
    [
        (FakeRt(), False, "filed"),
        (FakeRt(), True, "would-file"),
        (FakeRt(ticket(SEAT_TICKET, body())), False, "pending"),
        (FakeRt(answered=(1650,)), False, "answered"),
    ],
)
async def test_every_behind_result_carries_the_release_build_time(
    rt: FakeRt, dry_run: bool, result: str
) -> None:
    # Nagios measures drift grace from this, statelessly.
    report = await follow.follow(registry(), FakeImages(), rt, snapshot(), dry_run=dry_run)
    assert (report.services[0].result, report.services[0].since) == (result, BUILT + 60)


async def test_the_follower_opt_in_ignores_case() -> None:
    rows = registry(service(requestors=("scott", " Follower ")))
    assert [s.name for s in follow.followed(rows)] == [NAME]


async def test_a_live_ticket_from_anyone_for_the_same_commit_covers_it() -> None:
    rt = FakeRt(ticket(SEAT_TICKET, body()), ticket(1701, "prose that is not a ticket"))
    report = await follow.follow(registry(), FakeImages(), rt, snapshot())
    assert report.services[0].result == "pending"
    assert report.services[0].ticket == SEAT_TICKET
    assert rt.created == []


async def test_a_live_ticket_for_another_commit_does_not_cover_it() -> None:
    rt = FakeRt(ticket(1700, body(commit=RUNNING)))
    report = await follow.follow(registry(), FakeImages(), rt, snapshot())
    assert report.services[0].result == "filed"


async def test_a_commit_the_follower_already_filed_is_never_filed_again() -> None:
    # Resolved or rejected by a person: answered until the next release.
    rt = FakeRt(answered=(1650,))
    report = await follow.follow(registry(), FakeImages(), rt, snapshot())
    assert (report.services[0].result, report.services[0].ticket) == ("answered", 1650)
    assert rt.created == []
    assert any(
        f"Subject = '{subject_for(NAME, LATEST)}'" in q and "Creator = 'follower'" in q for q in rt.queries
    )


async def test_a_dry_run_files_nothing() -> None:
    rt = FakeRt()
    report = await follow.follow(registry(), FakeImages(), rt, snapshot(), dry_run=True)
    assert report.services[0].result == "would-file"
    assert rt.created == []


@pytest.mark.parametrize(
    ("images", "snap", "result"),
    [
        (FakeImages(**{NAME: OciError("registry down")}), None, "error"),
        (FakeImages(), {"other": running()}, "error"),
        (FakeImages(**{NAME: remote(image_id=OLD_ID)}), None, "current"),
        (FakeImages(**{NAME: remote(created=BUILT - 1)}), None, "ahead"),
    ],
)
async def test_services_that_need_nothing_file_nothing(
    images: FakeImages, snap: dict[str, Running] | None, result: str
) -> None:
    rt = FakeRt()
    report = await follow.follow(registry(), images, rt, snapshot(**(snap or {})))
    assert report.services[0].result == result
    assert rt.created == []
    expected = follow.OUTCOME_DEGRADED if result == "error" else follow.OUTCOME_OK
    assert report.outcome == expected


async def test_a_service_name_unsafe_for_a_query_is_not_followed() -> None:
    rt = FakeRt()
    report = await follow.follow(registry(service(name="x' OR 'y")), FakeImages(), rt, snapshot())
    assert report.services[0].result == "error"
    assert rt.created == [] and not any("x' OR" in q for q in rt.queries)


async def test_stalled_follower_tickets_are_reported() -> None:
    rt = FakeRt(
        ticket(1800, body(), creator="follower", status="stalled"),
        ticket(1801, "garbage", creator="follower", status="stalled"),
        stalled=(1800, 1801),
    )
    report = await follow.follow(registry(), FakeImages(), rt, snapshot())
    assert report.stalled == (Stalled(1800, NAME, LATEST), Stalled(1801, "?", "?"))
    # The stalled one is live and names the same commit, so it is not refiled.
    assert report.services[0].result == "pending"
    assert "1 followed of 1, 0 filed, 1 behind, 0 unjudged, 2 stalled" == report.detail


# ------------------------------------------------------------- status file


def test_the_status_file_is_one_fact_per_line_and_ends_whole(tmp_path: Path) -> None:
    report = FollowReport(
        follow.OUTCOME_OK,
        "1 followed",
        (
            follow.ServiceResult(NAME, "filed", ":latest is 635cdda3c863", 9001, BUILT - 7200),
            follow.ServiceResult("rotv", "current", "running_x", followed=False),
        ),
        (Stalled(1800, NAME, LATEST),),
    )
    path = write_status(tmp_path / "f" / "release-follow.status", report, now=BUILT)
    lines = path.read_text().splitlines()
    assert lines[0] == f"FOLLOWER generated={BUILT:.0f} outcome=ok detail=1_followed"
    assert lines[1] == (
        f"FOLLOWER service name={NAME} result=filed ticket=9001 followed=yes "
        f"since={BUILT - 7200:.0f} detail=:latest_is_635cdda3c863"
    )
    assert lines[2] == (
        "FOLLOWER service name=rotv result=current ticket=- followed=no since=- detail=running_x"
    )
    assert lines[3] == f"FOLLOWER stalled ticket=1800 service={NAME} commit={LATEST[:12]}"
    assert lines[-1] == "FOLLOWER end=1"


def test_an_empty_field_is_still_a_field(tmp_path: Path) -> None:
    path = write_status(tmp_path / "s", FollowReport(follow.OUTCOME_OK, ""))
    assert "detail=- " not in path.read_text()
    assert path.read_text().splitlines()[0].endswith("detail=-")


# -------------------------------------------------------------------- main

ENV = {"RT_URL": "http://127.0.0.1:8084", "RT_USER": "follower", "RT_PASSWORD": "pw", "RT_TIMEZONE": "UTC"}
REGISTRY_TOML = """
[service."mcp-nagios"]
repo = "crunchtools/mcp-nagios"
image = "quay.io/crunchtools/mcp-nagios"
unit = "mcp-nagios.crunchtools.com.service"
nagios_host = "ctr-mcp-nagios.crunchtools.com"
requestors = ["scott", "follower"]
probe = { kind = "mcp", url = "http://127.0.0.1:8026/mcp" }
"""


@pytest.fixture
def setup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    for name, value in ENV.items():
        monkeypatch.setenv(name, value)
    (tmp_path / "services.toml").write_text(REGISTRY_TOML)
    (tmp_path / follow.CONTAINERS_FILE).write_text("[]")
    (tmp_path / follow.IMAGES_FILE).write_text("[]")
    monkeypatch.setenv(follow.CONFIG_ENV, str(tmp_path / "services.toml"))
    monkeypatch.setenv(follow.SNAPSHOT_ENV, str(tmp_path))
    monkeypatch.setenv(follow.STATUS_ENV, str(tmp_path / "out" / "release-follow.status"))
    return tmp_path


def fake_run(report: FollowReport | Exception) -> Any:
    async def run(*_args: object) -> FollowReport:
        if isinstance(report, Exception):
            raise report
        return report

    return run


@pytest.mark.parametrize(
    ("report", "code", "outcome"),
    [
        (FollowReport(follow.OUTCOME_OK, "fine"), follow.EXIT_OK, "ok"),
        (FollowReport(follow.OUTCOME_DEGRADED, "one unjudged"), follow.EXIT_DEGRADED, "degraded"),
        (RtError("RT is down"), follow.EXIT_COULD_NOT_RUN, "could-not-run"),
    ],
)
def test_main_exit_codes_and_the_status_it_leaves(
    setup: Path, monkeypatch: pytest.MonkeyPatch, report: FollowReport | Exception, code: int, outcome: str
) -> None:
    monkeypatch.setattr(follow, "_run", fake_run(report))
    assert follow.main([]) == code
    assert f"outcome={outcome}" in (setup / "out" / "release-follow.status").read_text()


def test_a_dry_run_writes_no_status_and_needs_no_status_path(
    setup: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(follow.STATUS_ENV)
    seen: list[bool] = []

    async def run(_r: object, _c: object, _s: object, dry_run: bool) -> FollowReport:
        seen.append(dry_run)
        return FollowReport(follow.OUTCOME_OK, "fine")

    monkeypatch.setattr(follow, "_run", run)
    assert follow.main(["--dry-run"]) == follow.EXIT_OK
    assert seen == [True]
    assert not (setup / "out").exists()


@pytest.mark.parametrize("missing", [follow.CONFIG_ENV, follow.SNAPSHOT_ENV, follow.STATUS_ENV])
def test_a_missing_path_is_could_not_run(
    setup: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], missing: str
) -> None:
    del setup
    monkeypatch.delenv(missing)
    assert follow.main([]) == follow.EXIT_COULD_NOT_RUN
    assert missing in capsys.readouterr().err


def test_any_rt_user_but_follower_is_refused(setup: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RT_USER", "scott")
    monkeypatch.setattr(follow, "_run", fake_run(AssertionError("must not run")))
    assert follow.main([]) == follow.EXIT_COULD_NOT_RUN
    status = (setup / "out" / "release-follow.status").read_text()
    assert "outcome=could-not-run" in status and "RT_USER_must_be" in status


@pytest.mark.parametrize(
    "breakage",
    ["registry", "snapshot", "rt"],
)
def test_bad_configuration_is_could_not_run_and_says_so_to_nagios(
    setup: Path, monkeypatch: pytest.MonkeyPatch, breakage: str
) -> None:
    if breakage == "registry":
        (setup / "services.toml").write_text("not = [toml")
    elif breakage == "snapshot":
        (setup / follow.IMAGES_FILE).unlink()
    else:
        monkeypatch.delenv("RT_URL")
    assert follow.main([]) == follow.EXIT_COULD_NOT_RUN
    assert "outcome=could-not-run" in (setup / "out" / "release-follow.status").read_text()


def test_a_status_file_that_cannot_be_written_is_degraded_not_fatal(
    setup: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    (setup / "out").write_text("a file where the directory should be")
    monkeypatch.setattr(follow, "_run", fake_run(FollowReport(follow.OUTCOME_OK, "fine")))
    assert follow.main([]) == follow.EXIT_DEGRADED
    assert "cannot write" in capsys.readouterr().err


def test_main_prints_what_it_did(
    setup: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    del setup
    report = FollowReport(
        follow.OUTCOME_OK,
        "1 followed",
        (
            follow.ServiceResult(NAME, "filed", ":latest is x", 9001),
            follow.ServiceResult("b", "current", "ok"),
        ),
        (Stalled(1800, NAME, LATEST),),
    )
    monkeypatch.setattr(follow, "_run", fake_run(report))
    follow.main([])
    out = capsys.readouterr().out
    assert f"{NAME}: filed (ticket 9001)" in out
    assert "b: current: ok" in out
    assert "stalled follower ticket 1800" in out


async def test_run_wires_the_real_clients(setup: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """_run builds an RtClient and a RegistryReader and hands both to follow()."""
    seen: list[str] = []

    async def fake_follow(
        _reg: object, images: object, rt: object, _snap: object, dry_run: bool
    ) -> FollowReport:
        seen.extend([type(images).__name__, type(rt).__name__, str(dry_run)])
        return FollowReport(follow.OUTCOME_OK, "fine")

    monkeypatch.setattr(follow, "follow", fake_follow)
    from deployer.registry import load_registry
    from deployer.rt import RtCredentials

    creds = RtCredentials.from_env(ENV)
    report = await follow._run(load_registry(setup / "services.toml"), creds, Snapshot([], []), False)
    assert report.outcome == follow.OUTCOME_OK
    assert seen == ["RegistryReader", "RtClient", "False"]


def test_version(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        follow.main(["--version"])
    assert capsys.readouterr().out.startswith("release-follow ")


# -------------------------------------------------------------- the unit


UNIT = Path(__file__).resolve().parent.parent / "deploy/systemd/release-follow.crunchtools.com.service"


def test_the_unit_holds_no_socket_and_files_as_follower() -> None:
    text = UNIT.read_text()
    assert "--entrypoint release-follow" in text
    assert "podman.sock" not in text and "dbus" not in text
    assert "--cap-drop=all" in text and "--read-only" in text and "--user 0" not in text
    # Only its own corner of the deployer's state is writable.
    assert "-v /var/lib/deployer:/var" not in text
    assert "-v /var/lib/deployer/follower:/var/lib/deployer/follower" in text
    assert "EnvironmentFile=/run/srv-secrets/release-follow.crunchtools.com/release-follow.env" in text
    assert "--env-file" not in text
    # The command after the image overrides the image's CMD ["run"].
    assert "--config /config/services.toml" in text.split("quay.io/crunchtools/deployer:", 1)[1]


def test_the_timer_is_hourly() -> None:
    text = UNIT.with_suffix(".timer").read_text()
    assert "OnCalendar=hourly" in text
