"""Media file sync via rsync.

If exactly one endpoint is local to the orchestrator, rsync can copy directly.
If both endpoints are remote (topology C), rsync can't address remote→remote in
one invocation, so we stage on the orchestrator's disk in two hops.
"""
from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

from ..adapters import Endpoint

logger = logging.getLogger(__name__)


class RsyncError(RuntimeError):
    pass


async def sync(source: Endpoint, target: Endpoint, orchestrator_staging: str) -> int:
    """Rsync media from source to target. Returns bytes transferred (best-effort)."""
    src = source.config.paths.media
    dst = target.config.paths.media

    # rsync trailing-slash semantics: "src/" copies contents into "dst/"
    src_url = source.rsync_url(src.rstrip("/") + "/")
    dst_url = target.rsync_url(dst.rstrip("/") + "/")

    if not source.is_local and not target.is_local:
        # Two-hop via orchestrator staging — necessary for remote→remote
        stage = os.path.join(orchestrator_staging, "media") + "/"
        Path(stage).mkdir(parents=True, exist_ok=True)
        logger.info("media sync pull source=%s -> staging", source.name)
        await _run_rsync(["rsync", "-a", "--delete",
                          *(source.rsync_ssh_opt() or []),
                          src_url, stage])
        logger.info("media sync push staging -> target=%s", target.name)
        await _run_rsync(["rsync", "-a", "--delete",
                          *(target.rsync_ssh_opt() or []),
                          stage, dst_url])
        return 0  # we don't bother summing the two hops

    ssh_opt = source.rsync_ssh_opt() or target.rsync_ssh_opt() or []
    logger.info("media sync direct source=%s -> target=%s", source.name, target.name)
    await _run_rsync(["rsync", "-a", "--delete", *ssh_opt, src_url, dst_url])
    return 0


async def _run_rsync(cmd: list[str]) -> None:
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise RsyncError(f"rsync failed (rc={proc.returncode}): {stderr.decode(errors='replace').strip()}")
