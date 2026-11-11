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
from datetime import datetime
from enum import IntEnum

from ..analysis.track import ANIM_DIR
from . import db as dbm

QUEUED, RUNNING, DONE, FAILED, CANCELLED = "queued", "running", "done", "failed", "cancelled"
OPEN = (QUEUED, RUNNING)
# The reason of the render queued when a track's analysis finishes. It goes
# to the front of its priority, so each imported track gets its default
# render before the next track's analyze starts.
DEFAULT = "default"


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
    """A track joined the play queue: its open import work (analyze and
    render) moves up to the play-queue priority. A running analyze moves up
    too: its default render gets the analyze's priority when it finishes."""
    with db:
        db.execute("UPDATE jobs SET priority = ? WHERE track_id = ? AND state IN (?, ?) AND priority > ? "
                   "AND kind IN ('analyze', 'render')",
                   (int(Priority.QUEUED), track_id, *OPEN, int(Priority.QUEUED)))


def next_job(db: sqlite3.Connection) -> sqlite3.Row | None:
    """The queued job the worker runs next: highest priority first, a
    default render ahead of the rest of its priority, then FIFO."""
    return db.execute(
        "SELECT * FROM jobs WHERE state = ? ORDER BY priority, "
        "(kind = 'render' AND json_extract(spec, '$.reason') = ?) DESC, id LIMIT 1",
        (QUEUED, DEFAULT)).fetchone()


def render_seconds_per_pixel_frame(db: sqlite3.Connection, last: int = 10) -> float | None:
    """Render time per frame per pixel over the last finished renders, for
    estimates; None before the first."""
    seconds = work = 0.0
    for r in db.execute("SELECT started_at, finished_at, result FROM jobs WHERE kind = 'render' AND state = ? "
                        "AND started_at IS NOT NULL AND finished_at IS NOT NULL AND result IS NOT NULL "
                        "ORDER BY id DESC LIMIT ?", (DONE, last)):
        res = dbm.loads(r["result"])
        seconds += (datetime.fromisoformat(r["finished_at"]) - datetime.fromisoformat(r["started_at"])).total_seconds()
        work += res["frame_count"] * res["pixel_count"]
    return seconds / work if work else None


def job(db: sqlite3.Connection, job_id: int) -> sqlite3.Row | None:
    return db.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()


def start(db: sqlite3.Connection, job_id: int) -> bool:
    """Mark a queued job running. False if it is no longer queued."""
    with db:
        return db.execute("UPDATE jobs SET state = ?, started_at = ? WHERE id = ? AND state = ?",
                          (RUNNING, dbm.now(), job_id, QUEUED)).rowcount == 1


def finish(db: sqlite3.Connection, job_id: int, state: str, result: dict | None = None,
           error: str | None = None, traceback: str | None = None):
    with db:
        db.execute("UPDATE jobs SET state = ?, finished_at = ?, result = ?, error = ?, traceback = ? WHERE id = ?",
                   (state, dbm.now(), dbm.dumps(result), error, traceback, job_id))


def requeue_running(db: sqlite3.Connection) -> list[int]:
    """Jobs left running by a previous server go back to the queue (the
    server kills a surviving worker at startup)."""
    with db:
        ids = [r["id"] for r in db.execute("SELECT id FROM jobs WHERE state = ?", (RUNNING,))]
        db.execute("UPDATE jobs SET state = ?, started_at = NULL WHERE state = ?", (QUEUED, RUNNING))
    return ids


def open_anim_paths(db: sqlite3.Connection) -> set[str]:
    """Output paths of queued and running renders. A finished render's file
    exists before its result reaches the DB, so cleanup must keep these."""
    return {r[0] for r in db.execute("SELECT json_extract(spec, '$.anim_path') FROM jobs "
                                     "WHERE kind = 'render' AND state IN (?, ?)", OPEN)}


def cancel(db: sqlite3.Connection, job_id: int) -> bool:
    """Cancel a queued job. False if it isn't queued (a running job is
    cancelled through the worker)."""
    with db:
        n = db.execute("UPDATE jobs SET state = ?, finished_at = ? WHERE id = ? AND state = ?",
                       (CANCELLED, dbm.now(), job_id, QUEUED)).rowcount
    return n == 1


def retry(db: sqlite3.Connection, job_id: int) -> int:
    """Queue a failed or cancelled job's work again, through the same rules
    as a new request: an open job of the same work is returned instead."""
    j = db.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    if j is None:
        raise KeyError(f"no job {job_id}")
    if j["state"] not in (FAILED, CANCELLED):
        raise ValueError(f"job {job_id} is {j['state']}; only failed or cancelled jobs can be retried")
    spec = dbm.loads(j["spec"])
    if j["kind"] == "render":
        return queue_render(db, j["version_id"], spec["reason"], Priority(j["priority"]))
    if j["kind"] == "regrid":
        return queue_regrid(db, j["track_id"], spec["key"], spec["bpm"], spec["offset"])
    return queue_analyze(db, j["track_id"], spec["key"], Priority(j["priority"]))


def as_dict(row: sqlite3.Row) -> dict:
    d = dict(row)
    d["spec"] = dbm.loads(d["spec"])
    d["result"] = dbm.loads(d["result"])
    return d
