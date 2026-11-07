"""Nagios tests.

Two things worth pinning: `cmd.cgi` answers 200 with its errors inside the
page, so a status code is not evidence the command took; and the status file
carries `end=1`, without which a reader cannot tell a complete file from a
truncated one.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from pathlib import Path

import httpx
import pytest

from deployer.hold import Hold
from deployer.nagios import (
    CMD_DEL_HOST_DOWNTIME,
    CMD_DEL_SVC_DOWNTIME,
    CMD_SCHEDULE_HOST_DOWNTIME,
    CMD_SCHEDULE_HOST_SVC_DOWNTIME,
    CMD_SCHEDULE_SVC_DOWNTIME,
    STATUS_PREFIX,
    NagiosCommands,
    NagiosCredentials,
    NagiosError,
    my_windows,
    write_status,
)

Handler = Callable[[httpx.Request], httpx.Response]
CREDS = NagiosCredentials(cmd_cgi_url="http://nagios/cgi-bin/cmd.cgi", username="u", password="p")
HOST = "ctr-acquacotta.crunchtools.com"
OK_BODY = "<div class='infoMessage'>Your command request was successfully submitted to Nagios</div>"
MINUTES = 90


def commands(handler: Handler) -> NagiosCommands:
    return NagiosCommands(CREDS, client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))


#: What statusjson.cgi answers for a host this program has a window on. Two
#: entries, because open_downtime without a service opens the host's window and
#: its services'.
def listing(*entries: dict[str, object]) -> dict[str, object]:
    return {"data": {"downtimelist": {str(n): entry for n, entry in enumerate(entries)}}}


def window(entry_id: int, service: str | None = None, author: str = "deployer") -> dict[str, object]:
    return {
        "downtime_id": entry_id,
        "host_name": HOST,
        "service_description": service,
        "author": author,
    }


def recorder(downtimes: dict[str, object] | None = None) -> tuple[list[dict[str, str]], Handler]:
    """Records every cmd.cgi POST, and answers the downtime list GET.

    The list matters: cancelling needs a `down_id`, so a transport that only
    answers the POST cannot exercise the cancel path at all.
    """
    seen: list[dict[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json=downtimes if downtimes is not None else listing())
        seen.append(dict(httpx.QueryParams(request.content.decode())))
        return httpx.Response(200, text=OK_BODY)

    return seen, handler


# --- credentials ---


def test_credentials_come_from_the_environment() -> None:
    creds = NagiosCredentials.from_env(
        {"NAGIOS_CMD_URL": "http://n/cmd.cgi", "NAGIOS_USER": "u", "NAGIOS_PASSWORD": "p"}
    )
    assert creds.comment_author == "deployer"


@pytest.mark.parametrize("absent", ["NAGIOS_CMD_URL", "NAGIOS_USER", "NAGIOS_PASSWORD"])
def test_a_missing_credential_is_refused(absent: str) -> None:
    env = {"NAGIOS_CMD_URL": "http://n/cmd.cgi", "NAGIOS_USER": "u", "NAGIOS_PASSWORD": "p"}
    del env[absent]
    with pytest.raises(NagiosError, match=absent):
        NagiosCredentials.from_env(env)


# --- opening and closing ---


async def test_host_downtime_covers_the_host_and_its_services() -> None:
    # A container restart turns the host check and every service check red
    # together, so both commands are needed.
    seen, handler = recorder()
    async with commands(handler) as nagios:
        await nagios.open_downtime(HOST, MINUTES, "deploying")
    assert [int(call["cmd_typ"]) for call in seen] == [
        CMD_SCHEDULE_HOST_DOWNTIME,
        CMD_SCHEDULE_HOST_SVC_DOWNTIME,
    ]
    assert seen[0]["host"] == HOST
    assert seen[0]["fixed"] == "1"
    assert seen[0]["hours"] == "1"
    assert seen[0]["minutes"] == "30"
    assert seen[0]["com_data"] == "deploying"


async def test_service_downtime_sends_one_command_with_the_service() -> None:
    seen, handler = recorder()
    async with commands(handler) as nagios:
        await nagios.open_downtime(HOST, 30, "deploying", service="Gunicorn")
    assert [int(call["cmd_typ"]) for call in seen] == [CMD_SCHEDULE_SVC_DOWNTIME]
    assert seen[0]["service"] == "Gunicorn"


async def test_cancelling_sends_a_down_id_not_a_host() -> None:
    # The bug this replaces: DEL_HOST_DOWNTIME takes down_id and nothing else.
    # Sent a host name Nagios answers "not authorized", which reads like a
    # credentials problem and is not one -- so every deploy left its window
    # open and a rollback that did not come back stayed silent.
    seen, handler = recorder(listing(window(7), window(8, service="Gunicorn")))
    async with commands(handler) as nagios:
        await nagios.cancel_downtime(HOST)
    assert [int(call["cmd_typ"]) for call in seen] == [CMD_DEL_HOST_DOWNTIME, CMD_DEL_SVC_DOWNTIME]
    assert [call["down_id"] for call in seen] == ["7", "8"]
    assert not any("host" in call for call in seen)


async def test_cancelling_one_service_leaves_the_host_window_alone() -> None:
    seen, handler = recorder(listing(window(7), window(8, service="Gunicorn")))
    async with commands(handler) as nagios:
        await nagios.cancel_downtime(HOST, service="Gunicorn")
    assert [call["down_id"] for call in seen] == ["8"]


async def test_a_window_somebody_else_opened_is_left_alone() -> None:
    # A guard against accidents rather than an authorization boundary: the CGI
    # user can already delete any downtime. But cleaning up after a deploy must
    # not end a window a person opened to work on the same service.
    seen, handler = recorder(listing(window(7, author="scott"), window(9)))
    async with commands(handler) as nagios:
        await nagios.cancel_downtime(HOST)
    assert [call["down_id"] for call in seen] == ["9"]


async def test_a_window_on_another_host_is_left_alone() -> None:
    other = {"downtime_id": 5, "host_name": "ctr-other", "service_description": None, "author": "deployer"}
    seen, handler = recorder(listing(other, window(9)))
    async with commands(handler) as nagios:
        await nagios.cancel_downtime(HOST)
    assert [call["down_id"] for call in seen] == ["9"]


async def test_nothing_to_cancel_sends_no_command() -> None:
    seen, handler = recorder(listing())
    async with commands(handler) as nagios:
        await nagios.cancel_downtime(HOST)
    assert seen == []


@pytest.mark.parametrize(
    "answer",
    [{}, {"data": None}, {"data": {}}, {"data": {"downtimelist": None}}, {"data": {"downtimelist": 3}}],
)
async def test_a_downtime_list_shape_this_cannot_read_holds_no_ids(answer: dict[str, object]) -> None:
    seen, handler = recorder(answer)
    async with commands(handler) as nagios:
        await nagios.cancel_downtime(HOST)
    assert seen == []


async def test_a_downtime_list_as_a_plain_array_is_read_too() -> None:
    # Nagios gives a mapping keyed by id on some builds and an array on others.
    seen, handler = recorder({"data": {"downtimelist": [window(4)]}})
    async with commands(handler) as nagios:
        await nagios.cancel_downtime(HOST)
    assert [call["down_id"] for call in seen] == ["4"]


async def test_an_unreadable_downtime_list_is_an_error_not_an_empty_one() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = "<html>not json</html>" if request.method == "GET" else OK_BODY
        return httpx.Response(200, text=body)

    async with commands(handler) as nagios:
        with pytest.raises(NagiosError, match="not JSON"):
            await nagios.cancel_downtime(HOST)


async def test_a_200_with_no_confirmation_is_a_failure() -> None:
    # cmd.cgi answers 200 whether or not it acted, so the body is the evidence.
    async with commands(lambda _r: httpx.Response(200, text="<html>nothing happened</html>")) as nagios:
        with pytest.raises(NagiosError, match="no confirmation"):
            await nagios.open_downtime(HOST, MINUTES, "deploying")


async def test_an_error_inside_the_page_is_reported() -> None:
    body = "<div class='errorMessage'>Author was not entered</div>"
    async with commands(lambda _r: httpx.Response(200, text=body)) as nagios:
        with pytest.raises(NagiosError, match="Author was not entered"):
            await nagios.open_downtime(HOST, MINUTES, "deploying")


async def test_an_http_error_names_the_status() -> None:
    async with commands(lambda _r: httpx.Response(503, text="down")) as nagios:
        with pytest.raises(NagiosError, match="503"):
            await nagios.cancel_downtime(HOST)


async def test_an_unreachable_nagios_names_the_url() -> None:
    def refused(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    async with commands(refused) as nagios:
        with pytest.raises(NagiosError, match="cmd.cgi"):
            await nagios.open_downtime(HOST, MINUTES, "deploying")


async def test_an_unreachable_nagios_names_the_url_when_cancelling_too() -> None:
    def refused(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    async with commands(refused) as nagios:
        with pytest.raises(NagiosError, match="statusjson.cgi"):
            await nagios.cancel_downtime(HOST)


# --- the status file ---


def test_the_status_file_has_the_collector_frame(tmp_path: Path) -> None:
    path = write_status(tmp_path / "deployer.status", "ok", "acquacotta 635cdda", now=1700000000)
    lines = path.read_text(encoding="utf-8").splitlines()
    assert lines[0].startswith(f"{STATUS_PREFIX} generated=1700000000 ")
    # end=1 is what proves the file was written whole; a reader that cannot
    # find it should treat the file as unusable, not as "nothing wrong".
    assert lines[-1] == f"{STATUS_PREFIX} end=1"


def test_holds_appear_as_item_lines(tmp_path: Path) -> None:
    holds = [
        Hold(unit="a.service", service="a", ticket=7, revision="f" * 40, at=1699999999),
        Hold(unit="b.service", service="b", ticket=8, revision="e" * 40, at=1699999998),
    ]
    text = write_status(tmp_path / "s", "held", "two services held", holds=holds).read_text("utf-8")
    assert "hold unit=a.service ticket=7" in text
    assert "revision=" + "f" * 12 in text
    assert text.count(f"{STATUS_PREFIX} hold ") == len(holds)


def test_no_holds_means_no_item_lines(tmp_path: Path) -> None:
    text = write_status(tmp_path / "s", "ok", "nothing held").read_text(encoding="utf-8")
    assert " hold " not in text


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("has space", "has_space"),
        ("a|b", "a/b"),
        ("", "-"),
        # A tab is outside the printable range, so it is DELETED rather than
        # substituted -- the same as cl_clean's `tr -cd`. Nothing survives to
        # split a field, which is the property that matters.
        ("tab\there", "tabhere"),
    ],
)
def test_values_are_cleaned_so_a_field_cannot_split(tmp_path: Path, raw: str, expected: str) -> None:
    # Fields are space-separated, so a space in a value would read as a new one.
    text = write_status(tmp_path / "s", "ok", raw).read_text(encoding="utf-8")
    assert f"detail={expected}" in text


def test_a_long_detail_is_truncated(tmp_path: Path) -> None:
    text = write_status(tmp_path / "s", "ok", "x" * 500).read_text(encoding="utf-8")
    assert "x" * 80 in text
    assert "x" * 81 not in text


def test_writing_the_status_creates_its_directory(tmp_path: Path) -> None:
    path = write_status(tmp_path / "deep" / "deeper" / "s", "ok", "fine")
    assert path.is_file()


# --- which windows are this program's, tested on its own ---


def test_my_windows_takes_the_host_window_and_its_services() -> None:
    # What open_downtime opens without a service: a container restart turns
    # the host check and every service check red together.
    entries: list[Mapping[str, object]] = [window(1), window(2, service="Gunicorn")]
    assert my_windows(entries, "deployer", HOST, None) == [(1, False), (2, True)]


def test_my_windows_ignores_an_entry_with_no_usable_id() -> None:
    # A string id is not an id this can send as down_id.
    entries: list[Mapping[str, object]] = [
        {"downtime_id": "7", "host_name": HOST, "author": "deployer"},
        window(9),
    ]
    assert my_windows(entries, "deployer", HOST, None) == [(9, False)]
