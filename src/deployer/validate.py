"""Small validators shared by the registry and the ticket parser.

Both of those read input that must not be half-believed -- a config file and a
body an agent wrote -- and both ended up with the same shape: check a field,
raise with a message naming where the fault is. This module is that shape,
once.

Every validator raises :class:`ValidationError`. The callers catch it at their
own entry point and re-raise it as their own error type, so
``RegistryError`` and ``TicketError`` stay the contract a caller catches while
the checking lives in one place.

The ``where`` argument is a dotted path into whatever is being read
(``service.acquacotta.probe.url``, ``line 3``). It is the whole value of these
messages: "must be a positive integer" is useless without it.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

__all__ = [
    "ValidationError",
    "known_keys",
    "mapping",
    "non_empty_str",
    "one_of",
    "positive_int",
    "str_list",
]


class ValidationError(Exception):
    """A field did not hold. Carries a message fit to show whoever wrote the input."""


def mapping(value: Any, where: str) -> dict[str, Any]:
    """``value`` as a table, or raise."""
    if not isinstance(value, dict):
        raise ValidationError(f"{where} must be a table, got {type(value).__name__}")
    return value


def known_keys(
    given: Mapping[str, Any],
    allowed: Iterable[str],
    where: str,
    not_yet: Mapping[str, str] | None = None,
) -> None:
    """Refuse any key outside ``allowed``.

    ``not_yet`` names keys that are refused *with a reason* -- a field that is
    planned but not built. Saying "not in 0.1.0" is actionable, where "unknown
    key" invites a second guess at the spelling.
    """
    permitted = frozenset(allowed)
    reasons = not_yet or {}
    for key in sorted(given):
        if key in permitted:
            continue
        reason = reasons.get(key)
        if reason:
            raise ValidationError(f"{where}: {key!r} is not accepted -- {reason}")
        raise ValidationError(f"{where}: unknown key {key!r}; allowed: {', '.join(sorted(permitted))}")


def non_empty_str(value: Any, where: str) -> str:
    """``value`` as a non-empty string, or raise."""
    if not isinstance(value, str) or not value:
        raise ValidationError(f"{where} must be a non-empty string")
    return value


def positive_int(value: Any, where: str) -> int:
    """``value`` as a positive integer, or raise.

    ``bool`` is excluded on purpose: ``True`` is an ``int`` of value 1 in
    Python, and a config that said ``settle = true`` meaning something would be
    read as one second.
    """
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ValidationError(f"{where} must be a positive integer, got {value!r}")
    return value


def one_of(value: Any, options: Iterable[str], where: str) -> str:
    """``value`` as one of ``options``, or raise."""
    allowed = tuple(options)
    if not isinstance(value, str) or value not in allowed:
        quoted = " or ".join(repr(option) for option in allowed)
        raise ValidationError(f"{where} must be {quoted}, got {value!r}")
    return value


def str_list(value: Any, where: str) -> tuple[str, ...]:
    """``value`` as a non-empty tuple of non-empty strings, or raise."""
    if not isinstance(value, list) or not value:
        raise ValidationError(f"{where} must be a non-empty list of strings")
    for item in value:
        if not isinstance(item, str) or not item:
            raise ValidationError(f"{where} must hold non-empty strings, got {item!r}")
    return tuple(value)
