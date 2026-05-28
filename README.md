# netbox-enterprise-sync

Periodic active-to-passive replication for paired NetBox clusters. Runs as a
single Docker container and copies the active cluster's Postgres database and
media files onto a warm-standby passive cluster every few minutes. Designed
so the passive can be promoted with minimal data loss when the active fails.

```
┌────────────────────────┐    every 5–15 min     ┌────────────────────────┐
│  ACTIVE NetBox stack   │  ────────────────────▶│  PASSIVE NetBox stack  │
│  Postgres + Redis +    │   pg_dump | restore   │  Postgres + Redis +    │
│  media + web + workers │   rsync media         │  media + web + workers │
└────────────────────────┘                       └────────────────────────┘
              ▲                                              ▲
              │ ssh                                          │ docker net
              │                                              │
              └─────────── netbox-enterprise-sync ───────────┘
                        (Docker container — orchestrator)
```

## Table of contents

- [What it syncs (and what it doesn't)](#what-it-syncs-and-what-it-doesnt)
- [Deployment topologies](#deployment-topologies)
- [Prerequisites](#prerequisites)
- [Install on a fresh machine](#install-on-a-fresh-machine)
  - [1. Prepare the active host](#1-prepare-the-active-host)
  - [2. Configure passive Redis as a replica](#2-configure-passive-redis-as-a-replica)
  - [3. Clone the repo on the orchestrator host](#3-clone-the-repo-on-the-orchestrator-host)
  - [4. Create secrets](#4-create-secrets)
  - [5. Write config.yaml](#5-write-configyaml)
  - [6. Build and start](#6-build-and-start)
  - [7. Verify](#7-verify)
- [Configuration reference](#configuration-reference)
- [Control API](#control-api)
- [Failover runbook](#failover-runbook)
- [Operational notes](#operational-notes)
- [Troubleshooting](#troubleshooting)
- [Future NetBox plugin](#future-netbox-plugin)
- [License](#license)

---

## What it syncs (and what it doesn't)

Every `interval_seconds` (default 600s / 10 min), one cycle does:

1. **Version gate** — read `release.yaml` (or equivalent) on both sides; abort
   if NetBox versions don't match (a restore across versions breaks migrations).
2. **Quiesce target** — run `quiesce_cmd` (typically `docker compose stop netbox netbox-worker`).
3. **`pg_dump`** the active DB, excluding `core_objectchange` data, into a
   staging file on the orchestrator's disk.
4. **Ship and `pg_restore`** onto the passive (drop + recreate + parallel
   restore with `-j 4`).
5. **`rsync --delete`** media files from active to passive.
6. **Verify** passive Redis is a healthy replica of active Redis
   (`role:slave/replica` + `master_link_status:up`).
7. **Smoke check** the restored DB (`SELECT COUNT(*) FROM dcim_device`, etc.).
8. **Resume target** — run `resume_cmd` (in a `finally` so the target always
   comes back up, even if a mid-cycle step failed).

**Deliberately out of scope:**

- **Reports / scripts / `configuration.py`** — these are code. Deploy them via
  your container image, not by rsync from a live production filesystem.
- **Redis snapshot replication** — would mangle sessions and in-flight RQ
  jobs. Use Redis's built-in `REPLICAOF` instead; the cycle just verifies it
  stays healthy.
- **DNS / load-balancer flips on failover** — out of scope for the
  orchestrator. The future NetBox plugin will own that.

---

## Deployment topologies

The same image and config schema support three placements. Switching is a
two-line config edit, not a code change.

| Topology | Orchestrator host | `source.transport` | `target.transport` |
|---|---|---|---|
| **A** (default) | Same host as passive NetBox | `ssh` | `local` |
| **B** | Same host as active NetBox | `local` | `ssh` |
| **C** | A third, out-of-band host | `ssh` | `ssh` |

Topology A is the recommended default: the passive host is where the heavy I/O
(restore + media write) lands, so co-locating saves a hop.

---

## Prerequisites

On the **orchestrator host**:

- Docker Engine ≥ 24 and Docker Compose v2 (`docker compose`, not the legacy
  `docker-compose`)
- ~2× the expected pg dump size in free disk on whatever volume backs
  `/var/lib/netbox-sync` in the container
- Private network connectivity (VPN, VPC peering, etc.) to whichever NetBox
  hosts the topology requires SSH to

On the **active NetBox host** (for any topology where it's reached via ssh):

- `pg_dump`, `psql`, `rsync`, `cat`, `redis-cli` available in PATH for the
  `netbox-sync` user (any standard Linux distro with `postgresql-client-16`
  and `rsync` installed satisfies this)
- A dedicated `netbox-sync` system user with shell access and an authorized
  public SSH key from the orchestrator

On the **passive NetBox host** (similarly, if reached via ssh):

- Same toolchain as the active host
- A `netbox-sync` user with permission to run the `quiesce_cmd` / `resume_cmd`
  (typically requires either docker-group membership or sudoers entries)

Both NetBox clusters must run **the same NetBox version**.

---

## Install on a fresh machine

These steps walk through topology A — orchestrator co-located with the passive
NetBox stack. For B/C, only the SSH setup direction and `transport:` values
change.

### 1. Prepare the active host

```bash
# On the active host (as root or via sudo)
adduser --system --group --shell /bin/bash --home /home/netbox-sync netbox-sync
install -d -o netbox-sync -g netbox-sync -m 700 /home/netbox-sync/.ssh

# Install the toolchain (Debian/Ubuntu — adapt for your distro)
apt-get update
apt-get install -y postgresql-client-16 rsync redis-tools
```

If your NetBox Postgres runs inside a Docker container on the active host
(typical `netbox-docker` deploys), the host's `pg_dump` connects to it via
TCP on `127.0.0.1:5432`. Confirm the container publishes the port, or set
`source.postgres.host` to the docker network name and run the orchestrator
on the same docker network instead.

### 2. Configure passive Redis as a replica

On the passive cluster's Redis instance, set:

```redis
replicaof <active-redis-host> 6379
```

…either in `redis.conf` (and restart) or at runtime:

```bash
redis-cli -h passive-redis CONFIG SET replicaof "active-redis-host 6379"
redis-cli -h passive-redis CONFIG REWRITE
```

Verify with `redis-cli -h passive-redis INFO replication` — you should see
`role:slave` and `master_link_status:up`.

### 3. Clone the repo on the orchestrator host

```bash
git clone https://github.com/cbeye-netbox/netbox-enterprise-sync.git
cd netbox-enterprise-sync
```

### 4. Create secrets

The orchestrator reads every credential from Docker secrets — nothing is baked
into the image or `config.yaml`.

```bash
mkdir -p secrets
chmod 700 secrets

# SSH key for connecting to the active host
ssh-keygen -t ed25519 -N "" -f secrets/source_ssh_key -C "netbox-sync"

# Postgres passwords (same value on both sides if your clusters share a
# password, otherwise the active's and passive's respective netbox role passwords)
printf '%s' 'YOUR-ACTIVE-PG-PASSWORD' > secrets/source_pg_password
printf '%s' 'YOUR-PASSIVE-PG-PASSWORD' > secrets/target_pg_password

# Control API token — random bytes
openssl rand -hex 32 > secrets/api_token

chmod 600 secrets/*
```

Then install the public key on the active host:

```bash
# Copy the .pub file's contents to /home/netbox-sync/.ssh/authorized_keys on the active host
ssh-copy-id -i secrets/source_ssh_key.pub netbox-sync@active.netbox.example.internal
# or manually append it to ~netbox-sync/.ssh/authorized_keys
```

Test the SSH connection from the orchestrator host:

```bash
ssh -i secrets/source_ssh_key netbox-sync@active.netbox.example.internal \
    pg_dump --version
```

### 5. Write `config.yaml`

```bash
cp config.example.yaml config.yaml
$EDITOR config.yaml
```

Set, at minimum:

- `source.ssh.host` — the active NetBox host's reachable address
- `source.postgres.host` — usually `127.0.0.1` if pg_dump runs on the active
  host and Postgres is local there
- `target.postgres.host` — the docker network hostname of the passive's
  Postgres (e.g., `netbox-postgres`), assuming the orchestrator joins the
  passive's network
- `target.redis.host` — likewise for passive Redis
- `target.paths.media` — where you'll mount the passive's media volume inside
  the orchestrator container (config example uses `/mnt/passive-media`)
- `target.version_file` — a path readable by the orchestrator that contains
  the passive's NetBox version
- `target.quiesce_cmd` / `target.resume_cmd` — the commands your container
  runtime uses to stop/start passive NetBox

### 6. Build and start

```bash
docker compose build
docker compose up -d
docker compose logs -f netbox-sync
```

The first cycle runs at startup. On the very first run the passive NetBox
will be fully replaced — make sure it has no in-flight work you care about.

### 7. Verify

```bash
# Health endpoint (no auth)
curl -s http://localhost:9911/health | jq .
# {
#   "source": "cluster-east",
#   "target": "cluster-west",
#   "enabled": true,
#   "in_flight": false,
#   "last_success_at": 1716889332.4,
#   "last_failure_at": null,
#   "interval_seconds": 600
# }

# Trigger an immediate cycle
TOKEN=$(cat secrets/api_token)
curl -X POST -H "X-Api-Token: $TOKEN" http://localhost:9911/sync-now

# Tail the structured cycle log
docker compose exec netbox-sync tail -F /var/lib/netbox-sync/cycles.log
```

Open the passive NetBox UI (in maintenance/read-only mode) and confirm device
counts, recent changes, and image attachments match the active.

---

## Configuration reference

The full schema lives in [`config.example.yaml`](./config.example.yaml). The
two endpoint blocks (`source` and `target`) have identical shape — that
symmetry is what makes `POST /reverse` a config swap with no special-casing.

| Key | Meaning |
|---|---|
| `sync.interval_seconds` | Cycle cadence in seconds |
| `sync.enabled_file` | Path inside the container where the enabled flag (`0`/`1`) is persisted |
| `sync.orchestrator_staging_dir` | Local dir on the orchestrator for the dump staging file |
| `sync.cycle_log` | Append-only JSONL log of every cycle outcome |
| `<endpoint>.name` | Human-readable cluster name (appears in `/state` and logs) |
| `<endpoint>.transport` | `local` (same host as orchestrator) or `ssh` (reach via ssh) |
| `<endpoint>.ssh.{host,user,key_file,port}` | Required when `transport: ssh` |
| `<endpoint>.postgres.{host,port,user,db,password_file}` | Postgres connection on the endpoint side |
| `<endpoint>.redis.{host,port}` | Redis connection on the endpoint side |
| `<endpoint>.paths.media` | Path to NetBox's media dir on the endpoint side |
| `<endpoint>.version_file` | Path to read NetBox version from (typically `release.yaml`) |
| `<endpoint>.quiesce_cmd` / `resume_cmd` | Best-effort commands to stop/start NetBox on the endpoint (target only in practice) |
| `<endpoint>.staging_dir` | Where on the endpoint to stage the dump (only used for remote targets) |
| `control_api.bind` | `host:port` for the FastAPI control surface |
| `control_api.token_file` | Path to file containing the API token |
| `alerts.webhook_url` | POST `{title, detail}` here on cycle failure (Slack-compatible) |

Passwords and tokens are read from files (Docker secrets convention) — never
embedded in YAML.

---

## Control API

Served on `control_api.bind` (default `0.0.0.0:9911`). **Expose only on a
private interface in production** — there's no TLS termination here.

| Method | Path | Auth | Purpose |
|---|---|---|---|
| `GET` | `/health` | none | Last cycle status, last_success_at, in_flight, source/target names, interval |
| `GET` | `/state` | none | Source, target, enabled, in_flight |
| `GET` | `/version` | none | NetBox version detected on source and target |
| `POST` | `/pause` | token | Stop running cycles |
| `POST` | `/resume` | token | Re-enable cycles |
| `POST` | `/sync-now` | token | Run a cycle immediately |
| `POST` | `/reverse` | token | Atomically swap source/target in YAML and reload. Leaves sync paused. |

Auth header: `X-Api-Token: <contents of secrets/api_token>`.

Example:
```bash
TOKEN=$(cat secrets/api_token)
curl -X POST -H "X-Api-Token: $TOKEN" http://localhost:9911/sync-now
```

---

## Failover runbook

When the active fails (or you want to drain it for maintenance):

1. **Pause sync** on the orchestrator, if it's still up:
   ```bash
   curl -X POST -H "X-Api-Token: $TOKEN" http://localhost:9911/pause
   ```
2. **Promote passive Redis**:
   ```bash
   redis-cli -h passive-redis CONFIG SET replicaof "NO ONE"
   redis-cli -h passive-redis CONFIG REWRITE
   ```
3. **Bring passive NetBox up writeable** — remove maintenance/read-only flag,
   start web and worker containers.
4. **Flip LB / DNS** so user traffic lands on the (former passive, now active)
   cluster. This is your responsibility — the orchestrator does not touch
   external networking.
5. **Reverse the orchestrator direction**:
   ```bash
   curl -X POST -H "X-Api-Token: $TOKEN" http://localhost:9911/reverse
   ```
   The YAML's `source` and `target` blocks are swapped and the in-memory
   config is reloaded. Sync remains paused.
6. **When the original active returns**:
   - Confirm it's running the same NetBox version as the new active
   - Configure its Redis as a `REPLICAOF` of the new active's Redis
   - Resume:
     ```bash
     curl -X POST -H "X-Api-Token: $TOKEN" http://localhost:9911/resume
     ```
   The next cycle will drop+recreate the database on what's now the passive
   side. Make sure no operator is still treating the old active as a
   read-only window into history.

---

## Operational notes

- **Disk sizing**: `/var/lib/netbox-sync` needs at least 2× the expected dump
  size (one current dump + room for the next one being written). Multi-GB
  NetBox installs should plan accordingly.
- **NetBox version drift**: any version mismatch fails the cycle closed. Roll
  upgrades on both sides together; pause sync first to avoid noise.
- **`core_objectchange` exclusion**: the cycle drops *data* from that table,
  not the schema. Restored passives have an empty changelog. This matches the
  [NetBox-recommended](https://netbox.readthedocs.io/en/stable/administration/replicating-netbox/)
  trade-off for keeping dumps manageable.
- **Passwords in argv**: `PGPASSWORD=...` is passed via `env` shim, briefly
  visible in `ps` on the endpoint host. Acceptable on a dedicated
  `netbox-sync` user; not acceptable on a shared host. Switch to `~/.pgpass`
  on the endpoint side if needed.
- **Healthcheck**: `/health` returns 200 always; the Docker healthcheck just
  pings it. To page on stalled sync, alert on `last_success_at` falling more
  than `2 × interval_seconds` behind `now()`.
- **Single instance**: do not run two orchestrators against the same target.
  There's no distributed lock; the in-memory cycle lock only protects within
  one process.

---

## Troubleshooting

**`pg_dump failed on cluster-east (rc=2): ...connection refused`**
The SSH user can't reach the source Postgres. Either pg isn't listening on
the configured host/port from the endpoint's perspective, or the endpoint
firewall blocks it. Run `ssh -i secrets/source_ssh_key netbox-sync@host
pg_isready -h <pghost> -p 5432` to isolate.

**`NetBox version mismatch: source='v4.1.7' target='v4.1.6'`**
The two clusters are on different versions. Pause sync, finish the upgrade
on both sides, then resume.

**`target cluster-west Redis role is 'master'; expected slave/replica`**
The passive's Redis isn't configured as a replica. See
[step 2](#2-configure-passive-redis-as-a-replica). The cycle will keep
failing until this is fixed (which is intentional — restoring DB without
Redis replication is a half-finished sync).

**Cycle takes longer than `interval_seconds`**
You'll see overlapping cycle attempts blocked by the lock. Either lengthen
`interval_seconds` or graduate to Postgres logical replication for the DB
pipe (this orchestrator's shape stays the same; only the postgres pipeline
swaps). A few-hundred-MB dump over a private link should comfortably fit
in <2 min; if you're far above that, capacity-plan for the upgrade.

**`mkdir on cluster-west failed: Permission denied`**
The SSH user on the target host can't write to `target.staging_dir`. Either
adjust the path or grant the user permission.

**`could not read version on cluster-west: ... No such file or directory`**
`target.version_file` points to a path that doesn't exist (or isn't readable
to the orchestrator). For `netbox-docker`, the file lives inside the netbox
container at `/opt/netbox/netbox/release.yaml`; either bake a sidecar that
publishes it to a host path or change `version_probe` to read from a HTTP
endpoint you publish on the NetBox side.

**`rsync: failed: Connection timed out`**
Long-running rsync over a flaky link. The SSH `ServerAliveInterval=30 /
ServerAliveCountMax=4` keepalive should cover normal blips, but a wholly
broken link will eventually time out. Investigate the VPN, then retry — the
next cycle picks up where this one left off (rsync is incremental).

---

## Future NetBox plugin

A NetBox plugin (separate repo, separate install) will sit on top of this
service and provide:

- **Dashboard panel** — calls `/health` + `/state` on the local orchestrator,
  surfaces lag and current direction inside the NetBox UI.
- **"Promote to active" admin action** — wraps the failover runbook above
  with confirmation dialogs: `/pause` → operator flips LB → `/reverse` →
  `/resume`.
- **"Sync now" button** — `/sync-now` exposed to operators with appropriate
  permissions.
- **Drift detector** — a NetBox background job that polls `/health` from
  every NetBox instance's local orchestrator and raises a notification if
  `last_success_at` falls behind by more than `2 × interval`.

**Important design constraint**: the plugin must not store "I am active" in
any synced NetBox model. Doing so would self-overwrite every cycle. The role
lives only in the orchestrator's local config + state files; the plugin
queries the orchestrator API for it.

---

## License

Apache-2.0 — see [LICENSE](./LICENSE).
