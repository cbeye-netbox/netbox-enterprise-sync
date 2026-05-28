"""Postgres dump + restore pipeline.

`pg_dump` runs on the source endpoint (locally in the orchestrator container if
the endpoint is local; via ssh on the remote host otherwise). Output is staged
on the orchestrator's disk. For a remote target, the staged dump is then
rsync'd onto the target's filesystem so `pg_restore` can run there with the
file local to it.
"""
from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import Optional

from ..adapters import Endpoint
from ..config import PostgresConfig

logger = logging.getLogger(__name__)


class PgError(RuntimeError):
    pass


def _pg_conn_args(pg: PostgresConfig, db: Optional[str] = None) -> list[str]:
    return [
        "-h", pg.host,
        "-p", str(pg.port),
        "-U", pg.user,
        "-d", db or pg.db,
        "-w",  # never prompt for password; rely on PGPASSWORD env
    ]


def _with_pgpassword(endpoint: Endpoint, pg_argv: list[str]) -> list[str]:
    """Prepend `env PGPASSWORD=<pw>` so password plumbing works on both local
    subprocess and ssh-shipped commands without any shell-quote acrobatics.
    """
    pw = endpoint.config.postgres.password
    if pw:
        return ["env", f"PGPASSWORD={pw}", *pg_argv]
    return pg_argv


async def _run_pg(endpoint: Endpoint, pg_argv: list[str]) -> None:
    cmd = endpoint.wrap(_with_pgpassword(endpoint, pg_argv))
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise PgError(
            f"{pg_argv[0]} failed on {endpoint.name} (rc={proc.returncode}): "
            f"{stderr.decode(errors='replace').strip()}"
        )


async def dump(source: Endpoint, dump_path: str) -> int:
    """Run pg_dump on `source`, stream output to `dump_path` on the orchestrator.

    Returns dump size in bytes.
    """
    pg = source.config.postgres
    Path(dump_path).parent.mkdir(parents=True, exist_ok=True)

    pg_argv = [
        "pg_dump",
        "-Fc", "-Z", "6",
        "--no-owner", "--no-acl",
        "--exclude-table-data=core_objectchange",
        *_pg_conn_args(pg),
    ]
    cmd = source.wrap(_with_pgpassword(source, pg_argv))

    logger.info("pg_dump start source=%s", source.name)
    with open(dump_path, "wb") as out:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=out,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()

    if proc.returncode != 0:
        raise PgError(
            f"pg_dump failed on {source.name} (rc={proc.returncode}): "
            f"{stderr.decode(errors='replace').strip()}"
        )

    size = Path(dump_path).stat().st_size
    logger.info("pg_dump done source=%s bytes=%d", source.name, size)
    return size


async def restore(target: Endpoint, local_dump_path: str) -> None:
    """Restore a dump onto `target`.

    For remote targets, the dump is shipped to `target.staging_dir` first so
    pg_restore can read it as a local file (custom format needs a file, not a
    pipe, to be useful with parallel restore).
    """
    pg = target.config.postgres

    target_dump_path = await _stage_dump_on_target(target, local_dump_path)

    # Terminate connections to the target DB so DROP DATABASE doesn't error
    terminate_sql = (
        f"SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
        f"WHERE datname = '{pg.db}' AND pid <> pg_backend_pid();"
    )
    await _run_pg(target, [
        "psql", *_pg_conn_args(pg, db="postgres"),
        "-v", "ON_ERROR_STOP=1",
        "-c", terminate_sql,
    ])
    await _run_pg(target, [
        "psql", *_pg_conn_args(pg, db="postgres"),
        "-v", "ON_ERROR_STOP=1",
        "-c", f"DROP DATABASE IF EXISTS {pg.db};",
    ])
    await _run_pg(target, [
        "psql", *_pg_conn_args(pg, db="postgres"),
        "-v", "ON_ERROR_STOP=1",
        "-c", f'CREATE DATABASE {pg.db} OWNER "{pg.user}";',
    ])

    logger.info("pg_restore start target=%s", target.name)
    await _run_pg(target, [
        "pg_restore",
        "-j", "4",
        "--no-owner", "--no-acl",
        *_pg_conn_args(pg),
        target_dump_path,
    ])
    logger.info("pg_restore done target=%s", target.name)


async def smoke_check(target: Endpoint) -> dict:
    """Run cheap invariant queries against the restored target."""
    pg = target.config.postgres
    counts: dict[str, str] = {}
    for label, sql in [
        ("dcim_device_count", "SELECT COUNT(*) FROM dcim_device;"),
        ("max_objectchange", "SELECT COALESCE(MAX(time), 'never') FROM core_objectchange;"),
    ]:
        cmd = target.wrap(_with_pgpassword(target, [
            "psql", *_pg_conn_args(pg),
            "-tA", "-v", "ON_ERROR_STOP=1",
            "-c", sql,
        ]))
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, err = await proc.communicate()
        if proc.returncode != 0:
            raise PgError(f"smoke check {label} failed: {err.decode(errors='replace').strip()}")
        counts[label] = out.decode().strip()
    return counts


async def _stage_dump_on_target(target: Endpoint, local_dump_path: str) -> str:
    if target.is_local:
        return local_dump_path

    remote_path = os.path.join(target.config.staging_dir, "db.dump")
    mkdir = await asyncio.create_subprocess_exec(
        *target.wrap(["mkdir", "-p", target.config.staging_dir]),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, err = await mkdir.communicate()
    if mkdir.returncode != 0:
        raise PgError(f"mkdir on {target.name} failed: {err.decode(errors='replace').strip()}")

    rsync_cmd = [
        "rsync", "-a",
        *(target.rsync_ssh_opt() or []),
        local_dump_path,
        target.rsync_url(remote_path),
    ]
    proc = await asyncio.create_subprocess_exec(
        *rsync_cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, err = await proc.communicate()
    if proc.returncode != 0:
        raise PgError(f"shipping dump to {target.name} failed: {err.decode(errors='replace').strip()}")
    return remote_path
