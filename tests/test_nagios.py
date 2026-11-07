"""Nagios tests.

Three things worth pinning: `cmd.cgi` answers 200 with its errors inside the
page, so a status code is not evidence the command took; the status file
carries `end=1`, without which a reader cannot tell a complete file from a
truncated one; and the window is stamped in the NAGIOS SERVER's zone, not this
container's. The last one is pinned with a fixed epoch and a zone this
container is not in, because `time.localtime` passed every test while opening
every real window five hours in the future.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from pathlib import Path
from zoneinfo import ZoneInfo

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
# Eastern, like the real Nagios server, and NOT this container's zone -- the
# test process runs wherever CI runs it, so a test that used the local zone
# would be asserting that the code agrees with itself.
NAGIOS_ZONE = ZoneInfo("America/New_York")
CREDS = NagiosCredentials(
    cmd_cgi_url="http://nagios/cgi-bin/cmd.cgi",
    username="u",
    password="p",
    timezone=NAGIOS_ZONE,
)
HOST = "ctr-acquacotta.crunchtools.com"
GOOD_ENV: Mapping[str, str] = {
    "NAGIOS_CMD_URL": "http://n/cmd.cgi",
    "NAGIOS_USER": "u",
    "NAGIOS_PASSWORD": "p",
    "NAGIOS_TIMEZONE": "America/New_York",
}
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


def recorder(
    downtimes: dict[str, object] | None = None, gets: list[httpx.URL] | None = None
) -> tuple[list[dict[str, str]], Handler]:
    """Records every cmd.cgi POST, and answers the downtime list GET.

    The list matters: cancelling needs a `down_id`, so a transport that only
    answers the POST cannot exercise the cancel path at all. Pass ``gets`` to
    record the URLs it was asked for, which is how the statusjson URL and its
    query are pinned.
    """
    seen: list[dict[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            if gets is not None:
                gets.append(request.url)
            return httpx.Response(200, json=downtimes if downtimes is not None else listing())
        seen.append(dict(httpx.QueryParams(request.content.decode())))
        return httpx.Response(200, text=OK_BODY)

    return seen, handler


# --- credentials ---


def test_credentials_come_from_the_environment() -> None:
    creds = NagiosCredentials.from_env(dict(GOOD_ENV))
    assert creds.comment_author == "deployer"
    assert creds.timezone == NAGIOS_ZONE


@pytest.mark.parametrize("absent", list(GOOD_ENV))
def test_a_missing_credential_is_refused(absent: str) -> None:
    env = dict(GOOD_ENV)
    del env[absent]
    with pytest.raises(NagiosError, match=absent):
        NagiosCredentials.from_env(env)


def test_an_unknown_timezone_is_refused_rather_than_guessed() -> None:
    # A default would be a guess, and this is the one setting where a wrong
    # guess is silent: the window opens, Nagios accepts it, and it suppresses
    # nothing.
    env = {**GOOD_ENV, "NAGIOS_TIMEZONE": "Middle/Earth"}
    with pytest.raises(NagiosError, match="not a zone this container knows"):
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


async def test_the_window_is_stamped_in_the_nagios_servers_zone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The epoch from ticket 1572, and the string Nagios should have been sent.

    1794085507 is 16:05:07 Eastern and 21:05:07 UTC. `time.localtime` in a
    container carrying no local-zone file and no TZ produced the UTC one, Nagios
    read it as Eastern, and the window opened five hours after the restart it
    was supposed to cover -- then was deleted before it began. Every window
    this program had ever opened was a no-op and nothing said so.

    The epoch is fixed rather than `time.time()` so this asserts a conversion
    and not merely that two calls agree.
    """
    monkeypatch.setattr("deployer.nagios.time.time", lambda: 1794085507.0)
    seen, handler = recorder()
    async with commands(handler) as nagios:
        await nagios.open_downtime(HOST, 6, "deployer ticket 1572")
    assert seen[0]["start_time"] == "11-07-2026 16:05:07"
    assert seen[0]["end_time"] == "11-07-2026 16:11:07"


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


async def test_the_downtime_list_is_asked_for_by_host_at_the_right_url() -> None:
    # statusjson.cgi sits beside cmd.cgi and is derived from it, so a wrong
    # derivation would mean cancelling never finds an id -- which is exactly
    # the bug this whole path replaces, and a mock that accepts any GET would
    # not notice.
    gets: list[httpx.URL] = []
    _seen, handler = recorder(listing(), gets)
    async with commands(handler) as nagios:
        await nagios.cancel_downtime(HOST)
    assert len(gets) == 1
    assert str(gets[0]).startswith("http://nagios/cgi-bin/statusjson.cgi?")
    params = dict(gets[0].params)
    assert params == {"query": "downtimelist", "details": "true", "hostname": HOST}


async def test_an_http_error_on_the_downtime_list_is_reported() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        code = httpx.codes.SERVICE_UNAVAILABLE if request.method == "GET" else httpx.codes.OK
        return httpx.Response(code, text="down" if request.method == "GET" else OK_BODY)

    async with commands(handler) as nagios:
        with pytest.raises(NagiosError, match="503"):
            await nagios.cancel_downtime(HOST)
