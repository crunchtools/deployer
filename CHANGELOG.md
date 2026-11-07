# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/) and this project adheres to
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added
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
