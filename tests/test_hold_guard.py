"""The auto-update hold guard, exercised as the shell command it actually is.

The guard is one `ExecCondition` line in a systemd drop-in, and it is the only
thing standing between a rolled-back image and its return at the next
fleet-wide auto-update. Nothing in this package imports it, so without this
file the single most consequential line of configuration the program ships
would be the one line nobody tests.

What is pinned is the **published** text: the condition is read out of the
drop-in under `deploy/systemd/` rather than written out again here, so a typo
in the file that gets deployed fails these tests instead of passing them. Only
the state directory is substituted, because the real one is a host path.

What is NOT pinned is systemd's half of the contract -- that a failing
`ExecCondition` skips the unit with `Result=success` rather than failing it.
That is systemd's behaviour, not this program's, and it was measured on the
host with a throwaway unit before the real guard was installed anywhere.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

DROP_IN = Path(__file__).resolve().parent.parent / "deploy" / "systemd" / "50-deployer-hold.conf"
#: The path the published condition names, replaced with a temporary one.
DEPLOYED_HOLD_DIR = "/var/lib/deployer/hold"
_EXEC_CONDITION = re.compile(r"^ExecCondition=/bin/sh -c '(?P<script>.*)'\s*$", re.M)


def condition(hold_dir: Path) -> str:
    """The shell script the deployed drop-in runs, pointed at ``hold_dir``."""
    found = _EXEC_CONDITION.search(DROP_IN.read_text(encoding="utf-8"))
    if found is None:
        pytest.fail(f"{DROP_IN.name} has no ExecCondition=/bin/sh -c '...' line to test")
    script = found.group("script")
    if DEPLOYED_HOLD_DIR not in script:
        pytest.fail(f"{DROP_IN.name}'s condition no longer names {DEPLOYED_HOLD_DIR}; this test is stale")
    return script.replace(DEPLOYED_HOLD_DIR, str(hold_dir))


def run(hold_dir: Path) -> int:
    return subprocess.run(
        ["/bin/sh", "-c", condition(hold_dir)],
        capture_output=True,
        check=False,
    ).returncode


def test_an_empty_hold_directory_lets_the_update_run(tmp_path: Path) -> None:
    (tmp_path / "hold").mkdir()
    assert run(tmp_path / "hold") == 0


def test_an_absent_hold_directory_lets_the_update_run(tmp_path: Path) -> None:
    # The normal state of a host that has never rolled anything back. `find`
    # writes to stderr and exits non-zero for a missing directory, which is why
    # the condition discards its stderr and tests its OUTPUT instead -- reading
    # find's exit status here would freeze every host that has no hold
    # directory, which is all of them until the first rollback.
    assert run(tmp_path / "never-created") == 0


def test_one_hold_file_stops_the_update(tmp_path: Path) -> None:
    hold = tmp_path / "hold"
    hold.mkdir()
    (hold / "mcp-pcloud.crunchtools.com.service").write_text('{"ticket": 1571}', encoding="utf-8")
    assert run(hold) != 0


def test_a_hold_in_a_subdirectory_is_not_a_hold(tmp_path: Path) -> None:
    # -mindepth 1 -maxdepth 1 -type f: holds are files directly in the
    # directory. Without the depth bounds, `find` would walk into anything
    # nested and a stray file in a subdirectory would freeze the fleet.
    hold = tmp_path / "hold"
    (hold / "nested").mkdir(parents=True)
    (hold / "nested" / "not-a-hold").write_text("x", encoding="utf-8")
    assert run(hold) == 0


def test_a_directory_named_like_a_unit_is_not_a_hold(tmp_path: Path) -> None:
    hold = tmp_path / "hold"
    (hold / "acquacotta.crunchtools.com.service").mkdir(parents=True)
    assert run(hold) == 0


def test_an_in_flight_write_errs_towards_blocking(tmp_path: Path) -> None:
    """A dotfile counts here, and deliberately does not in check_deployer.sh.

    The hold is written to a dot-prefixed scratch file and renamed. The Nagios
    check skips those, because reporting a half-written hold as a hold would
    page somebody for a file that exists for a millisecond. This condition does
    not skip them, and that asymmetry is the right way round: the check's error
    is a false alarm, and this one's is a fleet update deferred by two minutes
    until the next timer. Blocking an update that was about to happen anyway is
    the cheap mistake; letting a rolled-back image back onto a host is not.
    """
    hold = tmp_path / "hold"
    hold.mkdir()
    (hold / ".mcp-pcloud.crunchtools.com.service.tmp").write_text("{", encoding="utf-8")
    assert run(hold) != 0
