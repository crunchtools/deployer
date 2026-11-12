"""The status-file frame: every value must stay one field on one line."""

from __future__ import annotations

from pathlib import Path

import pytest

from deployer.statefile import frame_field, write_frame

FIELD_LIMIT = 80


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("plain", "plain"),
        ("two words", "two_words"),
        ("a|b", "a/b"),
        ("tab\there\nnewline", "tabherenewline"),
        ("café ☃", "caf_"),
        ("", "-"),
        ("\x00\x1f", "-"),
    ],
)
def test_frame_field_is_one_printable_field(raw: str, expected: str) -> None:
    assert frame_field(raw) == expected


def test_frame_field_is_bounded() -> None:
    assert frame_field("x" * 500) == "x" * FIELD_LIMIT


def test_a_frame_prefixes_every_line_and_ends_whole(tmp_path: Path) -> None:
    path = write_frame(tmp_path / "s", "P", ["a=1", f"b={frame_field('x y|z')}"])
    assert path.read_text().splitlines() == ["P a=1", "P b=x_y/z", "P end=1"]
