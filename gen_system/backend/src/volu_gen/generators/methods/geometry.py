"""`geometry`: a shape spinning and drifting through the volume
(docs/generators.md → Methods).

Drawn in `ctx.box` coordinates. Each shape is a distance to the lit set
(a sphere's shell, a cube's edges, a torus's tube, …), lit with a soft
edge about one pitch wide. The shape turns at a steady rate per bar,
drifts on a slow Lissajous path, pulses in scale on kicks and glows with
the bass. With `swap_beats`, the next shape in `shapes` morphs in (the two
distances blended) so that it lands on the beat.
"""

from __future__ import annotations

from typing import Literal

import numpy as np
from pydantic import Field

from ..method import Method, MethodParams, SegmentInfo, Suitability, register
from ..music import Music
from .common import Signal, color, hit_env, rotation, smoothstep, soft, tint

Shape = Literal["sphere", "cube", "octahedron", "torus", "rings"]
SHAPES: tuple[str, ...] = Shape.__args__


def distance(shape: str, q: np.ndarray) -> np.ndarray:
    """Unsigned distance from local points (…, 3) to the shape's lit set, at
    unit size (it fits a sphere of radius about 1)."""
    x, y, z = q[..., 0], q[..., 1], q[..., 2]
    if shape == "sphere":
        return np.abs(np.sqrt(x * x + y * y + z * z) - 1)
    if shape == "cube":   # the 12 edges of a cube of half-side b
        b = 0.62
        a = np.abs(q) - b
        o = np.maximum(a, 0)
        return np.sqrt(np.minimum(np.minimum(o[..., 0] ** 2 + a[..., 1] ** 2 + a[..., 2] ** 2,
                                             a[..., 0] ** 2 + o[..., 1] ** 2 + a[..., 2] ** 2),
                                  a[..., 0] ** 2 + a[..., 1] ** 2 + o[..., 2] ** 2))
    if shape == "octahedron":
        return np.abs(np.abs(x) + np.abs(y) + np.abs(z) - 1) * 0.57735
    if shape == "torus":   # a tube of radius 0.25 around a ring of radius 0.75
        return np.maximum(np.sqrt((np.sqrt(x * x + z * z) - 0.75) ** 2 + y * y) - 0.25, 0)
    if shape == "rings":   # three rings at right angles, a gyroscope
        return np.minimum(np.minimum(np.sqrt((np.sqrt(x * x + z * z) - 1) ** 2 + y * y),
                                     np.sqrt((np.sqrt(x * x + y * y) - 1) ** 2 + z * z)),
                          np.sqrt((np.sqrt(y * y + z * z) - 1) ** 2 + x * x))
    raise ValueError(f"unknown shape {shape!r}")


class GeometryParams(MethodParams):
    """shapes      the shapes, in order (one: no swapping)
    swap_beats  morph to the next shape every this many beats (0: never)
    morph       morph length, beats; it ends on the swap beat
    size        shape radius (box units)
    spin        turns per bar
    wander      how far the shape drifts from the center (0–1)
    pulse       scale pulse on kicks (× size)
    glow        halo with the bass (0–1)
    color       palette index of the first shape; each next shape, the next color"""
    shapes: list[Shape] = Field(["cube"], min_length=1, max_length=5)
    swap_beats: Literal[0, 1, 2, 4, 8, 16] = 0
    morph: float = Field(0.4, gt=0.0, le=4.0)
    size: float = Field(0.7, ge=0.2, le=1.2)
    spin: float = Field(0.125, ge=0.0, le=1.0)
    wander: float = Field(0.3, ge=0.0, le=1.0)
    pulse: float = Field(0.2, ge=0.0, le=0.6)
    glow: float = Field(0.4, ge=0.0, le=1.0)
    color: int = Field(0, ge=0)


@register
class Shapes(Method):
    id = "geometry"
    version = 1
    Params = GeometryParams
    suits = Suitability(energy_db=(-20.0, 6.0))

    @classmethod
    def choose_params(cls, seg: SegmentInfo, music: Music, rng: np.random.Generator) -> GeometryParams:
        drop = "drop" in seg.tags
        n = int(rng.integers(2, 4)) if drop or seg.energy_db > -8 else 1
        shapes = [SHAPES[i] for i in rng.permutation(len(SHAPES))[:n]]
        swap = 0 if n == 1 else int(rng.choice([1, 2])) if drop else 4
        return GeometryParams(shapes=shapes, swap_beats=swap, size=float(rng.uniform(0.55, 0.8)),
                              spin=float(rng.uniform(0.06, 0.2)), wander=float(rng.uniform(0.2, 0.6)),
                              pulse=float(rng.uniform(0.25, 0.4) if drop else rng.uniform(0.1, 0.25)),
                              glow=float(rng.uniform(0.3, 0.8)), color=int(rng.integers(8)))

    def __init__(self, ctx, seg, params: GeometryParams):
        super().__init__(ctx, seg, params)
        p, rng, music = params, self.rng, ctx.music
        self.axis = rng.normal(size=3)                       # tumble axis, turned at a fraction of spin
        self.tumble = float(rng.uniform(0.3, 0.6))
        self.drift = rng.uniform(0.04, 0.12, 3)              # drift cycles per bar on x, y, z
        self.drift_phase = rng.uniform(0, 2 * np.pi, 3)
        self.beat0 = float(np.round(music.beat_at(seg.start)))
        self.bass = Signal.envelope(music, "bass", seg, tau=0.2) if p.glow > 0 else None
        self.rgb = np.stack([color(ctx, p.color + i) for i in range(len(p.shapes))])
        self.halo_rgb = tint(color(ctx, p.color + len(p.shapes)), 0.2)

    def _shapes(self, t: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        """Per frame: the shape morphed from, the shape morphed to, and the
        morph weight (0: from, 1: to)."""
        p, L = self.params, len(self.params.shapes)
        if L == 1 or p.swap_beats == 0:
            z = np.zeros(len(t), np.int64)
            return z, z, np.ones(len(t), np.float32)
        morph = min(p.morph, p.swap_beats / 2)
        b = self.ctx.music.beat_at(t) - self.beat0 + morph   # the next shape starts morphing in `morph` beats early
        k = np.floor(b / p.swap_beats).astype(np.int64)
        m = smoothstep(0.0, morph, b - k * p.swap_beats)
        return np.mod(k - 1, L), np.mod(k, L), m

    def render(self, start: int, stop: int) -> np.ndarray:
        ctx, p, music = self.ctx, self.params, self.ctx.music
        t = ctx.times(start, stop)
        bar = music.bar_at(t)
        spin = 2 * np.pi * p.spin * bar
        R = rotation([0, 1, 0], spin) @ rotation(self.axis, self.tumble * spin)
        reach = p.wander * max(1 - 0.5 * p.size, 0.2)
        c = reach * np.sin(2 * np.pi * self.drift[None] * bar[:, None] + self.drift_phase[None])
        scale = p.size * (1 + p.pulse * hit_env(music, "kick", t, 0.12))
        q = np.einsum("npi,nij->npj", (ctx.box[None] - c[:, None].astype(np.float32)), R.astype(np.float32))
        q /= scale.astype(np.float32)[:, None, None]

        a, b, m = self._shapes(t)
        d = np.zeros((len(t), ctx.pixel_count), np.float32)
        rgb = np.zeros((len(t), 3), np.float32)
        for i, shape in enumerate(p.shapes):
            w = np.where(a == i, 1 - m, 0) + np.where(b == i, m, 0)   # this shape's morph weight per frame
            f = np.flatnonzero(w > 0)
            if len(f):
                d[f] += w[f, None] * distance(shape, q[f])
                rgb[f] += w[f, None] * self.rgb[i]
        d *= scale.astype(np.float32)[:, None]               # back to box units

        w = ctx.pitch
        v = soft(d - 0.4 * w, w)[..., None] * rgb[:, None, :]
        if self.bass is not None:
            g = p.glow * 0.4 * np.clip(self.bass(t), 0, 1.5)[:, None] * np.exp(-(d / (3 * w)) ** 2)
            v = v + g[..., None] * self.halo_rgb
        return np.minimum(v, 1.0).astype(np.float32)
