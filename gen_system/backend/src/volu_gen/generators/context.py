"""`RenderContext`: what a method renders onto (docs/generators.md → Inputs)."""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np

from ..geometry import MAX_FPS, MIN_FPS, Geometry
from .music import Music


def normalized_positions(positions_mm: np.ndarray) -> np.ndarray:
    """(P, 3) float32: centered, y up, divided by the largest half-extent so
    the aspect ratio is kept and the longest axis spans [-1, 1]."""
    p = np.asarray(positions_mm, np.float64)
    lo, hi = p.min(axis=0), p.max(axis=0)
    half = float((hi - lo).max()) / 2 or 1.0
    out = (p - (lo + hi) / 2) / half
    out[:, 1] *= -1  # config y grows downward
    return out.astype(np.float32)


@dataclass(frozen=True, eq=False)
class RenderContext:
    """The display and the render settings, shared by every segment.

    fps     the display FPS when the render started (decision 0010)
    seed    the plan's seed (track params)
    Frame k is shown from k / fps (the engine picks floor(t × fps)), so
    `times` gives each frame's start. Methods work in seconds and never
    assume a rate.
    """
    geometry: Geometry
    music: Music
    fps: int
    seed: int = 0
    pos: np.ndarray = field(init=False)      # (P, 3) float32, normalized, y up
    parity: np.ndarray = field(init=False)   # (P,) int32, 1 on odd (offset) slices

    def __post_init__(self):
        if not (isinstance(self.fps, int) and MIN_FPS <= self.fps <= MAX_FPS):
            raise ValueError(f"fps must be a whole number from {MIN_FPS} to {MAX_FPS}, got {self.fps!r}")
        pos = normalized_positions(self.geometry.positions)
        parity = (self.geometry.iz % 2).astype(np.int32)
        for a in (pos, parity):
            a.setflags(write=False)
        object.__setattr__(self, "pos", pos)
        object.__setattr__(self, "parity", parity)

    @property
    def pos_mm(self) -> np.ndarray:
        """(P, 3) float64 mm in the config's axes (y down)."""
        return self.geometry.positions

    @property
    def ix(self) -> np.ndarray:
        return self.geometry.ix

    @property
    def iy(self) -> np.ndarray:
        return self.geometry.iy

    @property
    def iz(self) -> np.ndarray:
        return self.geometry.iz

    @property
    def pixel_count(self) -> int:
        return self.geometry.pixel_count

    @property
    def frame_count(self) -> int:
        """Frames covering the whole track: ceil(duration × fps)."""
        return math.ceil(self.music.duration * self.fps - 1e-9)

    def times(self, start: int, stop: int) -> np.ndarray:
        """Start time of frames start … stop − 1, seconds."""
        return np.arange(start, stop, dtype=np.float64) / self.fps

    def frame_at(self, t: float) -> int:
        """The frame shown at time t."""
        return math.floor(t * self.fps + 1e-9)
