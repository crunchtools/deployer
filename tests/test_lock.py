"""The run lock.

The property worth pinning: a second run stands down rather than failing. The
unit that starts it is a oneshot whose failure wakes a model, and "the previous
run is still going" is the most ordinary thing that can happen.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from deployer.lock import LOCK_NAME, AlreadyRunning, only_one


def test_the_lock_is_taken_and_released(tmp_path: Path) -> None:
    with only_one(tmp_path):
        pass
    # Taking it twice in sequence works, so the first one really let go.
    with only_one(tmp_path):
        pass
    assert (tmp_path / LOCK_NAME).is_file()


def test_a_second_holder_is_refused(tmp_path: Path) -> None:
    with only_one(tmp_path), pytest.raises(AlreadyRunning, match=LOCK_NAME):
        with only_one(tmp_path):
            pass  # pragma: no cover


def test_the_lock_is_released_even_when_the_body_raises(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="deploy blew up"):
        with only_one(tmp_path):
            raise ValueError("deploy blew up")
    # Would raise AlreadyRunning if the lock had leaked.
    with only_one(tmp_path):
        pass


def test_the_state_directory_is_created(tmp_path: Path) -> None:
    deeper = tmp_path / "var" / "lib" / "deployer"
    with only_one(deeper):
        pass
    assert (deeper / LOCK_NAME).is_file()


def test_the_lock_file_is_not_unlinked(tmp_path: Path) -> None:
    # Unlinking races: a second process can hold a lock on an inode with no
    # name, and a third then creates a new file and takes a second lock on the
    # same state directory.
    with only_one(tmp_path):
        inode = (tmp_path / LOCK_NAME).stat().st_ino
    assert (tmp_path / LOCK_NAME).stat().st_ino == inode


def test_the_lock_file_is_private(tmp_path: Path) -> None:
    with only_one(tmp_path):
        mode = (tmp_path / LOCK_NAME).stat().st_mode
    assert not mode & 0o077


def test_a_stale_lock_file_does_not_block_a_run(tmp_path: Path) -> None:
    # The kernel drops a flock when the process goes, however it goes, which is
    # the whole reason this is not a pid file: a run killed mid-deploy must not
    # leave a file that blocks every run afterwards.
    handle = os.open(tmp_path / LOCK_NAME, os.O_CREAT | os.O_WRONLY, 0o600)
    os.close(handle)
    taken = False
    with only_one(tmp_path):
        taken = True
    assert taken
