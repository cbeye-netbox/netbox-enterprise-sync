"""One sync cycle — schema-fingerprint gate → pg_dump → pg_restore → smoke check.

Postgres is the only thing synced. Media and Redis are out of scope; we don't
touch the NetBox application at all.
"""
from __future__ import annotations

import logging
import os
import time
from pathlib import Path

from .alerts import Alerter
from .config import Config
from .pipelines import postgres
from .state import State

logger = logging.getLogger(__name__)


class CycleError(RuntimeError):
    pass


async def run_one_cycle(cfg: Config, state: State, alerter: Alerter) -> dict:
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
    try:
        # 1. Schema-fingerprint gate.
        # Compare the latest applied Django migration per app on both sides. If
        # they don't match, the two NetBox installs are running different
        # versions (or different plugin sets) and a restore would corrupt the
        # passive — abort.
        src_fp = await postgres.schema_fingerprint(cfg.source)
        tgt_fp = await postgres.schema_fingerprint(cfg.target)
        entry["source_fingerprint"] = src_fp
        entry["target_fingerprint"] = tgt_fp
        if src_fp != tgt_fp:
            raise CycleError(
                "schema fingerprint mismatch — NetBox versions or plugin sets differ "
                "between source and target. Bring them in sync before resuming."
            )

        # 2. Dump on source → staging file on the orchestrator's disk.
        Path(cfg.sync.orchestrator_staging_dir).mkdir(parents=True, exist_ok=True)
        dump_path = os.path.join(cfg.sync.orchestrator_staging_dir, "db.dump")
        entry["dump_bytes"] = await postgres.dump(cfg.source, dump_path)

        # 3. Restore onto target (drop+recreate+pg_restore -j 4).
        await postgres.restore(cfg.target, dump_path)

        # 4. Smoke check the restored DB.
        entry["smoke"] = await postgres.smoke_check(cfg.target)

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
