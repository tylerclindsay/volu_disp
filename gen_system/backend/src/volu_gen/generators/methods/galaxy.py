"""`galaxy`: a tilted spiral galaxy, pixels as stars (docs/generators.md →
Methods).

Drawn in `ctx.box` coordinates. Each pixel's place in the disc (radius,
angle, height) is fixed per segment; the spiral arms are a density wave
turning rigidly with the disc, so a frame only shifts the arms' angle:
the rotation angle is the integral of a speed that follows the mix's
smoothed energy. Each pixel is a star with its own brightness and
twinkle (flickers on purpose, Smoothness); kicks brighten the whole galaxy.
"""

from __future__ import annotations

import numpy as np
from pydantic import Field

from ..method import GROOVE, Method, MethodParams, SegmentInfo, Suitability, register
from ..music import Music
from .common import Signal, color, hit_env, rotation, smoothstep, soft, tint

CORE = 0.22   # core radius, box units


class GalaxyParams(MethodParams):
    """arms     number of spiral arms
    twist    how tightly the arms wind
    radius   disc radius (box units)
    tilt     disc tilt from horizontal, degrees; yaw: its turn about y
    spin     turns per second at full energy (a third of it when quiet)
    stars    share of pixels that are bright stars; the rest are dust
    twinkle  how deep stars twinkle (0–1)
    kick     how much kicks brighten the galaxy
    color    palette index of the inner arms; the outer arms use the next"""
    arms: int = Field(2, ge=1, le=5)
    twist: float = Field(1.5, ge=0.0, le=4.0)
    radius: float = Field(1.1, ge=0.5, le=1.6)
    tilt: float = Field(30.0, ge=0.0, le=90.0)
    yaw: float = Field(0.0, ge=-180.0, le=180.0)
    spin: float = Field(0.08, ge=0.0, le=0.5)
    stars: float = Field(0.35, ge=0.0, le=1.0)
    twinkle: float = Field(0.35, ge=0.0, le=1.0)
    kick: float = Field(0.4, ge=0.0, le=1.0)
    color: int = Field(0, ge=0)


@register
class Galaxy(Method):
    id = "galaxy"
    version = 1
    Params = GalaxyParams
    suits = Suitability(phrase_types=frozenset({GROOVE, "breakdown", "buildup"}), energy_db=(-40.0, -4.0),
                        min_bars=4)

    @classmethod
    def choose_params(cls, seg: SegmentInfo, music: Music, rng: np.random.Generator) -> GalaxyParams:
        return GalaxyParams(arms=int(rng.integers(2, 5)), twist=float(rng.uniform(0.8, 2.2)),
                            tilt=float(rng.uniform(15, 60)), yaw=float(rng.uniform(-45, 45)),
                            spin=float(rng.uniform(0.05, 0.12)), color=int(rng.integers(8)))

    def __init__(self, ctx, seg, params: GalaxyParams):
        super().__init__(ctx, seg, params)
        p, rng, P = params, self.rng, ctx.pixel_count
        R = (rotation([0, 1, 0], np.radians([p.yaw])) @ rotation([1, 0, 0], np.radians([p.tilt])))[0]
        q = ctx.box.astype(np.float64) @ R   # disc frame: x, z in the disc, y its normal
        r = np.hypot(q[:, 0], q[:, 2])
        self.theta = np.arctan2(q[:, 2], q[:, 0]).astype(np.float32)
        self.wind = (p.twist * np.log(r + 0.05)).astype(np.float32)   # log spiral: arm angle grows with ln r
        w = ctx.pitch
        thick = w + 0.25 * np.exp(-(r / CORE) ** 2)          # disc thickness, a bulge at the core
        disc = np.exp(-(q[:, 1] / thick) ** 2) * soft(r - p.radius, 3 * w) * (0.3 + 0.7 * smoothstep(0, CORE, r))
        self.disc = disc.astype(np.float32)
        self.core = np.exp(-(r ** 2 + q[:, 1] ** 2) / CORE ** 2).astype(np.float32)
        star = rng.random(P) < p.stars
        self.star = np.where(star, 1.0, 0.5).astype(np.float32) * rng.uniform(0.8, 1.0, P).astype(np.float32)
        self.freq = rng.uniform(0.3, 2.0, P).astype(np.float32) * 2 * np.pi
        self.phase = rng.uniform(0, 2 * np.pi, P).astype(np.float32)
        inner, outer = color(ctx, p.color), color(ctx, p.color + 1)
        u = np.clip(r / p.radius, 0, 1)[:, None]
        self.arm_rgb = ((1 - u) * inner + u * outer).astype(np.float32)
        self.core_rgb = tint(inner, 0.6)
        e = Signal.envelope(ctx.music, "mix", seg, tau=1.0)
        self.turns = Signal(e.t, p.spin * (1 + 2 * np.clip(e.v, 0, 1.5)) / 3)   # turns/s: a third when quiet

    def render(self, start: int, stop: int) -> np.ndarray:
        ctx, p = self.ctx, self.params
        t = ctx.times(start, stop)
        turn = 2 * np.pi * self.turns.integral(t)
        angle = (self.theta[None, :] - turn.astype(np.float32)[:, None]) * p.arms - self.wind * p.arms
        arm = 0.15 + 0.85 * ((1 + np.cos(angle)) / 2) ** 1.5   # dim dust between the arms
        tf = t.astype(np.float32)[:, None]
        tw = 1 - p.twinkle * ((1 + np.sin(self.freq * tf + self.phase)) / 2) ** 2
        lift = 1 + p.kick * hit_env(ctx.music, "kick", t, 0.25)[:, None]
        a = (arm * self.disc * self.star * tw * lift)[..., None] * self.arm_rgb
        c = (self.core * (0.6 + 0.4 * tw) * lift)[..., None] * self.core_rgb
        return np.minimum(a + c, 1.0).astype(np.float32)

