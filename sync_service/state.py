"""Cycle state — enabled flag, last success/failure timestamps, JSONL cycle log.

State is filesystem-backed so it survives container restarts. The orchestrator
assumes it is the only writer; running two orchestrators against the same state
dir is unsupported.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Optional

from .config import Config


class State:
    def __init__(self, cfg: Config) -> None:
        self.enabled_file = Path(cfg.sync.enabled_file)
        self.cycle_log = Path(cfg.sync.cycle_log)
        base = self.enabled_file.parent
        base.mkdir(parents=True, exist_ok=True)
        self.cycle_log.parent.mkdir(parents=True, exist_ok=True)
        self.last_success_file = base / "last_success_at"
        self.last_failure_file = base / "last_failure_at"
        self.in_flight = False
        self.cycle_lock = asyncio.Lock()
        # Default to enabled if no flag file exists
        if not self.enabled_file.exists():
            self.set_enabled(True)

    @property
    def enabled(self) -> bool:
        if not self.enabled_file.exists():
            return True
        return self.enabled_file.read_text().strip() == "1"

    def set_enabled(self, value: bool) -> None:
        self.enabled_file.parent.mkdir(parents=True, exist_ok=True)
        self.enabled_file.write_text("1" if value else "0")

    @property
    def last_success_at(self) -> Optional[float]:
        return _read_timestamp(self.last_success_file)

    @property
    def last_failure_at(self) -> Optional[float]:
        return _read_timestamp(self.last_failure_file)

    def record_success(self, ts: float) -> None:
        self.last_success_file.write_text(str(ts))

    def record_failure(self, ts: float) -> None:
        self.last_failure_file.write_text(str(ts))

    def log_cycle(self, entry: dict) -> None:
        with open(self.cycle_log, "a") as f:
            f.write(json.dumps(entry, default=str) + "\n")


def _read_timestamp(path: Path) -> Optional[float]:
    if not path.exists():
        return None
    try:
        return float(path.read_text().strip())
    except (ValueError, OSError):
        return None
