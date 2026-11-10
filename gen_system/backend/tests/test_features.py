import os

import numpy as np
import pytest
import soundfile as sf

from volu_gen.analysis import features as F
from volu_gen.analysis.audio import BASE_SR, STEM_NAMES
from volu_gen.analysis.features import (ADTOF_FPS, CLASSES, DRUM_CLASSES, ENV_HOP, FEATURES_VERSION, LANES,
                                        N_BANDS, ONSET_HOP, SILENCE, SPEC_HOP, band_edges, band_weights,
                                        compute_features, drum_hits, envelope, loud_level, onset_flux,
                                        read_features, spectrum_width, write_features)
from volu_gen.analysis.pipeline import _needs_features, run_features
from volu_gen.analysis.track import TrackDir, import_track


def sine(n: int, hz: float, amp: float, right: float = 1.0) -> np.ndarray:
    """(2, n) sine; the right channel is `right` × the left."""
    x = amp * np.sin(2 * np.pi * hz * np.arange(n) / BASE_SR)
    return np.stack([x, right * x]).astype(np.float32)


def hits(n: int, times: list[float], hz: float, decay_s: float, amp: float = 0.8,
         noise: bool = False) -> np.ndarray:
    """(2, n) silence with a decaying sine (or noise) hit at each time."""
    rng = np.random.default_rng(0)
    x = np.zeros(n)
    m = int(0.25 * BASE_SR)
    env = amp * np.exp(-np.arange(m) / (decay_s * BASE_SR))
    for t in times:
        i = int(round(t * BASE_SR))
        src = rng.standard_normal(m) if noise else np.sin(2 * np.pi * hz * np.arange(m) / BASE_SR)
        seg = x[i:i + m]
        seg += (env * src)[:len(seg)]
    return np.stack([x, x]).astype(np.float32)


def test_envelope_level_length_and_timing():
    n = 2 * BASE_SR + 100
    a = sine(n, 1000, 0.5)
    a[:, :BASE_SR] = 0  # silent first second
    env = envelope(a, n)
    assert env.shape == (1 + n // ENV_HOP,)
    t = np.arange(len(env)) * ENV_HOP / BASE_SR
    np.testing.assert_allclose(env[(t > 1.01) & (t < 1.99)], 0.5 / np.sqrt(2), rtol=0.01)
    assert (env[t < 0.994] == 0).all()
    # The centered 10 ms window reaches half power about at the step.
    half = t[np.argmax(env > 0.5 / np.sqrt(2) / np.sqrt(2))]
    assert half == pytest.approx(1.0, abs=0.004)


def test_loud_level():
    # Silent frames don't count: the 95th percentile is over the 1000 loud ones.
    env = np.concatenate([np.full(900, 0.1), np.full(100, 0.5), np.zeros(1000)])
    assert loud_level(env) == pytest.approx(0.5)
    assert loud_level(np.zeros(100)) == SILENCE
    assert loud_level(np.full(100, SILENCE / 2)) == SILENCE


def test_band_weights_partition():
    w = band_weights()
    f = np.fft.rfftfreq(F.SPEC_FFT, 1 / BASE_SR)
    e = band_edges()
    assert w.shape == (len(f), N_BANDS) and len(e) == N_BANDS + 1
    inside = (f >= e[0]) & (f < e[-1])
    np.testing.assert_allclose(w[inside].sum(axis=1), 1, atol=1e-6)
    assert (w[~inside] == 0).all()
    assert (w.sum(axis=0) > 0).all()  # every band gets some bins, even the narrow low ones


@pytest.mark.parametrize("band", [0, 7, 15, 23])
def test_spectrum_peaks_in_the_sine_band(band):
    e = band_edges()
    hz = np.sqrt(e[band] * e[band + 1])
    n = BASE_SR
    spec, width = spectrum_width(sine(n, hz, 0.5), n)
    assert spec.shape == (N_BANDS, 1 + n // SPEC_HOP)
    mid = spec[:, 20:-20]
    assert (mid.argmax(axis=0) == band).all()
    # Band powers sum to the sine's mean square (0.125 → -9 dB).
    total = 10 * np.log10((10 ** (mid / 10)).sum(axis=0))
    leak = 0.5 if band == 0 else 0.1  # the window leaks part of a 45 Hz sine below 40 Hz
    np.testing.assert_allclose(total, 10 * np.log10(0.125), atol=leak)
    np.testing.assert_allclose(width[20:-20], 0, atol=1e-3)  # identical channels: mono


@pytest.mark.parametrize("right, expect", [(1.0, 0.0), (0.0, 0.5), (-1.0, 1.0)])
def test_width(right, expect):
    n = BASE_SR
    a = sine(n, 440, 0.5, right)
    a[:, :n // 2] = 0
    _, width = spectrum_width(a, n)
    t = np.arange(len(width)) * SPEC_HOP / BASE_SR
    np.testing.assert_allclose(width[(t > 0.6) & (t < 0.9)], expect, atol=1e-3)
    assert np.isnan(width[t < 0.4]).all()  # silent frames


def test_spectrum_silence_floor():
    n = BASE_SR // 2
    spec, _ = spectrum_width(np.zeros((2, n), np.float32), n)
    assert (spec == F.DB_FLOOR).all()


def fake_activations(n: int, peaks: dict[str, list[float]]):
    """A stand-in drum model: probability 1 at each given time per class."""
    def act(drums):
        a = np.zeros((1 + n // (BASE_SR // ADTOF_FPS), len(DRUM_CLASSES)), np.float32)
        for c, times in peaks.items():
            a[np.round(np.array(times) * ADTOF_FPS).astype(int), DRUM_CLASSES.index(c)] = 1
        return a
    return act


def test_drum_hits_snap_to_the_attack_and_merge():
    n = 3 * BASE_SR
    drums = hits(n, [0.5, 1.0], 60, 0.08) + hits(n, [2.0], 60, 0.08, amp=0.2)
    # Model peaks 10-20 ms early (as ADTOF reads), two kick peaks on one
    # attack, and a snare and cymbal that aren't kept.
    act = fake_activations(n, {"kick": [0.49, 0.98, 0.99, 1.99], "snare": [1.01], "cymbal": [1.5]})
    h = drum_hits(drums, n, act)
    assert list(h) == CLASSES == ["kick", "hat"]  # snare and cymbal are found but not kept
    np.testing.assert_allclose(h["kick"][0], [0.5, 1.0, 2.0], atol=0.005)
    # Strength is loudness: the quiet third kick is weaker.
    assert h["kick"][1][2] < 0.5 * h["kick"][1][0]
    assert h["kick"][1].dtype == np.float32 and h["kick"][1].max() <= 1
    assert len(h["hat"][0]) == 0


def test_adtof_finds_synthetic_kicks():
    """The real model: decaying 60 Hz hits are kicks and nothing else."""
    n = 4 * BASE_SR
    kicks = [0.5, 1.0, 1.5, 2.0, 2.5, 3.0]
    act = F.adtof_activations(hits(n, kicks, 60, 0.08))
    assert act.shape == (1 + n // (BASE_SR // ADTOF_FPS), len(DRUM_CLASSES))
    h = drum_hits(hits(n, kicks, 60, 0.08), n)
    np.testing.assert_allclose(h["kick"][0], kicks, atol=0.006)
    assert len(h["hat"][0]) == 0


def test_flux_ignores_steady_tone_and_bleed():
    n = 3 * BASE_SR
    a = sine(n, 100, 0.5)
    a[:, :BASE_SR // 2] = 0
    fl, = onset_flux(a, n, [(30, 150)])
    t = np.arange(len(fl)) * ONSET_HOP / BASE_SR
    assert t[np.argmax(fl)] == pytest.approx(0.5, abs=0.012)  # the start
    assert fl[(t > 0.6) & (t < 2.9)].max() < 0.01 * fl.max()  # not the steady tone (nor the cut at the end)
    # A band that never gets above -60 dBFS (stem bleed) has no onsets.
    quiet = hits(n, [0.5, 1.0, 1.5], 60, 0.08, amp=5e-4)
    fl, = onset_flux(quiet, n, [(30, 150)])
    assert (fl == 0).all()


def lanes_for(n: int) -> dict[str, np.ndarray]:
    return {"mix": sine(n, 440, 0.3), "vocals": np.zeros((2, n), np.float32),
            "drums": hits(n, [0.25, 0.75, 1.25], 60, 0.08), "bass": sine(n - 500, 55, 0.4),
            "other": sine(n + 500, 2000, 0.2, 0.0)}


def test_compute_features_shapes_and_lanes():
    n = int(1.5 * BASE_SR) + 33
    lanes = lanes_for(n)
    loaded = []
    f = compute_features(lambda k: loaded.append(k) or lanes[k], n)
    assert loaded == LANES  # each lane loaded once, in order
    assert f["env"].shape == (len(LANES), 1 + n // ENV_HOP)
    assert f["spec"].shape == (len(LANES), N_BANDS, 1 + n // SPEC_HOP)
    assert f["width"].shape == (len(LANES), 1 + n // SPEC_HOP)
    assert list(f["onsets"]) == CLASSES
    vi = LANES.index("vocals")
    assert f["env_ref"][vi] == SILENCE and np.isnan(f["width"][vi]).all()
    # The bass stem is 500 samples short: silence at the end, not an error.
    assert f["env"][LANES.index("bass"), -1] == 0
    np.testing.assert_allclose(f["width"][LANES.index("other"), 10:-10], 0.5, atol=1e-3)
    np.testing.assert_allclose(f["onsets"]["kick"][0], [0.25, 0.75, 1.25], atol=0.012)


def test_chunk_size_does_not_change_result(monkeypatch):
    n = BASE_SR + 777
    lanes = lanes_for(n)
    a = compute_features(lanes.__getitem__, n)
    monkeypatch.setattr(F, "CHUNK", 37)
    b = compute_features(lanes.__getitem__, n)
    for k in ("env", "spec", "width", "env_ref"):
        np.testing.assert_allclose(a[k], b[k], rtol=1e-4, atol=1e-6)
    for c in CLASSES:
        np.testing.assert_array_equal(a["onsets"][c][0], b["onsets"][c][0])


def test_round_trip(tmp_path):
    n = BASE_SR + 5
    f = compute_features(lanes_for(n).__getitem__, n)
    write_features(tmp_path / "features.npz", f)
    g = read_features(tmp_path / "features.npz")
    for k in ("version", "sample_rate", "env_hop", "spec_hop", "n_fft", "n_samples", "lanes", "classes"):
        assert g[k] == f[k]
    assert g["env"].dtype == g["spec"].dtype == g["width"].dtype == np.float16
    np.testing.assert_allclose(g["env"], f["env"], rtol=2e-3, atol=1e-6)
    np.testing.assert_allclose(g["spec"], f["spec"], atol=0.07)
    np.testing.assert_array_equal(g["env_ref"], f["env_ref"])
    np.testing.assert_allclose(g["band_edges"], band_edges())
    assert g["env_t"][1] == ENV_HOP / BASE_SR and len(g["env_t"]) == g["env"].shape[1]
    assert g["spec_t"][1] == SPEC_HOP / BASE_SR and len(g["spec_t"]) == g["spec"].shape[2]
    for c in CLASSES:
        t, s = g["onsets"][c]
        assert t.dtype == np.float64 and s.dtype == np.float32
        np.testing.assert_array_equal(t, f["onsets"][c][0])
    assert F.features_version(tmp_path / "features.npz") == FEATURES_VERSION
    assert F.features_version(tmp_path / "missing.npz") is None


def make_track(tmp_path, n=BASE_SR) -> TrackDir:
    src = tmp_path / "track.wav"
    sf.write(src, sine(n, 440, 0.3).T, BASE_SR)
    t = import_track(src, tmp_path / "data")
    t._audio = sf.read(src, dtype="float32", always_2d=True)[0].T  # no ffmpeg needed
    t.data["source"]["duration"] = n / BASE_SR
    t.save_stems({name: hits(n, [0.2 + 0.1 * i], 60, 0.05) for i, name in enumerate(STEM_NAMES)})
    return t


def test_stage_staleness(tmp_path):
    t = make_track(tmp_path)
    assert _needs_features(t, set())               # missing
    run_features(t)
    assert not _needs_features(t, set())
    assert _needs_features(t, {"features"})        # forced
    later = t.features_path.stat().st_mtime + 5
    os.utime(t.stem_path("drums"), (later, later))  # stems re-computed after
    assert _needs_features(t, set())
    run_features(t)
    os.utime(t.features_path, (later + 5, later + 5))
    assert not _needs_features(t, set())
    f = read_features(t.features_path)
    write_features(t.features_path, {**f, "version": FEATURES_VERSION + 1})
    assert _needs_features(t, set())               # another format version


def test_run_features_reads_the_stems(tmp_path):
    t = make_track(tmp_path)
    run_features(t)
    f = read_features(t.features_path)
    assert f["n_samples"] == t.audio.shape[1]
    np.testing.assert_allclose(f["onsets"]["kick"][0], [0.2 + 0.1 * STEM_NAMES.index("drums")], atol=0.012)
    assert f["env_ref"][LANES.index("mix")] == pytest.approx(0.3 / np.sqrt(2), rel=0.02)
