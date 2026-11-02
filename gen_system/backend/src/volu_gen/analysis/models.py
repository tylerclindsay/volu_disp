"""Model wrappers, one per GPU stage. Each loads its model on construction;
the pipeline drops it before loading the next, so peak VRAM is one model's
worth. Heavy imports stay inside the classes so the pure-numpy parts of the
package import without torch."""

from __future__ import annotations

import os
import sys

import numpy as np

from .audio import BASE_SR, free_gpu, mono

# Mood descriptors scored with CLAP, as "<word> music".
MOOD_WORDS = [
    "energetic", "calm", "dark", "euphoric", "aggressive", "dreamy",
    "melancholic", "uplifting", "tense", "playful", "hypnotic", "atmospheric",
]

MODEL_IDS = {
    "stems": "demucs htdemucs",
    "beats": "beat_this final0",
    "sections": "ASLP-lab/SongFormer",
    "genre": "mtg-upf/discogs-maest-30s-pw-129e",
    "mood": "laion/larger_clap_general",
}


class Stems:
    def __init__(self, device: str):
        from demucs.api import Separator
        self.sep = Separator(model="htdemucs", device=device)

    def __call__(self, audio: np.ndarray) -> dict[str, np.ndarray]:
        """(2, n) BASE_SR → {stem: (2, n)}."""
        import random

        import torch
        # demucs shifts the input by a random 0–0.5 s (shifts=1) using the
        # global `random`; unseeded, stems differ run to run enough to flip
        # near-threshold drums/bass states (seen on Skrillex - Kora).
        random.seed(0)
        _, stems = self.sep.separate_tensor(torch.from_numpy(audio), BASE_SR)
        return {name: wav.cpu().numpy() for name, wav in stems.items()}


class Beats:
    def __init__(self, device: str):
        from beat_this.inference import Audio2Beats
        self.a2b = Audio2Beats(checkpoint_path="final0", device=device, dbn=False)

    def __call__(self, audio: np.ndarray) -> dict:
        """Raw picked beats/downbeats and per-frame (50 fps) probabilities."""
        import torch
        from beat_this.inference import Audio2Frames
        beat_logits, down_logits = Audio2Frames.__call__(self.a2b, mono(audio, BASE_SR), BASE_SR)
        raw_beats, raw_downbeats = self.a2b.frames2beats(beat_logits, down_logits)
        return {
            "raw_beats": np.asarray(raw_beats), "raw_downbeats": np.asarray(raw_downbeats),
            "beat_prob": torch.sigmoid(beat_logits.float()).cpu().numpy(),
            "down_prob": torch.sigmoid(down_logits.float()).cpu().numpy(),
        }


SONGFORMER_SR = 24000
SECTION_CHUNK_S = 150.0
SECTION_OVERLAP_S = 60.0


class Sections:
    def __init__(self, device: str):
        from huggingface_hub import snapshot_download
        from transformers import AutoModel
        local = snapshot_download(repo_id=MODEL_IDS["sections"],
                                  ignore_patterns=["SongFormer.pt", "SongFormer.safetensors"])
        if local not in sys.path:
            sys.path.append(local)
        os.environ["SONGFORMER_LOCAL_DIR"] = local
        self.model = AutoModel.from_pretrained(local, trust_remote_code=True, low_cpu_mem_usage=False)
        self.model.to(device).eval()

    def __call__(self, audio: np.ndarray) -> list[dict]:
        """Section boundaries as [{start, end}]; SongFormer's labels are dropped."""
        x = mono(audio, SONGFORMER_SR)
        return chunked_sections(self._run, len(x) / SONGFORMER_SR, x)

    def _run(self, x: np.ndarray) -> list[dict]:
        out = self.model(x)
        free_gpu()
        return out


def chunked_sections(model, dur: float, x: np.ndarray, chunk_s: float = SECTION_CHUNK_S,
                     overlap_s: float = SECTION_OVERLAP_S, sr: int = SONGFORMER_SR) -> list[dict]:
    """Run a section model on overlapping windows; SongFormer's memory grows
    with input length (a 3-min input already needs ~7 GB). Each window owns
    the middle of its overlap with its neighbours; boundaries come from the
    window that owns them, and segments split only by an ownership cut are
    rejoined. `model(x)` returns [{start, end, ...}] in seconds."""
    if dur <= chunk_s:
        segs = [{"start": s["start"], "end": s["end"]} for s in model(x)]
        return [{"start": round(s["start"], 3), "end": round(s["end"], 3)} for s in segs]
    step = chunk_s - overlap_s
    starts = list(np.arange(0, max(dur - chunk_s, 0) + step, step))
    starts[-1] = max(0.0, dur - chunk_s)  # last window ends at the track end
    cuts = [(starts[j - 1] + chunk_s + starts[j]) / 2 for j in range(1, len(starts))]
    segs = []
    for i, s0 in enumerate(starts):
        own_from = 0.0 if i == 0 else cuts[i - 1]
        own_to = dur if i == len(starts) - 1 else cuts[i]
        for seg in model(x[int(s0 * sr):int((s0 + chunk_s) * sr)]):
            a, b = seg["start"] + s0, seg["end"] + s0
            if b <= own_from or a >= own_to:
                continue
            segs.append({"start": max(a, own_from), "end": min(b, own_to)})
    merged = [segs[0]]
    for seg in segs[1:]:
        if any(abs(seg["start"] - c) < 1e-6 for c in cuts):
            merged[-1]["end"] = seg["end"]
        else:
            merged.append(seg)
    return [{"start": round(float(s["start"]), 3), "end": round(float(s["end"]), 3)} for s in merged]


class Genre:
    def __init__(self, device: str):
        from transformers import pipeline
        self.clf = pipeline("audio-classification", model=MODEL_IDS["genre"],
                            trust_remote_code=True, device=device)
        self.n_labels = self.clf.model.config.num_labels

    def __call__(self, audio: np.ndarray) -> dict:
        """Discogs style scores averaged over 30 s windows; top 10."""
        win = 30 * 16000
        x = mono(audio, 16000)
        starts = list(range(0, max(1, len(x) - win + 1), win)) or [0]
        totals: dict[str, float] = {}
        for s in starts:
            for r in self.clf({"raw": x[s:s + win], "sampling_rate": 16000}, top_k=self.n_labels):
                totals[r["label"]] = totals.get(r["label"], 0.0) + r["score"] / len(starts)
        top = sorted(totals.items(), key=lambda kv: -kv[1])[:10]
        return {"model": MODEL_IDS["genre"], "windows": len(starts),
                "top": [{"label": k, "score": round(v, 4)} for k, v in top]}


class Mood:
    def __init__(self, device: str):
        import torch
        from transformers import ClapModel, ClapProcessor
        self.device = device
        self.model = ClapModel.from_pretrained(MODEL_IDS["mood"]).to(device).eval()
        self.proc = ClapProcessor.from_pretrained(MODEL_IDS["mood"])
        with torch.no_grad():
            txt = self.proc(text=[f"{w} music" for w in MOOD_WORDS], return_tensors="pt",
                            padding=True).to(device)
            self.text_emb = torch.nn.functional.normalize(self.model.get_text_features(**txt), dim=-1)

    def __call__(self, audio: np.ndarray) -> dict:
        """Raw cosine per mood word for the whole track (10 s chunks, mean
        embedding). Only meaningful z-scored across the library — see
        volu_gen.analysis.mood."""
        import torch
        x = mono(audio, 48000)
        win = 10 * 48000
        chunks = [x[s:s + win] for s in range(0, max(1, len(x) - win + 1), win)] or [x]
        with torch.no_grad():
            embs = []
            for i in range(0, len(chunks), 8):
                inp = self.proc(audios=chunks[i:i + 8], sampling_rate=48000,
                                return_tensors="pt").to(self.device)
                embs.append(torch.nn.functional.normalize(self.model.get_audio_features(**inp), dim=-1))
            audio_emb = torch.nn.functional.normalize(torch.cat(embs).mean(0), dim=-1)
            cos = (self.text_emb @ audio_emb).cpu().numpy()
        return {"model": MODEL_IDS["mood"], "cos": {w: round(float(v), 5) for w, v in zip(MOOD_WORDS, cos)}}


STAGE_MODELS = {"stems": Stems, "beats": Beats, "sections": Sections, "genre": Genre, "mood": Mood}
