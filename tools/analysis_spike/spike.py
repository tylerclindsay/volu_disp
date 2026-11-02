"""Analysis spike: try beat/downbeat, section, genre, and mood models on real
tracks before committing to them in gen_system's architecture.

Runs one stage at a time across all tracks (load model, process every track,
unload), so peak VRAM is one model's worth. Writes per-track results and
listening aids to --out, plus summary.md with timings and peak VRAM.

    .venv\\Scripts\\python spike.py <audio files or folders> [--out DIR]

See README.md for setup and how to check results by ear.
"""

from __future__ import annotations

import argparse
import gc
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import numpy as np
import soundfile as sf
import soxr
import torch

AUDIO_EXTS = {".wav", ".flac", ".mp3", ".m4a", ".aac", ".ogg", ".opus", ".aiff", ".aif"}
STAGES = ["stems", "beats", "sections", "genre", "mood", "phrases"]
STEM_NAMES = ["drums", "bass", "other", "vocals"]
BASE_SR = 44100

# Mood descriptors scored with CLAP, as "<word> music".
MOOD_WORDS = [
    "energetic", "calm", "dark", "euphoric", "aggressive", "dreamy",
    "melancholic", "uplifting", "tense", "playful", "hypnotic", "atmospheric",
]

# Phrase/event rules (dB relative to the stem's loud level; tune by ear).
PHRASE_BARS = 8
STEM_ON_DB = -18.0       # a stem counts as "on" in a bar above this level
MIN_STATE_BARS = 2       # on/off changes must hold this long to count
DROP_JUMP_DB = 4.0       # mix energy jump into a phrase that marks a drop
BUILD_SLOPE_DB = 0.35    # dB/bar rise across a phrase that marks a build-up
BREAKDOWN_DRUMS = 0.25   # drums on in < this fraction of bars → breakdown


# ---------------------------------------------------------------- utilities

def find_tracks(inputs: list[str]) -> list[Path]:
    tracks = []
    for item in inputs:
        p = Path(item)
        if p.is_dir():
            tracks += sorted(f for f in p.rglob("*") if f.suffix.lower() in AUDIO_EXTS)
        elif p.is_file():
            tracks.append(p)
        else:
            sys.exit(f"not found: {item}")
    return tracks


def slug(path: Path) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", path.stem).strip("_")[:80]


def load_audio(path: Path) -> np.ndarray:
    """Decode any format with ffmpeg → float32 (2, n) at BASE_SR."""
    cmd = ["ffmpeg", "-v", "error", "-i", str(path), "-f", "f32le",
           "-ac", "2", "-ar", str(BASE_SR), "-"]
    raw = subprocess.run(cmd, capture_output=True, check=True).stdout
    return np.frombuffer(raw, dtype=np.float32).reshape(-1, 2).T.copy()


def mono(audio: np.ndarray, sr: int) -> np.ndarray:
    m = audio.mean(axis=0)
    return m if sr == BASE_SR else soxr.resample(m, BASE_SR, sr).astype(np.float32)


def free_gpu():
    gc.collect()
    if torch.cuda.is_available():
        torch.cuda.empty_cache()


class Track:
    def __init__(self, path: Path, out_root: Path):
        self.path = path
        self.dir = out_root / slug(path)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.json_path = self.dir / "analysis.json"
        self.data = json.loads(self.json_path.read_text()) if self.json_path.exists() else {}
        self.data["source"] = str(path)
        self._audio = None

    @property
    def audio(self) -> np.ndarray:
        if self._audio is None:
            self._audio = load_audio(self.path)
            self.data["duration"] = self._audio.shape[1] / BASE_SR
        return self._audio

    def stem(self, name: str) -> np.ndarray:
        a, sr = sf.read(self.dir / "stems" / f"{name}.flac", dtype="float32", always_2d=True)
        assert sr == BASE_SR
        return a.T

    def save(self):
        self.json_path.write_text(json.dumps(self.data, indent=1))


class StageRun:
    """Times one stage on one track and records peak VRAM."""

    def __init__(self, track: Track, stage: str, stats: dict):
        self.track, self.stage, self.stats = track, stage, stats

    def __enter__(self):
        if torch.cuda.is_available():
            torch.cuda.synchronize()
            torch.cuda.reset_peak_memory_stats()
        self.t0 = time.perf_counter()

    def __exit__(self, exc_type, exc, tb):
        if torch.cuda.is_available():
            torch.cuda.synchronize()
        entry = {"seconds": round(time.perf_counter() - self.t0, 2)}
        if torch.cuda.is_available():
            entry["peak_vram_gb"] = round(torch.cuda.max_memory_reserved() / 1e9, 2)
        if exc is not None:
            entry["error"] = f"{exc_type.__name__}: {exc}"
            print(f"    ! {self.stage} failed: {entry['error']}")
        self.stats.setdefault(self.track.dir.name, {})[self.stage] = entry
        self.track.save()
        return exc is not None  # log the failure and carry on with the next track


def write_labels(path: Path, rows: list[tuple[float, float, str]]):
    """Audacity label track: start<TAB>end<TAB>label."""
    path.write_text("".join(f"{s:.3f}\t{e:.3f}\t{l}\n" for s, e, l in rows))


# ------------------------------------------------------------------- stages

def stage_stems(tracks, device, force, stats):
    from demucs.api import Separator
    sep = Separator(model="htdemucs", device=device)
    for t in tracks:
        stem_dir = t.dir / "stems"
        if not force and all((stem_dir / f"{n}.flac").exists() for n in STEM_NAMES):
            print(f"  {t.dir.name}: stems cached")
            continue
        print(f"  {t.dir.name}")
        with StageRun(t, "stems", stats):
            _, stems = sep.separate_tensor(torch.from_numpy(t.audio), BASE_SR)
            stem_dir.mkdir(exist_ok=True)
            for name, wav in stems.items():
                sf.write(stem_dir / f"{name}.flac", wav.cpu().numpy().T, BASE_SR)
    del sep


def stage_beats(tracks, device, force, stats):
    from beat_this.inference import Audio2Beats, Audio2Frames
    a2b = Audio2Beats(checkpoint_path="final0", device=device, dbn=False)
    for t in tracks:
        print(f"  {t.dir.name}")
        with StageRun(t, "beats", stats):
            beat_logits, down_logits = Audio2Frames.__call__(a2b, mono(t.audio, BASE_SR), BASE_SR)
            raw_beats, raw_downbeats = a2b.frames2beats(beat_logits, down_logits)
            raw_beats, raw_downbeats = np.asarray(raw_beats), np.asarray(raw_downbeats)
            beat_prob = torch.sigmoid(beat_logits.float()).cpu().numpy()
            down_prob = torch.sigmoid(down_logits.float()).cpu().numpy()
            grid = fit_grid(raw_beats, beat_prob, down_prob, t.data["duration"])
            beats, downbeats = (grid["beats"], grid["downbeats"]) if grid["used"] else (raw_beats, raw_downbeats)
            ibi = np.diff(beats)
            # Beats per bar between consecutive downbeats → meter.
            per_bar = np.diff(np.searchsorted(beats, downbeats - 1e-3))
            meter = int(np.bincount(per_bar).argmax()) if len(per_bar) else None
            bar_bpm = [round(60.0 / float(np.median(np.diff(beats[(beats >= a) & (beats < b + 1e-3)]))), 2)
                       if ((beats >= a) & (beats < b + 1e-3)).sum() > 1 else None
                       for a, b in zip(downbeats[:-1], downbeats[1:])]
            t.data["tempo"] = {
                "bpm": round(60.0 / float(np.median(ibi)), 2) if len(ibi) else None,
                "bpm_stability": round(float(np.std(60.0 / ibi)), 2) if len(ibi) else None,
                "meter": meter,
                "beats": [round(float(b), 3) for b in beats],
                "downbeats": [round(float(b), 3) for b in downbeats],
                "bar_bpm": bar_bpm,
                "grid": {k: v for k, v in grid.items() if k not in ("beats", "downbeats")},
                "raw_beats": [round(float(b), 3) for b in raw_beats],
                "raw_downbeats": [round(float(b), 3) for b in raw_downbeats],
            }
            write_labels(t.dir / "labels_downbeats.txt",
                         [(d, d, f"bar {i + 1}") for i, d in enumerate(downbeats)])
    del a2b


ACT_FPS = 50           # Beat This frame rate
GRID_WINDOW_S = 16     # window for reporting grid fit quality
GRID_MIN_SCORE = 0.6   # median beat probability at grid points (active windows)
SNAP_BPM = 0.1         # fitted tempos this close to a whole BPM are snapped to it


def grid_scores(prob: np.ndarray, bpms: np.ndarray, duration: float, n_phase: int = 96):
    """For each tempo, the best phase and the mean beat probability at the
    grid points it produces (whole track). Returns (scores, phases)."""
    t = np.arange(len(prob)) / ACT_FPS
    scores, phases = [], []
    for bpm in bpms:
        p = 60.0 / bpm
        ph = np.arange(n_phase) / n_phase * p
        g = ph[:, None] + p * np.arange(int(duration / p))[None, :]
        s = np.interp(g, t, prob, right=0).mean(1)
        scores.append(s.max())
        phases.append(ph[s.argmax()])
    return np.array(scores), np.array(phases)


def fit_grid(raw_beats: np.ndarray, beat_prob: np.ndarray, down_prob: np.ndarray, duration: float) -> dict:
    """Fit one constant-tempo 4/4 grid to Beat This's beat probabilities.

    Beat This's picked beats drop out in beatless breakdowns, flip to half
    time in sparse parts, and sit on a 20 ms frame grid. Its probabilities
    are better, but their timing still shifts by tens of ms with the sound
    (Hard Driver - Spark Inside fits best at 160.09 BPM, yet by ear it is
    exactly 160 with one continuous grid; 160.09 drifts ~60 ms by the end).
    Electronic tracks are made at a fixed DAW tempo, so: search tempo + phase
    over the whole track, snap the tempo to a whole BPM when close, refit
    the phase, and choose the downbeat phase from the downbeat
    probabilities. Used only if the grid fits the track.
    """
    ibi = np.diff(raw_beats)
    if len(ibi) < 16:
        return {"used": False, "reason": "too few beats"}
    bpm0 = 60.0 / float(np.median(ibi))
    # ±8%: picked beats sit on 20 ms frames, so bpm0 can be ~6% off (174 → 166.7).
    coarse = np.arange(bpm0 * 0.92, bpm0 * 1.08, 0.05)
    s, _ = grid_scores(beat_prob, coarse, duration, n_phase=48)
    fine = np.arange(coarse[s.argmax()] - 0.1, coarse[s.argmax()] + 0.1, 0.002)
    s, _ = grid_scores(beat_prob, fine, duration)
    fitted = float(fine[s.argmax()])
    bpm = float(round(fitted)) if abs(fitted - round(fitted)) <= SNAP_BPM else fitted
    _, ph = grid_scores(beat_prob, np.array([bpm]), duration, n_phase=384)
    offset, period = float(ph[0]), 60.0 / bpm

    grid_beats = offset + period * np.arange(int((duration - offset) / period) + 1)
    t = np.arange(len(beat_prob)) / ACT_FPS
    at_grid = np.interp(grid_beats, t, beat_prob)
    # Fit quality per window, over windows that have any beat activity.
    windows = []
    for a in np.arange(0, duration, GRID_WINDOW_S):
        sel = (grid_beats >= a) & (grid_beats < a + GRID_WINDOW_S)
        active = beat_prob[int(a * ACT_FPS):int((a + GRID_WINDOW_S) * ACT_FPS)].max(initial=0) > 0.5
        if sel.any():
            windows.append({"start": round(float(a), 1), "score": round(float(at_grid[sel].mean()), 2),
                            "active": bool(active)})
    active_scores = [w["score"] for w in windows if w["active"]]
    median_score = float(np.median(active_scores)) if active_scores else 0.0
    down_at = np.interp(grid_beats, t, down_prob)
    votes = [float(down_at[k::4].mean()) for k in range(4)]
    phase = int(np.argmax(votes))
    return {
        "used": median_score >= GRID_MIN_SCORE, "bpm": round(bpm, 3), "offset": round(offset, 4),
        "median_window_score": round(median_score, 2), "windows": windows,
        "fitted_bpm": round(fitted, 3), "downbeat_phase_scores": [round(v, 2) for v in votes],
        "beats": grid_beats, "downbeats": grid_beats[phase::4],
    }


def songformer_chunked(model, x: np.ndarray, chunk_s: float, overlap_s: float) -> list[dict]:
    """Run SongFormer on overlapping windows; its memory grows with input
    length (a 3-min input already needs ~7 GB). Each window owns the middle
    of its overlap with its neighbours; boundaries and labels come from the
    window that owns them."""
    sr = 24000
    dur = len(x) / sr
    if not chunk_s or dur <= chunk_s:
        return model(x)
    step = chunk_s - overlap_s
    starts = list(np.arange(0, max(dur - chunk_s, 0) + step, step))
    starts[-1] = max(0.0, dur - chunk_s)  # last window ends at the track end
    segs = []
    for i, s0 in enumerate(starts):
        own_from = 0.0 if i == 0 else (starts[i - 1] + chunk_s + s0) / 2
        own_to = dur if i == len(starts) - 1 else (s0 + chunk_s + starts[i + 1]) / 2
        for seg in model(x[int(s0 * sr):int((s0 + chunk_s) * sr)]):
            a, b = seg["start"] + s0, seg["end"] + s0
            if b <= own_from or a >= own_to:
                continue
            segs.append({"start": max(a, own_from), "end": min(b, own_to), "label": seg["label"]})
        free_gpu()
    # Ownership cuts are not real boundaries: rejoin a segment split by one,
    # keeping the label of the longer piece.
    merged = [segs[0]]
    for seg in segs[1:]:
        prev = merged[-1]
        at_cut = any(abs(seg["start"] - (starts[j - 1] + chunk_s + starts[j]) / 2) < 1e-6
                     for j in range(1, len(starts)))
        if at_cut:
            if seg["end"] - seg["start"] > prev["end"] - prev["start"]:
                prev["label"] = seg["label"]
            prev["end"] = seg["end"]
        else:
            merged.append(seg)
    return merged


def stage_sections(tracks, device, force, stats, chunk_s=150.0, overlap_s=60.0):
    from huggingface_hub import snapshot_download
    from transformers import AutoModel
    local = snapshot_download(repo_id="ASLP-lab/SongFormer",
                              ignore_patterns=["SongFormer.pt", "SongFormer.safetensors"])
    sys.path.append(local)
    os.environ["SONGFORMER_LOCAL_DIR"] = local
    model = AutoModel.from_pretrained(local, trust_remote_code=True, low_cpu_mem_usage=False)
    model.to(device).eval()
    for t in tracks:
        print(f"  {t.dir.name}")
        with StageRun(t, "sections", stats):
            segs = songformer_chunked(model, mono(t.audio, 24000), chunk_s, overlap_s)
            t.data["sections"] = [{"start": round(s["start"], 3), "end": round(s["end"], 3),
                                   "label": s["label"]} for s in segs]
            write_labels(t.dir / "labels_sections.txt",
                         [(s["start"], s["end"], s["label"]) for s in segs])
    del model


def stage_genre(tracks, device, force, stats):
    from transformers import pipeline
    clf = pipeline("audio-classification", model="mtg-upf/discogs-maest-30s-pw-129e",
                   trust_remote_code=True, device=device)
    n_labels = clf.model.config.num_labels
    win = 30 * 16000
    for t in tracks:
        print(f"  {t.dir.name}")
        with StageRun(t, "genre", stats):
            x = mono(t.audio, 16000)
            starts = list(range(0, max(1, len(x) - win + 1), win)) or [0]
            totals: dict[str, float] = {}
            for s in starts:
                for r in clf({"raw": x[s:s + win], "sampling_rate": 16000}, top_k=n_labels):
                    totals[r["label"]] = totals.get(r["label"], 0.0) + r["score"] / len(starts)
            top = sorted(totals.items(), key=lambda kv: -kv[1])[:10]
            t.data["genre"] = {"model": "discogs-maest-30s-pw-129e", "windows": len(starts),
                               "top": [{"label": k, "score": round(v, 4)} for k, v in top]}
    del clf


def stage_mood(tracks, device, force, stats, clap_model):
    from transformers import ClapModel, ClapProcessor
    model = ClapModel.from_pretrained(clap_model).to(device).eval()
    proc = ClapProcessor.from_pretrained(clap_model)
    with torch.no_grad():
        txt = proc(text=[f"{w} music" for w in MOOD_WORDS], return_tensors="pt", padding=True).to(device)
        text_emb = torch.nn.functional.normalize(model.get_text_features(**txt), dim=-1)

    def score(chunks: list[np.ndarray]) -> dict:
        embs = []
        for i in range(0, len(chunks), 8):
            inp = proc(audios=chunks[i:i + 8], sampling_rate=48000, return_tensors="pt").to(device)
            embs.append(torch.nn.functional.normalize(model.get_audio_features(**inp), dim=-1))
        audio_emb = torch.nn.functional.normalize(torch.cat(embs).mean(0), dim=-1)
        # Raw cosine per word. Some words score high on everything, so these
        # only mean something normalized per word across many tracks
        # (see normalize_moods).
        cos = (text_emb @ audio_emb).cpu().numpy()
        return {w: round(float(v), 5) for w, v in zip(MOOD_WORDS, cos)}

    for t in tracks:
        print(f"  {t.dir.name}")
        with StageRun(t, "mood", stats), torch.no_grad():
            x = mono(t.audio, 48000)
            win = 10 * 48000
            chunks = lambda a, b: [x[s:s + win] for s in range(a, max(a + 1, b - win + 1), win)] or [x[a:b]]
            t.data["mood"] = {"model": clap_model, "track_cos": score(chunks(0, len(x)))}
            sections = t.data.get("sections") or []
            t.data["mood"]["sections"] = [
                {"start": s["start"], "label": s["label"],
                 "cos": score(chunks(int(s["start"] * 48000), int(s["end"] * 48000)))}
                for s in sections if s["end"] - s["start"] >= 5
            ]
    del model
    normalize_moods(tracks)


def normalize_moods(tracks):
    """z-score each word's cosine across all tracks in this run, so a mood
    ranks high only if this track scores it higher than other tracks do."""
    rows = [t.data["mood"]["track_cos"] for t in tracks if "mood" in t.data]
    if len(rows) < 3:
        return
    mat = np.array([[r[w] for w in MOOD_WORDS] for r in rows])
    mu, sd = mat.mean(0), mat.std(0) + 1e-9
    for t in tracks:
        m = t.data.get("mood")
        if not m:
            continue
        z = lambda cos: dict(sorted(((w, round(float((cos[w] - mu[i]) / sd[i]), 2))
                                     for i, w in enumerate(MOOD_WORDS)), key=lambda kv: -kv[1]))
        m["track"] = z(m["track_cos"])
        for s in m["sections"]:
            s["top"] = list(z(s["cos"]).items())[:3]
        m["normalized_over"] = len(rows)
        t.save()


def stage_phrases(tracks, device, force, stats):
    """CPU only: per-bar stem energy, phrase grid, and EDM-style event tags."""
    for t in tracks:
        tempo = t.data.get("tempo")
        if not tempo or not (t.dir / "stems" / "drums.flac").exists():
            print(f"  {t.dir.name}: needs stems + beats, skipped")
            continue
        print(f"  {t.dir.name}")
        with StageRun(t, "phrases", stats):
            t.data.update(compute_phrases(t, np.array(tempo["downbeats"])))
            write_labels(t.dir / "labels_phrases.txt",
                         [(p["start"], p["end"], f"{p['section']} {p['bars']}b {'/'.join(p['tags'])}".strip())
                          for p in t.data["phrases"]])
            write_click_track(t, tempo)


def bar_db(signal: np.ndarray, bounds: np.ndarray) -> np.ndarray:
    m = signal.mean(axis=0)
    rms = np.array([np.sqrt(np.mean(m[int(a * BASE_SR):int(b * BASE_SR)] ** 2) + 1e-12)
                    for a, b in zip(bounds[:-1], bounds[1:])])
    db = 20 * np.log10(rms + 1e-9)
    return db - np.percentile(db, 95)  # 0 dB = the stem's loud level in this track


def stable_states(on: np.ndarray) -> np.ndarray:
    """Debounce: a state change only counts once it holds MIN_STATE_BARS."""
    out = on.copy()
    for i in range(1, len(on)):
        if on[i] != out[i - 1] and not np.all(on[i:i + MIN_STATE_BARS] == on[i]):
            out[i] = out[i - 1]
    return out


def compute_phrases(t: Track, downbeats: np.ndarray) -> dict:
    end = t.data.get("duration") or t.audio.shape[1] / BASE_SR
    bounds = np.append(downbeats, end)
    n_bars = len(downbeats)
    energy = {n: bar_db(t.stem(n), bounds) for n in STEM_NAMES}
    energy["mix"] = bar_db(t.audio, bounds)
    on = {n: stable_states(energy[n] > STEM_ON_DB) for n in ("drums", "bass")}

    def bar_at(sec: float) -> int:
        return int(np.clip(np.abs(downbeats - sec).argmin(), 0, n_bars - 1))

    # Phrase boundaries: section starts snapped to the nearest downbeat, then
    # PHRASE_BARS-bar splits from each. (Snapping to a global 4-bar grid was
    # tried and moved a real drop that starts 2 bars off that grid.)
    # Drum/bass on/off changes are point events; they don't cut phrases.
    sections = t.data.get("sections") or [{"start": 0.0, "end": end, "label": "?"}]
    cuts = sorted({0} | {bar_at(s["start"]) for s in sections})
    events = []
    for n in ("drums", "bass"):
        for i in np.nonzero(np.diff(on[n].astype(int)))[0] + 1:
            events.append({"bar": int(i) + 1, "time": round(float(downbeats[i]), 3),
                           "event": f"{n}_{'in' if on[n][i] else 'out'}"})
    filled = []
    for a, b in zip(cuts, cuts[1:] + [n_bars]):
        filled += list(range(a, b, PHRASE_BARS))
    cuts = filled

    phrases = []
    for i, (a, b) in enumerate(zip(cuts, cuts[1:] + [n_bars])):
        mix = energy["mix"][a:b]
        slope = float(np.polyfit(np.arange(len(mix)), mix, 1)[0]) if len(mix) > 2 else 0.0
        prev_tail = energy["mix"][max(0, a - 2):a]
        jump = float(mix[:2].mean() - prev_tail.mean()) if len(prev_tail) else 0.0
        drums_frac = float(on["drums"][a:b].mean())
        tags = []
        if drums_frac < BREAKDOWN_DRUMS:
            tags.append("breakdown")
        prev_drop = bool(phrases) and "drop" in phrases[-1]["tags"]
        if jump >= DROP_JUMP_DB and on["drums"][a] and on["bass"][a] and not prev_drop:
            tags.append("drop")
        if slope >= BUILD_SLOPE_DB:
            tags.append("buildup")
        sec = max((s for s in sections if s["start"] <= downbeats[a] + 0.5),
                  key=lambda s: s["start"], default=sections[0])
        phrases.append({
            "start": round(float(downbeats[a]), 3), "end": round(float(bounds[b]), 3),
            "start_bar": a + 1, "bars": b - a, "section": sec["label"], "tags": tags,
            "mix_db": round(float(mix.mean()), 1), "slope_db_per_bar": round(slope, 2),
            "jump_db": round(jump, 1), "drums_on": round(drums_frac, 2),
            "bass_on": round(float(on["bass"][a:b].mean()), 2),
        })
    return {
        "bar_energy_db": {k: [round(float(v), 1) for v in arr] for k, arr in energy.items()},
        "events": sorted(events, key=lambda e: e["bar"]),
        "phrases": phrases,
    }


def write_click_track(t: Track, tempo: dict):
    """Mix at -6 dB + clicks: high on downbeats, low on beats, chime on phrases."""
    out = t.audio.mean(axis=0) * 0.5
    n = int(0.03 * BASE_SR)
    env = np.exp(-np.arange(n) / (0.006 * BASE_SR))

    def click(sec: float, freq: float, gain: float, length: int = n):
        i = int(sec * BASE_SR)
        seg = out[i:i + length]
        e = env if length == n else np.exp(-np.arange(length) / (0.08 * BASE_SR))
        seg += gain * np.sin(2 * np.pi * freq * np.arange(len(seg)) / BASE_SR) * e[:len(seg)]

    downs = set(np.round(tempo["downbeats"], 3))
    for b in tempo["beats"]:
        click(b, 1760 if round(b, 3) in downs else 880, 0.4)
    for p in t.data.get("phrases", []):
        click(p["start"], 2637, 0.3, int(0.4 * BASE_SR))
    sf.write(t.dir / "check_clicks.wav", np.clip(out, -1, 1), BASE_SR)


# ------------------------------------------------------------------ summary

def write_summary(tracks: list[Track], stats: dict, out: Path):
    lines = ["# Analysis spike results", ""]
    lines += ["## Stage cost", "", "| track | duration | " + " | ".join(STAGES) + " |",
              "|---|---|" + "---|" * len(STAGES)]
    for t in tracks:
        s = stats.get(t.dir.name, {})
        cell = lambda st: ("ERR" if "error" in s[st] else
                           f"{s[st]['seconds']} s / {s[st].get('peak_vram_gb', '–')} GB") if st in s else "–"
        lines.append(f"| {t.dir.name} | {t.data.get('duration', 0):.0f} s | "
                     + " | ".join(cell(st) for st in STAGES) + " |")
    for t in tracks:
        d = t.data
        lines += ["", f"## {t.dir.name}", ""]
        for st, e in stats.get(t.dir.name, {}).items():
            if "error" in e:
                lines.append(f"- **{st} failed:** `{e['error']}`")
        if tempo := d.get("tempo"):
            g = tempo.get("grid", {})
            lines.append(f"- Tempo: **{tempo['bpm']} BPM** (± {tempo['bpm_stability']}), "
                         f"meter {tempo['meter']}, {len(tempo['downbeats'])} bars; constant grid "
                         f"{'used' if g.get('used') else 'NOT used'} ({g.get('bpm')} BPM, fitted {g.get('fitted_bpm')}, median window "
                         f"score {g.get('median_window_score')}, downbeat phase scores {g.get('downbeat_phase_scores')})")
            if g.get("windows"):
                lines.append("  - Grid fit by 16 s window: " + " ".join(
                    f"{fmt(w['start'])}={w['score']:.2f}{'' if w['active'] else '(quiet)'}" for w in g["windows"]))
        if g := d.get("genre"):
            lines.append("- Genre: " + ", ".join(f"{x['label']} ({x['score']:.2f})" for x in g["top"][:5]))
        if m := d.get("mood"):
            lines.append("- Mood (z): " + ", ".join(f"{k} {v:+.1f}" for k, v in list(m["track"].items())[:5]))
        if secs := d.get("sections"):
            lines += ["", "| section | start | end |", "|---|---|---|"]
            lines += [f"| {s['label']} | {fmt(s['start'])} | {fmt(s['end'])} |" for s in secs]
        if ph := d.get("phrases"):
            lines += ["", "| bar | time | bars | section | tags | mix dB | slope | jump | drums | bass |",
                      "|---|---|---|---|---|---|---|---|---|---|"]
            lines += [f"| {p['start_bar']} | {fmt(p['start'])} | {p['bars']} | {p['section']} | "
                      f"{' '.join(p['tags'])} | {p['mix_db']} | {p['slope_db_per_bar']} | {p['jump_db']} | "
                      f"{p['drums_on']} | {p['bass_on']} |" for p in ph]
    (out / "summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    (out / "stats.json").write_text(json.dumps(stats, indent=1))


def fmt(sec: float) -> str:
    return f"{int(sec // 60)}:{sec % 60:05.2f}"


# --------------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("inputs", nargs="+", help="audio files or folders")
    ap.add_argument("--out", default=str(Path(__file__).resolve().parents[2] / "data" / "analysis_spike"))
    ap.add_argument("--stages", default=",".join(STAGES), help=f"comma list from {STAGES}")
    ap.add_argument("--device", default="cuda" if torch.cuda.is_available() else "cpu")
    ap.add_argument("--vram-limit-gb", type=float, default=8.0,
                    help="cap this process's VRAM to emulate the target card (0 = no cap)")
    ap.add_argument("--clap-model", default="laion/larger_clap_general")
    ap.add_argument("--section-chunk-s", type=float, default=150.0,
                    help="SongFormer window length in seconds (0 = whole track at once)")
    ap.add_argument("--force", action="store_true", help="recompute cached stems")
    args = ap.parse_args()

    stages = [s for s in STAGES if s in args.stages.split(",")]
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    if args.device == "cuda" and args.vram_limit_gb:
        total = torch.cuda.get_device_properties(0).total_memory / 1e9
        torch.cuda.set_per_process_memory_fraction(min(1.0, args.vram_limit_gb / total))
        print(f"VRAM capped at {args.vram_limit_gb} GB of {total:.1f} GB")

    tracks = [Track(p, out) for p in find_tracks(args.inputs)]
    stats_path = out / "stats.json"
    stats = json.loads(stats_path.read_text()) if stats_path.exists() else {}
    fns = {"stems": stage_stems, "beats": stage_beats,
           "sections": lambda *a: stage_sections(*a, chunk_s=args.section_chunk_s),
           "genre": stage_genre, "phrases": stage_phrases,
           "mood": lambda *a: stage_mood(*a, clap_model=args.clap_model)}
    for st in stages:
        print(f"[{st}]")
        fns[st](tracks, args.device, args.force, stats)
        free_gpu()
    write_summary(tracks, stats, out)
    print(f"\nwrote {out / 'summary.md'}")


if __name__ == "__main__":
    main()
