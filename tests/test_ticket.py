"""Ticket parsing tests.

The body is text an agent wrote, so most of what matters is what gets refused.
"""

from __future__ import annotations

import pytest

from deployer.ticket import MAX_BODY_BYTES, MAX_LINES, TicketError, creator_may_request, parse_ticket

SHA = "635cdda3c8632d1b5aed4cc22943895205ce5138"
PR_NUMBER = 235
HASH_PREFIXED_PR = 7

GOOD = f"""
service: acquacotta.crunchtools.com
repo: crunchtools/acquacotta
commit: {SHA}
pr: {PR_NUMBER}
"""


def test_a_good_body_parses() -> None:
    req = parse_ticket(GOOD)
    assert req.service == "acquacotta.crunchtools.com"
    assert req.repo == "crunchtools/acquacotta"
    assert req.commit == SHA
    assert req.pr == PR_NUMBER
    assert req.short_commit == "635cdda"


def test_pr_is_optional() -> None:
    assert parse_ticket(f"service: x\nrepo: a/b\ncommit: {SHA}\n").pr is None


def test_a_hash_prefixed_pr_is_accepted() -> None:
    assert parse_ticket(f"service: x\nrepo: a/b\ncommit: {SHA}\npr: #7\n").pr == HASH_PREFIXED_PR


def test_blank_lines_fences_and_headings_are_ignored() -> None:
    body = f"```\n\n# Deploy\nservice: x\n\nrepo: a/b\ncommit: {SHA}\n```\n"
    assert parse_ticket(body).service == "x"


def test_an_uppercase_sha_is_normalised() -> None:
    assert parse_ticket(f"service: x\nrepo: a/b\ncommit: {SHA.upper()}\n").commit == SHA


@pytest.mark.parametrize("missing", ["service", "repo", "commit"])
def test_every_required_field_is_required(missing: str) -> None:
    body = "\n".join(ln for ln in GOOD.splitlines() if not ln.startswith(f"{missing}:"))
    with pytest.raises(TicketError, match="missing required field"):
        parse_ticket(body)


def test_prose_is_refused_rather_than_skipped() -> None:
    # Skipping anything unparseable would let a body mean two things at once.
    with pytest.raises(TicketError, match="not a 'key: value' line"):
        parse_ticket(GOOD + "\nplease deploy this quickly, it is urgent\n")


def test_an_unknown_key_is_refused() -> None:
    with pytest.raises(TicketError, match="unknown key 'image'"):
        parse_ticket(GOOD + "\nimage: quay.io/evil/thing\n")


def test_a_repeated_key_is_refused() -> None:
    # The ambiguity this prevents is "which revision does the ticket mean".
    with pytest.raises(TicketError, match="'commit' given twice"):
        parse_ticket(GOOD + f"\ncommit: {'a' * 40}\n")


def test_an_empty_value_is_refused() -> None:
    with pytest.raises(TicketError, match="'service' has no value"):
        parse_ticket(f"service:\nrepo: a/b\ncommit: {SHA}\n")


@pytest.mark.parametrize(
    "commit",
    ["635cdda", "", "z" * 40, SHA + "a", SHA[:39], "HEAD", "main"],
)
def test_only_a_full_sha_is_accepted(commit: str) -> None:
    with pytest.raises(TicketError):
        parse_ticket(f"service: x\nrepo: a/b\ncommit: {commit}\n")


@pytest.mark.parametrize("repo", ["acquacotta", "a/b/c", "crunchtools/", "/acquacotta", "a b/c"])
def test_repo_must_be_owner_slash_name(repo: str) -> None:
    with pytest.raises(TicketError, match="is not owner/name"):
        parse_ticket(f"service: x\nrepo: {repo}\ncommit: {SHA}\n")


@pytest.mark.parametrize("service", ["../etc/passwd", "-leading-dash", "has space", "a/b"])
def test_an_implausible_service_name_is_refused(service: str) -> None:
    with pytest.raises(TicketError, match="not a plausible service name"):
        parse_ticket(f"service: {service}\nrepo: a/b\ncommit: {SHA}\n")


@pytest.mark.parametrize("pr", ["0", "-3", "abc", "1.5"])
def test_a_bad_pr_is_refused(pr: str) -> None:
    with pytest.raises(TicketError, match="must be a positive"):
        parse_ticket(f"service: x\nrepo: a/b\ncommit: {SHA}\npr: {pr}\n")


def test_an_oversized_body_is_refused() -> None:
    with pytest.raises(TicketError, match="larger than"):
        parse_ticket("x" * (MAX_BODY_BYTES + 1))


def test_too_many_lines_is_refused() -> None:
    with pytest.raises(TicketError, match="at most"):
        parse_ticket("\n" * (MAX_LINES + 1))


@pytest.mark.parametrize(
    ("creator", "allowed"),
    [("developer1", True), ("DEVELOPER1", True), (" developer1 ", True), ("takeda", False), ("", False)],
)
def test_creator_is_checked_against_the_registry_row(creator: str, allowed: bool) -> None:
    assert creator_may_request(creator, ("developer1", "josui")) is allowed
