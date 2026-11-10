"""The render job: a plan → frames → an animation file
(docs/generators.md → Output and render job).

The plan is retimed on the current grid and rendered exactly; a plan made
by another director or method version is refused (the server re-plans
first). Each segment's method renders the frames it covers; a crossfade is
centered on the segment boundary, and each frame's weights sum to 1:

    weight_i(t) = ramp(t, start_i, h_i) − ramp(t, end_i, h_i+1)

`ramp` steps from 0 to 1 over [T − h, T + h] (smoothstep; a step at T for a
cut, h = 0) and h is half the fade, clipped to half of either segment so
fades never overlap. Frames are blended in linear light, scaled by the
track's intensity, clipped and quantized to uint8 (decision 0018: the file
holds WS2811 PWM values, linear in light, so no gamma curve is applied).

Rendering any frame range gives the same frames as rendering the whole
track, so chunk size never changes the output.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Iterator, Mapping
from dataclasses import replace
from pathlib import Path

import numpy as np

from ..animfile import AnimWriter
from ..geometry import Geometry
from .context import RenderContext
from .director import DIRECTOR_VERSION, DirectorParams
from .method import METHODS, Method, SegmentInfo
from .music import Music
from .plan import Plan

GENERATOR_ID = "director"


class StalePlan(ValueError):
    """The plan was made by another director or method version, or names a
    method that isn't registered: re-plan before rendering."""


class RenderCancelled(Exception):
    """The job was cancelled between chunks; no file was written."""


def to_uint8(linear: np.ndarray) -> np.ndarray:
    """Linear RGB → uint8 PWM values: clipped to [0, 1], rounded (decision 0018)."""
    return np.rint(np.clip(linear, 0.0, 1.0) * 255.0).astype(np.uint8)


def render_context(geometry: Geometry, music: Music, fps: int, plan: Plan) -> RenderContext:
    """The context a plan renders in: its seed, palette and strobe limit."""
    params = DirectorParams.model_validate(plan.director.params)
    return RenderContext(geometry, music, fps, seed=params.seed, palette=tuple(plan.palette),
                         strobe_max_rate=params.strobe_max_rate)


def _frame(t: float, fps: int) -> int:
    """The first frame starting at or after t."""
    return math.ceil(t * fps - 1e-9)


def _ramp(t: np.ndarray, at: float, half: float) -> np.ndarray:
    """0 before, 1 after: smoothstep over [at − half, at + half], or a step
    at `at` (frames starting at it are after) when half is 0."""
    if half <= 0:
        return (t >= at).astype(np.float32)
    u = np.clip((t - (at - half)) / (2 * half), 0.0, 1.0)
    return (u * u * (3 - 2 * u)).astype(np.float32)


class Renderer:
    """Renders a plan's frames at the context's FPS.

    The plan is retimed on `ctx.music`'s grid. Raises StalePlan for another
    director or method version, and pydantic's ValidationError for params
    or overrides the method rejects, before any frame is rendered.
    """

    def __init__(self, ctx: RenderContext, plan: Plan, methods: Mapping[str, type[Method]] = METHODS):
        if plan.director.version != DIRECTOR_VERSION:
            raise StalePlan(f"plan made by director version {plan.director.version}, "
                            f"current is {DIRECTOR_VERSION}")
        for s in plan.segments:
            cls = methods.get(s.method)
            if cls is None:
                raise StalePlan(f"bars {s.start_bar}–{s.end_bar}: method {s.method!r} is not registered")
            if cls.version != s.method_version:
                raise StalePlan(f"bars {s.start_bar}–{s.end_bar}: {s.method} version {s.method_version}, "
                                f"current is {cls.version}")
        self.ctx = ctx
        self.plan = plan.retime(ctx.music)
        self.intensity = DirectorParams.model_validate(plan.director.params).intensity
        self._methods = methods
        self._params = [methods[s.method].Params.model_validate({**s.params, **s.overrides})
                        for s in self.plan.segments]
        self._half = [0.0] + [self._half_fade(a, b) for a, b in zip(self.plan.segments, self.plan.segments[1:])]
        self._half.append(0.0)
        self._live: dict[int, Method] = {}

    def _half_fade(self, prev, seg) -> float:
        if seg.transition.kind == "cut":
            return 0.0
        d = seg.transition.beats * float(self.ctx.music.beat_length(seg.start))
        return min(d, prev.end - prev.start, seg.end - seg.start) / 2

    @property
    def frame_count(self) -> int:
        return self.ctx.frame_count

    def extent(self, i: int) -> tuple[int, int]:
        """Frames segment i contributes to (its own plus its fades), clipped to the track."""
        s = self.plan.segments[i]
        return (max(_frame(s.start - self._half[i], self.ctx.fps), 0),
                min(_frame(s.end + self._half[i + 1], self.ctx.fps), self.frame_count))

    def weights(self, i: int, t: np.ndarray) -> np.ndarray:
        """Segment i's blend weight at each time; summed over segments, 1."""
        s = self.plan.segments[i]
        return _ramp(t, s.start, self._half[i]) - _ramp(t, s.end, self._half[i + 1])

    def _method(self, i: int) -> Method:
        m = self._live.get(i)
        if m is None:
            s = self.plan.segments[i]
            info = replace(SegmentInfo.from_bars(self.ctx.music, s.start_bar, s.end_bar), start=s.start, end=s.end)
            m = self._live[i] = self._methods[s.method](self.ctx, info, self._params[i])
        return m

    def linear(self, start: int, stop: int) -> np.ndarray:
        """Frames start … stop − 1, blended, as float32 linear RGB (before
        intensity and clipping), shape (stop − start, P, 3)."""
        if not 0 <= start <= stop <= self.frame_count:
            raise ValueError(f"frames {start}–{stop} outside 0–{self.frame_count}")
        P = self.ctx.pixel_count
        out = np.zeros((stop - start, P, 3), np.float32)
        t = self.ctx.times(start, stop)
        for i, s in enumerate(self.plan.segments):
            a, b = self.extent(i)
            if b <= start or a >= stop:
                continue
            w = self.weights(i, t)
            nz = np.flatnonzero(w > 0)
            if not len(nz):
                continue
            j0, j1 = int(nz[0]), int(nz[-1]) + 1
            frames = self._method(i).render(start + j0, start + j1)
            if not (isinstance(frames, np.ndarray) and frames.dtype == np.float32
                    and frames.shape == (j1 - j0, P, 3)):
                got = f"{frames.dtype} {frames.shape}" if isinstance(frames, np.ndarray) else type(frames).__name__
                raise TypeError(f"{s.method}.render({start + j0}, {start + j1}) returned {got}, "
                                f"expected float32 ({j1 - j0}, {P}, 3)")
            if not np.isfinite(frames).all():
                raise ValueError(f"{s.method}.render({start + j0}, {start + j1}) returned NaN or inf")
            wj = w[j0:j1]
            if (wj == 1).all():
                out[j0:j1] += frames
            else:
                out[j0:j1] += frames * wj[:, None, None]
        return out

    def render(self, start: int, stop: int) -> np.ndarray:
        """Frames start … stop − 1 as file frames: uint8 (n, P, 3)."""
        return to_uint8(self.linear(start, stop) * np.float32(self.intensity))

    def chunks(self, chunk_frames: int) -> Iterator[np.ndarray]:
        """The whole track in order, chunk_frames frames at a time. Methods
        are built when their first frame is reached and dropped after
        their last, so memory stays flat over a long track."""
        for start in range(0, self.frame_count, chunk_frames):
            stop = min(start + chunk_frames, self.frame_count)
            yield self.render(start, stop)
            for i in [i for i in self._live if self.extent(i)[1] <= stop]:
                del self._live[i]


def anim_meta(ctx: RenderContext, plan: Plan, render_id: str) -> dict:
    """The .anim metadata of a render (decision 0008); `plan` retimed."""
    a = ctx.music.analysis
    return {
        "render_id": render_id,
        "track_sha256": a["source"]["sha256"],
        "geometry_hash": ctx.geometry.hash,
        "pixel_count": ctx.pixel_count,
        "fps": ctx.fps,
        "frame_count": ctx.frame_count,
        "generator": {"id": GENERATOR_ID, "version": DIRECTOR_VERSION},
        "params": plan.director.params,
        "plan": plan.model_dump(mode="json"),
        "analysis": {"version": a.get("version"), "tempo_override": a["tempo"].get("override")},
    }


def render_anim(path: Path, ctx: RenderContext, plan: Plan, render_id: str, *,
                progress: Callable[[int, int], None] | None = None,
                cancelled: Callable[[], bool] | None = None,
                methods: Mapping[str, type[Method]] = METHODS) -> dict:
    """Render a plan into `path` (via <path>.tmp, renamed when done) in
    1 s chunks. Between chunks, `progress(frames_done, frame_count)` is
    called and `cancelled()` checked; a cancel raises RenderCancelled and
    leaves no file. Returns the file's metadata."""
    r = Renderer(ctx, plan, methods)
    with AnimWriter(path, anim_meta(ctx, r.plan, render_id)) as w:
        for frames in r.chunks(w.chunk_frames):
            w.write(frames)
            if progress is not None:
                progress(w.frames_written, r.frame_count)
            if cancelled is not None and w.frames_written < r.frame_count and cancelled():
                raise RenderCancelled(render_id)
    return w.meta
