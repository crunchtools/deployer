"""The pre-deploy dump: the unit is run, and what it leaves is checked.

The unit is the host's and is not under test. What is: that a run which
succeeds but leaves nothing, leaves an old file, or leaves something that is
not a pg_dump archive is a failure, and that the dumps rotate like the
rollback tags they sit beside.
"""

from __future__ import annotations

import os
from dataclasses import replace
from pathlib import Path

import pytest

from deployer.predeploy import DUMPS_KEPT, PG_DUMP_MAGIC, PreDeployError, dump_dir, pre_deploy
from deployer.registry import PreDeploy, Probe, Service
from deployer.systemd import SystemdError

NOW = 1_800_000_000.0
SUFFIX = "0123456789ab"
SERVICE = Service(
    name="rootsofthevalley.org",
    repo="crunchtools/rotv",
    image="quay.io/crunchtools/rotv",
    unit="rootsofthevalley.org.service",
    probe=Probe(kind="http", url="http://127.0.0.1:8082/api/health"),
    nagios_host="ctr-rootsofthevalley.org",
    requestors=("scott",),
    pre_deploy=PreDeploy(kind="pg_dump", unit="rootsofthevalley.org-pg-dump@.service", timeout=42),
)


class FakeUnit:
    """The host template unit: writes what ``content`` says, or fails."""

    def __init__(self, state: Path, content: bytes | None = PG_DUMP_MAGIC + b"\x01\x0e", fails: bool = False):
        self.state = state
        self.content = content
        self.fails = fails
        self.runs: list[tuple[str, float]] = []

    async def run(self, instance: str, timeout: float) -> None:
        self.runs.append((instance, timeout))
        if self.fails:
            raise SystemdError(f"{instance} failed: failed, result 'exit-code'")
        if self.content is not None:
            directory = dump_dir(self.state, SERVICE)
            directory.mkdir(parents=True, exist_ok=True)
            path = directory / f"{instance.split('@')[1].removesuffix('.service')}.dump"
            path.write_bytes(self.content)
            os.utime(path, (NOW, NOW))


async def test_a_good_dump_runs_the_instance_and_returns_the_path(tmp_path: Path) -> None:
    unit = FakeUnit(tmp_path)
    path = await pre_deploy(unit, SERVICE, tmp_path, SUFFIX, NOW)
    assert unit.runs == [(f"rootsofthevalley.org-pg-dump@{SUFFIX}.service", 42)]
    assert path == tmp_path / "dumps" / "rootsofthevalley.org" / f"{SUFFIX}.dump"


async def test_a_failed_unit_is_a_failed_step(tmp_path: Path) -> None:
    with pytest.raises(PreDeployError, match="did not succeed.*exit-code"):
        await pre_deploy(FakeUnit(tmp_path, fails=True), SERVICE, tmp_path, SUFFIX, NOW)


async def test_a_unit_that_succeeds_and_writes_nothing_is_a_failed_step(tmp_path: Path) -> None:
    with pytest.raises(PreDeployError, match="left no readable dump"):
        await pre_deploy(FakeUnit(tmp_path, content=None), SERVICE, tmp_path, SUFFIX, NOW)


async def test_a_file_that_is_not_a_pg_dump_archive_is_refused(tmp_path: Path) -> None:
    # Plain-format SQL, or an error message redirected into the file.
    with pytest.raises(PreDeployError, match="not a pg_dump custom-format archive"):
        await pre_deploy(FakeUnit(tmp_path, content=b"pg_dump: error"), SERVICE, tmp_path, SUFFIX, NOW)


async def test_an_old_dump_of_the_same_name_does_not_pass_for_this_one(tmp_path: Path) -> None:
    # Same outgoing release as a refused ticket earlier: same name, old file.
    await pre_deploy(FakeUnit(tmp_path), SERVICE, tmp_path, SUFFIX, NOW)
    with pytest.raises(PreDeployError, match="predates this run"):
        await pre_deploy(FakeUnit(tmp_path, content=None), SERVICE, tmp_path, SUFFIX, NOW + 3600)


async def test_a_suffix_that_cannot_name_an_instance_is_refused_before_anything_runs(tmp_path: Path) -> None:
    unit = FakeUnit(tmp_path)
    with pytest.raises(PreDeployError, match="cannot name a unit instance"):
        await pre_deploy(unit, SERVICE, tmp_path, "../x", NOW)
    assert unit.runs == []


async def test_a_row_without_the_step_is_refused(tmp_path: Path) -> None:
    with pytest.raises(PreDeployError, match="no pre_deploy step"):
        await pre_deploy(FakeUnit(tmp_path), replace(SERVICE, pre_deploy=None), tmp_path, SUFFIX, NOW)


async def test_dumps_rotate_like_rollback_tags_and_partials_are_swept(tmp_path: Path) -> None:
    directory = dump_dir(tmp_path, SERVICE)
    directory.mkdir(parents=True)
    old = []
    for age, name in enumerate(("cccccccccccc", "bbbbbbbbbbbb", "aaaaaaaaaaaa"), start=1):
        path = directory / f"{name}.dump"
        path.write_bytes(PG_DUMP_MAGIC)
        os.utime(path, (NOW - age * 60, NOW - age * 60))
        old.append(path)
    partial = directory / ".dddddddddddd.partial"
    partial.write_bytes(b"PGD")

    path = await pre_deploy(FakeUnit(tmp_path), SERVICE, tmp_path, SUFFIX, NOW)

    kept = sorted(p.name for p in directory.iterdir())
    assert len(kept) == DUMPS_KEPT
    assert kept == sorted([path.name, old[0].name])
    assert not partial.exists()


async def test_a_dump_that_will_not_rotate_does_not_fail_the_step(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(self: Path, missing_ok: bool = False) -> None:
        raise PermissionError(f"cannot remove {self}")

    directory = dump_dir(tmp_path, SERVICE)
    directory.mkdir(parents=True)
    (directory / ".old.partial").write_bytes(b"PGD")
    monkeypatch.setattr(Path, "unlink", refuse)
    path = await pre_deploy(FakeUnit(tmp_path), SERVICE, tmp_path, SUFFIX, NOW)
    assert path.exists()
