"""WebSocket fan-out: every engine mutation is pushed to all screens watching a session."""

from __future__ import annotations

import asyncio
import json
import logging
from collections import defaultdict

from fastapi import WebSocket

log = logging.getLogger("ws")


class Broadcaster:
    def __init__(self) -> None:
        self._clients: dict[str, set[WebSocket]] = defaultdict(set)

    async def connect(self, session_id: str, ws: WebSocket) -> None:
        await ws.accept()
        self._clients[session_id].add(ws)

    def disconnect(self, session_id: str, ws: WebSocket) -> None:
        self._clients[session_id].discard(ws)

    async def publish(self, session_id: str, message: dict) -> None:
        clients = list(self._clients.get(session_id, ()))
        if not clients:
            return
        data = json.dumps(message, default=str)

        async def send(ws: WebSocket) -> None:
            try:
                await asyncio.wait_for(ws.send_text(data), timeout=2)
            except Exception:
                self.disconnect(session_id, ws)

        await asyncio.gather(*(send(ws) for ws in clients))


broadcaster = Broadcaster()
