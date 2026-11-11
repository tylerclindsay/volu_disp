"""The worker's jobs (docs/architecture.md → Worker). Jobs are pure: a spec
goes in, files and a result come out. No DB and no IPC here; `Worker` runs
these and sends the messages.

Every output goes to <name>.tmp and is renamed when complete, so killing
the worker at any time leaves no partial file.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from ..analysis.pipeline import STAGES, analyze, set_override
from ..analysis.track import ANIM_DIR, TRACKS_DIR, TrackDir
from ..animfile import FORMAT_VERSION
from ..generators import GENERATOR_ID, Music, Plan, render_anim, render_context
from ..geometry import Geometry
from ..ipc import AnalyzeResult, AnalyzeSpec, RegridResult, RegridSpec, RenderResult, RenderSpec


class JobError(Exception):
    """A spec the worker can't run: unknown track, another geometry, …"""


@dataclass(frozen=True, eq=False)
class JobEnv:
    """What a job runs with.

    device     torch device for analysis; None: CUDA if available, else CPU
    log        one line of text for the server's job log
    progress   (stage, track name, frac 0–1, eta s or None)
    cancelled  True once the server cancelled the job (renders check it between chunks)
    """
    data_dir: Path
    geometry: Geometry
    device: str | None = None
    log: Callable[[str], None] = print
    progress: Callable[[str, str | None, float, float | None], None] = lambda *a: None
    cancelled: Callable[[], bool] = lambda: False


def _track(env: JobEnv, key: str) -> TrackDir:
    t = TrackDir(env.data_dir / TRACKS_DIR / key)
    try:
        t.source_path   # raises if the track was never imported
    except FileNotFoundError:
        raise JobError(f"no imported track {key} in {env.data_dir / TRACKS_DIR}") from None
    return t


def _summary(t: TrackDir) -> dict:
    tempo = t.data.get("tempo") or {}
    return {"key": t.dir.name, "duration": t.data["source"].get("duration"), "bpm": tempo.get("bpm"),
            "tempo_mode": tempo.get("mode"), "grid_override": tempo.get("override")}


def _device() -> str:
    import torch
    return "cuda" if torch.cuda.is_available() else "cpu"


def run_analyze(spec: AnalyzeSpec, env: JobEnv) -> AnalyzeResult:
    """Run the analysis stages on one track; finished stages are skipped
    unless forced. The first stage that fails fails the job."""
    stages = STAGES if spec.stages is None else spec.stages
    unknown = sorted((set(stages) | set(spec.force)) - set(STAGES))
    if unknown:
        raise JobError(f"unknown stages {unknown}; stages are {STAGES}")
    order = [s for s in STAGES if s in stages]
    t = _track(env, spec.key)

    def on_stage(stage: str, track: TrackDir):
        env.progress(stage, track.name, order.index(stage) / len(order), None)

    stats = analyze([t], order, set(spec.force), env.device or _device(), log=env.log,
                    on_stage=on_stage, keep_going=False)
    return AnalyzeResult(**_summary(t), stages=stats.get(t.name, {}))


def run_regrid(spec: RegridSpec, env: JobEnv) -> RegridResult:
    """Set or clear the user grid; re-derives phrases, events and bars."""
    t = _track(env, spec.key)
    if "tempo" not in t.data:
        raise JobError(f"track {spec.key} has no beat grid yet (analyze it first)")
    env.progress("regrid", t.name, 0.0, None)
    set_override(t, spec.bpm, spec.offset)
    return RegridResult(**_summary(t))


def anim_file(env: JobEnv, spec: RenderSpec) -> Path:
    """The render's output path; it must be <render_id>.anim in the track's
    animations dir."""
    path = (env.data_dir / spec.anim_path).resolve()
    want = (env.data_dir / TRACKS_DIR / spec.key / ANIM_DIR / f"{spec.render_id}.anim").resolve()
    if path != want:
        raise JobError(f"anim_path {spec.anim_path!r} is not {TRACKS_DIR}/{spec.key}/{ANIM_DIR}/"
                       f"{spec.render_id}.anim")
    return path


def run_render(spec: RenderSpec, env: JobEnv) -> RenderResult:
    """Render the plan exactly as given (a stale plan fails: the server
    re-plans). Raises RenderCancelled, leaving no file, when cancelled."""
    if spec.generator != GENERATOR_ID:
        raise JobError(f"unknown generator {spec.generator!r}")
    if spec.geometry_hash != env.geometry.hash:
        raise JobError(f"render is for geometry {spec.geometry_hash}, the worker's display config has "
                       f"{env.geometry.hash}: restart the worker with the server's display config")
    plan = Plan.model_validate(spec.plan)
    t = _track(env, spec.key)
    path = anim_file(env, spec)
    path.parent.mkdir(exist_ok=True)
    ctx = render_context(env.geometry, Music.from_track(t), spec.fps, plan)
    t0 = time.monotonic()

    def progress(done: int, total: int):
        elapsed = time.monotonic() - t0
        env.progress("render", t.name, done / total, elapsed * (total - done) / done if done else None)

    env.progress("render", t.name, 0.0, None)
    meta = render_anim(path, ctx, plan, spec.render_id, progress=progress, cancelled=env.cancelled)
    return RenderResult(render_id=spec.render_id, anim_path=spec.anim_path, frame_count=meta["frame_count"],
                        fps=meta["fps"], pixel_count=meta["pixel_count"], geometry_hash=meta["geometry_hash"],
                        format_version=FORMAT_VERSION, generator=meta["generator"]["id"],
                        generator_version=meta["generator"]["version"],
                        method_versions={s.method: s.method_version for s in plan.segments},
                        grid_override=meta["analysis"]["tempo_override"])


RUNNERS: dict[str, Callable] = {"analyze": run_analyze, "regrid": run_regrid, "render": run_render}
