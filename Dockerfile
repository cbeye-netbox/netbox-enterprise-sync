# NetBox active/passive sync orchestrator
#
# Standalone Docker container. Reaches both Postgres servers over TCP. The
# postgres-client major version should match the NetBox Postgres major version
# (NetBox 4.x runs on Postgres 16 by default).

FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates gnupg curl \
    && install -d /usr/share/postgresql-common/pgdg \
    && curl -fsSL https://www.postgresql.org/media/keys/ACCC4CF8.asc \
         -o /usr/share/postgresql-common/pgdg/apt.postgresql.org.asc \
    && echo "deb [signed-by=/usr/share/postgresql-common/pgdg/apt.postgresql.org.asc] \
        https://apt.postgresql.org/pub/repos/apt $(. /etc/os-release && echo $VERSION_CODENAME)-pgdg main" \
        > /etc/apt/sources.list.d/pgdg.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends postgresql-client-16 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml /app/
COPY sync_service /app/sync_service

RUN pip install --no-cache-dir .

# Debian's passwd package ships a vestigial `sync` user at UID 4 (a relic of
# the `sync` coreutils shell). Remove it before creating our own UID-10001 user
# under the same name.
RUN userdel sync 2>/dev/null || true \
    && useradd --system --uid 10001 --home /home/sync --create-home sync \
    && mkdir -p /var/lib/netbox-sync /etc/netbox-sync \
    && chown -R sync:sync /var/lib/netbox-sync /etc/netbox-sync /home/sync

USER sync

EXPOSE 9911

HEALTHCHECK --interval=60s --timeout=5s --start-period=30s --retries=3 \
    CMD curl -fsS http://127.0.0.1:9911/health > /dev/null || exit 1

ENV CONFIG_PATH=/etc/netbox-sync/config.yaml

CMD ["python", "-m", "sync_service.main"]
