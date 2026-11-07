"""Tests for the shared validators.

These exist once so that the registry and the ticket parser do not each grow
their own near-copy — which is what they had, and what Gourmand's copy-paste
check caught.
"""

from __future__ import annotations

from typing import Any

import pytest

from deployer import validate
from deployer.validate import ValidationError

SETTLE = 60


def test_mapping_accepts_a_table_and_names_what_it_got() -> None:
    assert validate.mapping({"a": 1}, "where") == {"a": 1}
    with pytest.raises(ValidationError, match="where must be a table, got list"):
        validate.mapping([], "where")


def test_known_keys_allows_the_permitted_set() -> None:
    # Asserts the two things that matter: it does not raise on a permitted set,
    # and it only inspects -- a validator that edited its input would be a
    # surprising way to lose a config value.
    given = {"a": 1, "b": 2}
    validate.known_keys(given, ("a", "b", "c"), "where")
    assert given == {"a": 1, "b": 2}


def test_known_keys_refuses_an_unknown_key_and_lists_the_allowed() -> None:
    with pytest.raises(ValidationError) as exc:
        validate.known_keys({"z": 1}, ("a", "b"), "where")
    assert "unknown key 'z'" in str(exc.value)
    assert "a, b" in str(exc.value)


def test_known_keys_refuses_a_planned_key_with_its_reason() -> None:
    # The point of not_yet: "not in 0.1.0" is actionable, "unknown key" is not.
    with pytest.raises(ValidationError, match="not in 0.1.0"):
        validate.known_keys({"later": 1}, ("a",), "where", {"later": "not in 0.1.0"})


@pytest.mark.parametrize("value", ["", 3, None, [], True])
def test_non_empty_str_refuses_everything_else(value: Any) -> None:
    with pytest.raises(ValidationError, match="must be a non-empty string"):
        validate.non_empty_str(value, "where")


def test_non_empty_str_passes_a_string_through() -> None:
    assert validate.non_empty_str("x", "where") == "x"


def test_positive_int_passes_a_positive_integer() -> None:
    assert validate.positive_int(SETTLE, "where") == SETTLE


@pytest.mark.parametrize("value", [0, -1, "60", None, 1.5])
def test_positive_int_refuses_everything_else(value: Any) -> None:
    with pytest.raises(ValidationError, match="must be a positive integer"):
        validate.positive_int(value, "where")


@pytest.mark.parametrize("value", [True, False])
def test_positive_int_refuses_bool_even_though_it_is_an_int(value: bool) -> None:
    # True == 1, so `settle = true` would otherwise silently mean one second.
    with pytest.raises(ValidationError, match="must be a positive integer"):
        validate.positive_int(value, "where")


def test_one_of_passes_a_member_and_quotes_the_options() -> None:
    assert validate.one_of("auto", ("auto", "manual"), "where") == "auto"
    with pytest.raises(ValidationError) as exc:
        validate.one_of("sometimes", ("auto", "manual"), "where")
    assert "'auto' or 'manual'" in str(exc.value)


def test_str_list_passes_a_list_of_names() -> None:
    assert validate.str_list(["a", "b"], "where") == ("a", "b")


@pytest.mark.parametrize("value", [[], "a", [""], [3], None, [None]])
def test_str_list_refuses_everything_else(value: Any) -> None:
    with pytest.raises(ValidationError, match="where"):
        validate.str_list(value, "where")


def test_every_message_names_where_the_fault_is() -> None:
    # Without `where`, "must be a positive integer" tells nobody anything.
    with pytest.raises(ValidationError) as exc:
        validate.positive_int(0, "service.acquacotta.settle")
    assert "service.acquacotta.settle" in str(exc.value)
