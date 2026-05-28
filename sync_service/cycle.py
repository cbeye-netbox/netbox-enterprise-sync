"""One sync cycle — version gate → quiesce → dump → restore → media → redis health → smoke → resume.

Reports, scripts, and configuration.py are deliberately NOT synced; they belong
in the NetBox container image. Redis is verified, not snapshot-replicated.
"""
from __future__ import annotations

import asyncio
import logging
import os
import shlex
import time
from pathlib import Path
from typing import Optional

from .adapters import Endpoint, make_endpoint
from .alerts import Alerter
from .config import Config
from .pipelines import media, postgres, redis
from .state import State

logger = logging.getLogger(__name__)


class CycleError(RuntimeError):
    pass


async def run_one_cycle(cfg: Config, state: State, alerter: Alerter) -> dict:
    """Run a single sync cycle. Returns a dict describing the outcome.

    Raises if the cycle fails. The caller is the scheduler loop which logs and continues.
    """
    if state.cycle_lock.locked():
        raise CycleError("cycle already in flight")

    async with state.cycle_lock:
        return await _run_locked(cfg, state, alerter)


async def _run_locked(cfg: Config, state: State, alerter: Alerter) -> dict:
    started = time.time()
    state.in_flight = True
    entry: dict = {
        "started_at": started,
        "source": cfg.source.name,
        "target": cfg.target.name,
        "status": "in_progress",
    }

    source = make_endpoint(cfg.source)
    target = make_endpoint(cfg.target)

    try:
        # 1. Version gate
        sv = await read_version(source)
        tv = await read_version(target)
        entry["source_version"] = sv
        entry["target_version"] = tv
        if sv != tv:
            raise CycleError(f"NetBox version mismatch: source={sv!r} target={tv!r}")

        # 2. Quiesce target NetBox web/workers
        await _run_optional(target.config.quiesce_cmd, target, "quiesce")

        try:
            # 3. Dump on source → stage on orchestrator disk
            Path(cfg.sync.orchestrator_staging_dir).mkdir(parents=True, exist_ok=True)
            dump_path = os.path.join(cfg.sync.orchestrator_staging_dir, "db.dump")
            entry["dump_bytes"] = await postgres.dump(source, dump_path)

            # 4. Restore on target (ships dump to target side if remote)
            await postgres.restore(target, dump_path)

            # 5. Media rsync
            await media.sync(source, target, cfg.sync.orchestrator_staging_dir)

            # 6. Redis replication health
            entry["redis"] = await redis.check_replication_health(target)

            # 7. Smoke checks against the restored target
            entry["smoke"] = await postgres.smoke_check(target)
        finally:
            # Always try to bring the target back up, even if mid-cycle steps failed
            await _run_optional(target.config.resume_cmd, target, "resume")

        finished = time.time()
        entry["finished_at"] = finished
        entry["duration_seconds"] = round(finished - started, 3)
        entry["status"] = "success"
        state.record_success(finished)
        return entry

    except Exception as e:
        finished = time.time()
        entry["finished_at"] = finished
        entry["duration_seconds"] = round(finished - started, 3)
        entry["status"] = "failure"
        entry["error"] = str(e)
        state.record_failure(finished)
        await alerter.send(
            f"NetBox sync failed: {cfg.source.name} → {cfg.target.name}",
            str(e),
        )
        raise
    finally:
        state.log_cycle(entry)
        state.in_flight = False


async def read_version(endpoint: Endpoint) -> str:
    cmd = endpoint.wrap(["cat", endpoint.config.version_file])
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, err = await proc.communicate()
    if proc.returncode != 0:
        raise CycleError(
            f"could not read version on {endpoint.name}: "
            f"{err.decode(errors='replace').strip()}"
        )
    return out.decode().strip()


async def _run_optional(cmd_str: Optional[str], endpoint: Endpoint, label: str) -> None:
    """Run quiesce/resume command on the endpoint. Best-effort: log on failure, don't raise."""
    if not cmd_str:
        return
    argv = shlex.split(cmd_str)
    wrapped = endpoint.wrap(argv)
    logger.info("%s on %s", label, endpoint.name)
    proc = await asyncio.create_subprocess_exec(
        *wrapped,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, err = await proc.communicate()
    if proc.returncode != 0:
        logger.warning(
            "%s on %s failed (rc=%d): %s",
            label, endpoint.name, proc.returncode,
            err.decode(errors='replace').strip(),
        )
