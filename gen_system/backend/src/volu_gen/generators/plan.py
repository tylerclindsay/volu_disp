"""The plan: the director's output and the render job's input
(docs/generators.md → Plan). Plain data, stored as JSON in the version's
DB row and in the `.anim` metadata.

Segments are anchored by bar numbers; `start`/`end` are recomputed from the
current grid (`retime`) before every render, so a stored plan never renders
on an old grid. Plans don't depend on the display FPS.
"""

from __future__ import annotations

from typing import Any, Literal

import numpy as np
from pydantic import BaseModel, ConfigDict, Field, model_validator

from .method import MethodParams, SegmentInfo, get_method
from .music import Music
from .palette import HexColor


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Transition(_Model):
    """How a segment comes in. `cut`: hard cut on its first downbeat.
    `crossfade`: from the previous segment over `beats` beats, centered on
    that downbeat (decision 0018)."""
    kind: Literal["cut", "crossfade"] = "cut"
    beats: float = Field(0.0, ge=0.0)

    @model_validator(mode="after")
    def _beats(self):
        if (self.kind == "crossfade") != (self.beats > 0):
            raise ValueError("a crossfade needs beats > 0; a cut has none")
        return self


class Segment(_Model):
    """One instance of a method over bars start_bar … end_bar − 1.

    params     what the method's choose_params picked
    overrides  user edits (Studio): only the fields changed; they win over params
    pinned     kept as is by re-planning (set by any instance edit)
    """
    start_bar: int = Field(ge=0)
    end_bar: int
    start: float = 0.0
    end: float = 0.0
    method: str
    method_version: int = Field(ge=1)
    params: dict[str, Any] = {}
    overrides: dict[str, Any] = {}
    pinned: bool = False
    transition: Transition = Transition()

    @model_validator(mode="after")
    def _bars(self):
        if self.end_bar <= self.start_bar:
            raise ValueError(f"segment bars {self.start_bar} to {self.end_bar} are empty")
        return self

    def method_params(self) -> MethodParams:
        """The params to render with: params, then overrides, validated by
        the method's Params model."""
        return get_method(self.method).Params.model_validate({**self.params, **self.overrides})

    def info(self, music: Music) -> SegmentInfo:
        return SegmentInfo.from_bars(music, self.start_bar, self.end_bar)


class DirectorInfo(_Model):
    version: int = Field(ge=1)
    params: dict[str, Any] = {}   # track params


class Plan(_Model):
    director: DirectorInfo
    palette: list[HexColor] = []   # the track palette, sRGB "#rrggbb"
    segments: list[Segment]

    @model_validator(mode="after")
    def _contiguous(self):
        if not self.segments:
            raise ValueError("a plan needs at least one segment")
        for a, b in zip(self.segments, self.segments[1:]):
            if b.start_bar != a.end_bar:
                raise ValueError(f"segments must be contiguous: bar {a.end_bar} then {b.start_bar}")
        return self

    def retime(self, music: Music) -> Plan:
        """A copy with start/end from the current grid, clipped to the
        track. The first segment also covers any lead-in before its bar and
        the last runs to the end of the track, so the plan covers [0, duration]."""
        bars = np.array([s.start_bar for s in self.segments] + [self.segments[-1].end_bar])
        t = np.clip(music.bar_time(bars), 0.0, music.duration)
        t[0], t[-1] = 0.0, music.duration
        segs = [s.model_copy(update={"start": float(t[i]), "end": float(t[i + 1])})
                for i, s in enumerate(self.segments)]
        return self.model_copy(update={"segments": segs})

    def segment_index(self, t: float) -> int:
        """Index of the segment playing at time t (on the plan's times)."""
        starts = [s.start for s in self.segments]
        return max(int(np.searchsorted(starts, t, side="right")) - 1, 0)

    def stale_methods(self) -> list[int]:
        """Indices of segments whose method version differs from the
        registered one (the plan must be re-planned before rendering)."""
        return [i for i, s in enumerate(self.segments)
                if get_method(s.method).version != s.method_version]
