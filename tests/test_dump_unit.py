"""The reference pre-deploy unit, read and run.

The pre-deploy tests drive a fake unit, so they cannot catch a mistake in the
real file. These read its directives, and run its ExecStart script against a
stub podman: the archive must be checked with `pg_restore -l` before it is
renamed, and a failed check must leave nothing at the path the deployer reads.
"""

from __future__ import annotations

import configparser
import shlex
import subprocess
from pathlib import Path

import pytest

from deployer.registry import PRE_DEPLOY_TIMEOUT_FLOOR_SECONDS

UNIT = Path(__file__).parent.parent / "deploy" / "systemd" / "rootsofthevalley.org-pg_dump@.service"
DUMPS = "/var/lib/deployer/dumps/rootsofthevalley.org"
INSTANCE = "0123456789abt1601"


def directives() -> configparser.ConfigParser:
    """The unit's sections. configparser lowercases keys; systemd's case does not matter here."""
    parser = configparser.ConfigParser(interpolation=None, strict=False)
    parser.read(UNIT, encoding="utf-8")
    return parser


def test_it_is_requisite_on_the_service_and_never_starts_it() -> None:
    unit = directives()["Unit"]
    assert unit["requisite"] == "rootsofthevalley.org.service"
    assert "requires" not in unit
    assert "wants" not in unit


def test_systemd_ends_a_hung_dump_before_the_deployer_stops_waiting() -> None:
    service = directives()["Service"]
    assert service["type"] == "oneshot"
    assert int(service["timeoutstartsec"]) <= PRE_DEPLOY_TIMEOUT_FLOOR_SECONDS


def test_it_dumps_as_the_app_owner() -> None:
    command = directives()["Service"]["execstart"]
    assert "pg_dump -Fc -U rotv -d rotv" in command


def script(tmp_path: Path) -> list[str]:
    """ExecStart with the host's paths pointed into ``tmp_path``."""
    argv = shlex.split(directives()["Service"]["execstart"])
    assert argv[:2] == ["/bin/sh", "-c"]
    body = (
        argv[2]
        .replace("%i", INSTANCE)
        .replace(DUMPS, str(tmp_path / "dumps"))
        .replace("/usr/bin/podman", str(tmp_path / "podman"))
    )
    return ["/bin/sh", "-c", body]


def stub_podman(tmp_path: Path, restore_ok: bool) -> None:
    stub = tmp_path / "podman"
    stub.write_text(
        "#!/bin/sh\n"
        'case "$*" in\n'
        '  *pg_dump*) printf "PGDMP" ;;\n'
        f"  *pg_restore*) cat >/dev/null; exit {0 if restore_ok else 1} ;;\n"
        "  *) exit 99 ;;\n"
        "esac\n",
        encoding="utf-8",
    )
    stub.chmod(0o755)
    (tmp_path / "dumps").mkdir()


@pytest.mark.parametrize("restore_ok", [True, False])
def test_the_archive_is_checked_before_it_is_published(tmp_path: Path, restore_ok: bool) -> None:
    stub_podman(tmp_path, restore_ok)
    done = subprocess.run(script(tmp_path), check=False, capture_output=True)
    final = tmp_path / "dumps" / f"{INSTANCE}.dump"
    partial = tmp_path / "dumps" / f".{INSTANCE}.partial"
    if restore_ok:
        assert done.returncode == 0
        assert final.read_bytes() == b"PGDMP"
        assert not partial.exists()
    else:
        # A failed check fails the unit and publishes nothing; the partial is
        # left for the next good run to sweep.
        assert done.returncode != 0
        assert not final.exists()
        assert partial.exists()
