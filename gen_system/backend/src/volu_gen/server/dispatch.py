"""Job dispatch: the server's end of the worker link (docs/architecture.md
→ Worker, IPC, decision 0007).

The worker runs one job at a time. When it is free, the server takes
`jobs.next_job`, builds its spec, marks it running and submits it:
- A job row names its work; the spec is built when the job starts
  (`build_spec`). A render gets its version's plan, re-planned first if it
  has none or a stale one, with segment times from the current grid, and
  the display FPS and geometry hash in effect now (decision 0010). A
  render whose planning fails fails, with the error and traceback.
- Results update the DB (`apply_result`): an analysis that finishes queues
  the track's default render (Main flows 1); a regrid re-plans the draft
  (Main flows 4); a render fills its version row, and the player switches
  a loaded version to the new file (Main flows 3).
- A result for a job that is no longer running in the DB (its track was
  removed) is dropped; cleanup deletes the file it wrote.

Recovery (Failure handling):
- On every (re)connect, `hello`. A worker that reports no job while the
  server has one running lost it (it restarted): the job is submitted
  again. A worker that reports a job the server does not run finishes it
  first; its result is dropped.
- A worker the server started that exits fails its running job (unless
  the server killed it to cancel the job), and is restarted.
- Cancel: a render stops between chunks (`cancel`); any other job only by
  killing the worker, which the server can do only for a worker it
  started.
"""

from __future__ import annotations

import asyncio
import traceback
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import zmq
import zmq.asyncio
from zmq.utils.monitor import parse_monitor_message

from ..generators import GENERATOR_ID, DirectorParams, Plan
from ..ipc import (FROM_WORKER, AnalyzeSpec, Cancel, Cancelled, Done, Failed, Hello, JobSpec, Log,
                   MessageError, Msg, Progress, RegridSpec, RenderSpec, Submit, WorkerHello, connect_dealer,
                   decode, encode)
from . import db as dbm
from . import jobs
from .jobs import Priority
from .planning import PlanState, pinned_of, plan_for_render, plan_track

if TYPE_CHECKING:
    from .app import Server
    from .children import Child

HELLO_RETRY = 1.0   # s between hellos until the worker answers


class DispatchError(Exception):
    """A job that cannot start (its planning failed, …); it fails."""


async def build_spec(s: Server, row) -> tuple[JobSpec, dict | None]:
    """The spec of a queued job, and for a render the plan to store in its
    version row. Raises DispatchError (with the cause) when it can't be built."""
    spec = dbm.loads(row["spec"])
    if row["kind"] == "analyze":
        return AnalyzeSpec(key=spec["key"]), None
    if row["kind"] == "regrid":
        return RegridSpec(key=spec["key"], bpm=spec["bpm"], offset=spec["offset"]), None
    v = s.library.version(row["version_id"])
    t = s.library.track_dir(spec["key"])
    try:
        mood_z = await s.mood_z(t.data.get("source", {}).get("sha256", ""))
        params = DirectorParams.model_validate(dbm.loads(v["params"]))
        plan, _ = await asyncio.to_thread(plan_for_render, t, params, dbm.loads(v["plan"]), mood_z)
    except Exception as e:
        raise DispatchError(f"planning failed: {type(e).__name__}: {e}") from e
    plan_json = plan.model_dump(mode="json")
    return RenderSpec(key=spec["key"], render_id=spec["render_id"], anim_path=spec["anim_path"],
                      generator=GENERATOR_ID, plan=plan_json, fps=s.settings.display_fps,
                      geometry_hash=s.display.geometry.hash), plan_json


async def apply_result(s: Server, row, msg: Done | Failed | Cancelled):
    """Record a running job's result and do what follows from it."""
    job_id, track_id = row["id"], row["track_id"]
    if isinstance(msg, Cancelled):
        jobs.finish(s.db, job_id, jobs.CANCELLED)
    elif isinstance(msg, Failed):
        jobs.finish(s.db, job_id, jobs.FAILED, error=msg.error, traceback=msg.traceback)
        if row["kind"] == "analyze":
            s.library.sync_analysis(track_id, s.library.track_dir(dbm.loads(row["spec"])["key"]), "failed")
    else:
        result = msg.result.model_dump(mode="json")
        jobs.finish(s.db, job_id, jobs.DONE, result=result)
        if row["kind"] == "analyze":
            await _analyzed(s, row)
        elif row["kind"] == "regrid":
            await _regridded(s, row)
        else:
            s.library.apply_render(row["version_id"], result)
            s.player.on_render_done(row["version_id"])
    s.hub.changed("track", track_id)
    s.hub.changed("tracks")
    s.sweep_soon()


async def _analyzed(s: Server, row):
    """Main flows 1: the draft (default track params, planned when its
    render starts) and its render, ahead of the next track's analysis."""
    lib = s.library
    lib.sync_analysis(row["track_id"], lib.track_dir(lib.track(row["track_id"])["key"]), "done")
    s.invalidate_moods()
    vid = lib.ensure_draft(row["track_id"], DirectorParams())
    if lib.version(vid)["anim_path"] is None:
        jobs.queue_render(s.db, vid, jobs.DEFAULT, Priority(row["priority"]))


async def _regridded(s: Server, row):
    """Main flows 4: the draft is re-planned on the new grid (pinned
    segments keep their bars). Saved versions keep their plans."""
    lib = s.library
    track = lib.track(row["track_id"])
    lib.sync_analysis(track["id"], lib.track_dir(track["key"]), "done")
    d = lib.draft(track["id"])
    if d is None or d["plan"] is None:
        return
    params = DirectorParams.model_validate(dbm.loads(d["params"]))
    state = PlanState(params=params, pinned=pinned_of(Plan.model_validate(dbm.loads(d["plan"]))))
    t = lib.track_dir(track["key"])
    try:
        plan = await asyncio.to_thread(plan_track, t, state, await s.mood_z(t.data["source"].get("sha256", "")))
    except Exception as e:
        s.log(f"re-plan of track {track['id']} after a regrid failed: {e}")
        s.hub.event("error", source="planning", kind="regrid", message=str(e), track_id=track["id"])
        return
    lib.set_draft_plan(track["id"], params, plan.model_dump(mode="json"))


@dataclass(eq=False)
class Running:
    """The job the worker runs. submit is None for a job adopted from a
    worker that outlived a server restart."""
    job_id: int
    kind: str
    track_id: int
    submit: Submit | None
    progress: dict[str, Any] | None = None
    killed: bool = field(default=False)   # the server killed the worker to cancel it


class WorkerLink:
    def __init__(self, server: Server, ctx: zmq.asyncio.Context, port: int, host: str,
                 child: Child | None = None):
        self.s = server
        self.child = child
        self.sock = connect_dealer(ctx, port, host)
        self.monitor = self.sock.get_monitor_socket(zmq.EVENT_CONNECTED | zmq.EVENT_DISCONNECTED)
        self.connected = False
        self.ready = False            # answered this connection's hello
        self.pid: int | None = None
        self.running: Running | None = None
        self.foreign: int | None = None   # a job the worker runs that the server does not
        self.dispatching = False          # off until startup is done
        self._kick = asyncio.Event()
        self._hello_sent = 0.0

    def kick(self):
        """Something in the job queue changed: start a job if the worker is free."""
        self._kick.set()

    def send(self, msg: Msg) -> bool:
        if not self.ready and not isinstance(msg, Hello):
            return False
        try:
            self.sock.send(encode(msg), zmq.NOBLOCK)
            return True
        except zmq.Again:
            return False

    def _say_hello(self):
        self._hello_sent = asyncio.get_running_loop().time()
        self.send(Hello())

    async def run(self):
        await asyncio.gather(self._events(), self._receive(), self._dispatch(), self._retry_hello())

    async def _events(self):
        while True:
            ev = parse_monitor_message(await self.monitor.recv_multipart())["event"]
            if ev == zmq.EVENT_CONNECTED:
                self.connected = True
                self._say_hello()
            elif ev == zmq.EVENT_DISCONNECTED:
                self.connected = self.ready = False
                self.s.state_changed()

    async def _retry_hello(self):
        while True:
            await asyncio.sleep(0.25)
            if self.connected and not self.ready and \
                    asyncio.get_running_loop().time() - self._hello_sent > HELLO_RETRY:
                self._say_hello()

    async def _receive(self):
        while True:
            data = await self.sock.recv()
            try:
                msg = decode(FROM_WORKER, data)
            except MessageError as e:
                self.s.log(f"worker: dropped a bad message: {e}")
                continue
            if isinstance(msg, WorkerHello):
                self._on_hello(msg)
            elif isinstance(msg, Progress):
                if self.running is not None and self.running.job_id == msg.job_id:
                    self.running.progress = msg.model_dump(mode="json", exclude={"type"})
                    self.s.hub.latest(f"progress:{msg.job_id}", {"type": "progress", **self.running.progress})
            elif isinstance(msg, Log):
                self.s.worker_log.append(msg.line)
                self.s.hub.event("log", source="worker", line=msg.line)
            else:
                await self._on_result(msg)

    def _on_hello(self, hello: WorkerHello):
        self.ready, self.pid = True, hello.pid
        run = self.running
        if hello.job_id is None:
            self.foreign = None
            if run is not None:   # the worker lost it (it restarted)
                if run.submit is not None and self.send(run.submit):
                    self.s.log(f"worker: job {run.job_id} submitted again")
                else:   # adopted: the server never had its spec; it is built again
                    self.running = None
                    self._requeue(run.job_id)
        elif run is None or run.job_id != hello.job_id:
            row = jobs.job(self.s.db, hello.job_id)
            if run is None and self.dispatching and row is not None and row["state"] == jobs.QUEUED \
                    and jobs.start(self.s.db, row["id"]):
                # a worker that outlived a server restart, with a job requeued at startup
                self.running = Running(row["id"], row["kind"], row["track_id"], None)
                self.s.jobs_changed()
            else:
                self.foreign = hello.job_id
        self.s.state_changed()
        self.kick()

    async def _on_result(self, msg: Done | Failed | Cancelled):
        run = self.running
        if run is not None and run.job_id == msg.job_id:
            self.running = None
        elif self.foreign == msg.job_id:
            self.foreign = None
        row = jobs.job(self.s.db, msg.job_id)
        if row is not None and row["state"] == jobs.RUNNING:
            await apply_result(self.s, row, msg)
        else:
            self.s.sweep_soon()   # a render of a removed track left a file
        self.s.jobs_changed()
        self.s.state_changed()

    async def _dispatch(self):
        while True:
            await self._kick.wait()
            self._kick.clear()
            while self.dispatching and self.ready and self.running is None and self.foreign is None:
                row = jobs.next_job(self.s.db)
                if row is None:
                    break
                await self._start(row)

    async def _start(self, row):
        try:
            spec, plan = await build_spec(self.s, row)
        except (DispatchError, KeyError) as e:
            cause = e.__cause__ or e
            jobs.finish(self.s.db, row["id"], jobs.FAILED, error=str(e),
                        traceback="".join(traceback.format_exception(cause)))
            self.s.jobs_changed()
            self.s.hub.changed("tracks")
            return
        if not jobs.start(self.s.db, row["id"]):
            return   # cancelled or removed while its plan was made
        if plan is not None:
            with self.s.db:
                self.s.db.execute("UPDATE versions SET plan = ? WHERE id = ?", (dbm.dumps(plan), row["version_id"]))
        submit = Submit(job_id=row["id"], spec=spec)
        self.running = Running(row["id"], row["kind"], row["track_id"], submit)
        if not self.send(submit):   # the worker went away: back to the queue
            self.running = None
            self._requeue(row["id"])
        self.s.jobs_changed()
        self.s.hub.changed("track", row["track_id"])
        self.s.state_changed()

    def _requeue(self, job_id: int):
        with self.s.db:
            self.s.db.execute("UPDATE jobs SET state = ?, started_at = NULL WHERE id = ? AND state = ?",
                              (jobs.QUEUED, job_id, jobs.RUNNING))
        self.s.jobs_changed()

    # -- cancelling and process events
    async def cancel_running(self, job_id: int) -> bool:
        """Cancel the running job. A render stops between chunks (its state
        changes when the worker answers); any other job by killing and
        restarting the worker. False if it can't be cancelled."""
        run = self.running
        if run is None or run.job_id != job_id:
            return False
        if run.kind == "render":
            return self.send(Cancel(job_id=job_id))
        if self.child is None or not self.child.available:
            return False
        run.killed = True
        self.running = None
        self.ready = False
        jobs.finish(self.s.db, job_id, jobs.CANCELLED)
        await self.child.restart()
        self.s.jobs_changed()
        return True

    async def wait_done(self, job_id: int, timeout: float) -> bool:
        """Wait until the worker no longer runs the job. False on timeout."""
        deadline = asyncio.get_running_loop().time() + timeout
        while self.running is not None and self.running.job_id == job_id:
            if asyncio.get_running_loop().time() > deadline:
                return False
            await asyncio.sleep(0.05)
        return True

    def on_exit(self, code: int):
        """The worker the server started exited."""
        run, self.running = self.running, None
        self.ready, self.foreign = False, None
        if run is not None and not run.killed:
            jobs.finish(self.s.db, run.job_id, jobs.FAILED, error=f"the worker exited with code {code}")
        self.s.jobs_changed()
        self.s.state_changed()

    def running_progress(self, job_id: int) -> dict[str, Any] | None:
        run = self.running
        return run.progress if run is not None and run.job_id == job_id else None

    def close(self):
        self.sock.disable_monitor()
        self.monitor.close()
        self.sock.close()
