#!/usr/bin/env python3
"""Compression benchmark for animation-file chunks on synthetic patterns.

Produced the Results table in docs/decisions/0008-animation-file-format.md.
Compares zstd (levels 3, 9) and LZ4 on 1 s chunks, with and without the
temporal-delta filter, at prototype (960 px) and large (20k px) sizes.

Usage:
    uv run --no-project --python 3.11 --with numpy --with zstandard --with lz4 \
        python tools/anim_codec_bench.py
"""
import time, numpy as np, zstandard as zstd, lz4.frame as lz4
rng = np.random.default_rng(0)
FPS = 120

def geom(nx, ny, nz):
    pts = []
    for z in range(nz):
        for x in range(nx):
            for y in range(ny):
                pts.append((x*50 + (z%2)*25, y*50 + (z%2)*25, z*25))
    return np.array(pts, np.float32)

def hsv(h):
    h = (h % 1.0)[..., None] * 6
    k = (np.array([5, 3, 1]) + h) % 6
    return np.clip(1 - np.clip(np.minimum(k, 4 - k), 0, 1), 0, 1)

def gen(kind, pos, n):
    P = len(pos); out = np.empty((n, P, 3), np.uint8)
    c = pos - pos.mean(0); r = np.linalg.norm(c, axis=1); span = r.max()
    parts = np.zeros(P)
    for i in range(n):
        t = i / FPS; beat = (t * 128 / 60) % 1
        if kind == "wave":     # smooth travelling hue field
            f = hsv(c[:, 0] / span * 0.5 + t * 0.2) * (0.6 + 0.4*np.sin(c[:, 1]/span*6 + t*3))[:, None]
        elif kind == "pulse":  # beat-synced expanding sphere, exp decay
            shell = np.exp(-((r/span - beat) * 8)**2) * np.exp(-beat * 3)
            f = shell[:, None] * np.array([1, .3, .8])
        elif kind == "sparkle": # random particles with decay (worst case)
            parts *= 0.85; parts[rng.integers(0, P, P // 50)] = 1
            f = parts[:, None] * hsv(np.full(P, t * 0.1))
        elif kind == "dark":   # mostly black, a strobe on each beat
            f = np.ones((P, 3)) * (beat < 0.05)
        out[i] = np.clip(f * 255 + 0.5, 0, 255).astype(np.uint8)
    return out

def delta(fr):  # temporal delta mod 256 within a chunk; first frame raw
    d = fr.copy(); d[1:] = fr[1:] - fr[:-1]; return d

def bench(name, fr):
    raw = fr.nbytes; res = []
    for label, prep in [("raw", lambda a: a), ("delta", delta)]:
        for codec, lvl in [("zstd", 3), ("zstd", 9), ("lz4", 0)]:
            chunks = [prep(fr[i:i+FPS]).tobytes() for i in range(0, len(fr), FPS)]
            if codec == "zstd":
                c = zstd.ZstdCompressor(level=lvl); d = zstd.ZstdDecompressor()
                t0 = time.perf_counter(); comp = [c.compress(x) for x in chunks]; te = time.perf_counter()-t0
                t0 = time.perf_counter(); [d.decompress(x) for x in comp]; td = time.perf_counter()-t0
            else:
                t0 = time.perf_counter(); comp = [lz4.compress(x) for x in chunks]; te = time.perf_counter()-t0
                t0 = time.perf_counter(); [lz4.decompress(x) for x in comp]; td = time.perf_counter()-t0
            # undo delta cost (cumsum mod 256) per chunk
            if label == "delta":
                arr = np.frombuffer(chunks[0], np.uint8).reshape(-1, fr.shape[1], 3)
                t0 = time.perf_counter(); np.cumsum(arr, axis=0, dtype=np.uint8); tu = (time.perf_counter()-t0) * len(chunks)
            else: tu = 0
            n = len(chunks)
            res.append(f"  {label:5} {codec}{lvl if codec=='zstd' else '':<2} ratio {raw/sum(map(len,comp)):6.1f}x  "
                       f"decode {1000*(td+tu)/n:6.2f} ms/s-chunk  encode {1000*te/n:6.1f} ms/chunk")
    print(f"{name}  ({raw/1e6:.0f} MB raw)"); print("\n".join(res))

for gname, g, secs in [("prototype 960px", geom(10, 12, 8), 20), ("large 20k px", geom(40, 25, 20), 5)]:
    print(f"=== {gname} ===")
    for k in ["wave", "pulse", "sparkle", "dark"]:
        bench(k, gen(k, g, secs * FPS))
