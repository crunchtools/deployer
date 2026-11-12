"""The service registry: what this deployer is willing to touch, and how.

Every per-service fact lives here rather than in a ticket or in a skill's prose.
A ticket says *which* service and *which* commit; the registry says what image
that service runs, what proves it healthy, how long to watch, and who may ask.
That split is deliberate: a requestor must not be able to choose its own
success test.

Unknown keys are a load error, not a value that is ignored. The two keys most
likely to be reached for early -- ``also_restart`` and ``preflight`` -- are not
implemented in 0.1.0, and a registry that accepted them quietly would be a
registry that lied about what it was going to do.
"""

from __future__ import annotations

import ssl
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final, Literal

from . import validate
from .validate import ValidationError

__all__ = ["FOLLOWER", "NEVER_FOLLOWED", "Probe", "RegistryError", "Service", "load_registry"]

ProbeKind = Literal["http", "mcp", "log"]
RollbackMode = Literal["auto", "manual"]

DEFAULT_SETTLE_SECONDS: Final = 60
DEFAULT_BUILD_DEADLINE_SECONDS: Final = 30 * 60
DEFAULT_READY_SECONDS: Final = 60

#: The RT user ``release-follow`` files as. A row opts in to being followed by
#: listing it in ``requestors`` -- which is also what lets the runner accept the
#: ticket, so the opt-in and the authorisation cannot drift apart.
FOLLOWER: Final = "follower"
#: Services nothing may follow, whatever their row says. ``trentina`` is the
#: gateway every seat talks through and ``josui.crunchtools.com`` is the seat
#: that would be deploying it; neither should move because a timer noticed a
#: release. A ``rollback = "manual"`` row is refused too (see ``_check_follower``).
NEVER_FOLLOWED: Final = frozenset({"trentina", "josui.crunchtools.com"})

_SERVICE_KEYS: Final = frozenset(
    {
        "repo",
        "image",
        "unit",
        "probe",
        "ready",
        "settle",
        "build_deadline",
        "rollback",
        "nagios_host",
        "requestors",
    }
)
_REQUIRED_SERVICE_KEYS: Final = frozenset({"repo", "image", "unit", "probe", "nagios_host", "requestors"})
_PROBE_KEYS: Final = frozenset(
    {"kind", "url", "expect", "expect_status", "expect_body", "server_name", "ca_file"}
)
#: Which probe keys belong to which kind. A key that belongs to another kind is
#: refused by name rather than ignored: `url` on a `log` row would read as a
#: service that is being reached over HTTP when nothing of the sort happens,
#: and a silently dropped key is how a registry starts lying about its proof.
_HTTP_ONLY_PROBE_KEYS: Final = frozenset({"url", "expect_status", "expect_body", "server_name", "ca_file"})
_LOG_ONLY_PROBE_KEYS: Final = frozenset({"expect"})
#: The two keys that describe the TLS handshake rather than the request. Both
#: are meaningless without a certificate to check them against, so both are
#: refused on a URL that is not ``https``.
_TLS_PROBE_KEYS: Final = frozenset({"server_name", "ca_file"})
_HTTPS_PREFIX: Final = "https://"

# Named so the error message can say why a key is refused rather than only that
# it is unknown. Being told "not in 0.1.0" is actionable; "unknown key" invites
# a second guess at the spelling.
_NOT_YET: Final = {
    "also_restart": "restarting sibling units is not in 0.1.0",
    "preflight": "pre-flight hooks need a capability this deployer does not hold",
}


class RegistryError(Exception):
    """The registry could not be loaded. Always a "could not run", never a deploy."""


@dataclass(frozen=True, slots=True)
class Probe:
    """What proves a service healthy.

    ``http`` is a plain GET compared against ``expect_status`` and, optionally,
    a substring of the body. An ``https`` URL may also carry ``server_name``
    and ``ca_file``, which is how a TLS-terminating proxy is checked: the only
    probe expressible without them is ``http://127.0.0.1:80/`` expecting a
    redirect, which cannot see a proxy that is serving the wrong certificate.
    ``mcp`` is a JSON-RPC ``initialize`` POST, which is
    the only health signal an MCP server reliably has -- several of them expose
    no ``/health`` route at all, so an HTTP status check against one would pass
    without testing anything.

    ``log`` is for a service with no network surface at all: the agent seats
    answer nothing, and Nagios watches them with one check that says "the
    container is running". It waits for ``expect`` to appear in the container's
    own log and then holds on the container's identity rather than on any
    answer. It proves less than the other two and says so -- but the
    alternative on offer was leaving those services taking whatever ``:latest``
    held at their last restart, which proves nothing whatsoever.

    What it is deliberately *not* is a probe that cannot fail. A kind meaning
    "look at nothing" would pass for a container whose main process is
    ``sleep``, which is roughly what a Claude Code seat looks like from the
    outside, and a check that cannot fail is worse than no check because it
    gets trusted.
    """

    kind: ProbeKind
    #: Empty on a ``log`` row, where it is a refused key rather than an omitted
    #: one -- see ``_HTTP_ONLY_PROBE_KEYS``.
    url: str = ""
    #: The line that must appear in the log. ``log`` rows only.
    expect: str | None = None
    expect_status: int = 200
    expect_body: str | None = None
    #: The name this service answers to, sent as *both* the TLS SNI and the
    #: HTTP ``Host`` header. The socket still goes to the URL's host -- which
    #: on this fleet is loopback, because the deployer runs ``--network host``
    #: beside the proxy -- so this asks the local Apache for one named vhost
    #: and has OpenSSL check the certificate it answers with against that name.
    server_name: str | None = None
    #: A PEM bundle the certificate chain must verify against, in place of the
    #: system trust store. The origin certificates this fleet serves are issued
    #: by a CA no system trusts, so without this the only verifiable answer
    #: from the proxy would be "something is listening on 443".
    ca_file: str | None = None

    @property
    def reads_log(self) -> bool:
        """Whether this probe is answered by the log rather than by a request."""
        return self.kind == "log"

    @property
    def pins_tls(self) -> bool:
        """Whether this row names the identity the TLS handshake must prove.

        A row that does cannot share a client with one that does not.
        ``verify`` is a per-client setting, and httpcore keys a pooled
        connection by origin alone -- so two rows probing
        ``https://127.0.0.1:443/`` under different names on one client would
        reuse each other's handshake, and the second would be "verified"
        against the first one's name.
        """
        return self.ca_file is not None or self.server_name is not None


@dataclass(frozen=True, slots=True)
class Service:
    """One row of the registry."""

    name: str
    repo: str
    image: str
    unit: str
    probe: Probe
    nagios_host: str
    requestors: tuple[str, ...]
    #: How long the probe may go on failing after the unit comes active
    #: before the deploy is called broken. A container service is "active" as
    #: soon as conmon says the process started, which is seconds before the
    #: process inside it is listening, so this window is the gap between those
    #: two facts -- not a tolerance for a service that keeps falling over.
    ready: int = DEFAULT_READY_SECONDS
    settle: int = DEFAULT_SETTLE_SECONDS
    build_deadline: int = DEFAULT_BUILD_DEADLINE_SECONDS
    rollback: RollbackMode = "auto"

    @property
    def container(self) -> str:
        """The podman container's name, which is the registry key.

        Deliberately not derived from the unit name. On this fleet the two
        often match -- ``acquacotta.crunchtools.com.service`` runs
        ``acquacotta.crunchtools.com`` -- and often do not:
        ``mcp-pcloud.crunchtools.com.service`` runs a container called
        ``mcp-pcloud``. So the key *is* the container and the unit is a
        separate field, and seeding a row means reading
        ``podman inspect --format '{{.Name}}'`` rather than assuming.
        """
        return self.name

    @property
    def rolls_back_automatically(self) -> bool:
        return self.rollback == "auto"


@dataclass(frozen=True, slots=True)
class Registry:
    """Every service this deployer will act on, by name."""

    services: Mapping[str, Service] = field(default_factory=dict)

    def get(self, name: str) -> Service:
        """The service by name, or ``RegistryError`` naming what is on offer.

        A service that is absent is refused here rather than part-way through a
        deploy, and the message lists the registry so the answer to "then what
        can I ask for" is in the refusal.
        """
        try:
            return self.services[name]
        except KeyError:
            known = ", ".join(sorted(self.services)) or "nothing"
            raise RegistryError(f"service {name!r} is not in the registry; it holds: {known}") from None

    @property
    def names(self) -> tuple[str, ...]:
        """Every service name, sorted. The set a caller may ask for."""
        return tuple(sorted(self.services))

    def __len__(self) -> int:
        return len(self.services)


def _refuse_keys_of_the_other_kind(table: dict[str, Any], kind: ProbeKind, where: str) -> None:
    """Refuse probe keys that belong to a different kind than the row declares.

    Ignoring them would be worse than refusing: a `log` row carrying a `url`
    reads as a service being reached over HTTP when nothing of the sort
    happens, and the row would be claiming a proof it is not performing.
    """
    wrong = sorted((_HTTP_ONLY_PROBE_KEYS if kind == "log" else _LOG_ONLY_PROBE_KEYS) & table.keys())
    if not wrong:
        return
    why = "a log probe reaches nothing over the network" if kind == "log" else "expect belongs to a log probe"
    raise ValidationError(f"{where}.probe has {', '.join(wrong)} but kind is {kind!r}: {why}")


def _optional_str(table: dict[str, Any], key: str, where: str) -> str | None:
    """A probe key that may be absent but must not be empty when it is there."""
    value = table.get(key)
    return None if value is None else validate.non_empty_str(value, f"{where}.probe.{key}")


def _parse_tls(table: dict[str, Any], url: str, where: str) -> tuple[str | None, str | None]:
    """``server_name`` and ``ca_file``, refused at load rather than at deploy.

    Both are refused on a URL that is not ``https``, by name and with the
    reason, for the same argument as ``_refuse_keys_of_the_other_kind``: a row
    carrying them over plaintext claims a certificate was checked when no
    certificate was ever offered.

    A ``ca_file`` is checked by *loading* it here, not by stat'ing it. Both
    outcomes are the registry being wrong rather than the service being
    unhealthy, and the constitution gives those different exit codes: an
    unloadable registry is exit 2, "could not run", where a failed probe is
    exit 1 and takes a rollback and a ticket with it. A bundle that is absent
    or is not PEM would otherwise reach the probe and be reported as the
    service failing, in a maintenance window, having spent one.
    """
    wrong = sorted(_TLS_PROBE_KEYS & table.keys())
    if wrong and not url.startswith(_HTTPS_PREFIX):
        raise ValidationError(
            f"{where}.probe has {', '.join(wrong)} but {url!r} is not https: "
            "there is no certificate to verify on a plaintext URL"
        )
    ca_file = _optional_str(table, "ca_file", where)
    if ca_file is not None:
        if not Path(ca_file).is_file():
            raise ValidationError(f"{where}.probe.ca_file {ca_file!r} is not a file this deployer can read")
        try:
            ssl.create_default_context(cafile=ca_file)
        except (OSError, ssl.SSLError) as exc:
            raise ValidationError(
                f"{where}.probe.ca_file {ca_file!r} is not a bundle OpenSSL will load: {exc}"
            ) from None
    return _optional_str(table, "server_name", where), ca_file


def _parse_probe(raw: Any, where: str) -> Probe:
    table = validate.mapping(raw, f"{where}.probe")
    validate.known_keys(table, _PROBE_KEYS, f"{where}.probe")
    kind_name = validate.one_of(table.get("kind"), ("http", "mcp", "log"), f"{where}.probe.kind")
    # Narrowed by hand rather than cast: one_of guarantees membership, but its
    # return type is str, and a cast here would be the same suppression in a
    # longer spelling.
    kind: ProbeKind
    if kind_name == "http":
        kind = "http"
    elif kind_name == "mcp":
        kind = "mcp"
    else:
        kind = "log"

    _refuse_keys_of_the_other_kind(table, kind, where)

    if kind == "log":
        expect = validate.non_empty_str(table.get("expect"), f"{where}.probe.expect")
        return Probe(kind=kind, expect=expect)

    url = validate.non_empty_str(table.get("url"), f"{where}.probe.url")
    status = validate.positive_int(table.get("expect_status", 200), f"{where}.probe.expect_status")
    body = table.get("expect_body")
    if body is not None and not isinstance(body, str):
        raise ValidationError(f"{where}.probe.expect_body must be a string when present")
    server_name, ca_file = _parse_tls(table, url, where)
    return Probe(
        kind=kind,
        url=url,
        expect_status=status,
        expect_body=body,
        server_name=server_name,
        ca_file=ca_file,
    )


def _check_follower(name: str, requestors: tuple[str, ...], rollback: RollbackMode, where: str) -> None:
    """Refuse ``follower`` on a row that must never deploy unattended.

    A load error rather than a skip inside ``release-follow``: the runner reads
    the same file, and a row that lists ``follower`` is a row whose follower
    tickets the runner would ACCEPT. Refusing the whole registry stops both
    halves -- the filer and the deployer -- until the row is fixed.

    ``rollback = "manual"`` is the registry's own word for "a failed deploy
    needs a person" (migrations, state a newer version may have written), and a
    deploy nobody asked for is exactly the one nobody is watching.
    """
    if FOLLOWER not in {who.strip().lower() for who in requestors}:
        return
    if name in NEVER_FOLLOWED:
        raise ValidationError(f"{where}.requestors lists {FOLLOWER!r}, but {name} is never followed")
    if rollback != "auto":
        raise ValidationError(
            f"{where}.requestors lists {FOLLOWER!r}, but rollback is {rollback!r}: a service whose "
            "failed deploy needs a person is not deployed by a timer"
        )


def _parse_service(name: str, raw: Any) -> Service:
    where = f"service.{name}"
    table = validate.mapping(raw, where)
    validate.known_keys(table, _SERVICE_KEYS, where, _NOT_YET)

    absent = sorted(_REQUIRED_SERVICE_KEYS - table.keys())
    if absent:
        raise ValidationError(f"{where}: missing required key(s): {', '.join(absent)}")

    mode = validate.one_of(table.get("rollback", "auto"), ("auto", "manual"), f"{where}.rollback")
    rollback: RollbackMode = "auto" if mode == "auto" else "manual"
    requestors = validate.str_list(table.get("requestors"), f"{where}.requestors")
    _check_follower(name, requestors, rollback, where)
    return Service(
        name=name,
        repo=validate.non_empty_str(table["repo"], f"{where}.repo"),
        image=validate.non_empty_str(table["image"], f"{where}.image"),
        unit=validate.non_empty_str(table["unit"], f"{where}.unit"),
        probe=_parse_probe(table["probe"], where),
        nagios_host=validate.non_empty_str(table["nagios_host"], f"{where}.nagios_host"),
        requestors=requestors,
        ready=validate.positive_int(table.get("ready", DEFAULT_READY_SECONDS), f"{where}.ready"),
        settle=validate.positive_int(table.get("settle", DEFAULT_SETTLE_SECONDS), f"{where}.settle"),
        build_deadline=validate.positive_int(
            table.get("build_deadline", DEFAULT_BUILD_DEADLINE_SECONDS), f"{where}.build_deadline"
        ),
        rollback=rollback,
    )


def load_registry(path: str | Path) -> Registry:
    """Read and validate the registry at ``path``.

    Raises :class:`RegistryError` for anything wrong, including a file that is
    absent or unreadable. Nothing here half-loads: either every row is good or
    the caller gets a refusal, because a registry with one silently dropped row
    is a registry that will refuse a deploy for no visible reason.
    """
    file = Path(path)
    try:
        raw = tomllib.loads(file.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise RegistryError(f"no registry at {file}") from None
    except OSError as exc:
        raise RegistryError(f"cannot read the registry at {file}: {exc}") from None
    except tomllib.TOMLDecodeError as exc:
        raise RegistryError(f"{file} is not valid TOML: {exc}") from None

    try:
        validate.known_keys(raw, ("service",), str(file))
        table = validate.mapping(raw.get("service", {}), f"{file}: service")
        if not table:
            raise ValidationError(f"{file} defines no services")
        services = {name: _parse_service(name, row) for name, row in table.items()}
    except ValidationError as exc:
        # One error type crosses this boundary, so a caller catches RegistryError
        # and nothing else.
        raise RegistryError(str(exc)) from None

    return Registry(services=services)
