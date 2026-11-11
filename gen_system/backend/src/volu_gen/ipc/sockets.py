"""ZeroMQ sockets for the three links (docs/architecture.md → IPC,
decision 0007). The child binds and the server connects, so a restarted
server reconnects to a running child.

| Link                    | Child (binds)               | Server (connects)          |
|-------------------------|-----------------------------|----------------------------|
| server ↔ engine control | `bind_router`               | `connect_dealer`           |
| engine → server preview | `bind_pub`                  | `connect_sub`              |
| server ↔ worker jobs    | `bind_router`               | `connect_dealer`           |

The functions take a `zmq.Context` or a `zmq.asyncio.Context` (the
server's), and the socket type follows the context.
"""

from __future__ import annotations

import time

import zmq
from zmq.utils.monitor import recv_monitor_message

from .messages import Msg, encode

DEFAULT_HOST = "127.0.0.1"
# Fixed defaults next to the HTTP port (8770); the server passes the ports
# to the children when it starts them.
ENGINE_CONTROL_PORT = 8771
ENGINE_PREVIEW_PORT = 8772
WORKER_PORT = 8773
HEARTBEAT_IVL_MS = 1000
HEARTBEAT_TIMEOUT_MS = 3000   # a peer silent this long is disconnected
# The server's DEALERs keep this routing id across reconnects, so a child's
# ROUTER reaches the server under the same id after a reconnect.
SERVER_ID = b"server"


class PortInUse(OSError):
    """The port is bound by another process."""


def address(port: int, host: str = DEFAULT_HOST) -> str:
    return f"tcp://{host}:{port}"


def _common(sock: zmq.Socket):
    sock.setsockopt(zmq.LINGER, 0)
    sock.setsockopt(zmq.HEARTBEAT_IVL, HEARTBEAT_IVL_MS)
    sock.setsockopt(zmq.HEARTBEAT_TIMEOUT, HEARTBEAT_TIMEOUT_MS)
    sock.setsockopt(zmq.HEARTBEAT_TTL, HEARTBEAT_TIMEOUT_MS)


def _bind(sock: zmq.Socket, port: int, host: str) -> zmq.Socket:
    try:
        sock.bind(address(port, host))
    except zmq.ZMQError as e:
        sock.close()
        if e.errno == zmq.EADDRINUSE:
            raise PortInUse(f"{address(port, host)} is in use (is another volu process running?)") from e
        raise
    return sock


def bind_router(ctx: zmq.Context, port: int, host: str = DEFAULT_HOST) -> zmq.Socket:
    """A child's control socket. Sends to an absent peer raise
    EHOSTUNREACH (ROUTER_MANDATORY) instead of vanishing; a reconnect under
    the same routing id takes over the old connection (ROUTER_HANDOVER)."""
    sock = ctx.socket(zmq.ROUTER)
    _common(sock)
    sock.setsockopt(zmq.ROUTER_MANDATORY, 1)
    sock.setsockopt(zmq.ROUTER_HANDOVER, 1)
    return _bind(sock, port, host)


def connect_dealer(ctx: zmq.Context, port: int, host: str = DEFAULT_HOST,
                   routing_id: bytes = SERVER_ID) -> zmq.Socket:
    """The server's end of a control link. IMMEDIATE: nothing is queued
    while the child is down, so a non-blocking send raises zmq.Again,
    meaning "peer down"."""
    sock = ctx.socket(zmq.DEALER)
    _common(sock)
    sock.setsockopt(zmq.IMMEDIATE, 1)
    sock.setsockopt(zmq.ROUTING_ID, routing_id)
    sock.connect(address(port, host))
    return sock


def bind_pub(ctx: zmq.Context, port: int, host: str = DEFAULT_HOST) -> zmq.Socket:
    """The engine's preview socket. A PUB send never blocks: at its HWM it
    drops. The HWM stays at the default (1000): with a small one (1–64)
    the PUB drops the last frames of a burst, which are the newest, even
    when the server is keeping up (decision 0007 → Amendment). Frames
    queue here only while the server process is frozen."""
    sock = ctx.socket(zmq.PUB)
    sock.setsockopt(zmq.LINGER, 0)
    return _bind(sock, port, host)


def connect_sub(ctx: zmq.Context, port: int, host: str = DEFAULT_HOST) -> zmq.Socket:
    """The server's preview socket: latest-only (CONFLATE)."""
    sock = ctx.socket(zmq.SUB)
    sock.setsockopt(zmq.LINGER, 0)
    sock.setsockopt(zmq.CONFLATE, 1)   # before connect
    sock.setsockopt(zmq.SUBSCRIBE, b"")
    sock.connect(address(port, host))
    return sock


def send_to(router: zmq.Socket, peer: bytes, msg: Msg) -> bool:
    """Send from a ROUTER without blocking. False if the peer is not
    connected (or its queue is full); the message is then dropped."""
    try:
        router.send_multipart([peer, encode(msg)], zmq.NOBLOCK)
        return True
    except zmq.ZMQError as e:
        if e.errno in (zmq.EHOSTUNREACH, zmq.EAGAIN):
            return False
        raise


class PeerWatch:
    """Counts the peers connected to a bound socket, from its monitor
    events, and how long it has had none. A peer that stops answering
    heartbeats counts as disconnected after HEARTBEAT_TIMEOUT_MS.

    Poll `monitor` with the socket and call `update()` when it is readable
    (or just every loop: it never blocks).
    """

    def __init__(self, sock: zmq.Socket):
        self._sock = sock
        self.monitor = sock.get_monitor_socket(zmq.EVENT_ACCEPTED | zmq.EVENT_DISCONNECTED)
        self.peers = 0
        self._alone_since: float | None = time.monotonic()

    def update(self):
        while self.monitor.poll(0):
            event = recv_monitor_message(self.monitor)["event"]
            if event == zmq.EVENT_ACCEPTED:
                self.peers += 1
            elif event == zmq.EVENT_DISCONNECTED:
                self.peers = max(self.peers - 1, 0)
        if self.peers:
            self._alone_since = None
        elif self._alone_since is None:
            self._alone_since = time.monotonic()

    def alone_for(self) -> float:
        """Seconds since the last peer left (or since the watch started), 0
        while a peer is connected."""
        return 0.0 if self._alone_since is None else time.monotonic() - self._alone_since

    def close(self):
        self._sock.disable_monitor()
        self.monitor.close()
