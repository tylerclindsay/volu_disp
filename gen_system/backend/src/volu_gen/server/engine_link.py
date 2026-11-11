"""The server's end of the engine links (docs/architecture.md → IPC,
decision 0007): control (DEALER → the engine's ROUTER) and preview (SUB ←
the engine's PUB).

- On every (re)connect the server sends `hello`; the engine's reply
  carries its instance id, PID and slots. Then the server sends the full
  `configure`, and the player adopts or restores the slots.
- Nothing but `hello` goes out before the reply: the server's DEALER has
  IMMEDIATE, and a command sent to an engine that has not answered could
  act on state the engine does not have.
- The engine's `status` (~20 Hz) is its heartbeat. Silence for
  SILENCE_TIMEOUT s on a connected engine means it hangs: the server kills
  and restarts it. ZeroMQ heartbeats would not show this, because libzmq
  answers them from its own thread.
- The server keeps the set of animation files the engine may have open
  (sent in `load`/`swap_anim`, until `released`), so cleanup never deletes
  one.
"""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING

import zmq
import zmq.asyncio
from zmq.utils.monitor import parse_monitor_message

from ..ipc import (FROM_ENGINE, EngineError, EngineHello, Hello, Load, MessageError, Msg, Released, Status,
                   SwapAnim, TrackEnded, TrackStarted, connect_dealer, connect_sub, decode, encode)
from ..ipc.messages import PREVIEW_HEADER
from ..ipc.sockets import HEARTBEAT_TIMEOUT_MS

if TYPE_CHECKING:
    from .app import Server
    from .children import Child

SILENCE_TIMEOUT = HEARTBEAT_TIMEOUT_MS / 1000   # s without a message from a connected engine
HELLO_RETRY = 1.0                               # s between hellos until the engine answers


class EngineLink:
    def __init__(self, server: Server, ctx: zmq.asyncio.Context, control_port: int, preview_port: int,
                 host: str, child: Child | None = None):
        self.s = server
        self.child = child
        self.sock = connect_dealer(ctx, control_port, host)
        self.monitor = self.sock.get_monitor_socket(zmq.EVENT_CONNECTED | zmq.EVENT_DISCONNECTED)
        self.preview = connect_sub(ctx, preview_port, host)
        self.connected = False      # a TCP connection to the engine exists
        self.ready = False          # it answered this connection's hello
        self.hello: EngineHello | None = None
        self.status: Status | None = None
        self.open_anims: set[str] = set()
        self._last_msg = 0.0
        self._hello_sent = 0.0
        self.answered = asyncio.Event()   # set on every hello reply (startup waits on it)

    # -- sending
    def send(self, msg: Msg) -> bool:
        """Send without blocking. False when the engine is not ready (down,
        or has not answered hello); the message is dropped."""
        if not self.ready and not isinstance(msg, Hello):
            return False
        try:
            self.sock.send(encode(msg), zmq.NOBLOCK)
        except zmq.Again:
            return False
        if isinstance(msg, (Load, SwapAnim)):
            self.open_anims.add(msg.anim_path)
        return True

    def _say_hello(self):
        self._hello_sent = time.monotonic()
        self.send(Hello())

    # -- loops
    async def run(self):
        await asyncio.gather(self._control(), self._events(), self._previews(), self._watch())

    async def _events(self):
        while True:
            ev = parse_monitor_message(await self.monitor.recv_multipart())["event"]
            if ev == zmq.EVENT_CONNECTED:
                self.connected = True
                self._say_hello()
            elif ev == zmq.EVENT_DISCONNECTED:
                self.connected = self.ready = False
                self.s.state_changed()

    async def _control(self):
        while True:
            data = await self.sock.recv()
            try:
                msg = decode(FROM_ENGINE, data)
            except MessageError as e:
                self.s.log(f"engine: dropped a bad message: {e}")
                continue
            self._last_msg = time.monotonic()
            if isinstance(msg, EngineHello):
                self._on_hello(msg)
            elif not self.ready:
                continue   # from before this connection's hello
            elif isinstance(msg, Status):
                self.status = msg
                self.s.player.on_status(msg)
                self.s.hub.latest("status", msg.model_dump(mode="json"))
            elif isinstance(msg, TrackStarted):
                self.s.player.on_track_started(msg.track_id)
            elif isinstance(msg, TrackEnded):
                self.s.player.on_track_ended(msg.track_id)
            elif isinstance(msg, Released):
                self.open_anims.discard(msg.anim_path)
                self.s.sweep_soon()
            elif isinstance(msg, EngineError):
                self.s.log(f"engine error ({msg.kind}): {msg.message}")
                self.s.hub.event("error", source="engine", kind=msg.kind, message=msg.message)

    def _on_hello(self, hello: EngineHello):
        new = self.hello is None or hello.instance_id != self.hello.instance_id
        self.hello, self.ready = hello, True
        if new:
            self.open_anims = {s.anim_path for s in hello.slots}
            self.s.log(f"engine: instance {hello.instance_id}, pid {hello.pid}, {hello.state}")
        if self.child is not None:
            self.child.adopt(hello.pid)
        self.send(self.s.configure_msg())
        self.s.player.on_hello(hello)
        self.answered.set()
        self.s.state_changed()

    async def _previews(self):
        while True:
            data = await self.preview.recv()
            if len(data) >= PREVIEW_HEADER.size and self.s.hub.clients:
                self.s.hub.preview(data)

    async def _watch(self):
        while True:
            await asyncio.sleep(0.25)
            now = time.monotonic()
            if self.connected and not self.ready and now - self._hello_sent > HELLO_RETRY:
                self._say_hello()
            if self.child is None:
                continue
            if self.ready and now - self._last_msg > SILENCE_TIMEOUT:
                self.s.log(f"engine: silent for {SILENCE_TIMEOUT:g} s; restarting it")
                await self.restart()
            elif not self.connected and self.child.adopted and now - self._last_msg > SILENCE_TIMEOUT:
                # no process handle: a lost connection is the only sign that an adopted engine died
                self.s.log("engine: the adopted engine is gone; starting a new one")
                await self.restart()

    async def restart(self):
        self.ready = False
        self.lost()
        await self.child.restart()

    def lost(self):
        """The engine process is gone: it holds and plays nothing."""
        self.open_anims.clear()
        self.status = None
        self.s.player.engine_lost()

    def close(self):
        self.sock.disable_monitor()
        for sock in (self.monitor, self.sock, self.preview):
            sock.close()
