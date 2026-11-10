"""Analysis pipeline (decision 0004): stems → beats/grid → sections → genre
→ mood → phrases, results stored per track as analysis.json; then waveform
peaks (decision 0006) as peaks.npz and stem features (decision 0012) as
features.npz.

Runs one stage at a time across all tracks (load model, process every
track, unload), so peak VRAM is one model's worth. Finished stages are
skipped unless forced; phrases are re-run whenever an earlier stage ran,
peaks and features whenever the stems are newer than them.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import numpy as np

from . import grid as grid_mod
from .audio import STEM_NAMES, free_gpu
from .track import TRACKS_DIR, TrackDir
from .features import FEATURES_VERSION, compute_features, features_version, write_features
from .mood import library_z
from .peaks import BLOCK, LANES, PEAKS_VERSION, chunks_of, compute_peaks, peaks_version, write_peaks
from .phrases import bar_energy, compute_phrases

STAGES = ["stems", "beats", "sections", "genre", "mood", "phrases", "peaks", "features"]
CPU_STAGES = {"phrases", "peaks", "features"}  # no model; run after the model stages


def _needs(t: TrackDir, stage: str, force: set[str]) -> bool:
    from .models import MODEL_IDS
    if stage in force:
        return True
    if stage == "stems":
        return not t.has_stems() or t.data["models"].get("stems") != MODEL_IDS["stems"]
    key = {"beats": "tempo"}.get(stage, stage)
    return key not in t.data or t.data["models"].get(stage) != MODEL_IDS[stage]


def analyze(tracks: list[TrackDir], stages: list[str] = STAGES, force: set[str] = frozenset(),
            device: str = "cuda", log=print) -> dict:
    """Run `stages` on `tracks`. Returns per-track, per-stage timing."""
    from .models import MODEL_IDS, STAGE_MODELS
    stats: dict = {}
    changed: set[int] = set()  # tracks whose grid-dependent results are stale
    for stage in [s for s in STAGES if s in stages and s not in CPU_STAGES]:
        todo = [t for t in tracks if _needs(t, stage, force)]
        if not todo:
            continue
        log(f"[{stage}] {len(todo)} track(s)")
        model = STAGE_MODELS[stage](device)
        for t in todo:
            log(f"  {t.name}")
            with _timed(stats, t, stage, log):
                _run_stage(stage, model, t)
                t.data["models"][stage] = MODEL_IDS[stage]
                changed.add(id(t))
            t.save()
            t.drop_audio()
        del model
        free_gpu()
    if "phrases" in stages:
        todo = [t for t in tracks if id(t) in changed or _needs_phrases(t, force)]
        if todo:
            log(f"[phrases] {len(todo)} track(s)")
        for t in todo:
            if "tempo" not in t.data or not t.has_stems():
                log(f"  {t.name}: needs stems + beats, skipped")
                continue
            with _timed(stats, t, "phrases", log):
                run_phrases(t)
            t.save()
            t.drop_audio()
    for stage, needs, run in (("peaks", _needs_peaks, run_peaks),
                              ("features", _needs_features, run_features)):
        if stage not in stages:
            continue
        todo = [t for t in tracks if needs(t, force)]
        if todo:
            log(f"[{stage}] {len(todo)} track(s)")
        for t in todo:
            if not t.has_stems():
                log(f"  {t.name}: needs stems, skipped")
                continue
            with _timed(stats, t, stage, log):
                run(t)
            t.save()  # duration may be new
            t.drop_audio()
    return stats


def _needs_phrases(t: TrackDir, force) -> bool:
    return "phrases" in force or "phrases" not in t.data


def _stale_file(t: TrackDir, path: Path, version: int | None, current: int, stage: str, force) -> bool:
    """Missing, another format version, or older than any stem file."""
    if stage in force or version != current:
        return True
    return t.has_stems() and any(t.stem_path(n).stat().st_mtime > path.stat().st_mtime for n in STEM_NAMES)


def _needs_peaks(t: TrackDir, force) -> bool:
    return _stale_file(t, t.peaks_path, peaks_version(t.peaks_path), PEAKS_VERSION, "peaks", force)


def _needs_features(t: TrackDir, force) -> bool:
    return _stale_file(t, t.features_path, features_version(t.features_path), FEATURES_VERSION,
                       "features", force)


def _run_stage(stage: str, model, t: TrackDir):
    if stage == "stems":
        t.save_stems(model(t.audio))
    elif stage == "beats":
        b = model(t.audio)
        fit = grid_mod.fit_grid(b["raw_beats"], b["beat_prob"], b["down_prob"], t.duration)
        old = t.data.get("tempo") or {}
        t.data["tempo"] = grid_mod.build_tempo(fit, b["raw_beats"], b["raw_downbeats"],
                                               t.duration, old.get("override"))
    elif stage == "sections":
        t.data["sections"] = model(t.audio)
    elif stage in ("genre", "mood"):
        t.data[stage] = model(t.audio)


def run_phrases(t: TrackDir):
    """Re-derive everything that depends on the beat grid (CPU only)."""
    downbeats = np.array(t.data["tempo"]["downbeats"])
    energy = bar_energy(t.audio, {n: t.stem(n) for n in STEM_NAMES}, downbeats, t.duration)
    sections = [{"start": s["start"], "end": s["end"]} for s in t.data.get("sections", [])]
    t.data.update(compute_phrases(energy, downbeats, t.duration, sections))


def run_peaks(t: TrackDir):
    """Waveform peaks of the mix and each stem (CPU only); stems are streamed."""
    mix = t.audio
    lanes = {"mix": chunks_of(mix), **{n: t.stem_blocks(n, BLOCK) for n in LANES if n != "mix"}}
    write_peaks(t.peaks_path, compute_peaks(lanes, mix.shape[1]))


def run_features(t: TrackDir):
    """Stem features for generators (CPU only); one stem in memory at a time."""
    write_features(t.features_path, compute_features(
        lambda lane: t.audio if lane == "mix" else t.stem(lane), t.audio.shape[1]))


def set_override(t: TrackDir, bpm: float | None, offset: float | None):
    """Replace the fitted grid with a user one (BPM + time of a downbeat), or
    clear it with bpm=None. Re-runs only the grid-dependent steps."""
    tempo = t.data["tempo"]
    override = {"bpm": float(bpm), "offset": float(offset)} if bpm is not None else None
    t.data["tempo"] = grid_mod.build_tempo(tempo["fit"], tempo["raw"]["beats"],
                                           tempo["raw"]["downbeats"], t.duration, override)
    if t.has_stems():
        run_phrases(t)
    t.save()


def library_moods(data_dir: Path) -> dict[str, dict[str, float]]:
    """Mood z-scores for every analyzed track in the data dir, keyed by sha256."""
    cos = {}
    for p in (Path(data_dir) / TRACKS_DIR).glob("*/analysis.json"):
        d = json.loads(p.read_text())
        if "mood" in d:
            cos[d["source"]["sha256"]] = d["mood"]["cos"]
    return library_z(cos)


class _timed:
    """Times one stage on one track; records peak VRAM and errors."""

    def __init__(self, stats: dict, t: TrackDir, stage: str, log=print):
        self.stats, self.t, self.stage, self.log = stats, t, stage, log

    def __enter__(self):
        import torch
        if torch.cuda.is_available():
            torch.cuda.synchronize()
            torch.cuda.reset_peak_memory_stats()
        self.t0 = time.perf_counter()

    def __exit__(self, exc_type, exc, tb):
        import torch
        entry = {"seconds": round(time.perf_counter() - self.t0, 2)}
        if torch.cuda.is_available():
            torch.cuda.synchronize()
            entry["peak_vram_gb"] = round(torch.cuda.max_memory_reserved() / 1e9, 2)
        if exc is not None:
            entry["error"] = f"{exc_type.__name__}: {exc}"
            self.log(f"    ! {self.stage} failed: {entry['error']}")
        self.stats.setdefault(self.t.name, {})[self.stage] = entry
        return exc_type is not None and issubclass(exc_type, Exception)  # log, go on to the next track
