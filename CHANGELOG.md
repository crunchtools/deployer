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
- Podman client over the socket with exactly seven operations — pull, inspect
  image, inspect container, tag, untag, remove image, list images. Two tests
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
- Repository scaffold: the `deployer` package, its CLI verbs (`run`,
  `rollback`, `unhold`, `holds`, and `--dry-run`), the container image, and the
  org's standard gates. The verbs parse and are not yet wired; an unimplemented
  verb exits 2 ("could not run") rather than 0, so the unit that starts it
  cannot read a scaffold as a successful deploy.
- The capability boundary this program rests on, asserted by a test: the package
  exposes no container create, start, run or exec. A process that cannot start a
  container cannot mount a live volume and therefore cannot relabel one.
- Container image on `quay.io/hummingbird/python:3.14-fips` carrying neither a
  podman binary nor systemctl — the podman REST API over its unix socket and
  systemd over D-Bus are the only routes out. `journalctl` is staged from the
  builder because the journal has no D-Bus route.

## [0.0.0] - 2026-11-07

Repository created.
