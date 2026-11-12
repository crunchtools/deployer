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
from deployer.stage import Proof, StageError, Verdict, classify_revision, stage, tag_suffix, unit_shape_ok

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

    def __init__(
        self,
        running_image: str = OLD,
        pulled: str = NEW,
        labels: dict[str, str] | None = None,
        registry: dict[str, str] | None = None,
    ):
        self.tags: list[tuple[str, str]] = []
        self.untags: list[tuple[str, str]] = []
        self.pulls: list[str] = []
        #: Refs other than :latest the registry holds (gated-<sha>, main). A
        #: ref missing here fails to pull, as an absent tag does.
        self._registry = registry or {}
        #: What podman says the container was created from. The gate reads this
        #: rather than guessing which word of `podman run` is the image.
        self.reference = f"{IMAGE}:latest"
        self._running = running_image
        self._pulled = pulled
        # `labels or {...}` would treat an explicitly empty dict as "not given",
        # which is the case the no-revision test is trying to set up.
        self._labels = {OLD: OLD_REV, NEW: NEW_REV} if labels is None else labels

    async def inspect_container(self, name: str) -> dict[str, Any]:
        del name
        if not self._running:
            return {"ImageName": self.reference}
        return {"Image": self._running, "ImageName": self.reference}

    async def inspect_image(self, name: str) -> ImageRef:
        rev = self._labels.get(name)
        return ImageRef(id=name, digest=None, tags=(), labels={REVISION_LABEL: rev} if rev else {})

    async def pull(self, reference: str) -> str:
        self.pulls.append(reference)
        if reference.endswith(":latest"):
            return self._pulled
        if reference not in self._registry:
            raise PodmanError(f"{reference}: manifest unknown")
        return self._registry[reference]

    async def tag(self, name: str, reference: str) -> None:
        self.tags.append((name, reference))

    async def untag(self, name: str, reference: str) -> None:
        self.untags.append((name, reference))


def container(reference: str = f"{IMAGE}:latest") -> dict[str, Any]:
    """What podman reports for a running container, as the gate reads it."""
    return {"Image": OLD, "ImageName": reference}


async def test_a_well_shaped_unit_passes_the_gate() -> None:
    await unit_shape_ok(FakeSystemd(f"run -d --name x {IMAGE}:latest"), service(), container())


async def test_the_image_need_not_be_the_last_argument() -> None:
    # `podman run ... IMAGE CMD ARGS` is the ordinary shape for anything that
    # takes arguments of its own. mcp-pcloud's real unit ends `--port 8028`,
    # and an ends-with check refused it -- found on the real units, not here.
    await unit_shape_ok(
        FakeSystemd(f"run -d --name x {IMAGE}:latest --transport streamable-http --port 8028"),
        service(),
        container(),
    )


async def test_a_container_running_a_pinned_tag_is_refused() -> None:
    # The question the gate exists to answer: would moving :latest change what
    # this runs. podman answers it directly.
    with pytest.raises(StageError, match="is running"):
        await unit_shape_ok(FakeSystemd(f"run -d {IMAGE}:6.8.0"), service(), container(f"{IMAGE}:6.8.0"))


async def test_a_unit_that_merely_mentions_latest_is_refused() -> None:
    # The hole in looking for the reference anywhere in the command line: a
    # unit that runs something else and passes ours as an option value.
    with pytest.raises(StageError, match="is running"):
        await unit_shape_ok(
            FakeSystemd(f"run -d -e MIRROR={IMAGE}:latest localhost/other:1.0"),
            service(),
            container("localhost/other:1.0"),
        )


async def test_a_unit_changed_since_the_container_started_is_refused() -> None:
    # The container answers for the present. A unit edited since does not, and
    # the next restart would start something else.
    with pytest.raises(StageError, match="does not name it"):
        await unit_shape_ok(FakeSystemd(f"run -d {IMAGE}:6.8.0"), service(), container())


async def test_a_unit_naming_the_same_image_twice_is_refused() -> None:
    with pytest.raises(StageError, match="names both"):
        await unit_shape_ok(
            FakeSystemd(f"run -d --name x {IMAGE}:latest --fallback {IMAGE}:6.8.0"),
            service(),
            container(),
        )


async def test_a_container_with_no_image_name_is_refused() -> None:
    with pytest.raises(StageError, match="an unnamed image"):
        await unit_shape_ok(FakeSystemd(f"run -d {IMAGE}:latest"), service(), {"Image": OLD})


async def test_a_unit_that_pulls_on_start_is_refused() -> None:
    systemd = FakeSystemd(f"run -d {IMAGE}:latest", [f"podman pull {IMAGE}:latest"])
    with pytest.raises(StageError, match="pulls on start"):
        await unit_shape_ok(systemd, service(), container())


async def test_an_unreadable_exec_start_is_refused_not_assumed_fine() -> None:
    with pytest.raises(StageError, match="cannot read ExecStart"):
        await unit_shape_ok(FakeSystemd(None), service(), container())


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
    assert classify_revision(revision, NEW_REV, age, service(), proof=Proof.PROMOTED) is expected


@pytest.mark.parametrize(("age", "expected"), [(10, Verdict.WAITING), (BUILD_DEADLINE, Verdict.UNPROMOTED)])
def test_a_matching_label_without_promotion_is_never_a_match(age: int, expected: Verdict) -> None:
    # The label says which code, not that main or a release carried it.
    assert classify_revision(NEW_REV, NEW_REV, age, service()) is expected


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


# Build once per PR (constitution container-release, RT#1550): the label names
# the PR head; gated-<merge sha> equal to :main says main promoted that commit.
PR_HEAD = "9999888877776666555544443333222211110000"
GATED = f"{IMAGE}:gated-{NEW_REV}"
MAIN = f"{IMAGE}:main"


async def test_a_gated_digest_main_promoted_is_a_match_whatever_its_label() -> None:
    podman = FakePodman(labels={OLD: OLD_REV, NEW: PR_HEAD}, registry={GATED: NEW, MAIN: NEW})
    staged = await stage(podman, FakeSystemd(f"run -d {IMAGE}:latest"), service(), NEW_REV)
    assert staged.candidate_gated and staged.candidate_image_id == NEW
    assert (
        classify_revision(staged.candidate_revision, NEW_REV, 10, service(), proof=staged.proof)
        is Verdict.MATCH
    )
    assert f"{IMAGE}:latest" not in podman.pulls
    assert all(ref != f"{IMAGE}:latest" for _, ref in podman.tags)
    assert set(podman.untags) == {(NEW, GATED), (NEW, MAIN)}


@pytest.mark.parametrize(("age", "expected"), [(10, Verdict.WAITING), (BUILD_DEADLINE, Verdict.STALE)])
async def test_a_gated_digest_main_does_not_hold_is_not_deployed(age: int, expected: Verdict) -> None:
    """A gated tag alone also exists for a PR head that never merged."""
    podman = FakePodman(pulled=OLD, labels={OLD: OLD_REV, NEW: PR_HEAD}, registry={GATED: NEW, MAIN: OLD})
    staged = await stage(podman, FakeSystemd(f"run -d {IMAGE}:latest"), service(), NEW_REV)
    assert not staged.candidate_gated
    verdict = classify_revision(staged.candidate_revision, NEW_REV, age, service(), proof=staged.proof)
    assert verdict is expected
    assert latest_ends_on(podman) == OLD
    assert set(podman.untags) == {(NEW, GATED), (OLD, MAIN)}


def latest_ends_on(podman: FakePodman) -> str | None:
    """Where the local :latest was last tagged, or None if staging never moved it."""
    moves = [name for name, ref in podman.tags if ref == f"{IMAGE}:latest"]
    return moves[-1] if moves else None


async def test_a_gated_digest_a_release_promoted_is_a_match_after_main_moved_on() -> None:
    """release-follow's path: the ticket names the :latest revision, main is ahead."""
    podman = FakePodman(
        pulled=NEW, labels={OLD: OLD_REV, NEW: PR_HEAD}, registry={GATED: NEW, MAIN: "sha256:2222main"}
    )
    staged = await stage(podman, FakeSystemd(f"run -d {IMAGE}:latest"), service(), NEW_REV)
    assert staged.candidate_gated and staged.candidate_image_id == NEW
    assert podman.pulls == [GATED, MAIN, f"{IMAGE}:latest"]
    # :latest moved with the pull and was put straight back.
    assert latest_ends_on(podman) == OLD


async def test_latest_is_not_asked_when_main_already_promoted() -> None:
    podman = FakePodman(labels={OLD: OLD_REV, NEW: PR_HEAD}, registry={GATED: NEW, MAIN: NEW})
    await stage(podman, FakeSystemd(f"run -d {IMAGE}:latest"), service(), NEW_REV)
    assert f"{IMAGE}:latest" not in podman.pulls
    assert latest_ends_on(podman) is None


async def test_an_absent_latest_is_not_a_promotion() -> None:
    class NoLatest(FakePodman):
        async def pull(self, reference: str) -> str:
            if reference.endswith(":latest"):
                self.pulls.append(reference)
                raise PodmanError(f"{reference}: manifest unknown")
            return await super().pull(reference)

    podman = NoLatest(labels={OLD: OLD_REV, NEW: PR_HEAD}, registry={GATED: NEW, MAIN: OLD})
    staged = await stage(podman, FakeSystemd(f"run -d {IMAGE}:latest"), service(), NEW_REV)
    assert not staged.candidate_gated
    assert latest_ends_on(podman) == OLD


async def test_a_latest_pull_that_fails_stops_staging_and_restores_everything() -> None:
    class LatestUnauthorized(FakePodman):
        async def pull(self, reference: str) -> str:
            if reference.endswith(":latest"):
                raise PodmanError("unauthorized: access to the requested resource is not authorized")
            return await super().pull(reference)

    podman = LatestUnauthorized(labels={OLD: OLD_REV, NEW: PR_HEAD}, registry={GATED: NEW, MAIN: OLD})
    with pytest.raises(StageError, match="could not pull .*:latest"):
        await stage(podman, FakeSystemd(f"run -d {IMAGE}:latest"), service(), NEW_REV)
    assert latest_ends_on(podman) == OLD
    assert set(podman.untags) == {(NEW, GATED), (OLD, MAIN)}


async def test_without_a_gated_tag_staging_takes_the_latest_path() -> None:
    podman = FakePodman()
    staged = await stage(podman, FakeSystemd(f"run -d {IMAGE}:latest"), service(), NEW_REV)
    assert not staged.candidate_gated
    # The candidate IS the registry's :latest: a release promoted it.
    assert staged.candidate_promoted
    assert podman.pulls == [GATED, f"{IMAGE}:latest"]
    assert podman.tags[-1] == (OLD, f"{IMAGE}:latest")
    assert not podman.untags


async def test_the_pulled_refs_are_untagged_when_tagging_the_candidate_fails() -> None:
    class FailsToTag(FakePodman):
        async def tag(self, name: str, reference: str) -> None:
            if "candidate-" in reference:
                raise PodmanError("no space left on device")
            await super().tag(name, reference)

    podman = FailsToTag(labels={OLD: OLD_REV, NEW: PR_HEAD}, registry={GATED: NEW, MAIN: NEW})
    with pytest.raises(PodmanError, match="no space left"):
        await stage(podman, FakeSystemd(f"run -d {IMAGE}:latest"), service(), NEW_REV)
    assert set(podman.untags) == {(NEW, GATED), (NEW, MAIN)}
    assert all(ref != f"{IMAGE}:latest" for _, ref in podman.tags)


async def test_a_missing_main_still_untags_the_gated_ref_and_is_not_a_match() -> None:
    podman = FakePodman(pulled=OLD, labels={OLD: OLD_REV, NEW: PR_HEAD}, registry={GATED: NEW})
    staged = await stage(podman, FakeSystemd(f"run -d {IMAGE}:latest"), service(), NEW_REV)
    assert not staged.candidate_gated
    assert podman.untags == [(NEW, GATED)]


async def test_a_pull_failure_that_is_not_an_absent_tag_stops_staging() -> None:
    """Auth or network trouble must not read as "no gated tag" and fall back."""

    class Unauthorized(FakePodman):
        async def pull(self, reference: str) -> str:
            if ":gated-" in reference:
                raise PodmanError("unauthorized: access to the requested resource is not authorized")
            return await super().pull(reference)

    podman = Unauthorized()
    with pytest.raises(StageError, match="could not pull .*unauthorized"):
        await stage(podman, FakeSystemd(f"run -d {IMAGE}:latest"), service(), NEW_REV)
    assert f"{IMAGE}:latest" not in podman.pulls


# A ticket naming a pull request's HEAD commit: gated-<head> exists and its
# label matches whether or not the pull request ever merged, so only :main or
# :latest holding that digest may make it deployable.
HEAD_GATED = f"{IMAGE}:gated-{PR_HEAD}"
HEAD_LABELS = {OLD: OLD_REV, NEW: PR_HEAD}


def head_verdict(staged: Any, age: int = 10) -> Verdict:
    return classify_revision(
        staged.candidate_revision,
        PR_HEAD,
        age,
        service(),
        proof=staged.proof,
    )


@pytest.mark.parametrize(("age", "expected"), [(10, Verdict.WAITING), (BUILD_DEADLINE, Verdict.UNPROMOTED)])
async def test_an_unmerged_pull_request_head_is_not_deployable(age: int, expected: Verdict) -> None:
    podman = FakePodman(pulled=OLD, labels=HEAD_LABELS, registry={HEAD_GATED: NEW, MAIN: OLD})
    staged = await stage(podman, FakeSystemd(f"run -d {IMAGE}:latest"), service(), PR_HEAD)
    assert staged.candidate_revision == PR_HEAD  # the label matches the ticket ...
    assert not staged.candidate_promoted  # ... but nothing promoted it
    assert head_verdict(staged, age) is expected
    assert latest_ends_on(podman) == OLD
    assert set(podman.untags) == {(NEW, HEAD_GATED), (OLD, MAIN)}


async def test_a_merged_head_main_holds_is_a_match() -> None:
    podman = FakePodman(labels=HEAD_LABELS, registry={HEAD_GATED: NEW, MAIN: NEW})
    staged = await stage(podman, FakeSystemd(f"run -d {IMAGE}:latest"), service(), PR_HEAD)
    assert staged.candidate_promoted
    assert head_verdict(staged) is Verdict.MATCH
    assert latest_ends_on(podman) is None


async def test_a_released_head_with_merges_on_top_is_a_match() -> None:
    podman = FakePodman(pulled=NEW, labels=HEAD_LABELS, registry={HEAD_GATED: NEW, MAIN: "sha256:2222main"})
    staged = await stage(podman, FakeSystemd(f"run -d {IMAGE}:latest"), service(), PR_HEAD)
    assert staged.candidate_promoted
    assert head_verdict(staged) is Verdict.MATCH
    assert latest_ends_on(podman) == OLD
