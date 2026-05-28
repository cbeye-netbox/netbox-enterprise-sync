"""Outbound alert webhook (Slack-compatible JSON: {title, detail})."""
from __future__ import annotations

import logging

import httpx

logger = logging.getLogger(__name__)


class Alerter:
    def __init__(self, webhook_url: str) -> None:
        self.webhook_url = webhook_url

    async def send(self, title: str, detail: str) -> None:
        if not self.webhook_url:
            return
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                await client.post(self.webhook_url, json={"title": title, "detail": detail})
        except Exception as e:
            logger.warning("alert webhook failed: %s", e)
