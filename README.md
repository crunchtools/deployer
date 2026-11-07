# deployer

A ticket in, a verified deploy out. `deployer` drains the Request Tracker
`Deploy` queue: for each ticket it deploys the commit the ticket names, checks
that the result actually holds, and puts the service back on its previous image
if it does not.

No model runs in this process. It is started by a systemd unit, and everything
it is willing to do is fixed by config that went through review. The agent that
*decides* a deploy should happen — a Developer seat finishing a piece of work —
only files a ticket; the agent that notices the queue is not empty only starts
a unit. That split is borrowed from
[ashigaru](https://github.com/crunchtools/ashigaru): the job that runs the model
holds no credential that can write, and the job that writes runs no model.

## What it cannot do

`deployer` can pull, inspect, tag, untag, remove and list images, and it can
restart or start a unit named in its registry. It **cannot create, start, run or
exec a container**, and a test asserts the absence.

That is the load-bearing part. A process that cannot start a container cannot
mount a live volume, and therefore cannot relabel one — which is precisely the
mistake a deploy robot must not be able to make, by construction rather than by
remembering.

## The loop

1. A seat merges its work, cuts the release, and files a ticket in `Deploy`
   naming the service, the repo and the commit.
2. A scheduled check on the Kagetora agent looks at the queue every two minutes
   **without calling a model**. When the queue is not empty, it starts this
   unit. An idle day therefore costs nothing; cost scales with deploys and
   failures rather than with time.
3. `deployer run` takes a lock and drains the queue oldest-first, one ticket at
   a time.

For each ticket:

- Refuse anything outside the registry, any ticket whose RT `Creator` is not a
  listed requestor, and any unit that pulls on start or is pinned to something
  other than `:latest`. A refusal is not an attempt.
- Tag the image the service is *running now* as `rollback-<revision>` — read
  from the running container, not from the local `:latest`, because those two
  can differ and that gap is how a surprise gets deployed.
- Pull `:latest`, tag it `candidate-<revision>`, and immediately put `:latest`
  back where it was. The local tag never moves before the deploy step, so a
  ticket that is refused or waiting cannot turn an unrelated crash-restart into
  a deploy.
- Only deploy if the candidate's `org.opencontainers.image.revision` is the
  commit the ticket named. If no such image exists yet and the ticket is young,
  wait and say so once; if it is old, stall it.
- Verify: the unit is active, the running container's image ID *is* the
  candidate, and the health probe holds continuously for the settle window.
  Checking the image ID matters — without it, a restart that quietly came back
  on the old image would look like a success.
- On failure: roll back, re-verify, write a hold, and hand the ticket back to
  whoever filed it with the journal tail and the probe output on it. If the
  rollback itself does not verify, cancel the maintenance downtime so Nagios
  pages a human — a robot with nobody awake has no other move.

## Holds

A rolled-back service gets a hold file. While it stands, the host's nightly
`podman auto-update` is skipped rather than failed, so the image that was just
rolled back is not quietly deployed again at 03:00.

A hold never expires. Expiry would mean a known-bad image going live because
nobody got to it in time, which is worse than the alert staying red. It clears
when the service verifies on a later ticket, or when a person runs
`deployer unhold <service>`.

The drop-in that implements the skip is **static config**, installed alongside
the unit and not written by this program. `deployer` refuses to run at all if
it is missing, because a hold nothing honours is worse than no hold.

## Usage

```
deployer run                 # drain the queue
deployer rollback <service>  # put a service back on its rollback image
deployer unhold <service>    # clear a hold once the fix has shipped
deployer holds               # what is held, since when, for which ticket
deployer --dry-run run       # say what would happen, change nothing
```

## Ticket format

Queue `Deploy`. The subject is for people and is not parsed. The body is
`key: value` lines:

```
service: acquacotta.crunchtools.com
repo: crunchtools/acquacotta
commit: 635cdda3c8632d1b5aed4cc22943895205ce5138
pr: 235
```

One ticket is one revision, and the body is read once. A fix is a *new* ticket,
not an edit — which keeps the deployer from having to parse a ticket's history
to work out what it is being asked to do now.

The ticket never names an image or a success test. Those come from the
registry, so that a seat cannot choose its own proof.

## Configuration

`services.toml` holds one row per service: the unit, the image, the health
probe and what it should return, the settle window, how long to wait for a
build, whether rollback is automatic, and who may file tickets for it. A
service with database migrations is `rollback = "manual"`: old code against a
new schema is worse than a failed deploy with the downtime still open.

An unknown key is a load error rather than a value that is ignored.

## Licence

AGPL-3.0-or-later.
