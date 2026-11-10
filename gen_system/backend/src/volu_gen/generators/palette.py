"""Track palettes (docs/generators.md → Director).

One palette per track, chosen by the director from the track's mood (or
picked by the user) and passed to every method, so method changes stay
visually coherent. Plans store colors as sRGB hex; methods get them as
linear RGB (`RenderContext.colors`).
"""

from __future__ import annotations

from typing import Annotated

import numpy as np
from pydantic import StringConstraints

HexColor = Annotated[str, StringConstraints(pattern=r"^#[0-9a-fA-F]{6}$")]

# name → (colors, mood words it suits). Mood words are analysis.json's CLAP
# words (decision 0004). Colors *(tune)* by eye on the display.
PALETTES: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "ember":  (("#ff2a00", "#ff7b00", "#ffc400", "#ff0040"), ("aggressive", "tense", "energetic")),
    "ocean":  (("#0040ff", "#00b3ff", "#00ffd5", "#a0f0ff"), ("calm", "atmospheric", "dreamy")),
    "neon":   (("#ff00aa", "#00e5ff", "#ffe600", "#7a00ff"), ("energetic", "playful", "euphoric")),
    "aurora": (("#00ff88", "#00c2ff", "#b400ff", "#ff5ec4"), ("uplifting", "euphoric", "dreamy")),
    "violet": (("#5a00ff", "#b000ff", "#ff00c8", "#3050ff"), ("dark", "hypnotic", "melancholic")),
    "ice":    (("#ffffff", "#a8d8ff", "#4080ff", "#c0a0ff"), ("calm", "melancholic", "atmospheric")),
    "acid":   (("#a0ff00", "#00ff40", "#ffff00", "#00ffd0"), ("hypnotic", "aggressive", "energetic")),
    "sunset": (("#ff4060", "#ff9030", "#ffd040", "#ff60c0"), ("uplifting", "playful", "euphoric")),
}
DEFAULT_PALETTE = "neon"
MOOD_GAIN = 2.0  # palette weight = exp(MOOD_GAIN × mean z of its words) *(tune)*


def mood_scores(cos: dict[str, float]) -> dict[str, float]:
    """Raw CLAP cosines standardized across one track's words. A stand-in
    for the library-wide z-scores (decision 0004) when the caller has none:
    it removes the track's overall level, not each word's bias."""
    if len(cos) < 2:
        return {}
    v = np.array(list(cos.values()), np.float64)
    sd = v.std() or 1.0
    return {k: float((c - v.mean()) / sd) for k, c in cos.items()}


def choose_palette(mood_z: dict[str, float], rng: np.random.Generator) -> str:
    """A palette name, by weighted random choice: palettes whose mood words
    score high are likelier. Draws exactly one number from rng."""
    names = sorted(PALETTES)
    w = np.array([np.exp(MOOD_GAIN * np.mean([mood_z.get(m, 0.0) for m in PALETTES[n][1]]))
                  for n in names])
    c = np.cumsum(w / w.sum())
    return names[min(int(np.searchsorted(c, rng.random(), side="right")), len(names) - 1)]


def hex_to_linear(colors) -> np.ndarray:
    """(n, 3) float32 linear RGB from sRGB hex strings."""
    if not len(colors):
        return np.zeros((0, 3), np.float32)
    c = np.array([[int(h[i:i + 2], 16) for i in (1, 3, 5)] for h in colors], np.float64) / 255
    lin = np.where(c <= 0.04045, c / 12.92, ((c + 0.055) / 1.055) ** 2.4)
    return lin.astype(np.float32)
