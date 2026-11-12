# deployer Constitution

> **Version:** 0.3.0
> **Ratified:** 2026-11-07
> **Amended:** 2026-11-11
> **Status:** Active
> **Inherits:** [crunchtools/constitution](https://github.com/crunchtools/constitution) v1.41.0
> **Profile:** CLI Tool

This file holds what is specific to `deployer`. The fleet rules and the CLI
Tool profile apply at the inherited version and are checked against this repo's
files by `constitution.yml`. They are not restated here.

## Single Responsibility

One sentence: drain the Request Tracker `Deploy` queue, deploy the commit each
ticket names, verify it, and roll back once if it does not hold.

Anything that is not that is out of scope, including deciding *whether* a
deploy should happen. That decision belongs to whoever files the ticket.

Amended at 0.3.0 (RT #1551): this repository ships a second tool, `cve-enqueue`,
whose one sentence is to keep one open RT `Rebuild` ticket per image listing the
version bumps the fleet CVE scan says it needs. It is here because it needs this
package's RT client and nothing else, and a repo of its own would be a copy of
that client. It is not a deployer verb and is admitted only while all of these
hold: it imports nothing from this package but the RT client; it runs from its
own unit with no podman or D-Bus socket, no state directory and no registry; and
it holds no credential that can file an accepted Deploy ticket. The day it needs
a second credential or a second dependency, it moves out.

## The Capability Boundary

This is the rule the rest of the design rests on, so it is first.

`deployer` may pull, inspect, tag, untag, remove and list **images**, and it may
restart or start a **unit named in its registry**. It may not create, start, run
or exec a **container**, and `tests/test_cli.py` asserts that no module in the
package exposes such a verb.

The reason is not least-privilege for its own sake. A process that can start a
container can mount a live volume, and a container that mounts a live volume
with a relabelling flag takes that volume away from the service that owns it.
That is the class of mistake a deploy robot must not be able to make, and the
only durable way to guarantee it is to not hold the capability.

The same reasoning puts two more things out of reach: no `daemon-reload`, and no
writing of unit files or drop-ins. Every file `deployer` needs systemd to honour
is static config installed beside its unit and reviewed as config. Where such a
file is load-bearing — the `podman auto-update` hold guard — `deployer` checks
it is present and **refuses to run at all** if it is not, because a hold that
nothing honours is worse than no hold.

## No Model In The Deploy Path

No language model runs in this process, and it holds no model credential. It is
started by a systemd unit. The agent that notices the queue is non-empty starts
that unit and does nothing else; the agent that decides work is finished files a
ticket and does nothing else. This is ashigaru's authority rule — the job that
writes runs no model, the job that runs a model holds nothing that writes —
applied to deployment.

It follows that `deployer` reaches Request Tracker and Nagios **directly**, not
through the Trentina gateway. The gateway's judging layer is a model; putting it
in the deploy path would contradict the paragraph above, and it would also make
the gateway itself permanently undeployable by this tool.

## Exit Code Contract

The profile's contract holds, with `2` read in python-sdlc's sense of "could not
run" as well as the profile's "usage error" — both mean *this never got far
enough to try*, which is what a caller needs to distinguish from a failed
deploy:

| Code | Meaning |
|------|---------|
| `0` | Every ticket drained reached a decided state |
| `1` | A deploy failed, or a rollback failed to verify |
| `2` | Could not run: bad arguments, missing or invalid registry, or the hold guard is not installed |

A refusal — a service not in the registry, a ticket from an unlisted requestor,
a unit of the wrong shape, no image for the commit — is a decided state and
exits `0`. Refusing correctly is the tool working.

## Untrusted Input

A ticket body is text an agent wrote. It is parsed with a strict `key: value`
grammar and nothing else; no field of it selects an image, a success test or a
command. Those come from the registry, so a requestor cannot choose its own
proof. One ticket carries one revision and is read once — a fix is a new
ticket, which is what keeps this tool from having to interpret a ticket's edit
history.

`services.toml` is config, not input: an unknown key is a load error rather
than a value silently ignored.

## State

All state is files under one directory, so that backing anything out is a file
delete:

| Path | What |
|------|------|
| `hold/<unit>` | A rolled-back service. Carries ticket, revision, time. **Never expires.** |
| `rolled-back/<service>/<rev>` | A revision that was started and failed to verify. Never deployed **automatically** again. |
| `tickets/<id>` | What has already been said on a ticket, so a waiting one is not commented on every run. |

A hold has no expiry because expiry means a known-bad image going live when
nobody got to the alert in time. A red check is the better failure.

`rolled-back/<service>/<rev>` is cleared by `deployer unhold <service>`, for
the revision that service's hold names, and by nothing else. Amended at 0.2.0:
the rule used to read "never deployed again", and that was wrong in one
direction. A rollback is not always the revision's fault — the deployer itself
reported a healthy service as down once, because `RestartUnit` queues a job and
returns and verification read a unit mid-restart — and a permanent record
condemned a good commit with no way back but editing this directory. Which the
section above explicitly contemplates ("backing anything out is a file
delete"), so the amendment changes who does it, not whether it can be done: a
person who has cleared the hold, having seen the revision the hold names.
Nothing automatic clears it, and nothing automatic retries a revision.
