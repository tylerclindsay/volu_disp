"""Per-track analysis cache: `<data>/cache/<sha256[:16]>/` holds `stems/`
and `analysis.json`, keyed by audio content so renames don't re-analyze."""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import soundfile as sf

from .audio import BASE_SR, STEM_NAMES, file_sha256, load_audio

SCHEMA_VERSION = 2
# Older versions whose model outputs are still valid; only the grid-dependent
# results (phrases, events, per-bar energy) are dropped and re-derived.
# v2: per-bar drop detection, `low` energy lane.
COMPATIBLE_VERSIONS = {1}
DERIVED_KEYS = ["phrases", "events", "bars"]


def default_data_dir() -> Path:
    # backend/src/volu_gen/analysis/cache.py → repo root
    return Path(__file__).resolve().parents[5] / "data"


class TrackCache:
    def __init__(self, path: Path, data_dir: Path, sha256: str | None = None):
        self.path = Path(path)
        sha = sha256 or file_sha256(self.path)
        self.dir = Path(data_dir) / "cache" / sha[:16]
        self.dir.mkdir(parents=True, exist_ok=True)
        self.json_path = self.dir / "analysis.json"
        self.data = json.loads(self.json_path.read_text()) if self.json_path.exists() else {}
        if self.data.get("version") in COMPATIBLE_VERSIONS:
            for k in DERIVED_KEYS:
                self.data.pop(k, None)
            self.data["version"] = SCHEMA_VERSION
        elif self.data.get("version") != SCHEMA_VERSION:
            self.data = {"version": SCHEMA_VERSION}
        self.data["source"] = {**self.data.get("source", {}), "sha256": sha, "path": str(self.path)}
        self.data.setdefault("models", {})
        self._audio = None

    @property
    def name(self) -> str:
        return self.path.stem

    @property
    def audio(self) -> np.ndarray:
        """(2, n) float32 at BASE_SR, decoded once and kept."""
        if self._audio is None:
            self._audio = load_audio(self.path)
            self.data["source"]["duration"] = round(self._audio.shape[1] / BASE_SR, 3)
        return self._audio

    @property
    def duration(self) -> float:
        if "duration" not in self.data["source"]:
            _ = self.audio
        return self.data["source"]["duration"]

    def drop_audio(self):
        self._audio = None

    # -- stems
    def stem_path(self, name: str) -> Path:
        return self.dir / "stems" / f"{name}.flac"

    def has_stems(self) -> bool:
        return all(self.stem_path(n).exists() for n in STEM_NAMES)

    def save_stems(self, stems: dict[str, np.ndarray]):
        (self.dir / "stems").mkdir(exist_ok=True)
        for name, wav in stems.items():
            sf.write(self.stem_path(name), wav.T, BASE_SR)

    def stem(self, name: str) -> np.ndarray:
        a, sr = sf.read(self.stem_path(name), dtype="float32", always_2d=True)
        assert sr == BASE_SR
        return a.T

    def save(self):
        self.json_path.write_text(json.dumps(self.data, indent=1))
