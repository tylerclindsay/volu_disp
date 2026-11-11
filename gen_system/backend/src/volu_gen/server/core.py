"""The API server's shared state and lifecycle (docs/architecture.md →
API server): the display, the DB, the settings, the player, the links to
the engine and the worker, node detection and the WebSocket hub.

Without a `Runtime` the server is HTTP only: no child processes, no IPC,
no pings (the HTTP tests run this way). With one, `start` brings up the
links in this order (API server → Startup):
1. Jobs left running by a previous server go back to the queue.
2. Worker: a surviving one is killed by PID, then leftover `*.tmp` files
   are deleted, then a new worker starts. (Connect-only: the server
   adopts the job a running worker reports, and deletes no `*.tmp`.)
3. Engine: a running one that answers `hello` is adopted; else one starts.
4. Node pings; an animation-file sweep; the first dispatch.

zmq.asyncio needs a selector event loop: on Windows uvicorn's default is
the Proactor loop, so `volu serve` runs on `asyncio.SelectorEventLoop`.
"""

from __future__ import annotations

import asyncio
import sys
from collections import deque
from dataclasses import dataclass
from pathlib import Path

import zmq.asyncio

from ..analysis import library_moods
from ..ipc import DEFAULT_HOST, ENGINE_CONTROL_PORT, ENGINE_PREVIEW_PORT, WORKER_PORT, Configure, Msg, Shutdown
from . import cleanup, jobs
from . import db as dbm
from . import settings as settings_mod
from .children import Child, kill_pid, module_argv, probe_hello
from .dispatch import WorkerLink
from .display import Display
from .engine_link import EngineLink
from .library import Library
from .live import Hub
from .nodes import NodeWatch
from .player import Player

ENGINE_PROBE = 1.0      # s to wait for a running engine to answer before starting one
SHUTDOWN_WAIT = 3.0     # s for the engine to exit after `shutdown`


@dataclass(frozen=True)
class Runtime:
    """How the server runs its children.

    spawn  start the engine and the worker, and restart them when they die;
           False: only connect (to children started by hand)
    ping   detect drive nodes by ping
    """
    host: str = DEFAULT_HOST
    engine_control_port: int = ENGINE_CONTROL_PORT
    engine_preview_port: int = ENGINE_PREVIEW_PORT
    worker_port: int = WORKER_PORT
    spawn: bool = True
    ping: bool = True
    ping_interval: float = 2.0


class Server:
    """Everything the routes share."""

    def __init__(self, display: Display, data_dir: Path, db_path: Path | str | None = None,
                 runtime: Runtime | None = None):
        self.display = display
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.db = dbm.connect(db_path if db_path is not None else self.data_dir / dbm.DB_NAME)
        self.library = Library(self.db, self.data_dir, display)
        self.settings = settings_mod.load(self.db, display)
        self.runtime = runtime
        self.hub = Hub()
        self.player = Player(self)
        self.engine: EngineLink | None = None
        self.worker: WorkerLink | None = None
        self.nodes: NodeWatch | None = None
        self.online: frozenset[int] = frozenset()
        self.armed_override: bool | None = None   # the UI toggle; None: armed when nodes are online
        self.worker_log: deque[str] = deque(maxlen=500)
        self._moods: dict[str, dict[str, float]] | None = None
        self._ctx: zmq.asyncio.Context | None = None
        self._children: list[Child] = []
        self._tasks: list[asyncio.Task] = []
        self._sweep: asyncio.Task | None = None
        self._sweep_again = False

    @classmethod
    def open(cls, display_path: Path, data_dir: Path, runtime: Runtime | None = None) -> Server:
        return cls(Display.load(display_path), data_dir, runtime=runtime)

    def log(self, line: str):
        print(f"volu: {line}", file=sys.stderr, flush=True)

    # -- moods (planning input)
    def invalidate_moods(self):
        """Call when a track's mood analysis changes."""
        self._moods = None

    async def mood_z(self, sha256: str) -> dict[str, float] | None:
        """Library-wide mood z-scores of one track (decision 0004), or None
        while the library is too small."""
        if self._moods is None:
            self._moods = await asyncio.to_thread(library_moods, self.data_dir)
        return self._moods.get(sha256)

    # -- what the engine gets
    @property
    def armed(self) -> bool:
        """Hardware output: automatic when drive nodes are detected; the UI
        toggle overrides it. Never with an invalid display config."""
        if self.display.problems:
            return False
        return self.armed_override if self.armed_override is not None else bool(self.online)

    def configure_msg(self) -> Configure:
        st, off = self.settings, self.settings.offsets()
        return Configure(display_config=str(self.display.config.path), geometry_hash=self.display.geometry.hash,
                         audio_device=st.audio_device, audio_mode=st.audio_mode, display_fps=st.display_fps,
                         preview_fps=st.preview_fps, display_offset_ms=off.display_ms,
                         preview_offset_ms=off.preview_ms, network_interface=st.network_interface,
                         armed=self.armed, online_nodes=sorted(self.online), brightness=st.brightness)

    def engine_send(self, msg: Msg) -> bool:
        return self.engine is not None and self.engine.send(msg)

    def reconfigure(self):
        """Settings, nodes or arming changed: the engine gets a new full
        `configure` (applied live)."""
        self.engine_send(self.configure_msg())
        self.state_changed()

    def set_online(self, online: frozenset[int]):
        self.online = online
        self.reconfigure()

    # -- live state for the UI
    def state(self) -> dict:
        e, w = self.engine, self.worker
        status = e.status if e is not None and e.ready else None
        return {
            "type": "state",
            "engine": {"connected": bool(e and e.ready), "pid": e.hello.pid if e and e.ready else None,
                       "state": status.state if status else None},
            "worker": {"connected": bool(w and w.ready), "pid": w.pid if w and w.ready else None,
                       "job_id": w.running.job_id if w and w.running else None},
            "player": self.player.snapshot(),
            "output": {"armed": self.armed, "armed_override": self.armed_override,
                       "display_valid": not self.display.problems,
                       "nodes": [{"id": n.id, "ip": n.ip, "online": n.id in self.online}
                                 for n in self.display.config.drive_nodes]},
        }

    def state_changed(self):
        self.hub.latest("state", self.state())

    def jobs_changed(self):
        self.hub.changed("jobs")
        if self.worker is not None:
            self.worker.kick()

    # -- cleanup
    def sweep_soon(self):
        """Delete animation files nothing uses (Lifecycle → Cleanup), soon."""
        if self._sweep is None or self._sweep.done():
            self._sweep = asyncio.get_running_loop().create_task(self.sweep())
        else:
            self._sweep_again = True

    async def sweep(self):
        while True:
            self._sweep_again = False
            keep = self.library.anim_paths() | jobs.open_anim_paths(self.db)
            if self.engine is not None:
                keep |= self.engine.open_anims
            for p in await asyncio.to_thread(cleanup.sweep_anims, self.data_dir, keep):
                self.log(f"deleted {p.relative_to(self.data_dir).as_posix()}")
            if not self._sweep_again:
                return

    # -- lifecycle
    async def start(self):
        rt = self.runtime
        if rt is None:
            return
        if sys.platform == "win32" and isinstance(asyncio.get_running_loop(), asyncio.ProactorEventLoop):
            raise RuntimeError("the server needs a selector event loop for zmq.asyncio "
                               "(uvicorn: loop='asyncio:SelectorEventLoop')")
        self._ctx = zmq.asyncio.Context()
        if requeued := jobs.requeue_running(self.db):
            self.log(f"jobs {requeued} were running under the last server; queued again")
        cfg, data = self.display.config.path, self.data_dir

        worker_child = None
        if rt.spawn:
            if (pid := await asyncio.to_thread(probe_hello, rt.worker_port, rt.host)) is not None:
                self.log(f"worker: killing the surviving worker, pid {pid}")
                await asyncio.to_thread(kill_pid, pid)
            for p in await asyncio.to_thread(lambda: cleanup.delete_files(cleanup.leftover_tmp(data))):
                self.log(f"deleted leftover {p.relative_to(data).as_posix()}")
            worker_child = Child("worker", module_argv("volu_gen.worker", "--display", cfg, "--data", data,
                                                       "--host", rt.host, "--port", rt.worker_port),
                                 on_exit=lambda code: self.worker.on_exit(code), log=self.log,
                                 module="volu_gen.worker")
        self.worker = WorkerLink(self, self._ctx, rt.worker_port, rt.host, worker_child)
        self.worker.dispatching = True

        engine_child = None
        if rt.spawn:
            engine_child = Child("engine", module_argv("volu_gen.engine", "--display", cfg, "--data", data,
                                                       "--host", rt.host, "--control-port", rt.engine_control_port,
                                                       "--preview-port", rt.engine_preview_port),
                                 on_exit=self._engine_exited, log=self.log, module="volu_gen.engine")
        self.engine = EngineLink(self, self._ctx, rt.engine_control_port, rt.engine_preview_port, rt.host,
                                 engine_child)

        self._children = [c for c in (worker_child, engine_child) if c is not None]
        self._spawn(self.worker.run(), self.engine.run(), *(c.run() for c in self._children))
        if worker_child is not None:
            worker_child.start()
        if engine_child is not None:
            try:
                await asyncio.wait_for(self.engine.answered.wait(), ENGINE_PROBE)
            except TimeoutError:
                engine_child.start()

        if rt.ping:
            self.nodes = NodeWatch([(n.id, n.ip) for n in self.display.config.drive_nodes], self.set_online,
                                   source=lambda: self.settings.network_interface, interval=rt.ping_interval)
            self._spawn(self.nodes.run())
        await self.sweep()
        self.worker.kick()

    def _spawn(self, *coros):
        for c in coros:
            t = asyncio.get_running_loop().create_task(c)
            t.add_done_callback(self._task_done)
            self._tasks.append(t)

    def _task_done(self, t: asyncio.Task):
        if not t.cancelled() and t.exception() is not None:
            self.log(f"internal task failed: {t.exception()!r}")

    def _engine_exited(self, code: int):
        if self.engine is not None:
            self.engine.ready = False
            self.engine.lost()

    async def stop(self):
        """A clean stop also stops the children (a crash leaves the engine
        playing; API server → Startup adopts it)."""
        engine_child = self.engine.child if self.engine is not None else None
        if engine_child is not None and self.engine.ready:
            self.engine.send(Shutdown())
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        if engine_child is not None and engine_child.proc is not None:
            try:
                await asyncio.to_thread(engine_child.proc.wait, SHUTDOWN_WAIT)
            except Exception:
                pass
        for c in self._children:
            await c.kill()
        for link in (self.engine, self.worker):
            if link is not None:
                link.close()
        if self.nodes is not None:
            self.nodes.close()
        if self._ctx is not None:
            self._ctx.term()
            self._ctx = None

    def close(self):
        self.db.close()
