"""Postgres pipeline — dump from active, restore to passive.

Everything runs as subprocesses inside the orchestrator container. The PG
client tools (pg_dump, pg_restore, psql) ship in the image and talk to both
endpoints over TCP.
"""
from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

from ..config import EndpointConfig, PostgresConfig

logger = logging.getLogger(__name__)


class PgError(RuntimeError):
    pass


def _conn_args(pg: PostgresConfig, db: str | None = None) -> list[str]:
    return [
        "-h", pg.host,
        "-p", str(pg.port),
        "-U", pg.user,
        "-d", db or pg.db,
        "-w",  # never prompt; use PGPASSWORD env
    ]


def _env(pg: PostgresConfig) -> dict[str, str]:
    env = dict(os.environ)
    if pg.password:
        env["PGPASSWORD"] = pg.password
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


async def _query(pg: PostgresConfig, sql: str, db: str | None = None) -> str:
    """Run a single-row, single-column SELECT and return the value as a string."""
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
    migration name is a reliable, auth-free way to tell which NetBox version
    is on the other side without hitting NetBox's API.
    """
    pg = endpoint.postgres
    # MAX(name) per app gives the most-recently-applied migration in each app.
    # Concatenating them produces a stable fingerprint that differs whenever
    # either side has migrations the other doesn't.
    sql = (
        "SELECT string_agg(app || ':' || name, ',' ORDER BY app) "
        "FROM (SELECT app, MAX(name) AS name FROM django_migrations GROUP BY app) m;"
    )
    try:
        return await _query(pg, sql)
    except PgError as e:
        raise PgError(f"reading schema fingerprint on {endpoint.name}: {e}") from e


async def dump(source: EndpointConfig, dump_path: str) -> int:
    """Run pg_dump against the source PG; write the custom-format dump to dump_path.

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
    """Drop the target DB, recreate it, restore the dump."""
    pg = target.postgres

    # Kill existing connections so DROP DATABASE doesn't error out
    terminate_sql = (
        f"SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
        f"WHERE datname = '{pg.db}' AND pid <> pg_backend_pid();"
    )
    env = _env(pg)
    await _run(
        ["psql", *_conn_args(pg, db="postgres"), "-v", "ON_ERROR_STOP=1", "-c", terminate_sql],
        env, f"terminate connections on {target.name}",
    )
    await _run(
        ["psql", *_conn_args(pg, db="postgres"), "-v", "ON_ERROR_STOP=1",
         "-c", f"DROP DATABASE IF EXISTS {pg.db};"],
        env, f"drop database on {target.name}",
    )
    await _run(
        ["psql", *_conn_args(pg, db="postgres"), "-v", "ON_ERROR_STOP=1",
         "-c", f'CREATE DATABASE {pg.db} OWNER "{pg.user}";'],
        env, f"create database on {target.name}",
    )

    logger.info("pg_restore start target=%s host=%s", target.name, pg.host)
    await _run(
        [
            "pg_restore",
            "-j", "4",
            "--no-owner", "--no-acl",
            *_conn_args(pg),
            dump_path,
        ],
        env, f"pg_restore on {target.name}",
    )
    logger.info("pg_restore done target=%s", target.name)


async def smoke_check(target: EndpointConfig) -> dict[str, str]:
    """Cheap sanity queries against the freshly restored target."""
    pg = target.postgres
    return {
        "dcim_device_count": await _query(pg, "SELECT COUNT(*) FROM dcim_device;"),
        "last_objectchange": await _query(pg, "SELECT COALESCE(MAX(time)::text, 'never') FROM core_objectchange;"),
    }
