"""Attempt-bookkeeping tests.

The asymmetry is the thing to pin down: a bad revision is refused forever, a
struggling service is refused until someone looks.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from deployer.attempts import ROLLBACK_LIMIT, ROLLBACK_WINDOW_SECONDS, AttemptLog

SERVICE = "acquacotta.crunchtools.com"
GOOD = "635cdda3c8632d1b5aed4cc22943895205ce5138"
BAD = "f0effffab7fc0b0f8476c14f5794e0da60256eb0"
DAY = ROLLBACK_WINDOW_SECONDS


def test_an_unseen_revision_has_not_been_rolled_back(tmp_path: Path) -> None:
    assert not AttemptLog(tmp_path).was_rolled_back(SERVICE, BAD)


def test_a_marked_revision_is_remembered(tmp_path: Path) -> None:
    log = AttemptLog(tmp_path)
    log.mark_rolled_back(SERVICE, BAD)
    assert log.was_rolled_back(SERVICE, BAD)
    assert not log.was_rolled_back(SERVICE, GOOD)


def test_the_mark_is_case_insensitive_on_the_revision(tmp_path: Path) -> None:
    log = AttemptLog(tmp_path)
    log.mark_rolled_back(SERVICE, BAD.upper())
    assert log.was_rolled_back(SERVICE, BAD)


def test_a_mark_is_per_service(tmp_path: Path) -> None:
    # The same image can be fine on one service and broken on another.
    log = AttemptLog(tmp_path)
    log.mark_rolled_back(SERVICE, BAD)
    assert not log.was_rolled_back("mcp-pcloud.crunchtools.com", BAD)


def test_a_bad_revision_does_not_become_good_with_age(tmp_path: Path) -> None:
    # No window on this one: an image that broke a service does not become safe
    # because time passed.
    log = AttemptLog(tmp_path)
    log.mark_rolled_back(SERVICE, BAD)
    path = tmp_path / "rolled-back" / SERVICE / BAD
    path.write_text(f"{time.time() - 365 * DAY}\n", encoding="utf-8")
    assert log.was_rolled_back(SERVICE, BAD)


def test_no_rollbacks_logged_is_zero(tmp_path: Path) -> None:
    log = AttemptLog(tmp_path)
    assert log.recent_rollbacks(SERVICE) == 0
    assert not log.too_many_rollbacks(SERVICE)


def test_rollbacks_inside_the_window_count(tmp_path: Path) -> None:
    log = AttemptLog(tmp_path)
    for _ in range(ROLLBACK_LIMIT - 1):
        log.record_rollback(SERVICE)
    assert log.recent_rollbacks(SERVICE) == ROLLBACK_LIMIT - 1
    assert not log.too_many_rollbacks(SERVICE)


def test_the_limit_stops_the_service(tmp_path: Path) -> None:
    # The first rollback is the system working. The third in a day means
    # something redeploying will not fix, and restarting production while a
    # person is asleep is what the bound prevents.
    log = AttemptLog(tmp_path)
    for _ in range(ROLLBACK_LIMIT):
        log.record_rollback(SERVICE)
    assert log.too_many_rollbacks(SERVICE)


def test_rollbacks_outside_the_window_do_not_count(tmp_path: Path) -> None:
    log = AttemptLog(tmp_path)
    old = time.time() - DAY - 60
    for _ in range(ROLLBACK_LIMIT + 2):
        log.record_rollback(SERVICE, when=old)
    assert log.recent_rollbacks(SERVICE) == 0
    assert not log.too_many_rollbacks(SERVICE)


def test_the_count_is_per_service(tmp_path: Path) -> None:
    log = AttemptLog(tmp_path)
    for _ in range(ROLLBACK_LIMIT):
        log.record_rollback("other.crunchtools.com")
    assert log.recent_rollbacks(SERVICE) == 0
    assert log.too_many_rollbacks("other.crunchtools.com")


def test_an_unreadable_log_line_counts_rather_than_being_skipped(tmp_path: Path) -> None:
    # Under-counting would let a fourth rollback through, which is the one
    # outcome this bound exists to prevent.
    log = AttemptLog(tmp_path)
    (tmp_path / "rollbacks.log").write_text(
        f"not-a-timestamp\t{SERVICE}\n" * ROLLBACK_LIMIT, encoding="utf-8"
    )
    assert log.recent_rollbacks(SERVICE) == ROLLBACK_LIMIT
    assert log.too_many_rollbacks(SERVICE)


def test_a_blank_log_line_is_ignored(tmp_path: Path) -> None:
    log = AttemptLog(tmp_path)
    (tmp_path / "rollbacks.log").write_text("\n\n", encoding="utf-8")
    assert log.recent_rollbacks(SERVICE) == 0


@pytest.mark.parametrize("value", ["../escape", "a/b", "", "has space", "semi;colon"])
def test_a_name_unfit_for_a_path_is_refused(tmp_path: Path, value: str) -> None:
    log = AttemptLog(tmp_path)
    with pytest.raises(ValueError, match="not a usable"):
        log.was_rolled_back(value, GOOD)
    with pytest.raises(ValueError, match="not a usable"):
        log.was_rolled_back(SERVICE, value)
