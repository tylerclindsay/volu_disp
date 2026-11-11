"""The UI's WebSocket (`/api/ws`, docs/architecture.md → HTTP API →
WebSocket): live state pushed to every connected browser.

Each client has its own sender task, so a slow browser only delays
itself. Two kinds of message:
- **Events** (`changed`, `error`, `log`) go out in order. A client that
  falls MAX_EVENTS behind is dropped; the UI reconnects and refetches.
- **Latest-only** messages (`state`, `status`, `progress` per job, and
  binary preview frames) replace a waiting one of the same key, so a
  slow client skips to the newest. The server never queues them.
"""

from __future__ import annotations

import asyncio
import json
from collections import deque
from typing import Any

from fastapi import WebSocket, WebSocketDisconnect

MAX_EVENTS = 1000
PREVIEW = "preview"


class _Client:
    def __init__(self, ws: WebSocket):
        self.ws = ws
        self.events: deque[str] = deque()
        self.latest: dict[str, str | bytes] = {}
        self.wake = asyncio.Event()
        self.overrun = False

    def push(self, text: str):
        if len(self.events) >= MAX_EVENTS:
            self.overrun = True
        else:
            self.events.append(text)
        self.wake.set()

    def put(self, key: str, data: str | bytes):
        self.latest[key] = data
        self.wake.set()

    async def send_loop(self):
        try:
            while not self.overrun:
                await self.wake.wait()
                self.wake.clear()
                while self.events:
                    await self.ws.send_text(self.events.popleft())
                while self.latest and not self.events:
                    key = next(iter(self.latest))
                    data = self.latest.pop(key)
                    await (self.ws.send_bytes(data) if isinstance(data, bytes) else self.ws.send_text(data))
        except (WebSocketDisconnect, RuntimeError, OSError):
            pass   # the browser went away


def _dumps(msg: dict[str, Any]) -> str:
    return json.dumps(msg, separators=(",", ":"))


class Hub:
    def __init__(self):
        self._clients: set[_Client] = set()

    @property
    def clients(self) -> int:
        return len(self._clients)

    def event(self, type: str, **fields):
        text = _dumps({"type": type, **fields})
        for c in self._clients:
            c.push(text)

    def changed(self, what: str, track_id: int | None = None):
        """A resource the UI fetched over HTTP changed: refetch it.
        what: tracks | track (with track_id: its analysis, plan, versions) |
        jobs | queue | settings."""
        if track_id is None:
            self.event("changed", what=what)
        else:
            self.event("changed", what=what, track_id=track_id)

    def latest(self, key: str, msg: dict[str, Any]):
        text = _dumps(msg)
        for c in self._clients:
            c.put(key, text)

    def preview(self, data: bytes):
        for c in self._clients:
            c.put(PREVIEW, data)

    async def serve(self, ws: WebSocket, first: dict[str, Any]):
        """Run one client until it disconnects. `first` (the state
        snapshot) goes out before anything else."""
        await ws.accept()
        client = _Client(ws)
        self._clients.add(client)   # before the snapshot's send, so nothing after it is missed
        sender = None
        try:
            await ws.send_text(_dumps(first))
            sender = asyncio.create_task(client.send_loop())
            receiver = asyncio.create_task(self._drain(ws))
            await asyncio.wait({sender, receiver}, return_when=asyncio.FIRST_COMPLETED)
            receiver.cancel()
        except (WebSocketDisconnect, RuntimeError, OSError):
            pass
        finally:
            self._clients.discard(client)
            if sender is not None:
                sender.cancel()
        if client.overrun:
            await ws.close(code=1013, reason="too far behind; reconnect")

    @staticmethod
    async def _drain(ws: WebSocket):
        """The UI sends nothing; this only waits for the disconnect."""
        try:
            while True:
                await ws.receive_text()
        except (WebSocketDisconnect, RuntimeError):
            pass
