"""`Music`: what generators know about a track (docs/generators.md → Inputs).

Wraps `analysis.json`, `features.npz` and, lazily, the audio. Queries are
vectorized over an array of times in seconds and valid at any frame rate:
envelopes and spectra are interpolated between their frames, onsets are
times. Methods never index the analysis arrays themselves.

Bars are numbered from 1 as in `analysis.json`: bar b starts at downbeat
b − 1. Positions are fractional (bar 3.5 is halfway through bar 3) and
extend past the first and last downbeat at the edge bars' lengths, so
grid and variable-tempo tracks (raw beats, decision 0004) read the same.

The API server builds a `Music` for planning and never touches audio there,
so stems are decoded only when a method in the worker asks for samples.
"""

from __future__ import annotations

from collections.abc import Callable

import numpy as np

from ..analysis.audio import BASE_SR, STEM_NAMES
from ..analysis.features import SILENCE, read_features
from ..analysis.track import TrackDir

SAMPLE_LANES = ["mix", *STEM_NAMES]  # lanes `samples` can read; mix is the source audio
DB_FLOOR = -120.0  # env(db=True) of silence


def _frac_index(t, marks: np.ndarray) -> np.ndarray:
    """Fractional index of each time in a sorted list of marks (mark i at
    index i), linear between marks and extrapolated at the edge intervals."""
    if len(marks) < 2:
        raise ValueError("need at least two marks")
    t = np.asarray(t, np.float64)
    j = np.clip(np.searchsorted(marks, t, side="right") - 1, 0, len(marks) - 2)
    return j + (t - marks[j]) / (marks[j + 1] - marks[j])


def _frac_time(x, marks: np.ndarray) -> np.ndarray:
    """Inverse of `_frac_index`."""
    if len(marks) < 2:
        raise ValueError("need at least two marks")
    x = np.asarray(x, np.float64)
    j = np.clip(np.floor(x).astype(np.int64), 0, len(marks) - 2)
    return marks[j] + (x - j) * (marks[j + 1] - marks[j])


def _nearest(ht: np.ndarray, hs: np.ndarray, t: np.ndarray, before: bool):
    """Distance to and strength of the last hit at or before (or the next
    after) each time; inf and 0 where there is none."""
    j = np.searchsorted(ht, t, side="right") - (1 if before else 0)
    ok = (j >= 0) & (j < len(ht))
    if not ok.any():
        return np.full(t.shape, np.inf), np.zeros(t.shape, np.float32)
    jj = np.clip(j, 0, len(ht) - 1)
    dt = np.where(ok, (t - ht[jj]) if before else (ht[jj] - t), np.inf)
    return dt, np.where(ok, hs[jj], 0.0).astype(np.float32)


class Music:
    """A track's analysis, features and audio, as queries over time.

    analysis  the `analysis.json` dict (kept as `self.analysis`, the escape hatch)
    features  `read_features()` output, or None while the stage hasn't run
              (feature queries then raise)
    load      lane ("mix" or a stem) → (2, n) float32 at BASE_SR; None: no audio
    """

    def __init__(self, analysis: dict, features: dict | None = None,
                 load: Callable[[str], np.ndarray] | None = None):
        self.analysis = analysis
        self.features = features
        self._load = load
        self._audio: dict[str, np.ndarray] = {}
        tempo = analysis["tempo"]
        self.mode: str = tempo["mode"]
        self.bpm: float | None = tempo["bpm"]
        self.meter: int = tempo["meter"] or 4
        self.duration: float = float(analysis["source"]["duration"])
        self.beats = np.asarray(tempo["beats"], np.float64)
        self.downbeats = np.asarray(tempo["downbeats"], np.float64)
        self.sections: list[dict] = analysis.get("sections", [])
        self.phrases: list[dict] = analysis.get("phrases", [])
        self.events: list[dict] = analysis.get("events", [])
        self._bar_db = {k: np.asarray(v, np.float64)
                        for k, v in analysis.get("bars", {}).get("energy_db", {}).items()}
        self._phrase_starts = np.array([p["start"] for p in self.phrases], np.float64)
        self._section_starts = np.array([s["start"] for s in self.sections], np.float64)
        if features is not None:
            self._lane = {name: i for i, name in enumerate(features["lanes"])}

    @classmethod
    def from_track(cls, track: TrackDir, *, audio: bool = True) -> Music:
        """A stored track's Music. `audio=False` for planning (the server)."""
        features = read_features(track.features_path) if track.features_path.exists() else None

        def load(lane: str) -> np.ndarray:
            return track.audio if lane == "mix" else track.stem(lane)

        return cls(track.data, features, load if audio else None)

    # -- grid
    @property
    def bar_count(self) -> int:
        """Bars from bar 1 to the last downbeat (the last bar may run past
        the end of the track)."""
        return len(self.downbeats)

    def bar_time(self, bar) -> np.ndarray:
        """Start time of (fractional) bar positions."""
        return _frac_time(np.asarray(bar, np.float64) - 1, self.downbeats)

    def bar_at(self, t) -> np.ndarray:
        """Fractional bar position at each time: 1.0 at the first downbeat."""
        return _frac_index(t, self.downbeats) + 1

    def beat_at(self, t) -> np.ndarray:
        """Fractional beat count at each time, 0 at the first beat."""
        return _frac_index(t, self.beats)

    def beat_phase(self, t) -> np.ndarray:
        """Position within the beat, [0, 1)."""
        return np.mod(self.beat_at(t), 1.0)

    def bar_phase(self, t) -> np.ndarray:
        """Position within the bar, [0, 1)."""
        return np.mod(self.bar_at(t), 1.0)

    def beat_length(self, t) -> np.ndarray:
        """Length of the beat at each time, seconds."""
        j = np.clip(np.floor(self.beat_at(t)).astype(np.int64), 0, len(self.beats) - 2)
        return self.beats[j + 1] - self.beats[j]

    # -- structure
    def section_index(self, t) -> np.ndarray:
        """Index into `sections` at each time (0 before the first)."""
        return np.maximum(np.searchsorted(self._section_starts, t, side="right") - 1, 0)

    def phrase_index(self, t) -> np.ndarray:
        """Index into `phrases` at each time, −1 before the first phrase."""
        return np.searchsorted(self._phrase_starts, t, side="right") - 1

    @property
    def energy_lanes(self) -> list[str]:
        """Lanes with per-bar energy (`analysis.json` → bars.energy_db)."""
        return list(self._bar_db)

    def energy_db(self, lane: str, t) -> np.ndarray:
        """Per-bar energy (dB re. the lane's loud level) at each time,
        interpolated between bar centers and held before the first and
        after the last."""
        v = self._bar_db[lane]
        return np.interp(self.bar_at(t) - 1.5, np.arange(len(v)), v)

    def bar_energy_db(self, lane: str, start_bar: int, end_bar: int) -> np.ndarray:
        """Per-bar energy of bars start_bar … end_bar − 1 (bars missing at
        the track's ends are left out)."""
        v = self._bar_db[lane]
        return v[max(start_bar - 1, 0):max(end_bar - 1, 0)]

    @property
    def genre(self) -> list[dict]:
        return self.analysis.get("genre", {}).get("top", [])

    @property
    def mood(self) -> dict[str, float]:
        """Raw CLAP cosines; z-scoring across the library is the caller's."""
        return self.analysis.get("mood", {}).get("cos", {})

    # -- features (decision 0012)
    def _f(self) -> dict:
        if self.features is None:
            raise RuntimeError("no features.npz for this track (analysis stage `features` not run)")
        return self.features

    def env(self, lane: str, t, db: bool = False) -> np.ndarray:
        """RMS envelope, 1.0 at the lane's loud level (peaks go above), or in
        dB re. that level."""
        f = self._f()
        i = self._lane[lane]
        e = np.interp(t, f["env_t"], f["env"][i].astype(np.float32), right=0.0) / f["env_ref"][i]
        if not db:
            return e
        return np.maximum(20 * np.log10(np.maximum(e, 1e-12)), DB_FLOOR)

    def spectrum(self, lane: str, t) -> np.ndarray:
        """(n, 24) band levels in dB, interpolated between frames."""
        f = self._f()
        s = f["spec"][self._lane[lane]]
        x = np.clip(np.asarray(t, np.float64) * f["sample_rate"] / f["spec_hop"], 0, s.shape[1] - 1)
        j = np.minimum(np.floor(x).astype(np.int64), s.shape[1] - 2)
        a = (x - j)[..., None]
        return ((1 - a) * s[:, j].T + a * s[:, j + 1].T).astype(np.float32)

    def width(self, lane: str, t) -> np.ndarray:
        """Stereo width (side / mid energy) at each time."""
        f = self._f()
        return np.interp(t, f["spec_t"], f["width"][self._lane[lane]].astype(np.float32))

    def width_stats(self, lane: str, t0: float, t1: float) -> float:
        """Mean width over [t0, t1), ignoring silent frames; 0 if all silent."""
        f = self._f()
        i = self._lane[lane]
        k = (f["spec_t"] >= t0) & (f["spec_t"] < t1)
        loud = self.env(lane, f["spec_t"][k]) * f["env_ref"][i] > SILENCE
        w = f["width"][i][k][loud].astype(np.float64)
        return float(w.mean()) if len(w) else 0.0

    def onsets(self, cls: str, t0: float = 0.0, t1: float = np.inf,
               min_strength: float = 0.0) -> tuple[np.ndarray, np.ndarray]:
        """(times, strengths) of `cls` hits in [t0, t1)."""
        t, s = self._f()["onsets"][cls]
        k = (t >= t0) & (t < t1) & (s >= min_strength)
        return t[k], s[k]

    def since(self, cls: str, t, min_strength: float = 0.0) -> tuple[np.ndarray, np.ndarray]:
        """(time since the last hit at or before each time, its strength);
        inf and 0 where there is none. The building block for decays."""
        ht, hs = self.onsets(cls, min_strength=min_strength)
        return _nearest(ht, hs, np.asarray(t, np.float64), before=True)

    def until(self, cls: str, t, min_strength: float = 0.0) -> tuple[np.ndarray, np.ndarray]:
        """(time until the next hit after each time, its strength); inf and 0
        where there is none."""
        ht, hs = self.onsets(cls, min_strength=min_strength)
        return _nearest(ht, hs, np.asarray(t, np.float64), before=False)

    # -- audio samples
    def _samples_of(self, lane: str) -> np.ndarray:
        if lane not in SAMPLE_LANES:
            raise ValueError(f"unknown lane {lane!r}; one of {SAMPLE_LANES}")
        if self._load is None:
            raise RuntimeError("this Music has no audio (built for planning)")
        if lane not in self._audio:
            self._audio[lane] = np.ascontiguousarray(self._load(lane).T, np.float32)
        return self._audio[lane]

    def samples(self, lane: str, t0: float, t1: float) -> np.ndarray:
        """(n, 2) float32 stereo at BASE_SR for [t0, t1); zeros outside the track."""
        a = self._samples_of(lane)
        i0, i1 = round(t0 * BASE_SR), round(t1 * BASE_SR)
        out = np.zeros((max(i1 - i0, 0), 2), np.float32)
        s0, s1 = max(i0, 0), min(i1, len(a))
        if s1 > s0:
            out[s0 - i0:s1 - i0] = a[s0:s1]
        return out

    def windows(self, lane: str, t, length: int) -> np.ndarray:
        """(len(t), length, 2): the `length` samples ending at each time, the
        common case for waveform methods; zeros outside the track."""
        a = self._samples_of(lane)
        ends = np.round(np.asarray(t, np.float64) * BASE_SR).astype(np.int64)
        idx = ends[:, None] - length + np.arange(length)
        ok = (idx >= 0) & (idx < len(a))
        return np.where(ok[..., None], a[np.clip(idx, 0, len(a) - 1)], 0.0).astype(np.float32)
