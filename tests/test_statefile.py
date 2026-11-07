"""Tests for the shared state-file helpers.

These exist once so holds and attempt records cannot drift apart again -- which
they had, one writing atomically and the other not, before Gourmand's
copy-paste check pointed at the pair.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from deployer.statefile import safe_component, write_whole


def test_a_plain_name_passes_through() -> None:
    assert safe_component("acquacotta.crunchtools.com.service", "unit") == (
        "acquacotta.crunchtools.com.service"
    )


@pytest.mark.parametrize(
    "value",
    ["", ".", "..", "../escape", "a/b", "has space", "semi;colon", "quote'", "tab\t", "null\0"],
)
def test_anything_that_could_escape_the_directory_is_refused(value: str) -> None:
    with pytest.raises(ValueError, match="not a usable"):
        safe_component(value, "unit")


def test_the_message_names_what_was_being_built(value: str = "a/b") -> None:
    with pytest.raises(ValueError, match="revision"):
        safe_component(value, "revision")


def test_writing_creates_parents_and_the_content(tmp_path: Path) -> None:
    path = write_whole(tmp_path / "deep" / "deeper" / "file", "contents")
    assert path.read_text(encoding="utf-8") == "contents"


def test_writing_leaves_no_scratch_file_behind(tmp_path: Path) -> None:
    write_whole(tmp_path / "file", "contents")
    assert [p.name for p in tmp_path.iterdir()] == ["file"]


def test_writing_twice_replaces_the_content(tmp_path: Path) -> None:
    write_whole(tmp_path / "file", "first")
    write_whole(tmp_path / "file", "second")
    assert (tmp_path / "file").read_text(encoding="utf-8") == "second"


def test_a_reader_never_sees_a_partial_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # The property that matters for a hold: an empty file would read as "this
    # service is not held" when it is.
    path = tmp_path / "file"
    seen: list[bool] = []
    real_replace = Path.replace

    def watching(self: Path, target: str | Path) -> Path:
        seen.append(path.exists())
        return real_replace(self, target)

    monkeypatch.setattr(Path, "replace", watching)
    write_whole(path, "contents")
    assert seen == [False]
    assert path.read_text(encoding="utf-8") == "contents"
