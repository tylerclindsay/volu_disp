"""Node detection (docs/architecture.md → API server, docs/ui.md →
Output): until node status packets (protocol type 3) exist, a drive node
is online when its configured IP answers a ping. The server pushes the
online set to the engine in `configure`; armed output goes only to those
nodes. This moves into the engine when type 3 exists.

Pings run in threads (the server's selector event loop can't run
subprocesses on Windows), all nodes at once, every INTERVAL s.
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor

INTERVAL = 2.0     # s between rounds
TIMEOUT_MS = 500   # per ping


def ping(ip: str, source: str | None = None, timeout_ms: int = TIMEOUT_MS) -> bool:
    """One ICMP echo. Checks for a reply line ("TTL="), not the exit code:
    Windows ping exits 0 on "Destination host unreachable" from a router."""
    if sys.platform == "win32":
        argv = ["ping", "-n", "1", "-w", str(timeout_ms)] + (["-S", source] if source else []) + [ip]
        flags = {"creationflags": subprocess.CREATE_NO_WINDOW}
    else:
        argv = ["ping", "-c", "1", "-W", str(max(1, round(timeout_ms / 1000)))] + \
            (["-I", source] if source else []) + [ip]
        flags = {}
    try:
        out = subprocess.run(argv, capture_output=True, text=True, timeout=timeout_ms / 1000 + 2, **flags)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return "ttl=" in out.stdout.lower()


class NodeWatch:
    """nodes      (node id, ip) of the display config
    source     IP of the display network interface to ping from (None: any)
    on_change  called with the new online set when it changes
    """

    def __init__(self, nodes: Sequence[tuple[int, str]], on_change: Callable[[frozenset[int]], None],
                 source: Callable[[], str | None] = lambda: None,
                 pinger: Callable[[str, str | None], bool] = ping, interval: float = INTERVAL):
        self.nodes = list(nodes)
        self.on_change = on_change
        self.source = source
        self.pinger = pinger
        self.interval = interval
        self.online: frozenset[int] = frozenset()
        self._pool = ThreadPoolExecutor(max_workers=max(1, min(32, len(self.nodes))),
                                        thread_name_prefix="ping")

    async def check(self) -> frozenset[int]:
        loop = asyncio.get_running_loop()
        src = self.source()
        up = await asyncio.gather(*(loop.run_in_executor(self._pool, self.pinger, ip, src)
                                    for _, ip in self.nodes))
        online = frozenset(n for (n, _), ok in zip(self.nodes, up) if ok)
        if online != self.online:
            self.online = online
            self.on_change(online)
        return online

    async def run(self):
        while True:
            await self.check()
            await asyncio.sleep(self.interval)

    def close(self):
        self._pool.shutdown(wait=False, cancel_futures=True)
