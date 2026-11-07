"""What has already been tried, so it is not tried again.

Two records, and neither is kept per ticket. A ticket carries one revision and
is read once, so counting attempts against a ticket would count a thing that
cannot vary; what matters is the *revision* and the *service*.

``rolled-back/<service>/<revision>``
    A revision that was started and failed to verify. Never deployed again, by
    this program, ever. Not a counter and not a window -- an image that broke a
    service does not become safe because time passed.

``rollbacks.log``
    One line per rollback. Three for one service inside a day stop that service
    until a person runs ``deployer unhold``. The first rollback is the system
    working; the third in a day means something is wrong that redeploying will
    not fix, and continuing to restart production while a person is asleep is
    the failure mode this bound exists to prevent.

The asymmetry is deliberate: a bad revision is refused forever, a struggling
service is refused until someone looks.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Final

from .statefile import safe_component, write_whole

__all__ = ["ROLLBACK_LIMIT", "ROLLBACK_WINDOW_SECONDS", "AttemptLog"]

#: Rollbacks of one service within the window before it is refused.
ROLLBACK_LIMIT: Final = 3
ROLLBACK_WINDOW_SECONDS: Final = 24 * 60 * 60


class AttemptLog:
    """Both records, under one state directory."""

    def __init__(self, state_dir: str | Path) -> None:
        root = Path(state_dir)
        self._rolled_back = root / "rolled-back"
        self._log = root / "rollbacks.log"

    # --- revisions that broke something ---

    def _revision_path(self, service: str, revision: str) -> Path:
        return (
            self._rolled_back
            / safe_component(service, "service")
            / safe_component(revision.lower(), "revision")
        )

    def mark_rolled_back(self, service: str, revision: str) -> None:
        """Record that ``revision`` was started on ``service`` and failed.

        Only ever called after a revision actually ran. A revision refused
        before it started -- no matching image yet, the wrong unit shape, an
        unlisted requestor -- is not recorded, because nothing was learned about
        the image itself and the next ticket for it should get a fair hearing.
        """
        write_whole(self._revision_path(service, revision), f"{time.time()}\n")

    def was_rolled_back(self, service: str, revision: str) -> bool:
        """Whether this revision has already broken this service."""
        return self._revision_path(service, revision).exists()

    # --- how often a service has needed rolling back ---

    def record_rollback(self, service: str, when: float | None = None) -> None:
        """Append one rollback for ``service`` to the log."""
        self._log.parent.mkdir(parents=True, exist_ok=True)
        stamp = time.time() if when is None else when
        with self._log.open("a", encoding="utf-8") as handle:
            handle.write(f"{stamp}\t{safe_component(service, 'service')}\n")

    def recent_rollbacks(self, service: str, within: float = ROLLBACK_WINDOW_SECONDS) -> int:
        """How many times ``service`` has been rolled back inside ``within``."""
        cutoff = time.time() - within
        count = 0
        try:
            lines = self._log.read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            return 0
        for line in lines:
            stamp, _, name = line.partition("\t")
            if name.strip() != service:
                continue
            try:
                at = float(stamp)
            except ValueError:
                # A line this version cannot read is not a reason to under-count
                # and let a fourth rollback through, so it counts.
                count += 1
                continue
            if at >= cutoff:
                count += 1
        return count

    def too_many_rollbacks(
        self,
        service: str,
        limit: int = ROLLBACK_LIMIT,
        within: float = ROLLBACK_WINDOW_SECONDS,
    ) -> bool:
        """Whether ``service`` has been rolled back too often to keep trying."""
        return self.recent_rollbacks(service, within) >= limit

    def forget_rollbacks(self, service: str) -> int:
        """Drop ``service``'s rollbacks from the log. Returns how many went.

        What ``deployer unhold`` calls, and the only thing that clears the
        three-in-24-hours refusal. Without it a service that hit the limit
        would stay refused for a whole day even after the fix shipped, and the
        person who fixed it would have no way to say so.

        Other services' lines are rewritten untouched, so unholding one service
        does not reset the count for another.
        """
        wanted = safe_component(service, "service")
        try:
            lines = self._log.read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            return 0
        kept = [line for line in lines if line.partition("\t")[2].strip() != wanted]
        gone = len(lines) - len(kept)
        if gone:
            write_whole(self._log, "".join(f"{line}\n" for line in kept))
        return gone
