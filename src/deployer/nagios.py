"""Nagios: the maintenance window around a deploy, and the record afterwards.

Two unrelated things that both happen to involve Nagios.

**Downtime** goes through ``cmd.cgi``, the same POST mcp-nagios makes, so a
window this program opens looks exactly like one a person opened.

A window is opened before a unit is touched and closed when the deploy
verifies. The one case where it is **cancelled early and deliberately** is a
rollback that did not come back: at that point a human has to see the service,
and the downtime is the only thing stopping Nagios from saying so.

A window is also opened in the Nagios server's **own time zone**, which is
not this container's. ``cmd.cgi`` takes ``start_time`` as naive wall clock and
reads it in its own zone, so formatting the epoch with ``time.localtime`` in a
container that carries no local-zone file and no ``TZ`` produced a UTC string
that Nagios read as Eastern: every window this program opened began five hours
in the future and was deleted before it started. Measured against the real
Nagios on ticket 1572 -- `SCHEDULE_HOST_DOWNTIME` at 21:05:07 for a window
starting 02:05:07. The suppression the design promises had never once
existed. So the zone is configuration (``NAGIOS_TIMEZONE``), required rather
than defaulted, because a default here is a guess and a wrong guess is silent.

Closing one needs its **id**. ``DEL_HOST_DOWNTIME`` takes ``down_id`` and
nothing else; sent a host name it answers "not authorized", which reads like a
credentials problem and is not one. Measured against the real Nagios: opening
worked and closing never did, so every deploy would have left its window open
and the one alarm this program deliberately raises would never have sounded.
So cancelling reads ``statusjson.cgi?query=downtimelist`` first, and cancels
only the windows whose comment author is this program -- which also means
cleaning up after a deploy cannot end a window a person opened by hand to work
on the same service.

**The status file** is a record of the last run, in nagios-agent's collector
frame (``collector-lib.sh``, 7.1.0): a ``generated=`` line, item lines, and
``end=1`` to prove the file was written whole.

It is written **only when a ticket runs**, which matters for whoever reads it:
it is not a heartbeat, and its age says nothing about whether this program is
healthy. The Nagios check for holds therefore reads the host's own state
directory rather than this file, because a hold placed last week sits under a
status file whose `generated=` is from last week too.
"""

from __future__ import annotations

import re
import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Final, Self
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx

from .hold import Hold
from .statefile import frame_field
from .statefile import write_frame

__all__ = [
    "STATUS_PREFIX",
    "NagiosCommands",
    "NagiosCredentials",
    "NagiosError",
    "my_windows",
    "write_status",
]

#: cmd.cgi command ids, from Nagios's own table.
CMD_SCHEDULE_HOST_DOWNTIME: Final = 55
CMD_SCHEDULE_SVC_DOWNTIME: Final = 56
CMD_DEL_HOST_DOWNTIME: Final = 78
CMD_DEL_SVC_DOWNTIME: Final = 79
CMD_SCHEDULE_HOST_SVC_DOWNTIME: Final = 86

DEFAULT_TIMEOUT_SECONDS: Final = 30.0
STATUS_PREFIX: Final = "DEPLOYER"
_ERROR_RE: Final = re.compile(r"errorMessage'>([^<]+)")


class NagiosError(Exception):
    """A Nagios command failed, or was refused."""


@dataclass(frozen=True, slots=True)
class NagiosCredentials:
    """Where Nagios's CGI lives and who this program is to it."""

    cmd_cgi_url: str
    username: str
    password: str
    #: The Nagios server's own zone, because that is how it reads the naive
    #: wall-clock times cmd.cgi takes. Not this container's zone, which is UTC.
    timezone: ZoneInfo
    comment_author: str = "deployer"

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> NagiosCredentials:
        """Read from the environment the unit supplies, refusing a gap.

        A half-configured unit is a "could not run": deploying with no way to
        open a maintenance window means every deploy pages somebody.
        """
        names = ("NAGIOS_CMD_URL", "NAGIOS_USER", "NAGIOS_PASSWORD", "NAGIOS_TIMEZONE")
        missing = [n for n in names if not env.get(n)]
        if missing:
            raise NagiosError(f"Nagios is not configured: {', '.join(missing)} not set")
        name = env["NAGIOS_TIMEZONE"]
        try:
            zone = ZoneInfo(name)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise NagiosError(f"NAGIOS_TIMEZONE {name!r} is not a zone this container knows: {exc}") from None
        return cls(
            cmd_cgi_url=env["NAGIOS_CMD_URL"],
            username=env["NAGIOS_USER"],
            password=env["NAGIOS_PASSWORD"],
            timezone=zone,
            comment_author=env.get("NAGIOS_COMMENT_AUTHOR", "deployer"),
        )


def _wall_clock(epoch: float, zone: ZoneInfo) -> str:
    """``epoch`` as cmd.cgi wants it: naive wall clock in the Nagios server's zone.

    ``time.localtime`` was wrong here and wrong silently. This container
    carries no local-zone file and no ``TZ``, so it formatted UTC, and Nagios
    read that string as Eastern -- a window five hours in the future, deleted
    before it began. The zone has to be the server's and the server cannot be
    asked, so it is configuration.
    """
    return datetime.fromtimestamp(epoch, zone).strftime("%m-%d-%Y %H:%M:%S")


class NagiosCommands:
    """Open and close a maintenance window."""

    def __init__(self, credentials: NagiosCredentials, client: httpx.AsyncClient | None = None) -> None:
        self._creds = credentials
        self._client = client or httpx.AsyncClient(
            auth=httpx.BasicAuth(credentials.username, credentials.password),
            timeout=httpx.Timeout(DEFAULT_TIMEOUT_SECONDS),
        )

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.close()

    async def close(self) -> None:
        await self._client.aclose()

    async def _submit(self, command: int, form: Mapping[str, str]) -> None:
        payload = {"cmd_typ": str(command), "cmd_mod": "2", "btnSubmit": "Commit", **form}
        try:
            response = await self._client.post(self._creds.cmd_cgi_url, data=payload)
        except httpx.HTTPError as exc:
            raise NagiosError(f"cannot reach Nagios at {self._creds.cmd_cgi_url}: {exc}") from None
        if response.status_code >= httpx.codes.BAD_REQUEST:
            raise NagiosError(f"Nagios returned HTTP {response.status_code} for command {command}")

        body = response.text
        if "successfully submitted" in body.lower():
            return
        # cmd.cgi answers 200 with the error inside the page, so a status code
        # is not enough to know the command took.
        errors = _ERROR_RE.findall(body)
        raise NagiosError(
            f"Nagios refused command {command}: {'; '.join(errors) if errors else 'no confirmation'}"
        )

    async def open_downtime(self, host: str, minutes: int, comment: str, service: str | None = None) -> None:
        """Open a fixed window starting now.

        Without ``service`` the host and every service on it go into downtime,
        which is what a container restart needs: the host check and all its
        service checks go red together.
        """
        now = time.time()
        form = {
            "host": host,
            "com_author": self._creds.comment_author,
            "com_data": comment,
            "trigger": "0",
            "start_time": _wall_clock(now, self._creds.timezone),
            "end_time": _wall_clock(now + minutes * 60, self._creds.timezone),
            "fixed": "1",
            "hours": str(minutes // 60),
            "minutes": str(minutes % 60),
        }
        if service:
            await self._submit(CMD_SCHEDULE_SVC_DOWNTIME, {**form, "service": service})
            return
        await self._submit(CMD_SCHEDULE_HOST_DOWNTIME, {**form, "childoptions": "0"})
        await self._submit(CMD_SCHEDULE_HOST_SVC_DOWNTIME, {**form, "childoptions": "0"})

    async def cancel_downtime(self, host: str, service: str | None = None) -> None:
        """Close the window, by finding its id first.

        ``DEL_HOST_DOWNTIME`` takes a ``down_id`` and nothing else. Sent a host
        name it answers "Sorry, but you are not authorized to commit the
        specified command" -- which reads like a credentials problem and is not
        one. Measured against the real Nagios: opening worked and closing never
        did, so every deploy left its window open and a rollback that did not
        come back would have stayed silent. That window is the only thing
        standing between a broken service and somebody finding out, and
        cancelling it is the one place this program deliberately makes noise.

        Only this program's own windows are cancelled, matched on the comment
        author. A guard against accidents rather than an authorization
        boundary -- the CGI user can already delete any downtime -- but it
        means cleaning up after a deploy cannot end a window a person opened by
        hand to work on the same service.

        Called on success, and deliberately on a rollback that did not verify.
        """
        wanted = await self._downtime_ids(host, service)
        for entry_id, is_service in wanted:
            command = CMD_DEL_SVC_DOWNTIME if is_service else CMD_DEL_HOST_DOWNTIME
            await self._submit(command, {"down_id": str(entry_id)})

    async def _downtime_ids(self, host: str, service: str | None) -> list[tuple[int, bool]]:
        """This program's downtime ids on ``host``, with whether each is a service's."""
        url = self._creds.cmd_cgi_url.rsplit("/", maxsplit=1)[0] + "/statusjson.cgi"
        params = {"query": "downtimelist", "details": "true", "hostname": host}
        try:
            response = await self._client.get(url, params=params)
        except httpx.HTTPError as exc:
            raise NagiosError(f"cannot reach Nagios at {url}: {exc}") from None
        if response.status_code >= httpx.codes.BAD_REQUEST:
            raise NagiosError(f"Nagios returned HTTP {response.status_code} for the downtime list")
        try:
            listed = response.json()
        except ValueError as exc:
            raise NagiosError(f"Nagios's downtime list was not JSON: {exc}") from None

        return my_windows(_listed_downtimes(listed), self._creds.comment_author, host, service)


def write_status(
    path: str | Path,
    outcome: str,
    detail: str,
    holds: Iterable[Hold] = (),
    now: float | None = None,
) -> Path:
    """Write the last-run record in nagios-agent's collector frame.

    The ``end=1`` line is what proves the file was written whole; a reader that
    does not find it should treat the file as unusable rather than as a report
    of nothing wrong.
    """
    stamp = time.time() if now is None else now
    lines = [f"generated={stamp:.0f} outcome={frame_field(outcome)} detail={frame_field(detail)}"]
    lines.extend(
        f"hold unit={frame_field(hold.unit)} ticket={hold.ticket} "
        f"revision={frame_field(hold.revision[:12])} since={hold.at:.0f}"
        for hold in holds
    )
    return write_frame(Path(path), STATUS_PREFIX, lines)


def _require_mapping(value: object) -> Mapping[str, object]:
    """``value`` as a mapping, or an empty one. A shape this cannot read holds
    no downtime ids, and reporting none is the same as finding none."""
    return value if isinstance(value, Mapping) else {}


def _listed_downtimes(answer: object) -> list[Mapping[str, object]]:
    """The entries out of a ``statusjson.cgi?query=downtimelist`` answer.

    Nagios gives them as a mapping keyed by id, or as a list, depending on the
    build. Both are read; any other shape holds no ids.
    """
    entries = _require_mapping(_require_mapping(answer).get("data")).get("downtimelist")
    found = list(entries.values()) if isinstance(entries, Mapping) else entries
    if not isinstance(found, list):
        return []
    return [entry for entry in found if isinstance(entry, Mapping)]


def my_windows(
    entries: Sequence[Mapping[str, object]], author: str, host: str, service: str | None
) -> list[tuple[int, bool]]:
    """``(id, is_a_service_window)`` for this program's windows on ``host``.

    Without ``service`` this takes the host's window **and its services'**,
    because that is what :meth:`NagiosCommands.open_downtime` opens: a
    container restart turns the host check and every service check red
    together.
    """
    wanted: list[tuple[int, bool]] = []
    for entry in entries:
        entry_id = entry.get("downtime_id")
        on_service = entry.get("service_description") or None
        matches = entry.get("author") == author and entry.get("host_name") == host
        if matches and isinstance(entry_id, int) and (service is None or on_service == service):
            wanted.append((entry_id, on_service is not None))
    return wanted
