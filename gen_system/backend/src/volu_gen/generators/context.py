"""`RenderContext`: what a method renders onto (docs/generators.md → Inputs)."""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np

from ..geometry import MAX_FPS, MIN_FPS, Geometry
from .music import Music
from .palette import DEFAULT_PALETTE, PALETTES, hex_to_linear

STROBE_MAX_RATE = 60.0  # flashes/s: the default and ceiling (generators.md → Smoothness)


def normalized_positions(positions_mm: np.ndarray) -> np.ndarray:
    """(P, 3) float32: centered, y up, divided by the largest half-extent so
    the aspect ratio is kept and the longest axis spans [-1, 1]."""
    p = np.asarray(positions_mm, np.float64)
    lo, hi = p.min(axis=0), p.max(axis=0)
    half = float((hi - lo).max()) / 2 or 1.0
    out = (p - (lo + hi) / 2) / half
    out[:, 1] *= -1  # config y grows downward
    return out.astype(np.float32)


def box_positions(positions_mm: np.ndarray) -> np.ndarray:
    """(P, 3) float32: y up, each axis stretched to [-1, 1] (0 for an axis
    with one pixel), so a shape drawn in it fills the volume whatever the
    display's aspect ratio."""
    p = np.asarray(positions_mm, np.float64)
    lo, hi = p.min(axis=0), p.max(axis=0)
    half = (hi - lo) / 2
    out = np.where(half > 0, (p - (lo + hi) / 2) / np.where(half > 0, half, 1.0), 0.0)
    out[:, 1] *= -1  # config y grows downward
    return out.astype(np.float32)


def nearest_spacing(points: np.ndarray) -> float:
    """Distance from the pixel nearest the center to its nearest neighbour."""
    c = int(np.argmin(np.linalg.norm(points, axis=1)))
    d = np.linalg.norm(points - points[c], axis=1)
    d = d[d > 0]
    return float(d.min()) if len(d) else 1.0


@dataclass(frozen=True, eq=False)
class RenderContext:
    """The display and the render settings, shared by every segment.

    fps      the display FPS when the render started (decision 0010)
    seed     the plan's seed (track params)
    palette  the plan's track palette, sRGB hex (empty: the default palette)
    strobe_max_rate  the track param, flashes/s; methods use `strobe_rate`
    Frame k is shown from k / fps (the engine picks floor(t × fps)), so
    `times` gives each frame's start. Methods work in seconds and never
    assume a rate.
    """
    geometry: Geometry
    music: Music
    fps: int
    seed: int = 0
    palette: tuple[str, ...] = ()
    strobe_max_rate: float = STROBE_MAX_RATE
    pos: np.ndarray = field(init=False)      # (P, 3) float32, normalized, y up
    box: np.ndarray = field(init=False)      # (P, 3) float32, each axis stretched to [-1, 1], y up
    pitch: float = field(init=False)         # nearest-neighbour spacing in `box` units
    parity: np.ndarray = field(init=False)   # (P,) int32, 1 on odd (offset) slices
    colors: np.ndarray = field(init=False)   # (n, 3) float32 linear RGB of the palette

    def __post_init__(self):
        if not (isinstance(self.fps, int) and MIN_FPS <= self.fps <= MAX_FPS):
            raise ValueError(f"fps must be a whole number from {MIN_FPS} to {MAX_FPS}, got {self.fps!r}")
        pos = normalized_positions(self.geometry.positions)
        box = box_positions(self.geometry.positions)
        parity = (self.geometry.iz % 2).astype(np.int32)
        colors = hex_to_linear(self.palette or PALETTES[DEFAULT_PALETTE][0])
        for a in (pos, box, parity, colors):
            a.setflags(write=False)
        object.__setattr__(self, "pos", pos)
        object.__setattr__(self, "box", box)
        object.__setattr__(self, "pitch", nearest_spacing(box))
        object.__setattr__(self, "parity", parity)
        object.__setattr__(self, "colors", colors)

    @property
    def strobe_rate(self) -> float:
        """The fastest a method may flash, flashes/s: the track param, at
        most STROBE_MAX_RATE and half the FPS (one on/off cycle per two frames)."""
        return min(self.strobe_max_rate, STROBE_MAX_RATE, self.fps / 2)

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
