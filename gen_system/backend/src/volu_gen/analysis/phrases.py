"""Phrases, events, and per-bar stem energy (decision 0004 → Phrases and
events). CPU only; depends on the beat grid, so it is re-run whenever the
grid changes (e.g. a user override)."""

from __future__ import annotations

import numpy as np
from scipy.signal import butter, sosfiltfilt

from .audio import BASE_SR, STEM_NAMES

# Rules in dB relative to the stem's loud level; tuned by ear on the spike tracks.
PHRASE_BARS = 8
STEM_ON_DB = -18.0       # a stem counts as "on" in a bar above this level
MIN_STATE_BARS = 2       # on/off changes must hold this long to count
DROP_JUMP_DB = 4.0       # mix jump (2 bars after vs. 2 before) that marks a drop
DROP_ARRIVAL_DB = 2.0    # the drop bar itself is within this of the next bar (not a fill)
# Commented out (decision 0005): fitted to too few cases, not checked on held-out tracks.
# DROP_LEVEL_DB = -3.0     # a drop's first 2 bars reach at least this (0 dB = loud level)
# DROP_HOLD_BARS = 4       # drums stay on in all but one of the drop's first bars
LOW_HZ = 150.0           # "low" lane: drums + bass below this (kick and bass together)
BUILD_SLOPE_DB = 0.35    # dB/bar rise across a phrase that marks a build-up
BREAKDOWN_DRUMS = 0.25   # drums on in < this fraction of bars → breakdown


def bar_db(signal: np.ndarray, bounds: np.ndarray, sr: int = BASE_SR) -> np.ndarray:
    """RMS level per bar of (channels, n) audio, in dB re. the 95th
    percentile bar (0 dB = the signal's loud level in this track)."""
    m = signal.mean(axis=0)
    rms = np.array([np.sqrt(np.mean(m[int(a * sr):int(b * sr)] ** 2) + 1e-12)
                    for a, b in zip(bounds[:-1], bounds[1:])])
    db = 20 * np.log10(rms + 1e-9)
    return db - np.percentile(db, 95)


def stable_states(on: np.ndarray) -> np.ndarray:
    """Debounce: a state change only counts once it holds MIN_STATE_BARS."""
    out = on.copy()
    for i in range(1, len(on)):
        if on[i] != out[i - 1] and not np.all(on[i:i + MIN_STATE_BARS] == on[i]):
            out[i] = out[i - 1]
    return out


def low_band(signal: np.ndarray, sr: int = BASE_SR) -> np.ndarray:
    sos = butter(4, LOW_HZ, "low", fs=sr, output="sos")
    return sosfiltfilt(sos, signal, axis=-1)


def bar_energy(mix: np.ndarray, stems: dict[str, np.ndarray], downbeats: np.ndarray,
               duration: float, sr: int = BASE_SR) -> dict[str, np.ndarray]:
    """Per-bar dB of the mix, each stem, and `low`: drums + bass summed and
    low-passed. demucs splits a kick's low end between drums and bass
    differently from track to track (and with its random shift), so drop
    detection uses the sum, which doesn't care where the split falls."""
    bounds = np.append(downbeats, duration)
    energy = {n: bar_db(stems[n], bounds, sr) for n in STEM_NAMES}
    energy["mix"] = bar_db(mix, bounds, sr)
    energy["low"] = bar_db(low_band(stems["drums"] + stems["bass"], sr), bounds, sr)
    return energy


def mix_jumps(mix: np.ndarray) -> np.ndarray:
    """Per bar: mean mix level of this bar and the next minus the 2 bars before."""
    return np.array([mix[i:i + 2].mean() - mix[max(0, i - 2):i].mean() if i else 0.0
                     for i in range(len(mix))])


def find_drops(energy: dict[str, np.ndarray], on: dict[str, np.ndarray]) -> list[int]:
    """Bars (0-based) where a drop starts, checked at every bar.

    A drop: the mix jumps >= DROP_JUMP_DB, and the low lane and drums are
    on. If the bar isn't at the new level yet (a one-bar fill before the
    drop), the drop moves to the first of the next 2 bars that is, with
    drums and low on. Of drops within 2 bars of each other, the biggest jump
    wins. A jump less than PHRASE_BARS after the previous drop is the same
    drop coming back from a short break.

    Known false drops (decision 0005): a riser into a drop, and one bar of
    drums opening a buildup. The level and drum-hold checks that rejected
    them are commented out below.
    """
    mix = energy["mix"]
    n = len(mix)
    jump = mix_jumps(mix)

    def arrived(i: int) -> bool:
        return i + 1 >= n or mix[i] >= mix[i + 1] - DROP_ARRIVAL_DB

    best: dict[int, float] = {}  # drop bar → biggest jump of the candidates placed there
    for i in range(1, n):
        if not (jump[i] >= DROP_JUMP_DB and on["low"][i] and on["drums"][i]):
            continue
        # if mix[i:i + 2].mean() < DROP_LEVEL_DB:
        #     continue
        # if np.sum(~on["drums"][i:i + DROP_HOLD_BARS]) > 1:
        #     continue
        # Not there yet: move to where the level arrives (kept in place if it never does).
        at = next((j for j in range(i, min(i + 3, n))
                   if arrived(j) and on["low"][j] and on["drums"][j]), i)
        best[at] = max(best.get(at, -np.inf), jump[i])
    drops: list[int] = []
    for i in sorted(best):
        if any(abs(j - i) <= 2 and best[j] > best[i] for j in best):
            continue
        if drops and i - drops[-1] < PHRASE_BARS:
            continue
        drops.append(i)
    return drops


def compute_phrases(energy: dict[str, np.ndarray], downbeats: np.ndarray, duration: float,
                    sections: list[dict]) -> dict:
    """Phrases, events, and section start bars from per-bar energy.

    Phrase boundaries: section starts snapped to the nearest downbeat and
    drop bars (find_drops), then PHRASE_BARS-bar splits from each. (Snapping
    to a global 4-bar grid was tried and moved a real drop that starts 2
    bars off that grid.) Drum/bass on/off changes are point events; they
    don't cut phrases.

    Returns {"sections", "phrases", "events", "bars"} for analysis.json.
    """
    downbeats = np.asarray(downbeats, float)
    bounds = np.append(downbeats, duration)
    n_bars = len(downbeats)
    on = {n: stable_states(energy[n] > STEM_ON_DB) for n in ("drums", "bass", "low")}

    def bar_at(sec: float) -> int:
        return int(np.clip(np.abs(downbeats - sec).argmin(), 0, n_bars - 1))

    sections = sections or [{"start": 0.0, "end": duration}]
    sections = [{**s, "start_bar": bar_at(s["start"]) + 1} for s in sections]
    drops = find_drops(energy, on)
    cuts = sorted({0} | {s["start_bar"] - 1 for s in sections} | set(drops))
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
    for a, b in zip(cuts, cuts[1:] + [n_bars]):
        mix = energy["mix"][a:b]
        slope = float(np.polyfit(np.arange(len(mix)), mix, 1)[0]) if len(mix) > 2 else 0.0
        prev_tail = energy["mix"][max(0, a - 2):a]
        jump = float(mix[:2].mean() - prev_tail.mean()) if len(prev_tail) else 0.0
        drums_frac = float(on["drums"][a:b].mean())
        tags = []
        if drums_frac < BREAKDOWN_DRUMS:
            tags.append("breakdown")
        if a in drops:
            tags.append("drop")
        if slope >= BUILD_SLOPE_DB:
            tags.append("buildup")
        sec = max((i for i, s in enumerate(sections) if s["start"] <= downbeats[a] + 0.5),
                  key=lambda i: sections[i]["start"], default=0)
        phrases.append({
            "start": round(float(downbeats[a]), 3), "end": round(float(bounds[b]), 3),
            "start_bar": a + 1, "bars": b - a, "section": sec, "tags": tags,
            "mix_db": round(float(mix.mean()), 1), "slope_db_per_bar": round(slope, 2),
            "jump_db": round(jump, 1), "drums_on": round(drums_frac, 2),
            "bass_on": round(float(on["bass"][a:b].mean()), 2),
        })
    return {
        "sections": sections,
        "phrases": phrases,
        "events": sorted(events, key=lambda e: e["bar"]),
        "bars": {"energy_db": {k: [round(float(v), 1) for v in arr] for k, arr in energy.items()}},
    }
