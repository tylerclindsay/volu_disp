"""Constant-tempo beat grid (decision 0004 → Beat grid). Pure numpy.

Beat This's picked beats drop out in beatless breakdowns, flip to half time
in sparse parts, and sit on a 20 ms frame grid. Its probabilities are
better, but their timing still shifts by tens of ms with the sound (Hard
Driver - Spark Inside fits best at 160.085 BPM, yet by ear it is exactly 160
with one continuous grid; 160.085 drifts ~60 ms by the end). Electronic
tracks are made at a fixed DAW tempo, so: search tempo + phase over the
whole track, snap the tempo to a whole BPM when close, refit the phase, and
choose the downbeat from the downbeat probabilities. No phase jumps.
"""

from __future__ import annotations

import numpy as np

ACT_FPS = 50           # Beat This frame rate
METER = 4              # the grid assumes 4/4
GRID_WINDOW_S = 16     # window for reporting grid fit quality
# Accept the grid if the median beat probability at grid points (over
# windows with beat activity) reaches this. Fragile: Hard Driver's correct
# grid scores exactly 0.60 (decision 0004 → Known gaps).
GRID_MIN_SCORE = 0.6
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


def grid_times(bpm: float, offset: float, duration: float, meter: int = METER):
    """Beats and downbeats of a constant grid with a downbeat at `offset`,
    covering [0, duration]."""
    period = 60.0 / bpm
    first = offset - np.floor(offset / period + 1e-9) * period
    n_back = int(round((offset - first) / period))
    beats = first + period * np.arange(int((duration - first) / period) + 1)
    downbeats = beats[n_back % meter::meter]
    return beats, downbeats


def fit_grid(raw_beats: np.ndarray, beat_prob: np.ndarray, down_prob: np.ndarray,
             duration: float) -> dict:
    """Fit one constant-tempo 4/4 grid to Beat This's probabilities.

    Returns the fit: `used` (passed the acceptance check), `bpm` (after
    snapping), `fitted_bpm`, `offset` (time of the first downbeat), `score`,
    per-window scores, and the downbeat phase votes.
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
    first_beat, period = float(ph[0]), 60.0 / bpm

    grid_beats = first_beat + period * np.arange(int((duration - first_beat) / period) + 1)
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
    score = float(np.median(active_scores)) if active_scores else 0.0
    down_at = np.interp(grid_beats, t, down_prob)
    votes = [float(down_at[k::METER].mean()) for k in range(METER)]
    phase = int(np.argmax(votes))
    return {
        "used": score >= GRID_MIN_SCORE,
        "bpm": round(bpm, 3), "fitted_bpm": round(fitted, 3),
        "offset": round(first_beat + phase * period, 4),
        "score": round(score, 2), "windows": windows,
        "downbeat_phase_scores": [round(v, 2) for v in votes],
    }


def raw_meter(beats: np.ndarray, downbeats: np.ndarray) -> int | None:
    """Most common number of beats between consecutive downbeats."""
    per_bar = np.diff(np.searchsorted(beats, downbeats - 1e-3))
    return int(np.bincount(per_bar).argmax()) if len(per_bar) else None


def build_tempo(fit: dict, raw_beats, raw_downbeats, duration: float,
                override: dict | None = None) -> dict:
    """The analysis.json `tempo` block.

    Uses the user override if set, else the fitted grid if it was accepted,
    else Beat This's raw beats (`mode: variable`).
    """
    raw_beats, raw_downbeats = np.asarray(raw_beats, float), np.asarray(raw_downbeats, float)
    if override:
        mode, bpm, offset = "grid", float(override["bpm"]), float(override["offset"])
    elif fit.get("used"):
        mode, bpm, offset = "grid", float(fit["bpm"]), float(fit["offset"])
    else:
        mode = "variable"
    if mode == "grid":
        beats, downbeats = grid_times(bpm, offset, duration)
        meter = METER
    else:
        beats, downbeats = raw_beats, raw_downbeats
        ibi = np.diff(beats)
        bpm = 60.0 / float(np.median(ibi)) if len(ibi) else None
        offset = float(downbeats[0]) if len(downbeats) else None
        meter = raw_meter(beats, downbeats)
    return {
        "mode": mode,
        "bpm": round(bpm, 3) if bpm else None,
        "offset": round(offset, 4) if offset is not None else None,
        "meter": meter,
        "override": override,
        "beats": [round(float(b), 3) for b in beats],
        "downbeats": [round(float(b), 3) for b in downbeats],
        "fit": fit,
        "raw": {"beats": [round(float(b), 3) for b in raw_beats],
                "downbeats": [round(float(b), 3) for b in raw_downbeats]},
    }
