import numpy as np
import pytest

from volu_gen.analysis.grid import ACT_FPS, build_tempo, fit_grid, grid_times


def fake_activations(bpm: float, first_beat: float, downbeat_phase: int, duration: float,
                     width: float = 0.02):
    """Beat This-like per-frame probabilities: a peak on every beat, a
    downbeat peak on every 4th, and picked beats rounded to 20 ms frames."""
    t = np.arange(int(duration * ACT_FPS)) / ACT_FPS
    beats = np.arange(first_beat, duration, 60.0 / bpm)
    near = np.abs(t[:, None] - beats[None, :])
    beat_prob = np.exp(-0.5 * (near / width) ** 2).max(1)
    downs = beats[downbeat_phase::4]
    down_prob = np.exp(-0.5 * (np.abs(t[:, None] - downs[None, :]) / width) ** 2).max(1)
    return np.round(beats * ACT_FPS) / ACT_FPS, beat_prob, down_prob, downs


@pytest.mark.parametrize("bpm, expected", [(160.0, 160.0), (160.06, 160.0), (127.5, 127.5)])
def test_fit_snaps_to_whole_bpm_only_when_close(bpm, expected):
    raw, bp, dp, downs = fake_activations(bpm, 0.3, 2, 150.0)
    fit = fit_grid(raw, bp, dp, 150.0)
    assert fit["used"]
    assert fit["bpm"] == pytest.approx(expected, abs=0.01)


def test_fit_finds_downbeat_phase_and_offset():
    raw, bp, dp, downs = fake_activations(174.0, 0.21, 3, 120.0)
    fit = fit_grid(raw, bp, dp, 120.0)
    assert fit["bpm"] == 174.0
    period = 60 / 174
    # offset is a downbeat: within a few ms of the true first downbeat
    assert fit["offset"] == pytest.approx(downs[0], abs=0.005)
    assert np.argmax(fit["downbeat_phase_scores"]) == 3
    assert fit["offset"] < 4 * period


def test_too_few_beats_is_rejected():
    fit = fit_grid(np.arange(5) * 0.5, np.zeros(200), np.zeros(200), 4.0)
    assert not fit["used"]


def test_grid_times_extends_back_from_downbeat():
    beats, downs = grid_times(120.0, 1.75, 10.0)
    assert beats[0] == pytest.approx(0.25)
    assert np.allclose(np.diff(beats), 0.5)
    assert beats[-1] <= 10.0
    assert downs[0] == pytest.approx(1.75)
    assert np.allclose(np.diff(downs), 2.0)


def test_build_tempo_modes():
    fit = {"used": True, "bpm": 120.0, "offset": 0.5}
    raw_b, raw_d = [0.5, 1.0, 1.6, 2.0, 2.5], [0.5, 2.5]
    t = build_tempo(fit, raw_b, raw_d, 10.0)
    assert t["mode"] == "grid" and t["downbeats"][0] == 0.5 and t["meter"] == 4

    t = build_tempo({**fit, "used": False}, raw_b, raw_d, 10.0)
    assert t["mode"] == "variable" and t["beats"] == raw_b and t["downbeats"] == raw_d

    t = build_tempo({**fit, "used": False}, raw_b, raw_d, 10.0, {"bpm": 100.0, "offset": 1.0})
    assert t["mode"] == "grid" and t["bpm"] == 100.0 and t["downbeats"][:2] == [1.0, 3.4]
    assert t["fit"]["bpm"] == 120.0  # the fit is kept so the override can be cleared
