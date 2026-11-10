"""Building blocks shared by methods (docs/generators.md → Smoothness).

Everything here is a closed-form function of time, or of per-segment data
sampled on a grid fixed by the segment's span, so methods built on it stay
range-independent.
"""

from __future__ import annotations

import numpy as np

from ..method import SegmentInfo
from ..music import Music

ENV_RATE = 300.0   # Hz: the grid smoothed envelopes are sampled on (the features' envelope rate)
HIT_LOOKBACK = 4   # hits a decay envelope looks back over


def smoothstep(e0: float, e1: float, x) -> np.ndarray:
    u = np.clip((np.asarray(x, np.float32) - e0) / (e1 - e0), 0.0, 1.0)
    return u * u * (3 - 2 * u)


def soft(d, width: float) -> np.ndarray:
    """Brightness from a distance outside a shape: 1 inside (d ≤ 0), easing
    to 0 at d = width. Every edge is drawn with it, about one pitch wide, so
    a moving edge fades across pixels instead of popping."""
    return 1.0 - smoothstep(0.0, width, d)


def hit_env(music: Music, cls: str, t: np.ndarray, decay: float, min_strength: float = 0.0) -> np.ndarray:
    """(n,) float32: an instant attack at each `cls` hit and an exponential
    decay (time constant `decay`, s), scaled by the hit's strength; the
    largest of the last HIT_LOOKBACK hits, so a weak hit never cuts a strong
    one's tail short."""
    ht, hs = music.onsets(cls, min_strength=min_strength)
    t = np.asarray(t, np.float64)
    out = np.zeros(t.shape, np.float32)
    if not len(ht):
        return out
    j = np.searchsorted(ht, t, side="right")
    for k in range(1, HIT_LOOKBACK + 1):
        i = j - k
        ok = i >= 0
        ic = np.maximum(i, 0)
        v = np.where(ok, hs[ic] * np.exp(-(t - ht[ic]) / decay), 0.0)
        out = np.maximum(out, v.astype(np.float32))
    return out


def span(seg: SegmentInfo, margin: float = 1.0) -> tuple[float, float]:
    """The times a method of this segment may be asked for: the segment, half
    of it on each side (the most a crossfade reaches, decision 0018) and a
    margin. Per-segment data covers this span."""
    half = seg.duration / 2
    return seg.start - half - margin, seg.end + half + margin


class Signal:
    """A function of time sampled on a fixed grid over a segment's span:
    linear between samples, held outside. `integral(t)` is its running
    integral from the span's start (e.g. a speed → a travelled angle)."""

    def __init__(self, t: np.ndarray, v: np.ndarray):
        self.t = np.asarray(t, np.float64)
        self.v = np.asarray(v, np.float64)
        dt = np.diff(self.t)
        self._cum = np.concatenate([[0.0], np.cumsum((self.v[1:] + self.v[:-1]) / 2 * dt)])

    def __call__(self, t) -> np.ndarray:
        return np.interp(t, self.t, self.v).astype(np.float32)

    def integral(self, t) -> np.ndarray:
        """∫ from the grid start to t; outside the grid, extended at the edge values."""
        t = np.asarray(t, np.float64)
        out = np.interp(t, self.t, self._cum)
        out = out + np.where(t < self.t[0], (t - self.t[0]) * self.v[0], 0.0)
        return out + np.where(t > self.t[-1], (t - self.t[-1]) * self.v[-1], 0.0)

    @classmethod
    def envelope(cls, music: Music, lane: str, seg: SegmentInfo, tau: float) -> Signal:
        """A lane's RMS envelope (1.0 at its loud level) smoothed by a causal
        exponential of time constant `tau` (s): rises and falls without the
        raw envelope's flicker."""
        t0, t1 = span(seg, margin=1.0 + 5 * tau)
        t = t0 + np.arange(int(np.ceil((t1 - t0) * ENV_RATE)) + 1) / ENV_RATE
        x = music.env(lane, t).astype(np.float64)
        k = np.exp(-np.arange(int(5 * tau * ENV_RATE) + 1) / (tau * ENV_RATE))
        y = np.convolve(x, k / k.sum())[:len(x)]
        y[:len(k)] /= np.cumsum(k / k.sum())[:min(len(k), len(y))]   # no fade-in from 0 at the grid start
        return cls(t, y)


def rotation(axis: np.ndarray, angle: np.ndarray) -> np.ndarray:
    """(n, 3, 3) rotation matrices about one unit axis by each angle (rad)."""
    x, y, z = np.asarray(axis, np.float64) / np.linalg.norm(axis)
    a = np.asarray(angle, np.float64)
    c, s, C = np.cos(a), np.sin(a), 1 - np.cos(a)
    return np.stack([np.stack([c + x * x * C, x * y * C - z * s, x * z * C + y * s], -1),
                     np.stack([y * x * C + z * s, c + y * y * C, y * z * C - x * s], -1),
                     np.stack([z * x * C - y * s, z * y * C + x * s, c + z * z * C], -1)], -2)


def color(ctx, i: int) -> np.ndarray:
    """(3,) float32: palette color i (wrapping), linear RGB."""
    return ctx.colors[i % len(ctx.colors)]


def tint(c: np.ndarray, white: float) -> np.ndarray:
    """A color moved toward white by `white` (0–1), in linear light."""
    return (np.asarray(c, np.float32) * (1 - white) + white).astype(np.float32)
