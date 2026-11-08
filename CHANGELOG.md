# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/) and this project adheres to
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added
- **`server_name` and `ca_file` on an `https` probe, so a TLS-terminating
  proxy can be verified.** `proxy.crunchtools.com` fronts every site on the
  fleet, and the only probe expressible before this was
  `http://127.0.0.1:80/` expecting a 301 — which is answered just as happily
  by an Apache serving an expired certificate, or serving the default vhost's
  certificate to every name. Not a new probe kind: the scheme is already in
  the URL, so the keys ride on it and work for `mcp` rows behind the same
  proxy.

  `server_name` goes out as **both** the TLS SNI and the HTTP `Host` header —
  the SNI decides which certificate Apache offers, the header decides which
  vhost answers, and a probe that sent one without the other would be
  reporting on a different request than it made. `ca_file` is used through a
  plain `ssl.create_default_context(cafile=...)` with `check_hostname` left
  alone, so OpenSSL checks the chain, the validity dates and the name while
  the socket still goes to loopback. There is no `verify=False` anywhere and
  nothing reads `getpeercert()` to compare names by hand: on a loopback socket
  the verification is not a MITM defence, it *is* the check.

  Both keys are refused on a URL that is not `https`, and a `ca_file` naming a
  file that is not there is refused when the registry loads. The alternative
  was a registry that loads and then fails every probe for that service in a
  maintenance window, to learn what one `stat` knew.

  Tested against a real TLS listener, with certificates minted in the test by
  `trustme` (a new dev dependency; it cannot reach the image, which installs
  only `[project.dependencies]`). A stubbed transport cannot refuse a
  certificate, so against one the whole feature is a no-op that passes. A
  matching chain and name verifies; a certificate from another authority, one
  for another host, and an expired one each fail, with the specific verdict
  asserted rather than "verify failed".

### Fixed
- **A row that pins TLS no longer shares the fleet's one HTTP client.**
  `verify` is a per-client setting, so a `ca_file` row needed its own; but
  measured against httpx 0.28.1 and httpcore 1.0.9, the pool also keys a
  keep-alive connection by **origin alone**. Two rows probing
  `https://127.0.0.1:443/` under different `server_name`s on one client reuse
  the same handshake, and the second comes back 200 with its own name never
  offered — a false pass on the one thing the row is buying. So `server_name`
  alone is enough to earn a client, and the shared client's settings are
  untouched.
- The success comment on a pinned row now says which name the certificate
  verified as. "`https://127.0.0.1:443/` answered throughout" is equally true
  of a proxy serving the wrong certificate to that port, and the comment is
  the only receipt a deploy leaves.

## [0.3.0] - 2026-11-07

### Added
- **A third probe kind, `log`, for services that answer nothing.** The agent
  seats — Kagetora, Josui and the developer seats — expose no port, and Nagios
  watches each with a single check that says "the container is running". Both
  existing probe kinds need something to send a request to, so those services
  could not be onboarded at all, which left six-plus containers running
  whatever `:latest` held at their last restart. A `log` row names a line
  (`probe = { kind = "log", expect = "..." }`); the readiness window waits for
  it to appear and the settle window then holds on the container's identity.

  It is deliberately not a kind meaning "check nothing". That would pass for a
  container whose main process is `sleep`, which is roughly what a Claude Code
  seat looks like from outside, and a check that cannot fail is worse than no
  check because it gets trusted.

  The log is read **only from `State.StartedAt` onwards**. A unit that
  `podman start`s an existing container keeps the previous run's output, so a
  whole-log grep would be answered by the *last* run's startup line and call a
  service ready before it had done anything — the same family of mistake as
  reading a restart as finished the moment it was requested. Field names and
  the `since` behaviour were measured against podman 5.8.2 rather than assumed.

  Keys are refused across kinds: `url`, `expect_status` and `expect_body` on a
  `log` row, and `expect` on an `http` or `mcp` row. A silently ignored key is
  how a registry starts claiming a proof it is not performing.

### Changed
- **The settle window now fails a container that was replaced or restarted
  inside it**, for every probe kind rather than only `log`. It compares `Id`
  and `RestartCount` against the values read when the window opened. A
  crash-looping service answers its probe perfectly well between crashes, so
  sampling a window without asking whether it is still the same process was
  measuring the gaps between failures.
- The success comment on a resolved ticket no longer ends `and <url> answered
  throughout` unconditionally. On a `log` row there is no URL and nothing
  answered; that sentence was a fabrication waiting to happen on the one tier
  that proves least, which is exactly where a reader most needs to know what
  was actually checked.

## [0.2.0] - 2026-11-07

### Added
- `deploy/`: reference copies of the unit, the auto-update hold drop-in and
  `services.toml`, plus a credential-free `deployer.env.example`. `/srv` stays
  the authoritative home; these exist so the fleet's config-drift check has a
  counterpart to compare against instead of reporting this service as
  *unmapped*. They carry the obligation that goes with them — see
  `deploy/README.md`.
- A test for the auto-update hold guard, which had none. It reads the
  `ExecCondition` out of the published drop-in and runs it, so a typo in the
  file that gets deployed fails the suite rather than passing it: the guard is
  the only thing standing between a rolled-back image and its return at the
  next fleet-wide auto-update, and nothing in the package imports it.

### Fixed
- **The maintenance window now outlasts a rollback.** It was sized
  `settle + 300`, which predated both the restart wait and the `ready` window
  and so measured one of the three things it has to cover. acquacotta got six
  minutes against a good path of up to 120 + 180 + 60 = 360 seconds and a
  rollback path near 720, so **a rollback that was working would have paged
  somebody in the middle of it** — and the one page this design raises
  deliberately, a rollback that did *not* come back, would have arrived
  indistinguishable from that noise.

  It is now twice the whole verification plus the margin, computed from the
  service's own `ready` and `settle`, because the point of those keys being
  per service is that acquacotta is not an MCP server. Caught by the cto
  review of the program's done claim, not by a test.
- The Deployed comment carries the **version and the digest**, not only the
  revision and a local image id. `ImageRef.version` existed for this and its
  docstring said so; nothing used it. A commit answers "which code", a version
  answers "which release", and a digest is the only one of the three that means
  the same thing in a registry, in a signature and on another host. Both are
  optional on a real image, and an unlabelled one now reads "version
  unlabelled, digest none reported" rather than putting `None` on a ticket,
  where it would look like a bug in the robot instead of a fact about the
  image.

## [0.1.4] - 2026-11-07

### Added
- `NAGIOS_TIMEZONE`, required: the Nagios server's own zone, as an IANA name
  such as `America/New_York`. An unrecognised one is refused at start-up.

### Fixed
- **The maintenance window now starts when the deploy does.** `cmd.cgi` takes
  `start_time` as naive wall clock and reads it in the Nagios server's zone.
  This container has no local-zone file and no `TZ`, so `time.localtime`
  formatted UTC, Nagios read that string as Eastern, and every window this
  program ever opened began **five hours in the future** — then was deleted
  before it started. Measured against the real Nagios on ticket 1572:
  `SCHEDULE_HOST_DOWNTIME` submitted at 21:05:07 for a window starting
  02:05:07. The suppression the design promises had never once existed, so
  every deploy was free to alert while its unit restarted.

  Exactly the bug `RT_TIMEZONE` exists to prevent, in the function next door —
  and the README row for `RT_TIMEZONE` already said "a container's clock is
  UTC … reading `America/New_York` as UTC is five hours out". Knowing it in one
  place did not stop it in the other. `NAGIOS_TIMEZONE` is required rather than
  defaulted: this is the one setting where a wrong guess is silent, because the
  window opens, Nagios accepts it, and it suppresses nothing.

## [0.1.3] - 2026-11-07

### Added
- `ready` in the registry: how long a service may take to answer its health
  probe after the restart, default 60 seconds, separate from `settle`.

### Fixed
- **Verification now waits for the service to come up before it starts
  requiring it to stay up.** Those are two questions and running them as one
  made every service in the registry undeployable. A container unit is `active`
  the moment conmon reports the process started, which is seconds before the
  process inside it binds its port, so the settle window spent its first sample
  in that gap and a single connection-refused ended the deploy. Measured on the
  first real ticket this deployer ran: mcp-pcloud went from *open* to *"rolled
  back, needs a person"* in twelve seconds, with a settle window of thirty, on
  a service that was fine — and the good commit it was deploying was recorded
  as one that had broken the service.

  `ready` is a bounded, retrying wait for the first passing probe; `settle` is
  unchanged — sampled, with the first failure fatal. The two checks the host
  can answer without waiting, *is the unit active* and *is the staged image the
  one running*, stay fatal in both phases, so a dead unit is still reported at
  once rather than polled for a minute.
- An empty log tail no longer asserts why it was empty. It had one explanation
  built in — the container runs its own init — and put *"mcp-pcloud runs its
  own init"* on ticket 1571, which is false; the container was two seconds old
  and had not printed yet. It now reports the emptiness and names both
  commands.

## [0.1.2] - 2026-11-07

### Fixed
- **`restart` now waits for the unit to come back.** `RestartUnit` over D-Bus
  *queues a job and returns*. Verification ran immediately afterwards, read
  `ActiveState` as `deactivating` — the old container was still stopping — and
  failed on its first sample. The rollback then did the same thing, so the
  first real deploy reported a perfectly healthy service as *"down, needs a
  person"*, cancelled its maintenance window to page, and held it. Every deploy
  would have done exactly that.

  It waits on `ActiveEnterTimestamp` rather than on `ActiveState` alone: the
  old instance is still `active` in the moment between the job being queued and
  the stop beginning, so "it is active" is not proof that a restart happened. A
  newer activation timestamp is. A restart that has not completed within 120
  seconds, or that ends `failed`, raises — a deploy that cannot be told to have
  finished must not be verified as though it had.
- `deployer unhold` now also forgives the revision its hold names. A rolled-back
  revision is still never retried *automatically*, but a rollback is not always
  the revision's fault — the bug above condemned a good commit — and without
  this the only way back was editing the state directory by hand. It takes a
  person who has looked: the hold file names the revision.

## [0.1.1] - 2026-11-07

## [0.1.0] - 2026-11-07

### Added
- Service registry (`services.toml`): one row per service holding the image, the
  unit, the health probe and what it must return, the settle window, the build
  deadline, whether rollback is automatic, and which requestors may file for it.
  An unknown key is a load error, and `also_restart` and `preflight` are refused
  by name with the reason rather than quietly ignored.
- Ticket parsing: a strict `key: value` grammar over `service`, `repo`, `commit`
  and an optional `pr`. Prose, a repeated key and an abbreviated sha are all
  refused. One ticket carries one revision; a fix is a new ticket.
- Podman client over the socket with exactly eight operations — pull, inspect
  image, inspect container, tag, untag, remove image, list images, read a
  container's logs. Reading logs is a GET: it adds nothing to what this program
  can *do* to a container, which is why it sits inside the boundary. Two tests
  hold the line: one compares the class against the declared surface, the other
  refuses any method name that hints at a container lifecycle.
- Unit-shape gate: a unit whose `ExecStart` does not end in `<image>:latest`, or
  whose `ExecStartPre` pulls on start, is refused rather than handled. So is one
  whose `ExecStart` cannot be read.
- Candidate staging: tag the running container's image as `rollback-<revision>`,
  pull, tag the arrival as `candidate-<revision>`, then put `:latest` back on the
  outgoing image. The local `:latest` therefore never moves until a deploy moves
  it, so a refused or waiting ticket cannot turn an unrelated crash-restart into
  a deploy. A missing revision label falls back to the image id with its
  algorithm prefix stripped, because a tag cannot contain a colon.
- `classify_revision`: a candidate that does not match the ticket is *waiting*
  until the service's `build_deadline` and *stale* after it, since a seat files
  its ticket when it merges and CI builds afterwards.
- Verification: a deploy counts only if the unit is active, the running
  container *is* the candidate image, and the health probe answers as the
  registry says — all three, continuously, for the settle window. The image
  check is the one that catches a restart that came back cleanly on the old
  code; the window is what catches a service that answers once and then falls
  over.
- Rollback, attempted once: move `:latest` back, restart, verify again. If the
  rollback itself does not verify, the maintenance downtime is cancelled so
  Nagios pages — a robot that has broken a service and cannot restore it has no
  remaining move. A `rollback = "manual"` service is never rolled back
  automatically, because old code against a new schema is worse than a failed
  deploy with the downtime still open.
- Rollback tag rotation: two kept per image, the rest removed. Rollback images
  are application-sized and unrotated ones have filled a fleet host's disk before now.
- Systemd client over D-Bus with restart, start, state and one property read,
  refusing any unit the registry does not name. No `daemon-reload` and no
  unit-file writes, so the static hold guard cannot be rewritten by this
  program. `podman-auto-update.service` is readable and not restartable.
- The `run` state machine, which is the order the rest of the package is used
  in. The guard is checked before a single ticket is read; everything that can
  refuse refuses before the host is touched and none of it counts as an
  attempt; staging is reversible and happens before the decision to deploy; RT
  and Nagios are told *before* the restart, so an unreachable one leaves the
  host untouched and the ticket queued rather than deploying something nobody
  is told about; and after the restart their failures are reported but never
  undo a good deploy.
- A hold is not a gate on deploying. A held service is one whose last deploy
  failed, and the next ticket — carrying the fix — is exactly what should get
  through; verifying it is how the hold clears. What a hold stops is `podman
  auto-update`, which would otherwise put the broken `:latest` back overnight.
  The gate against repeating a mistake is the attempt log: the same revision is
  never deployed to the same service twice.
- An exclusive `flock` on the state directory, so two runs cannot overlap and
  each restore `:latest` from under the other. A second run *stands down* and
  exits 0: the unit that starts it is a oneshot whose failure wakes a model, and
  "the previous run is still going" is the most ordinary thing that can happen.
- Failure reports on the ticket carry the service's own last output, not just
  the probe's verdict. "The health probe returned 503" does not tell a developer
  what broke; a traceback does. The whole tail is kept on the host at
  `<state>/failures/<service>.log`, 0600 in a 0700 directory and overwritten
  per service, and the ticket carries the same tail with every value from the
  container's environment removed bar an allowlist of names that are never
  credentials. Default deny: a blocklist of secret-looking names fails by
  publishing a credential — `DB=hunter2` sails through one — while an allowlist
  fails by redacting something harmless. An environment whose shape cannot be
  read, and a secret too short to substitute out of text safely, both stop
  publication rather than risk it. The tail is captured **before** the
  rollback, because podman's logs endpoint is addressed by container name and
  after a restart that name is the restored container.
- `deployer unhold` clears the rollback count as well as the hold. They are one
  decision: the three-rollbacks-in-a-day refusal comes from the gate rather
  than from the hold, so clearing only the hold would leave the service refused
  for the rest of the day with nothing on the ticket to say why.
- Deploying is idempotent. If the container is already running the commit the
  ticket names, nothing is restarted: the run verifies what is there and
  finishes the ticket. The failure that makes this worth having is a deploy
  that verified and then could not be written back to RT — the ticket stays
  open, comes round again two minutes later, and would otherwise restart a
  healthy service to arrive where it already was. If the right revision is
  live and *unhealthy*, the ticket is refused rather than restarted: that is a
  person's call, not a robot's.
- `deployer rollback` and `deployer unhold` take the same lock as a run. Both
  write what a failing deploy writes — the rollback moves `:latest` while a
  deploy's staging is holding it, and unhold clears a hold a deploy may have
  just placed, which would let the broken image back at the next auto-update
  with nothing saying why.
- `deployer rollback <service>` requires the hold guard and places a hold, for
  the same reason the automatic path does: `:latest` still carries the image
  being rolled away from, so without a hold the next auto-update puts it
  straight back. The hold records ticket 0, meaning "a person did this by
  hand".
- CLI verbs wired: `run`, `rollback <service>`, `unhold <service>`, `holds` and
  `--dry-run`. `holds` and `unhold` read nothing but the state directory, so
  they work on a host where podman, systemd or RT is the thing that is broken.
  `rollback` honours a `rollback = "manual"` registry row by refusing and
  printing the two commands to run by hand. Exit codes as
  this repo's constitution sets them: 0 when every ticket drained reached a
  decided state — a **refusal** included, because refusing correctly is the
  tool working — 1 when a deploy failed or a rollback failed to verify, and 2
  for could-not-run. A rolled-back deploy is 1 even when the rollback itself
  went perfectly: it leaves a held service and a stalled ticket, and a unit
  that exited 0 with a service held would report success for a state nobody
  has dealt with.
- Repository scaffold: the `deployer` package, the container image, and the
  org's standard gates.
- The capability boundary this program rests on, asserted by a test: the package
  exposes no container create, start, run or exec. A process that cannot start a
  container cannot mount a live volume and therefore cannot relabel one.
- Container image on `quay.io/hummingbird/python:3.14-fips` carrying no package
  manager, no podman binary, no systemctl and no journalctl — the podman REST
  API over its unix socket and systemd over D-Bus are the only routes out.
- RT's `Created` is read in a configured timezone (`RT_TIMEZONE`, required).
  RT renders dates with no zone in the string and this RT is on
  `America/New_York` while a container's own clock is UTC; reading the string as
  local time would be five hours out, which is enough to call a fresh ticket
  stale or to wait for ever on an old one. An unknown zone name is refused at
  startup, and an unreadable date is an error rather than being treated as
  "now" — that would hand the ticket a fresh build deadline on every run.

## [0.0.0] - 2026-11-07

Repository created.
