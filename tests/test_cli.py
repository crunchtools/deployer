"""The parser's shape, the path contract, the capability claim, and the verbs.

The capability test looks like a test of prose, and it is not. The absence of
container create/start/run/exec is the property the whole design rests on, so
the moment a module appears that offers one, something should fail.

The verbs that read only the state directory -- ``holds`` and ``unhold`` -- are
tested against a real one, because that is their point: they work on a host
where podman, systemd or RT is the thing that is broken.
"""

from __future__ import annotations

import importlib
import pkgutil
from pathlib import Path
from typing import Any

import httpx
import pytest

import deployer
from deployer.attempts import ROLLBACK_LIMIT, AttemptLog
from deployer.hold import Hold, HoldStore
from deployer.cli import (
    CONFIG_ENV,
    EXIT_COULD_NOT_RUN,
    EXIT_ERROR,
    EXIT_OK,
    STATE_ENV,
    Hands,
    _rollback,
    build_parser,
    main,
)
from deployer.lock import only_one
from deployer.podman import ImageRef
from deployer.registry import load_registry
from deployer.systemd import UnitState

FORBIDDEN = ("container_create", "container_start", "container_run", "container_exec")

PATHS = ["--config", "/nowhere/services.toml", "--state", "/nowhere/state"]


def test_version_is_the_package_version() -> None:
    assert deployer.__version__ == "0.3.0"


@pytest.mark.parametrize("verb", ["run", "holds"])
def test_bare_verbs_parse(verb: str) -> None:
    args = build_parser().parse_args([verb])
    assert args.verb == verb
    assert args.dry_run is False


@pytest.mark.parametrize("verb", ["rollback", "unhold"])
def test_service_verbs_require_a_service(verb: str) -> None:
    args = build_parser().parse_args([verb, "acquacotta.crunchtools.com"])
    assert args.service == "acquacotta.crunchtools.com"
    with pytest.raises(SystemExit):
        build_parser().parse_args([verb])


def test_no_verb_is_an_error() -> None:
    with pytest.raises(SystemExit):
        build_parser().parse_args([])


def test_dry_run_and_paths_are_available_to_every_verb() -> None:
    args = build_parser().parse_args(["--dry-run", *PATHS, "run"])
    assert args.dry_run is True
    assert args.state == "/nowhere/state"
    assert args.config == "/nowhere/services.toml"


def test_paths_come_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(CONFIG_ENV, "/from/env/services.toml")
    monkeypatch.setenv(STATE_ENV, "/from/env/state")
    args = build_parser().parse_args(["run"])
    assert args.config == "/from/env/services.toml"
    assert args.state == "/from/env/state"


def test_a_missing_path_could_not_run_rather_than_guessing(monkeypatch: pytest.MonkeyPatch) -> None:
    # Guessing a path would mean reporting on whatever happened to be there.
    monkeypatch.delenv(CONFIG_ENV, raising=False)
    monkeypatch.delenv(STATE_ENV, raising=False)
    assert main(["run"]) == EXIT_COULD_NOT_RUN


def test_an_unloadable_registry_could_not_run(tmp_path: Path) -> None:
    bad = tmp_path / "services.toml"
    bad.write_text("this is not toml\n", encoding="utf-8")
    assert main(["--config", str(bad), "--state", str(tmp_path), "holds"]) == EXIT_COULD_NOT_RUN


# --- holds and unhold, which read nothing but the state directory ---


def registry_file(tmp_path: Path) -> Path:
    path = tmp_path / "services.toml"
    path.write_text(
        """
[service.acquacotta]
repo = "crunchtools/acquacotta"
image = "quay.io/crunchtools/acquacotta"
unit = "acquacotta.crunchtools.com.service"
nagios_host = "ctr-acquacotta.crunchtools.com"
requestors = ["developer1"]
probe = { kind = "http", url = "http://127.0.0.1:8080/health" }
# One second, not the 60 these services really use, so these tests do not each
# wait a minute of real time. The registry refuses 0 -- a settle window of no
# time is not a settle window -- and the loop's own timing is pinned in
# tests/test_verify.py, which is where it lives. `ready` is here for the same
# reason and matters more, because that window RETRIES a failing probe: left at
# its default, every test with a failing probe would wait a real minute.
settle = 1
ready = 1
""",
        encoding="utf-8",
    )
    return path


def run(tmp_path: Path, *argv: str) -> int:
    state = tmp_path / "state"
    state.mkdir(exist_ok=True)
    return main(["--config", str(registry_file(tmp_path)), "--state", str(state), *argv])


def test_holds_says_so_when_nothing_is_held(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert run(tmp_path, "holds") == EXIT_OK
    assert "no holds" in capsys.readouterr().out


def test_holds_names_the_unit_the_ticket_and_the_age(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    state = tmp_path / "state"
    state.mkdir()
    HoldStore(state).place(
        Hold(
            unit="acquacotta.crunchtools.com.service",
            service="acquacotta",
            ticket=1601,
            revision="f" * 40,
        )
    )
    assert run(tmp_path, "holds") == EXIT_OK
    out = capsys.readouterr().out
    assert "acquacotta.crunchtools.com.service" in out
    assert "ticket=1601" in out


def test_unhold_clears_the_hold_and_the_rollback_count(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # Both, because they are one decision: a service that hit the
    # three-in-a-day limit is refused by the gate, not by the hold, so
    # clearing only the hold would leave it refused with nothing to show why.
    state = tmp_path / "state"
    state.mkdir()
    holds = HoldStore(state)
    holds.place(
        Hold(
            unit="acquacotta.crunchtools.com.service",
            service="acquacotta",
            ticket=1601,
            revision="f" * 40,
        )
    )
    attempts = AttemptLog(state)
    for _ in range(ROLLBACK_LIMIT):
        attempts.record_rollback("acquacotta")

    assert run(tmp_path, "unhold", "acquacotta") == EXIT_OK
    assert "cleared the hold" in capsys.readouterr().out
    assert not holds.held("acquacotta.crunchtools.com.service")
    assert not attempts.too_many_rollbacks("acquacotta")


def test_unhold_of_an_unheld_service_is_not_an_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run(tmp_path, "unhold", "acquacotta") == EXIT_OK
    assert "was not held" in capsys.readouterr().out


def test_unhold_of_an_unknown_service_could_not_run(tmp_path: Path) -> None:
    assert run(tmp_path, "unhold", "not-a-service") == EXIT_COULD_NOT_RUN


def test_a_dry_run_unhold_changes_nothing(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    state = tmp_path / "state"
    state.mkdir()
    holds = HoldStore(state)
    holds.place(
        Hold(
            unit="acquacotta.crunchtools.com.service",
            service="acquacotta",
            ticket=1601,
            revision="f" * 40,
        )
    )
    assert run(tmp_path, "--dry-run", "unhold", "acquacotta") == EXIT_OK
    assert "would clear" in capsys.readouterr().out
    assert holds.held("acquacotta.crunchtools.com.service")


def test_package_exposes_no_container_lifecycle_verb() -> None:
    for module in sorted(m.name for m in pkgutil.iter_modules(deployer.__path__)):
        mod = importlib.import_module(f"deployer.{module}")
        for forbidden in FORBIDDEN:
            assert not hasattr(mod, forbidden), f"deployer.{module} exposes {forbidden}"


# --- rollback by hand: the guard, the lock, and the hold it leaves ---
#
# This path moves `:latest` and restarts a production unit, so it is tested
# rather than only read. The clients go in through `Hands`; everything else is
# the real code path, lock included.


class RollbackPodman:
    """Just enough podman for a by-hand rollback."""

    def __init__(self, rollback_tag: str = "localhost/acquacotta:rollback-abc123") -> None:
        self.rollback_tag = rollback_tag
        self.running = "sha256:aaa"
        self.tagged: list[tuple[str, str]] = []
        self.healthy = True

    async def inspect_container(self, name: str) -> dict[str, Any]:
        return {"Image": self.running, "Config": {"Env": []}}

    async def inspect_image(self, name: str) -> ImageRef:
        return ImageRef(id=self.running, digest=None, tags=(), labels={})

    async def pull(self, reference: str) -> str:
        raise AssertionError("a by-hand rollback must not pull")

    async def tag(self, name: str, reference: str) -> None:
        self.tagged.append((name, reference))

    async def untag(self, name: str, reference: str) -> None:
        return None

    async def remove_image(self, name: str, force: bool = False) -> None:
        return None

    async def list_images(self, reference: str | None = None) -> tuple[ImageRef, ...]:
        if self.rollback_tag:
            return (ImageRef(id="sha256:old", digest=None, tags=(self.rollback_tag,), labels={}),)
        return ()

    async def logs(self, name: str, tail: int = 40) -> str:
        return ""


class RollbackSystemd:
    def __init__(self, podman: RollbackPodman, guard: bool = True) -> None:
        self.podman = podman
        self.guard = guard
        self.restarts: list[str] = []

    async def restart(self, unit: str) -> None:
        self.restarts.append(unit)
        self.podman.running = "sha256:old"

    async def state(self, unit: str) -> UnitState:
        return UnitState(name=unit, active_state="active", sub_state="running", result="success")

    async def property(self, unit: str, name: str, interface: str = "") -> Any:
        return ""

    async def hold_guard_installed(self) -> bool:
        return self.guard


def hands(podman: RollbackPodman, systemd: RollbackSystemd) -> Hands:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200 if podman.healthy else 503, text="ok")

    return Hands(
        podman=podman,
        systemd=systemd,
        probes=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )


async def roll_back_by_hand(tmp_path: Path, **kwargs: Any) -> tuple[int, RollbackPodman, RollbackSystemd]:
    state = tmp_path / "state"
    state.mkdir(exist_ok=True)
    podman = RollbackPodman(rollback_tag=kwargs.pop("rollback_tag", "localhost/acquacotta:rollback-abc"))
    podman.healthy = kwargs.pop("healthy", True)
    systemd = RollbackSystemd(podman, guard=kwargs.pop("guard", True))
    registry = load_registry(registry_file(tmp_path))
    code = await _rollback(
        registry,
        "acquacotta",
        state,
        dry_run=kwargs.pop("dry_run", False),
        hands=hands(podman, systemd),
    )
    assert not kwargs, f"unused: {kwargs}"
    return code, podman, systemd


async def test_rolling_back_by_hand_restores_verifies_and_holds(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, podman, systemd = await roll_back_by_hand(tmp_path)
    assert code == EXIT_OK
    assert systemd.restarts == ["acquacotta.crunchtools.com.service"]
    # Ticket 0 means "no ticket -- a person did this by hand".
    hold = HoldStore(tmp_path / "state").get("acquacotta.crunchtools.com.service")
    assert hold is not None
    assert hold.ticket == 0
    assert "is now held" in capsys.readouterr().out


async def test_a_by_hand_rollback_is_refused_without_the_guard(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # Without the guard the hold would not be honoured, so the broken image
    # comes back at the next auto-update and the rollback was for nothing.
    code, _podman, systemd = await roll_back_by_hand(tmp_path, guard=False)
    assert code == EXIT_COULD_NOT_RUN
    assert systemd.restarts == []
    assert "hold guard is not installed" in capsys.readouterr().err
    assert not HoldStore(tmp_path / "state").held("acquacotta.crunchtools.com.service")


async def test_a_by_hand_rollback_stands_down_while_a_deploy_holds_the_lock(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # The important one. This moves `:latest`, and a run that is mid-staging has
    # already read it and will put it back where it found it. Interleaved, the
    # tag ends up pointing at whichever wrote last.
    state = tmp_path / "state"
    state.mkdir()
    registry = load_registry(registry_file(tmp_path))
    podman = RollbackPodman()
    systemd = RollbackSystemd(podman)
    with only_one(state):
        code = await _rollback(registry, "acquacotta", state, dry_run=False, hands=hands(podman, systemd))
    assert code == EXIT_COULD_NOT_RUN
    assert systemd.restarts == []
    assert "A deploy is in flight" in capsys.readouterr().err


async def test_a_by_hand_rollback_with_nothing_to_roll_back_to_says_so(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, _podman, systemd = await roll_back_by_hand(tmp_path, rollback_tag="")
    assert code == EXIT_COULD_NOT_RUN
    assert systemd.restarts == []
    assert "nothing to roll back to" in capsys.readouterr().err


async def test_a_by_hand_rollback_that_does_not_come_back_exits_one(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, _podman, _systemd = await roll_back_by_hand(tmp_path, healthy=False)
    assert code == EXIT_ERROR
    assert "did not come back" in capsys.readouterr().err
    # Held anyway: the service is on the old image and nothing should overwrite
    # that from the registry tonight.
    assert HoldStore(tmp_path / "state").held("acquacotta.crunchtools.com.service")


async def test_a_dry_run_rollback_changes_nothing(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code, podman, systemd = await roll_back_by_hand(tmp_path, dry_run=True)
    assert code == EXIT_OK
    assert (systemd.restarts, podman.tagged) == ([], [])
    assert "would put" in capsys.readouterr().out
    assert not HoldStore(tmp_path / "state").held("acquacotta.crunchtools.com.service")


def manual_registry_file(tmp_path: Path) -> Path:
    path = tmp_path / "manual.toml"
    path.write_text(
        """
[service.acquacotta]
repo = "crunchtools/acquacotta"
image = "quay.io/crunchtools/acquacotta"
unit = "acquacotta.crunchtools.com.service"
nagios_host = "ctr-acquacotta.crunchtools.com"
requestors = ["scott"]
probe = { kind = "http", url = "http://127.0.0.1:8080/health" }
settle = 1
ready = 1
# The thing under test: a service whose schema changes are not safely
# reversible by a robot.
rollback = "manual"
""",
        encoding="utf-8",
    )
    return path


async def test_a_manual_rollback_service_is_refused_before_the_host_is_touched(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # The registry is the thing that knows a service carries migrations.
    # Writing that down is pointless if it is then ignored.
    state = tmp_path / "state"
    state.mkdir()
    podman = RollbackPodman()
    systemd = RollbackSystemd(podman)
    code = await _rollback(
        load_registry(manual_registry_file(tmp_path)),
        "acquacotta",
        state,
        dry_run=False,
        hands=hands(podman, systemd),
    )
    assert code == EXIT_COULD_NOT_RUN
    assert (systemd.restarts, podman.tagged) == ([], [])
    err = capsys.readouterr().err
    assert "rollback = manual" in err
    # And it says what to do instead, with the two commands.
    assert "podman tag" in err
    assert "systemctl restart acquacotta.crunchtools.com.service" in err


def test_unhold_stands_down_while_a_deploy_holds_the_lock(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # unhold writes the same two records a failing deploy writes. Run while a
    # deploy is finishing it can drop a hold that deploy just placed, and the
    # broken image then comes back at the next auto-update with nothing saying
    # why.
    state = tmp_path / "state"
    state.mkdir()
    holds = HoldStore(state)
    holds.place(
        Hold(
            unit="acquacotta.crunchtools.com.service",
            service="acquacotta",
            ticket=1601,
            revision="f" * 40,
        )
    )
    with only_one(state):
        code = main(["--config", str(registry_file(tmp_path)), "--state", str(state), "unhold", "acquacotta"])
    assert code == EXIT_COULD_NOT_RUN
    assert "writing the same holds" in capsys.readouterr().err
    # Untouched, which is the point.
    assert holds.held("acquacotta.crunchtools.com.service")


# --- `run`: the parts of the CLI path that are not the runner ---
#
# The state machine itself is tested in test_runner.py against a fake host.
# What is left here is what `_run` owns: the lock, the status file, and the map
# from a run's outcome to an exit code.


def test_run_stands_down_quietly_when_another_run_holds_the_lock(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # Exit 0, because the unit that starts this is a oneshot whose failure
    # wakes a model, and "the previous run is still going" is the most
    # ordinary thing that can happen. And no status file: the run that holds
    # the lock owns it.
    state = tmp_path / "state"
    state.mkdir()
    with only_one(state):
        code = main(["--config", str(registry_file(tmp_path)), "--state", str(state), "run"])
    assert code == EXIT_OK
    assert "standing down" in capsys.readouterr().err
    assert not (state / "deployer.status").exists()


def test_a_half_configured_unit_could_not_run_and_says_so_to_nagios(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # Deploying with no way to reach RT is not a deploy. Exit 2, and the status
    # file says could-not-run so the Nagios check reports it rather than
    # leaving yesterday's outcome standing.
    state = tmp_path / "state"
    state.mkdir()
    for name in ("RT_URL", "RT_USER", "RT_PASSWORD", "RT_TIMEZONE"):
        monkeypatch.delenv(name, raising=False)
    code = main(["--config", str(registry_file(tmp_path)), "--state", str(state), "run"])
    assert code == EXIT_COULD_NOT_RUN
    assert "RT is not configured" in capsys.readouterr().err
    status = (state / "deployer.status").read_text(encoding="utf-8")
    assert "outcome=could-not-run" in status
    # Written whole, or a reader cannot tell it from a truncated file.
    assert status.rstrip().endswith("end=1")
