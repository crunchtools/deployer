# Reference copies of what is deployed

`/srv` on the host is the authoritative home for everything running, and it is
its own git repo. These are the second copies, and they exist so that
`check_config_drift.sh` has something to compare against: a service whose
project repo keeps no reference copy is reported as *unmapped*, and "we looked
and found nothing" and "nobody ever looked" are the same green tile.

So they carry an obligation. **A change to the deployed config is not finished
until the copy here matches it**, in the same PR or the next one — a stale
reference copy here turns the fleet's config-drift check amber, which is the
check doing its job.

| File | Deployed at |
|---|---|
| `systemd/deployer.crunchtools.com.service` | `/etc/systemd/system/`, with the `/srv` copy beside the config |
| `systemd/50-deployer-hold.conf` | `/etc/systemd/system/podman-auto-update.service.d/` |
| `services.toml` | `/srv/deployer.crunchtools.com/config/` |
| `systemd/release-follow.crunchtools.com.service`, `.timer` | `/etc/systemd/system/`, with the `/srv/release-follow.crunchtools.com/config` copy |
| `deployer.env.example` | nothing — the real `deployer.env` is 0600 and holds passwords |
| `release-follow.env.example` | nothing — the real one is 0600 and holds the `follower` RT password |

The Nagios service definition for this program is **not** here. It belongs to
the check that reads it, which is `nagios-agent`, and it lives in that repo's
`deploy/nagios/deployer.cfg`.
