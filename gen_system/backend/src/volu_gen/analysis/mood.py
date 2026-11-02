"""Library-wide mood normalization (decision 0004 → Mood).

Some CLAP words score high on every track, so a raw cosine means little on
its own. Each word is z-scored across the library: a mood ranks high only
if this track scores it higher than other tracks do. Recompute as the
library grows; analysis.json stores only the raw cosines.
"""

from __future__ import annotations

import numpy as np

MIN_TRACKS = 3  # fewer than this and z-scores are meaningless


def library_z(cos_by_track: dict[str, dict[str, float]]) -> dict[str, dict[str, float]]:
    """{track: {word: cos}} → {track: {word: z}}, each sorted high → low.
    Returns {} below MIN_TRACKS tracks."""
    if len(cos_by_track) < MIN_TRACKS:
        return {}
    words = list(next(iter(cos_by_track.values())))
    keys = list(cos_by_track)
    mat = np.array([[cos_by_track[k][w] for w in words] for k in keys])
    mu, sd = mat.mean(0), mat.std(0) + 1e-9
    z = (mat - mu) / sd
    return {k: dict(sorted(((w, round(float(z[i, j]), 2)) for j, w in enumerate(words)),
                           key=lambda kv: -kv[1]))
            for i, k in enumerate(keys)}
