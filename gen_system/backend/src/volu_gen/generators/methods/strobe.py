"""`strobe`: flashes on the beat grid, the whole display or alternating
slices (docs/generators.md → Methods, Smoothness).

Flash k starts at bar 1 + k / per_bar and lasts `duty` of the flash
period, but at least one frame (1 / fps), so every flash lands on at least
one frame at any FPS and none is missed. The flash rate is halved until it
is at most `ctx.strobe_rate` (60 flashes/s, half the FPS, or the track
param), so flashes stay on the grid. Brightness follows the drums'
envelope by `follow`, so the strobe dies with the drums.
"""

from __future__ import annotations

from typing import Literal

import numpy as np
from pydantic import Field

from ..method import Method, MethodParams, SegmentInfo, Suitability, register
from ..music import Music
from .common import Signal, color, tint


class StrobeParams(MethodParams):
    """per_beat  flashes per beat (0.5: every other beat)
    pattern   all: the whole display; slices: odd and even slices take
              turns; chase: one slice per flash, front to back
    duty      on-time as a fraction of the flash period
    color     palette index; white: how far toward white (0–1)
    follow    how much brightness follows the drums (0: always full)"""
    per_beat: Literal[0.5, 1, 2, 4] = 1
    pattern: Literal["all", "slices", "chase"] = "all"
    duty: float = Field(0.3, gt=0.0, le=0.9)
    color: int = Field(0, ge=0)
    white: float = Field(0.7, ge=0.0, le=1.0)
    follow: float = Field(0.5, ge=0.0, le=1.0)


@register
class Strobe(Method):
    id = "strobe"
    version = 1
    Params = StrobeParams
    suits = Suitability(phrase_types=frozenset({"drop"}), energy_db=(-6.0, 6.0))

    @classmethod
    def choose_params(cls, seg: SegmentInfo, music: Music, rng: np.random.Generator) -> StrobeParams:
        fast = (music.bpm or 120.0) < 135 and seg.energy_db > -3
        return StrobeParams(per_beat=[1, 2][int(fast and rng.random() < 0.5)],
                            pattern=["all", "slices", "chase"][int(rng.integers(3))],
                            duty=float(rng.uniform(0.2, 0.4)), color=int(rng.integers(8)),
                            white=float(rng.uniform(0.4, 0.9)))

    def __init__(self, ctx, seg, params: StrobeParams):
        super().__init__(ctx, seg, params)
        p = params
        self.drums = Signal.envelope(ctx.music, "drums", seg, tau=0.15) if p.follow > 0 else None
        self.rgb = tint(color(ctx, p.color), p.white)
        Z = ctx.geometry.size[2]
        if p.pattern == "all":
            self.groups, self.group = 1, np.zeros(ctx.pixel_count, np.int64)
        elif p.pattern == "slices":
            self.groups, self.group = 2, ctx.parity.astype(np.int64)
        else:
            self.groups, self.group = Z, ctx.iz.astype(np.int64)

    def render(self, start: int, stop: int) -> np.ndarray:
        ctx, p, music = self.ctx, self.params, self.ctx.music
        t = ctx.times(start, stop)
        per_bar = p.per_beat * music.meter * np.ones(len(t))
        bar_len = music.beat_length(t) * music.meter
        while (fast := per_bar / bar_len > ctx.strobe_rate).any():   # halve until within the limit
            per_bar = np.where(fast, per_bar / 2, per_bar)
        x = (music.bar_at(t) - 1) * per_bar
        k = np.floor(x + 1e-9)
        since = t - music.bar_time(1 + k / per_bar)
        on_time = np.maximum(p.duty * bar_len / per_bar, 1.0 / ctx.fps)
        on = since < on_time - 1e-9
        level = np.ones(len(t), np.float32)
        if self.drums is not None:
            level = 1 - p.follow + p.follow * np.clip(self.drums(t), 0.0, 1.0)
        lit = (self.group[None, :] == np.mod(k, self.groups).astype(np.int64)[:, None]) & on[:, None]
        v = np.where(lit, level[:, None], 0.0).astype(np.float32)
        return v[..., None] * self.rgb
