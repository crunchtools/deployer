"""Request Tracker, reached directly.

Not through the Trentina gateway, and that is a design constraint rather than a
convenience. The gateway's judging layer is a language model, and this program
is the half of the design with no model in it; routing its ticket updates
through one would put a model back in the deploy path. It would also make the
gateway permanently undeployable by this tool, since restarting the gateway
would kill the call carrying the deploy.

This speaks RT's REST 1.0 API with basic auth, which is what works against this
RT today with credentials that already exist. REST 2 would be the nicer API and
is available in RT 6, but it needs a token this deployment does not have
(``has_rest_token: false``), and a new long-lived credential on the host buys no
capability this program needs: it reads a queue, reads a ticket, comments, and
sets status and owner.

A ticket's **body is its first attachment**, not a field of ``ticket/show``,
which carries none. Reading it costs two more calls per ticket and is worth it:
the alternative is parsing the ticket's whole history, which means reading
everything anyone has ever written on it.

Comments are written **only when something changed**. A ticket waiting for a
build is looked at every two minutes, and a robot that said "still waiting" 720
times a day would make the ticket useless to the person who eventually reads it.
What has already been said lives in the state directory, not in the ticket, so
deciding whether to speak never means parsing RT's history.
"""

from __future__ import annotations

import re
import time
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Final, Self
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx

from .statefile import safe_component, write_whole

__all__ = ["RtClient", "RtCredentials", "RtError", "Ticket", "TicketSaid"]

DEFAULT_TIMEOUT_SECONDS: Final = 30.0
#: RT answers REST 1.0 with a status line before the payload.
_STATUS_RE: Final = re.compile(r"\ART/\S+\s+(\d{3})\s*(.*)")
_OPEN_STATES: Final = ("new", "open", "stalled")
#: RT 6 renders dates as ``Fri Nov 06 04:00:46 YYYY``, with no zone in the
#: string at all. The zone is RT's ``$Timezone`` setting, which is why it has
#: to be configured here rather than guessed: this RT is on
#: ``America/New_York`` (RT_Config.pm) while a container's own clock is UTC, so
#: reading the string as local time would be five hours out -- enough to call a
#: fresh ticket stale, or to wait forever on an old one.
_CREATED_FORMAT: Final = "%a %b %d %H:%M:%S %Y"
#: The first id in RT's ``Attachments: 13583: (Unnamed) (text/plain / 113b),``
#: listing. First, because that one is the body.
_ATTACHMENT_ID: Final = re.compile(r"(\d+):")
#: RT's confirmation line after a create: ``# Ticket 1551 created.``
_CREATED_ID: Final = re.compile(r"#\s*Ticket\s+(\d+)\s+created", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class RtCredentials:
    """Where RT is and who this program is to it.

    One object rather than five arguments, and the thing the unit's env file
    maps onto. ``http_user``/``http_password`` are the auth proxy in front of
    RT, which is a separate credential from the RT login itself.
    """

    base_url: str
    username: str
    password: str
    timezone: str
    http_user: str = ""
    http_password: str = ""

    def zone(self) -> ZoneInfo:
        """``timezone`` as a zone, refusing a name the system does not know.

        Checked here, at load, rather than at the first date: a typo in an env
        file should stop the run with the name in the message, not surface
        twenty minutes later as an unreadable ticket.
        """
        try:
            return ZoneInfo(self.timezone)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise RtError(
                f"RT_TIMEZONE {self.timezone!r} is not a timezone this system knows: {exc}"
            ) from None

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> RtCredentials:
        """Read the credentials from the environment the unit supplies.

        Every one is required except the proxy pair, which is absent when RT is
        not behind a proxy. A missing value raises rather than defaulting, so a
        half-configured unit is a "could not run" and not a run that silently
        talks to the wrong RT or to none.
        """
        missing = [name for name in ("RT_URL", "RT_USER", "RT_PASSWORD", "RT_TIMEZONE") if not env.get(name)]
        if missing:
            raise RtError(f"RT is not configured: {', '.join(missing)} not set")
        credentials = cls(
            base_url=env["RT_URL"],
            username=env["RT_USER"],
            password=env["RT_PASSWORD"],
            timezone=env["RT_TIMEZONE"],
            http_user=env.get("RT_HTTP_USER", ""),
            http_password=env.get("RT_HTTP_PASSWORD", ""),
        )
        credentials.zone()
        return credentials


class RtError(Exception):
    """An RT call failed, or its answer could not be read."""


@dataclass(frozen=True, slots=True)
class Ticket:
    """The fields of a ticket this program acts on."""

    id: int
    queue: str
    status: str
    subject: str
    creator: str
    owner: str
    body: str = ""
    #: When RT says the ticket was filed, as an epoch. The build deadline is
    #: measured from here, so a wrong zone is a wrong deadline.
    created: float = 0.0

    @property
    def is_open(self) -> bool:
        return self.status.lower() in _OPEN_STATES


class TicketSaid:
    """What has already been said on a ticket, so it is not said twice.

    Kept beside the other state rather than read back out of RT: a comment this
    program wrote is a fact about this program, and asking RT would mean parsing
    a history whose contents other people also write.
    """

    def __init__(self, state_dir: str) -> None:
        self._root = Path(state_dir)

    def last(self, ticket: int) -> str:
        """The last state recorded for ``ticket``, or the empty string."""
        try:
            return _said_path(self._root, ticket).read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            return ""

    def changed(self, ticket: int, state: str) -> bool:
        """Whether ``state`` differs from what was last recorded."""
        return self.last(ticket) != state

    def record(self, ticket: int, state: str) -> None:
        write_whole(_said_path(self._root, ticket), f"{state}\n")


def _said_path(root: Path, ticket: int) -> Path:
    """Where the last thing said on ``ticket`` is recorded.

    A ticket id is a positive integer. Checked rather than assumed: ``-1``
    survives the filename check on its own, because a hyphen is legal in a
    filename.
    """
    if ticket <= 0:
        raise ValueError(f"not a usable ticket id for a filename: {ticket!r}")
    return root / "tickets" / safe_component(str(ticket), "ticket id")


def _parse_status(text: str) -> None:
    """Raise unless RT's status line says the call succeeded."""
    first = text.splitlines()[0] if text else ""
    match = _STATUS_RE.match(first)
    if not match:
        raise RtError(f"RT did not answer with a status line: {first[:120]!r}")
    code = int(match.group(1))
    if code >= httpx.codes.BAD_REQUEST:
        raise RtError(f"RT returned {code}: {match.group(2) or first}")


def _parse_fields(text: str) -> dict[str, str]:
    """RT's ``key: value`` show format into a mapping.

    Continuation lines (indented) belong to the previous key, which is how a
    multi-line ticket body arrives.
    """
    fields: dict[str, str] = {}
    key = ""
    for line in text.splitlines()[1:]:
        if not line.strip():
            continue
        if line[:1].isspace() and key:
            fields[key] = f"{fields[key]}\n{line.strip()}"
            continue
        name, sep, value = line.partition(":")
        if not sep:
            continue
        key = name.strip()
        fields[key] = value.strip()
    return fields


class RtClient:
    """The four operations this program needs from RT."""

    def __init__(self, credentials: RtCredentials, client: httpx.AsyncClient | None = None) -> None:
        self._base = credentials.base_url.rstrip("/")
        self._zone = credentials.zone()
        self._credentials = {"user": credentials.username, "pass": credentials.password}
        if client is not None:
            self._client = client
            return
        auth = (
            httpx.BasicAuth(credentials.http_user, credentials.http_password)
            if credentials.http_user
            else None
        )
        self._client = httpx.AsyncClient(
            auth=auth, timeout=httpx.Timeout(DEFAULT_TIMEOUT_SECONDS), follow_redirects=True
        )

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.close()

    async def close(self) -> None:
        await self._client.aclose()

    async def _post(self, path: str, data: dict[str, str] | None = None) -> str:
        form = dict(self._credentials)
        form.update(data or {})
        try:
            response = await self._client.post(f"{self._base}/REST/1.0/{path}", data=form)
        except httpx.HTTPError as exc:
            raise RtError(f"cannot reach RT at {self._base}: {exc}") from None
        if response.status_code >= httpx.codes.BAD_REQUEST:
            raise RtError(f"RT returned HTTP {response.status_code} for {path}")
        _parse_status(response.text)
        return response.text

    async def _search(self, query: str) -> tuple[int, ...]:
        """Ticket ids matching an RT search query, oldest first."""
        text = await self._post("search/ticket", {"query": query, "orderby": "+id", "format": "i"})
        ids: list[int] = []
        for line in text.splitlines():
            _, found, rest = line.strip().partition("ticket/")
            if not found:
                continue
            # `format=i` gives "ticket/12"; `format=s` gives "ticket/12: subject".
            # Reading both means a change of format is not a silently empty queue.
            number = rest.partition(":")[0].strip()
            if number.isdigit():
                ids.append(int(number))
        return tuple(ids)

    async def search(self, query: str) -> tuple[int, ...]:
        """Ticket ids matching a TicketSQL ``query``, oldest first, any status.

        The caller builds the query and owns its quoting: every value it
        interpolates must already be checked to a charset with no quote in it.
        """
        return await self._search(query)

    async def open_tickets(self, queue: str) -> tuple[int, ...]:
        """Ticket ids in ``queue`` that are new or open, oldest first.

        Stalled tickets are deliberately excluded: a stalled ticket is one a
        person has been handed, and picking it back up would undo the handing
        over. The enqueue lane needs the opposite — see ``live_tickets`` — so a
        stalled Rebuild ticket is not refiled as a duplicate.
        """
        return await self._search(f"Queue = '{queue}' AND (Status = 'new' OR Status = 'open')")

    async def live_tickets(self, queue: str) -> tuple[int, ...]:
        """Ticket ids in ``queue`` that are new, open OR stalled, oldest first.

        Includes stalled on purpose: for a backlog queue (Rebuild), a ticket a
        person has stalled is still the open record for its image, and refiling a
        duplicate while the first sits stalled is the one thing an idempotent
        filer must not do.
        """
        clause = " OR ".join(f"Status = '{state}'" for state in _OPEN_STATES)
        return await self._search(f"Queue = '{queue}' AND ({clause})")

    async def create_ticket(self, queue: str, subject: str, text: str) -> int:
        """File a new ticket in ``queue`` and return its id.

        REST 1.0 create: the body is the ``Text`` field of the Create
        transaction, which is exactly where ``ticket(...).body`` later reads it
        from. RT answers ``# Ticket N created.``.
        """
        content = f"id: ticket/new\nQueue: {queue}\nSubject: {subject}\nText: {_indent(text)}\n"
        reply = await self._post("ticket/new", {"content": content})
        match = _CREATED_ID.search(reply)
        if not match:
            head = " | ".join(reply.splitlines()[:3])
            raise RtError(f"RT did not confirm a ticket was created: {head!r}")
        return int(match.group(1))

    async def ticket(self, ticket_id: int) -> Ticket:
        """One ticket, with its body."""
        fields = _parse_fields(await self._post(f"ticket/{ticket_id}/show"))
        body = await self._body(ticket_id)
        return Ticket(
            id=ticket_id,
            queue=fields.get("Queue", ""),
            status=fields.get("Status", ""),
            subject=fields.get("Subject", ""),
            creator=fields.get("Creator", ""),
            owner=fields.get("Owner", ""),
            body=body,
            created=_parse_created(fields.get("Created", ""), self._zone, ticket_id),
        )

    async def _body(self, ticket_id: int) -> str:
        """The ticket's body, which is its FIRST attachment.

        ``ticket/<id>/show`` carries **no body at all** in REST 1.0. That was
        worth finding out the hard way: a test fixture that invented a ``Text:``
        field made every test pass while every real ticket was refused as
        "missing required field(s): service, repo, commit" -- the body was
        always the empty string.

        The first attachment is the Create transaction's content. Later ones are
        comments, including this program's own, and reading those would let a
        later comment change what the ticket asks for. One ticket is one
        revision, read once.

        Two extra round trips per ticket, to a loopback RT, on a queue whose
        depth is a handful. The alternative is parsing a ticket's whole history,
        which means reading everything anyone has ever written on it.
        """
        listed = _parse_fields(await self._post(f"ticket/{ticket_id}/attachments"))
        first = _ATTACHMENT_ID.search(listed.get("Attachments", ""))
        if not first:
            # RT makes an attachment for every Create transaction, even an empty
            # one, so none at all is RT being wrong rather than the ticket.
            raise RtError(f"ticket {ticket_id}: RT lists no attachments, so its body cannot be read")
        shown = _parse_fields(await self._post(f"ticket/{ticket_id}/attachments/{first.group(1)}"))
        return shown.get("Content", "")

    async def comment(self, ticket_id: int, text: str) -> None:
        """Add a comment. Internal, not correspondence: this is not mail."""
        body = f"id: {ticket_id}\nAction: comment\nText: {_indent(text)}\n"
        await self._post(f"ticket/{ticket_id}/comment", {"content": body})

    async def set_fields(self, ticket_id: int, **fields: str) -> None:
        """Set ticket fields, e.g. ``status="resolved"`` or ``owner="developer1"``."""
        lines = "\n".join(f"{name.capitalize()}: {value}" for name, value in fields.items())
        await self._post(f"ticket/{ticket_id}/edit", {"content": f"{lines}\n"})


def _parse_created(value: str, zone: ZoneInfo, ticket_id: int) -> float:
    """RT's ``Created`` string into an epoch, in RT's own timezone.

    Raises rather than falling back to "now". A ticket whose age cannot be read
    cannot be told from a ticket whose build is late, and treating an
    unreadable date as this instant would hand every such ticket a fresh
    build deadline for ever.
    """
    try:
        naive = time.strptime(value.strip(), _CREATED_FORMAT)
    except ValueError:
        raise RtError(f"ticket {ticket_id}: cannot read Created {value!r} as {_CREATED_FORMAT!r}") from None
    return datetime(*naive[:6], tzinfo=zone).timestamp()


def _indent(text: str) -> str:
    """RT's multi-line field continuation: every line after the first indented."""
    first, *rest = text.splitlines() or [""]
    return "\n".join([first, *(f" {line}" for line in rest)])
