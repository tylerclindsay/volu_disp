"""Waveform peaks for the UI's stem lanes (decision 0006): per bucket min /
max / rms of the mix and each stem, on one linear int16 scale, in a pyramid
of levels that halve the rate. Stored as peaks.npz in the track's data dir.
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

import numpy as np

from .audio import BASE_SR

PEAKS_VERSION = 1
BUCKET = 147        # level-0 samples per bucket: 300 buckets/s, divides BASE_SR
N_LEVELS = 9        # 300 … 1.17 buckets/s
LANES = ["mix", "vocals", "drums", "bass", "other"]
BLOCK = BUCKET * 3000  # 10 s read blocks
SCALE = 32767


def level_rate(k: int, sample_rate: int = BASE_SR, bucket: int = BUCKET) -> float:
    """Buckets per second at level k; bucket i starts at i / rate."""
    return sample_rate / (bucket * 2 ** k)


def level0_stats(chunks: Iterable[np.ndarray], n_buckets: int):
    """min, max, sum of squares and value count per BUCKET-sample bucket over
    (channels, m) chunks. Every chunk but the last must be a whole number of
    buckets. Buckets past the audio stay empty (count 0)."""
    mn = np.zeros(n_buckets, np.float32)
    mx = np.zeros(n_buckets, np.float32)
    sq = np.zeros(n_buckets, np.float64)
    cnt = np.zeros(n_buckets, np.int64)
    i, partial = 0, False
    for c in chunks:
        m = c.shape[1]
        if m == 0 or i >= n_buckets:
            continue
        assert not partial, "only the last chunk may end in a partial bucket"
        full, rest = divmod(m, BUCKET)
        parts = []
        if full:
            b = c[:, :full * BUCKET].reshape(c.shape[0], full, BUCKET)
            parts.append((b.min(axis=(0, 2)), b.max(axis=(0, 2)),
                          np.square(b, dtype=np.float64).sum(axis=(0, 2)),
                          np.full(full, b.shape[0] * BUCKET)))
        if rest:
            r = c[:, full * BUCKET:]
            parts.append((r.min(keepdims=True)[0], r.max(keepdims=True)[0],
                          np.array([np.square(r, dtype=np.float64).sum()]), np.array([r.size])))
            partial = True
        for p_mn, p_mx, p_sq, p_cnt in parts:
            j = min(i + len(p_mn), n_buckets)
            mn[i:j], mx[i:j], sq[i:j], cnt[i:j] = (a[:j - i] for a in (p_mn, p_mx, p_sq, p_cnt))
            i = j
    return mn, mx, sq, cnt


def halve(mn, mx, sq, cnt):
    """Next level: pairs of buckets merged (odd last bucket kept on its own)."""
    if len(mn) % 2:
        mn, mx = np.append(mn, mn[-1]), np.append(mx, mx[-1])
        sq, cnt = np.append(sq, 0.0), np.append(cnt, 0)
    return (np.minimum(mn[0::2], mn[1::2]), np.maximum(mx[0::2], mx[1::2]),
            sq[0::2] + sq[1::2], cnt[0::2] + cnt[1::2])


def _quantize(mn, mx, sq, cnt) -> np.ndarray:
    rms = np.sqrt(sq / np.maximum(cnt, 1))
    return np.clip(np.round(np.stack([mn, mx, rms]) * SCALE), -SCALE, SCALE).astype(np.int16)


def compute_peaks(lanes: dict[str, Iterable[np.ndarray]], n_samples: int) -> dict:
    """Peaks for each lane (in LANES order) from (channels, m) chunk iterables.
    Bucket counts follow n_samples (the mix length); a shorter stem reads as
    silence at the end, a longer one is cut."""
    n0 = -(-n_samples // BUCKET)
    per_lane = []
    for name in LANES:
        stats = level0_stats(lanes[name], n0)
        levels = []
        for _ in range(N_LEVELS):
            levels.append(_quantize(*stats))
            stats = halve(*stats)
        per_lane.append(levels)
    return {"version": PEAKS_VERSION, "sample_rate": BASE_SR, "bucket": BUCKET,
            "n_samples": int(n_samples), "lanes": list(LANES),
            "levels": [np.stack([lv[k] for lv in per_lane]) for k in range(N_LEVELS)]}


def chunks_of(audio: np.ndarray, block: int = BLOCK):
    """(channels, n) array → (channels, block) views."""
    return (audio[:, s:s + block] for s in range(0, audio.shape[1], block))


def write_peaks(path: Path, peaks: dict):
    tmp = Path(path).with_suffix(".tmp.npz")
    np.savez_compressed(tmp, version=peaks["version"], sample_rate=peaks["sample_rate"],
                        bucket=peaks["bucket"], n_samples=peaks["n_samples"],
                        lanes=np.array(peaks["lanes"]),
                        **{f"level{k}": a for k, a in enumerate(peaks["levels"])})
    tmp.replace(path)


def read_peaks(path: Path) -> dict:
    """Inverse of write_peaks; levels[k] is int16 (lanes, [min, max, rms], n_k)."""
    with np.load(path) as z:
        n = sum(1 for k in z.files if k.startswith("level"))
        return {"version": int(z["version"]), "sample_rate": int(z["sample_rate"]),
                "bucket": int(z["bucket"]), "n_samples": int(z["n_samples"]),
                "lanes": [str(s) for s in z["lanes"]],
                "levels": [z[f"level{k}"] for k in range(n)]}


def peaks_version(path: Path) -> int | None:
    try:
        with np.load(path) as z:
            return int(z["version"])
    except (OSError, KeyError, ValueError):
        return None
