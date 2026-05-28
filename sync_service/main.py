"""Service entrypoint: scheduler loop + control API served from one process.

Run with:  python -m sync_service.main
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import sys

import uvicorn

from .alerts import Alerter
from .config import Config
from .control import create_app
from .cycle import run_one_cycle
from .state import State


logger = logging.getLogger("sync_service")


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "time": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "name": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def setup_logging() -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(logging.INFO)
    logging.getLogger("uvicorn.error").setLevel(logging.INFO)
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)


class Scheduler:
    def __init__(self, cfg: Config, state: State, alerter: Alerter) -> None:
        self.cfg = cfg
        self.state = state
        self.alerter = alerter
        self._wake = asyncio.Event()
        self._stop = asyncio.Event()

    def trigger_cycle_now(self) -> None:
        self._wake.set()

    def reload_config(self) -> None:
        self.cfg = Config.load(self.cfg.config_path)
        self.alerter = Alerter(self.cfg.alerts.webhook_url)

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    async def run(self) -> None:
        # Run once at startup if enabled, then on interval
        first = True
        while not self._stop.is_set():
            if not first:
                try:
                    await asyncio.wait_for(self._wake.wait(), timeout=self.cfg.sync.interval_seconds)
                except asyncio.TimeoutError:
                    pass
                finally:
                    self._wake.clear()
            first = False

            if self._stop.is_set():
                break
            if not self.state.enabled:
                logger.info("sync disabled; skipping cycle")
                continue

            try:
                entry = await run_one_cycle(self.cfg, self.state, self.alerter)
                logger.info(
                    "cycle ok source=%s target=%s duration=%.2fs",
                    entry["source"], entry["target"], entry["duration_seconds"],
                )
            except Exception as e:
                logger.exception("cycle failed: %s", e)


async def main() -> None:
    setup_logging()
    config_path = os.environ.get("CONFIG_PATH", "/etc/netbox-sync/config.yaml")
    cfg = Config.load(config_path)
    state = State(cfg)
    alerter = Alerter(cfg.alerts.webhook_url)
    scheduler = Scheduler(cfg, state, alerter)
    app = create_app(state, scheduler)

    server = uvicorn.Server(uvicorn.Config(
        app,
        host=cfg.control_api.bind_host,
        port=cfg.control_api.bind_port,
        log_config=None,
        access_log=False,
    ))

    loop = asyncio.get_running_loop()
    def _stop(*_: object) -> None:
        scheduler.stop()
        server.should_exit = True
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, _stop)

    logger.info(
        "starting orchestrator source=%s target=%s interval=%ds bind=%s",
        cfg.source.name, cfg.target.name, cfg.sync.interval_seconds, cfg.control_api.bind,
    )
    await asyncio.gather(scheduler.run(), server.serve())


if __name__ == "__main__":
    asyncio.run(main())
