"""Rollback tests.

Two properties matter beyond the happy path: a `rollback = "manual"` service is
never rolled back by this program, and a rollback that does not come back is
reported as needing a person rather than retried.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from deployer.podman import ImageRef, PodmanError
from deployer.registry import Probe, Service
from deployer.rollback import ROLLBACK_TAGS_KEPT, RollbackError, roll_back, rotate_rollback_tags
from deployer.stage import Staged
from deployer.systemd import UnitState
from deployer.verify import Host

OLD = "sha256:old"
NEW = "sha256:new"
IMAGE = "quay.io/crunchtools/acquacotta"
LATEST = f"{IMAGE}:latest"


def service(**over: Any) -> Service:
    base: dict[str, Any] = {
        "name": "acquacotta.crunchtools.com",
        "repo": "crunchtools/acquacotta",
        "image": IMAGE,
        "unit": "acquacotta.crunchtools.com.service",
        "probe": Probe(kind="http", url="http://127.0.0.1:8080/"),
        "nagios_host": "ctr-acquacotta.crunchtools.com",
        "requestors": ("developer1",),
        "settle": 5,
    }
    return Service(**{**base, **over})


def staged() -> Staged:
    return Staged(
        outgoing_image_id=OLD,
        outgoing_revision="f0effff",
        rollback_tag="localhost/acquacotta:rollback-f0effff",
        candidate_image_id=NEW,
        candidate_revision="635cdda",
        candidate_version="2.34.0",
        candidate_digest="sha256:feed",
        candidate_tag="localhost/acquacotta:candidate-635cdda",
        latest_tag=LATEST,
    )


class FakePodman:
    def __init__(self, tags: tuple[str, ...] = ()) -> None:
        self.tagged: list[tuple[str, str]] = []
        self.removed: list[str] = []
        self._listed = tags

    async def tag(self, name: str, reference: str) -> None:
        self.tagged.append((name, reference))

    async def untag(self, name: str, reference: str) -> None:
        del name, reference

    async def remove_image(self, name: str, force: bool = False) -> None:
        del force
        self.removed.append(name)

    async def list_images(self, reference: str | None = None) -> tuple[ImageRef, ...]:
        del reference
        return tuple(
            ImageRef(id=f"id{n}", digest=None, tags=(tag,), labels={}) for n, tag in enumerate(self._listed)
        )


class FakeRestarter:
    def __init__(self) -> None:
        self.restarted: list[str] = []

    async def restart(self, unit: str) -> None:
        self.restarted.append(unit)


class FakeSystemd:
    def __init__(self, active: str = "active") -> None:
        self._active = active

    async def state(self, unit: str) -> UnitState:
        return UnitState(name=unit, active_state=self._active, sub_state="running", result="success")


class FakeContainers:
    def __init__(self, image: str = OLD) -> None:
        self._image = image

    async def inspect_container(self, name: str) -> dict[str, Any]:
        del name
        return {"Image": self._image}


async def nosleep(_seconds: float) -> None:
    return None


def ok_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(lambda _r: httpx.Response(200, text="ok")))


async def test_a_rollback_moves_latest_back_restarts_and_verifies() -> None:
    podman, restarter = FakePodman(), FakeRestarter()
    async with ok_client() as client:
        host = Host(FakeSystemd(), FakeContainers(OLD), nosleep, client)
        restored = await roll_back(podman, restarter, host, service(), staged())
    assert restored.attempted and restored.verified
    assert podman.tagged == [(OLD, LATEST)]
    assert restarter.restarted == ["acquacotta.crunchtools.com.service"]
    assert not restored.needs_a_person


async def test_a_rollback_that_does_not_come_back_needs_a_person() -> None:
    # The signal to cancel the downtime and let Nagios page. Not retried: a
    # loop that restarts production is worse than a red check.
    podman, restarter = FakePodman(), FakeRestarter()
    async with ok_client() as client:
        host = Host(FakeSystemd(active="failed"), FakeContainers(OLD), nosleep, client)
        restored = await roll_back(podman, restarter, host, service(), staged())
    assert restored.attempted
    assert not restored.verified
    assert restored.needs_a_person
    assert "not active" in restored.reason


async def test_a_rollback_that_comes_back_on_the_wrong_image_is_not_verified() -> None:
    podman, restarter = FakePodman(), FakeRestarter()
    async with ok_client() as client:
        host = Host(FakeSystemd(), FakeContainers(NEW), nosleep, client)
        restored = await roll_back(podman, restarter, host, service(), staged())
    assert restored.needs_a_person


async def test_a_manual_rollback_service_is_left_alone() -> None:
    # Old code against a new schema is worse than a failed deploy with the
    # downtime still open.
    podman, restarter = FakePodman(), FakeRestarter()
    async with ok_client() as client:
        host = Host(FakeSystemd(), FakeContainers(OLD), nosleep, client)
        restored = await roll_back(podman, restarter, host, service(rollback="manual"), staged())
    assert not restored.attempted
    assert not restored.needs_a_person
    assert podman.tagged == []
    assert restarter.restarted == []
    assert "manual" in restored.reason


async def test_a_podman_failure_during_rollback_is_a_rollback_error() -> None:
    class Broken(FakePodman):
        async def tag(self, name: str, reference: str) -> None:
            del name, reference
            raise PodmanError("no space left on device")

    async with ok_client() as client:
        host = Host(FakeSystemd(), FakeContainers(OLD), nosleep, client)
        with pytest.raises(RollbackError, match="no space left"):
            await roll_back(Broken(), FakeRestarter(), host, service(), staged())


async def test_rotation_removes_by_tag_without_untagging_first() -> None:
    # Untagging made the reference stop resolving, so the removal that followed
    # it failed and was swallowed -- leaving the image data on disk, which is
    # the one thing the rotation exists to prevent.
    tags = tuple(f"localhost/acquacotta:rollback-{n}" for n in range(3))
    podman = FakePodman(tags=tags)
    removed = await rotate_rollback_tags(podman, service())
    assert removed == (tags[2],)
    assert podman.removed == [tags[2]]


async def test_rotation_keeps_two_and_removes_the_rest() -> None:
    tags = tuple(f"localhost/acquacotta:rollback-{n}" for n in range(5))
    podman = FakePodman(tags=tags)
    removed = await rotate_rollback_tags(podman, service())
    assert len(removed) == len(tags) - ROLLBACK_TAGS_KEPT
    assert podman.removed == list(tags[ROLLBACK_TAGS_KEPT:])


async def test_rotation_removes_nothing_when_there_are_only_two() -> None:
    podman = FakePodman(tags=("localhost/acquacotta:rollback-a", "localhost/acquacotta:rollback-b"))
    assert await rotate_rollback_tags(podman, service()) == ()
    assert podman.removed == []


async def test_rotation_ignores_tags_for_other_images() -> None:
    podman = FakePodman(tags=("localhost/rotv:rollback-x", "localhost/acquacotta:rollback-a"))
    removed = await rotate_rollback_tags(podman, service(), keep=0)
    assert removed == ("localhost/acquacotta:rollback-a",)


async def test_a_tag_that_will_not_go_does_not_fail_a_good_deploy() -> None:
    # The Nagios disk check is what notices accumulation; failing here would
    # turn a successful deploy into a reported failure.
    class Stubborn(FakePodman):
        async def remove_image(self, name: str, force: bool = False) -> None:
            del name, force
            raise PodmanError("image is in use")

    podman = Stubborn(tags=tuple(f"localhost/acquacotta:rollback-{n}" for n in range(4)))
    # Reports what actually went, which is nothing here, and does not raise.
    assert await rotate_rollback_tags(podman, service()) == ()
