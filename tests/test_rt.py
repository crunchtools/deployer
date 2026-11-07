"""RT client tests.

The one worth the most: a ticket is commented on only when something changed.
A waiting ticket is looked at every two minutes, and a robot that said "still
waiting" 720 times a day would make the ticket useless to whoever reads it.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from deployer.rt import RtClient, RtCredentials, RtError, Ticket, TicketSaid

OK = "RT/6.0.2 200 Ok\n\n"
TICKET_ID = 1234
# America/New_York because that is what this RT renders its dates in; the
# container's own clock is UTC, which is exactly why the zone is configuration
# and not an assumption.
CREDS = RtCredentials(
    base_url="http://rt.example/", username="deployer", password="secret", timezone="America/New_York"
)


Handler = Callable[[httpx.Request], httpx.Response]


def client(handler: Handler) -> RtClient:
    """A client whose transport is a stub, so no RT is needed."""
    return RtClient(CREDS, client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))


def answering(text: str) -> Handler:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=text)

    return handler


ATTACHMENTS = OK + "id: ticket/1234/attachments\n\nAttachments: 13583: (Unnamed) (text/plain / 9b)\n"


def with_body(show: str, body: str = "service: acquacotta\n") -> Handler:
    """A transport that answers the three calls reading one ticket makes.

    `ticket/<id>/show` carries no body in REST 1.0 -- the body is the first
    attachment's `Content:`. A fixture that invented a `Text:` field let every
    test pass while every real ticket was refused for having no body at all.
    """
    indented = "\n".join([body.splitlines()[0], *(f"         {line}" for line in body.splitlines()[1:])])
    attachment = OK + f"id: 13583\nContentType: text/plain\n\nContent: {indented}\n\n"

    def handler(request: httpx.Request) -> httpx.Response:
        path = str(request.url.path)
        if path.endswith("/attachments"):
            return httpx.Response(200, text=ATTACHMENTS)
        if "/attachments/" in path:
            return httpx.Response(200, text=attachment)
        return httpx.Response(200, text=show)

    return handler


# --- credentials ---


def test_credentials_come_from_the_environment() -> None:
    creds = RtCredentials.from_env(
        {
            "RT_URL": "http://rt/",
            "RT_USER": "u",
            "RT_PASSWORD": "p",
            "RT_TIMEZONE": "UTC",
            "RT_HTTP_USER": "proxy",
        }
    )
    assert creds.base_url == "http://rt/"
    assert creds.http_user == "proxy"
    assert creds.http_password == ""


@pytest.mark.parametrize("absent", ["RT_URL", "RT_USER", "RT_PASSWORD", "RT_TIMEZONE"])
def test_a_missing_credential_is_refused_not_defaulted(absent: str) -> None:
    env = {"RT_URL": "http://rt/", "RT_USER": "u", "RT_PASSWORD": "p", "RT_TIMEZONE": "UTC"}
    del env[absent]
    with pytest.raises(RtError, match=absent):
        RtCredentials.from_env(env)


def test_an_unknown_timezone_is_refused_at_load_not_at_the_first_date() -> None:
    # A typo here would otherwise surface as an unreadable ticket twenty
    # minutes into a run, with nothing pointing at the env file.
    env = {"RT_URL": "http://rt/", "RT_USER": "u", "RT_PASSWORD": "p", "RT_TIMEZONE": "Mars/Olympus"}
    with pytest.raises(RtError, match="Mars/Olympus"):
        RtCredentials.from_env(env)


# --- the status line ---


async def test_an_rt_error_status_is_an_rt_error() -> None:
    async with client(answering("RT/6.0.2 401 Credentials required\n")) as rt:
        with pytest.raises(RtError, match="401"):
            await rt.ticket(TICKET_ID)


async def test_an_answer_with_no_status_line_is_refused() -> None:
    # RT behind a misconfigured proxy answers 200 with an HTML login page.
    async with client(answering("<html>please log in</html>")) as rt:
        with pytest.raises(RtError, match="did not answer with a status line"):
            await rt.ticket(TICKET_ID)


async def test_an_http_error_names_the_status() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(502, text="bad gateway")

    async with client(handler) as rt:
        with pytest.raises(RtError, match="502"):
            await rt.ticket(TICKET_ID)


async def test_an_unreachable_rt_names_the_base_url() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    async with client(handler) as rt:
        with pytest.raises(RtError, match="rt.example"):
            await rt.ticket(TICKET_ID)


# --- reading ---


async def test_the_open_queue_is_read_oldest_first() -> None:
    body = OK + "ticket/12: deploy acquacotta\nticket/34: deploy mcp-pcloud\n"
    async with client(answering(body)) as rt:
        assert await rt.open_tickets("Deploy") == (12, 34)


async def test_an_empty_queue_is_an_empty_tuple() -> None:
    async with client(answering(OK + "No matching results.\n")) as rt:
        assert await rt.open_tickets("Deploy") == ()


async def test_the_query_excludes_stalled_tickets() -> None:
    # A stalled ticket is one a person has been handed; picking it back up
    # would undo the handing over.
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.content.decode())
        return httpx.Response(200, text=OK)

    async with client(handler) as rt:
        await rt.open_tickets("Deploy")
    assert "stalled" not in seen[0]
    assert "new" in seen[0]


async def test_a_ticket_is_read_into_its_fields() -> None:
    show = (
        OK + "id: ticket/1234\nQueue: Deploy\nStatus: new\n"
        "Subject: deploy acquacotta 635cdda\nCreator: developer1\nOwner: Nobody\n"
        "Created: Fri Nov 06 04:00:46 2026\n"
    )
    handler = with_body(show, "service: acquacotta.crunchtools.com\nrepo: crunchtools/acquacotta\n")
    async with client(handler) as rt:
        ticket = await rt.ticket(TICKET_ID)
    assert ticket.queue == "Deploy"
    assert ticket.creator == "developer1"
    assert ticket.is_open
    # The body comes from the first attachment, with RT's indentation on the
    # continuation lines undone.
    assert ticket.body == "service: acquacotta.crunchtools.com\nrepo: crunchtools/acquacotta"


@pytest.mark.parametrize(
    ("status", "open_"),
    [("new", True), ("open", True), ("stalled", True), ("resolved", False), ("rejected", False)],
)
def test_is_open_follows_the_status(status: str, open_: bool) -> None:
    ticket = Ticket(id=1, queue="Deploy", status=status, subject="", creator="", owner="")
    assert ticket.is_open is open_


# --- writing ---


async def test_a_comment_indents_its_continuation_lines() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.content.decode())
        return httpx.Response(200, text=OK)

    async with client(handler) as rt:
        await rt.comment(TICKET_ID, "first line\nsecond line")
    # RT's multi-line field format: every line after the first is indented, or
    # the second line reads as a new field name.
    assert "second+line" in seen[0] or "second line" in seen[0]
    assert "Action%3A+comment" in seen[0] or "Action: comment" in seen[0]


async def test_set_fields_capitalises_the_names_rt_expects() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(httpx.QueryParams(request.content.decode())["content"])
        return httpx.Response(200, text=OK)

    async with client(handler) as rt:
        await rt.set_fields(TICKET_ID, status="stalled", owner="developer1")
    assert "Status: stalled" in seen[0]
    assert "Owner: developer1" in seen[0]


# --- what has already been said ---


def test_nothing_said_yet_reads_as_empty(tmp_path: Path) -> None:
    assert TicketSaid(str(tmp_path)).last(TICKET_ID) == ""


def test_a_recorded_state_is_remembered(tmp_path: Path) -> None:
    said = TicketSaid(str(tmp_path))
    said.record(TICKET_ID, "waiting")
    assert said.last(TICKET_ID) == "waiting"


def test_the_same_state_is_not_a_change(tmp_path: Path) -> None:
    # This is what stops 720 "still waiting" comments a day.
    said = TicketSaid(str(tmp_path))
    said.record(TICKET_ID, "waiting")
    assert not said.changed(TICKET_ID, "waiting")
    assert said.changed(TICKET_ID, "rolled-back")


def test_a_first_state_is_always_a_change(tmp_path: Path) -> None:
    assert TicketSaid(str(tmp_path)).changed(TICKET_ID, "waiting")


def test_state_is_per_ticket(tmp_path: Path) -> None:
    said = TicketSaid(str(tmp_path))
    said.record(TICKET_ID, "waiting")
    assert said.changed(TICKET_ID + 1, "waiting")


def test_a_ticket_id_unfit_for_a_filename_is_refused(tmp_path: Path) -> None:
    said = TicketSaid(str(tmp_path))
    with pytest.raises(ValueError, match="not a usable"):
        said.last(-1)


# --- Created, and the timezone it is rendered in ---


def _show(created: str) -> str:
    return (
        OK + "id: ticket/1234\nQueue: Deploy\nStatus: new\nSubject: s\n"
        f"Creator: developer1\nOwner: Nobody\nCreated: {created}\n"
    )


CREATED = "Fri Nov 06 04:00:46 2026"


async def test_created_is_read_in_rts_own_timezone() -> None:
    # RT renders dates with no zone in the string at all. This RT is on
    # America/New_York, so that wall clock is 09:00:46 UTC.
    async with client(with_body(_show(CREATED))) as rt:
        ticket = await rt.ticket(TICKET_ID)
    assert ticket.created == datetime(2026, 11, 6, 9, 0, 46, tzinfo=UTC).timestamp()


async def test_the_configured_zone_is_the_one_used() -> None:
    # The same string under UTC is five hours earlier in epoch terms. Pinned
    # because reading it as the container's own clock -- which IS UTC -- would
    # be enough to call a fresh ticket stale or wait for ever on an old one.
    utc = RtCredentials(base_url="http://rt.example/", username="u", password="p", timezone="UTC")
    transport = httpx.MockTransport(with_body(_show(CREATED)))
    async with RtClient(utc, client=httpx.AsyncClient(transport=transport)) as rt:
        as_utc = await rt.ticket(TICKET_ID)
    async with client(with_body(_show(CREATED))) as rt:
        as_new_york = await rt.ticket(TICKET_ID)
    assert as_new_york.created - as_utc.created == 5 * 3600


async def test_an_unreadable_created_is_an_error_not_a_guess() -> None:
    # Treating it as "now" would hand the ticket a fresh build deadline on
    # every single run, so it would wait for ever instead of stalling.
    async with client(with_body(_show("yesterday afternoon"))) as rt:
        with pytest.raises(RtError, match="cannot read Created"):
            await rt.ticket(TICKET_ID)


async def test_a_missing_created_is_an_error() -> None:
    show = OK + "id: ticket/1234\nQueue: Deploy\nStatus: new\nCreator: d\nOwner: Nobody\n"
    async with client(with_body(show)) as rt:
        with pytest.raises(RtError, match="cannot read Created"):
            await rt.ticket(TICKET_ID)


# --- the body, which is the first attachment and not a field of `show` ---


async def test_the_body_comes_from_the_first_attachment() -> None:
    # The one that matters. `ticket/<id>/show` has no body field at all, and a
    # fixture that invented one let every test pass while every real ticket was
    # refused for having no body.
    seen: list[str] = []
    body = "service: acquacotta\nrepo: crunchtools/acquacotta\ncommit: " + "a" * 40 + "\n"
    handler = with_body(_show(CREATED), body)

    def recording(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url.path))
        return handler(request)

    async with client(recording) as rt:
        ticket = await rt.ticket(TICKET_ID)
    assert ticket.body == body.rstrip("\n")
    # Three calls: the fields, the attachment list, and the first attachment.
    assert [path.rsplit("/REST/1.0/", 1)[-1] for path in seen] == [
        "ticket/1234/show",
        "ticket/1234/attachments",
        "ticket/1234/attachments/13583",
    ]


async def test_the_first_attachment_is_taken_not_the_newest() -> None:
    # Later attachments are comments, including this program's own. Reading one
    # would let a comment change what the ticket asks for.
    listing = (
        OK
        + "id: x\n\nAttachments: 13583: (Unnamed) (text/plain / 9b),\n"
        + "             13584: (Unnamed) (text/plain / 4b)\n"
    )
    asked: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = str(request.url.path)
        asked.append(path)
        if path.endswith("/attachments"):
            return httpx.Response(200, text=listing)
        if "/attachments/" in path:
            return httpx.Response(200, text=OK + "id: 13583\n\nContent: service: acquacotta\n")
        return httpx.Response(200, text=_show(CREATED))

    async with client(handler) as rt:
        await rt.ticket(TICKET_ID)
    assert any(path.endswith("/attachments/13583") for path in asked)
    assert not any(path.endswith("/attachments/13584") for path in asked)


async def test_a_ticket_rt_lists_no_attachments_for_is_an_error() -> None:
    # RT makes an attachment for every Create transaction, even an empty one,
    # so none at all is RT being wrong rather than the ticket being empty.
    def handler(request: httpx.Request) -> httpx.Response:
        listed = str(request.url.path).endswith("/attachments")
        text = OK + "id: x\n\nAttachments:\n" if listed else _show(CREATED)
        return httpx.Response(200, text=text)

    async with client(handler) as rt:
        with pytest.raises(RtError, match="lists no attachments"):
            await rt.ticket(TICKET_ID)
