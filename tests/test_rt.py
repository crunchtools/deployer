"""RT client tests.

The one worth the most: a ticket is commented on only when something changed.
A waiting ticket is looked at every two minutes, and a robot that said "still
waiting" 720 times a day would make the ticket useless to whoever reads it.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import httpx
import pytest

from deployer.rt import RtClient, RtCredentials, RtError, Ticket, TicketSaid

OK = "RT/6.0.2 200 Ok\n\n"
TICKET_ID = 1234
CREDS = RtCredentials(base_url="http://rt.example/", username="deployer", password="secret")


Handler = Callable[[httpx.Request], httpx.Response]


def client(handler: Handler) -> RtClient:
    """A client whose transport is a stub, so no RT is needed."""
    return RtClient(CREDS, client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))


def answering(text: str) -> Handler:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=text)

    return handler


# --- credentials ---


def test_credentials_come_from_the_environment() -> None:
    creds = RtCredentials.from_env(
        {"RT_URL": "http://rt/", "RT_USER": "u", "RT_PASSWORD": "p", "RT_HTTP_USER": "proxy"}
    )
    assert creds.base_url == "http://rt/"
    assert creds.http_user == "proxy"
    assert creds.http_password == ""


@pytest.mark.parametrize("absent", ["RT_URL", "RT_USER", "RT_PASSWORD"])
def test_a_missing_credential_is_refused_not_defaulted(absent: str) -> None:
    env = {"RT_URL": "http://rt/", "RT_USER": "u", "RT_PASSWORD": "p"}
    del env[absent]
    with pytest.raises(RtError, match=absent):
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
    body = (
        OK + "id: ticket/1234\nQueue: Deploy\nStatus: new\n"
        "Subject: deploy acquacotta 635cdda\nCreator: developer1\nOwner: Nobody\n"
        "Text: service: acquacotta.crunchtools.com\n"
        "      repo: crunchtools/acquacotta\n"
    )
    async with client(answering(body)) as rt:
        ticket = await rt.ticket(TICKET_ID)
    assert ticket.queue == "Deploy"
    assert ticket.creator == "developer1"
    assert ticket.is_open
    # Indented continuation lines are the ticket body arriving over several lines.
    assert "repo: crunchtools/acquacotta" in ticket.body


@pytest.mark.parametrize(
    ("status", "open_"),
    [("new", True), ("open", True), ("stalled", True), ("resolved", False), ("rejected", False)],
)
def test_is_open_follows_the_status(status: str, open_: bool) -> None:
    assert Ticket(id=1, queue="Deploy", status=status, subject="", creator="", owner="").is_open is open_


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
