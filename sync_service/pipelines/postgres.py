"""Postgres pipeline — dump from active, restore to passive.

Everything runs as subprocesses inside the orchestrator container. The PG
client tools (pg_dump, pg_restore, psql) ship in the image and talk to both
endpoints over TCP.

Connection plumbing notes:
- `PGCONNECT_TIMEOUT=10` is set on every libpq invocation so a misconfigured
  endpoint surfaces an error in seconds, not minutes.
- The target side can optionally use a separate "admin" role for the
  DROP/CREATE DATABASE steps via `postgres.admin_user` + `admin_password_file`.
  pg_restore still runs as the main (database-owning) role. This avoids
  needing CREATEDB on the netbox role itself.
"""
from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import Optional

from ..config import EndpointConfig, PostgresConfig

logger = logging.getLogger(__name__)

CONNECT_TIMEOUT_SECONDS = 10


class PgError(RuntimeError):
    pass


def _conn_args(pg: PostgresConfig, db: Optional[str] = None, as_admin: bool = False) -> list[str]:
    user = pg.admin_user if (as_admin and pg.admin_user) else pg.user
    return [
        "-h", pg.host,
        "-p", str(pg.port),
        "-U", user,
        "-d", db or pg.db,
        "-w",  # never prompt; PGPASSWORD env supplies the password
    ]


def _env(pg: PostgresConfig, as_admin: bool = False) -> dict[str, str]:
    env = dict(os.environ)
    env["PGCONNECT_TIMEOUT"] = str(CONNECT_TIMEOUT_SECONDS)
    pw = pg.admin_password if (as_admin and pg.admin_user) else pg.password
    if pw:
        env["PGPASSWORD"] = pw
    return env


async def _run(cmd: list[str], env: dict[str, str], step: str) -> None:
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )
    _, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise PgError(f"{step} failed (rc={proc.returncode}): {stderr.decode(errors='replace').strip()}")


async def _query(pg: PostgresConfig, sql: str, db: Optional[str] = None) -> str:
    cmd = ["psql", *_conn_args(pg, db=db), "-tA", "-v", "ON_ERROR_STOP=1", "-c", sql]
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=_env(pg),
    )
    out, err = await proc.communicate()
    if proc.returncode != 0:
        raise PgError(f"psql query failed (rc={proc.returncode}): {err.decode(errors='replace').strip()}")
    return out.decode().strip()


async def schema_fingerprint(endpoint: EndpointConfig) -> str:
    """Return a fingerprint of the applied Django migrations on this DB.

    NetBox tracks every migration in `django_migrations`. The latest applied
    migration name per app is a stable, auth-free way to tell which NetBox
    version (and plugin set) is on each side.
    """
    pg = endpoint.postgres
    sql = (
        "SELECT string_agg(app || ':' || name, ',' ORDER BY app) "
        "FROM (SELECT app, MAX(name) AS name FROM django_migrations GROUP BY app) m;"
    )
    logger.info("schema fingerprint check endpoint=%s host=%s", endpoint.name, pg.host)
    try:
        return await _query(pg, sql)
    except PgError as e:
        raise PgError(f"reading schema fingerprint on {endpoint.name}: {e}") from e


async def dump(source: EndpointConfig, dump_path: str) -> int:
    """Run pg_dump against the source PG; write a custom-format dump to dump_path.

    Returns the dump file size in bytes.
    """
    pg = source.postgres
    Path(dump_path).parent.mkdir(parents=True, exist_ok=True)

    cmd = [
        "pg_dump",
        "-Fc", "-Z", "6",
        "--no-owner", "--no-acl",
        "--exclude-table-data=core_objectchange",
        *_conn_args(pg),
    ]
    logger.info("pg_dump start source=%s host=%s", source.name, pg.host)
    with open(dump_path, "wb") as out:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=out,
            stderr=asyncio.subprocess.PIPE,
            env=_env(pg),
        )
        _, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise PgError(f"pg_dump on {source.name} (rc={proc.returncode}): {stderr.decode(errors='replace').strip()}")
    size = Path(dump_path).stat().st_size
    logger.info("pg_dump done source=%s bytes=%d", source.name, size)
    return size


async def restore(target: EndpointConfig, dump_path: str) -> None:
    """Drop the target DB, recreate it, restore the dump.

    DROP/CREATE DATABASE runs as `admin_user` if configured (otherwise as the
    main user). pg_restore always runs as the main user, which becomes the
    owner of the new database.
    """
    pg = target.postgres
    admin_env = _env(pg, as_admin=True)
    main_env = _env(pg, as_admin=False)
    using_admin = bool(pg.admin_user)
    logger.info(
        "pg_restore prep target=%s host=%s admin_role=%s",
        target.name, pg.host, pg.admin_user or "(main user)",
    )

    terminate_sql = (
        f"SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
        f"WHERE datname = '{pg.db}' AND pid <> pg_backend_pid();"
    )
    await _run(
        ["psql", *_conn_args(pg, db="postgres", as_admin=using_admin),
         "-v", "ON_ERROR_STOP=1", "-c", terminate_sql],
        admin_env, f"terminate connections on {target.name}",
    )
    await _run(
        ["psql", *_conn_args(pg, db="postgres", as_admin=using_admin),
         "-v", "ON_ERROR_STOP=1",
         "-c", f"DROP DATABASE IF EXISTS {pg.db};"],
        admin_env, f"drop database on {target.name}",
    )
    await _run(
        ["psql", *_conn_args(pg, db="postgres", as_admin=using_admin),
         "-v", "ON_ERROR_STOP=1",
         "-c", f'CREATE DATABASE {pg.db} OWNER "{pg.user}";'],
        admin_env, f"create database on {target.name}",
    )

    logger.info("pg_restore start target=%s host=%s", target.name, pg.host)
    await _run(
        [
            "pg_restore",
            "-j", "4",
            "--no-owner", "--no-acl",
            *_conn_args(pg),  # main user — owns the restored DB
            dump_path,
        ],
        main_env, f"pg_restore on {target.name}",
    )
    logger.info("pg_restore done target=%s", target.name)


async def smoke_check(target: EndpointConfig) -> dict[str, str]:
    """Cheap sanity queries against the freshly restored target."""
    pg = target.postgres
    return {
        "dcim_device_count": await _query(pg, "SELECT COUNT(*) FROM dcim_device;"),
        "last_objectchange": await _query(pg, "SELECT COALESCE(MAX(time)::text, 'never') FROM core_objectchange;"),
    }
