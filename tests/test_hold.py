"""Hold tests.

The two that carry weight: no ticket runs when the guard is absent, and a hold
whose contents are unreadable is still a hold. Losing the detail must not lose
the protection, because the file's existence is what the guard tests.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from deployer.hold import Hold, HoldStore, MissingGuardError, require_guard

UNIT = "acquacotta.crunchtools.com.service"
SECOND_TICKET = 2
AGE_SECONDS = 60
TICKET = 1234
REVISION = "635cdda3c8632d1b5aed4cc22943895205ce5138"


class FakeSystemd:
    def __init__(self, installed: bool) -> None:
        self._installed = installed

    async def hold_guard_installed(self) -> bool:
        return self._installed


def a_hold(
    unit: str = UNIT,
    ticket: int = TICKET,
    at: float | None = None,
) -> Hold:
    """A hold with the fields a test cares about, typed rather than kwargs."""
    if at is None:
        return Hold(unit=unit, service="acquacotta.crunchtools.com", ticket=ticket, revision=REVISION)
    return Hold(unit=unit, service="acquacotta.crunchtools.com", ticket=ticket, revision=REVISION, at=at)


async def test_the_guard_being_installed_lets_a_run_proceed() -> None:
    await require_guard(FakeSystemd(installed=True))


async def test_no_guard_means_no_ticket_runs() -> None:
    # A hold that nothing honours is worse than no hold: it reads as protection
    # that is not there, and the next auto-update undoes the rollback.
    with pytest.raises(MissingGuardError) as exc:
        await require_guard(FakeSystemd(installed=False))
    assert "would not be honoured" in str(exc.value)
    assert "50-deployer-hold.conf" in str(exc.value)


def test_a_hold_round_trips(tmp_path: Path) -> None:
    store = HoldStore(tmp_path)
    store.place(a_hold())
    read = store.get(UNIT)
    assert read is not None
    assert read.ticket == TICKET
    assert read.revision == REVISION
    assert read.service == "acquacotta.crunchtools.com"


def test_an_absent_hold_reads_as_none(tmp_path: Path) -> None:
    store = HoldStore(tmp_path)
    assert store.get(UNIT) is None
    assert not store.held(UNIT)
    assert len(store) == 0


def test_placing_a_hold_marks_the_unit_held(tmp_path: Path) -> None:
    store = HoldStore(tmp_path)
    store.place(a_hold())
    assert store.held(UNIT)
    assert len(store) == 1


def test_clearing_says_whether_there_was_one(tmp_path: Path) -> None:
    store = HoldStore(tmp_path)
    assert store.clear(UNIT) is False
    store.place(a_hold())
    assert store.clear(UNIT) is True
    assert not store.held(UNIT)


def test_a_second_hold_on_one_unit_replaces_the_first(tmp_path: Path) -> None:
    store = HoldStore(tmp_path)
    store.place(a_hold(ticket=1))
    store.place(a_hold(ticket=SECOND_TICKET))
    read = store.get(UNIT)
    assert read is not None
    assert read.ticket == SECOND_TICKET
    assert len(store) == 1


def test_an_unreadable_hold_is_still_a_hold(tmp_path: Path) -> None:
    # The file's existence is what the auto-update guard tests, so a file this
    # version cannot parse must not read as "not held".
    store = HoldStore(tmp_path)
    store.place(a_hold())
    (tmp_path / "hold" / UNIT).write_text("not json at all", encoding="utf-8")
    assert store.held(UNIT)
    read = store.get(UNIT)
    assert read is not None
    assert read.unit == UNIT
    assert read.ticket == 0


def test_a_hold_with_the_wrong_field_types_does_not_crash(tmp_path: Path) -> None:
    store = HoldStore(tmp_path)
    store.place(a_hold())
    (tmp_path / "hold" / UNIT).write_text('{"ticket": "many", "at": "soon"}', encoding="utf-8")
    read = store.get(UNIT)
    assert read is not None
    assert read.ticket == 0
    assert read.at == 0.0


def test_holds_are_listed_oldest_first(tmp_path: Path) -> None:
    # A report should lead with the stalest, which is the one most likely to be
    # freezing fleet updates.
    store = HoldStore(tmp_path)
    now = time.time()
    store.place(a_hold(unit="b.service", at=now))
    store.place(a_hold(unit="a.service", at=now - 100))
    assert [hold.unit for hold in store] == ["a.service", "b.service"]


def test_the_scratch_file_is_not_listed_as_a_hold(tmp_path: Path) -> None:
    store = HoldStore(tmp_path)
    store.place(a_hold())
    (tmp_path / "hold" / f".{UNIT}.new").write_text("{}", encoding="utf-8")
    assert len(store) == 1


def test_a_hold_reports_its_age() -> None:
    assert a_hold(at=time.time() - AGE_SECONDS).age_seconds >= AGE_SECONDS - 1
    # A clock that went backwards reads as brand new rather than negative.
    assert a_hold(at=time.time() + 600).age_seconds == 0.0


@pytest.mark.parametrize("unit", ["../escape.service", "a/b.service", ".", ".."])
def test_a_unit_name_that_would_escape_the_directory_is_refused(tmp_path: Path, unit: str) -> None:
    store = HoldStore(tmp_path)
    with pytest.raises(ValueError, match="not a usable unit name"):
        store.held(unit)
