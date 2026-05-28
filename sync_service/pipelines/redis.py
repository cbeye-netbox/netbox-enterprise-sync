"""Redis replication health check.

We do NOT snapshot Redis between cycles — sessions and in-flight RQ jobs would
get mangled. Instead, the passive Redis is expected to run as a continuous
`REPLICAOF` of the active Redis (configured outside this service). Each cycle
just verifies the replication topology is healthy on the target.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Optional

from ..adapters import Endpoint

logger = logging.getLogger(__name__)


class RedisCheckError(RuntimeError):
    pass


async def check_replication_health(target: Endpoint) -> dict:
    """Verify target Redis is a healthy replica.

    Expected: role in {slave, replica}, master_link_status = up.
    """
    redis = target.config.redis
    cmd = target.wrap([
        "redis-cli",
        "-h", redis.host,
        "-p", str(redis.port),
        "INFO", "replication",
    ])
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, err = await proc.communicate()
    if proc.returncode != 0:
        raise RedisCheckError(
            f"redis-cli failed on {target.name} (rc={proc.returncode}): "
            f"{err.decode(errors='replace').strip()}"
        )

    info = _parse_info(out.decode())
    role = info.get("role")
    link = info.get("master_link_status")

    if role not in {"slave", "replica"}:
        raise RedisCheckError(
            f"target {target.name} Redis role is {role!r}; expected slave/replica. "
            "Configure 'replicaof <active-redis-host> <port>' on the passive Redis."
        )
    if link != "up":
        raise RedisCheckError(
            f"target {target.name} Redis master_link_status is {link!r}; expected 'up'"
        )
    return {"role": role, "master_link_status": link}


def _parse_info(text: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    for line in text.splitlines():
        if ":" in line and not line.startswith("#"):
            k, _, v = line.partition(":")
            fields[k.strip()] = v.strip()
    return fields
