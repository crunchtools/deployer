"""The podman surface, and nothing beyond it.

This module is the capability boundary made real. It speaks to the podman
Libpod REST API over its unix socket, and it offers exactly eight operations:
pull an image, inspect an image, inspect a container, tag, untag, remove an
image, list images, and read a container's logs.

There is no create, start, run or exec here, and that absence is the design
rather than an oversight. A process that can start a container can mount a
live volume, and a container that mounts a live volume with a relabelling flag
takes that volume away from the service that owns it. The only durable way for
a deploy robot not to make that mistake is for it not to hold the capability,
so the method list below is the whole of what this program can do to a
container engine.

``ALLOWED_OPERATIONS`` names the surface, and a test checks the class against
it. Adding a method without adding it there fails that test; adding it to both
is a visible change to the boundary, which is the point.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from types import TracebackType
from typing import Any, Final, Self

import httpx

__all__ = ["ALLOWED_OPERATIONS", "ImageRef", "PodmanError", "PodmanImages"]

API_VERSION: Final = "v5.0.0"
#: Where the podman socket is mounted is a property of the deployment, so it
#: comes from the environment and the unit supplies it. No built-in path: a
#: guessed one would mean acting on whatever engine happened to be there.
SOCKET_ENV: Final = "PODMAN_SOCKET"
DEFAULT_TIMEOUT_SECONDS: Final = 30.0
# A pull crosses the network and can legitimately take minutes.
PULL_TIMEOUT_SECONDS: Final = 600.0
# A response larger than this is not something this program needs to read.
MAX_RESPONSE_BYTES: Final = 10 * 1024 * 1024

#: How many log lines go on a ticket. Enough to show a traceback, little
#: enough that the comment is still readable.
DEFAULT_LOG_LINES: Final = 40
#: A log frame header is 8 bytes: one stream byte, three reserved, four of
#: big-endian length.
_FRAME_HEADER_BYTES: Final = 8
_STREAM_BYTES: Final = frozenset({0, 1, 2})

REVISION_LABEL: Final = "org.opencontainers.image.revision"
VERSION_LABEL: Final = "org.opencontainers.image.version"

#: Every callable this class offers. Read-only properties are not operations
#: and are not listed; ``tests/test_podman.py`` checks the methods against this.
ALLOWED_OPERATIONS: Final = frozenset(
    {
        "pull",
        "inspect_image",
        "inspect_container",
        "tag",
        "untag",
        "remove_image",
        "list_images",
        "logs",
        "close",
    }
)


class PodmanError(Exception):
    """A podman call failed, or its answer could not be believed."""


@dataclass(frozen=True, slots=True)
class ImageRef:
    """What this program needs to know about one image."""

    id: str
    digest: str | None
    tags: tuple[str, ...]
    labels: dict[str, str]

    @property
    def revision(self) -> str | None:
        """The commit the image was built from, per its OCI label.

        This is the only thing that ties an image to a commit a ticket named,
        and only a build out of the pipeline sets it.
        """
        return self.labels.get(REVISION_LABEL)

    @property
    def version(self) -> str | None:
        """The image's OCI version label, or ``None`` when it carries none.

        Recorded on the ticket so a person reading it later can see which
        release went out, not only which commit.
        """
        return self.labels.get(VERSION_LABEL)


def _split_reference(reference: str) -> tuple[str, str]:
    """``repo:tag`` into its parts, defaulting the tag to ``latest``.

    The rightmost colon only counts as a tag separator when it is after the
    last slash -- otherwise ``registry:5000/thing`` would be read as a tag.
    """
    head, sep, tail = reference.rpartition(":")
    if sep and "/" not in tail:
        return head, tail
    return reference, "latest"


class PodmanImages:
    """The seven operations, over the podman socket.

    Named for what it is allowed to touch. There is deliberately no
    ``PodmanContainers`` with a lifecycle on it.
    """

    def __init__(
        self,
        socket_path: str | None = None,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        """``client`` is for tests; in production the socket path is used.

        Taking the client as an argument is what keeps the tests from reaching
        into a private attribute to stub the transport.
        """
        if client is not None:
            self._socket_path = socket_path or "<injected>"
            self._client = client
            return
        resolved = socket_path or os.environ.get(SOCKET_ENV)
        if not resolved:
            raise PodmanError(f"no podman socket: set ${SOCKET_ENV} or pass socket_path")
        self._socket_path = resolved
        self._client = httpx.AsyncClient(
            transport=httpx.AsyncHTTPTransport(uds=resolved),
            base_url=f"http://podman/{API_VERSION}/libpod",
            timeout=httpx.Timeout(timeout),
        )

    @property
    def socket_path(self) -> str:
        """Where this client is talking. Read-only state, not an operation."""
        return self._socket_path

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.close()

    async def close(self) -> None:
        """Close the HTTP client and its unix-socket transport."""
        await self._client.aclose()

    async def _request(
        self,
        method: str,
        path: str,
        params: dict[str, Any] | None = None,
        timeout: float | None = None,
    ) -> httpx.Response:
        try:
            response = await self._client.request(
                method, path, params=params, timeout=timeout or httpx.USE_CLIENT_DEFAULT
            )
        except httpx.HTTPError as exc:
            raise PodmanError(f"{method} {path}: cannot reach podman at {self._socket_path}: {exc}") from None

        if response.status_code >= httpx.codes.BAD_REQUEST:
            detail = response.text[:300].strip()
            raise PodmanError(f"{method} {path}: podman returned {response.status_code}: {detail}")
        if len(response.content) > MAX_RESPONSE_BYTES:
            raise PodmanError(f"{method} {path}: response larger than {MAX_RESPONSE_BYTES} bytes")
        return response

    async def _json(self, method: str, path: str, params: dict[str, Any] | None = None) -> Any:
        response = await self._request(method, path, params)
        try:
            return response.json()
        except json.JSONDecodeError as exc:
            raise PodmanError(f"{method} {path}: podman's answer was not JSON: {exc}") from None

    # --- the eight ---

    async def pull(self, reference: str) -> str:
        """Pull ``reference`` and return the image id it resolved to.

        The pull stream is newline-delimited JSON ending in an object carrying
        the id. A stream that ends without one is an error, not an empty
        success: a pull that silently fetched nothing looks exactly like a
        successful deploy afterwards.
        """
        response = await self._request(
            "POST", "/images/pull", {"reference": reference}, timeout=PULL_TIMEOUT_SECONDS
        )
        image_id: str | None = None
        error: str | None = None
        for line in response.text.splitlines():
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(event, dict):
                error = event.get("error") or error
                ids = event.get("images") or []
                if isinstance(ids, list) and ids:
                    image_id = str(ids[-1])
                image_id = str(event.get("id") or "") or image_id
        if error:
            raise PodmanError(f"pull {reference}: {error}")
        if not image_id:
            raise PodmanError(f"pull {reference}: podman reported no image id")
        return image_id

    async def inspect_image(self, name: str) -> ImageRef:
        """Inspect an image by name, tag or id."""
        inspected = self._require_dict(
            await self._json("GET", f"/images/{name}/json"), f"inspect image {name}"
        )
        labels = inspected.get("Labels") or {}
        if not isinstance(labels, dict):
            labels = {}
        return ImageRef(
            id=str(inspected.get("Id") or ""),
            digest=(str(inspected["Digest"]) if inspected.get("Digest") else None),
            tags=tuple(str(tag) for tag in (inspected.get("RepoTags") or [])),
            labels={str(key): str(value) for key, value in labels.items()},
        )

    async def inspect_container(self, name: str) -> dict[str, Any]:
        """Inspect a container by name or id.

        Read-only. This is how the image a service is *running now* is found,
        which is not the same thing as whatever the local ``:latest`` points
        at -- and that gap is exactly how a surprise gets deployed.
        """
        return self._require_dict(await self._json("GET", f"/containers/{name}/json"), f"inspect {name}")

    async def tag(self, name: str, reference: str) -> None:
        """Add ``reference`` as a tag on the image ``name``."""
        repo, image_tag = _split_reference(reference)
        await self._request("POST", f"/images/{name}/tag", {"repo": repo, "tag": image_tag})

    async def untag(self, name: str, reference: str) -> None:
        """Remove the tag ``reference`` from the image ``name``."""
        repo, image_tag = _split_reference(reference)
        await self._request("POST", f"/images/{name}/untag", {"repo": repo, "tag": image_tag})

    async def remove_image(self, name: str, force: bool = False) -> None:
        """Remove an image. Used only to rotate old rollback tags."""
        await self._request("DELETE", f"/images/{name}", {"force": force})

    async def list_images(self, reference: str | None = None) -> tuple[ImageRef, ...]:
        """List images, optionally filtered by a reference glob."""
        params: dict[str, Any] = {}
        if reference:
            params["filters"] = json.dumps({"reference": [reference]})
        listed = await self._json("GET", "/images/json", params or None)
        if not isinstance(listed, list):
            raise PodmanError("list images: podman's answer was not a list")
        found: list[ImageRef] = []
        for entry in listed:
            if not isinstance(entry, dict):
                continue
            labels = entry.get("Labels") or {}
            if not isinstance(labels, dict):
                labels = {}
            found.append(
                ImageRef(
                    id=str(entry.get("Id") or ""),
                    digest=(str(entry["Digest"]) if entry.get("Digest") else None),
                    tags=tuple(str(tag) for tag in (entry.get("Names") or entry.get("RepoTags") or [])),
                    labels={str(key): str(value) for key, value in labels.items()},
                )
            )
        return tuple(found)

    async def logs(
        self,
        name: str,
        tail: int = DEFAULT_LOG_LINES,
        since: float | None = None,
        strict: bool = False,
    ) -> str:
        """The last ``tail`` lines a container has written. Read-only.

        ``since`` is a Unix timestamp and the endpoint drops everything older.
        It is what makes this usable as a readiness signal: a unit that
        ``podman start``s an *existing* container keeps the previous run's
        output, so a startup line from the last run would otherwise satisfy a
        check about this one. That is the same mistake as reading a restart as
        complete the moment it was asked for -- the event is in the log, just
        not the event being asked about. Measured against the live libpod
        endpoint: ``since`` in the past returns lines, ``since`` in the future
        returns nothing.

        ``strict`` re-raises instead of swallowing. A caller writing a comment
        wants the tail or nothing; a caller *deciding* on readiness must be
        able to tell "the log does not contain the line" from "the log could
        not be read", because those two want different words on the ticket and
        only one of them is the service's fault.

        Here because a developer reading a stalled ticket needs to see what the
        service said as it failed, and "the health probe returned 502" is not
        that. The unit's journal would say less: a container that starts and
        then answers wrongly is a perfectly healthy unit.

        Reading logs is a GET. It adds nothing to what this program can *do* to
        a container -- there is still no create, start, run or exec -- which is
        why it sits inside the boundary rather than breaking it.

        Never raises. A comment explaining a failed deploy is worth sending
        without its log tail; losing the comment because the log fetch failed
        would be the wrong trade.
        """
        params = {"stdout": "true", "stderr": "true", "tail": str(max(1, tail))}
        if since is not None:
            # Whole seconds: the endpoint takes a Unix timestamp, and a
            # fractional one is not worth the risk of it being parsed as zero.
            params["since"] = str(int(since))
        try:
            response = await self._request("GET", f"/containers/{name}/logs", params)
        except PodmanError as exc:
            if strict:
                raise
            return f"[no logs: {exc}]"
        return _demultiplex(response.content)

    @staticmethod
    def _require_dict(value: Any, where: str) -> dict[str, Any]:
        if not isinstance(value, dict):
            raise PodmanError(f"{where}: expected an object, got {type(value).__name__}")
        return value


def _demultiplex(raw: bytes) -> str:
    """Docker's multiplexed log stream into plain text.

    A container without a TTY has its stdout and stderr interleaved as frames:
    one stream byte, three reserved, four bytes of big-endian length, then the
    payload. A container WITH a TTY has no frames at all and the body is the
    text itself.

    Which of the two arrives is a property of how the container was created, so
    this reads either: a first byte that is not a stream number, or a declared
    length that runs past the end of the data, means the bytes are not frames
    and the whole thing is decoded as text. Guessing wrong the other way would
    print eight bytes of binary header in front of every line.
    """
    if not raw:
        return ""
    if raw[0] not in _STREAM_BYTES:
        return raw.decode("utf-8", errors="replace")

    parts: list[str] = []
    offset = 0
    while offset + _FRAME_HEADER_BYTES <= len(raw):
        if raw[offset] not in _STREAM_BYTES:
            return raw.decode("utf-8", errors="replace")
        length = int.from_bytes(raw[offset + 4 : offset + _FRAME_HEADER_BYTES], "big")
        start = offset + _FRAME_HEADER_BYTES
        if start + length > len(raw):
            return raw.decode("utf-8", errors="replace")
        parts.append(raw[start : start + length].decode("utf-8", errors="replace"))
        offset = start + length
    return "".join(parts)
