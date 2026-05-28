from typing import Optional

from .base import Endpoint


class LocalEndpoint(Endpoint):
    def wrap(self, command: list[str]) -> list[str]:
        return list(command)

    def rsync_url(self, path: str) -> str:
        return path

    def rsync_ssh_opt(self) -> Optional[list[str]]:
        return None
