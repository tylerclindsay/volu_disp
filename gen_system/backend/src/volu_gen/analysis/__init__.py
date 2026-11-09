"""Audio analysis: stems, beat grid, sections, phrases, genre, mood.

Design and reasons: docs/decisions/0004-audio-analysis.md. Ported from
tools/analysis_spike/spike.py — re-check behavior changes by ear
(`python -m volu_gen.analysis <file> --check`).
"""

from .pipeline import STAGES, analyze, library_moods, run_phrases, set_override
from .track import SCHEMA_VERSION, TrackDir, default_data_dir, import_track, stored_tracks

__all__ = ["SCHEMA_VERSION", "STAGES", "TrackDir", "analyze", "default_data_dir", "import_track",
           "library_moods", "run_phrases", "set_override", "stored_tracks"]
