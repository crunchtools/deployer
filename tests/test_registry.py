"""Registry tests.

The interesting cases are the refusals. A registry that accepts a row it does
not fully implement, or drops one it cannot parse, would make the deployer
refuse a deploy later for a reason nobody can see from the config.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from deployer.registry import (
    DEFAULT_BUILD_DEADLINE_SECONDS,
    DEFAULT_SETTLE_SECONDS,
    RegistryError,
    load_registry,
)

# Named so PLR2004 is satisfied honestly rather than silenced: these are the
# facts the fixture asserts, not incidental literals.
EXPECTED_ROWS = 2
PCLOUD_SETTLE = 90

GOOD = """
[service."acquacotta.crunchtools.com"]
repo = "crunchtools/acquacotta"
image = "quay.io/crunchtools/acquacotta"
unit = "acquacotta.crunchtools.com.service"
nagios_host = "ctr-acquacotta.crunchtools.com"
requestors = ["developer1", "developer2", "developer3", "josui"]
probe = { kind = "http", url = "http://127.0.0.1:8080/api/plugins", expect_status = 200 }

[service."mcp-pcloud.crunchtools.com"]
repo = "crunchtools/mcp-pcloud"
image = "quay.io/crunchtools/mcp-pcloud"
unit = "mcp-pcloud.crunchtools.com.service"
nagios_host = "ctr-mcp-pcloud.crunchtools.com"
requestors = ["developer1"]
settle = 90
build_deadline = 1800
rollback = "manual"
probe = { kind = "mcp", url = "http://127.0.0.1:8021/mcp" }
"""


def write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "services.toml"
    path.write_text(text, encoding="utf-8")
    return path


def test_a_good_registry_loads_both_rows(tmp_path: Path) -> None:
    reg = load_registry(write(tmp_path, GOOD))
    assert len(reg) == EXPECTED_ROWS
    assert reg.names == ("acquacotta.crunchtools.com", "mcp-pcloud.crunchtools.com")


def test_defaults_apply_where_a_row_is_silent(tmp_path: Path) -> None:
    svc = load_registry(write(tmp_path, GOOD)).get("acquacotta.crunchtools.com")
    assert svc.settle == DEFAULT_SETTLE_SECONDS
    assert svc.build_deadline == DEFAULT_BUILD_DEADLINE_SECONDS
    assert svc.rolls_back_automatically is True
    assert svc.probe.expect_body is None


def test_explicit_values_win_and_manual_rollback_is_honoured(tmp_path: Path) -> None:
    svc = load_registry(write(tmp_path, GOOD)).get("mcp-pcloud.crunchtools.com")
    assert svc.settle == PCLOUD_SETTLE
    assert svc.rolls_back_automatically is False
    assert svc.probe.kind == "mcp"


def test_an_unknown_service_is_refused_and_the_message_lists_what_there_is(tmp_path: Path) -> None:
    reg = load_registry(write(tmp_path, GOOD))
    with pytest.raises(RegistryError) as exc:
        reg.get("rootsofthevalley.org")
    message = str(exc.value)
    # Split rather than substring-match: the refusal must name what was asked
    # for AND list what is on offer, so the answer to "then what can I ask
    # for" is in the refusal itself.
    asked, _, offered = message.partition("; it holds: ")
    assert asked.endswith("is not in the registry")
    assert repr("rootsofthevalley.org") in asked
    assert tuple(offered.split(", ")) == reg.names


@pytest.mark.parametrize(
    ("key", "fragment"),
    [
        ("also_restart", "not in 0.1.0"),
        ("preflight", "capability this deployer does not hold"),
    ],
)
def test_keys_we_have_not_built_are_refused_by_name(tmp_path: Path, key: str, fragment: str) -> None:
    # Silently ignoring these would be a registry that lies about what it does.
    text = GOOD + f'\n[service."x"]\n{key} = ["y"]\n'
    with pytest.raises(RegistryError) as exc:
        load_registry(write(tmp_path, text))
    assert fragment in str(exc.value)


def test_an_unknown_key_is_a_load_error(tmp_path: Path) -> None:
    text = GOOD.replace("settle = 90", "settle = 90\nsettel = 90")
    with pytest.raises(RegistryError, match="unknown key 'settel'"):
        load_registry(write(tmp_path, text))


@pytest.mark.parametrize("missing", ["repo", "image", "unit", "probe", "nagios_host", "requestors"])
def test_every_required_key_is_required(tmp_path: Path, missing: str) -> None:
    lines = [ln for ln in GOOD.splitlines() if not ln.startswith(f"{missing} ")]
    with pytest.raises(RegistryError, match="missing required key"):
        load_registry(write(tmp_path, "\n".join(lines)))


@pytest.mark.parametrize(
    "probe",
    [
        '{ url = "http://x/y" }',
        '{ kind = "https", url = "http://x/y" }',
        '{ kind = "http" }',
        '{ kind = "http", url = "" }',
        '{ kind = "http", url = "http://x/y", expect_status = "200" }',
        '{ kind = "http", url = "http://x/y", expect_body = 3 }',
        '{ kind = "http", url = "http://x/y", expct_status = 200 }',
    ],
)
def test_a_bad_probe_is_refused(tmp_path: Path, probe: str) -> None:
    text = GOOD.split('[service."mcp-pcloud')[0].replace(
        'probe = { kind = "http", url = "http://127.0.0.1:8080/api/plugins", expect_status = 200 }',
        f"probe = {probe}",
    )
    with pytest.raises(RegistryError):
        load_registry(write(tmp_path, text))


@pytest.mark.parametrize("value", ["0", "-1", '"60"', "true"])
def test_settle_must_be_a_positive_integer(tmp_path: Path, value: str) -> None:
    with pytest.raises(RegistryError, match="settle must be a positive integer"):
        load_registry(write(tmp_path, GOOD.replace("settle = 90", f"settle = {value}")))


@pytest.mark.parametrize("value", ["[]", '"developer1"', '[""]', "[3]"])
def test_requestors_must_be_a_non_empty_list_of_names(tmp_path: Path, value: str) -> None:
    text = GOOD.replace('requestors = ["developer1"]', f"requestors = {value}")
    with pytest.raises(RegistryError, match="requestors"):
        load_registry(write(tmp_path, text))


def test_an_unknown_rollback_mode_is_refused(tmp_path: Path) -> None:
    with pytest.raises(RegistryError, match="rollback must be"):
        load_registry(write(tmp_path, GOOD.replace('rollback = "manual"', 'rollback = "sometimes"')))


def test_an_absent_file_is_a_registry_error_not_a_crash(tmp_path: Path) -> None:
    with pytest.raises(RegistryError, match="no registry at"):
        load_registry(tmp_path / "nope.toml")


def test_invalid_toml_names_the_file(tmp_path: Path) -> None:
    with pytest.raises(RegistryError, match="not valid TOML"):
        load_registry(write(tmp_path, "[service.x\n"))


def test_an_empty_registry_is_refused(tmp_path: Path) -> None:
    with pytest.raises(RegistryError, match="defines no services"):
        load_registry(write(tmp_path, "[service]\n"))


def test_a_stray_top_level_table_is_refused(tmp_path: Path) -> None:
    with pytest.raises(RegistryError, match="unknown key 'defaults'"):
        load_registry(write(tmp_path, GOOD + "\n[defaults]\nsettle = 10\n"))
