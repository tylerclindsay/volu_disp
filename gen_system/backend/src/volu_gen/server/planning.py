"""Render planning in the server (decision 0011): the director turns a
track's analysis, features, track params and pinned instance edits into a
plan. Pure Python, no audio, no models; blocking, so callers run it off
the event loop."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from ..analysis import TrackDir
from ..generators import DIRECTOR_VERSION, METHODS, DirectorParams, Music, Plan, Segment, make_plan


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


def _music(track: TrackDir) -> Music:
    if "phrases" not in track.data:
        raise ValueError("the track's analysis is not finished")
    return Music.from_track(track, audio=False)


def plan_track(track: TrackDir, state: PlanState, mood_z: dict[str, float] | None) -> Plan:
    """Plan a track. Raises ValueError (pydantic's ValidationError
    included) for a state the director rejects, e.g. a bad override value."""
    return make_plan(_music(track), state.params, [p.segment() for p in state.pinned], mood_z)


def stale_plan(plan: dict) -> bool:
    """Made by another director version, or names a method that changed or
    is no longer registered: it must be re-planned before a render."""
    return (plan.get("director", {}).get("version") != DIRECTOR_VERSION
            or any(s["method"] not in METHODS or METHODS[s["method"]].version != s["method_version"]
                   for s in plan.get("segments", [])))


def plan_for_render(track: TrackDir, params: DirectorParams, plan: dict | None,
                    mood_z: dict[str, float] | None) -> tuple[Plan, bool]:
    """The plan a render gets (docs/architecture.md → API server): the
    stored plan with segment times from the current grid; a version with
    no plan, or a stale one, is planned first (keeping its pinned
    segments). Returns (plan, re-planned)."""
    music = _music(track)
    if plan is None:
        return make_plan(music, params, (), mood_z), True
    if stale_plan(plan):
        pinned = [Segment.model_validate(s) for s in plan["segments"] if s.get("pinned")]
        return make_plan(music, params, pinned, mood_z), True
    return Plan.model_validate(plan).retime(music), False
