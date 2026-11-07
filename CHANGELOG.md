# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/) and this project adheres to
[Semantic Versioning](https://semver.org/).

## [Unreleased]

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
  what broke; a traceback does.
- `deployer unhold` clears the rollback count as well as the hold. They are one
  decision: the three-rollbacks-in-a-day refusal comes from the gate rather
  than from the hold, so clearing only the hold would leave the service refused
  for the rest of the day with nothing on the ticket to say why.
- CLI verbs wired: `run`, `rollback <service>`, `unhold <service>`, `holds` and
  `--dry-run`. `holds` and `unhold` read nothing but the state directory, so
  they work on a host where podman, systemd or RT is the thing that is broken.
  `rollback` honours a `rollback = "manual"` registry row by refusing and
  printing the two commands to run by hand. Exit codes: 0 for a verb that did
  what it was asked — including a deploy that failed and was rolled back
  cleanly — 1 only for a service that needs a person, and 2 for "could not run".
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
