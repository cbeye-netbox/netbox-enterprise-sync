"""Endpoint adapter contract.

An Endpoint represents one NetBox cluster (active or passive) from the
orchestrator's perspective. The adapter abstracts whether the endpoint is
reachable directly on this host (`local`) or only via ssh.

Pipelines never branch on transport — they compose these primitives.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Optional

from ..config import EndpointConfig


class Endpoint(ABC):
    def __init__(self, config: EndpointConfig) -> None:
        self.config = config

    @property
    def name(self) -> str:
        return self.config.name

    @property
    def is_local(self) -> bool:
        return self.config.transport == "local"

    @abstractmethod
    def wrap(self, command: list[str]) -> list[str]:
        """Return `command` ready to be exec'd to run on this endpoint.

        Local: returns command unchanged.
        SSH:   prepends ssh + connection args.
        """

    @abstractmethod
    def rsync_url(self, path: str) -> str:
        """Return an rsync-compatible URL for `path` on this endpoint.

        Local: `path` itself.
        SSH:   `user@host:path`.
        """

    @abstractmethod
    def rsync_ssh_opt(self) -> Optional[list[str]]:
        """Return ['-e', 'ssh ...'] if rsync needs ssh transport for this endpoint.

        Returns None when the endpoint is local.
        """
