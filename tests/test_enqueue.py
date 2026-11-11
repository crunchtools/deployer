"""cve-enqueue tests (RT #1551).

The ones that matter most: two identical runs write nothing to RT (idempotency
read back from the Subject digest), and a ticket is resolved ONLY on a fresh,
successful, zero-finding scan — never on a scan error or a stale file, because
"no data" must not read as "cleared".
"""

from __future__ import annotations

import subprocess
import sys
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import cast
from urllib.parse import parse_qs

import httpx
import pytest

from deployer import enqueue
from deployer.enqueue import Finding, ImageState, digest, parse_status, subject_for
from deployer.rt import RtClient, RtCredentials, RtError, Ticket

REF = "quay.io/crunchtools/tig:latest"
NEW_TICKET_ID = 1551
TID = 42  # an existing ticket's id, in tests that assert which ticket was touched


def finding(**over: str) -> Finding:
    base = {
        "cve": "CVE-1", "pkg": "libx", "type": "deb", "installed": "1.0.0",
        "fix": "1.2.0", "net": "1", "path": "/p",
    }
    return Finding(**{**base, **over})


# --------------------------------------------------------------- status parsing


def _status(*lines: str, generated: int | None = None, end: bool = True) -> str:
    gen = generated if generated is not None else int(time.time())
    head = f"CVE generated={gen} db_built={gen} db_age_days=0 db_updated=1 images=2 elapsed=5"
    body = [head, *lines]
    if end:
        body.append("CVE end=1")
    return "\n".join(body) + "\n"


def test_parse_status_findings_and_result_ok() -> None:
    text = _status(
        f"CVE item={REF} seen=1 source=podman result=ok fix_crit=1 fix_high=0 rce=1",
        f"CVE finding item={REF} cve=CVE-9 pkg=golang.org/x/net type=go-module "
        f"installed=v0.1.0 fix=v0.2.0 net=1 path=/app/bin",
    )
    complete, states = parse_status(text)
    assert complete is True
    assert states[REF].ok is True
    assert states[REF].findings == (
        Finding("CVE-9", "golang.org/x/net", "go-module", "v0.1.0", "v0.2.0", "1", "/app/bin"),
    )


def test_parse_status_errored_image_keeps_no_findings_and_is_not_ok() -> None:
    text = _status(f"CVE item={REF} seen=1 result=error msg=scan_failed")
    _, states = parse_status(text)
    assert states[REF].ok is False
    assert states[REF].findings == ()


def test_parse_status_dedupes_identical_finding_lines() -> None:
    line = f"CVE finding item={REF} cve=CVE-9 pkg=p type=deb installed=1 fix=2 net=0 path=/x"
    text = _status(f"CVE item={REF} seen=1 result=ok fix_crit=2 fix_high=0", line, line)
    _, states = parse_status(text)
    assert len(states[REF].findings) == 1


def test_parse_status_truncated_is_incomplete() -> None:
    complete, _ = parse_status(_status(f"CVE item={REF} seen=1 result=ok fix_crit=0", end=False))
    assert complete is False


def test_path_with_spaces_is_kept_to_end_of_line() -> None:
    text = _status(
        f"CVE item={REF} seen=1 result=ok fix_crit=1",
        f"CVE finding item={REF} cve=CVE-9 pkg=p type=deb installed=1 fix=2 net=0 path=/a b/c d",
    )
    _, states = parse_status(text)
    assert states[REF].findings[0].path == "/a b/c d"


# --------------------------------------------------------------- flagging


def test_flag_cross_major() -> None:
    assert "cross-major 25->11" in enqueue._flag("25.2.1", "11.0.0")


def test_flag_v_and_go_prefixes_are_not_false_cross_major() -> None:
    assert enqueue._flag("v1.72.2", "1.79.3") == ""  # v stripped: 1 -> 1
    assert enqueue._flag("go1.25.0", "1.26.9") == ""  # go stripped: 1 -> 1, minor


def test_flag_downgrade() -> None:
    assert "LOWER" in enqueue._flag("go1.25.0", "1.24.13")


def test_flag_clean_forward_bump_is_unflagged() -> None:
    assert enqueue._flag("1.2.0", "1.3.0") == ""


# --------------------------------------------------------------- sanitising


def test_safe_strips_newlines_and_control_chars() -> None:
    assert "\n" not in enqueue._safe("evil\nSubject: injected")
    assert enqueue._safe("ok.pkg_1-2/3") == "ok.pkg_1-2/3"


def test_digest_is_stable_regardless_of_finding_order() -> None:
    a = [finding(cve="A"), finding(cve="B")]
    assert digest(a) == digest(list(reversed(a)))


# --------------------------------------------------------------- reconcile


class FakeRt:
    """A recording stand-in for RtClient: counts every write."""

    def __init__(self, tickets: Sequence[Ticket] = ()) -> None:
        self._tickets = {t.id: t for t in tickets}
        self.created: list[tuple[str, str, str]] = []
        self.comments: list[tuple[int, str]] = []
        self.fields: list[tuple[int, dict[str, str]]] = []

    async def live_tickets(self, queue: str) -> tuple[int, ...]:
        return tuple(sorted(self._tickets))

    async def ticket(self, ticket_id: int) -> Ticket:
        return self._tickets[ticket_id]

    async def create_ticket(self, queue: str, subject: str, text: str) -> int:
        self.created.append((queue, subject, text))
        return 9000 + len(self.created)

    async def comment(self, ticket_id: int, text: str) -> None:
        self.comments.append((ticket_id, text))

    async def set_fields(self, ticket_id: int, **fields: str) -> None:
        self.fields.append((ticket_id, fields))

    @property
    def writes(self) -> int:
        return len(self.created) + len(self.comments) + len(self.fields)


def _ticket(tid: int, subject: str, status: str = "open") -> Ticket:
    return Ticket(id=tid, queue="Rebuild", status=status, subject=subject, creator="", owner="")


async def _reconcile(fake: FakeRt, states: dict[str, ImageState]) -> None:
    # FakeRt is a structural stand-in; plan_run/apply_plan only call the methods
    # it implements. Cast so mypy accepts it in place of the concrete RtClient.
    client = cast(RtClient, fake)
    plan = await enqueue.plan_run(client, "Rebuild", states)
    await enqueue.apply_plan(client, "Rebuild", states, plan)


@pytest.mark.asyncio
async def test_new_findings_file_one_ticket() -> None:
    fake = FakeRt()
    await _reconcile(fake, {REF: ImageState(REF, True, (finding(),))})
    assert len(fake.created) == 1
    assert fake.created[0][0] == "Rebuild"
    assert fake.created[0][1].startswith("CVE bumps: " + REF)
    assert fake.comments == [] and fake.fields == []


@pytest.mark.asyncio
async def test_two_identical_runs_make_no_writes_the_second_time() -> None:
    findings = (finding(),)
    subject = subject_for(REF, digest(findings))
    fake = FakeRt([_ticket(TID, subject)])
    await _reconcile(fake, {REF: ImageState(REF, True, findings)})
    assert fake.writes == 0  # already in sync, nothing said


@pytest.mark.asyncio
async def test_changed_findings_comment_and_resubject() -> None:
    old_subject = subject_for(REF, digest((finding(cve="OLD"),)))
    fake = FakeRt([_ticket(TID, old_subject)])
    new = (finding(cve="NEW"),)
    await _reconcile(fake, {REF: ImageState(REF, True, new)})
    assert len(fake.comments) == 1 and fake.comments[0][0] == TID
    assert len(fake.fields) == 1
    assert fake.fields[0][1]["subject"] == subject_for(REF, digest(new))
    assert fake.created == []


@pytest.mark.asyncio
async def test_clean_fresh_scan_resolves_the_ticket() -> None:
    fake = FakeRt([_ticket(TID, subject_for(REF, "deadbeef0000"))])
    await _reconcile(fake, {REF: ImageState(REF, True, ())})  # ok, zero findings
    assert any(f[1].get("status") == "resolved" for f in fake.fields)
    assert fake.created == []


@pytest.mark.asyncio
async def test_errored_image_never_resolves_its_ticket() -> None:
    fake = FakeRt([_ticket(TID, subject_for(REF, "deadbeef0000"))])
    await _reconcile(fake, {REF: ImageState(REF, False, ())})  # ok=False (result=error)
    assert fake.writes == 0  # no data is not "cleared"


@pytest.mark.asyncio
async def test_stalled_ticket_is_not_refiled_as_duplicate() -> None:
    findings = (finding(),)
    fake = FakeRt([_ticket(TID, subject_for(REF, digest(findings)), status="stalled")])
    await _reconcile(fake, {REF: ImageState(REF, True, findings)})
    assert fake.created == [] and fake.writes == 0


# --------------------------------------------------------------- freshness gate


def test_read_status_refuses_stale(tmp_path: Path) -> None:
    p = tmp_path / "cve.status"
    p.write_text(_status(f"CVE item={REF} seen=1 result=ok fix_crit=0", generated=1))
    usable, _, reason = enqueue._read_status(str(p), max_age=100)
    assert usable is False and "stale" in reason


def test_read_status_refuses_truncated(tmp_path: Path) -> None:
    p = tmp_path / "cve.status"
    p.write_text(_status(f"CVE item={REF} seen=1 result=ok fix_crit=0", end=False))
    usable, _, reason = enqueue._read_status(str(p), max_age=10**9)
    assert usable is False and "truncated" in reason


def test_read_status_accepts_fresh_complete(tmp_path: Path) -> None:
    p = tmp_path / "cve.status"
    p.write_text(_status(f"CVE item={REF} seen=1 result=ok fix_crit=0"))
    usable, states, _ = enqueue._read_status(str(p), max_age=10**9)
    assert usable is True and REF in states


# --------------------------------------------------------------- queue guard & main


def test_main_refuses_the_deploy_queue(capsys: pytest.CaptureFixture[str]) -> None:
    rc = enqueue.main(["--queue", "Deploy", "--status", "/nonexistent"])
    assert rc == enqueue.EXIT_COULD_NOT_RUN
    assert "Deploy" in capsys.readouterr().err


def test_main_refuses_any_non_rebuild_queue(capsys: pytest.CaptureFixture[str]) -> None:
    # The guard is an allowlist: only the Rebuild queue is accepted, so a queue
    # that is neither Deploy nor Rebuild is still refused (not an auth boundary
    # that only blocks Deploy).
    rc = enqueue.main(["--queue", "Incoming", "--status", "/nonexistent"])
    assert rc == enqueue.EXIT_COULD_NOT_RUN
    assert "Incoming" in capsys.readouterr().err


def test_main_refuses_when_no_status_path(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    # Neither --status nor $CVE_STATUS: exit 2 before any credential or file
    # access, naming the env var. (The Rebuild queue is accepted so the status
    # guard, not the queue guard, is what fires.)
    monkeypatch.delenv("CVE_STATUS", raising=False)
    rc = enqueue.main(["--queue", "Rebuild"])
    assert rc == enqueue.EXIT_COULD_NOT_RUN
    assert "CVE_STATUS" in capsys.readouterr().err


# ------------------------------------------- rt.py additions (real client, mock transport)

CREDS = RtCredentials(
    base_url="http://rt.example/", username="rebuilder", password="x", timezone="America/New_York"
)


Handler = Callable[[httpx.Request], httpx.Response]


def _client(handler: Handler) -> RtClient:
    return RtClient(CREDS, client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))


@pytest.mark.asyncio
async def test_create_ticket_posts_the_create_transaction_and_parses_the_id() -> None:
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["path"] = request.url.path
        seen["content"] = parse_qs(request.content.decode())["content"][0]
        return httpx.Response(200, text="RT/6.0.2 200 Ok\n\n# Ticket 1551 created.\n")

    async with _client(handler) as client:
        tid = await client.create_ticket("Rebuild", "CVE bumps: x [abc]", "body")
    assert tid == NEW_TICKET_ID
    assert seen["method"] == "POST"
    assert seen["path"].endswith("/REST/1.0/ticket/new")
    # The create transaction carries the queue, subject and body we passed, so a
    # wrong endpoint or a mis-sent field would fail this, not slip through.
    assert "id: ticket/new" in seen["content"]
    assert "Queue: Rebuild" in seen["content"]
    assert "Subject: CVE bumps: x [abc]" in seen["content"]
    assert "Text: body" in seen["content"]


# ------------------------------------------- the import clause the constitution names


def test_enqueue_imports_only_the_rt_client_from_the_package() -> None:
    # The deployer constitution (0.3.0) admits cve-enqueue into this package ONLY
    # while it imports nothing from the package but the RT client — no module that
    # can touch a container, a unit or the registry. A fresh interpreter imports
    # enqueue and fails if any capability-bearing module loaded with it. (A
    # subprocess, because this test process has already imported them.)
    forbidden = ["deployer.podman", "deployer.systemd", "deployer.runner", "deployer.cli"]
    code = (
        "import importlib, sys\n"
        "importlib.import_module('deployer.enqueue')\n"
        f"bad = [m for m in {forbidden!r} if m in sys.modules]\n"
        "assert not bad, 'enqueue pulled in capability-bearing modules: ' + repr(bad)\n"
    )
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr


@pytest.mark.asyncio
async def test_create_ticket_raises_when_rt_does_not_confirm() -> None:
    # RT answered 200 but without the `# Ticket N created` line: creation is not
    # confirmed, so this must raise rather than return a bogus id.
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="RT/6.0.2 200 Ok\n\nno ticket line here\n")

    async with _client(handler) as client:
        with pytest.raises(RtError):
            await client.create_ticket("Rebuild", "CVE bumps: x [abc]", "body")


@pytest.mark.asyncio
async def test_live_tickets_query_includes_stalled() -> None:
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["query"] = dict(httpx.QueryParams(request.content.decode())).get("query", "")
        return httpx.Response(200, text="RT/6.0.2 200 Ok\n\nticket/7\n")

    async with _client(handler) as client:
        assert await client.live_tickets("Rebuild") == (7,)
    assert "stalled" in seen["query"] and "Rebuild" in seen["query"]
