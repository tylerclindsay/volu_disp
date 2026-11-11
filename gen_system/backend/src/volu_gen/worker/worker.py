"""The worker process (docs/architecture.md → Worker, IPC): binds a ROUTER,
runs one job at a time and reports on it.

The main thread owns the socket (ZeroMQ sockets are not thread-safe). It
answers `hello`, starts jobs, passes `cancel` on, and sends what the job
thread queues. The job thread only puts messages in the outbox.

Delivery:
- Progress is latest-only: when several are queued, only the newest goes
  out, so the rate is at most one per loop (POLL_MS).
- A result (`done`, `failed`, `cancelled`) that can't be sent because the
  server is gone is kept and sent before the reply to the next `hello`.
  Until its result is sent, a job counts as running: `hello` reports its
  id, and a new `submit` is refused. So a reconnected server sees either
  the job running or its result, never neither.
- The worker exits when no server has been connected for
  `server_timeout` s (decision 0007: it does not outlive the server).
"""

from __future__ import annotations

import os
import queue
import sys
import threading
import traceback
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

import zmq

from ..generators import RenderCancelled
from ..geometry import Geometry
from ..ipc import (DEFAULT_HOST, HEARTBEAT_TIMEOUT_MS, TO_WORKER, WORKER_PORT, Cancel, Cancelled, Done, Failed,
                   Hello, Log, MessageError, Msg, PeerWatch, Progress, Submit, WorkerHello, bind_router, decode,
                   send_to)
from .jobs import RUNNERS, JobEnv

POLL_MS = 50
RESULTS = (Done, Failed, Cancelled)


@dataclass(eq=False)
class _Job:
    id: int
    kind: str
    cancel: threading.Event = field(default_factory=threading.Event)


class Worker:
    """data_dir        holds tracks/; spec paths are relative to it
    geometry        from the display config the server gave; renders for another geometry fail
    server_timeout  exit after this many s with no server; 0: never
    runners         job kind → fn(spec, JobEnv) → result (tests swap these)
    """

    def __init__(self, data_dir: Path, geometry: Geometry, port: int = WORKER_PORT, host: str = DEFAULT_HOST,
                 *, server_timeout: float = HEARTBEAT_TIMEOUT_MS / 1000, device: str | None = None,
                 runners: Mapping[str, Callable] = RUNNERS, log: Callable[[str], None] | None = None):
        self.data_dir = Path(data_dir)
        self.geometry = geometry
        self.server_timeout = server_timeout
        self.device = device
        self.runners = runners
        self._print = log or (lambda line: print(f"worker: {line}", file=sys.stderr, flush=True))
        self._ctx = zmq.Context()
        self.sock = bind_router(self._ctx, port, host)
        self.watch = PeerWatch(self.sock)
        self._server: bytes | None = None    # routing id of the latest hello
        self._outbox: queue.SimpleQueue[Msg] = queue.SimpleQueue()
        self._unsent: list[Msg] = []         # results the server has not received
        self._job: _Job | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    @property
    def busy(self) -> bool:
        return self._job is not None

    def stop(self):
        """Make `run` return (from any thread)."""
        self._stop.set()

    def run(self):
        """Serve until `stop()` or until the server is gone for
        `server_timeout` s."""
        poller = zmq.Poller()
        poller.register(self.sock, zmq.POLLIN)
        poller.register(self.watch.monitor, zmq.POLLIN)
        while not self._stop.is_set():
            ready = dict(poller.poll(POLL_MS))
            if self.sock in ready:
                self._receive()
            self.watch.update()
            self._flush()
            if self.server_timeout and self.watch.alone_for() > self.server_timeout:
                self._print(f"no server for {self.server_timeout:g} s, exiting")
                break

    def close(self):
        self.watch.close()
        self.sock.close()
        self._ctx.term()

    # -- receiving
    def _receive(self):
        while self.sock.poll(0):
            frames = self.sock.recv_multipart(zmq.NOBLOCK)
            if len(frames) != 2:
                self._print(f"dropped a message of {len(frames)} frames")
                continue
            peer, data = frames
            try:
                msg = decode(TO_WORKER, data)
            except MessageError as e:
                self._print(f"dropped a bad message: {e}")
                continue
            if isinstance(msg, Hello):
                self._hello(peer)
            elif isinstance(msg, Submit):
                self._submit(msg)
            elif isinstance(msg, Cancel):
                self._cancel(msg)

    def _hello(self, peer: bytes):
        self._server = peer
        unsent, self._unsent = self._unsent, []
        for m in unsent:
            self._send_result(m)
        self._flush()
        self._send(WorkerHello(pid=os.getpid(), job_id=self._job.id if self._job else None))

    def _submit(self, msg: Submit):
        if self._job is not None:
            if self._job.id != msg.job_id:   # the same job again (after a reconnect) keeps running
                self._send(Failed(job_id=msg.job_id, error=f"worker busy with job {self._job.id}"))
            return
        if msg.kind not in self.runners:
            self._send(Failed(job_id=msg.job_id, error=f"no runner for {msg.kind!r} jobs"))
            return
        job = self._job = _Job(msg.job_id, msg.kind)
        self._thread = threading.Thread(target=self._run_job, args=(job, msg), name=f"job-{job.id}", daemon=True)
        self._thread.start()

    def _cancel(self, msg: Cancel):
        job = self._job
        if job is None or job.id != msg.job_id:
            self._print(f"cancel for job {msg.job_id}, which is not running")
        elif job.kind != "render":
            self._print(f"cancel for {job.kind} job {job.id}: only renders cancel; kill the worker instead")
        else:
            job.cancel.set()

    # -- the job thread
    def _run_job(self, job: _Job, msg: Submit):
        def log(line: str):
            self._print(f"[job {job.id}] {line}")
            self._outbox.put(Log(line=line))

        def progress(stage: str, track: str | None, frac: float, eta: float | None):
            self._outbox.put(Progress(job_id=job.id, stage=stage, track=track,
                                      frac=min(max(frac, 0.0), 1.0), eta=eta))

        env = JobEnv(self.data_dir, self.geometry, self.device, log, progress, job.cancel.is_set)
        self._print(f"job {job.id}: {job.kind} {msg.spec.key}")
        try:
            result: Msg = Done(job_id=job.id, result=self.runners[job.kind](msg.spec, env))
        except RenderCancelled:
            result = Cancelled(job_id=job.id)
        except Exception as e:
            result = Failed(job_id=job.id, error=f"{type(e).__name__}: {e}", traceback=traceback.format_exc())
        self._print(f"job {job.id}: {result.type}" + (f" ({result.error})" if isinstance(result, Failed) else ""))
        self._outbox.put(result)

    # -- sending
    def _flush(self):
        out = []
        while True:
            try:
                out.append(self._outbox.get_nowait())
            except queue.Empty:
                break
        for i, m in enumerate(out):
            if isinstance(m, Progress) and any(isinstance(n, Progress) for n in out[i + 1:]):
                continue
            if isinstance(m, RESULTS):
                self._send_result(m)
            else:
                self._send(m)

    def _send_result(self, msg: Msg):
        """The job ends when the server has its result; until then it is kept."""
        if self._send(msg):
            if self._job is not None and self._job.id == msg.job_id:
                self._job = None
        else:
            self._unsent.append(msg)

    def _send(self, msg: Msg) -> bool:
        return self._server is not None and send_to(self.sock, self._server, msg)

    @property
    def job_alive(self) -> bool:
        """A job thread is still running (it may be past its result)."""
        return self._thread is not None and self._thread.is_alive()


def lower_priority():
    """Below-normal process priority, so the worker never competes with the
    engine or the UI (docs/architecture.md → Worker)."""
    if sys.platform == "win32":
        import ctypes
        BELOW_NORMAL_PRIORITY_CLASS = 0x4000
        k32 = ctypes.windll.kernel32
        k32.SetPriorityClass(k32.GetCurrentProcess(), BELOW_NORMAL_PRIORITY_CLASS)
    else:
        os.nice(5)
