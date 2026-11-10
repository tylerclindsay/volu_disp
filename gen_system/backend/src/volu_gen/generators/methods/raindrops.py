"""`raindrops`: a sea surface with drops falling into it and ripples
spreading from where they land (docs/generators.md → Methods).

Drawn in `ctx.box` coordinates (each axis [-1, 1], y up), so the sea
spans the whole floor of any display. Drops land on hats (some of them),
big drops on strong kicks, and a few at random times so calm parts without
hats still rain. The drop list (time, place, size) is made once per
segment; each frame is then closed-form: a drop falls under gravity for
FALL s before it lands, and its ripple is a ring of radius speed × age that
fades out. Bass lifts the sea (swell).
"""

from __future__ import annotations

import numpy as np
from pydantic import Field

from ..method import GROOVE, Method, MethodParams, SegmentInfo, Suitability, register
from ..music import Music
from .common import Signal, color, soft, span, tint

FALL = 0.35            # s a drop falls before landing
BIG_KICK = 0.5         # kick strength at or above which a kick drops a big drop
RIPPLE_DECAY = 0.45    # s, time constant of a ripple's fade


class RaindropsParams(MethodParams):
    """level    sea height (box units, −1 floor, 1 ceiling)
    swell    how far bass lifts the sea
    waves    height of the slow background waves
    hats     share of hats that drop a drop (0–1)
    kicks    strong kicks drop big drops
    ambient  extra drops per second at random times
    speed    ripple speed (box units per second)
    fill     brightness of the water under the surface
    color    palette index of the sea; drops use the next color"""
    level: float = Field(-0.5, ge=-0.9, le=0.5)
    swell: float = Field(0.25, ge=0.0, le=0.6)
    waves: float = Field(0.06, ge=0.0, le=0.3)
    hats: float = Field(0.5, ge=0.0, le=1.0)
    kicks: bool = True
    ambient: float = Field(1.0, ge=0.0, le=8.0)
    speed: float = Field(1.2, ge=0.3, le=4.0)
    fill: float = Field(0.08, ge=0.0, le=0.4)
    color: int = Field(0, ge=0)


@register
class Raindrops(Method):
    id = "raindrops"
    version = 1
    Params = RaindropsParams
    suits = Suitability(phrase_types=frozenset({GROOVE, "breakdown", "buildup"}), energy_db=(-30.0, -2.0),
                        min_bars=2)

    @classmethod
    def choose_params(cls, seg: SegmentInfo, music: Music, rng: np.random.Generator) -> RaindropsParams:
        calm = seg.energy_db < -10 or "breakdown" in seg.tags
        return RaindropsParams(level=float(rng.uniform(-0.6, -0.2)), swell=float(rng.uniform(0.15, 0.35)),
                               hats=float(rng.uniform(0.3, 0.7)), ambient=2.0 if calm else 0.5,
                               speed=float(rng.uniform(0.9, 1.6)), color=int(rng.integers(8)))

    def __init__(self, ctx, seg, params: RaindropsParams):
        super().__init__(ctx, seg, params)
        p, music, rng = params, ctx.music, self.rng
        t0, t1 = span(seg)
        drops = []   # (land time, x, z, size)
        ht, hs = music.onsets("hat", t0, t1 + FALL)
        keep = rng.random(len(ht)) < p.hats
        for t, s, (x, z) in zip(ht[keep], hs[keep], rng.uniform(-0.9, 0.9, (int(keep.sum()), 2))):
            drops.append((t, x, z, 0.5 + 0.4 * s))
        if p.kicks:
            kt, _ = music.onsets("kick", t0, t1 + FALL, min_strength=BIG_KICK)
            for t, (x, z) in zip(kt, np.clip(rng.normal(0, 0.3, (len(kt), 2)), -0.8, 0.8)):
                drops.append((t, x, z, 1.2))
        n = rng.poisson(p.ambient * (t1 + FALL - t0))
        for t, x, z, s in zip(np.sort(rng.uniform(t0, t1 + FALL, n)), *rng.uniform(-0.9, 0.9, (2, n)),
                              rng.uniform(0.3, 0.7, n)):
            drops.append((t, x, z, s))
        d = np.array(sorted(drops), np.float64).reshape(-1, 4)
        self.drops = d
        self.bass = Signal.envelope(music, "bass", seg, tau=0.25)
        self.life = min(4 * RIPPLE_DECAY, 3.0 / p.speed)
        self.wave = rng.uniform(0, 2 * np.pi, 4)   # phases of the background waves
        c = color(ctx, p.color)
        self.sea, self.ripple, self.drop = c, tint(color(ctx, p.color + 1), 0.3), tint(color(ctx, p.color + 1), 0.6)

    def _level(self, t: np.ndarray) -> np.ndarray:
        p = self.params
        return (p.level + p.swell * np.clip(self.bass(t), 0.0, 1.5)).astype(np.float32)

    def render(self, start: int, stop: int) -> np.ndarray:
        ctx, p = self.ctx, self.params
        t = ctx.times(start, stop)
        x, y, z = ctx.box[:, 0], ctx.box[:, 1], ctx.box[:, 2]
        w = ctx.pitch
        tf = t.astype(np.float32)[:, None]
        a = self.wave.astype(np.float32)
        h = (self._level(t)[:, None]
             + p.waves * 0.5 * (np.sin(2.1 * x + 0.9 * tf + a[0]) + np.sin(1.7 * z - 0.7 * tf + a[1]))
             + p.waves * 0.3 * np.sin(1.3 * (x + z) + 1.3 * tf + a[2]))
        bright = np.zeros(h.shape, np.float32)     # ripple highlights
        drop = np.zeros(h.shape, np.float32)
        lo, hi = np.searchsorted(self.drops[:, 0], [t[0] - self.life, t[-1] + FALL], side="right")
        for T, dx, dz, s in self.drops[lo:hi]:
            # ripple: a ring of radius speed × age that lifts and lights the surface
            j0, j1 = np.searchsorted(t, [T, T + self.life])
            if j1 > j0:
                age = (t[j0:j1] - T).astype(np.float32)[:, None]
                dist = np.sqrt((x - dx) ** 2 + (z - dz) ** 2)
                ring = s * np.exp(-age / RIPPLE_DECAY) * np.exp(-((dist - p.speed * age) / (1.5 * w)) ** 2)
                h[j0:j1] += 0.15 * ring
                bright[j0:j1] += ring
            # the drop, falling under gravity onto the sea
            k0, k1 = np.searchsorted(t, [T - FALL, T])
            if k1 > k0:
                u = ((t[k0:k1] - (T - FALL)) / FALL).astype(np.float32)[:, None]
                top, sea = 1.0 + w, float(self._level(np.array([T]))[0])
                dy = top - (top - sea) * u * u
                r = np.sqrt((x - dx) ** 2 + (y - dy) ** 2 + (z - dz) ** 2)
                drop[k0:k1] = np.maximum(drop[k0:k1], soft(r - 0.3 * w * s, w))
        d = np.abs(y - h)
        surface = soft(d - 0.3 * w, w)
        body = p.fill * soft(y - h, w)
        out = ((surface + body)[..., None] * self.sea
               + (np.minimum(bright, 1.0) * soft(d - 0.3 * w, 2 * w))[..., None] * self.ripple
               + drop[..., None] * self.drop)
        return np.minimum(out, 1.0).astype(np.float32)
