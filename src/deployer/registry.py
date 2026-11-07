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

import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final, Literal

from . import validate
from .validate import ValidationError

__all__ = ["Probe", "RegistryError", "Service", "load_registry"]

ProbeKind = Literal["http", "mcp"]
RollbackMode = Literal["auto", "manual"]

DEFAULT_SETTLE_SECONDS: Final = 60
DEFAULT_BUILD_DEADLINE_SECONDS: Final = 30 * 60

_SERVICE_KEYS: Final = frozenset(
    {
        "repo",
        "image",
        "unit",
        "probe",
        "settle",
        "build_deadline",
        "rollback",
        "nagios_host",
        "requestors",
    }
)
_REQUIRED_SERVICE_KEYS: Final = frozenset({"repo", "image", "unit", "probe", "nagios_host", "requestors"})
_PROBE_KEYS: Final = frozenset({"kind", "url", "expect_status", "expect_body"})

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
    a substring of the body. ``mcp`` is a JSON-RPC ``initialize`` POST, which is
    the only health signal an MCP server reliably has -- several of them expose
    no ``/health`` route at all, so an HTTP status check against one would pass
    without testing anything.
    """

    kind: ProbeKind
    url: str
    expect_status: int = 200
    expect_body: str | None = None


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
    settle: int = DEFAULT_SETTLE_SECONDS
    build_deadline: int = DEFAULT_BUILD_DEADLINE_SECONDS
    rollback: RollbackMode = "auto"

    @property
    def container(self) -> str:
        """The podman container's name.

        On this fleet a unit ``x.service`` runs a container named ``x``, which
        is the registry key. Named here rather than derived at each call site so
        the assumption is in one place if a service ever breaks it.
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


def _parse_probe(raw: Any, where: str) -> Probe:
    table = validate.mapping(raw, f"{where}.probe")
    validate.known_keys(table, _PROBE_KEYS, f"{where}.probe")
    kind_name = validate.one_of(table.get("kind"), ("http", "mcp"), f"{where}.probe.kind")
    # Narrowed by hand rather than cast: one_of guarantees membership, but its
    # return type is str, and a cast here would be the same suppression in a
    # longer spelling.
    kind: ProbeKind = "http" if kind_name == "http" else "mcp"
    url = validate.non_empty_str(table.get("url"), f"{where}.probe.url")
    status = validate.positive_int(table.get("expect_status", 200), f"{where}.probe.expect_status")
    body = table.get("expect_body")
    if body is not None and not isinstance(body, str):
        raise ValidationError(f"{where}.probe.expect_body must be a string when present")
    return Probe(kind=kind, url=url, expect_status=status, expect_body=body)


def _parse_service(name: str, raw: Any) -> Service:
    where = f"service.{name}"
    table = validate.mapping(raw, where)
    validate.known_keys(table, _SERVICE_KEYS, where, _NOT_YET)

    absent = sorted(_REQUIRED_SERVICE_KEYS - table.keys())
    if absent:
        raise ValidationError(f"{where}: missing required key(s): {', '.join(absent)}")

    mode = validate.one_of(table.get("rollback", "auto"), ("auto", "manual"), f"{where}.rollback")
    rollback: RollbackMode = "auto" if mode == "auto" else "manual"
    return Service(
        name=name,
        repo=validate.non_empty_str(table["repo"], f"{where}.repo"),
        image=validate.non_empty_str(table["image"], f"{where}.image"),
        unit=validate.non_empty_str(table["unit"], f"{where}.unit"),
        probe=_parse_probe(table["probe"], where),
        nagios_host=validate.non_empty_str(table["nagios_host"], f"{where}.nagios_host"),
        requestors=validate.str_list(table.get("requestors"), f"{where}.requestors"),
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
