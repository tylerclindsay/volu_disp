"""Generators: the director, the plan and methods (docs/generators.md).

Imported by the API server for planning, so nothing here imports torch or
other heavy modules at module level.
"""

from .context import RenderContext, normalized_positions
from .method import (GROOVE, METHODS, PHRASE_TYPES, PLAN_STREAM, RENDER_STREAM, Method,
                     MethodParams, SegmentInfo, Suitability, get_method, register, segment_rng)
from .music import SAMPLE_LANES, Music
from .plan import DirectorInfo, Plan, Segment, Transition

__all__ = ["DirectorInfo", "GROOVE", "METHODS", "Method", "MethodParams", "Music", "PHRASE_TYPES",
           "PLAN_STREAM", "Plan", "RENDER_STREAM", "RenderContext", "SAMPLE_LANES", "Segment",
           "SegmentInfo", "Suitability", "Transition", "get_method", "normalized_positions",
           "register", "segment_rng"]
