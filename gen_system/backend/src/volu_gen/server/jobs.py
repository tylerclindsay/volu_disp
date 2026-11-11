"""The job queue (docs/architecture.md → Worker). The server owns it; the
worker runs one job at a time.

A job row holds only what identifies the work: a render job names its
version, and the server builds the full spec (plan, fps, geometry hash)
when the job starts, so the job renders the version's latest plan at the
display FPS in effect then (decision 0010).
"""

from __future__ import annotations

import sqlite3
import uuid
from enum import IntEnum

from . import db as dbm

ANIM_DIR = "animations"
QUEUED, RUNNING, DONE, FAILED, CANCELLED = "queued", "running", "done", "failed", "cancelled"
OPEN = (QUEUED, RUNNING)


class Priority(IntEnum):
    """Highest first. The worker never preempts a running job."""
    STUDIO = 0    # Studio render, regrid
    QUEUED = 1    # render (or unfinished import work) of a track in the play queue
    IMPORT = 2    # analyze + default render (FIFO), re-render stale, FPS re-renders


def anim_path(key: str, render_id: str) -> str:
    """Where a render goes, relative to the data dir. Every render gets a
    new file: Windows can't replace a file that is open."""
    return f"tracks/{key}/{ANIM_DIR}/{render_id}.anim"


def new_render_id() -> str:
    return uuid.uuid4().hex


def _insert(db: sqlite3.Connection, kind: str, track_id: int, spec: dict, priority: Priority,
            version_id: int | None = None) -> int:
    with db:
        return db.execute(
            "INSERT INTO jobs (kind, track_id, version_id, spec, priority, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (kind, track_id, version_id, dbm.dumps(spec), int(priority), dbm.now())).lastrowid


def queue_analyze(db: sqlite3.Connection, track_id: int, key: str,
                  priority: Priority = Priority.IMPORT) -> int:
    """Analyze a track (finished stages are skipped). Reuses an open job."""
    row = db.execute("SELECT id FROM jobs WHERE kind = 'analyze' AND track_id = ? AND state IN (?, ?)",
                     (track_id, *OPEN)).fetchone()
    if row is not None:
        return row["id"]
    return _insert(db, "analyze", track_id, {"key": key}, priority)


def queue_regrid(db: sqlite3.Connection, track_id: int, key: str, bpm: float | None,
                 offset: float | None) -> int:
    """Set (or with bpm None, clear) the user grid. A queued regrid of the
    same track is cancelled: only the latest override matters."""
    with db:
        db.execute("UPDATE jobs SET state = ?, finished_at = ? WHERE kind = 'regrid' AND track_id = ? "
                   "AND state = ?", (CANCELLED, dbm.now(), track_id, QUEUED))
    return _insert(db, "regrid", track_id, {"key": key, "bpm": bpm, "offset": offset}, Priority.STUDIO)


def queue_render(db: sqlite3.Connection, version_id: int, reason: str, priority: Priority) -> int:
    """Render a version. If a render of it is already queued, that job is
    kept (it takes the version's plan when it starts) and gets the higher
    of the two priorities."""
    v = db.execute("SELECT v.track_id, t.key FROM versions v JOIN tracks t ON t.id = v.track_id "
                   "WHERE v.id = ?", (version_id,)).fetchone()
    if v is None:
        raise KeyError(f"no version {version_id}")
    row = db.execute("SELECT id, priority FROM jobs WHERE kind = 'render' AND version_id = ? AND state = ?",
                     (version_id, QUEUED)).fetchone()
    if row is not None:
        if priority < row["priority"]:
            with db:
                db.execute("UPDATE jobs SET priority = ? WHERE id = ?", (int(priority), row["id"]))
        return row["id"]
    rid = new_render_id()
    spec = {"key": v["key"], "reason": reason, "render_id": rid, "anim_path": anim_path(v["key"], rid)}
    return _insert(db, "render", v["track_id"], spec, priority, version_id)


def promote_track(db: sqlite3.Connection, track_id: int):
    """A track joined the play queue: its queued import work (analyze and
    render) moves up to the play-queue priority."""
    with db:
        db.execute("UPDATE jobs SET priority = ? WHERE track_id = ? AND state = ? AND priority > ? "
                   "AND kind IN ('analyze', 'render')",
                   (int(Priority.QUEUED), track_id, QUEUED, int(Priority.QUEUED)))


def cancel(db: sqlite3.Connection, job_id: int) -> bool:
    """Cancel a queued job. False if it isn't queued (a running job is
    cancelled through the worker)."""
    with db:
        n = db.execute("UPDATE jobs SET state = ?, finished_at = ? WHERE id = ? AND state = ?",
                       (CANCELLED, dbm.now(), job_id, QUEUED)).rowcount
    return n == 1


def retry(db: sqlite3.Connection, job_id: int) -> int:
    """Queue a failed or cancelled job's work again, as a new job."""
    j = db.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    if j is None:
        raise KeyError(f"no job {job_id}")
    if j["state"] not in (FAILED, CANCELLED):
        raise ValueError(f"job {job_id} is {j['state']}; only failed or cancelled jobs can be retried")
    spec = dbm.loads(j["spec"])
    if j["kind"] == "render":
        return queue_render(db, j["version_id"], spec["reason"], Priority(j["priority"]))
    return _insert(db, j["kind"], j["track_id"], spec, Priority(j["priority"]), j["version_id"])


def as_dict(row: sqlite3.Row) -> dict:
    d = dict(row)
    d["spec"] = dbm.loads(d["spec"])
    d["result"] = dbm.loads(d["result"])
    return d
