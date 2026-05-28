"""Typed configuration loaded from a single YAML file at /etc/netbox-sync/config.yaml.

The sync service is a standalone Docker container; it reaches both Postgres
hosts over TCP. No SSH, no NetBox host access required.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

import yaml


@dataclass
class PostgresConfig:
    host: str
    user: str
    db: str
    port: int = 5432
    password_file: Optional[str] = None
    # Optional separate admin role used only for DROP/CREATE DATABASE on the
    # target side. Useful when the application role (netbox) does not have
    # CREATEDB — common with K8s-managed Postgres operators that grant only
    # the app-scoped role. If unset, the main user is used for everything.
    admin_user: Optional[str] = None
    admin_password_file: Optional[str] = None

    @property
    def password(self) -> Optional[str]:
        if self.password_file and Path(self.password_file).exists():
            return Path(self.password_file).read_text().strip()
        return None

    @property
    def admin_password(self) -> Optional[str]:
        if self.admin_password_file and Path(self.admin_password_file).exists():
            return Path(self.admin_password_file).read_text().strip()
        return None


@dataclass
class EndpointConfig:
    name: str
    postgres: PostgresConfig
    staging_dir: str = "/var/lib/netbox-sync/staging"


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
        """Atomically swap source and target blocks in the YAML config file."""
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
        postgres=PostgresConfig(**d["postgres"]),
        staging_dir=d.get("staging_dir", "/var/lib/netbox-sync/staging"),
    )
