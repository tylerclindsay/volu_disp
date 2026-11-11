"""Render planning in the server (decision 0011): the director turns a
track's analysis, features, track params and pinned instance edits into a
plan. Pure Python, no audio, no models; blocking, so callers run it off
the event loop."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from ..analysis import TrackDir
from ..generators import METHODS, DirectorParams, Music, Plan, Segment, make_plan


class PinnedInstance(BaseModel):
    """A user-edited method instance: the director keeps its bars, method
    and overrides, and plans the rest of the track around it."""
    model_config = ConfigDict(extra="forbid")

    start_bar: int = Field(ge=0)
    end_bar: int
    method: str
    overrides: dict[str, Any] = {}

    def segment(self) -> Segment:
        # The director re-chooses params and transition and drops pins of
        # unregistered methods, so the version here is only a placeholder.
        version = METHODS[self.method].version if self.method in METHODS else 1
        return Segment(start_bar=self.start_bar, end_bar=self.end_bar, method=self.method,
                       method_version=version, overrides=self.overrides, pinned=True)


class PlanState(BaseModel):
    """The full Studio state: track params plus every instance edit. The
    UI sends all of it on each edit (not a delta), so undo can send an
    earlier state again and, planning being deterministic, get the same plan."""
    model_config = ConfigDict(extra="forbid")

    params: DirectorParams = DirectorParams()
    pinned: list[PinnedInstance] = []


def pinned_of(plan: Plan) -> list[PinnedInstance]:
    return [PinnedInstance(start_bar=s.start_bar, end_bar=s.end_bar, method=s.method, overrides=s.overrides)
            for s in plan.segments if s.pinned]


def plan_track(track: TrackDir, state: PlanState, mood_z: dict[str, float] | None) -> Plan:
    """Plan a track. Raises ValueError (pydantic's ValidationError
    included) for a state the director rejects, e.g. a bad override value."""
    if "phrases" not in track.data:
        raise ValueError("the track's analysis is not finished")
    music = Music.from_track(track, audio=False)
    return make_plan(music, state.params, [p.segment() for p in state.pinned], mood_z)
