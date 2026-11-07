"""Podman client tests.

The first test is the one that matters most: it checks the class against the
declared surface, so a container lifecycle method cannot appear without someone
also editing ``ALLOWED_OPERATIONS``. That makes widening the boundary a visible
change rather than an accident.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from deployer.podman import (
    ALLOWED_OPERATIONS,
    SOCKET_ENV,
    REVISION_LABEL,
    VERSION_LABEL,
    PodmanError,
    PodmanImages,
    _split_reference,
)

SHA = "635cdda3c8632d1b5aed4cc22943895205ce5138"
FORBIDDEN_FRAGMENTS = ("create", "start", "run", "exec", "kill", "stop", "commit", "attach")


def client(handler: Any, **kwargs: Any) -> PodmanImages:
    """A client whose transport is a stub, so no socket is needed.

    The stub goes in through the constructor rather than over a private
    attribute, which is why PodmanImages takes a client at all.
    """
    return PodmanImages(
        client=httpx.AsyncClient(
            transport=httpx.MockTransport(handler), base_url="http://podman/v5.0.0/libpod"
        ),
        **kwargs,
    )


def test_the_public_surface_is_exactly_what_is_declared() -> None:
    methods = {
        name
        for name in dir(PodmanImages)
        if not name.startswith("_") and callable(getattr(PodmanImages, name, None))
    }
    assert methods == set(ALLOWED_OPERATIONS)


def test_no_method_hints_at_a_container_lifecycle() -> None:
    # The absence is the design: a process that cannot start a container cannot
    # mount a live volume, and so cannot relabel one out from under a service.
    for name in dir(PodmanImages):
        if name.startswith("_"):
            continue
        for fragment in FORBIDDEN_FRAGMENTS:
            assert fragment not in name, f"PodmanImages.{name} looks like a lifecycle verb"


@pytest.mark.parametrize(
    ("reference", "expected"),
    [
        ("quay.io/crunchtools/acquacotta:latest", ("quay.io/crunchtools/acquacotta", "latest")),
        ("quay.io/crunchtools/acquacotta", ("quay.io/crunchtools/acquacotta", "latest")),
        ("localhost/acquacotta:rollback-f0effff", ("localhost/acquacotta", "rollback-f0effff")),
        ("registry:5000/thing", ("registry:5000/thing", "latest")),
    ],
)
def test_a_reference_splits_into_repo_and_tag(reference: str, expected: tuple[str, str]) -> None:
    assert _split_reference(reference) == expected


async def test_pull_returns_the_image_id() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/images/pull")
        body = '{"stream":"pulling"}\n{"images":["abc123"],"id":"abc123"}\n'
        return httpx.Response(200, text=body)

    async with client(handler) as images:
        assert await images.pull("quay.io/crunchtools/acquacotta:latest") == "abc123"


async def test_a_pull_that_reports_no_id_is_an_error() -> None:
    # A pull that silently fetched nothing is indistinguishable from a good
    # deploy afterwards, so it must fail here.
    async with client(lambda _r: httpx.Response(200, text='{"stream":"..."}\n')) as images:
        with pytest.raises(PodmanError, match="no image id"):
            await images.pull("x:latest")


async def test_a_pull_error_in_the_stream_surfaces() -> None:
    body = '{"stream":"trying"}\n{"error":"manifest unknown"}\n'
    async with client(lambda _r: httpx.Response(200, text=body)) as images:
        with pytest.raises(PodmanError, match="manifest unknown"):
            await images.pull("x:nope")


async def test_inspect_image_reads_the_revision_and_version_labels() -> None:
    payload = {
        "Id": "sha256:deadbeef",
        "Digest": "sha256:feed",
        "RepoTags": ["quay.io/crunchtools/acquacotta:latest"],
        "Labels": {REVISION_LABEL: SHA, VERSION_LABEL: "2.34.0"},
    }
    async with client(lambda _r: httpx.Response(200, json=payload)) as images:
        ref = await images.inspect_image("quay.io/crunchtools/acquacotta:latest")
    assert ref.revision == SHA
    assert ref.version == "2.34.0"
    assert ref.digest == "sha256:feed"


async def test_an_image_with_no_labels_has_no_revision() -> None:
    async with client(lambda _r: httpx.Response(200, json={"Id": "x", "Labels": None})) as images:
        assert (await images.inspect_image("x")).revision is None


async def test_inspect_container_returns_the_object() -> None:
    payload = {"Image": "sha256:deadbeef", "State": {"Running": True}}
    async with client(lambda _r: httpx.Response(200, json=payload)) as images:
        assert (await images.inspect_container("acquacotta.crunchtools.com"))["Image"] == "sha256:deadbeef"


async def test_tag_and_untag_send_repo_and_tag_separately() -> None:
    seen: list[tuple[str, dict[str, str]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.url.path, dict(request.url.params)))
        return httpx.Response(201)

    async with client(handler) as images:
        await images.tag("sha256:abc", "localhost/acquacotta:rollback-f0effff")
        await images.untag("sha256:abc", "localhost/acquacotta:rollback-f0effff")

    assert seen[0][1] == {"repo": "localhost/acquacotta", "tag": "rollback-f0effff"}
    assert seen[0][0].endswith("/images/sha256:abc/tag")
    assert seen[1][0].endswith("/images/sha256:abc/untag")


async def test_list_images_filters_by_reference() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        filters = json.loads(request.url.params["filters"])
        assert filters == {"reference": ["localhost/acquacotta:rollback-*"]}
        return httpx.Response(200, json=[{"Id": "a", "Names": ["localhost/acquacotta:rollback-1"]}])

    async with client(handler) as images:
        found = await images.list_images("localhost/acquacotta:rollback-*")
    assert found[0].tags == ("localhost/acquacotta:rollback-1",)


async def test_a_non_list_answer_to_list_is_an_error() -> None:
    async with client(lambda _r: httpx.Response(200, json={"not": "a list"})) as images:
        with pytest.raises(PodmanError, match="not a list"):
            await images.list_images()


async def test_an_http_error_names_the_status_and_the_call() -> None:
    async with client(lambda _r: httpx.Response(404, text="no such image")) as images:
        with pytest.raises(PodmanError, match="404"):
            await images.inspect_image("nope")


async def test_an_unreachable_socket_names_the_socket() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    async with client(handler, socket_path="/run/nope.sock") as images:
        with pytest.raises(PodmanError, match="/run/nope.sock"):
            await images.list_images()


async def test_a_non_json_answer_is_an_error() -> None:
    async with client(lambda _r: httpx.Response(200, text="<html>")) as images:
        with pytest.raises(PodmanError, match="not JSON"):
            await images.inspect_image("x")


async def test_a_missing_socket_env_is_refused_rather_than_guessed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Guessing a socket path would mean acting on whatever engine was there.
    monkeypatch.delenv(SOCKET_ENV, raising=False)
    with pytest.raises(PodmanError, match=SOCKET_ENV):
        PodmanImages()


def test_the_socket_env_is_used_when_no_path_is_passed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(SOCKET_ENV, "/run/from-env.sock")
    images = PodmanImages()
    assert images.socket_path == "/run/from-env.sock"


@pytest.mark.parametrize(("force", "expected"), [(False, "false"), (True, "true")])
async def test_remove_image_hits_delete_and_passes_force(force: bool, expected: str) -> None:
    seen: list[tuple[str, str, dict[str, str]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path, dict(request.url.params)))
        return httpx.Response(200, json={})

    async with client(handler) as images:
        await images.remove_image("localhost/acquacotta:rollback-old", force=force)

    method, path, params = seen[0]
    assert method == "DELETE"
    assert path.endswith("/images/localhost/acquacotta:rollback-old")
    assert params["force"] == expected


async def test_remove_image_surfaces_an_http_error() -> None:
    async with client(lambda _r: httpx.Response(409, text="image is in use")) as images:
        with pytest.raises(PodmanError, match="409"):
            await images.remove_image("localhost/x:rollback-1")
