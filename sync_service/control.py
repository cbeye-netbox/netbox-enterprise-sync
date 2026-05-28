"""Control API — FastAPI surface used by humans and the future NetBox plugin.

Routes that mutate state (`/pause`, `/resume`, `/sync-now`, `/reverse`) require
the X-Api-Token header to match the configured token file. Read-only routes
are unauthenticated to make health monitoring trivial.

The embedded dashboard is served at `/` (single HTML file from `static/`).
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Annotated, Optional

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import HTMLResponse

from .adapters import make_endpoint
from .state import State

logger = logging.getLogger(__name__)


STATIC_DIR = Path(__file__).parent / "static"


def create_app(state: State, scheduler) -> FastAPI:
    app = FastAPI(title="netbox-active-passive-sync", version="0.1.0", docs_url="/api-docs")

    def require_token(x_api_token: Annotated[Optional[str], Header()] = None) -> None:
        expected = scheduler.cfg.control_api.token
        if expected and x_api_token != expected:
            raise HTTPException(status_code=401, detail="invalid api token")

    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    async def index() -> HTMLResponse:
        return HTMLResponse((STATIC_DIR / "index.html").read_text(encoding="utf-8"))

    @app.get("/health")
    async def health() -> dict:
        return {
            "source": scheduler.cfg.source.name,
            "target": scheduler.cfg.target.name,
            "enabled": state.enabled,
            "in_flight": state.in_flight,
            "last_success_at": state.last_success_at,
            "last_failure_at": state.last_failure_at,
            "interval_seconds": scheduler.cfg.sync.interval_seconds,
        }

    @app.get("/state")
    async def get_state() -> dict:
        return {
            "source": scheduler.cfg.source.name,
            "target": scheduler.cfg.target.name,
            "enabled": state.enabled,
            "in_flight": state.in_flight,
        }

    @app.get("/version")
    async def get_version() -> dict:
        from .cycle import read_version
        out: dict[str, str] = {}
        for label, ep_cfg in (("source", scheduler.cfg.source), ("target", scheduler.cfg.target)):
            try:
                out[label] = await read_version(make_endpoint(ep_cfg))
            except Exception as e:
                out[label] = f"<error: {e}>"
        return out

    @app.get("/cycles")
    async def get_cycles(limit: int = 20) -> list[dict]:
        """Return the most recent N cycle log entries, newest first."""
        cycle_log = Path(scheduler.cfg.sync.cycle_log)
        if not cycle_log.exists():
            return []
        limit = max(1, min(limit, 500))
        # Tail efficiently: read whole file (cycle log stays small in practice)
        lines = cycle_log.read_text(encoding="utf-8", errors="replace").splitlines()
        entries: list[dict] = []
        for line in lines[-limit:]:
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        entries.reverse()
        return entries

    @app.post("/pause", dependencies=[Depends(require_token)])
    async def pause() -> dict:
        state.set_enabled(False)
        logger.info("sync paused via API")
        return {"enabled": False}

    @app.post("/resume", dependencies=[Depends(require_token)])
    async def resume() -> dict:
        state.set_enabled(True)
        logger.info("sync resumed via API")
        return {"enabled": True}

    @app.post("/sync-now", dependencies=[Depends(require_token)])
    async def sync_now() -> dict:
        if not state.enabled:
            raise HTTPException(409, "sync is paused; resume before triggering")
        if state.in_flight:
            raise HTTPException(409, "cycle already in flight")
        scheduler.trigger_cycle_now()
        return {"triggered": True}

    @app.post("/reverse", dependencies=[Depends(require_token)])
    async def reverse() -> dict:
        if state.in_flight:
            raise HTTPException(409, "cannot reverse while cycle is in flight")
        # Pause first so no cycle starts mid-swap
        state.set_enabled(False)
        scheduler.cfg.reverse_on_disk()
        scheduler.reload_config()
        logger.info(
            "direction reversed: new source=%s new target=%s; sync remains paused — call /resume after LB/DNS flip",
            scheduler.cfg.source.name, scheduler.cfg.target.name,
        )
        return {
            "reversed": True,
            "enabled": False,
            "new_source": scheduler.cfg.source.name,
            "new_target": scheduler.cfg.target.name,
        }

    return app
