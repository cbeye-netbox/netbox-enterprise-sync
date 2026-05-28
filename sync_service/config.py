"""Typed configuration loaded from a single YAML file at /etc/netbox-sync/config.yaml.

Only the orchestrator reads this. Endpoints (active/passive NetBox clusters)
are unaware of the orchestrator's existence.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Optional

import yaml

Transport = Literal["local", "ssh"]


@dataclass
class PostgresConfig:
    host: str
    user: str
    db: str
    port: int = 5432
    password_file: Optional[str] = None

    @property
    def password(self) -> Optional[str]:
        if self.password_file and Path(self.password_file).exists():
            return Path(self.password_file).read_text().strip()
        return None


@dataclass
class RedisConfig:
    host: str
    port: int = 6379


@dataclass
class PathsConfig:
    media: str


@dataclass
class SSHConfig:
    host: str
    user: str
    key_file: str
    port: int = 22


@dataclass
class EndpointConfig:
    name: str
    transport: Transport
    postgres: PostgresConfig
    redis: RedisConfig
    paths: PathsConfig
    version_file: str
    ssh: Optional[SSHConfig] = None
    quiesce_cmd: Optional[str] = None
    resume_cmd: Optional[str] = None
    staging_dir: str = "/tmp/netbox-sync"

    def __post_init__(self) -> None:
        if self.transport == "ssh" and self.ssh is None:
            raise ValueError(f"endpoint {self.name}: transport=ssh requires an ssh block")

    @property
    def is_local(self) -> bool:
        return self.transport == "local"


@dataclass
class SyncConfig:
    interval_seconds: int
    enabled_file: str
    orchestrator_staging_dir: str
    cycle_log: str


@dataclass
class ControlAPIConfig:
    bind: str
    token_file: str

    @property
    def token(self) -> Optional[str]:
        if self.token_file and Path(self.token_file).exists():
            return Path(self.token_file).read_text().strip() or None
        return None

    @property
    def bind_host(self) -> str:
        return self.bind.rsplit(":", 1)[0]

    @property
    def bind_port(self) -> int:
        return int(self.bind.rsplit(":", 1)[1])


@dataclass
class AlertsConfig:
    webhook_url: str = ""


@dataclass
class Config:
    sync: SyncConfig
    source: EndpointConfig
    target: EndpointConfig
    control_api: ControlAPIConfig
    alerts: AlertsConfig
    config_path: str

    @classmethod
    def load(cls, path: str) -> "Config":
        with open(path) as f:
            raw = yaml.safe_load(f)
        return cls(
            sync=SyncConfig(**raw["sync"]),
            source=_endpoint_from_dict(raw["source"]),
            target=_endpoint_from_dict(raw["target"]),
            control_api=ControlAPIConfig(**raw["control_api"]),
            alerts=AlertsConfig(**(raw.get("alerts") or {})),
            config_path=path,
        )

    def reverse_on_disk(self) -> None:
        """Atomically swap source and target blocks in the YAML config file.

        The reload of in-memory config happens after this returns; callers should
        call Config.load(path) again to pick up the new direction.
        """
        with open(self.config_path) as f:
            raw = yaml.safe_load(f)
        raw["source"], raw["target"] = raw["target"], raw["source"]
        tmp = self.config_path + ".tmp"
        with open(tmp, "w") as f:
            yaml.safe_dump(raw, f, sort_keys=False)
        os.replace(tmp, self.config_path)


def _endpoint_from_dict(d: dict[str, Any]) -> EndpointConfig:
    return EndpointConfig(
        name=d["name"],
        transport=d["transport"],
        postgres=PostgresConfig(**d["postgres"]),
        redis=RedisConfig(**d["redis"]),
        paths=PathsConfig(**d["paths"]),
        version_file=d["version_file"],
        ssh=SSHConfig(**d["ssh"]) if d.get("ssh") else None,
        quiesce_cmd=d.get("quiesce_cmd"),
        resume_cmd=d.get("resume_cmd"),
        staging_dir=d.get("staging_dir", "/tmp/netbox-sync"),
    )
