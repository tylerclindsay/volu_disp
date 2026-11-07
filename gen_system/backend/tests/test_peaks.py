import os

import numpy as np
import pytest
import soundfile as sf

from volu_gen.analysis.audio import BASE_SR, STEM_NAMES
from volu_gen.analysis.cache import TrackCache
from volu_gen.analysis.peaks import (BUCKET, LANES, N_LEVELS, SCALE, chunks_of, compute_peaks,
                                     level_rate, read_peaks, write_peaks)
from volu_gen.analysis.pipeline import _needs_peaks, run_peaks


def burst(n: int, start_s: float, end_s: float, amp: float = 0.5, hz: float = 440.0) -> np.ndarray:
    """(2, n) silence with a sine burst from start_s to end_s (right channel at half level)."""
    t = np.arange(n) / BASE_SR
    x = np.where((t >= start_s) & (t < end_s), amp * np.sin(2 * np.pi * hz * t), 0.0)
    return np.stack([x, 0.5 * x]).astype(np.float32)


def peaks_for(audio: dict[str, np.ndarray], n: int, block: int = BUCKET * 1000) -> dict:
    return compute_peaks({k: chunks_of(audio.get(k, np.zeros((2, n), np.float32)), block)
                          for k in LANES}, n)


def test_burst_lands_in_its_buckets_only():
    n = 3 * BASE_SR
    p = peaks_for({"drums": burst(n, 1.0, 1.5)}, n)
    lv = p["levels"][0]
    d = lv[LANES.index("drums")]
    t = np.arange(lv.shape[2]) / level_rate(0)
    inside = (t >= 1.0) & (t + 1 / level_rate(0) <= 1.5)
    outside = (t + 1 / level_rate(0) <= 1.0) | (t >= 1.5)
    assert (d[1, inside] > 0.45 * SCALE).all() and (d[0, inside] < -0.45 * SCALE).all()
    assert (d[:, outside] == 0).all()
    rms = d[2, inside] / SCALE  # both channels: sqrt((0.5² + 0.25²) / 2 / 2)
    np.testing.assert_allclose(rms, np.sqrt((0.25 + 0.0625) / 4), rtol=0.03)
    # Other lanes are silent; values never exceed the sine's amplitude.
    for k in ("mix", "vocals", "bass", "other"):
        assert (lv[LANES.index(k)] == 0).all()
    assert np.abs(lv).max() <= round(0.5 * SCALE)


def test_clipping_and_silence():
    n = BASE_SR
    loud = np.full((2, n), 1.7, np.float32)
    p = peaks_for({"mix": loud}, n)
    for a in p["levels"]:
        assert a.dtype == np.int16 and np.abs(a).max() <= SCALE
        assert (a[LANES.index("mix"), 1] == SCALE).all()
        assert (a[LANES.index("bass")] == 0).all()


@pytest.mark.parametrize("n", [BUCKET * 300, BUCKET * 300 + 1, BUCKET * 300 - 1, 12345, 5])
def test_bucket_counts_and_timing(n):
    p = peaks_for({}, n, block=BUCKET * 7)
    assert len(p["levels"]) == N_LEVELS
    for k, a in enumerate(p["levels"]):
        size = BUCKET * 2 ** k
        assert a.shape == (len(LANES), 3, -(-n // size))
        # bucket i starts at i / rate, and the last one starts before the end
        assert (a.shape[2] - 1) / level_rate(k) < n / BASE_SR <= a.shape[2] / level_rate(k)
    assert level_rate(0) == 300.0 and level_rate(8) == pytest.approx(300 / 256)


def test_partial_last_bucket_uses_its_samples():
    n = BUCKET * 10 + 20
    x = np.zeros((2, n), np.float32)
    x[:, -20:] = 0.8  # only the last, partial bucket is loud
    a = peaks_for({"mix": x}, n, block=BUCKET * 4)["levels"][0][LANES.index("mix")]
    assert a.shape[1] == 11
    assert a[1, -1] == round(0.8 * SCALE) and a[2, -1] == round(0.8 * SCALE)
    assert (a[:, :-1] == 0).all()


def test_block_size_does_not_change_result():
    rng = np.random.default_rng(0)
    n = BUCKET * 1000 + 77
    x = (rng.standard_normal((2, n)) * 0.2).astype(np.float32)
    a = peaks_for({"other": x}, n, block=BUCKET * 3)["levels"]
    b = peaks_for({"other": x}, n, block=BUCKET * 999)["levels"]
    for la, lb in zip(a, b):
        np.testing.assert_array_equal(la, lb)


def test_levels_consistent_with_finer_level():
    rng = np.random.default_rng(1)
    n = BUCKET * 2 ** N_LEVELS * 3 + 1234  # odd bucket counts on several levels
    x = (rng.standard_normal((2, n)) * 0.3 * np.linspace(0, 1, n)).astype(np.float32)
    lv = peaks_for({"mix": x}, n)["levels"]
    for k in range(1, N_LEVELS):
        fine, coarse = lv[k - 1][0].astype(np.int64), lv[k][0].astype(np.int64)
        m = fine.shape[1]
        assert coarse.shape[1] == -(-m // 2)
        for i in range(coarse.shape[1]):
            f = fine[:, 2 * i:2 * i + 2]
            assert coarse[0, i] == f[0].min() and coarse[1, i] == f[1].max()
            # rms is the sample-weighted mean of squares (only rounding apart)
            assert f[2].min() - 1 <= coarse[2, i] <= f[2].max() + 1
        # direct rms over the whole coarse bucket matches
        size = BUCKET * 2 ** k
        seg = x[:, :size]
        assert abs(coarse[2, 0] - np.sqrt(np.mean(seg.astype(np.float64) ** 2)) * SCALE) <= 1


def test_round_trip(tmp_path):
    n = BASE_SR * 2 + 99
    p = peaks_for({"vocals": burst(n, 0.2, 0.9)}, n)
    write_peaks(tmp_path / "peaks.npz", p)
    q = read_peaks(tmp_path / "peaks.npz")
    for k in ("version", "sample_rate", "bucket", "n_samples", "lanes"):
        assert q[k] == p[k]
    assert len(q["levels"]) == N_LEVELS
    for a, b in zip(p["levels"], q["levels"]):
        assert b.dtype == np.int16
        np.testing.assert_array_equal(a, b)


def make_track(tmp_path, n=BASE_SR * 2) -> TrackCache:
    src = tmp_path / "track.wav"
    sf.write(src, burst(n, 0.5, 1.0).T, BASE_SR)
    t = TrackCache(src, tmp_path / "data")
    t._audio = sf.read(src, dtype="float32", always_2d=True)[0].T  # no ffmpeg needed
    t.data["source"]["duration"] = n / BASE_SR
    t.save_stems({name: burst(n, 0.1 * i, 0.1 * i + 0.3) for i, name in enumerate(STEM_NAMES)})
    return t


def test_stale_when_stems_change(tmp_path):
    t = make_track(tmp_path)
    assert _needs_peaks(t, set())               # missing
    run_peaks(t)
    assert not _needs_peaks(t, set())
    assert _needs_peaks(t, {"peaks"})           # forced
    # Stems re-computed after the peaks → stale.
    later = t.peaks_path.stat().st_mtime + 5
    os.utime(t.stem_path("bass"), (later, later))
    assert _needs_peaks(t, set())
    run_peaks(t)
    os.utime(t.peaks_path, (later + 5, later + 5))
    assert not _needs_peaks(t, set())


def test_stale_on_other_version(tmp_path):
    t = make_track(tmp_path)
    run_peaks(t)
    p = read_peaks(t.peaks_path)
    write_peaks(t.peaks_path, {**p, "version": p["version"] + 1})
    assert _needs_peaks(t, set())


def test_run_peaks_matches_stems(tmp_path):
    t = make_track(tmp_path)
    run_peaks(t)
    p = read_peaks(t.peaks_path)
    lv = p["levels"][0]
    assert p["n_samples"] == t.audio.shape[1]
    for i, name in enumerate(STEM_NAMES):
        d = lv[LANES.index(name)]
        on = np.flatnonzero(d[1] > 0)
        # FLAC stems are 16-bit, so only the burst's buckets are non-zero
        assert on[0] / level_rate(0) == pytest.approx(0.1 * i, abs=1 / level_rate(0))
        assert (on[-1] + 1) / level_rate(0) == pytest.approx(0.1 * i + 0.3, abs=1 / level_rate(0))
