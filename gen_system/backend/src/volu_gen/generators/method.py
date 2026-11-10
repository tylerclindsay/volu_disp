"""The method interface and registry (docs/generators.md → Methods).

A method is one way of animating the display. It declares what it suits,
picks its own params for a segment, and renders any frame range of that
segment as a closed-form function of time and cheap per-segment data
precomputed in `__init__`; nothing is carried from frame to frame.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import ClassVar

import numpy as np
from pydantic import BaseModel, ConfigDict

from .context import RenderContext
from .music import Music

GROOVE = "groove"  # the phrase type of an untagged phrase
PHRASE_TYPES = frozenset({"drop", "buildup", "breakdown", GROOVE})

# RNG streams per segment, so planning and rendering draw independently.
PLAN_STREAM = 0
RENDER_STREAM = 1
TRACK_STREAM = 2   # track-wide draws (the palette), at bar 0


def segment_rng(seed: int, start_bar: int, stream: int) -> np.random.Generator:
    """The RNG of one segment: seeded from (seed, segment start bar, stream),
    so re-planning or editing one segment doesn't reshuffle the others."""
    if seed < 0 or start_bar < 0:
        raise ValueError("seed and start_bar must be >= 0")
    return np.random.default_rng(np.random.SeedSequence([seed, start_bar, stream]))


@dataclass(frozen=True)
class SegmentInfo:
    """What the director and a method know about one segment.

    Bars are `analysis.json` bar numbers (bar 1 at the first downbeat); the
    segment covers bars start_bar … end_bar − 1. Times are seconds on the
    current grid, clipped to the track.
    tags       phrase types of the phrases it overlaps (`groove` if untagged)
    energy_db  mean per-bar mix energy, dB re. the loud level
    """
    start_bar: int
    end_bar: int
    start: float
    end: float
    tags: frozenset[str] = frozenset()
    energy_db: float = 0.0

    @property
    def bars(self) -> int:
        return self.end_bar - self.start_bar

    @property
    def duration(self) -> float:
        return self.end - self.start

    @classmethod
    def from_bars(cls, music: Music, start_bar: int, end_bar: int) -> SegmentInfo:
        if end_bar <= start_bar:
            raise ValueError(f"empty segment: bars {start_bar} to {end_bar}")
        t0, t1 = (float(np.clip(music.bar_time(b), 0.0, music.duration)) for b in (start_bar, end_bar))
        tags = set()
        for p in music.phrases:
            if p["start_bar"] < end_bar and p["start_bar"] + p["bars"] > start_bar:
                tags.update(p["tags"] or [GROOVE])
        e = music.bar_energy_db("mix", start_bar, end_bar) if "mix" in music.energy_lanes else []
        return cls(start_bar, end_bar, t0, t1, frozenset(tags), float(np.mean(e)) if len(e) else 0.0)


@dataclass(frozen=True)
class Suitability:
    """What a method suits, for the director's choice.

    phrase_types  phrase types it fits (PHRASE_TYPES)
    energy_db     (low, high) per-bar mix energy range it fits
    min_bars      shortest segment it reads well in
    """
    phrase_types: frozenset[str] = PHRASE_TYPES
    energy_db: tuple[float, float] = (-60.0, 6.0)
    min_bars: int = 1

    def __post_init__(self):
        unknown = self.phrase_types - PHRASE_TYPES
        if unknown:
            raise ValueError(f"unknown phrase types {sorted(unknown)}")


class MethodParams(BaseModel):
    """Base of every method's `Params`: plain JSON values, unknown fields
    rejected, so a typo in an override fails instead of being ignored."""
    model_config = ConfigDict(extra="forbid", frozen=True)


class Method(ABC):
    """One way of animating the display. Subclasses set the class vars and
    implement `render`; `__init__` may precompute per-segment data (spawn
    lists, a star field) from `self.rng`."""
    id: ClassVar[str]                      # stable, e.g. "raindrops"
    version: ClassVar[int]                 # bump when output changes for the same params
    Params: ClassVar[type[MethodParams]]
    suits: ClassVar[Suitability] = Suitability()

    @classmethod
    def choose_params(cls, seg: SegmentInfo, music: Music, rng: np.random.Generator) -> MethodParams:
        """Base params for a segment, from what the analysis knows about it.
        Runs in the API server: no audio, no heavy imports."""
        return cls.Params()

    def __init__(self, ctx: RenderContext, seg: SegmentInfo, params: MethodParams):
        self.ctx = ctx
        self.seg = seg
        self.params = params
        self.rng = segment_rng(ctx.seed, max(seg.start_bar, 0), RENDER_STREAM)

    @abstractmethod
    def render(self, start: int, stop: int) -> np.ndarray:
        """Frames start … stop − 1 as float32 linear RGB in [0, 1], shape
        (stop − start, P, 3). Any range within the segment (and a little
        beyond it, for transitions) must render the same frames."""


METHODS: dict[str, type[Method]] = {}


def register(cls: type[Method]) -> type[Method]:
    """Class decorator: add a method to METHODS under its id."""
    if not getattr(cls, "id", None):
        raise TypeError(f"{cls.__name__}: no id")
    if cls.id in METHODS and METHODS[cls.id] is not cls:
        raise ValueError(f"method id {cls.id!r} already registered")
    if not isinstance(getattr(cls, "version", None), int) or cls.version < 1:
        raise TypeError(f"{cls.id}: version must be an int >= 1")
    if not (isinstance(getattr(cls, "Params", None), type) and issubclass(cls.Params, MethodParams)):
        raise TypeError(f"{cls.id}: Params must subclass MethodParams")
    METHODS[cls.id] = cls
    return cls


def get_method(method_id: str) -> type[Method]:
    try:
        return METHODS[method_id]
    except KeyError:
        raise KeyError(f"unknown method {method_id!r}") from None
