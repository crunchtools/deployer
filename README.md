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
- Pull `gated-<commit>` and `:main` (and, only if `:main` has moved on, the
  registry's `:latest`), or just `:latest` for a repository that publishes no
  gated tags; tag the candidate `candidate-<revision>`, and immediately put
  `:latest` back where it was. The local tag never moves before the deploy step,
  so a ticket that is refused or waiting cannot turn an unrelated crash-restart
  into a deploy.
- Only deploy a candidate that is the commit the ticket named — its
  `org.opencontainers.image.revision`, or the `gated-<commit>` tag — **and** that
  main or a release promoted: `:main` or the registry's `:latest` holds that
  image. A pull request's head is built and labelled whether or not it merges,
  so a matching label alone is refused. If no such image exists yet and the
  ticket is young, wait and say so once; if it is old, stall it with the reason.
- Verify: the unit is active, the running container's image ID *is* the
  candidate, and the health probe — given up to `ready` seconds to answer at
  all — then holds continuously for the settle window. Checking the image ID
  matters: without it, a restart that quietly came back on the old image would
  look like a success. Separating `ready` from `settle` matters just as much,
  because a container unit is active the moment conmon reports the process
  started, several seconds before the process inside it is listening.
- On failure: roll back, re-verify, write a hold, and hand the ticket back to
  whoever filed it with the service's own last output and the probe result on
  it. That output is published to everyone who can read the queue, so it is
  redacted first — see below. If the rollback itself does not verify, cancel the maintenance downtime so
  Nagios pages a human — a robot with nobody awake has no other move.

RT and Nagios are told before the restart, not after. If either cannot be
reached, the run stops with the host untouched and the ticket still queued,
which is a far better outcome than a deploy nobody is told about. Past the
restart their failures are recorded but never undo a good deploy.

## What goes on a ticket, and what stays on the host

A failing service usually prints the thing that failed, and the thing that
failed is often a credential it was handed in its environment. The ticket is
readable by everyone in the queue, so:

- the **whole, unredacted tail** is written to `<state>/failures/<service>.log`,
  0600 in a 0700 directory, one file per service, overwritten. The ticket names
  the path;
- what goes **on the ticket** is that tail with every value from the
  container's environment removed, *except* an allowlist of names that are
  never credentials — `PATH`, `HOME`, `LANG` and a handful more.

Default deny, and that is the whole point. A blocklist of secret-looking names
fails by publishing a credential: `DB=hunter2` sails straight through one. An
allowlist fails by redacting something harmless, which costs a line of a log.
Adding a name to it is a code change, which is the right weight for deciding
that something is never a secret.

Two things stop publication entirely rather than risk it: an environment whose
shape cannot be read, and a secret whose value is too short to remove from text
without wrecking it. In the second case the ticket names the variable, so
somebody can lengthen it or decide it is not a credential.

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
probe and what it should return, how long the service may take to come up
(`ready`, default 60s) and how long it must then stay up (`settle`, default
60s), how long to wait for a build, whether rollback is automatic, and who may
file tickets for it. A service with database migrations is
`rollback = "manual"`: old code against a new schema is worse than a failed
deploy with the downtime still open.

An unknown key is a load error rather than a value that is ignored.

An `http` or `mcp` probe on an `https` URL may name what the handshake has to
prove:

```toml
probe = { kind = "http", url = "https://127.0.0.1:443/", server_name = "crunchtools.com", ca_file = "/etc/deployer/cloudflare-origin-ca.pem", expect_status = 200 }
```

`server_name` goes out as both the TLS SNI and the `Host` header, and `ca_file`
is the bundle the chain must verify against; the socket still goes to the URL's
host. That is the difference between "something is listening on 443" and "the
proxy is serving a certificate that is valid, in date, and for this name".
Both keys are refused on a URL that is not `https`, and a `ca_file` that names
nothing is refused when the registry loads rather than when the deploy runs.

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
| `NAGIOS_TIMEZONE` | the Nagios server's own zone, as an IANA name (`America/New_York`, not `EST` or `-05:00`); an unrecognised one is refused by name at start-up rather than fallen back from. Required, for the reason directly above and in the same direction: `cmd.cgi` takes `start_time` as naive wall clock and reads it in its zone, so a UTC string opened every window five hours late |

## Exit codes

| Code | Meaning |
|---|---|
| 0 | every ticket drained reached a decided state — including a **refusal**, and including a ticket waiting for its build or a second run standing down. For `rollback`: a rollback that verified |
| 1 | a deploy failed, or a rollback failed to verify |
| 2 | could not run: a missing path, an unloadable registry, an unreachable RT, the auto-update hold guard being absent, or a verb asked to do something the registry row forbids |

A **refusal is exit 0**. A service not in the registry, a commit nothing built,
a requestor not listed — these are decided states, and refusing correctly is
the tool working. The ticket is stalled and owned by whoever filed it, and that
is the channel.

A **deploy that was rolled back is exit 1** even when the rollback itself went
perfectly. It leaves a held service and a stalled ticket, and a oneshot unit
exiting 0 with a service held would report success for a state nobody has dealt
with. The wake-up that costs is one per failure, not one per idle minute, which
is the shape the whole design is built around.

## Deploying twice is not deploying twice

If the container is already running the commit the ticket names, nothing is
restarted. The run verifies what is there and finishes the ticket.

That matters more than it sounds. The expensive failure is a deploy that
verified and then could not be written back to RT: the ticket stays open, comes
round again two minutes later, and without this would restart a healthy service
to arrive exactly where it already was. It also covers the case where something
else — an auto-update, a person — put that image live first.

## Following releases

Ashigaru cuts releases on its own, and nothing it runs may hold an RT
credential, so a release nobody filed a ticket for used to sit undeployed.
`release-follow` is the filer for those. Hourly, for each row that lists
`follower` in `requestors`, it compares the registry's `:latest` with what the
container runs and, when `:latest` is newer, files an ordinary Deploy ticket as
RT user `follower`. The deploy then goes exactly as a seat's would.

Listing `follower` is the opt-in and the authorisation at once: the runner
accepts the ticket because the row lists the creator. The registry refuses it
on a `rollback = "manual"` row and on `trentina` and `josui.crunchtools.com`.
It never files a downgrade, never files while a live ticket covers the same
commit, and never refiles a commit it already filed. A follower ticket the
runner hands back is stalled and owned by `follower`, which nobody reads, so
each one is listed in `/var/lib/deployer/follower/release-follow.status` for
the Nagios check to warn on.

Every registry row is compared, not only the followed ones; a row that does not
list `follower` is never filed for, but when it runs an image older than
`:latest` its status line says `behind`. Each line that is behind carries
`since=`, the build time of `:latest`, which nagios-agent's deploy drift check
measures its 24-hour grace from. rotv, the service that drifted 37 releases,
deploys by hand and is one of those rows.

```
release-follow --dry-run     # say what would be filed, file nothing
```

## Licence

AGPL-3.0-or-later.
