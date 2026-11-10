"""Per-track data directory: `<data>/tracks/<sha256[:16]>/` holds the source
audio (copied in on import), `stems/`, `analysis.json` and `peaks.npz`,
keyed by the source file's content. This is stored track data, not a
disposable cache: it holds the user's grid override and is costly to
rebuild."""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from pathlib import Path

import numpy as np
import soundfile as sf

from .audio import BASE_SR, STEM_NAMES, load_audio

TRACKS_DIR = "tracks"  # under the data dir
SOURCE_STEM = "source"  # the imported audio is `source<ext>`, e.g. source.flac
SCHEMA_VERSION = 2
# Older versions whose model outputs are still valid; only the grid-dependent
# results (phrases, events, per-bar energy) are dropped and re-derived.
# v2: per-bar drop detection, `low` energy lane.
COMPATIBLE_VERSIONS = {1}
DERIVED_KEYS = ["phrases", "events", "bars"]


def tmp_path_for(path: Path) -> Path:
    """Where a file is written before its atomic rename: <name>.tmp, so an
    interrupted write never leaves a partial file under the real name."""
    return path.with_name(path.name + ".tmp")


def default_data_dir() -> Path:
    # backend/src/volu_gen/analysis/track.py → repo root
    return Path(__file__).resolve().parents[5] / "data"


def import_track(path: Path, data_dir: Path) -> TrackDir:
    """Copy an audio file into its track dir and return the track. The file is
    read once, hashed while copying; importing the same bytes again returns
    the existing track without writing to it."""
    path = Path(path)
    root = Path(data_dir) / TRACKS_DIR
    root.mkdir(parents=True, exist_ok=True)
    tmp = root / f".import-{uuid.uuid4().hex}.tmp"
    h = hashlib.sha256()
    try:
        with open(path, "rb") as f, open(tmp, "wb") as out:
            for block in iter(lambda: f.read(1 << 20), b""):
                h.update(block)
                out.write(block)
        sha = h.hexdigest()
        d = root / sha[:16]
        d.mkdir(exist_ok=True)
        if not any(d.glob(SOURCE_STEM + ".*")):
            os.replace(tmp, d / (SOURCE_STEM + path.suffix.lower()))
    finally:
        tmp.unlink(missing_ok=True)
    t = TrackDir(d)
    src = t.data["source"]
    before = dict(src)
    src["sha256"] = sha
    src.setdefault("original_path", str(path))
    src.setdefault("name", path.stem)
    if src != before or not t.json_path.exists():
        t.save()
    return t


def stored_tracks(data_dir: Path) -> list[TrackDir]:
    """Every imported track in the data dir."""
    root = Path(data_dir) / TRACKS_DIR
    if not root.is_dir():
        return []
    return [TrackDir(d) for d in sorted(root.iterdir()) if d.is_dir() and any(d.glob(SOURCE_STEM + ".*"))]


class TrackDir:
    """An imported track's directory (see `import_track`)."""

    def __init__(self, path: Path):
        self.dir = Path(path)
        self.json_path = self.dir / "analysis.json"
        self.data = json.loads(self.json_path.read_text()) if self.json_path.exists() else {}
        if self.data.get("version") in COMPATIBLE_VERSIONS:
            for k in DERIVED_KEYS:
                self.data.pop(k, None)
            self.data["version"] = SCHEMA_VERSION
        elif self.data.get("version") != SCHEMA_VERSION:
            self.data = {"version": SCHEMA_VERSION, "source": self.data.get("source", {})}
        src = self.data.setdefault("source", {})
        if "path" in src:  # from before copy-on-import, when the file was referenced in place
            src.setdefault("original_path", src.pop("path"))
        self.data.setdefault("models", {})
        self._audio = None

    @property
    def name(self) -> str:
        return self.data["source"].get("name", self.dir.name)

    @property
    def source_path(self) -> Path:
        found = sorted(self.dir.glob(SOURCE_STEM + ".*"))
        if not found:
            raise FileNotFoundError(f"{self.dir}: no {SOURCE_STEM}.* audio (track not imported)")
        return found[0]

    @property
    def audio(self) -> np.ndarray:
        """(2, n) float32 at BASE_SR, decoded once and kept."""
        if self._audio is None:
            self._audio = load_audio(self.source_path)
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
            path = self.stem_path(name)
            tmp = tmp_path_for(path)
            sf.write(tmp, wav.T, BASE_SR, format="FLAC")
            os.replace(tmp, path)

    def stem(self, name: str) -> np.ndarray:
        a, sr = sf.read(self.stem_path(name), dtype="float32", always_2d=True)
        assert sr == BASE_SR
        return a.T

    def stem_blocks(self, name: str, blocksize: int):
        """The stem as (2, blocksize) float32 chunks, without loading it all."""
        for b in sf.blocks(self.stem_path(name), blocksize=blocksize, dtype="float32", always_2d=True):
            yield b.T

    @property
    def peaks_path(self) -> Path:
        return self.dir / "peaks.npz"

    def save(self):
        tmp = tmp_path_for(self.json_path)
        tmp.write_text(json.dumps(self.data, indent=1))
        os.replace(tmp, self.json_path)
