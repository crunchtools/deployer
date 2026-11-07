"""One run at a time.

``run`` is started by a timer every couple of minutes while a deploy takes a
minute of settle plus however long a pull runs. Two overlapping runs would
stage the same service twice and each would restore ``:latest`` from under the
other, so the second one has to stand down.

Standing down is **not an error**. The caller that started it is a oneshot unit
whose failure wakes a model, and "the previous run is still going" is the most
ordinary thing that can happen here.
"""

from __future__ import annotations

import fcntl
import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

__all__ = ["AlreadyRunning", "LOCK_NAME", "only_one"]

LOCK_NAME = "run.lock"


class AlreadyRunning(Exception):
    """Another run holds the lock. Expected, and not a failure."""


@contextmanager
def only_one(state_dir: str | Path) -> Iterator[None]:
    """Hold an exclusive advisory lock on ``<state_dir>/run.lock``.

    ``flock`` and not a pid file: the kernel drops the lock when the process
    goes, however it goes, so a run killed mid-deploy does not leave a stale
    file that blocks every run afterwards.

    The lock file is never unlinked. Unlinking it races -- a second process can
    be holding a lock on an inode that no longer has a name, and a third would
    then create a new file and take a second lock on the same state directory.
    """
    path = Path(state_dir) / LOCK_NAME
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = os.open(path, os.O_CREAT | os.O_WRONLY | os.O_CLOEXEC, 0o600)
    try:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise AlreadyRunning(f"another deployer run holds {path}") from exc
        yield
    finally:
        # Closing releases the lock. Doing it in a finally rather than relying
        # on process exit matters for the tests, which take the lock twice in
        # one interpreter.
        os.close(handle)
