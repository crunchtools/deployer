"""Scaffold tests: the parser's shape, the path contract, and the capability claim.

The last one looks like a test of prose, and it is not. The absence of container
create/start/run/exec is the property the whole design rests on, so the moment a
module appears that offers one, something should fail.
"""

from __future__ import annotations

import importlib
import pkgutil

import pytest

import deployer
from deployer.cli import CONFIG_ENV, EXIT_COULD_NOT_RUN, STATE_ENV, build_parser, main

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


def test_unimplemented_verb_says_so_rather_than_claiming_success() -> None:
    # A scaffold that exited 0 would read as "deployed" to the unit that runs it.
    assert main([*PATHS, "holds"]) == EXIT_COULD_NOT_RUN


def test_package_exposes_no_container_lifecycle_verb() -> None:
    for module in sorted(m.name for m in pkgutil.iter_modules(deployer.__path__)):
        mod = importlib.import_module(f"deployer.{module}")
        for forbidden in FORBIDDEN:
            assert not hasattr(mod, forbidden), f"deployer.{module} exposes {forbidden}"
