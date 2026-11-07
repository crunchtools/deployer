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

`deployer` can pull, inspect, tag, untag, remove and list images, read a
container's logs, and restart or start a unit named in its registry. It
**cannot create, start, run or exec a container**, and a test asserts the
absence.

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
  whoever filed it with the service's own last output and the probe result on
  it. If the rollback itself does not verify, cancel the maintenance downtime so
  Nagios pages a human — a robot with nobody awake has no other move.

RT and Nagios are told before the restart, not after. If either cannot be
reached, the run stops with the host untouched and the ticket still queued,
which is a far better outcome than a deploy nobody is told about. Past the
restart their failures are recorded but never undo a good deploy.

## Holds

A rolled-back service gets a hold file. While it stands, the host's nightly
`podman auto-update` is skipped rather than failed, so the image that was just
rolled back is not quietly deployed again at 03:00.

A hold never expires. Expiry would mean a known-bad image going live because
nobody got to it in time, which is worse than the alert staying red. It clears
when the service verifies on a later ticket, or when a person runs
`deployer unhold <service>`.

A hold is **not** a gate on deploying. The next ticket carries the fix, and
letting it through is the whole point; verifying it is how the hold clears. What
stops the same mistake twice is the attempt log: a revision that was rolled back
is never deployed to that service again, whatever the ticket says.

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

Where things live is a property of the deployment, so it comes from the unit's
environment and there are no built-in paths — a guessed one would mean acting on
whatever happened to be there. A missing variable is exit 2, "could not run".

| Variable | What |
|---|---|
| `DEPLOYER_CONFIG` | `services.toml` |
| `DEPLOYER_STATE` | state directory: holds, attempts, ticket state, the status file and the run lock |
| `DEPLOYER_QUEUE` | RT queue to drain (default `Deploy`) |
| `PODMAN_SOCKET` | podman's unix socket |
| `RT_URL`, `RT_USER`, `RT_PASSWORD` | RT, reached directly rather than through the Trentina gateway: that keeps a model out of the deploy path and leaves the gateway itself deployable by this tool |
| `RT_TIMEZONE` | the zone RT renders its dates in. Required, because RT's `Created` string carries no zone and a container's clock is UTC — reading `America/New_York` as UTC is five hours out, enough to call a fresh ticket stale |
| `RT_HTTP_USER`, `RT_HTTP_PASSWORD` | the auth proxy in front of RT, if there is one |
| `NAGIOS_CMD_URL`, `NAGIOS_USER`, `NAGIOS_PASSWORD` | `cmd.cgi`, for the maintenance window |

## Exit codes

| Code | Meaning |
|---|---|
| 0 | the verb did what it was asked — **including** a deploy that failed and was rolled back cleanly, and including a second run standing down because one is already going |
| 1 | a service needs a person: a rollback that did not come back |
| 2 | could not run: a missing path, an unloadable registry, an unreachable RT, or the auto-update hold guard being absent |

A rollback is a *success* of this program. The unit that starts it is a oneshot
whose failure wakes a model, and waking one to be told what the ticket already
says would be the expensive way to learn nothing.

## Licence

AGPL-3.0-or-later.
