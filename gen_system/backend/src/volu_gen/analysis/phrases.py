"""Phrases, events, and per-bar stem energy (decision 0004 → Phrases and
events). CPU only; depends on the beat grid, so it is re-run whenever the
grid changes (e.g. a user override)."""

from __future__ import annotations

import numpy as np

from .audio import BASE_SR, STEM_NAMES

# Rules in dB relative to the stem's loud level; tuned by ear on the spike tracks.
PHRASE_BARS = 8
STEM_ON_DB = -18.0       # a stem counts as "on" in a bar above this level
MIN_STATE_BARS = 2       # on/off changes must hold this long to count
DROP_JUMP_DB = 4.0       # mix energy jump into a phrase that marks a drop
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


def bar_energy(mix: np.ndarray, stems: dict[str, np.ndarray], downbeats: np.ndarray,
               duration: float, sr: int = BASE_SR) -> dict[str, np.ndarray]:
    bounds = np.append(downbeats, duration)
    energy = {n: bar_db(stems[n], bounds, sr) for n in STEM_NAMES}
    energy["mix"] = bar_db(mix, bounds, sr)
    return energy


def compute_phrases(energy: dict[str, np.ndarray], downbeats: np.ndarray, duration: float,
                    sections: list[dict]) -> dict:
    """Phrases, events, and section start bars from per-bar energy.

    Phrase boundaries: section starts snapped to the nearest downbeat, then
    PHRASE_BARS-bar splits from each. (Snapping to a global 4-bar grid was
    tried and moved a real drop that starts 2 bars off that grid.) Drum/bass
    on/off changes are point events; they don't cut phrases.

    Returns {"sections", "phrases", "events", "bars"} for analysis.json.
    """
    downbeats = np.asarray(downbeats, float)
    bounds = np.append(downbeats, duration)
    n_bars = len(downbeats)
    on = {n: stable_states(energy[n] > STEM_ON_DB) for n in ("drums", "bass")}

    def bar_at(sec: float) -> int:
        return int(np.clip(np.abs(downbeats - sec).argmin(), 0, n_bars - 1))

    sections = sections or [{"start": 0.0, "end": duration}]
    sections = [{**s, "start_bar": bar_at(s["start"]) + 1} for s in sections]
    cuts = sorted({0} | {s["start_bar"] - 1 for s in sections})
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
        # Known gap (decision 0004): drops are only found at phrase starts.
        prev_drop = bool(phrases) and "drop" in phrases[-1]["tags"]
        if jump >= DROP_JUMP_DB and on["drums"][a] and on["bass"][a] and not prev_drop:
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
