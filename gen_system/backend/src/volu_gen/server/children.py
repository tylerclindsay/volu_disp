"""Starting, watching and stopping the engine and worker processes
(docs/architecture.md → API server: it starts the engine and the worker and
restarts them if they die).

A `Child` is either started by the server (it has a process handle and
sees the exit at once) or adopted (an engine that outlived a server
restart: only its PID from `hello` is known). Restarts back off from 1 s
to 60 s while the child keeps dying within RESET_AFTER s of its start.

Children start in their own process group (Windows) or session (POSIX),
so a Ctrl+C in the server's console stops only the server; the server
then stops them in order. A crash of the server leaves the engine playing.
"""

from __future__ import annotations

import asyncio
import importlib.util
import os
import signal
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path

import msgpack
import zmq

from ..ipc import DEFAULT_HOST, Hello, connect_dealer, encode

BACKOFF_MIN, BACKOFF_MAX = 1.0, 60.0   # s between restarts
RESET_AFTER = 30.0                     # s of uptime after which the backoff starts over
POLL = 0.25                            # s between exit checks


def module_argv(module: str, *args: str | int | Path) -> list[str]:
    return [sys.executable, "-m", module, *(str(a) for a in args)]


def module_exists(module: str) -> bool:
    try:
        return importlib.util.find_spec(module) is not None
    except ModuleNotFoundError:
        return False


def probe_hello(port: int, host: str = DEFAULT_HOST, timeout: float = 0.5) -> int | None:
    """The PID of a child that answers `hello` on its port, or None
    (blocking). The server uses it at startup to find a surviving worker."""
    ctx = zmq.Context()
    sock = connect_dealer(ctx, port, host, routing_id=b"probe")
    deadline = time.monotonic() + timeout
    try:
        while True:   # IMMEDIATE: the send fails until the connection is up
            try:
                sock.send(encode(Hello()), zmq.NOBLOCK)
                break
            except zmq.Again:
                if time.monotonic() > deadline:
                    return None
                time.sleep(0.01)
        while (left := deadline - time.monotonic()) > 0:
            if sock.poll(int(left * 1000) + 1):
                try:
                    msg = msgpack.unpackb(sock.recv())
                except ValueError:
                    continue
                if isinstance(msg, dict) and msg.get("type") == "hello" and isinstance(msg.get("pid"), int):
                    return msg["pid"]
        return None
    finally:
        sock.close()
        ctx.term()


def kill_pid(pid: int, timeout: float = 5.0) -> bool:
    """Kill a process the server did not start, and wait for it to exit.
    True if it is gone (or was already)."""
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes
        SYNCHRONIZE, PROCESS_TERMINATE, WAIT_OBJECT_0 = 0x00100000, 0x0001, 0
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.OpenProcess.restype = wintypes.HANDLE
        h = k32.OpenProcess(SYNCHRONIZE | PROCESS_TERMINATE, False, pid)
        if not h:
            return True   # no such process
        try:
            k32.TerminateProcess(h, 1)
            return k32.WaitForSingleObject(h, int(timeout * 1000)) == WAIT_OBJECT_0
        finally:
            k32.CloseHandle(h)
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        return True
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        time.sleep(0.02)
    return False


class Child:
    """One supervised child process.

    on_exit(code)  called (on the event loop) when a child the server
                   started exits without being asked to; it is then
                   restarted after the backoff
    """

    def __init__(self, name: str, argv: list[str], on_exit: Callable[[int], None] = lambda code: None,
                 log: Callable[[str], None] = print, module: str | None = None):
        self.name = name
        self.argv = argv
        self.on_exit = on_exit
        self.log = log
        self.available = module is None or module_exists(module)
        self.proc: subprocess.Popen | None = None
        self.pid: int | None = None          # started or adopted
        self.wanted = False                  # keep it running
        self._started_at = 0.0
        self._backoff = BACKOFF_MIN
        self._next_start = 0.0
        if not self.available:
            log(f"{name}: module {module} is not built yet; not starting it")

    @property
    def running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def start(self):
        """Start the child now (and keep it running from here on)."""
        self.wanted = True
        if not self.available or self.running:
            return
        flags = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if sys.platform == "win32" \
            else {"start_new_session": True}
        self.proc = subprocess.Popen(self.argv, stdin=subprocess.DEVNULL, **flags)
        self.pid = self.proc.pid
        self._started_at = time.monotonic()
        self.log(f"{self.name}: started, pid {self.pid}")

    def adopt(self, pid: int):
        """A child answered `hello` with its PID. One the server started
        keeps its process handle: its PID can differ (on Windows a venv's
        python.exe is a launcher that runs the interpreter as its own
        child, and ends it when it is killed). Any other is adopted by PID."""
        if not self.running:
            self.proc, self.pid = None, pid
        self.wanted = True

    async def kill(self, timeout: float = 5.0):
        """Kill the child and wait for it to exit. It is not restarted until
        `start` (or `restart`)."""
        self.wanted = False
        proc, pid = self.proc, self.pid
        self.proc = self.pid = None
        if proc is not None:
            proc.kill()
            await asyncio.to_thread(proc.wait, timeout)
        elif pid is not None:
            await asyncio.to_thread(kill_pid, pid, timeout)

    async def restart(self):
        await self.kill()
        self._backoff = BACKOFF_MIN
        self.start()

    async def run(self):
        """Watch the child: report an unexpected exit and restart it after
        the backoff. Runs until cancelled."""
        while True:
            await asyncio.sleep(POLL)
            proc = self.proc
            if proc is not None and proc.poll() is not None:
                self.proc = self.pid = None
                code = proc.returncode
                up = time.monotonic() - self._started_at
                self._backoff = BACKOFF_MIN if up > RESET_AFTER else min(self._backoff * 2, BACKOFF_MAX)
                self._next_start = time.monotonic() + self._backoff
                self.log(f"{self.name}: exited with code {code} after {up:.1f} s; restarting in {self._backoff:g} s")
                self.on_exit(code)
            if self.wanted and self.proc is None and self.pid is None and time.monotonic() >= self._next_start:
                self.start()

    @property
    def adopted(self) -> bool:
        return self.proc is None and self.pid is not None
