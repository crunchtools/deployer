"""Reading one image's identity out of an OCI registry, over HTTP, without pulling it.

``release-follow`` needs three facts about ``<image>:latest`` and nothing else:
the image id (the config blob's digest, which is what podman calls the image
id), the ``org.opencontainers.image.revision`` label, and when it was built. All
three live in the image's config blob, which is a few kilobytes; pulling the
image to read them would need a container engine, and the follower deliberately
holds none.

Read-only and anonymous. The fleet's images are public on Quay, which answers a
manifest read with no credential at all; a registry that demands a bearer token
gets the standard anonymous token exchange (the ``WWW-Authenticate`` challenge)
and nothing more. No credential is ever sent, so there is none to leak.

A multi-arch index is resolved to the one platform asked for, ``linux/amd64`` by
default, because that is what the host runs. Picking "the first entry" would
read the labels of whatever architecture the build happened to list first.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final, Self

import httpx

from .podman import REVISION_LABEL

__all__ = ["OciError", "RegistryReader", "RemoteImage", "parse_created"]

DEFAULT_TIMEOUT_SECONDS: Final = 30.0
#: A manifest or config blob larger than this is not one this program needs.
MAX_DOCUMENT_BYTES: Final = 4 * 1024 * 1024
DEFAULT_PLATFORM: Final = ("linux", "amd64")

_INDEX_TYPES: Final = frozenset(
    {
        "application/vnd.oci.image.index.v1+json",
        "application/vnd.docker.distribution.manifest.list.v2+json",
    }
)
_MANIFEST_TYPES: Final = frozenset(
    {
        "application/vnd.oci.image.manifest.v1+json",
        "application/vnd.docker.distribution.manifest.v2+json",
    }
)
_ACCEPT: Final = ", ".join(sorted(_INDEX_TYPES | _MANIFEST_TYPES))
_DIGEST_RE: Final = re.compile(r"\Asha256:[0-9a-f]{64}\Z")
_CHALLENGE_RE: Final = re.compile(r'(\w+)="([^"]*)"')
#: Registries write nanoseconds; ``datetime.fromisoformat`` reads at most six.
_FRACTION_RE: Final = re.compile(r"(\.\d{6})\d+")


class OciError(Exception):
    """The registry could not be read, or what it said could not be believed."""


@dataclass(frozen=True, slots=True)
class RemoteImage:
    """What the registry says one tag currently is."""

    reference: str
    #: The config blob's digest -- the same string podman reports as the image id.
    image_id: str
    revision: str | None
    #: The config's ``created``, as an epoch, or None if it carries none.
    created: float | None


def parse_created(value: object) -> float | None:
    """An OCI ``created`` timestamp as an epoch, or None if it is not one."""
    if not isinstance(value, str) or not value:
        return None
    text = _FRACTION_RE.sub(r"\1", value.strip()).replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    # RFC 3339 requires a zone. Without one the instant depends on this
    # process's clock zone, so it is no build time at all.
    return parsed.timestamp() if parsed.tzinfo is not None else None


class RegistryReader:
    """Anonymous, read-only access to manifests and config blobs."""

    def __init__(
        self,
        client: httpx.AsyncClient | None = None,
        platform: tuple[str, str] = DEFAULT_PLATFORM,
    ) -> None:
        """``client`` is the HTTP client to use, for tests; by default one is made.

        Either way the reader owns it and closes it when its ``async with``
        exits. ``platform`` is the ``(os, architecture)`` a multi-arch index is
        resolved to.
        """
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(DEFAULT_TIMEOUT_SECONDS), follow_redirects=True
        )
        self._platform = platform
        self._tokens: dict[str, str] = {}

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self._client.aclose()

    async def image(self, image: str, tag: str = "latest") -> RemoteImage:
        """What ``image:tag`` is in its registry right now."""
        host, sep, repo = image.partition("/")
        if not sep or "." not in host or not repo or ":" in repo or "@" in repo:
            raise OciError(f"{image!r} is not a registry/repository reference without a tag")
        base = f"https://{host}/v2/{repo}"
        manifest = await self._manifest(base, repo, tag)
        config = manifest.get("config")
        config_digest = config.get("digest") if isinstance(config, dict) else None
        if not isinstance(config_digest, str) or not _DIGEST_RE.match(config_digest):
            raise OciError(f"{image}:{tag}: the manifest names no sha256 config blob")
        blob = await self._json(f"{base}/blobs/{config_digest}", repo)
        inner = blob.get("config")
        labels = inner.get("Labels") if isinstance(inner, dict) else None
        revision = labels.get(REVISION_LABEL) if isinstance(labels, dict) else None
        return RemoteImage(
            reference=f"{image}:{tag}",
            image_id=config_digest,
            revision=revision if isinstance(revision, str) and revision else None,
            created=parse_created(blob.get("created")),
        )

    async def _manifest(self, base: str, repo: str, reference: str) -> dict[str, Any]:
        """The image manifest ``reference`` names, through an index if it is one.

        An index is resolved to this reader's platform, by digest, and that
        manifest is read in turn. Only one level: an index of indexes is not a
        shape a registry serves for an image.
        """
        manifest = await self._json(f"{base}/manifests/{reference}", repo, accept=_ACCEPT)
        if manifest.get("mediaType") not in _INDEX_TYPES and "manifests" not in manifest:
            return manifest
        entries = manifest.get("manifests")
        for entry in entries if isinstance(entries, list) else []:
            platform = entry.get("platform") if isinstance(entry, dict) else None
            if not isinstance(platform, dict):
                continue
            digest = entry.get("digest")
            wanted = (platform.get("os"), platform.get("architecture")) == self._platform
            if wanted and isinstance(digest, str) and _DIGEST_RE.match(digest):
                return await self._json(f"{base}/manifests/{digest}", repo, accept=_ACCEPT)
        raise OciError(f"{repo}:{reference}: the index has no {'/'.join(self._platform)} manifest")

    async def _json(self, url: str, repo: str, accept: str | None = None) -> dict[str, Any]:
        headers = {"Accept": accept} if accept else {}
        response = await self._get(url, headers, repo)
        if response.status_code == httpx.codes.UNAUTHORIZED:
            token = await self._token(response.headers.get("www-authenticate", ""), repo, httpx.URL(url).host)
            response = await self._get(url, {**headers, "Authorization": f"Bearer {token}"}, repo)
        if response.status_code != httpx.codes.OK:
            raise OciError(f"{url} answered HTTP {response.status_code}")
        if len(response.content) > MAX_DOCUMENT_BYTES:
            raise OciError(f"{url} is larger than {MAX_DOCUMENT_BYTES} bytes")
        try:
            parsed = json.loads(response.content)
        except ValueError as exc:
            raise OciError(f"{url} is not JSON: {exc}") from None
        if not isinstance(parsed, dict):
            raise OciError(f"{url} is not a JSON object")
        return parsed

    async def _get(self, url: str, headers: dict[str, str], repo: str) -> httpx.Response:
        try:
            return await self._client.get(url, headers=headers)
        except httpx.HTTPError as exc:
            raise OciError(f"cannot reach the registry for {repo}: {exc}") from None

    async def _token(self, challenge: str, repo: str, host: str) -> str:
        """The anonymous pull token a ``Bearer`` challenge offers. Cached per repo."""
        if repo in self._tokens:
            return self._tokens[repo]
        scheme, _, params = challenge.partition(" ")
        fields = dict(_CHALLENGE_RE.findall(params))
        realm = fields.get("realm", "")
        if scheme.lower() != "bearer" or not realm.startswith("https://"):
            raise OciError(f"{repo}: the registry asked for {scheme or 'nothing'}, not an anonymous token")
        if httpx.URL(realm).host != host:
            # The realm comes from the registry's answer. Followed anywhere, it
            # would let that answer point this host at another service.
            raise OciError(f"{repo}: the token realm {realm!r} is not on {host}")
        query = {"service": fields.get("service", ""), "scope": f"repository:{repo}:pull"}
        try:
            response = await self._client.get(realm, params=query)
        except httpx.HTTPError as exc:
            raise OciError(f"{repo}: cannot reach the token endpoint: {exc}") from None
        if response.status_code != httpx.codes.OK:
            raise OciError(f"{repo}: the token endpoint answered HTTP {response.status_code}")
        try:
            answer = response.json()
        except ValueError:
            raise OciError(f"{repo}: the token endpoint did not answer JSON") from None
        token = answer.get("token") or answer.get("access_token") if isinstance(answer, dict) else None
        if not isinstance(token, str) or not token:
            raise OciError(f"{repo}: the token endpoint gave no token")
        self._tokens[repo] = token
        return token
