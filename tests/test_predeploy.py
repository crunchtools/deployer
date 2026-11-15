"""The pre-deploy dump: the unit is run, and what it leaves is checked.

The unit is the host's and is not under test. What is: that a run which
succeeds but leaves nothing, leaves an old file, or leaves something that is
not a pg_dump archive is a failure; that a unit systemd would let outlive the
deployer's wait is refused before it starts; and that rotation, which runs only
after a deploy holds, keeps every dump of the newest releases -- so the one
taken before a failed attempt's migrations survives the retries.
"""

from __future__ import annotations

import os
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from deployer.predeploy import DUMPS_KEPT, PG_DUMP_MAGIC, PreDeployError, dump_dir, pre_deploy, rotate_dumps
from deployer.registry import PreDeploy, Probe, Service
from deployer.systemd import SystemdError

NOW = 1_800_000_000.0
NAME = "0123456789abt1601"
TIMEOUT = 300
SERVICE = Service(
    name="rootsofthevalley.org",
    repo="crunchtools/rotv",
    image="quay.io/crunchtools/rotv",
    unit="rootsofthevalley.org.service",
    probe=Probe(kind="http", url="http://127.0.0.1:8082/api/health"),
    nagios_host="ctr-rootsofthevalley.org",
    requestors=("scott",),
    pre_deploy=PreDeploy(kind="pg_dump", unit="rootsofthevalley.org-pg_dump@.service", timeout=TIMEOUT),
)


class FakeUnit:
    """The host template unit: writes what ``content`` says, or fails."""

    def __init__(
        self,
        state: Path,
        content: bytes | None = PG_DUMP_MAGIC + b"\x01\x0e",
        fails: bool = False,
        limit: Any = 240_000_000,
        at: float = NOW,
    ) -> None:
        self.state = state
        self.content = content
        self.fails = fails
        #: The unit's TimeoutStartUSec, as systemd reports it.
        self.limit = limit
        self.at = at
        self.runs: list[tuple[str, float]] = []

    async def property(self, unit: str, name: str, interface: str = "") -> Any:
        assert name == "TimeoutStartUSec"
        return self.limit

    async def run(self, instance: str, timeout: float) -> None:
        self.runs.append((instance, timeout))
        if self.fails:
            raise SystemdError(f"{instance} failed: failed, result 'exit-code'")
        if self.content is not None:
            directory = dump_dir(self.state, SERVICE)
            directory.mkdir(parents=True, exist_ok=True)
            path = directory / f"{instance.split('@')[1].removesuffix('.service')}.dump"
            path.write_bytes(self.content)
            os.utime(path, (self.at, self.at))


async def test_a_good_dump_runs_the_instance_and_returns_the_path(tmp_path: Path) -> None:
    unit = FakeUnit(tmp_path)
    path = await pre_deploy(unit, SERVICE, tmp_path, NAME, NOW)
    assert unit.runs == [(f"rootsofthevalley.org-pg_dump@{NAME}.service", TIMEOUT)]
    assert path == tmp_path / "dumps" / "rootsofthevalley.org" / f"{NAME}.dump"


async def test_a_failed_unit_is_a_failed_step(tmp_path: Path) -> None:
    with pytest.raises(PreDeployError, match="did not succeed.*exit-code"):
        await pre_deploy(FakeUnit(tmp_path, fails=True), SERVICE, tmp_path, NAME, NOW)


async def test_a_unit_that_succeeds_and_writes_nothing_is_a_failed_step(tmp_path: Path) -> None:
    with pytest.raises(PreDeployError, match="left no readable dump"):
        await pre_deploy(FakeUnit(tmp_path, content=None), SERVICE, tmp_path, NAME, NOW)


async def test_a_file_that_is_not_a_pg_dump_archive_is_refused(tmp_path: Path) -> None:
    # Plain-format SQL, or an error message redirected into the file.
    with pytest.raises(PreDeployError, match="not a pg_dump custom-format archive"):
        await pre_deploy(FakeUnit(tmp_path, content=b"pg_dump: error"), SERVICE, tmp_path, NAME, NOW)


async def test_an_old_dump_of_the_same_name_does_not_pass_for_this_one(tmp_path: Path) -> None:
    await pre_deploy(FakeUnit(tmp_path), SERVICE, tmp_path, NAME, NOW)
    with pytest.raises(PreDeployError, match="predates this run"):
        await pre_deploy(FakeUnit(tmp_path, content=None), SERVICE, tmp_path, NAME, NOW + 3600)


@pytest.mark.parametrize("limit", [(TIMEOUT + 1) * 1_000_000, 2**64 - 1, "", None])
async def test_a_unit_that_could_outlive_the_wait_is_refused_before_it_runs(
    tmp_path: Path, limit: Any
) -> None:
    # The unit's TimeoutStartSec must not exceed pre_deploy.timeout, or the
    # deployer walks away from a dump still running. Infinity and an
    # unreadable value are refused the same way.
    unit = FakeUnit(tmp_path, limit=limit)
    with pytest.raises(PreDeployError, match="TimeoutStartSec must not exceed"):
        await pre_deploy(unit, SERVICE, tmp_path, NAME, NOW)
    assert unit.runs == []


async def test_a_name_that_cannot_name_an_instance_is_refused_before_anything_runs(tmp_path: Path) -> None:
    unit = FakeUnit(tmp_path)
    with pytest.raises(PreDeployError, match="cannot name a unit instance"):
        await pre_deploy(unit, SERVICE, tmp_path, "../x", NOW)
    assert unit.runs == []


async def test_a_row_without_the_step_is_refused(tmp_path: Path) -> None:
    with pytest.raises(PreDeployError, match="no pre_deploy step"):
        await pre_deploy(FakeUnit(tmp_path), replace(SERVICE, pre_deploy=None), tmp_path, NAME, NOW)


async def test_the_step_itself_rotates_nothing(tmp_path: Path) -> None:
    directory = dump_dir(tmp_path, SERVICE)
    directory.mkdir(parents=True)
    old = [directory / f"{release}t1.dump" for release in ("aaaaaaaaaaaa", "bbbbbbbbbbbb", "cccccccccccc")]
    for path in old:
        path.write_bytes(PG_DUMP_MAGIC)
    partial = directory / ".ddddddddddddt2.partial"
    partial.write_bytes(b"PGD")
    await pre_deploy(FakeUnit(tmp_path), SERVICE, tmp_path, NAME, NOW)
    assert all(path.exists() for path in [*old, partial])


def test_rotation_keeps_every_dump_of_the_newest_releases(tmp_path: Path) -> None:
    directory = dump_dir(tmp_path, SERVICE)
    directory.mkdir(parents=True)
    ages = {
        "aaaaaaaaaaaat10.dump": 400,  # the oldest release: goes
        "bbbbbbbbbbbbt11.dump": 300,  # the second release, first attempt: kept with its retry
        "bbbbbbbbbbbbt12.dump": 200,
        "cccccccccccct13.dump": 100,  # the newest release
    }
    for name, age in ages.items():
        path = directory / name
        path.write_bytes(PG_DUMP_MAGIC)
        os.utime(path, (NOW - age, NOW - age))
    (directory / ".cccccccccccct9.partial").write_bytes(b"PGD")

    oldest_release_files = 2  # its one dump and the stray partial
    assert rotate_dumps(tmp_path, SERVICE) == oldest_release_files
    kept = sorted(path.name for path in directory.iterdir())
    assert kept == ["bbbbbbbbbbbbt11.dump", "bbbbbbbbbbbbt12.dump", "cccccccccccct13.dump"]
    assert len({name.partition("t")[0] for name in kept}) == DUMPS_KEPT


def test_rotation_that_cannot_remove_a_file_does_not_raise(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(self: Path, missing_ok: bool = False) -> None:
        raise PermissionError(f"cannot remove {self}")

    directory = dump_dir(tmp_path, SERVICE)
    directory.mkdir(parents=True)
    (directory / ".oldt1.partial").write_bytes(b"PGD")
    monkeypatch.setattr(Path, "unlink", refuse)
    assert rotate_dumps(tmp_path, SERVICE) == 0


def test_rotation_with_no_dump_directory_does_nothing(tmp_path: Path) -> None:
    assert rotate_dumps(tmp_path, SERVICE) == 0
