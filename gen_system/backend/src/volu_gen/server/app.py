"""The API server's HTTP API for the UI (docs/architecture.md → API
server, HTTP API; docs/ui.md). Routes are under /api. The server is the
only writer of the DB; blocking work (file copies, planning, deletes) runs
off the event loop. Each change tells the UI over the WebSocket what to
refetch (`changed`), and wakes the job dispatch.
"""

from __future__ import annotations

import asyncio
import sqlite3
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Literal

import numpy as np
from fastapi import APIRouter, Body, FastAPI, HTTPException, Query, Request, UploadFile, WebSocket
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from ..analysis.peaks import level_rate
from ..analysis.track import TRACKS_DIR
from ..generators import DIRECTOR_VERSION, GENERATOR_ID, METHODS, PALETTES, DirectorParams, Plan
from ..ipc import Test, TestOff
from . import db as dbm
from . import jobs
from . import settings as settings_mod
from .core import Server
from .jobs import Priority
from .library import delete_dir, dir_size, scan
from .planning import PlanState, pinned_of, plan_track
from .player import Mode, NotPlayable


def create_app(server: Server) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await server.start()
        try:
            yield
        finally:
            await server.stop()

    app = FastAPI(title="volu_disp", version="0.1.0", lifespan=lifespan)
    app.state.server = server
    app.include_router(router, prefix="/api")
    return app


router = APIRouter()
CANCEL_WAIT = 30.0   # s for a cancelled render to stop (it checks between 1 s chunks)


def srv(request: Request) -> Server:
    return request.app.state.server


def not_found(e: KeyError) -> HTTPException:
    return HTTPException(404, e.args[0] if e.args else "not found")


def unprocessable(e: ValidationError) -> RequestValidationError:
    return RequestValidationError(e.errors(include_url=False, include_context=False))


def track_or_404(s: Server, track_id: int):
    try:
        return s.library.track(track_id)
    except KeyError as e:
        raise not_found(e) from None


# -- display
@router.get("/display")
async def get_display(request: Request) -> dict:
    return srv(request).display.summary()


@router.get("/display/positions")
async def get_positions(request: Request) -> Response:
    """Pixel positions in geometry order: (P, 3) float32 little-endian, mm,
    config axes (y down)."""
    pos = srv(request).display.geometry.positions.astype("<f4")
    return Response(pos.tobytes(), media_type="application/octet-stream")


# -- settings
@router.get("/settings")
async def get_settings(request: Request) -> settings_mod.Settings:
    return srv(request).settings


@router.patch("/settings")
async def patch_settings(request: Request, changes: dict[str, Any] = Body()) -> settings_mod.Settings:
    """Change some settings; the engine gets them live. A new display FPS
    stops playback, cancels a running render and queues it again, and
    queues an FPS re-render of the active version of every track in the
    play queue (Main flows 6)."""
    s = srv(request)
    try:
        new = settings_mod.patch(s.settings, changes, s.display)
    except ValidationError as e:
        raise unprocessable(e) from None
    old_fps = s.settings.display_fps
    if new.display_fps != old_fps:
        s.player.fps_changed()
    settings_mod.save(s.db, new)
    s.settings = new
    if new.display_fps != old_fps:
        await _restart_running_render(s)
        for v in _queue_mismatches(s, new.display_fps):
            jobs.queue_render(s.db, v["id"], "fps", Priority.QUEUED)
        s.jobs_changed()
        s.hub.changed("tracks")
    s.reconfigure()
    s.hub.changed("settings")
    return new


async def _restart_running_render(s: Server):
    """A render running at the old rate stops and is queued again."""
    run = s.worker.running if s.worker is not None else None
    if run is None or run.kind != "render":
        return
    row = jobs.job(s.db, run.job_id)
    if row is not None and await s.worker.cancel_running(run.job_id):
        jobs.queue_render(s.db, row["version_id"], dbm.loads(row["spec"])["reason"], Priority(row["priority"]))


def _queue_mismatches(s: Server, fps: int) -> list:
    """Active versions of queued tracks that are rendered at another rate
    (with the track's duration)."""
    return s.db.execute(
        "SELECT DISTINCT v.*, t.duration AS track_duration FROM play_queue q JOIN tracks t ON t.id = q.track_id "
        "JOIN versions v ON v.id = t.active_version_id "
        "WHERE v.anim_path IS NOT NULL AND v.fps != ?", (fps,)).fetchall()


@router.get("/settings/fps-impact")
async def fps_impact(request: Request, fps: int) -> dict:
    """What changing the display FPS to `fps` would do, for the
    confirmation dialog. `queue_rerender_seconds` estimates the automatic
    re-renders from recent render times (None before the first render)."""
    s = srv(request)
    unplayable = s.db.execute("SELECT COUNT(*) FROM versions WHERE anim_path IS NOT NULL AND fps = ?",
                              (s.settings.display_fps,)).fetchone()[0] if fps != s.settings.display_fps else 0
    mismatches = _queue_mismatches(s, fps)
    rate = jobs.render_seconds_per_pixel_frame(s.db)
    seconds = None if rate is None else round(
        rate * fps * s.display.geometry.pixel_count * sum(v["track_duration"] or 0.0 for v in mismatches), 1)
    return {"fps": fps, "problems": s.display.fps_problems(fps),
            "versions_unplayable": unplayable,
            "queue_rerenders": len(mismatches), "queue_rerender_seconds": seconds}


class SyncChange(BaseModel):
    model_config = ConfigDict(extra="forbid")
    display_ms: float | None = None
    preview_ms: float | None = None
    calibrated_ms: float | None = None


@router.get("/sync")
async def get_sync(request: Request) -> settings_mod.SyncOffsets:
    """The A/V sync offsets of the current audio device + mode."""
    return srv(request).settings.offsets()


@router.patch("/sync")
async def patch_sync(request: Request, change: SyncChange) -> settings_mod.SyncOffsets:
    s = srv(request)
    cur = s.settings.offsets()
    try:
        offsets = settings_mod.SyncOffsets.model_validate(
            {**cur.model_dump(), **change.model_dump(exclude_unset=True)})
    except ValidationError as e:
        raise unprocessable(e) from None
    s.settings = s.settings.with_offsets(offsets)
    settings_mod.save(s.db, s.settings)
    s.reconfigure()   # applied at the next frame, no reload
    s.hub.changed("settings")
    return offsets


# -- library
def _track_out(s: Server, row) -> dict:
    return s.library.track_dict(row, s.settings.display_fps)


@router.get("/tracks")
async def list_tracks(request: Request) -> list[dict]:
    s = srv(request)
    return [_track_out(s, r) for r in s.db.execute("SELECT * FROM tracks ORDER BY id")]


async def _add(s: Server, path: Path, original: str | None = None) -> dict:
    t = await asyncio.to_thread(s.library.import_file, path, original)
    track_id, created = s.library.add(t)
    if created:
        s.jobs_changed()
        s.hub.changed("tracks")
    return {"track": _track_out(s, s.library.track(track_id)), "created": created}


@router.post("/tracks")
async def upload_tracks(request: Request, files: list[UploadFile]) -> list[dict]:
    """Add files (browser upload). Each is copied into its track dir; a
    file already in the library returns its track (`created` false)."""
    s = srv(request)
    tmp_dir = s.data_dir / TRACKS_DIR
    tmp_dir.mkdir(parents=True, exist_ok=True)
    out = []
    for f in files:
        tmp = tmp_dir / f".upload-{uuid.uuid4().hex}.tmp"
        try:
            await asyncio.to_thread(_save_upload, f, tmp)
            out.append(await _add(s, tmp, f.filename or "upload"))
        finally:
            tmp.unlink(missing_ok=True)
    return out


def _save_upload(f: UploadFile, path: Path):
    with open(path, "wb") as out:
        while block := f.file.read(1 << 20):
            out.write(block)


class ScanRequest(BaseModel):
    path: str


@router.post("/tracks/scan")
async def scan_folder(request: Request, req: ScanRequest) -> list[dict]:
    """Import every audio file under a folder (Scan folder)."""
    s = srv(request)
    folder = Path(req.path)
    if not folder.is_dir():
        raise HTTPException(404, f"not a folder: {req.path}")
    return [await _add(s, f) for f in await asyncio.to_thread(scan, folder)]


@router.get("/tracks/{track_id}")
async def get_track(request: Request, track_id: int) -> dict:
    s = srv(request)
    row = track_or_404(s, track_id)
    d = _track_out(s, row)
    d["data_bytes"] = await asyncio.to_thread(dir_size, s.library.tracks_dir / row["key"])
    return d


@router.delete("/tracks/{track_id}", status_code=204)
async def remove_track(request: Request, track_id: int, delete_data: bool = False):
    """Remove a track from the library: its versions, queue entries and
    jobs go too (a running job is cancelled). Its directory stays unless
    `delete_data`. Not while the engine plays it (or Studio has it loaded)."""
    s = srv(request)
    track_or_404(s, track_id)
    if s.player.loads(track_id):
        raise HTTPException(409, "the track is loaded in the engine; stop it or open another first")
    run = s.worker.running if s.worker is not None else None
    if run is not None and run.track_id == track_id and not await s.worker.cancel_running(run.job_id) \
            and run.kind != "render":
        raise HTTPException(409, f"the worker is running {run.kind} job {run.job_id} on this track and "
                                 "can't be stopped (it was not started by this server); try again when it is done")
    if run is not None and run.track_id == track_id and delete_data and \
            not await s.worker.wait_done(run.job_id, CANCEL_WAIT):
        raise HTTPException(409, f"render job {run.job_id} on this track has not stopped yet; try again")
    path = s.library.remove(track_id, delete_data)
    s.invalidate_moods()
    s.player.track_removed(track_id)
    s.jobs_changed()
    s.hub.changed("tracks")
    s.hub.changed("queue")
    if path is not None and path.exists():
        await asyncio.to_thread(delete_dir, path)
    s.sweep_soon()


@router.get("/tracks/{track_id}/analysis")
async def get_analysis(request: Request, track_id: int) -> FileResponse:
    s = srv(request)
    t = s.library.track_dir(track_or_404(s, track_id)["key"])
    if not t.json_path.exists():
        raise HTTPException(404, "no analysis yet")
    return FileResponse(t.json_path, media_type="application/json")


@router.get("/tracks/{track_id}/peaks")
async def get_peaks_info(request: Request, track_id: int) -> dict:
    """Waveform peaks metadata (decision 0006); fetch a level's data from
    /peaks/{level}."""
    s = srv(request)
    path = s.library.track_dir(track_or_404(s, track_id)["key"]).peaks_path
    if not path.exists():
        raise HTTPException(404, "no peaks yet")
    with np.load(path) as z:
        n_levels = sum(1 for k in z.files if k.startswith("level"))
        info = {"version": int(z["version"]), "sample_rate": int(z["sample_rate"]),
                "bucket": int(z["bucket"]), "n_samples": int(z["n_samples"]),
                "lanes": [str(x) for x in z["lanes"]], "values": ["min", "max", "rms"]}
    bucket, n = info["bucket"], info["n_samples"]
    info["levels"] = [{"level": k, "rate": level_rate(k, info["sample_rate"], bucket),
                       "buckets": -(-n // (bucket << k))} for k in range(n_levels)]
    return info


@router.get("/tracks/{track_id}/peaks/{level}")
async def get_peaks_level(request: Request, track_id: int, level: int) -> Response:
    """One level: int16 little-endian, shape (lanes, 3 [min, max, rms],
    buckets), row-major."""
    s = srv(request)
    path = s.library.track_dir(track_or_404(s, track_id)["key"]).peaks_path
    if not path.exists():
        raise HTTPException(404, "no peaks yet")
    with np.load(path) as z:
        if f"level{level}" not in z.files:
            raise HTTPException(404, f"no peaks level {level}")
        a = np.ascontiguousarray(z[f"level{level}"], dtype="<i2")
    return Response(a.tobytes(), media_type="application/octet-stream")


class GridOverride(BaseModel):
    model_config = ConfigDict(extra="forbid")
    bpm: float = Field(ge=20, le=400)
    offset: float   # time of any downbeat, s


@router.put("/tracks/{track_id}/grid")
async def set_grid(request: Request, track_id: int, grid: GridOverride) -> dict:
    """Override the fitted beat grid (Studio header). Queues a `regrid`
    job; the draft is re-planned when it finishes."""
    s = srv(request)
    row = _analyzed(s, track_id)
    job = jobs.queue_regrid(s.db, track_id, row["key"], grid.bpm, grid.offset)
    s.jobs_changed()
    return {"job_id": job}


@router.delete("/tracks/{track_id}/grid")
async def clear_grid(request: Request, track_id: int) -> dict:
    """Return to the fitted grid."""
    s = srv(request)
    row = _analyzed(s, track_id)
    job = jobs.queue_regrid(s.db, track_id, row["key"], None, None)
    s.jobs_changed()
    return {"job_id": job}


def _analyzed(s: Server, track_id: int):
    row = track_or_404(s, track_id)
    if row["analysis"] != "done":
        raise HTTPException(409, "the track's analysis is not finished")
    return row


# -- generators, planning, versions
@router.get("/generators")
async def list_generators() -> list[dict]:
    """Parameter schemas for the Studio forms: the director's track params
    and each method's instance params (pydantic JSON Schema)."""
    return [{
        "id": GENERATOR_ID,
        "version": DIRECTOR_VERSION,
        "params_schema": DirectorParams.model_json_schema(),
        "palettes": {name: list(colors) for name, (colors, _) in PALETTES.items()},
        "methods": [{"id": m.id, "version": m.version, "params_schema": m.Params.model_json_schema(),
                     "suits": {"phrase_types": sorted(m.suits.phrase_types),
                               "energy_db": list(m.suits.energy_db), "min_bars": m.suits.min_bars}}
                    for _, m in sorted(METHODS.items())],
    }]


async def _plan(s: Server, track_row, state: PlanState) -> int:
    """Plan a track and store params and plan in its draft. Returns the
    draft's version id."""
    t = s.library.track_dir(track_row["key"])
    mood_z = await s.mood_z(t.data["source"].get("sha256", ""))
    try:
        plan = await asyncio.to_thread(plan_track, t, state, mood_z)
    except ValidationError as e:
        raise unprocessable(e) from None
    except ValueError as e:
        raise HTTPException(422, str(e)) from None
    vid = s.library.set_draft_plan(track_row["id"], state.params, plan.model_dump(mode="json"))
    s.hub.changed("track", track_row["id"])
    return vid


@router.get("/tracks/{track_id}/plan")
async def get_plan(request: Request, track_id: int) -> dict:
    """The draft's track params, pinned instances and plan (all null
    before the first plan)."""
    s = srv(request)
    track_or_404(s, track_id)
    d = s.library.draft(track_id)
    if d is None or d["plan"] is None:
        return {"version_id": None, "state": None, "plan": None}
    plan = Plan.model_validate(dbm.loads(d["plan"]))
    state = PlanState(params=DirectorParams.model_validate(dbm.loads(d["params"])), pinned=pinned_of(plan))
    return {"version_id": d["id"], "state": state.model_dump(mode="json"), "plan": plan.model_dump(mode="json")}


@router.post("/tracks/{track_id}/plan")
async def post_plan(request: Request, track_id: int, state: PlanState) -> dict:
    """Re-plan the draft from the full Studio state (track params plus
    every instance edit). Nothing renders until Render."""
    s = srv(request)
    row = _analyzed(s, track_id)
    vid = await _plan(s, row, state)
    v = s.library.version(vid)
    return {"version_id": vid, "plan": dbm.loads(v["plan"])}


@router.post("/tracks/{track_id}/render")
async def render_draft(request: Request, track_id: int) -> dict:
    """Render the draft (Studio Render, top priority). A track with no
    draft yet is planned with the default track params first."""
    s = srv(request)
    row = _analyzed(s, track_id)
    d = s.library.draft(track_id)
    vid = d["id"] if d is not None and d["plan"] is not None else await _plan(s, row, PlanState())
    job = jobs.queue_render(s.db, vid, "studio", Priority.STUDIO)
    s.jobs_changed()
    return {"job_id": job}


def _in_queue(s: Server, track_id: int) -> bool:
    return s.db.execute("SELECT 1 FROM play_queue WHERE track_id = ?", (track_id,)).fetchone() is not None


def _rerender_active(s: Server, row, reason: str) -> int | None:
    if row["active_version_id"] is None:
        return None
    prio = Priority.QUEUED if _in_queue(s, row["id"]) else Priority.IMPORT
    return jobs.queue_render(s.db, row["active_version_id"], reason, prio)


@router.post("/tracks/{track_id}/rerender")
async def rerender(request: Request, track_id: int) -> dict:
    """Re-render the active version in place with its params and plan
    (FPS re-render, or re-render of a stale version)."""
    s = srv(request)
    row = track_or_404(s, track_id)
    job = _rerender_active(s, row, "rerender")
    if job is None:
        raise HTTPException(409, "the track has no active version")
    s.jobs_changed()
    return {"job_id": job}


@router.post("/tracks/rerender")
async def rerender_all(request: Request, which: Literal["stale", "fps_mismatch"] = Body(embed=True)) -> dict:
    """Library bulk actions: Re-render stale, Re-render FPS mismatch."""
    s = srv(request)
    out = []
    for row in s.db.execute("SELECT * FROM tracks ORDER BY id").fetchall():
        if s.library.animation_status(row, s.settings.display_fps) == which:
            out.append(_rerender_active(s, row, which))
    s.jobs_changed()
    return {"job_ids": out}


@router.get("/tracks/{track_id}/versions")
async def list_versions(request: Request, track_id: int, plan: bool = False) -> list[dict]:
    s = srv(request)
    row = track_or_404(s, track_id)
    return [s.library.version_dict(v, row, s.settings.display_fps, plan) for v in s.library.versions(track_id)]


class SaveVersion(BaseModel):
    name: str | None = None


@router.post("/tracks/{track_id}/versions")
async def save_version(request: Request, track_id: int, req: SaveVersion) -> dict:
    """Save draft as version."""
    s = srv(request)
    row = track_or_404(s, track_id)
    try:
        vid = s.library.save_draft(track_id, req.name)
    except ValueError as e:
        raise HTTPException(409, str(e)) from None
    s.hub.changed("track", track_id)
    return s.library.version_dict(s.library.version(vid), row, s.settings.display_fps)


@router.put("/tracks/{track_id}/active")
async def set_active(request: Request, track_id: int, version_id: int = Body(embed=True)) -> dict:
    s = srv(request)
    track_or_404(s, track_id)
    try:
        s.library.set_active(track_id, version_id)
    except KeyError as e:
        raise not_found(e) from None
    s.player.reconcile_next()   # the queue plays the active version
    s.hub.changed("track", track_id)
    s.hub.changed("tracks")
    return _track_out(s, s.library.track(track_id))


# -- presets
class PresetIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    generator: str = GENERATOR_ID
    params: DirectorParams

    @field_validator("generator")
    @classmethod
    def _known(cls, v: str) -> str:
        if v != GENERATOR_ID:
            raise ValueError(f"unknown generator {v!r}")
        return v


def _preset_out(row) -> dict:
    return {"id": row["id"], "generator": row["generator"], "name": row["name"],
            "params": dbm.loads(row["params"])}


@router.get("/presets")
async def list_presets(request: Request, generator: str = GENERATOR_ID) -> list[dict]:
    return [_preset_out(r) for r in srv(request).db.execute(
        "SELECT * FROM presets WHERE generator = ? ORDER BY name", (generator,))]


@router.post("/presets", status_code=201)
async def create_preset(request: Request, p: PresetIn) -> dict:
    s = srv(request)
    try:
        with s.db:
            pid = s.db.execute("INSERT INTO presets (generator, name, params) VALUES (?, ?, ?)",
                               (p.generator, p.name, dbm.dumps(p.params.model_dump(mode="json")))).lastrowid
    except sqlite3.IntegrityError:
        raise HTTPException(409, f"a preset named {p.name!r} exists") from None
    s.hub.changed("presets")
    return _preset_out(s.db.execute("SELECT * FROM presets WHERE id = ?", (pid,)).fetchone())


@router.put("/presets/{preset_id}")
async def update_preset(request: Request, preset_id: int, p: PresetIn) -> dict:
    s = srv(request)
    try:
        with s.db:
            n = s.db.execute("UPDATE presets SET generator = ?, name = ?, params = ? WHERE id = ?",
                             (p.generator, p.name, dbm.dumps(p.params.model_dump(mode="json")),
                              preset_id)).rowcount
    except sqlite3.IntegrityError:
        raise HTTPException(409, f"a preset named {p.name!r} exists") from None
    if n == 0:
        raise HTTPException(404, f"no preset {preset_id}")
    s.hub.changed("presets")
    return _preset_out(s.db.execute("SELECT * FROM presets WHERE id = ?", (preset_id,)).fetchone())


@router.delete("/presets/{preset_id}", status_code=204)
async def delete_preset(request: Request, preset_id: int):
    s = srv(request)
    with s.db:
        if s.db.execute("DELETE FROM presets WHERE id = ?", (preset_id,)).rowcount == 0:
            raise HTTPException(404, f"no preset {preset_id}")
    s.hub.changed("presets")


# -- play queue
def _queue(s: Server) -> list[int]:
    return [r["track_id"] for r in s.db.execute("SELECT track_id FROM play_queue ORDER BY pos")]


def _queue_out(s: Server) -> list[dict]:
    return [{"track_id": t, "animation": s.library.animation_status(s.library.track(t), s.settings.display_fps)}
            for t in _queue(s)]


def _queued(s: Server, track_id: int):
    """A track joined the queue: its import work moves up, and an active
    version at another rate gets an FPS re-render (Main flows 2)."""
    jobs.promote_track(s.db, track_id)
    row = s.library.track(track_id)
    if row["active_version_id"] is not None:
        v = s.library.version(row["active_version_id"])
        if v["anim_path"] is not None and v["fps"] != s.settings.display_fps:
            jobs.queue_render(s.db, v["id"], "fps", Priority.QUEUED)


def _queue_changed(s: Server):
    s.player.reconcile_next()
    s.jobs_changed()
    s.hub.changed("queue")


@router.get("/queue")
async def get_queue(request: Request) -> list[dict]:
    return _queue_out(srv(request))


@router.post("/queue")
async def append_queue(request: Request, track_id: int = Body(embed=True)) -> list[dict]:
    """Add to queue."""
    s = srv(request)
    track_or_404(s, track_id)
    with s.db:
        s.db.execute("INSERT INTO play_queue (pos, track_id) "
                     "VALUES ((SELECT COALESCE(MAX(pos), -1) + 1 FROM play_queue), ?)", (track_id,))
    _queued(s, track_id)
    _queue_changed(s)
    return _queue_out(s)


@router.put("/queue")
async def set_queue(request: Request, track_ids: list[int] = Body(embed=True)) -> list[dict]:
    """Replace the queue (reorder, remove). Positions keep growing, so an
    entry the engine preloaded as next is never confused with a new one."""
    s = srv(request)
    for t in track_ids:
        track_or_404(s, t)
    before = set(_queue(s))
    with s.db:
        base = s.db.execute("SELECT COALESCE(MAX(pos), -1) + 1 FROM play_queue").fetchone()[0]
        s.db.execute("DELETE FROM play_queue")
        s.db.executemany("INSERT INTO play_queue (pos, track_id) VALUES (?, ?)",
                         [(base + i, t) for i, t in enumerate(track_ids)])
    for t in dict.fromkeys(track_ids):
        if t not in before:
            _queued(s, t)
    _queue_changed(s)
    return _queue_out(s)


# -- jobs
@router.get("/jobs")
async def list_jobs(request: Request, state: list[str] | None = Query(None), limit: int = 200) -> list[dict]:
    s = srv(request)
    sql, args = "SELECT * FROM jobs", []
    if state:
        sql += f" WHERE state IN ({', '.join('?' * len(state))})"
        args += state
    sql += " ORDER BY id DESC LIMIT ?"
    out = [jobs.as_dict(r) for r in s.db.execute(sql, (*args, limit))]
    for j in out:   # the running job's latest progress (also pushed over the WebSocket)
        j["progress"] = s.worker.running_progress(j["id"]) if s.worker is not None else None
    return out


@router.delete("/jobs/{job_id}", status_code=204)
async def cancel_job(request: Request, job_id: int):
    """Cancel a queued job, or the running one: a render stops between
    chunks (it shows cancelled when the worker confirms); another job by
    restarting the worker."""
    s = srv(request)
    if jobs.cancel(s.db, job_id):
        s.jobs_changed()
        return
    if s.worker is None or not await s.worker.cancel_running(job_id):
        row = jobs.job(s.db, job_id)
        if row is None:
            raise HTTPException(404, f"no job {job_id}")
        raise HTTPException(409, f"job {job_id} is {row['state']}" + (
            "; only a worker this server started can be stopped" if row["state"] == jobs.RUNNING else ""))


@router.post("/jobs/{job_id}/retry")
async def retry_job(request: Request, job_id: int) -> dict:
    s = srv(request)
    try:
        job = jobs.retry(s.db, job_id)
        s.jobs_changed()
        return {"job_id": job}
    except KeyError as e:
        raise not_found(e) from None
    except ValueError as e:
        raise HTTPException(409, str(e)) from None


# -- storage
@router.get("/storage")
async def storage(request: Request) -> dict:
    """Disk used by track data, and orphaned track directories (no
    library entry)."""
    s = srv(request)
    lib = s.library
    total = await asyncio.to_thread(dir_size, lib.tracks_dir) if lib.tracks_dir.exists() else 0
    orphans = [{"key": d.name, "name": lib.orphan_name(d), "bytes": await asyncio.to_thread(dir_size, d)}
               for d in lib.orphans()]
    return {"tracks_bytes": total, "orphans": orphans}


@router.delete("/storage/orphans/{key}", status_code=204)
async def delete_orphan(request: Request, key: str):
    s = srv(request)
    match = [d for d in s.library.orphans() if d.name == key]
    if not match:
        raise HTTPException(404, f"no orphaned track directory {key!r}")
    await asyncio.to_thread(delete_dir, match[0])


# -- live state, transport, Studio (Main flows 2, 5, 7)
@router.get("/state")
async def get_state(request: Request) -> dict:
    """What the WebSocket's `state` message carries: engine and worker
    links, the player (mode, playback track, Studio selection, what the
    engine holds), and output (armed, nodes online)."""
    return srv(request).state()


@router.websocket("/ws")
async def websocket(ws: WebSocket):
    s: Server = ws.app.state.server
    await s.hub.serve(ws, s.state())


def _transport(s: Server, action, engine: bool = True):
    """Run a player action. Transport needs the engine; mode and Studio
    changes don't (the engine gets them when it connects)."""
    if engine and not (s.engine is not None and s.engine.ready):
        raise HTTPException(409, "the engine is not connected")
    try:
        action()
    except NotPlayable as e:
        raise HTTPException(409, str(e)) from None
    return s.player.snapshot()


@router.post("/player/play")
async def play(request: Request) -> dict:
    """Play: the paused track, else the playback track from its start, else
    the first playable track in the queue (Studio: the Studio track)."""
    s = srv(request)
    return _transport(s, s.player.play)


@router.post("/player/pause")
async def pause(request: Request) -> dict:
    s = srv(request)
    return _transport(s, s.player.pause)


@router.post("/player/stop")
async def stop(request: Request) -> dict:
    s = srv(request)
    return _transport(s, s.player.stop)


@router.post("/player/next")
async def next_track(request: Request) -> dict:
    s = srv(request)
    return _transport(s, s.player.next)


@router.post("/player/seek")
async def seek(request: Request, t: float = Body(embed=True, ge=0.0)) -> dict:
    s = srv(request)
    return _transport(s, lambda: s.player.seek(t))


@router.post("/player/play-now")
async def play_now(request: Request, track_id: int = Body(embed=True)) -> dict:
    """Library "Play now": in Playback, at once; the queue is unchanged."""
    s = srv(request)
    track_or_404(s, track_id)
    return _transport(s, lambda: s.player.play_now(track_id))


@router.put("/player/mode")
async def set_mode(request: Request, mode: Mode = Body(embed=True)) -> dict:
    """Engine mode: entering Studio pauses the queue; leaving it loads the
    playback track again where it was, paused."""
    s = srv(request)
    return _transport(s, lambda: s.player.set_mode(mode), engine=False)


class StudioSelection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    track_id: int | None
    version_id: int | None = None   # None: the draft if rendered, else the active version


@router.put("/studio")
async def open_studio(request: Request, sel: StudioSelection) -> dict:
    """The track open in Studio, and which of its versions plays there. A
    new track starts at 0; another version of the same one keeps the playhead."""
    s = srv(request)
    if sel.track_id is not None:
        track_or_404(s, sel.track_id)
        if sel.version_id is not None:
            try:
                if s.library.version(sel.version_id)["track_id"] != sel.track_id:
                    raise KeyError(f"version {sel.version_id} is not a version of track {sel.track_id}")
            except KeyError as e:
                raise not_found(e) from None
    elif sel.version_id is not None:
        raise HTTPException(422, "a version needs its track")
    return _transport(s, lambda: s.player.open_studio(sel.track_id, sel.version_id), engine=False)


# -- test patterns, calibration, arming (docs/ui.md → Display, Output)
class TestPattern(BaseModel):
    __test__ = False   # not a pytest class
    model_config = ConfigDict(extra="forbid")
    pattern: str
    params: dict[str, Any] = {}


@router.post("/display/test")
async def test_pattern(request: Request, t: TestPattern) -> dict:
    """Show a test pattern (or the calibration flash + click) in place of
    the animation; playback state is kept. The engine checks the name."""
    s = srv(request)
    if not s.engine_send(Test(pattern=t.pattern, params=t.params)):
        raise HTTPException(409, "the engine is not connected")
    return {"pattern": t.pattern}


@router.delete("/display/test", status_code=204)
async def test_pattern_off(request: Request):
    if not srv(request).engine_send(TestOff()):
        raise HTTPException(409, "the engine is not connected")


@router.put("/output/armed")
async def set_armed(request: Request, armed: bool | None = Body(embed=True)) -> dict:
    """Override arming (the bottom-bar toggle): true or false; null returns
    to automatic (armed while drive nodes answer). An invalid display
    config always stays disarmed."""
    s = srv(request)
    s.armed_override = armed
    s.reconfigure()
    return s.state()["output"]


@router.get("/worker/log")
async def worker_log(request: Request, lines: int = 200) -> list[str]:
    """The worker's latest log lines (also pushed over the WebSocket)."""
    log = srv(request).worker_log
    return list(log)[-lines:] if lines > 0 else []
