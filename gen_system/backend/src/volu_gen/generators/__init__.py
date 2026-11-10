"""Generators: the director, the plan and methods (docs/generators.md).

Imported by the API server for planning, so nothing here imports torch or
other heavy modules at module level.
"""

from .context import RenderContext, normalized_positions
from .director import DIRECTOR_VERSION, DirectorParams, make_plan, replan
from .method import (GROOVE, METHODS, PHRASE_TYPES, PLAN_STREAM, RENDER_STREAM, TRACK_STREAM, Method,
                     MethodParams, SegmentInfo, Suitability, get_method, register, segment_rng)
from .music import SAMPLE_LANES, Music
from .palette import PALETTES, hex_to_linear
from .plan import DirectorInfo, Plan, Segment, Transition

__all__ = ["DIRECTOR_VERSION", "DirectorInfo", "DirectorParams", "GROOVE", "METHODS", "Method",
           "MethodParams", "Music", "PALETTES", "PHRASE_TYPES", "PLAN_STREAM", "Plan", "RENDER_STREAM",
           "RenderContext", "SAMPLE_LANES", "Segment", "SegmentInfo", "Suitability", "TRACK_STREAM",
           "Transition", "get_method", "hex_to_linear", "make_plan", "normalized_positions",
           "register", "replan", "segment_rng"]
