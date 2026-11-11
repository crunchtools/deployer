"""cve-enqueue — turn the fleet's standing CVE bump backlog into RT tickets.

The surviving half of the RT #1551 rebuild lane after the auto-rebuilder was
dropped (a same-commit rebuild does not clear a pinned-dependency Critical, so a
model-free rebuilder would hold a write credential and clear nothing). This reads
the grype scanner's ``cve.status`` and keeps exactly one open RT ticket per image
in the **Rebuild** queue, listing the bumps that image needs: the package, the
version it is on, the version grype says fixes it, whether it is network-
reachable, and where it lives. It holds NO GitHub token and triggers no build —
it files the backlog for a person, nothing more.

Model-free and idempotent, and the idempotency is read back from RT, not a local
state directory: a short digest of the current bump list rides in the ticket
Subject, so a run whose findings are unchanged writes nothing at all. A ticket is
RESOLVED only when its image has a fresh, successful scan with zero fixable
Criticals — never on a scan error or a stale file, because "no data" must not
read as "cleared". The queue is fixed to Rebuild and the Deploy queue is refused:
this credential may file backlog, never a deploy.

grype's suggested fix is shown as-is but is NOT trusted as the target: grype can
list a fix in a lower release line (a downgrade) or across a major (node 25 ->
11). Those are flagged in the ticket so a person picks the right same-major
version; the filer does not guess it.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import os
import re
import sys
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Final

from .rt import RtClient, RtCredentials, RtError

EXIT_OK: Final = 0
EXIT_CHANGED: Final = 0
EXIT_COULD_NOT_RUN: Final = 2
EXIT_STALE: Final = 1

DEFAULT_STATUS: Final = "/var/lib/grype-scan/cve.status"
DEFAULT_QUEUE: Final = "Rebuild"
#: One missed 6h scan plus slack; a scan older than this is not acted on.
DEFAULT_MAX_AGE_SECONDS: Final = 32400
_SUBJECT_PREFIX: Final = "CVE bumps:"
#: Fields entering a ticket come from scanned images, so they are constrained to
#: a charset that cannot carry an RT control line or a newline into the Subject.
_SAFE: Final = re.compile(r"[^A-Za-z0-9 ._:/@+,>()\[\]-]")
#: Leading non-digits on a version: the ``v`` of ``v1.2.3`` and the ``go`` of
#: ``go1.25.0``. Stripped before a major is compared, so ``v1 -> 1`` is not a
#: false cross-major and ``go1.25 -> 1.26`` is a minor, not a major.
_VER_PREFIX: Final = re.compile(r"^[^0-9]*")


def _safe(value: str, limit: int = 160) -> str:
    """A field reduced to a charset that is safe in an RT Subject or body."""
    return _SAFE.sub("", value).strip()[:limit]


@dataclass(frozen=True, slots=True)
class Finding:
    """One fixable-Critical bump record, parsed from a ``CVE finding`` line."""

    cve: str
    pkg: str
    type: str
    installed: str
    fix: str
    net: str
    path: str


@dataclass(frozen=True, slots=True)
class ImageState:
    """What one image's scan says: did it scan, and what does it need bumped."""

    ref: str
    ok: bool
    findings: tuple[Finding, ...]


def _field(name: str, line: str) -> str:
    """Read ``name=value`` up to the next space from a status line."""
    marker = f"{name}="
    start = line.find(marker)
    if start < 0:
        return ""
    start += len(marker)
    end = line.find(" ", start)
    return line[start:] if end < 0 else line[start:end]


def _version_tuple(ver: str) -> tuple[int, ...]:
    """Numeric components of a version, prefix stripped. Non-numeric -> empty."""
    bare = _VER_PREFIX.sub("", ver)
    parts: list[int] = []
    for chunk in bare.split("."):
        digits = re.match(r"\d+", chunk)
        if not digits:
            break
        parts.append(int(digits.group()))
    return tuple(parts)


def _flag(installed: str, fix: str) -> str:
    """A caution when grype's fix is not an obvious same-major forward bump."""
    iv, fv = _version_tuple(installed), _version_tuple(fix)
    if not iv or not fv:
        return ""
    if iv[0] != fv[0]:
        return f"  [!! cross-major {iv[0]}->{fv[0]}: confirm the same-major fix]"
    if fv < iv:
        return "  [!! grype lists a LOWER version: confirm the fix for this release line]"
    return ""


def parse_status(text: str) -> tuple[bool, dict[str, ImageState]]:
    """``cve.status`` into (complete, {ref: ImageState}).

    ``complete`` is the ``CVE end=1`` marker: a file without it was truncated
    mid-write and must not be acted on. Only ``result=ok`` images get a state;
    an errored image is recorded ok=False (so its ticket is never auto-closed),
    and its findings, if any, are ignored.
    """
    complete = False
    results: dict[str, bool] = {}
    findings: dict[str, list[Finding]] = {}
    for line in text.splitlines():
        if line == "CVE end=1":
            complete = True
        elif line.startswith("CVE finding item="):
            ref = _field("item", line)
            if ref:
                findings.setdefault(ref, []).append(
                    Finding(
                        cve=_field("cve", line),
                        pkg=_field("pkg", line),
                        type=_field("type", line),
                        installed=_field("installed", line),
                        fix=_field("fix", line),
                        net=_field("net", line),
                        # path is last on the line and may contain spaces.
                        path=line.split(" path=", 1)[1] if " path=" in line else "",
                    )
                )
        elif line.startswith("CVE item="):
            ref = _field("item", line)
            if ref:
                results[ref] = _field("result", line) == "ok"
    states: dict[str, ImageState] = {}
    for ref, ok in results.items():
        found = tuple(dict.fromkeys(findings.get(ref, ()))) if ok else ()
        states[ref] = ImageState(ref=ref, ok=ok, findings=found)
    return complete, states


def _bump_lines(findings: Sequence[Finding]) -> list[str]:
    """The per-finding lines, deduped and sorted, for body and digest."""
    seen: list[str] = []
    for f in sorted(findings, key=lambda x: (x.cve, x.pkg, x.installed, x.fix)):
        net = " network" if f.net == "1" else ""
        line = (
            f"  - {_safe(f.cve, 64)}  {_safe(f.pkg, 80)} ({_safe(f.type, 24)})  "
            f"{_safe(f.installed, 48)} -> {_safe(f.fix, 48)}{net}{_flag(f.installed, f.fix)}"
        )
        path = _safe(f.path, 200)
        if path:
            line += f"\n      {path}"
        if line not in seen:
            seen.append(line)
    return seen


def digest(findings: Sequence[Finding]) -> str:
    """A short, stable digest of the bump list, carried in the Subject."""
    joined = "\n".join(sorted({f"{f.cve}|{f.pkg}|{f.installed}|{f.fix}" for f in findings}))
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()[:12]


def subject_for(ref: str, dig: str) -> str:
    """``CVE bumps: <ref> [<digest>]`` — the image key and the idempotency tag."""
    return f"{_SUBJECT_PREFIX} {_safe(ref, 160)} [{dig}]"


def _parse_subject(subject: str) -> tuple[str, str]:
    """(ref, digest) from a subject this program wrote, or ("","")."""
    if not subject.startswith(_SUBJECT_PREFIX):
        return "", ""
    rest = subject[len(_SUBJECT_PREFIX):].strip()
    match = re.match(r"(\S+)\s+\[([0-9a-f]+)\]\s*$", rest)
    return (match.group(1), match.group(2)) if match else (rest.split(" ")[0], "")


def body_for(ref: str, findings: Sequence[Finding]) -> str:
    """The ticket body: what to bump, with the don't-trust-the-version caution."""
    n = len({(f.cve, f.pkg) for f in findings})
    lines = _bump_lines(findings)
    return "\n".join(
        [
            _safe(ref, 160),
            "",
            f"{n} fixable Critical CVE(s) need a version bump. grype's suggested fix is",
            "shown; confirm the same-major target before bumping (grype can list a lower",
            "or cross-major fixed version — those are flagged).",
            "",
            *lines,
            "",
            "Filed by cve-enqueue from the fleet CVE scan (RT #1551); kept in sync",
            "automatically — it updates when the bumps change and resolves when the",
            "image's scan is clean. A same-commit rebuild will NOT clear these; the fix",
            "is a version bump.",
        ]
    )


@dataclass(frozen=True, slots=True)
class Plan:
    """What a run intends to do, computed before any write (for --dry-run)."""

    create: tuple[tuple[str, str], ...]  # (ref, subject)
    update: tuple[tuple[int, str], ...]  # (ticket_id, new subject)
    resolve: tuple[int, ...]
    unchanged: tuple[str, ...]           # refs already in sync


async def plan_run(client: RtClient, queue: str, states: Mapping[str, ImageState]) -> Plan:
    """Reconcile the scan against the live Rebuild tickets. Reads only."""
    live = await client.live_tickets(queue)
    by_ref: dict[str, tuple[int, str]] = {}  # ref -> (ticket_id, digest)
    for tid in live:
        ticket = await client.ticket(tid)
        ref, dig = _parse_subject(ticket.subject)
        if ref:
            by_ref[ref] = (tid, dig)

    create: list[tuple[str, str]] = []
    update: list[tuple[int, str]] = []
    resolve: list[int] = []
    unchanged: list[str] = []

    for ref, state in states.items():
        if state.findings:
            dig = digest(state.findings)
            existing = by_ref.get(ref)
            if existing is None:
                create.append((ref, subject_for(ref, dig)))
            elif existing[1] == dig:
                unchanged.append(ref)
            else:
                update.append((existing[0], subject_for(ref, dig)))
        elif state.ok and ref in by_ref:
            # Fresh, successful scan, zero findings: the image is clean -> resolve.
            # (An errored image has ok=False and never reaches here.)
            resolve.append(by_ref[ref][0])
    return Plan(tuple(create), tuple(update), tuple(resolve), tuple(unchanged))


async def apply_plan(
    client: RtClient, queue: str, states: Mapping[str, ImageState], plan: Plan
) -> None:
    """Carry out a plan: create, comment+resubject, resolve."""
    for ref, subject in plan.create:
        await client.create_ticket(queue, subject, body_for(ref, states[ref].findings))
    for tid, subject in plan.update:
        ref = _parse_subject(subject)[0]
        await client.comment(tid, "The bump list changed:\n\n" + body_for(ref, states[ref].findings))
        await client.set_fields(tid, subject=subject)
    for tid in plan.resolve:
        await client.comment(tid, "The image's latest scan is clean — no fixable Critical remains.")
        await client.set_fields(tid, status="resolved")


def _read_status(path: str, max_age: int) -> tuple[bool, dict[str, ImageState], str]:
    """(usable, states, reason). Usable only if complete and fresh."""
    try:
        with open(path, encoding="utf-8") as handle:
            text = handle.read()
    except OSError as exc:
        return False, {}, f"cannot read {path}: {exc}"
    complete, states = parse_status(text)
    if not complete:
        return False, {}, f"{path} is truncated (no end marker); refusing to act"
    header = next((ln for ln in text.splitlines() if ln.startswith("CVE generated=")), "")
    generated = _field("generated", header)
    if not generated.isdigit():
        return False, {}, f"{path} has no readable generated time"
    age = int(time.time()) - int(generated)
    if age > max_age:
        return False, {}, f"scan is stale ({age}s old > {max_age}s); refusing to act"
    return True, states, ""


async def _run(creds: RtCredentials, queue: str, states: Mapping[str, ImageState], dry_run: bool) -> int:
    async with RtClient(creds) as client:
        plan = await plan_run(client, queue, states)
        verb = "would" if dry_run else ""
        for ref, _ in plan.create:
            print(f"{verb + ' ' if verb else ''}file Rebuild ticket for {ref}".strip())
        for tid, _ in plan.update:
            print(f"{verb + ' ' if verb else ''}update ticket {tid}".strip())
        for tid in plan.resolve:
            print(f"{verb + ' ' if verb else ''}resolve ticket {tid}".strip())
        if not (plan.create or plan.update or plan.resolve):
            print(f"no change: {len(plan.unchanged)} image(s) already tracked")
        if not dry_run:
            await apply_plan(client, queue, states, plan)
    return EXIT_OK


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cve-enqueue", description="File RT Rebuild tickets from the fleet CVE scan."
    )
    parser.add_argument(
        "--status", default=os.environ.get("CVE_STATUS", DEFAULT_STATUS), help="Path to cve.status"
    )
    parser.add_argument(
        "--queue", default=os.environ.get("REBUILD_QUEUE", DEFAULT_QUEUE), help="RT queue (not Deploy)"
    )
    parser.add_argument(
        "--max-age",
        type=int,
        default=int(os.environ.get("CVE_STATUS_MAX_AGE", DEFAULT_MAX_AGE_SECONDS)),
    )
    parser.add_argument("--dry-run", action="store_true", help="Compute the plan, change nothing")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.queue.strip().lower() == "deploy":
        print("cve-enqueue: refusing the Deploy queue — this files backlog, never a deploy", file=sys.stderr)
        return EXIT_COULD_NOT_RUN
    try:
        creds = RtCredentials.from_env(os.environ)
    except RtError as exc:
        print(f"cve-enqueue: {exc}", file=sys.stderr)
        return EXIT_COULD_NOT_RUN
    usable, states, reason = _read_status(args.status, args.max_age)
    if not usable:
        print(f"cve-enqueue: {reason}", file=sys.stderr)
        return EXIT_STALE
    try:
        return asyncio.run(_run(creds, args.queue, states, args.dry_run))
    except RtError as exc:
        print(f"cve-enqueue: RT error: {exc}", file=sys.stderr)
        return EXIT_COULD_NOT_RUN


if __name__ == "__main__":
    raise SystemExit(main())
