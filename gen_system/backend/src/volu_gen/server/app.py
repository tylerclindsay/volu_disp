"""The API server's HTTP API for the UI (docs/architecture.md → API
server, docs/ui.md). Routes are under /api. The server is the only writer
of the DB; blocking work (file copies, planning, deletes) runs off the
event loop.
"""

from __future__ import annotations

import asyncio
import sqlite3
import uuid
from pathlib import Path
from typing import Any, Literal

import numpy as np
from fastapi import APIRouter, Body, FastAPI, HTTPException, Query, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, ConfigDict, ValidationError

from ..analysis import library_moods
from ..analysis.peaks import level_rate
from ..analysis.track import TRACKS_DIR
from ..generators import DIRECTOR_VERSION, GENERATOR_ID, METHODS, PALETTES, DirectorParams, Plan
from . import db as dbm
from . import jobs
from . import settings as settings_mod
from .display import Display
from .jobs import Priority
from .library import Library, delete_dir, dir_size, scan
from .planning import PlanState, pinned_of, plan_track


class Server:
    """Everything the routes share: the display, the DB, the settings."""

    def __init__(self, display: Display, data_dir: Path, db_path: Path | str | None = None):
        self.display = display
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.db = dbm.connect(db_path if db_path is not None else self.data_dir / dbm.DB_NAME)
        self.library = Library(self.db, self.data_dir, display)
        self.settings = settings_mod.load(self.db, display)
        self._moods: dict[str, dict[str, float]] | None = None

    @classmethod
    def open(cls, display_path: Path, data_dir: Path) -> Server:
        return cls(Display.load(display_path), data_dir)

    def invalidate_moods(self):
        """Call when a track's mood analysis changes."""
        self._moods = None

    async def mood_z(self, sha256: str) -> dict[str, float] | None:
        """Library-wide mood z-scores of one track (decision 0004), or None
        while the library is too small."""
        if self._moods is None:
            self._moods = await asyncio.to_thread(library_moods, self.data_dir)
        return self._moods.get(sha256)

    def close(self):
        self.db.close()


def create_app(server: Server) -> FastAPI:
    app = FastAPI(title="volu_disp", version="0.1.0")
    app.state.server = server
    app.include_router(router, prefix="/api")
    return app


router = APIRouter()


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
    """Change some settings. A new display FPS queues an FPS re-render of
    the active version of every track in the play queue (Main flows 6)."""
    s = srv(request)
    try:
        new = settings_mod.patch(s.settings, changes, s.display)
    except ValidationError as e:
        raise unprocessable(e) from None
    old_fps = s.settings.display_fps
    settings_mod.save(s.db, new)
    s.settings = new
    if new.display_fps != old_fps:
        for v in _queue_mismatches(s, new.display_fps):
            jobs.queue_render(s.db, v["id"], "fps", Priority.QUEUED)
    return new


def _queue_mismatches(s: Server, fps: int) -> list:
    """Active versions of queued tracks that are rendered at another rate."""
    return s.db.execute(
        "SELECT DISTINCT v.* FROM play_queue q JOIN tracks t ON t.id = q.track_id "
        "JOIN versions v ON v.id = t.active_version_id "
        "WHERE v.anim_path IS NOT NULL AND v.fps != ?", (fps,)).fetchall()


@router.get("/settings/fps-impact")
async def fps_impact(request: Request, fps: int) -> dict:
    """What changing the display FPS to `fps` would do, for the
    confirmation dialog."""
    s = srv(request)
    unplayable = s.db.execute("SELECT COUNT(*) FROM versions WHERE anim_path IS NOT NULL AND fps = ?",
                              (s.settings.display_fps,)).fetchone()[0] if fps != s.settings.display_fps else 0
    return {"fps": fps, "problems": s.display.fps_problems(fps),
            "versions_unplayable": unplayable,
            "queue_rerenders": len(_queue_mismatches(s, fps))}


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
    jobs go too. Its directory stays unless `delete_data`."""
    s = srv(request)
    track_or_404(s, track_id)
    path = s.library.remove(track_id, delete_data)
    s.invalidate_moods()
    if path is not None and path.exists():
        await asyncio.to_thread(delete_dir, path)


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
    bpm: float
    offset: float   # time of any downbeat, s


@router.put("/tracks/{track_id}/grid")
async def set_grid(request: Request, track_id: int, grid: GridOverride) -> dict:
    """Override the fitted beat grid (Studio header). Queues a `regrid`
    job; the draft is re-planned when it finishes."""
    s = srv(request)
    row = _analyzed(s, track_id)
    if not 20 <= grid.bpm <= 400:
        raise HTTPException(422, "bpm must be from 20 to 400")
    return {"job_id": jobs.queue_regrid(s.db, track_id, row["key"], grid.bpm, grid.offset)}


@router.delete("/tracks/{track_id}/grid")
async def clear_grid(request: Request, track_id: int) -> dict:
    """Return to the fitted grid."""
    s = srv(request)
    row = _analyzed(s, track_id)
    return {"job_id": jobs.queue_regrid(s.db, track_id, row["key"], None, None)}


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
    return s.library.set_draft_plan(track_row["id"], state.params, plan.model_dump(mode="json"))


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
    return {"job_id": jobs.queue_render(s.db, vid, "studio", Priority.STUDIO)}


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
    return {"job_id": job}


@router.post("/tracks/rerender")
async def rerender_all(request: Request, which: Literal["stale", "fps_mismatch"] = Body(embed=True)) -> dict:
    """Library bulk actions: Re-render stale, Re-render FPS mismatch."""
    s = srv(request)
    out = []
    for row in s.db.execute("SELECT * FROM tracks ORDER BY id").fetchall():
        if s.library.animation_status(row, s.settings.display_fps) == which:
            out.append(_rerender_active(s, row, which))
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
    return s.library.version_dict(s.library.version(vid), row, s.settings.display_fps)


@router.put("/tracks/{track_id}/active")
async def set_active(request: Request, track_id: int, version_id: int = Body(embed=True)) -> dict:
    s = srv(request)
    track_or_404(s, track_id)
    try:
        s.library.set_active(track_id, version_id)
    except KeyError as e:
        raise not_found(e) from None
    return _track_out(s, s.library.track(track_id))


# -- presets
class PresetIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    generator: str = GENERATOR_ID
    params: DirectorParams


def _preset_out(row) -> dict:
    return {"id": row["id"], "generator": row["generator"], "name": row["name"],
            "params": dbm.loads(row["params"])}


@router.get("/presets")
async def list_presets(request: Request, generator: str = GENERATOR_ID) -> list[dict]:
    return [_preset_out(r) for r in srv(request).db.execute(
        "SELECT * FROM presets WHERE generator = ? ORDER BY name", (generator,))]


def _check_generator(generator: str):
    if generator != GENERATOR_ID:
        raise HTTPException(422, f"unknown generator {generator!r}")


@router.post("/presets", status_code=201)
async def create_preset(request: Request, p: PresetIn) -> dict:
    s = srv(request)
    _check_generator(p.generator)
    try:
        with s.db:
            pid = s.db.execute("INSERT INTO presets (generator, name, params) VALUES (?, ?, ?)",
                               (p.generator, p.name, dbm.dumps(p.params.model_dump(mode="json")))).lastrowid
    except sqlite3.IntegrityError:
        raise HTTPException(409, f"a preset named {p.name!r} exists") from None
    return _preset_out(s.db.execute("SELECT * FROM presets WHERE id = ?", (pid,)).fetchone())


@router.put("/presets/{preset_id}")
async def update_preset(request: Request, preset_id: int, p: PresetIn) -> dict:
    s = srv(request)
    _check_generator(p.generator)
    try:
        with s.db:
            n = s.db.execute("UPDATE presets SET generator = ?, name = ?, params = ? WHERE id = ?",
                             (p.generator, p.name, dbm.dumps(p.params.model_dump(mode="json")),
                              preset_id)).rowcount
    except sqlite3.IntegrityError:
        raise HTTPException(409, f"a preset named {p.name!r} exists") from None
    if n == 0:
        raise HTTPException(404, f"no preset {preset_id}")
    return _preset_out(s.db.execute("SELECT * FROM presets WHERE id = ?", (preset_id,)).fetchone())


@router.delete("/presets/{preset_id}", status_code=204)
async def delete_preset(request: Request, preset_id: int):
    s = srv(request)
    with s.db:
        if s.db.execute("DELETE FROM presets WHERE id = ?", (preset_id,)).rowcount == 0:
            raise HTTPException(404, f"no preset {preset_id}")


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
    return _queue_out(s)


@router.put("/queue")
async def set_queue(request: Request, track_ids: list[int] = Body(embed=True)) -> list[dict]:
    """Replace the queue (reorder, remove)."""
    s = srv(request)
    for t in track_ids:
        track_or_404(s, t)
    before = set(_queue(s))
    with s.db:
        s.db.execute("DELETE FROM play_queue")
        s.db.executemany("INSERT INTO play_queue (pos, track_id) VALUES (?, ?)", list(enumerate(track_ids)))
    for t in dict.fromkeys(track_ids):
        if t not in before:
            _queued(s, t)
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
    return [jobs.as_dict(r) for r in s.db.execute(sql, (*args, limit))]


@router.delete("/jobs/{job_id}", status_code=204)
async def cancel_job(request: Request, job_id: int):
    s = srv(request)
    if not jobs.cancel(s.db, job_id):
        raise HTTPException(409, f"job {job_id} is not queued")


@router.post("/jobs/{job_id}/retry")
async def retry_job(request: Request, job_id: int) -> dict:
    s = srv(request)
    try:
        return {"job_id": jobs.retry(s.db, job_id)}
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
