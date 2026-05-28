# netbox-enterprise-sync

Periodic active-to-passive Postgres replication for paired NetBox installs.

Runs as a single Docker container on any host that can reach both Postgres
servers over TCP. Every few minutes it `pg_dump`s the active database and
`pg_restore`s it onto the passive database — so the passive can be promoted
with minimal data loss if the active fails.

```
   ┌────────────────────┐    every 5–15 min     ┌────────────────────┐
   │ ACTIVE Postgres    │  ────────────────────▶│ PASSIVE Postgres   │
   │ (netbox DB)        │   pg_dump | restore   │ (netbox DB)        │
   └────────────────────┘                       └────────────────────┘
            ▲                                            ▲
            │ TCP                                        │ TCP
            │                                            │
            └────────── netbox-enterprise-sync ──────────┘
                       (Docker container, separate host)
```

The orchestrator does not touch the NetBox application — it only talks to the
two databases. NetBox itself can run anywhere (bare metal, Kubernetes,
managed service) as long as its Postgres is reachable from the orchestrator
host.

## Table of contents

- [What it syncs (and what it doesn't)](#what-it-syncs-and-what-it-doesnt)
- [Prerequisites](#prerequisites)
- [Install on a fresh machine](#install-on-a-fresh-machine)
- [Web dashboard](#web-dashboard)
- [Configuration reference](#configuration-reference)
- [Control API](#control-api)
- [Failover runbook](#failover-runbook)
- [Operational notes](#operational-notes)
- [Troubleshooting](#troubleshooting)
- [License](#license)

---

## What it syncs (and what it doesn't)

Every `interval_seconds` (default 600s / 10 min):

1. **Schema fingerprint gate** — read `django_migrations` on both sides; if
   the latest applied migration per app doesn't match, abort. This catches
   NetBox version drift or plugin drift before it corrupts the passive.
2. **`pg_dump`** the active DB (custom format, level-6 compression,
   `--exclude-table-data=core_objectchange` per the
   [NetBox replication guide](https://netbox.readthedocs.io/en/stable/administration/replicating-netbox/)).
3. **`pg_restore`** onto the passive: terminate connections, drop database,
   recreate, parallel restore with `-j 4`.
4. **Smoke check** — `SELECT COUNT(*) FROM dcim_device`, latest
   `core_objectchange.time`.

**Deliberately out of scope:**

- Media files / attachments — sync your S3 bucket separately (or accept that
  attachments are lost on failover).
- Redis — sessions and RQ queues. Operationally cheap to lose; configure
  Redis replication separately if you need it.
- NetBox configuration files (`configuration.py`, plugins, scripts, reports)
  — these are code; deploy them via your NetBox image/release process.

This service does exactly one job: keep the passive Postgres caught up with
the active Postgres.

---

## Prerequisites

On the **orchestrator host** (any Linux box with Docker):

- Docker Engine ≥ 24 and Docker Compose v2 (`docker compose`, not the legacy
  `docker-compose`)
- TCP reachability to both Postgres servers on their `postgres.port` (default
  5432). Test with `psql -h <host> -p 5432 -U netbox -d netbox -c 'SELECT 1'`
  before deploying.
- ~2× the expected pg dump size in free disk on whatever volume backs
  `/var/lib/netbox-sync` in the container

On both **Postgres servers**:

- A user the orchestrator can connect as (`netbox` works fine) with permission
  to `pg_dump` everything on the source and to drop/create/restore the
  `netbox` database on the target. Typically: ownership of the `netbox` DB
  plus `CREATEDB`.

Both NetBox installs must be on **the same NetBox version + same plugins**.
The schema-fingerprint gate enforces this — a mismatch aborts the cycle.

---

## Install on a fresh machine

These steps get you from "fresh Linux VM" to "running sync."

### 1. Clone the repo

```bash
git clone https://github.com/cbeye-netbox/netbox-enterprise-sync.git
cd netbox-enterprise-sync
```

### 2. Create secrets

```bash
mkdir -p secrets
chmod 700 secrets

# Postgres passwords (the netbox role's password on each side)
printf '%s' 'ACTIVE-PG-PASSWORD'  > secrets/source_pg_password
printf '%s' 'PASSIVE-PG-PASSWORD' > secrets/target_pg_password

# Random API token for the dashboard / control API
openssl rand -hex 32 > secrets/api_token

chmod 600 secrets/*
```

### 3. Write `config.yaml`

```bash
cp config.example.yaml config.yaml
$EDITOR config.yaml
```

Set:

- `source.postgres.host` — hostname/IP of the active Postgres (reachable from
  this VM)
- `target.postgres.host` — hostname/IP of the passive Postgres
- `source.postgres.user` / `target.postgres.user` — usually `netbox`
- `source.postgres.db` / `target.postgres.db` — usually `netbox`
- `sync.interval_seconds` — how often to run a cycle (300 = 5 min, 900 = 15 min)

### 4. Test connectivity from the host

```bash
# Quick sanity check — you'll be prompted for the password
psql -h <active-pg-host>  -U netbox -d netbox -c 'SELECT 1'
psql -h <passive-pg-host> -U netbox -d netbox -c 'SELECT 1'
```

If those don't work, fix firewall / `pg_hba.conf` / network routes before
proceeding. The container will fail with the same error if Postgres isn't
reachable.

### 5. Build and start

```bash
docker compose -f docker-compose.example.yml up -d --build
docker compose -f docker-compose.example.yml logs -f netbox-sync
```

The first cycle runs at startup. On that first run the **passive Postgres
will be wiped and replaced** with the active's contents — make sure that's
what you want.

### 6. Verify

```bash
curl -s http://localhost:9911/health | jq .
```

Should print source/target names, `enabled: true`, last_success_at, etc.

Open `http://<orchestrator-host>:9911/` in a browser for the dashboard.

---

## Web dashboard

`http://<orchestrator>:9911/` serves a single-page operator dashboard. Same
process as the JSON API — no separate service.

**What you see (no auth needed):**

- Source → Target direction with cluster names
- `Enabled` / `Paused` and `Idle` / `Syncing…` badges
- Last success + last failure timestamps (relative + absolute)
- Configured cycle interval
- Most recent 20 cycles with status, duration, direction, and error detail
- Red banner if the orchestrator becomes unreachable

**Actions (require the API token):**

| Button | Endpoint | What it does |
|---|---|---|
| Pause | `POST /pause` | Stop running cycles |
| Resume | `POST /resume` | Re-enable cycles |
| Sync now | `POST /sync-now` | Trigger an immediate cycle |
| Reverse direction | `POST /reverse` | Swap source/target in `config.yaml` and pause. Confirmation dialog first. |

Paste the API token (contents of `secrets/api_token`) into the lock field in
the header. The `Locked` badge flips to `Unlocked` and action buttons become
clickable. Token is kept in browser `localStorage`; clear the field to
forget it.

**Polling cadence**: `/health` every 5s, `/cycles` every 15s. No WebSocket —
the polling cost is trivial.

**Security note**: there's no TLS termination here and the token is the only
auth gate. Bind to a private interface (the docker-compose example publishes
only on `127.0.0.1`). For remote access, front it with an authenticated
reverse proxy or a VPN.

---

## Configuration reference

The full schema lives in [`config.example.yaml`](./config.example.yaml).
`source` and `target` have identical shape — that symmetry is what makes
`POST /reverse` a config swap with no special-casing.

| Key | Meaning |
|---|---|
| `sync.interval_seconds` | Cycle cadence in seconds |
| `sync.enabled_file` | Path inside the container for the enabled flag (`0`/`1`) |
| `sync.orchestrator_staging_dir` | Local directory for the dump staging file |
| `sync.cycle_log` | Append-only JSONL log of every cycle outcome |
| `<endpoint>.name` | Human-readable cluster name (shown in `/state` and logs) |
| `<endpoint>.postgres.host` | Hostname or IP of this side's Postgres |
| `<endpoint>.postgres.port` | TCP port (default 5432) |
| `<endpoint>.postgres.user` | Role to connect as (usually `netbox`) |
| `<endpoint>.postgres.db` | Database name (usually `netbox`) |
| `<endpoint>.postgres.password_file` | Path to a file containing the password (Docker secret convention) |
| `control_api.bind` | `host:port` for the FastAPI control surface |
| `control_api.token_file` | Path to file containing the API token |
| `alerts.webhook_url` | POST `{title, detail}` here on cycle failure (Slack-compatible). Optional. |

---

## Control API

Served on `control_api.bind` (default `0.0.0.0:9911`).

| Method | Path | Auth | Purpose |
|---|---|---|---|
| `GET` | `/` | none | Web dashboard |
| `GET` | `/health` | none | Last cycle status, last_success_at, in_flight, source/target |
| `GET` | `/state` | none | Source, target, enabled, in_flight |
| `GET` | `/version` | none | Schema fingerprint on each side (live) |
| `GET` | `/cycles?limit=N` | none | Most recent N cycle log entries |
| `POST` | `/pause` | token | Stop running cycles |
| `POST` | `/resume` | token | Re-enable cycles |
| `POST` | `/sync-now` | token | Run a cycle immediately |
| `POST` | `/reverse` | token | Atomically swap source/target in YAML and reload. Leaves sync paused. |
| `GET` | `/api-docs` | none | OpenAPI / Swagger UI |

Token header: `X-Api-Token`.

```bash
TOKEN=$(cat secrets/api_token)
curl -X POST -H "X-Api-Token: $TOKEN" http://localhost:9911/sync-now
```

---

## Failover runbook

When the active fails:

1. **Pause sync** on the orchestrator (if it's still up):
   ```bash
   curl -X POST -H "X-Api-Token: $TOKEN" http://localhost:9911/pause
   ```
2. **Point NetBox at the (formerly passive) database.** This is your
   responsibility — update the NetBox config to use what was the passive's
   Postgres, restart the NetBox app, flip DNS/load balancer so users hit it.
3. **Reverse the orchestrator direction**:
   ```bash
   curl -X POST -H "X-Api-Token: $TOKEN" http://localhost:9911/reverse
   ```
   This swaps the `source` and `target` YAML blocks. Sync stays paused.
4. **When the original active's Postgres returns**:
   - Confirm it's on the same NetBox version (the schema-fingerprint gate
     will catch this, but check first)
   - Resume:
     ```bash
     curl -X POST -H "X-Api-Token: $TOKEN" http://localhost:9911/resume
     ```
   The next cycle will drop+recreate the database on the original active.
   Make sure nothing is still using it as a read-only view of pre-failover
   data.

---

## Operational notes

- **Disk sizing**: `/var/lib/netbox-sync` needs at least 2× the dump size
  (one current + room for the next).
- **NetBox version drift**: any mismatch fails the cycle closed (the
  schema-fingerprint gate). Upgrade both sides together; pause sync first.
- **`core_objectchange` exclusion**: the cycle drops *data* from that table,
  not the schema. Restored passives have an empty changelog. This matches
  the [NetBox-recommended](https://netbox.readthedocs.io/en/stable/administration/replicating-netbox/)
  trade-off for keeping dumps manageable.
- **Healthcheck**: `/health` returns 200 always. To page on stalled sync,
  alert on `last_success_at` falling more than `2 × interval_seconds` behind
  `now()`.
- **Single instance**: do not run two orchestrators against the same target.
  There's no distributed lock; the in-memory cycle lock only protects within
  one process.
- **Password exposure**: `PGPASSWORD` is set in the subprocess env (not in
  argv), so it doesn't show up in `ps`. Still treat the password files as
  secrets.

---

## Troubleshooting

**`pg_dump on cluster-east (rc=2): ...connection refused`**
The orchestrator host can't reach the active Postgres. Test with
`psql -h <host> -U netbox -d netbox -c 'SELECT 1'` from the orchestrator host.
Check firewalls, `pg_hba.conf`, network routes.

**`schema fingerprint mismatch — NetBox versions or plugin sets differ`**
The two databases have different `django_migrations` state. Bring them in
sync (upgrade one side, or install/remove a plugin) and `POST /resume`.

**Cycle takes longer than `interval_seconds`**
Cycles will queue and block each other. Either lengthen `interval_seconds`
or switch to native Postgres logical replication (out of scope for this
tool but the right answer beyond a few hundred MB).

**`could not read Username for 'https://github.com'` when cloning**
SSH-clone instead: `git clone git@github.com:cbeye-netbox/netbox-enterprise-sync.git`

---

## License

Apache-2.0 — see [LICENSE](./LICENSE).
