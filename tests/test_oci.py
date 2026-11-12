"""Registry reader tests.

The ones that matter: a multi-arch index is resolved to the host's platform
rather than to whichever entry is first, and the anonymous token exchange sends
no credential and is not repeated for every request.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest

from deployer.oci import MAX_DOCUMENT_BYTES, OciError, RegistryReader, parse_created
from deployer.podman import REVISION_LABEL

IMAGE = "quay.io/crunchtools/mcp-nagios"
BASE = "https://quay.io/v2/crunchtools/mcp-nagios"
CONFIG = "sha256:" + "c" * 64
AMD = "sha256:" + "a" * 64
ARM = "sha256:" + "b" * 64
REV = "53c306b7f17fbd16942ab29b15d0a1d7f727e3f9"
CREATED = "2026-11-11T13:49:04.689795245Z"
REALM = "https://quay.io/v2/auth"
DOCKER_MANIFEST = "application/vnd.docker.distribution.manifest.v2+json"
OCI_INDEX = "application/vnd.oci.image.index.v1+json"
STAMP = datetime(2026, 11, 11, 1, 31, 38, tzinfo=UTC).timestamp()


def manifest(config: str = CONFIG) -> dict[str, Any]:
    return {"mediaType": DOCKER_MANIFEST, "config": {"digest": config}}


def blob(labels: dict[str, str] | None = None, created: object = CREATED) -> dict[str, Any]:
    return {"created": created, "config": {"Labels": {REVISION_LABEL: REV} if labels is None else labels}}


def index(*platforms: tuple[str, str, str]) -> dict[str, Any]:
    return {
        "mediaType": OCI_INDEX,
        "manifests": [{"digest": d, "platform": {"os": o, "architecture": a}} for o, a, d in platforms],
    }


Handler = Callable[[httpx.Request], httpx.Response]


def serving(docs: dict[str, object]) -> tuple[Handler, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        doc = docs.get(str(request.url).split("?")[0])
        if doc is None:
            return httpx.Response(404)
        if isinstance(doc, httpx.Response):
            return doc
        return httpx.Response(200, json=doc)

    return handler, seen


def reader(handler: Handler) -> RegistryReader:
    return RegistryReader(httpx.AsyncClient(transport=httpx.MockTransport(handler)))


async def test_a_single_manifest_gives_id_revision_and_build_time() -> None:
    handler, _ = serving({f"{BASE}/manifests/latest": manifest(), f"{BASE}/blobs/{CONFIG}": blob()})
    async with reader(handler) as r:
        got = await r.image(IMAGE)
    assert (got.image_id, got.revision, got.reference) == (CONFIG, REV, f"{IMAGE}:latest")
    assert got.created == parse_created("2026-11-11T13:49:04.689795Z")


async def test_an_index_is_resolved_to_the_hosts_platform_not_the_first_entry() -> None:
    handler, _ = serving(
        {
            f"{BASE}/manifests/latest": index(("linux", "arm64", ARM), ("linux", "amd64", AMD)),
            f"{BASE}/manifests/{AMD}": manifest(),
            f"{BASE}/manifests/{ARM}": manifest("sha256:" + "d" * 64),
            f"{BASE}/blobs/{CONFIG}": blob(),
        }
    )
    async with reader(handler) as r:
        assert (await r.image(IMAGE)).image_id == CONFIG


@pytest.mark.parametrize(
    "entries",
    [
        [{"digest": ARM, "platform": {"os": "linux", "architecture": "arm64"}}],
        [{"digest": "not-a-digest", "platform": {"os": "linux", "architecture": "amd64"}}],
        [{"digest": AMD}, "junk"],
    ],
)
async def test_an_index_without_this_platform_is_an_error(entries: list[object]) -> None:
    handler, _ = serving({f"{BASE}/manifests/latest": {"mediaType": OCI_INDEX, "manifests": entries}})
    async with reader(handler) as r:
        with pytest.raises(OciError, match="no linux/amd64 manifest"):
            await r.image(IMAGE)


async def test_a_bearer_challenge_is_answered_anonymously_and_cached() -> None:
    challenge = httpx.Response(
        401, headers={"WWW-Authenticate": f'Bearer realm="{REALM}",service="quay.io",scope="x"'}
    )
    calls: list[httpx.Request] = []

    docs: dict[str, Any] = {f"{BASE}/manifests/latest": manifest(), f"{BASE}/blobs/{CONFIG}": blob()}

    def answer(request: httpx.Request) -> httpx.Response:
        url = str(request.url).split("?")[0]
        if url == REALM:
            assert request.url.params["scope"] == "repository:crunchtools/mcp-nagios:pull"
            return httpx.Response(200, json={"token": "anon"})
        authorised = request.headers.get("Authorization") == "Bearer anon"
        return httpx.Response(200, json=docs[url]) if authorised else challenge

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return answer(request)

    async with reader(handler) as r:
        assert (await r.image(IMAGE)).revision == REV
    assert sum(1 for c in calls if str(c.url).startswith(REALM)) == 1
    assert all("Basic" not in c.headers.get("Authorization", "") for c in calls)


@pytest.mark.parametrize(
    ("challenge", "token_answer", "fragment"),
    [
        ('Basic realm="x"', None, "not an anonymous token"),
        ('Bearer realm="http://plain/auth"', None, "not an anonymous token"),
        (f'Bearer realm="{REALM}"', httpx.Response(500), "token endpoint answered HTTP 500"),
        (f'Bearer realm="{REALM}"', httpx.Response(200, text="nope"), "did not answer JSON"),
        (f'Bearer realm="{REALM}"', httpx.Response(200, json={"other": 1}), "gave no token"),
        (f'Bearer realm="{REALM}"', httpx.Response(200, json=["x"]), "gave no token"),
    ],
)
async def test_a_token_exchange_that_fails_says_why(
    challenge: str, token_answer: httpx.Response | None, fragment: str
) -> None:
    refused = httpx.Response(401, headers={"WWW-Authenticate": challenge})

    def handler(request: httpx.Request) -> httpx.Response:
        at_realm = str(request.url).startswith(REALM)
        return token_answer if at_realm and token_answer is not None else refused

    async with reader(handler) as r:
        with pytest.raises(OciError, match=fragment):
            await r.image(IMAGE)


async def test_an_unreachable_token_endpoint_is_an_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url).startswith(REALM):
            raise httpx.ConnectError("down")
        return httpx.Response(401, headers={"WWW-Authenticate": f'Bearer realm="{REALM}"'})

    async with reader(handler) as r:
        with pytest.raises(OciError, match="cannot reach the token endpoint"):
            await r.image(IMAGE)


async def test_an_unreachable_registry_is_an_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    async with reader(handler) as r:
        with pytest.raises(OciError, match="cannot reach the registry"):
            await r.image(IMAGE)


@pytest.mark.parametrize(
    ("answer", "fragment"),
    [
        (httpx.Response(404), "HTTP 404"),
        (httpx.Response(200, text="<html>"), "is not JSON"),
        (httpx.Response(200, json=["a"]), "not a JSON object"),
        (httpx.Response(200, content=b" " * (MAX_DOCUMENT_BYTES + 1)), "larger than"),
        (httpx.Response(200, json={"mediaType": DOCKER_MANIFEST}), "no sha256 config blob"),
        (httpx.Response(200, json=manifest("md5:x")), "no sha256 config blob"),
    ],
)
async def test_a_manifest_that_cannot_be_believed_is_an_error(answer: httpx.Response, fragment: str) -> None:
    handler, _ = serving({f"{BASE}/manifests/latest": answer})
    async with reader(handler) as r:
        with pytest.raises(OciError, match=fragment):
            await r.image(IMAGE)


@pytest.mark.parametrize(
    "config",
    [blob(labels={}), blob(labels={REVISION_LABEL: ""}), {"config": None}, {"config": {"Labels": None}}],
)
async def test_a_config_without_a_revision_reads_as_none(config: dict[str, Any]) -> None:
    handler, _ = serving({f"{BASE}/manifests/latest": manifest(), f"{BASE}/blobs/{CONFIG}": config})
    async with reader(handler) as r:
        assert (await r.image(IMAGE)).revision is None


@pytest.mark.parametrize(
    "reference", ["mcp-nagios", "quay.io/x:latest", "localhost/x", "quay.io/", "q.io/a@b"]
)
async def test_a_reference_that_is_not_registry_and_repository_is_refused(reference: str) -> None:
    handler, seen = serving({})
    async with reader(handler) as r:
        with pytest.raises(OciError, match="not a registry/repository reference"):
            await r.image(reference)
    assert seen == []


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("2026-11-11T01:31:38Z", STAMP),
        ("2026-11-11T01:31:38.123456789Z", STAMP + 0.123456),
        ("2026-11-11T03:31:38+02:00", STAMP),
        ("2026-11-11T01:31:38", None),
        ("", None),
        (None, None),
        (42, None),
        ("yesterday", None),
    ],
)
def test_parse_created(value: object, expected: float | None) -> None:
    assert parse_created(value) == expected


def test_the_documents_are_plain_json() -> None:
    # The fixtures above are what a registry sends; keep them serialisable.
    assert json.loads(json.dumps(index(("linux", "amd64", AMD))))["manifests"][0]["digest"] == AMD


async def test_a_token_realm_on_another_host_is_refused() -> None:
    seen: list[str] = []
    challenge = httpx.Response(
        401, headers={"WWW-Authenticate": 'Bearer realm="https://elsewhere.example/auth"'}
    )

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return challenge

    async with reader(handler) as r:
        with pytest.raises(OciError, match="is not on quay.io"):
            await r.image(IMAGE)
    assert not any("elsewhere" in url for url in seen)
