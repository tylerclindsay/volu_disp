"""Audio decoding and small helpers shared by the analysis stages."""

from __future__ import annotations

import gc
import subprocess
from pathlib import Path

import numpy as np
import soxr

BASE_SR = 44100
STEM_NAMES = ["drums", "bass", "other", "vocals"]


def load_audio(path: Path) -> np.ndarray:
    """Decode any format with ffmpeg → float32 (2, n) at BASE_SR."""
    cmd = ["ffmpeg", "-v", "error", "-i", str(path), "-f", "f32le",
           "-ac", "2", "-ar", str(BASE_SR), "-"]
    raw = subprocess.run(cmd, capture_output=True, check=True).stdout
    return np.frombuffer(raw, dtype=np.float32).reshape(-1, 2).T.copy()


def mono(audio: np.ndarray, sr: int) -> np.ndarray:
    """Downmix (2, n) BASE_SR audio and resample to sr."""
    m = audio.mean(axis=0)
    return m if sr == BASE_SR else soxr.resample(m, BASE_SR, sr).astype(np.float32)


def free_gpu():
    import torch
    gc.collect()
    if torch.cuda.is_available():
        torch.cuda.empty_cache()
