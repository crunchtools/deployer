"""Parsing a Deploy ticket.

A ticket body is text an agent wrote, so it is read with a strict grammar and
nothing else: ``key: value`` lines, a fixed set of keys, and a hard refusal for
anything that does not fit. No field of it selects an image, a health check or
a command -- those come from the registry, so that a requestor cannot choose
its own proof.

One ticket carries one revision and is read once. A fix is a *new* ticket, not
an edit. That rule is what keeps this module from having to reconstruct intent
from a ticket's edit history, which would mean parsing more untrusted text to
answer "what is it asking for *now*".
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final

from . import validate
from .validate import ValidationError

__all__ = ["Request", "TicketError", "parse_ticket"]

# A full git object name. Deploying by abbreviated sha would mean resolving it
# against something, and the only thing available to resolve it against is the
# image we are deciding whether to trust.
_SHA_RE: Final = re.compile(r"\A[0-9a-f]{40}\Z")
_REPO_RE: Final = re.compile(r"\A[A-Za-z0-9._-]+/[A-Za-z0-9._-]+\Z")
_SERVICE_RE: Final = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._-]*\Z")
_LINE_RE: Final = re.compile(r"\A([a-z_]+)[ \t]*:[ \t]*(.*)\Z")

_REQUIRED: Final = ("service", "repo", "commit")
_OPTIONAL: Final = ("pr",)
_ALLOWED: Final = frozenset(_REQUIRED + _OPTIONAL)

# A body longer than this is not a deploy request. The cap is here so a
# pathological ticket cannot make this loop interesting.
MAX_BODY_BYTES: Final = 8192
MAX_LINES: Final = 64
#: No forge has issue numbers this long; beyond it the value is not a PR.
MAX_PR_DIGITS: Final = 9


class TicketError(Exception):
    """The ticket could not be understood. Always a refusal, never a deploy."""


@dataclass(frozen=True, slots=True)
class Request:
    """What one ticket asks for, once it has been believed."""

    service: str
    repo: str
    commit: str
    pr: int | None = None

    @property
    def short_commit(self) -> str:
        """The first seven characters, for a tag or a log line -- never for a lookup."""
        return self.commit[:7]


def _scan(body: str) -> dict[str, str]:
    """Pull ``key: value`` pairs out of a body, refusing anything ambiguous."""
    fields: dict[str, str] = {}
    lines = body.splitlines()
    if len(lines) > MAX_LINES:
        raise ValidationError(f"body has {len(lines)} lines; at most {MAX_LINES} are read")

    for number, line in enumerate(lines, start=1):
        text = line.strip()
        # Blank lines and a markdown fence are the two things RT and the seats
        # add without meaning anything by them.
        if not text or text.startswith(("```", "#")):
            continue

        match = _LINE_RE.match(text)
        if not match:
            raise ValidationError(f"line {number}: not a 'key: value' line: {text[:60]!r}")

        key, value = match.group(1), match.group(2).strip()
        validate.known_keys({key: value}, _ALLOWED, f"line {number}")
        if key in fields:
            # Two values for one key is exactly the ambiguity that would make
            # "which revision does this ticket mean" a judgement call.
            raise ValidationError(f"line {number}: {key!r} given twice")
        if not value:
            raise ValidationError(f"line {number}: {key!r} has no value")
        fields[key] = value

    return fields


def parse_ticket(body: str) -> Request:
    """Read a Deploy ticket body into a :class:`Request`, or refuse it.

    Raises :class:`TicketError` with a reason fit to put on the ticket.
    """
    try:
        return _parse(body)
    except ValidationError as exc:
        # One error type crosses this boundary, so a caller catches TicketError
        # and nothing else.
        raise TicketError(str(exc)) from None


def _parse(body: str) -> Request:
    if len(body.encode("utf-8")) > MAX_BODY_BYTES:
        raise ValidationError(f"body is larger than {MAX_BODY_BYTES} bytes")

    fields = _scan(body)

    absent = [key for key in _REQUIRED if key not in fields]
    if absent:
        raise ValidationError(f"missing required field(s): {', '.join(absent)}")

    service = validate.non_empty_str(fields["service"], "service")
    if not _SERVICE_RE.match(service):
        raise ValidationError(f"service {service!r} is not a plausible service name")

    repo = validate.non_empty_str(fields["repo"], "repo")
    if not _REPO_RE.match(repo):
        raise ValidationError(f"repo {repo!r} is not owner/name")

    commit = fields["commit"].lower()
    if not _SHA_RE.match(commit):
        raise ValidationError(f"commit {fields['commit']!r} must be a full 40-character sha")

    pr: int | None = None
    if "pr" in fields:
        raw = fields["pr"].lstrip("#")
        # isascii() as well as isdigit(): isdigit() is true for non-ASCII digit
        # characters, and int() accepts them, so a body could carry a PR number
        # nobody reading the ticket would recognise. The length bound keeps a
        # pathological value out of int().
        if not raw.isascii() or not raw.isdigit() or len(raw) > MAX_PR_DIGITS:
            raise ValidationError(f"pr {fields['pr']!r} must be a positive number")
        pr = validate.positive_int(int(raw), f"pr {fields['pr']!r}")

    return Request(service=service, repo=repo, commit=commit, pr=pr)


def creator_may_request(creator: str, requestors: tuple[str, ...]) -> bool:
    """Whether ``creator`` is allowed to ask for a deploy of this service.

    Every profile on the gateway reaches Request Tracker as the same identity,
    so this check is not proof of who filed the ticket -- the gateway's queue
    guard is what makes "a ticket is in Deploy" meaningful. This is the second
    half of that: the registry says which requestors a given service accepts,
    so a seat cannot deploy a service it has no business touching even once a
    ticket exists.
    """
    return creator.strip().lower() in {name.strip().lower() for name in requestors}
