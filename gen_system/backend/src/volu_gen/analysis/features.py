"""Stem features for generators (decision 0012): per-lane RMS envelopes,
band spectra and stereo width at fixed rates, and onset lists per class.
Drum onsets come from the ADTOF drum transcription model on the drums stem
(decision 0016). All times are seconds in the BASE_SR decode, independent
of the display FPS and the beat grid. Stored as features.npz in the
track's data dir.
"""

from __future__ import annotations

import contextlib
import io
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from numpy.lib.stride_tricks import sliding_window_view
from scipy import fft
from scipy.ndimage import maximum_filter1d, median_filter
from scipy.signal.windows import hann

from .audio import BASE_SR
from .peaks import LANES

FEATURES_VERSION = 2
ENV_HOP = 147           # 300 Hz, the peaks level-0 bucket
ENV_WIN = 441           # 10 ms Hann
SPEC_HOP = 441          # 100 Hz
SPEC_FFT = 4096         # 93 ms: bass resolution, ~90 ms fades
N_BANDS = 24
BAND_LO, BAND_HI = 40.0, 16000.0
ONSET_HOP = 147
ONSET_FFT = 1024
SILENCE = 1e-3          # -60 dBFS RMS: quieter frames are silent (env_ref, width)
DB_FLOOR = -120.0       # spectrum dB of a silent band
ONSET_LAG = 3           # flux frame difference: 10 ms, so the rise outweighs ripple
LOCAL_S = 0.5           # onset thresholds: moving median and moving max over this span
CHUNK = 2048            # STFT frames per block, to bound memory
ADTOF_FPS = 100         # ADTOF activation frames; frame k at k / 100 s
SNAP = (-0.02, 0.04)    # drum hit snap window around an ADTOF peak (it reads ~10-20 ms early)


@dataclass(frozen=True)
class OnsetClass:
    name: str
    source: str             # lane
    lo: float               # band, Hz
    hi: float
    gap: float              # min seconds between onsets
    delta: float = 0.02     # above the moving median, in units of the band's loud magnitude (tune)
    local: float = 0.3      # and at least this fraction of the moving max (tune)


DRUM_CLASSES = ["kick", "snare", "tom", "hat", "cymbal"]  # ADTOF's outputs, in its order
ONSET_CLASSES = [
    OnsetClass("bass", "bass", 0.0, BASE_SR / 2, 0.080),
    OnsetClass("other", "other", 0.0, BASE_SR / 2, 0.050),
    OnsetClass("vocals", "vocals", 0.0, BASE_SR / 2, 0.080),
]
CLASSES = DRUM_CLASSES + [c.name for c in ONSET_CLASSES]
CLASS_SOURCE = {**{c: "drums" for c in DRUM_CLASSES}, **{c.name: c.source for c in ONSET_CLASSES}}


def band_edges() -> np.ndarray:
    """N_BANDS + 1 log-spaced edges, BAND_LO to BAND_HI."""
    return np.geomspace(BAND_LO, BAND_HI, N_BANDS + 1)


def band_weights(n_fft: int = SPEC_FFT, sr: int = BASE_SR) -> np.ndarray:
    """(bins, N_BANDS) triangular weights in log frequency, peaking at each
    band's geometric center; they sum to 1 for every bin between the outer
    edges, so band powers add up to the power in that range."""
    edges = band_edges()
    f = fft.rfftfreq(n_fft, 1 / sr)
    w = np.zeros((len(f), N_BANDS), np.float32)
    rows = np.flatnonzero((f >= edges[0]) & (f < edges[-1]))
    centers = np.log(np.sqrt(edges[:-1] * edges[1:]))
    pos = np.interp(np.log(f[rows]), centers, np.arange(N_BANDS))
    j = np.floor(pos).astype(int)
    frac = pos - j
    w[rows, j] += 1 - frac
    w[rows, np.minimum(j + 1, N_BANDS - 1)] += frac
    return w


def _frames(x: np.ndarray, n: int, hop: int, n_frames: int):
    """Centered frames of 1-D x: frame k is x[k·hop − n//2 :][:n], zeros
    outside x. Yields (k0, (m, n) view) blocks of up to CHUNK frames."""
    pad = n // 2
    xp = np.zeros((n_frames - 1) * hop + n, x.dtype)
    m = min(len(x), len(xp) - pad)
    xp[pad:pad + m] = x[:m]
    v = sliding_window_view(xp, n)[::hop]
    for k0 in range(0, n_frames, CHUNK):
        yield k0, v[k0:k0 + CHUNK]


def _stft_power(audio: np.ndarray, n_fft: int, hop: int, n_frames: int):
    """Centered Hann STFT of a (2, n) lane's mid and side. Yields (k0, pm, ps):
    (m, bins) per-bin power, scaled so a frame's bins sum to its windowed
    mean square. pm + ps is the mean of the two channels' power."""
    w = hann(n_fft, sym=False).astype(np.float32)
    scale = np.full(n_fft // 2 + 1, 2.0 / (n_fft * np.sum(w.astype(np.float64) ** 2)))
    scale[[0, -1]] /= 2
    scale = scale.astype(np.float32)
    mid, side = (audio[0] + audio[1]) * 0.5, (audio[0] - audio[1]) * 0.5
    for (k0, fm), (_, fs) in zip(_frames(mid, n_fft, hop, n_frames), _frames(side, n_fft, hop, n_frames)):
        yield (k0, _power(fft.rfft(fm * w, axis=1, workers=-1), scale),
               _power(fft.rfft(fs * w, axis=1, workers=-1), scale))


def _power(z: np.ndarray, scale: np.ndarray) -> np.ndarray:
    """|z|² · scale, squaring z's float32 view in place (2× faster than z.real, z.imag)."""
    v = z.view(np.float32).reshape(*z.shape, 2)
    np.square(v, out=v)
    p = v[..., 0] + v[..., 1]
    p *= scale
    return p


def _db(p: np.ndarray) -> np.ndarray:
    return 10 * np.log10(np.maximum(p, 10 ** (DB_FLOOR / 10)))


def envelope(audio: np.ndarray, n_samples: int) -> np.ndarray:
    """RMS of both channels over a centered ENV_WIN Hann window; frame k at
    k · ENV_HOP / BASE_SR s."""
    n_frames = 1 + n_samples // ENV_HOP
    p = (audio[0] ** 2 + audio[1] ** 2) * 0.5
    w = hann(ENV_WIN).astype(np.float32)
    w /= w.sum()
    out = np.empty(n_frames, np.float32)
    for k0, f in _frames(p, ENV_WIN, ENV_HOP, n_frames):
        out[k0:k0 + len(f)] = f @ w
    return np.sqrt(np.maximum(out, 0))


def loud_level(env: np.ndarray) -> float:
    """95th percentile of the envelope over non-silent frames; SILENCE for a
    silent lane, so a normalized silent lane stays below 1."""
    loud = env[env > SILENCE]
    return float(np.percentile(loud, 95)) if loud.size else SILENCE


def spectrum_width(audio: np.ndarray, n_samples: int) -> tuple[np.ndarray, np.ndarray]:
    """(N_BANDS, n) band power in dBFS (0 dB = mean square 1) and (n,) stereo
    width side / (mid + side) RMS, NaN where the frame is silent; frame k at
    k · SPEC_HOP / BASE_SR s."""
    n_frames = 1 + n_samples // SPEC_HOP
    wts = band_weights()
    spec = np.empty((N_BANDS, n_frames), np.float32)
    width = np.empty(n_frames, np.float32)
    for k0, pm, ps in _stft_power(audio, SPEC_FFT, SPEC_HOP, n_frames):
        k1 = k0 + len(pm)
        spec[:, k0:k1] = _db((pm + ps) @ wts).T
        m, s = pm.sum(axis=1), ps.sum(axis=1)
        with np.errstate(invalid="ignore", divide="ignore"):
            wd = np.sqrt(s) / (np.sqrt(m) + np.sqrt(s))
        wd[m + s < SILENCE ** 2] = np.nan
        width[k0:k1] = wd
    return spec, width


def onset_flux(audio: np.ndarray, n_samples: int, bands: list[tuple[float, float]]) -> list[np.ndarray]:
    """Magnitude spectral flux per band (sum over the band's bins of the
    half-wave rectified difference to ONSET_LAG frames earlier), from one
    shared STFT; frame k at k · ONSET_HOP / BASE_SR s. Linear magnitude, not
    log: log flux fires on ripple in decaying kicks and ~10 ms early.
    Divided by the band's loud magnitude (95th percentile of its bin sum over
    frames above SILENCE), so 1 is a rise from silence to loud in 10 ms; all
    zero for a band that is never above SILENCE (no onsets from bleed)."""
    n_frames = 1 + n_samples // ONSET_HOP
    f = fft.rfftfreq(ONSET_FFT, 1 / BASE_SR)
    sel = [(f >= lo) & (f < hi) for lo, hi in bands]
    flux = [np.zeros(n_frames, np.float32) for _ in bands]
    msum = [np.zeros(n_frames, np.float32) for _ in bands]
    loud = [np.zeros(n_frames, bool) for _ in bands]
    prev: list[np.ndarray | None] = [None] * len(bands)
    for k0, pm, ps in _stft_power(audio, ONSET_FFT, ONSET_HOP, n_frames):
        p = pm + ps
        k1 = k0 + len(p)
        for i, s in enumerate(sel):
            mag = np.sqrt(p[:, s])
            if prev[i] is None:
                prev[i] = np.repeat(mag[:1], ONSET_LAG, axis=0)
            m = np.concatenate([prev[i], mag])
            flux[i][k0:k1] = np.maximum(m[ONSET_LAG:] - m[:-ONSET_LAG], 0).sum(axis=1)
            msum[i][k0:k1] = mag.sum(axis=1)
            loud[i][k0:k1] = p[:, s].sum(axis=1) > SILENCE ** 2
            prev[i] = m[-ONSET_LAG:]
    for fl, ms, ld in zip(flux, msum, loud):
        if ld.any():
            fl /= np.percentile(ms[ld], 95)
        else:
            fl[:] = 0
    return flux


def pick_onsets(flux: np.ndarray, hop_s: float, gap_s: float, delta: float, local: float,
                local_s: float = LOCAL_S) -> tuple[np.ndarray, np.ndarray]:
    """Local maxima of normalized flux (`onset_flux`) above both the moving
    median + delta and `local` × the moving max, at least gap_s apart (the
    stronger of two close peaks wins). The moving max drops the small bumps
    that follow a hit in its decay. Returns sorted times (s) and strengths:
    peak / 95th percentile of the kept peaks, clipped to 1."""
    if len(flux) < 3:
        return np.zeros(0), np.zeros(0, np.float32)
    size = int(round(local_s / hop_s)) | 1
    thr = np.maximum(median_filter(flux, size=size, mode="nearest") + delta,
                     local * maximum_filter1d(flux, size=size, mode="nearest"))
    c = flux[1:-1]
    cand = np.flatnonzero((c >= flux[:-2]) & (c > flux[2:]) & (c > thr[1:-1])) + 1
    gap = max(1, int(round(gap_s / hop_s)))
    blocked = np.zeros(len(flux), bool)
    keep = []
    for i in cand[np.argsort(-flux[cand], kind="stable")]:
        if not blocked[i]:
            keep.append(i)
            blocked[max(0, i - gap + 1):i + gap] = True
    keep = np.sort(np.array(keep, int))
    return keep * hop_s, _strengths(flux[keep])


def _strengths(peaks: np.ndarray) -> np.ndarray:
    """Peaks / their 95th percentile, clipped to 1."""
    ref = np.percentile(peaks, 95) if len(peaks) else 1.0
    return np.clip(peaks / ref if ref > 0 else peaks, 0, 1).astype(np.float32)


_adtof = None  # (model, spectrogram processor), loaded on first use


def adtof_activations(drums: np.ndarray) -> np.ndarray:
    """(frames, 5) ADTOF Frame_RNN hit probabilities for DRUM_CLASSES at
    ADTOF_FPS, from the mono sum of a (2, n) BASE_SR drums stem. CPU: ~6 s
    for a 6.6-min track."""
    global _adtof
    import torch
    from adtof_pytorch import (calculate_n_bins, create_frame_rnn_model, get_default_weights_path,
                               load_pytorch_weights)
    from adtof_pytorch.audio import create_adtof_processor
    if _adtof is None:
        model = create_frame_rnn_model(calculate_n_bins())
        with contextlib.redirect_stdout(io.StringIO()):  # it prints the weights path
            load_pytorch_weights(model, get_default_weights_path(), strict=True)
        _adtof = model.eval(), create_adtof_processor(sample_rate=BASE_SR, fps=ADTOF_FPS)
    model, proc = _adtof
    x = proc.apply_filterbank(proc.compute_stft(drums.mean(axis=0))).T
    with torch.no_grad():
        return model(torch.from_numpy(np.ascontiguousarray(x[None, :, :, None]))).numpy()[0]


def drum_hits(drums: np.ndarray, n_samples: int,
              activations: Callable[[np.ndarray], np.ndarray] | None = None) -> dict:
    """{class: (times, strengths)} for DRUM_CLASSES. ADTOF decides what hit
    and roughly when (its peak picking and thresholds); each hit then moves
    to the strongest full-band flux peak of the drums stem within SNAP,
    since ADTOF's 10 ms frames read ~10-20 ms early. Strength is that flux
    peak (loudness, not model confidence) / the class's 95th percentile."""
    from adtof_pytorch.post_processing import FRAME_RNN_THRESHOLDS, NotePeakPickingProcessor
    act = (activations or adtof_activations)(drums)
    flux = onset_flux(drums, n_samples, [(0.0, BASE_SR / 2)])[0]
    hop = ONSET_HOP / BASE_SR
    out = {}
    for i, name in enumerate(DRUM_CLASSES):
        picker = NotePeakPickingProcessor(threshold=FRAME_RNN_THRESHOLDS[i], pre_avg=0.1, post_avg=0.01,
                                          pre_max=0.02, post_max=0.01, combine=0.02, fps=ADTOF_FPS)
        frames = set()
        for t, _ in picker.process(act[:, i]):
            k0 = max(0, int((t + SNAP[0]) / hop))
            k1 = min(len(flux), int((t + SNAP[1]) / hop) + 1)
            if k1 > k0:
                frames.add(k0 + int(np.argmax(flux[k0:k1])))  # two hits snapping to one attack stay one
        k = np.array(sorted(frames), int)
        out[name] = (k * hop, _strengths(flux[k]))
    return out


def _fit(audio: np.ndarray, n: int) -> np.ndarray:
    """(2, n): a shorter lane reads as silence at the end, a longer one is cut."""
    if audio.shape[1] >= n:
        return audio[:, :n]
    return np.pad(audio, ((0, 0), (0, n - audio.shape[1])))


def compute_features(load: Callable[[str], np.ndarray], n_samples: int,
                     drum_activations: Callable[[np.ndarray], np.ndarray] | None = None) -> dict:
    """Features for each lane in LANES order; load(lane) returns its (2, n)
    float32 audio and is called once per lane, so one lane is in memory at a
    time. Lengths follow n_samples (the mix length). `drum_activations` is
    the drum model (default `adtof_activations`; tests pass a stand-in)."""
    n_env, n_spec = 1 + n_samples // ENV_HOP, 1 + n_samples // SPEC_HOP
    env = np.zeros((len(LANES), n_env), np.float32)
    env_ref = np.zeros(len(LANES), np.float32)
    spec = np.zeros((len(LANES), N_BANDS, n_spec), np.float32)
    width = np.zeros((len(LANES), n_spec), np.float32)
    onsets = {}
    for li, lane in enumerate(LANES):
        a = _fit(load(lane), n_samples)
        env[li] = envelope(a, n_samples)
        env_ref[li] = loud_level(env[li])
        spec[li], width[li] = spectrum_width(a, n_samples)
        if lane == "drums":
            onsets.update(drum_hits(a, n_samples, drum_activations))
        cls = [c for c in ONSET_CLASSES if c.source == lane]
        if cls:
            for c, fl in zip(cls, onset_flux(a, n_samples, [(c.lo, c.hi) for c in cls])):
                onsets[c.name] = pick_onsets(fl, ONSET_HOP / BASE_SR, c.gap, c.delta, c.local)
        del a
    return {"version": FEATURES_VERSION, "sample_rate": BASE_SR, "env_hop": ENV_HOP,
            "spec_hop": SPEC_HOP, "n_fft": SPEC_FFT, "band_edges": band_edges(),
            "n_samples": int(n_samples), "lanes": list(LANES), "classes": list(CLASSES),
            "env": env, "env_ref": env_ref, "spec": spec, "width": width,
            "onsets": {k: onsets[k] for k in CLASSES}}


def write_features(path: Path, f: dict):
    path = Path(path)
    tmp = path.with_name(path.name + ".tmp")  # a file object, so numpy doesn't append .npz
    onsets = {}
    for k, (t, s) in f["onsets"].items():
        onsets[f"onset_{k}_t"] = np.asarray(t, np.float64)
        onsets[f"onset_{k}_s"] = np.asarray(s, np.float32)
    with open(tmp, "wb") as fh:
        np.savez_compressed(fh, version=f["version"], sample_rate=f["sample_rate"],
                            env_hop=f["env_hop"], spec_hop=f["spec_hop"], n_fft=f["n_fft"],
                            band_edges=np.asarray(f["band_edges"], np.float64),
                            n_samples=f["n_samples"], lanes=np.array(f["lanes"]),
                            classes=np.array(f["classes"]),
                            env=f["env"].astype(np.float16), env_ref=f["env_ref"].astype(np.float32),
                            spec=f["spec"].astype(np.float16), width=f["width"].astype(np.float16),
                            **onsets)
    tmp.replace(path)


def read_features(path: Path) -> dict:
    """Inverse of write_features, plus frame times: `env_t` (n_env,) and
    `spec_t` (n_spec,) in seconds. env, spec and width stay float16;
    `onsets[class]` is (times, strengths)."""
    with np.load(path) as z:
        f = {k: int(z[k]) for k in ("version", "sample_rate", "env_hop", "spec_hop", "n_fft", "n_samples")}
        f["lanes"] = [str(s) for s in z["lanes"]]
        f["classes"] = [str(s) for s in z["classes"]]
        for k in ("band_edges", "env", "env_ref", "spec", "width"):
            f[k] = z[k]
        f["onsets"] = {c: (z[f"onset_{c}_t"], z[f"onset_{c}_s"]) for c in f["classes"]}
    f["env_t"] = np.arange(f["env"].shape[1]) * f["env_hop"] / f["sample_rate"]
    f["spec_t"] = np.arange(f["spec"].shape[2]) * f["spec_hop"] / f["sample_rate"]
    return f


def features_version(path: Path) -> int | None:
    try:
        with np.load(path) as z:
            return int(z["version"])
    except (OSError, KeyError, ValueError):
        return None
