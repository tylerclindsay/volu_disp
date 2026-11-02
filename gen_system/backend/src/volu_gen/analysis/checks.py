"""Listening aids for checking analysis by ear: a click track and Audacity
label files, written to `<track cache>/check/`."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import soundfile as sf

from .audio import BASE_SR
from .cache import TrackCache


def write_labels(path: Path, rows: list[tuple[float, float, str]]):
    """Audacity label track (File → Import → Labels): start<TAB>end<TAB>label."""
    path.write_text("".join(f"{s:.3f}\t{e:.3f}\t{l}\n" for s, e, l in rows))


def write_checks(t: TrackCache):
    d = t.data
    out_dir = t.dir / "check"
    out_dir.mkdir(exist_ok=True)
    tempo = d["tempo"]
    write_labels(out_dir / "labels_downbeats.txt",
                 [(x, x, f"bar {i + 1}") for i, x in enumerate(tempo["downbeats"])])
    if secs := d.get("sections"):
        write_labels(out_dir / "labels_sections.txt",
                     [(s["start"], s["end"], f"section {i}") for i, s in enumerate(secs)])
    if phrases := d.get("phrases"):
        write_labels(out_dir / "labels_phrases.txt",
                     [(p["start"], p["end"], f"{p['bars']}b {'/'.join(p['tags'])}".strip())
                      for p in phrases])
    write_click_track(out_dir / "clicks.wav", t.audio, tempo, d.get("phrases", []))


def write_click_track(path: Path, audio: np.ndarray, tempo: dict, phrases: list[dict]):
    """Mix at -6 dB + clicks: high on downbeats, low on beats, chime on
    phrases, and a second, higher chime on drops."""
    out = audio.mean(axis=0) * 0.5
    n = int(0.03 * BASE_SR)
    env = np.exp(-np.arange(n) / (0.006 * BASE_SR))

    def click(sec: float, freq: float, gain: float, length: int = n):
        i = int(sec * BASE_SR)
        seg = out[i:i + length]
        e = env if length == n else np.exp(-np.arange(length) / (0.08 * BASE_SR))
        seg += gain * np.sin(2 * np.pi * freq * np.arange(len(seg)) / BASE_SR) * e[:len(seg)]

    downs = set(np.round(tempo["downbeats"], 3))
    for b in tempo["beats"]:
        click(b, 1760 if round(b, 3) in downs else 880, 0.4)
    for p in phrases:
        click(p["start"], 2637, 0.3, int(0.4 * BASE_SR))
        if "drop" in p["tags"]:
            click(p["start"] + 0.12, 3520, 0.3, int(0.4 * BASE_SR))
    sf.write(path, np.clip(out, -1, 1), BASE_SR)
