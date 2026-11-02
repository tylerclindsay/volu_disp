"""Audio analysis: stems, beat grid, sections, phrases, genre, mood.

Design and reasons: docs/decisions/0004-audio-analysis.md. Ported from
tools/analysis_spike/spike.py — re-check behavior changes by ear
(`python -m volu_gen.analysis <file> --check`).
"""

from .cache import SCHEMA_VERSION, TrackCache, default_data_dir
from .pipeline import STAGES, analyze, library_moods, run_phrases, set_override

__all__ = ["SCHEMA_VERSION", "STAGES", "TrackCache", "analyze", "default_data_dir",
           "library_moods", "run_phrases", "set_override"]
