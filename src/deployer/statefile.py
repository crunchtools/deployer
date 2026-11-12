"""Writing and naming the small files this program keeps as state.

Holds and attempt records are both "a short text file under the state
directory, named after a service or a revision". That was written twice before
Gourmand's copy-paste check pointed it out, and the two copies had already
drifted: one wrote atomically and one did not.

Both now go through here, so every state file this program writes is written
whole and named through the same check.
"""

from __future__ import annotations

from pathlib import Path
from typing import Final

__all__ = ["frame_field", "safe_component", "write_frame", "write_whole"]

#: What may appear in a path component this program builds.
_SAFE: Final = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-")


def safe_component(value: str, what: str) -> str:
    """``value`` as a single path component, or a ``ValueError``.

    Service names come from the registry and revisions from an image label, so
    neither is chosen by whoever filed a ticket. The check is here anyway: "not
    attacker-controlled today" is not a property to build a file path on, and a
    separator would write outside the directory this program believes it owns.
    """
    if not value or value in (".", "..") or any(char not in _SAFE for char in value):
        raise ValueError(f"not a usable {what} for a filename: {value!r}")
    return value


def write_whole(path: Path, text: str) -> Path:
    """Write ``text`` to ``path``, creating parents, visible only when complete.

    Through a scratch file and a rename, so a reader never sees a half-written
    file. For a hold that matters directly: a reader seeing an empty file could
    conclude a service is not held when it is.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    scratch = path.with_name(f".{path.name}.new")
    scratch.write_text(text, encoding="utf-8")
    scratch.replace(path)
    return path


def frame_field(text: str) -> str:
    """Printable ASCII, no spaces or pipes, at most 80 characters.

    The same shape as ``cl_clean`` in nagios-agent's collector-lib.sh: status
    files are space-separated lines, so a space in a value would be read as a
    new field.
    """
    kept = "".join(char for char in text if "\040" <= char <= "\176")
    return kept.replace(" ", "_").replace("|", "/")[:80] or "-"


def write_frame(path: Path, prefix: str, lines: list[str]) -> Path:
    """A status file in nagios-agent's collector frame, written whole.

    Every line carries ``prefix``, and the last is ``<prefix> end=1``: a reader
    that does not find it treats the file as unusable rather than as a report
    of nothing wrong.
    """
    framed = [f"{prefix} {line}" for line in lines] + [f"{prefix} end=1"]
    return write_whole(path, "\n".join(framed) + "\n")
