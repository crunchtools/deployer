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

import pytest

import deployer
from deployer.attempts import ROLLBACK_LIMIT, AttemptLog
from deployer.hold import Hold, HoldStore
from deployer.cli import CONFIG_ENV, EXIT_COULD_NOT_RUN, EXIT_OK, STATE_ENV, build_parser, main

FORBIDDEN = ("container_create", "container_start", "container_run", "container_exec")

PATHS = ["--config", "/nowhere/services.toml", "--state", "/nowhere/state"]


def test_version_is_the_package_version() -> None:
    assert deployer.__version__ == "0.0.0"


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
