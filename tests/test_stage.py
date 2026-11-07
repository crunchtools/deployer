"""Staging tests.

The ordering assertion is the important one: when staging finishes, the local
``:latest`` must point exactly where it pointed before. Everything else about
this design rests on that, because it is what stops a half-finished or refused
ticket from turning an unrelated crash-restart into a deploy.
"""

from __future__ import annotations

from typing import Any

import pytest

from deployer.podman import REVISION_LABEL, ImageRef, PodmanError
from deployer.registry import Probe, Service
from deployer.stage import StageError, Verdict, classify_revision, stage, tag_suffix, unit_shape_ok

OLD = "sha256:0000old"
NEW = "sha256:1111new"
OLD_REV = "f0effffab7fc0b0f8476c14f5794e0da60256eb0"
NEW_REV = "635cdda3c8632d1b5aed4cc22943895205ce5138"
IMAGE = "quay.io/crunchtools/acquacotta"
UNIT = "acquacotta.crunchtools.com.service"
BUILD_DEADLINE = 1800


def service(**over: Any) -> Service:
    base: dict[str, Any] = {
        "name": "acquacotta.crunchtools.com",
        "repo": "crunchtools/acquacotta",
        "image": IMAGE,
        "unit": UNIT,
        "probe": Probe(kind="http", url="http://127.0.0.1:8080/"),
        "nagios_host": "ctr-acquacotta.crunchtools.com",
        "requestors": ("developer1",),
        "build_deadline": BUILD_DEADLINE,
    }
    return Service(**{**base, **over})


class FakeSystemd:
    """Returns ExecStart/ExecStartPre in systemd's own structure shape."""

    def __init__(self, exec_start: str | None, exec_start_pre: list[str] | None = None) -> None:
        self._start = [("/usr/bin/podman", exec_start.split(), False)] if exec_start else []
        self._pre = [("/usr/bin/podman", line.split(), False) for line in (exec_start_pre or [])]

    async def property(self, unit: str, name: str, interface: str = "") -> Any:
        del unit, interface
        return self._start if name == "ExecStart" else self._pre


class FakePodman:
    """Records every tag call, which is how the ordering is asserted."""

    def __init__(self, running_image: str = OLD, pulled: str = NEW, labels: dict[str, str] | None = None):
        self.tags: list[tuple[str, str]] = []
        self._running = running_image
        self._pulled = pulled
        # `labels or {...}` would treat an explicitly empty dict as "not given",
        # which is the case the no-revision test is trying to set up.
        self._labels = {OLD: OLD_REV, NEW: NEW_REV} if labels is None else labels

    async def inspect_container(self, name: str) -> dict[str, Any]:
        del name
        if not self._running:
            return {}
        return {"Image": self._running}

    async def inspect_image(self, name: str) -> ImageRef:
        rev = self._labels.get(name)
        return ImageRef(id=name, digest=None, tags=(), labels={REVISION_LABEL: rev} if rev else {})

    async def pull(self, reference: str) -> str:
        del reference
        return self._pulled

    async def tag(self, name: str, reference: str) -> None:
        self.tags.append((name, reference))


async def test_a_well_shaped_unit_passes_the_gate() -> None:
    await unit_shape_ok(FakeSystemd(f"run -d --name x {IMAGE}:latest"), service())


async def test_a_unit_pinned_to_a_version_is_refused() -> None:
    with pytest.raises(StageError, match="does not end"):
        await unit_shape_ok(FakeSystemd(f"run -d {IMAGE}:6.8.0"), service())


async def test_a_unit_that_pulls_on_start_is_refused() -> None:
    systemd = FakeSystemd(f"run -d {IMAGE}:latest", [f"podman pull {IMAGE}:latest"])
    with pytest.raises(StageError, match="pulls on start"):
        await unit_shape_ok(systemd, service())


async def test_an_unreadable_exec_start_is_refused_not_assumed_fine() -> None:
    with pytest.raises(StageError, match="cannot read ExecStart"):
        await unit_shape_ok(FakeSystemd(None), service())


async def test_staging_leaves_latest_where_it_started() -> None:
    podman = FakePodman()
    systemd = FakeSystemd(f"run -d {IMAGE}:latest")
    staged = await stage(podman, systemd, service(), NEW_REV)

    # The rollback tag comes off the RUNNING container's image, and the last
    # tag written puts :latest back on it.
    assert podman.tags[0] == (OLD, f"localhost/acquacotta:rollback-{OLD_REV[:12]}")
    assert podman.tags[1] == (NEW, f"localhost/acquacotta:candidate-{NEW_REV[:12]}")
    assert podman.tags[-1] == (OLD, f"{IMAGE}:latest")
    assert staged.candidate_revision == NEW_REV
    assert staged.outgoing_revision == OLD_REV


async def test_latest_is_restored_even_when_the_pull_changed_nothing() -> None:
    # Unconditionally, not as an optimisation: the invariant is "when staging
    # returns, :latest is where it was", and making it conditional is how an
    # invariant stops being one.
    podman = FakePodman(running_image=OLD, pulled=OLD)
    staged = await stage(podman, FakeSystemd(f"run -d {IMAGE}:latest"), service(), OLD_REV)
    assert podman.tags[-1] == (OLD, f"{IMAGE}:latest")
    assert staged.candidate_image_id == OLD


async def test_latest_is_restored_when_candidate_tagging_fails() -> None:
    # The hazard the finally exists for: the pull has already moved the local
    # tag, so a failure after it must not leave :latest on an unverified image.
    class FailsAfterPull(FakePodman):
        async def tag(self, name: str, reference: str) -> None:
            if "candidate-" in reference:
                raise PodmanError("no space left on device")
            await super().tag(name, reference)

    podman = FailsAfterPull()
    with pytest.raises(PodmanError, match="no space left"):
        await stage(podman, FakeSystemd(f"run -d {IMAGE}:latest"), service(), NEW_REV)
    assert podman.tags[-1] == (OLD, f"{IMAGE}:latest")


async def test_a_failure_to_restore_latest_says_so_loudly() -> None:
    class CannotRestore(FakePodman):
        async def tag(self, name: str, reference: str) -> None:
            if reference == f"{IMAGE}:latest":
                raise PodmanError("read-only image store")
            await super().tag(name, reference)

    with pytest.raises(StageError, match="may point at an unverified image"):
        await stage(CannotRestore(), FakeSystemd(f"run -d {IMAGE}:latest"), service(), NEW_REV)


async def test_a_container_with_no_image_id_is_refused() -> None:
    podman = FakePodman(running_image="")
    with pytest.raises(StageError, match="no image id"):
        await stage(podman, FakeSystemd(f"run -d {IMAGE}:latest"), service(), NEW_REV)


async def test_an_uninspectable_container_is_refused_with_the_reason() -> None:
    class Broken(FakePodman):
        async def inspect_container(self, name: str) -> dict[str, Any]:
            del name
            raise PodmanError("no such container")

    with pytest.raises(StageError, match="no such container"):
        await stage(Broken(), FakeSystemd(f"run -d {IMAGE}:latest"), service(), NEW_REV)


async def test_an_image_with_no_revision_label_still_gets_a_rollback_tag() -> None:
    # Falls back to the image id: a rollback tag that cannot be formed would
    # mean deploying with no way back.
    podman = FakePodman(labels={})
    staged = await stage(podman, FakeSystemd(f"run -d {IMAGE}:latest"), service(), NEW_REV)
    # The id's "sha256:" prefix is dropped: a tag cannot contain a colon, and a
    # rollback tag that failed to exist would leave no route back.
    assert staged.rollback_tag == "localhost/acquacotta:rollback-0000old"
    assert ":" not in staged.rollback_tag.rpartition(":")[2]
    assert staged.outgoing_revision is None


@pytest.mark.parametrize(
    ("revision", "age", "expected"),
    [
        (NEW_REV, 10, Verdict.MATCH),
        (NEW_REV.upper(), 10, Verdict.MATCH),
        (OLD_REV, 10, Verdict.WAITING),
        (None, 10, Verdict.WAITING),
        (OLD_REV, BUILD_DEADLINE - 1, Verdict.WAITING),
        (OLD_REV, BUILD_DEADLINE, Verdict.STALE),
        (None, BUILD_DEADLINE + 1, Verdict.STALE),
    ],
)
def test_a_mismatch_is_waiting_until_the_build_deadline(
    revision: str | None, age: int, expected: Verdict
) -> None:
    # A seat files the ticket when it merges; CI builds afterwards. For the
    # first build_deadline a mismatch means "not yet", after it means "not
    # coming".
    assert classify_revision(revision, NEW_REV, age, service()) is expected


@pytest.mark.parametrize(
    ("revision", "image_id", "expected"),
    [
        (NEW_REV, OLD, NEW_REV[:12]),
        (None, "sha256:abcdef0123456789", "abcdef012345"),
        (None, "abcdef0123456789", "abcdef012345"),
        ("", "sha256:abc", "abc"),
    ],
)
def test_a_tag_suffix_never_contains_a_colon(revision: str | None, image_id: str, expected: str) -> None:
    suffix = tag_suffix(revision, image_id)
    assert suffix == expected
    assert ":" not in suffix
