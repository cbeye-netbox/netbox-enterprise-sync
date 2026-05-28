import shlex
from typing import Optional

from .base import Endpoint


SSH_BASE_OPTS = [
    "-o", "StrictHostKeyChecking=accept-new",
    "-o", "BatchMode=yes",
    "-o", "ServerAliveInterval=30",
    "-o", "ServerAliveCountMax=4",
]


class SSHEndpoint(Endpoint):
    def _ssh_args(self) -> list[str]:
        ssh = self.config.ssh
        assert ssh is not None  # checked at config-load time
        return [
            "ssh",
            "-i", ssh.key_file,
            "-p", str(ssh.port),
            *SSH_BASE_OPTS,
            f"{ssh.user}@{ssh.host}",
        ]

    def wrap(self, command: list[str]) -> list[str]:
        # ssh sends everything after the host as a single command string to the
        # remote login shell. Shell-quote each argv element so multi-word args
        # (SQL fragments, paths with spaces) survive the trip.
        quoted = " ".join(shlex.quote(arg) for arg in command)
        return [*self._ssh_args(), quoted]

    def rsync_url(self, path: str) -> str:
        ssh = self.config.ssh
        assert ssh is not None
        return f"{ssh.user}@{ssh.host}:{path}"

    def rsync_ssh_opt(self) -> Optional[list[str]]:
        ssh = self.config.ssh
        assert ssh is not None
        opts = " ".join(SSH_BASE_OPTS)
        return ["-e", f"ssh -i {ssh.key_file} -p {ssh.port} {opts}"]
